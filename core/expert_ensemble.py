"""
专家评分 Ensemble — 第二意见融合

设计动机
--------
7 因子加权模型存在三个固有脆弱点：
1. 权重取自历史 OOS IC 中位数，是统计意义上的最优平均解，无法对单只股票"特事特办"
2. 数据缺口（资金流/北向/事件）会让某些因子被打成中性，权重被再分配，信号被稀释
3. 单一模型的"独裁"风险：极端行情下，单模型可能系统性偏向某种风格

第二意见的合理形态：与 7 因子模型**不同视角**的 5 维度评分，
通过差异幅度 (Δ) 调整最终置信度，从而在模型一致时给高分、不一致时降权。

5 维度（TdxStockHunter 风格）
----------------------------
fundamental    25  ROE + 营收/净利增速 + 现金流稳健（长线基本面，与 7 因子里的 fundamental 不同角度）
technical      25  趋势 + 动量 + 形态（六维技术分，与 technical_scorer 完全对齐）
capital        20  大单资金 + 北向 + 主力净流入（与 capital_flow 部分重叠但权重不同）
valuation      15  PE/PB 估值水位（用 percentile_rank 而非线性扣分）
event          15  公告/业绩/新闻事件催化（已由 EventProvider 覆盖）

融合规则（merge rule）
----------------------
|expert_score − model_score|  融合策略
≤ 10                         高一致性：confidence=高，ensemble_score 加权平均
10 < Δ ≤ 25                  中度分歧：confidence=中，ensemble_score 用专家 0.4 + 模型 0.6（专家做兜底）
> 25                         高度分歧：confidence=低，ensemble_score 大幅下调 -8 分，
                             并标记 `conflict=True` 供 portfolio_optimizer 降仓使用

融合公式
--------
ensemble_score = model_score
              + 0.15 * (expert_score - model_score)        # 一致时：温和上拉
              + confidence_bonus                          # 1.5 (高) / 0.5 (中) / 0 (低)
              - conflict_penalty                          # 0 / 0 / 8
并 clamp 到 [0, 100]

注意：ensemble 不修改 model 自己的输出，作为 `expert_adjusted_score` 字段附加。
"""

import logging
import math
from core import numeric
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

from core.fundamental_provider import FundamentalProvider
from core.event_provider import EventProvider
# 注：TechnicalScorer.score 需要 K 线 DataFrame，与本模块"stock dict"接口不匹配，
# 故专家 5 维中的 technical 维度用 stock dict 已有字段（动量/RPS/换手/量比）做轻量评分。
# 如需接入完整 6 维技术分，由调用方在 stock_data 中预填 'tech_score_0_100' 字段。


logger = logging.getLogger(__name__)


# 2026-09-23（审查报告 R1）：委托 core.numeric（契约 A 唯一实现）
_finite = numeric.is_finite


_as_float = numeric.float_or


# 5 维权重（专家卡）— 内部固定，不与 7 因子权重联动
EXPERT_WEIGHTS = {
    'fundamental': 0.25,
    'technical':   0.25,
    'capital':     0.20,
    'valuation':   0.15,
    'event':       0.15,
}

# Δ 阈值与对应 confidence / penalty
HIGH_CONSENSUS_THRESHOLD = 10.0   # |Δ| ≤ 10 视为高一致性
MEDIUM_CONSENSUS_THRESHOLD = 25.0 # |Δ| ≤ 25 为中度；超过即冲突
CONFLICT_PENALTY = 8.0            # 冲突时扣分

