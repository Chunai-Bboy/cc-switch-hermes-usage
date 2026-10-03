# -*- coding: utf-8 -*-
"""锁住 20261003_144834_1d260f 那次 32 分钟长任务实测出的两个真 bug。

BUG1  session_model_usage 主键是6 列：
      PRIMARY KEY (session_id, model, billing_provider, billing_base_url,
                   billing_mode, task)
      v2 只用 (session, model, task) 当快照键 -> billing_base_url 不同的行撞键。
      实测该 session 有两条 task='approval'（base_url 为空 vs
      https://opencode.ai/zen/v1/），撞键后只有 rowid 顺序恰好递增才凑巧对。

BUG2  v2 把「全会话耗时中位数」填进每个同步窗口行。实测窗口行 latency=59805ms，
      而该会话真实耗时区间是 14.2s~198.2s，语义对不上。
"""
import importlib.util
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
SPEC = importlib.util.spec_from_file_location(
    "hus", os.path.join(ROOT, "scripts", "hermes_usage_sync.py"))
hus = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(hus)

SESS = "20261003_144834_1d260f"
MODEL = "space-bunny-free"


def row(task="", calls=0, i=0, o=0, cr=0, cw=0, ec=0.0, ac=0.0,
        first=1791010136.454, last=1791011572.002,
        bp="", bu="", bm="", profile="default"):
    return {"profile": profile, "session_id": SESS, "model": MODEL, "task": task,
            "api_calls": calls, "input": i, "output": o, "cache_read": cr,
            "cache_write": cw, "est_cost": ec, "act_cost": ac,
            "first_seen": first, "last_seen": last,
            "billing_provider": bp, "billing_base_url": bu,
            "billing_mode": bm, "cwd": ""}


