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

import numpy as np
import pandas as pd

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

    # 默认权重（fallback 兜底，2026-09-06 审查修复：与 data/weights/v1.json
    # 对齐。原先 DEFAULT 与 v1.json 严重矛盾（hot_theme 0.10 vs 0.50），
    # 一旦 v1.json 丢失会静默回退到 DEFAULT 导致选股行为 5× 漂移）。
    # 真正生效权重以 get_weights() 为准：v1.json > config.yml > 此处兜底。
    # risk 不进权重表，由 penalty 路径独立扣分（见 ActiveWeight 与 penalty 注释）
    DEFAULT_WEIGHTS = {
        'short': {
            # 2026-09-19 方案G（保守混合，用户决策后定稿，与 v1.json/config.yml 同步）：
            # 口径 = ret_hold1d（尾盘买入，与实盘一致）。
            # 证据：2024-01~2026-09 全市场 OOS，Top5 日均超额 +1.845%（t=21.4）
            # vs 旧基线（hot/rev 等权）+0.850%（t=9.4）。训练/持有切分寻优（500 组
            # 随机搜索 + 坐标精修）持有期最优 +1.754% 未显著超越 G → G 已在持有期前沿。
            # （2026-09-21 更正：原注释写 1.799%，与 config.yml / SKILL.md / README 的
            #  1.754% 不一致。1.754% 是"训练期最优解在持有期的收益"，1.799% 是
            #  "坐标精修训练期最优解在持有期的收益"，两者是不同口径。）
            # 核心增量 = liq_dev（缩量偏离）0.14 / vol_dev（波动收敛偏离）0.07。
            # 噪声腿保留 0.08（用户决策"不归零"，实证代价 0.031 pp/日）。
            'capital_flow': 0.01,
            'north_flow': 0.00,   # 2024-08 起北向官方停发，仅东财估算口径
            'momentum': 0.02,
            'technical': 0.02,
            'volume_price': 0.02,
            'hot_theme': 0.55,
            'liq_dev': 0.14,
            'reversal_20d': 0.10,
            'vol_dev': 0.07,
            'volatility': 0.06,
            'dragon_tiger': 0.01,
            # 2026-09-21 补登记（守卫测试 test_default_weights_keys_match_v1）：
            # 以下两个零权重键此前只登记在 v1.json 与 config.yml，fallback 层
            # 缺键。`_weights_equivalent` 把缺键当作 0，所以三层"等价性"测试
            # 一直通过 —— 但 v1.json 一旦丢失/被清空，回落到本层后这些因子
            # 从"显式权重 0"退化为"未登记"，会走 factors.get 的默认 50 静默
            # 路径，与"零权重链路先通、≥60 交易日后再审批加权"的约定不对称。
            'size': 0.00,      # 小市值（估值快照积累 ≥60 交易日后 OOS 验证）
            'liquidity': 0.00,  # 原始流动性（已证伪：84% 是小市值效应）
        },
        'long': {
            'fundamental': 0.40,
            'north_flow': 0.00,
            'momentum': 0.25,
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
            # 2026-09-14 整体审查 P3 附带修复：原比较 `weights != loaded` 恒为真——
            # loaded 是 {short:…, long:…} 嵌套结构、weights 是扁平的单一模式权重，
            # 两者永不相等 → 该告警长期是假阳性（被文档列为"已知无害告警"）。
            # 改为语义等价比较（缺失键视为 0、容差 1e-9），使告警只在**真不一致**时出现。
            if weights and not any(
                    self._weights_equivalent(weights, v)
                    for v in loaded.values() if isinstance(v, dict)):
                logger.warning("v1.json 权重与 config.yml 不一致！config 权重被忽略。")
            logger.info(f"权重从 {model_version}.json 加载")
        elif weights:
            self.weights = weights
            logger.info(f"权重从 config 加载（v1.json 尚不存在）")
        else:
            self.weights = self.DEFAULT_WEIGHTS
            logger.info("权重从 DEFAULT_WEIGHTS 加载")

    @staticmethod
    def _weights_equivalent(a: Dict, b: Dict, tol: float = 1e-9) -> bool:
        """权重语义等价比较（2026-09-14 整体审查新增）。

        用于判断"config.yml 传入的权重"与"v1.json 中某一模式的权重"是否一致：
          - 缺失键视为 0（v1.json 不收录零权重因子，config.yml 会显式写 0.00）；
          - None / 非数值一律按 0 处理；
          - 浮点比较带容差。
        """
        keys = set(a) | set(b)
        try:
            return all(
                abs(float(a.get(k) or 0) - float(b.get(k) or 0)) <= tol
                for k in keys
            )
        except (TypeError, ValueError):
            return False

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
        # P2-K hook（2026-09-05 审查报告）：横截面标准化由 rank_stocks 批量注入
        # _factor_scores（缓存批量现算结果）+ _factors_override（标准化后的分值）。
        # 仅当 config short_term.sell.standardize=true 时注入；单股直调路径不受影响。
        factors = stock_data.get('_factor_scores') \
            or self.factor_lib.compute_all_factors(stock_data, mode)
        override = stock_data.get('_factors_override')
        if override:
            for k, v in override.items():
                if k in factors and v is not None:
                    factors[k] = v

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
            # 通达信新因子（2026-09-05 新增）
            if factor_name == 'valuation_fundamental' and stock_data.get('fundamentals') is None:
                is_neutral = True
            if factor_name == 'event_catalyst' and not stock_data.get('recent_events'):
                is_neutral = True
            # 小市值（2026-09-18 R-B）：无市值数据 → 数据不可用（权重让渡）
            if factor_name == 'size' and stock_data.get('total_market_cap') is None:
                is_neutral = True
            # 热点题材（2026-09-05 审查 P1-2）：hot_theme 依赖 is_hot_stock /
            # blocks / concept_names 三源。三者全部缺失（回测快照无热点历史、
            # 或实盘字段意外丢失）→ 数据不可用，权重让渡给活跃因子。
            # 注意区分"查过但不是热点"（is_hot_stock=False 存在）——那是真实
            # 信号，不算缺失，否则实盘 hot_theme 永远失效。
            if factor_name == 'hot_theme' \
                    and 'is_hot_stock' not in stock_data \
                    and not stock_data.get('blocks') \
                    and not stock_data.get('concept_names'):
                is_neutral = True

            # 2026-09-19 补（全项目深查 P1-1）：K 线派生的四个新启用因子
            # （liq_dev / vol_dev / volatility / liquidity）此前不在白名单里，
            # 缺失时 factor_library 返回的中性 50 被当作**真实数据**计入权重，
            # 与 OOS 验证口径（NaN 剔除，缺数据行不进 IC）不一致 ——
            # 与"专家失明"同构：缺失数据伪装成有效信号参与加权。
            # 判定依据 = 原始百分位是否写入（由 rank_stocks 计算）：
            #   有百分位 → 真实横截面值（0-100）可参与排序
            #   无百分位 → 数据不足（K 线行数不够 / 快照模式无行情）→ 让渡权重
            _peri_map = {
                'liq_dev': '_liq_dev_percentile',
                'vol_dev': '_vol_dev_percentile',
                'volatility': '_volatility_percentile',
                'liquidity': '_liquidity_percentile',
                'size': '_size_percentile',
            }
            if factor_name in _peri_map \
                    and stock_data.get(_peri_map[factor_name]) is None:
                is_neutral = True

            # momentum / reversal_20d 的数据源是 rps_20，但 data_engine 在无 K 线时
            # **返回恒 50 而非 None**（data_engine.py:1272），因此 rps_20 无法作为
            # 可用性判据（用 None 判断永不触发，会给出虚假的安全感）。
            # 正确修法是改 data_engine 返回 None 或按 kline_df 存在性判定 ——
            # 那会改变动量腿（权重 0.12）在实盘/回测两条路径上的行为，属未经 OOS
            # 验证的语义变更，故**本轮不动**，记为遗留项（见深查报告 P2-4）。
            # 影响评估：无 K 线股票（无成交量/停牌）在候选池中占比很低，且
            # raw_return_20=0 会落到横截面中位附近，偏差有界。

            factor_scores[factor_name] = (factor_score, weight, is_neutral)
            if is_neutral:
                neutral_weight += weight

        # 活跃因子权重总和（risk 不在内）
        # 改进：用 weights 实际总和作为分母，避免 config 删/加键时分母硬编码 1.0 漂移
        weights_total = sum(weights.values()) if weights else 1.0
        active_weight = weights_total - neutral_weight

        for factor_name, (factor_score, weight, is_neutral) in factor_scores.items():
            if is_neutral:
                # 数据不可用：权重全部让渡给活跃因子，自身不贡献分数。
                # （此前实现同时"按原权重×50 计入"+"active 因子吸收权重"= 双重计权，
                #   权重总和变成 1+neutral_weight，数据缺失时分数反而虚高）
                breakdown[factor_name] = {
                    'raw_score': round(factor_score, 2),
                    'weight': weight,
                    'weighted': 0.0,
                    'effective_weight': 0.0,
                    'data_available': False,
                    'note': '接口不可用，权重已让渡给活跃因子'
                }
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

        # 防御性兜底：active_weight <= 0（所有加权因子都数据不可用）时给中性 50，
        # 避免 weighted_score=0 → 总分 0。当前权重下不会触发（capital_flow/north_flow
        # 之外总有数据），但保留以防未来权重调整引入全 neutral 场景。
        if active_weight <= 0:
            weighted_score = 50.0

        # === Risk penalty（risk 改成纯扣分项，不进加权） ===
        # 设计：risk_filter 已经把 ST/解禁压力/成交额过低/涨停封死的票 in-pass 直接拦。
        # 留到打分阶段的票，可能仍有软风险（成交额偏低、换手率偏高、量比异常等），
        # 这些 penalty<0.8 的"软风险"通过这里在总分上扣减。
        risk_check = stock_data.get('risk_check', {})
        risk_penalty = 0.0
        risk_note = ''
        hard_block = False
        if risk_check:
            if not risk_check.get('passed', True):
                # 硬拦截（2026-09-05 审查 P2-6 统一语义）：risk_filter 保证
                # 未通过 ⇒ penalty ≥ 0.8。旧实现把硬失败也按 ×0.5 折减，
                # 与 rank_stocks 的"直接淘汰"不一致 —— 同一只票走批量路径
                # 被剔除、走单票路径却还能拿 ~57 分。统一为硬失败 = 0 分。
                hard_block = True
                risk_note = 'risk_check 未通过（硬拦截）'
            else:
                # passed=True 但 score_penalty 不为 0：软风险扣分
                # penalty < 0.4 → ×0.2（轻度软风险）
                # penalty ≥ 0.4 → ×0.5（较高风险如低成交额/高换手）
                # 分段系数让轻度软风险（量比异常 0.3）不受太大影响，
                # 同时让高风险的惩罚力度拉开差距
                risk_penalty = risk_check.get('score_penalty', 0)
                if risk_penalty > 0:
                    risk_note = f"软风险扣分(penalty={risk_penalty})"

        if hard_block:
            score = 0.0
        else:
            risk_coeff = 0.2 if risk_penalty < 0.4 else 0.5
            score = weighted_score * (1 - risk_penalty * risk_coeff)

        # 追高惩罚（2026-09-07 P1 论证后实施，子代理结论：做成风控覆盖项而非
        # 独立加权因子——独立因子会与 hot_theme 0.50 重复计数）。
        # 华泰月度跟踪证实 A 股短线呈反转效应（沪深300 池 1 个月反转 IC 27.69%）：
        # 当日涨幅 >7% 的票次日均值回归风险高。惩罚 = clip((pct-7)/(9.5-7),0,1)×0.5，
        # 即 7%→0%、9.5% 及以上→最多砍 50%。factor_raw 实测仅 1.2% 候选票
        # 涨幅>7%，正常场景几乎不触发（风险覆盖而非常规打分）。
        chase_pct = stock_data.get('pct_chg')
        try:
            chase_pct = float(chase_pct) if chase_pct is not None else None
        except (TypeError, ValueError):
            chase_pct = None
        if mode == 'short' and chase_pct is not None and chase_pct > 7.0:
            chase_penalty = min((chase_pct - 7.0) / 2.5, 1.0) * 0.5
            score *= (1 - chase_penalty)
            logger.info(f"追高惩罚 {stock_data.get('code')}: 当日涨幅 {chase_pct:.1f}% "
                        f"→ 分数 ×{1-chase_penalty:.2f}")

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
            # P2-N（2026-09-05 审查报告）：波动率自适应止损。
            # stop_mode='atr' → 止损距离 = max(atr_mult × ATR14, 固定止损距离)：
            # 高波动票止损更宽（减少噪音止损），下限仍不低于固定止损距离。
            # 默认 'fixed' 保持历史行为。ATR 缺失（无 K 线）回退固定止损。
            if self.sell_config.get('stop_mode', 'fixed') == 'atr':
                atr = self._atr14(stock_data)
                if atr is not None and price > 0:
                    sl_dist = max(self.sell_config.get('atr_mult', 1.0) * atr, sl * price)
                    stop_price = round(price - sl_dist, 2)
                else:
                    stop_price = round(price * (1 - sl), 2)
            else:
                stop_price = round(price * (1 - sl), 2)
            target_price = round(price * (1 + tp), 2)
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
            'risk_blocked': hard_block,
        }

    @staticmethod
    def _atr14(stock_data: Dict) -> Optional[float]:
        """从 kline_df 计算 14 日 ATR（TR 简单均值）。

        P2-N 配套：stop_mode='atr' 时的波动率输入。无 K 线或行数 <15 时
        返回 None（调用方回退固定止损）——缺失显式处理，不用猜测值。
        """
        df = stock_data.get('kline_df')
        if df is None or len(df) < 15:
            return None
        try:
            import pandas as pd
            high = df['high'].astype(float)
            low = df['low'].astype(float)
            close = df['close'].astype(float)
            prev_close = close.shift(1)
            tr = pd.concat([high - low,
                            (high - prev_close).abs(),
                            (low - prev_close).abs()], axis=1).max(axis=1)
            atr = float(tr.tail(14).mean())
            return atr if atr > 0 else None
        except Exception:
            return None

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

        # 龙虎榜（2026-08-14 修正：上榜≠利好，只展示机构真金白银方向）
        dt = stock_data.get('dragon_tiger', {})
        if dt and dt.get('records'):
            inst = dt.get('institution', {})
            inst_net = inst.get('net_amt', 0)
            if inst_net > 0:
                parts.append(f"龙虎榜机构净买入{inst_net:.0f}万")
            elif inst_net < 0:
                parts.append(f"龙虎榜机构净卖出{abs(inst_net):.0f}万")
            else:
                parts.append("龙虎榜上榜（无机构净买入）")

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
            # 从 sell_config 读取，避免 config.yml 调整后简报文案与实际参数脱节
            # （此前此处硬编码 1.02 / 0.98，与 score_stock 中的 sell_config 不一致）
            tp = self.sell_config.get('take_profit', 0.02)
            sl = abs(self.sell_config.get('stop_loss', -0.02))
            reasoning += (f"短线交易价值。"
                         f"目标{price*(1+tp):.2f}（+{tp*100:.0f}%止盈），"
                         f"止损{price*(1-sl):.2f}（-{sl*100:.0f}%止损）。")
        else:
            # 长线止盈止损：config.yml 未提供 long_term.sell 段，沿用既有约定值
            reasoning += (f"中长期配置价值。"
                         f"目标{price*1.15:.2f}（+15%），止损{price*0.92:.2f}（-8%）。")

        return reasoning

    def rank_stocks(self, stocks_data: List[Dict], mode: str = 'short',
                    top_n: int = 3, min_score: float = 60,
                    diagnostics: Dict = None) -> List[Dict]:
        """
        批量选股评分 + 排序

        参数：
          stocks_data: 股票数据列表
          mode: 'short' / 'long'
          top_n: 最多返回N只
          min_score: 最低评分阈值
          diagnostics: 可选出参 dict。调用方传入后，若全部候选被门槛过滤清零，
                       会写入 top_unqualified（最高分标的的 code/name/score/
                       min_score/gap/candidates），供简报在"无推荐"时展示。
                       不传时行为与历史实现完全一致。

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

        # R-B（2026-09-18）：小市值因子横截面百分位——市值越小分越高。
        # 依据 study-vault F09 洞察（小市值 = A 股最稳健截面因子，现有 9 因子
        # 无规模维度）。权重 0 链路先行：OOS 面板（kline 构建）无市值数据，
        # 验证条件 = 市值历史快照积累 ≥60 交易日后跑 OOS，再由 calibrate_weights
        # 审批加权（与 valuation_fundamental / event_catalyst 同一启用契约）。
        mcap_values = [s.get('total_market_cap') for s in stocks_data
                       if s.get('total_market_cap')]
        if mcap_values:
            for s in stocks_data:
                mv = s.get('total_market_cap')
                if mv:
                    # 大市值秩（市值<=该值占比）取反 → 小市值百分位
                    big_rank = sum(1 for v in mcap_values if v <= mv) / len(mcap_values)
                    s['_size_percentile'] = 1.0 - big_rank

        # R-C（2026-09-18 对标 Barra Liquidity）：流动性因子横截面百分位——
        # 量比与换手率的均值百分位（高流动性=高换手=高分）。
        # 权重 0 链路先行（对标 Barra Liquidity 因子，当前缺失维度）。
        # 启用条件：OOS IC 确认 + calibrate 审批（与 size 同一契约）。
        liq_vals = []
        for s in stocks_data:
            vr = s.get('volume_ratio')
            to = s.get('turnover')
            if vr and vr > 0 and to is not None:
                try:
                    liq_vals.append((s, float(vr) * float(to)))
                except (TypeError, ValueError):
                    pass
        if liq_vals:
            liq_sorted = sorted(v for _, v in liq_vals)
            n = len(liq_sorted)
            for s, combined in liq_vals:
                rank = sum(1 for _, v in liq_vals if v <= combined) / n
                s['_liquidity_percentile'] = rank

        # R-D（2026-09-18 对标 Barra Volatility）：波动率因子——20 日日收益率标准差
        # 横截面百分位取反（低波动=高分，Barra 低波动溢价）。
        # 由 kline 数据计算，rank_stocks 已有 kline_df 可算。
        for s in stocks_data:
            kline = s.get('kline_df')
            if kline is not None and isinstance(kline, pd.DataFrame) and len(kline) >= 20:
                try:
                    close = kline['close'].astype(float)
                    rets = close.pct_change().dropna()
                    if len(rets) >= 20:
                        vol_20d = float(rets.tail(20).std())
                        if vol_20d > 0:
                            s['_volatility_raw'] = vol_20d
                except Exception:
                    pass
        vol_raws = [(s, s['_volatility_raw']) for s in stocks_data
                    if '_volatility_raw' in s]
        if vol_raws:
            sorted_vols = sorted(v for _, v in vol_raws)
            n_v = len(sorted_vols)
            for s, v in vol_raws:
                rank = sum(1 for _, x in vol_raws if x <= v) / n_v
                s['_volatility_percentile'] = rank  # 高波动 → 高百分位 → 低分

        # R-E（2026-09-19 方案G·OOS 实证）：缩量偏离因子——当日成交额相对自身常态的偏离。
        # 证据（2024-01~2026-09，299.9万行/647天/5225只，ret_hold1d，剔除接近涨停）：
        #   liq_dev IC +0.0654 / ICIR 0.535 / t +12.81，与规模代理（60日滚动中位数）
        #   横截面相关仅 -0.088 → 与小市值效应正交，是独立 alpha（见
        #   docs/因子扩容与权重论证_20260918.md 第二章）。
        # 方向：偏离越低（当日缩量/地量）→ 分越高（在 factor_library 取反）。
        # 注意与 _liquidity_percentile（量比×换手，高流动性高分）方向相反且口径不同：
        # 原始 liquidity 的 OOS 证据属于 log(amount) 口径，84% 是规模效应（相关 0.838），
        # 不能反向套用到 turnover 乘积口径 —— 故 liquidity 权重保持 0 不动，不加不反。
        liq_raws = []
        for s in stocks_data:
            kline = s.get('kline_df')
            if (kline is not None and isinstance(kline, pd.DataFrame)
                    and 'amount' in kline.columns and len(kline) >= 30):
                try:
                    amt = np.log1p(
                        pd.to_numeric(kline['amount'], errors='coerce').clip(lower=0).dropna())
                    if len(amt) >= 30:
                        # 与 OOS 面板同口径：60 日窗口含当日，min 30 日
                        level = float(amt.tail(60).median())
                        dev = float(amt.iloc[-1] - level)
                        if np.isfinite(dev):
                            s['_liq_dev_raw'] = dev
                            liq_raws.append((s, dev))
                except Exception:
                    pass
        if liq_raws:
            n_liq = len(liq_raws)
            for s, dev in liq_raws:
                rank = sum(1 for _, x in liq_raws if x <= dev) / n_liq
                s['_liq_dev_percentile'] = rank  # 高偏离（放量）→ 高百分位 → 低分

        # R-F（2026-09-19 方案G·OOS 实证）：波动收敛偏离因子——20 日波动率相对自身
        # 60 日常态的偏离。证据：vol_dev IC +0.0263 / ICIR 0.225 / t +5.37，
        # 偏离成分优于水平成分（vol_level ICIR 仅 0.126）。方向：偏离越低
        # （波动收敛）→ 分越高。
        vol_dev_raws = []
        for s in stocks_data:
            kline = s.get('kline_df')
            if (kline is not None and isinstance(kline, pd.DataFrame)
                    and 'close' in kline.columns and len(kline) >= 50):
                try:
                    close = pd.to_numeric(kline['close'], errors='coerce').dropna()
                    rets = close.pct_change()
                    vol_series = rets.rolling(20).std().dropna()
                    if len(vol_series) >= 30:
                        level = float(vol_series.tail(60).median())
                        dev = float(vol_series.iloc[-1] - level)
                        if np.isfinite(dev):
                            s['_vol_dev_raw'] = dev
                            vol_dev_raws.append((s, dev))
                except Exception:
                    pass
        if vol_dev_raws:
            n_vd = len(vol_dev_raws)
            for s, dev in vol_dev_raws:
                rank = sum(1 for _, x in vol_dev_raws if x <= dev) / n_vd
                s['_vol_dev_percentile'] = rank  # 高偏离（波动放大）→ 高百分位 → 低分

        # P2-K（2026-09-05 审查报告）：横截面因子标准化（实验开关）。
        # 批量预计算因子分 → 每个因子横截面 rank 0-100 → 写入 _factors_override，
        # score_stock 的 hook 会用其覆盖原始因子分。默认关闭（保持历史行为）；
        # 启用后必须重跑 --mode oos 与 calibrate_weights（口径变化）。
        if self.sell_config.get('standardize', False):
            self._apply_factor_standardization(stocks_data, mode)

        results = []
        for stock in stocks_data:
            # 风控前置检查
            risk_check = stock.get('risk_check', {})
            if isinstance(risk_check, dict) and not risk_check.get('passed', True):
                penalty = risk_check.get('score_penalty', 1.0)
                if penalty >= 0.8:
                    continue  # 严重风险，直接跳过

            result = self.score_stock(stock, mode)
            # 附带全字段源 dict 引用：下游补算（如 blocks/dragon_tiger 补全后重算）
            # 需要完整因子输入，瘦 result 缺 main_fund_accumulated/rps_20/macd_status 等
            result['_src'] = stock
            results.append(result)

        # 按评分降序
        results.sort(key=lambda x: x['score'], reverse=True)

        # 过滤最低分并限制数量
        qualified = [r for r in results if r['score'] >= min_score]

        # 可观测性（2026-09-07 评分审查）：全部被门槛过滤时，记录最高分与
        # 差距——否则"没有评分达标"无法区分"没有好票"与"门槛过高"
        if not qualified and results:
            top = results[0]
            logger.warning(
                f"min_score 过滤清零: 最高分 {top['score']:.1f}（{top.get('code')} "
                f"{top.get('name', '')}），距门槛 {min_score} 差 {min_score - top['score']:.1f} 分"
                f"（候选 {len(results)} 只）")
            # 2026-09-14：把"最高分标的"外送给调用方——简报在无推荐时展示它，
            # 用于区分"市场确实没有好票"与"门槛相对当前评分尺度偏高"。
            # diagnostics=None（默认）时不产生任何副作用，回测等调用方不受影响。
            if diagnostics is not None:
                _top_score = float(top.get('score', 0.0))
                diagnostics['top_unqualified'] = {
                    'code': top.get('code', ''),
                    'name': top.get('name', ''),
                    'score': round(_top_score, 2),
                    'min_score': round(float(min_score), 2),
                    'gap': round(float(min_score) - _top_score, 2),
                    'candidates': len(results),
                }

        # 防凑数：如果第3名与第1名分差超过20分，裁掉尾巴
        if len(qualified) >= 3:
            top_score = qualified[0]['score']
            # 从最后一名往前裁，直到差距合理
            while len(qualified) >= 2 and qualified[-1]['score'] < top_score - 20:
                qualified.pop()
        # 如果只剩1只且评分很好，也是合理结果（不硬凑到3只）

        return qualified[:top_n]

    def _apply_factor_standardization(self, stocks_data: List[Dict],
                                      mode: str) -> None:
        """P2-K：对当日候选池批量做横截面因子标准化（原地写入 stock dict）。

        流程：批量现算因子分（缓存到 _factor_scores，score_stock 直接复用，
        不重复计算）→ 逐因子横截面 rank 0-100 → 写入 _factors_override。
        样本 <5 的因子跳过（标准化无意义）；NaN/None 不参与、保持原中性逻辑。
        """
        from core.factor_standardizer import cross_sectional_standardize
        for s in stocks_data:
            try:
                s['_factor_scores'] = self.factor_lib.compute_all_factors(s, mode)
            except Exception as e:
                logger.warning(f"{s.get('code')} 标准化预计算失败（该股走原始分）: {str(e)[:60]}")
                s['_factor_scores'] = {}
        if not stocks_data:
            return
        factor_names = list(stocks_data[0].get('_factor_scores', {}).keys())
        # 修复（2026-09-06 审查）：risk 不是加权因子（penalty 路径独立处理），
        # 恒中性值进标准化只会制造虚假名次，排除
        factor_names = [f for f in factor_names if f != 'risk']
        n_std = 0
        for fname in factor_names:
            vals = {}
            for s in stocks_data:
                v = (s.get('_factor_scores') or {}).get(fname)
                if v is not None:
                    try:
                        fv = float(v)
                        if fv == fv:  # NaN 过滤
                            vals[str(s.get('code'))] = fv
                    except (TypeError, ValueError):
                        # 2026-09-18 审查 P2-7：因子值不可解析会静默减少参与标准化的
                        # 样本量（影响因子分位），留痕便于发现上游字段漂移。
                        logger.debug(f"因子 {fname} 值不可解析，已跳过: {v!r}")
            if len(vals) < 5:
                continue
            std = cross_sectional_standardize(vals, method='rank')
            for s in stocks_data:
                code = str(s.get('code'))
                if std.get(code) is not None:
                    s.setdefault('_factors_override', {})[fname] = std[code]
            n_std += 1
        logger.info(f"横截面因子标准化启用: {n_std}/{len(factor_names)} 个因子 "
                    f"已按候选池 rank 0-100 重排（standardize=true）")
