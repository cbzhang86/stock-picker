# -*- coding: utf-8 -*-
"""权重边界强制（有界注水）的不变量回归锁（2026-09-21）

`_enforce_weight_bounds` 把权重压进 [min_single, 上限] 且保持 Σw == 1。
此前它只被 `test_weight_bounds_20260917.py` 锁了两点（上限不被突破、
不可行不抛异常），本文件补的是**守恒类不变量**与**覆盖率闸门 ↔ 边界强制
的执行顺序**（P2-12 覆盖面缺口）。

⚠️ 文档与实现不一致（已核实，2026-09-21）
------------------------------------------
本文件按**实际代码行为**写断言，并按此记录一个真实缺口：

`_enforce_weight_bounds` 的 docstring 第 341 行承诺"residual > 0 → 只分配给
**未触各自上限**的因子"，第 336-337 行进一步解释这是为了防止"注水把覆盖率
闸门刚压下去的额度又还回去（实测 capital_flow 被推到 7.5%，突破闸门 5%
上限）"。

但实现里 `room = {f: cap[f] - v for f, v in w.items() if cap[f] - v > tol}`
取的是**绝对**剩余空间，`w[f] += step * (r / total_room)` 按**比例**分配 ——
于是 `r` 较小的因子（例如已被闸门压到 0.05 的 capital_flow，r≈0）虽被排除，
而 `r` 较大的未触上限因子会被**过度加配**，可能突破自己的上限。

更直接地说：`room` 的比例权重没有排除"分配后会越界"的因子，只排除了"当前
就完全没空间"的因子。因此该函数的守恒不变量是：
  (1) Σw == 1（兜底归一化保证）
  (2) 最终 `feasible` 会如实反映"是否有因子越界"（第 380 行的 all() 检查）
但**不是** docstring 承诺的"残差只流向未触上限因子"。

后果：`ceilings` 目前**只能防止已被压低的因子继续降低**（通过第 355-356 行
的夹取），**不能**保证它不接收回流额度。这正是 P2-12 需要人工确认的点 ——
修 docstring 还是修实现，应由数据/理论判断，不是测试该决定的。
"""
import importlib.util
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

_spec = importlib.util.spec_from_file_location(
    'calibrate_weights', os.path.join(ROOT, 'scripts', 'calibrate_weights.py'))
cw = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(cw)


def _entry(score, reliability, ic_main, n_days=100, coverage=1.0):
    """构造满足 calibrate 最小字段需求的 consensus 条目"""
    return {
        'per_convention': {}, 'ic_main': ic_main, 'pos_ratio': 1.0,
        'consistency_factor': 1.0, 'overnight_vs_intraday_divergence': False,
        't_stat': 2.0, 'n_days': n_days, 'reliability': reliability,
        'score': score, 'coverage': coverage, 'mean_ic': ic_main,
        'n_conventions': 3, 'sign_stable': True,
    }


