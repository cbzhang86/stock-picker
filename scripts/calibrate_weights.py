"""
权重校准器 — 让权重跟随真实样本外 IC，而不是研报中位数

设计背景
--------
config.yml 里的权重起点来自"A 股量化研报中位数"（资金流+动量共 50%），
但样本外 IC 验证（core/oos_validator.py）显示这套权重与实测方向严重背离：

  因子            当前权重   跨口径稳健 OOS IC
  capital_flow     0.35     弱正（方向一致，幅度未达显著）
  momentum         0.25     强反向（三种口径全负）
  technical        0.15     负向 / 不稳定
  volume_price     0.10     稳定反向（9 个验证折全负）
  hot_theme        0.10     正向（hold1d t=2.17、overnight t=2.70）
  dragon_tiger     0.05     噪声

即：权重第二高的因子（momentum 0.25）是实测最反向的因子。

核心防过拟合设计
----------------
1. **跨口径取悲观值**：同一个因子在 hold1d / intraday / overnight 三种收益
   口径下的 IC 可能符号相反（这是实测发现的真实情况）。校准取三者中的
   **最小值**作为该因子的证据强度 —— 只有在最不利的口径下仍然为正的因子，
   才配拿到权重。这比只挑一个好看的口径稳健得多。
2. **负 IC 归零**：反向因子不给权重（不做"取反"，因为反向的稳定性未经
   独立样本确认，贸然取反风险更大；建议的做法是先降权观察）。
3. **坍缩保护**：单因子上限 0.50（比 feedback/optimizer.py 的 0.8 更严）。
4. **三段式**：默认只产出建议报告，必须显式 --apply 才写入文件。

用法：
  # 只出建议（默认）
  python scripts/calibrate_weights.py

  # 用指定口径组合
  python scripts/calibrate_weights.py --conventions hold1d overnight

  # 审批后写入 v1.json
  python scripts/calibrate_weights.py --apply
"""

import argparse
import glob
import json
import logging
import os
import sys
from datetime import datetime
from typing import Dict, List, Optional, Tuple

if sys.platform == 'win32':
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    sys.stderr.reconfigure(encoding='utf-8', errors='replace')

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..'))

logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s',
                    datefmt='%H:%M:%S')
logger = logging.getLogger(__name__)

# 单因子权重上限（坍缩保护）
MAX_SINGLE_WEIGHT = 0.50
# 单因子权重下限（保留少量探索权重，避免因子被一次性判死后再也测不出翻转）
MIN_SINGLE_WEIGHT = 0.02
# 收缩系数 λ 的取值范围：证据再弱也至少采纳 20% 的 IC 意见，
# 证据再强也不完全抛弃先验（不超过 80%），防止单期 IC 噪声主导配置
LAMBDA_MIN = 0.2
LAMBDA_MAX = 0.8
# |t| 达到该值即视为"统计可靠"（用于计算可靠性系数）
T_RELIABLE = 2.0
# 样本外交易日数达到该值才认为 IC 估计稳定（15 天的 IC 不足以支撑激进调权）
DAYS_RELIABLE = 60
# 主口径 IC 为负的因子，其先验权重额外打折（保留少量"探索权重"以便日后检测翻转，
# 但不让已被证伪的方向继续占据大额配置）。
# 折扣按 IC 的负值大小分级：IC 越负折得越狠，避免"显著反向因子"与
# "接近零的噪声因子"拿到同样的保留权重。
NEG_PENALTY = 0.5
NEG_PENALTY_SCALE = 0.05   # IC 每负 0.05，惩罚指数 +1
# IC 幂次：>1 放大强弱差距，=1 线性，<1 更平均
IC_GAMMA = 1.0
# 主口径：决定因子强度。hold1d = 尾盘买入持有 1 天，最贴近尾盘策略原意
PRIMARY_CONVENTION = 'hold1d'

# P2-L（2026-09-05 审查报告）——校准闸门：
# 1) 覆盖率闸门：因子历史覆盖率（0-1）低于该值时，建议权重硬上限 COVERAGE_CAP。
#    窄样本 IC 不配大权重——防止"待积累的估值/事件新因子"靠几十行数据拿到大额配置。
COVERAGE_GATE = 0.60
COVERAGE_CAP = 0.05
# 2) λ 解锁护栏：主口径测试期整体样本不足（最大 n_days < DAYS_RELIABLE）时，
#    即使个别因子 t 值高，λ 上限也收紧到 0.5（保留至少一半等权先验）。
LAMBDA_CAP_LOW_SAMPLE = 0.5

DEFAULT_CONVENTIONS = ['hold1d', 'intraday', 'overnight']


def load_latest_ic(reports_dir: str, convention: str) -> Optional[dict]:
    """读取指定口径最近一次的 OOS IC 结果"""
    pattern = os.path.join(reports_dir, f'oos_ic_*_{convention}.json')
    files = sorted(glob.glob(pattern), key=os.path.getmtime, reverse=True)
    if not files:
        return None
    with open(files[0], 'r', encoding='utf-8') as f:
        data = json.load(f)
    data['_source_file'] = os.path.basename(files[0])
    return data


def extract_factor_evidence(ic_data: dict) -> Dict[str, float]:
    """
    从单口径结果中提取每个因子的 OOS IC 证据

    优先级：walk-forward 各折均值 > 测试集 IC
    （walk-forward 用滚动多折，比单次切分稳健）
    """
    evidence = {}
    if not ic_data:
        return evidence

    wf = ic_data.get('walk_forward', {}).get('factors', {})
    result = ic_data.get('result', {}).get('factors', {})

    for factor in set(list(wf.keys()) + list(result.keys())):
        w = wf.get(factor, {})
        r = result.get(factor, {})
        val = w.get('mean_oos_ic')
        if val is None:
            val = r.get('oos_ic')
        if val is None:
            val = r.get('test', {}).get('ic')
        if val is not None:
            evidence[factor] = float(val)
    return evidence


