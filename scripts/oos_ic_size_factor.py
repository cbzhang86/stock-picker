# -*- coding: utf-8 -*-
"""size（小市值）因子 OOS IC 验证 —— 近似口径 A（2026-09-18）

## 口径与已知偏差

历史市值 ≈ **当日收盘价 × 当前总股本**。
  - 股本来源：`DataEngine.get_all_quotes()` 的 `total_market_cap / price`。
  - 送转股对市值中性（价格与股本同比调整）→ 近似误差主要来自增发/回购/可转债
    转股，2.2 年窗口内通常为二阶小量。
  - **这是近似口径**：若 IC 显著，需用精确历史股本（akshare 股本变动）复核后再
    进入 calibrate_weights 审批流程。

size 因子分 = (1 − 当日横截面市值秩百分位) × 100（小市值 → 高分）。

收益口径：hold1d（尾盘买 → T+1 收盘卖）为主，intraday 并列参考，
与 `data/reports/oos_ic_*_hold1d.json` 的其他因子可比。

用法：
  python scripts/oos_ic_size_factor.py [--start 2024-05-01] [--end 2026-09-03]
"""
import argparse
import json
import os
import sys
from datetime import datetime

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--start', default='2024-05-01')
    ap.add_argument('--end', default='2026-09-03')
    args = ap.parse_args()

    from core.oos_validator import OOSValidator, RET_HOLD1D, RET_INTRADAY
    from core.data_engine import DataEngine

    print(f"=== size 因子 OOS IC 验证（近似口径 A：close × 当前总股本）===")
    print(f"窗口: {args.start} ~ {args.end}\n")

    v = OOSValidator()
    panel = v.build_panel(args.start, args.end, factors=['momentum'])
    if panel.empty:
        print("面板为空")
        return
    print(f"面板: {len(panel):,} 行 | {panel['code'].nunique()} 只 | "
          f"{panel['date'].nunique()} 天\n")

    # 当前总股本 = 总市值 / 价格（腾讯行情口径）
    de = DataEngine({})
    quotes = de.get_all_quotes()
    shares = {}
    for _, r in quotes.iterrows():
        try:
            mc = r.get('total_market_cap')
            px = r.get('price')
            if mc and px and float(px) > 0:
                shares[str(r['code']).zfill(6)] = float(mc) / float(px)
        except Exception:
            continue
    print(f"股本覆盖: {len(shares)} 只（行情源 total_market_cap/price）")
    if not shares:
        print("无股本数据，退出")
        return

    panel['_shares'] = panel['code'].astype(str).str.zfill(6).map(shares)
    panel['mcap_proxy'] = panel['close'] * panel['_shares']
    cov = panel['mcap_proxy'].notna().mean() * 100
    print(f"市值覆盖: {cov:.1f}% 面板行\n")

    # size 因子分：小市值高分（横截面秩百分位取反）
    panel = panel[panel['mcap_proxy'].notna() & (panel['mcap_proxy'] > 0)].copy()
    rank_pct = panel.groupby('date')['mcap_proxy'].rank(pct=True)
    panel['size'] = (1.0 - rank_pct) * 100.0

    for ret_col, label in ((RET_HOLD1D, 'hold1d（主口径）'),
                           (RET_INTRADAY, 'intraday（参考）')):
        ic = v.daily_ic(panel, 'size', ret_col=ret_col)
        st = v._ic_stats(ic)
        print(f"[{label}] IC {st['ic']:+.4f} | t {st['t_stat']:+.2f} | "
              f"ICIR {st['icir']} | 正比例 {st['pos_ratio']} | n_days {st['n_days']}")

        # 三折符号稳定性
        if st['n_days'] >= 6:
            vals = ic.values
            folds = np.array_split(vals, 3)
            means = [float(np.mean(f)) for f in folds if len(f)]
            print(f"  三折均值: {[round(m, 4) for m in means]} | "
                  f"符号稳定: {len(set(np.sign(means))) == 1}")
            # 逐月
            by_m = {}
            for d, x in ic.items():
                by_m.setdefault(str(d)[:7], []).append(x)
            mstr = ' '.join(f"{m}:{np.mean(vs):+.3f}" for m, vs in sorted(by_m.items()))
            print(f"  逐月 IC: {mstr}")

    # 与小型/大型分组收益（尾部分组检验）
    # 2026-09-18 修正：逐日分组 → 再对交易日取均值（混池分组会被跨日水平差异污染，
    # 与逐日 IC 口径不一致）
    print("\n=== 尾部检验（逐日 5 组 → 日均收益 %；size 高分=小市值）===")
    sub = panel[['date', 'size', RET_HOLD1D]].dropna().copy()
    sub['grp'] = sub.groupby('date')['size'].transform(
        lambda s: pd.qcut(s.rank(method='first'), 5, labels=False, duplicates='drop'))
    daily = sub.groupby(['date', 'grp'])[RET_HOLD1D].mean().reset_index()
    g = daily.groupby('grp')[RET_HOLD1D].mean().round(4)
    for i, r in g.items():
        # size 分为升序：grp 0 = size 最低 = 市值最大
        tag = ('大市值' if i == 0 else '小市值' if i == 4 else f'组{int(i)+1}')
        print(f"  {tag:<6} 日均收益 {r:+.4f}%")
    if 0 in g.index and 4 in g.index:
        print(f"  小-大 价差: {g.loc[4] - g.loc[0]:+.4f}pp/日（小市值减大市值）")

    # 落盘
    out_dir = os.path.join(PROJECT_ROOT, 'data', 'reports')
    os.makedirs(out_dir, exist_ok=True)
    out = os.path.join(out_dir, f"oos_ic_size_proxy_{args.start}_{args.end}.json")
    with open(out, 'w', encoding='utf-8') as f:
        json.dump({
            'factor': 'size',
            'proxy': 'close × current_total_shares（近似口径 A）',
            'window': [args.start, args.end],
            'created_at': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
            'coverage_pct': round(float(cov), 2),
            'ic_hold1d': v._ic_stats(v.daily_ic(panel, 'size', ret_col=RET_HOLD1D)),
            'ic_intraday': v._ic_stats(v.daily_ic(panel, 'size', ret_col=RET_INTRADAY)),
        }, f, ensure_ascii=False, indent=2)
    print(f"\n结果已落盘: {out}")


if __name__ == '__main__':
    main()
