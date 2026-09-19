# -*- coding: utf-8 -*-
"""缩量/波动收敛偏离因子配套测试（2026-09-19 方案G，OOS 实证后落地）

OOS 证据（2024-01~2026-09，299.9万行/647天/5225只，ret_hold1d）：
  liq_dev IC +0.0654 / ICIR 0.535 / t +12.81（与规模代理相关 -0.088，正交）
  vol_dev IC +0.0263 / ICIR 0.225 / t +5.37
方向：偏离越低（缩量/波动收敛）→ 分越高
"""
import os
import sys
import unittest

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class TestDeviationFactors(unittest.TestCase):

    def test_factor_library_produces_both(self):
        from core.factor_library import FactorLibrary
        lib = FactorLibrary()
        factors = lib.compute_all_factors({'rps_20': 80})
        self.assertIn('liq_dev', factors, 'liq_dev 因子缺失')
        self.assertIn('vol_dev', factors, 'vol_dev 因子缺失')
        self.assertAlmostEqual(factors['liq_dev'], 50.0,
                               msg='无百分位时 liq_dev 应为中性 50')
        self.assertAlmostEqual(factors['vol_dev'], 50.0,
                               msg='无百分位时 vol_dev 应为中性 50')

    def test_liq_dev_direction(self):
        """缩量（偏离低 → percentile 低）→ 高分"""
        from core.factor_library import FactorLibrary
        lib = FactorLibrary()
        factors = lib.compute_all_factors({'rps_20': 80, '_liq_dev_percentile': 0.2})
        self.assertAlmostEqual(factors['liq_dev'], 80.0,
                               msg='percentile 0.2（缩量）→ (1-0.2)*100 = 80 高分')
        factors = lib.compute_all_factors({'rps_20': 80, '_liq_dev_percentile': 0.9})
        self.assertAlmostEqual(factors['liq_dev'], 10.0,
                               msg='percentile 0.9（放量）→ 10 低分')

    def test_vol_dev_direction(self):
        """波动收敛（偏离低）→ 高分"""
        from core.factor_library import FactorLibrary
        lib = FactorLibrary()
        factors = lib.compute_all_factors({'rps_20': 80, '_vol_dev_percentile': 0.2})
        self.assertAlmostEqual(factors['vol_dev'], 80.0)
        factors = lib.compute_all_factors({'rps_20': 80, '_vol_dev_percentile': 0.9})
        self.assertAlmostEqual(factors['vol_dev'], 10.0)

    def _kline(self, n, amounts, closes):
        dates = pd.date_range('2026-06-01', periods=n, freq='D').strftime('%Y-%m-%d')
        return pd.DataFrame({'date': dates, 'open': closes, 'high': closes,
                             'low': closes, 'close': closes,
                             'volume': amounts, 'amount': amounts})

    def test_rank_stocks_writes_liq_dev_percentile(self):
        """缩量票的 percentile 应显著低于放量票"""
        from core.scoring_model import ScoringModel
        sm = ScoringModel()
        # 60 日常态 1e8；A 缩量 2e7，B 放量 3e8
        n = 65
        base = [1e8] * n
        cl = [10.0] * n
        ka = self._kline(n, base[:-1] + [2e7], cl)
        kb = self._kline(n, base[:-1] + [3e8], cl)
        stocks = [{'code': '000001', 'kline_df': ka}, {'code': '000002', 'kline_df': kb}]
        sm.rank_stocks(stocks, mode='short')
        pa = stocks[0].get('_liq_dev_percentile')
        pb = stocks[1].get('_liq_dev_percentile')
        self.assertIsNotNone(pa, 'rank_stocks 未写入 _liq_dev_percentile')
        self.assertIsNotNone(pb)
        self.assertLess(pa, pb, '缩量票 percentile 应低于放量票')

    def test_rank_stocks_writes_vol_dev_percentile(self):
        from core.scoring_model import ScoringModel
        sm = ScoringModel()
        n = 65
        # A：近 20 日波动收敛；B：近 20 日波动放大
        rng = np.random.default_rng(7)
        cl_a = list(10 + np.cumsum(rng.normal(0, 0.05, n - 20))) + \
            list(10 + np.cumsum(rng.normal(0, 0.01, 20)))
        cl_b = list(10 + np.cumsum(rng.normal(0, 0.05, n - 20))) + \
            list(10 + np.cumsum(rng.normal(0, 0.30, 20)))
        amt = [1e8] * n
        ka = self._kline(n, amt, cl_a)
        kb = self._kline(n, amt, cl_b)
        stocks = [{'code': '000001', 'kline_df': ka}, {'code': '000002', 'kline_df': kb}]
        sm.rank_stocks(stocks, mode='short')
        pa = stocks[0].get('_vol_dev_percentile')
        pb = stocks[1].get('_vol_dev_percentile')
        self.assertIsNotNone(pa, 'rank_stocks 未写入 _vol_dev_percentile')
        self.assertIsNotNone(pb)
        self.assertLess(pa, pb, '波动收敛票 percentile 应低于波动放大票')

    def test_short_kline_no_crash(self):
        """K 线不足 30 行 → 不写百分位、不崩溃"""
        from core.scoring_model import ScoringModel
        sm = ScoringModel()
        ka = self._kline(10, [1e8] * 10, [10.0] * 10)
        stocks = [{'code': '000001', 'kline_df': ka}]
        sm.rank_stocks(stocks, mode='short')
        self.assertNotIn('_liq_dev_percentile', stocks[0])
        self.assertNotIn('_vol_dev_percentile', stocks[0])

    def test_registered_in_v1_config_and_default(self):
        """权重三层一致性（项目硬契约）：v1.json > config.yml > DEFAULT_WEIGHTS"""
        import json
        import io
        import re
        v1 = json.load(io.open(os.path.join(PROJECT_ROOT, 'data', 'weights', 'v1.json'),
                               encoding='utf-8'))
        short = v1['short']
        for f, w in (('liq_dev', 0.14), ('vol_dev', 0.07)):
            self.assertIn(f, short, f'v1.json 缺 {f}')
            self.assertAlmostEqual(short[f], w, msg=f'v1.json {f} 应为 {w}')
        self.assertAlmostEqual(sum(v for v in short.values()
                                   if isinstance(v, (int, float))), 1.0,
                               msg='v1.json short 权重和应为 1.0')
        cfg = io.open(os.path.join(PROJECT_ROOT, 'config.yml'), encoding='utf-8').read()
        self.assertIsNotNone(re.search(r'(?m)^\s*liq_dev:\s*0\.14', cfg),
                             'config.yml 缺 liq_dev: 0.14')
        self.assertIsNotNone(re.search(r'(?m)^\s*vol_dev:\s*0\.07', cfg),
                             'config.yml 缺 vol_dev: 0.07')
        from core.scoring_model import ScoringModel
        sm = ScoringModel()  # 无参数构造 → 加载 v1.json
        w = sm.get_weights('short') if hasattr(sm, 'get_weights') else None
        if w is not None:
            self.assertAlmostEqual(w.get('liq_dev'), 0.14, msg='生效权重应加载 liq_dev 0.14')
            self.assertAlmostEqual(w.get('vol_dev'), 0.07, msg='生效权重应加载 vol_dev 0.07')

    def test_oos_validator_registers(self):
        from core.oos_validator import K_FACTORS
        self.assertIn('liq_dev', K_FACTORS)
        self.assertIn('vol_dev', K_FACTORS)


if __name__ == '__main__':
    unittest.main()
