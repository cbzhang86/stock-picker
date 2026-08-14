"""因子相关性 + 区分度诊断（纯本地读库，零网络调用）

用法:
    python scripts/factor_diag.py [--mode short|long] [--verbose]

输出三块:
  1. 因子区分度: nunique / std / 众数占比 — 恒值因子立刻暴露
  2. 跨期相关性矩阵 (Pearson + Spearman) — 仅 data_available=True 样本
  3. 同日截面相关 — 消除时间混杂，检测 Simpson 悖论

退出码: 0=正常, 1=无数据, 2=有恒值因子(>40% 众数占比)
"""
import sqlite3
import json
import sys
import os
import argparse
import pandas as pd
import numpy as np

DB = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                  'data', 'db', 'predictions.db')
FACTORS = ['capital_flow', 'north_flow', 'momentum', 'technical',
           'volume_price', 'hot_theme', 'dragon_tiger']
# 天生稀疏因子豁免恒值判定：多数样本恒 50（中性值）是设计使然，非数据异常
# dragon_tiger: 大多数票无上榜记录 → raw 恒 50（中性）
SPARSE_EXEMPT = {'dragon_tiger', 'north_flow'}
# 恒值判定分析窗口（交易日数）：排除旧 bug 期数据（如 hot_theme 修复前恒 65）
WINDOW_DAYS = 30


def load_data(mode='short'):
    conn = sqlite3.connect(DB)
    df = pd.read_sql_query(
        "SELECT date, code, mode, score, factor_scores FROM predictions "
        "WHERE mode=? AND factor_scores IS NOT NULL ORDER BY date",
        conn, params=(mode,))
    conn.close()

    rows = []
    for _, r in df.iterrows():
        try:
            fs = json.loads(r['factor_scores'])
        except Exception:
            continue
        row = {'date': r['date'], 'code': r['code'], 'final_score': r['score']}
        for f in FACTORS:
            d = fs.get(f, {})
            row[f + '_raw'] = d.get('raw_score')
            row[f + '_avail'] = d.get('data_available', False)
        rows.append(row)
    return pd.DataFrame(rows)


def diag_distinction(wide):
    """1. 因子区分度"""
    print("=" * 70)
    print("【1】因子区分度诊断（raw_score 全样本）")
    print(f"    恒值判定窗口: 最近 {WINDOW_DAYS} 个交易日（豁免: {', '.join(sorted(SPARSE_EXEMPT))}）")
    print("=" * 70)
    has_const = False
    # 窗口数据：只取最近 WINDOW_DAYS 个交易日（排除旧 bug 期恒值数据污染）
    dates = sorted(wide['date'].unique())
    if len(dates) > WINDOW_DAYS:
        window_dates = set(dates[-WINDOW_DAYS:])
        window_wide = wide[wide['date'].isin(window_dates)]
        print(f"    窗口 {len(dates)}→{len(window_dates)} 天，样本 {len(wide)}→{len(window_wide)}")
    else:
        window_wide = wide
    for f in FACTORS:
        col = f + '_raw'
        s = window_wide[col].dropna()
        if s.empty:
            print(f"  {f:16s} 窗口内无有效数据")
            continue
        nunique = s.nunique()
        mode_val = s.mode().iloc[0] if not s.mode().empty else np.nan
        mode_ratio = (s == mode_val).mean()
        flag = ''
        if f in SPARSE_EXEMPT:
            flag = '（稀疏因子豁免）'
        elif mode_ratio > 0.40:
            flag = ' ⚠️ 恒值!'
            has_const = True
        print(f"  {f:16s} n={len(s):3d}  nunique={nunique:3d}  std={s.std():6.2f}  "
              f"min={s.min():6.1f}  max={s.max():6.1f}  "
              f"众数={mode_val:.1f}(占{mode_ratio*100:.0f}%){flag}")
    return has_const


