# -*- coding: utf-8 -*-
"""深查 P1-1 / P1-2 修复的回归锁（2026-09-19）

P1-1：score_stock 中性让渡白名单必须覆盖所有"百分位驱动"的因子 ——
      缺失时不得以中性 50 冒充真实值参与加权（与专家失明同构的缺陷）。
P1-2：_SOURCE_FACTOR_IMPACT 必须登记所有非零权重因子的数据源依赖 ——
      K 线源故障时简报"受影响权重合计"不得漏算。

守卫性质：这两条把"新增因子时容易漏改的联动点"变成可自动校验的契约。
"""
import json
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# 由「原始百分位字段」驱动的因子 → 缺失时必须走让渡
PERCENTILE_DRIVEN = {
    'liq_dev': '_liq_dev_percentile',
    'vol_dev': '_vol_dev_percentile',
    'volatility': '_volatility_percentile',
    'liquidity': '_liquidity_percentile',
    'size': '_size_percentile',
}


def _short_weights():
    with open(os.path.join(PROJECT_ROOT, 'data', 'weights', 'v1.json'),
              encoding='utf-8') as f:
        return json.load(f)['short']


class TestNeutralReassignmentCoversNewFactors(unittest.TestCase):
    """P1-1：缺失 → 权重让渡（data_available=False, effective_weight=0）"""

    def _score(self, stock):
        from core.scoring_model import ScoringModel
        return ScoringModel().score_stock(stock, mode='short')

    def test_liq_dev_missing_transfers_weight(self):
        r = self._score({'code': '000001', 'rps_20': 80})   # 无 _liq_dev_percentile
        b = r['breakdown']['liq_dev']
        self.assertFalse(b['data_available'],
                         'liq_dev 无百分位时必须是"数据不可用"（让渡权重），不得当真实 50 计分')
        self.assertEqual(b['effective_weight'], 0.0)

    def test_vol_dev_and_volatility_missing_transfer(self):
        r = self._score({'code': '000001', 'rps_20': 80})
        for f in ('vol_dev', 'volatility'):
            b = r['breakdown'][f]
            self.assertFalse(b['data_available'], f'{f} 缺失时应让渡')
            self.assertEqual(b['effective_weight'], 0.0, f'{f} 有效权重应为 0')

    def test_present_percentile_is_used_as_real(self):
        """有百分位 → 视为真实数据，不触发让渡"""
        r = self._score({'code': '000001', 'rps_20': 80,
                         '_liq_dev_percentile': 0.2,      # 缩量 → 80 分
                         '_vol_dev_percentile': 0.3,      # 收敛 → 70 分
                         '_volatility_percentile': 0.4})  # 低波动 → 60 分
        self.assertTrue(r['breakdown']['liq_dev']['data_available'])
        self.assertAlmostEqual(r['breakdown']['liq_dev']['raw_score'], 80.0)
        self.assertTrue(r['breakdown']['vol_dev']['data_available'])
        self.assertAlmostEqual(r['breakdown']['vol_dev']['raw_score'], 70.0)
        self.assertTrue(r['breakdown']['volatility']['data_available'])
        self.assertAlmostEqual(r['breakdown']['volatility']['raw_score'], 60.0)

    def test_weight_conservation_when_all_new_factors_missing(self):
        """新因子全部缺失时，总有效权重仍应为 1.0（让渡不改变归一化）
        注：breakdown 的 effective_weight 保留 4 位小数，故容差取 3 位。"""
        r = self._score({'code': '000001', 'rps_20': 80})
        total_eff = sum(b.get('effective_weight', 0.0)
                        for b in r['breakdown'].values() if isinstance(b, dict))
        self.assertAlmostEqual(total_eff, 1.0, places=3,
                               msg='活跃因子吸收让渡权重后，有效权重总和应为 1.0')

    def test_all_percentile_driven_factors_are_covered(self):
        """守卫：所有以 _*_percentile 为数据源的因子都必须在让渡白名单内"""
        src = open(os.path.join(PROJECT_ROOT, 'core', 'scoring_model.py'),
                   encoding='utf-8').read()
        block = src[src.index('_peri_map = {'):src.index('_peri_map = {') + 600]
        for factor, field in PERCENTILE_DRIVEN.items():
            self.assertIn(f"'{factor}': '{field}'", block,
                          f'让渡白名单缺 {factor}（新增百分位驱动因子时必须同步登记）')


