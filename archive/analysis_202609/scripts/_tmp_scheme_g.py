# -*- coding: utf-8 -*-
"""
临时分析脚本（非业务代码，可删）：方案 G（保守混合）实证 + 热点数据失效压力测试。

背景（用户决策 2026-09-19）：
  1. 口径确认 = 尾盘买入 → ret_hold1d，方案 E 结论适用
  2. "全归零不太合理" → 保留小权重噪声腿作为降级兜底
  本脚本验证：加回 0.08 的噪声腿（方案 G）损失多少收益，
  以及 hot_theme 数据源失效时，G 的兜底能力是否优于 E。

方案：
  A 当前基线    hot .42 / rev .42 / mom .03 / tech .03 / vp .03 / dt .02
  E 纯净版      hot .60 / liq_dev .15 / rev .10 / vol_dev .08 / vol .07
  G 保守混合    hot .55 / liq_dev .14 / rev .10 / vol_dev .07 / vol .06
                + mom .02 / tech .02 / vp .02 / dt .01 / cf .01   (Σ=1.00)

用法：python scripts/_tmp_scheme_g.py
"""
import os
import sys
import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.oos_validator import OOSValidator, RET_HOLD1D  # noqa: E402

PANEL_CACHE = 'data/cache/_tmp_panel_2023-10_2026-09.pkl'
START, END = '2023-10-01', '2026-09-03'
BASE_FACTORS = ['momentum', 'technical', 'volume_price', 'reversal_20d',
                'volatility', 'liquidity', 'hot_theme', 'dragon_tiger']

SCHEMES = {
    'A_当前基线': {'hot_theme': .42, 'reversal_20d': .42, 'momentum': .03,
                   'technical': .03, 'volume_price': .03, 'dragon_tiger': .02},
    'E_纯净版': {'hot_theme': .60, 'liq_dev': .15, 'reversal_20d': .10,
                 'vol_dev': .08, 'volatility': .07},
    'G_保守混合': {'hot_theme': .55, 'liq_dev': .14, 'reversal_20d': .10,
                   'vol_dev': .07, 'volatility': .06, 'momentum': .02,
                   'technical': .02, 'volume_price': .02, 'dragon_tiger': .01,
                   'capital_flow': .01},
}


def board_limit(code: str) -> float:
    c = str(code)
    if c.startswith(('688', '689', '300', '301')):
        return 20.0
    if c.startswith(('43', '83', '87', '88', '92')):
        return 30.0
    return 10.0


