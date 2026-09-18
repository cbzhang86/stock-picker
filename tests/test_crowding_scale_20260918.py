# -*- coding: utf-8 -*-
"""R1 拥挤度分档仓位压缩配套测试（2026-09-18）"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _s(levels):
    from strategies.short_term import ShortTermStrategy
    return ShortTermStrategy({'buy': {'crowding_scale_levels': levels}})


def _a(avg_chg):
    return {'details': {'avg_chg': avg_chg}}


class TestCrowdingPositionScale(unittest.TestCase):
    """分档压缩：按阈值降序匹配首个满足项；未匹配 1.0；关闭时恒 1.0"""

    LEVELS = [[1.5, 0.25], [0.5, 0.5]]

    def test_top_tier(self):
        self.assertAlmostEqual(_s(self.LEVELS)._crowding_position_scale(_a(1.8)), 0.25)

    def test_mid_tier(self):
        self.assertAlmostEqual(_s(self.LEVELS)._crowding_position_scale(_a(0.7)), 0.5)

    def test_neutral_no_scale(self):
        self.assertAlmostEqual(_s(self.LEVELS)._crowding_position_scale(_a(0.2)), 1.0)

    def test_boundary_matches_tier(self):
        self.assertAlmostEqual(_s(self.LEVELS)._crowding_position_scale(_a(0.5)), 0.5)
        self.assertAlmostEqual(_s(self.LEVELS)._crowding_position_scale(_a(1.5)), 0.25)

    def test_symmetric_down_day(self):
        """对称口径：暴跌日同样压缩（-1.8% → 0.25）"""
        self.assertAlmostEqual(_s(self.LEVELS)._crowding_position_scale(_a(-1.8)), 0.25)

    def test_disabled_when_empty(self):
        self.assertAlmostEqual(_s([])._crowding_position_scale(_a(3.0)), 1.0)

    def test_missing_details_safe(self):
        self.assertAlmostEqual(_s(self.LEVELS)._crowding_position_scale({}), 1.0)
        self.assertAlmostEqual(_s(self.LEVELS)._crowding_position_scale({'details': {}}), 1.0)

    def test_unsorted_levels_handled(self):
        """乱序输入也按阈值降序匹配（防配置顺序错误）"""
        s = _s([[0.5, 0.5], [1.5, 0.25]])
        self.assertAlmostEqual(s._crowding_position_scale(_a(1.8)), 0.25)
        self.assertAlmostEqual(s._crowding_position_scale(_a(0.6)), 0.5)

    def test_config_default_disabled(self):
        """config.yml 登记 crowding_scale_levels: []（默认关闭）

        原因：回测组合模拟按日归一化（尺度不变），仓位类改动在回测中不可验证；
        混合缩放还会因延期买入使归一化分母失真（2026-09-18 实测伪结果）。
        """
        import io
        import re
        cfg = io.open(os.path.join(PROJECT_ROOT, 'config.yml'),
                      encoding='utf-8').read()
        self.assertIsNotNone(
            re.search(r'(?m)^\s*crowding_scale_levels:\s*\[\]\s*$', cfg),
            'config.yml crowding_scale_levels 应为 []（回测不可验证 → 默认关闭）')


if __name__ == '__main__':
    unittest.main()
