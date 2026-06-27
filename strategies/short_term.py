"""
短线尾盘策略

核心逻辑：
  尾盘（14:50-15:00）全市场扫描 → 多因子评分 → 推荐Top N
  如果市场赚钱效应差，自动跳过推荐。

权重分配：
  主力资金流 27% + 北向资金 10% + RPS动量 15%
  + 技术形态 15% + 量价配合 10% + 风险 10% + 热点题材 8% + 龙虎榜 5%

卖出规则：
  - 止盈：T+1 开盘+2% 以上分批止盈
  - 止损：T+1 开盘-2% 或 收盘跌破MA5
  - 时间止损：T+3 日无表现
"""

import logging
from typing import Dict, List, Optional
from datetime import datetime, timedelta
from concurrent.futures import ThreadPoolExecutor, as_completed

import pandas as pd

from strategies.base import BaseStrategy
from core.data_engine import DataEngine
from core.scoring_model import ScoringModel
from core.risk_filter import RiskFilter
from core.portfolio_optimizer import PortfolioOptimizer

logger = logging.getLogger(__name__)


class ShortTermStrategy(BaseStrategy):
    """短线尾盘策略"""

    def __init__(self, config: dict = None):
        super().__init__(config)
        config = config or {}
        self.data_engine = DataEngine()
        # 修复：从 config.yml 的 weights 键加载，而非不存在的 weights_model
        weights_cfg = config.get('weights', config.get('weights_model'))
        self.scoring_model = ScoringModel(
            weights=weights_cfg if weights_cfg else None
        )
        # 从嵌套的 buy 段读取参数
        buy_cfg = config.get('buy', {})
        self.risk_filter = RiskFilter(config=buy_cfg)
        self.top_n = buy_cfg.get('max_candidates', 3)
        self.min_score = buy_cfg.get('min_score', 60)

    def run(self, market_data: Dict = None) -> List[Dict]:
        """
        运行短线尾盘策略

        参数：
          market_data: 可选，外部传入的全市场数据

        返回：
          推荐列表 [{code, name, score, rating, decision, ...}]
        """
        logger.info("=" * 50)
        logger.info("短线尾盘策略运行中...")

        # 回测模式标志（从 market_data 传入，非回测时为 False）
        is_backtest = market_data and market_data.get('backtest_mode', False)
        backtest_date = market_data.get('backtest_date') if is_backtest else None

        # 1. 获取全市场行情
        if market_data:
            quotes_df = market_data.get('quotes_df')
        else:
            quotes_df = self.data_engine.get_all_quotes()

        if quotes_df is None or quotes_df.empty:
            logger.warning("无可用的行情数据，策略跳过")
            return []

        logger.info(f"全市场共 {len(quotes_df)} 只股票")

        # 1.5 市场环境评估（0额外API成本，从行情数据计算）
        # 同花顺强势股（回测模式下跳过实时API）
        if is_backtest:
            hot_codes = set()
            hot_df = pd.DataFrame()
            logger.info("回测模式：热点数据跳过（hot_theme 因子中性化）")
        else:
            hot_df = self.data_engine.get_ths_hot_stocks()
            hot_codes = set()
            if not hot_df.empty:
                hot_codes = set(str(c).zfill(6) for c in hot_df['代码'].tolist() if pd.notna(c))
                logger.info(f"同花顺强势股: {len(hot_codes)} 只有题材归因标签")
        market_assessment = self._assess_market(quotes_df, hot_df, is_backtest=is_backtest)
        logger.info(f"市场环境综合评分: {market_assessment['total']}/100 "
                     f"({market_assessment['level']})")

        if market_assessment['skip']:
            logger.warning(f"市场赚钱效应较差({market_assessment['level']})，策略跳过")
            # 仍然把市场评估信息传出去（供报告显示）
            return [{
                'market_assessment': market_assessment,
                'data_source_status': self.data_engine.get_data_source_summary(),
                'skip_reason': f"市场赚钱效应较差({market_assessment['level']})，建议空仓观望或减仓",
            }]

        # 根据市场环境调整参数（使用局部变量，不修改实例属性）
        if market_assessment['level'] == '弱市':
            effective_top_n = min(self.top_n, 2)   # 弱市最多推荐2只
            effective_min_score = max(self.min_score, 65)  # 提高评分门槛
            logger.info(f"弱市模式: 最多推荐{effective_top_n}只, 最低评分{effective_min_score}")
        else:
            effective_top_n = self.top_n
            effective_min_score = self.min_score

        # 2. 批量过滤 + 预评分
        # 2.0 预热 lockup 缓存（一次 akshare 调用 ~3-4s，让 5000+ 只预过滤不走 IO）
        try:
            self.data_engine._ensure_lockup_cache()
        except Exception as e:
            logger.warning(f"lockup 缓存预热失败(已熔断): {str(e)[:60]}")

        candidates = self._prefilter(quotes_df)
        logger.info(f"预过滤后 {len(candidates)} 只进入详评")

        if len(candidates) == 0:
            logger.info("今日尾盘策略跳过：没有足够合格的标的")
            return []

        # 3. 获取详细数据（回测模式传入日期限制）
        enriched = self._enrich_data(candidates, hot_codes, is_backtest=is_backtest, backtest_date=backtest_date)

        # 4. 评分 + 排序
        recommendations = self.scoring_model.rank_stocks(
            enriched, mode='short',
            top_n=effective_top_n, min_score=effective_min_score
        )

        if not recommendations:
            logger.info("今日尾盘策略跳过：没有评分达标的标的")
        else:
            logger.info(f"推荐 {len(recommendations)} 只股票 (上限{effective_top_n})")

        # 附加数据源状态到结果
        source_status = self.data_engine.get_data_source_summary()
        for rec in recommendations:
            rec['data_source_status'] = source_status
            rec['market_data'] = {}

        # 暴露详评数据给外部（用于因子采集）
        self._last_enriched = enriched

        # 附加市场级数据（回测模式下跳过实时北向API）
        if not is_backtest:
            try:
                north_summary = self.data_engine.get_north_flow_summary()
                if north_summary:
                    for rec in recommendations:
                        rec['market_data'] = {'north_flow': north_summary}
            except Exception:
                pass

        # 对推荐结果补充板块归属和龙虎榜（仅对 top N 做，走 em_get 限流）
        for rec in recommendations:
            code = rec['code']
            try:
                blocks = self.data_engine.get_stock_blocks(code)
                rec['blocks'] = blocks
            except Exception:
                rec['blocks'] = {"total": 0, "boards": [], "concept_tags": []}
            try:
                dt = self.data_engine.get_dragon_tiger(code)
                rec['dragon_tiger'] = dt
            except Exception:
                rec['dragon_tiger'] = {"records": [], "seats": {"buy": [], "sell": []}, "institution": {}}
            # 板块和龙虎榜数据已补全，重新计算 hot_theme 和 dragon_tiger 评分
            updated_factors = self.scoring_model.factor_lib.compute_all_factors(rec, 'short')
            if 'hot_theme' in updated_factors and rec.get('breakdown', {}).get('hot_theme'):
                rec['breakdown']['hot_theme']['raw_score'] = updated_factors['hot_theme']
                rec['breakdown']['hot_theme']['note'] = '板块归属已纳入'
            if 'dragon_tiger' in updated_factors and rec.get('breakdown', {}).get('dragon_tiger'):
                rec['breakdown']['dragon_tiger']['raw_score'] = updated_factors['dragon_tiger']
                rec['breakdown']['dragon_tiger']['note'] = '龙虎榜数据已纳入'
            # breakdown 中 hot_theme / dragon_tiger 的实际得分通过 scoring_model.compute_all_factors
            # 算出来的，不要 pop 掉，让配置里 0.08+0.05 这 13% 权重真的生效

        # 组合优化：评分加权仓位分配
        recommendations = PortfolioOptimizer.allocate(recommendations)

        return recommendations

    def _prefilter(self, quotes_df: pd.DataFrame) -> list:
        """
        初筛过滤

        快速过滤掉明显不合格的股票，减少后续API调用

        过滤条件：
        - 非ST
        - 非涨停封死
        - 成交额 > 3000万
        - 非停牌（有价格）
        """
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
                'name_raw': row.get('name', ''),
            }

            # 风险过滤
            risk_result = self.risk_filter.check_stock(stock, data_engine=self.data_engine)
            stock['risk_check'] = risk_result

            if risk_result['passed'] or risk_result['score_penalty'] < 0.8:
                # 设置风险评分（0-100，越高越安全）
                stock['risk_score'] = 100.0 * (1.0 - risk_result.get('score_penalty', 0))
                candidates.append(stock)

        return candidates

    def _assess_market(self, quotes_df: pd.DataFrame, hot_df: pd.DataFrame,
                       is_backtest: bool = False) -> Dict:
        """
        市场环境评估 — 判断今日是否适合短线操作

        从全市场行情数据计算5个维度指标，综合评分。
        0额外API成本（全从已有数据计算）。

        返回：{
            total: 综合评分(0-100),
            level: 强市/中性市/弱市/极差市,
            skip: 是否跳过推荐,
            details: {各分项得分},
            summary: 文字描述,
        }
        """
        import numpy as np

        pct = quotes_df['pct_chg'].dropna()
        advancing = int((pct > 0).sum())
        declining = int((pct < 0).sum())
        flat = int((pct == 0).sum())
        total = advancing + declining + flat
        ad_ratio = advancing / max(declining, 1)

        limit_ups = int((pct >= 9.5).sum())
        limit_downs = int((pct <= -9.5).sum())
        ud_ratio = limit_ups / max(limit_downs, 1)

        median_chg = float(pct.median())
        pct_up_3 = int((pct >= 3).sum())
        hot_count = len(hot_df) if hot_df is not None and not hot_df.empty else 0

        # 北向资金（回测模式下跳过实时API）
        north_total = 0
        if not is_backtest:
            try:
                north = self.data_engine.get_north_flow_summary()
                north_total = north['total'] if north else 0
            except Exception:
                pass

        # 各维度评分（0-100）
        def scale(value, thresholds):
            """thresholds: [(下限, 上限, 100分时值), ...] 线性内插"""
            for lo, hi, score_at_hi in thresholds:
                if lo <= value < hi:
                    return score_at_hi
            return 50  # 默认中性

        # 涨跌比评分 (30分权重): >2=满分, 1-2=线性, <0.5=0分
        ad_score = scale(ad_ratio, [
            (0, 0.3, 0), (0.3, 0.5, 10), (0.5, 0.8, 30),
            (0.8, 1.0, 50), (1.0, 1.5, 70), (1.5, 2.0, 85),
            (2.0, 999, 100),
        ])

        # 涨停跌停比评分 (25分权重): >5=满分, <1=0分
        ud_score = scale(ud_ratio, [
            (0, 0.5, 0), (0.5, 1.0, 15), (1.0, 2.0, 40),
            (2.0, 3.0, 60), (3.0, 5.0, 80), (5.0, 999, 100),
        ])

        # 中位数涨幅评分 (20分权重): >1%=满分, <-1%=0分
        median_score = scale(median_chg, [
            (-10, -2, 0), (-2, -1, 15), (-1, -0.5, 35),
            (-0.5, 0, 50), (0, 0.5, 70), (0.5, 1.0, 85),
            (1.0, 10, 100),
        ])

        # 强势股数量评分 (15分权重): >100=满分, <20=0分
        hot_score = scale(hot_count, [
            (0, 10, 0), (10, 20, 15), (20, 30, 30),
            (30, 50, 50), (50, 80, 70), (80, 100, 85),
            (100, 9999, 100),
        ])

        # 北向资金评分 (10分权重): >40亿=满分, <-80亿=0分
        north_score = scale(north_total, [
            (-999, -80, 0), (-80, -40, 20), (-40, -10, 40),
            (-10, 10, 60), (10, 40, 80), (40, 999, 100),
        ])

        total_score = (
            ad_score * 0.30 + ud_score * 0.25 + median_score * 0.20
            + hot_score * 0.15 + north_score * 0.10
        )

        if total_score < 40:
            level = '极差市'
            skip = True
        elif total_score < 55:
            level = '弱市'
            skip = False
        elif total_score < 70:
            level = '中性市'
            skip = False
        else:
            level = '强市'
            skip = False

        logger.info(f"  市场环境: 涨跌比{ad_ratio:.2f}({advancing}/{declining}) "
                     f"涨停{limit_ups}跌停{limit_downs} "
                     f"中位数涨幅{median_chg:+.2f}% "
                     f"强势股{hot_count}只 北向{north_total:+.0f}亿")

        return {
            'total': round(total_score, 1),
            'level': level,
            'skip': skip,
            'summary': (
                f"涨跌比{ad_ratio:.2f}，涨停{limit_ups}跌停{limit_downs}，"
                f"中位数涨幅{median_chg:+.2f}%，强势股{hot_count}只"
            ),
            'details': {
                'ad_ratio': round(ad_ratio, 2),
                'advancing': advancing, 'declining': declining,
                'limit_ups': limit_ups, 'limit_downs': limit_downs,
                'median_chg': median_chg,
                'hot_count': hot_count,
                'north_total': north_total,
                'ad_score': round(ad_score, 1),
                'ud_score': round(ud_score, 1),
                'median_score': round(median_score, 1),
                'hot_score': round(hot_score, 1),
                'north_score': round(north_score, 1),
            },
        }

    def _enrich_data(self, candidates: list, hot_codes: set = None,
                     is_backtest: bool = False, backtest_date: str = None) -> list:
        """
        获取详细数据 — 多维度初筛后取前 200 只拉取完整数据

        初筛评分（利用已有数据，不额外请求）：
          - 流动性 30分：成交额排名百分位
          - 活跃度 20分：换手率排名百分位
          - 短期动量 15分：当日涨幅（正合理，过高扣分）
          - 风险 20分：risk_score
          - 估值 15分：PE合理区间得分
          - 合计 100分

        取前 200 只进入详评阶段
        """
        # 1. 先给所有候选股算初步评分
        scored = []
        # 收集排名数据
        amounts = [s.get('amount', 0) or 0 for s in candidates]
        turnovers = [s.get('turnover', 0) or 0 for s in candidates]
        pct_chgs = [s.get('pct_chg', 0) or 0 for s in candidates]

        import numpy as np
        # 改进：用numpy的percentile替代O(n²)的循环
        amount_arr = np.array(amounts)
        turnover_arr = np.array(turnovers)

        def pct_rank_arr(arr, val):
            if len(arr) == 0:
                return 50
            return np.searchsorted(np.sort(arr), val) / len(arr) * 100

        for stock in candidates:
            amount = stock.get('amount', 0) or 0
            turnover = stock.get('turnover', 0) or 0
            pct_chg = stock.get('pct_chg', 0) or 0
            risk_score = stock.get('risk_score', 50)
            pe = stock.get('pe')

            # 流动性评分：成交额越高分越高
            amount_score = pct_rank_arr(amount_arr, amount) * 0.30

            # 活跃度评分：换手率1%-10%最佳，过低冷清，过高异常
            if 1 <= turnover <= 10:
                turnover_score = pct_rank_arr(turnover_arr, turnover) * 0.20
            elif 0.5 <= turnover < 1:
                turnover_score = 30 * 0.20
            elif turnover > 10:
                turnover_score = 20 * 0.20
            else:
                turnover_score = 5 * 0.20

            # 短期动量：涨跌幅在1%~5%最佳，过大回调风险高，过小无动量
            if 1 <= pct_chg <= 5:
                momentum_score = 85 * 0.15
            elif -1 < pct_chg < 1:
                momentum_score = 60 * 0.15
            elif 5 < pct_chg <= 9:
                momentum_score = 50 * 0.15
            elif -5 < pct_chg <= -1:
                momentum_score = 30 * 0.15
            else:
                momentum_score = 15 * 0.15

            # 风险评分
            risk_score_component = risk_score * 0.20

            # 估值评分：PE在10-30合理区间得分高
            if pe and pe > 0:
                if 10 <= pe <= 30:
                    pe_score = 85 * 0.15
                elif 5 <= pe < 10 or 30 < pe <= 50:
                    pe_score = 65 * 0.15
                elif 50 < pe <= 100:
                    pe_score = 40 * 0.15
                else:
                    pe_score = 20 * 0.15
            else:
                pe_score = 30 * 0.15  # PE负或缺失

            preliminary_score = amount_score + turnover_score + momentum_score + risk_score_component + pe_score

            stock['preliminary_score'] = round(preliminary_score, 2)
            scored.append(stock)

        # 按初步评分排序取前 200
        scored.sort(key=lambda x: x.get('preliminary_score', 0), reverse=True)
        top_candidates = scored[:200]
        logger.info(f"初步评分排序，前10只: {[(s['code'], s.get('name',''), s['preliminary_score']) for s in top_candidates[:10]]}")

        enriched = []
        all_raw_returns = {}
        total = len(top_candidates)
        logger.info(f"详评 {total} 只（初步评分前200）")

        # 预加载大单缓存 — 跳过（境外网络akshare可能超时，各股票单独调用时自动降权）

        # 先集中获取所有资金流（回测模式下跳过实时API，设默认值）
        for stock in top_candidates:
            code = stock['code']
            if is_backtest:
                stock['main_fund_accumulated'] = 0
                stock['north_flow_accumulated'] = 0
                stock['tail_end_stats'] = {'available': False}
            else:
                try:
                    main_accum = self.data_engine.get_main_fund_accumulated(code, days=10)
                except Exception:
                    main_accum = None
                stock['main_fund_accumulated'] = main_accum

                try:
                    north_accum = self.data_engine.get_north_flow_accumulated(code, days=10)
                except Exception:
                    north_accum = None
                stock['north_flow_accumulated'] = north_accum

                # 尾盘成交结构（从已缓存的大单数据提取，不走额外API）
                try:
                    tail_end = self.data_engine.get_tail_end_stats(code)
                    stock['tail_end_stats'] = tail_end
                except Exception:
                    stock['tail_end_stats'] = {'available': False}

        # K线 + 技术指标（并行拉取，每只独立线程）
        # 缓存命中的毫秒级返回，未命中的走baostock
        from concurrent.futures import ThreadPoolExecutor, as_completed
        from threading import Lock
        enrich_lock = Lock()

        def fetch_kline(stock):
            code = stock['code']
            try:
                # 回测模式：只取回测日期之前的K线，防止前瞻偏差
                if is_backtest and backtest_date:
                    from datetime import datetime, timedelta
                    bt_end = datetime.strptime(backtest_date, '%Y-%m-%d').strftime('%Y-%m-%d')
                    bt_start = (datetime.strptime(backtest_date, '%Y-%m-%d') - timedelta(days=120)).strftime('%Y-%m-%d')
                    kline = self.data_engine.get_kline(code, start_date=bt_start, end_date=bt_end)
                else:
                    kline = self.data_engine.get_kline(code)
                if kline is not None and not kline.empty and len(kline) >= 20:
                    close = kline['close']
                    high = kline.get('high', close)
                    low = kline.get('low', close)
                    macd = self.scoring_model.factor_lib.calc_macd_status(close)
                    stock['macd_status'] = macd
                    stock['kline_df'] = kline
                    raw_return_20 = (close.iloc[-1] / close.iloc[-20] - 1) * 100
                    with enrich_lock:
                        all_raw_returns[code] = raw_return_20
                    stock['raw_return_20'] = raw_return_20
                else:
                    stock['macd_status'] = {'score': 50, 'status': 'unknown'}
                    stock['raw_return_20'] = 0

                # 双源技术校验：AShareHub 技术因子（独立熔断，失败不影响 K 线）
                try:
                    asharehub_tech = self.data_engine.get_technical_factors_asharehub(code)
                    if asharehub_tech is not None:
                        stock['asharehub_tech'] = asharehub_tech
                except Exception:
                    pass

                # AShareHub 概念板块（hot_theme 增强，独立熔断）
                try:
                    concepts = self.data_engine.get_concept_members(code)
                    if concepts is not None:
                        stock['concept_names'] = concepts
                except Exception:
                    pass

                # AShareHub 财务指标（长线策略，独立熔断）
                try:
                    fin = self.data_engine.get_financial_indicators(code)
                    if fin is not None:
                        stock['financial_indicators'] = fin
                except Exception:
                    pass
            except Exception as e:
                logger.warning(f"{code} 技术面失败: {str(e)[:60]}")
                stock['macd_status'] = {'score': 50, 'status': 'unknown'}
                stock['raw_return_20'] = 0
            return stock

        # baostock 是全局单例，只能用1个线程。但大部分已缓存，串行走就行。
        # 用 max_workers=3 让少量未命中并行，大部分已命中毫秒返回
        with ThreadPoolExecutor(max_workers=3) as executor:
            futures = {executor.submit(fetch_kline, s): s for s in top_candidates}
            for future in as_completed(futures):
                try:
                    enriched.append(future.result(timeout=30))
                except Exception:
                    # 超时或异常：跳过该股票
                    stock = futures[future]
                    stock['macd_status'] = {'score': 50, 'status': 'unknown'}
                    stock['raw_return_20'] = 0
                    enriched.append(stock)

        # 横截面RPS计算
        if all_raw_returns:
            codes_list = list(all_raw_returns.keys())
            returns_series = pd.Series([all_raw_returns[c] for c in codes_list])
            rps_values = returns_series.rank(pct=True) * 100
            for code, rps_val in zip(codes_list, rps_values):
                for s in enriched:
                    if s['code'] == code:
                        s['rps_20'] = float(rps_val)
                        break

        # 补充新数据源信号（不额外请求 API，只打标签）
        # 板块归属和龙虎榜在最终推荐后单独补充（避免 em_get 限流阻塞200只流程）
        for s in enriched:
            s['is_hot_stock'] = hot_codes and s['code'] in hot_codes
            s['blocks'] = {"total": 0, "boards": [], "concept_tags": []}
            s['dragon_tiger'] = {"records": [], "seats": {"buy": [], "sell": []}, "institution": {}}

        logger.info(f"详评完成: {len(enriched)} 只")
        return enriched

    def get_required_fields(self) -> list:
        return [
            'code', 'name', 'price', 'pct_chg', 'amount',
            'turnover', 'volume_ratio', 'pe', 'pb', 'name_raw'
        ]

    def describe(self) -> str:
        return (f"短线尾盘策略: T+0尾盘选股 → T+1开盘卖出。"
                f"主力资金(27%)+动量(15%)+技术(15%)+量价(10%)+风控(10%)"
                f"+北向(10%)+热点(8%)+龙虎榜(5%)。"
                f"含市场环境评估，极差市自动跳过。")
