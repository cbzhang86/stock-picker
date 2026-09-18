# -*- coding: utf-8 -*-
"""流动性因子 + 波动率因子配套测试（2026-09-18 对标 Barra）"""
import os
import sys
import unittest

import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class TestLiquidityAndVolatility(unittest.TestCase):

    def test_factor_library_produces_both(self):
        from core.factor_library import FactorLibrary
        lib = FactorLibrary()
        factors = lib.compute_all_factors({'rps_20': 80})
        self.assertIn('liquidity', factors, 'liquidity 因子缺失')
        self.assertIn('volatility', factors, 'volatility 因子缺失')
        self.assertAlmostEqual(factors['liquidity'], 50.0, msg='无百分位时 liquidity 应为中性 50')
        self.assertAlmostEqual(factors['volatility'], 50.0, msg='无百分位时 volatility 应为中性 50')

    def test_liquidity_from_percentile(self):
        from core.factor_library import FactorLibrary
        lib = FactorLibrary()
        factors = lib.compute_all_factors({'rps_20': 80, '_liquidity_percentile': 0.8})
        self.assertAlmostEqual(factors['liquidity'], 80.0)

    def test_volatility_low_vol_high_score(self):
        """低波动溢价：percentile 0.3（低波动）→ 高分 70"""
        from core.factor_library import FactorLibrary
        lib = FactorLibrary()
        factors = lib.compute_all_factors({'rps_20': 80, '_volatility_percentile': 0.3})
        self.assertAlmostEqual(factors['volatility'], 70.0,
                               msg='percentile 0.3（低波动）→ (1-0.3)*100 = 70 高分')

    def test_registered_in_v1_and_config(self):
        import json, io, re
        v1 = json.load(io.open(os.path.join(PROJECT_ROOT, 'data', 'weights', 'v1.json'),
                               encoding='utf-8'))
        short = v1['short']
        for f in ('liquidity', 'volatility'):
            self.assertIn(f, short, f'v1.json 缺 {f}')
            self.assertAlmostEqual(short[f], 0.0)
        self.assertAlmostEqual(sum(v for v in short.values()
                                   if isinstance(v, (int, float))), 1.0)
        cfg = io.open(os.path.join(PROJECT_ROOT, 'config.yml'), encoding='utf-8').read()
        self.assertIsNotNone(re.search(r'(?m)^\s*liquidity:\s*0\.00', cfg))
        self.assertIsNotNone(re.search(r'(?m)^\s*volatility:\s*0\.00', cfg))

    def test_scoring_model_neutral_detection(self):
        """缺流动性/波动率数据 → 中性化标记（权重让渡路径可用）"""
        from core.scoring_model import ScoringModel
        sm = ScoringModel()
        stock = {'code': '000001', 'rps_20': 80}  # 无 total_market_cap / volume_ratio
        result = sm.score_stock(stock, mode='short')
        self.assertIn('score', result)


if __name__ == '__main__':
    unittest.main()
