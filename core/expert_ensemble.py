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
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

from core.fundamental_provider import FundamentalProvider
from core.event_provider import EventProvider
# 注：TechnicalScorer.score 需要 K 线 DataFrame，与本模块"stock dict"接口不匹配，
# 故专家 5 维中的 technical 维度用 stock dict 已有字段（动量/RPS/换手/量比）做轻量评分。
# 如需接入完整 6 维技术分，由调用方在 stock_data 中预填 'tech_score_0_100' 字段。


logger = logging.getLogger(__name__)


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


@dataclass
class ExpertVerdict:
    """单个股票的专家评分与融合结果"""
    code: str
    expert_score: float           # 0-100，5 维加权得分
    model_score: float            # 0-100，7 因子加权得分（来自主模型）
    delta: float                  # expert - model
    confidence: str               # 'high' / 'medium' / 'low' / 'conflict'
    ensemble_score: float         # 融合后的最终建议分
    expert_breakdown: Dict        # 5 维 raw_score 展开
    notes: List[str]              # 推理与告警


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
    def _fundamental_dim(stock: Dict) -> Tuple[float, str]:
        """基本面（ROE 主导 + 增长）— 与 7 因子 valuation_fundamental 区分"""
        fund = stock.get('fundamentals') or {}
        if not fund:
            return 50.0, '无基本面数据 → 中性'
        score = 50.0
        roe = fund.get('roe')
        rev_g = fund.get('revenue_growth')
        prof_g = fund.get('profit_growth')

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
        return max(0, min(100, score)), f'ROE={roe}, rev_g={rev_g}, prof_g={prof_g}'

    @staticmethod
    def _technical_dim(stock: Dict) -> Tuple[float, str]:
        """
        技术面 — 轻量评分，4 个子信号平均
        优先用调用方预填的完整 6 维技术分（tech_score_0_100），无则用 K 线派生字段拼装
        """
        full = stock.get('tech_score_0_100')
        if full is not None:
            return float(full), f'6维技术分={full}'

        score = 50.0
        notes = []
        rps = stock.get('rps')               # 0-100，横截面百分位
        pct_chg = stock.get('pct_chg')       # 当日涨幅 %
        turnover = stock.get('turnover')     # 换手率 %
        amount_ratio = stock.get('amount_ratio')  # 量比

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

        return max(0, min(100, score)), ', '.join(notes) or '技术面中性'

    @staticmethod
    def _capital_dim(stock: Dict) -> Tuple[float, str]:
        """资金面 — 大单 + 北向 + 主力净流入（与 capital_flow 不同权重组合）"""
        main_fund = stock.get('main_fund_accumulated')
        north = stock.get('north_flow_accumulated')

        # 缺失数据用中性
        if main_fund is None and north is None:
            return 50.0, '无资金流数据'

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
            # 类似处理
            if north > 2000:
                score += 12; notes.append('北向>2000w')
            elif north < -2000:
                score -= 12; notes.append('北向<-2000w')

        return max(0, min(100, score)), ', '.join(notes) or '资金面中性'

    @staticmethod
    def _valuation_dim(stock: Dict) -> Tuple[float, str]:
        """估值水位 — PE/PB 横截面百分位排名"""
        pe_rank = stock.get('pe_percentile')      # 0-100，越高越贵
        pb_rank = stock.get('pb_percentile')

        if pe_rank is None and pb_rank is None:
            return 50.0, '无估值百分位'

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

        return max(0, min(100, score)), ', '.join(notes) or '估值中性'

    @staticmethod
    def _event_dim(stock: Dict) -> Tuple[float, str]:
        """事件催化 — 复用 EventProvider.score"""
        events = stock.get('recent_events') or []
        if not events:
            return 50.0, '无近期事件'
        s = EventProvider.score(events, reference_date=stock.get('_decision_date'))
        # 取最早一条的标题作为 note
        title = events[0].get('title', '')[:24]
        return s, f'event_score={s:.1f}, top="{title}"'

    # ── 综合打分 ─────────────────────────────────────

    def score(self, stock: Dict) -> Tuple[float, Dict, List[str]]:
        """
        对单只股票给出专家 5 维评分

        返回：(expert_score 0-100, breakdown dict, notes list)
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
        expert_score = 0.0
        for name, weight in EXPERT_WEIGHTS.items():
            raw, note = dims[name]
            expert_score += raw * weight
            breakdown[name] = {
                'raw_score': round(raw, 2),
                'weight': weight,
                'weighted': round(raw * weight, 2),
            }
            if note:
                notes.append(f'[{name}] {note}')
        return round(expert_score, 2), breakdown, notes


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

        expert_score, breakdown, notes = self.scorer.score(stock)
        delta = expert_score - model_score
        abs_delta = abs(delta)

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
        ensemble_score = round(max(0, min(100, ensemble_raw)), 2)

        return ExpertVerdict(
            code=stock.get('code', ''),
            expert_score=round(expert_score, 2),
            model_score=round(model_score, 2),
            delta=round(delta, 2),
            confidence=confidence,
            ensemble_score=ensemble_score,
            expert_breakdown=breakdown,
            notes=notes,
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
    """
    return {
        'high':     1.00,
        'medium':   0.85,
        'low':      0.70,
        'conflict': 0.50,
    }.get(confidence, 1.00)
