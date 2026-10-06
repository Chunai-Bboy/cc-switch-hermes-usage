#!/usr/bin/env python3
"""
Hermes → CC Switch 用量同步验收检查（v2.2）

与 v1 的根本区别：v1 只验「有没有数据」，v2 验「数据对不对」。

三方对账（v2.2 修正基准）：
  Hermes 实时真值（session_model_usage） vs 同步账本（sync-state.db snapshots）
  vs CC Switch 仪表盘（proxy_request_logs）。
  - CC 必须等于账本：不等 = 同步器自己的账算错了，FAIL。
  - 账本可以大于实时真值：Hermes 升级/压缩会删老会话聚合行，而账本与 CC
    保留已同步过的真实历史。这记 WARN「上游丢历史」，不是 FAIL。
    实测：0.21.2→0.21.5 升级删掉 10 个会话的累计 882,626 input。
    绝不允许用 --reset-baseline 删真实历史来把检查做绿。
  - 账本小于实时真值 = 有增量还没同步，FAIL（跑一次 sync）。

退出码：
  0  无 FAIL（WARN 允许）
  1  有 FAIL 项（数据不一致 / 缺失）
  2  环境不可用（Hermes 或 CC Switch 库找不到）
"""

import argparse
import importlib.util
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


def hermes_dbs(base=None):
    base = base or HERMES_HOME
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


def _zero():
    return {"calls": 0, "input": 0, "output": 0, "cache_read": 0,
            "cache_write": 0, "reasoning": 0}


def truth_from_hermes(dbs):
    """真值：所有 profile 的 session_model_usage 累计值（实时，会被上游删行）。

    返回 (总量, 按序列 {(profile, session, model, task): {...}}, 按 task)。
    序列粒度不含 billing 三列：同 key 多 billing 行在总量与序列层都被正确求和。
    """
    tot = _zero()
    tot["main_calls"] = 0
    tot["aux_calls"] = 0
    per_series = {}
    per_task = {}
    for name, path in dbs:
        with closing(ro(path)) as c:
            ucols = {r[1] for r in c.execute("PRAGMA table_info(session_model_usage)")}
            has = lambda x: x if x in ucols else "NULL"
            sql = ("SELECT COALESCE(session_id,''), COALESCE(model,'?'),"
                   " COALESCE(task,''), COALESCE(api_call_count,0),"
                   " COALESCE(input_tokens,0), COALESCE(output_tokens,0),"
                   " COALESCE(cache_read_tokens,0), COALESCE(cache_write_tokens,{cw}),"
                   " COALESCE(reasoning_tokens,{rs})"
                   " FROM session_model_usage").format(
                       cw=has("cache_write_tokens"), rs=has("reasoning_tokens"))
            for sid, model, task, calls, i, o, cr, cw, rs in c.execute(sql):
                tot["calls"] += calls; tot["input"] += i; tot["output"] += o
                tot["cache_read"] += cr; tot["cache_write"] += cw
                tot["reasoning"] += rs
                s = per_series.setdefault((name, sid, model, task), _zero())
                s["calls"] += calls; s["input"] += i; s["output"] += o
                s["cache_read"] += cr; s["cache_write"] += cw
                s["reasoning"] += rs
                if task == "":
                    tot["main_calls"] = tot.get("main_calls", 0) + calls
                else:
                    tot["aux_calls"] = tot.get("aux_calls", 0) + calls
                a = per_task.setdefault(task or "(main)", [0, 0])
                a[0] += calls; a[1] += cr
    return tot, per_series, per_task


