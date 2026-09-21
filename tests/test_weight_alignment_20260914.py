"""
权重三层一致性 — 回归测试（2026-09-14 整体审查 P1-1）

背景：ScoringModel 权重加载优先级为 `v1.json > config.yml > DEFAULT_WEIGHTS`。
2026-09-06 的审查修复只把 DEFAULT_WEIGHTS 与 v1.json 对齐，**漏了中间层 config.yml**
（当时仍是"资金权重时代"的 capital_flow 0.3036 / technical 0.0619），
一旦 v1.json 丢失就会静默回落到旧参数，而动态门槛（强市65/中性70/弱市75）
是按 v1 的评分尺度定的 → 评分尺度与门槛错配、选股行为静默剧变。

本测试锁死三层一致性，防止再次漂移。全部离线可跑。

运行：
  python -m unittest tests.test_weight_alignment_20260914 -v
"""

import json
import logging
import os
import sys
import unittest

import yaml

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)

from core.scoring_model import ScoringModel


class TestWeightLayerAlignment(unittest.TestCase):
    """config.yml / v1.json / DEFAULT_WEIGHTS 三层必须等价"""

    @classmethod
    def setUpClass(cls):
        with open(os.path.join(PROJECT_ROOT, 'config.yml'), encoding='utf-8') as f:
            cls.cfg = yaml.safe_load(f)
        with open(os.path.join(PROJECT_ROOT, 'data', 'weights', 'v1.json'),
                  encoding='utf-8') as f:
            cls.v1 = json.load(f)

    def test_config_matches_v1_short(self):
        cfg_w = self.cfg['short_term']['weights']
        v1_w = self.v1['short']
        self.assertTrue(
            ScoringModel._weights_equivalent(cfg_w, v1_w),
            'config.yml 的 short_term.weights 与 v1.json 已漂移，必须同步（见 docs/审查报告_整体审查_2026-09-14.md P1-1）',
        )

    def test_config_matches_default_short(self):
        cfg_w = self.cfg['short_term']['weights']
        default_w = ScoringModel.DEFAULT_WEIGHTS['short']
        self.assertTrue(
            ScoringModel._weights_equivalent(cfg_w, default_w),
            'config.yml 的 short_term.weights 与 DEFAULT_WEIGHTS 已漂移',
        )

    def test_weights_sum_to_one(self):
        for name, w in (('config.yml', self.cfg['short_term']['weights']),
                        ('v1.json', self.v1['short']),
                        ('DEFAULT_WEIGHTS', ScoringModel.DEFAULT_WEIGHTS['short'])):
            self.assertAlmostEqual(sum(v for v in w.values() if v), 1.0, places=9,
                                   msg=f'{name} 的 short 权重合计不等于 1.0')

    def test_all_factors_declared_in_config(self):
        """config.yml 必须显式声明全部因子（含零权重），便于人工核对与未来启用"""
        cfg_w = self.cfg['short_term']['weights']
        self.assertGreaterEqual(len(cfg_w), 9, 'config.yml 因子条数异常（应为 9 维）')
        for fac in ('hot_theme', 'technical', 'capital_flow', 'volume_price',
                    'momentum', 'dragon_tiger', 'north_flow',
                    'valuation_fundamental', 'event_catalyst'):
            self.assertIn(fac, cfg_w)


class TestMismatchWarningSemantics(unittest.TestCase):
    """告警只在真不一致时出现（2026-09-14 修掉恒真假阳性）"""

    def setUp(self):
        with open(os.path.join(PROJECT_ROOT, 'config.yml'), encoding='utf-8') as f:
            self.cfg_w = dict(yaml.safe_load(f)['short_term']['weights'])

    def test_no_warning_when_consistent(self):
        """一致时不得有任何 WARNING（此前恒假阳性会在此失败）"""
        with self.assertNoLogs('core.scoring_model', level='WARNING'):
            ScoringModel(self.cfg_w)

    def test_warning_when_mismatched(self):
        drifted = dict(self.cfg_w)
        drifted['technical'] = round(drifted.get('technical', 0.16) + 0.05, 4)
        with self.assertLogs('core.scoring_model', level='WARNING') as cm:
            ScoringModel(drifted)
        self.assertTrue(
            any('不一致' in m for m in cm.output),
            '真不一致时必须告警（该告警是同步 config.yml 的信号）',
        )

    def test_effective_weights_come_from_v1(self):
        """无论 config 传什么，生效权重都必须等于 v1.json"""
        sm = ScoringModel(self.cfg_w)
        loaded = sm.get_weights('short')
        with open(os.path.join(PROJECT_ROOT, 'data', 'weights', 'v1.json'),
                  encoding='utf-8') as f:
            v1_w = json.load(f)['short']
        for k, v in v1_w.items():
            self.assertAlmostEqual(loaded[k], v, places=9, msg=f'{k} 生效权重不等于 v1.json')

    def test_default_weights_keys_match_v1(self):
        """DEFAULT_WEIGHTS 必须含 v1.json 的全部因子键（含零权重键）

        `_weights_equivalent` 把"缺键"当作 0 处理，所以 v1.json 新增
        `size: 0.00` 这类零权重键时，`test_config_matches_default_short`
        依然会通过 —— 但 fallback 层从此没有该因子的显式登记点。
        后果：v1.json 一旦丢失，回落到 DEFAULT_WEIGHTS 后 `size` 等键
        从"权重 0"退化为"未登记"，与"零权重链路先通、积累 ≥60 交易日
        后再审批加权"的约定在语义上不再对称（该因子会走 factors.get 的
        默认 50 静默路径）。此守卫要求三层键集合完全一致。
        """
        with open(os.path.join(PROJECT_ROOT, 'data', 'weights', 'v1.json'),
                  encoding='utf-8') as f:
            v1_keys = set(json.load(f)['short'])
        default_keys = set(ScoringModel.DEFAULT_WEIGHTS['short'])
        self.assertEqual(
            default_keys, v1_keys,
            f'DEFAULT_WEIGHTS 的键集合与 v1.json 不一致 —— '
            f'缺: {sorted(v1_keys - default_keys)}，'
            f'多: {sorted(default_keys - v1_keys)}'
        )


class TestEquivalenceHelper(unittest.TestCase):
    """_weights_equivalent 的边界行为"""

    def test_missing_key_treated_as_zero(self):
        self.assertTrue(ScoringModel._weights_equivalent({'a': 0.5, 'b': 0.0}, {'a': 0.5}))

    def test_none_treated_as_zero(self):
        self.assertTrue(ScoringModel._weights_equivalent({'a': None}, {'a': 0.0}))

    def test_tolerance(self):
        self.assertTrue(ScoringModel._weights_equivalent({'a': 0.5}, {'a': 0.5 + 1e-12}))
        self.assertFalse(ScoringModel._weights_equivalent({'a': 0.5}, {'a': 0.5001}))

    def test_non_numeric_is_not_equal(self):
        self.assertFalse(ScoringModel._weights_equivalent({'a': 'x'}, {'a': 0.5}))


if __name__ == '__main__':
    logging.basicConfig(level=logging.WARNING)
    unittest.main(verbosity=2)
