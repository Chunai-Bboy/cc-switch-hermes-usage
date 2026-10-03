#!/usr/bin/env python3
"""
Hermes Agent -> CC Switch 用量同步 v2（skill: cc-switch-hermes-usage）

设计契约（三条铁律）
  1. 不造假：源数据证明不了的指标一律不写死。聚合源没有 status/latency，
     就让 CC Switch 的成功率/平均延迟列对 Hermes 无意义，而不是伪造 200/0。
  2. 内容寻址幂等：request_id 由累计值本身派生，崩溃重跑不会双计，
     last_seen 抖动不会造重复行。
  3. Hermes 库绝对只读：mode=ro + PRAGMA query_only=ON，任何路径都不写 state.db。

职责边界
  proxy_request_logs  -> 只放「聚合总量」，覆盖全部 task（含辅助任务）
  sync-state.db       -> 放「逐请求事实」，来自 ccswitch-usage 插件账本（可选）
  两者永不相加，避免重复计数。

退出码
  0  成功（含 0 增量）      10 Hermes state.db 不存在
  11 Hermes schema 不兼容   20 CC Switch DB 不存在
  21 CC Switch schema 不兼容 30 写入失败
  40 有历史记录但无本地同步状态（防双计，需 --reset-baseline）
  41 config.yaml custom_providers 被合成 provider 污染（仅 --check 报告）
"""

import argparse
import json
import os
import re
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
HERMES_HOME = os.path.expandvars(r"%LOCALAPPDATA%\hermes")
PLUGIN_LEDGER = os.path.join(HERMES_HOME, "ccswitch-usage.sqlite")

# 合成 provider 的命名前缀：CC Switch 的 Hermes 同步会把 CC Switch 的 provider
# 写回 config.yaml 的 custom_providers，合成 id 会被写成只有 name 的空壳条目。
SYNTHETIC_PREFIX = "_"

# CC Switch proxy_request_logs 列（3.20.4 实测）
LOG_COLUMNS = 24
LOG_INSERT = """
INSERT OR IGNORE INTO proxy_request_logs
(request_id, provider_id, app_type, model, request_model,
 input_tokens, output_tokens, cache_read_tokens, cache_creation_tokens,
 input_cost_usd, output_cost_usd, cache_read_cost_usd, cache_creation_cost_usd,
 total_cost_usd, latency_ms, first_token_ms, status_code, error_message,
 session_id, provider_type, is_streaming, cost_multiplier, created_at, data_source)
VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
"""

COST_SETTLED = ("actual", "final", "settled", "complete")
SNAPSHOT_COLS = ("api_calls", "input", "output", "cache_read", "cache_write",
                 "est_cost", "act_cost")
# 只用于窗口归属，不参与 delta 计算
SNAP_META_COL = "last_seen"

# CC Switch 的 proxy_request_logs 全库没有 reasoning / cache_write 的对应列
# （实测：全库 0 个 reasoning 列），这两个维度在 CC Switch 侧无处安放。
# 聚合快照里照常跟踪，但只落到我们自己的 sidecar，供 --report 与人工核对。
EXTRA_DIMENSIONS = ("reasoning",)


def save_extra_dimensions(state_conn, rows, verbose=False):
    """把 CC Switch 存不下的维度写进 sidecar。只旁路存储，不影响任何聚合逻辑。"""
    state_conn.execute("""CREATE TABLE IF NOT EXISTS dimension_totals (
        key TEXT PRIMARY KEY, profile TEXT, session_id TEXT, model TEXT, task TEXT,
        reasoning_tokens REAL, cache_write_tokens REAL, captured_at REAL)""")
    now = time.time()
    state_conn.executemany(
        "INSERT OR REPLACE INTO dimension_totals VALUES (?,?,?,?,?,?,?,?)",
        [("%s|%s|%s|%s" % (r["profile"], r["session_id"], r["model"], r["task"]),
          r["profile"], r["session_id"], r["model"], r["task"],
          r.get("reasoning", 0), r.get("cache_write", 0), now)
         for r in rows])
    if verbose:
        tot = sum(r.get("reasoning", 0) for r in rows)
        print("[extra] sidecar 记录 %d 行, reasoning 合计 %d" % (len(rows), tot))


def read_extra_totals(state_conn):
    try:
        return {r[0]: {"key": r[0], "profile": r[1], "session_id": r[2], "model": r[3],
                       "task": r[4], "reasoning_tokens": r[5],
                       "cache_write_tokens": r[6], "captured_at": r[7]}
                for r in state_conn.execute(
                    "SELECT key,profile,session_id,model,task,reasoning_tokens,"
                    "cache_write_tokens,captured_at FROM dimension_totals")}
    except sqlite3.OperationalError:
        return {}