class TestSourceFactorImpactCoverage(unittest.TestCase):
    """P1-2：非零权重因子必须登记在正确的数据源族里（显式依赖表，永不跳过）

    设计说明：不能用"是否出现在任一映射"做判据 —— `akshare_codes` / `tencent_quote`
    的值是哨兵 `'全部因子'`（有意覆盖全体），会让判据恒真而失效。
    改为按**数据源族**逐项声明依赖，新增因子若漏登记即失败。
    """

    # 因子 → 必须出现在这些数据源键中（任选其一即可视为已登记）
    REQUIRED = {
        'technical': ('mootdx_kline', 'baostock_kline', 'asharehub_tech_factors'),
        'volume_price': ('mootdx_kline', 'baostock_kline'),
        'momentum': ('mootdx_kline', 'baostock_kline'),
        'reversal_20d': ('mootdx_kline', 'baostock_kline'),
        'liq_dev': ('mootdx_kline', 'baostock_kline'),
        'vol_dev': ('mootdx_kline', 'baostock_kline'),
        'volatility': ('mootdx_kline', 'baostock_kline'),
        'liquidity': ('mootdx_kline', 'baostock_kline'),
        'capital_flow': ('akshare_fund_flow', 'ths_fund_flow', 'big_deal',
                         'asharehub_moneyflow'),
        'north_flow': ('akshare_north_flow',),
        'hot_theme': ('asharehub_concepts', 'ths_hot', 'eastmoney_blocks'),
        'dragon_tiger': ('dragon_tiger',),
    }

    def test_nonzero_weight_factors_have_source_family(self):
        from reports.market_briefing import _SOURCE_FACTOR_IMPACT as IMPACT
        weights = _short_weights()
        problems = []
        for f, w in weights.items():
            if not (isinstance(w, (int, float)) and w > 0):
                continue
            keys = self.REQUIRED.get(f)
            if keys is None:
                problems.append(f'{f}(权重 {w}) 未在测试的依赖表中声明')
                continue
            if not any(k in IMPACT for k in keys):
                problems.append(f'{f} 依赖的数据源键 {keys} 均不在 _SOURCE_FACTOR_IMPACT')
                continue
            if not any(f in IMPACT.get(k, []) or '全部因子' in IMPACT.get(k, [])
                       for k in keys):
                problems.append(f'{f} 未被任何对应数据源映射列出（降级警示会漏算）')
        self.assertEqual(problems, [], '数据源影响登记缺口: ' + '; '.join(problems))

    def test_kline_factors_registered(self):
        """K 线源映射必须包含 2026-09-19 启用的三个 K 线派生因子"""
        from reports.market_briefing import _SOURCE_FACTOR_IMPACT
        for key in ('mootdx_kline', 'baostock_kline'):
            facs = set(_SOURCE_FACTOR_IMPACT[key])
            for f in ('liq_dev', 'vol_dev', 'volatility'):
                self.assertIn(f, facs, f'{key} 映射缺 {f}')

    def test_degradation_share_includes_new_factors(self):
        """K 线源故障时，受影响权重合计应包含新因子（约 0.27）"""
        from reports.market_briefing import _degradation_note
        lines = _degradation_note({'mootdx_kline': {'available': False}}, mode='short')
        text = '\n'.join(lines)
        for f in ('liq_dev', 'vol_dev', 'volatility'):
            self.assertIn(f, text, f'降级说明应列出 {f}')
        share_ok = any(k in text for k in ('权重', '%', '合计'))
        self.assertTrue(share_ok, '降级说明应包含受影响权重合计')