def ledger_from_snapshots(state_db):
    """同步账本：snapshots 每序列最后记录的累计值（含上游已删的序列）。

    返回 {(profile, session, model, task): {...}}，缺库缺表返回 None。
    """
    if not os.path.exists(state_db):
        return None
    out = {}
    with closing(ro(state_db)) as st:
        tbls = {r[0] for r in st.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        if "snapshots" not in tbls:
            return None
        for k, calls, i, o, cr, cw in st.execute(
                "SELECT key, api_calls, input, output, cache_read,"
                " cache_write FROM snapshots"):
            p = k.split("|", 3)
            if len(p) < 4:
                continue
            a = out.setdefault((p[0], p[1], p[2], p[3]), _zero())
            a["calls"] += calls or 0; a["input"] += i or 0; a["output"] += o or 0
            a["cache_read"] += cr or 0; a["cache_write"] += cw or 0
    return out


def lost_sessions(ledger, truth_series):
    """账本里还在、实时真值里已整个消失的会话（= 上游删了聚合行）。

    返回 [(session_id, input 累计)]，按 input 降序。session 级判断：
    同一 session 只要还有任何一条序列活着，就不算丢失（可能是 billing
    身份迁移换了 key，旧 key 的 delta 已被新 key 承接，CC/账本仍然自洽）。
    """
    live = {(p, s) for (p, s, _m, _t) in truth_series}
    acc = {}
    for (p, s, _m, _t), v in ledger.items():
        if (p, s) in live:
            continue
        a = acc.setdefault(s, [0, 0])
        a[0] += v["calls"] or 0
        a[1] += v["input"] or 0
    return sorted(((s, v[1]) for s, v in acc.items() if v[1]),
                  key=lambda kv: -kv[1])


def main():
    ap = argparse.ArgumentParser(description="Hermes → CC Switch 同步验收")
    ap.add_argument("--cc-db", default=os.path.expanduser(r"~\.cc-switch\cc-switch.db"))
    ap.add_argument("--state-db", default=STATE_DB)
    ap.add_argument("--hermes-home", default=None,
                    help="覆盖 Hermes 主目录（真值发现/插件账本/config 检查全走这里，"
                         "测试隔离用；默认 %s）" % HERMES_HOME)
    ap.add_argument("--json", action="store_true", help="额外输出 JSON 结论")
    args = ap.parse_args()
    c = Check()
    summary = {}

    home = args.hermes_home or HERMES_HOME
    plugin_ledger = os.path.join(home, "ccswitch-usage.sqlite")

    # ---------------------------------------------------------- 0. 环境
    dbs = hermes_dbs(home)
    if not dbs:
        print("ERROR[10] 未发现 Hermes state.db（%s）" % home, file=sys.stderr)
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
        # 基准关系（v2.2）：CC == 账本 必须成立（同步器自己的账）；
        # 账本 >= 实时真值 是上游删历史的正常表现（WARN）；
        # 账本 < 实时真值 才是漏同步（FAIL）。
        truth, truth_series, per_task = truth_from_hermes(dbs)
        ledger = ledger_from_snapshots(args.state_db)
        summary["truth"] = truth
        summary["cc_switch"] = {"rows": cnt, "input": a_in, "output": a_out,
                                "cache_read": a_cr, "cache_creation": a_cw,
                                "cost_usd": round(a_cost, 6), "latency_rows": a_lat}

        def _lt(dim):
            return sum(v[dim] for v in ledger.values()) if ledger else None

        print("\n=== 三方对账：实时真值(session_model_usage) vs 同步账本(snapshots) "
              "vs CC Switch ===")
        print("  %-18s %16s %16s %16s %s"
              % ("维度", "实时真值", "同步账本", "CC Switch 合计", "判定"))
        lost_rows = []

        def _line(name, tv, lv, cv, verdict):
            print("  %-18s %16s %16s %16s %s"
                  % (name, "{:,}".format(int(tv)),
                     "{:,}".format(int(lv)) if lv is not None else "（无账本）",
                     "{:,}".format(int(cv)) if cv is not None else "聚合行(不按行计)",
                     verdict))

        # API 调用数：CC 是窗口 delta 行，行数与调用数不可比，只查 账本 vs 真值。
        tv_calls = truth["calls"]
        lv_calls = _lt("calls")
        if lv_calls is None:
            _line("API 调用数", tv_calls, None, None, "n/a")
        elif lv_calls < tv_calls:
            _line("API 调用数", tv_calls, lv_calls, None, "FAIL 有增量未同步")
            c.bad("对账 API 调用数",
                  "账本落后实时真值 {:,}（跑一次 sync）".format(int(tv_calls - lv_calls)))
        else:
            _line("API 调用数", tv_calls, lv_calls, None,
                  "一致" if lv_calls == tv_calls else "WARN 上游丢历史")
            if lv_calls > tv_calls:
                c.warn("对账 API 调用数",
                       "账本比实时真值多 {:,}：Hermes 删过老会话聚合行"
                       .format(int(lv_calls - tv_calls)))

        # token 三维度：必须 账本 == CC；与真值的关系只允许账本 >= 真值。
        for name, dim, cv in [("input tokens", "input", a_in),
                              ("output tokens", "output", a_out),
                              ("cache_read tokens", "cache_read", a_cr)]:
            tv = truth[dim]
            lv = _lt(dim)
            if lv is None:  # 账本不可用：退回旧口径 CC vs 真值
                d = cv - tv
                _line(name, tv, None, cv, "一致" if d == 0 else "不一致")
                (c.ok if d == 0 else c.bad)(
                    "对账 " + name,
                    "一致" if d == 0 else "差 {:+,}（账本不可用，按旧口径）".format(int(d)))
                continue
            cc_d = lv - cv
            if cc_d != 0:
                _line(name, tv, lv, cv, "FAIL 账本≠CC")
                c.bad("对账 " + name,
                      "同步账本与 CC 差 {:+,}（同步器算错或半写入）".format(int(cc_d)))
            elif lv < tv:
                _line(name, tv, lv, cv, "FAIL 有增量未同步")
                c.bad("对账 " + name,
                      "账本落后实时真值 {:+,}（跑一次 sync）".format(int(lv - tv)))
            elif lv > tv:
                _line(name, tv, lv, cv, "WARN 上游丢历史")
                c.warn("对账 " + name,
                       "账本比实时真值多 {:,}：Hermes 删过老会话聚合行，"
                       "历史已安全存于账本/CC".format(int(lv - tv)))
                if dim == "input":
                    lost_rows = lost_sessions(ledger, truth_series)
                    summary["upstream_lost"] = {
                        "sessions": len(lost_rows),
                        "input": sum(v for _, v in lost_rows)}
            else:
                _line(name, tv, lv, cv, "一致")
                c.ok("对账 " + name, "一致")

        if lost_rows:
            print("  上游已删的会话（其历史仍在账本/CC，不是同步故障）：")
            for sid, in_in in lost_rows:
                print("    %s  input %s" % (sid, "{:,}".format(int(in_in))))

        # cache_write：CC Switch 用 cache_creation 承载；账本 == CC 才算对。
        cw_ledger = _lt("cache_write")
        cw_ok = (cw_ledger == a_cw) if cw_ledger is not None else (truth["cache_write"] == a_cw)
        _line("cache_write", truth["cache_write"], cw_ledger, a_cw,
              "一致" if cw_ok else "WARN")
        (c.ok if cw_ok else c.warn)(
            "对账 cache_write",
            "一致" if cw_ok else "账本 {:,} / CC cache_creation {:,}".format(
                cw_ledger or 0, a_cw))

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
        if os.path.exists(plugin_ledger):
            with closing(ro(plugin_ledger)) as lg:
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
                       "0 条 aux —— 本窗口可能确无辅助调用，或 Hermes 版本未含 "
                       "post_auxiliary_call（该钩子自 0.21.5 起提供）。占真值 %d/%d = %.0f%%"
                       % (truth.get("aux_calls", 0), truth.get("calls", 0),
                          100.0 * truth.get("aux_calls", 0) / max(1, truth.get("calls", 0))))
            summary["plugin"] = {"rows": ev, "kinds": kinds,
                                 "main_usable": good,
                                 "main_input": agg_ev[0], "main_output": agg_ev[1],
                                 "main_cache_read": agg_ev[2]}
        else:
            c.warn("插件账本", "未安装（逐请求事实不可得，属预期）")

    # ---------------------------------------------------------- 7. config 污染
    cfg = os.path.join(home, "config.yaml")
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