def extract_factor_stats(ic_data: dict) -> Dict[str, Dict]:
    """提取测试集的 t 值、ICIR、覆盖度等可靠性指标"""
    stats = {}
    for factor, d in ic_data.get('result', {}).get('factors', {}).items():
        stats[factor] = {
            't_stat': d.get('test', {}).get('t_stat'),
            'icir': d.get('test', {}).get('icir'),
            'n_days': d.get('test', {}).get('n_days'),
            'coverage': d.get('coverage'),
        }
    return stats


def consensus_evidence(all_evidence: Dict[str, Dict[str, float]],
                       all_stats: Dict[str, Dict] = None,
                       primary: str = PRIMARY_CONVENTION) -> Dict[str, Dict]:
    """
    综合多口径证据

    为什么不用"跨口径取最小值"定强度
    --------------------------------
    早期版本直接取 min，结果把每个因子都拖到它最不适用的那个口径的水平上：
    hot_theme 在 hold1d(+0.065)/overnight(+0.102) 都显著为正，却只因为
    intraday 是 +0.002 就被降到与 capital_flow 同级 —— 而 intraday 口径
    （次日开盘才买）本来就不该反映"题材热度"这种隔夜发酵型信号。

    改后的规则
    ----------
    - **强度**由主口径（默认 hold1d，最贴近尾盘买入）决定
    - **折扣（一致性）**：⚠️ 三口径**并非独立测试** —— 实测
      `hold1d ≈ overnight + intraday`（同一笔买卖的收益分解，见
      core/oos_validator.py::_compute_forward_returns）。因此"跨口径为正比例"
      （pos_ratio）的"3/3 一致"并不比 1 票强多少，它**仅作一致性提示，
      不作为独立证据强度**。具体处理见下方 `consistency_factor`：
      全部口径同号为正（且 ≥2 个口径有数据）→ 不打折（1.0）；否则统一打
      0.7 折（一档固定折扣，而非连续乘法）。
    - **折扣 2（可靠性）**：|t|/2 截断到 [0,1]。样本外只有 15 天时 t 值普遍
      很小，这会自动压低 IC 权重、保留更多先验权重
    """
    factors = set()
    for ev in all_evidence.values():
        factors.update(ev.keys())

    out = {}
    for f in factors:
        vals = {conv: ev.get(f) for conv, ev in all_evidence.items()}
        present = {k: v for k, v in vals.items() if v is not None}
        if not present:
            continue

        ic_main = present.get(primary)
        if ic_main is None:
            # 主口径缺失时退化为各口径均值
            ic_main = sum(present.values()) / len(present)

        pos_ratio = sum(1 for v in present.values() if v > 0) / len(present)

        st = (all_stats or {}).get(primary, {}).get(f, {})
        t_stat = st.get('t_stat')
        n_days = st.get('n_days') or 0
        # 可靠性 = 显著性 × 样本充分性。15 天的测试期即便 t=2 也不能算铁证，
        # 样本量折扣会自动压低 λ，让权重更保守；随着样本积累会自动放开。
        reliability = (min(abs(t_stat) / T_RELIABLE, 1.0) if t_stat is not None else 0.0) \
            * min(n_days / DAYS_RELIABLE, 1.0)

        # 一致性档位（非连续乘法）：三口径非独立（hold1d ≈ overnight + intraday），
        # 故 pos_ratio 只作展示、不再乘进 score。全部口径同号为正且 ≥2 口径有数据
        # → 不打折（1.0）；否则统一打 0.7 折（一档固定折扣）。
        all_positive = (len(present) >= 2) and all(v > 0 for v in present.values())
        consistency_factor = 1.0 if all_positive else 0.7

        # 真正有信息量的维度：overnight 与 intraday 符号相反（如 hot_theme
        # 隔夜 +0.0848 / 日内 -0.0149）。pos_ratio 的同号一致掩盖不了这个分歧。
        ov = present.get('overnight')
        iv = present.get('intraday')
        overnight_vs_intraday_divergence = (
            ov is not None and iv is not None and (ov * iv) < 0
        )

        score = ic_main * consistency_factor * reliability if ic_main > 0 else 0.0

        out[f] = {
            'per_convention': vals,
            'ic_main': round(ic_main, 4),
            'pos_ratio': round(pos_ratio, 3),
            'consistency_factor': consistency_factor,
            'overnight_vs_intraday_divergence': overnight_vs_intraday_divergence,
            't_stat': t_stat,
            'n_days': n_days,
            'reliability': round(reliability, 3),
            'score': round(score, 6),
            'coverage': st.get('coverage'),
            'mean_ic': round(sum(present.values()) / len(present), 4),
            'n_conventions': len(present),
            'sign_stable': (min(present.values()) > 0) or (max(present.values()) < 0),
        }
    return out


