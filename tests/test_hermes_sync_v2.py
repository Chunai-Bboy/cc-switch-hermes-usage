#!/usr/bin/env python3
"""v2 单测：内容寻址幂等键、messages.timestamp 优先、真实延迟归属、
只读加固、config 污染修复、未映射维度 sidecar 隔离、verify 验收契约。

跑法：  python tests/test_hermes_sync_v2.py -v
"""
import contextlib
import importlib.util
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
_spec = importlib.util.spec_from_file_location(
    "hus", HERE.parent / "scripts" / "hermes_usage_sync.py")
hus = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(hus)

CC_SCHEMA = """
CREATE TABLE providers (id TEXT NOT NULL, app_type TEXT NOT NULL, name TEXT NOT NULL,
  settings_config TEXT NOT NULL, meta TEXT NOT NULL DEFAULT '{}',
  cost_multiplier TEXT NOT NULL DEFAULT '1.0', provider_type TEXT,
  PRIMARY KEY (id, app_type));
CREATE TABLE model_pricing (model_id TEXT PRIMARY KEY, display_name TEXT,
  input_cost_per_million TEXT, output_cost_per_million TEXT,
  cache_read_cost_per_million TEXT, cache_creation_cost_per_million TEXT);
CREATE TABLE proxy_request_logs (
  request_id TEXT PRIMARY KEY, provider_id TEXT NOT NULL, app_type TEXT NOT NULL,
  model TEXT NOT NULL, request_model TEXT, input_tokens INTEGER, output_tokens INTEGER,
  cache_read_tokens INTEGER, cache_creation_tokens INTEGER, input_cost_usd TEXT,
  output_cost_usd TEXT, cache_read_cost_usd TEXT, cache_creation_cost_usd TEXT,
  total_cost_usd TEXT, latency_ms INTEGER, first_token_ms INTEGER, status_code INTEGER,
  error_message TEXT, session_id TEXT, provider_type TEXT, is_streaming INTEGER,
  cost_multiplier TEXT, created_at INTEGER, data_source TEXT);
"""

HERMES_SCHEMA = """
CREATE TABLE sessions (id TEXT PRIMARY KEY, billing_provider TEXT, billing_mode TEXT,
  cwd TEXT, api_call_count INTEGER DEFAULT 0);
CREATE TABLE session_model_usage (session_id TEXT, model TEXT, task TEXT,
  api_call_count INTEGER, input_tokens INTEGER, output_tokens INTEGER,
  cache_read_tokens INTEGER, cache_write_tokens INTEGER, reasoning_tokens INTEGER,
  estimated_cost_usd REAL, actual_cost_usd REAL, cost_status TEXT,
  first_seen REAL, last_seen REAL);
CREATE TABLE messages (id INTEGER PRIMARY KEY, session_id TEXT, role TEXT,
  content TEXT, timestamp REAL, finish_reason TEXT, token_count INTEGER);
"""

SNAP_DDL = ("CREATE TABLE IF NOT EXISTS snapshots (key TEXT PRIMARY KEY, "
            + ", ".join("%s REAL DEFAULT 0" % c for c in hus.SNAPSHOT_COLS) + ")")


@contextlib.contextmanager
def db(path):
    conn = sqlite3.connect(str(path))
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


@contextlib.contextmanager
def ro(path):
    conn = sqlite3.connect("file:%s?mode=ro" % str(path).replace("\\", "/"), uri=True)
    try:
        yield conn
    finally:
        conn.close()


