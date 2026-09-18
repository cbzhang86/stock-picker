"""
2026-09-05 审查报告改进项 — 单元测试套件（P3-P）

覆盖本轮全部新增/修复功能，全部离线可跑（无网络、无 DataEngine 实例化）：
  - TestRiskFilterProxyLimitUp   P0-E  涨停代理过滤
  - TestScoringModel             P2-K/N neutral 让渡 / _factors_override / ATR 止损 / 硬拦截
  - TestExpertEnsemble           P0-B  三档置信度融合公式
  - TestHotThemeGating           P1-G  概念加分 gating 与 breakdown 回填
  - TestFactorStandardizer       P2-K  rank/winsor_z 横截面标准化
  - TestExitPath                 P0-D  卖出规则模拟（止盈/止损/双触发/跳空/T+1/时间止损）
  - TestATRFromKline             P2-N  无前视 ATR
  - TestMultipleTesting          P1-F  DSR/PBO/PSR 数学性质
  - TestCapacitySlippage         P3-S  分档滑点查表
  - TestDriftMonitor             P3-Q  PSI 与漂移检查（临时 sqlite）

运行：
  python -m unittest tests.test_improvements_20260905 -v
"""

import json
import math
import os
import sqlite3
import sys
import tempfile
import unittest

import numpy as np
import pandas as pd

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)

from core.risk_filter import RiskFilter
from core.scoring_model import ScoringModel
from core.expert_ensemble import ExpertEnsemble, confidence_to_weight_factor, \
    HIGH_CONSENSUS_THRESHOLD, MEDIUM_CONSENSUS_THRESHOLD, CONFLICT_PENALTY
from core.factor_library import FactorLibrary
from core.factor_standardizer import cross_sectional_standardize, mad_winsorize
from core.backtest_engine import BacktestEngine
from core.drift_monitor import DriftMonitor

sys.path.insert(0, os.path.join(PROJECT_ROOT, 'scripts'))
from multiple_testing import (expected_max_sharpe, psr, deflated_sharpe_ratio,
                               pbo_cscv, _norm_cdf, _norm_ppf)
from capacity_check import lookup_slippage


# ════════════════════════════ P0-E 涨停代理过滤 ════════════════════════════

class TestRiskFilterProxyLimitUp(unittest.TestCase):
    """P0-E：pct_chg >= 板块上限×0.98 触发硬过滤（penalty 0.8）"""

    def setUp(self):
        self.rf = RiskFilter({'exclude_st': True, 'exclude_limit_up': True,
                              'min_volume': 30_000_000})

    def _stock(self, code, pct_chg):
        return {'code': code, 'name': '测试股', 'pct_chg': pct_chg,
                'amount': 500_000_000}

    def test_near_limit_up_10pct_board(self):
        r = self.rf.check_stock(self._stock('600000', 9.9))
        self.assertFalse(r['passed'])
        self.assertGreaterEqual(r['score_penalty'], 0.8)
        self.assertTrue(any('接近涨停' in x for x in r['reasons']))

    def test_below_proxy_threshold_passes(self):
        r = self.rf.check_stock(self._stock('600000', 9.0))
        self.assertTrue(r['passed'])
        self.assertEqual(r['score_penalty'], 0.0)

    def test_20pct_board_boundary(self):
        limit = RiskFilter.get_board_limit('300750')
        self.assertGreater(limit, 10.0)          # 创业板不是 10% 板
        proxy = limit * 0.98
        r1 = self.rf.check_stock(self._stock('300750', proxy))
        self.assertFalse(r1['passed'])
        r2 = self.rf.check_stock(self._stock('300750', proxy - 0.5))
        self.assertTrue(r2['passed'])

    def test_switch_off(self):
        rf = RiskFilter({'exclude_limit_up': False, 'min_volume': 30_000_000})
        r = rf.check_stock(self._stock('600000', 9.9))
        self.assertTrue(r['passed'])

    def test_board_limit_defaults(self):
        self.assertEqual(RiskFilter.get_board_limit('600000'), 10.0)
        self.assertEqual(RiskFilter.get_board_limit('999999'), 10.0)  # 兜底


# ════════════════════════════ P2-K/N ScoringModel ════════════════════════════

TEST_WEIGHTS = {'capital_flow': 0.5, 'technical': 0.5}


