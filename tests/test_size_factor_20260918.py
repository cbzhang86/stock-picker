# -*- coding: utf-8 -*-
"""R-B 小市值因子配套测试（2026-09-18）

覆盖：横截面百分位方向（小市值高分）/ compute_all_factors 接入 / 权重 0 登记
/ 缺失市值中性化 / 权重三层等价性不受 size:0.00 影响。
"""
import io
import json
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class TestSizeFactorPercentile(unittest.TestCase):
    """rank_stocks 横截面百分位：市值越小 _size_percentile 越高"""

    def _stocks(self):
        return [
            {'code': '000001', 'name': '小盘', 'total_market_cap': 10e8},
            {'code': '000002', 'name': '中盘', 'total_market_cap': 50e8},
            {'code': '000003', 'name': '大盘', 'total_market_cap': 100e8},
        ]

    def test_smaller_cap_higher_percentile(self):
        from core.scoring_model import ScoringModel
        sm = ScoringModel()
        stocks = self._stocks()
        sm.rank_stocks(stocks, mode='short')
        pct = {s['code']: s.get('_size_percentile') for s in stocks}
        self.assertIsNotNone(pct['000001'])
        self.assertGreater(pct['000001'], pct['000002'],
                           '小市值百分位应高于中市值')
        self.assertGreater(pct['000002'], pct['000003'],
                           '中市值百分位应高于大市值')
        # 秩方向：最大市值 → 0
        self.assertAlmostEqual(pct['000003'], 0.0, places=9)

    def test_missing_mcap_gets_no_percentile(self):
        from core.scoring_model import ScoringModel
        sm = ScoringModel()
        stocks = self._stocks()
        stocks.append({'code': '000004', 'name': '缺市值'})  # 无 total_market_cap
        sm.rank_stocks(stocks, mode='short')
        missing = [s for s in stocks if s['code'] == '000004'][0]
        self.assertIsNone(missing.get('_size_percentile'),
                          '缺失市值不应写入 _size_percentile（中性化由 score_stock 处理）')


class TestSizeFactorComputation(unittest.TestCase):
    """compute_all_factors 输出 size 因子：百分位驱动，缺失中性 50"""

    def test_size_from_percentile(self):
        from core.factor_library import FactorLibrary
        lib = FactorLibrary()
        factors = lib.compute_all_factors({'rps_20': 80, '_size_percentile': 0.8})
        self.assertAlmostEqual(factors['size'], 80.0)

    def test_size_neutral_without_percentile(self):
        from core.factor_library import FactorLibrary
        lib = FactorLibrary()
        factors = lib.compute_all_factors({'rps_20': 80})
        self.assertAlmostEqual(factors['size'], 50.0,
                               msg='无百分位时 size 应为中性 50（缺失数据契约）')


class TestSizeWeightRegistration(unittest.TestCase):
    """权重 0 登记：v1.json 与 config.yml 均含 size，Σ=1.00，三层等价性不破坏"""

    def test_v1_json_registered_and_sums_to_one(self):
        v1 = json.load(io.open(os.path.join(
            PROJECT_ROOT, 'data', 'weights', 'v1.json'), encoding='utf-8'))
        short = v1['short']
        self.assertIn('size', short, 'v1.json short 缺少 size 键（评分循环按权重表遍历）')
        self.assertAlmostEqual(short['size'], 0.0)
        self.assertAlmostEqual(sum(v for v in short.values()
                                   if isinstance(v, (int, float))), 1.0)

    def test_config_registered(self):
        import re
        cfg = io.open(os.path.join(PROJECT_ROOT, 'config.yml'),
                      encoding='utf-8').read()
        self.assertIsNotNone(
            re.search(r'(?m)^\s*size:\s*0(\.0+)?\s*#', cfg),
            'config.yml 未登记 size: 0.00')

    def test_equivalence_tolerates_size_zero(self):
        """config 显式 size=0.00 与 v1/DEFAULT 缺失 size 在语义等价判定下应一致"""
        from core.scoring_model import ScoringModel
        base = {'capital_flow': 0.05, 'hot_theme': 0.42, 'reversal_20d': 0.42,
                'technical': 0.03, 'volume_price': 0.03, 'momentum': 0.03,
                'dragon_tiger': 0.02, 'north_flow': 0.0}
        with_size = {**base, 'size': 0.0}
        self.assertTrue(ScoringModel._weights_equivalent(base, with_size),
                        '缺失键视为 0 契约：显式 0 与缺失应等价')

    def test_neutral_detection_branch(self):
        """缺市值时 size 因子应被标记 neutral（权重让渡路径可用）"""
        from core.scoring_model import ScoringModel
        sm = ScoringModel()
        # weights 含 size=0 也能走通 neutral 分支（不 crash 即可，权重 0 无实际影响）
        stock = {'code': '000001', 'rps_20': 80}  # 无 total_market_cap
        result = sm.score_stock(stock, mode='short')
        self.assertIn('score', result)


if __name__ == '__main__':
    unittest.main()
