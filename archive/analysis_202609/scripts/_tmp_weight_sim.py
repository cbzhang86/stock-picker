# -*- coding: utf-8 -*-
"""
临时分析脚本（非业务代码，可删）：新因子正交性 + 权重方案实证对比。

承接 _tmp_liq_decomp.py 的结论：
  liq_raw (−pct(log amount)) 与 liq_level（规模代理）相关 0.838 → 84% 是小票效应
  liq_dev（当日成交额 vs 自身 60 日常态，取反）IC .0654 / ICIR .535 / t 12.81
         与 liq_level 相关仅 −0.088 → 与规模近乎正交，是真正的独立腿

本脚本回答两件事：
  1. liq_dev / vol_dev 与 hot_theme / reversal_20d 的相关性（决定能否加权）
  2. 多套权重方案在同一面板上的实证对比（Top-N 组合收益 / IC / 回撤）

用法：python scripts/_tmp_weight_sim.py
"""
import os
import sys
import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.oos_validator import RET_HOLD1D  # noqa: E402

PANEL_CACHE = 'data/cache/_tmp_panel_2023-10_2026-09.pkl'
OUT_MD = 'data/reports/_weight_sim_20260918.md'


def board_limit(code: str) -> float:
    c = str(code)
    if c.startswith(('688', '689', '300', '301')):
        return 20.0
    if c.startswith(('43', '83', '87', '88', '92', 'bj', 'BJ')):
        return 30.0
    return 10.0


