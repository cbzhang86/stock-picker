# -*- coding: utf-8 -*-
"""P4 尾盘急拉过滤配套测试（2026-09-18）"""
import os
import sys
import unittest

import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _make_strategy(max_close_pos):
    from strategies.short_term import ShortTermStrategy
    return ShortTermStrategy({'buy': {'max_close_pos': max_close_pos}})


def _quotes(rows):
    """rows: [(code, name, price, pct_chg, amount, high, low)]"""
    return pd.DataFrame(rows, columns=['code', 'name', 'price', 'pct_chg',
                                       'amount', 'high', 'low'])


class TestClosePosFilter(unittest.TestCase):
    """尾盘急拉过滤：收盘位置超阈值剔除，缺失数据不误伤"""

    def test_high_close_pos_filtered(self):
        s = _make_strategy(0.85)
        # 价格 10.9，low=10 high=11 → close_pos=0.9 > 0.85 → 剔除
        q = _quotes([('000001', '急拉票', 10.9, 2.0, 5e7, 11.0, 10.0)])
        self.assertEqual(len(s._prefilter(q)), 0, 'close_pos=0.9 应被剔除')

    def test_mid_close_pos_kept(self):
        s = _make_strategy(0.85)
        # 价格 10.5 → close_pos=0.5 → 保留
        q = _quotes([('000002', '中性票', 10.5, 1.0, 5e7, 11.0, 10.0)])
        out = s._prefilter(q)
        self.assertEqual(len(out), 1, 'close_pos=0.5 应保留')
        self.assertAlmostEqual(out[0]['close_pos'], 0.5, places=3)

    def test_missing_high_low_not_filtered(self):
        s = _make_strategy(0.85)
        q = _quotes([('000003', '缺高低', 10.5, 1.0, 5e7, None, None)])
        out = s._prefilter(q)
        self.assertEqual(len(out), 1, '缺 high/low 不应误伤（缺失数据契约）')

    def test_flat_bar_not_filtered(self):
        """最高=最低（一字板）→ 无法计算 close_pos → 不过滤。

        注意：pct_chg 用 1.0 而非涨停值——一字涨停会被 exclude_limit_up
        硬过滤（正确行为），这里要隔离验证的只是 close_pos 过滤自身。
        """
        s = _make_strategy(0.85)
        q = _quotes([('000004', '一字', 10.5, 1.0, 5e7, 10.5, 10.5)])
        out = s._prefilter(q)
        self.assertEqual(len(out), 1, 'high==low 应跳过过滤（不误伤）')

    def test_disabled_when_zero(self):
        s = _make_strategy(0)
        q = _quotes([('000005', '急拉票', 11.0, 5.0, 5e7, 11.0, 10.0)])
        self.assertEqual(len(s._prefilter(q)), 1, 'max_close_pos=0 应完全关闭')

    def test_config_default_wiring(self):
        """config.yml 登记了 max_close_pos（P4 A/B 后默认 0=关闭，链路保留）"""
        import io
        import re
        cfg = io.open(os.path.join(PROJECT_ROOT, 'config.yml'),
                      encoding='utf-8').read()
        self.assertIsNotNone(
            re.search(r'(?m)^\s*max_close_pos:\s*0(\.0)?\s*$', cfg),
            'config.yml max_close_pos 应回归默认关闭（A/B 结论：池子边际≈0 时过滤无效）')

    def test_backtest_snapshot_has_high_low(self):
        """回测快照行含 high/low（P4 在回测生效的前提）"""
        import inspect
        from core import backtest_engine
        src = inspect.getsource(backtest_engine.BacktestEngine._load_historical_snapshots)
        self.assertIn("'high'", src, '回测快照缺 high 字段')
        self.assertIn("'low'", src, '回测快照缺 low 字段')


if __name__ == '__main__':
    unittest.main()
