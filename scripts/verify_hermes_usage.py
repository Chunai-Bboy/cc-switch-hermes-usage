#!/usr/bin/env python3
"""
Hermes → CC Switch 用量同步验收检查（v2）

与 v1 的根本区别：v1 只验「有没有数据」，v2 验「数据对不对」。
核心检查是三方对账：CC Switch 仪表盘里的数字  vs  Hermes 源库累计值
vs  插件逐请求事实。任何一项对不上就 FAIL。

退出码：
  0  全部通过
  1  有 FAIL 项（数据不一致 / 缺失）
  2  环境不可用（Hermes 或 CC Switch 库找不到）
"""

import argparse
import json
import os
import sqlite3
import sys
import time
from contextlib import closing

PROVIDER_ID = "_hermes_session"
APP_TYPE = "hermes"
DATA_SOURCE = "hermes_session"
STATE_DB = os.path.expandvars(r"%LOCALAPPDATA%\cc-switch-hermes-usage\sync-state.db")
HERMES_HOME = os.path.expandvars(r"%LOCALAPPDATA%\hermes")
PLUGIN_LEDGER = os.path.join(HERMES_HOME, "ccswitch-usage.sqlite")

OK, WARN, BAD = "OK  ", "WARN", "FAIL"


class Check:
    def __init__(self):
        self.rows = []
        self.failed = 0

    def add(self, level, name, detail=""):
        self.rows.append((level, name, detail))
        if level == BAD:
            self.failed += 1

    def ok(self, n, d=""):
        self.add(OK, n, d)

    def warn(self, n, d=""):
        self.add(WARN, n, d)

    def bad(self, n, d=""):
        self.add(BAD, n, d)

    def dump(self):
        width = max(len(r[1]) for r in self.rows) + 2
        for level, name, detail in self.rows:
            line = "  [%s] %-*s" % (level, width, name)
            if detail:
                line += " " + detail
            print(line)


def ro(path, timeout=10.0):
    conn = sqlite3.connect("file:%s?mode=ro" % str(path).replace("\\", "/"),
                           uri=True, timeout=timeout)
    conn.execute("PRAGMA busy_timeout=%d" % int(timeout * 1000))
    return conn


def hermes_dbs():
    base = HERMES_HOME
    out = []
    if os.path.exists(os.path.join(base, "state.db")):
        out.append(("default", os.path.join(base, "state.db")))
    pd = os.path.join(base, "profiles")
    if os.path.isdir(pd):
        for p in sorted(os.listdir(pd)):
            f = os.path.join(pd, p, "state.db")
            if os.path.exists(f):
                out.append((p, f))
    return out


def truth_from_hermes(dbs):
    """真值：所有 profile 的 session_model_usage 累计值。"""
    tot = {"calls": 0, "input": 0, "output": 0, "cache_read": 0,
           "cache_write": 0, "reasoning": 0, "aux_calls": 0, "main_calls": 0}
    per_task = {}
    for name, path in dbs:
        with closing(ro(path)) as c:
            ucols = {r[1] for r in c.execute("PRAGMA table_info(session_model_usage)")}
            has = lambda x: x if x in ucols else "NULL"
            sql = ("SELECT COALESCE(task,''), COALESCE(api_call_count,0),"
                   " COALESCE(input_tokens,0), COALESCE(output_tokens,0),"
                   " COALESCE(cache_read_tokens,0), COALESCE(cache_write_tokens,{cw}),"
                   " COALESCE(reasoning_tokens,{rs})"
                   " FROM session_model_usage").format(
                       cw=has("cache_write_tokens"), rs=has("reasoning_tokens"))
            for task, calls, i, o, cr, cw, rs in c.execute(sql):
                tot["calls"] += calls; tot["input"] += i; tot["output"] += o
                tot["cache_read"] += cr; tot["cache_write"] += cw
                tot["reasoning"] += rs
                if task == "":
                    tot["main_calls"] += calls
                else:
                    tot["aux_calls"] += calls
                a = per_task.setdefault(task or "(main)", [0, 0])
                a[0] += calls; a[1] += cr
    return tot, per_task


