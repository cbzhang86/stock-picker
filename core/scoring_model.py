"""
核心评分模型 — 多因子加权评分系统

设计参考：
  - claude-for-financial-services-cn china-deal-screening 七维评分模型
  - TradingAgents-CN-lite schemas.py 5档评级枚举
  - 原提示词中的因子权重分配

支持：
  - 短线/长线双模式权重
  - 可版本化的权重管理
  - 因子分项展开（可解释性）
  - 评级映射
"""

import json
import os
import logging
from typing import Dict, List, Optional, Tuple
from dataclasses import dataclass
from datetime import datetime

import numpy as np


from core.factor_library import FactorLibrary

logger = logging.getLogger(__name__)


# ---- 评分等级 ----
@dataclass
class RatingLevel:
    """5档评级（参考 TradingAgents-CN-lite PortfolioRating）"""
    name: str
    min_score: float
    max_score: float
    label_cn: str

RATING_LEVELS = [
    RatingLevel('buy', 80, 101, '买入'),
    RatingLevel('overweight', 65, 80, '增持'),
    RatingLevel('hold', 45, 65, '持有'),
    RatingLevel('underweight', 25, 45, '减持'),
    RatingLevel('sell', 0, 25, '卖出'),
]


def score_to_rating(score: float) -> Tuple[str, str]:
    """分数 → 评级"""
    score = min(max(score, 0), 100)  # 先截断到0-100
    for level in RATING_LEVELS:
        if level.min_score <= score < level.max_score:
            return level.name, level.label_cn
    return 'sell', '卖出'


