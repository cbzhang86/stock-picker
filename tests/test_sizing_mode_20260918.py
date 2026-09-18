# -*- coding: utf-8 -*-
"""绝对仓位口径（sizing_mode='absolute'）配套测试（2026-09-18）

背景：`normalized` 口径（默认）按当日委托合计归一化 → **尺度不变**，导致任何
仓位/sizing 类改动（弱市压缩、波动保险丝、连亏压缩、拥挤度分档）在回测中
无法验证。本文件锁定新口径 `absolute`：

  A. absolute：uniform 缩放 → 组合收益**随敞口等比缩小**（可测量，不再是伪影）；
  B. 合计 100% 且不缩放时，absolute 与 normalized 结果**一致**（口径兼容性）；
  C. absolute 下现金留存生效：合计 40% → 收益约为满仓的 40%（线性无杠杆）；
  D. normalized 默认值不变（回归保护：历史可比性）。
"""
import os
import sys
import unittest

import pandas as pd

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from core.backtest_engine import BacktestEngine  # noqa: E402


def _trading_days(n=8):
    from core.trading_calendar import trading_days
    days = trading_days('2026-09-07', '2026-09-25')
    if not days or len(days) < n:
        raise unittest.SkipTest('交易日历不可用，跳过')
    return list(days)[:n]


def _make_kline(dates, step):
    px, rows = 10.0, []
    for d in dates:
        open_ = px
        close = round(px * step, 4)
        rows.append({'date': d, 'open': open_, 'high': max(open_, close) * 1.001,
                     'low': min(open_, close) * 0.999, 'close': close,
                     'volume': 1_000_000.0, 'amount': 1e8})
        px = close
    return pd.DataFrame(rows)


class _StubDE:
    def __init__(self, klines):
        self._k = klines

    def get_kline(self, code, start_date=None, end_date=None):
        return self._k.get(code)


class TestAbsoluteSizing(unittest.TestCase):

    def setUp(self):
        self.days = _trading_days()
        # 全部上涨（+1%/日，不触发 ±2% 止盈止损，走 T+3 时间止损）→ 正收益敞口
        self.klines = {f'60000{i}': _make_kline(self.days, 1.01) for i in range(1, 4)}

    def _engine(self, sizing_mode=None):
        cfg = {'initial_capital': 1_000_000, 'fill_convention': 'open_t1',
               'sell': {'take_profit': 0.02, 'stop_loss': -0.02, 'time_stop_days': 3}}
        if sizing_mode:
            cfg['sizing_mode'] = sizing_mode
        eng = BacktestEngine(config=cfg)
        eng.data_engine = _StubDE(self.klines)
        return eng

    def _records(self, weights):
        return [{'date': self.days[0], 'code': c, 'name': c, 'score': 80,
                 'rating': '增持', 'buy_price': 10.0, 'allocation_pct': a}
                for c, a in weights]

    def _sim(self, weights, sizing_mode=None):
        return self._engine(sizing_mode)._simulate_portfolio(self._records(weights))

    # ── A. absolute：缩放可测量（收益随敞口缩放，而非不变）──
    def test_absolute_scaling_changes_result(self):
        full = self._sim([('600001', 40.0), ('600002', 33.0), ('600003', 27.0)], 'absolute')
        half = self._sim([('600001', 20.0), ('600002', 16.5), ('600003', 13.5)], 'absolute')
        self.assertTrue(full.get('trade_details'), '用例未产生交易')
        self.assertNotAlmostEqual(full['total_return'], half['total_return'], places=4,
                                  msg='absolute 口径下缩放未被反映（应可测量）')
        # 满仓 100% 与半仓 50%：正收益敞口下满仓收益应约为半仓的 2 倍
        ratio = full['total_return'] / half['total_return'] if half['total_return'] else None
        self.assertIsNotNone(ratio)
        self.assertGreater(ratio, 1.5, f'收益未随敞口放大（ratio={ratio}）')
        self.assertLess(ratio, 2.6, f'收益放大超出线性范围（ratio={ratio}）')

    # ── B. 合计 100% 时不缩放 → 两口径一致（历史可比性）──
    def test_full_deployment_matches_normalized(self):
        w = [('600001', 40.0), ('600002', 33.0), ('600003', 27.0)]
        norm = self._sim(w, 'normalized')
        abso = self._sim(w, 'absolute')
        self.assertAlmostEqual(norm['total_return'], abso['total_return'], places=6)
        self.assertAlmostEqual(norm['max_drawdown'], abso['max_drawdown'], places=6)

    # ── C. absolute 下现金留存生效（合计 40% ≠ 满仓）──
    def test_partial_deployment_keeps_cash(self):
        full = self._sim([('600001', 40.0), ('600002', 33.0), ('600003', 27.0)], 'absolute')
        part = self._sim([('600001', 15.0), ('600002', 13.2), ('600003', 11.8)], 'absolute')
        self.assertLess(abs(part['total_return']), abs(full['total_return']),
                        '部分仓位收益绝对值应小于满仓（现金不参与盈亏）')

    # ── D. 默认口径回归保护 ──
    def test_default_is_normalized_invariant(self):
        w = [('600001', 40.0), ('600002', 33.0), ('600003', 27.0)]
        s = [('600001', 16.0), ('600002', 13.2), ('600003', 10.8)]
        a = self._sim(w)             # 默认（normalized）
        b = self._sim(s)             # 默认（normalized）
        self.assertAlmostEqual(a['total_return'], b['total_return'], places=6,
                               msg='默认口径的尺度不变性被破坏（历史可比性风险）')

    # ── E. absolute 超配按比例收缩，不依赖买入顺序 ──
    def test_absolute_overallocation_order_independent(self):
        """Σalloc=120% > 100% → 按比例收缩到 100%，结果不依赖记录顺序"""
        w = [('600001', 50.0), ('600002', 40.0), ('600003', 30.0)]
        a = self._sim(w, 'absolute')
        b = self._sim(list(reversed(w)), 'absolute')   # 逆序
        self.assertTrue(a.get('trade_details'))
        self.assertAlmostEqual(a['total_return'], b['total_return'], places=6,
                               msg='absolute 口径结果依赖买入顺序（顺序截断伪影）')
        # 所有持仓的成本合计不超过初始资金的 100%（+滑点容忍）
        total_cost = sum(t.get('cost_total', 0) for t in a['trade_details']
                         if t.get('entry_date') == self.days[0])
        self.assertLessEqual(total_cost, 1_000_000 * 1.02,
                             f'超配未收缩：首日投入 {total_cost:.0f} 超过本金')

    def test_absolute_partial_no_cap_scale(self):
        """Σalloc=40% < 100% → 不收缩，按绝对投入（现金留存）"""
        w = [('600001', 15.0), ('600002', 13.0), ('600003', 12.0)]
        pf = self._sim(w, 'absolute')
        full = self._sim([('600001', 40.0), ('600002', 33.0), ('600003', 27.0)], 'absolute')
        self.assertLess(abs(pf['total_return']), abs(full['total_return']))


