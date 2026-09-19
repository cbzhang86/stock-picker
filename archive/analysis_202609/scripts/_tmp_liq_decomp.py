# -*- coding: utf-8 -*-
"""
临时分析脚本（非业务代码，可删）：流动性/波动率因子的"规模效应"隔离检验。

问题：OOS 发现 liquidity = -pct(log(amount)) 的 IC = +0.0516 (t=3.81)，
      但 log(amount) 与市值高度相关，这个溢价可能只是"小市值效应"的伪装。

方法：把 log(amount) 拆成两个正交成分
      level  = 60 日滚动中位数（持久水平 ≈ 规模/常驻流动性）
      dev    = log(amount) - level（当日相对自身常态的偏离 = 瞬时活跃度）
  若 IC 主要来自 level  → 是规模/流动性水平效应（与小市值同源，需中性化）
  若 IC 主要来自 dev    → 是"当日清淡/低关注"效应，是真正独立的新 alpha

同时检验 Amihud 非流动性（|ret|/amount）作为对照。

用法：python scripts/_tmp_liq_decomp.py
"""
import os
import sys
import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.oos_validator import OOSValidator, RET_HOLD1D  # noqa: E402

PANEL_CACHE = 'data/cache/_tmp_panel_2023-10_2026-09.pkl'
START, END = '2023-10-01', '2026-09-03'
EVAL_START = '2024-01-01'   # 评估区间（前 3 个月只用于滚动窗口预热）

BASE_FACTORS = ['momentum', 'technical', 'volume_price', 'reversal_20d',
                'volatility', 'liquidity', 'hot_theme', 'dragon_tiger']


def build_or_load():
    if os.path.exists(PANEL_CACHE):
        print(f'[cache] 读取已缓存面板 {PANEL_CACHE}')
        return pd.read_pickle(PANEL_CACHE)
    print(f'[build] 构造面板 {START} ~ {END} ...')
    v = OOSValidator()
    panel = v.build_panel(START, END, factors=BASE_FACTORS)
    keep = ['date', 'code', 'close', 'volume', 'amount', RET_HOLD1D] + BASE_FACTORS
    keep = [c for c in keep if c in panel.columns]
    panel = panel[keep]
    # 注：环境无 pyarrow/fastparquet，用 pickle 缓存（本机临时文件，勿入库）
    panel.to_pickle(PANEL_CACHE)
    print(f'[build] 已缓存 {len(panel)} 行 -> {PANEL_CACHE}')
    return panel


def add_decomp_factors(df: pd.DataFrame) -> pd.DataFrame:
    df = df.sort_values(['code', 'date']).reset_index(drop=True)
    code = df['code']

    # ── 基础量 ──
    amt = np.log1p(df['amount'].clip(lower=0))
    ret = df.groupby('code', sort=False)['close'].pct_change()

    def grp_roll(s, w, how='median'):
        r = s.groupby(code, sort=False).rolling(w, min_periods=max(5, w // 2))
        return (r.median() if how == 'median' else r.mean()).reset_index(level=0, drop=True)

    # ── ① 持久水平（规模/常驻流动性代理）──
    level60 = grp_roll(amt, 60, 'median')
    df['liq_level'] = 100.0 - (level60.groupby(df['date']).rank(pct=True) * 100)

    # ── ② 瞬时偏离（当日成交额 vs 自身常态，按构造与 level 正交）──
    dev = amt - level60
    df['liq_dev'] = 100.0 - (dev.groupby(df['date']).rank(pct=True) * 100)

    # ── ③ 原始 log(amount)（对照，已验证 IC +0.0516）──
    df['liq_raw'] = 100.0 - (amt.groupby(df['date']).rank(pct=True) * 100)

    # ── ④ Amihud 非流动性：20 日 mean(|ret| / amount)，高 = 不易成交 ──
    amihud = (ret.abs() / df['amount'].clip(lower=1.0))
    amihud20 = grp_roll(amihud, 20, 'mean')
    df['amihud20'] = amihud20.groupby(df['date']).rank(pct=True) * 100

    # ── ⑤ 波动率同样做分解（区分"天生高波动" vs "近期异常波动"）──
    vol20 = grp_roll(ret, 20, 'std') if False else (
        ret.groupby(code, sort=False).rolling(20).std().reset_index(level=0, drop=True))
    vol_level = grp_roll(vol20.fillna(0), 60, 'median')
    df['vol_level'] = 100.0 - (vol_level.groupby(df['date']).rank(pct=True) * 100)
    df['vol_dev'] = 100.0 - ((vol20 - vol_level).groupby(df['date']).rank(pct=True) * 100)

    return df


def ic_stats(panel: pd.DataFrame, facs, ret_col=RET_HOLD1D):
    """按日横截面 Spearman IC → 均值 / ICIR / t 值"""
    out = []
    daily = {}
    for d, g in panel.groupby('date', sort=True):
        gg = g[[ret_col] + facs].dropna(subset=[ret_col])
        for f in facs:
            sub = gg[[f, ret_col]].replace([np.inf, -np.inf], np.nan).dropna()
            if len(sub) < 50:
                continue
            daily.setdefault(f, []).append(sub[f].corr(sub[ret_col], method='spearman'))

    for f in facs:
        s = pd.Series(daily.get(f, []))
        s = s.replace([np.inf, -np.inf], np.nan).dropna()
        if len(s) < 30:
            out.append((f, np.nan, np.nan, np.nan, len(s)))
            continue
        ic, sd = s.mean(), s.std()
        icir = ic / sd if sd > 0 else np.nan
        t = ic / (sd / np.sqrt(len(s))) if sd > 0 else np.nan
        out.append((f, ic, icir, t, len(s)))
    return out


def main():
    panel = build_or_load()
    print(f'[info] 面板 {len(panel)} 行 / {panel["date"].nunique()} 天 / {panel["code"].nunique()} 只')

    print('[calc] 计算分解因子 ...')
    panel = add_decomp_factors(panel)
    panel = panel[panel['date'] >= EVAL_START].copy()
    print(f'[info] 评估区间 {EVAL_START}+ : {len(panel)} 行 / {panel["date"].nunique()} 天')

    facs = ['liq_raw', 'liq_level', 'liq_dev', 'amihud20',
            'vol_level', 'vol_dev', 'volatility', 'liquidity',
            'hot_theme', 'reversal_20d']
    facs = [f for f in facs if f in panel.columns]

    print()
    print('=' * 88)
    print('流动性/波动率 分解检验  (Spearman IC, 口径 %s)' % RET_HOLD1D)
    print('=' * 88)
    print('%-14s %10s %10s %10s %8s' % ('因子', 'IC', 'ICIR', 't值', '天数'))
    print('-' * 88)
    for f, ic, icir, t, n in ic_stats(panel, facs):
        flag = ''
        if isinstance(t, float) and abs(t) >= 2:
            flag = '  <-- 显著'
        print('%-14s %10.4f %10.3f %10.2f %8d%s' % (f, ic, icir, t, n, flag))

    # ── 相关性：level 与 dev 是否真的正交 ──
    print()
    print('=' * 88)
    print('横截面相关性（判定 level / dev 是否可分离）')
    print('=' * 88)
    sub = panel[['liq_level', 'liq_dev', 'liq_raw', 'amihud20', 'volatility']].dropna()
    print(sub.corr().round(3).to_string())


if __name__ == '__main__':
    main()