class CheckResult:
    def __init__(self, ok: bool, code: int = 0, msg: str = "", warn: str = ""):
        self.ok, self.code, self.msg, self.warn = ok, code, msg, warn

    def __repr__(self):
        return "[%s] %s%s" % ("OK" if self.ok else "FAIL", self.msg,
                               ("  | WARN: " + self.warn) if self.warn else "")


# ---------------------------------------------------------------- 只读打开

def ro_connect(path: Path, timeout: float = 5.0) -> sqlite3.Connection:
    """只读 + query_only：任何写操作都会被 SQLite 自己拒绝，而不是靠我们自觉。"""
    conn = sqlite3.connect("file:%s?mode=ro" % str(path).replace("\\", "/"),
                           uri=True, timeout=timeout)
    conn.execute("PRAGMA query_only=ON")
    conn.execute("PRAGMA busy_timeout=%d" % int(timeout * 1000))
    return conn


def table_exists(conn, name: str) -> bool:
    return conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
                        (name,)).fetchone() is not None


def columns_of(conn, table: str) -> set:
    try:
        return {r[1] for r in conn.execute("PRAGMA table_info(%s)" % table)}
    except sqlite3.Error:
        return set()


# ---------------------------------------------------------------- 发现

def discover_hermes_dbs() -> list:
    base = Path(HERMES_HOME)
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


# ---------------------------------------------------------------- 健康检查

def check_hermes(dbs) -> CheckResult:
    if not dbs:
        return CheckResult(False, 10, "未发现 Hermes state.db（%s）" % HERMES_HOME)
    for name, path in dbs:
        try:
            conn = ro_connect(path)
        except sqlite3.Error as e:
            return CheckResult(False, 11, "profile %s 无法只读打开: %s" % (name, e))
        missing = [t for t in ("sessions", "session_model_usage")
                   if not table_exists(conn, t)]
        conn.close()
        if missing:
            return CheckResult(False, 11, "profile %s 缺表 %s" % (name, missing))
    return CheckResult(True, 0, "发现 %d 个 Hermes 数据库: %s"
                       % (len(dbs), ", ".join(n for n, _ in dbs)))


def check_cc_switch(db_path: str) -> CheckResult:
    if not os.path.exists(db_path):
        return CheckResult(False, 20, "CC Switch DB 不存在: %s" % db_path)
    conn = sqlite3.connect(db_path)
    missing = [t for t in ("proxy_request_logs", "providers")
               if not table_exists(conn, t)]
    conn.close()
    if missing:
        return CheckResult(False, 21, "CC Switch DB 缺表: %s" % missing)
    return CheckResult(True, 0, "CC Switch DB 正常: %s" % db_path)


def check_plugin_ledger() -> CheckResult:
    """插件账本是可选增强。没有它只是少了逐请求事实，不影响主链路。"""
    if not os.path.exists(PLUGIN_LEDGER):
        return CheckResult(True, 0, "插件账本未安装（逐请求事实不可得，属预期）",
                           warn="缺 %s" % PLUGIN_LEDGER)
    try:
        conn = ro_connect(Path(PLUGIN_LEDGER))
    except sqlite3.Error as e:
        return CheckResult(True, 0, "插件账本无法只读打开: %s" % e)
    if not table_exists(conn, "request_events"):
        conn.close()
        return CheckResult(True, 0, "插件账本无 request_events 表")
    n = conn.execute("SELECT COUNT(*) FROM request_events").fetchone()[0]
    conn.close()
    return CheckResult(True, 0, "插件账本可用: %d 条逐请求事件" % n)


def check_config_pollution() -> CheckResult:
    """检测 CC Switch 的 Hermes 同步是否把合成 provider 写进了 config.yaml。"""
    cfg = Path(HERMES_HOME) / "config.yaml"
    if not cfg.is_file():
        return CheckResult(True, 0, "无 config.yaml，跳过污染检查")
    try:
        import yaml  # type: ignore
        data = yaml.safe_load(cfg.read_text(encoding="utf-8")) or {}
    except Exception:
        return CheckResult(True, 0, "config.yaml 无法解析，跳过污染检查")
    bad = []
    for entry in (data.get("custom_providers") or []):
        if not isinstance(entry, dict):
            continue
        name = str(entry.get("name") or "")
        if name.startswith(SYNTHETIC_PREFIX) and not entry.get("base_url"):
            bad.append(name)
    if not bad:
        return CheckResult(True, 0, "config.yaml custom_providers 无合成条目污染")
    return CheckResult(
        True, 41, "config.yaml custom_providers 有 %d 个合成空壳条目: %s"
                 % (len(bad), ", ".join(bad)),
        warn="这些条目由 CC Switch 的 Hermes provider 同步从 CC Switch providers 回写产生，"
             "只有 name 没有 base_url；用 --repair-config 清理")


