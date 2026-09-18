# -*- coding: utf-8 -*-
"""每日市值/估值快照落库（2026-09-18 P5 数据线）

目的：为 size（精确市值）与估值（PE/PB）因子积累 **point-in-time 日频历史**，
使它们可在 ≥60 个交易日后进入 OOS 验证 → 权重审批流程。

数据源：腾讯行情（DataEngine.get_all_quotes，免费、一次全市场、含 PE/PB/市值），
**不消耗 ASHareHub 配额**。

落库：`data/cache/factor_daily.db` 表 `valuation_snapshot`
  (date, code, pe, pb, total_mv, circ_mv, fetched_at) PRIMARY KEY(date, code)
  INSERT OR REPLACE → 幂等、可重复执行（同日重跑只覆盖）。

契约（沿用项目约定）：
  - 缺失字段一律写 None，**不回落 0**（避免把"无数据"伪装成"零值"）；
  - 字段缺失率 > 20% 时 warning（上游字段漂移要"响一声"）。

用法：
  python scripts/snapshot_valuation_daily.py            # 今日快照
  python scripts/snapshot_valuation_daily.py --date 2026-09-17   # 指定日期（补录）
"""
import argparse
import os
import sqlite3
import sys
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FACTOR_DB = os.path.join(PROJECT_ROOT, 'data', 'cache', 'factor_daily.db')

DDL = """
CREATE TABLE IF NOT EXISTS valuation_snapshot (
    date TEXT, code TEXT,
    pe REAL, pb REAL, total_mv REAL, circ_mv REAL,
    fetched_at TEXT,
    PRIMARY KEY (date, code)
)
"""


def _init(conn):
    conn.execute(DDL)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_val_snap_date ON valuation_snapshot(date)")
    conn.commit()


def write_snapshot(conn, date: str, rows: list, fetched_at: str = None) -> int:
    """写快照（幂等）。rows: [(code, pe, pb, total_mv, circ_mv), ...] → 返回写入行数。"""
    fetched_at = fetched_at or datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    conn.executemany(
        "INSERT OR REPLACE INTO valuation_snapshot "
        "(date, code, pe, pb, total_mv, circ_mv, fetched_at) VALUES (?,?,?,?,?,?,?)",
        [(date, c, pe, pb, tmv, cmv, fetched_at) for c, pe, pb, tmv, cmv in rows])
    conn.commit()
    return len(rows)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--date', default=None, help='快照日期（默认今天）')
    args = ap.parse_args()
    date = args.date or datetime.now().strftime('%Y-%m-%d')

    from core.data_engine import DataEngine
    de = DataEngine({})
    quotes = de.get_all_quotes()
    if quotes is None or quotes.empty:
        print("行情为空，未写入（保留既有快照）")
        return 1

    def _f(v):
        """非有限值/缺失 → None（不回落 0）"""
        import math
        try:
            f = float(v)
        except (TypeError, ValueError):
            return None
        return f if math.isfinite(f) and f > 0 else None

    def _f_raw(v):
        """原值转 float；缺失/非数值 → None（用于区分'字段缺失'与'值非正'）"""
        import math
        try:
            f = float(v)
        except (TypeError, ValueError):
            return None
        return f if math.isfinite(f) else None

    rows = []
    # 区分统计：*_lost = 字段缺失（上游漂移，需告警）；*_nonpos = 值为非正（业务语义，正常）
    miss = {'pe_lost': 0, 'pb_lost': 0, 'mv_lost': 0}
    nonpos = {'pe': 0, 'pb': 0}
    for _, r in quotes.iterrows():
        code = str(r.get('code', '')).zfill(6)
        if not code or code.startswith('39'):
            continue
        pe_raw, pb_raw = _f_raw(r.get('pe')), _f_raw(r.get('pb'))
        tmv, cmv = _f(r.get('total_market_cap')), _f(r.get('circulating_market_cap'))
        pe, pb = _f(r.get('pe')), _f(r.get('pb'))
        # 字段缺失（None/NaN）≠ 值非正（亏损公司 PE<0 / 净资产为负 PB<0）
        if pe_raw is None:
            miss['pe_lost'] += 1
        elif pe is None:
            nonpos['pe'] += 1
        if pb_raw is None:
            miss['pb_lost'] += 1
        elif pb is None:
            nonpos['pb'] += 1
        if tmv is None:
            miss['mv_lost'] += 1
        rows.append((code, pe, pb, tmv, cmv))

    n = len(rows) or 1
    for k, v in miss.items():
        rate = v / n * 100
        if rate > 20:
            print(f"⚠ 字段缺失率偏高 {k}: {rate:.1f}%（{v}/{n}）——检查上游字段漂移")
    print(f"非正值（业务语义，正常）: PE≤0 {nonpos['pe']} 只（{nonpos['pe']/n*100:.1f}%）、"
          f"PB≤0 {nonpos['pb']} 只")

    conn = sqlite3.connect(FACTOR_DB)
    try:
        _init(conn)
        written = write_snapshot(conn, date, rows)
        total = conn.execute("SELECT COUNT(*) FROM valuation_snapshot").fetchone()[0]
        days = conn.execute("SELECT COUNT(DISTINCT date) FROM valuation_snapshot").fetchone()[0]
        dr = conn.execute("SELECT MIN(date), MAX(date) FROM valuation_snapshot").fetchone()
    finally:
        conn.close()

    print(f"估值快照写入完成: {date} | 本次 {written} 只 | 库内累计 {total:,} 行 / {days} 个交易日 "
          f"({dr[0]} ~ {dr[1]})")
    print(f"启用条件：累计 ≥ 60 个交易日后跑 OOS（run_backtest --mode oos）验证 size/PE/PB，"
          f"再走 calibrate_weights 审批")
    return 0


if __name__ == '__main__':
    sys.exit(main())
