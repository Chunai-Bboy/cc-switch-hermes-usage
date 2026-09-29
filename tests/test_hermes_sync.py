#!/usr/bin/env python3
"""hermes_usage_sync 单元测试：用临时 fixture DB 验证 delta 语义与容错。"""
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile

SCRIPT = os.path.join(os.path.dirname(__file__), "..", "scripts", "hermes_usage_sync.py")
SCRIPT = os.path.abspath(SCRIPT)

CC_SCHEMA = """
CREATE TABLE providers (id TEXT NOT NULL, app_type TEXT NOT NULL, name TEXT NOT NULL,
  settings_config TEXT NOT NULL, website_url TEXT, category TEXT, created_at INTEGER,
  sort_index INTEGER, notes TEXT, icon TEXT, icon_color TEXT, meta TEXT NOT NULL DEFAULT '{}',
  is_current BOOLEAN NOT NULL DEFAULT 0, in_failover_queue BOOLEAN NOT NULL DEFAULT 0,
  cost_multiplier TEXT NOT NULL DEFAULT '1.0', limit_daily_usd TEXT, limit_monthly_usd TEXT,
  provider_type TEXT, PRIMARY KEY (id, app_type));
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
  cache_read_tokens INTEGER, cache_write_tokens INTEGER, estimated_cost_usd REAL,
  actual_cost_usd REAL, cost_status TEXT, first_seen REAL, last_seen REAL);
"""


def make_env(tmp, schema_full=True):
    hermes_dir = os.path.join(tmp, "hermes")
    os.makedirs(hermes_dir, exist_ok=True)
    hdb = os.path.join(hermes_dir, "state.db")
    c = sqlite3.connect(hdb)
    c.executescript(HERMES_SCHEMA if schema_full else
                    "CREATE TABLE session_model_usage (session_id TEXT, model TEXT);")
    return hdb, c


def add_usage(conn, sid, model, task, calls, i, o, cr, cw, est, act, status, seen):
    conn.execute(
        "INSERT INTO session_model_usage VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (sid, model, task, calls, i, o, cr, cw, est, act, status, seen, seen))


def run_sync(tmp, cc_db, state_db, extra=()):
    env = dict(os.environ)
    env["LOCALAPPDATA"] = tmp
    cmd = [sys.executable, SCRIPT, "--cc-db", cc_db, "--state-db", state_db, *extra]
    r = subprocess.run(cmd, capture_output=True, text=True, env=env)
    return r


def totals(cc_db):
    c = sqlite3.connect(cc_db)
    row = c.execute("SELECT COUNT(*), SUM(input_tokens), SUM(output_tokens)"
                    " FROM proxy_request_logs WHERE data_source='hermes_session'").fetchone()
    c.close()
    return row


def test_baseline_and_delta():
    tmp = tempfile.mkdtemp()
    try:
        hdb, h = make_env(tmp)
        h.execute("INSERT INTO sessions VALUES ('s1','nous','chat','/w',10)")
        add_usage(h, "s1", "m1", "", 5, 100, 10, 50, 0, 0.01, 0.01, "actual", 1000.0)
        h.commit(); h.close()
        cc = os.path.join(tmp, "cc.db")
        c = sqlite3.connect(cc); c.executescript(CC_SCHEMA); c.commit(); c.close()
        st = os.path.join(tmp, "state.db")

        r = run_sync(tmp, cc, st)
        assert r.returncode == 0, r.stderr
        assert totals(cc) == (1, 100, 10), totals(cc)

        # cumulative 增长：再同步应只写增量
        h = sqlite3.connect(hdb)
        add_usage(h, "s1", "m1", "", 8, 260, 30, 120, 0, 0.02, 0.02, "actual", 2000.0)
        h.commit(); h.close()
        r = run_sync(tmp, cc, st)
        assert r.returncode == 0, r.stderr
        assert totals(cc) == (2, 260, 30), totals(cc)  # 100+160, 10+20

        # 第三次无变化：0 写入
        r = run_sync(tmp, cc, st)
        assert totals(cc) == (2, 260, 30), totals(cc)

        # provider 注册
        c = sqlite3.connect(cc)
        name = c.execute("SELECT name FROM providers WHERE id='_hermes_session'").fetchone()
        c.close()
        assert name == ("Hermes Agent",), name
        print("PASS baseline_and_delta")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_counter_reset():
    tmp = tempfile.mkdtemp()
    try:
        hdb, h = make_env(tmp)
        add_usage(h, "s1", "m1", "", 5, 100, 10, 0, 0, 0, 0, "", 1000.0)
        h.commit(); h.close()
        cc = os.path.join(tmp, "cc.db")
        c = sqlite3.connect(cc); c.executescript(CC_SCHEMA); c.commit(); c.close()
        st = os.path.join(tmp, "state.db")
        run_sync(tmp, cc, st)

        # 计数器重置为更小值 -> 以新基线写入
        h = sqlite3.connect(hdb)
        add_usage(h, "s1", "m1", "", 2, 30, 5, 0, 0, 0, 0, "", 2000.0)
        h.commit(); h.close()
        r = run_sync(tmp, cc, st, ("--verbose",))
        assert "计数器重置" in r.stdout, r.stdout
        assert totals(cc)[0] == 2, totals(cc)
        print("PASS counter_reset")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_pricing_fallback():
    tmp = tempfile.mkdtemp()
    try:
        hdb, h = make_env(tmp)
        add_usage(h, "s1", "free-m1", "", 1, 1_000_000, 0, 0, 0, 0, 0, "", 1000.0)
        h.commit(); h.close()
        cc = os.path.join(tmp, "cc.db")
        c = sqlite3.connect(cc); c.executescript(CC_SCHEMA)
        c.execute("INSERT INTO model_pricing VALUES ('free-m1','free','1.0','2.0','0.5','0')")
        c.commit(); c.close()
        st = os.path.join(tmp, "state.db")
        r = run_sync(tmp, cc, st)
        assert r.returncode == 0, r.stderr
        c = sqlite3.connect(cc)
        cost = c.execute("SELECT total_cost_usd FROM proxy_request_logs"
                         " WHERE data_source='hermes_session'").fetchone()[0]
        c.close()
        assert abs(float(cost) - 1.0) < 1e-6, cost  # 1M * $1.0/M
        print("PASS pricing_fallback")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_missing_hermes_db():
    tmp = tempfile.mkdtemp()
    try:
        cc = os.path.join(tmp, "cc.db")
        c = sqlite3.connect(cc); c.executescript(CC_SCHEMA); c.commit(); c.close()
        st = os.path.join(tmp, "state.db")
        r = run_sync(tmp, cc, st)
        assert r.returncode == 10, (r.returncode, r.stderr)
        print("PASS missing_hermes_db")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    test_baseline_and_delta()
    test_counter_reset()
    test_pricing_fallback()
    test_missing_hermes_db()
    print("ALL TESTS PASSED")
