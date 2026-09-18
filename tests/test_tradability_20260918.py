# -*- coding: utf-8 -*-
"""涨跌停可成交性建模测试（2026-09-18 全项目审查 P2-2 修复）

买入端：T+1 开盘 ≥ 涨停价 → 不可买入（封板无卖单）
持有端：一字跌停（open/high 均 ≤ 跌停价）→ 当日无法卖出，顺延
开关：backtest.tradability_check（默认 True；false 复现历史口径）
"""
import os
import sys
import unittest

import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from core.backtest_engine import BacktestEngine  # noqa: E402


def _days(n=6):
    from core.trading_calendar import trading_days
    ds = trading_days('2026-09-07', '2026-09-30')
    if not ds or len(ds) < n:
        raise unittest.SkipTest('交易日历不可用')
    return list(ds)[:n]


class _StubDE:
    def __init__(self, klines):
        self._k = klines

    def get_kline(self, code, start_date=None, end_date=None):
        return self._k.get(code)


def _kline_rows(dates, rows_spec):
    """rows_spec: [(open, high, low, close), ...] 与 dates 等长"""
    return pd.DataFrame([{'date': d, 'open': o, 'high': h, 'low': l, 'close': c,
                          'volume': 1e6, 'amount': 1e8}
                         for d, (o, h, l, c) in zip(dates, rows_spec)])


class TestTradability(unittest.TestCase):

    def setUp(self):
        self.days = _days()
        # 600001：T+1 开盘即涨停（10.0 → 11.0 = +10%），不可买入
        # 600002：买入后第 2 天一字跌停（−10%），无法卖出 → 顺延
        self.klines = {
            '600001': _kline_rows(self.days, [
                (10.0, 10.1, 9.9, 10.0), (11.0, 11.0, 11.0, 11.0),
                (11.0, 11.0, 11.0, 11.0), (11.0, 11.0, 11.0, 11.0),
                (11.0, 11.0, 11.0, 11.0), (11.0, 11.0, 11.0, 11.0)]),
            '600002': _kline_rows(self.days, [
                (10.0, 10.1, 9.9, 10.0), (10.0, 10.2, 9.9, 10.0),
                (9.0, 9.0, 9.0, 9.0),               # 一字跌停（−10%）
                (9.0, 9.5, 8.9, 9.4),               # 跌停打开 → 可卖
                (9.4, 9.6, 9.2, 9.5), (9.5, 9.7, 9.3, 9.6)]),
        }

    def _sim(self, code, allocation=40.0, tradability=True):
        eng = BacktestEngine(config={'initial_capital': 1_000_000,
                                     'fill_convention': 'open_t1',
                                     'tradability_check': tradability,
                                     'sell': {'take_profit': 0.02, 'stop_loss': -0.02,
                                              'time_stop_days': 3}})
        eng.data_engine = _StubDE(self.klines)
        recs = [{'date': self.days[0], 'code': code, 'name': code, 'score': 80,
                 'rating': '增持', 'buy_price': 10.0, 'allocation_pct': allocation}]
        return eng._simulate_portfolio(recs)

    def test_limit_up_open_blocks_buy(self):
        pf = self._sim('600001', tradability=True)
        self.assertEqual(len(pf.get('trade_details') or []), 0,
                         'T+1 开盘涨停应无法买入（P2-2 修复）')

    def test_limit_up_open_buyable_when_check_disabled(self):
        pf = self._sim('600001', tradability=False)
        self.assertEqual(len(pf.get('trade_details') or []), 1,
                         '关闭开关时应复现历史口径（可买入）')

    def test_limit_down_locked_defers_exit(self):
        """一字跌停当日不可卖 → 顺延到下一交易日（退出日应晚于跌停日）"""
        pf = self._sim('600002', tradability=True)
        details = pf.get('trade_details') or []
        self.assertTrue(details, '用例未产生交易')
        exit_date = str(details[0].get('exit_date'))
        lock_day = str(self.days[2])
        self.assertNotEqual(exit_date, lock_day,
                            '一字跌停当日不应成交（应顺延）')

    def test_limit_down_exit_allowed_when_disabled(self):
        pf = self._sim('600002', tradability=False)
        details = pf.get('trade_details') or []
        self.assertTrue(details)
        # 关闭开关：跌停日按止损价成交（历史口径）
        self.assertTrue(details[0].get('exit_date'))

    def test_limit_pct_by_board(self):
        self.assertAlmostEqual(BacktestEngine._limit_pct('600000'), 0.10)
        self.assertAlmostEqual(BacktestEngine._limit_pct('300001'), 0.20)
        self.assertAlmostEqual(BacktestEngine._limit_pct('688001'), 0.20)
        self.assertAlmostEqual(BacktestEngine._limit_pct('830001'), 0.30)

    def test_limit_price_rounding(self):
        eng = BacktestEngine(config={})
        self.assertAlmostEqual(eng._limit_price('600000', 10.0, up=True), 11.00)
        self.assertAlmostEqual(eng._limit_price('600000', 10.0, up=False), 9.00)
        self.assertAlmostEqual(eng._limit_price('300001', 10.0, up=True), 12.00)


if __name__ == '__main__':
    unittest.main()
