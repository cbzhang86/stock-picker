# -*- coding: utf-8 -*-
"""回归测试：退出路径 T+N 交易日定位（2026-09-17，P0-4 边界）

范围说明（重要）：
  本文件【仅】对既有的 `_locate_tn_rows`（按交易日历定位 T+1/T+5 目标行，
  缺口顺延/超限弃算）做回归锁定。这是退出路径应使用、且 `_calculate_results` 已经在
  用的 T+N 定位能力。

  `_simulate_portfolio` 内联的退出逻辑（L1079 起）与 `_simulate_exit_path` 是两套实现，
  其收敛（让组合模拟的退出也走 `_locate_tn_rows` 日历口径）被明确列为 P0-4 后续项，
  **不在本次缺陷1/2/3 的改动范围内**（任务要求"本次不合并"）。故本测试不触碰组合退出
  实现，只守住"T+N 定位"这一底层能力不被破坏，避免两套口径再次分叉。
"""
import os
import sys
import unittest

import pandas as pd

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from core.backtest_engine import BacktestEngine  # noqa: E402


def _make_kline_dates(date_list):
    """date_list: 升序 'YYYY-MM-DD' 字符串列表，构造最小 K 线（含 date 列）。"""
    px, rows = 10.0, []
    for d in date_list:
        rows.append({'date': d, 'open': px, 'high': px * 1.001,
                     'low': px * 0.999, 'close': px, 'volume': 1e6, 'amount': 1e8})
        px = round(px * 1.001, 4)
    return pd.DataFrame(rows)


class TestExitPathTnLocating20260917(unittest.TestCase):

    @staticmethod
    def _calendar(buy, end):
        from core.trading_calendar import trading_days
        days = list(trading_days(buy, end))
        return days

    def _engine(self):
        return BacktestEngine(config={
            'slippage_tiers': [[2e6, 0.001], [1e7, 0.002], [1e8, 0.004], [1e12, 0.008]]})

    def test_locate_t1_t5_matches_calendar(self):
        cal = self._calendar('2026-09-07', '2026-09-30')
        if not cal:
            self.skipTest('交易日历不可用')
        buy = '2026-09-07'
        future = [d for d in cal if d > buy]
        if len(future) < 5:
            self.skipTest('交易日历样本不足')
        t1_target, t5_target = future[0], future[4]
        kline = _make_kline_dates([buy] + future[:8])
        t1, t5 = self._engine()._locate_tn_rows(kline, buy, t5_needed=True)
        self.assertIsNotNone(t1)
        self.assertIsNotNone(t5)
        self.assertEqual(str(kline.iloc[t1]['date']), t1_target)
        self.assertEqual(str(kline.iloc[t5]['date']), t5_target)

    def test_locate_t5_defers_on_small_gap(self):
        """T+5 目标日缺失但其后1交易日存在（缺口≤3）→ 顺延到最近存在的行。"""
        cal = self._calendar('2026-09-07', '2026-09-30')
        if not cal:
            self.skipTest('交易日历不可用')
        buy = '2026-09-07'
        future = [d for d in cal if d > buy]
        if len(future) < 6:
            self.skipTest('样本不足')
        t5_target = future[4]
        t5_next = future[5]  # 缺口后最近交易日（顺延1日）
        kline = _make_kline_dates([buy] + [d for d in future[:8] if d != t5_target])
        _, t5 = self._engine()._locate_tn_rows(kline, buy, t5_needed=True)
        self.assertIsNotNone(t5)
        self.assertEqual(str(kline.iloc[t5]['date']), t5_next)

    def test_locate_t5_abandoned_on_large_gap(self):
        """T+5 目标日缺失且顺延超过3个交易日 → 弃算返回 None（不静默错位）。"""
        cal = self._calendar('2026-09-07', '2026-09-30')
        if not cal:
            self.skipTest('交易日历不可用')
        buy = '2026-09-07'
        future = [d for d in cal if d > buy]
        if len(future) < 9:
            self.skipTest('样本不足')
        t5_target = future[4]
        # 移除 t5_target 起其后连续 4 个交易日（顺延>3）→ 应弃算
        removed = set(future[4:8])
        kline = _make_kline_dates([buy] + [d for d in future[:10] if d not in removed])
        _, t5 = self._engine()._locate_tn_rows(kline, buy, t5_needed=True)
        self.assertIsNone(t5)

    def test_locate_t1_none_abandons_trade(self):
        """T+1 目标日缺失（决策日即末行，无后续）→ t1_idx=None，该笔应弃算。"""
        cal = self._calendar('2026-09-07', '2026-09-30')
        if not cal:
            self.skipTest('交易日历不可用')
        buy = '2026-09-07'
        future = [d for d in cal if d > buy]
        if not future:
            self.skipTest('样本不足')
        # kline 仅含决策日，无 T+1
        kline = _make_kline_dates([buy])
        t1, _ = self._engine()._locate_tn_rows(kline, buy, t5_needed=True)
        self.assertIsNone(t1)


if __name__ == '__main__':
    unittest.main()