class Base(unittest.TestCase):
    def setUp(self):
        self.t = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self.t.cleanup)
        self.dir = Path(self.t.name)
        self.hdb = self.dir / "state.db"
        self.ccdb = self.dir / "cc.db"
        self.sdb = self.dir / "sync.db"
        with db(self.hdb) as c:
            c.executescript(HERMES_SCHEMA)
        with db(self.ccdb) as c:
            c.executescript(CC_SCHEMA)

    def add_usage(self, sess, model, task, calls, i, o, cr=0, cw=0, rs=0,
                  est=0.0, act=0.0, status="unknown", first=1000.0, last=2000.0):
        with db(self.hdb) as c:
            c.execute("INSERT INTO session_model_usage VALUES "
                      "(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                      (sess, model, task, calls, i, o, cr, cw, rs, est, act,
                       status, first, last))

    def add_msg(self, sess, ts, role="assistant", finish="stop"):
        with db(self.hdb) as c:
            c.execute("INSERT INTO messages (session_id,role,content,timestamp,"
                      "finish_reason,token_count) VALUES (?,?,'x',?,?,NULL)",
                      (sess, role, ts, finish))

    def set_usage(self, sql, args=()):
        with db(self.hdb) as c:
            c.execute(sql, args)

    def snap_rows(self):
        return hus.read_hermes_snapshots([("default", self.hdb)])

    def sync(self, events=()):
        with db(self.sdb) as st:
            hus.ensure_snapshots_table(st)
            rows = self.snap_rows()
            deltas, snaps = hus.compute_deltas(rows, st)
            with db(self.ccdb) as cc:
                hus.ensure_provider(cc)
                n = hus.write_deltas(cc, deltas, hus.latency_index(events))
            hus.save_snapshots(st, snaps)
            hus.save_events(st, events)
            hus.save_extra_dimensions(st, rows)
        return n

    def cc_rows(self):
        with ro(self.ccdb) as c:
            return c.execute(
                "SELECT request_id,input_tokens,output_tokens,cache_read_tokens,"
                "latency_ms,status_code,created_at,total_cost_usd "
                "FROM proxy_request_logs WHERE data_source=? ORDER BY rowid",
                (hus.DATA_SOURCE,)).fetchall()


# ==================================================================== 幂等
class TestIdempotency(Base):
    """v2 核心：内容寻址幂等键（v1 用 last_seen_ms 收尾，会双计）。"""

    def test_last_seen_jitter_alone_creates_no_row(self):
        self.add_usage("s1", "m1", "", 5, 100, 20, 300)
        self.assertEqual(self.sync(), 1)
        before = self.cc_rows()
        self.set_usage("UPDATE session_model_usage SET last_seen=9999.0")
        self.assertEqual(self.sync(), 0, "last_seen 抖动不得产生新行")
        self.assertEqual(self.cc_rows(), before)

    def test_counters_advance_adds_exactly_one_row(self):
        self.add_usage("s1", "m1", "", 5, 100, 20, 300)
        self.sync()
        self.add_usage("s1", "m1", "", 7, 260, 60, 900)
        self.assertEqual(self.sync(), 1)
        rows = self.cc_rows()
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[-1][1:7], (160, 40, 600, 0, 200, 2000))

    def test_crash_replay_is_idempotent(self):
        """log 写成功但快照丢失 -> 重跑必须零新增。"""
        self.add_usage("s1", "m1", "", 5, 100, 20, 300)
        self.sync()
        before = self.cc_rows()
        with db(self.sdb) as st:
            st.execute("DELETE FROM snapshots")
        self.sync()
        self.assertEqual(self.cc_rows(), before, "崩溃重跑必须无新增行")

    def test_cost_only_correction_is_imported(self):
        self.add_usage("s1", "m1", "", 5, 100, 20, 300, est=0.5)
        self.sync()
        self.set_usage("UPDATE session_model_usage SET estimated_cost_usd=0.2,"
                       "cost_status='actual', actual_cost_usd=0.2")
        self.assertEqual(self.sync(), 1, "估算->实际的成本回滚必须能导入")

    def test_counter_reset_starts_new_baseline(self):
        self.add_usage("s1", "m1", "", 9, 900, 90)
        self.sync()
        self.set_usage("UPDATE session_model_usage SET api_call_count=1,"
                       "input_tokens=10, output_tokens=1")
        self.assertEqual(self.sync(), 1, "重置后应重建基线")
        self.assertEqual(len(self.cc_rows()), 2)

    def test_request_id_has_no_last_seen(self):
        self.add_usage("s1", "m1", "", 5, 100, 20, 300, last=1789134946681)
        self.sync()
        rid = self.cc_rows()[0][0]
        self.assertNotIn("1789134946681", rid, "request_id 不得再含 last_seen_ms")
        self.assertIn("@c5:", rid, "request_id 应含内容寻址标记")