class TestScoringModel(unittest.TestCase):
    """neutral 权重让渡 / _factors_override / ATR 止损 / 硬拦截"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='sm_weights_')
        self.sm = ScoringModel(weights=TEST_WEIGHTS, weights_dir=self.tmp,
                               sell_config={'take_profit': 0.02,
                                            'stop_loss': -0.02,
                                            'stop_mode': 'fixed'})

    def _stock(self, **kw):
        s = {'code': '600000', 'name': '测试', 'price': 10.0,
             '_factor_scores': {'capital_flow': 60.0, 'technical': 70.0},
             'main_fund_accumulated': 5_000_000}
        s.update(kw)
        return s

    def test_neutral_weight_reassignment(self):
        """main_fund 缺失 → capital_flow 中性，权重全部让渡给 technical"""
        r = self.sm.score_stock(self._stock(main_fund_accumulated=None))
        self.assertFalse(r['breakdown']['capital_flow']['data_available'])
        self.assertEqual(r['breakdown']['capital_flow']['weighted'], 0.0)
        self.assertAlmostEqual(
            r['breakdown']['technical']['effective_weight'], 1.0, places=4)
        self.assertAlmostEqual(r['score'], 70.0, places=2)  # 70 × 1.0

    def test_no_neutral_normal_weighting(self):
        r = self.sm.score_stock(self._stock())
        self.assertAlmostEqual(r['score'], 65.0, places=2)  # 60×0.5 + 70×0.5

    def test_factors_override_applied(self):
        """_factors_override 覆盖 _factor_scores（P2-K 标准化管线注入点）"""
        r = self.sm.score_stock(self._stock(
            _factors_override={'technical': 95.0}))
        self.assertAlmostEqual(r['score'], 77.5, places=2)  # 60×.5 + 95×.5

    def test_factors_override_ignores_unknown_and_none(self):
        r1 = self.sm.score_stock(self._stock(
            _factors_override={'unknown_factor': 99.0}))
        self.assertAlmostEqual(r1['score'], 65.0, places=2)
        r2 = self.sm.score_stock(self._stock(
            _factors_override={'technical': None}))
        self.assertAlmostEqual(r2['score'], 65.0, places=2)

    def test_hard_block_zero_score(self):
        r = self.sm.score_stock(self._stock(risk_check={'passed': False,
                                                        'score_penalty': 0.9}))
        self.assertEqual(r['score'], 0.0)
        self.assertTrue(r['risk_blocked'])

    def test_soft_risk_penalty(self):
        r = self.sm.score_stock(self._stock(risk_check={'passed': True,
                                                        'score_penalty': 0.3}))
        # penalty 0.3 < 0.4 → 系数 0.2 → 65 × (1-0.06) = 61.1
        self.assertAlmostEqual(r['score'], 61.1, places=2)

    def test_atr_stop_loss_wider_than_fixed(self):
        sm = ScoringModel(weights=TEST_WEIGHTS, weights_dir=self.tmp,
                          sell_config={'take_profit': 0.02, 'stop_loss': -0.02,
                                       'stop_mode': 'atr', 'atr_mult': 1.0})
        kline = pd.DataFrame({'open': [10.0] * 20, 'high': [11.0] * 20,
                              'low': [9.0] * 20, 'close': [10.0] * 20})
        r = sm.score_stock(self._stock(kline_df=kline))
        # atr=2.0 → sl_dist = max(1.0×2.0, 0.02×10) = 2.0 → stop = 8.0
        self.assertAlmostEqual(r['stop_price'], 8.0, places=2)

    def test_atr_missing_falls_back_to_fixed(self):
        sm = ScoringModel(weights=TEST_WEIGHTS, weights_dir=self.tmp,
                          sell_config={'take_profit': 0.02, 'stop_loss': -0.02,
                                       'stop_mode': 'atr', 'atr_mult': 1.0})
        r = sm.score_stock(self._stock())   # 无 kline_df
        self.assertAlmostEqual(r['stop_price'], 9.8, places=2)

    def test_fixed_stop_unchanged(self):
        r = self.sm.score_stock(self._stock())
        self.assertAlmostEqual(r['stop_price'], 9.8, places=2)

    def test_atr14_static(self):
        kline = pd.DataFrame({'high': [11.0] * 20, 'low': [9.0] * 20,
                              'close': [10.0] * 20})
        self.assertAlmostEqual(ScoringModel._atr14({'kline_df': kline}), 2.0)
        self.assertIsNone(ScoringModel._atr14({'kline_df': kline.iloc[:14]}))
        self.assertIsNone(ScoringModel._atr14({}))


# ════════════════════════════ P0-B ExpertEnsemble ════════════════════════════

class TestExpertEnsemble(unittest.TestCase):
    """三档置信度融合：用 model_score 控制 Δ，stock 最小化（expert=50）"""

    def setUp(self):
        self.ee = ExpertEnsemble()
        self.stock = {'code': '600000'}   # 全维无数据 → expert_score = 50

    def test_high_consensus(self):
        v = self.ee.fuse(self.stock, model_score=50.0)     # Δ=0
        self.assertEqual(v.confidence, 'high')
        self.assertAlmostEqual(v.ensemble_score, 50 + 0 + 1.5, places=2)

    def test_medium_consensus(self):
        v = self.ee.fuse(self.stock, model_score=35.0)     # Δ=15 ≤25
        self.assertEqual(v.confidence, 'medium')
        expected = 35 + 0.15 * 15 + 0.5
        self.assertAlmostEqual(v.ensemble_score, expected, places=2)

    def test_conflict(self):
        v = self.ee.fuse(self.stock, model_score=10.0)     # Δ=40 >25
        self.assertEqual(v.confidence, 'conflict')
        expected = 10 + 0.15 * 40 + 0 - CONFLICT_PENALTY
        self.assertAlmostEqual(v.ensemble_score, expected, places=2)
        self.assertTrue(any('高度分歧' in n for n in v.notes))

    def test_threshold_constants_match_fuse(self):
        v1 = self.ee.fuse(self.stock, model_score=50 - HIGH_CONSENSUS_THRESHOLD)
        self.assertEqual(v1.confidence, 'high')
        v2 = self.ee.fuse(self.stock,
                          model_score=50 - MEDIUM_CONSENSUS_THRESHOLD)
        self.assertEqual(v2.confidence, 'medium')

    def test_clamp_and_round(self):
        v = self.ee.fuse(self.stock, model_score=-50)      # 负分 → clamp 0
        self.assertEqual(v.model_score, -50.0)
        self.assertGreaterEqual(v.ensemble_score, 0)

    def test_confidence_weight_factor(self):
        self.assertEqual(confidence_to_weight_factor('high'), 1.00)
        self.assertEqual(confidence_to_weight_factor('medium'), 0.85)
        self.assertEqual(confidence_to_weight_factor('conflict'), 0.50)
        self.assertEqual(confidence_to_weight_factor('unknown'), 1.00)

    def test_expert_neutral_baseline(self):
        """全维无数据时 expert_score 恰为 50（各维中性值）"""
        verdict = self.ee.fuse(self.stock, model_score=50)
        self.assertAlmostEqual(verdict.expert_score, 50.0, places=2)


# ════════════════════════════ P1-G hot_theme gating ════════════════════════════

class TestHotThemeGating(unittest.TestCase):

    def test_hot_stock_bonus(self):
        self.assertAlmostEqual(FactorLibrary.calc_hot_theme_score(True), 70.0)
        self.assertAlmostEqual(FactorLibrary.calc_hot_theme_score(False), 50.0)

    def test_concept_bonus_gated_by_default(self):
        """P1-G：概念数量加分默认关闭（未经 OOS 验证）"""
        s = FactorLibrary.calc_hot_theme_score(
            False, concept_names=['概念'] * 12)
        self.assertAlmostEqual(s, 50.0)

    def test_concept_bonus_explicit_on(self):
        s12 = FactorLibrary.calc_hot_theme_score(
            False, concept_names=['概念'] * 12, concept_count_bonus=True)
        s6 = FactorLibrary.calc_hot_theme_score(
            False, concept_names=['概念'] * 6, concept_count_bonus=True)
        s3 = FactorLibrary.calc_hot_theme_score(
            False, concept_names=['概念'] * 3, concept_count_bonus=True)
        self.assertAlmostEqual(s12, 65.0)
        self.assertAlmostEqual(s6, 60.0)
        self.assertAlmostEqual(s3, 55.0)

    def test_breakdown_backfill(self):
        d = {}
        FactorLibrary.calc_hot_theme_score(True, breakdown=d)
        self.assertEqual(d.get('hot_stock'), 20)
        d2 = {}
        FactorLibrary.calc_hot_theme_score(False, concept_names=['x'] * 10,
                                           concept_count_bonus=True,
                                           breakdown=d2)
        self.assertEqual(d2.get('concept_count'), 15)

    def test_sector_momentum_scoring(self):
        blocks = {'total': 1, 'boards': [
            {'name': '半导体', 'change_pct': 4.0, 'lead_stock': ''}]}
        s = FactorLibrary.calc_hot_theme_score(False, blocks=blocks)
        self.assertAlmostEqual(s, 65.0)     # 50 + min(4×5, 15)

    def test_style_boards_excluded(self):
        """风格/指数标签板块（融资融券等）不计入题材热度"""
        blocks = {'total': 1, 'boards': [
            {'name': '融资融券', 'change_pct': 9.0, 'lead_stock': ''}]}
        s = FactorLibrary.calc_hot_theme_score(False, blocks=blocks)
        self.assertAlmostEqual(s, 50.0)

    def test_lead_stock_bonus(self):
        blocks = {'total': 1, 'boards': [
            {'name': '白酒', 'change_pct': 0.0, 'lead_stock': '贵州茅台'}]}
        s = FactorLibrary.calc_hot_theme_score(False, blocks=blocks,
                                               stock_name='贵州茅台')
        self.assertAlmostEqual(s, 55.0)


# ════════════════════════════ P2-K factor_standardizer ════════════════════════════

class TestFactorStandardizer(unittest.TestCase):

    def test_rank_method(self):
        out = cross_sectional_standardize({'a': 1, 'b': 2, 'c': 3})
        self.assertAlmostEqual(out['a'], 33.33, places=2)
        self.assertAlmostEqual(out['b'], 66.67, places=2)
        self.assertAlmostEqual(out['c'], 100.0, places=2)

    def test_rank_ties_average(self):
        out = cross_sectional_standardize({'a': 1, 'b': 1, 'c': 2})
        self.assertAlmostEqual(out['a'], 50.0, places=2)
        self.assertAlmostEqual(out['b'], 50.0, places=2)
        self.assertAlmostEqual(out['c'], 100.0, places=2)

    def test_none_preserved(self):
        out = cross_sectional_standardize({'a': None, 'b': 5, 'c': 1})
        self.assertIsNone(out['a'])
        self.assertAlmostEqual(out['b'], 100.0, places=2)
        self.assertAlmostEqual(out['c'], 50.0, places=2)

    def test_all_none(self):
        out = cross_sectional_standardize({'a': None, 'b': None})
        self.assertIsNone(out['a'])
        self.assertIsNone(out['b'])

    def test_single_valid(self):
        out = cross_sectional_standardize({'a': None, 'b': 42})
        self.assertAlmostEqual(out['b'], 100.0, places=2)

    def test_winsor_z_range_and_monotonic(self):
        vals = {'a': 1.0, 'b': 2.0, 'c': 3.0, 'd': 4.0, 'e': 100.0}
        out = cross_sectional_standardize(vals, method='winsor_z')
        for v in out.values():
            self.assertGreater(v, 0.0)
            self.assertLess(v, 100.0)
        self.assertGreater(out['e'], out['d'])    # winsorize 保序（只压幅度）
        self.assertLess(out['a'], out['b'])
        # 压缩生效：无 winsorize 时 100 的 z≈65 → logistic≈100.00；
        # 截断到 ±3σ 后 z≤3 → e=95.26 < 99
        self.assertLess(out['e'], 99.0)

    def test_mad_winsorize_clips_extreme(self):
        v = np.array([1.0, 2.0, 3.0, 4.0, 100.0])
        w = mad_winsorize(v)
        self.assertLess(w[-1], 10.0)              # 100 被截断
        np.testing.assert_array_equal(w[:4], v[:4])

    def test_mad_winsorize_constant(self):
        v = np.array([5.0, 5.0, 5.0])
        np.testing.assert_array_equal(mad_winsorize(v), v)

    def test_unknown_method_raises(self):
        with self.assertRaises(ValueError):
            cross_sectional_standardize({'a': 1}, method='bogus')


# ════════════════════════════ P0-D 卖出规则模拟 ════════════════════════════

def make_kline(rows):
    return pd.DataFrame(rows, columns=['open', 'high', 'low', 'close'])


class TestExitPath(unittest.TestCase):
    """BacktestEngine._simulate_exit_path 纯函数（fill=10, tp=10.2, sl=9.8）"""

    EP = staticmethod(BacktestEngine._simulate_exit_path)

    def test_take_profit(self):
        k = make_kline([(10, 10.1, 9.9, 10)] * 2
                       + [(10.05, 10.30, 10.00, 10.10)]
                       + [(10, 10.1, 9.9, 10)] * 2)
        price, idx, reason = self.EP(k, 1, 10.0, 0.02, -0.02, 3)
        self.assertEqual((round(price, 4), idx, reason), (10.2, 2, 'take_profit'))

    def test_stop_loss(self):
        k = make_kline([(10, 10.1, 9.9, 10)] * 2
                       + [(10.05, 10.10, 9.70, 10.00)]
                       + [(10, 10.1, 9.9, 10)] * 2)
        price, idx, reason = self.EP(k, 1, 10.0, 0.02, -0.02, 3)
        self.assertEqual((round(price, 4), idx, reason), (9.8, 2, 'stop_loss'))

    def test_double_trigger_conservative_stop(self):
        """同日双触发：保守取止损（回测不高估）"""
        k = make_kline([(10, 10.1, 9.9, 10)] * 2
                       + [(10.05, 10.30, 9.70, 10.00)]
                       + [(10, 10.1, 9.9, 10)] * 2)
        price, idx, reason = self.EP(k, 1, 10.0, 0.02, -0.02, 3)
        self.assertEqual((round(price, 4), idx, reason), (9.8, 2, 'stop_loss'))

    def test_gap_down_fills_at_open(self):
        k = make_kline([(10, 10.1, 9.9, 10)] * 2
                       + [(9.50, 9.80, 9.30, 9.60)]
                       + [(10, 10.1, 9.9, 10)] * 2)
        price, idx, reason = self.EP(k, 1, 10.0, 0.02, -0.02, 3)
        self.assertEqual((round(price, 4), idx, reason), (9.5, 2, 'stop_loss_gap'))

    def test_gap_up_fills_at_open(self):
        k = make_kline([(10, 10.1, 9.9, 10)] * 2
                       + [(10.50, 10.80, 10.40, 10.70)]
                       + [(10, 10.1, 9.9, 10)] * 2)
        price, idx, reason = self.EP(k, 1, 10.0, 0.02, -0.02, 3)
        self.assertEqual((round(price, 4), idx, reason), (10.5, 2, 'take_profit_gap'))

    def test_time_stop(self):
        k = make_kline([(10, 10.1, 9.9, 10.05)] * 5)
        price, idx, reason = self.EP(k, 1, 10.0, 0.02, -0.02, 3)
        self.assertEqual((round(price, 4), idx, reason), (10.05, 4, 'time_stop'))

    def test_strict_t_plus_1(self):
        """买入日（entry_idx=1）即使极端波动也不退出"""
        k = make_kline([(10, 10.1, 9.9, 10),
                        (10, 10.50, 9.50, 10),          # 买入日：不检查
                        (10, 10.1, 9.9, 10),
                        (10, 10.1, 9.9, 10),
                        (10, 10.1, 9.9, 10.02)])
        price, idx, reason = self.EP(k, 1, 10.0, 0.02, -0.02, 3)
        self.assertEqual(reason, 'time_stop')
        self.assertEqual(idx, 4)                          # 首个可检查日是 2

    def test_no_data_and_invalid(self):
        k = make_kline([(10, 10.1, 9.9, 10)] * 3)
        self.assertEqual(self.EP(k, 2, 10.0, 0.02, -0.02, 3)[2], 'no_data')
        self.assertEqual(self.EP(k, 1, 0.0, 0.02, -0.02, 3)[2], 'invalid_input')
        self.assertEqual(self.EP(k, None, 10.0, 0.02, -0.02, 3)[2],
                         'invalid_input')

    def test_stop_price_abs_override(self):
        """ATR 模式：绝对止损价覆盖比例价（仅止损线）"""
        k = make_kline([(10, 10.1, 9.9, 10)] * 2
                       + [(10.05, 10.30, 9.70, 10.0)]
                       + [(10, 10.1, 9.9, 10)] * 2)
        # abs=9.0：low 9.7 未破 9.0 → 止盈先触发
        price, _, reason = self.EP(k, 1, 10.0, 0.02, -0.02, 3, stop_price_abs=9.0)
        self.assertEqual(reason, 'take_profit')
        # abs=9.75：low 9.7 击穿 → 以 9.75 止损（不是比例价 9.8）；
        # 且该日 high=10.3 同时触止盈 → 双触发保守仍取止损
        price, _, reason = self.EP(k, 1, 10.0, 0.02, -0.02, 3, stop_price_abs=9.75)
        self.assertEqual((round(price, 4), reason), (9.75, 'stop_loss'))


# ════════════════════════════ P2-N ATR 无前视 ════════════════════════════

class TestATRFromKline(unittest.TestCase):

    def _kline(self, n=20, big_after=None):
        rows = [(10.0, 11.0, 9.0, 10.0)] * n
        if big_after is not None:
            for i in range(big_after, n):
                rows[i] = (10.0, 30.0, 1.0, 10.0)   # 未来巨幅波动
        return pd.DataFrame(rows, columns=['open', 'high', 'low', 'close'])

    def test_constant_range_atr(self):
        atr = BacktestEngine._atr_from_kline(self._kline(), 19)
        self.assertAlmostEqual(atr, 2.0, places=6)

    def test_no_lookahead(self):
        """upto_idx 之后的巨幅波动不影响 ATR（无前视）"""
        atr_clean = BacktestEngine._atr_from_kline(self._kline(), 4)
        atr_future = BacktestEngine._atr_from_kline(
            self._kline(big_after=5), 4)
        self.assertAlmostEqual(atr_clean, atr_future, places=6)

    def test_insufficient_rows(self):
        self.assertIsNone(BacktestEngine._atr_from_kline(self._kline(10), 9))


# ════════════════════════════ P1-F multiple testing ════════════════════════════

class TestMultipleTesting(unittest.TestCase):

    def test_psr_edge_cases(self):
        self.assertEqual(psr(sharpe=2.0, n_obs=1), 0.5)
        self.assertGreater(psr(sharpe=2.0, n_obs=252, benchmark_sr=0.0), 0.5)

    def test_expected_max_sharpe_monotonic(self):
        self.assertEqual(expected_max_sharpe(1, 0.04), 0.0)
        self.assertEqual(expected_max_sharpe(10, 0.0), 0.0)
        self.assertGreater(expected_max_sharpe(50, 0.04),
                           expected_max_sharpe(5, 0.04))
        self.assertGreater(expected_max_sharpe(5, 0.04), 0.0)

    def test_deflated_sharpe_needs_trials(self):
        rng = np.random.default_rng(42)
        rets = rng.normal(0.001, 0.01, 252)
        r = deflated_sharpe_ratio(rets, [1.2])
        self.assertIsNone(r['dsr'])
        r2 = deflated_sharpe_ratio(rets, [0.5, 1.0, 1.29, 0.8, 1.5, 0.2])
        self.assertEqual(r2['n_trials'], 6)
        self.assertGreater(r2['benchmark_sr'], 0.0)
        self.assertTrue(0.0 < r2['dsr'] < 1.0)

    def test_dsr_penalizes_lucky_noise(self):
        """同样 Sharpe 的策略：试验越多（多重检验越激烈）DSR 越低"""
        rng = np.random.default_rng(7)
        rets = rng.normal(0.0008, 0.01, 252)
        few = deflated_sharpe_ratio(rets, [1.0, 1.1])
        many = deflated_sharpe_ratio(rets, list(np.linspace(0.0, 1.1, 200)))
        self.assertGreater(few['dsr'], many['dsr'])

    def test_pbo_cscv_basic(self):
        rng = np.random.default_rng(7)
        R = rng.normal(0.0, 0.01, (64, 4))
        r = pbo_cscv(R, n_splits=8)
        self.assertEqual(r['n_combinations'], math.comb(8, 4))
        self.assertTrue(0.0 <= r['pbo'] <= 1.0)

    def test_pbo_insufficient(self):
        R = np.random.default_rng(1).normal(0, 0.01, (10, 3))
        self.assertIsNone(pbo_cscv(R, n_splits=16)['pbo'])
        self.assertIsNone(pbo_cscv(np.zeros((10, 1)))['pbo'])   # M<2

    def test_norm_roundtrip(self):
        for x in (-2.0, -0.5, 0.0, 0.7, 2.5):
            self.assertAlmostEqual(_norm_ppf(_norm_cdf(x)), x, places=4)


# ════════════════════════════ P3-S 分档滑点 ════════════════════════════

class TestCapacitySlippage(unittest.TestCase):

    TIERS = [[2_000_000, 0.001], [10_000_000, 0.002],
             [100_000_000, 0.004], [1_000_000_000_000, 0.008]]

    def test_tier_lookup(self):
        self.assertEqual(lookup_slippage(1_000_000, self.TIERS), 0.001)
        self.assertEqual(lookup_slippage(2_000_000, self.TIERS), 0.001)   # 边界含
        self.assertEqual(lookup_slippage(5_000_000, self.TIERS), 0.002)
        self.assertEqual(lookup_slippage(50_000_000, self.TIERS), 0.004)
        self.assertEqual(lookup_slippage(2_000_000_000, self.TIERS), 0.008)

    def test_empty_tiers_default(self):
        self.assertEqual(lookup_slippage(1e9, []), 0.001)
        self.assertEqual(lookup_slippage(1e9, None), 0.001)

    def test_unsorted_tiers(self):
        """乱序档位输入：排序后正常查表（5000 归属 [100, 1e12] 区间的 1e12 档）"""
        messy = [[1_000_000_000_000, 0.008], [100, 0.001]]
        self.assertEqual(lookup_slippage(50, messy), 0.001)
        self.assertEqual(lookup_slippage(5000, messy), 0.008)


# ════════════════════════════ P3-Q 漂移监控 ════════════════════════════

class TestDriftMonitor(unittest.TestCase):

    def test_psi_identical_is_zero(self):
        s = [1.0, 2.0, 3.0] * 10
        self.assertEqual(DriftMonitor._psi(s, s), 0.0)

    def test_psi_shifted_is_large(self):
        psi = DriftMonitor._psi([0.0] * 10, [3.0] * 10)
        self.assertGreater(psi, 0.25)

    def _make_db(self, path, n_days=24, recent_bad=True):
        conn = sqlite3.connect(path)
        conn.execute("CREATE TABLE predictions (id INTEGER PRIMARY KEY, "
                     "date TEXT, code TEXT, mode TEXT)")
        conn.execute("CREATE TABLE outcomes (prediction_id INTEGER, "
                     "t1_return REAL)")
        for d in range(n_days):
            date = f"2026-08-{d + 1:02d}"
            ret = (-1.0 if (recent_bad and d >= n_days - 10) else 1.0)
            for k in range(2):   # 每日 2 条推荐
                cur = conn.execute(
                    "INSERT INTO predictions (date, code, mode) VALUES (?,?,?)",
                    (date, '600000', 'short'))
                conn.execute("INSERT INTO outcomes VALUES (?,?)",
                             (cur.lastrowid, ret))
        conn.commit()
        conn.close()

    def test_alert_on_mean_shift(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = os.path.join(tmp, 'p.db')
            self._make_db(db)
            dm = DriftMonitor(db_path=db, baseline_days=10, recent_days=10)
            r = dm.check()
            self.assertEqual(r['verdict'], 'ALERT')
            self.assertGreater(r['psi'], 0.25)
            self.assertLess(r['recent_mean'], r['baseline_mean'])

    def test_insufficient_sample(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = os.path.join(tmp, 'p.db')
            self._make_db(db, n_days=5)
            dm = DriftMonitor(db_path=db, baseline_days=10, recent_days=10)
            r = dm.check()
            self.assertEqual(r['verdict'], 'INSUFFICIENT')
            self.assertIsNone(r['psi'])

    def test_missing_db(self):
        dm = DriftMonitor(db_path=os.path.join(tempfile.gettempdir(),
                                               'no_such_db_x9.db'))
        self.assertEqual(dm.check()['verdict'], 'INSUFFICIENT')

    def test_save_writes_json(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = os.path.join(tmp, 'p.db')
            self._make_db(db)
            dm = DriftMonitor(db_path=db, baseline_days=10, recent_days=10)
            # save() 写死 data/reports/drift_latest.json，这里只验证 check+序列化
            r = dm.check()
            s = json.dumps(r, ensure_ascii=False)
            self.assertIn('verdict', s)


if __name__ == '__main__':
    unittest.main(verbosity=2)