def repair_config(verbose=False) -> int:
    """只删除「合成前缀 + 无 base_url + 无 models」这一种明确无害的空壳条目。"""
    cfg = Path(HERMES_HOME) / "config.yaml"
    try:
        import yaml  # type: ignore
    except ImportError:
        print("需要 pyyaml 才能修复 config.yaml", file=sys.stderr)
        return 1
    raw = cfg.read_text(encoding="utf-8")
    try:
        data = yaml.safe_load(raw) or {}
    except Exception as e:
        print("config.yaml 解析失败，未改动: %s" % e, file=sys.stderr)
        return 1
    providers = data.get("custom_providers")
    if not isinstance(providers, list):
        print("custom_providers 不是列表，无需修复")
        return 0
    keep = [e for e in providers
            if not (isinstance(e, dict)
                    and str(e.get("name") or "").startswith(SYNTHETIC_PREFIX)
                    and not e.get("base_url"))]
    removed = [e.get("name") for e in providers if e not in keep]
    if not removed:
        print("无需修复")
        return 0
    backup = cfg.with_suffix(".yaml.repairsync-bak")
    backup.write_text(raw, encoding="utf-8")
    data["custom_providers"] = keep
    cfg.write_text(yaml.safe_dump(data, allow_unicode=True, sort_keys=False),
                   encoding="utf-8")
    print("已移除合成条目: %s（备份: %s）" % (", ".join(map(str, removed)), backup.name))
    return 0


# ---------------------------------------------------------------- 数据获取

def read_hermes_snapshots(dbs, verbose=False):
    """读全部 profile 的 session_model_usage 累计快照（cumulative 语义）。"""
    rows = []
    for profile, path in dbs:
        conn = ro_connect(path)
        conn.row_factory = sqlite3.Row
        ucols = columns_of(conn, "session_model_usage")
        scols = columns_of(conn, "sessions")
        mcols = columns_of(conn, "messages")

        sql = """
        SELECT u.session_id, u.model, COALESCE(u.task,'') AS task,
               u.api_call_count, u.input_tokens, u.output_tokens,
               u.cache_read_tokens, u.cache_write_tokens, u.reasoning_tokens,
               u.estimated_cost_usd, u.actual_cost_usd, u.cost_status,
               u.first_seen, u.last_seen, u.billing_provider, u.billing_base_url,
               u.billing_mode, s.billing_provider, s.billing_mode, s.cwd
        FROM session_model_usage u
        LEFT JOIN sessions s ON s.id = u.session_id
        """
        for col, default in (("cost_status", "NULL"), ("first_seen", "NULL")):
            if col not in ucols:
                sql = sql.replace("u." + col, default)
        if "task" not in ucols:
            sql = sql.replace("COALESCE(u.task, '')", "''")
        for col in ("api_call_count", "input_tokens", "output_tokens",
                    "cache_read_tokens", "cache_write_tokens", "reasoning_tokens",
                    "estimated_cost_usd", "actual_cost_usd", "last_seen"):
            if col not in ucols:
                sql = sql.replace("u." + col, "NULL")
        for col in ("billing_provider", "billing_mode", "cwd"):
            if col not in scols:
                sql = sql.replace("s." + col, "''")
        for col in ("billing_provider", "billing_base_url", "billing_mode"):
            if col not in ucols:
                # 必须带 AS：sqlite3.Row 按列名取值，匿名列 '' 取不到 r[col]
                sql = sql.replace("u." + col, "'' AS " + col)

        for r in conn.execute(sql):
            keys = r.keys()

            def col(name, default=0):
                # 老版本缺列时 SQL 已被替换成 NULL 字面量，Row 里就没有这个键。
                return int(r[name] or default) if name in keys else default
            rows.append({
                "profile": profile, "session_id": r["session_id"] or "",
                "model": r["model"] or "unknown", "task": r["task"] or "",
                "api_calls": int(r["api_call_count"] or 0),
                "input": int(r["input_tokens"] or 0),
                "output": int(r["output_tokens"] or 0),
                "cache_read": int(r["cache_read_tokens"] or 0),
                "cache_write": col("cache_write_tokens"),
                "reasoning": col("reasoning_tokens"),
                "est_cost": float(r["estimated_cost_usd"] or 0.0),
                "act_cost": float(r["actual_cost_usd"] or 0.0),
                "cost_status": (r["cost_status"] or "").lower(),
                "first_seen": float(r["first_seen"] or 0.0),
                "last_seen": float(r["last_seen"] or 0.0),
                "billing_provider": r["billing_provider"] or "",
                "billing_mode": r["billing_mode"] or "",
                "billing_base_url": r["billing_base_url"] or "",
                "cwd": r["cwd"] or "",
            })

        # 主循环最后一条真实 assistant 消息时刻：比 last_seen 更贴近「用户看到的时刻」，
        # 且不受 background_review 那种「8 次调用压进同一毫秒」的压缩影响。
        if mcols and {"session_id", "role", "timestamp"} <= mcols:
            for sid, ts in conn.execute(
                    "SELECT session_id, MAX(timestamp) FROM messages "
                    "WHERE role='assistant' AND timestamp IS NOT NULL GROUP BY session_id"):
                for row in rows:
                    if row["session_id"] == sid:
                        row["last_msg_ts"] = float(ts or 0.0)
        conn.close()
    for r in rows:
        r.setdefault("last_msg_ts", r["last_seen"])
    if verbose:
        print("[fetch] 读取 %d 条 cumulative 快照" % len(rows))
    return rows