def apply_correlation_discount(consensus: Dict[str, Dict],
                               primary_ic_data: Optional[dict],
                               threshold: float = 0.5) -> int:
    """P2-L 可选：因子间相关性折扣。

    高相关因子抱团等于隐性放大同一信号（权重表上分散、实际是同一个 bet）。
    对平均 |corr| > threshold 的因子，按 (1 - avg|corr|) 折扣其 score。

    数据来源：主口径 OOS 报告 JSON 的 factor_corr 矩阵（OOSValidator.factor_corr）。
    报告未存矩阵时静默跳过（返回 0），不阻断校准。返回被调整的因子数。

    折扣后归一化不变式（INVARIANT）
    --------------------------------
    折扣只应改变因子间的**相对比例**，而不能改变证据总量的守恒中间态。
    旧实现直接 `score *= (1 - avg|corr|)` 但不重新归一化，导致折扣后所有
    score 之和变小，破坏了权重守恒的中间语义（后续 calibrate 会再归一化，
    折扣量级被悄悄抵消掉一部分，不可解释）。

    本实现：先逐因子记下折扣 `(1 - avg|corr|)`（未超阈值的因子折扣=1.0），再整体
    乘一个缩放因子 `scale = Σ_before / Σ(score*discount)`，使折扣后总分严格等于
    折扣前总分（容差 1e-9）。即：
        score_i_new = score_i * discount_i * (Σ_before / Σ(score*discount))
    从而 Σ(score_new) == Σ_before（不变量），相对比例被正确保留。
    """
    if not primary_ic_data:
        return 0
    corr = primary_ic_data.get('factor_corr') \
        or primary_ic_data.get('result', {}).get('factor_corr')
    if not isinstance(corr, dict) or not corr:
        logger.info("主口径报告未含 factor_corr 矩阵，跳过相关性折扣")
        return 0

    # 折扣前总分（不变量基准）
    total_before = sum(d.get('score', 0.0) for d in consensus.values())
    if total_before <= 0:
        # 无正证据可折扣，直接返回（避免后续除零）
        return 0

    # 第一遍：逐因子计算折扣，不直接改写 score
    discount: Dict[str, float] = {}
    n_adj = 0
    for f in list(consensus.keys()):
        row = corr.get(f, {})
        if not isinstance(row, dict):
            discount[f] = 1.0
            continue
        pairs = []
        for k, v in row.items():
            if k != f and k in consensus and v is not None:
                try:
                    pairs.append(abs(float(v)))
                except (TypeError, ValueError):
                    pass
        if not pairs:
            discount[f] = 1.0
            continue
        avg_abs = sum(pairs) / len(pairs)
        if avg_abs > threshold:
            discount[f] = 1.0 - avg_abs
            n_adj += 1
            logger.info(f"相关性折扣: {f} 平均|corr|={avg_abs:.2f} → ×{discount[f]:.2f}")
        else:
            discount[f] = 1.0

    # 折扣后"原始"总分（未归一化）
    total_after_raw = sum(d.get('score', 0.0) * discount[f] for f, d in consensus.items())
    # 缩放因子：把折扣后总分拉回折扣前总分，保持 Σscore 守恒（中间量语义正确）
    scale = total_before / total_after_raw if total_after_raw > 0 else 1.0

    # 第二遍：应用折扣 + 归一化缩放，并记录 corr_discount
    for f, d in consensus.items():
        dc = discount[f]
        d['score'] = d['score'] * dc * scale
        if dc < 1.0:
            d['corr_discount'] = round(dc, 3)

    # 不变量自检（仅调试/防御性）：折扣前后 Σscore 应相等（容差 1e-9）
    total_after = sum(d.get('score', 0.0) for d in consensus.values())
    if abs(total_after - total_before) > 1e-9:
        logger.warning(f"相关性折扣归一化不变量偏离: "
                       f"Σ_before={total_before:.6f} Σ_after={total_after:.6f}")
    return n_adj


def _enforce_weight_bounds(weights: Dict[str, float],
                           max_single: float = MAX_SINGLE_WEIGHT,
                           min_single: float = MIN_SINGLE_WEIGHT,
                           ceilings: Optional[Dict[str, float]] = None,
                           tol: float = 1e-9,
                           max_iter: int = 64) -> Tuple[Dict[str, float], bool]:
    """把权重压进 [min_single, 上限] 且保持 Σw == 1（保和再分配）。

    2026-09-17 修复的缺陷
    ---------------------
    `calibrate` 第 5 步（覆盖率闸门）把被压因子的超额**按比例回流给所有未命中
    闸门的因子**，但**没有再次遵守 max_single**。实测：收缩后 hot_theme 0.4052 /
    capital_flow 0.3770 → capital_flow 因覆盖率 9.4% 被压到 5%，回流额度 0.3270
    几乎全额加到 hot_theme → **0.6179，突破 MAX_SINGLE_WEIGHT = 0.50**
    （"坍缩保护"静默失效，若 --apply 就会把越界权重写进 v1.json）。

    算法：有界注水（water-filling），支持**逐因子上限**
    ------------------------------------------------
      `ceilings` 可为个别因子指定比 max_single 更严的上限（例如覆盖率闸门命中的
      因子应为 COVERAGE_CAP=5%）。**若不指定逐因子上限，注水会把闸门刚压下去的
      额度又还回去**（实测 capital_flow 被推到 7.5%，突破闸门 5% 上限）——
      这正是需要 ceilings 的原因。
      1. 把所有因子夹到 [min_single, ceilings.get(f, max_single)]；
      2. 算残差 residual = 1 - Σw；
      3. residual > 0 → 只分配给未触各自上限的因子，按剩余上行空间比例；
         residual < 0 → 只从高于下限的因子回收，按剩余下行空间比例；
      4. 重复至收敛或 max_iter。

    不可行情形（如各因子上限之和 < 1）返回最接近的可行点并置 feasible=False，
    由调用方告警——**绝不抛异常、绝不破坏 Σw = 1**。
    """
    w = {f: float(v) for f, v in weights.items()}
    if not w:
        return {}, True
    cap = {f: float((ceilings or {}).get(f, max_single)) for f in w}

    for _ in range(max_iter):
        for f in w:
            if w[f] > cap[f]:
                w[f] = cap[f]
            elif w[f] < min_single:
                w[f] = min_single
        residual = 1.0 - sum(w.values())
        if abs(residual) <= tol:
            break
        if residual > 0:
            room = {f: cap[f] - v for f, v in w.items() if cap[f] - v > tol}
            total_room = sum(room.values())
            if total_room <= tol:
                break                      # 上行空间耗尽：不可行
            step = min(residual, total_room)
            for f, r in room.items():
                w[f] += step * (r / total_room)
        else:
            slack = {f: v - min_single for f, v in w.items() if v - min_single > tol}
            total_slack = sum(slack.values())
            if total_slack <= tol:
                break                      # 下行空间耗尽：不可行
            step = min(-residual, total_slack)
            for f, s in slack.items():
                w[f] -= step * (s / total_slack)

    s = sum(w.values())
    feasible = abs(s - 1.0) <= 1e-6 and all(
        min_single - 1e-9 <= v <= cap[f] + 1e-9 for f, v in w.items())
    if not feasible and s > 0:
        w = {f: v / s for f, v in w.items()}   # 兜底保 Σ=1（可能仍越界，交由调用方告警）
    return w, feasible


