# -*- coding: utf-8 -*-
"""
临时分析脚本（非业务代码，可删）：方案 G 的权重寻优确认（训练/持有期切分）。

目的（用户要求）：确认 G 是当前最有权重方案后再落地，避免"手工拍脑袋"。

方法：
  - 目标函数 = Top5 日均超额的 t 值（稳健性优先，不只看均值）
  - 训练期 2024-01-01 ~ 2025-08-31（402 天）内寻优
  - 持有期 2025-09-01 ~ 2026-09-30（245 天）只做评估，不参与寻优
  - 搜索：A/E/G 基线 + Dirichlet 随机搜索 + 对最优解的坐标精修
  - 判定：若 G 的持有期表现接近寻优解（差距 < 0.15 pp/日 或在噪声内），确认 G 落地；
          若寻优解显著更优且持有期同样成立，用寻优解替换 G（仍需可解释）

用法：python scripts/_tmp_weight_opt.py
"""
import os
import sys
import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.oos_validator import RET_HOLD1D  # noqa: E402

PANEL_CACHE = 'data/cache/_tmp_panel_2023-10_2026-09.pkl'
TRAIN_END = '2025-08-31'
FACTORS = ['hot_theme', 'liq_dev', 'reversal_20d', 'vol_dev', 'volatility',
           'momentum', 'technical', 'volume_price', 'dragon_tiger']

N_RANDOM = 500
N_REFINE = 160

SCHEMES = {
    'A_当前基线': {'hot_theme': .4421, 'reversal_20d': .4421, 'momentum': .0316,
                   'technical': .0316, 'volume_price': .0316, 'dragon_tiger': .0211},
    'E_纯净版': {'hot_theme': .60, 'liq_dev': .15, 'reversal_20d': .10,
                 'vol_dev': .08, 'volatility': .07},
    'G_保守混合': {'hot_theme': .55, 'liq_dev': .14, 'reversal_20d': .10,
                   'vol_dev': .07, 'volatility': .06, 'momentum': .02,
                   'technical': .02, 'volume_price': .02, 'dragon_tiger': .0111},
}


def board_limit(code: str) -> float:
    c = str(code)
    if c.startswith(('688', '689', '300', '301')):
        return 20.0
    if c.startswith(('43', '83', '87', '88', '92')):
        return 30.0
    return 10.0


def load():
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
    df = df[(~df['_near_lu'].fillna(False)) & df[RET_HOLD1D].notna()].copy()

    for c in FACTORS:
        if c in df.columns:
            df[c] = df.groupby('date')[c].rank(pct=True) * 100
    for c in FACTORS:
        if c in df.columns:
            df[c] = df[c].fillna(50.0)
    return df


# 预切分：每日的 (因子矩阵, 收益) 缓存，避免每次评估重复 groupby
def prepare_daily(df, start, end):
    sub = df[(df['date'] >= start) & (df['date'] <= end)]
    days = []
    for d, g in sub.groupby('date', sort=True):
        if len(g) < 100:
            continue
        F = g[FACTORS].to_numpy(dtype=np.float64)          # NaN 已填 50
        r = g[RET_HOLD1D].to_numpy(dtype=np.float64)
        days.append((F, r))
    return days


def evaluate(days, w):
    """w: dict 因子->权重（缺失因子=0）。返回 (Top5日均超额%, t值)"""
    wv = np.array([w.get(f, 0.0) for f in FACTORS], dtype=np.float64)
    tot = wv.sum()
    if tot <= 0:
        return np.nan, np.nan
    wv /= tot
    ex5 = []
    for F, r in days:
        s = F @ wv
        k = min(5, len(r))
        idx = np.argpartition(-s, k - 1)[:k]
        ex5.append(r[idx].mean() - r.mean())
    e = np.asarray(ex5)
    e = e[np.isfinite(e)]
    if len(e) < 30 or e.std() == 0:
        return np.nan, np.nan
    return e.mean() * 100, e.mean() / (e.std() / np.sqrt(len(e)))