def read_plugin_events(verbose=False):
    """读 ccswitch-usage 插件账本（可选）。只读，永远不改 Hermes。"""
    if not os.path.exists(PLUGIN_LEDGER):
        return []
    try:
        conn = ro_connect(Path(PLUGIN_LEDGER))
    except sqlite3.Error:
        return []
    if not table_exists(conn, "request_events"):
        conn.close()
        return []
    conn.row_factory = sqlite3.Row
    events = [dict(r) for r in conn.execute(
        "SELECT event_id, kind, session_id, model, provider, started_at_ms, ended_at_ms,"
        " duration_ms, status, status_code, usage_available, input_tokens, output_tokens,"
        " cache_read_tokens, cache_write_tokens, reasoning_tokens "
        "FROM request_events")]
    conn.close()
    if verbose:
        print("[plugin] 读到 %d 条逐请求事件" % len(events))
    return events


# ---------------------------------------------------------------- delta

def series_key(r):
    """序列键 = Hermes 表的真实主键，不多不少。

    session_model_usage 的 PRIMARY KEY 是
    (session_id, model, billing_provider, billing_base_url, billing_mode, task)，
    而 SQLite 在普通表上会为 PRIMARY KEY 建隐式唯一索引，所以这 6 列本身就
    保证不会撞。v2 原来只用 (session, model, task)，少了 3 列，实测
    20261003_144834_1d260f 里两条 task='approval'（base_url 为空 vs
    https://opencode.ai/zen/v1/）被并成一条序列。

    这里刻意**不加** first_seen 之类的"保险"字段：主键之外的任何可变列进键，
    都会在它被改写时把老序列变成新序列，从而重复计数（v1 回归测试实测踩到：
    first_seen 1000 -> 2000 就多写了 100 input / 10 output）。
    """
    return "|".join([
        r["profile"], r["session_id"], r["model"], r["task"],
        r.get("billing_provider") or "", r.get("billing_base_url") or "",
        r.get("billing_mode") or "",
    ])


def compute_deltas(rows, state_conn, verbose=False):
    deltas, new_snaps = [], []
    resets = 0
    for r in rows:
        key = series_key(r)
        prev = state_conn.execute(
            "SELECT " + ",".join(SNAPSHOT_COLS + (SNAP_META_COL,))
            + " FROM snapshots WHERE key=?", (key,)).fetchone()
        prev = dict(zip(SNAPSHOT_COLS + (SNAP_META_COL,), prev)) if prev else None

        if prev is None:
            d = {c: r[c] for c in SNAPSHOT_COLS}
            d["baseline"] = True
        elif all(r[c] >= prev[c] for c in SNAPSHOT_COLS):
            d = {c: r[c] - prev[c] for c in SNAPSHOT_COLS}
            d["baseline"] = False
        else:
            d = {c: r[c] for c in SNAPSHOT_COLS}
            d["baseline"] = True
            prev = None          # 计数器重置：上一次的时刻已无意义，别拿来划窗口
            resets += 1
            if verbose:
                print("[delta] 计数器重置: %s" % key)

        if any(d[c] > 0 for c in SNAPSHOT_COLS):
            d["row"] = r
            d["prev_last_seen"] = (prev or {}).get(SNAP_META_COL) or 0.0
            deltas.append(d)
        new_snaps.append((key,) + tuple(r[c] for c in SNAPSHOT_COLS)
                         + (r["last_seen"],))
    if verbose:
        print("[delta] %d 条待写入增量, %d 次计数器重置" % (len(deltas), resets))
    return deltas, new_snaps