def calibrate(consensus: Dict[str, Dict], current_weights: Dict[str, float],
              gamma: float = IC_GAMMA,
              max_single: float = MAX_SINGLE_WEIGHT,
              min_single: float = MIN_SINGLE_WEIGHT) -> (Dict[str, float], Dict):
    """
    由因子证据生成权重（带先验收缩）

    三步：
      1. score = max(0, IC_主口径) × 一致性折扣 × 可靠性折扣  → 归一化得 w_ic
      2. λ（对 IC 意见的信任度）= 全体因子中最高的可靠性，截断到 [0.2, 0.8]
      3. w = λ·w_ic + (1-λ)·w_prior，主口径 IC<=0 的因子再乘 NEG_PENALTY，
         最后归一化 + 单因子上限裁剪

    **先验用等权而不是当前权重**：当前权重来自研报中位数，已被本系统实测
    推翻（权重第二高的 momentum 是实测最反向的因子）。用一个已被证伪的配置
    做锚点没有依据；等权是无信息时的最大熵选择。

    **为什么必须收缩**：样本外只有 15 个交易日，IC 估计本身噪声很大。完全按
    IC 分配会让单期噪声主导配置；完全不动则等于无视证据。λ 会随样本积累
    自动提高。

    返回：(权重, 过程明细)
    """
    universe = [f for f in current_weights if f in consensus]
    if not universe:
        return {}, {}

    # 1. IC 侧权重
    scores = {f: max(0.0, consensus[f]['score']) ** gamma for f in universe}
    total = sum(scores.values())

    detail = {'scores': scores, 'total_score': total}
    if total <= 0:
        logger.warning("所有因子主口径 IC 均 <= 0，无法生成有效权重"
                       "（建议先检查因子定义与收益口径是否匹配）")
        return {}, detail

    w_ic = {f: s / total for f, s in scores.items()}

    # 2. 信任度 λ
    reliability = max((consensus[f]['reliability'] for f in universe), default=0.0)
    lam = max(LAMBDA_MIN, min(reliability, LAMBDA_MAX))
    # P2-L：λ 解锁护栏——整体样本不足时不许激进调权。
    # 用全体因子的最大 n_days 判定"证据池成熟度"：只要没有任何因子积累到
    # 60 天测试样本，λ 上限收紧到 0.5（至少一半权重留在等权先验）。
    n_days_max = max((consensus[f].get('n_days') or 0 for f in universe), default=0)
    low_sample_note = None
    if n_days_max < DAYS_RELIABLE:
        lam = min(lam, LAMBDA_CAP_LOW_SAMPLE)
        low_sample_note = (f"样本不足(最大 n_days={n_days_max} <{DAYS_RELIABLE})，"
                           f"λ 上限收紧至 {LAMBDA_CAP_LOW_SAMPLE}")

    # 3. 向等权先验收缩 + 负 IC 惩罚
    n = len(universe)
    w_prior = {f: 1.0 / n for f in universe}
    merged, penalties = {}, {}
    for f in universe:
        w = lam * w_ic[f] + (1 - lam) * w_prior[f]
        ic_main = consensus[f]['ic_main']
        if ic_main <= 0:
            pen = NEG_PENALTY ** (1 + abs(ic_main) / NEG_PENALTY_SCALE)
            w *= pen
            penalties[f] = round(pen, 4)
        merged[f] = w

    # 4. 归一化 → 上限裁剪 → 超额按比例重分配给未触顶因子（单次，避免迭代震荡）
    s = sum(merged.values())
    merged = {f: w / s for f, w in merged.items()}

    over = {f: w for f, w in merged.items() if w > max_single}
    if over:
        excess = sum(w - max_single for w in over.values())
        for f in over:
            merged[f] = max_single
        rest = {f: w for f, w in merged.items() if f not in over}
        rest_total = sum(rest.values())
        if rest_total > 0:
            for f in rest:
                merged[f] += excess * (rest[f] / rest_total)

    # 5. 覆盖率闸门（P2-L）：覆盖率 <0.6 的因子权重硬上限 5%，超额回流给
    #    覆盖达标的因子。与上限裁剪叠加执行（先 max_single 再 gate）。
    gated = {f for f in merged
             if (consensus[f].get('coverage') is not None
                 and consensus[f]['coverage'] < COVERAGE_GATE)}
    if gated:
        excess = 0.0
        for f in gated:
            if merged[f] > COVERAGE_CAP:
                excess += merged[f] - COVERAGE_CAP
                merged[f] = COVERAGE_CAP
        if excess > 0:
            rest = {f: w for f, w in merged.items() if f not in gated}
            rest_total = sum(rest.values())
            if rest_total > 0:
                for f in rest:
                    merged[f] += excess * (rest[f] / rest_total)
        logger.info(f"覆盖率闸门生效: {sorted(gated)} 权重上限 {COVERAGE_CAP:.0%}"
                    f"（覆盖率 <{COVERAGE_GATE:.0%}）")

    # 6. 权重下限（MIN_SINGLE_WEIGHT）：保留少量探索权重，避免因子被一次性判死
    #    后再也测不出翻转。被压到 min_single 以下的因子抬升到 min_single，所需
    #    额度从"高于下限且未触上限"的因子按其可让出空间（headroom）按比例回收。
    #    整体为保和再分配（只做 +deficit / -deficit），故 Σw 严格保持 1（容差 1e-6），
    #    且不违反 MAX_SINGLE_WEIGHT。单次迭代即收敛，无需循环。
    #    极端情况（因子数 × min_single > 1，或可回流额度不足）：跳过下限并告警，
    #    绝不抛异常、绝不破坏 Σw=1。
    below = {f: w for f, w in merged.items() if w < min_single}
    if below:
        deficit = sum(min_single - w for w in below.values())
        donors = {f: w for f, w in merged.items()
                  if w > min_single and w <= max_single}
        available = sum(w - min_single for w in donors.values())
        if len(merged) * min_single > 1.0 + 1e-9 or available < deficit - 1e-9:
            logger.warning(
                f"权重下限不可行（因子数×min_single="
                f"{len(merged) * min_single:.3f} > 1 或 可回流额度 {available:.4f} "
                f"< 缺口 {deficit:.4f}），跳过下限（min_single={min_single}）")
            detail['min_single_applied'] = False
            detail['lifted_factors'] = []
        else:
            for f in below:
                merged[f] = min_single
            headroom = {f: w - min_single for f, w in donors.items()}
            total_headroom = sum(headroom.values())
            if total_headroom > 0:
                for f in donors:
                    merged[f] -= deficit * (headroom[f] / total_headroom)
            detail['min_single_applied'] = True
            detail['lifted_factors'] = sorted(below.keys())
    else:
        detail['min_single_applied'] = False
        detail['lifted_factors'] = []

    # 7. 边界强制（2026-09-17 修复）：步骤 4/5/6 各自做局部再分配，但**先后顺序
    #    会让后一步推翻前一步的约束** —— 实测覆盖率闸门（步骤 5）的回流把
    #    hot_theme 从 0.4052 推到 0.6179，突破 MAX_SINGLE_WEIGHT=0.50。
    #    这里统一做一次有界注水，保证 [min_single, max_single] 与 Σw=1 同时成立。
    #    正常情况下该步近乎无操作（不改变已满足约束的结果），只在越界时纠偏。
    before_enforce = dict(merged)
    # 逐因子上限：命中覆盖率闸门的因子其上限是 COVERAGE_CAP（5%），
    # 其余为 MAX_SINGLE_WEIGHT。**必须把闸门上限传给注水**，否则注水会把
    # 闸门刚压下去的额度按"还有空间"又还给该因子（实测 capital_flow 被推回 7.5%）。
    ceilings = {f: (COVERAGE_CAP if f in gated else max_single) for f in merged}
    merged, bounds_feasible = _enforce_weight_bounds(merged, max_single, min_single,
                                                     ceilings=ceilings)
    detail['bounds_enforced'] = bounds_feasible
    detail['bounds_adjusted'] = sorted(
        f for f in merged if abs(merged[f] - before_enforce.get(f, 0.0)) > 1e-9)
    if not bounds_feasible:
        logger.warning(
            f"权重边界不可行（min_single={min_single}, max_single={max_single}, "
            f"因子数={len(merged)}）：返回最接近可行点，请人工复核")
    elif detail['bounds_adjusted']:
        logger.info(f"权重边界纠偏: {detail['bounds_adjusted']} "
                    f"（详见 bounds_enforced / bounds_adjusted）")

    detail.update({
        'w_ic': w_ic, 'lambda': round(lam, 3),
        'w_prior': w_prior, 'reliability': round(reliability, 3),
        'neg_penalty': NEG_PENALTY, 'max_single': max_single,
        'min_single': min_single,
        'penalties': penalties,
        'low_sample_note': low_sample_note,
        'coverage_gated': sorted(gated) if gated else [],
    })

    return {f: round(w, 4) for f, w in sorted(merged.items(), key=lambda x: -x[1])}, detail


