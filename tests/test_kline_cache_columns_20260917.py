"""
2026-09-17 回归测试：K 线缓存命中路径必须补算技术指标（缺陷3修复）

背景：get_kline 的缓存命中路径（data_engine.py）直接返回 window，未调用
_calc_kline_indicators；而 live/抓取路径会算 pct_chg/ma5/ma10/ma20/
avg_volume_5/volume_ratio。缓存表只持久化 8 个原始列，故命中路径必须在返回前
补算 —— 否则同一 DataFrame 在冷热缓存下列集不一致，行为随缓存冷热而变。

本测试：构造同一 code 分别走「冷缓存（无缓存→触发抓取）」与「热缓存（已有缓存
→ 命中）」两条路径，断言两次返回的 columns 集合完全一致且包含全部 6 个指标列。
"""
import unittest
from unittest import mock

import numpy as np
import pandas as pd

from core.data_engine import DataEngine

_INDICATOR_COLS = {'pct_chg', 'ma5', 'ma10', 'ma20', 'avg_volume_5', 'volume_ratio'}
_RAW_COLS = ['date', 'open', 'high', 'low', 'close', 'volume', 'amount']


def _raw_kline(n=30):
    """构造不含指标列的"原始"K线（模拟缓存/抓取返回）。"""
    dates = pd.date_range('2026-01-01', periods=n, freq='B')
    rng = np.random.RandomState(42)
    close = 10 + np.cumsum(rng.randn(n))
    return pd.DataFrame({
        'date': dates,
        'open': close + rng.randn(n) * 0.1,
        'high': close + abs(rng.randn(n)) * 0.2,
        'low': close - abs(rng.randn(n)) * 0.2,
        'close': close,
        'volume': rng.randint(1_000_000, 5_000_000, n).astype(float),
        'amount': rng.randint(1_000_000, 5_000_000, n).astype(float),
    })


class TestKlineCacheColumns(unittest.TestCase):
    def setUp(self):
        self.de = DataEngine()
        self.code = '600000'
        self.start = '2026-01-01'
        self.end = '2026-03-01'

    def test_cold_and_hot_cache_return_identical_columns(self):
        raw = _raw_kline()

        # 冷缓存：_get_kline_from_cache 返回 None → 触发 mootdx 抓取（返回原始列）
        # 热缓存：_get_kline_from_cache 直接返回原始列（命中路径）
        cold_cache = mock.MagicMock(return_value=None)
        hot_cache = mock.MagicMock(return_value=raw.copy())
        mootdx = mock.MagicMock(return_value=raw.copy())
        saved = mock.MagicMock()

        # ---- 冷缓存路径 ----
        with mock.patch.object(self.de, '_get_kline_from_cache', cold_cache), \
             mock.patch.object(self.de, '_fetch_kline_mootdx', mootdx), \
             mock.patch.object(self.de, '_save_kline_to_cache', saved):
            cold_df = self.de.get_kline(self.code, self.start, self.end, adjust='none')

        # ---- 热缓存路径（重置调用计数，复用同一 raw）----
        cold_cache.reset_mock()
        mootdx.reset_mock()
        with mock.patch.object(self.de, '_get_kline_from_cache', hot_cache), \
             mock.patch.object(self.de, '_fetch_kline_mootdx', mootdx), \
             mock.patch.object(self.de, '_save_kline_to_cache', saved):
            hot_df = self.de.get_kline(self.code, self.start, self.end, adjust='none')

        # 热缓存路径不得触发 mootdx 抓取（证明走的是缓存命中分支）
        mootdx.assert_not_called()

        # 两次返回列集必须完全一致
        self.assertEqual(set(cold_df.columns), set(hot_df.columns),
                         "冷/热缓存返回列集不一致（缺陷3复现）")
        # 必须包含全部 6 个技术指标列
        self.assertTrue(_INDICATOR_COLS.issubset(set(cold_df.columns)),
                        f"冷缓存缺少指标列: {_INDICATOR_COLS - set(cold_df.columns)}")
        self.assertTrue(_INDICATOR_COLS.issubset(set(hot_df.columns)),
                        f"热缓存缺少指标列: {_INDICATOR_COLS - set(hot_df.columns)}")

    def test_ensure_kline_indicators_is_idempotent_on_full_cols(self):
        """_ensure_kline_indicators 对已含指标列的 DataFrame 幂等（不重复计算）。"""
        full = self.de._calc_kline_indicators(_raw_kline())
        self.assertTrue(_INDICATOR_COLS.issubset(set(full.columns)))
        again = self.de._ensure_kline_indicators(full)
        # 列集不变，且返回同一对象（未新建列）
        self.assertEqual(set(again.columns), set(full.columns))

    def test_ensure_kline_indicators_adds_missing_cols(self):
        """_ensure_kline_indicators 对缺指标列的 DataFrame 补算全部 6 列。"""
        raw = _raw_kline()
        self.assertFalse(_INDICATOR_COLS.issubset(set(raw.columns)))
        out = self.de._ensure_kline_indicators(raw)
        self.assertTrue(_INDICATOR_COLS.issubset(set(out.columns)))
        # 原始列完好
        for c in _RAW_COLS:
            self.assertIn(c, out.columns)


if __name__ == "__main__":
    unittest.main()
