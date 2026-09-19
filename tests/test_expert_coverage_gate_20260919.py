# -*- coding: utf-8 -*-
"""专家五维覆盖率门控 + 已覆盖维重归一测试（2026-09-19 用户决策"完善专家五维"）

背景：生产日志 expert 恒 ≈48-50 vs model ≈74-75 恒冲突——5 维中常只有 technical
有数据（权重 0.25），其余 4 维缺失退化为中性 50，"失明专家"系统性压低高分票仓位。
修复：① coverage < 0.40 → 专家弃权（low_coverage，ensemble=model，仓位系数 1.0）
     ② expert_score 只聚合有数据维度（按权重重归一），缺失维不再稀释
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.expert_ensemble import (ExpertEnsemble, EXPERT_WEIGHTS,
                                  MIN_EXPERT_COVERAGE, confidence_to_weight_factor)


def make_stock(**kw):
    """构造指定维度有数据的 stock dict"""
    s = {'code': '600000'}
    s.update(kw)
    return s


class TestCoverageGate(unittest.TestCase):

    def setUp(self):
        self.ee = ExpertEnsemble()

    def test_zero_coverage_abstains(self):
        """全维无数据 → 弃权，ensemble=model，仓位系数 1.0"""
        v = self.ee.fuse({'code': '600000'}, model_score=74.0)
        self.assertTrue(v.abstained)
        self.assertEqual(v.confidence, 'low_coverage')
        self.assertAlmostEqual(v.ensemble_score, 74.0, places=2)
        self.assertAlmostEqual(v.coverage, 0.0, places=4)
        self.assertEqual(confidence_to_weight_factor(v.confidence), 1.00)

    def test_single_dim_below_gate(self):
        """仅 technical 有数据（权重 0.25 < 0.40）→ 仍弃权（生产最常见场景）"""
        v = self.ee.fuse(make_stock(rps=85, pct_chg=2.0, turnover=5.0), model_score=75.0)
        self.assertTrue(v.abstained,
                        '单维 0.25 < 门控 0.40，不应发声（这正是生产"恒冲突"的场景）')
        self.assertAlmostEqual(v.ensemble_score, 75.0, places=2)

    def test_two_dims_pass_gate_and_renormalize(self):
        """technical(0.25) + capital(0.20) 有数据 → coverage 0.45 ≥ 门控，重归一发声"""
        v = self.ee.fuse(make_stock(
            rps=85, pct_chg=2.0, turnover=5.0,          # technical: 50+12+8+5=75
            main_fund_accumulated=60_000_000,           # capital: 50+18=68
        ), model_score=75.0)
        self.assertFalse(v.abstained)
        self.assertAlmostEqual(v.coverage, 0.45, places=4)
        expected_expert = (75.0 * 0.25 + 68.0 * 0.20) / 0.45
        self.assertAlmostEqual(v.expert_score, expected_expert, places=1,
                               msg='expert 应只聚合已覆盖维（75/68），不含缺失维的 50')
        # expert ≈ 71.9 vs model 75 → Δ≈-3.1 → high
        self.assertEqual(v.confidence, 'high')

    def test_conflict_still_works_when_covered(self):
        """覆盖充足时，真实的观点分歧仍应触发 conflict 降分"""
        v = self.ee.fuse(make_stock(
            rps=10, pct_chg=8.0, turnover=20.0,         # technical: 50-10-8-12=20
            fundamentals={'roe': -5},                    # fundamental: 50-20=30
        ), model_score=90.0)
        self.assertFalse(v.abstained)
        self.assertAlmostEqual(v.coverage, 0.50, places=4)
        expected_expert = (20.0 * 0.25 + 30.0 * 0.25) / 0.50
        self.assertAlmostEqual(v.expert_score, expected_expert, places=1)  # = 25
        self.assertEqual(v.confidence, 'conflict')
        self.assertAlmostEqual(v.ensemble_score, 90 + 0.15 * (25 - 90) - 8.0, places=1)

    def test_full_coverage_equals_old_behavior(self):
        """全维覆盖时与旧公式完全一致（回归保护）：expert=Σraw×w，不重归一"""
        stock = make_stock(
            fundamentals={'roe': 20, 'revenue_growth': 20, 'profit_growth': 25},
            rps=85, pct_chg=2.0, turnover=5.0,
            main_fund_accumulated=60_000_000, north_flow_accumulated=3000,
            pe_percentile=15, pb_percentile=15,
            recent_events=[{'title': '中标公告', 'date': '2026-09-18'}],
        )
        v = self.ee.fuse(stock, model_score=50.0)
        self.assertAlmostEqual(v.coverage, 1.0, places=4)
        self.assertFalse(v.abstained)
        # 全覆盖时重归一 = 原加权平均
        manual = sum(v.expert_breakdown[n]['weighted'] for n in EXPERT_WEIGHTS)
        self.assertAlmostEqual(v.expert_score, manual, places=2)

    def test_breakdown_carries_covered_flag(self):
        v = self.ee.fuse(make_stock(rps=85), model_score=50.0)
        self.assertTrue(v.expert_breakdown['technical']['covered'])
        self.assertFalse(v.expert_breakdown['fundamental']['covered'])
        self.assertFalse(v.expert_breakdown['valuation']['covered'])

    def test_gate_constant_semantics(self):
        """门控 0.40 = 至少两个维度（单维最高权重 0.25，前两维之和 0.50 ≥ 门控）"""
        self.assertAlmostEqual(MIN_EXPERT_COVERAGE, 0.40)
        self.assertLess(max(EXPERT_WEIGHTS.values()), MIN_EXPERT_COVERAGE,
                        '任何单维都不应独自过门控')
        top2 = sum(sorted(EXPERT_WEIGHTS.values(), reverse=True)[:2])
        self.assertGreaterEqual(top2, MIN_EXPERT_COVERAGE,
                                '前两维组合应能过门控')

    def test_weight_factor_unchanged_for_legacy_states(self):
        """既有 confidence 档的仓位系数不受影响（回归保护）"""
        self.assertEqual(confidence_to_weight_factor('high'), 1.00)
        self.assertEqual(confidence_to_weight_factor('medium'), 0.85)
        self.assertEqual(confidence_to_weight_factor('low'), 0.70)
        self.assertEqual(confidence_to_weight_factor('conflict'), 0.50)


if __name__ == '__main__':
    unittest.main()