def load_and_enrich():
    df = pd.read_pickle(PANEL_CACHE).sort_values(['code', 'date']).reset_index(drop=True)

    # ⚠️ 口径修正（2026-09-18）：ret_hold1d 单位是【百分数】不是小数。
    # 证据：1%/99% 分位恰好 ±9.x%（对应 10% 涨跌停板），中位数 −0.31%
    # = 每日滑点+佣金成本。之前按小数处理导致复利曲线爆炸（MDD −1e30%）。
    df[RET_HOLD1D] = df[RET_HOLD1D] / 100.0
    # 价格错误离群值（max 曾达 +350729%）→ 按北交所 30% 板上浮裁剪
    df.loc[df[RET_HOLD1D].abs() > 0.35, RET_HOLD1D] = np.nan
    code = df['code']

    amt = np.log1p(df['amount'].clip(lower=0))
    ret = df.groupby('code', sort=False)['close'].pct_change()

    def grp_roll(s, w, how='median'):
        r = s.groupby(code, sort=False).rolling(w, min_periods=max(5, w // 2))
        return (r.median() if how == 'median' else r.mean()).reset_index(level=0, drop=True)

    level60 = grp_roll(amt, 60, 'median')
    df['liq_dev'] = 100.0 - ((amt - level60).groupby(df['date']).rank(pct=True) * 100)
    df['liq_level'] = 100.0 - (level60.groupby(df['date']).rank(pct=True) * 100)
    df['liquidity'] = 100.0 - (amt.groupby(df['date']).rank(pct=True) * 100)

    vol20 = ret.groupby(code, sort=False).rolling(20).std().reset_index(level=0, drop=True)
    vol_level = grp_roll(vol20.fillna(0), 60, 'median')
    df['volatility'] = 100.0 - (vol20.groupby(df['date']).rank(pct=True) * 100)
    df['vol_dev'] = 100.0 - ((vol20 - vol_level).groupby(df['date']).rank(pct=True) * 100)

    # 涨停代理过滤（与生产 risk_filter 同口径）
    pct = df.groupby('code', sort=False)['close'].pct_change() * 100
    lim = df['code'].map(board_limit)
    df['_near_limit_up'] = pct >= lim * 0.98

    return df


def main():
    df = load_and_enrich()
    df = df[df[RET_HOLD1D].notna()].copy()
    print(f'[info] {len(df)} 行 / {df["date"].nunique()} 天 / {df["code"].nunique()} 只')

    # ── 1. 正交性 ──
    cols = ['hot_theme', 'reversal_20d', 'liq_dev', 'liq_level', 'liquidity',
            'vol_dev', 'volatility', 'technical', 'volume_price']
    cols = [c for c in cols if c in df.columns]
    print('\n=== 横截面相关性（Spearman 秩相关抽样，判定独立性）===')
    samp = df.sample(min(400_000, len(df)), random_state=7)[cols].replace(
        [np.inf, -np.inf], np.nan).dropna()
    print(samp.corr(method='spearman').round(3).to_string())

    # ── 2. 权重方案对比 ──
    # 统一用横截面百分位秩（0-100）作为因子值，缺失填 50（与生产"缺失→中性"同口径）
    fac_cols = [c for c in cols if c != 'hot_theme'] + ['hot_theme']
    for c in fac_cols:
        df[c] = df.groupby('date')[c].rank(pct=True) * 100
    df[fac_cols] = df[fac_cols].fillna(50.0)

    # 涨停过滤（生产硬过滤口径）
    trade = df[~df['_near_limit_up'].fillna(False)].copy()
    print(f'[info] 剔除接近涨停后：{len(trade)} 行')

    SCHEMES = {
        'A_BASE_当前v1.json': {'hot_theme': .42, 'reversal_20d': .42, 'momentum': .03,
                               'capital_flow': .05, 'technical': .03, 'volume_price': .03,
                               'dragon_tiger': .02},
        'B_纯hot_theme': {'hot_theme': 1.0},
        'C_基线+liq_dev(挪噪声腿)': {'hot_theme': .40, 'reversal_20d': .40, 'liq_dev': .10,
                                    'volatility': .05, 'capital_flow': .05},
        'D_基线+liq_dev+vol_dev': {'hot_theme': .38, 'reversal_20d': .36, 'liq_dev': .12,
                                   'vol_dev': .06, 'volatility': .03, 'capital_flow': .05},
        'E_ICIR加权(理论最优)': {'hot_theme': .60, 'liq_dev': .15, 'reversal_20d': .10,
                                'vol_dev': .08, 'volatility': .07},
        'F_激进等权4腿': {'hot_theme': .25, 'reversal_20d': .25, 'liq_dev': .25, 'volatility': .25},
    }

    print('\n=== 权重方案实证对比（Top-N 组合，已剔除接近涨停）===')
    print('口径：ret_hold1d 已扣滑点佣金，单位百分数→已转小数，|ret|>35% 视为价格错误剔除')
    hdr = ('%-24s %7s %7s %11s %11s %9s %9s %9s' %
           ('方案', 'IC', 'ICIR', 'Top5超额%', 'Top10超额%', 'Top5t值', 'Top5MDD%', 'Top10MDD%'))
    print(hdr)
    print('-' * len(hdr))

    rows = []
    for name, w in SCHEMES.items():
        w = {k: v for k, v in w.items() if k in trade.columns}
        tot = sum(w.values())
        w = {k: v / tot for k, v in w.items()}
        score = sum(trade[k] * v for k, v in w.items())
        trade = trade.assign(_score=score)

        # 全样本 IC / ICIR
        ics = []
        tops5, tops10, uni = [], [], []
        for d, g in trade.groupby('date', sort=True):
            if len(g) < 100:
                continue
            ics.append(g['_score'].corr(g[RET_HOLD1D], method='spearman'))
            g5 = g.nlargest(5, '_score')[RET_HOLD1D].mean()
            g10 = g.nlargest(10, '_score')[RET_HOLD1D].mean()
            u = g[RET_HOLD1D].mean()
            tops5.append(g5 - u)
            tops10.append(g10 - u)
            uni.append(u)
        s = pd.Series(ics).dropna()
        ic, icir = s.mean(), (s.mean() / s.std() if s.std() > 0 else np.nan)
        e5 = pd.Series(tops5).dropna()
        e10 = pd.Series(tops10).dropna()

        def mdd(x):
            cum = (1 + x).cumprod()
            return float((cum / cum.cummax() - 1).min() * 100)

        t5 = e5.mean() / (e5.std() / np.sqrt(len(e5))) if e5.std() > 0 else np.nan
        print('%-24s %7.4f %7.3f %11.4f %11.4f %9.2f %9.2f %9.2f' %
              (name, ic, icir, e5.mean() * 100, e10.mean() * 100, t5, mdd(e5), mdd(e10)))
        rows.append((name, ic, icir, e5.mean() * 100, e10.mean() * 100, t5, mdd(e5), mdd(e10)))

    # ── 3. 成交额下限（可实践性检验）──
    print('\n=== 可实践性：加成交额下限后 Top5 日均超额（方案 C）===')
    w = {k: v for k, v in
         {'hot_theme': .40, 'reversal_20d': .40, 'liq_dev': .10, 'volatility': .05,
          'capital_flow': .05}.items() if k in trade.columns}
    tot = sum(w.values())
    w = {k: v / tot for k, v in w.items()}
    trade = trade.assign(_score=sum(trade[k] * v for k, v in w.items()))
    for floor in [0, 30e6, 50e6, 100e6]:
        sub = trade[trade['amount'] >= floor] if floor > 0 else trade
        ex = []
        for d, g in sub.groupby('date', sort=True):
            if len(g) < 100:
                continue
            ex.append(g.nlargest(5, '_score')[RET_HOLD1D].mean() - g[RET_HOLD1D].mean())
        e = pd.Series(ex).dropna()
        print('  成交额 >= %6.0f万 : 日均超额 %+.4f%%  (t=%.2f, 天数 %d)' %
              (floor / 1e4, e.mean() * 100, e.mean() / (e.std() / np.sqrt(len(e))), len(e)))

    with open(OUT_MD, 'w', encoding='utf-8') as f:
        f.write('# 权重方案实证对比（临时分析，2026-09-18）\n\n')
        f.write('| 方案 | IC | ICIR | Top5日均超额% | Top10日均超额% | Top5 t值 | Top5最大回撤% | Top10最大回撤% |\n')
        f.write('|---|---|---|---|---|---|---|---|\n')
        for r in rows:
            f.write('| %s | %.4f | %.3f | %.4f | %.4f | %.2f | %.2f | %.2f |\n' % r)
        f.write('\n> 口径：2024-01~2026-09，%d 天，已剔除接近涨停（板限×0.98）。\n'
                % trade['date'].nunique())
    print(f'\n[out] 已写入 {OUT_MD}')


if __name__ == '__main__':
    main()
