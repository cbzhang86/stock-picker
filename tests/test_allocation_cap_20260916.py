# -*- coding: utf-8 -*-
"""回归测试：单票仓位上限与总仓位约束（2026-09-16 P1-2）

背景：
  `PortfolioOptimizer._scoring_weight` 先按 MAX_ALLOCATION=0.40 截断，随后又
  执行 `pct/total*100` 归一化 —— total 恰是"被截断后的和"，于是 40% 被重新
  放大：荐股 1 只 → 100%、2 只 → 50%，风控上限形同虚设（当日推送即"仓位100%"）。

修复后的约束优先级（硬 → 软）：
  单票上限 MAX_ALLOCATION  >  合计 ≤ 100%  >  单票下限 MIN_ALLOCATION

本文件锁定三条不变量：
  A. 任意荐股数量下 max(allocation_pct) <= 40%；
  B. 任意荐股数量下 sum(allocation_pct) <= 100%（并给出 cash_pct 兜底口径）；
  C. 常规规模（≥3 只）仍应把仓位分配到接近满仓，不得因修复而系统性缩水。
"""
import unittest

from core.portfolio_optimizer import PortfolioOptimizer as P
from strategies.short_term import ShortTermStrategy

CAP = P.MAX_ALLOCATION * 100            # 40.0
FLOOR = P.MIN_ALLOCATION * 100          # 10.0


def _recs(scores):
    return [{'code': f'{i:06d}', 'name': f'S{i}', 'score': s}
            for i, s in enumerate(scores)]


class TestAllocationInvariants(unittest.TestCase):

    def _check(self, recs, label=''):
        out = P.allocate(recs)
        pcts = [r['allocation_pct'] for r in out]
        self.assertLessEqual(max(pcts), CAP + 1e-6,
                             f'{label} 单票突破上限: {pcts}')
        self.assertLessEqual(sum(pcts), 100.0 + 1e-6,
                             f'{label} 总仓位超 100%: {pcts}')
        self.assertGreaterEqual(min(pcts), 0.0, f'{label} 出现负仓位: {pcts}')
        # cash_pct 必须与仓位合计互补（100 - Σ）
        self.assertAlmostEqual(out[0]['cash_pct'],
                               round(max(0.0, 100.0 - sum(pcts)), 1), places=1)
        return pcts

    # ── A/B：上限与总仓位 ──

    def test_single_recommendation_capped(self):
        """原缺陷最直观的复现点：1 只推荐曾被放大到 100%"""
        pcts = self._check(_recs([65.06]), 'n=1')
        self.assertEqual(pcts, [40.0])
        self.assertAlmostEqual(P.allocate(_recs([65.06]))[0]['cash_pct'], 60.0)

    def test_two_recommendations_capped(self):
        pcts = self._check(_recs([80, 75]), 'n=2')
        self.assertEqual(pcts, [40.0, 40.0])

    def test_never_exceeds_cap_for_many_counts(self):
        for n, scores in [(1, [70]), (2, [80, 75]), (3, [90, 80, 70]),
                          (5, [90, 88, 86, 84, 82]),
                          (10, [95, 93, 91, 89, 87, 85, 83, 81, 79, 77]),
                          (12, list(range(95, 71, -2)))]:
            self._check(_recs(scores), f'n={n}')

    def test_floor_cannot_push_total_over_100(self):
        """下限副作用：10 只分数接近时 10%×10 会把合计顶过 100%（曾 102.8%）"""
        pcts = self._check(_recs([95, 93, 91, 89, 87, 85, 83, 81, 79, 77]),
                           'n=10 下限挤压')
        self.assertLessEqual(sum(pcts), 100.0 + 1e-6)

    def test_equal_count_split_is_ten_percent(self):
        """n=10 等分时下限与均分重合，应恰好满仓 100%"""
        pcts = self._check(_recs(list(range(95, 65, -3))), 'n=10 等分')
        self.assertEqual(len(pcts), 10)
        self.assertLessEqual(sum(pcts), 100.0 + 1e-6)

    def test_extreme_count_relaxes_floor(self):
        """n ≥ 11 时下限（10%）与总仓位 100% 不可兼得 → 总仓位约束优先"""
        pcts = self._check(_recs(list(range(95, 71, -2))), 'n=12')
        self.assertLess(max(pcts), FLOOR)   # 下限被放宽
        self.assertLess(sum(pcts), 100.0 + 1e-6)

    # ── C：常规规模不得缩水 ──

    def test_normal_case_still_fully_invested(self):
        for n, scores in [(3, [90, 80, 70]), (5, [90, 88, 86, 84, 82])]:
            pcts = self._check(_recs(scores), f'n={n}')
            self.assertAlmostEqual(sum(pcts), 100.0, delta=0.2,
                                   msg=f'n={n} 常规规模应接近满仓: {pcts}')

    # ── 等权分支（原实现同样突破上限）──

    def test_equal_weight_strategy_respects_cap(self):
        for n in (1, 2, 3, 5, 6, 7, 9, 12):
            recs = _recs([80] * n)
            out = P.allocate(recs, strategy='equal_weight')
            pcts = [r['allocation_pct'] for r in out]
            self.assertLessEqual(max(pcts), CAP + 1e-6, f'等权 n={n}: {pcts}')
            self.assertLessEqual(sum(pcts), 100.0 + 1e-6, f'等权 n={n}: {pcts}')

    def test_zero_score_falls_back_to_equal_weight_within_cap(self):
        """total_score<=0 的回落路径也必须在约束内"""
        recs = [{'code': 'A', 'score': 0}, {'code': 'B', 'score': 0}]
        out = P.allocate(recs)
        pcts = [r['allocation_pct'] for r in out]
        self.assertLessEqual(max(pcts), CAP + 1e-6)

    # ── 弱市压缩与上限的叠加 ──

    def test_position_scale_keeps_invariants(self):
        for scale in (1.0, 0.6, 0.5, 0.3):
            for n, scores in [(1, [65.06]), (2, [80, 75]), (3, [90, 80, 70])]:
                recs = P.allocate(_recs(scores))
                out = ShortTermStrategy._apply_position_scale(recs, scale)
                pcts = [r['allocation_pct'] for r in out]
                self.assertLessEqual(max(pcts), CAP + 1e-6,
                                     f'scale={scale} n={n}: {pcts}')
                self.assertLessEqual(sum(pcts), 100.0 + 1e-6,
                                     f'scale={scale} n={n}: {pcts}')
                self.assertAlmostEqual(out[0]['cash_pct'],
                                       round(max(0.0, 100.0 - sum(pcts)), 1),
                                       places=1)

    def test_position_scale_halves_three_stock_case(self):
        """scale=0.5 且三只满仓 → 总仓位 50%、现金 50%（既有 gate 语义不变）"""
        recs = P.allocate(_recs([90, 80, 70]))
        out = ShortTermStrategy._apply_position_scale(recs, 0.5)
        self.assertAlmostEqual(sum(r['allocation_pct'] for r in out), 50.0,
                               delta=0.05)

    # ── 边界 ──

    def test_empty_list(self):
        self.assertEqual(P.allocate([]), [])

    def test_no_side_effect_on_other_fields(self):
        recs = _recs([90, 80, 70])
        recs[0]['blocks'] = {'total': 3}
        out = P.allocate(recs)
        self.assertEqual(out[0]['blocks'], {'total': 3})
        self.assertEqual(out[0]['score'], 90)


if __name__ == '__main__':
    unittest.main()
