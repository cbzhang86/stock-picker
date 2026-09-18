# -*- coding: utf-8 -*-
"""估值/精确市值因子 OOS IC 验证（2026-09-18）

数据源：`data/cache/valuation_history.db`（baidu_lg 回填，精确历史总市值/PE-TTM/PB）
收益口径：hold1d = close_{t+1}/close_t - 1（%，与 OOS 报告其他因子可比）

因子定义（横截面百分位，当日）：
  size_exact = (1 − 总市值秩百分位) × 100     → 小市值高分（与近似口径 A 交叉验证）
  value_pe   = (1 − PE(TTM) 秩百分位) × 100    → 低估值高分（PE<=0 剔除：亏损无意义）
  value_pb   = (1 − PB 秩百分位) × 100         → 低 PB 高分（PB<=0 剔除）

注意：股票池为「按流动性前 N 只」（valuation_history 覆盖范围），
非全市场 → IC 结论适用于该子池，外推到全市场需先扩大回填范围。

用法：
  python scripts/oos_ic_valuation_factor.py [--start 2024-05-01] [--end 2026-09-03]
"""
import argparse
import json
import os
import sqlite3
import sys
from datetime import datetime

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
KLINE_DB = os.path.join(PROJECT_ROOT, 'data', 'cache', 'kline_cache.db')
VAL_DB = os.path.join(PROJECT_ROOT, 'data', 'cache', 'valuation_history.db')


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--start', default='2024-05-01')
    ap.add_argument('--end', default='2026-09-03')
    args = ap.parse_args()

    if not os.path.exists(VAL_DB):
        print(f"估值库不存在: {VAL_DB}（先跑 scripts/backfill_valuation_history.py）")
        return

    # 1. 估值数据
    vconn = sqlite3.connect(VAL_DB)
    val = pd.read_sql_query(
        "SELECT code, date, total_mv, pe_ttm, pb FROM valuation_history "
        "WHERE date >= ? AND date <= ?", vconn, params=[args.start, args.end])
    vconn.close()
    if val.empty:
        print("估值数据为空")
        return
    codes = sorted(val['code'].unique())
    print(f"=== 估值因子 OOS IC 验证 ===\n估值覆盖 {len(codes)} 只 | "
          f"{len(val):,} 行 | {val['date'].min()} ~ {val['date'].max()}")

    # 2. K 线 + 前向收益（hold1d，%)
    kconn = sqlite3.connect(KLINE_DB)
    ph = ','.join(['?'] * len(codes))
    kl = pd.read_sql_query(
        f"SELECT code, date, close FROM kline_cache WHERE code IN ({ph}) "
        f"AND date >= ? AND date <= ?", kconn,
        params=codes + [args.start, args.end])
    kconn.close()
    kl['date'] = kl['date'].astype(str)
    kl = kl.sort_values(['code', 'date'])
    kl['ret_hold1d'] = (kl.groupby('code')['close'].shift(-1) / kl['close'] - 1) * 100

    # 3. 合并 + 因子分
    df = kl.merge(val, on=['code', 'date'], how='inner')
    df = df[df['ret_hold1d'].notna()].copy()
    g = df.groupby('date')
    df['size_exact'] = (1 - g['total_mv'].rank(pct=True)) * 100
    pe = df['pe_ttm'].where(df['pe_ttm'] > 0)
    df['value_pe'] = (1 - pe.groupby(df['date']).rank(pct=True)) * 100
    pb = df['pb'].where(df['pb'] > 0)
    df['value_pb'] = (1 - pb.groupby(df['date']).rank(pct=True)) * 100
    print(f"合并后面板 {len(df):,} 行 | {df['date'].nunique()} 天 | "
          f"PE 有效 {df['value_pe'].notna().mean()*100:.1f}% | "
          f"PB 有效 {df['value_pb'].notna().mean()*100:.1f}%\n")

    # 4. IC（复用 OOSValidator 的实现，保证与其他因子口径一致）
    from core.oos_validator import OOSValidator
    v = OOSValidator()
    out = {}
    print(f"{'因子':<14}{'IC':>9}{'t':>8}{'ICIR':>8}{'正比例':>8}{'n_days':>8}  三折均值 / 符号稳定")
    print('-' * 86)
    for f in ('size_exact', 'value_pe', 'value_pb'):
        ic = v.daily_ic(df, f, ret_col='ret_hold1d')
        st = v._ic_stats(ic)
        folds = [float(np.mean(fo)) for fo in np.array_split(ic.values, 3) if len(fo)]
        stable = len(set(np.sign([x for x in folds if x != 0]))) <= 1
        out[f] = {**st, 'folds': [round(x, 4) for x in folds], 'sign_stable': bool(stable)}
        print(f"{f:<14}{st['ic']:>+9.4f}{st['t_stat']:>+8.2f}"
              f"{str(st['icir']):>8}{st['pos_ratio']:>8}{st['n_days']:>8}  "
              f"{[round(x,4) for x in folds]} / {stable}")

    # 5. 尾部检验（逐日分组 → 再对交易日取均值；混池分组会被跨日水平差异污染，
    #    与逐日 IC 口径不一致——2026-09-18 修正）
    print("\n=== 尾部检验（逐日 5 组 → 日均收益 %，与 IC 同口径） ===")
    for f in ('size_exact', 'value_pe', 'value_pb'):
        sub = df[['date', f, 'ret_hold1d']].dropna().copy()
        if sub.empty:
            continue
        sub['grp'] = sub.groupby('date')[f].transform(
            lambda s: pd.qcut(s.rank(method='first'), 5, labels=False, duplicates='drop'))
        # 每个 (日期, 组) 的均值 → 再对各组按日平均
        daily = sub.groupby(['date', 'grp'])['ret_hold1d'].mean().reset_index()
        gg = daily.groupby('grp')['ret_hold1d'].mean().round(4)
        lo_name = {'size_exact': '大市值', 'value_pe': '高PE', 'value_pb': '高PB'}[f]
        hi_name = {'size_exact': '小市值', 'value_pe': '低PE', 'value_pb': '低PB'}[f]
        seq = ' | '.join(
            f"{lo_name if i == 0 else hi_name if i == 4 else f'组{int(i)+1}'}:{v_:+.3f}"
            for i, v_ in gg.items())
        spread = gg.get(4, float('nan')) - gg.get(0, float('nan'))
        # 2026-09-18 审查补充：中位数价差交叉校验——若均值价差与秩 IC 反向，
        # 用中位数判断是否由极值拉动（中位数与秩 IC 同向 → 均值反向系极值所致）
        dmed = sub.groupby(['date', 'grp'])['ret_hold1d'].median().reset_index()
        gm = dmed.groupby('grp')['ret_hold1d'].median()
        spread_med = gm.get(4, float('nan')) - gm.get(0, float('nan'))
        print(f"  {f:<12} {seq}  → 均值价差 {spread:+.4f}pp | "
              f"中位数价差 {spread_med:+.4f}pp")

    out_dir = os.path.join(PROJECT_ROOT, 'data', 'reports')
    os.makedirs(out_dir, exist_ok=True)
    path = os.path.join(out_dir,
                        f"oos_ic_size_value_exact_{args.start}_{args.end}.json")
    with open(path, 'w', encoding='utf-8') as fo:
        json.dump({'window': [args.start, args.end],
                   'universe': f'{len(codes)} 只（流动性前 N，非全市场）',
                   'created_at': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
                   'factors': out}, fo, ensure_ascii=False, indent=2)
    print(f"\n结果已落盘: {path}")


if __name__ == '__main__':
    main()
