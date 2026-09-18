# -*- coding: utf-8 -*-
"""权重校准层方法论缺陷修复 — 回归测试（2026-09-17）

覆盖：
  T1  apply_correlation_discount 折扣后重新归一（Σscore 守恒不变式）
  T2  MIN_SINGLE_WEIGHT 权重下限真正接入 calibrate
  T3  pos_ratio 从乘法证据降级为一致性档位 + overnight/intraday 分歧标记

⚠️ 现实约束：当前 OOS 报告里没有 factor_corr 字段（另一 worker 正在补落盘），
所以本文件所有涉及 factor_corr 的测试都自行 mock/构造数据，不依赖真实报告。
"""
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from scripts.calibrate_weights import (          # noqa: E402
    apply_correlation_discount,
    calibrate,
    consensus_evidence,
    MIN_SINGLE_WEIGHT,
    MAX_SINGLE_WEIGHT,
)


def _make_consensus(scores: dict) -> dict:
    """构造最小可用 consensus（apply_correlation_discount 只读 score 字段）"""
    return {f: {'score': float(s)} for f, s in scores.items()}


# ─────────────────────────────────────────────────────────────────────────────
# T1：apply_correlation_discount 折扣后重新归一
# ─────────────────────────────────────────────────────────────────────────────
class TestCorrelationDiscount(unittest.TestCase):

    def test_sum_score_preserved_after_discount(self):
        """折扣前后 Σscore 必须相等（不变量，容差 1e-9）"""
        scores = {'A': 1.0, 'B': 2.0, 'C': 3.0, 'D': 4.0}
        consensus = _make_consensus(scores)
        # A 与另外三个高相关；B/C/D 相关性低
        factor_corr = {
            'A': {'B': 0.8, 'C': 0.8, 'D': 0.8},
            'B': {'A': 0.2, 'C': 0.1, 'D': 0.15},
            'C': {'A': 0.1, 'B': 0.1, 'D': 0.2},
            'D': {'A': 0.15, 'B': 0.1, 'C': 0.1},
        }
        report = {'factor_corr': factor_corr}
        before = sum(d['score'] for d in consensus.values())
        n_adj = apply_correlation_discount(consensus, report, threshold=0.5)
        after = sum(d['score'] for d in consensus.values())
        self.assertEqual(n_adj, 1)  # 只有 A 超阈值
        self.assertAlmostEqual(after, before, delta=1e-9)

    def test_only_high_corr_factors_discounted(self):
        """仅 avg|corr| > threshold(0.5) 的因子被折扣（出现 corr_discount 且 <1）"""
        scores = {'A': 1.0, 'B': 2.0, 'C': 3.0, 'D': 4.0}
        consensus = _make_consensus(scores)
        factor_corr = {
            'A': {'B': 0.8, 'C': 0.8, 'D': 0.8},
            'B': {'A': 0.2, 'C': 0.1, 'D': 0.15},
            'C': {'A': 0.1, 'B': 0.1, 'D': 0.2},
            'D': {'A': 0.15, 'B': 0.1, 'C': 0.1},
        }
        apply_correlation_discount(consensus, {'factor_corr': factor_corr}, threshold=0.5)
        self.assertIn('corr_discount', consensus['A'])
        self.assertLess(consensus['A']['corr_discount'], 1.0)
        # B/C/D 未超阈值 → 不应有 corr_discount 键
        for f in ('B', 'C', 'D'):
            self.assertNotIn('corr_discount', consensus[f])

    def test_no_factor_corr_returns_zero_no_raise(self):
        """主口径报告无 factor_corr 时返回 0 且不抛异常（保持既有降级行为）"""
        consensus = _make_consensus({'A': 1.0, 'B': 2.0})
        # 完全没有 factor_corr 键
        self.assertEqual(apply_correlation_discount(consensus, {'result': {}}), 0)
        # factor_corr 为空 dict
        self.assertEqual(apply_correlation_discount(consensus, {'factor_corr': {}}), 0)
        # primary_ic_data 为 None
        self.assertEqual(apply_correlation_discount(consensus, None), 0)