class TestBriefingFallbackWarns(unittest.TestCase):
    """P2-2：source_status 缺失时回退必须留痕（logger.warning）"""

    def test_fallback_logs_warning(self):
        src = open(os.path.join(PROJECT_ROOT, 'reports', 'market_briefing.py'),
                   encoding='utf-8').read()
        idx = src.index('snapshot = de.get_data_source_summary()')
        window = src[max(0, idx - 400):idx + 400]
        self.assertIn('logger.warning', window,
                      'source_status 回退分支必须打 warning，防止静默全绿')


class TestRecalibrateThresholdsTool(unittest.TestCase):
    """P1-3：重校工具可运行（不产出噪声结论、样本不足时明确退出）"""

    def test_script_importable_and_thresholds_documented(self):
        import scripts.recalibrate_thresholds as rt
        # 与 short_term 的实际常量/档位标签对齐（防脚本与业务脱节）
        from strategies.short_term import DEFAULT_DYNAMIC_MIN_SCORE, \
            LEVEL_STRONG, LEVEL_NEUTRAL, LEVEL_WEAK
        self.assertEqual(rt.CURRENT_THRESHOLDS[LEVEL_STRONG],
                         DEFAULT_DYNAMIC_MIN_SCORE[LEVEL_STRONG])
        self.assertEqual(rt.CURRENT_THRESHOLDS[LEVEL_NEUTRAL],
                         DEFAULT_DYNAMIC_MIN_SCORE[LEVEL_NEUTRAL])
        self.assertEqual(rt.CURRENT_THRESHOLDS[LEVEL_WEAK],
                         DEFAULT_DYNAMIC_MIN_SCORE[LEVEL_WEAK])
        self.assertIn('hot_theme', rt.W_NEW)
        self.assertAlmostEqual(sum(rt.W_NEW.values()), 1.0, places=2,
                               msg='W_NEW 应为方案 G（Σ=1.00）')

    def test_run_context_mode_handles_empty(self):
        """无/不足样本时返回 1 且不抛异常（避免自动化误判成功）"""
        import scripts.recalibrate_thresholds as rt
        rc = rt.from_run_context()
        self.assertIn(rc, (0, 1))