def diag_cross_period_corr(wide):
    """2. 跨期相关性（附带时间混杂警示）"""
    print()
    print("=" * 70)
    print("【2】跨期相关性矩阵（Pearson，仅 data_available=True 样本）")
    print("    ⚠️ 跨期相关可能含时间混杂，看【3】同日截面确认")
    print("=" * 70)

    print("  各因子 data_available=True 样本数：")
    for f in FACTORS:
        n = int(wide[f + '_avail'].sum())
        print(f"    {f:16s} {n:3d} / {len(wide)}")

    print()
    corr_rows = []
    for i, f1 in enumerate(FACTORS):
        for f2 in FACTORS[i + 1:]:
            mask = wide[f1 + '_avail'] & wide[f2 + '_avail']
            s1 = wide.loc[mask, f1 + '_raw']
            s2 = wide.loc[mask, f2 + '_raw']
            if len(s1) < 5:
                continue
            pear = s1.corr(s2)
            spear = s1.corr(s2, method='spearman')
            corr_rows.append((f1, f2, len(s1), pear, spear))

    corr_df = pd.DataFrame(corr_rows, columns=['A', 'B', 'n', 'Pearson', 'Spearman'])
    corr_df = corr_df.sort_values('Pearson', key=lambda x: x.abs(), ascending=False)
    print(f"  {'因子A':16s} {'因子B':16s} {'n':>4s} {'Pearson':>8s} {'Spearman':>9s}")
    for _, r in corr_df.iterrows():
        flag = ' ⚠️' if abs(r['Pearson']) > 0.6 else ''
        print(f"  {r['A']:16s} {r['B']:16s} {r['n']:4d} {r['Pearson']:8.3f} {r['Spearman']:9.3f}{flag}")


def diag_cross_section_corr(wide, verbose=False):
    """3. 同日截面相关（消除时间混杂）"""
    print()
    print("=" * 70)
    print("【3】同日截面相关（消除时间混杂，真实共线应多天稳定出现）")
    print("=" * 70)

    pairs_all = []
    for i, f1 in enumerate(FACTORS):
        for f2 in FACTORS[i + 1:]:
            pairs_all.append((f1, f2))

    if verbose:
        print(f"  {'日期':12s} {'n':>3s}", end='')
        for a, b in pairs_all:
            print(f" {a[:4]}-{b[:4]}", end='')
        print()

    hit_summary = {p: [] for p in pairs_all}
    for d in sorted(wide['date'].unique()):
        day = wide[wide['date'] == d]
        if verbose:
            print(f"  {d:12s} {len(day):3d}", end='')

        for a, b in pairs_all:
            mask = day[a + '_avail'] & day[b + '_avail']
            if mask.sum() >= 4:
                c = day.loc[mask, a + '_raw'].corr(day.loc[mask, b + '_raw'])
                if verbose:
                    print(f" {c:8.3f}", end='')
                if pd.notna(c) and abs(c) > 0.6:
                    hit_summary[(a, b)].append(f"{d}:{c:.2f}")
            else:
                if verbose:
                    print(f"    n<4", end='')
        if verbose:
            print()

    print()
    print("  同日 |corr|>0.6 汇总（真实共线需多天稳定出现）：")
    any_hit = False
    for (a, b), hits in hit_summary.items():
        if hits:
            any_hit = True
            print(f"    {a:16s} vs {b:16s}: {len(hits)} 天 |corr|>0.6  {' '.join(hits[:8])}")
    if not any_hit:
        print("    ✅ 无任何因子对在同日截面 |corr|>0.6 — 无稳定共线性")


def diag_factor_vs_final(wide):
    """4. 因子与最终分数的关系"""
    print()
    print("=" * 70)
    print("【4】因子 raw_score 与 final_score 的相关（谁在驱动推荐）")
    print("=" * 70)
    for f in FACTORS:
        mask = wide[f + '_avail']
        if mask.sum() < 5:
            continue
        s = wide.loc[mask, f + '_raw']
        fs = wide.loc[mask, 'final_score']
        pear = s.corr(fs)
        spear = s.corr(fs, method='spearman')
        print(f"  {f:16s} n={mask.sum():3d}  Pearson={pear:6.3f}  Spearman={spear:6.3f}")


def main():
    parser = argparse.ArgumentParser(description='因子相关性 + 区分度诊断')
    parser.add_argument('--mode', default='short', choices=['short', 'long'])
    parser.add_argument('--verbose', action='store_true', help='打印逐日截面相关明细')
    args = parser.parse_args()

    wide = load_data(args.mode)
    if wide.empty:
        print(f"无 {args.mode} 模式数据")
        sys.exit(1)

    print(f"\n📊 因子诊断报告（{args.mode} 模式）")
    print(f"样本: {len(wide)} 条, 交易日: {wide['date'].nunique()} 个 "
          f"({wide['date'].min()} ~ {wide['date'].max()})")
    print()

    has_const = diag_distinction(wide)
    diag_cross_period_corr(wide)
    diag_cross_section_corr(wide, verbose=args.verbose)
    diag_factor_vs_final(wide)

    print()
    if has_const:
        print("⚠️ 检测到恒值因子（>40% 样本为同一分值）— 可能数据源断或字段漏解析")
        sys.exit(2)
    else:
        print("✅ 未检测到恒值因子")
        sys.exit(0)


if __name__ == '__main__':
    main()