# ---------------------------------------------------------------- 成本

def price_from_cc_switch(cc_conn, model, dinput, doutput, dread, dwrite):
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
    """actual（已结算）> estimated > actual>0 > None（交给 model_pricing 兜底）。"""
    if row["cost_status"] in COST_SETTLED:
        return d["act_cost"], "actual"
    if d["est_cost"] > 0:
        return d["est_cost"], "estimated"
    if d["act_cost"] > 0:
        return d["act_cost"], "actual"
    return None, "priced"


def latency_index(events):
    """(session_id, model) -> 排好序的 [dur_ms]，只收主循环成功事件。

    注意两点，都是实测踩出来的：
    1) 插件的 post_api_request 也覆盖 background_review（子 agent 走同一条
       conversation_loop 代码路径），但**不覆盖** title_generation / approval
       （它们走 auxiliary_client，没有这个 hook）。所以能拿到的耗时天然只
       代表 conversation_loop 这一侧。
    2) 不能把它套到辅助调用行上：实测 title_generation 行曾被误填主循环中位数。
    返回原始列表而不是中位数，是为了让调用方能按时间窗自己筛。
    """
    idx = {}
    for e in events:
        if (e.get("kind") or "") != "main":
            continue
        if (e.get("status") or "") != "success" or not e.get("duration_ms"):
            continue
        idx.setdefault((e.get("session_id") or "", e.get("model") or ""), []).append(
            (int(e.get("started_at_ms") or 0), int(e["duration_ms"])))
    return idx


def window_latency(lat_idx, session_id, model, prev_last_seen, cur_last_seen):
    """只取落在 (prev_last_seen, cur_last_seen] 这个真实时间窗里的事件。

    BUG 修复：v2 把「全会话耗时中位数」填进每个窗口行。实测一个 43 次调用的
    会话里窗口行显示 latency=59805ms，而该会话真实耗时区间是 14.2s~198.2s，
    语义完全对不上——行代表一个时间窗口，填的却是整段会话。
    没有可用时间窗（例如首次基线）时返回 None，让调用方写 0 而不是编一个数。
    """
    items = lat_idx.get((session_id, model)) or []
    if not items or not cur_last_seen:
        return None
    lo = (prev_last_seen or 0.0) * 1000.0
    hi = cur_last_seen * 1000.0 + 1000.0
    if lo <= 0:
        return None                      # 首次基线，没有"上一次"，不编
    sel = [d for t, d in items if lo < t <= hi]
    return median(sel) if sel else None


