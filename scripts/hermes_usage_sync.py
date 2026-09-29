#!/usr/bin/env python3
"""
Hermes Agent -> CC Switch 用量同步（skill: cc-switch-hermes-usage 核心脚本）

功能：
  1. 健康检查：Hermes state.db 存在性 / schema 探测 / 多 profile 发现 / CC Switch DB 检查
  2. 数据获取：读取 session_model_usage（cumulative 快照）+ JOIN sessions
  3. 增量语义：本地 state.db 记录上次快照，仅写正增量（delta），防止 cumulative 重复计数
  4. 成本计算：actual_cost > estimated_cost > CC Switch model_pricing 兜底计算
  5. 幂等写入：request_id + INSERT OR IGNORE，可安全重复运行
  6. Provider 注册：_hermes_session (Hermes Agent)，供 usage 页"全部来源"下拉识别

CC Switch 侧零改动：数据写入其原生 proxy_request_logs 表（app_type='hermes'），
由 CC Switch 使用统计页原生展示（已通过 CDP 验证 get_provider_stats /
get_model_stats / get_request_logs / get_usage_data_sources 全链路支持）。

退出码：
  0  成功（含 0 增量）
  10 Hermes state.db 不存在
  11 Hermes schema 不兼容（缺 sessions/session_model_usage 关键表）
  20 CC Switch DB 不存在
  21 CC Switch schema 不兼容（缺 proxy_request_logs/providers）
  30 写入失败
"""

import argparse
import os
import sqlite3
import sys
import time
from pathlib import Path

# ---------------------------------------------------------------- 常量

PROVIDER_ID = "_hermes_session"
PROVIDER_NAME = "Hermes Agent"
APP_TYPE = "hermes"
DATA_SOURCE = "hermes_session"

STATE_DB = os.path.expandvars(r"%LOCALAPPDATA%\cc-switch-hermes-usage\sync-state.db")

# CC Switch proxy_request_logs 列（与 3.20.4 实测一致）
LOG_INSERT = """
INSERT OR IGNORE INTO proxy_request_logs
(request_id, provider_id, app_type, model, request_model,
 input_tokens, output_tokens, cache_read_tokens, cache_creation_tokens,
 input_cost_usd, output_cost_usd, cache_read_cost_usd, cache_creation_cost_usd,
 total_cost_usd, latency_ms, first_token_ms, status_code, error_message,
 session_id, provider_type, is_streaming, cost_multiplier, created_at, data_source)
VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
"""

LOG_COLUMNS = 24


class CheckResult:
    def __init__(self, ok: bool, code: int = 0, msg: str = ""):
        self.ok, self.code, self.msg = ok, code, msg

    def __repr__(self):
        return f"[{'OK' if self.ok else 'FAIL'}] {self.msg}"


# ---------------------------------------------------------------- 健康检查

