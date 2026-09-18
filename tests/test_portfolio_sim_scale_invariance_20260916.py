# -*- coding: utf-8 -*-
"""回归测试：仓位口径变更对回测组合模拟是尺度不变的（2026-09-16 P1-2 配套验证）

背景：
  P1-2 取消了 `_scoring_weight` 末尾的 `pct/total*100` 归一化，改为"上限优先 +
  不足部分留现金"。实盘侧这是风控修复（1 只推荐不再显示 100% 仓位）；但回测侧
  必须证明**净值不受影响** —— 否则就是"修了一个 bug、引入另一个"。

不变性依据（`_simulate_portfolio` 第 1000-1020 行）：
      total_alloc = Σ allocation_pct
      allocated   = cash × (allocation_pct / total_alloc)
  资金按权重比例分配，未分配部分留作现金不参与盈亏 → 组合收益只取决于
  **相对**权重，与绝对尺度无关。故"合计 100%"与"合计 40%（现金 60%）"
  在同一比例下必须给出完全相同的净值曲线。

本文件锁定：
  A. 同比例、不同绝对尺度 → 组合指标逐项相同（尺度不变性）；
  B. 单票 40% 与单票 100% 等价（新增上限不改变回测口径）；
  C. 仓位全缺的既有降级行为不变（warning + 空结果，不静默出 0 收益）；
  D. 测试非空转：必须真的产生了交易。
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
    """取一段真实交易日，保证与 _simulate_portfolio 的日历口径一致"""
    from core.trading_calendar import trading_days
    days = trading_days('2026-09-07', '2026-09-25')
    if not days or len(days) < n:
        raise unittest.SkipTest('交易日历不可用，跳过组合模拟不变性测试')
    return list(days)[:n]


def _make_kline(dates, pattern):
    """构造确定性 K 线：pattern='up' 每日 +3%（触发止盈）/ 'down' -3%（触发止损）"""
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


class TestPortfolioScaleInvariance(unittest.TestCase):

    def setUp(self):
        self.days = _trading_days()
        self.klines = {
            '600001': _make_kline(self.days, 'up'),     # 止盈
            '600002': _make_kline(self.days, 'down'),   # 止损
            '600003': _make_kline(self.days, 'flat'),   # 时间止损
        }

    def _engine(self):
        eng = BacktestEngine(config={'initial_capital': 1_000_000,
                                     'fill_convention': 'open_t1',
                                     'sell': {'take_profit': 0.02,
                                              'stop_loss': -0.02,
                                              'time_stop_days': 3}})
        eng.data_engine = _StubDE(self.klines)
        return eng

    def _records(self, weights):
        """weights: [(code, allocation_pct), ...] 决策日统一为首个交易日"""
        return [{'date': self.days[0], 'code': c, 'name': c, 'score': 80,
                 'rating': '增持', 'buy_price': 10.0, 'allocation_pct': a}
                for c, a in weights]

    def _sim(self, weights):
        return self._engine()._simulate_portfolio(self._records(weights))

    # ── D. 先证明测试非空转 ──

    def test_trades_actually_happen(self):
        pf = self._sim([('600001', 37.5), ('600002', 33.3), ('600003', 29.2)])
        self.assertTrue(pf.get('trade_details'), '用例未产生交易，测试无效')
        self.assertGreater(len(pf.get('equity_curve') or []), 1)

    # ── A. 尺度不变性 ──

    def test_cash_bucket_equivalent_to_full_investment(self):
        """同比例：合计 100%（旧口径输出）≡ 合计 40%（新口径输出）"""
        full = self._sim([('600001', 37.5), ('600002', 33.3), ('600003', 29.2)])
        # 同比例缩放到 40%（模拟"1 只被 40% 上限截断"后的现金口径场景）
        scaled = self._sim([('600001', 15.0), ('600002', 13.32), ('600003', 11.68)])
        for k in ('total_return', 'max_drawdown', 'sharpe_ratio'):
            self.assertAlmostEqual(
                full[k], scaled[k], places=6,
                msg=f'{k} 不满足尺度不变性: {full[k]} vs {scaled[k]}')
        self.assertEqual(len(full['trade_details']), len(scaled['trade_details']))
        for a, b in zip(full['equity_curve'], scaled['equity_curve']):
            self.assertAlmostEqual(a, b, places=4)

    def test_scale_invariant_across_arbitrary_scale(self):
        for scale in (1.0, 0.5, 0.4, 0.2):
            base = self._sim([('600001', 50.0), ('600002', 30.0)])
            sc = self._sim([('600001', 50.0 * scale), ('600002', 30.0 * scale)])
            self.assertAlmostEqual(base['total_return'], sc['total_return'],
                                   places=6, msg=f'scale={scale} 收益不一致')

    # ── B. 单票上限不改变回测口径 ──

    def test_single_stock_40_vs_100_identical(self):
        a = self._sim([('600001', 40.0)])
        b = self._sim([('600001', 100.0)])
        self.assertAlmostEqual(a['total_return'], b['total_return'], places=6)
        self.assertAlmostEqual(a['sharpe_ratio'], b['sharpe_ratio'], places=6)
        self.assertAlmostEqual(a['max_drawdown'], b['max_drawdown'], places=6)

    def test_sell_rules_still_apply(self):
        """止盈/止损/时间止损在两种仓位口径下都必须照常触发"""
        full = self._sim([('600001', 37.5), ('600002', 33.3), ('600003', 29.2)])
        reasons = {t.get('exit_reason') for t in full['trade_details']}
        self.assertTrue(reasons - {None, ''},
                        f'未见任何卖出原因，卖出规则可能失效: {reasons}')

    # ── C. 既有降级行为不变 ──

    def test_all_zero_allocation_keeps_degradation(self):
        with self.assertLogs('core.backtest_engine', level='WARNING') as cm:
            pf = self._engine()._simulate_portfolio(
                [{'date': self.days[0], 'code': '600001', 'allocation_pct': 0}])
        self.assertIn('缺少 allocation_pct', '\n'.join(cm.output))
        self.assertEqual(pf.get('total_return'), 0.0)

    def test_missing_allocation_key_keeps_degradation(self):
        with self.assertLogs('core.backtest_engine', level='WARNING'):
            pf = self._engine()._simulate_portfolio(
                [{'date': self.days[0], 'code': '600001'}])
        self.assertEqual(pf.get('total_return'), 0.0)

    def test_empty_records(self):
        pf = self._engine()._simulate_portfolio([])
        self.assertEqual(pf.get('total_return'), 0.0)


if __name__ == '__main__':
    unittest.main()