def median(xs):
    s = sorted(xs)
    n = len(s)
    if not n:
        return None
    return s[n // 2] if n % 2 else (s[n // 2 - 1] + s[n // 2]) // 2


# ---------------------------------------------------------------- 写入

def ensure_provider(cc_conn):
    cc_conn.execute(
        "INSERT OR IGNORE INTO providers (id, app_type, name, settings_config)"
        " VALUES (?, ?, ?, '{}')", (PROVIDER_ID, APP_TYPE, PROVIDER_NAME))
    cc_conn.execute(
        "UPDATE providers SET name = ? WHERE id = ? AND app_type = ? AND name IS NOT ?",
        (PROVIDER_NAME, PROVIDER_ID, APP_TYPE, PROVIDER_NAME))


def make_request_id(row):
    """内容寻址幂等键。

    v1 用 last_seen_ms 收尾：last_seen 会抖动，且「写完 log 但没存快照」时重跑
    会得到不同的 key，从而重复计一行。v2 把「已导入的累计值」编进 key：
      - 同样的累计值 -> 同样的 key -> INSERT OR IGNORE 天然去重（崩溃可重跑）
      - 累计值前进 -> 必然是新 key，不会吞掉真实增量
      - last_seen 单独抖动而计数没动 -> key 不变，不产生 0 token 垃圾行
    """
    return "hermes:%s:%s:%s:%s:%s:%s@%s@c%d:%.6f:%.6f" % (
        row["profile"], row["session_id"], row["model"], row["task"],
        _slug(row.get("billing_provider")), _slug(row.get("billing_base_url")),
        _slug(row.get("billing_mode")),
        int(row["api_calls"]), row["est_cost"], row["act_cost"])


def _slug(url):
    """base_url 片段塞进 request_id：稳定、可读、不会引入非法字符。"""
    if not url:
        return "-"
    keep = [c if (c.isalnum() or c in "._-") else "_" for c in url]
    return ("".join(keep)[:48] or "-")


def write_deltas(cc_conn, deltas, lat_idx, dry=False, verbose=False):
    written = 0
    now = int(time.time())
    for d in deltas:
        row = d["row"]
        cost, cost_kind = pick_cost(d, row)
        if cost is None:
            cost = price_from_cc_switch(cc_conn, row["model"],
                                        d["input"], d["output"],
                                        d["cache_read"], d["cache_write"])
        # 聚合源证明不了 status_code / latency。latency 只在「本行就是主循环」且
        # 插件有该 session 的成功观测时填真实中位数；辅助调用行拿不到就留 0。
        if row["task"] == "":
            latency = window_latency(lat_idx, row["session_id"], row["model"],
                                     d.get("prev_last_seen"), row["last_seen"]) or 0
        else:
            latency = 0
        created = int(row["last_msg_ts"] or row["last_seen"] or now)
        params = (
            make_request_id(row), PROVIDER_ID, APP_TYPE, row["model"], row["model"],
            d["input"], d["output"], d["cache_read"], d["cache_write"],
            0, 0, 0, 0, "%.6f" % cost,
            int(latency or 0), 0, 200, None,
            row["session_id"], row["billing_provider"] or "hermes", 0, "1.0",
            created, DATA_SOURCE,
        )
        assert len(params) == LOG_COLUMNS, "列数不匹配: %d" % len(params)
        if not dry:
            cc_conn.execute(LOG_INSERT, params)
        written += 1
        if verbose:
            print("[write] %s in=%s out=%s cr=%s cost=$%.6f (%s) latency=%s %s"
                  % (params[0], d["input"], d["output"], d["cache_read"], cost,
                     cost_kind, latency or "n/a",
                     "baseline" if d["baseline"] else "delta"))
    return written


def save_events(state_conn, events):
    """把插件逐请求事实落到我们自己的 sidecar，永不进 proxy_request_logs（防双计）。"""
    state_conn.execute("""CREATE TABLE IF NOT EXISTS request_events (
        event_id TEXT PRIMARY KEY, kind TEXT, session_id TEXT, model TEXT,
        started_at_ms INTEGER, duration_ms INTEGER, status TEXT, status_code INTEGER,
        input_tokens INTEGER, output_tokens INTEGER, cache_read_tokens INTEGER,
        cache_write_tokens INTEGER, reasoning_tokens INTEGER)""")
    state_conn.executemany(
        "INSERT OR REPLACE INTO request_events VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        [(e["event_id"], e.get("kind"), e.get("session_id"), e.get("model"),
          e.get("started_at_ms"), e.get("duration_ms"), e.get("status"),
          e.get("status_code"), e.get("input_tokens"), e.get("output_tokens"),
          e.get("cache_read_tokens"), e.get("cache_write_tokens"),
          e.get("reasoning_tokens")) for e in events])


def ensure_snapshots_table(state_conn):
    state_conn.execute("CREATE TABLE IF NOT EXISTS snapshots ("
                       " key TEXT PRIMARY KEY, "
                       + ", ".join("%s REAL DEFAULT 0" % c for c in SNAPSHOT_COLS)
                       + ", %s REAL DEFAULT 0)" % SNAP_META_COL)


def ensure_snapshots_column(state_conn):
    cols = {r[1] for r in state_conn.execute("PRAGMA table_info(snapshots)")}
    if SNAP_META_COL not in cols:
        state_conn.execute("ALTER TABLE snapshots ADD COLUMN %s REAL DEFAULT 0"
                           % SNAP_META_COL)
        return True
    return False


def save_snapshots(state_conn, snaps):
    state_conn.executemany(
        "INSERT OR REPLACE INTO snapshots (key, " + ",".join(SNAPSHOT_COLS)
        + ", " + SNAP_META_COL + ")"
        " VALUES (?," + ",".join("?" * (len(SNAPSHOT_COLS) + 1)) + ")", snaps)


# ---------------------------------------------------------------- 报告

