# -*- coding: utf-8 -*-
"""回填同花顺强势股历史（修复 hot_stocks 日期空洞）

背景（2026-09-17）
------------------
`factor_daily.db.hot_stocks` 是 hot_theme 因子 OOS 验证的数据源。
2026-09-07 之前采集挂在策略主流程的尾部，策略跳过日（极差市/门槛清零）与
未运行日会漏采 —— 实测 6/30-9/16 的 57 个交易日里缺了 28 天，
导致 hot_theme 的 OOS IC 只有 12 天可算（其余日期无数据、无方差）。

同花顺接口 `getharden/date/{date}/` **支持按历史日期查询**（实测 2026-07/08/09
均返回完整数据），因此缺口可以精确回填。

用法：
    python scripts/backfill_hot_stocks.py                       # 回填 kline_cache 覆盖范围内的缺口
    python scripts/backfill_hot_stocks.py --start 2026-06-30 --end 2026-09-16
    python scripts/backfill_hot_stocks.py --dry-run             # 只列出缺口，不写入

幂等：已采集的日期自动跳过（不重写）。
"""
import argparse
import logging
import os
import sqlite3
import sys

import requests

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s',
                    datefmt='%H:%M:%S')
logger = logging.getLogger('backfill_hot')

KLINE_DB = os.path.join(ROOT, 'data', 'cache', 'kline_cache.db')
FACTOR_DB = os.path.join(ROOT, 'data', 'cache', 'factor_daily.db')
URL = ("http://zx.10jqka.com.cn/event/api/getharden/"
       "date/{date}/orderby/date/orderway/desc/charset/GBK/")
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/131.0.0.0 Safari/537.36",
    "Referer": "http://zx.10jqka.com.cn/",
}


def _trading_days(start: str, end: str) -> list:
    conn = sqlite3.connect(KLINE_DB)
    try:
        return [r[0] for r in conn.execute(
            "SELECT DISTINCT date FROM kline_cache WHERE date>=? AND date<=? "
            "ORDER BY date", (start, end))]
    finally:
        conn.close()


def _captured_dates() -> set:
    conn = sqlite3.connect(FACTOR_DB)
    try:
        return {r[0] for r in conn.execute("SELECT DISTINCT date FROM hot_stocks")}
    finally:
        conn.close()


def _fetch(date: str) -> list:
    r = requests.get(URL.format(date=date), headers=HEADERS, timeout=15)
    data = r.json()
    if data.get('errocode', 0) != 0:
        raise RuntimeError(data.get('errormsg', 'errocode!=0'))
    rows = []
    for d in data.get('data') or []:
        code = str(d.get('code', '')).zfill(6)
        name = str(d.get('name', ''))
        if code:
            rows.append((date, code, name))
    return rows


def _write(rows: list) -> int:
    conn = sqlite3.connect(FACTOR_DB)
    try:
        conn.executemany(
            "INSERT OR REPLACE INTO hot_stocks (date, code, name) VALUES (?, ?, ?)",
            rows)
        conn.commit()
        return len(rows)
    finally:
        conn.close()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--start', default=None, help='起始日（默认 kline 最早覆盖日）')
    ap.add_argument('--end', default=None, help='结束日（默认 kline 最晚覆盖日）')
    ap.add_argument('--dry-run', action='store_true')
    args = ap.parse_args()

    days = _trading_days(args.start or '2000-01-01', args.end or '2099-12-31')
    if not days:
        logger.error("kline_cache 无覆盖，无法确定回填范围")
        return 2
    lo, hi = (args.start or days[0]), (args.end or days[-1])
    days = [d for d in days if lo <= d <= hi]
    have = _captured_dates()
    todo = [d for d in days if d not in have]

    logger.info(f"窗口 {lo}~{hi}: 交易日 {len(days)} | 已采集 {len(have & set(days))} | 缺口 {len(todo)}")
    if args.dry_run:
        print('缺口日期:', todo)
        return 0
    if not todo:
        logger.info("无缺口，退出")
        return 0

    ok = fail = 0
    total_rows = 0
    for i, d in enumerate(todo, 1):
        try:
            rows = _fetch(d)
            if rows:
                total_rows += _write(rows)
                ok += 1
            else:
                logger.warning(f"{d}: 接口返回空（可能当日无榜单），跳过")
                fail += 1
        except Exception as e:
            logger.warning(f"{d}: 失败 {str(e)[:60]}")
            fail += 1
        if i % 10 == 0:
            logger.info(f"进度 {i}/{len(todo)} 成功 {ok} 失败 {fail} 累计 {total_rows} 行")
        time.sleep(0.3)          # 温和限速

    import time as _t
    logger.info(f"回填完成: 成功 {ok} 失败 {fail} 写入 {total_rows} 行")
    return 0 if fail == 0 else 1


if __name__ == '__main__':
    import time
    sys.exit(main())