def maximize_icir_weights(ic_matrix, cap: float = 0.30,
                          min_obs: int = 30, min_improvement: float = 0.05):
    """最大化 ICIR 权重（2026-09-07 P1 论证后实施，华泰多因子系列之十）。

    ic_matrix: DataFrame（行=日期，列=因子，值=日频 RankIC）
    规则（子代理论证结论，docs/因子与权重评估_对照机构研报_2026-09-07.md）：
      - 准入：日频 IC 有效观测 >= min_obs（默认 30）的因子才进入优化；
        未准入因子按等权先验获得锚定份额（anchor_ratio = 未准入数/总因子数）；
      - 协方差矩阵用 Ledoit-Wolf 对角压缩（日频 IC 观测少，样本协方差病态）；
      - 约束：w>=0、Σw=1、单因子 <= cap（默认 0.30，化解集中度）；
      - 回退：优化后组合 ICIR 相对等权提升 < min_improvement → 回退全等权；
        未准入因子占比 > 0.4 → 直接回退全等权。

    返回 (weights_dict, note)；无法求解时 (None, reason)。
    """
    import numpy as np
    import pandas as pd

    ic = ic_matrix.copy()
    all_factors = list(ic.columns)
    if len(all_factors) < 2:
        return None, '少于 2 个因子，无需 ICIR 优化'

    qualified = [c for c in all_factors if ic[c].notna().sum() >= min_obs]
    unqualified = [c for c in all_factors if c not in qualified]
    anchor_ratio = len(unqualified) / len(all_factors)
    if anchor_ratio > 0.4:
        ew = {c: round(1.0 / len(all_factors), 4) for c in all_factors}
        return ew, (f'准入因子占比不足（合格 {len(qualified)}/{len(all_factors)}，'
                    f'要求 n_obs>={min_obs}）→ 回退全等权')
    if not qualified:
        return None, '无合格因子'

    def _icir(w, sub):
        port = sub.values @ w
        return float(port.mean() / (port.std() + 1e-12))

    sub = ic[qualified].copy()
    for c in qualified:                      # 少量缺失按当日均值填补（仅用于协方差估计）
        sub[c] = sub[c].fillna(sub[c].mean())
    mu = sub.mean().values

    if len(qualified) == 1:
        w_q = np.array([1.0])
    else:
        from sklearn.covariance import LedoitWolf
        cov = LedoitWolf().fit(sub.values).covariance_
        from scipy.optimize import minimize
        n = len(mu)

        def neg_icir(w):
            return -(w @ mu) / np.sqrt(max(w @ cov @ w, 1e-12))

        constraints = [{'type': 'eq', 'fun': lambda w: float(w.sum() - 1.0)}]
        bounds = [(0.0, cap)] * n
        res = minimize(neg_icir, np.full(n, 1.0 / n), method='SLSQP',
                       bounds=bounds, constraints=constraints,
                       options={'maxiter': 200})
        w_q = res.x if res.success else np.full(n, 1.0 / n)
        w_q = np.clip(w_q, 0.0, cap)
        w_q = w_q / w_q.sum()

    ew_q = np.full(len(qualified), 1.0 / len(qualified))
    icir_opt, icir_ew = _icir(w_q, sub), _icir(ew_q, sub)
    if icir_opt < icir_ew * (1 + min_improvement):
        w_q = ew_q
        note = (f'ICIR 提升不足（等权 {icir_ew:.3f} → 优化 {icir_opt:.3f}，'
                f'阈值 +{min_improvement:.0%}）→ 合格因子回退等权')
    else:
        note = f'组合 ICIR {icir_ew:.3f} → {icir_opt:.3f}'

    w_full = {}
    for c, x in zip(qualified, w_q):
        w_full[c] = x * (1 - anchor_ratio)
    for c in unqualified:
        w_full[c] = anchor_ratio / max(len(unqualified), 1)
    weights = {c: round(v, 4) for c, v in w_full.items()}
    return weights, note + f'（未准入锚定 {anchor_ratio:.0%}）'