if __name__ == '__main__':
    unittest.main()


class TestDuplicateBuyCashLeak(unittest.TestCase):
    """回归：持仓期内同 code 再次被推荐 → 跳过开仓，且**不得扣减现金**（2026-09-18 修复）

    原 bug：`cash -= allocated` 在重复持仓 `continue` 之前执行 → 仓位未建立但现金
    已扣 → 凭空亏损（每次 ≈ 单笔仓位 33-40% 权益）。曾导致多次 A/B 结论失真。
    """

    def setUp(self):
        self.days = _trading_days()
        self.klines = {'600001': _make_kline(self.days, 1.01),
                       '600002': _make_kline(self.days, 1.01)}

    def _sim(self, records, mode='normalized'):
        eng = BacktestEngine(config={'initial_capital': 1_000_000,
                                     'fill_convention': 'open_t1',
                                     'sizing_mode': mode,
                                     'sell': {'take_profit': 0.02, 'stop_loss': -0.02,
                                              'time_stop_days': 3}})
        eng.data_engine = _StubDE(self.klines)
        return eng._simulate_portfolio(records)

    def test_duplicate_recommendation_does_not_leak_cash(self):
        base = [{'date': self.days[0], 'code': '600001', 'name': 'a', 'score': 80,
                 'rating': '增持', 'buy_price': 10.0, 'allocation_pct': 40.0}]
        # 同一票在后续交易日（仍在 T+3 持仓期）再次被推荐
        dup = base + [{'date': self.days[1], 'code': '600001', 'name': 'a', 'score': 80,
                       'rating': '增持', 'buy_price': 10.0, 'allocation_pct': 40.0}]
        a, b = self._sim(base), self._sim(dup)
        self.assertTrue(a.get('trade_details'), '用例未产生交易')
        self.assertAlmostEqual(a['total_return'], b['total_return'], places=6,
                               msg='重复推荐导致净值变化（现金被重复扣减 → 泄漏）')

    def test_duplicate_in_absolute_mode_no_leak(self):
        base = [{'date': self.days[0], 'code': '600001', 'name': 'a', 'score': 80,
                 'rating': '增持', 'buy_price': 10.0, 'allocation_pct': 40.0}]
        dup = base + [{'date': self.days[1], 'code': '600001', 'name': 'a', 'score': 80,
                       'rating': '增持', 'buy_price': 10.0, 'allocation_pct': 40.0}]
        a, b = self._sim(base, 'absolute'), self._sim(dup, 'absolute')
        self.assertAlmostEqual(a['total_return'], b['total_return'], places=6,
                               msg='absolute 口径下重复推荐泄漏现金')


class TestCashConstrainedDeterminism(unittest.TestCase):
    """审查修复回归（2026-09-18）：现金受限时的分配需与记录顺序无关"""

    def setUp(self):
        self.days = _trading_days()
        self.klines = {f'60000{i}': _make_kline(self.days, 1.01) for i in range(1, 4)}

    def _sim(self, recs, mode):
        eng = BacktestEngine(config={'initial_capital': 1_000_000,
                                     'fill_convention': 'open_t1', 'sizing_mode': mode,
                                     'sell': {'take_profit': 0.02, 'stop_loss': -0.02,
                                              'time_stop_days': 3}})
        eng.data_engine = _StubDE(self.klines)
        return eng._simulate_portfolio(recs)

    def test_absolute_cash_constrained_order_independent(self):
        """两批推荐 + 现金受限场景：正序/逆序结果必须一致"""
        d0, d1 = self.days[0], self.days[1]
        recs = [{'date': d0, 'code': '600001', 'name': 'a', 'score': 80,
                 'rating': '增持', 'buy_price': 10.0, 'allocation_pct': 60.0},
                {'date': d0, 'code': '600002', 'name': 'b', 'score': 79,
                 'rating': '增持', 'buy_price': 10.0, 'allocation_pct': 60.0},
                {'date': d1, 'code': '600003', 'name': 'c', 'score': 78,
                 'rating': '增持', 'buy_price': 10.0, 'allocation_pct': 60.0}]
        a = self._sim(recs, 'absolute')
        b = self._sim(list(reversed(recs)), 'absolute')
        self.assertTrue(a.get('trade_details'), '用例未产生交易')
        self.assertAlmostEqual(a['total_return'], b['total_return'], places=6,
                               msg='现金受限下分配依赖记录顺序（非确定性）')
