# -*- coding: utf-8 -*-
"""
临时分析脚本（非业务代码，可删）：权重方案的稳健性检验（子样本 / 分年度）。

目的：排除"方案 E 只在特定年份/市况有效"的过拟合风险。
方案 A(当前基线) / B(纯hot) / E(hot主导+新因子排序) 在多个子区间重复对比。

用法：python scripts/_tmp_robust.py
"""
import os
import sys
import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.oos_validator import RET_HOLD1D  # noqa: E402

PANEL_CACHE = 'data/cache/_tmp_panel_2023-10_2026-09.pkl'


def board_limit(code: str) -> float:
    c = str(code)
    if c.startswith(('688', '689', '300', '301')):
        return 20.0
    if c.startswith(('43', '83', '87', '88', '92')):
        return 30.0
    return 10.0


def enrich():
    df = pd.read_pickle(PANEL_CACHE).sort_values(['code', 'date']).reset_index(drop=True)
    df[RET_HOLD1D] = df[RET_HOLD1D] / 100.0
    df.loc[df[RET_HOLD1D].abs() > 0.35, RET_HOLD1D] = np.nan
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


SCHEMES = {
    'A_当前基线': {'hot_theme': .42, 'reversal_20d': .42, 'momentum': .03,
                   'technical': .03, 'volume_price': .03, 'dragon_tiger': .02},
    'B_纯hot': {'hot_theme': 1.0},
    'E_hot主导+新因子': {'hot_theme': .60, 'liq_dev': .15, 'reversal_20d': .10,
                         'vol_dev': .08, 'volatility': .07},
}


def run(trade, name, w):
    w = {k: v for k, v in w.items() if k in trade.columns}
    tot = sum(w.values())
    w = {k: v / tot for k, v in w.items()}
    trade = trade.assign(_s=sum(trade[k] * v for k, v in w.items()))
    ex5, ex10 = [], []
    for d, g in trade.groupby('date', sort=True):
        if len(g) < 100:
            continue
        ex5.append(g.nlargest(5, '_s')[RET_HOLD1D].mean() - g[RET_HOLD1D].mean())
        ex10.append(g.nlargest(10, '_s')[RET_HOLD1D].mean() - g[RET_HOLD1D].mean())
    e5 = pd.Series(ex5).dropna()
    e10 = pd.Series(ex10).dropna()
    t5 = e5.mean() / (e5.std() / np.sqrt(len(e5))) if e5.std() > 0 else np.nan
    return e5.mean() * 100, e10.mean() * 100, t5, len(e5)


def main():
    df = enrich()
    trade = df[(~df['_near_lu'].fillna(False)) & df[RET_HOLD1D].notna()].copy()

    fac = ['hot_theme', 'reversal_20d', 'momentum', 'technical', 'volume_price',
           'dragon_tiger', 'liq_dev', 'vol_dev', 'volatility']
    fac = [c for c in fac if c in trade.columns]
    for c in fac:
        trade[c] = trade.groupby('date')[c].rank(pct=True) * 100
    trade[fac] = trade[fac].fillna(50.0)

    periods = [
        ('全样本 2024-01~2026-09', None, None),
        ('2024 全年', '2024-01-01', '2024-12-31'),
        ('2025 全年', '2025-01-01', '2025-12-31'),
        ('2026 年内', '2026-01-01', '2026-09-30'),
        ('前半段(训练)', '2024-01-01', '2025-08-31'),
        ('后半段(检验)', '2025-09-01', '2026-09-30'),
    ]

    print('=' * 92)
    print('稳健性检验：Top5 日均超额%（已扣成本、已剔除接近涨停不可买股）')
    print('=' * 92)
    print('%-22s %18s %18s %18s' % ('区间', 'A_当前基线', 'B_纯hot', 'E_hot主导+新因子'))
    print('-' * 92)
    for label, s, e in periods:
        sub = trade if s is None else trade[(trade['date'] >= s) & (trade['date'] <= e)]
        if sub['date'].nunique() < 30:
            continue
        cells = []
        for name, w in SCHEMES.items():
            m5, m10, t5, n = run(sub, name, w)
            cells.append('%+.3f%% (t=%+.1f)' % (m5, t5))
        print('%-22s %18s %18s %18s' % (label, *cells))

    print()
    print('=' * 92)
    print('E 相对 A 的改进是否稳定（E − A，百分点/日）')
    print('=' * 92)
    for label, s, e in periods:
        sub = trade if s is None else trade[(trade['date'] >= s) & (trade['date'] <= e)]
        if sub['date'].nunique() < 30:
            continue
        a = run(sub, 'A', SCHEMES['A_当前基线'])[0]
        ee = run(sub, 'E', SCHEMES['E_hot主导+新因子'])[0]
        b = run(sub, 'B', SCHEMES['B_纯hot'])[0]
        print('  %-22s E−A = %+.3f pp   E−B = %+.3f pp   (%d 天)'
              % (label, ee - a, ee - b, sub['date'].nunique()))


if __name__ == '__main__':
    main()
