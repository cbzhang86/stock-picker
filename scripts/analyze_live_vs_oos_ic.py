# -*- coding: utf-8 -*-
"""P6：实盘 IC 与 OOS 全市场 IC 的矛盾归因（2026-09-18）

## 矛盾
- 实盘（predictions.db，过筛后的每日 top-N 推荐）score→T+1 收益 IC ≈ +0.17（94 条）
- OOS 全市场横截面（2.2 年面板）合成因子 IC ≈ −0.004 ~ +0.017

## 假设
两者**测的不是同一个统计量**：
  全市场 IC = 全体股票的排序能力；
  实盘 IC   = **仅在头部样本内**的排序能力（选择效应 / range restriction）。
若头部区域 IC 显著高于全市场，则矛盾消解——模型的价值在头部，而非全市场排序。

## 方法
1. 用当前生效权重（v1.json）在 OOS 面板上合成综合分（覆盖到的因子按权重归一化）；
2. 计算三种 IC（均为逐日 Spearman，hold1d）：
   a. **全市场 IC**：当日所有股票
   b. **头部 IC**：每日按合成分取 top-3（与实盘推荐数一致）后，仅在这些样本内计算
   c. **分位 IC**：每日 top-10% / top-20% 内计算（看单调性）
3. 对比实盘 IC（读 predictions.db + outcomes）。

## 用法
  python scripts/analyze_live_vs_oos_ic.py [--start 2024-05-01] [--end 2026-09-03]
"""
import argparse
import io
import json
import os
import sqlite3
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PRED_DB = os.path.join(PROJECT_ROOT, 'data', 'db', 'predictions.db')


def _spearman_daily(df, score_col, ret_col, date_col='date', min_n=5, top_frac=None,
                    top_n=None):
    """逐日 Spearman IC；top_frac/top_n 给定时先按 score 取头部再算 IC。

    注意：top_n 模式下头部样本天然只有 n 个，min_n 需相应放宽（否则恒为 0 天，
    2026-09-18 修复：top_n=3 时用 min_n=2，top_frac 时沿用调用方 min_n）。
    """
    sub = df[[date_col, score_col, ret_col]].dropna()
    if sub.empty:
        return pd.Series(dtype=float)
    eff_min_n = 2 if top_n else min_n
    out = {}
    for d, g in sub.groupby(date_col):
        if top_n:
            g = g.nlargest(top_n, score_col)
        elif top_frac:
            k = max(int(len(g) * top_frac), 2)
            g = g.nlargest(k, score_col)
        if len(g) < eff_min_n:
            continue
        ic = g[score_col].rank().corr(g[ret_col].rank())
        if pd.notna(ic):
            out[d] = ic
    return pd.Series(out)