# ==================================================================== 只读
class TestReadOnly(Base):
    def test_ro_connect_rejects_writes(self):
        with self.assertRaises(sqlite3.OperationalError):
            with ro(self.hdb) as c:
                c.execute("PRAGMA query_only=ON")
                c.execute("DELETE FROM session_model_usage")

    def test_ro_connect_sets_query_only(self):
        conn = hus.ro_connect(self.hdb)
        try:
            self.assertEqual(conn.execute("PRAGMA query_only").fetchone()[0], 1)
        finally:
            conn.close()

    def test_state_db_unchanged_bytewise(self):
        self.add_usage("s1", "m1", "", 3, 10, 5, 7)
        self.add_msg("s1", 555.0)
        before = self.hdb.read_bytes()
        self.sync()
        self.assertEqual(self.hdb.read_bytes(), before, "state.db 必须字节级不变")


# ==================================================================== 时间戳
class TestTimestamp(Base):
    def test_last_message_ts_overrides_last_seen(self):
        self.add_usage("s1", "m1", "", 2, 10, 5, last=100000.0)
        self.add_msg("s1", 777.0)
        row = self.snap_rows()[0]
        self.assertEqual(row["last_msg_ts"], 777.0)
        self.assertNotEqual(row["last_msg_ts"], row["last_seen"])

    def test_created_at_uses_message_timestamp(self):
        self.add_usage("s1", "m1", "", 2, 10, 5, last=100000.0)
        self.add_msg("s1", 777.0)
        self.sync()
        self.assertEqual(self.cc_rows()[0][6], 777)

    def test_falls_back_to_last_seen_without_messages(self):
        self.add_usage("s1", "m1", "", 2, 10, 5, last=100000.0)
        self.sync()
        self.assertEqual(self.cc_rows()[0][6], 100000)


# ==================================================================== 插件延迟
class TestPluginLatency(Base):
    EVENTS = [
        {"event_id": "main:a:success", "kind": "main", "session_id": "s1", "model": "m1",
         "duration_ms": 1000, "status": "success", "status_code": None, "usage_available": 1,
         "input_tokens": 1, "output_tokens": 1, "cache_read_tokens": 1,
         "cache_write_tokens": 0, "reasoning_tokens": 0},
        {"event_id": "main:b:success", "kind": "main", "session_id": "s1", "model": "m1",
         "duration_ms": 3000, "status": "success", "status_code": None, "usage_available": 1,
         "input_tokens": 1, "output_tokens": 1, "cache_read_tokens": 1,
         "cache_write_tokens": 0, "reasoning_tokens": 0},
        {"event_id": "main:c:error", "kind": "main", "session_id": "s1", "model": "m1",
         "duration_ms": 9999, "status": "error", "status_code": 500, "usage_available": 0,
         "input_tokens": None, "output_tokens": None, "cache_read_tokens": None,
         "cache_write_tokens": None, "reasoning_tokens": None},
    ]
    # 事件时间戳必须落在 (prev_last_seen, cur_last_seen] 这个真实窗口里，
    # 否则 BUG2 的修复会正确地拒绝给出耗时（宁可 0 也不编）。
    # 类体里的推导式看不见同层常量，所以直接写死 2_500_000。
    TIMED = [dict(e, started_at_ms=2_500_000 + i * 1000)
             for i, e in enumerate(EVENTS)]

    def test_median_excludes_errors(self):
        idx = hus.latency_index(self.TIMED)
        got = hus.window_latency(idx, "s1", "m1", 2000.0, 3000.0)
        self.assertEqual(got, 2000,
                         "错误请求 9999ms 不得污染成功率延迟")

    def test_first_baseline_has_no_window_so_no_latency(self):
        """BUG2：首次同步没有「上一次」，不能编一个中位数出来。"""
        self.add_usage("s1", "m1", "", 3, 30, 3)
        self.sync(self.TIMED)
        self.assertEqual(self.cc_rows()[0][4], 0,
                         "基线行必须留 0，而不是拿全会话中位数充数")

    def test_latency_written_when_plugin_present(self):
        self.add_usage("s1", "m1", "", 3, 30, 3)
        self.sync(self.TIMED)                       # 基线
        with db(self.hdb) as c:
            c.execute("DELETE FROM session_model_usage")
        self.add_usage("s1", "m1", "", 6, 60, 6, last=3000.0)
        self.sync(self.TIMED)                       # 窗口 (2000, 3000]
        self.assertEqual(self.cc_rows()[-1][4], 2000)

    def test_latency_zero_without_plugin(self):
        self.add_usage("s1", "m1", "", 3, 30, 3)
        self.sync()
        self.assertEqual(self.cc_rows()[0][4], 0)

    def test_events_go_to_sidecar_never_to_proxy_logs(self):
        self.add_usage("s1", "m1", "", 3, 30, 3)
        self.sync(self.EVENTS)
        with ro(self.sdb) as c:
            n = c.execute("SELECT COUNT(*) FROM request_events").fetchone()[0]
        self.assertEqual(n, 3, "3 条事件应进 sidecar")
        self.assertEqual(len(self.cc_rows()), 1,
                         "proxy_request_logs 只应有 1 条聚合行（绝不能 3+1）")


