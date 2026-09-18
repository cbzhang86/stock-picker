# -*- coding: utf-8 -*-
"""P2-4 分区间 / 分市场状态检验 — 单元测试（合成数据驱动）

覆盖验收清单：
  - 盈利期数/总期数计算正确
  - "最赚一期占比"与"去掉最赚一期"算得对
  - 边界：end 落在某月最后一天时，该月不被截断
  - 0 交易 → 优雅处理（提示 + 非 0 退出）
  - 可选大盘序列 → market_pct 正确累计
"""
import json
import os
import sys
import tempfile
import unittest

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(PROJECT_ROOT, 'scripts'))
import subperiod as s


# 1月+10 / 2月-4 / 3月+6 / 6月(月底)+3
TRADES = [
    {'date': '2026-01-05', 'ret': 4.0}, {'date': '2026-01-20', 'ret': 6.0},
    {'date': '2026-02-10', 'ret': -2.0}, {'date': '2026-02-25', 'ret': -2.0},
    {'date': '2026-03-03', 'ret': 3.0}, {'date': '2026-03-30', 'ret': 3.0},
    {'date': '2026-06-30', 'ret': 3.0},
]


class TestSubperiod(unittest.TestCase):

    def test_monthly_counts(self):
        res = s.analyze(TRADES, freq='M')
        self.assertEqual(res['n_periods'], 4)
        self.assertEqual(res['n_positive'], 3)
        self.assertAlmostEqual(res['total_pnl'], 15.0, places=6)

    def test_best_share_and_after_best(self):
        res = s.analyze(TRADES, freq='M')
        self.assertEqual(res['best_period'], '2026-01')
        # 最赚一期 +10 / 总 +15 ≈ 0.6667
        self.assertAlmostEqual(res['best_share'], 10.0 / 15.0, places=3)
        # 去掉最赚一期：15 - 10 = 5
        self.assertAlmostEqual(res['after_best'], 5.0, places=6)

    def test_quarterly(self):
        res = s.analyze(TRADES, freq='Q')
        # Q1(1-3月): +12, Q2(4-6月): +3 → 2 期，均盈利
        self.assertEqual(res['n_periods'], 2)
        self.assertEqual(res['n_positive'], 2)
        self.assertAlmostEqual(res['total_pnl'], 15.0, places=6)

    def test_month_end_boundary_not_truncated(self):
        # end=2026-06-30 应把 6/30 的交易计入 2026-06（不被截断成空月）
        res = s.analyze(TRADES, freq='M', end='2026-06-30')
        periods = [r['period'] for r in res['rows']]
        self.assertIn('2026-06', periods, "end 落在月最后一天时该月不应被截断")
        jun = next(r for r in res['rows'] if r['period'] == '2026-06')
        self.assertAlmostEqual(jun['pnl'], 3.0, places=6)

    def test_zero_trades_raises(self):
        with self.assertRaises(ValueError):
            s.analyze([])

    def test_cli_zero_trades_nonzero_exit(self):
        fd, path = tempfile.mkstemp(suffix='.json')
        os.close(fd)
        with open(path, 'w', encoding='utf-8') as f:
            json.dump([], f)
        try:
            rc = s.main(['--trades', path])
            self.assertNotEqual(rc, 0, "0 交易应非 0 退出码")
        finally:
            os.remove(path)

    def test_cli_success_returns_zero(self):
        fd, path = tempfile.mkstemp(suffix='.json')
        os.close(fd)
        with open(path, 'w', encoding='utf-8') as f:
            json.dump(TRADES, f)
        try:
            rc = s.main(['--trades', path])
            self.assertEqual(rc, 0, "有效输入应退出码 0")
        finally:
            os.remove(path)

    def test_market_pct(self):
        # 大盘：1月每日 +0.1%（2天）→ 月累计 +0.2；其余无
        market = {'2026-01-05': 0.1, '2026-01-20': 0.1}
        res = s.analyze(TRADES, freq='M', market=market)
        jan = next(r for r in res['rows'] if r['period'] == '2026-01')
        self.assertIsNotNone(jan['market_pct'])
        self.assertAlmostEqual(jan['market_pct'], 0.2, places=6)


if __name__ == '__main__':
    unittest.main(verbosity=2)