# 2026-09-19（用户决策"完善专家五维"）：覆盖率门控。
# 生产日志证据（daily_job_20260917/18.log）：expert 恒 ≈48-50 vs model ≈74-75，
# 几乎恒"冲突"——根因是 5 维中常只有 technical 有数据（权重 0.25），其余 4 维
# 因基本面/估值/资金流/事件缺失退化为中性 50，把 expert 拉到 ≈50。
# "失明专家"对高分票系统性打出 Δ≈−25 → 恒 conflict → −8 分 + 仓位系数 0.5，
# 冲突的原因不是观点分歧而是数据缺失。
# 修复 = 两招：
#   ① 覆盖率门控：有数据维度权重占比 < MIN_EXPERT_COVERAGE 时专家弃权
#     （confidence='low_coverage'，ensemble=model，仓位系数 1.0，不降不罚）
#   ② 已覆盖维重归一：expert_score 只在有数据的维度上按权重归一，
#     不再让缺失维的中性 50 稀释有数据的信号
MIN_EXPERT_COVERAGE = 0.40   # ≈ 至少两个维度有真实数据（单维最高 0.25）


@dataclass
class ExpertVerdict:
    """单个股票的专家评分与融合结果"""
    code: str
    expert_score: float           # 0-100，已覆盖维加权得分（未覆盖时 = 50）
    model_score: float            # 0-100，7 因子加权得分（来自主模型）
    delta: float                  # expert - model
    confidence: str               # 'high' / 'medium' / 'low' / 'conflict' / 'low_coverage'
    ensemble_score: float         # 融合后的最终建议分
    expert_breakdown: Dict        # 5 维 raw_score 展开（含 covered 标记）
    notes: List[str]              # 推理与告警
    coverage: float = 1.0         # 有数据维度的权重占比 0-1
    abstained: bool = False       # 覆盖率不足 → 专家弃权