class TestLatencyTaskIsolation(Base):
    """回归：辅助调用行绝不能套用主循环的耗时（实测踩过的坑）。"""

    EV = [{"event_id": "main:a:success", "kind": "main", "session_id": "s1", "model": "m1",
           "duration_ms": 4000, "status": "success", "status_code": None,
           "usage_available": 1, "input_tokens": 1, "output_tokens": 1,
           "cache_read_tokens": 1, "cache_write_tokens": 0, "reasoning_tokens": 0,
           "started_at_ms": 2_500_000}]

    def test_main_row_gets_latency_aux_row_does_not(self):
        self.add_usage("s1", "m1", "", 3, 30, 3)
        self.add_usage("s1", "m1", "title_generation", 1, 5, 1)
        self.sync(self.EV)                                   # 基线
        with db(self.hdb) as c:
            c.execute("DELETE FROM session_model_usage")
        self.add_usage("s1", "m1", "", 6, 60, 6, last=3000.0)
        self.add_usage("s1", "m1", "title_generation", 1, 5, 1, last=3000.0)
        self.sync(self.EV)                                   # 窗口 (2000, 3000]
        by_rid = {r[0]: r for r in self.cc_rows()}
        main_rid = [k for k in by_rid if "@c6:" in k and "title_generation" not in k][0]
        aux_rid = [k for k in by_rid if "title_generation" in k and "@c1:" in k][0]
        self.assertEqual(by_rid[main_rid][4], 4000, "主循环行应填真实耗时")
        self.assertEqual(by_rid[aux_rid][4], 0,
                         "辅助调用行没有插件观测，必须留 0 而不是套主循环中位数")

    def test_latency_index_ignores_non_main_kinds(self):
        idx = hus.latency_index(self.EV + [dict(self.EV[0], event_id="aux:x",
                                                 kind="aux", duration_ms=9999)])
        self.assertEqual(hus.window_latency(idx, "s1", "m1", 2000.0, 3000.0), 4000)


# ==================================================================== 未映射维度
class TestUnmappedDimensions(Base):
    """CC Switch 全库没有 reasoning 列，只能落 sidecar；这条契约不能被悄悄改掉。"""

    def test_reasoning_is_read_and_kept_in_sidecar(self):
        self.add_usage("s1", "m1", "", 2, 10, 5, rs=777)
        rows = self.snap_rows()
        self.assertEqual(rows[0]["reasoning"], 777, "reasoning 必须从 state.db 读出来")
        self.sync()
        self.assertEqual(len(self.cc_rows()), 1)
        with ro(self.sdb) as c:
            n = c.execute("SELECT SUM(reasoning_tokens) FROM dimension_totals").fetchone()[0]
        self.assertEqual(n, 777, "reasoning 必须落在 sidecar.dimension_totals")
        with ro(self.ccdb) as c:
            cols = [r[1] for r in c.execute("PRAGMA table_info(proxy_request_logs)")]
        self.assertEqual([x for x in cols if "reason" in x.lower()], [],
                         "proxy_request_logs 不得被私自加列（CC Switch 侧零改动）")

    def test_missing_reasoning_column_degrades_to_zero(self):
        old = self.dir / "old.db"
        with db(old) as c:
            c.executescript(HERMES_SCHEMA.replace("reasoning_tokens INTEGER, ", ""))
            c.execute("INSERT INTO session_model_usage (session_id,model,task,"
                      "api_call_count,input_tokens,output_tokens,cache_read_tokens,"
                      "cache_write_tokens,estimated_cost_usd,actual_cost_usd,cost_status,"
                      "first_seen,last_seen) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                      ("s1", "m1", "", 2, 10, 5, 0, 0, 0.0, 0.0, "unknown", 1.0, 2.0))
        rows = hus.read_hermes_snapshots([("default", old)])
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["reasoning"], 0, "缺列应降级为 0 而不是抛 SQL 错")


