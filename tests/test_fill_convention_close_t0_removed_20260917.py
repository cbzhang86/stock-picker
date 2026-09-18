# -*- coding: utf-8 -*-
"""回归测试：close_t0 成交分支移除后两条路径口径一致（2026-09-17 收尾）

背景：
  _simulate_portfolio 早已固定 open_t1（T+1 开盘买）。但 _calculate_results 的逐笔路径
  此前仍残留一段生产代码：
      if self.fill_convention == 'close_t0':
          fill_price = float(kline.iloc[0]['close'])   # T 日收盘价（前视）
  以及 entry_idx 的 close_t0 分支。这导致：
    - 两路径口径分叉（组合永远 open_t1，逐笔会响应 close_t0）；
    - 残留分支用 T 日收盘价成交，属前视（收盘前无法执行）。
  修复（2026-09-17）：_calculate_results 也固定 open_t1（fill_price=open_t1、
  entry_idx=1），与 _simulate_portfolio 完全一致；config 配 close_t0 时优雅降级为 open_t1。

本测试断言：当 config.fill_convention='close_t0'（故意误配）时，两条路径都用 T+1 开盘
成交，且绝不出现用 T 日收盘价（kline.iloc[0]['close']）成交的行为。
"""
import os
import sys
import unittest

import pandas as pd

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from core.backtest_engine import BacktestEngine  # noqa: E402


def _trading_days(n=10):
    from core.trading_calendar import trading_days
    days = trading_days('2026-09-07', '2026-09-30')
    if not days or len(days) < n:
        raise unittest.SkipTest('交易日历不可用，跳过 close_t0 移除测试')
    return list(days)[:n]


def _make_kline(dates, prices):
    """prices: list of (open, close) 与 dates 一一对应。"""
    rows = []
    for d, (o, c) in zip(dates, prices):
        rows.append({'date': d, 'open': float(o), 'high': max(o, c) * 1.001,
                     'low': min(o, c) * 0.999, 'close': float(c),
                     'volume': 1_000_000.0, 'amount': 1e8})
    return pd.DataFrame(rows)


class _StubDE:
    def __init__(self, klines):
        self._k = klines

    def get_kline(self, code, start_date=None, end_date=None):
        return self._k.get(code)