class ExpertScorer:
    """
    TdxStockHunter 风格 5 维专家评分器

    与 7 因子模型的差异：
    - 7 因子是"按 IC 加权平均"，偏统计意义
    - 5 维专家是"按维度独立评分后加权"，每维自己决定信号强度
      （例如 fundamental 维即使权重仅 25%，但 ROE 极好时该项仍可独立给出 90+ 分）
    """

    def __init__(self):
        # 不再创建 TechnicalScorer 实例；technical 维度改用 stock dict 已有字段
        pass

    # ── 5 维独立打分（每维 0-100） ─────────────────────

    @staticmethod
    def _fundamental_dim(stock: Dict) -> Tuple[float, str, bool]:
        """基本面（ROE 主导 + 增长）— 与 7 因子 valuation_fundamental 区分"""
        fund = stock.get('fundamentals') or {}
        if not fund:
            return 50.0, '无基本面数据 → 中性', False
        score = 50.0
        roe = fund.get('roe')
        rev_g = fund.get('revenue_growth')
        prof_g = fund.get('profit_growth')
        # 2026-09-22 修复（契约 A）：`is not None` 会把 NaN/±Inf 算作"已覆盖"，
        # 人为抬高 coverage（把失明维度当有效第二意见）。统一用 _finite。
        roe = float(roe) if _finite(roe) else None
        rev_g = float(rev_g) if _finite(rev_g) else None
        prof_g = float(prof_g) if _finite(prof_g) else None
        covered = roe is not None or rev_g is not None or prof_g is not None
        if not covered:
            return 50.0, '基本面字段全空 → 中性', False

        if roe is not None:
            if roe >= 25:    score += 25
            elif roe >= 18:  score += 20
            elif roe >= 12:  score += 12
            elif roe >= 8:   score += 5
            elif roe < 0:    score -= 20

        # 增长（双指标都正面才计入，避免单一指标噪声）
        if rev_g is not None and prof_g is not None:
            if rev_g > 15 and prof_g > 20:
                score += 18
            elif rev_g > 5 and prof_g > 10:
                score += 10
            elif rev_g < -10 or prof_g < -20:
                score -= 15
        return max(0, min(100, score)), f'ROE={roe}, rev_g={rev_g}, prof_g={prof_g}', covered

    @staticmethod
    def _technical_dim(stock: Dict) -> Tuple[float, str, bool]:
        """
        技术面 — 轻量评分，4 个子信号平均
        优先用调用方预填的完整 6 维技术分（tech_score_0_100），无则用 K 线派生字段拼装
        """
        full = stock.get('tech_score_0_100')
        if full is not None:
            # 2026-09-22 修复（契约 A）：`float(NaN)` 不报错 → raw_score NaN →
            # expert/ensemble 全链路 NaN；且 `is not None` 会把 NaN 当"已覆盖"。
            if _finite(full):
                return float(full), f'6维技术分={full}', True
            return 50.0, '技术分非有限 → 中性', False

        score = 50.0
        notes = []
        rps = stock.get('rps')               # 0-100，横截面百分位
        pct_chg = stock.get('pct_chg')       # 当日涨幅 %
        turnover = stock.get('turnover')     # 换手率 %
        amount_ratio = stock.get('amount_ratio')  # 量比
        covered = any(_finite(v) for v in (rps, pct_chg, turnover, amount_ratio))
        if not covered:
            return 50.0, '无技术面字段 → 中性', False

        rps = float(rps) if _finite(rps) else None
        pct_chg = float(pct_chg) if _finite(pct_chg) else None
        turnover = float(turnover) if _finite(turnover) else None
        amount_ratio = float(amount_ratio) if _finite(amount_ratio) else None

        if rps is not None:
            if rps >= 80:    score += 12; notes.append(f'RPS={rps:.0f}强')
            elif rps >= 60:  score += 6
            elif rps <= 20:  score -= 10; notes.append(f'RPS={rps:.0f}弱')

        if pct_chg is not None:
            # 短线不宜追高，但温和上涨加分
            if 0 < pct_chg < 5:    score += 8; notes.append(f'+{pct_chg}%温和')
            elif pct_chg >= 7:     score -= 8; notes.append(f'+{pct_chg}%追高风险')
            elif -3 < pct_chg < 0: score += 2  # 小幅回调可接受

        if turnover is not None:
            if 2 <= turnover <= 8:  score += 5; notes.append(f'换手{turnover}%活跃')
            elif turnover > 15:     score -= 12; notes.append(f'换手{turnover}%异常')

        if amount_ratio is not None and amount_ratio >= 2:
            score += 4; notes.append(f'量比{amount_ratio}放量')

        return max(0, min(100, score)), ', '.join(notes) or '技术面中性', covered

    @staticmethod
    def _capital_dim(stock: Dict) -> Tuple[float, str, bool]:
        """资金面 — 大单 + 北向 + 主力净流入（与 capital_flow 不同权重组合）"""
        main_fund = stock.get('main_fund_accumulated')
        north = stock.get('north_flow_accumulated')

        # 缺失数据用中性
        # 2026-09-22 修复（契约 A）：`is None` 会把 NaN 算作"已覆盖"并给中性 50 →
        # 用失明维度抬高 coverage、放行 MIN_EXPERT_COVERAGE 门控。统一用 _finite。
        main_fund = float(main_fund) if _finite(main_fund) else None
        north = float(north) if _finite(north) else None
        if main_fund is None and north is None:
            return 50.0, '无资金流数据', False

        score = 50.0
        notes = []

        if main_fund is not None:
            # 修复（2026-09-06 全量审查）：main_fund_accumulated 实为「当日主力
            # 净流入，单位元」（data_engine._get_capital_flow_asharehub 返回注释
            # 明确"当日主力净流入（元)"）。原注释"百分位 0-100 / 万元"均与实际
            # 不符，万元阈值导致任何净流入 >5000 元的票都拿 +18 满档加成
            # （capital 维虚高、丧失区分度）。改为元量纲阈值。
            if main_fund > 50_000_000:      # 净流入 > 5000 万
                score += 18; notes.append('主力净流入>5000万')
            elif main_fund > 10_000_000:    # 净流入 > 1000 万
                score += 10; notes.append('主力净流入>1000万')
            elif main_fund < -50_000_000:
                score -= 18; notes.append('主力净流出>5000万')
            elif main_fund < -10_000_000:
                score -= 10; notes.append('主力净流出>1000万')

        if north is not None:
            # 2026-09-22 修复（缺陷排查 P1-7 · 单位错配）：北向金额字段口径更正。
            # data_engine.get_north_flow_accumulated 的返回是
            # 「近 N 日净增持**股数**」（最新持股量 − N 日前持股量），**不是万元**。
            # 原阈值 `north > 2000` 注释写"北向>2000w"→ 2000 股是极小量，
            # 任何净增持都能拿满 ±12，capital 维丧失区分度（与 2026-09-06 已修的
            # main_fund 单位问题同构，当时漏修此分支）。
            #
            # 2026-09-23 实证标定（剩余项 #4，用户要求"找数据"）：
            #   数据 = 本地 factor_daily.db north_flow 表（AShareHub
            #   northbound_holdings 同源同口径，单位=股），n=178 样本、
            #   覆盖 2026-06-30~07-10 共 5 个交易日（候选池股票）。
            #   分布：p15=−1318万股 / p50=+36万股 / p85=+2298万股。
            #   东财个股北向接口已确认停发（RPT_MUTUAL_HOLD_DETRENDD
            #   "报表配置不存在"，与 2024-08 北向披露新政一致）→ 只能用
            #   AShareHub 口径积累样本。
            #   阈值定 ±1500万股：命中率 31%（±500万→56% 太松，±2000万→25% 偏严），
            #   与"显著加仓/减仓"语义匹配。
            #   ⚠️ 局限：小样本（n=178/5 日）且仅覆盖候选池（偏大票），月度复检时
            #   用新积累的 north_flow 数据复核（脚本：按本注释口径重算分位）。
            if north > 15_000_000:           # 净增持 > 1500 万股
                score += 12; notes.append('北向净增持>1500万股')
            elif north < -15_000_000:        # 净减持 > 1500 万股
                score -= 12; notes.append('北向净减持>1500万股')

        return max(0, min(100, score)), ', '.join(notes) or '资金面中性', True

    @staticmethod
    def _valuation_dim(stock: Dict) -> Tuple[float, str, bool]:
        """估值水位 — PE/PB 横截面百分位排名"""
        pe_rank = stock.get('pe_percentile')      # 0-100，越高越贵
        pb_rank = stock.get('pb_percentile')

        # 2026-09-22 修复（契约 A）：非有限值按缺失处理（同 _capital_dim）
        pe_rank = float(pe_rank) if _finite(pe_rank) else None
        pb_rank = float(pb_rank) if _finite(pb_rank) else None
        if pe_rank is None and pb_rank is None:
            return 50.0, '无估值百分位', False

        score = 50.0
        notes = []

        # 估值"便宜"才有短线弹性（破净/低 PE 是加分）；高位估值得低分
        if pe_rank is not None:
            if pe_rank < 20:    score += 15; notes.append(f'PE 百分位{pe_rank:.0f}便宜')
            elif pe_rank < 40:  score += 8
            elif pe_rank > 80:  score -= 12; notes.append(f'PE 百分位{pe_rank:.0f}泡沫')
            elif pe_rank > 60:  score -= 4

        if pb_rank is not None:
            if pb_rank < 20:    score += 8
            elif pb_rank > 80:  score -= 8

        return max(0, min(100, score)), ', '.join(notes) or '估值中性', True

    @staticmethod
    def _event_dim(stock: Dict) -> Tuple[float, str, bool]:
        """事件催化 — 复用 EventProvider.score"""
        events = stock.get('recent_events') or []
        if not events:
            return 50.0, '无近期事件', False
        s = EventProvider.score(events, reference_date=stock.get('_decision_date'))
        # 取最早一条的标题作为 note
        title = events[0].get('title', '')[:24]
        return s, f'event_score={s:.1f}, top="{title}"', True

    # ── 综合打分 ─────────────────────────────────────

    def score(self, stock: Dict) -> Tuple[float, Dict, List[str], float]:
        """
        对单只股票给出专家 5 维评分（2026-09-19 起：已覆盖维重归一）

        返回：(expert_score 0-100, breakdown dict, notes list, coverage 0-1)
          expert_score 只聚合有真实数据的维度（按其权重重归一），
          缺失维不再以中性 50 稀释信号；全维缺失 → 50.0（coverage=0）。
          coverage = 有数据维度的权重占比（0-1）。
        """
        dims = {
            'fundamental': self._fundamental_dim(stock),
            'technical':   self._technical_dim(stock),
            'capital':     self._capital_dim(stock),
            'valuation':   self._valuation_dim(stock),
            'event':       self._event_dim(stock),
        }
        breakdown = {}
        notes = []
        weighted_sum = 0.0
        covered_weight = 0.0
        for name, weight in EXPERT_WEIGHTS.items():
            raw, note, covered = dims[name]
            # 2026-09-22 修复（契约 A）：任一分维返回非有限值（如上游字段为 NaN
            # 且该维未做守卫）都会让 expert_score 变 NaN → 进而污染 ensemble。
            # 非有限一律降级为"未覆盖 + 中性 50"。
            if covered and not _finite(raw):
                raw, covered = 50.0, False
                note = (note or '') + ' [非有限值 → 按缺失处理]'
            else:
                raw = float(raw)
            if covered:
                weighted_sum += raw * weight
                covered_weight += weight
            breakdown[name] = {
                'raw_score': round(raw, 2),
                'weight': weight,
                'weighted': round(raw * weight, 2),
                'covered': covered,
            }
            if note:
                notes.append(f'[{name}] {note}')
        coverage = covered_weight / sum(EXPERT_WEIGHTS.values())
        expert_score = (weighted_sum / covered_weight) if covered_weight > 0 else 50.0
        return round(expert_score, 2), breakdown, notes, round(coverage, 4)