class ScoringModel:
    """
    核心评分模型

    支持：
    - 短线/长线权重
    - 权重版本管理（JSON序列化）
    - 单股票评分 + 批量排序
    """

    # 默认权重（与 config.yml 保持同步，作为 fallback）
    # 短线：研报中位数对齐（资金流+龙虎榜共0.30 + momentum 0.25 + hot_theme 0.10）
    # 长线：研报中位数对齐（基础财务+估值共0.50 + momentum+北向 0.35 + 机构 0.15）
    # risk 不进权重表，由 penalty 路径独立扣分（见 ActiveWeight 与 penalty 注释）
    DEFAULT_WEIGHTS = {
        'short': {
            'capital_flow': 0.25,
            'north_flow': 0.10,
            'momentum': 0.25,
            'technical': 0.15,
            'volume_price': 0.10,
            'hot_theme': 0.10,
            'dragon_tiger': 0.05,
        },
        'long': {
            'fundamental': 0.30,
            'north_flow': 0.20,
            'momentum': 0.15,
            'valuation': 0.20,
            'institutional': 0.15,
        }
    }

    def __init__(self, weights: dict = None, model_version: str = 'v1',
                 weights_dir: str = 'data/weights', sell_config: dict = None):
        self.factor_lib = FactorLibrary()
        self.model_version = model_version
        self.weights_dir = weights_dir
        self.sell_config = sell_config or {}
        os.makedirs(self.weights_dir, exist_ok=True)

        # 加载权重（优先级：v1.json > config传入 > DEFAULT_WEIGHTS）
        loaded = self._load_weights(model_version)
        if loaded:
            self.weights = loaded
            if weights and weights != loaded:
                logger.warning(f"v1.json 权重与 config.yml 不一致！config 权重被忽略。")
            logger.info(f"权重从 {model_version}.json 加载")
        elif weights:
            self.weights = weights
            logger.info(f"权重从 config 加载（v1.json 尚不存在）")
        else:
            self.weights = self.DEFAULT_WEIGHTS
            logger.info("权重从 DEFAULT_WEIGHTS 加载")

    def _load_weights(self, version: str) -> Optional[Dict]:
        """从文件加载权重"""
        path = os.path.join(self.weights_dir, f'{version}.json')
        if os.path.exists(path):
            try:
                with open(path, 'r', encoding='utf-8') as f:
                    return json.load(f)
            except Exception as e:
                logger.warning(f"加载权重失败 {path}: {e}")
        return None

    def save_weights(self, version: str = None):
        """保存当前权重"""
        if version is None:
            version = self.model_version
        path = os.path.join(self.weights_dir, f'{version}.json')
        with open(path, 'w', encoding='utf-8') as f:
            json.dump(self.weights, f, ensure_ascii=False, indent=2)
        logger.info(f"权重已保存: {path}")

    def get_weights(self, mode: str = 'short') -> Dict:
        """获取指定模式的权重"""
        # 处理扁平权重 dict（从 config.yml 直接传入，无 'short'/'long' 嵌套）
        # 例: {'capital_flow': 0.25, 'north_flow': 0.10, ...}
        if mode in self.weights:
            return self.weights[mode]
        # 扁平 dict：检查是否有因子名作为 key（而非模式名）
        if self.weights and any(k in self.weights for k in self.DEFAULT_WEIGHTS.get(mode, {})):
            return self.weights
        return self.DEFAULT_WEIGHTS.get(mode, {})

    def score_stock(self, stock_data: Dict, mode: str = 'short') -> Dict:
        """
        对单只股票评分

        参数：
          stock_data: {
              'code': '000001',
              'name': '平安银行',
              'price': 12.50,
              'main_fund_accumulated': 5000_0000,   # 主力累计流入
              'north_flow_accumulated': 2000_0000,  # 北向累计流入
              'rps_20': 85,                         # RPS值
              'macd_status': {...},                  # MACD状态
              'volume_ratio': 1.2,                   # 量比
              'turnover': 2.5,                       # 换手率
              'risk_check': {'passed': True, ...},   # 风控结果
              'pct_chg': 1.5,                        # 当日涨幅
              'amount': 500_000_000,                 # 成交额
          }

        返回：
          {
              'score': 85.0,       # 总分 0-100
              'rating': 'buy',     # 评级
              'rating_cn': '买入', # 中文评级
              'breakdown': {...},  # 各因子分项
              'decision': '推荐买入' # 决策建议
          }
        """
        score = 0.0
        breakdown = {}
        weights = self.get_weights(mode)

        # 计算各因子得分
        factors = self.factor_lib.compute_all_factors(stock_data, mode)

        # 判断各因子是否有真实数据支撑，将"数据不可用"的因子权重重分配给活跃因子
        # 防止 45% 权重输出恒定 50 分导致总分被压缩
        # risk 因子不参与加权（语义上它是过滤/惩罚，不是分项；详见文末 risk penalty 段）
        neutral_weight = 0.0
        factor_scores = {}
        weighted_score = 0.0
        for factor_name, weight in weights.items():
            if factor_name == 'risk':
                continue  # risk 走后的 penalty 路径，不在加权循环里混
            factor_score = factors.get(factor_name, 50.0)
            is_neutral = False

            # 资金流/北向：原始数据为 None → 数据不可用
            if factor_name == 'capital_flow' and stock_data.get('main_fund_accumulated') is None:
                is_neutral = True
            if factor_name == 'north_flow' and stock_data.get('north_flow_accumulated') is None:
                is_neutral = True

            factor_scores[factor_name] = (factor_score, weight, is_neutral)
            if is_neutral:
                neutral_weight += weight

        # 活跃因子权重总和（risk 不在内）
        # 改进：用 weights 实际总和作为分母，避免 config 删/加键时分母硬编码 1.0 漂移
        weights_total = sum(weights.values()) if weights else 1.0
        active_weight = weights_total - neutral_weight

        for factor_name, (factor_score, weight, is_neutral) in factor_scores.items():
            if is_neutral:
                # 数据不可用：用原权重，标记出来
                weighted = factor_score * weight
                breakdown[factor_name] = {
                    'raw_score': round(factor_score, 2),
                    'weight': weight,
                    'weighted': round(weighted, 2),
                    'data_available': False,
                    'note': '接口不可用，已降权'
                }
                weighted_score += weighted
            else:
                # 数据可用：获得额外权重分配（按比例吸收不可用因子的权重）
                extra = (neutral_weight * weight / active_weight) if active_weight > 0 else 0
                effective_weight = weight + extra
                weighted = factor_score * effective_weight
                breakdown[factor_name] = {
                    'raw_score': round(factor_score, 2),
                    'weight': weight,
                    'effective_weight': round(effective_weight, 4),
                    'weighted': round(weighted, 2),
                    'data_available': True
                }
                weighted_score += weighted

        # === Risk penalty（risk 改成纯扣分项，不进加权） ===
        # 设计：risk_filter 已经把 ST/解禁压力/成交额过低/涨停封死的票 in-pass 直接拦。
        # 留到打分阶段的票，可能仍有软风险（成交额偏低、换手率偏高、量比异常等），
        # 这些 penalty<0.8 的"软风险"通过这里在总分上扣减。
        risk_check = stock_data.get('risk_check', {})
        risk_penalty = 0.0
        risk_note = ''
        if risk_check:
            if not risk_check.get('passed', True):
                # 硬拦截：risk_filter 已在前置拦下，这里是为了双保险
                risk_penalty = risk_check.get('score_penalty', 0)
                risk_note = 'risk_check 未通过'
            else:
                # passed=True 但 score_penalty 不为 0：软风险扣分
                # penalty < 0.4 → ×0.2（轻度软风险）
                # penalty ≥ 0.4 → ×0.5（较高风险如低成交额/高换手）
                # 分段系数让轻度软风险（量比异常 0.3）不受太大影响，
                # 同时让高风险的惩罚力度拉开差距
                risk_penalty = risk_check.get('score_penalty', 0)
                if risk_penalty > 0:
                    risk_note = f"软风险扣分(penalty={risk_penalty})"

        risk_coeff = 0.2 if risk_penalty < 0.4 else 0.5
        score = weighted_score * (1 - risk_penalty * risk_coeff)

        # 转百分制 + 截断
        final_score = round(min(max(score, 0), 100), 2)

        # 评级
        rating_name, rating_cn = score_to_rating(final_score)

        # 决策建议
        decision = self._make_decision(final_score, mode)

        # 计算目标价和止损价（优先从 sell_config 读取）
        price = stock_data.get('price', 0)
        if mode == 'short':
            tp = self.sell_config.get('take_profit', 0.02)
            sl = abs(self.sell_config.get('stop_loss', -0.02))
            target_price = round(price * (1 + tp), 2)
            stop_price = round(price * (1 - sl), 2)
        else:
            target_price = round(price * 1.15, 2)   # 长线 +15%
            stop_price = round(price * 0.92, 2)     # 长线 -8%

        # 生成选股理由（一句话+数据）
        reasoning = self._generate_reasoning(stock_data, breakdown, mode)

        return {
            'code': stock_data.get('code', ''),
            'name': stock_data.get('name', ''),
            'price': price,
            'score': final_score,
            'rating': rating_name,
            'rating_cn': rating_cn,
            'breakdown': breakdown,
            'decision': decision,
            'target_price': target_price,
            'stop_price': stop_price,
            'reasoning': reasoning,
            'mode': mode,
            'model_version': self.model_version,
        }

    def _make_decision(self, score: float, mode: str) -> str:
        """生成决策建议"""
        if mode == 'short':
            if score >= 80:
                return '强烈推荐买入（尾盘）'
            elif score >= 65:
                return '建议买入（尾盘）'
            elif score >= 50:
                return '可关注，条件不足'
            else:
                return '不推荐'
        else:
            if score >= 80:
                return '强烈推荐建仓'
            elif score >= 65:
                return '建议分批建仓'
            elif score >= 50:
                return '可加入观察池'
            else:
                return '不推荐'

    def _generate_reasoning(self, stock_data: Dict, breakdown: Dict,
                            mode: str = 'short') -> str:
        """生成选股理由 — 一段话+关键数据"""
        parts = []

        # 资金面
        main_fund = stock_data.get('main_fund_accumulated', 0)
        if main_fund is not None and abs(main_fund) > 0:
            direction = '净流入' if main_fund > 0 else '净流出'
            parts.append(
                f"主力资金近10日{direction}约{abs(main_fund)/1e4:.0f}万元"
                if abs(main_fund) >= 1e4 else
                f"主力资金近10日{direction}{abs(main_fund):.0f}元"
            )
        elif main_fund is None:
            parts.append("主力资金接口当日不可用（已降权处理）")

        # 北向资金
        north = stock_data.get('north_flow_accumulated', 0)
        if north is not None and abs(north) > 0:
            nd = '净买入' if north > 0 else '净卖出'
            parts.append(f"北向资金近10日{nd}约{abs(north)/1e4:.0f}万元"
                         if abs(north) >= 1e4 else
                         f"北向资金近10日{nd}{abs(north):.0f}元")
        elif north is None:
            parts.append("北向资金接口当日不可用（已降权处理）")

        # 动量
        rps = stock_data.get('rps_20')
        if rps:
            parts.append(f"RPS 20日排位约{float(rps):.0f}分位" if not isinstance(rps, (int, float)) or rps <= 100
                         else f"短期涨幅强劲，RPS处于较高分位")

        # 技术面
        macd = stock_data.get('macd_status', {})
        if isinstance(macd, dict) and macd.get('status'):
            status_map = {
                'golden_cross': 'MACD金叉',
                'bullish': 'MACD多头排列',
                'death_cross': 'MACD死叉',
                'bearish': 'MACD空头排列',
            }
            status_cn = status_map.get(macd['status'], '')
            if status_cn:
                parts.append(status_cn)

        # 量价
        vol_ratio = stock_data.get('volume_ratio')
        if vol_ratio:
            ratio_desc = '放量' if vol_ratio > 1.2 else '缩量' if vol_ratio < 0.8 else '量能适中'
            parts.append(f"{ratio_desc}（量比{vol_ratio:.1f}）")

        # 热点题材
        if stock_data.get('is_hot_stock', False):
            blocks = stock_data.get('blocks', {})
            tags = blocks.get('concept_tags', []) if blocks else []
            if tags:
                parts.append(f"热门题材归属：{'、'.join(tags[:5])}")
            else:
                parts.append("同花顺强势股，有题材归因标签")

        # 龙虎榜
        dt = stock_data.get('dragon_tiger', {})
        if dt and dt.get('records'):
            latest = dt['records'][0]
            inst = dt.get('institution', {})
            parts.append(f"龙虎榜上榜：净买入{latest.get('net_buy_wan', 0):.0f}万"
                         f"{'，机构净买入' + str(inst.get('net_amt', 0)) + '万' if inst.get('net_amt', 0) > 0 else ''}")

        # 评分汇总
        if breakdown:
            top_factor = max(breakdown.items(),
                           key=lambda x: x[1].get('weighted', 0))
            parts.append(f"最大贡献因子：{top_factor[0]}（{top_factor[1].get('raw_score', 0):.0f}分）")

        # 组合成段落
        if parts:
            reasoning = "；".join(parts) + "。"
        else:
            reasoning = "数据不足，评分仅供参考。"

        # 加上品质评价
        risk = stock_data.get('risk_check', {})
        if isinstance(risk, dict) and risk.get('passed', True):
            quality = "基本面稳健" if mode == 'long' else "流动性良好，风险可控"
            reasoning += f"整体{quality}，具备"

        price = stock_data.get('price', 0)
        if mode == 'short':
            reasoning += (f"短线交易价值。"
                         f"目标{price*1.02:.2f}（+2%止盈），止损{price*0.98:.2f}（-2%止损）。")
        else:
            reasoning += (f"中长期配置价值。"
                         f"目标{price*1.15:.2f}（+15%），止损{price*0.92:.2f}（-8%）。")

        return reasoning

    def rank_stocks(self, stocks_data: List[Dict], mode: str = 'short',
                    top_n: int = 3, min_score: float = 60) -> List[Dict]:
        """
        批量选股评分 + 排序

        参数：
          stocks_data: 股票数据列表
          mode: 'short' / 'long'
          top_n: 最多返回N只
          min_score: 最低评分阈值

        返回：评分降序排列的推荐列表
        """
        # 横截面排名：capital_flow 用百分位替代绝对值评分，避免全员满分
        # 收集全部有效 main_fund_accumulated，在 200 只内排百分位
        main_values = [
            s.get('main_fund_accumulated')
            for s in stocks_data
            if s.get('main_fund_accumulated') is not None
            and abs(s['main_fund_accumulated']) > 0
        ]
        if main_values:
            for s in stocks_data:
                mv = s.get('main_fund_accumulated')
                if mv is not None and abs(mv) > 0:
                    # 百分位 = 有多少股票 <= 该值 / 总数
                    rank = sum(1 for v in main_values if v <= mv) / len(main_values)
                    s['_capital_flow_percentile'] = rank

        results = []
        for stock in stocks_data:
            # 风控前置检查
            risk_check = stock.get('risk_check', {})
            if isinstance(risk_check, dict) and not risk_check.get('passed', True):
                penalty = risk_check.get('score_penalty', 1.0)
                if penalty >= 0.8:
                    continue  # 严重风险，直接跳过

            result = self.score_stock(stock, mode)
            results.append(result)

        # 按评分降序
        results.sort(key=lambda x: x['score'], reverse=True)

        # 过滤最低分并限制数量
        qualified = [r for r in results if r['score'] >= min_score]

        # 防凑数：如果第3名与第1名分差超过20分，裁掉尾巴
        if len(qualified) >= 3:
            top_score = qualified[0]['score']
            # 从最后一名往前裁，直到差距合理
            while len(qualified) >= 2 and qualified[-1]['score'] < top_score - 20:
                qualified.pop()
        # 如果只剩1只且评分很好，也是合理结果（不硬凑到3只）

        return qualified[:top_n]
