# -*- coding: utf-8 -*-
"""P3 拥挤度断路器配套测试（2026-09-18）"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _make_strategy(crowding_avg_pct):
    from strategies.short_term import ShortTermStrategy
    return ShortTermStrategy({'buy': {'crowding_avg_pct': crowding_avg_pct}})


def _assessment(avg_chg):
    return {'total': 60, 'level': '中性市',
            'details': {'avg_chg': avg_chg, 'median_chg': avg_chg / 2}}


class TestCrowdingBreaker(unittest.TestCase):
    """拥挤度断路器：极端暴涨日停推，中性日放行，0=关闭"""

    def test_triggers_on_extreme_up_day(self):
        s = _make_strategy(2.0)
        reason = s._crowding_breaker_reason(_assessment(2.5))
        self.assertIsNotNone(reason, 'avg_chg=2.5% 应触发断路器')
        self.assertIn('拥挤度断路器', reason)

    def test_triggers_on_extreme_down_day(self):
        s = _make_strategy(2.0)
        reason = s._crowding_breaker_reason(_assessment(-2.3))
        self.assertIsNotNone(reason, '对称口径：avg_chg=-2.3% 也应触发')

    def test_passes_on_neutral_day(self):
        s = _make_strategy(2.0)
        self.assertIsNone(s._crowding_breaker_reason(_assessment(1.0)),
                          'avg_chg=1.0% 不应触发')

    def test_boundary_equal_triggers(self):
        s = _make_strategy(2.0)
        self.assertIsNotNone(s._crowding_breaker_reason(_assessment(2.0)),
                              'avg_chg 恰等于阈值应触发（>= 语义）')

    def test_disabled_when_zero(self):
        s = _make_strategy(0)
        self.assertIsNone(s._crowding_breaker_reason(_assessment(9.9)),
                          'crowding_avg_pct=0 应完全关闭')

    def test_missing_details_safe(self):
        s = _make_strategy(2.0)
        self.assertIsNone(s._crowding_breaker_reason({}),
                          '缺 details 时应安全返回 None（不误伤）')
        self.assertIsNone(s._crowding_breaker_reason({'details': {}}))

    def test_config_default_wiring(self):
        """config.yml 已登记 crowding_avg_pct: 2.0"""
        import io
        import re
        cfg = io.open(os.path.join(PROJECT_ROOT, 'config.yml'),
                      encoding='utf-8').read()
        self.assertIsNotNone(
            re.search(r'(?m)^\s*crowding_avg_pct:\s*2\.0', cfg),
            'config.yml 未登记 crowding_avg_pct: 2.0')

    def test_assess_market_emits_avg_chg(self):
        """_assess_market details 应含 avg_chg（断路器数据源）"""
        import pandas as pd
        s = _make_strategy(2.0)
        quotes = pd.DataFrame({
            'code': ['000001', '000002', '600000'],
            'name': ['a', 'b', 'c'],
            'price': [10.0, 20.0, 5.0],
            'pct_chg': [1.0, -0.5, 2.0],
            'amount': [5e7, 6e7, 7e7],
        })
        m = s._assess_market(quotes, None, is_backtest=True)
        self.assertIn('avg_chg', m.get('details', {}),
                      '市场评估 details 缺 avg_chg（断路器数据源断裂）')
        self.assertAlmostEqual(m['details']['avg_chg'], (1.0 - 0.5 + 2.0) / 3, places=3)


if __name__ == '__main__':
    unittest.main()


class TestBreakerConfigRobustness(unittest.TestCase):
    """审查修复回归（2026-09-18）：阈值非数值不得崩策略"""

    def test_non_numeric_threshold_no_crash(self):
        s = _make_strategy('2.0%')          # 误配：带百分号
        self.assertIsNone(s._crowding_breaker_reason(_assessment(3.0)),
                          '非数值阈值应安全返回 None，而不是抛异常')

    def test_non_numeric_details_no_crash(self):
        s = _make_strategy(2.0)
        self.assertIsNone(s._crowding_breaker_reason({'details': {'avg_chg': 'NaN%'}}))