class ExpertEnsemble:
    """
    专家评分 + 主模型 融合器

    用法：
        ee = ExpertEnsemble()
        verdict = ee.fuse(stock_dict_with_model_score)
        # verdict.ensemble_score 即可送 portfolio_optimizer
        # verdict.confidence 决定仓位调整系数
    """

    def __init__(self):
        self.scorer = ExpertScorer()

    def fuse(self, stock: Dict, model_score: Optional[float] = None) -> ExpertVerdict:
        """
        融合主模型分数与专家分数

        参数：
          stock: 包含主模型已计算分数字段（'score'）或外部传入 model_score
                 同时包含各维所需原始数据
        """
        if model_score is None:
            model_score = stock.get('score', 50.0)

        # 2026-09-22 修复（缺陷排查 P0）：`stock.get('score', 50.0)` 的默认值
        # 只在**键缺失**时生效 —— 键存在而值为 None/NaN 时默认值无效。原实现的
        # `delta = expert_score - model_score` 会直接 TypeError（None），
        # 或让 model_score=NaN 一路穿透；而下面 `max(0, min(100, NaN))` 会返回
        # **100**（`min(100, NaN)` 取 100）→ NaN 被当成"最强共识满分"参与仓位加权。
        if not _finite(model_score):
            # 2026-09-22 修复（缺陷排查 P0）：`stock.get('score', 50.0)` 的默认值
            # 只在**键缺失**时生效 —— 键存在而值为 None/NaN 时默认值无效。
            # 原实现的 `delta = expert_score - model_score` 会 TypeError（None），
            # 或让 model_score=NaN 一路穿透；而 `max(0, min(100, NaN))` 返回
            # **100**（`min(100, NaN)` 取 100）→ NaN 被当成"最强共识满分"，
            # 再经 PortfolioOptimizer 的 0.6/0.4 加权**放大仓位**。
            # 处理：显式抛错，复用调用方既有的降级路径
            # （strategies/short_term.py 捕获后"降级到仅主模型分数"，不下发
            #  ensemble_score/weight_factor），比静默给一个错值安全。
            raise ValueError(
                f'model_score 非有限值（{model_score!r}），无法融合；'
                f'调用方应降级为仅使用主模型分数')
        model_score = float(model_score)

        expert_score, breakdown, notes, coverage = self.scorer.score(stock)
        delta = expert_score - model_score
        abs_delta = abs(delta)

        # 覆盖率门控（2026-09-19）：数据不足 → 专家弃权，不参与融合、不降不罚。
        # 这是"失明专家"修复的核心：看不见基本面的专家没有资格投反对票。
        if coverage < MIN_EXPERT_COVERAGE:
            ensemble_score = round(max(0, min(100, model_score)), 2)
            notes.append(
                f'⚠️ 专家弃权: 维度覆盖率 {coverage:.0%} < {MIN_EXPERT_COVERAGE:.0%}，'
                f'有数据维度不足以构成第二意见 → ensemble=model，仓位不降')
            return ExpertVerdict(
                code=stock.get('code', ''),
                expert_score=expert_score,
                model_score=round(model_score, 2),
                delta=round(delta, 2),
                confidence='low_coverage',
                ensemble_score=ensemble_score,
                expert_breakdown=breakdown,
                notes=notes,
                coverage=coverage,
                abstained=True,
            )

        # 置信度分级
        if abs_delta <= HIGH_CONSENSUS_THRESHOLD:
            confidence = 'high'
            confidence_bonus = 1.5
            conflict_penalty = 0.0
        elif abs_delta <= MEDIUM_CONSENSUS_THRESHOLD:
            confidence = 'medium'
            confidence_bonus = 0.5
            conflict_penalty = 0.0
        else:
            confidence = 'conflict'
            confidence_bonus = 0.0
            conflict_penalty = CONFLICT_PENALTY
            notes.append(f'⚠️ 高度分歧: model={model_score:.1f} vs expert={expert_score:.1f} (Δ={delta:+.1f})')

        # 融合公式
        ensemble_raw = (
            model_score
            + 0.15 * delta
            + confidence_bonus
            - conflict_penalty
        )
        # 2026-09-22 修复（缺陷排查 P0）：`max(0, min(100, x))` 对 NaN 返回 100
        # （`min(100, nan)` 取 100）→ 坏值变"满分共识"。入口已保证 model_score
        # 有限，此处再兜一层，确保任何新增分维都不会重新引入该路径。
        if not _finite(ensemble_raw):
            logger.warning(f"ensemble_raw 非有限（{ensemble_raw!r}）→ 回落 model_score")
            ensemble_raw = model_score
        ensemble_score = round(max(0.0, min(100.0, float(ensemble_raw))), 2)

        return ExpertVerdict(
            code=stock.get('code', ''),
            expert_score=round(expert_score, 2),
            model_score=round(model_score, 2),
            delta=round(delta, 2),
            confidence=confidence,
            ensemble_score=ensemble_score,
            expert_breakdown=breakdown,
            notes=notes,
            coverage=coverage,
            abstained=False,
        )

    def fuse_batch(self, stocks: List[Dict]) -> List[ExpertVerdict]:
        """批量融合"""
        return [self.fuse(s) for s in stocks]


def confidence_to_weight_factor(confidence: str) -> float:
    """
    confidence → 仓位调整系数（供 portfolio_optimizer 调用）

    high    → 1.00 （一致，正常配置）
    medium  → 0.85 （中度分歧，轻度降仓）
    low     → 0.70 （轻度低置信，降仓）
    conflict→ 0.50 （冲突显著，强烈降仓）
    low_coverage → 1.00 （专家弃权：数据不足，不构成第二意见，不做任何调整）
    """
    return {
        'high':     1.00,
        'medium':   0.85,
        'low':      0.70,
        'conflict': 0.50,
        'low_coverage': 1.00,
    }.get(confidence, 1.00)
