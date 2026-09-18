# -*- coding: utf-8 -*-
"""校准器新增能力测试（2026-09-18 审查 P1-5 / P2-1 修复）

P1-5：新因子（OOS 验证为正但未进 v1.json）此前永远拿不到权重提案 → 闭环断点。
P2-1：momentum 与 reversal_20d 完全共线（reversal = 100 − momentum），
      若当独立因子分配会重复计数 → 组内只保留证据更强者。
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from scripts.calibrate_weights import calibrate  # noqa: E402


def _cons(ic_main, rel=1.0):
    """构造 consensus 条目（字段与 consensus_evidence() 产出对齐：

    calibrate() 会读取 score / reliability / ic_main / n_days / coverage / t_stat，
    缺失键会 KeyError，故此 helper 一次性给全。
    """
    return {
        'ic_main': ic_main,
        'score': max(0.0, ic_main) * rel,     # 与生产逻辑同构：正 IC × 可靠性
        'reliability': rel,
        't_stat': 3.0 if ic_main > 0 else -3.0,
        'n_days': 200,
        'coverage': 0.9,
    }


class TestCalibrateCandidates(unittest.TestCase):

    def test_default_ignores_unknown_factors(self):
        """默认行为（历史可比）：只处理 v1.json 中已有因子"""
        current = {'hot_theme': 0.6, 'momentum': 0.4}
        cons = {'hot_theme': _cons(0.03), 'size': _cons(0.02)}
        w, _ = calibrate(cons, current, collinear_groups=[])
        self.assertIn('hot_theme', w)
        self.assertNotIn('size', w, '默认不应把未生效因子纳入')

    def test_include_candidates_adds_new_factor(self):
        """include_candidates=True → 新因子（OOS 正）可获权重提案"""
        current = {'hot_theme': 0.6, 'momentum': 0.4}
        cons = {'hot_theme': _cons(0.03), 'size': _cons(0.02)}
        w, _ = calibrate(cons, current, include_candidates=True, collinear_groups=[])
        self.assertIn('size', w, '新因子应进入提案（P1-5 修复）')
        self.assertGreater(w['size'], 0)

    def test_negative_candidate_excluded(self):
        """只有 IC 为正的新因子才进候选池"""
        current = {'hot_theme': 0.6, 'momentum': 0.4}
        cons = {'hot_theme': _cons(0.03), 'junk': _cons(-0.02)}
        w, _ = calibrate(cons, current, include_candidates=True, collinear_groups=[])
        self.assertNotIn('junk', w, '负 IC 因子不应作为候选进入')


class TestCollinearGuard(unittest.TestCase):

    def test_collinear_group_keeps_stronger_member(self):
        current = {'hot_theme': 0.3, 'momentum': 0.1, 'reversal_20d': 0.6}
        cons = {'hot_theme': _cons(0.03),
                'momentum': _cons(-0.03),
                'reversal_20d': _cons(0.03)}
        w, detail = calibrate(cons, current,
                              collinear_groups=[('momentum', 'reversal_20d')])
        self.assertIn('reversal_20d', w)
        self.assertEqual(w.get('momentum', 0.0), 0.0,
                         '共线组内弱成员（momentum）不应获得 IC 分配（P2-1 修复）')
        self.assertIn('momentum', detail.get('collinear_skipped', {}),
                      '应在明细中标注被跳过的共线成员')

    def test_group_with_single_member_noop(self):
        """组内只有一个成员时不产生副作用"""
        current = {'reversal_20d': 1.0}
        cons = {'reversal_20d': _cons(0.03)}
        w, _ = calibrate(cons, current, collinear_groups=[('momentum', 'reversal_20d')])
        self.assertIn('reversal_20d', w)


if __name__ == '__main__':
    unittest.main()
