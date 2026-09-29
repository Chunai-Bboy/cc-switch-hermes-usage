#!/usr/bin/env python3
"""
验证 Hermes 用量是否已在 CC Switch 使用统计中可见。
检查点：
  1. _hermes_session provider 已注册
  2. proxy_request_logs 中存在 hermes_session 记录（及最新时间）
  3. 汇总口径（模拟 usage 页的 provider 统计 / 模型统计）
  4. state.db 快照与 CC Switch 记录数一致性（delta 健康度）
退出码：0 全部通过；1 有未通过项
"""

import argparse
import os
import sqlite3
import sys
from datetime import datetime

PROVIDER_ID = "_hermes_session"
APP_TYPE = "hermes"
DATA_SOURCE = "hermes_session"
STATE_DB = os.path.expandvars(r"%LOCALAPPDATA%\cc-switch-hermes-usage\sync-state.db")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--cc-db", default=os.path.expanduser(r"~\.cc-switch\cc-switch.db"))
    ap.add_argument("--state-db", default=STATE_DB)
    args = ap.parse_args()
    ok = True

    cc = sqlite3.connect(f"file:{args.cc_db}?mode=ro", uri=True)

    # 1. provider 注册
    row = cc.execute(
        "SELECT name FROM providers WHERE id=? AND app_type=?",
        (PROVIDER_ID, APP_TYPE)).fetchone()
    if row:
        print(f"OK  provider 已注册: {row[0]}")
    else:
        print("FAIL provider 未注册（先运行 hermes_usage_sync.py）")
        ok = False

    # 2. 记录存在性
    row = cc.execute(
        "SELECT COUNT(*), MIN(created_at), MAX(created_at),"
        " SUM(input_tokens), SUM(output_tokens), SUM(cache_read_tokens),"
        " SUM(total_cost_usd) FROM proxy_request_logs WHERE data_source=?",
        (DATA_SOURCE,)).fetchone()
    cnt, tmin, tmax = row[0], row[1], row[2]
    if cnt > 0:
        print(f"OK  hermes 记录 {cnt} 条")
        print(f"    时间范围: {datetime.fromtimestamp(tmin)} ~ {datetime.fromtimestamp(tmax)}")
        print(f"    合计: in={row[3]} out={row[4]} cache_read={row[5]} cost=${row[6]:.6f}")
    else:
        print("FAIL 无 hermes 记录")
        ok = False

    # 3. usage 页模型统计口径
    print("\n-- 模型统计（usage 页'模型统计' Tab 口径）--")
    for r in cc.execute(
            "SELECT model, COUNT(*) reqs, SUM(input_tokens), SUM(output_tokens),"
            " SUM(cache_read_tokens), SUM(total_cost_usd)"
            " FROM proxy_request_logs WHERE data_source=?"
            " GROUP BY model ORDER BY reqs DESC", (DATA_SOURCE,)):
        print(f"    {r[0]:35s} x{r[1]:<4d} in={r[2]:<10d} out={r[3]:<8d} "
              f"cr={r[4]:<10d} ${r[5]:.6f}")

    # 4. delta 健康度：state 快照 vs 写入记录
    if os.path.exists(args.state_db):
        st = sqlite3.connect(f"file:{args.state_db}?mode=ro", uri=True)
        snaps = st.execute("SELECT COUNT(*) FROM snapshots").fetchone()[0]
        st.close()
        print(f"\n-- delta 健康度 --")
        print(f"    state 快照 {snaps} 个 key / CC Switch 记录 {cnt} 条")
        if snaps == 0 and cnt > 0:
            print("    WARN state 为空但有记录（旧版脚本产物），下次同步后将建立基线")

    cc.close()
    print("\nUI: CC Switch -> 设置 -> 使用统计 -> 点击 App 筛选行的 'Hermes' 按钮"
          "\n    (或 全部来源 下拉选 'Hermes Agent'; 日期范围需覆盖数据日期，默认'当天'看不到历史数据)")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
