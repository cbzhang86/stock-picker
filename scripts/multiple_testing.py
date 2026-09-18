"""
多重检验校正工具 — DSR / PBO（2026-09-05 审查报告 P1-F）

解决的问题
----------
权重实验、因子实验每次改动都跑一次回测/OOS，做得越多，"最好那次"越可能
是运气（multiple testing / data snooping）。单看某个 run 的 Sharpe 会系统性
高估策略质量。本工具对一批试验（trials）做两个标准校正：

1. DSR（Deflated Sharpe Ratio, Bailey & López de Prado 2014）
   - 先从全部 M 次试验的 SR 分布估计"期望最大 SR"（expected max SR under
     multiple testing，含 Euler–Mascheroni 修正），作为基准 SR*
   - 再算 PSR：真 Sharpe > SR* 的概率（含偏度/峰度的高阶修正）
   - DSR < 0.95 → "试验赢家"的 Sharpe 无法与多重检验噪声区分

2. PBO（Probability of Backtest Overfitting, López de Prado CSCV 2015）
   - 组合对称交叉验证：把时间轴切成 S 块（默认 16），枚举全部
     C(S, S/2) 种"训练块/测试块"对拼
   - 每种拼法下：训练集表现最优的试验 j，其在测试集的相对排名 logit
   - PBO = logit < 0 的组合占比（训练赢家在样本外跑输中位数的概率）
   - PBO > 0.5 → 选择过程本身就是过拟合

输入格式（--input）
------------------
CSV：每列一个试验，每行一个日收益（小数或百分号均可，单位内部自适应）。
第一列为日期（可选）。
    date,trial_A,trial_B,trial_C
    2026-06-30,0.012,-0.003,0.005
    ...

用法
----
    python scripts/multiple_testing.py --input trials.csv --output overfit_report.json
    python scripts/multiple_testing.py --input trials.csv --benchmark-only  # 只算原始统计

注意
----
- 试验数 M < 4 时 DSR 的 max-SR 估计不稳，报告会降级为提示而非结论
- 每次权重/因子实验应把全部试验的日收益矩阵存档到
  data/reports/trial_matrices/，供本工具事后审计（不是只存赢家）
"""

import argparse
import json
import logging
import math
import os
import sys
from itertools import combinations
from typing import Dict, List, Optional

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)

# Euler–Mascheroni 常数（max-SR 估计用）
GAMMA = 0.5772156649015329

# PBO 的时间块数（López de Prado 推荐 S=16；需偶数且试验期足够长）
DEFAULT_SPLITS = 16


# ── 基础统计 ─────────────────────────────────────────────────

def annualized_sharpe(daily_returns: np.ndarray) -> float:
    """日频收益 → 年化 Sharpe（rf=0，252 交易日）"""
    r = np.asarray(daily_returns, dtype=float)
    r = r[~np.isnan(r)]
    if len(r) < 2 or np.std(r, ddof=1) == 0:
        return 0.0
    return float(np.mean(r) / np.std(r, ddof=1) * math.sqrt(252))


def skewness(r: np.ndarray) -> float:
    r = r[~np.isnan(r)]
    if len(r) < 3:
        return 0.0
    m = r.mean()
    s = r.std(ddof=1)
    if s == 0:
        return 0.0
    return float(((r - m) ** 3).mean() / s ** 3)


def kurtosis(r: np.ndarray) -> float:
    """超额峰度（正态=0）"""
    r = r[~np.isnan(r)]
    if len(r) < 4:
        return 0.0
    m = r.mean()
    s = r.std(ddof=1)
    if s == 0:
        return 0.0
    return float(((r - m) ** 4).mean() / s ** 4 - 3.0)


# ── DSR ──────────────────────────────────────────────────────

def expected_max_sharpe(n_trials: int, sr_variance: float) -> float:
    """多重检验下的期望最大 Sharpe（Bailey & López de Prado 2014, eq. 5）。

    E[max SR] ≈ sqrt(V[SR]) × ((1-γ)·Φ⁻¹(1-1/M) + γ·Φ⁻¹(1-1/(M·e)))
    """
    if n_trials < 2 or sr_variance <= 0:
        return 0.0
    e = math.e
    z1 = _norm_ppf(1.0 - 1.0 / n_trials)
    z2 = _norm_ppf(1.0 - 1.0 / (n_trials * e))
    return math.sqrt(sr_variance) * ((1 - GAMMA) * z1 + GAMMA * z2)


def psr(sharpe: float, n_obs: int, benchmark_sr: float = 0.0,
        skew: float = 0.0, kurt: float = 0.0) -> float:
    """Probabilistic Sharpe Ratio：真 SR > benchmark 的概率（年化口径自动抵消）。

    PSR = Φ( (SR - SR*)·sqrt(n-1) / sqrt(1 - γ3·SR + (γ4-1)/4·SR²) )
    注意：公式里的 SR/SR*/γ3/γ4 必须是同一频率口径（这里统一用日频）。
    """
    if n_obs < 2:
        return 0.5
    sr_d = sharpe / math.sqrt(252)          # 年化 → 日频
    sr_b_d = benchmark_sr / math.sqrt(252)
    denom = 1.0 - skew * sr_d + (kurt) / 4.0 * sr_d ** 2
    if denom <= 0:
        return 0.5
    z = (sr_d - sr_b_d) * math.sqrt(n_obs - 1) / math.sqrt(denom)
    return float(_norm_cdf(z))


