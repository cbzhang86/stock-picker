# -*- coding: utf-8 -*-
"""回归测试：组合买入资金基数（2026-09-17 缺陷2 + 缺陷3）

背景：
  缺陷2：`_simulate_portfolio` 买入循环内用递减的 cash 作基数
         `allocated = cash * (allocation_pct / total_alloc)`，导致：
           - 实投合计被低估（两票各30% 实投 0.5C+0.25C=0.75C，应≈ base_equity）；
           - 同权两票因循环顺序得到不等投入额（先后 0.5C / 0.25C）。
         修复：进入循环前固定 base_equity = cash + 持仓市值，allocated 以 base_equity 为基数；
              并加防御 `if allocated > cash: allocated = cash`（不得透支）。
  缺陷3：持仓期内同 code 再次被推荐时直接覆写台账（旧 shares/cost_total 丢失而 cash 已扣 →
         净值低估）。修复：开仓前 `if rec['code'] in positions: continue`。

注（口径边界，已在交付报告中提出）：
  任务陈述"投入合计 ≈ 0.6 × base_equity" 假设 allocated = base × allocation_pct（不归一化）。
  但 allocation_pct 是百分制(0-100)，既有尺度不变性测试
  test_portfolio_sim_scale_invariance_20260916 锁定 /total_alloc 归一化（单票40%≡100%，
  即总额恒=base_equity）。为不破坏既有门禁，本次保留归一化口径，仅固定基数消除
  欠投/顺序依赖——修正后两票各30%投入合计 = base_equity（100%，顺序无关）。
  若需真正尊重组合优化器"现金不回补"（0.6×base），需同步修订尺度不变性测试，属后续项。
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
        raise unittest.SkipTest('交易日历不可用，跳过组合资金基数测试')
    return list(days)[:n]


def _make_kline(dates, pattern):
    step = {'up': 1.03, 'down': 0.97, 'flat': 1.0}[pattern]
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


class TestPortfolioCapitalBase20260917(unittest.TestCase):

    def setUp(self):
        self.days = _trading_days()
        self.klines = {
            '600001': _make_kline(self.days, 'up'),
            '600002': _make_kline(self.days, 'down'),
            '600003': _make_kline(self.days, 'flat'),
        }

    def _engine(self):
        eng = BacktestEngine(config={'initial_capital': 1_000_000,
                                     'fill_convention': 'open_t1',
                                     'slippage_tiers': [[2e6, 0.001], [1e7, 0.002],
                                                        [1e8, 0.004], [1e12, 0.008]],
                                     'sell': {'take_profit': 0.02,
                                              'stop_loss': -0.02,
                                              'time_stop_days': 3}})
        eng.data_engine = _StubDE(self.klines)
        return eng

    def _records(self, weights):
        return [{'date': self.days[0], 'code': c, 'name': c, 'score': 80,
                 'rating': '增持', 'buy_price': 10.0, 'allocation_pct': a}
                for c, a in weights]

    def _sim(self, weights):
        return self._engine()._simulate_portfolio(self._records(weights))

    def _capital_sum(self, pf):
        return sum(t['capital_used'] for t in pf['trade_details'])

    # ── 缺陷2：固定基数 + 等权均分（顺序无关）──

    def test_equal_weights_split_equally_regardless_of_order(self):
        """等权两票应均分 base_equity；修复前会因 cash 递减导致先后 0.5C/0.25C。"""
        fwd = self._sim([('600001', 30.0), ('600002', 30.0)])
        rev = self._sim([('600002', 30.0), ('600001', 30.0)])
        fwd_amts = sorted(t['capital_used'] for t in fwd['trade_details'])
        rev_amts = sorted(t['capital_used'] for t in rev['trade_details'])
        # 等权 → 各 0.5C（base_equity=1M 时）
        self.assertAlmostEqual(fwd_amts[0], 500_000.0, delta=1.0)
        self.assertAlmostEqual(fwd_amts[1], 500_000.0, delta=1.0)
        self.assertAlmostEqual(fwd_amts[0], fwd_amts[1], delta=1.0)  # 先后均分
        # 顺序颠倒后投入额集合不变
        self.assertEqual(fwd_amts, rev_amts)

    def test_total_invested_equals_base_equity(self):
        """归一化口径下两票各30%投入合计 = base_equity（100%）；修复前为 0.75C。

        说明：base_equity 在首买日 = cash = 1M（无既有持仓），归一化后
        总额恒 = base_equity。详见文件头注释关于 0.6×base 的口径边界说明。
        """
        pf = self._sim([('600001', 30.0), ('600002', 30.0)])
        total = self._capital_sum(pf)
        self.assertAlmostEqual(total, 1_000_000.0, delta=1.0)

    def test_unequal_weights_still_order_independent(self):
        """不等权（40/20）下，修复前先后投入额不同（0.667C/0.111C），
        修复后固定基数 → 各自投入与顺序无关（0.667C / 0.333C）。"""
        fwd = self._sim([('600001', 40.0), ('600002', 20.0)])
        rev = self._sim([('600002', 20.0), ('600001', 40.0)])
        fwd_amts = sorted(t['capital_used'] for t in fwd['trade_details'])
        rev_amts = sorted(t['capital_used'] for t in rev['trade_details'])
        # 40% 票拿 base*(40/60)=0.667M，20% 票拿 base*(20/60)=0.333M
        self.assertAlmostEqual(fwd_amts[1], 666_666.0, delta=2.0)
        self.assertAlmostEqual(fwd_amts[0], 333_333.0, delta=2.0)
        self.assertEqual(fwd_amts, rev_amts)  # 顺序无关

    # ── 缺陷2：现金不足不透支（现金不回补）──

    def test_no_overdraft_total_le_initial_capital(self):
        """三票各40%(total_alloc=120) 归一化后总额仍 = base_equity ≤ 初始资金，
        不得透支——验证投入合计不超过初始资金（无外部现金注入）。"""
        pf = self._sim([('600001', 40.0), ('600002', 40.0), ('600003', 40.0)])
        total = self._capital_sum(pf)
        self.assertLessEqual(total, 1_000_000.0 + 1.0)
        # 净值曲线不应出现透支（最低不为负）
        self.assertGreaterEqual(min(pf['equity_curve']), -1.0)

    def test_no_negative_cash_with_existing_position(self):
        """已有持仓占用全部现金且未平仓时，新仓 allocated 受 `if allocated > cash`
        防御限制，不得透支（cash 不为负）。用 'flat' 让 A 持仓期覆盖 B 的买入日，
        使 B 买入时现金已为 0。"""
        # A 用 flat（时间止损3日）确保持仓期内覆盖 B 的买入日，占用全部现金
        self.klines['600001'] = _make_kline(self.days, 'flat')
        recs = [
            {'date': self.days[0], 'code': '600001', 'name': '600001', 'score': 80,
             'rating': '增持', 'buy_price': 10.0, 'allocation_pct': 30.0},
            {'date': self.days[1], 'code': '600002', 'name': '600002', 'score': 80,
             'rating': '增持', 'buy_price': 10.0, 'allocation_pct': 30.0},
        ]
        eng = self._engine()
        pf = eng._simulate_portfolio(recs)
        # A 单票归一化买满全部现金(≈1M)后，B 买入时现金为 0 → allocated 被 `> cash`
        # 防御截断为 0，B 不开仓（不透支）。故只应有 1 笔成交。
        self.assertEqual(len(pf['trade_details']), 1,
                         '现金不足时新仓应被防御截断，不应透支开仓')
        # 组合净值曲线不得出现资不抵债（cash 防御保证不为负）
        self.assertGreaterEqual(min(pf['equity_curve']), 0.0)

    # ── 缺陷3：重复推荐同一 code 只开一次仓 ──

    def test_duplicate_code_opens_once(self):
        recs = [
            {'date': self.days[0], 'code': '600001', 'name': '600001', 'score': 80,
             'rating': '增持', 'buy_price': 10.0, 'allocation_pct': 30.0},
            {'date': self.days[0], 'code': '600001', 'name': '600001', 'score': 80,
             'rating': '增持', 'buy_price': 10.0, 'allocation_pct': 30.0},
        ]
        pf = self._engine()._simulate_portfolio(recs)
        codes = [t['code'] for t in pf['trade_details']]
        self.assertEqual(codes.count('600001'), 1, '重复 code 不应重复开仓')


if __name__ == '__main__':
    unittest.main()