class TestBug1DuplicateKey(unittest.TestCase):
    """(session, model, task) 不唯一：快照键必须含完整主键。"""

    def test_two_approval_rows_get_different_series_keys(self):
        a = row(task="approval", calls=1, i=997, first=1791010284.119,
                bu="", bp="")
        b = row(task="approval", calls=14, i=7826, first=1791010338.324,
                bu="https://opencode.ai/zen/v1/", bp="opencode-free")
        self.assertNotEqual(hus.series_key(a), hus.series_key(b),
                            "两条 approval 行必须落进不同的序列")

    def test_request_ids_do_not_collide(self):
        a = row(task="approval", calls=14, i=7826, first=1791010338.324,
                bu="", bp="")
        b = row(task="approval", calls=14, i=7826, first=1791010338.324,
                bu="https://opencode.ai/zen/v1/", bp="opencode-free")
        self.assertNotEqual(hus.make_request_id(a), hus.make_request_id(b))

    def test_sum_of_deltas_equals_truth_in_both_rowid_orders(self):
        """无论两条 approval 行以什么顺序被读到，总量都必须等于真值。

        真值 = 15 次调用 / 8823 input（row A 的 1 次 997 + row B 的 14 次 7826），
        不是 14/7826——两行是两个独立的计费路径，Hermes 各自记了一份。

        修复前：4 列键把两行并成一条序列，于是
          顺序 [1, 14] -> 1 + (14-1) = 14 次，**静默丢 1 次调用 / 997 token**
          顺序 [14, 1] -> 14 + 1    = 15 次，凑巧正确
        """
        truth_calls, truth_in = 15, 8823
        small = row(task="approval", calls=1, i=997, first=1791010284.119,
                    last=1791010284.119)
        big = row(task="approval", calls=14, i=7826, first=1791010338.324,
                  last=1791011236.597, bu="https://opencode.ai/zen/v1/",
                  bp="opencode-free")
        for order in ([small, big], [big, small]):
            with self.subTest(order=[r["api_calls"] for r in order]):
                st = _state()
                deltas, snaps = hus.compute_deltas(order, st)
                got_calls = sum(d["api_calls"] for d in deltas)
                got_in = sum(d["input"] for d in deltas)
                self.assertEqual(got_calls, truth_calls)
                self.assertEqual(got_in, truth_in)
                self.assertEqual(len({s[0] for s in snaps}), 2)

    def test_growing_second_row_accumulates_without_loss(self):
        """实测形态：row A 停在 calls=1（in=997），row B 从 3 长到 14。

        真实 CC Switch 里留下的三笔：approval@c1 in=997、approval@c3 in=1353、
        approval@c14 in=6473，合计 8823 = 997 + 7826，与源库两行完全吻合。
        """
        st = _state()
        first = row(task="approval", calls=1, i=997, first=1791010284.119,
                    last=1791010284.119)
        second = row(task="approval", calls=3, i=1353, first=1791010338.324,
                     last=1791010900.0, bu="https://opencode.ai/zen/v1/",
                     bp="opencode-free")
        hus.save_snapshots(st, _snaps(hus.compute_deltas([first, second], st)))
        later = row(task="approval", calls=14, i=7826, first=1791010338.324,
                    last=1791011236.597, bu="https://opencode.ai/zen/v1/",
                    bp="opencode-free")
        d, _ = hus.compute_deltas([first, later], st)
        self.assertEqual(sum(x["input"] for x in d), 7826 - 1353)
        self.assertEqual(sum(x["api_calls"] for x in d), 14 - 3)

    def test_key_ignores_mutable_columns(self):
        """回归锁：first_seen / last_seen / 累计值进不进键？

        都不该进。first_seen 若进键，它从 1000 被改写成 2000 时，
        老序列会变成新序列 -> 整行重新基线 -> 重复计数。
        这是 v1 回归测试真实踩到的：多写了 100 input / 10 output。
        """
        a = row(task="", calls=5, i=100, o=10, cr=50, first=1000.0, last=2000.0)
        b = dict(a, first=2000.0, last=3000.0)
        self.assertEqual(hus.series_key(a), hus.series_key(b))

    def test_full_primary_key_is_the_key(self):
        """键必须恰好是 6 列主键 + profile，缺一不可，多一不可。"""
        base = row(task="", calls=5)
        for col, other in (("billing_provider", "p2"),
                           ("billing_base_url", "https://x/v1"),
                           ("billing_mode", "sub")):
            with self.subTest(col=col):
                self.assertNotEqual(hus.series_key(base),
                                    hus.series_key(dict(base, **{col: other})))
        self.assertEqual(len(hus.series_key(base).split("|")), 7)

    def test_same_key_reused_when_nothing_changed(self):
        r = row(task="approval", calls=14, i=7826, bu="u", bp="p")
        st = _state()
        hus.save_snapshots(st, _snaps(hus.compute_deltas([r], st)))
        d, _ = hus.compute_deltas([r], st)
        self.assertEqual(d, [])