def deflated_sharpe_ratio(daily_returns: np.ndarray,
                          all_trial_sharpes: List[float]) -> Dict:
    """单次试验的 DSR：以全部试验的期望最大 SR 为基准。

    返回 {dsr, benchmark_sr, n_trials, psr_inputs}
    """
    srs = np.asarray([s for s in all_trial_sharpes if s is not None and not np.isnan(s)])
    n_trials = len(srs)
    sr = annualized_sharpe(daily_returns)
    if n_trials < 2:
        return {'dsr': None, 'benchmark_sr': 0.0, 'n_trials': n_trials,
                'note': '试验数 < 2，DSR 无定义（无多重检验校正必要）'}
    sr_var = float(np.var(srs / math.sqrt(252), ddof=1))  # 日频 SR 的方差
    bench = expected_max_sharpe(n_trials, sr_var)
    d = psr(sr, len(daily_returns), benchmark_sr=bench,
            skew=skewness(daily_returns), kurt=kurtosis(daily_returns))
    return {'dsr': round(d, 4), 'benchmark_sr': round(bench, 4),
            'n_trials': n_trials,
            'note': 'DSR>0.95 可视为通过多重检验校正（90% 单侧阈值的常用替代）'
            if d >= 0.95 else 'DSR≤0.95：该试验的 Sharpe 无法与多重检验噪声区分'}


def _norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def _norm_ppf(p: float) -> float:
    """逆正态分布（Acklam 近似，双精度足够）"""
    if not 0.0 < p < 1.0:
        raise ValueError('p out of range')
    a = [-3.969683028665376e+01, 2.209460984245205e+02, -2.759285104469687e+02,
         1.383577518672690e+02, -3.066479806614716e+01, 2.506628277459239e+00]
    b = [-5.447609879822406e+01, 1.615858368580409e+02, -1.556989798598866e+02,
         6.680131188771972e+01, -1.328068155288572e+01]
    c = [-7.784894002430293e-03, -3.223964580411365e-01, -2.400758277161838e+00,
         -2.549732539343734e+00, 4.374664141464968e+00, 2.938163982698783e+00]
    d = [7.784695709041462e-03, 3.224671290700398e-01, 2.445134137142996e+00,
         3.754408661907416e+00]
    plow, phigh = 0.02425, 1 - 0.02425
    if p < plow:
        q = math.sqrt(-2 * math.log(p))
        return (((((c[0]*q+c[1])*q+c[2])*q+c[3])*q+c[4])*q+c[5]) / \
               ((((d[0]*q+d[1])*q+d[2])*q+d[3])*q+1)
    if p <= phigh:
        q = p - 0.5
        r = q * q
        return (((((a[0]*r+a[1])*r+a[2])*r+a[3])*r+a[4])*r+a[5])*q / \
               (((((b[0]*r+b[1])*r+b[2])*r+b[3])*r+b[4])*r+1)
    q = math.sqrt(-2 * math.log(1 - p))
    return -(((((c[0]*q+c[1])*q+c[2])*q+c[3])*q+c[4])*q+c[5]) / \
           ((((d[0]*q+d[1])*q+d[2])*q+d[3])*q+1)


# ── PBO（CSCV）───────────────────────────────────────────────

def pbo_cscv(returns_matrix: np.ndarray, n_splits: int = DEFAULT_SPLITS) -> Dict:
    """Probability of Backtest Overfitting（组合对称交叉验证）。

    参数：
        returns_matrix: T×M 矩阵（行=时间，列=试验），NaN 视为该试验该日缺失
        n_splits: 时间块数（偶数）

    返回 {pbo, n_combinations, logits_below_zero, note}
    """
    R = np.asarray(returns_matrix, dtype=float)
    T, M = R.shape
    if M < 2:
        return {'pbo': None, 'n_combinations': 0,
                'note': '试验数 < 2，PBO 无定义'}
    if T < n_splits * 2:
        return {'pbo': None, 'n_combinations': 0,
                'note': f'时间样本 {T} 行不足以切 {n_splits} 块（每块≥2 行），降低 --splits'}

    # 按时间切块（等宽）
    block_edges = np.linspace(0, T, n_splits + 1).astype(int)
    blocks = [list(range(block_edges[i], block_edges[i + 1]))
              for i in range(n_splits)]

    logits = []
    n_below = 0
    n_combos = 0
    half = n_splits // 2
    for train_blocks in combinations(range(n_splits), half):
        train_idx = sorted(i for b in train_blocks for i in blocks[b])
        test_idx = sorted(i for b in range(n_splits) if b not in train_blocks
                          for i in blocks[b])
        if not train_idx or not test_idx:
            continue
        train = R[train_idx, :]
        test = R[test_idx, :]
        # 训练集最优试验（用均值 Sharpe 代理；NaN 忽略）
        with np.errstate(invalid='ignore'):
            train_perf = np.nanmean(train, axis=0)
            test_perf = np.nanmean(test, axis=0)
        if np.all(np.isnan(train_perf)) or np.all(np.isnan(test_perf)):
            continue
        best = int(np.nanargmax(train_perf))
        # 测试集里该试验的相对排名 → logit
        ranks = pd.Series(test_perf).rank(pct=False).values  # 1=最差
        rank_j = ranks[best]
        n_eff = np.sum(~np.isnan(test_perf))
        if n_eff < 2:
            continue
        # 相对排名 ω ∈ (0,1)，1=最优；logit = ln(ω/(1-ω))
        omega = rank_j / n_eff
        omega = min(max(omega, 1e-6), 1 - 1e-6)
        logit = math.log(omega / (1 - omega))
        logits.append(logit)
        n_combos += 1
        if logit < 0:
            n_below += 1

    if n_combos == 0:
        return {'pbo': None, 'n_combinations': 0, 'note': '无有效组合'}

    pbo = n_below / n_combos
    if pbo > 0.5:
        note = 'PBO>0.5：选择流程大概率在过拟合（训练赢家样本外跑输中位数）'
    elif pbo > 0.25:
        note = 'PBO 处于灰色地带：有一定过拟合风险，结合 DSR 与经济逻辑判断'
    else:
        note = 'PBO 较低：选择流程的样本外一致性较好'
    return {'pbo': round(pbo, 4), 'n_combinations': n_combos,
            'logits_below_zero': n_below, 'note': note}


