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
import sys
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple
from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from core.data_engine import DataEngine
from core.scoring_model import ScoringModel
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


class BacktestEngine:
    """回测引擎 — 逐日模拟"""

    def __init__(self, config: dict = None):
        self.config = config or {}
        self.data_engine = DataEngine()
        self.initial_capital = self.config.get('initial_capital', 1_000_000)
        self.commission = self.config.get('commission_rate', 0.0003)
        self.slippage = self.config.get('slippage', 0.001)

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
        if mode == 'short':
            strategy = ShortTermStrategy(self.config)
        else:
            strategy = LongTermStrategy(self.config)

        # 4. 预加载所有股票的历史 K 线（首次 ~10 分钟，缓存到 SQLite，
        #    后续回测从缓存读取 ~1 分钟）
        logger.info("预加载历史K线数据（首次较慢，后续缓存加速）...")
        snapshots = self._load_historical_snapshots(
            codes, name_map, start_date, end_date
        )
        logger.info(f"历史快照构建完成: {len(snapshots)} 个交易日")

        # 5. 逐日模拟
        all_records = []
        equity_curve = []

        for i, trade_date in enumerate(trade_calendar):
            if (i + 1) % 20 == 0:
                logger.info(f"  进度: {i+1}/{len(trade_calendar)}")

            # 获取历史当日行情快照（基于真实K线数据）
            day_data = snapshots.get(trade_date)
            if day_data is None or day_data.empty:
                continue

            # 运行策略
            try:
                recommendations = strategy.run({'quotes_df': day_data})
            except Exception as e:
                logger.warning(f"  {trade_date} 策略运行失败: {str(e)[:60]}")
                continue

            # 记录推荐
            for rec in recommendations:
                all_records.append({
                    'date': trade_date,
                    'code': rec.get('code', ''),
                    'name': rec.get('name', ''),
                    'score': rec.get('score', 0),
                    'rating': rec.get('rating', ''),
                    'buy_price': rec.get('price', 0),
                    'factor_breakdown': rec.get('breakdown', {}),
                })

            # 每日权益（简化：不模拟持仓，只算推荐命中率）
            equity_curve.append(len(all_records))

        # 6. 计算收益（基于真实的 T+1 K线数据）
        result = self._calculate_results(all_records, trade_calendar, mode,
                                         start_date, end_date, equity_curve)

        logger.info(f"\n回测完成: 胜率 {result.win_rate:.1f}% | "
                     f"平均收益 {result.avg_return_t1:+.2f}% | "
                     f"交易次数 {result.total_trades}")
        return result

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
        import sqlite3
        db_path = self.data_engine._kline_cache_path
        if not os.path.exists(db_path):
            logger.warning(f"K线缓存不存在: {db_path}")
            return {}

        import time as _time
        batch_size = 200
        total = len(codes)
        kline_dict = {}

        # 批量读取：一次性拉取所有股票在目标区间的 K 线
        # 通过 SQLite 的 WHERE IN 拼接加速
        codes_batches = [codes[i:i+500] for i in range(0, len(codes), 500)]
        for cb in codes_batches:
            try:
                placeholders = ','.join(['?' for _ in cb])
                conn = sqlite3.connect(db_path)
                # 用 PARSE_DECLTYPES 加速日期解析
                df_all = pd.read_sql_query(
                    f"SELECT code, date, open, high, low, close, volume, amount "
                    f"FROM kline_cache "
                    f"WHERE code IN ({placeholders}) AND date>=? AND date<=? "
                    f"ORDER BY code, date",
                    conn, params=[str(c).zfill(6) for c in cb] + [start_date, end_date]
                )
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
            except Exception:
                pass

            if (len(kline_dict) % 1000) == 0 or len(kline_dict) == 0 and len(codes_batches) == 1:
                logger.info(f"  历史快照加载: {len(kline_dict)} 只有效K线")

        logger.info(f"  历史快照加载完成: {len(kline_dict)}/{total} 只有效K线")

        # 构建 {date: DataFrame}
        trade_dates = sorted(set(
            ds for kline in kline_dict.values() for ds in kline['date_str'].unique()
        ))
        snapshots = {}

        for date_str in trade_dates:
            rows = []
            for code, kline in kline_dict.items():
                day = kline[kline['date_str'] == date_str]
                if day.empty:
                    continue
                r = day.iloc[0]
                prev_close = None
                idx = r.name  # 原始 DataFrame 中的位置
                if idx > 0:
                    prev_close = float(kline.iloc[idx - 1]['close'])
                pct_chg = ((float(r['close']) - prev_close) / prev_close * 100) if prev_close and prev_close > 0 else 0.0

                rows.append({
                    'code': code,
                    'name': name_map.get(code, ''),
                    'price': float(r['close']),
                    'pct_chg': round(pct_chg, 2),
                    'amount': float(r.get('amount', 0)),
                    'turnover': 0.0,       # 历史换手率不可用
                    'volume_ratio': 1.0,   # 历史量比不可用
                    'volume': float(r.get('volume', 0)),
                    'pe': None,            # 历史 PE 不可用
                    'pb': None,            # 历史 PB 不可用
                    'name_raw': name_map.get(code, ''),
                })

            if rows:
                snapshots[date_str] = pd.DataFrame(rows)

        return snapshots

    def _get_trade_calendar(self, start: str, end: str) -> List[str]:
        """获取真实交易日历"""
        try:
            import akshare as ak
            df = ak.tool_trade_date_hist_sina()
            df['trade_date'] = pd.to_datetime(df['trade_date']).dt.date
            mask = (df['trade_date'] >= pd.Timestamp(start).date()) & (df['trade_date'] <= pd.Timestamp(end).date())
            return df[mask]['trade_date'].astype(str).tolist()
        except Exception as e:
            logger.warning(f"交易日历获取失败，使用简化版（周一到周五）: {e}")
            start_dt = datetime.strptime(start, '%Y-%m-%d')
            end_dt = datetime.strptime(end, '%Y-%m-%d')
            dates = []
            current = start_dt
            while current <= end_dt:
                if current.weekday() < 5:
                    dates.append(current.strftime('%Y-%m-%d'))
                current += timedelta(days=1)
            return dates

    def _get_day_snapshot(self, trade_date: str) -> Optional[pd.DataFrame]:
        """
        获取某日的全市场行情快照

        注意：回测模式下，调用 get_all_quotes() 获取的是今日实时数据，
        并非历史当日数据。这意味着回测的预过滤/评分阶段使用的是
        当前市场的价格排名，而非历史当日的。
        这是一个已知的近似——回测主要验证策略框架的有效性，
        而非精确的历史复现。
        """
        try:
            return self.data_engine.get_all_quotes()
        except Exception as e:
            logger.warning(f"  {trade_date} 行情获取失败: {str(e)[:60]}")
        return None

    def _update_positions(self, positions: dict, trade_date: str):
        """更新持仓状态"""
        to_remove = []
        for code, pos in positions.items():
            pos['days_held'] = pos.get('days_held', 0) + 1
            # T+3 时间止损
            if pos['days_held'] >= 3:
                to_remove.append(code)
        for code in to_remove:
            del positions[code]

    def _calculate_results(self, records: List[Dict],
                           trade_calendar: List[str],
                           mode: str, start: str, end: str,
                           equity_curve: List[float]) -> BacktestResult:
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

            # T+1：用买入日后第一个交易日的开盘价模拟买入（考虑滑点+佣金）
            open_t1 = float(kline.iloc[1]['open']) if len(kline) > 1 else None
            close_t1 = float(kline.iloc[1]['close']) if len(kline) > 1 else None

            if open_t1 and close_t1 and buy_price > 0:
                # 以开盘价买入，考虑滑点和佣金
                effective_buy = open_t1 * (1 + self.slippage)
                effective_buy_with_commission = effective_buy * (1 + self.commission)
                # 以开盘价买入，T+1收盘价卖出（考虑滑点+佣金）
                effective_sell = close_t1 * (1 - self.slippage) * (1 - self.commission)
                ret_t1 = (effective_sell / effective_buy_with_commission - 1) * 100
                trade_returns_t1.append(ret_t1)
            else:
                continue

            # T+5
            if len(kline) > 5:
                close_t5 = float(kline.iloc[5]['close'])
                ret_t5 = (close_t5 / open_t1 - 1) * 100 if open_t1 else None
                if ret_t5 is not None:
                    trade_returns_t5.append(ret_t5)
            else:
                trade_returns_t5.append(None)

            trade_details.append({
                'date': buy_date,
                'code': code,
                'name': rec.get('name', ''),
                'score': rec.get('score', 0),
                'buy_price': buy_price,
                'open_t1': open_t1,
                'close_t1': close_t1,
                'return_t1': round(ret_t1, 2) if ret_t1 is not None else None,
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

        # 日均收益（用于夏普）
        daily_returns = returns_arr / 100  # 转为小数
        if len(daily_returns) > 1 and np.std(daily_returns) > 0:
            sharpe = (np.mean(daily_returns) / np.std(daily_returns)) * np.sqrt(252)
        else:
            sharpe = 0.0

        # 最大回撤
        cumulative_series = (1 + returns_arr / 100).cumprod()
        rolling_max = np.maximum.accumulate(cumulative_series)
        drawdowns = (cumulative_series - rolling_max) / rolling_max
        max_dd = np.min(drawdowns) * 100 if len(drawdowns) > 0 else 0.0

        # 基准收益（沪深300 — 用 data_engine 拉取代替）
        benchmark_return = self._calc_benchmark_return(start, end)

        # 超额收益
        excess_return = strategy_return - benchmark_return

        # T+5 统计
        t5_valid = [r for r in trade_returns_t5 if r is not None]
        avg_ret_t5 = np.mean(t5_valid) if t5_valid else 0.0

        # 月度收益
        monthly_returns = self._calc_monthly_returns(records, trade_returns_t1)

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
            max_win_t1=round(max_win, 2),
            max_loss_t1=round(max_loss, 2),
            max_drawdown=round(max_dd, 2),
            sharpe_ratio=round(sharpe, 2),
            benchmark_return=round(benchmark_return, 2),
            strategy_return=round(strategy_return, 2),
            excess_return=round(excess_return, 2),
            monthly_returns=monthly_returns,
            equity_curve=equity_curve,
            factor_performance=factor_performance,
            trade_details=trade_details[:50]  # 前50条明细
        )

    def _calc_benchmark_return(self, start: str, end: str) -> float:
        """计算沪深300基准收益"""
        try:
            kline = self.data_engine.get_kline('000300', start_date=start, end_date=end)
            if kline is not None and not kline.empty and len(kline) >= 2:
                return (kline['close'].iloc[-1] / kline['close'].iloc[0] - 1) * 100
        except Exception as e:
            logger.warning(f"基准收益计算失败: {e}")
        return 0.0

    def _calc_monthly_returns(self, records: List[Dict],
                               trade_returns: List[float]) -> List[Dict]:
        """计算月度收益汇总"""
        monthly = {}
        for rec, ret in zip(records, trade_returns):
            month = rec['date'][:7]  # YYYY-MM
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
        rows = []
        for td in trade_details:
            score = td.get('return_t1', None)
            if score is None:
                continue

            # 找到对应 records 条目获取 factor_breakdown
            rec = next((r for r in records if r['code'] == td['code']
                        and r['date'] == td['date']), None)
            if not rec:
                continue

            breakdown = rec.get('factor_breakdown', {})
            factor_scores = {}
            for fname, detail in breakdown.items():
                if isinstance(detail, dict):
                    factor_scores[fname] = detail.get('raw_score', 50)
                else:
                    factor_scores[fname] = 50
            rows.append({'return_t1': score, 'factor_scores': factor_scores,
                         'is_win': score >= 0})  # 边缘盈亏归入赢

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
        import sqlite3
        import os
        db_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), 'data', 'kline_cache.db')
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
                        if i < 20:
                            continue
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