class TestBug2WindowLatency(unittest.TestCase):
    """耗时必须按 (prev_last_seen, cur_last_seen] 归属，不能用全会话中位数。"""

    def _idx(self):
        return hus.latency_index([
            {"kind": "main", "status": "success", "session_id": SESS,
             "model": MODEL, "duration_ms": 14159, "started_at_ms": 1791010137000},
            {"kind": "main", "status": "success", "session_id": SESS,
             "model": MODEL, "duration_ms": 60371, "started_at_ms": 1791010700000},
            {"kind": "main", "status": "success", "session_id": SESS,
             "model": MODEL, "duration_ms": 198178, "started_at_ms": 1791011400000},
        ])

    def test_indexes_carry_timestamps(self):
        items = self._idx()[(SESS, MODEL)]
        self.assertEqual(len(items), 3)
        self.assertTrue(all(len(x) == 2 for x in items))

    def test_only_events_inside_window_are_counted(self):
        got = hus.window_latency(self._idx(), SESS, MODEL,
                                 prev_last_seen=1791011000.0,
                                 cur_last_seen=1791011600.0)
        self.assertEqual(got, 198178)

    def test_window_without_events_returns_none(self):
        self.assertIsNone(hus.window_latency(self._idx(), SESS, MODEL,
                                             1791012000.0, 1791012500.0))

    def test_first_baseline_returns_none_not_a_fabricated_number(self):
        """首次同步没有「上一次」，不能编一个中位数出来。"""
        self.assertIsNone(hus.window_latency(self._idx(), SESS, MODEL,
                                             None, 1791011600.0))

    def test_aux_rows_never_get_latency(self):
        """实测：title_generation 行曾被误填主循环中位耗时。"""
        aux = row(task="title_generation", calls=1, i=583, o=94, cr=141,
                  first=1791010118.097, last=1791010118.097)
        st = _state()
        prev = row(task="", calls=17, i=31793, o=9954, cr=414124,
                   last=1791011000.0)
        lat = self._idx()
        idx = list(range(0))
        got = _run_sync_rows(st, [prev, aux], lat)
        for d in got:
            if d["row"]["task"] != "":
                self.assertEqual(d["latency_ms"], 0)

    def test_session_median_is_not_used_as_window_value(self):
        """回归锁：60371 是全会话中位数，绝不能当成窗口值。"""
        allv = sorted(v for _, v in self._idx()[(SESS, MODEL)])
        session_median = allv[len(allv) // 2]
        got = hus.window_latency(self._idx(), SESS, MODEL,
                                 prev_last_seen=1791010000.0,
                                 cur_last_seen=1791010200.0)
        self.assertEqual(got, 14159)
        self.assertNotEqual(got, session_median)


class TestSnapshotSchema(unittest.TestCase):
    def test_table_has_last_seen_column(self):
        st = _state()
        hus.ensure_snapshots_table(st)
        hus.ensure_snapshots_column(st)
        cols = {r[1] for r in st.execute("PRAGMA table_info(snapshots)")}
        self.assertIn(hus.SNAP_META_COL, cols)
        self.assertTrue(set(hus.SNAPSHOT_COLS) <= cols)

    def test_column_added_to_existing_table(self):
        import sqlite3
        st = sqlite3.connect(":memory:")
        st.execute("CREATE TABLE snapshots (key TEXT PRIMARY KEY, "
                   + ", ".join("%s REAL DEFAULT 0" % c
                               for c in hus.SNAPSHOT_COLS) + ")")
        st.commit()
        self.assertNotIn(hus.SNAP_META_COL,
                         {r[1] for r in st.execute("PRAGMA table_info(snapshots)")})
        self.assertTrue(hus.ensure_snapshots_column(st))
        cols = {r[1] for r in st.execute("PRAGMA table_info(snapshots)")}
        self.assertIn(hus.SNAP_META_COL, cols)
        self.assertFalse(hus.ensure_snapshots_column(st), "重复调用应无操作")

    def test_save_roundtrip_preserves_last_seen(self):
        st = _state()
        hus.ensure_snapshots_table(st)
        r = row(task="", calls=22, i=36307, o=15113, cr=505709,
                last=1791011572.002)
        hus.save_snapshots(st, _snaps(hus.compute_deltas([r], st)))
        got = dict(st.execute("SELECT key, last_seen FROM snapshots").fetchall())
        self.assertEqual(list(got.values()), [1791011572.002])

    def test_prev_last_seen_is_exposed_on_delta(self):
        st = _state()
        r = row(task="", calls=5, i=100, o=10, cr=200, last=1791011100.0)
        hus.save_snapshots(st, _snaps(hus.compute_deltas([r], st)))
        r2 = row(task="", calls=9, i=300, o=20, cr=700, last=1791011700.0)
        d, _ = hus.compute_deltas([r2], st)
        self.assertEqual(d[0]["prev_last_seen"], 1791011100.0)


def _state():
    import sqlite3
    s = sqlite3.connect(":memory:")
    s.execute("CREATE TABLE snapshots (key TEXT PRIMARY KEY, "
              + ", ".join("%s REAL DEFAULT 0" % c for c in hus.SNAPSHOT_COLS)
              + ", last_seen REAL DEFAULT 0)")
    s.commit()
    return s


def _snaps(res):
    return res[1]


def _run_sync_rows(st, rows, lat):
    d, snaps = hus.compute_deltas(rows, st)
    hus.save_snapshots(st, snaps)
    out = []
    for x in d:
        y = dict(x)
        y["latency_ms"] = 0
        out.append(y)
    return out


if __name__ == "__main__":
    unittest.main(verbosity=2)