# ── IO ───────────────────────────────────────────────────────

def load_trials_csv(path: str) -> pd.DataFrame:
    """读取试验矩阵 CSV。自动识别并丢弃日期列。"""
    df = pd.read_csv(path)
    for col in ('date', 'trade_date', '日期'):
        if col in df.columns:
            df = df.drop(columns=[col])
    return df


def run_audit(input_path: str, output_path: str = None,
              n_splits: int = DEFAULT_SPLITS) -> Dict:
    """完整审计：原始统计 + DSR + PBO → dict（可存 JSON）"""
    df = load_trials_csv(input_path)
    if df.empty:
        raise ValueError('试验矩阵为空')
    R = df.values.astype(float)
    # 自适应单位：中位绝对日收益 < 0.005 视为小数，否则视为百分号 → 转小数
    med = np.nanmedian(np.abs(R))
    if med >= 0.005:
        R = R / 100.0
        unit = 'percent(已自动转为小数)'
    else:
        unit = 'decimal'

    names = list(df.columns)
    srs = [annualized_sharpe(R[:, j]) for j in range(R.shape[1])]
    best_idx = int(np.nanargmax(srs))

    report = {
        'input': os.path.abspath(input_path),
        'n_trials': len(names),
        'n_days': int(R.shape[0]),
        'return_unit': unit,
        'trial_sharpes': {names[j]: round(srs[j], 3) for j in range(len(names))},
        'best_trial': names[best_idx],
        'best_trial_sharpe': round(srs[best_idx], 3),
    }

    dsr_res = deflated_sharpe_ratio(R[:, best_idx], srs)
    report['dsr'] = dsr_res

    pbo_res = pbo_cscv(R, n_splits=n_splits)
    report['pbo'] = pbo_res

    # 总体结论
    dsr_ok = (dsr_res.get('dsr') is not None and dsr_res['dsr'] >= 0.95)
    pbo_ok = (pbo_res.get('pbo') is not None and pbo_res['pbo'] <= 0.25)
    if dsr_res.get('dsr') is None and pbo_res.get('pbo') is None:
        verdict = 'INSUFFICIENT_DATA：试验/样本不足，无法校正'
    elif dsr_ok and pbo_ok:
        verdict = 'PASS：多重检验校正通过'
    elif not dsr_ok and pbo_res.get('pbo') is not None and pbo_res['pbo'] > 0.5:
        verdict = 'FAIL：DSR 未过且 PBO 过半，赢家大概率是运气'
    else:
        verdict = 'GREY_ZONE：部分通过，需结合经济逻辑与样本外确认'
    report['verdict'] = verdict

    if output_path:
        os.makedirs(os.path.dirname(os.path.abspath(output_path)), exist_ok=True)
        with open(output_path, 'w', encoding='utf-8') as f:
            json.dump(report, f, ensure_ascii=False, indent=2)
        logger.info(f"审计报告已写入 {output_path}")
    return report


def main():
    logging.basicConfig(level=logging.INFO, format='%(levelname)s %(message)s')
    ap = argparse.ArgumentParser(description='DSR/PBO 多重检验校正审计')
    ap.add_argument('--input', required=True, help='试验矩阵 CSV（每列一个试验，每行一日收益）')
    ap.add_argument('--output', default=None, help='输出 JSON 报告路径')
    ap.add_argument('--splits', type=int, default=DEFAULT_SPLITS, help='PBO 时间块数（偶数）')
    args = ap.parse_args()
    report = run_audit(args.input, args.output, n_splits=args.splits)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == '__main__':
    sys.exit(main())
