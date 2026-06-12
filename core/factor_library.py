"""
因子库 — 30+ 量化因子计算

因子分类：
  资金面（6）  — 主力资金流、北向资金、融资融券
  动量（5）    — RPS 多种周期排位、N日新高比例
  技术（8）    — 均线、MACD、KDJ、RSI、BOLL、量比、换手率
  量价（4）    — 量价配合度、尾盘承接力
  估值（4）    — PE/PB/PS分位、股息率
  风险（3）    — 波动率、回撤、涨跌停状态

参考来源：
  - Sequoia-X 各策略文件（RPS排位、均线金叉）
  - daily-stock StockTrendAnalyzer（技术分析器）
  - a-stock-data tencent_quote（估值数据）
"""

import numpy as np
import pandas as pd
from typing import Dict, List, Union, Optional


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
    def calc_fund_trend_score(trend_slope: float) -> float:
        """
        主力资金趋势评分
        趋势斜率 > 0 且大 → 持续流入，高分
        参考 Sequoia-X 的 RPS 排位逻辑
        """
        # 斜率以万元/天为单位
        score = 50 + trend_slope * 10
        return np.clip(score, 0, 100)

    @staticmethod
    def calc_north_flow_score(accumulated_net: float, days: int = 10) -> float:
        """
        北向资金评分（0-100）
        北向持续净买入 → 高分
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

    @staticmethod
    def calc_n_day_high_ratio(close: pd.Series, high: pd.Series,
                              lookback: int = 20, n_days: int = 5) -> float:
        """
        N日内创新高比例
        NR20 = 近5天中创20日新高的天数比例
        """
        rolling_high = high.rolling(lookback).max()
        new_high = close >= rolling_high
        ratio = new_high.tail(n_days).sum() / n_days
        return float(ratio)

    # ========== 技术形态因子 ==========

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

    @staticmethod
    def calc_kdj_status(high: pd.Series, low: pd.Series, close: pd.Series,
                        n: int = 9, m1: int = 3, m2: int = 3) -> Dict:
        """
        KDJ 指标判断
        超买区（K>80）/ 超卖区（K<20）/ 金叉
        """
        if len(close) < n + max(m1, m2):
            return {'status': 'unknown', 'score': 50}

        low_min = low.rolling(n).min()
        high_max = high.rolling(n).max()
        rsv = (close - low_min) / (high_max - low_min) * 100

        k = rsv.ewm(span=m1, adjust=False).mean()
        d = k.ewm(span=m2, adjust=False).mean()
        j = 3 * k - 2 * d

        current_k = k.iloc[-1]
        current_d = d.iloc[-1]

        if current_k > 80 and current_d > 80:
            status = 'overbought'
            score = 20  # 超买，风险高
        elif current_k < 20 and current_d < 20:
            status = 'oversold'
            score = 80  # 超卖，可能反弹
        elif current_k > current_d and current_k > 50:
            status = 'bullish'
            score = 65
        elif current_k < current_d and current_k < 50:
            status = 'bearish'
            score = 35
        else:
            status = 'neutral'
            score = 50

        return {
            'status': status,
            'score': score,
            'k': current_k,
            'd': current_d,
            'j': j.iloc[-1] if len(j) > 0 else 0
        }

    @staticmethod
    def calc_rsi(close: pd.Series, period: int = 14) -> pd.Series:
        """RSI 计算"""
        delta = close.diff()
        gain = delta.clip(lower=0)
        loss = -delta.clip(upper=0)
        avg_gain = gain.ewm(span=period, adjust=False).mean()
        avg_loss = loss.ewm(span=period, adjust=False).mean()
        rs = avg_gain / avg_loss.replace(0, np.nan)
        rsi = 100 - (100 / (1 + rs))
        return rsi

    def calc_rsi_score(self, rsi_value: float) -> float:
        """RSI 值 → 评分"""
        # 30-70 为正常区间，40-60 中性
        if rsi_value > 80:
            return 30  # 超买，风险高
        elif rsi_value > 70:
            return 50  # 偏高
        elif rsi_value > 60:
            return 70  # 强势但不极端
        elif rsi_value > 40:
            return 60  # 中性偏强
        elif rsi_value > 30:
            return 50  # 中性偏弱
        elif rsi_value > 20:
            return 40  # 偏弱
        else:
            return 30  # 超卖

    @staticmethod
    def calc_bollinger_position(close: pd.Series, period: int = 20,
                                std_dev: int = 2) -> Dict:
        """
        BOLL 布林带位置判断
        返回当前位置：上轨之上/中轨之上/下轨之上/下轨之下
        """
        if len(close) < period:
            return {'position': 'unknown', 'score': 50}

        ma = close.rolling(period).mean()
        std = close.rolling(period).std()
        upper = ma + std_dev * std
        lower = ma - std_dev * std

        current_close = close.iloc[-1]
        current_upper = upper.iloc[-1]
        current_lower = lower.iloc[-1]
        current_ma = ma.iloc[-1]

        band_width = current_upper - current_lower

        if current_close > current_upper:
            position = 'above_upper'
            score = 30  # 突破上轨，可能过热
        elif current_close > current_ma:
            position = 'above_mid'
            # 越接近上轨分越高（趋势强）
            score = 50 + 50 * (current_close - current_ma) / (band_width / 2)
        elif current_close > current_lower:
            position = 'above_lower'
            score = 50 - 50 * (current_ma - current_close) / (band_width / 2)
        else:
            position = 'below_lower'
            score = 20  # 跌破下轨，弱势

        return {
            'position': position,
            'score': np.clip(score, 0, 100),
            'upper': current_upper,
            'mid': current_ma,
            'lower': current_lower,
            'band_width': band_width
        }

    @staticmethod
    def calc_ma_bias(close: pd.Series, ma_period: int = 20) -> float:
        """均线乖离率（价格偏离均线的程度）"""
        if len(close) < ma_period:
            return 0.0
        ma = close.rolling(ma_period).mean().iloc[-1]
        if ma == 0:
            return 0.0
        return (close.iloc[-1] - ma) / ma * 100

    def calc_ma_bias_score(self, bias: float) -> float:
        """乖离率评分"""
        # 偏离过大（>8%）可能回调，得分降低
        abs_bias = abs(bias)
        if abs_bias > 15:
            return 20
        elif abs_bias > 10:
            return 35
        elif bias > 5:
            return 50  # 偏高，注意回调
        elif bias > 0:
            return 65  # 小幅偏离均线上方，强势
        elif bias > -3:
            return 60  # 小幅偏离下方，可能有支撑
        elif bias > -8:
            return 45  # 偏离较多
        else:
            return 25  # 严重偏离

    # ========== 量价因子 ==========

    @staticmethod
    def calc_volume_ratio_score(volume_ratio: float) -> float:
        """
        量比评分
        量比 0.8-2.0 健康放量 → 高分
        量比 < 0.5 缩量 → 低分
        量比 > 5 异常放量 → 低分（可能有出货风险）
        """
        if volume_ratio is None or volume_ratio <= 0:
            return 50

        if 0.8 <= volume_ratio <= 2.0:
            score = 80
        elif 2.0 < volume_ratio <= 3.0:
            score = 70
        elif 0.5 <= volume_ratio < 0.8:
            score = 50
        elif 3.0 < volume_ratio <= 5.0:
            score = 40
        elif volume_ratio < 0.5:
            score = 30
        else:  # > 5.0
            score = 20
        return score

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
    def calc_tail_up_score(current_price: float, close_5min_ago: float) -> float:
        """
        尾盘拉升评分
        收盘前30分钟价格上升 → 加分
        参考原提示词中的"尾盘承接"逻辑
        """
        if close_5min_ago is None or close_5min_ago == 0:
            return 50
        pct = (current_price - close_5min_ago) / close_5min_ago * 100
        if pct > 1.0:
            return 85
        elif pct > 0.5:
            return 75
        elif pct > 0:
            return 60
        elif pct > -0.5:
            return 45
        else:
            return 30

    # ========== 新增因子 ==========

    @staticmethod
    def calc_ma_alignment_score(close: pd.Series) -> float:
        """均线排列评分：多头排列高分，空头排列低分"""
        if len(close) < 20:
            return 50
        ma5 = close.rolling(5).mean().iloc[-1]
        ma10 = close.rolling(10).mean().iloc[-1]
        ma20 = close.rolling(20).mean().iloc[-1]

        if ma5 > ma10 > ma20:
            return 85
        elif ma5 > ma10 and ma10 > ma20 * 0.98:
            return 70
        elif ma5 < ma10 < ma20:
            return 25
        elif ma5 < ma20 and ma10 < ma20:
            return 35
        else:
            return 50

    @staticmethod
    def calc_volume_trend_score(volume: pd.Series, period: int = 5) -> float:
        """量能趋势评分：近N日量能递增 → 高分"""
        if len(volume) < period:
            return 50
        recent = volume.tail(period).values
        # 避免除零
        if recent[0] == 0:
            return 50
        ratio = recent[-1] / recent[0]
        if ratio > 1.3:
            return 80
        elif ratio > 1.0:
            return 65
        elif ratio < 0.7:
            return 30
        else:
            return 50

    @staticmethod
    def calc_support_resistance_score(close: pd.Series, low: pd.Series, high: pd.Series) -> float:
        """支撑阻力评分：价格在关键支撑位附近 → 高分"""
        if len(close) < 30:
            return 50
        support_20 = low.rolling(20).min().iloc[-1]
        resistance_20 = high.rolling(20).max().iloc[-1]
        current = close.iloc[-1]

        distance_to_support = (current - support_20) / support_20 * 100 if support_20 > 0 else 999
        if distance_to_support < 2:
            return 75
        elif distance_to_support < 5:
            return 60
        elif distance_to_support > 15:
            return 35
        else:
            return 50

    @staticmethod
    def calc_hot_theme_score(is_hot_stock: bool, blocks: dict = None) -> float:
        """
        热门题材加分 — 同花顺热点 + 板块归属
        来源：a-stock-data §3.1 同花顺热点 + §3.3 板块归属

        is_hot_stock: 是否在同花顺强势股列表中
        blocks: 板块归属结果（东财 slist）
        返回: 0-100 分，仅供报告引用，实际加分由 ScoringModel 权重控制
        """
        score = 50.0
        if is_hot_stock:
            score += 25  # 强势股且有题材归因标签
        if blocks and blocks.get('total', 0) > 0:
            # 所属板块数越多越好（覆盖面广）
            board_count = min(blocks['total'], 20)
            score += min(board_count * 1.25, 25)
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
    def calc_valuation_score(pe: float, pb: float, roe: float = None) -> float:
        """
        估值评分（0-100）— PE 分位估值 + PB 辅助
        替代 long_term.py 内联的纯 PE 阈值逻辑
        """
        score = 50.0

        if pe is not None and pe > 0:
            if pe < 10:
                score += 30  # 明显低估
            elif pe < 15:
                score += 20
            elif pe < 20:
                score += 15
            elif pe < 30:
                score += 5
            elif pe < 50:
                score -= 5
            else:
                score -= 15  # PE >= 50，偏贵
        elif pe is not None and pe < 0:
            score -= 20  # 亏损，估值不适用

        # PB辅助
        if pb is not None and pb > 0:
            if 1 < pb < 3 and pe is not None and pe > 0 and pe < 20:
                score += 10  # PB合理 + PE低 = 低估确认
            elif pb < 1 and pe is not None and pe > 0:
                score -= 5   # 破净（可能银行股，扣少量分）

        # ROE 辅助：高ROE + 低PE = 可能低估
        if roe is not None and roe > 15 and pe is not None and pe < 15:
            score += 10

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
            factors['capital_flow'] = self.calc_main_fund_score(main_fund, days=None)
        else:
            factors['capital_flow'] = self.calc_main_fund_score(main_fund)
        factors['north_flow'] = self.calc_north_flow_score(
            stock_data.get('north_flow_accumulated', 0)
        )

        # 动量
        rps_value = stock_data.get('rps_20', 50)
        factors['momentum'] = self.calc_rps_score(rps_value)

        # 技术形态 — 优先用 TechnicalScorer 完整评分，其次回退到 MACD
        kline_df = stock_data.get('kline_df')
        if kline_df is not None and not kline_df.empty and len(kline_df) >= 20:
            from core.technical_scorer import TechnicalScorer
            tech_scorer = TechnicalScorer()
            tech_result = tech_scorer.score(kline_df)
            factors['technical'] = tech_result.total
        else:
            # 回退到原来的 MACD 评分
            macd_info = stock_data.get('macd_status', {})
            factors['technical'] = macd_info.get('score', 50) if isinstance(macd_info, dict) else 50

        # 量价
        factors['volume_price'] = self.calc_volume_ratio_score(
            stock_data.get('volume_ratio')
        )

        # 风险 — 风控通过时 score_penalty=0 → risk_score=100
        factors['risk'] = stock_data.get('risk_score', 100)

        # 新信号因子 — 同花顺热点/板块归属/龙虎榜
        factors['hot_theme'] = self.calc_hot_theme_score(
            stock_data.get('is_hot_stock', False),
            stock_data.get('blocks')
        )
        factors['dragon_tiger'] = self.calc_dragon_tiger_score(
            stock_data.get('dragon_tiger')
        )

        # 新因子（辅助信号，不直接参与短线权重，但供报告引用）
        if 'ma5' in stock_data and 'ma10' in stock_data and 'ma20' in stock_data:
            factors['ma_alignment'] = self.calc_ma_alignment_score(
                pd.Series([stock_data.get('ma5', 0), stock_data.get('ma10', 0), stock_data.get('ma20', 0)])
            )

        if mode == 'long':
            # 基本面因子：使用新的 ROE/PE 多维度评分，而非内联硬编码
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
