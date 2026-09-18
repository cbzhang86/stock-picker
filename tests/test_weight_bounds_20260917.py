# -*- coding: utf-8 -*-
"""回归测试：权重边界约束必须同时成立（2026-09-17）

背景（真实缺陷，由独立复核发现）
--------------------------------
`calibrate` 的步骤 4/5 各自做局部再分配，**后一步会推翻前一步的约束**：

    步骤 3  收缩+负惩罚+归一后      hot_theme 0.4052 / capital_flow 0.3770
    步骤 4  上限裁剪                无因子 > 0.50 → 未触发
    步骤 5  覆盖率闸门              capital_flow（覆盖率 9.4%）被压到 0.05，
                                    回流额度 0.3270 **按比例回流给所有未命中
                                    闸门的因子 —— 包括 hot_theme**
    → hot_theme 0.4052 + 0.2127 = **0.6179，突破 MAX_SINGLE_WEIGHT = 0.50**

后果：`MAX_SINGLE_WEIGHT` 号称"坍缩保护"，却在闸门命中时静默失效；
若执行 `--apply` 就会把越界权重写进 v1.json。

修复：新增 `_enforce_weight_bounds`（有界注水，支持逐因子上限），
在 `calibrate` 末尾统一保证 `min_single <= w <= ceilings` 且 `Σw == 1`。
**`ceilings` 必须包含覆盖率闸门的上限**，否则注水会把闸门刚压下去的额度
按"还有空间"又还回去（实测 capital_flow 被推回 7.5%）。

本文件锁定四条不变量（缺一不可）：
  A. max(w) <= MAX_SINGLE_WEIGHT
  B. 命中覆盖率闸门的因子 <= COVERAGE_CAP
  C. Σw == 1
  D. min(w) >= MIN_SINGLE_WEIGHT（不可行时允许跳过，但不得抛异常）
"""
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import importlib.util  # noqa: E402

# scripts/ 不是包，用 spec 加载
_spec = importlib.util.spec_from_file_location(
    'calibrate_weights', os.path.join(ROOT, 'scripts', 'calibrate_weights.py'))
cw = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(cw)


def _entry(score, reliability, ic_main, n_days=100, coverage=1.0):
    """构造一个满足 calibrate 最小字段需求的 consensus 条目"""
    return {
        'per_convention': {}, 'ic_main': ic_main, 'pos_ratio': 1.0,
        'consistency_factor': 1.0, 'overnight_vs_intraday_divergence': False,
        't_stat': 2.0, 'n_days': n_days, 'reliability': reliability,
        'score': score, 'coverage': coverage, 'mean_ic': ic_main,
        'n_conventions': 3, 'sign_stable': True,
    }


class TestEnforceWeightBounds(unittest.TestCase):
    """`_enforce_weight_bounds` 的直接单测"""

    def test_clamps_above_cap_and_keeps_sum(self):
        w = {'a': 0.90, 'b': 0.05, 'c': 0.05}
        out, ok = cw._enforce_weight_bounds(w, max_single=0.50, min_single=0.02)
        self.assertTrue(ok)
        self.assertLessEqual(max(out.values()), 0.50 + 1e-9)
        self.assertAlmostEqual(sum(out.values()), 1.0, places=9)

    def test_respects_per_factor_ceilings(self):
        # b 走"更严的逐因子上限"（模拟覆盖率闸门 5%）
        w = {'a': 0.70, 'b': 0.20, 'c': 0.10}
        out, ok = cw._enforce_weight_bounds(
            w, max_single=0.50, min_single=0.02, ceilings={'b': 0.05})
        self.assertTrue(ok)
        self.assertLessEqual(out['b'], 0.05 + 1e-9)
        self.assertLessEqual(max(out.values()), 0.50 + 1e-9)
        self.assertAlmostEqual(sum(out.values()), 1.0, places=9)

    def test_clamps_below_floor(self):
        w = {'a': 0.98, 'b': 0.02, 'c': 0.0}
        out, ok = cw._enforce_weight_bounds(w, max_single=0.50, min_single=0.10)
        self.assertTrue(ok)
        self.assertGreaterEqual(min(out.values()), 0.10 - 1e-9)
        self.assertAlmostEqual(sum(out.values()), 1.0, places=9)

    def test_infeasible_returns_flag_not_exception(self):
        # 5 个因子、上限 0.10 → 各上限之和 0.5 < 1，不可行
        w = {k: 0.2 for k in 'abcde'}
        out, ok = cw._enforce_weight_bounds(w, max_single=0.10, min_single=0.02)
        self.assertFalse(ok)                                   # 明确告警而非抛异常
        self.assertAlmostEqual(sum(out.values()), 1.0, places=6)  # 兜底仍保 Σ=1