def load_and_enrich():
    if os.path.exists(PANEL_CACHE):
        print(f'[cache] 读取 {PANEL_CACHE}')
        df = pd.read_pickle(PANEL_CACHE)
    else:
        print(f'[build] 构造面板 {START} ~ {END} ...')
        v = OOSValidator()
        df = v.build_panel(START, END, factors=BASE_FACTORS)
        keep = ['date', 'code', 'close', 'volume', 'amount', RET_HOLD1D] + BASE_FACTORS
        df = df[[c for c in keep if c in df.columns]]
        df.to_pickle(PANEL_CACHE)
        print(f'[build] 已缓存 {len(df)} 行')

    df = df.sort_values(['code', 'date']).reset_index(drop=True)
    df[RET_HOLD1D] = df[RET_HOLD1D] / 100.0                    # 单位修正：百分数→小数
    df.loc[df[RET_HOLD1D].abs() > 0.35, RET_HOLD1D] = np.nan   # 价格错误离群值剔除

    code = df['code']
    amt = np.log1p(df['amount'].clip(lower=0))
    ret = df.groupby('code', sort=False)['close'].pct_change()

    def grp_roll(s, w, how='median'):
        r = s.groupby(code, sort=False).rolling(w, min_periods=max(5, w // 2))
        return (r.median() if how == 'median' else r.mean()).reset_index(level=0, drop=True)

    level60 = grp_roll(amt, 60, 'median')
    df['liq_dev'] = 100.0 - ((amt - level60).groupby(df['date']).rank(pct=True) * 100)
    vol20 = ret.groupby(code, sort=False).rolling(20).std().reset_index(level=0, drop=True)
    vol_level = grp_roll(vol20.fillna(0), 60, 'median')
    df['volatility'] = 100.0 - (vol20.groupby(df['date']).rank(pct=True) * 100)
    df['vol_dev'] = 100.0 - ((vol20 - vol_level).groupby(df['date']).rank(pct=True) * 100)

    pct = df.groupby('code', sort=False)['close'].pct_change() * 100
    df['_near_lu'] = pct >= df['code'].map(board_limit) * 0.98
    return df


def run(trade, w, neutralize_hot=False):
    trade = trade.copy()
    if neutralize_hot:
        trade['hot_theme'] = 50.0   # 数据源失效 → 全市场同分（生产 fillna(50) 口径）
    w = {k: v for k, v in w.items() if k in trade.columns}
    tot = sum(w.values())
    w = {k: v / tot for k, v in w.items()}
    trade['_s'] = sum(trade[k] * v for k, v in w.items())
    ex5 = []
    for d, g in trade.groupby('date', sort=True):
        if len(g) < 100:
            continue
        ex5.append(g.nlargest(5, '_s')[RET_HOLD1D].mean() - g[RET_HOLD1D].mean())
    e5 = pd.Series(ex5).dropna()
    t = e5.mean() / (e5.std() / np.sqrt(len(e5))) if e5.std() > 0 else np.nan
    return e5.mean() * 100, t, len(e5)


def main():
    df = load_and_enrich()
    trade = df[(~df['_near_lu'].fillna(False)) & df[RET_HOLD1D].notna()].copy()

    fac = ['hot_theme', 'reversal_20d', 'momentum', 'technical', 'volume_price',
           'dragon_tiger', 'liq_dev', 'vol_dev', 'volatility']
    fac = [c for c in fac if c in trade.columns]
    for c in fac:
        trade[c] = trade.groupby('date')[c].rank(pct=True) * 100
    trade[fac] = trade[fac].fillna(50.0)

    periods = [('全样本', None, None), ('2024', '2024-01-01', '2024-12-31'),
               ('2025', '2025-01-01', '2025-12-31'), ('2026年内', '2026-01-01', '2026-09-30')]

    print('=' * 96)
    print('① 常规对比：Top5 日均超额%（已扣成本、已剔除接近涨停）')
    print('=' * 96)
    print('%-10s %16s %16s %16s' % ('区间', 'A_当前基线', 'E_纯净版', 'G_保守混合'))
    print('-' * 96)
    for label, s, e in periods:
        sub = trade if s is None else trade[(trade['date'] >= s) & (trade['date'] <= e)]
        if sub['date'].nunique() < 30:
            continue
        cells = ['%+.3f%% (t=%+.1f)' % run(sub, w)[:2] for w in SCHEMES.values()]
        print('%-10s %16s %16s %16s' % (label, *cells))

    print()
    print('=' * 96)
    print('② 压力测试：hot_theme 数据源失效（全市场 fillna(50) 同分）→ 检验兜底能力')
    print('=' * 96)
    print('%-10s %16s %16s' % ('区间', 'E_纯净版(失效)', 'G_保守混合(失效)'))
    print('-' * 96)
    for label, s, e in periods:
        sub = trade if s is None else trade[(trade['date'] >= s) & (trade['date'] <= e)]
        if sub['date'].nunique() < 30:
            continue
        c1 = '%+.3f%% (t=%+.1f)' % run(sub, SCHEMES['E_纯净版'], neutralize_hot=True)[:2]
        c2 = '%+.3f%% (t=%+.1f)' % run(sub, SCHEMES['G_保守混合'], neutralize_hot=True)[:2]
        print('%-10s %16s %16s' % (label, c1, c2))


if __name__ == '__main__':
    main()
