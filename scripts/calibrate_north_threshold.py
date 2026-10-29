# -*- coding: utf-8 -*-
"""北向净增持阈值标定（2026-09-23 新增，剩余项 #4）。

背景：expert_ensemble._capital_dim 的北向阈值（"显著加仓/减仓"档）需要用
真实分布标定。官方逐股北向披露已于 2024-08 停发（东财
RPT_MUTUAL_HOLD_DETRENDD 等报表已确认"配置不存在"），可用的唯一同口径
数据源是 AShareHub northbound_holdings —— 其样本由每日因子采集
（feedback/data_collector._save_north_flow）积累到
data/cache/factor_daily.db 的 north_flow 表。

用法：
  python scripts/calibrate_north_threshold.py            # 用全部样本
  python scripts/calibrate_north_threshold.py --days 30  # 只用最近 N 天

输出：分位数、候选阈值命中率、以及建议阈值（命中率≈30% 的最接近档）。
人工复核后更新 core/expert_ensemble.py::_capital_dim 的阈值与注释。

注意：样本为候选池股票（偏大票）且随采集积累；样本 <100 或覆盖 <3 个交易日
时仅打印提示，不建议直接改阈值。
"""
import argparse
import os
import sqlite3
import sys

import numpy as np

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB = os.path.join(ROOT, 'data', 'cache', 'factor_daily.db')

MIN_SAMPLE = 100
MIN_DAYS = 3
CANDIDATE_THRESHOLDS = [5e6, 8e6, 1e7, 1.2e7, 1.5e7, 2e7, 3e7]


def main() -> int:
    ap = argparse.ArgumentParser(description='北向净增持阈值标定')
    ap.add_argument('--days', type=int, default=None,
                    help='只用最近 N 天的样本（默认全部）')
    args = ap.parse_args()

    if not os.path.exists(DB):
        print(f'样本库不存在: {DB}')
        return 1
    conn = sqlite3.connect(DB)
    if args.days:
        rows = conn.execute(
            'SELECT DISTINCT date FROM north_flow ORDER BY date DESC LIMIT ?',
            (args.days,)).fetchall()
        dates = [r[0] for r in rows]
        if not dates:
            print('无样本')
            return 1
        qmarks = ','.join('?' * len(dates))
        rows = conn.execute(
            f'SELECT date, holding_change FROM north_flow '
            f'WHERE holding_change IS NOT NULL AND date IN ({qmarks})',
            dates).fetchall()
    else:
        rows = conn.execute(
            'SELECT date, holding_change FROM north_flow '
            'WHERE holding_change IS NOT NULL').fetchall()
    dates = sorted({r[0] for r in rows})
    conn.close()

    v = np.array([float(r[1]) for r in rows])
    n_days = len(dates)
    print(f'样本: n={len(v)} / {n_days} 个交易日'
          f'（{dates[0]} ~ {dates[-1]}，AShareHub northbound_holdings 同口径，单位=股）')
    if len(v) < MIN_SAMPLE or n_days < MIN_DAYS:
        print(f'⚠️ 样本不足（需 ≥{MIN_SAMPLE} 条且 ≥{MIN_DAYS} 个交易日）'
              f'→ 仅打印分布，不建议据此改阈值。请继续积累数据后重跑。')

    print('\n分位数（股）:')
    for q in (10, 15, 20, 50, 80, 85, 90):
        print(f'  p{q:<3d}= {np.percentile(v, q):>15,.0f}')

    print('\n候选阈值命中率（|净变更| ≥ 阈值）:')
    best = None
    for th in CANDIDATE_THRESHOLDS:
        hit = float(((v >= th) | (v <= -th)).mean())
        print(f'  ±{th/1e4:>6.0f} 万股 → {hit:5.0%}')
        # 目标：命中率最接近 30%（"显著"档的常用经验值）
        if best is None or abs(hit - 0.30) < abs(best[1] - 0.30):
            best = (th, hit)

    if len(v) >= MIN_SAMPLE and n_days >= MIN_DAYS:
        print(f'\n建议阈值: ±{best[0]/1e4:.0f} 万股（命中率 {best[1]:.0%}，最接近 30%）')
        print('→ 人工复核后更新 core/expert_ensemble.py::_capital_dim 的阈值与注释')
        return 0
    return 1


if __name__ == '__main__':
    sys.exit(main())