def table_exists(conn, name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone()
    return row is not None


def columns_of(conn, table: str) -> set:
    try:
        return {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
    except sqlite3.Error:
        return set()


def discover_hermes_dbs() -> list:
    """发现 Hermes state.db：默认 + profiles/*/state.db"""
    base = Path(os.path.expandvars(r"%LOCALAPPDATA%\hermes"))
    dbs = []
    main = base / "state.db"
    if main.exists():
        dbs.append(("default", main))
    profiles = base / "profiles"
    if profiles.is_dir():
        for p in sorted(profiles.iterdir()):
            if p.is_dir() and (p / "state.db").exists():
                dbs.append((p.name, p / "state.db"))
    return dbs


def check_hermes(dbs) -> CheckResult:
    if not dbs:
        return CheckResult(False, 10, "未发现 Hermes state.db（%LOCALAPPDATA%\\hermes）")
    for name, path in dbs:
        try:
            conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        except sqlite3.Error as e:
            return CheckResult(False, 11, f"profile {name}: 无法只读打开 {path}: {e}")
        ok_sessions = table_exists(conn, "sessions")
        ok_usage = table_exists(conn, "session_model_usage")
        if not (ok_sessions and ok_usage):
            conn.close()
            return CheckResult(False, 11,
                               f"profile {name}: 缺 sessions/session_model_usage 表")
        conn.close()
    return CheckResult(True, 0, f"发现 {len(dbs)} 个 Hermes 数据库: "
                                + ", ".join(n for n, _ in dbs))


def check_cc_switch(db_path: str) -> CheckResult:
    if not os.path.exists(db_path):
        return CheckResult(False, 20, f"CC Switch DB 不存在: {db_path}")
    conn = sqlite3.connect(db_path)
    missing = [t for t in ("proxy_request_logs", "providers")
               if not table_exists(conn, t)]
    conn.close()
    if missing:
        return CheckResult(False, 21, f"CC Switch DB 缺表: {missing}")
    return CheckResult(True, 0, f"CC Switch DB 正常: {db_path}")


# ---------------------------------------------------------------- 数据获取

def read_hermes_snapshots(dbs, verbose=False):
    """
    读取所有 profile 的 session_model_usage 快照。
    返回行列表（cumulative 语义，非 delta）。
    容错：缺列时按 0 处理（兼容旧版 hermes，参照上游 PR #6120 兼容策略）。
    """
    rows = []
    for profile, path in dbs:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        ucols = columns_of(conn, "session_model_usage")
        scols = columns_of(conn, "sessions")

        def g(row, col, default=0):
            return row[col] if col in row.keys() else default

        sql = f"""
        SELECT u.session_id,
               u.model,
               COALESCE(u.task, '')          AS task,
               u.api_call_count              AS api_calls,
               u.input_tokens                AS input_tokens,
               u.output_tokens               AS output_tokens,
               u.cache_read_tokens           AS cache_read,
               u.cache_write_tokens          AS cache_write,
               u.estimated_cost_usd          AS est_cost,
               u.actual_cost_usd             AS act_cost,
               u.cost_status                 AS cost_status,
               u.last_seen                   AS last_seen,
               s.billing_provider            AS billing_provider,
               s.billing_mode                AS billing_mode,
               s.cwd                         AS cwd
        FROM session_model_usage u
        LEFT JOIN sessions s ON s.id = u.session_id
        """
        # 老版本兼容：usage 表缺列时按 NULL/0 处理（参照上游 PR #6120 兼容策略）
        if "cost_status" not in ucols:
            sql = sql.replace("u.cost_status", "NULL")
        if "task" not in ucols:
            sql = sql.replace("COALESCE(u.task, '')", "''")
        for col in ("api_call_count", "input_tokens", "output_tokens",
                    "cache_read_tokens", "cache_write_tokens",
                    "estimated_cost_usd", "actual_cost_usd", "last_seen"):
            if col not in ucols:
                sql = sql.replace(f"u.{col}", "NULL")
        # sessions 表缺列时降级为空串
        for col in ("billing_provider", "billing_mode", "cwd"):
            if col not in scols:
                sql = sql.replace(f"s.{col}", "''")

        for r in conn.execute(sql):
            rows.append({
                "profile": profile,
                "session_id": r["session_id"] or "",
                "model": r["model"] or "unknown",
                "task": r["task"] or "",
                "api_calls": int(r["api_calls"] or 0),
                "input": int(r["input_tokens"] or 0),
                "output": int(r["output_tokens"] or 0),
                "cache_read": int(r["cache_read"] or 0),
                "cache_write": int(r["cache_write"] or 0),
                "est_cost": float(r["est_cost"] or 0.0),
                "act_cost": float(r["act_cost"] or 0.0),
                "cost_status": (r["cost_status"] or "").lower(),
                "last_seen": float(r["last_seen"] or 0.0),
                "billing_provider": r["billing_provider"] or "",
                "billing_mode": r["billing_mode"] or "",
                "cwd": r["cwd"] or "",
            })
        conn.close()
    if verbose:
        print(f"[fetch] 读取 {len(rows)} 条 cumulative 快照")
    return rows


# ---------------------------------------------------------------- delta 计算

SNAPSHOT_COLS = ("api_calls", "input", "output", "cache_read", "cache_write",
                 "est_cost", "act_cost")


def compute_deltas(rows, state_conn, verbose=False):
    """基于 state.db 中上次快照计算正增量；返回待写入的 delta 行 + 新快照。"""
    deltas = []
    new_snaps = []
    resets = 0
    for r in rows:
        key = f"{r['profile']}|{r['session_id']}|{r['model']}|{r['task']}"
        prev = state_conn.execute(
            "SELECT " + ",".join(SNAPSHOT_COLS) + " FROM snapshots WHERE key=?",
            (key,)).fetchone()
        prev = dict(zip(SNAPSHOT_COLS, prev)) if prev else None

        if prev is None:
            d = {c: r[c] for c in SNAPSHOT_COLS}
            d["baseline"] = True
        elif all(r[c] >= prev[c] for c in SNAPSHOT_COLS):
            d = {c: r[c] - prev[c] for c in SNAPSHOT_COLS}
            d["baseline"] = False
        else:
            # 计数器被重置/重建（会话归档、DB 替换）：以当前值为新基线
            d = {c: r[c] for c in SNAPSHOT_COLS}
            d["baseline"] = True
            resets += 1
            if verbose:
                print(f"[delta] 检测到计数器重置: {key}")

        if any(d[c] > 0 for c in SNAPSHOT_COLS) or d["baseline"]:
            if any(d[c] > 0 for c in SNAPSHOT_COLS):
                d["row"] = r
                deltas.append(d)
        new_snaps.append((key,) + tuple(r[c] for c in SNAPSHOT_COLS))

    if verbose:
        print(f"[delta] {len(deltas)} 条待写入增量, {resets} 次计数器重置")
    return deltas, new_snaps


# ---------------------------------------------------------------- 成本计算

def price_from_cc_switch(cc_conn, model: str, dinput, doutput, dread, dwrite):
    """用 CC Switch model_pricing 兜底计算成本（每百万 token 美元价）。"""
    row = cc_conn.execute(
        "SELECT input_cost_per_million, output_cost_per_million,"
        " cache_read_cost_per_million, cache_creation_cost_per_million"
        " FROM model_pricing WHERE model_id = ?", (model,)).fetchone()
    if not row:
        return 0.0
    try:
        return (dinput * float(row[0]) + doutput * float(row[1])
                + dread * float(row[2]) + dwrite * float(row[3])) / 1_000_000.0
    except (TypeError, ValueError):
        return 0.0


def pick_cost(d, row):
    """成本优先级：actual（已结算）> estimated > model_pricing 兜底 > 0"""
    if row["cost_status"] in ("actual", "final", "settled", "complete"):
        return d["act_cost"], "actual"
    if d["est_cost"] > 0:
        return d["est_cost"], "estimated"
    if d["act_cost"] > 0:
        return d["act_cost"], "actual"
    return None, "priced"


# ---------------------------------------------------------------- 写入

def ensure_provider(cc_conn):
    cc_conn.execute(
        "INSERT OR IGNORE INTO providers (id, app_type, name, settings_config)"
        " VALUES (?, ?, ?, '{}')", (PROVIDER_ID, APP_TYPE, PROVIDER_NAME))
    cc_conn.execute(
        "UPDATE providers SET name = ? WHERE id = ? AND app_type = ?"
        " AND name IS NOT ?", (PROVIDER_NAME, PROVIDER_ID, APP_TYPE, PROVIDER_NAME))


def write_deltas(cc_conn, deltas, dry=False, verbose=False):
    written = 0
    now = int(time.time())
    for d in deltas:
        row = d["row"]
        cost, cost_kind = pick_cost(d, row)
        if cost is None:
            cost = price_from_cc_switch(cc_conn, row["model"],
                                        d["input"], d["output"],
                                        d["cache_read"], d["cache_write"])
        # 幂等键：profile|session|model|task|last_seen
        rid = (f"hermes:{row['profile']}:{row['session_id']}:{row['model']}:"
               f"{row['task']}:{int(row['last_seen'] * 1000)}")
        created = int(row["last_seen"]) or now
        params = (
            rid, PROVIDER_ID, APP_TYPE, row["model"], row["model"],
            d["input"], d["output"], d["cache_read"], d["cache_write"],
            0, 0, 0, 0, f"{cost:.6f}",
            0, 0, 200, None,
            row["session_id"], row["billing_provider"] or "hermes", 0, "1.0",
            created, DATA_SOURCE,
        )
        assert len(params) == LOG_COLUMNS
        if not dry:
            cc_conn.execute(LOG_INSERT, params)
        written += 1
        if verbose:
            print(f"[write] {rid} in={d['input']} out={d['output']} "
                  f"cr={d['cache_read']} cost=${cost:.6f} ({cost_kind}, "
                  f"{'baseline' if d['baseline'] else 'delta'})")
    return written


def save_snapshots(state_conn, snaps):
    state_conn.executemany(
        "INSERT OR REPLACE INTO snapshots (key, " + ",".join(SNAPSHOT_COLS) + ")"
        " VALUES (?," + ",".join("?" * len(SNAPSHOT_COLS)) + ")", snaps)


# ---------------------------------------------------------------- 主流程

def main():
    ap = argparse.ArgumentParser(description="Hermes -> CC Switch 用量同步")
    ap.add_argument("--cc-db", default=os.path.expanduser(r"~\.cc-switch\cc-switch.db"))
    ap.add_argument("--state-db", default=STATE_DB)
    ap.add_argument("--check", action="store_true", help="仅健康检查")
    ap.add_argument("--dry-run", action="store_true", help="计算但不写入")
    ap.add_argument("--reset-baseline", action="store_true",
                    help="重建基线：删除 CC Switch 中已有 hermes_session 记录与本地状态后重扫"
                         "（用于迁移旧版全量脚本产物，防止双计）")
    ap.add_argument("--verbose", "-v", action="store_true")
    args = ap.parse_args()

    log = print if args.verbose else (lambda *a, **k: None)

    # 1. 健康检查
    dbs = discover_hermes_dbs()
    r1 = check_hermes(dbs)
    log("[check] hermes:", r1)
    r2 = check_cc_switch(args.cc_db)
    log("[check] cc-switch:", r2)
    if not r1.ok:
        print(f"ERROR[{r1.code}] {r1.msg}", file=sys.stderr)
        return r1.code
    if not r2.ok:
        print(f"ERROR[{r2.code}] {r2.msg}", file=sys.stderr)
        return r2.code
    if args.check:
        print(f"OK hermes: {r1.msg}")
        print(f"OK cc-switch: {r2.msg}")
        return 0

    # 2. 数据获取 + 3. delta 计算
    rows = read_hermes_snapshots(dbs, args.verbose)
    os.makedirs(os.path.dirname(args.state_db), exist_ok=True)
    state = sqlite3.connect(args.state_db)
    state.execute("CREATE TABLE IF NOT EXISTS snapshots ("
                  " key TEXT PRIMARY KEY, " + ", ".join(
                      f"{c} REAL DEFAULT 0" for c in SNAPSHOT_COLS) + ")")

    cc = sqlite3.connect(args.cc_db, timeout=10)

    # 迁移保护：有历史记录但无同步状态 -> 旧脚本产物，直接跑会双计
    existing = cc.execute(
        "SELECT COUNT(*) FROM proxy_request_logs WHERE data_source=?",
        (DATA_SOURCE,)).fetchone()[0]
    state_cnt = state.execute("SELECT COUNT(*) FROM snapshots").fetchone()[0]
    if existing > 0 and state_cnt == 0 and not args.reset_baseline \
            and not args.dry_run:
        print(f"ERROR[40] CC Switch 已有 {existing} 条 hermes_session 记录但无同步状态，"
              f"疑似旧版全量脚本产物；直接同步会双计。确认后加 --reset-baseline "
              f"重建基线（先删旧记录再按当前累计值重写）。", file=sys.stderr)
        state.close(); cc.close()
        return 40

    if args.dry_run:
        prev = {r[0] for r in state.execute("SELECT key FROM snapshots")}
        new_keys = {f"{r['profile']}|{r['session_id']}|{r['model']}|{r['task']}"
                    for r in rows} - prev
        print(f"[dry-run] hermes 快照 {len(rows)} 条, 其中 {len(new_keys)} 个新 key "
              f"将建立基线; CC Switch: {args.cc_db}")
        state.close(); cc.close()
        return 0

    if args.reset_baseline:
        print("[reset] 删除 CC Switch 旧 hermes_session 记录并清空本地状态...")
        cc.execute("DELETE FROM proxy_request_logs WHERE data_source=?", (DATA_SOURCE,))
        state.execute("DELETE FROM snapshots")
        cc.commit(); state.commit()

    deltas, snaps = compute_deltas(rows, state, args.verbose)

    # 4. 写入 CC Switch
    try:
        cc.execute("BEGIN IMMEDIATE")
        ensure_provider(cc)
        written = write_deltas(cc, deltas, dry=args.dry_run, verbose=args.verbose)
        if not args.dry_run:
            save_snapshots(state, snaps)
            state.commit()
        cc.commit()
    except sqlite3.Error as e:
        cc.rollback()
        state.rollback()
        print(f"ERROR[30] 写入失败: {e}", file=sys.stderr)
        state.close(); cc.close()
        return 30
    state.close(); cc.close()
    print(f"OK 同步完成: 写入 {written} 条增量（累计快照 {len(rows)} 条, "
          f"profile {len(dbs)} 个）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
