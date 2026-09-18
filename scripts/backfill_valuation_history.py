# -*- coding: utf-8 -*-
"""估值/市值历史回填（2026-09-18）——为未启用因子提供可回测的历史序列。

数据源：akshare `stock_zh_valuation_baidu`（百度股市通，免费）
  指标：总市值（亿元）/ 市盈率(TTM) / 市净率；period='近三年'（≈1096 个交易日）

落库：`data/cache/valuation_history.db`
  valuation_history(code, date, total_mv, pe_ttm, pb, source, fetched_at)
  PRIMARY KEY (code, date)；INSERT OR REPLACE（幂等、可断点续跑）

股票池：默认按 kline_cache 近 60 日均成交额取前 N（默认 300）只（排除 39 开头指数）。
  理由：全市场 5200 只 × 3 指标 ≈ 4 小时且易触发限流；IC 方向验证只需代表性样本，
  池子验完再决定是否全市场铺开（见 docs/下一步工作方案_2026-09-18.md 修订）。

用法：
  python scripts/backfill_valuation_history.py                 # 默认 300 只
  python scripts/backfill_valuation_history.py --limit 50      # 小样本试跑
  python scripts/backfill_valuation_history.py --force         # 忽略断点重抓
"""
import argparse
import os
import sqlite3
import sys
import time
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
KLINE_DB = os.path.join(PROJECT_ROOT, 'data', 'cache', 'kline_cache.db')
VAL_DB = os.path.join(PROJECT_ROOT, 'data', 'cache', 'valuation_history.db')
INDICATORS = [('总市值', 'total_mv'), ('市盈率(TTM)', 'pe_ttm'), ('市净率', 'pb')]


def _init_db():
    conn = sqlite3.connect(VAL_DB)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS valuation_history (
            code TEXT, date TEXT,
            total_mv REAL, pe_ttm REAL, pb REAL,
            source TEXT, fetched_at TEXT,
            PRIMARY KEY (code, date)
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_val_code ON valuation_history(code)")
    conn.commit()
    return conn


def _universe(limit: int) -> list:
    """按近 90 日均成交额取前 N 只（排除指数 39 前缀）"""
    conn = sqlite3.connect(KLINE_DB)
    try:
        rows = conn.execute("""
            SELECT code, AVG(amount) AS avg_amt FROM kline_cache
            WHERE date >= date((SELECT MAX(date) FROM kline_cache), '-90 day')
              AND amount > 0
            GROUP BY code ORDER BY avg_amt DESC
        """).fetchall()
    finally:
        conn.close()
    out = []
    for code, _ in rows:
        c = str(code).zfill(6)
        if c.startswith('39'):      # 指数排除
            continue
        out.append(c)
        if len(out) >= limit:
            break
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--limit', type=int, default=300, help='股票池大小（按流动性前 N）')
    ap.add_argument('--force', action='store_true', help='忽略断点，重抓全部')
    ap.add_argument('--sleep', type=float, default=0.6, help='每次调用间隔秒数（限流保护）')
    args = ap.parse_args()

    import akshare as ak

    conn = _init_db()
    done = set()
    if not args.force:
        # 断点：已有 >=600 行（≈近三年大部分交易日）的股票视为完成
        rows = conn.execute(
            "SELECT code, COUNT(*) FROM valuation_history GROUP BY code HAVING COUNT(*) >= 600"
        ).fetchall()
        done = {r[0] for r in rows}

    codes = _universe(args.limit)
    todo = [c for c in codes if c not in done]
    print(f"股票池 {len(codes)} 只 | 已完成 {len(done)} | 待抓取 {len(todo)}")
    print(f"预计耗时 ~{len(todo) * len(INDICATORS) * (args.sleep + 3) / 60:.0f} 分钟\n")

    stats = {'code_ok': 0, 'code_fail': 0, 'rows': 0, 'ind_fail': 0}
    t0 = time.time()
    for i, code in enumerate(todo, 1):
        recs = {}
        for ind_name, col in INDICATORS:
            for attempt in range(2):
                try:
                    df = ak.stock_zh_valuation_baidu(symbol=code, indicator=ind_name,
                                                     period='近三年')
                    if df is not None and not df.empty:
                        for _, r in df.iterrows():
                            d = str(r.iloc[0])
                            v = r.iloc[1]
                            try:
                                v = float(v)
                            except (TypeError, ValueError):
                                continue
                            recs.setdefault(d, {})[col] = v
                    break
                except Exception as e:
                    if attempt == 1:
                        stats['ind_fail'] += 1
                        print(f"  [{code}] {ind_name} 失败: {str(e)[:60]}")
                    else:
                        time.sleep(1.5)
            time.sleep(args.sleep)
        if recs:
            now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            conn.executemany(
                "INSERT OR REPLACE INTO valuation_history VALUES (?,?,?,?,?,?,?)",
                [(code, d, v.get('total_mv'), v.get('pe_ttm'), v.get('pb'),
                  'baidu_lg', now) for d, v in recs.items()])
            conn.commit()
            stats['code_ok'] += 1
            stats['rows'] += len(recs)
        else:
            stats['code_fail'] += 1
        if i % 20 == 0 or i == len(todo):
            el = time.time() - t0
            print(f"  进度 {i}/{len(todo)} | 成功 {stats['code_ok']} 失败 {stats['code_fail']} "
                  f"| 入库 {stats['rows']:,} 行 | 已用 {el/60:.1f} 分钟")

    total = conn.execute("SELECT COUNT(*) FROM valuation_history").fetchone()[0]
    cov = conn.execute("SELECT COUNT(DISTINCT code) FROM valuation_history").fetchone()[0]
    conn.close()
    print(f"\n完成：本次成功 {stats['code_ok']} 只 / 失败 {stats['code_fail']} 只；"
          f"库内共 {total:,} 行 / {cov} 只")


if __name__ == '__main__':
    main()