class TestWeightConservation(unittest.TestCase):
    """Σw == 1 与上下限是注水算法的硬不变量"""

    def test_sum_preserved_from_over_allocation(self):
        """Σ = 1.05（超额）→ 回收后仍为 1.0"""
        out, ok = cw._enforce_weight_bounds(
            {'a': 0.50, 'b': 0.50, 'c': 0.05}, max_single=0.50,
            min_single=0.0, ceilings={'b': 0.05})
        self.assertAlmostEqual(sum(out.values()), 1.0, places=9,
                               msg='守恒不变量：残差回收后 Σw 必须仍为 1')
        self.assertTrue(ok)

    def test_sum_preserved_from_under_allocation(self):
        """Σ = 0.60（不足）→ 补额后仍为 1.0"""
        out, ok = cw._enforce_weight_bounds(
            {'a': 0.50, 'b': 0.05, 'c': 0.05}, max_single=0.50,
            min_single=0.0, ceilings={'b': 0.05})
        self.assertAlmostEqual(sum(out.values()), 1.0, places=9,
                               msg='守恒不变量：残差补额后 Σw 必须仍为 1')
        self.assertTrue(ok)

    def test_clamp_never_exceeds_max_single(self):
        """夹取阶段是硬上限：任何最终值不得超过 max_single"""
        out, _ = cw._enforce_weight_bounds(
            {'a': 0.90, 'b': 0.08, 'c': 0.02},
            max_single=0.50, min_single=0.02)
        self.assertLessEqual(max(out.values()), 0.50 + 1e-9,
                             f'任何最终权重不得超过 max_single，实际 {out}')

    def test_min_single_lifted(self):
        """低于下限的因子必须被抬升到下限"""
        out, _ = cw._enforce_weight_bounds(
            {'a': 0.50, 'b': 0.50, 'c': 0.00},
            max_single=0.50, min_single=0.05)
        self.assertGreaterEqual(min(out.values()), 0.05 - 1e-9,
                                f'低于下限的因子应被抬升到下限，实际 {out}')
        self.assertAlmostEqual(sum(out.values()), 1.0, places=9)

    def test_already_feasible_is_untouched(self):
        """已满足全部约束的权重不应被扰动（幂等性）"""
        w = {'a': 0.40, 'b': 0.35, 'c': 0.25}
        out, ok = cw._enforce_weight_bounds(w, max_single=0.50,
                                            min_single=0.02)
        self.assertTrue(ok)
        for f, v in w.items():
            self.assertAlmostEqual(out[f], v, places=12,
                                   msg=f'{f} 已可行却被扰动')

    def test_empty_returns_empty_and_feasible(self):
        """空输入不抛异常，返回空字典 + feasible=True"""
        out, ok = cw._enforce_weight_bounds({}, max_single=0.50)
        self.assertEqual(out, {})
        self.assertTrue(ok)

    def test_infeasible_structure_does_not_raise_and_stays_normalised(self):
        """各因子上限之和 < 1 → 不抛异常，Σw 仍归一化为 1（但必然越界）

        ⚠️ 已知局限：此时返回值必然突破 max_single，而兜底归一化只看 Σw，
        所以 `feasible` 由第 380 行的 all() 判定为 False。本用例锁的是
        "不崩 + Σw 可归一"这两条，而非越界与否。
        """
        out, ok = cw._enforce_weight_bounds(
            {'a': 0.20, 'b': 0.20, 'c': 0.20},
            max_single=0.10, min_single=0.0)
        self.assertAlmostEqual(sum(out.values()), 1.0, places=6,
                               msg='不可行结构下兜底归一化仍须保证 Σw == 1')
        self.assertFalse(ok, '上限之和 < 1 属不可行，feasible 必须为 False')


class TestCeilingSemantics(unittest.TestCase):
    """ceilings 的实际语义 —— 按代码真实行为记录，不按 docstring 承诺"""

    def test_ceiling_clamps_factor_down(self):
        """被指定上限的因子会被夹取到该上限（这是 ceilings 确实起作用的场景）"""
        out, ok = cw._enforce_weight_bounds(
            {'a': 0.60, 'b': 0.50, 'c': 0.40}, max_single=0.50,
            min_single=0.0, ceilings={'c': 0.05})
        self.assertLessEqual(out['c'], 0.05 + 1e-9,
                             f'被闸门压低的因子不得反弹超过其上限，实际 {out}')
        self.assertAlmostEqual(sum(out.values()), 1.0, places=9)
        self.assertTrue(ok)

    def test_no_ceiling_allows_normal_water_filling(self):
        """不指定 ceilings 时按 max_single 正常注水（历史缺陷 09-17 的对照组）"""
        out, ok = cw._enforce_weight_bounds(
            {'a': 0.70, 'b': 0.30}, max_single=0.50, min_single=0.0)
        self.assertLessEqual(max(out.values()), 0.50 + 1e-9)
        self.assertAlmostEqual(sum(out.values()), 1.0, places=9)
        self.assertTrue(ok)


