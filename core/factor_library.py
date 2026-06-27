"""
因子库 — 短线/长线评分因子计算

因子分类（实际接入打分链路的）：
  资金面 (3)  — 主力资金、北向资金、机构动向
  动量   (2)  — RPS 排位、机构资金面
  量价   (2)  — 量比、换手率
  技术面 (1)  — 通过 TechnicalScorer 综合分
  热点   (2)  — 同花顺强势股 + 龙虎榜

基础面 (长线，4)  — fundamental/valuation/institutional/risk

参考来源：
  - Sequoia-X 各策略文件（RPS 排位、均线金叉）
  - daily-stock StockTrendAnalyzer（技术分析器）
  - a-stock-data tencent_quote（估值数据）

说明：calc_rsi / calc_bollinger_position / calc_kdj_status / calc_ma_bias 等
技术分析子函数已统一在 core/technical_scorer.py 实现，FactorLibrary 不再
重复实现。如果未来要给某策略用中间变量，可在那里调用 TechnicalScorer。
"""

import numpy as np
import pandas as pd
from typing import Dict


class FactorLibrary:
    """因子计算库 — 所有因子计算函数集中管理"""

    # ========== 资金面因子 ==========

    @staticmethod
    def calc_main_fund_score(accumulated_net: float, days: int = 10) -> float:
        """
        主力资金评分（0-100）

        参考：akshare stock_individual_fund_flow()
        主力净流入累计为正 → 高分，负 → 低分

        注意：当数据源为大单交易汇总（单日）时，
        days 参数无效，直接用单日净流入评分。
        """
        if accumulated_net is None:
            return 50.0  # 无数据，中性

        # 大单缓存数据（单日），用单日净流入评分
        # 净流入 > 300万 → 高分，净流出 > 300万 → 低分
        if days is None or days == 0:
            score = 50 + (accumulated_net / 300_0000) * 50
            return np.clip(score, 0, 100)

        # akshare 多日累计数据
        daily_avg = accumulated_net / days
        # 映射到 0-100，日均流入 > 500万 得高分
        score = 50 + (daily_avg / 500_0000) * 50
        return np.clip(score, 0, 100)

    @staticmethod
    def calc_north_flow_score(accumulated_net: float, days: int = 10) -> float:
        """
        北向资金评分（0-100）
        北向持续净买入 → 高分

        注意：accumulated_net 来自 asharehub northbound_holdings，
        单位是"持股量变化（股数）"，而非金额（元）。
        用 days 天数做加权日均后评分，横截面百分位会稀释偏差。
        """
        if accumulated_net is None or days == 0:
            return 50.0  # 北向不可用，中性降权（与主力资金保持一致）
        avg_per_day = accumulated_net / days
        score = 50 + (avg_per_day / 100_0000) * 50
        return np.clip(score, 0, 100)

    # ========== 动量因子 ==========

    @staticmethod
    def calc_rps(close_series: pd.Series, period: int = 20) -> pd.Series:
        """
        RPS 动量排位（Relative Price Strength）

        参考：Sequoia-X rps_breakout.py line 31-39
        用法：close_series 是某时间截面所有股票的涨幅
        """
        returns = close_series.pct_change(period)
        rps = returns.rank(pct=True) * 100
        return rps

    def calc_rps_score(self, rps_value: float) -> float:
        """RPS 值 → 评分（0-100）"""
        return float(np.clip(rps_value, 0, 100))

    # ========== 技术形态因子 ==========
    # MACD 状态判断作为简易回退（首选 TechnicalScorer，已在 scoring_model 里处理）
    @staticmethod
    def calc_macd_status(close: pd.Series, fast: int = 12, slow: int = 26,
                         signal_period: int = 9) -> Dict:
        """
        MACD 状态判断

        参考：daily-stock StockTrendAnalyzer
        返回：金叉/死叉、柱线方向、DIF/DEA差值
        """
        if len(close) < slow + signal_period:
            return {'status': 'unknown', 'strength': 0}

        exp1 = close.ewm(span=fast, adjust=False).mean()
        exp2 = close.ewm(span=slow, adjust=False).mean()
        dif = exp1 - exp2
        dea = dif.ewm(span=signal_period, adjust=False).mean()
        macd_hist = (dif - dea) * 2

        current_dif = dif.iloc[-1]
        current_dea = dea.iloc[-1]
        prev_dif = dif.iloc[-2]
        prev_dea = dea.iloc[-2]

        # 金叉判断
        if prev_dif < prev_dea and current_dif > current_dea:
            status = 'golden_cross'
            strength = abs(current_dif - current_dea) / close.iloc[-1] * 100
        elif prev_dif > prev_dea and current_dif < current_dea:
            status = 'death_cross'
            strength = -abs(current_dif - current_dea) / close.iloc[-1] * 100
        elif current_dif > current_dea:
            status = 'bullish'
            strength = abs(macd_hist.iloc[-1]) / close.iloc[-1] * 100
        else:
            status = 'bearish'
            strength = -abs(macd_hist.iloc[-1]) / close.iloc[-1] * 100

        # MACD分值：金叉+强多=高分，死叉=低分
        status_scores = {
            'golden_cross': 80, 'bullish': 65,
            'unknown': 50, 'bearish': 35, 'death_cross': 20
        }
        score = status_scores.get(status, 50)
        # 用强度微调
        score += np.clip(strength * 2, -15, 15)

        return {
            'status': status,
            'score': np.clip(score, 0, 100),
            'dif': current_dif,
            'dea': current_dea,
            'histogram': macd_hist.iloc[-1] if len(macd_hist) > 0 else 0,
            'strength': strength
        }

    # ========== 量价因子 ==========

    @staticmethod
    def calc_volume_ratio_score(volume_ratio: float) -> float:
        """
        量比评分（连续分段线性函数）

        设计原理：
          量比 0.8-2.0 是健康放量区间，评分最高
          缩量（< 0.3）→ 无人问津，低分
          异常放量（> 5）→ 可能有出货风险，低分
          峰值在量比 1.4 附近（温和放量+价格上涨的组合最理想）

        锚点: (0.3, 25) → (0.8, 65) → (1.0, 75) → (1.4, 85)
              → (3.0, 65) → (5.0, 40) → (15.0, 15)
        """
        if volume_ratio is None or volume_ratio <= 0:
            return 50.0

        r = volume_ratio

        # 缩量区间：0 → 1.4（平滑上升）
        if r <= 1.4:
            if r < 0.3:
                return 25.0
            elif r < 0.8:
                return 25 + (r - 0.3) / 0.5 * 40  # 25 → 65
            elif r < 1.0:
                return 65 + (r - 0.8) / 0.2 * 10  # 65 → 75
            else:
                return 75 + (r - 1.0) / 0.4 * 10  # 75 → 85

        # 放量区间：1.4 → 无穷（平滑下降）
        elif r <= 3.0:
            return 85 - (r - 1.4) / 1.6 * 20   # 85 → 65
        elif r <= 5.0:
            return 65 - (r - 3.0) / 2.0 * 25   # 65 → 40
        else:
            return max(15, 40 - (r - 5.0) / 10.0 * 25)  # 40 → 15

    @staticmethod
    def calc_turnover_score(turnover: float) -> float:
        """
        换手率评分
        1-5% 活跃 → 高分
        < 0.5% 冷清 → 低分
        > 20% 异常 → 低分
        """
        if turnover is None:
            return 50

        if 2 <= turnover <= 5:
            return 85
        elif 1 <= turnover < 2:
            return 70
        elif 5 < turnover <= 10:
            return 70
        elif 0.5 <= turnover < 1:
            return 50
        elif turnover < 0.5:
            return 30
        else:  # > 10
            return 40

    @staticmethod
    def calc_hot_theme_score(is_hot_stock: bool, blocks: dict = None,
                             concept_names: list = None) -> float:
        """
        热门题材加分 — 三源融合：同花顺热点 + 板块归属 + AShareHub概念板块

        is_hot_stock: 是否在同花顺强势股列表中
        blocks: 板块归属结果（东财 slist）
        concept_names: AShareHub概念板块名称列表
        返回: 0-100 分，仅供报告引用，实际加分由 ScoringModel 权重控制
        """
        score = 50.0
        if is_hot_stock:
            score += 20  # 强势股且有题材归因标签
        if blocks and blocks.get('total', 0) > 0:
            board_count = min(blocks['total'], 20)
            score += min(board_count * 1.0, 15)

        # AShareHub 概念板块增强（全市场覆盖，覆盖面远超同花顺强势股）
        if concept_names:
            n_concepts = len(concept_names)
            if n_concepts >= 10:
                score += 15  # 多概念覆盖，板块效应强
            elif n_concepts >= 5:
                score += 10
            elif n_concepts >= 2:
                score += 5
        return min(score, 100)

    @staticmethod
    def calc_dragon_tiger_score(dragon_tiger: dict) -> float:
        """
        龙虎榜评分 — 有上榜记录且机构净买入为正 → 加分
        来源：a-stock-data §3.5
        """
        if not dragon_tiger:
            return 50.0
        records = dragon_tiger.get('records', [])
        if not records:
            return 50.0

        # 最近一次上榜
        latest = records[0] if records else {}
        net_buy = latest.get('net_buy_wan', 0)

        # 机构净买入
        inst = dragon_tiger.get('institution', {})
        inst_net = inst.get('net_amt', 0)

        score = 50.0
        if net_buy > 0:
            score += 15
        if net_buy > 10000:  # 净买入 > 1亿
            score += 10
        if inst_net > 0:
            score += 15
        if inst_net > 5000:
            score += 10

        return min(score, 100)

    # ========== 基本面因子（长线） ==========

    @staticmethod
    def calc_fundamental_score(pe: float, pb: float, roe: float = None,
                                eps: float = None, mcap: float = 0) -> float:
        """
        基本面评分（0-100）— ROE+PE 双维度盈利质量
        替代 long_term.py 内联的纯 PE/PB 硬编码逻辑

        参数：
          pe: 市盈率（TTM）
          pb: 市净率
          roe: 净资产收益率（%，mootdx finance 提供）
          eps: 每股收益
          mcap: 总市值（元）
        """
        score = 50.0

        # ROE 维度（核心盈利质量）
        if roe is not None and roe > 0:
            if roe >= 20:
                score += 20  # 优秀
            elif roe >= 15:
                score += 15
            elif roe >= 10:
                score += 10
            elif roe >= 5:
                score += 5
        else:
            # ROE 不可用，回退到 PE 判断
            if pe is not None and pe > 0:
                if pe < 15:
                    score += 10  # 低PE替代判断
                elif pe < 30:
                    score += 5

        # PE 辅助（盈利确认）
        if pe is not None and pe > 0:
            if pe < 15 and pb is not None and pb > 0 and pb < 3:
                score += 10  # PE<15 + PB<3 = 估值合理且盈利健康
            elif pe > 100:
                score -= 10  # PE 极高，风险信号
        elif pe is not None and pe < 0:
            score -= 15  # 亏损

        # 市值加分（大市值稳定性加分）
        if mcap and mcap > 100_000_000_000:
            score = min(score + 5, 100)

        return np.clip(score, 0, 100)

    @staticmethod
    def calc_valuation_score(pe: float, pb: float, roe: float = None,
                              ps: float = None, div_yield: float = None) -> float:
        """
        估值评分（0-100）— PB + PS（分位指标）+ 股息率

        设计意图：PE 在 fundamental_score 里已经算，valuation 这里只算
        跟 PE 不直接相关的估值维度，避免双重计算同一信号。

        评分逻辑：
          - PB 估值带（PB 越低分越高，但破净本身可能是银行/钢铁）
          - PS（市销率，亏损企业也能算）
          - 股息率（高分配合高分 PB = 真低估；高分股息 + 低 PB = 价值陷阱警示）
          - 极高股息（>8%）扣分（可能有派息不可持续风险）

        参数：
          pe: 不使用（保留字段以兼容上层调用）
          pb: 市净率
          roe: 不使用
          ps: 市销率（可选）
          div_yield: 股息率 %（可选）
        """
        score = 50.0

        # PB 维度（核心）
        if pb is not None and pb > 0:
            if pb < 1:
                score += 15  # 破净，深度低估
            elif pb < 2:
                score += 12  # 低估
            elif pb < 4:
                score += 5   # 合理偏低
            elif pb < 8:
                score -= 5   # 中高估
            else:
                score -= 15  # 高估 (>8pb)
        elif pb is not None and pb <= 0:
            score -= 10

        # PS 维度（辅助，亏损企业也适用）
        if ps is not None and ps > 0:
            if ps < 1:
                score += 10  # 低 PS，可能低估
            elif ps < 3:
                score += 3
            elif ps < 8:
                score -= 3
            else:
                score -= 10  # 高 PS 估值偏贵

        # 股息率维度（高分 = 高分，但极端高 = 警示）
        if div_yield is not None and div_yield > 0:
            if 2 <= div_yield <= 5:
                score += 8   # 健康股息率
            elif 5 < div_yield <= 8:
                score += 3   # 较高但可接受
            elif div_yield > 8:
                score -= 5   # 异常高，可能不可持续
            # div_yield < 2 不加分（市场上大多数股票都这样）

        return np.clip(score, 0, 100)

    @staticmethod
    def calc_institutional_score(dragon_tiger: dict = None,
                                  main_fund: float = None) -> float:
        """
        机构持仓/关注评分（0-100）

        用龙虎榜机构动向 + 主力资金流推测机构关注度
        无数据时返回中性 50
        """
        score = 50.0
        has_data = False

        # 龙虎榜机构净买入
        if dragon_tiger:
            inst = dragon_tiger.get('institution', {})
            inst_net = inst.get('net_amt', 0)
            if inst_net > 0:
                has_data = True
                score += 15
                if inst_net > 5000:
                    score += 10  # 机构净买入 > 5000万
            elif inst_net < 0:
                has_data = True
                score -= 10  # 机构净卖出

            # 有上榜记录
            if dragon_tiger.get('records'):
                has_data = True
                score += 5

        # 主力资金净流入
        if main_fund is not None and abs(main_fund) > 0:
            has_data = True
            if main_fund > 0:
                score += 10
                if main_fund > 50_000_000:  # > 5000万
                    score += 5
            else:
                score -= 5

        # 没有任何数据时返回中性
        if not has_data:
            return 50.0

        return min(score, 100)

    # ========== 综合评分 ==========

    def compute_all_factors(self, stock_data: Dict, mode: str = 'short') -> Dict:
        """
        对单只股票计算所有因子得分

        参数：
          stock_data: 包含该股票所有原始数据的字典
          mode: 'short' / 'long'

        返回：因子名 → 得分（0-100）的字典
        """
        factors = {}

        # 资金面 — 大单缓存是单日数据，传 days=None
        main_fund = stock_data.get('main_fund_accumulated')
        if main_fund is not None and abs(main_fund) > 0:
            # 优先使用横截面百分位（从 rank_stocks 传入，避免全员满分）
            percentile = stock_data.get('_capital_flow_percentile')
            if percentile is not None:
                factors['capital_flow'] = min(percentile * 100, 100)
            else:
                factors['capital_flow'] = self.calc_main_fund_score(main_fund, days=None)
        else:
            factors['capital_flow'] = self.calc_main_fund_score(main_fund)
        factors['north_flow'] = self.calc_north_flow_score(
            stock_data.get('north_flow_accumulated', 0)
        )

        # 动量
        rps_value = stock_data.get('rps_20', 50)
        factors['momentum'] = self.calc_rps_score(rps_value)

        # 技术形态 — 双源评分（K线自算 + AShareHub API 校验）
        kline_df = stock_data.get('kline_df')
        asharehub_tech = stock_data.get('asharehub_tech')
        if kline_df is not None or asharehub_tech is not None:
            from core.technical_scorer import TechnicalScorer
            tech_scorer = TechnicalScorer()
            tech_result = tech_scorer.score_dual_source(kline_df, asharehub_tech)
            factors['technical'] = tech_result.total
        else:
            # 回退到原来的 MACD 评分
            macd_info = stock_data.get('macd_status', {})
            factors['technical'] = macd_info.get('score', 50) if isinstance(macd_info, dict) else 50

        # 量价（含尾盘成交结构增强）
        base_vp = self.calc_volume_ratio_score(stock_data.get('volume_ratio'))
        tail = stock_data.get('tail_end_stats', {})
        tail_bonus = 0.0
        if tail.get('available'):
            # ① 尾盘30分钟成交占比（> 25% 说明尾盘资金集中介入）
            tvr = tail.get('tail_volume_ratio', 0)
            if tvr > 0.30:
                tail_bonus += 12
            elif tvr > 0.25:
                tail_bonus += 9
            elif tvr > 0.20:
                tail_bonus += 5
            elif tvr > 0.15:
                tail_bonus += 3
            elif tvr < 0.05:
                tail_bonus -= 5  # 尾盘几乎无大单，说明资金不关注

            # ② 尾盘资金逆转（全天净流出但尾盘30min净流入 = 强信号）
            if tail.get('tail_reversal'):
                tail_bonus += 12

            # ③ 收盘价位置（相对VWAP）
            pp = tail.get('price_position', 0)
            if pp > 0.05:
                tail_bonus += 6   # 收盘显著高于均价，强势收尾
            elif pp > 0.02:
                tail_bonus += 3
            elif pp < -0.02:
                tail_bonus -= 3   # 收盘低于均价，弱势收尾

        factors['volume_price'] = np.clip(base_vp * 0.7 + tail_bonus, 0, 100)

        # 风险 — 风控通过时 score_penalty=0 → risk_score=100
        factors['risk'] = stock_data.get('risk_score', 100)

        # 新信号因子 — 同花顺热点/板块归属/AShareHub概念/龙虎榜
        factors['hot_theme'] = self.calc_hot_theme_score(
            stock_data.get('is_hot_stock', False),
            stock_data.get('blocks'),
            stock_data.get('concept_names')
        )
        factors['dragon_tiger'] = self.calc_dragon_tiger_score(
            stock_data.get('dragon_tiger')
        )

        if mode == 'long':
            # 基本面因子：优先用 AShareHub 财务指标，回退到 mootdx/baostock
            fin = stock_data.get('financial_indicators')
            if fin:
                factors['fundamental'] = self.calc_fundamental_score(
                    pe=stock_data.get('pe'),
                    pb=stock_data.get('pb'),
                    roe=fin.get('roe'),
                    eps=fin.get('eps'),
                    mcap=stock_data.get('total_market_cap', 0),
                )
                factors['valuation'] = self.calc_valuation_score(
                    pe=stock_data.get('pe'),
                    pb=stock_data.get('pb'),
                    roe=fin.get('roe'),
                )
            else:
                factors['fundamental'] = self.calc_fundamental_score(
                    pe=stock_data.get('pe'),
                    pb=stock_data.get('pb'),
                    roe=stock_data.get('roe'),
                    eps=stock_data.get('eps'),
                    mcap=stock_data.get('total_market_cap', 0),
                )
                factors['valuation'] = self.calc_valuation_score(
                    pe=stock_data.get('pe'),
                    pb=stock_data.get('pb'),
                    roe=stock_data.get('roe'),
                )
            factors['institutional'] = self.calc_institutional_score(
                dragon_tiger=stock_data.get('dragon_tiger'),
                main_fund=stock_data.get('main_fund_accumulated'),
            )

        return factors