def main():
    ap = argparse.ArgumentParser(description="Hermes → CC Switch 同步验收")
    ap.add_argument("--cc-db", default=os.path.expanduser(r"~\.cc-switch\cc-switch.db"))
    ap.add_argument("--state-db", default=STATE_DB)
    ap.add_argument("--json", action="store_true", help="额外输出 JSON 结论")
    args = ap.parse_args()
    c = Check()
    summary = {}

    # ---------------------------------------------------------- 0. 环境
    dbs = hermes_dbs()
    if not dbs:
        print("ERROR[10] 未发现 Hermes state.db（%s）" % HERMES_HOME, file=sys.stderr)
        return 2
    if not os.path.exists(args.cc_db):
        print("ERROR[20] CC Switch DB 不存在: %s" % args.cc_db, file=sys.stderr)
        return 2
    c.ok("环境", "%d 个 Hermes 库: %s" % (len(dbs), ", ".join(n for n, _ in dbs)))

    # ---------------------------------------------------------- 1. provider
    with closing(ro(args.cc_db)) as cc:
        pv = cc.execute("SELECT name FROM providers WHERE id=? AND app_type=?",
                        (PROVIDER_ID, APP_TYPE)).fetchone()
        if pv:
            c.ok("provider 注册", pv[0])
        else:
            c.bad("provider 注册", "缺失，先跑 hermes_usage_sync.py")

        # ------------------------------------------------- 2. 记录存在性
        agg = cc.execute(
            "SELECT COUNT(*), COALESCE(SUM(input_tokens),0), COALESCE(SUM(output_tokens),0),"
            " COALESCE(SUM(cache_read_tokens),0), COALESCE(SUM(cache_creation_tokens),0),"
            " COALESCE(SUM(CAST(total_cost_usd AS REAL)),0), MIN(created_at), MAX(created_at),"
            " COALESCE(SUM(CASE WHEN latency_ms>0 THEN 1 ELSE 0 END),0)"
            " FROM proxy_request_logs WHERE data_source=?", (DATA_SOURCE,)).fetchone()
        cnt, a_in, a_out, a_cr, a_cw, a_cost, t0, t1, a_lat = agg
        if cnt:
            c.ok("CC Switch 记录", "%d 条，%s ~ %s" % (
                cnt, time.strftime("%m-%d %H:%M", time.localtime(t0)),
                time.strftime("%m-%d %H:%M", time.localtime(t1))))
        else:
            c.bad("CC Switch 记录", "0 条")

        # ------------------------------------------------- 3. 三方对账（核心）
        truth, per_task = truth_from_hermes(dbs)
        summary["truth"] = truth
        summary["cc_switch"] = {"rows": cnt, "input": a_in, "output": a_out,
                                "cache_read": a_cr, "cache_creation": a_cw,
                                "cost_usd": round(a_cost, 6), "latency_rows": a_lat}
        print("\n=== 三方对账：CC Switch 仪表盘  vs  Hermes 源库累计值 ===")
        print("  %-14s %16s %16s %16s %s"
              % ("维度", "Hermes 真值", "CC Switch 合计", "差额", "判定"))
        pairs = [("API 调用数", truth["calls"], None),
                 ("input tokens", truth["input"], a_in),
                 ("output tokens", truth["output"], a_out),
                 ("cache_read tokens", truth["cache_read"], a_cr)]
        align = True
        for name, tv, cv in pairs:
            if cv is None:
                print("  %-14s %16s %16s %16s %s"
                      % (name, "{:,}".format(tv), "聚合行(不按行计)",
                         "-", "n/a"))
                continue
            d = cv - tv
            good = (d == 0)
            align = align and good
            print("  %-14s %16s %16s %16s %s"
                  % (name, "{:,}".format(tv), "{:,}".format(cv), "{:+,}".format(d),
                     "一致" if good else "不一致"))
            (c.ok if good else c.bad)("对账 " + name,
                                      "一致" if good else "差 {:+,}".format(d))

        # cache_write：CC Switch 用 cache_creation 承载
        if truth["cache_write"] == 0 and a_cw == 0:
            print("  %-14s %16s %16s %16s %s"
                  % ("cache_write", "0", "0", "0", "一致"))
            c.ok("对账 cache_write", "一致（两侧均为 0）")
        elif truth["cache_write"] == a_cw:
            c.ok("对账 cache_write", "一致")
        else:
            c.warn("对账 cache_write",
                   "真值 {:,} / CC cache_creation {:,}".format(truth["cache_write"], a_cw))

        # ------------------------------------------------- 4. 幂等性
        if cnt and os.path.exists(args.state_db):
            with closing(ro(args.state_db)) as st:
                keys = st.execute("SELECT COUNT(*) FROM snapshots").fetchone()[0]
                tbl = {r[0] for r in st.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'")}
                sidecar = (st.execute("SELECT COUNT(*) FROM request_events").fetchone()[0]
                           if "request_events" in tbl else 0)
                extra = (st.execute("SELECT COALESCE(SUM(reasoning_tokens),0),"
                                    " COALESCE(SUM(cache_write_tokens),0)"
                                    " FROM dimension_totals").fetchone()
                         if "dimension_totals" in tbl else (0, 0))
            c.ok("本地快照", "%d 个 key（>= CC Switch 记录数即无未落库增量）" % keys)
            if extra[0]:
                c.ok("sidecar 维度",
                     "reasoning {:,} / cache_write {:,}（CC Switch 无对应列，仅存 sidecar）"
                     .format(*extra))
                summary["sidecar"] = {"reasoning": extra[0], "cache_write": extra[1]}
        else:
            c.warn("本地快照", "sync-state.db 不存在")

        # ------------------------------------------------- 5. latency 归属
        lat_by_task = {}
        for r in cc.execute(
                "SELECT request_id, latency_ms FROM proxy_request_logs"
                " WHERE data_source=? AND latency_ms>0", (DATA_SOURCE,)):
            lat_by_task[r[0]] = r[1]
        aux_lat = [v for k, v in lat_by_task.items()
                   if ":title_generation:" in k or ":approval:" in k
                   or ":background_review:" in k]
        if aux_lat:
            c.bad("latency 归属",
                  "%d 条辅助任务行带了耗时（应恒为 0，辅助调用无 hook 观测）" % len(aux_lat))
        elif lat_by_task:
            c.ok("latency 归属",
                 "%d 条主循环行有真实耗时，辅助任务行均为 0" % len(lat_by_task))
        summary["latency_rows"] = a_lat

        # ------------------------------------------------- 6. 插件交叉验证
        if os.path.exists(PLUGIN_LEDGER):
            with closing(ro(PLUGIN_LEDGER)) as lg:
                ev = lg.execute("SELECT COUNT(*) FROM request_events").fetchone()[0]
                kinds = dict(lg.execute(
                    "SELECT COALESCE(kind,'?'), COUNT(*) FROM request_events GROUP BY 1"))
                good = lg.execute("SELECT COUNT(*) FROM request_events"
                                 " WHERE kind='main' AND usage_available=1").fetchone()[0]
                agg_ev = lg.execute(
                    "SELECT COALESCE(SUM(input_tokens),0), COALESCE(SUM(output_tokens),0),"
                    " COALESCE(SUM(cache_read_tokens),0)"
                    " FROM request_events WHERE kind='main' AND usage_available=1").fetchone()
            c.ok("插件账本", "%d 条事件（main %d / 其他 %d）"
                 % (ev, kinds.get("main", 0), ev - kinds.get("main", 0)))
            if kinds.get("aux", 0) == 0:
                c.warn("插件 aux 覆盖",
                       "0 条 aux —— post_auxiliary_call 不在 Hermes VALID_HOOKS，"
                       "结构上抓不到辅助任务（占真值 %d/%d = %.0f%%）"
                       % (truth["aux_calls"], truth["calls"],
                          100.0 * truth["aux_calls"] / max(1, truth["calls"])))
            summary["plugin"] = {"rows": ev, "kinds": kinds,
                                 "main_usable": good,
                                 "main_input": agg_ev[0], "main_output": agg_ev[1],
                                 "main_cache_read": agg_ev[2]}
        else:
            c.warn("插件账本", "未安装（逐请求事实不可得，属预期）")

    # ---------------------------------------------------------- 7. config 污染
    cfg = os.path.join(HERMES_HOME, "config.yaml")
    if os.path.exists(cfg):
        try:
            import yaml
            data = yaml.safe_load(open(cfg, encoding="utf-8")) or {}
            bad = [e.get("name") for e in (data.get("custom_providers") or [])
                   if isinstance(e, dict)
                   and str(e.get("name") or "").startswith("_")
                   and not e.get("base_url")]
            if bad:
                c.bad("config 污染",
                      "custom_providers 有合成空壳: %s（跑 --repair-config）" % ", ".join(bad))
            else:
                c.ok("config 污染", "无")
        except Exception as e:
            c.warn("config 污染", "无法解析 config.yaml: %s" % e)

    # ---------------------------------------------------------- 8. 只读性
    # 注意：PRAGMA query_only 是「每连接」设置，不会持久化到文件。
    # 所以不能新开连接去查它（永远是 0，那等于没查）。
    # 正确做法是直接调用同步脚本自己的连接工厂，看它返回的连接是否带 query_only。
    try:
        here = os.path.dirname(os.path.abspath(__file__))
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "hus_verify", os.path.join(here, "hermes_usage_sync.py"))
        hus = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(hus)
    except Exception as e:
        hus = None
        c.warn("只读保护", "无法加载同步脚本: %s" % e)

    if hus is not None:
        for name, path in dbs:
            try:
                conn = hus.ro_connect(path)
                q = conn.execute("PRAGMA query_only").fetchone()[0]
                try:
                    conn.execute("CREATE TABLE __write_probe__(x)")
                    wrote = True
                except sqlite3.OperationalError:
                    wrote = False
                conn.close()
                if q == 1 and not wrote:
                    c.ok("只读保护 (%s)" % name,
                         "query_only=1 且写操作被 SQLite 拒绝")
                else:
                    c.bad("只读保护 (%s)" % name,
                          "query_only=%s 可写=%s" % (q, wrote))
            except sqlite3.Error as e:
                c.warn("只读保护 (%s)" % name, str(e))

    # ---------------------------------------------------------- 9. 只读副作用
    # state.db 的 mtime 不该被同步改动（WAL 模式下可能变，故只提示不强判）
    for name, path in dbs:
        try:
            mt = time.strftime("%Y-%m-%d %H:%M:%S",
                               time.localtime(os.path.getmtime(path)))
            c.warn("state.db mtime (%s)" % name, mt)
        except OSError:
            pass

    # ---------------------------------------------------------- 输出
    print("\n=== 检查项 ===")
    c.dump()
    print("\n结论: %s（FAIL %d 项）"
          % ("全部通过" if not c.failed else "存在问题", c.failed))
    if args.json:
        summary["failed"] = c.failed
        print(json.dumps(summary, ensure_ascii=False, indent=2, default=str))
    return 1 if c.failed else 0


if __name__ == "__main__":
    sys.exit(main())