class TestCoverageGateOrdering(unittest.TestCase):
    """覆盖率闸门（COVERAGE_CAP）与边界强制的顺序：先压闸门，再统一注水

    calibrate 第 5 步命中闸门后写入 `ceilings`，第 7 步才调用
    `_enforce_weight_bounds(..., ceilings=ceilings)`。若顺序颠倒，闸门额度
    会在注水阶段被回流。
    """

    @staticmethod
    def _consensus(low_cov_key='B', low_cov=0.05):
        """B 低覆盖高分，A 高覆盖高分，C/D/E 噪声"""
        return {
            'A': _entry(score=1.0, reliability=0.8, ic_main=+0.06),
            'B': _entry(score=0.9, reliability=0.8, ic_main=+0.05,
                        coverage=low_cov),
            'C': _entry(score=0.0, reliability=0.0, ic_main=-0.01),
            'D': _entry(score=0.0, reliability=0.0, ic_main=-0.01),
            'E': _entry(score=0.0, reliability=0.0, ic_main=-0.01),
        }

    def test_gate_hits_below_threshold(self):
        w, detail = cw.calibrate(self._consensus(), {k: 0.2 for k in 'ABCDE'})
        self.assertIn('B', detail['coverage_gated'],
                      '覆盖率 5% 远低于阈值 60%，必须命中闸门')
        self.assertLessEqual(w['B'], cw.COVERAGE_CAP + 1e-9,
                             f'命中闸门的因子不得反弹超过 COVERAGE_CAP: {w}')
        self.assertAlmostEqual(sum(w.values()), 1.0, places=6)

    def test_gate_skipped_at_or_above_threshold(self):
        """覆盖率恰好等于 COVERAGE_GATE → 不命中（判定是严格小于）"""
        cons = self._consensus(low_cov=cw.COVERAGE_GATE)
        _, detail = cw.calibrate(cons, {k: 0.2 for k in 'ABCDE'})
        self.assertEqual(detail['coverage_gated'], [],
                         f'覆盖率 = {cw.COVERAGE_GATE} 恰好等于阈值，'
                         f'判定为 < 故不应命中，实际 {detail["coverage_gated"]}')

    def test_gate_skipped_when_coverage_unknown(self):
        """覆盖率缺失（None）→ 闸门跳过，不得误判为低覆盖"""
        cons = self._consensus()
        cons['B']['coverage'] = None
        _, detail = cw.calibrate(cons, {k: 0.2 for k in 'ABCDE'})
        self.assertEqual(detail['coverage_gated'], [],
                         '覆盖率未知不得当成低覆盖率压权重')

    def test_multiple_gated_factors_all_capped(self):
        """多个因子命中闸门 → 全部 ≤ COVERAGE_CAP，总额守恒"""
        cons = self._consensus()
        cons['C'] = _entry(score=0.5, reliability=0.6, ic_main=+0.04,
                           coverage=0.02)
        w, detail = cw.calibrate(cons, {k: 0.2 for k in 'ABCDE'})
        self.assertEqual(sorted(detail['coverage_gated']), ['B', 'C'])
        for f in ('B', 'C'):
            self.assertLessEqual(w[f], cw.COVERAGE_CAP + 1e-9,
                                 f'{f} 突破闸门上限: {w}')
        self.assertAlmostEqual(sum(w.values()), 1.0, places=6)

    def test_gate_capped_factor_not_over_written_on_apply(self):
        """闸门命中的因子即使被判为高可信，也不得被写入超过 cap 的权重

        回归锁 P2-12 的核心场景：闸门压权重是硬约束，不是"建议值"。
        第三个因子 C 用来提供注水的上行空间（否则 2 因子 + cap 0.05 +
        max 0.50 上限之和 < 1，会走不可行兜底分支而非闸门分支）。
        """
        cons = {
            'A': _entry(score=0.3, reliability=0.4, ic_main=+0.02),
            'B': _entry(score=1.0, reliability=1.0, ic_main=+0.09,
                        coverage=0.01),
            'C': _entry(score=0.7, reliability=0.7, ic_main=+0.04),
        }
        w, detail = cw.calibrate(cons, {'A': 0.4, 'B': 0.4, 'C': 0.2})
        self.assertIn('B', detail['coverage_gated'])
        self.assertLessEqual(w['B'], cw.COVERAGE_CAP + 1e-9,
                             f'最高可信因子仍受闸门约束，实际 B={w["B"]}')


if __name__ == '__main__':
    unittest.main(verbosity=2)
