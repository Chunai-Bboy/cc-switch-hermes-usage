"""发布前自查：扫描仓库内是否残留本机痕迹 / 密钥 / 编译产物。"""
import os
import re
import sys

ROOT = r"D:\GitHub\cc-switch-hermes-usage"
SELF = os.path.basename(__file__)   # 不扫自己，否则会匹配到检测规则本身

# 必须为 0 命中的模式
FORBIDDEN = [
    (r"zyq20", "本机用户名"),
    (r"[A-Za-z]:\\\\", "Windows 绝对路径"),
    (r"C:/Users/", "用户目录路径"),
    (r"AppData", "本机 AppData 路径"),
    (r"qccg|qoder2api|qoder", "本机内网凭据/项目代号"),
    (r"sk-[A-Za-z0-9]{20,}", "疑似 API key"),
    (r"ghp_[A-Za-z0-9]{20,}", "GitHub token"),
    (r"eyJ[A-Za-z0-9_-]{20,}", "JWT"),
    (r"\b\d{1,3}(\.\d{1,3}){3}\b(?!.*localhost)", "IP 地址"),
    (r"127\.0\.0\.1:\d+", "本机端口(应仅出现在文档示例)"),
    (r"__pycache__|\.pyc$", "Python 缓存"),
]

# 允许出现 localhost/127.0.0.1 的文件（本项目确实需要连本地 CC Switch）
ALLOW_LOCALHOST = {"scripts/hermes_usage_sync.py", "references/schema.md",
                   "references/troubleshooting.md", "README.md", "SKILL.md"}

issues = []
scanned = 0

for dirpath, dirnames, filenames in os.walk(ROOT):
    dirnames[:] = [d for d in dirnames if d not in (".git", "__pycache__")]
    for fn in filenames:
        full = os.path.join(dirpath, fn)
        rel = os.path.relpath(full, ROOT)
        # 二进制/缓存直接判定
        if fn == SELF or fn == '.gitignore':
            continue
        if fn.endswith((".pyc", ".db", ".db-wal", ".db-shm", ".exe", ".log")):
            issues.append((rel, "不该提交的文件类型", fn))
            continue
        if "__pycache__" in dirpath:
            issues.append((rel, "Python 缓存目录", dirpath))
            continue
        try:
            with open(full, "r", encoding="utf-8") as f:
                lines = f.readlines()
        except (UnicodeDecodeError, OSError):
            issues.append((rel, "非 UTF-8 文本（可能是二进制残留）", ""))
            continue
        scanned += 1
        for i, line in enumerate(lines, 1):
            for pat, why in FORBIDDEN:
                if re.search(pat, line):
                    if "127.0.0.1" in line and rel.replace("\\", "/") in ALLOW_LOCALHOST:
                        continue
                    issues.append((f"{rel}:{i}", why, line.strip()[:90]))

print(f"扫描文本文件: {scanned} 个")
if issues:
    print(f"\n[FAIL] found {len(issues)} issue(s):\n")
    for where, why, detail in issues:
        print(f"  [{why}] {where}")
        if detail:
            print(f"        {detail}")
    sys.exit(1)
print("\n[PASS] no local paths / secrets / caches / build artifacts")