def _stats(ic: pd.Series) -> str:
    if ic is None or ic.empty:
        return "n=0"
    n = len(ic)
    m, s = ic.mean(), ic.std(ddof=1) if n > 1 else 0.0
    t = m / (s / np.sqrt(n)) if s > 0 else float('nan')
    return f"IC {m:+.4f} | t {t:+.2f} | n_days {n}"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--start', default='2024-05-01')
    ap.add_argument('--end', default='2026-09-03')
    args = ap.parse_args()

    # ── 1. 实盘侧 IC ──
    live_ic = None
    if os.path.exists(PRED_DB):
        conn = sqlite3.connect(PRED_DB)
        try:
            q = """SELECT p.date, p.code, p.score, o.t1_return
                   FROM predictions p JOIN outcomes o ON o.prediction_id = p.id
                   WHERE p.mode='short' AND o.t1_return IS NOT NULL"""
            lv = pd.read_sql_query(q, conn)
        finally:
            conn.close()
        if not lv.empty:
            lv['date'] = lv['date'].astype(str)
            ics = []
            for d, g in lv.groupby('date'):
                if len(g) >= 3:
                    ics.append(g['score'].rank().corr(g['t1_return'].rank()))
            live_ic = pd.Series([x for x in ics if pd.notna(x)])
            print(f"实盘样本 {len(lv)} 条 / {lv['date'].nunique()} 天 | "
                  f"{_stats(live_ic)}  ← 与 OOS 全市场 IC 对比")
        else:
            print("实盘样本为空（predictions/outcomes 无数据）")
    else:
        print(f"未找到 {PRED_DB}")

    # ── 2. OOS 面板 → 合成因子 ──
    from core.oos_validator import OOSValidator, RET_HOLD1D
    v = OOSValidator()
    panel = v.build_panel(args.start, args.end)
    if panel.empty:
        print("面板为空")
        return
    print(f"\nOOS 面板: {len(panel):,} 行 | {panel['date'].nunique()} 天 | "
          f"{panel['code'].nunique()} 只")

    wpath = os.path.join(PROJECT_ROOT, 'data', 'weights', 'v1.json')
    weights = json.load(io.open(wpath, encoding='utf-8'))['short']
    used, wsum = [], 0.0
    for f, w in weights.items():
        if w and w > 0 and f in panel.columns:
            used.append((f, w))
            wsum += w
    if not used:
        print("权重覆盖的因子均不在面板中")
        return
    print("合成分因子: " + ', '.join(f"{f}×{w/wsum:.3f}" for f, w in used))
    panel['composite'] = sum(panel[f].fillna(50.0) * (w / wsum) for f, w in used)

    # ── 3. 三种 IC 对比 ──
    print("\n=== 合成分的 IC（hold1d，逐日 Spearman）===")
    full_ic = _spearman_daily(panel, 'composite', RET_HOLD1D)
    print(f"a. 全市场          : {_stats(full_ic)}")
    top3_ic = _spearman_daily(panel, 'composite', RET_HOLD1D, top_n=3)
    print(f"b. 头部 top-3      : {_stats(top3_ic)}  ← 与实盘样本同构")
    for frac in (0.10, 0.20):
        ic = _spearman_daily(panel, 'composite', RET_HOLD1D, top_frac=frac)
        print(f"c. 头部 top-{int(frac*100)}%   : {_stats(ic)}")

    # ── 4. 头部 vs 全市场收益差 ──
    print("\n=== 收益水平（hold1d，%）===")
    sub = panel[['date', 'composite', RET_HOLD1D]].dropna()
    head = sub.groupby('date', group_keys=False).apply(
        lambda g: g.nlargest(min(3, len(g)), 'composite'))
    print(f"全市场日均收益 {sub[RET_HOLD1D].mean():+.4f}% | "
          f"头部 top-3 日均收益 {head[RET_HOLD1D].mean():+.4f}% | "
          f"差 {head[RET_HOLD1D].mean() - sub[RET_HOLD1D].mean():+.4f}pp")

    # ── 5. 结论 ──
    print("\n=== 结论 ===")
    if live_ic is not None and not live_ic.empty and not top3_ic.empty:
        gap_full = abs(live_ic.mean() - full_ic.mean())
        gap_head = abs(live_ic.mean() - top3_ic.mean())
        print(f"实盘 IC {live_ic.mean():+.4f} | 全市场 IC {full_ic.mean():+.4f} "
              f"(差 {gap_full:.4f}) | 头部 top-3 IC {top3_ic.mean():+.4f} (差 {gap_head:.4f})")
        if gap_head < gap_full:
            print("→ 实盘 IC 更接近**头部区域 IC**：矛盾源于样本选择效应"
                  "（实盘只测头部，全市场 IC 被中后段稀释），两者不矛盾。")
        else:
            print("→ 实盘 IC 与头部/全市场均不吻合：可能存在其他差异"
                  "（时间窗口、因子覆盖、人工执行偏差），需进一步核查。")

    out = os.path.join(PROJECT_ROOT, 'data', 'reports',
                       f'live_vs_oos_ic_{args.start}_{args.end}.json')
    with io.open(out, 'w', encoding='utf-8') as f:
        json.dump({
            'live_ic': round(float(live_ic.mean()), 4) if live_ic is not None and not live_ic.empty else None,
            'full_ic': round(float(full_ic.mean()), 4) if not full_ic.empty else None,
            'top3_ic': round(float(top3_ic.mean()), 4) if not top3_ic.empty else None,
            'factors_used': [f for f, _ in used],
            'window': [args.start, args.end],
        }, f, ensure_ascii=False, indent=2)
    print(f"\n结果已落盘: {out}")


if __name__ == '__main__':
    main()