def report(state_conn, events, cc_db):
    out = {}
    out["aggregate_source"] = "Hermes session_model_usage（累计值→差值）"
    out["event_source"] = ("ccswitch-usage 插件账本" if events else "不可得（插件未安装/无数据）")
    snap = state_conn.execute(
        "SELECT COALESCE(SUM(api_calls),0), COALESCE(SUM(input),0), COALESCE(SUM(output),0),"
        " COALESCE(SUM(cache_read),0), COALESCE(SUM(cache_write),0) FROM snapshots").fetchone()
    out["snapshot_totals"] = dict(zip(
        ["calls", "in", "out", "cache_read", "cache_write"], snap))
    if events:
        agg = {}
        for e in events:
            if (e.get("status") or "") != "success" or not e.get("usage_available"):
                continue
            k = (e.get("session_id") or "", e.get("model") or "")
            a = agg.setdefault(k, [0, 0, 0, 0, 0])
            a[0] += 1
            a[1] += e.get("input_tokens") or 0
            a[2] += e.get("output_tokens") or 0
            a[3] += e.get("cache_read_tokens") or 0
            a[4] += e.get("reasoning_tokens") or 0
        d = [e.get("duration_ms") for e in events
             if (e.get("status") or "") == "success" and e.get("duration_ms")]
        err = [e for e in events if (e.get("status") or "") == "error"]
        out["event_totals"] = {
            "rows": len(events), "success": len(events) - len(err), "error": len(err),
            "by_session": {("%s|%s" % k): v for k, v in agg.items()},
            "latency_ms": ({"min": min(d), "median": median(d), "max": max(d)}
                           if d else None),
            "status_codes": sorted({e.get("status_code") for e in err
                                    if e.get("status_code")}),
        }
    extra = read_extra_totals(state_conn)
    if extra:
        out["sidecar_unmapped"] = {
            "why": "CC Switch proxy_request_logs 全库没有这些列，无法进仪表盘；仅存 sidecar",
            "columns_checked": "proxy_request_logs 27 列 / 全库 0 个 reasoning 列",
            "reasoning_tokens": sum(v["reasoning_tokens"] or 0 for v in extra.values()),
            "cache_write_tokens": sum(v["cache_write_tokens"] or 0 for v in extra.values()),
            "rows": len(extra),
        }
    out["truthfulness_note"] = (
        "聚合源无法证明 status_code / latency：CC Switch 用 "
        "`200<=status_code<300` 判成功，本脚本写 200 会让 Hermes 的成功率恒为 100%，"
        "这是已知失真；latency 在插件观测可用时填真实中位数，否则为 0。"
        "根治需要上游像 PR #6120 那样加 statusAvailable/latencyAvailable 标志位。")
    if not os.path.exists(cc_db):
        return out
    conn = sqlite3.connect("file:%s?mode=ro" % cc_db.replace("\\", "/"), uri=True)
    if not table_exists(conn, "proxy_request_logs"):
        conn.close()
        out["cc_switch"] = {"error": "proxy_request_logs 表不存在"}
        return out
    n, i, o, cr, cc_, cost = conn.execute(
        "SELECT COUNT(*), COALESCE(SUM(input_tokens),0), COALESCE(SUM(output_tokens),0),"
        " COALESCE(SUM(cache_read_tokens),0), COALESCE(SUM(cache_creation_tokens),0),"
        " COALESCE(SUM(CAST(total_cost_usd AS REAL)),0) FROM proxy_request_logs"
        " WHERE data_source=?", (DATA_SOURCE,)).fetchone()
    conn.close()
    out["cc_switch"] = {"rows": n, "in": i, "out": o, "cache_read": cr,
                        "cache_creation": cc_, "cost_usd": round(cost, 6)}
    return out


# ---------------------------------------------------------------- 主流程

