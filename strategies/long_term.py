"""
长线持股策略

核心逻辑：
  月度选股，持股周期3-6个月
  侧重基本面+北向长期资金+估值+中期动量

  权重分配：
    基本面(30%) + 北向资金(20%) + 中期动量(15%)
    + 估值(15%) + 风险(10%) + 机构持仓(10%)

  持股周期建议：
    3-6个月，跟随北向资金+业绩释放节奏
"""

import logging
from typing import Dict, List, Optional
from datetime import datetime, timedelta

import pandas as pd

from strategies.base import BaseStrategy
from core.data_engine import DataEngine
from core.scoring_model import ScoringModel
from core.risk_filter import RiskFilter
from core.portfolio_optimizer import PortfolioOptimizer

logger = logging.getLogger(__name__)


class LongTermStrategy(BaseStrategy):
    """长线持股策略"""

    def __init__(self, config: dict = None):
        super().__init__(config)
        config = config or {}
        self.data_engine = DataEngine()
        # 修复：从 config.yml 的 weights 键加载
        weights_cfg = config.get('weights', config.get('weights_model'))
        self.scoring_model = ScoringModel(
            weights=weights_cfg if weights_cfg else None,
            sell_config=config.get('sell', {})
        )
        # 从嵌套的 buy 段读取参数
        buy_cfg = config.get('buy', {})
        sell_cfg = config.get('sell', {})
        hold_cfg = config.get('hold_period', {})
        self.risk_filter = RiskFilter(config=buy_cfg)
        self.top_n = buy_cfg.get('max_candidates', 5)
        self.min_score = buy_cfg.get('min_score', 65)
        self.hold_cfg = config.get('hold_period', {})
        self.min_months = self.hold_cfg.get('min_months', 3)
        self.max_months = self.hold_cfg.get('max_months', 6)

    def run(self, market_data: Dict = None) -> List[Dict]:
        """运行长线策略"""
        logger.info("=" * 50)
        logger.info("长线持股策略运行中...")

        # 回测模式标志
        is_backtest = market_data and market_data.get('backtest_mode', False)
        backtest_date = market_data.get('backtest_date') if is_backtest else None
        self._backtest_mode = is_backtest

        if market_data and 'quotes_df' in market_data:
            quotes_df = market_data['quotes_df']
        else:
            quotes_df = self.data_engine.get_all_quotes()

        if quotes_df is None or quotes_df.empty:
            logger.warning("无可用的行情数据")
            return []

        # 预过滤（长线更严格）
        candidates = self._prefilter_long(quotes_df)
        logger.info(f"预过滤后 {len(candidates)} 只进入详评")

        if not candidates:
            return []

        enriched = self._enrich_data(candidates)
        recommendations = self.scoring_model.rank_stocks(
            enriched, mode='long',
            top_n=self.top_n, min_score=self.min_score
        )

        if recommendations:
            logger.info(f"推荐 {len(recommendations)} 只长线标的")
            # 组合优化：评分加权仓位分配
            recommendations = PortfolioOptimizer.allocate(recommendations)

        # 暴露详评数据供因子采集
        self._last_enriched = enriched

        return recommendations

    def _prefilter_long(self, quotes_df: pd.DataFrame) -> list:
        """长线初筛（比短线更严格）"""
        candidates = []

        for _, row in quotes_df.iterrows():
            stock = {
                'code': str(row['code']).zfill(6),
                'name': row.get('name', ''),
                'price': row.get('price', 0),
                'pct_chg': row.get('pct_chg', 0),
                'amount': row.get('amount', 0),
                'turnover': row.get('turnover', 0),
                'volume_ratio': row.get('volume_ratio', 1),
                'pe': row.get('pe', None),
                'pb': row.get('pb', None),
                'total_market_cap': row.get('total_market_cap', 0),
                'circulating_market_cap': row.get('circulating_market_cap', 0),
                'name_raw': row.get('name', ''),
            }

            risk_result = self.risk_filter.check_stock(stock, data_engine=self.data_engine)
            stock['risk_check'] = risk_result

            if risk_result['passed'] or risk_result['score_penalty'] < 0.5:
                # 长线：排除极小市值股票（<20亿，腾讯API返回单位是亿）
                mcap_yi = stock.get('total_market_cap', 0) or 0
                if mcap_yi < 20:
                    continue
                candidates.append(stock)

        return candidates

    def _enrich_data(self, candidates: list) -> list:
        """获取长线所需详细数据（基本面+北向+中期RPS+财务快照）"""
        enriched = []
        total = min(len(candidates), 50)
        is_backtest = getattr(self, '_backtest_mode', False)

        for i, stock in enumerate(candidates[:50]):
            code = stock['code']

            # 北向资金（回测模式下跳过实时API）
            if not is_backtest:
                try:
                    north_30d = self.data_engine.get_north_flow_accumulated(code, days=30)
                except Exception:
                    north_30d = None
                stock['north_flow_accumulated'] = north_30d
            else:
                stock['north_flow_accumulated'] = None

            # K线
            try:
                kline = self.data_engine.get_kline(code)
                if kline is not None and not kline.empty:
                    close = kline['close']
                    # 120日RPS（中期动量）
                    if len(close) >= 120:
                        rps_120 = (close.iloc[-1] / close.iloc[-120] - 1) * 100
                        stock['rps_20'] = rps_120
                    else:
                        stock['rps_20'] = 50

                    macd = self.scoring_model.factor_lib.calc_macd_status(close)
                    stock['macd_status'] = macd

                    # 存储K线供 TechnicalScorer 使用
                    stock['kline_df'] = kline
                else:
                    stock['macd_status'] = {'score': 50, 'status': 'unknown'}
                    stock['rps_20'] = 50
            except Exception:
                stock['macd_status'] = {'score': 50, 'status': 'unknown'}
                stock['rps_20'] = 50

            # 基本面财务快照（回测模式下跳过实时API）
            if not is_backtest:
                try:
                    fin = self.data_engine.get_financial_snapshot(code)
                    if fin:
                        stock['roe'] = fin.get('roe')
                        stock['eps'] = fin.get('eps')
                        # profit/income 供报告引用，不直接参与评分
                        stock['profit'] = fin.get('profit')
                        stock['income'] = fin.get('income')
                        stock['fin_report_date'] = fin.get('report_date', '')
                except Exception as e:
                    logger.warning(f"{code} 财务快照获取失败: {e}")

            # 去掉内联 PE/PB 硬编码评分 — 已移至 FactorLibrary 统一计算
            # compute_all_factors(mode='long') 自动调用 calc_fundamental_score / calc_valuation_score

            enriched.append(stock)

        # 横截面RPS计算（长线用120日涨幅排名）
        all_returns = {s['code']: s.get('rps_20', 50) for s in enriched}
        if all_returns:
            codes_list = list(all_returns.keys())
            returns_series = pd.Series([all_returns[c] for c in codes_list])
            rps_values = returns_series.rank(pct=True) * 100
            for code, rps_val in zip(codes_list, rps_values):
                for s in enriched:
                    if s['code'] == code:
                        s['rps_20'] = float(rps_val)
                        break

        # 附加数据源状态
        try:
            source_status = self.data_engine.get_data_source_summary()
            for rec in enriched:
                rec['data_source_status'] = source_status
        except Exception:
            pass

        if total > 0:
            logger.info(f"详评完成: {len(enriched)} 只")
        return enriched

    def get_hold_advice(self, stock_code: str) -> Dict:
        """查询某只股票的持股建议"""
        return {
            'code': stock_code,
            'suggested_hold_months': f"{self.min_months}-{self.max_months}个月",
            'advice': "建议按月跟踪北向资金变化和季报业绩"
        }

    def get_required_fields(self) -> list:
        return ['code', 'name', 'price', 'pe', 'pb', 'amount']

    def describe(self) -> str:
        return (f"长线持股策略: 月度选股，持有{self.min_months}-{self.max_months}个月。"
                f"基本面(30%)+北向(20%)+动量(15%)+估值(15%)+风控(10%)+机构(10%)")
