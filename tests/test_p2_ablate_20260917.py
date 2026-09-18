"""
P2-5 验证：run_backtest.py 新增 --ablate 因子消融。

给加权因子做 drop-one：被消融因子权重置 0，剩余权重重新归一化到和为 1，
通过 config['weights'] 注入策略（ShortTermStrategy 读取 config.get('weights')
→ ScoringModel(weights=...)）。不修改 data/weights/v1.json。

验收：消融后权重和为 1、被消融因子不参与加权、未消融因子的相对比例不变。
（ScoringModel 仅用作"扁平权重 dict 能被接受"的确认 —— 不修改 scoring_model。）
"""
import os
import sys
import unittest

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from scripts.run_backtest import apply_ablation, load_weights_from_v1  # noqa: E402
from core.scoring_model import ScoringModel  # noqa: E402


class TestAblation(unittest.TestCase):

    def test_apply_ablation_sum_is_one(self):
        w = {'a': 0.5, 'b': 0.3, 'c': 0.2}
        out = apply_ablation(w, ['b'])
        self.assertAlmostEqual(sum(out.values()), 1.0, places=9)

    def test_ablated_factor_excluded(self):
        w = {'a': 0.5, 'b': 0.3, 'c': 0.2}
        out = apply_ablation(w, ['b'])
        self.assertNotIn('b', out)
        self.assertIn('a', out)
        self.assertIn('c', out)

    def test_relative_proportion_preserved(self):
        # 未消融因子的相对比例不变（核心验收）
        w = {'a': 0.5, 'b': 0.3, 'c': 0.2}
        out = apply_ablation(w, ['b'])
        old_ratio = w['a'] / w['c']
        new_ratio = out['a'] / out['c']
        self.assertAlmostEqual(old_ratio, new_ratio, places=9)

    def test_does_not_mutate_input(self):
        w = {'a': 0.5, 'b': 0.3, 'c': 0.2}
        apply_ablation(w, ['b'])
        self.assertEqual(w, {'a': 0.5, 'b': 0.3, 'c': 0.2},
                         "apply_ablation 不应修改入参")

    def test_ablate_factor_not_present_returns_unchanged(self):
        w = {'a': 0.5, 'b': 0.5}
        out = apply_ablation(w, ['zzz'])
        self.assertEqual(out, w)

    def test_ablate_all_returns_original(self):
        # 全部被消融 → 无法归一，返回原 dict（避免除零）
        w = {'a': 0.5, 'b': 0.5}
        out = apply_ablation(w, ['a', 'b'])
        self.assertEqual(out, w)

    def test_multiple_factors_via_comma(self):
        # --ablate hot_theme,momentum 形态的入参（列表内逗号分隔）
        w = {'a': 0.4, 'b': 0.3, 'c': 0.2, 'd': 0.1}
        factors = []
        for a in ['b,c']:
            factors.extend(x.strip() for x in a.split(',') if x.strip())
        out = apply_ablation(w, factors)
        self.assertAlmostEqual(sum(out.values()), 1.0, places=9)
        self.assertNotIn('b', out)
        self.assertNotIn('c', out)
        # a:d 比例保持 0.4:0.1
        self.assertAlmostEqual(out['a'] / out['d'], w['a'] / w['d'], places=9)

    def test_load_v1_and_ablate_hot_theme(self):
        # 读取真实 v1.json（只读，不修改），消融 hot_theme 后权重和=1
        w = load_weights_from_v1('short')
        self.assertTrue(w, "应能读取 data/weights/v1.json 的 short 权重")
        self.assertIn('hot_theme', w)
        out = apply_ablation(w, ['hot_theme'])
        self.assertAlmostEqual(sum(out.values()), 1.0, places=9)
        self.assertNotIn('hot_theme', out)
        # capital_flow:technical 相对比例不变（容差 1e-8：归一化除法的
        # 浮点误差在不同权重组合下可达 ~2e-9，places=9 的 0.5e-9 过紧）
        self.assertLess(
            abs(out['capital_flow'] / out['technical']
                - w['capital_flow'] / w['technical']), 1e-8)

    def test_ablated_weights_dict_is_injection_ready(self):
        # run_backtest 注入的是这个扁平 dict（config['weights']）；
        # 结构正确（扁平 / 和为 1 / 不含被消融因子）→ 可被策略读取
        w = load_weights_from_v1('short')
        ablated = apply_ablation(w, ['hot_theme'])
        self.assertIsInstance(ablated, dict)
        self.assertAlmostEqual(sum(ablated.values()), 1.0, places=9)
        self.assertNotIn('hot_theme', ablated)

    def test_scoring_model_ignores_injected_weights_when_v1_present(self):
        """已知限制（顺带 bug，非本任务范围内可修）：

        ScoringModel(weights=...) 在 v1.json 存在时会忽略 weights 参数
        （__init__ 先 _load_weights('v1') 成功即 self.weights = loaded，
        weights 仅在 v1 缺失时生效）。因此 run_backtest 把消融权重写进
        config['weights'] 后，ShortTermStrategy 仍走 v1.json → 消融在
        **真实回测运行**中不会生效（权重数学本身正确，见上方测试）。

        锁定该行为，便于后续在 scoring_model.py 修复（尊重 weights 参数）
        后回归。修复需在 core/scoring_model.py（本任务不允许改动）。
        """
        w = load_weights_from_v1('short')
        ablated = apply_ablation(w, ['hot_theme'])
        sm = ScoringModel(weights=ablated)
        # 当前实现：返回的是 v1.json 原权重，而非注入的 ablated
        self.assertNotEqual(sm.get_weights('short'), ablated,
                            "已知限制：v1.json 存在时 ScoringModel 忽略注入权重")
        self.assertEqual(sm.get_weights('short'), w,
                         "已知限制：实际仍返回 v1.json 原权重")


if __name__ == '__main__':
    unittest.main()
