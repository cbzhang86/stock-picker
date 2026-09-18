"""
回测引擎 — 验证策略的历史表现

核心逻辑：
  逐日模拟：用真实全市场行情逐日跑策略 → 记录推荐
  → 用 mootdx 查 T+1/T+5 真实K线 → 统计胜率/收益/夏普/回撤

数据流：
  trade_calendar → for each trade_date:
    get_all_quotes() → strategy.run() → recommendations
    → backfill T+1 returns via get_kline()
  → calculate_results()

限制：
  - 资金流/北向在回测中不可用（当日大单数据不可回溯）
  - mootdx 提供约 600 个交易日（~2.5年）
  - 以次日开盘价成交，无法模拟盘中即时成交
"""

import logging
import os
import sqlite3
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple
from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from core.data_engine import DataEngine
from strategies.short_term import ShortTermStrategy
from strategies.long_term import LongTermStrategy

logger = logging.getLogger(__name__)


@dataclass
class BacktestResult:
    """回测结果"""
    strategy_name: str
    period: Tuple[str, str]
    total_trading_days: int
    total_trades: int
    win_rate: float
    avg_return_t1: float
    avg_return_t5: float
    max_win_t1: float
    max_loss_t1: float
    max_drawdown: float
    sharpe_ratio: float
    benchmark_return: float
    strategy_return: float
    excess_return: float
    monthly_returns: List[Dict]
    equity_curve: List[float] = field(default_factory=list)
    factor_performance: Dict = field(default_factory=dict)
    trade_details: List[Dict] = field(default_factory=list)
    mode: str = ''        # 'kfactor' / 'full' — 回测类型标记
    factors_tested: List[str] = field(default_factory=list)
    codes_count: int = 0
    # 2026-09-05 审查 P3-9：基准拉取失败时 False —— 超额收益数字不可信，
    # 报告层应据此把 excess_return 标为"无基准参照"而不是当作真实超额
    benchmark_available: bool = True
    # P1-J：多基准收益 {code: return% 或 None}；benchmark_return 为主基准（首个成功者）
    benchmarks: Dict = field(default_factory=dict)
    # P0-D：卖出规则路径口径（止盈/止损/时间止损，严格 T+1 制度）
    avg_return_rule: float = 0.0
    win_rate_rule: float = 0.0
    rule_exit_breakdown: Dict = field(default_factory=dict)


