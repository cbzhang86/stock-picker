# -*- coding: utf-8 -*-
"""剩余项 #1（ATR 止损真支持）与 #2（momentum 让渡）的守卫测试（2026-09-23）。

背景：`_simulate_portfolio` 与 `_calculate_results` 的 K 线拉取窗口此前从
决策日/买入日起 → `_atr_from_kline` 的 `upto_idx >= 14` 恒不成立 → 配置
`sell_config.stop_mode='atr'` 全程静默回落固定止损（"配了等于没配"）。

修复：K 线窗口前移 30 个自然日；`_locate_tn_rows_fallback` 不再假设首行=买入日；
组合日历剔除回看期。本文件锁定这些行为。
"""
import os
import sys
import unittest
from datetime import datetime, timedelta

import pandas as pd

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from core.backtest_engine import BacktestEngine  # noqa: E402


def _mk_kline(n, start='2026-06-01', close=10.0):
    dates = pd.date_range(start, periods=n, freq='D').strftime('%Y-%m-%d')
    return pd.DataFrame({
        'date': dates,
        'open': [close] * n, 'high': [close + 0.1] * n,
        'low': [close - 0.1] * n, 'close': [close] * n,
        'volume': [1e6] * n, 'amount': [1e7] * n,
    })


class TestAtrLookback(unittest.TestCase):
    """ATR14 需要入场前历史：窗口前移后必须可计算。"""

    def test_atr_computable_with_history(self):
        k = _mk_kline(40)                      # 买入行在 idx≈20（前移 30 自然日）
        atr = BacktestEngine._atr_from_kline(k, 20)
        self.assertIsNotNone(atr, 'upto_idx=20 时 ATR14 应可计算')
        self.assertGreater(atr, 0)

    def test_atr_none_without_history(self):
        k = _mk_kline(10)
        self.assertIsNone(BacktestEngine._atr_from_kline(k, 1),
                          '入场前历史不足 14 根时应返回 None（回退固定止损）')

    def test_locate_tn_with_lookback_kline(self):
        """K 线首行早于 buy_date 时，T+N 仍按日期正确定位。"""
        be = BacktestEngine.__new__(BacktestEngine)   # 不触发网络初始化
        buy_date = '2026-06-21'
        k = _mk_kline(40, start='2026-06-01')          # 首行早于 buy_date
        t1_idx, t5_idx = be._locate_tn_rows(k, buy_date)
        self.assertIsNotNone(t1_idx)
        # t1 行日期应严格晚于 buy_date
        t1_date = str(pd.Timestamp(k.iloc[t1_idx]['date']).date())
        self.assertGreater(t1_date, buy_date)
        # t5 在 t1 之后 4 行
        self.assertEqual(t5_idx - t1_idx, 4)

    def test_locate_tn_fallback_uses_buy_date(self):
        """日历不可用回退时，T+N 必须相对**买入行**定位（而非 K 线首行）。"""
        be = BacktestEngine.__new__(BacktestEngine)
        buy_date = '2026-06-21'
        k = _mk_kline(40, start='2026-06-01')
        t1_idx, t5_idx = be._locate_tn_rows_fallback(k, buy_date)
        entry = BacktestEngine._entry_row_index(k, buy_date)
        self.assertEqual(t1_idx, entry + 1)
        self.assertEqual(t5_idx, entry + 5)
        # 回退路径同样满足 ATR 的历史要求
        self.assertGreaterEqual(t1_idx, 14)

    def test_entry_row_index(self):
        k = _mk_kline(40, start='2026-06-01')
        self.assertEqual(BacktestEngine._entry_row_index(k, '2026-06-01'), 0)
        self.assertEqual(BacktestEngine._entry_row_index(k, '2026-06-05'), 4)
        # 超出 K 线末尾 → None（该笔无法定位，弃算）
        self.assertIsNone(BacktestEngine._entry_row_index(k, '2026-09-01'))
        # 缺口顺延：工作日 K 线（周六缺失）→ 周六的买入日顺延到周一
        kb = _mk_kline(40, start='2026-06-01')
        kb['date'] = pd.bdate_range('2026-06-01', periods=40).strftime('%Y-%m-%d')
        # 2026-06-06 是周六 → 应顺延到下一个工作日行
        sat_idx = BacktestEngine._entry_row_index(kb, '2026-06-06')
        self.assertEqual(str(pd.Timestamp(kb.iloc[sat_idx]['date']).date()),
                         '2026-06-08')


class TestPortfolioCalendarExcludesLookback(unittest.TestCase):
    """组合日历必须剔除 K 线回看期（否则 Sharpe 分母失真）。"""

    def test_source_has_lookback_filter(self):
        import io
        src = io.open(os.path.join(ROOT, 'core', 'backtest_engine.py'),
                      encoding='utf-8').read()
        self.assertIn('_first_decision', src,
                      '组合日历应按首个决策日过滤 K 线回看期')
        lookback_start = src.count("_look_start")
        self.assertGreaterEqual(lookback_start, 2,
                                '两条 K 线拉取路径（组合/逐笔）都应前移窗口')


class TestMomentumAvailabilityTransfer(unittest.TestCase):
    """剩余项 #2：rps_20 缺失 → momentum/reversal_20d 让渡权重。"""

    def test_missing_rps_transfers_weight(self):
        from core.scoring_model import ScoringModel
        sm = ScoringModel()
        r = sm.score_stock({'code': 'X', 'name': 'x', 'price': 10.0,
                            'rps_20': None, 'amount': 1e8}, 'short')
        for f in ('momentum', 'reversal_20d'):
            b = r['breakdown'][f]
            self.assertFalse(b['data_available'], f'{f} 缺 rps_20 应让渡')
            self.assertEqual(b.get('effective_weight'), 0.0,
                             f'{f} 让渡后有效权重应为 0')

    def test_present_rps_counts(self):
        from core.scoring_model import ScoringModel
        sm = ScoringModel()
        r = sm.score_stock({'code': 'X', 'name': 'x', 'price': 10.0,
                            'rps_20': 80, 'amount': 1e8}, 'short')
        self.assertTrue(r['breakdown']['momentum']['data_available'])
        self.assertGreater(r['breakdown']['momentum']['effective_weight'], 0)


if __name__ == '__main__':
    unittest.main()
