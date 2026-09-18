"""
横截面因子标准化（2026-09-05 审查报告 P2-K）

解决的问题
----------
7 因子的原始分分布形态不一：有些因子（hot_theme）在池内天然高分扎堆，
有些（capital_flow 绝对值）受极值拖累。直接加权求和时，"分数分布宽"的
因子实际话语权 > 配置权重。横截面标准化把每个因子在当日候选池内拉到
统一分布（百分位 0-100），让配置权重 = 真实话语权。

设计
----
- `rank`（默认/主推）：百分位排名 0-100（平均秩处理并列，与 pandas
  rank(pct=True) 口径一致，也即实盘 rps_20 / capital_flow 百分位的口径）。
  对极值天然免疫，无需 winsorize。
- `winsor_z`：MAD winsorize（±3·MAD 等效于正态 ±3σ）后 z-score 经 logistic
  软压缩映射 0-100。保留相对距离信息，但对分布形态敏感，仅作对照实验用。

使用边界（重要）
----------------
标准化是**实验开关**（config.yml short_term.sell.standardize，默认 false）：
启用后每日因子分布被重排 → 与历史回测/已校准权重 v1.json 的口径不再可比。
必须重跑 --mode oos 与 scripts/calibrate_weights.py 之后再切换权重。
"""

import logging
from typing import Dict, Optional

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)


def mad_winsorize(values: np.ndarray, n_mad: float = 3.0) -> np.ndarray:
    """MAD winsorize：超出 median ± n_mad·MAD 的值截断在边界。

    MAD = median(|x - median(x)|)；1.4826·MAD ≈ 正态 σ。
    全部相同值（MAD=0）时原样返回。
    """
    v = np.asarray(values, dtype=float)
    med = np.nanmedian(v)
    mad = np.nanmedian(np.abs(v - med))
    if mad == 0 or np.isnan(mad):
        return v
    upper = med + n_mad * 1.4826 * mad
    lower = med - n_mad * 1.4826 * mad
    return np.clip(v, lower, upper)


def cross_sectional_standardize(values: Dict[str, float],
                                method: str = 'rank') -> Dict[str, Optional[float]]:
    """对当日候选池的某个因子分做横截面标准化。

    参数：
        values: {code: 因子原始分}（None/NaN 的 code 会原样返回 None）
        method: 'rank'（百分位 0-100，默认）| 'winsor_z'（MAD winsorize 后 logistic→0-100）

    返回：
        {code: 标准化分 或 None}。输入 None/NaN → 返回 None（调用方保持
        该股该因子原逻辑，不硬造分值）。
    """
    clean: Dict[str, Optional[float]] = {}
    for code, v in values.items():
        if v is None:
            clean[code] = None
            continue
        try:
            fv = float(v)
        except (TypeError, ValueError):
            clean[code] = None
            continue
        clean[code] = None if np.isnan(fv) else fv

    valid = {c: v for c, v in clean.items() if v is not None}
    if not valid:
        return clean

    if method == 'rank':
        pct = pd.Series(valid).rank(pct=True) * 100.0   # 平均秩处理并列
        out = {c: round(float(p), 2) for c, p in pct.items()}
    elif method == 'winsor_z':
        vs = np.array(list(valid.values()), dtype=float)
        w = mad_winsorize(vs)
        med = np.median(w)
        mad = np.median(np.abs(w - med))
        sigma = 1.4826 * mad if mad > 0 else (np.std(w) if np.std(w) > 0 else 1.0)
        z = (w - med) / sigma
        p = 1.0 / (1.0 + np.exp(-z))                    # logistic 软压缩，避免硬截 0/100
        out = {c: round(float(p_) * 100, 2) for (c, _), p_ in zip(valid.items(), p)}
    else:
        raise ValueError(f'未知标准化方法: {method}')

    return {**clean, **out}