class BacktestEngine:
    """回测引擎 — 逐日模拟"""

    def __init__(self, config: dict = None):
        self.config = config or {}
        self.data_engine = DataEngine()
        self.initial_capital = self.config.get('initial_capital', 1_000_000)
        self.commission = self.config.get('commission_rate', 0.0003)
        self.slippage = self.config.get('slippage', 0.001)
        # P0-C 修复（2026-09-05 审查报告）：交易成本补全。
        # 原成本模型只有佣金+滑点，漏计印花税+过户费 → 单笔双边成本低估约 0.051%，
        # 95 笔交易的 T+5 累计收益被系统性高估。
        self.stamp_duty = self.config.get('stamp_duty', 0.0005)     # 印花税（仅卖出）
        self.transfer_fee = self.config.get('transfer_fee', 0.00001)  # 过户费（买卖双向）
        # P0-D 修复（2026-09-05 审查报告）：卖出规则参数（来自 config.yml sell 段，
        # run_backtest 的 full_config 已合并 strategy_config）。此前止盈/止损/时间
        # 止损从未在回测中被模拟，卖出规则形同虚设——推荐按 T+3 持有，回测却按
        # 固定 T+1/T+5 口径统计，实盘路径与回测路径完全脱节。
        self.sell_config = self.config.get('sell', {}) or {}
        # 仓位口径（2026-09-18 新增）：
        #   normalized（默认）= allocated = base_equity × alloc/Σalloc —— 只认相对权重，
        #     **尺度不变**（tests/test_portfolio_sim_scale_invariance_20260916.py 锁定），
        #     衡量的是选股质量；副作用：任何仓位/sizing 类改动在此口径下不可验证。
        #   absolute = allocated = base_equity × alloc% —— 绝对仓位口径，未投出部分留现金，
        #     使"弱市压缩 0.5 / 波动保险丝 / 连亏压缩 / 拥挤度分档"等仓位机制**第一次可被
        #     回测验证**。⚠️ 与 normalized 不可直接比较历史净值（口径不同），A/B 需同口径。
        self.sizing_mode = str(self.config.get('sizing_mode', 'normalized')).lower()
        # 涨跌停可成交性建模（2026-09-18 全项目审查 P2-2 修复）：
        #   买入端：T+1 开盘价 ≥ 涨停价 → 视为不可买入（涨停封板无卖单），跳过该笔；
        #   持有端：一字跌停（open/high 均 ≤ 跌停价）→ 当日无法卖出，顺延到下一交易日。
        #   默认 True（更接近真实约束）；置 false 可复现历史口径（早期回测未建模）。
        self.tradability_check = bool(self.config.get('tradability_check', True))
        if self.sizing_mode not in ('normalized', 'absolute'):
            logger.warning(f"未知 sizing_mode={self.sizing_mode}，回落 normalized")
            self.sizing_mode = 'normalized'
        # P1-J 修复（2026-09-05 审查报告）：多基准。仅比沪深300 会掩盖小盘池超额，
        # 中证1000(000852) 与小市值候选池更可比。拉取失败自动跳过，不阻断回测
        # （mootdx 对指数代码的市场路由可能失败，属 best-effort）。
        self.benchmark_codes = self.config.get('benchmark_codes', ['399300'])
        # 架构对标 #5（2026-09-05）：成交口径与分层滑点
        # fill_convention: 'open_t1'=次日开盘买（唯一有效口径，默认，保持历史可比）
        #                  'close_t0'=T日尾盘收盘买 —— 已于 2026-09-17 移除：
        #                  该分支是死代码，且 L1119 漏 /100 形成杠杆 bug；同时
        #                  _simulate_portfolio 的 close_t0 分支仍向 _slippage_for 传
        #                  个股日成交额而非单笔委托金额（与缺陷1同错）。config 仍可
        #                  读此字段以兼容旧配置，但行为一律按 open_t1。
        self.fill_convention = self.config.get('fill_convention', 'open_t1')
        # slippage_tiers: [[单笔委托金额上限(元), 滑点], ...] 升序；缺省用固定 self.slippage
        #   ⚠️ 2026-09-17 修复（缺陷1）：档位表键是【单笔委托金额（元）】，不是股票
        #   日成交额；与 scripts/capacity_check.py::lookup_slippage 同语义。
        self.slippage_tiers = self.config.get('slippage_tiers')
        # 假设单笔委托金额（缺陷1，2026-09-17）：逐笔收益路径（_calculate_results）
        # 无仓位台账，用此估算分层滑点。config.assumed_order_value 显式给定优先；
        # 否则 = initial_capital / max(1, assumed_positions(默认3))，与 capacity_check
        # 等权简化 alloc = capital / n_stocks 一致。
        self.assumed_order_value = float(
            self.config.get('assumed_order_value')
            or (self.initial_capital / max(1, int(self.config.get('assumed_positions', 3))))
        )

    def _warmup_kline_cache(self, codes: list, start_date: str, end_date: str):
        """批量预热K线缓存：确保目标区间所有股票K线已缓存在SQLite"""
        db_path = self.data_engine._kline_cache_path
        if not os.path.exists(db_path):
            logger.warning(f"K线缓存不存在({db_path})，回测将使用已有数据")
            return

        # 查缓存中已有多少股票
        conn = sqlite3.connect(db_path)
        try:
            cached = conn.execute(
                "SELECT COUNT(DISTINCT code) FROM kline_cache WHERE date>=? AND date<=?",
                (start_date, end_date)
            ).fetchone()[0]
        finally:
            conn.close()

        coverage = cached / max(len(codes), 1) * 100
        if coverage >= 50:
            logger.info(f"K线缓存覆盖率 {coverage:.0f}% ({cached}/{len(codes)})，跳过预热")
        else:
            logger.info(f"K线缓存覆盖率仅 {coverage:.0f}% ({cached}/{len(codes)})，回测只能使用已缓存数据")

    def run(self, mode: str = 'short',
            start_date: str = "2025-01-01",
            end_date: str = "2025-12-31") -> BacktestResult:
        """
        运行回测

        参数：
          mode: 'short' / 'long'
          start_date/end_date: 回测区间

        返回：BacktestResult（全部真实数据，无 np.random）
        """
        logger.info(f"\n{'='*60}")
        logger.info(f"回测启动: {mode}模式 | {start_date} ~ {end_date}")
        logger.info(f"{'='*60}")

        # 1. 交易日历
        trade_calendar = self._get_trade_calendar(start_date, end_date)
        logger.info(f"交易日数量: {len(trade_calendar)}")

        if not trade_calendar:
            return self._empty_result(mode, start_date, end_date)

        # 2. 获取全市场代码列表 & 名称映射
        codes = self.data_engine._get_all_codes()
        if not codes:
            logger.error("无可用的股票代码列表")
            return self._empty_result(mode, start_date, end_date)
        logger.info(f"A股代码列表: {len(codes)} 只")

        # 取今日行情仅用于获取股票名称（名称不随日期变化）
        try:
            today_q = self.data_engine.get_all_quotes()
            name_map = dict(zip(today_q['code'], today_q['name']))
        except Exception:
            name_map = {}

        # 3. 初始化策略
        # 2026-09-18 修复：此块此前被误缩进到上方 except Exception: 体内，
        # 导致 strategy 仅在 get_all_quotes() 抛异常时才初始化——正常路径
        # strategy 未定义，逐日运行全部失败（0 交易假象）。
        if mode == 'short':
            strategy = ShortTermStrategy(self.config)
        else:
            strategy = LongTermStrategy(self.config)
        # P2-5 修复（2026-09-17）：--ablate 消融权重需在 ScoringModel 实例化后
        # 显式覆盖 —— ScoringModel 加载优先级 v1.json > config，config 传入的
        # 消融权重被 v1.json 静默覆盖 → 消融实际不生效（所有消融结果 = 基线）。
        _ablated = getattr(self, '_ablated_weights', None)
        if _ablated:
            strategy.scoring_model.weights = _ablated
            strategy.scoring_model._weights = _ablated  # 兼容属性名
            logger.info(f"消融权重已覆盖 ScoringModel: {len(_ablated)} 因子")

        # 4. 预热K线缓存 + 预加载历史快照
        self._warmup_kline_cache(codes, start_date, end_date)
        logger.info("构建历史行情快照...")
        snapshots = self._load_historical_snapshots(
            codes, name_map, start_date, end_date
        )
        logger.info(f"历史快照构建完成: {len(snapshots)} 个交易日")

        # 5. 逐日模拟
        all_records = []

        for i, trade_date in enumerate(trade_calendar):
            if (i + 1) % 20 == 0:
                logger.info(f"  进度: {i+1}/{len(trade_calendar)}")

            # 获取历史当日行情快照（基于真实K线数据）
            day_data = snapshots.get(trade_date)
            if day_data is None or day_data.empty:
                continue

            # 运行策略（传入回测模式标志 + 当前模拟日期）
            # 2026-09-17：附带当日热点历史（hot_stocks 表），使回测模式下
            # hot_theme 不再恒中性（修复"回测 0 交易"+"因子从未被验证"）
            _hot_today = {c for (d, c) in getattr(self, '_hot_lookup', set()) or set()
                          if d == trade_date}
            try:
                recommendations = strategy.run({
                    'quotes_df': day_data,
                    'backtest_mode': True,
                    'backtest_date': trade_date,
                    'hot_codes': _hot_today,
                })
            except Exception as e:
                logger.warning(f"  {trade_date} 策略运行失败: {str(e)[:60]}")
                continue

            # 记录推荐（带仓位分配）
            for rec in recommendations:
                all_records.append({
                    'date': trade_date,
                    'code': rec.get('code', ''),
                    'name': rec.get('name', ''),
                    'score': rec.get('score', 0),
                    'rating': rec.get('rating', ''),
                    'buy_price': rec.get('price', 0),
                    'allocation_pct': rec.get('allocation_pct', 0),
                    'factor_breakdown': rec.get('breakdown', {}),
                })

        # 6. 计算收益指标 + 仓位模拟
        #    _calculate_results 保留原有的逐笔胜率/收益统计（不依赖仓位假设）
        #    _simulate_portfolio 提供真实的仓位模拟指标
        base_result = self._calculate_results(all_records, trade_calendar, mode,
                                              start_date, end_date)

        # 仓位模拟
        pf = self._simulate_portfolio(all_records)

        result = BacktestResult(
            strategy_name=base_result.strategy_name,
            period=base_result.period,
            total_trading_days=len(trade_calendar),
            total_trades=base_result.total_trades,
            win_rate=base_result.win_rate,
            avg_return_t1=base_result.avg_return_t1,
            avg_return_t5=base_result.avg_return_t5,
            max_win_t1=base_result.max_win_t1,
            max_loss_t1=base_result.max_loss_t1,
            # 修复（2026-09-06 全量审查）：P0-D 规则口径统计已由 _calculate_results
            # 算出，但此前未回传最终对象，报告层读到的恒为 0/空
            avg_return_rule=base_result.avg_return_rule,
            win_rate_rule=base_result.win_rate_rule,
            rule_exit_breakdown=base_result.rule_exit_breakdown,
            benchmarks=base_result.benchmarks,
            max_drawdown=pf['max_drawdown'],
            sharpe_ratio=pf['sharpe_ratio'],
            benchmark_return=base_result.benchmark_return,
            strategy_return=pf['total_return'],
            excess_return=round(pf['total_return'] - base_result.benchmark_return, 2),
            monthly_returns=pf['monthly_returns'],
            equity_curve=pf['equity_curve'],
            factor_performance=base_result.factor_performance,
            trade_details=pf['trade_details'],  # 全量明细（此前 [:50] 截断，明细表只有前50条）
            benchmark_available=base_result.benchmark_available,
        )

        logger.info(f"\\n回测完成: 胜率 {result.win_rate:.1f}% | "
                     f"平均收益 {result.avg_return_t1:+.2f}% | "
                     f"交易次数 {result.total_trades} | "
                     f"组合收益 {result.strategy_return:+.2f}%")
        return result

    # ── 历史量比计算 ─────────────────────────────────────────

    @staticmethod
    def _calc_volume_ratio(kline: pd.DataFrame, idx: int) -> float:
        """从 K 线序列计算当日量比（当日成交量 / 过去 5 日均量）

        P1-H 修复（2026-09-05 审查报告）：原用 20 日均量，而实盘 volume_ratio
        来自行情源字段（腾讯 field 49 / 通达信）为 5 日均量口径，OOS validator
        也按 5 日实现 → 同一因子三处两套定义，IC 校准出的权重对回测口径失真。
        统一为 5 日（不含当日）。
        """
        today_vol = float(kline.iloc[idx]['volume']) if 'volume' in kline.columns else 0
        if today_vol <= 0:
            return 1.0
        # 取过去 5 天（不含当日）
        lookback = max(0, idx - 5)
        past = kline.iloc[lookback:idx]['volume']
        if len(past) < 2:
            return 1.0
        avg_vol = float(past.mean())
        if avg_vol <= 0:
            return 1.0
        return today_vol / avg_vol

    def _load_historical_snapshots(self, codes: list, name_map: dict,
                                    start_date: str, end_date: str) -> Dict[str, pd.DataFrame]:
        """
        预加载所有股票的历史 K 线（仅从 SQLite 缓存读取，不触发 mootdx 降级），
        构建 {日期 -> 当日行情快照} 字典。

        每个快照 DataFrame 包含字段：
          code, name, price(=close), pct_chg, amount, volume,
          turnover(=0), volume_ratio(=1), pe/pb(=None)

        限制：
          - 无历史 PE/PB/换手率数据，用默认值替代
          - 名称取自今日行情（名称通常不变）
          - 股价=收盘价（非盘中实时价）
        """
        db_path = self.data_engine._kline_cache_path
        if not os.path.exists(db_path):
            logger.warning(f"K线缓存不存在: {db_path}")
            return {}

        total = len(codes)
        kline_dict = {}
        kline_date_index = {}   # {code: {date_str: 行号}} — P2-2 优化

        # 批量读取：一次性拉取所有股票在目标区间的 K 线
        # 通过 SQLite 的 WHERE IN 拼接加速
        codes_batches = [codes[i:i+500] for i in range(0, len(codes), 500)]
        for cb in codes_batches:
            try:
                placeholders = ','.join(['?' for _ in cb])
                conn = sqlite3.connect(db_path)
                try:
                    df_all = pd.read_sql_query(
                        f"SELECT code, date, open, high, low, close, volume, amount "
                        f"FROM kline_cache "
                        f"WHERE code IN ({placeholders}) AND date>=? AND date<=? "
                        f"ORDER BY code, date",
                        conn, params=[str(c).zfill(6) for c in cb] + [start_date, end_date]
                    )
                finally:
                    conn.close()
                if not df_all.empty:
                    df_all['date'] = pd.to_datetime(df_all['date'])
                    df_all['date_str'] = df_all['date'].dt.strftime('%Y-%m-%d')
                    for c in ['open', 'high', 'low', 'close', 'volume', 'amount']:
                        df_all[c] = pd.to_numeric(df_all[c], errors='coerce')
                    # 按 code 分组存入字典
                    for code in cb:
                        code_str = str(code).zfill(6)
                        sub = df_all[df_all['code'] == code_str]
                        if len(sub) >= 3:
                            kline_dict[code_str] = sub.reset_index(drop=True)
                            # 优化（2026-09-05 审查 P2-2）：预建 {date_str: 行号} 索引。
                            # 旧实现逐日对每只股票做全表布尔扫描
                            # kline[kline['date_str']==date_str]（O(D×N×rows)），
                            # 是回测启动的主要瓶颈；索引化后查找 O(1)。
                            kline_date_index[code_str] = dict(zip(
                                sub['date_str'], range(len(sub))))
            except Exception as e:
                logger.warning(f"历史快照加载异常: {str(e)[:80]}")
                pass

            if (len(kline_dict) % 1000) == 0 or len(kline_dict) == 0 and len(codes_batches) == 1:
                logger.info(f"  历史快照加载: {len(kline_dict)} 只有效K线")

        logger.info(f"  历史快照加载完成: {len(kline_dict)}/{total} 只有效K线")

        # 加载因子仓库数据（如果存在）
        factor_data = self._load_factor_data(start_date, end_date)
        cf_lookup = factor_data.get('capital_flow', {})   # {(date, code): value}
        nf_lookup = factor_data.get('north_flow', {})     # {(date, code): value}
        hot_lookup = factor_data.get('hot_stocks', set())  # set of (date, code)
        # 2026-09-17：把热点历史暴露给 run()——此前回测模式下 short_term 无条件清空
        # 热点，hot_theme（权重 0.50）恒中性 → 回测 0 交易 + 该因子从未被回测验证。
        # hot_stocks 历史已于 2026-09-17 回填（2024-01 起 658 天），可以接管。
        self._hot_lookup = hot_lookup

        # 构建 {date: DataFrame}
        trade_dates = sorted(set(
            ds for kline in kline_dict.values() for ds in kline['date_str'].unique()
        ))
        snapshots = {}

        for date_str in trade_dates:
            rows = []
            for code, kline in kline_dict.items():
                # P2-2 优化：O(1) 索引查找替代全表布尔扫描
                idx = kline_date_index[code].get(date_str)
                if idx is None:
                    continue
                r = kline.iloc[idx]
                prev_close = None
                if idx > 0:
                    prev_close = float(kline.iloc[idx - 1]['close'])
                pct_chg = ((float(r['close']) - prev_close) / prev_close * 100) if prev_close and prev_close > 0 else 0.0

                # 查找因子仓库数据
                code_str = str(code).zfill(6)
                cf_val = cf_lookup.get((date_str, code_str))
                nf_val = nf_lookup.get((date_str, code_str))
                is_hot = (date_str, code_str) in hot_lookup

                row = {
                    'code': code_str,
                    'name': name_map.get(code_str, ''),
                    'price': float(r['close']),
                    'pct_chg': round(pct_chg, 2),
                    'amount': float(r.get('amount', 0)),
                    'turnover': 0.0,
                    'volume_ratio': round(self._calc_volume_ratio(kline, idx), 2),
                    'volume': float(r.get('volume', 0)),
                    # P4（2026-09-18）：当日最高/最低——尾盘急拉过滤（close_pos）用，
                    # 与实盘 quotes 的 high/low 字段对齐
                    'high': float(r['high']) if r.get('high') is not None else None,
                    'low': float(r['low']) if r.get('low') is not None else None,
                    'pe': None,
                    'pb': None,
                    'name_raw': name_map.get(code_str, ''),
                }

                # 如果因子仓库中有数据，附加到快照
                if cf_val is not None:
                    row['main_fund_accumulated'] = cf_val
                if nf_val is not None:
                    row['north_flow_accumulated'] = nf_val
                if is_hot:
                    row['is_hot_stock'] = True

                rows.append(row)

            if rows:
                snapshots[date_str] = pd.DataFrame(rows)

        return snapshots

    def _load_factor_data(self, start_date: str, end_date: str) -> Dict:
        """从因子仓库批量加载历史因子数据"""
        factor_db = os.path.join(
            os.path.dirname(os.path.dirname(__file__)),
            'data', 'cache', 'factor_daily.db'
        )
        result = {
            'capital_flow': {},
            'north_flow': {},
            'hot_stocks': set(),
            'dragon_tiger': {},   # {(date, code): dict} — 2026-09-05 新增
        }
        if not os.path.exists(factor_db):
            return result

        conn = None
        try:
            conn = sqlite3.connect(factor_db)

            # 资金流
            for row in conn.execute(
                "SELECT date, code, accumulated_net FROM capital_flow "
                "WHERE date>=? AND date<=?", (start_date, end_date)
            ):
                result['capital_flow'][(row[0], row[1])] = row[2]

            # 北向
            for row in conn.execute(
                "SELECT date, code, holding_change FROM north_flow "
                "WHERE date>=? AND date<=?", (start_date, end_date)
            ):
                result['north_flow'][(row[0], row[1])] = row[2]

            # 热点
            for row in conn.execute(
                "SELECT date, code FROM hot_stocks "
                "WHERE date>=? AND date<=?", (start_date, end_date)
            ):
                result['hot_stocks'].add((row[0], row[1]))

            # 龙虎榜（2026-09-05 新增）
            # 注意：该表只记录当日进入推荐池的标的，覆盖远窄于资金流/热点，
            # 命中率天然极低。保留路径是为了让 dragon_tiger 因子在回测里
            # 有机会出现真实方差，而不是永远中性。
            try:
                for row in conn.execute(
                    "SELECT date, code, net_buy_wan, institution_net_wan, has_record "
                    "FROM dragon_tiger WHERE date>=? AND date<=?", (start_date, end_date)
                ):
                    result['dragon_tiger'][(row[0], row[1])] = {
                        'net_buy_wan': row[2],
                        'institution_net_wan': row[3],
                        'has_record': bool(row[4]),
                    }
            except sqlite3.Error:
                pass  # 旧库无该列时静默跳过，不阻断回测
        except Exception as e:
            logger.warning(f"因子仓库加载失败: {e}")
        finally:
            if conn:
                conn.close()

        cf = len(result['capital_flow'])
        nf = len(result['north_flow'])
        hs = len(result['hot_stocks'])
        dt = len(result['dragon_tiger'])
        if cf or nf or hs or dt:
            logger.info(f"因子仓库加载: 资金流{cf}条, 北向{nf}条, 热点{hs}条, 龙虎榜{dt}条")

        return result

    # ── 成交假设（架构对标 #5）────────────────────────────────

    @staticmethod
    def _limit_pct(code: str) -> float:
        """涨跌停幅度（2026-09-18 审查 P2-2）：与 risk_filter/策略口径一致。"""
        c = str(code).zfill(6)
        if c.startswith(('300', '301', '688', '689')):
            return 0.20          # 创业板/科创板 20cm
        if c.startswith(('4', '8', '92')):
            return 0.30          # 北交所 30cm
        return 0.10              # 主板 10cm

    def _limit_price(self, code: str, prev_close: float, up: bool) -> float:
        """涨/跌停价（A 股按前收盘价 × (1±幅度) 四舍五入到分）。"""
        pct = self._limit_pct(code)
        pct = pct if up else -pct
        try:
            return round(float(prev_close) * (1 + pct), 2)
        except (TypeError, ValueError):
            return float('nan')

    def _buy_blocked_by_limit_up(self, code: str, prev_close, fill) -> bool:
        """买入端：开盘价 ≥ 涨停价 → 封板无卖单，不可买入（保守建模）。"""
        if not self.tradability_check or not prev_close or not fill:
            return False
        lp = self._limit_price(code, prev_close, up=True)
        return bool(lp == lp and fill >= lp - 1e-6)

    def _exit_blocked_by_limit_down(self, code: str, prev_close, o, h) -> bool:
        """持有端：一字跌停（open 与 high 均在跌停价） → 当日无买盘，无法卖出。

        只拦"一字跌停"（open<=跌停 且 high<=跌停）；盘中触及跌停但曾打开
        （high > 跌停价）仍可按规则成交——这是保守但不过度悲观的假设。
        """
        if not self.tradability_check or not prev_close:
            return False
        ld = self._limit_price(code, prev_close, up=False)
        if ld != ld:
            return False
        try:
            return float(o) <= ld + 1e-6 and float(h) <= ld + 1e-6
        except (TypeError, ValueError):
            return False

    def _slippage_for(self, order_value) -> float:
        """按【单笔委托金额】取分层滑点，与 scripts/capacity_check.py::lookup_slippage 同语义。

        slippage_tiers 形如 [[2e6, 0.001], [1e7, 0.002], [1e8, 0.004], [1e12, 0.008]]
        （升序）。档位表键是【单笔委托金额（元）】，不是股票日成交额：单笔委托金额
        ≤ 某档上限即用该档滑点；未配置时用固定 self.slippage。

        ⚠️ 2026-09-17 修复（回测口径 P0-1）：旧实现三个调用点传入的是【个股当日成交额】
        （kline.iloc[i]['amount']，量级 1e8），导致所有候选日成交额 ≥ 3e7 全落最高两档
        （0.4%~0.8%/边），分级滑点退化成近似固定高滑点。正确入参应为【单笔委托金额】
        （初始资金 100万 / 约 3 只 ≈ 33万 → 应落最低档 0.1%）。本函数参数名
        amount → order_value 仅为澄清语义；调用点已同步改为传单笔委托金额
        （逐笔路径用 self.assumed_order_value，组合买入路径用 allocated）。
        """
        if not self.slippage_tiers:
            return self.slippage
        try:
            a = float(order_value) if order_value is not None else 0.0
        except (TypeError, ValueError):
            a = 0.0
        for upper, slip in sorted(self.slippage_tiers, key=lambda t: float(t[0])):
            if a <= float(upper):
                return float(slip)
        return float(self.slippage_tiers[-1][1])

    # ── 卖出规则路径模拟（P0-D，2026-09-05 审查报告）────────────────
    # 背景：策略卖出规则（止盈 +2% / 止损 -2% / T+3 时间止损）此前从未在
    # 回测中被模拟。本纯函数供逐笔收益统计与组合模拟统一复用。

    @staticmethod
    def _simulate_exit_path(kline: pd.DataFrame, entry_idx: int, fill_price: float,
                            take_profit: float = 0.02, stop_loss: float = -0.02,
                            max_hold_days: int = 3,
                            stop_price_abs: float = None):
        """按卖出规则模拟单笔持仓的退出路径（纯函数，无 IO，可单测）。

        参数：
            kline: 决策日起的 K 线（reset_index 后，含 open/high/low/close）
            entry_idx: 买入日所在行号（open_t1 口径=1，close_t0 口径=0）
            fill_price: 实际成交价（不含费用，费用由调用方统一计算）
            take_profit / stop_loss: 小数（0.02 = +2%），相对 fill_price
            max_hold_days: 时间止损交易日数（T+3 = 买入后第 3 个交易日收盘退出）
            stop_price_abs: 可选绝对止损价（P2-N ATR 自适应模式传入）。
                提供时覆盖 stop_loss 比例价；仅影响止损线，止盈/时间止损不变。

        返回：(exit_price, exit_idx, exit_reason)
            exit_reason ∈ {'take_profit', 'take_profit_gap', 'stop_loss',
                           'stop_loss_gap', 'time_stop', 'no_data', 'invalid_input'}

        规则细节：
          - 严格 A 股 T+1 制度：买入日（entry_idx）不可卖出，检查从 entry_idx+1 开始
          - 跳空穿越：开盘已越过阈值时按开盘价成交（不假设能以触发价成交——
            跳空低开时止损单实际成交价更差，这是真实约束不是悲观假设）
          - 同日双触发（high 触止盈、low 触止损但先后不可知）：保守取止损优先，
            避免高估；这是回测保守性原则的体现
        """
        n = len(kline)
        if fill_price is None or fill_price <= 0 or entry_idx is None:
            return None, None, 'invalid_input'
        tp_price = fill_price * (1 + take_profit)
        sl_price = stop_price_abs if stop_price_abs is not None \
            else fill_price * (1 + stop_loss)

        # 退出窗口：entry_idx+1 .. entry_idx+max_hold_days（含时间止损日）
        last_idx = min(entry_idx + max(1, int(max_hold_days)), n - 1)
        if last_idx <= entry_idx:
            return None, None, 'no_data'

        for i in range(entry_idx + 1, last_idx + 1):
            row = kline.iloc[i]
            o = float(row['open'])
            h = float(row['high'])
            l = float(row['low'])
            if o <= sl_price:
                return o, i, 'stop_loss_gap'
            if o >= tp_price:
                return o, i, 'take_profit_gap'
            hit_sl = l <= sl_price
            hit_tp = h >= tp_price
            if hit_sl:               # 双触发保守取止损
                return sl_price, i, 'stop_loss'
            if hit_tp:
                return tp_price, i, 'take_profit'
        # 未触发止盈/止损 → 时间止损日收盘退出
        return float(kline.iloc[last_idx]['close']), last_idx, 'time_stop'

    @staticmethod
    def _atr_from_kline(kline: pd.DataFrame, upto_idx: int):
        """买入日（含）之前的 14 日 ATR（TR 简单均值）。

        P2-N 配套：无前视——只用 <= upto_idx 的行。行数不足（<15 行历史）
        返回 None，调用方回退固定止损。
        """
        if upto_idx is None or upto_idx < 14:
            return None
        try:
            w = kline.iloc[max(0, upto_idx - 14):upto_idx + 1]
            high = w['high'].astype(float)
            low = w['low'].astype(float)
            close = w['close'].astype(float)
            prev_close = close.shift(1)
            tr = pd.concat([high - low,
                            (high - prev_close).abs(),
                            (low - prev_close).abs()], axis=1).max(axis=1)
            atr = float(tr.iloc[1:].tail(14).mean())   # 首行 prev_close 为 NaN 跳过
            return atr if atr > 0 else None
        except Exception as e:
            # 稳定性（2026-09-06 迭代）：原裸 except 静默吞错返回 None，
            # "无数据"与"代码 bug"无法区分（ATR 止损被静默关闭且无痕）
            logger.debug(f"ATR 计算失败（回退固定止损）: {type(e).__name__}: {str(e)[:80]}")
            return None

    def _get_trade_calendar(self, start: str, end: str) -> List[str]:
        """获取真实交易日历。

        稳定性（2026-09-06 迭代）：优先用 core.trading_calendar（带本地持久化
        缓存，联网一次即可反复用）；仅当日历与网络都不可用时才降级为
        "周一到周五"近似——该近似会把法定长假休市日与调休补班日误判为
        交易日，total_trading_days 与组合日期轴都会失真，因此降级时打
        ERROR 级日志明确标注结果不可信。
        """
        try:
            from core.trading_calendar import trading_days
            days = trading_days(start, end)
            if days is not None and days:
                return days
        except Exception as e:
            logger.warning(f"core.trading_calendar 不可用: {str(e)[:60]}")
        try:
            import akshare as ak
            df = ak.tool_trade_date_hist_sina()
            df['trade_date'] = pd.to_datetime(df['trade_date']).dt.date
            mask = (df['trade_date'] >= pd.Timestamp(start).date()) & (df['trade_date'] <= pd.Timestamp(end).date())
            return df[mask]['trade_date'].astype(str).tolist()
        except Exception as e:
            logger.error(
                f"交易日历获取失败（akshare + 本地缓存均不可用），降级为"
                f"'周一到周五'近似——法定长假/调休将被误判，回测结果不可信，"
                f"请恢复网络或人工检查日历缓存后重跑。原因: {e}")
            start_dt = datetime.strptime(start, '%Y-%m-%d')
            end_dt = datetime.strptime(end, '%Y-%m-%d')
            dates = []
            current = start_dt
            while current <= end_dt:
                if current.weekday() < 5:
                    dates.append(current.strftime('%Y-%m-%d'))
                current += timedelta(days=1)
            return dates

    def _locate_tn_rows(self, kline: pd.DataFrame, buy_date: str, t5_needed: bool = True):
        """T1（2026-09-06 第一梯队）：按交易日历定位 T+1/T+5 目标行。

        背景：原实现用位置索引（kline.iloc[1]/iloc[5]），当 K 线缓存存在
        ≤10 个工作日的断档（停牌/漏抓，低于残缺检测阈值）时，iloc[5] 实际
        对应 T+8，收益被静默算错。

        规则（正常序列下与旧口径完全一致）：
          1. 用交易日历计算决策日 buy_date 之后第 1 / 第 5 个交易日目标日；
          2. 在 kline 中精确匹配目标日期；
          3. 无该日（停牌/缺口）→ 顺延取 kline 中晚于目标日的最近行，
             顺延超过 3 个交易日则返回 None（该笔弃算，不再静默错位）；
          4. 交易日历不可用 → 回退旧的位置索引（行为与历史完全一致）。

        返回 (t1_idx, t5_idx)。t1_idx 为 None 表示该笔应弃算。
        """
        try:
            from core.trading_calendar import trading_days
        except Exception:
            return self._locate_tn_rows_fallback(kline)

        try:
            end_look = (datetime.strptime(buy_date, '%Y-%m-%d')
                        + timedelta(days=40)).strftime('%Y-%m-%d')
            cal = trading_days(buy_date, end_look)
            if not cal:
                return self._locate_tn_rows_fallback(kline)
            # 2026-09-07 回访修复：日历缓存有年份覆盖下限（_MIN_YEAR=2024），
            # buy_date 早于覆盖范围时 cal 非空但 future[0] 会指向错误年份，
            # 必须校验首个目标日确实紧跟 buy_date（间隔 ≤7 自然日），
            # 否则回退位置索引而非静默错位。
            future = [d for d in cal if d > buy_date]
            if not future:
                return None, None
            from datetime import datetime as _dt, timedelta as _td
            if (_dt.strptime(future[0], '%Y-%m-%d')
                    - _dt.strptime(buy_date, '%Y-%m-%d')) > _td(days=7):
                return self._locate_tn_rows_fallback(kline)
            t1_target = future[0]
            t5_target = future[4] if (t5_needed and len(future) >= 5) else None

            dates = pd.to_datetime(kline['date']).dt.strftime('%Y-%m-%d')
            date_list = dates.tolist()

            def _find(target):
                if target is None:
                    return None
                # 精确匹配
                if target in date_list:
                    return date_list.index(target)
                # 顺延：kline 中晚于目标日的最近行，且顺延 ≤3 个交易日
                after = [(i, d) for i, d in enumerate(date_list) if d > target]
                if not after:
                    return None
                idx, found = after[0]
                gap = trading_days(target, found)
                if gap is None or len(gap) - 1 > 3:   # 顺延超过 3 个交易日 → 弃算
                    return None
                return idx

            t1_idx = _find(t1_target)
            t5_idx = _find(t5_target) if t5_needed else None
            return t1_idx, t5_idx
        except Exception as e:
            logger.warning(f"T+N 交易日定位失败（回退位置索引）: {str(e)[:60]}")
            return self._locate_tn_rows_fallback(kline)

    @staticmethod
    def _locate_tn_rows_fallback(kline: pd.DataFrame):
        """日历不可用时的回退：与旧位置索引口径完全一致。"""
        t1_idx = 1 if len(kline) > 1 else None
        t5_idx = 5 if len(kline) > 5 else None
        return t1_idx, t5_idx

    def _calculate_results(self, records: List[Dict],
                           trade_calendar: List[str],
                           mode: str, start: str, end: str) -> BacktestResult:
        """计算回测结果指标（全部基于真实K线数据）"""
        n_trades = len(records)

        if n_trades == 0:
            return self._empty_result(mode, start, end)

        # 逐条查询 T+1/T+5 真实收益
        trade_returns_t1 = []
        trade_returns_t5 = []
        trade_details = []

        for rec in records:
            code = rec['code']
            buy_date = rec['date']
            buy_price = rec['buy_price']

            if buy_price <= 0:
                continue

            # 拉取买入日后 30 天 K 线（复用 DataEngine 缓存）
            end_look = (datetime.strptime(buy_date, '%Y-%m-%d') + timedelta(days=40)).strftime('%Y-%m-%d')
            kline = self.data_engine.get_kline(code, start_date=buy_date, end_date=end_look)

            if kline is None or kline.empty or len(kline) < 2:
                continue

            kline = kline.reset_index(drop=True)

            # T1（2026-09-06 第一梯队）：T+1/T+5 按交易日历定位（缺口顺延/弃算），
            # 正常连续序列下与旧位置索引完全一致
            t1_idx, t5_idx = self._locate_tn_rows(kline, buy_date)
            if t1_idx is None:
                logger.debug(f"T+N 定位失败弃算: {code}@{buy_date}（缺口顺延超限）")
                continue
            open_t1 = float(kline.iloc[t1_idx]['open'])
            close_t1 = float(kline.iloc[t1_idx]['close'])

            # 成交口径（架构对标 #5）：统一用 open_t1（T+1 开盘买入），与 _simulate_portfolio
            # 完全一致。close_t0（T 日尾盘收盘买）已于 2026-09-17 移除：该分支是死代码，且
            # 逐笔路径用 T 日收盘价成交属前视（收盘前无法执行）；config 仍可读 fill_convention
            # 字段以兼容旧配置，但两条路径行为一律按 open_t1。
            fill_price = open_t1
            # 2026-09-17 修复（缺陷1）：逐笔路径无仓位台账，滑点分档用【单笔委托金额】
            # 假设（self.assumed_order_value = 初始资金/持仓数），与 capacity_check 等权
            # 简化 alloc 一致；不再传个股当日成交额（旧口径会把所有候选推到最高滑点档）。
            slip = self._slippage_for(self.assumed_order_value)

            if fill_price and close_t1 and buy_price > 0:
                # P0-C 修复（2026-09-05 审查报告）：补齐印花税（仅卖出）+ 过户费（双向）。
                # 原公式只含佣金+滑点，单笔双边成本低估约 0.051%，95 笔累计高估约 5%。
                effective_buy = fill_price * (1 + slip) * (1 + self.commission + self.transfer_fee)
                effective_sell = close_t1 * (1 - slip) * (1 - self.commission - self.transfer_fee - self.stamp_duty)
                ret_t1 = (effective_sell / effective_buy - 1) * 100
                trade_returns_t1.append(ret_t1)
            else:
                continue

            # T+5（以同一口径买价为基础）
            # P0-C 修复：T+5 收益此前是裸价格比（完全不含成本），与 T+1 口径不一致。
            if t5_idx is not None:
                close_t5 = float(kline.iloc[t5_idx]['close'])
                if fill_price and close_t5 > 0:
                    effective_sell_t5 = close_t5 * (1 - slip) * (
                        1 - self.commission - self.transfer_fee - self.stamp_duty)
                    ret_t5 = (effective_sell_t5 / effective_buy - 1) * 100
                else:
                    ret_t5 = None
                if ret_t5 is not None:
                    trade_returns_t5.append(ret_t5)
            else:
                trade_returns_t5.append(None)

            # P0-D：按策略卖出规则（止盈/止损/时间止损）模拟真实持仓路径。
            # 与 T+1/T+5 固定口径并列输出 return_rule，卖出规则的有效性首次
            # 可直接验证。费用口径与 T+1 相同（复用 effective_buy 买入端）。
            # 注意：严格 T+1 制度下退出检查从买入次日开始（见 _simulate_exit_path）。
            # close_t0 已移除（2026-09-17）：统一 T+1 入场，与 fill_price=open_t1、
            # 与 _simulate_portfolio 的 open_t1 口径完全一致；不再响应 fill_convention。
            entry_idx = 1
            # P2-N：ATR 自适应止损（stop_mode='atr'），止损宽度 = max(atr_mult×ATR, 固定距离)
            stop_abs = None
            if self.sell_config.get('stop_mode', 'fixed') == 'atr':
                atr = self._atr_from_kline(kline, entry_idx)
                if atr is not None:
                    sl_pct = abs(self.sell_config.get('stop_loss', -0.02))
                    mult = self.sell_config.get('atr_mult', 1.0)
                    stop_abs = min(fill_price * (1 - sl_pct), fill_price - mult * atr)
            exit_price, _exit_i, exit_reason = self._simulate_exit_path(
                kline, entry_idx, fill_price,
                take_profit=self.sell_config.get('take_profit', 0.02),
                stop_loss=self.sell_config.get('stop_loss', -0.02),
                max_hold_days=self.sell_config.get('time_stop_days', 3),
                stop_price_abs=stop_abs,
            )
            if exit_price:
                effective_sell_rule = exit_price * (1 - slip) * (
                    1 - self.commission - self.transfer_fee - self.stamp_duty)
                ret_rule = (effective_sell_rule / effective_buy - 1) * 100
            else:
                ret_rule = None
                exit_reason = 'no_data'

            trade_details.append({
                'date': buy_date,
                'code': code,
                'name': rec.get('name', ''),
                'score': rec.get('score', 0),
                'buy_price': buy_price,
                'open_t1': open_t1,
                'close_t1': close_t1,
                'return_t1': round(ret_t1, 2) if ret_t1 is not None else None,
                # P0-D：卖出规则路径口径（止盈/止损/时间止损，严格 T+1）
                'return_rule': round(ret_rule, 2) if ret_rule is not None else None,
                'exit_reason': exit_reason,
            })

        if not trade_returns_t1:
            return self._empty_result(mode, start, end)

        # 统计指标
        returns_arr = np.array(trade_returns_t1)
        win_rate = np.mean(returns_arr > 0) * 100
        avg_ret = np.mean(returns_arr)
        max_win = np.max(returns_arr)
        max_loss = np.min(returns_arr)

        # 策略总收益（复合计算）
        cumulative = (1 + returns_arr / 100).prod()
        strategy_return = (cumulative - 1) * 100

        # （2026-09-06 审查清理）此处原有一段"逐笔收益当日频 ×sqrt(252)"的
        # Sharpe 计算——方法论错误（P0-D 重写时已指出）且结果从未被返回，
        # run() 最终用 _simulate_portfolio 的组合口径 sharpe_ratio 覆盖。已删除。
        # 逐笔路径不再计算 Sharpe（口径错误且未被使用），置 0.0 以匹配 BacktestResult
        # 必填字段；组合级 Sharpe 由 _simulate_portfolio 提供。
        sharpe = 0.0

        # 最大回撤
        cumulative_series = (1 + returns_arr / 100).cumprod()
        rolling_max = np.maximum.accumulate(cumulative_series)
        drawdowns = (cumulative_series - rolling_max) / rolling_max
        max_dd = np.min(drawdowns) * 100 if len(drawdowns) > 0 else 0.0

        # 基准收益（P1-J 多基准：默认沪深300 + 中证1000，主基准 = 首个成功者）
        # P3-9：失败返回 None → benchmark_available=False，超额收益不可信
        benchmarks = self._calc_benchmark_returns(start, end)
        primary_code = next((c for c, v in benchmarks.items() if v is not None), None)
        benchmark_return = benchmarks.get(primary_code) if primary_code else None
        benchmark_available = benchmark_return is not None
        if benchmark_return is None:
            benchmark_return = 0.0

        # 超额收益（仅在基准可用时才有意义）
        excess_return = strategy_return - benchmark_return

        # T+5 统计
        t5_valid = [r for r in trade_returns_t5 if r is not None]
        avg_ret_t5 = np.mean(t5_valid) if t5_valid else 0.0

        # P0-D：卖出规则路径口径统计（止盈/止损/时间止损的收益与原因分布）
        rule_valid = [d.get('return_rule') for d in trade_details
                      if d.get('return_rule') is not None]
        avg_ret_rule = float(np.mean(rule_valid)) if rule_valid else 0.0
        win_rate_rule = float(np.mean(np.array(rule_valid) > 0) * 100) if rule_valid else 0.0
        exit_reason_counts = {}
        for d in trade_details:
            reason = d.get('exit_reason', 'unknown')
            exit_reason_counts[reason] = exit_reason_counts.get(reason, 0) + 1

        # 月度收益
        # 修复（2026-09-05 审查 P2-1）：此前传 records + trade_returns_t1 做 zip，
        # 但 _calculate_results 会因缺 K 线/停牌 skip 中间记录，两者一一对应关系
        # 断裂（C 的收益被记到 B 的月份）。trade_details 与收益同步 append，
        # 天然对齐，改用它做月度归因。
        monthly_returns = self._calc_monthly_returns(trade_details)

        # 因子归因 — 统计各因子在赢/输单中的平均分差
        factor_performance = self._calc_factor_performance(trade_details, records)

        return BacktestResult(
            strategy_name=f"{mode}_strategy",
            period=(start, end),
            total_trading_days=len(trade_calendar),
            total_trades=len(trade_details),
            win_rate=round(win_rate, 2),
            avg_return_t1=round(avg_ret, 2),
            avg_return_t5=round(avg_ret_t5, 2),
            # P0-D：卖出规则路径口径
            avg_return_rule=round(avg_ret_rule, 2),
            win_rate_rule=round(win_rate_rule, 2),
            rule_exit_breakdown=exit_reason_counts,
            max_win_t1=round(max_win, 2),
            max_loss_t1=round(max_loss, 2),
            max_drawdown=round(max_dd, 2),
            sharpe_ratio=round(sharpe, 2),
            benchmark_return=round(benchmark_return, 2),
            strategy_return=round(strategy_return, 2),
            excess_return=round(excess_return, 2),
            benchmarks=benchmarks,
            monthly_returns=monthly_returns,
            factor_performance=factor_performance,
            trade_details=trade_details[:50],  # 前50条明细
            benchmark_available=benchmark_available,
        )

    # ── 仓位模拟回测 ─────────────────────────────────────────────

    def _simulate_portfolio(self, records: List[Dict]) -> Dict:
        """
        持仓台账模式逐日模拟（P0-D 重写，2026-09-05 审查报告）

        此前版本的问题（报告 P0-5 / P0-D）：
          1. 单日强制平仓（T+1 收盘全卖）→ 策略卖出规则（止盈/止损/T+3）从未生效
          2. 只统计有推荐的日期 → 空仓日缺失，把"每笔交易收益"当"日频收益"
             算 Sharpe，4.36 的年化夏普被严重高估
          3. 成本漏计印花税/过户费

        重写后语义：
          - 决策日 T 推荐 → 下一交易日开盘买入（open_t1）；close_t0 = 当日尾盘买入
          - 按策略卖出规则持仓：止盈/止损/时间止损，严格 A 股 T+1 制度
            （买入日不可卖，退出检查从买入次日开始）
          - 全区间逐日 mark-to-market（含空仓日），daily_returns 覆盖每一天
          - 成本口径与 _calculate_results 一致（佣金+滑点+印花税+过户费）
          - 按比例成交假设：不建模 100 股整数手与涨跌停无法成交
            （容量/可交易性属 P3-S 容量测算范畴）

        返回：
            equity_curve:     每日收盘总净值列表（覆盖全区间）
            total_return:     总收益率 (%)
            max_drawdown:     最大回撤 (%)
            sharpe_ratio:     夏普比率（年化，日频样本含空仓日）
            monthly_returns:  月度收益明细
            trade_details:    每笔交易明细（含仓位占比、实际盈亏、退出原因）
        """
        if not records:
            return self._empty_portfolio_result()

        logger.info(f"组合模拟口径: sizing_mode={self.sizing_mode}"
                    + ("（按 alloc% 绝对投入，未投出留现金——仓位机制可验证）"
                       if self.sizing_mode == 'absolute'
                       else "（按当日委托合计归一化——尺度不变，仅衡量选股质量）"))

        # 按日期分组
        from collections import defaultdict
        by_date = defaultdict(list)
        for rec in records:
            if rec.get('allocation_pct', 0) > 0:
                by_date[rec['date']].append(rec)
        if not by_date:
            # 修复（2026-09-06 审查）：allocation 全缺时不再静默返回空组合——
            # 静默 0 收益与逐笔胜率并列展示会造成"胜率正常但组合收益 0"的矛盾
            logger.warning("组合模拟跳过：全部推荐记录缺少 allocation_pct（策略未输出仓位），"
                           "组合口径指标将全为 0")
            return self._empty_portfolio_result()

        # 卖出规则参数（P0-D）
        tp = float(self.sell_config.get('take_profit', 0.02))
        sl = float(self.sell_config.get('stop_loss', -0.02))
        max_hold = max(1, int(self.sell_config.get('time_stop_days', 3)))
        # 2026-09-17：原 close_t0_fill（配置为 close_t0 时的填充标志）已随 close_t0 买入
        # 分支移除而删除；close_t0 分支为死代码+杠杆 bug，且错传滑点入参。
        # 现仅 open_t1 一种有效成交口径（见 __init__ 注释），pending_buys 一律在次日开盘处理。
        # P2-N：ATR 自适应止损开关（默认 fixed 保持历史行为）
        atr_mode = (self.sell_config.get('stop_mode', 'fixed') == 'atr')
        atr_mult = float(self.sell_config.get('atr_mult', 1.0))

        # ── 预拉每笔推荐的 K 线（缓存命中，毫秒级）──
        klines = {}        # (决策日, code) -> kline df
        row_of_date = {}   # (决策日, code) -> {'yyyy-mm-dd': 行号}
        for date in sorted(by_date.keys()):
            for rec in by_date[date]:
                key = (date, rec['code'])
                end_look = (datetime.strptime(date, '%Y-%m-%d')
                            + timedelta(days=40)).strftime('%Y-%m-%d')
                k = self.data_engine.get_kline(rec['code'], start_date=date, end_date=end_look)
                if k is None or k.empty or len(k) < 2:
                    continue
                k = k.reset_index(drop=True)
                klines[key] = k
                if 'date' in k.columns:
                    row_of_date[key] = {
                        str(pd.Timestamp(v).date()): i for i, v in enumerate(k['date'])
                    }

        # ── 全区间日历（2026-09-07 论证后实施）：改用真实交易日历覆盖
        # [首个决策日, 最后持仓日] 全区间——原"决策日∪K线窗口日"拼接会
        # 遗漏跨簇空仓日，Sharpe 年化分母失真。日历不可用时回退旧拼接口径。
        cal_set = set(by_date.keys())
        for m in row_of_date.values():
            cal_set.update(m.keys())
        try:
            from core.trading_calendar import trading_days
            _start = min(by_date.keys()) if by_date else None
            _end_candidates = [d for m in row_of_date.values() for d in m.keys()]
            _end = max(_end_candidates) if _end_candidates else None
            if _start and _end:
                _cal_days = trading_days(_start, _end)
                if _cal_days:
                    cal_set = set(_cal_days)
        except Exception as e:
            logger.warning(f"交易日历不可用，回退拼接口径: {str(e)[:50]}")
        calendar = sorted(cal_set)

        cash = float(self.initial_capital)
        positions = {}       # code -> 持仓台账
        pending_buys = []    # open_t1 口径：待下一交易日开盘买的 (决策日, rec)
        equity_curve = []
        trade_details = []
        daily_returns = []  # 每日收益率（小数，覆盖全区间含空仓日）
        prev_equity = cash

        # （2026-09-06 审查清理）此处原有 _buy_position/_date_of_row 两个嵌套
        # 函数，为 P0-D 重写残留——全程无调用点（买入逻辑在主循环内联实现），
        # 且闭包内对 cash 赋值在 Python 语义下也无法生效。已删除。

        for d in calendar:
            # ── 1. 开盘阶段：open_t1 口径，昨日决策 → 今日开盘买入 ──
            # 2026-09-17：移除 `not close_t0_fill` 守卫——close_t0 分支已删除，
            # pending_buys 一律在次日开盘处理（即使 config 误配 close_t0 也优雅降级为
            # open_t1 买入，而非静默零成交）。
            if pending_buys:
                total_alloc = sum(r.get('allocation_pct', 0) for _, r in pending_buys) or 1.0
                # 2026-09-17 修复（缺陷2）：进入买入循环前固定组合权益基数 base_equity，
                # 不再用循环内递减的 cash 作为基数——否则后续仓位投入额随循环顺序变化，
                # 且把 portfolio_optimizer 的"合计≤100% + 现金不回补"硬契约抹掉。
                # 市值口径：现有持仓以昨日收盘(last_close)估算当日市值；无 last_close 回落 fill_price。
                # （成本口径亦可接受，但会低估浮盈持仓的权益基数；此处用市值口径更贴近真实组合权益。）
                base_equity = cash + sum(p['shares'] * p.get('last_close', p['fill_price'])
                                         for p in positions.values())
                # 2026-09-18 审查修复（确定性）：本批次所需投入若超过可用现金，
                # 按 **比例** 收缩（cash_scale），而不是在循环内"先到先得"式截断——
                # 后者会让投入额依赖 pending_buys 的排序（非确定性）。
                _batch_want = base_equity * (total_alloc / 100.0) if self.sizing_mode == 'absolute' \
                    else base_equity * 1.0
                _cash_scale = min(1.0, cash / _batch_want) if _batch_want > 0 else 1.0
                still_pending = []
                for dec_date, rec in pending_buys:
                    key = (dec_date, rec['code'])
                    m = row_of_date.get(key, {})
                    row_i = m.get(d)
                    if row_i is None:
                        # 当日停牌：顺延；K 线已越过尾端则放弃该笔
                        k = klines.get(key)
                        if k is None or not m or d > str(pd.Timestamp(k['date'].iloc[-1]).date()):
                            continue
                        still_pending.append((dec_date, rec))
                        continue
                    row = klines[key].iloc[row_i]
                    fill = float(row['open'])
                    if fill <= 0:
                        continue
                    # 成交可行性（2026-09-18 审查 P2-2）：T+1 开盘即涨停 → 无卖单，不可买入。
                    # 该笔直接放弃（不进入 still_pending，避免无限顺延；实盘亦无法追入）。
                    if self._buy_blocked_by_limit_up(
                            rec['code'],
                            float(klines[key].iloc[row_i - 1]['close']) if row_i > 0 else None,
                            fill):
                        logger.debug(f"跳过买入 {rec['code']}@{d}：开盘涨停不可成交")
                        continue
                    # 缺陷1：先算 allocated 再取滑点（单笔委托金额为入参）；
                    # 缺陷2：allocated 以固定 base_equity 为基数（与循环顺序无关）。
                    # 仓位口径分叉（2026-09-18）：normalized=按当日委托合计归一化
                    # （尺度不变，只认相对权重）；absolute=按 alloc% 绝对投入，
                    # 未投出部分留现金（使仓位/sizing 机制可被回测验证）。
                    # absolute 的超配处理（2026-09-18 修正）：Σalloc > 100% 时按比例
                    # 收缩到 100%，**不做顺序截断**——顺序截断会让先买的吃满、后买的
                    # 只剩零头，使结果依赖 pending_buys 的排序（非确定性伪影）。
                    _alloc_pct = rec.get('allocation_pct', 0) or 0
                    if self.sizing_mode == 'absolute':
                        _cap_scale = min(1.0, 100.0 / total_alloc) if total_alloc > 0 else 1.0
                        allocated = base_equity * (_alloc_pct * _cap_scale / 100.0)
                    else:
                        allocated = base_equity * (_alloc_pct / total_alloc)
                    # 防御（现金不回补硬契约）：不得透支、不得让 cash 为负
                    if allocated > cash:
                        allocated = cash
                    slip = self._slippage_for(allocated)
                    buy_cost_ps = fill * (1 + slip) * (1 + self.commission + self.transfer_fee)
                    if allocated > 0 and buy_cost_ps > 0:
                        # 2026-09-18 修复（现金泄漏）：先检查重复持仓再扣现金。
                        # 原实现顺序为 `cash -= allocated` → 重复持仓 `continue`，
                        # 导致【仓位未建立但现金已扣】→ 凭空亏损（每次 ≈ 单笔仓位
                        # 33-40% 权益）。实测 absolute 8月窗口触发 2 次 → 组合收益
                        # −31.36%（月度口径却为 +4.25%，自相矛盾）；normalized 同窗口
                        # 0 次 → +3.23%。此 bug 曾使多次 A/B 结论失真（伪影）。
                        if rec['code'] in positions:
                            logger.info(
                                f"跳过重复开仓 {rec['code']}@{d}（持仓期内再次被推荐，"
                                f"保持现有台账，实盘同一票不重复买；未扣现金）")
                            continue
                        shares = allocated / buy_cost_ps
                        cash -= allocated
                        positions[rec['code']] = {
                            'shares': shares,
                            'cost_total': allocated,
                            'fill_price': fill,
                            'slip': slip,
                            'entry_date': d,          # 买入日
                            'decision_date': dec_date,
                            'key': key,
                            'held_days': 0,
                            'last_close': float(row['close']),
                            'rec': rec,
                        }
                pending_buys = still_pending

            # ── 2. 盘口阶段：持仓退出检查（严格 T+1：买入日不检查）──
            # 保守序：跳空穿越按开盘价 → 盘中止损优先于止盈（双触发不可知先后）
            # TODO(P0-4 后续，2026-09-17 标注)：本段内联退出逻辑与
            # _calculate_results 复用的 _simulate_exit_path（L498 起）是两套实现，
            # 口径应一致但代码重复。本次不合并（重构风险大），后续统一收敛到
            # _simulate_exit_path，避免两套卖出规则路径再次分叉。
            exit_codes = []
            for code, pos in positions.items():
                if pos['entry_date'] == d:
                    continue                          # 买入日不可卖（T+1 制度）
                m = row_of_date.get(pos['key'], {})
                row_i = m.get(d)
                if row_i is None:
                    continue                          # 当日停牌：不计数、不检查
                pos['held_days'] += 1
                row = klines[pos['key']].iloc[row_i]
                o, h, l = float(row['open']), float(row['high']), float(row['low'])
                # 成交可行性（2026-09-18 审查 P2-2）：一字跌停当日无买盘，无法卖出 →
                # 顺延到下一交易日（持仓与浮亏照常按收盘价盯市）。
                if self._exit_blocked_by_limit_down(
                        code,
                        float(klines[pos['key']].iloc[row_i - 1]['close']) if row_i > 0 else None,
                        o, h):
                    pos['last_close'] = float(row['close'])
                    logger.debug(f"卖出顺延 {code}@{d}：一字跌停无法成交")
                    continue
                fill = pos['fill_price']
                tp_price = fill * (1 + tp)
                # 止损线（P2-N）：atr 模式 = min(固定止损价, fill - atr_mult×ATR14)，
                # 即距离取 max——高波动票止损更宽，下限不低于固定止损。
                sl_price = fill * (1 + sl)
                if atr_mode:
                    atr = self._atr_from_kline(klines[pos['key']], row_i)
                    if atr is not None:
                        sl_price = min(sl_price, fill - atr_mult * atr)
                exit_price, reason = None, None
                if o <= sl_price:
                    exit_price, reason = o, 'stop_loss_gap'
                elif o >= tp_price:
                    exit_price, reason = o, 'take_profit_gap'
                elif l <= sl_price:
                    exit_price, reason = sl_price, 'stop_loss'
                elif h >= tp_price:
                    exit_price, reason = tp_price, 'take_profit'
                elif pos['held_days'] >= max_hold:
                    exit_price, reason = float(row['close']), 'time_stop'
                if exit_price is None:
                    pos['last_close'] = float(row['close'])
                    continue
                # 卖出成交：扣滑点/佣金/过户费/印花税（P0-C 全成本口径）
                proceeds = (pos['shares'] * exit_price * (1 - pos['slip'])
                            * (1 - self.commission - self.transfer_fee - self.stamp_duty))
                cash += proceeds
                ret_pct = (proceeds / pos['cost_total'] - 1) * 100 if pos['cost_total'] > 0 else 0
                trade_details.append({
                    'date': pos['decision_date'],
                    'exit_date': d,
                    'held_days': pos['held_days'],
                    'code': code,
                    'name': pos['rec'].get('name', ''),
                    'score': pos['rec'].get('score', 0),
                    'allocation_pct': pos['rec'].get('allocation_pct', 0),
                    'buy_price': round(pos['fill_price'] * (1 + pos['slip'])
                                       * (1 + self.commission + self.transfer_fee), 4),
                    'sell_price': round(exit_price * (1 - pos['slip'])
                                        * (1 - self.commission - self.transfer_fee
                                           - self.stamp_duty), 4),
                    # return_t1 字段名与 backtest_store 明细表对齐（历史兼容）；
                    # 台账模式下语义 = 该笔完整持有期收益（P0-D）
                    'return_t1': round(ret_pct, 2),
                    'return_pct': round(ret_pct, 2),
                    'capital_used': round(pos['cost_total'], 2),
                    'pnl': round(proceeds - pos['cost_total'], 2),
                    'exit_reason': reason,
                })
                exit_codes.append(code)
            for code in exit_codes:
                positions.pop(code, None)

            # ── 3. 决策阶段：当日收盘（open_t1 口径：决策日推荐入 pending，
            #       下一交易日开盘买入）。close_t0 分支已于 2026-09-17 移除——
            #       死代码 + 漏 /100 形成杠杆 bug + 仍向 _slippage_for 错传个股日成交额
            #       （与缺陷1同错）。详见 __init__ 注释。──
            day_recs = by_date.get(d, [])
            if day_recs:
                # close_t0 分支已移除，仅保留 open_t1：决策日推荐入 pending_buys，
                # 下一交易日开盘买入（严格 T+1 制度）。
                pending_buys.extend((d, r) for r in day_recs)

            # ── 4. 收盘 mark-to-market（含空仓日 — P0-D Sharpe 修复核心）──
            equity = cash + sum(p['shares'] * p.get('last_close', p['fill_price'])
                                for p in positions.values())
            equity_curve.append(round(equity, 2))
            daily_returns.append(equity / prev_equity - 1 if prev_equity > 0 else 0.0)
            prev_equity = equity

        if len(equity_curve) < 2:
            return self._empty_portfolio_result()

        # 计算总收益
        total_return = (equity_curve[-1] / self.initial_capital - 1) * 100

        # 年化夏普
        ret_arr = np.array(daily_returns)
        if len(ret_arr) > 1 and np.std(ret_arr) > 0:
            sharpe = float(np.mean(ret_arr) / np.std(ret_arr) * np.sqrt(252))
        else:
            sharpe = 0.0

        # 最大回撤（从 equity_curve 算）
        eq_arr = np.array(equity_curve)
        peak = np.maximum.accumulate(eq_arr)
        drawdowns = (eq_arr - peak) / peak
        max_dd = float(np.min(drawdowns)) * 100 if len(drawdowns) > 0 else 0.0

        # 月度收益
        monthly = self._calc_monthly_portfolio_returns(trade_details)

        return {
            'equity_curve': equity_curve,
            'total_return': round(total_return, 2),
            'max_drawdown': round(max_dd, 2),
            'sharpe_ratio': round(sharpe, 2),
            'monthly_returns': monthly,
            'trade_details': trade_details,
            # P0-D：日频样本数（含空仓日）。Sharpe 的分母样本口径审计依据——
            # 与 total_trading_days 对不上说明日频覆盖不完整
            'daily_return_days': len(daily_returns),
            'open_positions': len(positions),   # 期末未平仓数（T+3 内的尾巴仓）
        }

    def _empty_portfolio_result(self) -> Dict:
        return {
            'equity_curve': [self.initial_capital],
            'total_return': 0.0,
            'max_drawdown': 0.0,
            'sharpe_ratio': 0.0,
            'monthly_returns': [],
            'trade_details': [],
        }

    def _calc_monthly_portfolio_returns(self, trade_details: List[Dict]) -> List[Dict]:
        """从仓位模拟的交易明细中按月汇总收益（用月初本金做分母）"""
        monthly = {}
        # 按日期排序，计算月初本金
        sorted_trades = sorted(trade_details, key=lambda x: x['date'])
        # 从第一笔交易开始模拟资金流
        current_capital = self.initial_capital
        month_start_capital = {}
        prev_month = None
        for td in sorted_trades:
            month = td['date'][:7]
            if month != prev_month:
                month_start_capital[month] = current_capital
                prev_month = month
            # 更新资本（模拟后续月份使用）
            current_capital += td.get('pnl', 0)

        for td in sorted_trades:
            month = td['date'][:7]
            if month not in monthly:
                monthly[month] = {'pnls': [], 'trades': 0}
            monthly[month]['pnls'].append(td.get('pnl', 0))
            monthly[month]['trades'] += 1

        result = []
        for month in sorted(monthly.keys()):
            data = monthly[month]
            total_pnl = sum(data['pnls'])
            base = month_start_capital.get(month, self.initial_capital)
            est_return = (total_pnl / base) * 100 if base > 0 else 0
            result.append({
                'month': month,
                'pnl': round(total_pnl, 2),
                'est_return': round(est_return, 2),
                'trades': data['trades'],
            })
        return result

    def _calc_benchmark_returns(self, start: str, end: str) -> Dict[str, Optional[float]]:
        """多基准区间收益（P1-J，2026-09-05 审查报告）。

        历史 P3-9 修复：拉取失败返回 None（不静默归 0）→ benchmark_available=False，
        超额收益标记为不可信。现扩展为多基准（默认沪深300 + 中证1000）：
        小市值候选池对沪深300 的"超额"容易虚高，中证1000 更可比。
        单个基准失败自动跳过（记 None），不阻断回测。
        """
        results: Dict[str, Optional[float]] = {}
        for code in self.benchmark_codes:
            ret = None
            try:
                kline = self.data_engine.get_kline(code, start_date=start, end_date=end)
                if kline is not None and not kline.empty and len(kline) >= 2:
                    ret = (kline['close'].iloc[-1] / kline['close'].iloc[0] - 1) * 100
            except Exception as e:
                logger.warning(f"基准 {code} 拉取失败（跳过，不影响回测）: {e}")
                ret = None
            if ret is None:
                logger.warning(f"基准 {code} K线不可用（该基准记为无参照）")
            results[code] = ret
        return results

    def _calc_monthly_returns(self, trade_details: List[Dict]) -> List[Dict]:
        """计算月度收益汇总

        修复（2026-09-05 审查 P2-1）：改为只接收 trade_details（每条自带
        date + return_t1，与收益同步写入），不再做 records×returns 的 zip
        —— 当 _calculate_results 因缺 K 线 skip 中间记录时，zip 会把后面
        记录的收益错配到前面记录的月份。
        """
        monthly = {}
        for td in trade_details:
            ret = td.get('return_t1')
            if ret is None:
                continue
            month = td['date'][:7]  # YYYY-MM
            if month not in monthly:
                monthly[month] = {'returns': [], 'trades': 0}
            monthly[month]['returns'].append(ret)
            monthly[month]['trades'] += 1

        result = []
        for month in sorted(monthly.keys()):
            data = monthly[month]
            avg = np.mean(data['returns'])
            win = np.mean(np.array(data['returns']) > 0) * 100
            result.append({
                'month': month,
                'avg_return': round(avg, 2),
                'win_rate': round(win, 1),
                'trades': data['trades'],
            })
        return result

    def _calc_factor_performance(self, trade_details: List[Dict],
                                  records: List[Dict]) -> Dict:
        """
        计算各因子在赢/输单中的表现差异 + IC（信息系数）

        IC 定义：Spearman(rank(factor_raw_score), rank(return_t1))
        IC 解释：[-1, +1]，|IC| > 0.03 即认为有效

        改进点 (2026-06-17):
          - 赢亏分桶：`return_t1 >= 0` 算赢（边缘盈亏归入赢）
          - 加 IC 列：每个因子的 raw_score 与 return_t1 的 Spearman rank 相关系数
          - 双数据呈现：spread（赢亏均值差）+ IC（rank 相关系数）
          - 样本不足 5 条时 IC 算不出，标空
        """
        # 收集每条记录的因子原始分 + 收益
        # （2026-09-06 审查）预建 (code, date) -> record 索引，替代循环内 next() 线性扫描
        rec_index = {(r['code'], r['date']): r for r in records}
        rows = []
        for td in trade_details:
            score = td.get('return_t1', None)
            if score is None:
                continue

            # 找到对应 records 条目获取 factor_breakdown
            # 修复（2026-09-06 审查）：原用 next() 线性扫描 O(n²)，改字典索引 O(1)
            rec = rec_index.get((td['code'], td['date']))
            if not rec:
                continue

            breakdown = rec.get('factor_breakdown', {})
            factor_scores = {}
            for fname, detail in breakdown.items():
                if isinstance(detail, dict):
                    raw = detail.get('raw_score', 50)
                else:
                    raw = 50
                factor_scores[fname] = raw

            # ← 这里补上缺失的 append
            rows.append({
                'return_t1': score,
                'factor_scores': factor_scores,
                'is_win': score >= 0,
            })

        if not rows:
            return {}

        # 收集所有因子名
        all_factors = set()
        for r in rows:
            all_factors.update(r['factor_scores'].keys())

        result = {}
        for factor_name in all_factors:
            win_scores, lose_scores, all_scores, all_returns = [], [], [], []
            for r in rows:
                fs = r['factor_scores'].get(factor_name, 50)
                if r['is_win']:
                    win_scores.append(fs)
                else:
                    lose_scores.append(fs)
                all_scores.append(fs)
                all_returns.append(r['return_t1'])

            # 跳过常数列：标准差为 0 的因子无法计算相关性
            if np.std(all_scores) == 0:
                continue

            # spread（赢亏均值差）
            win_avg = np.mean(win_scores) if win_scores else 50
            lose_avg = np.mean(lose_scores) if lose_scores else 50
            spread = win_avg - lose_avg

            # IC（Spearman rank 相关）— 样本不足返回 None
            ic = None
            if len(all_scores) >= 5:
                try:
                    import scipy.stats as stats
                    # 重要：Spearman 不要求 raw_score 是 rank；这里因为是 0-100 分，可以直接用
                    # 如果是真正的 raw value，应先 rank；这里 raw_score 已是 0-100 标准化
                    ic_val, _ = stats.spearmanr(all_scores, all_returns)
                    ic = float(ic_val) if not np.isnan(ic_val) else None
                except (ImportError, Exception):
                    # 没装 scipy 时降级到皮尔逊
                    try:
                        arr = np.array(all_scores)
                        ret = np.array(all_returns)
                        if np.std(arr) > 0 and np.std(ret) > 0:
                            ic = float(np.corrcoef(arr, ret)[0, 1])
                    except Exception:
                        ic = None

            n = len(all_scores)
            win_n = len(win_scores)
            lose_n = len(lose_scores)

            # 解读：spread > 0 且 |IC| > 0.03 → 真有效
            verdict = self._judge_factor(win_n, lose_n, spread, ic, n)

            result[factor_name] = {
                'win_avg': round(win_avg, 1),
                'lose_avg': round(lose_avg, 1),
                'spread': round(spread, 1),
                'ic': round(ic, 4) if ic is not None else None,
                'n_samples': n,
                'win_count': win_n,
                'lose_count': lose_n,
                'verdict': verdict,
            }
        return result

    @staticmethod
    def _judge_factor(win_n, lose_n, spread, ic, n) -> str:
        """
        因子有效性判定阈值
        - 胜场 < 5 票：标注"样本不足"
        - spread > 0 且 |IC| > 0.03：真有效
        - spread > 0 仅在边沿：可能是噪声
        - spread < 0：因子反向（应当降权）
        """
        if n < 5:
            return '样本不足'
        if spread <= 0:
            return '反向（建议检查）'
        if ic is None:
            # 无 IC 时只看 spread
            return '微正（需观察）' if spread < 1.0 else '边缘有效'
        if ic >= 0.05:
            return '强有效 ✅'
        elif ic >= 0.03:
            return '有效'
        elif ic >= 0:
            return '弱信号'
        else:
            return '噪声/反向'

    # ============ B5c: 单票 K 线因子回测 ============

    def run_kline_factor_backtest(self,
                                   factors: List[str] = None,
                                   start_date: str = None,
                                   end_date: str = None,
                                   min_samples_per_code: int = 60) -> BacktestResult:
        """
        单票 K 因子回测 — 不依赖全市场 snapshot，仅用 K 线历史数据逐日重算技术类因子

        ⚠️ DEPRECATED（2026-09-05 审查 P1-1）：
          本方法的因子口径与生产不一致（momentum=原始20日涨幅而非横截面RPS、
          technical=MA排列简化版而非6维TechnicalScorer、volume_price=log映射
          而非生产分段映射函数），且对全部(股票,日)样本池化计算 IC —— 跨日
          市场收益方差会直接污染相关系数。因子有效性判定请改用
          core/oos_validator.py（--mode oos，逐日横截面 IC + 口径对齐实盘）。
          本方法仅作历史参考保留，其结论不得用于权重决策。

        参数：
          factors: 要回测的因子列表，默认 ['momentum', 'technical', 'volume_price']
          start_date/end_date: 区间，默认 '2024-01-01' ~ 今天
          min_samples_per_code: 单只票最少 K 线数（默认 60），不够就跳过

        输出：
          BacktestResult，factor_performance 字段填充每因子的 IC 与 verdict
          trade_details 留空（这不是策略级回测，是因子单变量统计验证）

        设计意图：
          这是当前数据条件下能给技术类因子可验证信号的标准做法。
          资金流/题材/龙虎榜/北向/基本面 在 K 线维度下没有数据，无法验证。
        """
        # （2026-09-06 审查）运行时告警，防止误用已废弃口径的结论做权重决策
        import warnings
        warnings.warn("run_kline_factor_backtest 已废弃（P1-1：因子口径与生产不一致、"
                      "跨日池化污染 IC），其结论不得用于权重决策",
                      DeprecationWarning, stacklevel=2)
        from datetime import datetime as dt
        from core.factor_library import FactorLibrary

        if factors is None:
            factors = ['momentum', 'technical', 'volume_price']
        if start_date is None:
            start_date = '2024-01-01'
        if end_date is None:
            end_date = dt.now().strftime('%Y-%m-%d')

        logger.info(f"\n{'='*60}")
        logger.info(f"K 因子回测启动: {factors} | {start_date} ~ {end_date}")
        logger.info(f"{'='*60}")

        # 1. 找出 K 线缓存里有足够历史的所有股票
        db_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), 'data', 'cache', 'kline_cache.db')
        if not os.path.exists(db_path):
            logger.warning(f"K线缓存不存在: {db_path}")
            return self._empty_result_kfactor(factors, start_date, end_date)

        conn = sqlite3.connect(db_path)
        try:
            # 统计每只票的 K 线数
            counts = conn.execute("""
                SELECT code, COUNT(*) as n, MIN(date) as start_d, MAX(date) as end_d
                FROM kline_cache
                WHERE date >= ? AND date <= ?
                GROUP BY code
                HAVING n >= ?
            """, (start_date, end_date, min_samples_per_code)).fetchall()
        finally:
            conn.close()

        logger.info(f"满足条件的股票: {len(counts)} 只")

        if not counts:
            return self._empty_result_kfactor(factors, start_date, end_date)

        # 2. 逐只票拉 K 线，逐日重算因子，T+1 收益配对
        per_code_results = {}  # {code: [(factor_name, raw_score, return_t1), ...]}
        skipped = 0
        for code, n, _, _ in counts:
            try:
                kline = self.data_engine.get_kline(code, start_date=start_date, end_date=end_date)
            except Exception as e:
                skipped += 1
                continue
            if kline is None or kline.empty or len(kline) < 21:
                continue

            # 计算 K 线序列上每日的因子原始分（不带评分函数，直接用原始信号）
            closes = kline['close'].astype(float)
            volumes = kline['volume'].astype(float) if 'volume' in kline.columns else None
            pct = kline.get('pct_chg', closes.pct_change() * 100)

            # 未来 T+1 收益：当日 close -> 后一日 open 之间的百分比
            future_open = kline['open'].astype(float).shift(-1)
            t1_return = (future_open / closes - 1) * 100  # %

            for i in range(20, len(kline) - 1):  # 留 20 日窗口 + 1 日未来收益
                slice_close = closes.iloc[:i+1]
                slice_volume = volumes.iloc[:i+1] if volumes is not None else None
                cur_pct = pct.iloc[i]
                fwd_ret = t1_return.iloc[i]
                if pd.isna(fwd_ret):
                    continue

                code_factor_scores = {}
                for f in factors:
                    if f == 'momentum':
                        # 20 日 composite 动量（不调用 calc_rps_score，避免离散去损失信息）
                        # （2026-09-05 审查 P3-11：删除永假的 `if i < 20: continue`
                        #   —— 外层 range 已从 20 起）
                        ret_20 = (closes.iloc[i] / closes.iloc[i-20] - 1) * 100
                        code_factor_scores['momentum'] = ret_20
                    elif f == 'technical':
                        # MA 排列简化版（不依赖 TechnicalScorer 完整 df）
                        if len(slice_close) < 5:
                            continue
                        ma5 = slice_close.tail(5).mean()
                        ma10 = slice_close.tail(10).mean() if len(slice_close) >= 10 else ma5
                        ma20 = slice_close.tail(20).mean() if len(slice_close) >= 20 else ma5
                        if ma5 > ma10 > ma20:
                            tscore = 85
                        elif ma5 < ma10 < ma20:
                            tscore = 25
                        elif ma5 > ma20:
                            tscore = 65
                        elif ma5 < ma20:
                            tscore = 35
                        else:
                            tscore = 50
                        code_factor_scores['technical'] = tscore
                    elif f == 'volume_price':
                        # 量比 × 涨跌方向
                        if slice_volume is None or len(slice_volume) < 5:
                            continue
                        avg5_vol = slice_volume.tail(5).mean()
                        if avg5_vol <= 0:
                            continue
                        vol_ratio = float(slice_volume.iloc[i] / avg5_vol)
                        # 量价方向：上涨+放量最高分；下跌+放量最低分
                        sign = 1 if cur_pct > 0 else -1
                        # 把 vol_ratio 标准化为 0-100：log(ratio)*30 + 50 + 方向加成
                        import math
                        base = 50 + math.log(max(vol_ratio, 0.1)) * 20
                        score_vp = base + sign * 10
                        code_factor_scores['volume_price'] = max(0, min(100, score_vp))

                # 收集
                if code_factor_scores:
                    if code not in per_code_results:
                        per_code_results[code] = []
                    per_code_results[code].append((code_factor_scores, fwd_ret))

        # 3. 整合所有票的因子信号 vs 收益对
        aggregated = {f: [] for f in factors}  # {factor: [(raw_score, return_t1), ...]}
        for code, recs in per_code_results.items():
            for scores, ret in recs:
                for f, s in scores.items():
                    if f in aggregated:
                        aggregated[f].append((s, ret))

        # 4. 计算每个因子的 IC / spread / verdict
        factor_performance = self._aggregate_factor_ic(aggregated, factors)

        logger.info(f"\nK 因子回测完成: 覆盖股票 {len(per_code_results)} 只, 累计样本对 {max((sum(len(v) for v in aggregated.values()) // max(1, len(factors)), 1))} 条")
        for f, perf in factor_performance.items():
            ic = perf.get('ic')
            ic_str = f"{ic:+.4f}" if ic is not None else '—'
            logger.info(f"  {f}: IC={ic_str} spread={perf.get('spread', 0):+.2f} n={perf.get('n_samples', 0)} {perf.get('verdict', '')}")

        result = BacktestResult(
            strategy_name='kline_factor_backtest',
            period=(start_date, end_date),
            total_trading_days=0,
            total_trades=sum(len(v) for v in aggregated.values()),
            win_rate=0.0,
            avg_return_t1=0.0,
            avg_return_t5=0.0,
            max_win_t1=0.0,
            max_loss_t1=0.0,
            max_drawdown=0.0,
            sharpe_ratio=0.0,
            benchmark_return=0.0,
            strategy_return=0.0,
            excess_return=0.0,
            monthly_returns=[],
            equity_curve=[],
            factor_performance=factor_performance,
            trade_details=[],
        )
        # 标记 mode 给 backtest_report.py 区分用
        result.mode = 'kfactor'
        result.factors_tested = factors
        result.codes_count = len(per_code_results)
        return result

    def _aggregate_factor_ic(self, aggregated, factors):
        """对每因子 [(raw_score, return_t1)] 对计算 IC/spread/verdict"""
        result = {}
        import scipy.stats as stats
        for f in factors:
            pairs = aggregated.get(f, [])
            if not pairs:
                continue
            scores = np.array([p[0] for p in pairs])
            returns = np.array([p[1] for p in pairs])

            is_win_mask = returns >= 0
            win_scores = scores[is_win_mask]
            lose_scores = scores[~is_win_mask]
            win_avg = float(np.mean(win_scores)) if len(win_scores) else 50.0
            lose_avg = float(np.mean(lose_scores)) if len(lose_scores) else 50.0
            spread = win_avg - lose_avg

            ic = None
            if len(scores) >= 5:
                try:
                    ic_val, _ = stats.spearmanr(scores, returns)
                    ic = float(ic_val) if not np.isnan(ic_val) else None
                except Exception:
                    try:
                        if np.std(scores) > 0 and np.std(returns) > 0:
                            ic = float(np.corrcoef(scores, returns)[0, 1])
                    except Exception:
                        ic = None

            n = len(scores)
            win_n = int(np.sum(is_win_mask))
            lose_n = n - win_n
            verdict = self._judge_factor(win_n, lose_n, spread, ic, n)

            result[f] = {
                'win_avg': round(win_avg, 1),
                'lose_avg': round(lose_avg, 1),
                'spread': round(spread, 1),
                'ic': round(ic, 4) if ic is not None else None,
                'n_samples': n,
                'win_count': win_n,
                'lose_count': lose_n,
                'verdict': verdict,
            }
        return result

    def _empty_result_kfactor(self, factors, start, end):
        result = BacktestResult(
            strategy_name='kline_factor_backtest',
            period=(start, end),
            total_trading_days=0, total_trades=0,
            win_rate=0.0, avg_return_t1=0.0, avg_return_t5=0.0,
            max_win_t1=0.0, max_loss_t1=0.0, max_drawdown=0.0,
            sharpe_ratio=0.0, benchmark_return=0.0,
            strategy_return=0.0, excess_return=0.0,
            monthly_returns=[], equity_curve=[], factor_performance={},
        )
        result.mode = 'kfactor'
        return result

    def _empty_result(self, mode: str, start: str, end: str) -> BacktestResult:
        return BacktestResult(
            strategy_name=f"{mode}_strategy",
            period=(start, end),
            total_trading_days=0,
            total_trades=0,
            win_rate=0.0, avg_return_t1=0.0, avg_return_t5=0.0,
            max_win_t1=0.0, max_loss_t1=0.0, max_drawdown=0.0,
            sharpe_ratio=0.0, benchmark_return=0.0,
            strategy_return=0.0, excess_return=0.0,
            monthly_returns=[], equity_curve=[], factor_performance={},
        )
