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

# 2026-09-23（审查报告 R2）：本脚本开始 import core.factor_registry（因子单一来源），
# 需要把项目根加入 sys.path —— 此前只导 stdlib，从未需要。
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

DB = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                  'data', 'db', 'predictions.db')
# 2026-09-23（审查报告 R2）：清单改由 core/factor_registry **单一来源**派生
# （本文件此前漏掉 4 个启用因子 → 恒值检测"假绿灯"，见 2026-09-22 事故记录）。
from core.factor_registry import DIAG_FACTORS, SPARSE_FACTORS

FACTORS = list(DIAG_FACTORS)
# 天生稀疏因子豁免恒值判定：多数样本恒 50（中性值）是设计使然，非数据异常
SPARSE_EXEMPT = set(SPARSE_FACTORS)
# 恒值判定分析窗口（交易日数）：排除旧 bug 期数据（如 hot_theme 修复前恒 65）
WINDOW_DAYS = 30
# 恒值判定的最小样本量（2026-09-22 修复）：新启用因子（如 2026-09-19 方案 G 的
# liq_dev/vol_dev/volatility/reversal_20d）落库天数极少时 nunique 必然 ==1，
# 按"恒值"报警是**误报**（会 rc=2 让自动化误判数据源断了）。样本不足时改报
# "样本不足，无法判定"，不计入 has_const。
MIN_SAMPLES_FOR_CONST = 10


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


def diag_distinction(wide, recent_days: int = 5):
    """1. 因子区分度。

    返回 (has_const_window, has_const_recent)：
      has_const_window  —— WINDOW_DAYS 窗口内检出恒值（可能只是历史坏数据期）；
      has_const_recent  —— 最近 recent_days 个交易日检出恒值（**当前**数据源故障）。
    2026-09-22 修复：原实现只看 30 日窗口，而该窗口必然覆盖到历史 bug 期
    （如 volume_ratio 修复前的 volume_price 恒 52.5），即使当前完全正常也会
    rc=2 → 自动化把"历史问题已修复"误判为"现在数据源断了"。
    现以"最近 5 个交易日"作为**退出码**依据，长窗口仅作参考披露。
    """
    def _scan(w, label):
        has_const = False
        detail = []
        for f in FACTORS:
            col = f + '_raw'
            s = w[col].dropna()
            if s.empty:
                continue
            nunique = s.nunique()
            mode_val = s.mode().iloc[0] if not s.mode().empty else np.nan
            mode_ratio = (s == mode_val).mean()
            flag = ''
            if f in SPARSE_EXEMPT:
                flag = '（稀疏因子豁免）'
            elif len(s) < MIN_SAMPLES_FOR_CONST:
                flag = f'（样本不足 <{MIN_SAMPLES_FOR_CONST}，无法判定恒值）'
            elif mode_ratio > 0.40:
                flag = ' ⚠️ 恒值!'
                has_const = True
                detail.append(f)
            _std = s.std()
            _std_str = f'{_std:6.2f}' if (_std == _std) else '   —  '
            print(f"  {f:16s} n={len(s):3d}  nunique={nunique:3d}  std={_std_str}  "
                  f"min={s.min():6.1f}  max={s.max():6.1f}  "
                  f"众数={mode_val:.1f}(占{mode_ratio*100:.0f}%){flag}")
        return has_const, detail

    print("=" * 70)
    print("【1】因子区分度诊断（raw_score 全样本）")
    print(f"    恒值判定窗口: 最近 {WINDOW_DAYS} 个交易日（豁免: {', '.join(sorted(SPARSE_EXEMPT))}）"
          f"；另单独检查最近 {recent_days} 个交易日（决定退出码）")
    print("=" * 70)
    dates = sorted(wide['date'].unique())
    if len(dates) > WINDOW_DAYS:
        window_dates = set(dates[-WINDOW_DAYS:])
        window_wide = wide[wide['date'].isin(window_dates)]
        print(f"    窗口 {len(dates)}→{len(window_dates)} 天，样本 {len(wide)}→{len(window_wide)}")
    else:
        window_wide = wide
    has_const_window, det_window = _scan(window_wide, 'window')

    # 最近 recent_days 个交易日的"当前健康度"
    recent_dates = set(dates[-recent_days:])
    recent_wide = wide[wide['date'].isin(recent_dates)]
    print(f"    --- 最近 {len(recent_dates)} 个交易日（样本 {len(recent_wide)}）当前健康度 ---")
    has_const_recent, det_recent = _scan(recent_wide, 'recent')
    if det_window and not has_const_recent:
        print(f"    ℹ️ 长窗口检出的恒值因子 {det_window} 未出现在最近 {recent_days} 个交易日"
              f" → 判定为历史坏数据期残留，不代表当前数据源状态")
    return has_const_window, has_const_recent


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
            # 2026-09-22 修复：零方差序列会让 numpy corrcoef 抛
            # "invalid value encountered in divide"（std=0 → NaN），且相关系数无意义。
            if s1.nunique() <= 1 or s2.nunique() <= 1:
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
                _sa = day.loc[mask, a + '_raw']
                _sb = day.loc[mask, b + '_raw']
                # 2026-09-22 修复：零方差序列 corr 产生 NaN + numpy RuntimeWarning
                if _sa.nunique() <= 1 or _sb.nunique() <= 1:
                    if verbose:
                        print(f"    nvar0", end='')
                    continue
                c = _sa.corr(_sb)
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

    has_const_window, has_const_recent = diag_distinction(wide)
    diag_cross_period_corr(wide)
    diag_cross_section_corr(wide, verbose=args.verbose)
    diag_factor_vs_final(wide)

    print()
    # 2026-09-22：退出码以**最近 5 个交易日**为准（长窗口会覆盖历史 bug 期，
    # 用它决定退出码会让自动化把"已修复的历史问题"误判为当前故障）。
    if has_const_recent:
        print("⚠️ 最近 5 个交易日检出恒值因子（>40% 样本为同一分值）"
              "— 当前数据源疑似断供或字段漏解析")
        sys.exit(2)
    if has_const_window:
        print("ℹ️ 30 日窗口检出恒值因子，但最近 5 个交易日正常"
              "→ 属历史坏数据期残留，不判为当前故障（rc=0）")
        sys.exit(0)
    print("✅ 未检测到恒值因子")
    sys.exit(0)


if __name__ == '__main__':
    main()
