# -*- coding: utf-8 -*-
"""月度滚动复检脚本配套测试（2026-09-18 P5③）

锁定：月度聚合正确 / 连续下降检测（含边界）/ 月份不足时保守不报。
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import scripts.monthly_review as mr  # noqa: E402


class TestMonthlyAggregation(unittest.TestCase):

    def test_monthly_ic_groups_by_month(self):
        dics = {'2026-01-05': 0.04, '2026-01-20': 0.06, '2026-02-03': 0.02}
        m = mr.monthly_ic(dics)
        self.assertEqual(set(m), {'2026-01', '2026-02'})
        self.assertAlmostEqual(sum(m['2026-01']) / 2, 0.05)

    def test_bad_values_skipped(self):
        dics = {'2026-01-05': None, '2026-01-06': 'x', '2026-02-03': 0.02}
        m = mr.monthly_ic(dics)
        self.assertEqual(list(m), ['2026-02'])


class TestDecayDetection(unittest.TestCase):

    def test_detects_two_month_decline(self):
        dics = {'2026-01-05': 0.05, '2026-02-05': 0.04, '2026-03-05': 0.02}
        info = mr.detect_decay(mr.monthly_ic(dics), months=2)
        self.assertTrue(info['decaying'], info)

    def test_not_decaying_on_mixed(self):
        dics = {'2026-01-05': 0.05, '2026-02-05': 0.02, '2026-03-05': 0.04}
        info = mr.detect_decay(mr.monthly_ic(dics), months=2)
        self.assertFalse(info['decaying'])

    def test_insufficient_months_conservative(self):
        dics = {'2026-01-05': 0.05, '2026-02-05': 0.01}
        info = mr.detect_decay(mr.monthly_ic(dics), months=2)
        self.assertFalse(info['decaying'], '月份不足时不得报衰减（防误报）')
        self.assertIn('月份不足', info['reason'])

    def test_three_month_setting(self):
        dics = {'2026-01-05': 0.09, '2026-02-05': 0.06,
                '2026-03-05': 0.03, '2026-04-05': 0.01}
        self.assertTrue(mr.detect_decay(mr.monthly_ic(dics), months=3)['decaying'])
        self.assertFalse(mr.detect_decay(mr.monthly_ic(dics), months=5)['decaying'])


class TestReadOnlyGuarantee(unittest.TestCase):

    def test_no_weight_write_in_source(self):
        """硬约束：复检脚本不得写权重文件"""
        import io
        src = io.open(os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            'scripts', 'monthly_review.py'), encoding='utf-8').read()
        self.assertNotIn('v1.json\', \'w\'', src)
        self.assertNotIn("open(WEIGHTS, 'w'", src)
        self.assertIn('只读', src)


if __name__ == '__main__':
    unittest.main()