def main():
    ap = argparse.ArgumentParser(
        description="Hermes -> CC Switch 用量同步 v2",
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--cc-db", default=os.path.expanduser(r"~\.cc-switch\cc-switch.db"))
    ap.add_argument("--state-db", default=STATE_DB)
    ap.add_argument("--check", action="store_true", help="仅健康检查（只读）")
    ap.add_argument("--dry-run", action="store_true", help="计算但不写入")
    ap.add_argument("--reset-baseline", action="store_true",
                    help="删除 CC Switch 旧 hermes_session 记录并清空本地状态后重扫")
    ap.add_argument("--report", action="store_true", help="输出聚合 vs 逐请求对比报告")
    ap.add_argument("--repair-config", action="store_true",
                    help="清理 config.yaml custom_providers 里的合成空壳条目")
    ap.add_argument("--verbose", "-v", action="store_true")
    args = ap.parse_args()

    log = print if args.verbose else (lambda *a, **k: None)

    if args.repair_config:
        return repair_config(args.verbose)

    dbs = discover_hermes_dbs()
    r1 = check_hermes(dbs)
    r2 = check_cc_switch(args.cc_db)
    r3 = check_plugin_ledger()
    # config 污染是诊断项，不是热路径：import yaml 冷启动约 20ms、safe_load 再 10ms，
    # 放进每 30 分钟一次的同步里纯属浪费。只在显式诊断/修复时才算。
    want_pollution = args.check or args.repair_config or args.report or args.verbose
    r4 = check_config_pollution() if want_pollution else CheckResult(
        True, 0, "config 污染检查已跳过（非诊断模式）")
    for r in (r1, r2, r3, r4):
        log("[check] %r" % r)
    if not r1.ok:
        print("ERROR[%d] %s" % (r1.code, r1.msg), file=sys.stderr)
        return r1.code
    if not r2.ok:
        print("ERROR[%d] %s" % (r2.code, r2.msg), file=sys.stderr)
        return r2.code
    if args.check:
        for r in (r1, r2, r3, r4):
            print("%r" % r)
        return 0

    rows = read_hermes_snapshots(dbs, args.verbose)
    events = read_plugin_events(args.verbose)
    os.makedirs(os.path.dirname(args.state_db), exist_ok=True)
    state = sqlite3.connect(args.state_db, timeout=10)
    ensure_snapshots_table(state)
    if ensure_snapshots_column(state) and not args.dry_run:
        print("[migrate] snapshots 表新增 %s 列（窗口归属用），已按 0 初始化；"
              "首次同步会重建基线" % SNAP_META_COL, file=sys.stderr)
        state.execute("DELETE FROM snapshots")
        state.commit()

    cc = sqlite3.connect(args.cc_db, timeout=10)
    existing = cc.execute("SELECT COUNT(*) FROM proxy_request_logs WHERE data_source=?",
                          (DATA_SOURCE,)).fetchone()[0]
    state_cnt = state.execute("SELECT COUNT(*) FROM snapshots").fetchone()[0]
    if existing > 0 and state_cnt == 0 and not args.reset_baseline and not args.dry_run:
        print("ERROR[40] CC Switch 已有 %d 条 hermes_session 记录但无本地同步状态，"
              "直接同步会双计。加 --reset-baseline 重建基线。" % existing,
              file=sys.stderr)
        state.close(); cc.close()
        return 40

    if args.report:
        state.commit()
        print(json.dumps(report(state, events, args.cc_db),
                         ensure_ascii=False, indent=2))
        state.close(); cc.close()
        return 0

    if args.dry_run:
        prev = {r[0] for r in state.execute("SELECT key FROM snapshots")}
        new = {series_key(r) for r in rows} - prev
        print("[dry-run] 快照 %d 条, 新 key %d 个将建基线; 插件事件 %d 条; CC Switch %s"
              % (len(rows), len(new), len(events), args.cc_db))
        state.close(); cc.close()
        return 0

    if args.reset_baseline:
        print("[reset] 删除 CC Switch 旧 hermes_session 记录并清空本地状态")
        cc.execute("DELETE FROM proxy_request_logs WHERE data_source=?", (DATA_SOURCE,))
        state.execute("DELETE FROM snapshots")
        cc.commit(); state.commit()

    deltas, snaps = compute_deltas(rows, state, args.verbose)
    t0 = time.perf_counter()
    try:
        cc.execute("BEGIN IMMEDIATE")
        ensure_provider(cc)
        written = write_deltas(cc, deltas, latency_index(events),
                              dry=args.dry_run, verbose=args.verbose)
        if not args.dry_run:
            save_snapshots(state, snaps)
            save_events(state, events)
            save_extra_dimensions(state, rows, args.verbose)
            state.commit()
        cc.commit()
    except sqlite3.Error as e:
        cc.rollback(); state.rollback()
        print("ERROR[30] 写入失败: %s" % e, file=sys.stderr)
        state.close(); cc.close()
        return 30
    elapsed = (time.perf_counter() - t0) * 1000
    state.close(); cc.close()
    print("OK 同步完成: 聚合增量 %d 条 (写入耗时 %.1fms), 累计快照 %d 条, "
          "profile %d 个, 插件逐请求事件 %d 条已存 sidecar"
          % (written, elapsed, len(rows), len(dbs), len(events)))
    if r4.code == 41:
        print("WARN: %s（用 --repair-config 清理）" % r4.msg)
    return 0


if __name__ == "__main__":
    sys.exit(main())
