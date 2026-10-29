# -*- coding: utf-8 -*-
"""权重校准器（ICIR 口径）两处缺陷的守卫测试（2026-09-23 决策分析中发现）。

1. **非生产因子键**：IC 矩阵含 OOS 实验派生列（`skip_momentum_*`），原实现会把它们
   当作可分配因子输出 → `--apply` 会往 v1.json 写入不存在的因子键 → 有效权重和 <1、
   评分尺度静默漂移。
2. **缺少符号约束**：纯 ICIR 最大化会给**负 IC 因子**正权重（靠压低 IC 方差刷 ICIR，
   而非提升均值），与同一份 OOS 报告"反向（应降权或取反）"的判定矛盾。
"""
import io
import os
import sys
import unittest

import numpy as np
import pandas as pd

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from scripts.calibrate_weights import maximize_icir_weights  # noqa: E402


def _matrix(n_days=120, seed=7):
    """构造 IC 矩阵：hot_theme/liq_dev 正、momentum 负、skip_momentum_1 为实验列。"""
    rng = np.random.default_rng(seed)
    d = {
        'hot_theme': rng.normal(0.036, 0.043, n_days),
        'liq_dev': rng.normal(0.055, 0.13, n_days),
        'vol_dev': rng.normal(0.034, 0.14, n_days),
        'momentum': rng.normal(-0.031, 0.18, n_days),      # 负 IC
        'technical': rng.normal(-0.019, 0.18, n_days),     # 负 IC
        'skip_momentum_1': rng.normal(-0.027, 0.19, n_days),  # OOS 实验列
    }
    return pd.DataFrame(d)


PROD = {'hot_theme', 'liq_dev', 'vol_dev', 'momentum', 'technical',
        'volume_price', 'reversal_20d', 'volatility', 'dragon_tiger',
        'capital_flow', 'north_flow', 'size', 'liquidity',
        'valuation_fundamental', 'event_catalyst'}


class TestNonProductionKeyFilter(unittest.TestCase):

    def test_experiment_columns_dropped(self):
        icm = _matrix()
        w, note = maximize_icir_weights(icm, cap=0.30, min_obs=30,
                                       min_improvement=0.0, allowed=PROD)
        self.assertNotIn('skip_momentum_1', w,
                         'OOS 实验列不得出现在权重提案中（会写成真空额度）')
        self.assertIn('skip_momentum_1', note, 'note 应披露被剔除的实验列')

    def test_all_returned_keys_are_production(self):
        icm = _matrix()
        w, _ = maximize_icir_weights(icm, cap=0.30, min_obs=30,
                                    min_improvement=0.0, allowed=PROD)
        self.assertTrue(set(w).issubset(PROD), f'出现非生产键: {set(w) - PROD}')

    def test_weights_sum_to_one(self):
        icm = _matrix()
        w, _ = maximize_icir_weights(icm, cap=0.30, min_obs=30,
                                    min_improvement=0.0, allowed=PROD)
        self.assertAlmostEqual(sum(w.values()), 1.0, places=4)


class TestSignConstraint(unittest.TestCase):

    def test_negative_ic_factors_get_zero(self):
        icm = _matrix()
        w, note = maximize_icir_weights(icm, cap=0.30, min_obs=30,
                                       min_improvement=0.0, allowed=PROD)
        for f in ('momentum', 'technical'):    # 两者均值 IC 均为负
            self.assertEqual(w.get(f), 0.0,
                             f'{f} 的 OOS 均值 IC ≤ 0，权重必须为 0（符号约束）')
        self.assertIn('符号约束', note)

    def test_positive_ic_factors_get_weight(self):
        icm = _matrix()
        w, _ = maximize_icir_weights(icm, cap=0.30, min_obs=30,
                                    min_improvement=0.0, allowed=PROD)
        for f in ('hot_theme', 'liq_dev', 'vol_dev'):
            self.assertGreater(w.get(f, 0.0), 0.0, f'{f} 正 IC 应获得权重')

    def test_no_negative_ic_weight_in_any_case(self):
        """随机多组矩阵：任何负均值 IC 的列都不得拿到 >0 权重。"""
        for seed in range(5):
            icm = _matrix(seed=seed)
            w, _ = maximize_icir_weights(icm, cap=0.30, min_obs=30,
                                        min_improvement=0.0, allowed=PROD)
            mu = {c: float(icm[c].mean()) for c in icm.columns}
            for c, v in w.items():
                if mu.get(c, 0.0) <= 0:
                    self.assertEqual(v, 0.0, f'seed={seed} {c} 负 IC 却有权重 {v}')

    def test_caller_passes_allowed(self):
        src = io.open(os.path.join(ROOT, 'scripts', 'calibrate_weights.py'),
                      encoding='utf-8').read()
        self.assertIn('allowed=set(current.keys())', src,
                      'icir 分支必须把优化因子域限制为生产权重表键集')
        self.assertIn('_alien', src, '写入前需有越界键的拒绝闸门')


if __name__ == '__main__':
    unittest.main()