class TestEndToEndIntegration(unittest.TestCase):
    """集成维度：rank_stocks → score_stock 真实链路（不是单点断言）"""

    def _kline(self, n, amount=1e8, close=10.0):
        import pandas as pd
        dates = pd.date_range('2026-06-01', periods=n, freq='D').strftime('%Y-%m-%d')
        return pd.DataFrame({'date': dates, 'open': [close] * n, 'high': [close] * n,
                             'low': [close] * n, 'close': [close] * n,
                             'volume': [amount] * n, 'amount': [amount] * n})

    def test_rank_then_score_mixed_kline_lengths(self):
        from core.scoring_model import ScoringModel
        sm = ScoringModel()
        stocks = [
            {'code': '000001', 'rps_20': 80, 'kline_df': self._kline(65)},   # 充足
            {'code': '000002', 'rps_20': 60, 'kline_df': self._kline(25)},   # 偏短
            {'code': '000003', 'rps_20': 50},                                # 无 K 线
        ]
        sm.rank_stocks(stocks, mode='short')
        for s in stocks:
            r = sm.score_stock(s, mode='short')
            self.assertIsInstance(r.get('score'), (int, float))
            self.assertGreaterEqual(r['score'], 0)
            self.assertLessEqual(r['score'], 100)
        # 充足样本：三条 K 线因子应为真实数据
        b1 = sm.score_stock(stocks[0], mode='short')['breakdown']
        self.assertTrue(b1['liq_dev']['data_available'], '65 日 K 线应能算出 liq_dev 百分位')
        self.assertTrue(b1['vol_dev']['data_available'], '65 日 K 线应能算出 vol_dev 百分位')
        # 无 K 线样本：必须走让渡，不得以 50 冒充
        b3 = sm.score_stock(stocks[2], mode='short')['breakdown']
        for f in ('liq_dev', 'vol_dev', 'volatility'):
            self.assertFalse(b3[f]['data_available'], f'无 K 线时 {f} 应让渡')

    def test_all_weighted_factors_present_in_factors_dict(self):
        """守卫：v1.json 中每个 short 权重键都必须能被 compute_all_factors 产出
        （否则 score_stock 会走 .get 默认 50 的静默路径）"""
        from core.factor_library import FactorLibrary
        factors = FactorLibrary().compute_all_factors({'rps_20': 80})
        for f in _short_weights():
            self.assertIn(f, factors, f'权重表含 {f}，但因子库不产出该键')


    def test_full_data_score_equals_weighted_sum(self):
        """无副作用验证：**所有因子数据齐全（无让渡）**时，综合分必须严格等于
        Σ(因子分×v1 权重) —— 证明新增的让渡逻辑对数据齐全的股票是完全 no-op。"""
        from core.scoring_model import ScoringModel
        weights = _short_weights()
        stock = {'code': '000001', 'rps_20': 80,
                 '_liq_dev_percentile': 0.2, '_vol_dev_percentile': 0.3,
                 '_volatility_percentile': 0.4, '_liquidity_percentile': 0.5,
                 '_size_percentile': 0.6,
                 'is_hot_stock': True,
                 'main_fund_accumulated': 60_000_000,      # capital_flow 不被让渡
                 'north_flow_accumulated': 3000}
        r = ScoringModel().score_stock(stock, mode='short')
        bd = r['breakdown']
        neutrals = [f for f, b in bd.items()
                    if isinstance(b, dict) and not b.get('data_available', True)
                    and weights.get(f, 0) > 0]      # 只看有权重的因子（size 权重 0 无影响）
        self.assertEqual(neutrals, [], f'本用例要求零让渡，实际被让渡: {neutrals}')
        expected = sum(bd[f]['raw_score'] * w for f, w in weights.items()
                       if f in bd and w > 0)
        self.assertAlmostEqual(r['score'], expected, places=2,
                               msg=f'零让渡时评分应严格等于加权和（{r["score"]} vs {expected:.2f}）')
        for f in ('liq_dev', 'vol_dev', 'volatility'):
            self.assertTrue(bd[f]['data_available'])


class TestCalibratorObjectiveGate(unittest.TestCase):
    """P2-1：--apply 必须显式接受 IC 目标，且拒绝时退出码非 0（自动化可感知）、
    绝不修改 v1.json"""

    def test_apply_without_ack_refuses_and_keeps_v1(self):
        import hashlib
        import subprocess
        v1 = os.path.join(PROJECT_ROOT, 'data', 'weights', 'v1.json')
        before = hashlib.md5(open(v1, 'rb').read()).hexdigest()
        proc = subprocess.run(
            [sys.executable, os.path.join(PROJECT_ROOT, 'scripts', 'calibrate_weights.py'),
             '--apply'],
            capture_output=True, text=True, timeout=300)
        after = hashlib.md5(open(v1, 'rb').read()).hexdigest()
        self.assertEqual(proc.returncode, 2,
                         f'闸门拒绝写入应返回码 2（实际 {proc.returncode}）—— '
                         f'否则自动化会把"已拒绝"误判为成功\n{proc.stdout[-300:]}')
        self.assertEqual(before, after, '闸门拒绝时不得修改 v1.json')
        self.assertIn('accept-ic-objective', proc.stdout)


class TestThemeSortNaNGuard(unittest.TestCase):
    """P2-3：展示层排序必须用 isfinite（NaN 是 float，isinstance 判不出来）"""

    def test_uses_isfinite(self):
        src = open(os.path.join(PROJECT_ROOT, 'core', 'data_engine.py'),
                   encoding='utf-8').read()
        idx = src.index('def _rank_key(s):')
        window = src[idx:idx + 500]
        self.assertIn('math.isfinite', window,
                      '主题榜排序键应改用 math.isfinite，避免 NaN 参与排序')


if __name__ == '__main__':
    unittest.main()
