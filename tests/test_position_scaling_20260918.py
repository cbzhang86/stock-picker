# -*- coding: utf-8 -*-
"""仓位机制栈参数化与总开关测试（2026-09-18）

背景：弱市压缩原为硬编码 0.5，无法 A/B；仓位机制的效果只有 absolute 口径可测。
本文件锁定：
  A. weak_market_scale 可配置（默认 0.5，可设 1.0 关闭）；
  B. 四类仓位缩放均可被 config 关闭（弱市/波动保险丝/连亏/拥挤度分档）；
  C. 「总开关 off」的等价配置语义（--position-scaling off 注入的键值）。
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class TestPositionScalingKnobs(unittest.TestCase):

    def test_weak_market_scale_default_and_override(self):
        from strategies.short_term import ShortTermStrategy
        self.assertAlmostEqual(ShortTermStrategy({'buy': {}}).weak_market_scale, 0.5)
        s = ShortTermStrategy({'buy': {'weak_market_scale': 1.0}})
        self.assertAlmostEqual(s.weak_market_scale, 1.0)
        s2 = ShortTermStrategy({'buy': {'weak_market_scale': 0.25}})
        self.assertAlmostEqual(s2.weak_market_scale, 0.25)

    def test_all_scaling_mechanisms_can_be_disabled(self):
        """总开关 off 的等价配置：四项缩放全部失效"""
        from strategies.short_term import ShortTermStrategy
        s = ShortTermStrategy({'buy': {
            'weak_market_scale': 1.0,
            'vol_breaker_scale': 1.0,
            'losing_streak_days': 0,
            'crowding_scale_levels': [],
        }})
        self.assertAlmostEqual(s.weak_market_scale, 1.0)
        self.assertAlmostEqual(s.vol_breaker_scale, 1.0)
        self.assertEqual(s.losing_streak_days, 0)
        self.assertEqual(s.crowding_scale_levels, [])
        # 拥挤度分档关闭 → 恒 1.0
        self.assertAlmostEqual(
            s._crowding_position_scale({'details': {'avg_chg': 5.0}}), 1.0)

    def test_config_registers_weak_market_scale(self):
        import io
        import re
        cfg = io.open(os.path.join(PROJECT_ROOT, 'config.yml'),
                      encoding='utf-8').read()
        self.assertIsNotNone(
            re.search(r'(?m)^\s*weak_market_scale:\s*0\.5', cfg),
            'config.yml 未登记 weak_market_scale: 0.5')

    def test_vol_breaker_not_triggered_with_high_threshold(self):
        """波动保险丝可通过提高阈值关闭"""
        from strategies.short_term import ShortTermStrategy
        s = ShortTermStrategy({'buy': {'vol_breaker_median_abs': 99.0}})
        self.assertAlmostEqual(
            s._get_vol_circuit_scale({'details': {'median_chg': 8.0}}), 1.0)


if __name__ == '__main__':
    unittest.main()