class TestCloseT0Removed20260917(unittest.TestCase):

    def setUp(self):
        self.days = _trading_days(10)

    def _engine(self, fill_convention='close_t0'):
        eng = BacktestEngine(config={
            'initial_capital': 1_000_000,
            'assumed_positions': 3,
            # 关键：故意把口径配成 close_t0，验证引擎优雅降级为 open_t1
            'fill_convention': fill_convention,
            'slippage_tiers': [[2e6, 0.001], [1e7, 0.002],
                               [1e8, 0.004], [1e12, 0.008]],
            'sell': {'take_profit': 0.02, 'stop_loss': -0.02,
                     'time_stop_days': 3},
        })
        return eng

    # ── 逐笔路径 _calculate_results：必须走 open_t1，不碰 T 日收盘 ──

    def test_calc_results_uses_open_t1_not_tday_close(self):
        # d0 收盘设为与 T+1 开盘明显不同的值，作为"是否用了 T 日收盘"的探针
        close_t0_probe = 9.50      # 若残留 close_t0 分支激活，fill 会取此值
        open_t1 = 10.20            # 修复后 fill 应取此值（T+1 开盘）
        close_t1 = 10.50
        prices = [(10.0, close_t0_probe), (open_t1, close_t1), (10.8, 11.0),
                  (11.0, 11.1), (11.1, 11.2), (11.2, 11.4), (11.4, 11.5),
                  (11.5, 11.6), (11.6, 11.7), (11.7, 11.8)]
        kl = _make_kline(self.days, prices)
        eng = self._engine('close_t0')
        eng.data_engine = _StubDE({'600001': kl})
        rec = {'date': self.days[0], 'code': '600001', 'name': '600001',
               'score': 80, 'rating': '增持', 'buy_price': 10.0}
        res = eng._calculate_results([rec], self.days, 'full',
                                     self.days[0], self.days[-1])
        self.assertTrue(res.trade_details, '应产出逐笔成交明细')
        td = res.trade_details[0]
        self.assertAlmostEqual(td['open_t1'], open_t1, delta=1e-6)

        slip = eng._slippage_for(eng.assumed_order_value)
        eb = open_t1 * (1 + slip) * (1 + eng.commission + eng.transfer_fee)
        es = close_t1 * (1 - slip) * (1 - eng.commission - eng.transfer_fee
                                      - eng.stamp_duty)
        expected_ret = (es / eb - 1) * 100
        # 用 open_t1 算出的收益须与引擎回报一致
        self.assertAlmostEqual(td['return_t1'], expected_ret, delta=0.05)
        # 用 T 日收盘(close_t0_probe)算出的收益须与引擎回报明显不同 → 证明未走 close_t0
        eb0 = close_t0_probe * (1 + slip) * (1 + eng.commission + eng.transfer_fee)
        ret_close0 = (es / eb0 - 1) * 100
        self.assertGreater(abs(td['return_t1'] - ret_close0), 1.0,
                           '引擎回报不应等于 close_t0(T日收盘)口径')

    # ── 组合路径 _simulate_portfolio：fill 必为 T+1 开盘 ──

    def test_sim_portfolio_uses_open_t1_not_tday_close(self):
        close_t0_probe = 9.50
        open_t1 = 10.20
        # 买入日 d0 收盘=探针值；T+1(d1) 开盘=open_t1 应为成交价；随后上行触发止盈平仓
        prices = [(10.0, close_t0_probe), (open_t1, 10.45), (10.3, 10.6),
                  (10.4, 10.8), (10.5, 11.0), (10.6, 11.1), (10.7, 11.2),
                  (10.8, 11.3), (10.9, 11.4), (11.0, 11.5)]
        kl = _make_kline(self.days, prices)
        eng = self._engine('close_t0')
        eng.data_engine = _StubDE({'600001': kl})
        rec = {'date': self.days[0], 'code': '600001', 'name': '600001',
               'score': 80, 'rating': '增持', 'buy_price': 10.0,
               'allocation_pct': 30.0}
        pf = eng._simulate_portfolio([rec])
        self.assertTrue(pf.get('trade_details'), '应产生组合成交明细')
        td = pf['trade_details'][0]
        slip = eng._slippage_for(eng.assumed_order_value)
        expected_buy = open_t1 * (1 + slip) * (1 + eng.commission + eng.transfer_fee)
        # buy_price 字段 = fill*(1+slip)*(1+comm+tf)，应等于 T+1 开盘口径
        self.assertAlmostEqual(td['buy_price'], expected_buy, delta=0.02)
        # 且不等于 T 日收盘口径（close_t0 分支会取 9.5）
        buy_close0 = close_t0_probe * (1 + slip) * (1 + eng.commission + eng.transfer_fee)
        self.assertGreater(abs(td['buy_price'] - buy_close0), 0.5,
                           '组合成交价不应等于 close_t0(T日收盘)口径')

    # ── 两路径一致：相同标的/口径下，逐笔与组合的成交价都落 open_t1 ──

    def test_both_paths_consistent_on_close_t0_config(self):
        close_t0_probe = 9.50
        open_t1 = 10.20
        close_t1 = 10.50
        prices = [(10.0, close_t0_probe), (open_t1, close_t1), (10.8, 11.0),
                  (11.0, 11.1), (11.1, 11.2), (11.2, 11.4), (11.4, 11.5),
                  (11.5, 11.6), (11.6, 11.7), (11.7, 11.8)]
        kl = _make_kline(self.days, prices)
        eng = self._engine('close_t0')
        eng.data_engine = _StubDE({'600001': kl})
        rec = {'date': self.days[0], 'code': '600001', 'name': '600001',
               'score': 80, 'rating': '增持', 'buy_price': 10.0,
               'allocation_pct': 30.0}
        res = eng._calculate_results([rec], self.days, 'full',
                                     self.days[0], self.days[-1])
        pf = eng._simulate_portfolio([rec])
        calc_fill = res.trade_details[0]['open_t1']
        slip = eng._slippage_for(eng.assumed_order_value)
        port_fill = pf['trade_details'][0]['buy_price'] / (
            (1 + slip) * (1 + eng.commission + eng.transfer_fee))
        # 两路径成交价都应等于 open_t1
        self.assertAlmostEqual(calc_fill, open_t1, delta=1e-6)
        self.assertAlmostEqual(port_fill, open_t1, delta=1e-4)
        # 且都不等于 T 日收盘探针
        self.assertNotAlmostEqual(calc_fill, close_t0_probe, delta=0.1)
        self.assertNotAlmostEqual(port_fill, close_t0_probe, delta=0.1)


if __name__ == '__main__':
    unittest.main()