def main():
    df = load()
    train = prepare_daily(df, '2024-01-01', TRAIN_END)
    hold = prepare_daily(df, '2025-09-01', '2026-09-30')
    print(f'[info] 训练期 {len(train)} 天 / 持有期 {len(hold)} 天 / 因子 {len(FACTORS)} 个')

    # ── 基线 ──
    print('\n=== 基线（训练期寻优 / 持有期验证）===')
    print('%-14s %22s %22s' % ('方案', '训练期 Top5超额', '持有期 Top5超额'))
    print('-' * 62)
    for name, w in SCHEMES.items():
        m1, t1 = evaluate(train, w)
        m2, t2 = evaluate(hold, w)
        print('%-14s %10s%% (t=%+5.1f) %10s%% (t=%+5.1f)' %
              (name, f'{m1:+.3f}', t1, f'{m2:+.3f}', t2))

    # ── 随机搜索（Dirichlet，偏向稀疏）──
    print(f'\n=== 随机搜索 {N_RANDOM} 组（训练期 t 值为目标）===')
    rng = np.random.default_rng(42)
    results = []
    for i in range(N_RANDOM):
        wv = rng.dirichlet(np.full(len(FACTORS), 0.55))
        w = dict(zip(FACTORS, wv.tolist()))
        _, t1 = evaluate(train, w)
        if np.isfinite(t1):
            results.append((t1, w))
    results.sort(key=lambda x: -x[0])
    print('随机搜索 Top3（训练期）：')
    for t1, w in results[:3]:
        m1, _ = evaluate(train, w)
        m2, t2 = evaluate(hold, w)
        ws = ' / '.join(f'{k} {v:.2f}' for k, v in
                        sorted(w.items(), key=lambda x: -x[1]) if v >= 0.03)
        print(f'  t={t1:+.1f} 持有期 {m2:+.3f}% (t={t2:+.1f})  {ws}')

    # ── 坐标精修（围绕随机最优 + 围绕 G）──
    print(f'\n=== 坐标精修（每维 ±{[0.15,0.07,0.03]} 步长收缩）===')
    starts = [results[0][1], SCHEMES['G_保守混合'], SCHEMES['E_纯净版']]
    best_w, best_t = results[0][1], results[0][0]
    for start in starts:
        w = dict(start)
        t_cur = evaluate(train, w)[1]
        if not np.isfinite(t_cur):
            continue
        for step in (0.15, 0.07, 0.03):
            improved = True
            while improved:
                improved = False
                for f in FACTORS:
                    for delta in (+step, -step):
                        w2 = dict(w)
                        w2[f] = max(0.0, w2.get(f, 0.0) + delta)
                        if w2.get(f, 0.0) == w.get(f, 0.0):
                            continue
                        t2 = evaluate(train, w2)[1]
                        if np.isfinite(t2) and t2 > t_cur + 1e-4:
                            w, t_cur = w2, t2
                            improved = True
        if t_cur > best_t:
            best_t, best_w = t_cur, w
    print(f'精修后训练期最优 t = {best_t:+.2f}')

    # ── 最优解 vs G：持有期对决 ──
    print('\n=== 最终对决（持有期 2025-09~2026-09，寻优过程未见）===')
    m1, t1 = evaluate(train, best_w)
    m2, t2 = evaluate(hold, best_w)
    ws = ' / '.join(f'{k} {v:.3f}' for k, v in
                    sorted(best_w.items(), key=lambda x: -x[1]) if v >= 0.02)
    print(f'寻优最优权重（训练 t={t1:+.1f}）：{ws}')
    print(f'  训练期 {m1:+.3f}%  |  持有期 {m2:+.3f}% (t={t2:+.1f})')
    m3, t3 = evaluate(hold, SCHEMES['G_保守混合'])
    m1g, t1g = evaluate(train, SCHEMES['G_保守混合'])
    print(f'  G 训练期 {m1g:+.3f}% (t={t1g:+.1f})  |  持有期 {m3:+.3f}% (t={t3:+.1f})')
    print(f'  持有期差（寻优−G）= {m2-m3:+.3f} pp/日')
    # 分年度稳定性
    print('\n  寻优解分年度持有表现：')
    for label, s, e in [('2024', '2024-01-01', '2024-12-31'),
                        ('2025', '2025-01-01', '2025-12-31'),
                        ('2026年内', '2026-01-01', '2026-09-30')]:
        sub = prepare_daily(df, s, e)
        mo, to = evaluate(sub, best_w)
        mg, tg = evaluate(sub, SCHEMES['G_保守混合'])
        print(f'    {label}: 寻优 {mo:+.3f}% (t={to:+.1f})  vs G {mg:+.3f}% (t={tg:+.1f})')


if __name__ == '__main__':
    main()