class TestCalibrateBoundsInvariants(unittest.TestCase):
    """`calibrate` 全链路：覆盖率闸门命中时四条不变量必须同时成立"""

    @staticmethod
    def _consensus():
        # A：证据最强 → 收缩后权重最高（模拟 hot_theme）
        # B：证据次强但覆盖率 5% → 命中覆盖率闸门（模拟 capital_flow）
        # C/D/E：无正证据 → 只拿先验与负惩罚
        return {
            'A': _entry(score=1.0, reliability=0.8, ic_main=+0.06),
            'B': _entry(score=0.9, reliability=0.8, ic_main=+0.05, coverage=0.05),
            'C': _entry(score=0.0, reliability=0.0, ic_main=-0.01),
            'D': _entry(score=0.0, reliability=0.0, ic_main=-0.01),
            'E': _entry(score=0.0, reliability=0.0, ic_main=-0.01),
        }

    def test_gate_does_not_break_max_single_cap(self):
        w, detail = cw.calibrate(self._consensus(),
                                 {k: 0.2 for k in 'ABCDE'})
        self.assertTrue(w, '不应返回空权重')
        self.assertLessEqual(max(w.values()), cw.MAX_SINGLE_WEIGHT + 1e-9,
                             f'单因子上限被突破: {w}')

    def test_gated_factor_respects_coverage_cap(self):
        w, detail = cw.calibrate(self._consensus(),
                                 {k: 0.2 for k in 'ABCDE'})
        self.assertLessEqual(w['B'], cw.COVERAGE_CAP + 1e-9,
                             f'覆盖率闸门上限被突破: B={w["B"]}')

    def test_sum_is_one(self):
        w, _ = cw.calibrate(self._consensus(), {k: 0.2 for k in 'ABCDE'})
        self.assertAlmostEqual(sum(w.values()), 1.0, places=6)

    def test_floor_holds_when_feasible(self):
        w, _ = cw.calibrate(self._consensus(), {k: 0.2 for k in 'ABCDE'})
        self.assertGreaterEqual(min(w.values()), cw.MIN_SINGLE_WEIGHT - 1e-9)

    def test_bounds_enforced_flag_reported(self):
        w, detail = cw.calibrate(self._consensus(), {k: 0.2 for k in 'ABCDE'})
        self.assertIn('bounds_enforced', detail)
        self.assertTrue(detail['bounds_enforced'])
        self.assertIn('bounds_adjusted', detail)

    def test_no_gate_scenario_leaves_bounds_intact(self):
        """无闸门命中时，边界强制应为近无操作（不改变已满足约束的结果）"""
        cons = self._consensus()
        cons['B']['coverage'] = 1.0          # 覆盖率达标 → 闸门不命中
        w, detail = cw.calibrate(cons, {k: 0.2 for k in 'ABCDE'})
        self.assertLessEqual(max(w.values()), cw.MAX_SINGLE_WEIGHT + 1e-9)
        self.assertAlmostEqual(sum(w.values()), 1.0, places=6)


if __name__ == '__main__':
    unittest.main(verbosity=2)
