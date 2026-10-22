#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""apply_fork_patch —— 给 cc-switch 源码重打"Hermes 用量统计"补丁。

CC Switch 自动更新会覆盖打过补丁的 exe；本脚本对新源码一键重放全部改动，
之后 `pnpm install && pnpm tauri build --no-bundle` 重建即可。

三处改动（对应两个用户可见问题）：
1) src/types/usage.ts                 AppType 与 KNOWN_APP_TYPES 各加 hermes
   -> 用量页「按应用筛选」行出现 Hermes 图标（上游有意不列，见该文件注释）
2) src-tauri/src/services/usage_stats.rs
   provider_name_coalesce 的 CASE 加 '_hermes_session' -> 'Hermes (Session)'
   -> 用量页供应商列显示可读名而非裸 id（照上游 _mcode_session 的模式）
3) src/components/usage/providerLabel.ts
   SESSION_PROVIDER_APPS 加 "Hermes (Session)": "hermes"
   -> 供应商筛选下拉把会话用量折叠到 Hermes 应用

前提：本 skill 的 sync 已不再往 providers 表插 '_hermes_session' 伪记录
（那条会在 Hermes 应用页渲染成幽灵「可添加」卡片），历史遗留由 sync 自动清理。

幂等：重复运行会自动跳过已打过的改动。校验：--verify 只检查不改写。
用法：python apply_fork_patch.py <cc-switch 源码根目录> [--verify]
"""
import io
import os
import re
import sys


def read(p):
    return io.open(p, encoding="utf-8").read()


def write(p, s):
    io.open(p, "w", encoding="utf-8").write(s)


def _line_indent(s, idx):
    """返回 idx 所在行的行首缩进（不含换行符）。"""
    bol = s.rfind("\n", 0, idx) + 1
    return s[bol:idx]


def patch_usage_ts(root, apply):
    """AppType 联合与 KNOWN_APP_TYPES 各加 hermes（都紧跟 opencode 之后）。"""
    p = os.path.join(root, "src", "types", "usage.ts")
    s = read(p)
    notes = []

    union = re.search(r"export type AppType =\s*(.*?);", s, re.S)
    assert union, "AppType 找不到"
    body = union.group(1)
    if '"hermes"' in body:
        notes.append("AppType 已含 hermes")
    else:
        new_body, n = re.subn(r'(\|\s*"opencode")(\s*)',
                              r'\1\n  | "hermes"\2', body, count=1)
        assert n == 1, "AppType 里找不到 | \"opencode\" 锚点"
        s = s[:union.start(1)] + new_body + s[union.end(1):]
        notes.append("AppType + hermes")

    arr = re.search(r"(export const KNOWN_APP_TYPES[^=]*=\s*\[)(.*?)(\])", s, re.S)
    assert arr, "KNOWN_APP_TYPES 找不到"
    body = arr.group(2)
    if '"hermes"' in body:
        notes.append("KNOWN_APP_TYPES 已含 hermes")
    else:
        new_body, n = re.subn(r'(\n(\s*)"opencode",)',
                              lambda m: m.group(1) + "\n" + m.group(2) + '"hermes",',
                              body, count=1)
        assert n == 1, 'KNOWN_APP_TYPES 里找不到 "opencode", 锚点'
        s = s[:arr.start(2)] + new_body + s[arr.end(2):]
        notes.append("KNOWN_APP_TYPES + hermes")

    if apply and notes and not all("已含" in x for x in notes):
        write(p, s)
    return p, notes


def patch_usage_stats_rs(root, apply):
    """provider_name_coalesce 的 CASE 加 _hermes_session 分支。"""
    p = os.path.join(root, "src-tauri", "src", "services", "usage_stats.rs")
    s = read(p)
    if "'_hermes_session'" in s:
        return p, ["CASE 已含 _hermes_session"]
    # 锚：'_mcode_session' 那一行（行尾是 "  \"+换行）
    m = re.search(r"^([ \t]*)WHEN '_mcode_session' THEN '[^']*' \\\n", s, re.M)
    assert m, ("找不到 CASE 锚点（上游可能重构了 provider_name_coalesce，"
               "需要人工更新本脚本）")
    indent = m.group(1)
    line = "%sWHEN '_hermes_session' THEN 'Hermes (Session)' \\\n" % indent
    s = s[:m.end()] + line + s[m.end():]
    if apply:
        write(p, s)
    return p, ["CASE + _hermes_session -> 'Hermes (Session)'"]


def patch_provider_label_ts(root, apply):
    """SESSION_PROVIDER_APPS 加 Hermes 映射。"""
    p = os.path.join(root, "src", "components", "usage", "providerLabel.ts")
    s = read(p)
    if "Hermes (Session)" in s:
        return p, ["映射已含 Hermes"]
    m = re.search(r'^([ \t]*)"MiniMax Code \(Session\)":\s*"mcode",\n', s, re.M)
    assert m, "找不到 SESSION_PROVIDER_APPS 锚点"
    indent = m.group(1)
    line = '%s"Hermes (Session)": "hermes",\n' % indent
    s = s[:m.end()] + line + s[m.end():]
    if apply:
        write(p, s)
    return p, ["providerLabel + Hermes (Session): hermes"]


PATCHES = (patch_usage_ts, patch_usage_stats_rs, patch_provider_label_ts)


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    verify = "--verify" in sys.argv
    root = args[0] if args else "."
    if not os.path.isdir(os.path.join(root, "src-tauri")):
        print("不是 cc-switch 源码根目录:", root)
        return 2
    present_all = True
    for fn in PATCHES:
        try:
            p, notes = fn(root, apply=not verify)
        except (AssertionError, OSError) as e:
            print("[!!] 补丁失败: %s" % e)
            return 3
        print("[%s] %s\n     %s" % ("检查" if verify else "打好",
                                    os.path.relpath(p, root),
                                    "\n     ".join(notes)))
        if not all("已含" in n for n in notes):
            present_all = False
    if verify:
        print("\n全部已打补丁 ✓" if present_all else "\n有缺漏：需运行本脚本（不带 --verify）打补丁 ✗")
        return 0 if present_all else 1
    print("\n重建：cd 源码目录后  pnpm install && pnpm tauri build --no-bundle")
    print("产物 src-tauri\\target\\release\\cc-switch.exe（替换前先备份旧 exe）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