# ==================================================================== config 污染
class TestConfigPollution(unittest.TestCase):
    def setUp(self):
        import yaml
        self.yaml = yaml
        self.t = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self.t.cleanup)
        self.home = Path(self.t.name) / "hermes"
        self.home.mkdir(parents=True)
        self.cfg = self.home / "config.yaml"
        self._old = hus.HERMES_HOME
        hus.HERMES_HOME = str(self.home)
        self.addCleanup(lambda: setattr(hus, "HERMES_HOME", self._old))

    def write(self, providers):
        self.cfg.write_text(self.yaml.safe_dump({"custom_providers": providers}),
                            encoding="utf-8")

    def test_detects_synthetic_shell(self):
        self.write([{"name": "stepfun", "base_url": "https://x"},
                    {"name": "_hermes_session"}])
        r = hus.check_config_pollution()
        self.assertEqual(r.code, 41)
        self.assertIn("_hermes_session", r.msg)

    def test_clean_config_ok(self):
        self.write([{"name": "stepfun", "base_url": "https://x"}])
        self.assertEqual(hus.check_config_pollution().code, 0)

    def test_synthetic_with_base_url_is_not_flagged(self):
        self.write([{"name": "_custom", "base_url": "https://x"}])
        self.assertEqual(hus.check_config_pollution().code, 0,
                         "带 base_url 的自定义 provider 不算污染")

    def test_repair_only_removes_shells_and_backs_up(self):
        self.write([{"name": "stepfun", "base_url": "https://x", "api_key": "k"},
                    {"name": "_hermes_session"},
                    {"name": "_custom", "base_url": "https://y"}])
        hus.repair_config()
        after = self.yaml.safe_load(self.cfg.read_text(encoding="utf-8"))
        self.assertEqual([e["name"] for e in after["custom_providers"]],
                         ["stepfun", "_custom"])
        self.assertTrue((self.home / "config.yaml.repairsync-bak").exists())