def build_ic_matrix(ic_data: dict) -> 'pd.DataFrame | None':
    """从 OOS 报告提取逐日 IC 矩阵（依赖 T-A 新增的 factors[f].daily_ics 字段）。"""
    import pandas as pd
    factors = (ic_data or {}).get('result', {}).get('factors', {})
    series_map = {}
    for f, info in factors.items():
        d = info.get('daily_ics')
        if d:
            series_map[f] = pd.Series(d)
    if len(series_map) < 2:
        return None
    return pd.DataFrame(series_map)


def build_report(consensus, current, proposed, sources, detail, primary) -> str:
    lines = []
    lines.append('# 权重校准建议（基于样本外 IC）')
    lines.append('')
    lines.append(f"- 生成时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    lines.append(f"- 证据来源：{', '.join(sources)}")
    lines.append(f"- 主口径：**{primary}**（定强度）；其余口径用于一致性折扣")
    lines.append(f"- 单因子上限：{MAX_SINGLE_WEIGHT:.0%}（坍缩保护）")
    lines.append(f"- 先验：等权（当前权重源自研报中位数，已被实测推翻，不作为锚点）")
    if detail:
        lines.append(f"- 对 IC 意见的信任度 λ = **{detail.get('lambda')}**"
                     f"（最高可靠性 {detail.get('reliability')}，"
                     f"剩余 {1 - (detail.get('lambda') or 0):.0%} 保留等权先验）")
    lines.append('')
    lines.append('## 因子证据')
    lines.append('')
    lines.append('| 因子 | 主口径IC | 各口径 IC | 口径为正比例 | 一致性 | 隔夜/日内分歧 | t值 | 可靠性 | 得分 | 负向惩罚 |')
    lines.append('|---|---|---|---|---|---|---|---|---|---|')
    penalties = (detail or {}).get('penalties', {})
    for f, d in sorted(consensus.items(), key=lambda x: -x[1]['score']):
        per = ', '.join(f"{k}={v:+.4f}" for k, v in d['per_convention'].items()
                        if v is not None)
        t = f"{d['t_stat']:+.2f}" if d['t_stat'] is not None else '—'
        pen = penalties.get(f)
        pen_s = f"×{pen:.3f}" if pen is not None else '—'
        dive = '⚠ True' if d.get('overnight_vs_intraday_divergence') else '—'
        lines.append(f"| {f} | {d['ic_main']:+.4f} | {per} | "
                     f"{d['pos_ratio']:.0%} | {d.get('consistency_factor')} | "
                     f"{dive} | {t} | {d['reliability']:.2f} | "
                     f"{d['score']:.6f} | {pen_s} |")
    lines.append('')
    lines.append('## 权重变化')
    lines.append('')
    lines.append('| 因子 | 当前权重 | 纯IC权重 | 建议权重 | 变化 |')
    lines.append('|---|---|---|---|---|')
    w_ic = (detail or {}).get('w_ic', {})
    for f in sorted(set(list(current.keys()) + list(proposed.keys()))):
        c = current.get(f, 0.0)
        p = proposed.get(f, 0.0)
        w = w_ic.get(f)
        w_s = f"{w:.1%}" if w is not None else '—'
        delta = p - c
        mark = ' ⚠️' if abs(delta) >= 0.10 else ''
        lines.append(f"| {f} | {c:.1%} | {w_s} | {p:.1%} | {delta:+.1%}{mark} |")
    lines.append('')
    lines.append('## 需要人工复核的变化')
    lines.append('')
    flags = []
    for f, p in proposed.items():
        c = current.get(f, 0.0)
        ic_main = consensus.get(f, {}).get('ic_main', 0)
        if p > c and ic_main <= 0:
            flags.append(f"- ⚠️ **{f}** 权重上升 {p-c:+.1%}，但主口径 IC = {ic_main:+.4f} "
                         f"（非正）。这是单因子上限截断后额度回流的副作用，"
                         f"若不接受可手工下调。")
        if consensus.get(f, {}).get('coverage') is not None \
                and consensus[f]['coverage'] < 0.2 and p > 0.1:
            flags.append(f"- ⚠️ **{f}** 拿到 {p:.1%} 权重，但历史覆盖率仅 "
                         f"{consensus[f]['coverage']*100:.0f}%，IC 证据建立在窄样本上。")
        if consensus.get(f, {}).get('t_stat') is not None \
                and abs(consensus[f]['t_stat']) < 1.0 and p > 0.1:
            flags.append(f"- ⚠️ **{f}** 拿到 {p:.1%} 权重，但 |t| = "
                         f"{abs(consensus[f]['t_stat']):.2f} < 1，统计上不显著。")
    lines.extend(flags if flags else ['- 无'])
    lines.append('')
    lines.append('> 本脚本默认只产出建议。`--apply` 才会写入 data/weights/v1.json，')
    lines.append('> 写入前会先备份当前权重到 v1.backup_<时间戳>.json。')
    return '\n'.join(lines)


def main():
    parser = argparse.ArgumentParser(description='按样本外 IC 校准因子权重')
    parser.add_argument('--conventions', nargs='+', default=DEFAULT_CONVENTIONS,
                        help='参与稳健性投票的收益口径（默认三种全用）')
    parser.add_argument('--gamma', type=float, default=IC_GAMMA,
                        help='IC 幂次，>1 放大强弱差距（默认 1.0 线性）')
    parser.add_argument('--max-single', type=float, default=MAX_SINGLE_WEIGHT,
                        help='单因子权重上限（默认 0.50）')
    parser.add_argument('--primary', default=PRIMARY_CONVENTION,
                        help='主口径（决定因子强度，默认 hold1d）')
    parser.add_argument('--corr-discount', action='store_true',
                        help='启用因子相关性折扣（需主口径报告含 factor_corr 矩阵，P2-L）')
    parser.add_argument('--out', help='报告输出路径')
    parser.add_argument('--apply', action='store_true',
                        help='写入 data/weights/v1.json（默认只出建议）')
    parser.add_argument('--method', choices=['consensus', 'icir'], default='consensus',
                        help='校准方法：consensus（默认，历史行为）/ icir（最大化 ICIR，'
                             '需 OOS 报告含 daily_ics 逐日序列——用最新 oos_validator '
                             '重跑即含）；ICIR 模式内置单因子上限与准入门槛，样本不足自动回退等权')
    parser.add_argument('--icir-cap', type=float, default=0.30,
                        help='icir 模式单因子权重上限（默认 0.30）')
    args = parser.parse_args()

    project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    reports_dir = os.path.join(project_root, 'data', 'reports')

    # 1. 加载各口径 IC 结果
    all_evidence, all_stats, sources, raw_data = {}, {}, [], {}
    for conv in args.conventions:
        data = load_latest_ic(reports_dir, conv)
        if data is None:
            logger.warning(f"找不到 {conv} 口径的 IC 结果，请先跑："
                           f"python scripts/run_backtest.py --mode oos --ret {conv}")
            continue
        ev = extract_factor_evidence(data)
        if ev:
            all_evidence[conv] = ev
            all_stats[conv] = extract_factor_stats(data)
            raw_data[conv] = data
            sources.append(f"{conv}({data['_source_file']})")

    if not all_evidence:
        logger.error("没有任何可用的 IC 结果，退出")
        return

    if args.primary not in all_evidence:
        logger.error(f"主口径 {args.primary} 的 IC 结果缺失，"
                     f"可用口径：{list(all_evidence.keys())}")
        return

    # T-A（2026-09-07）：icir 模式独立分支——构建逐日 IC 矩阵 → 最大化 ICIR。
    # 当前历史报告不含 daily_ics，需用最新 oos_validator 重跑 OOS 后才有。
    if args.method == 'icir':
        icm = build_ic_matrix(raw_data.get(args.primary))
        if icm is None:
            logger.error("ICIR 模式需要 OOS 报告含逐日 IC 序列（daily_ics）。"
                         "请先重跑：python scripts/run_backtest.py --mode oos "
                         f"--ret {args.primary}（新版 oos_validator 会输出 daily_ics），再试")
            return
        proposed_icir, note = maximize_icir_weights(
            icm, cap=args.icir_cap, min_obs=30, min_improvement=0.05)
        if not proposed_icir:
            logger.error(f"ICIR 校准失败: {note}")
            return
        logger.info(f"ICIR 校准: {note}")
        proposed = {**current, **proposed_icir}
        report = (f"ICIR 校准建议（主口径 {args.primary}，单因子上限 {args.icir_cap}）\n"
                  f"{'=' * 45}\n{note}\n当前权重来源: v1.json\n\n"
                  + "\n".join(f"  {k}: {current.get(k, 0):.2%} → {v:.2%}"
                              for k, v in sorted(proposed_icir.items(),
                                                 key=lambda kv: -kv[1])))
        if args.out:
            os.makedirs(os.path.dirname(args.out) or '.', exist_ok=True)
            with open(args.out, 'w', encoding='utf-8') as f:
                f.write(report)
        else:
            print(report)
        if args.apply:
            weights_dir = os.path.join(project_root, 'data', 'weights')
            os.makedirs(weights_dir, exist_ok=True)
            v1_path = os.path.join(weights_dir, 'v1.json')
            existing = {}
            if os.path.exists(v1_path):
                with open(v1_path, 'r', encoding='utf-8') as f:
                    existing = json.load(f)
            merged = dict(existing.get('short', {}))
            merged.update(proposed_icir)
            existing['short'] = merged
            with open(v1_path, 'w', encoding='utf-8') as f:
                json.dump(existing, f, ensure_ascii=False, indent=2)
            logger.info(f"ICIR 权重已写入: {v1_path}")
        else:
            print("\n⚠️ 未写入。确认无误后加 --apply 生效。")
        return

    # 2. 综合多口径证据
    consensus = consensus_evidence(all_evidence, all_stats, primary=args.primary)

    # 2.5 P2-L 可选：因子相关性折扣（默认关闭，--corr-discount 启用）
    if args.corr_discount:
        apply_correlation_discount(consensus, raw_data.get(args.primary))

    # 3. 当前权重（2026-09-05 审查 P3-8：实际生效的是 v1.json —— ScoringModel
    #    加载优先级 v1.json > config.yml > 默认。此前这里读 config.yml，导致
    #    首次 --apply 之后所有报告的"当前权重"列失真。改为优先读 v1.json。）
    v1_path = os.path.join(project_root, 'data', 'weights', 'v1.json')
    current = {}
    if os.path.exists(v1_path):
        with open(v1_path, 'r', encoding='utf-8') as f:
            current = dict(json.load(f).get('short', {}))
        logger.info(f"当前权重来源: data/weights/v1.json")
    if not current:
        import yaml
        cfg_path = os.path.join(project_root, 'config.yml')
        with open(cfg_path, 'r', encoding='utf-8') as f:
            cfg = yaml.safe_load(f)
        current = dict(cfg.get('short_term', {}).get('weights', {}))
        logger.info("当前权重来源: config.yml（v1.json 不存在或为空）")

    # 4. 校准
    proposed, detail = calibrate(consensus, current, gamma=args.gamma,
                                 max_single=args.max_single)
    if not proposed:
        return

    report = build_report(consensus, current, proposed, sources, detail, args.primary)

    if args.out:
        os.makedirs(os.path.dirname(args.out) or '.', exist_ok=True)
        with open(args.out, 'w', encoding='utf-8') as f:
            f.write(report)
        logger.info(f"报告已保存: {args.out}")
    else:
        print(report)

    # 5. 写入（显式 --apply）
    if args.apply:
        weights_dir = os.path.join(project_root, 'data', 'weights')
        os.makedirs(weights_dir, exist_ok=True)
        v1_path = os.path.join(weights_dir, 'v1.json')

        existing = {}
        if os.path.exists(v1_path):
            with open(v1_path, 'r', encoding='utf-8') as f:
                existing = json.load(f)
            backup = os.path.join(
                weights_dir, f"v1.backup_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json")
            with open(backup, 'w', encoding='utf-8') as f:
                json.dump(existing, f, ensure_ascii=False, indent=2)
            logger.info(f"原权重已备份: {backup}")

        # 保持 north_flow 等零权重键存在（权重表完整性）
        merged = {k: proposed.get(k, 0.0) for k in current}
        for k in existing.get('short', {}):
            if k not in merged:
                merged[k] = 0.0
        merged.update(proposed)

        existing['short'] = merged
        with open(v1_path, 'w', encoding='utf-8') as f:
            json.dump(existing, f, ensure_ascii=False, indent=2)
        logger.info(f"权重已写入: {v1_path}")
        logger.info("⚠️ 注意：ScoringModel 加载优先级 v1.json > config.yml，"
                    "config.yml 中的权重此后将被忽略")
    else:
        print("\n⚠️ 未写入。确认无误后加 --apply 生效。")


if __name__ == '__main__':
    main()