# ─────────────────────────────────────────────────────────────────────────────
# T2：MIN_SINGLE_WEIGHT 权重下限
# ─────────────────────────────────────────────────────────────────────────────
class TestMinSingleWeight(unittest.TestCase):

    def _build_universe(self, n_factors, weak_factor, weak_score):
        """构造 n_factors 个因子的 consensus + current_weights。

        weak_factor 得分极低（并经负 IC 惩罚进一步压低）。当 n_factors 足够大时，
        等权先验底座 (1-λ)/n 会降到 min_single 以下，从而让下限真正 binding，
        用于验证下限逻辑被接入。
        """
        consensus = {}
        current = {}
        for i in range(n_factors):
            f = weak_factor if i == 0 else f'factor_{i}'
            score = weak_score if f == weak_factor else 0.5 + i * 0.1
            ic_main = -0.05 if f == weak_factor else 0.06 + i * 0.005
            consensus[f] = {
                'score': float(score),
                'ic_main': ic_main,
                'reliability': 1.0,
                'n_days': 90,
                'coverage': 0.9,
                't_stat': 2.5,
            }
            current[f] = 1.0 / n_factors
        return consensus, current

    def test_floor_lifts_weak_factor_and_sum_stays_one(self):
        """得分极低的因子权重被抬到 >= min_single，且 Σw == 1（内部精确守恒）"""
        # 15 个因子 → 等权先验底座 (1-0.8)/15≈1.33% < 2% → 下限 binding
        consensus, current = self._build_universe(15, 'factor_0', 0.0)
        weights, detail = calibrate(consensus, current, min_single=MIN_SINGLE_WEIGHT)
        self.assertTrue(len(weights) > 0)
        # 内部 Σw 精确守恒（floor 只做保和再分配）；返回权重四舍五入 4 位，
        # 肉眼级 Σ 偏差不超过 4 位小数的累积误差预算。
        total = sum(weights.values())
        self.assertAlmostEqual(total, 1.0, delta=1e-3)
        self.assertGreaterEqual(weights['factor_0'], MIN_SINGLE_WEIGHT - 1e-9)
        # 下限生效 → detail 记录 lifted_factors 含该因子
        if detail.get('min_single_applied'):
            self.assertIn('factor_0', detail.get('lifted_factors', []))
        # 上限不得被违反
        for w in weights.values():
            self.assertLessEqual(w, MAX_SINGLE_WEIGHT + 1e-9)

    def test_floor_conflict_skips_with_warning(self):
        """下限与上限冲突（因子数×min_single > 1）时跳过下限且不抛异常"""
        factors = ['f' + str(i) for i in range(7)]
        consensus = {}
        current = {}
        for f in factors:
            consensus[f] = {'score': 0.5, 'ic_main': 0.06, 'reliability': 1.0,
                             'n_days': 90, 'coverage': 0.9, 't_stat': 2.5}
            current[f] = 1.0 / len(factors)
        # min_single = 0.20 → 7×0.20 = 1.4 > 1，必然冲突
        weights, detail = calibrate(consensus, current, min_single=0.20)
        total = sum(weights.values())
        self.assertAlmostEqual(total, 1.0, delta=1e-3)
        self.assertFalse(detail.get('min_single_applied', False))
        for w in weights.values():
            self.assertLessEqual(w, MAX_SINGLE_WEIGHT + 1e-9)


# ─────────────────────────────────────────────────────────────────────────────
# T3：pos_ratio 降级为一致性档位
# ─────────────────────────────────────────────────────────────────────────────
class TestPosRatioConsistency(unittest.TestCase):

    def _stats(self, t=2.5, n=90):
        return {'hold1d': {'F': {'t_stat': t, 'n_days': n, 'coverage': 0.9}},
                'intraday': {'F': {'t_stat': t, 'n_days': n, 'coverage': 0.9}},
                'overnight': {'F': {'t_stat': t, 'n_days': n, 'coverage': 0.9}}}

    def test_all_positive_consistency_factor_one(self):
        """三口径全正 → consistency_factor == 1.0"""
        ev = {'hold1d': {'F': 0.05}, 'intraday': {'F': 0.03}, 'overnight': {'F': 0.04}}
        out = consensus_evidence(ev, self._stats(), primary='hold1d')
        self.assertEqual(out['F']['consistency_factor'], 1.0)
        self.assertIn('pos_ratio', out['F'])  # 字段保留
        self.assertFalse(out['F']['overnight_vs_intraday_divergence'])

    def test_negative_convention_consistency_factor_point_seven(self):
        """存在负口径 → consistency_factor == 0.7"""
        ev = {'hold1d': {'F': 0.05}, 'intraday': {'F': 0.03}, 'overnight': {'F': -0.04}}
        out = consensus_evidence(ev, self._stats(), primary='hold1d')
        self.assertEqual(out['F']['consistency_factor'], 0.7)

    def test_overnight_intraday_divergence_flag(self):
        """overnight 与 intraday 符号相反 → overnight_vs_intraday_divergence == True"""
        # overnight 正、intraday 负（如 hot_theme 实测：隔夜 +0.0848 / 日内 -0.0149）
        ev = {'hold1d': {'F': 0.05}, 'intraday': {'F': -0.0149}, 'overnight': {'F': 0.0848}}
        out = consensus_evidence(ev, self._stats(), primary='hold1d')
        self.assertTrue(out['F']['overnight_vs_intraday_divergence'])

    def test_score_no_longer_ic_times_pos_ratio(self):
        """score 不再等于 ic_main * pos_ratio * reliability（证明改动生效）"""
        # 构造 pos_ratio != consistency_factor 的场景：2/3 为正 → pos_ratio=0.667
        ev = {'hold1d': {'F': 0.05}, 'intraday': {'F': 0.03}, 'overnight': {'F': -0.04}}
        out = consensus_evidence(ev, self._stats(), primary='hold1d')
        d = out['F']
        old_score = d['ic_main'] * d['pos_ratio'] * d['reliability']
        new_score = d['ic_main'] * d['consistency_factor'] * d['reliability']
        self.assertNotEqual(d['score'], old_score)   # 旧公式已失效
        self.assertAlmostEqual(d['score'], new_score, delta=1e-9)  # 新公式生效


if __name__ == '__main__':
    unittest.main()