# ==================================================================== 报告
class TestReport(Base):
    EVENTS = TestPluginLatency.EVENTS

    def test_report_shape(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as d:
            sdb = Path(d) / "s.db"
            with db(sdb) as st:
                st.execute(SNAP_DDL)
                hus.save_extra_dimensions(st, [{"profile": "default", "session_id": "s",
                                                "model": "m", "task": "", "reasoning": 42,
                                                "cache_write": 0}])
                r = hus.report(st, self.EVENTS, str(sdb))
            self.assertIn("truthfulness_note", r)
            self.assertEqual(r["event_totals"]["rows"], 3)
            self.assertEqual(r["event_totals"]["error"], 1)
            self.assertEqual(r["event_totals"]["status_codes"], [500])
            self.assertEqual(r["event_totals"]["latency_ms"]["median"], 2000)
            self.assertEqual(r["sidecar_unmapped"]["reasoning_tokens"], 42)


# ==================================================================== verify 契约
class TestVerifyScript(Base):
    """锁住 verify 的核心契约（v2.2 基准）：
    CC == 账本 必须成立；账本领先实时真值 = 上游丢历史（WARN 不是 FAIL）；
    账本落后实时真值 = 漏同步（FAIL）。真值路径用 --hermes-db 隔离到沙箱。"""

    def _run_verify(self):
        sp = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                          "..", "scripts", "verify_hermes_usage.py")
        return subprocess.run([sys.executable, "-X", "utf8", sp,
                               "--cc-db", str(self.ccdb), "--state-db", str(self.sdb),
                               "--hermes-home", str(self.dir)],
                              capture_output=True, text=True, encoding="utf-8")

    def test_verify_outputs_reconciliation_table(self):
        self.add_usage("s1", "m1", "", 5, 1000, 200, 500)
        self.sync()
        r = self._run_verify()
        self.assertIn("input tokens", r.stdout, "必须输出三方对账表")
        self.assertIn("实时真值", r.stdout)
        self.assertIn("同步账本", r.stdout)

    def test_verify_all_aligned_no_fail(self):
        """账本==CC==真值 时 verify 必须 0 FAIL。"""
        self.add_usage("s1", "m1", "", 5, 1000, 200, 500)
        self.add_usage("s1", "m1", "approval", 3, 40, 6)
        self.sync()
        r = self._run_verify()
        self.assertNotIn("[FAIL]", r.stdout, "全对齐时不应有 FAIL 检查项:\n" + r.stdout)
        self.assertEqual(r.returncode, 0)

    def test_verify_upstream_lost_is_warn_not_fail(self):
        """同步后上游删会话：账本领先真值必须 WARN，绝不 FAIL，
        且必须点名是哪个会话丢了（v2.2 修正的核心场景）。"""
        self.add_usage("s1", "m1", "", 5, 1000, 200, 500)
        self.add_usage("s2", "m1", "", 4, 888, 100)
        self.sync()
        self.set_usage("DELETE FROM session_model_usage WHERE session_id='s2'")
        r = self._run_verify()
        self.assertIn("上游丢历史", r.stdout)
        self.assertIn("s2", r.stdout, "必须点名丢失的会话")
        self.assertNotIn("[FAIL]", r.stdout, "上游丢历史不能判 FAIL 检查项:\n" + r.stdout)
        self.assertEqual(r.returncode, 0, "WARN-only 必须退出 0")

    def test_verify_unsynced_delta_is_fail(self):
        """真值前进但没再 sync：账本落后真值必须 FAIL（这才是该报警的情形）。"""
        self.add_usage("s1", "m1", "", 5, 1000, 200, 500)
        self.sync()
        self.set_usage("UPDATE session_model_usage SET api_call_count=6,"
                       " input_tokens=1500, output_tokens=300")
        r = self._run_verify()
        self.assertIn("FAIL", r.stdout)
        self.assertIn("有增量未同步", r.stdout)
        self.assertEqual(r.returncode, 1)

    def test_verify_ledger_cc_mismatch_is_fail(self):
        """账本与 CC 不等（同步器半写入/算错）必须 FAIL，哪怕两边都不等于真值。"""
        self.add_usage("s1", "m1", "", 5, 1000, 200, 500)
        self.sync()
        with db(self.ccdb) as c:
            c.execute("UPDATE proxy_request_logs SET input_tokens = input_tokens + 7")
        r = self._run_verify()
        self.assertIn("账本≠CC", r.stdout)
        self.assertEqual(r.returncode, 1)

    def test_verify_latency_pollution_is_flagged(self):
        """辅助任务行若带了 latency，verify 必须报出来（v2 曾踩过这个坑）。"""
        self.add_usage("s1", "m1", "title_generation", 1, 5, 1)
        self.sync()
        with db(self.ccdb) as c:
            c.execute("UPDATE proxy_request_logs SET latency_ms = 5000")
        r = self._run_verify()
        self.assertIn("latency", r.stdout)
        self.assertIn("FAIL", r.stdout, "辅助行带耗时必须判 FAIL")

    def test_verify_reports_unmapped_dimensions(self):
        self.add_usage("s1", "m1", "", 2, 10, 5, rs=777)
        self.sync()
        r = self._run_verify()
        self.assertIn("sidecar", r.stdout, "必须报告 CC Switch 存不下的维度")
        self.assertIn("777", r.stdout)


if __name__ == "__main__":
    unittest.main(verbosity=2)