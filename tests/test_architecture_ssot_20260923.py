# -*- coding: utf-8 -*-
"""审查报告 §2（架构 R1/R2/R3）与 §3.1（upsert_history 接线）的守卫测试。

R1  core/numeric.py —— 契约 A/B 的唯一实现，禁止再出现本地 `_finite` 族
R2  core/factor_registry.py —— 因子清单单一来源，消费方必须派生
R3  core/costs.py —— 成交成本常量单一来源（config.yml backtest 段）
§3.1 upsert_history 接线 —— point-in-time 历史表写入端
门槛 —— config.yml dynamic_min_score 覆盖通道接线 + 三方对账
"""
import io
import os
import re
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)


def _read(rel):
    return io.open(os.path.join(ROOT, rel), encoding='utf-8').read()


class TestR1NumericSingleSource(unittest.TestCase):
    """契约 A/B 工具必须唯一实现（core/numeric.py），本地实现只剩委托。"""

    FORBIDDEN_DEFS = (
        r'^def _finite\(', r'^def _is_finite_number\(', r'^def _as_float\(',
        r'^    def _safe_float\(', r'^def _safe_float\(', r'^    def _num\(',
        r'^def _f\(',
    )

    def test_numeric_module_exists_with_apis(self):
        from core import numeric
        for api in ('is_finite', 'opt_float', 'float_or', 'sanitize_nan'):
            self.assertTrue(callable(getattr(numeric, api)),
                            f'core/numeric.py 缺少 {api}')

    def test_no_local_finite_definitions_left(self):
        """全项目不得再出现**非委托**的本地 `_finite` 族实现。

        允许"委托 core.numeric 的薄包装"（如 data_engine._safe_float 保留方法名、
        函数体一行 `return numeric.float_or(...)`），但函数体内不得再手写
        有限性判定逻辑（isfinite/NaN 检测/try-float 组合）。
        """
        offenders = []
        for d in ('core', 'strategies', 'reports', 'feedback', 'scripts'):
            for fn in os.listdir(os.path.join(ROOT, d)):
                if not fn.endswith('.py'):
                    continue
                rel = f'{d}/{fn}'
                if rel.replace('\\', '/') == 'core/numeric.py':
                    continue
                src = _read(rel)
                lines = src.splitlines()
                for pat in self.FORBIDDEN_DEFS:
                    for m in re.finditer(pat, src, re.M):
                        line_no = src[:m.start()].count('\n')
                        body = '\n'.join(lines[line_no:line_no + 14])
                        if 'numeric' in body:
                            continue          # 已委托 → 合规
                        offenders.append(f'{rel}:{line_no + 1} ({m.group(0).strip()})')
        self.assertEqual(offenders, [],
                         f'仍存在非委托的本地 finite 实现（应委托 core/numeric.py）: '
                         f'{offenders}')

    def test_delegations_present(self):
        """既有调用名必须保留（薄包装委托），保证 100+ 调用点零改动。"""
        checks = [
            ('core/factor_library.py', '_is_finite_number = numeric.is_finite'),
            ('core/portfolio_optimizer.py', '_finite = numeric.is_finite'),
            ('core/expert_ensemble.py', '_finite = numeric.is_finite'),
            ('core/expert_ensemble.py', '_as_float = numeric.float_or'),
            ('core/risk_filter.py', '_as_float = numeric.float_or'),
            ('feedback/tracker.py', '_finite = numeric.is_finite'),
            ('feedback/data_collector.py', '_finite = numeric.is_finite'),
        ]
        for rel, anchor in checks:
            self.assertIn(anchor, _read(rel), f'{rel} 缺少委托: {anchor}')

    def test_semantics_preserved(self):
        """委托后语义不得变化（边界输入）。"""
        from core.numeric import is_finite, opt_float, float_or
        for bad in (None, float('nan'), float('inf'), float('-inf'), True,
                    '', 'x', {}, []):
            self.assertFalse(is_finite(bad), f'is_finite({bad!r}) 应为 False')
            self.assertIsNone(opt_float(bad), f'opt_float({bad!r}) 应为 None')
        for good, v in ((3, 3.0), (-5, -5.0), (0, 0.0), (np_f := float('2.5'), 2.5)):
            self.assertTrue(is_finite(good), f'is_finite({good!r}) 应为 True')
            self.assertEqual(opt_float(good), v)
        self.assertEqual(float_or(None, 7), 7)
        self.assertEqual(float_or('x', 3), 3)
        self.assertEqual(float_or(0), 0.0)


class TestR2FactorRegistry(unittest.TestCase):
    """因子清单单一来源：消费方必须 == 注册表派生。"""

    def test_registry_self_consistent(self):
        from core.factor_registry import check_consistency
        problems = check_consistency()
        self.assertEqual(problems, [], f'factor_registry 自检不一致: {problems}')

    def test_oos_lists_derived_from_registry(self):
        import core.oos_validator as ov
        from core import factor_registry as fr
        self.assertEqual(ov.K_FACTORS, list(fr.OOS_KLINE_FACTORS))
        self.assertEqual(ov.SNAPSHOT_FACTORS, list(fr.OOS_SNAPSHOT_FACTORS))
        self.assertEqual(ov.ALL_FACTORS,
                         list(fr.OOS_KLINE_FACTORS) + list(fr.OOS_SNAPSHOT_FACTORS))

    def test_diag_list_derived_from_registry(self):
        import scripts.factor_diag as fd
        from core import factor_registry as fr
        self.assertEqual(fd.FACTORS, list(fr.DIAG_FACTORS))
        self.assertEqual(fd.SPARSE_EXEMPT, set(fr.SPARSE_FACTORS))

    def test_panel_list_derived_from_registry(self):
        import scripts.recalibrate_thresholds as rt
        from core import factor_registry as fr
        self.assertEqual(rt.PANEL_FACTORS, list(fr.PANEL_FACTORS))

    def test_weight_keys_match_registry(self):
        """权重表键集必须与注册表 WEIGHT_KEYS 一致（防清单漂移）。"""
        import json
        from core.scoring_model import ScoringModel
        from core import factor_registry as fr
        v1 = json.load(io.open(os.path.join(ROOT, 'data', 'weights', 'v1.json'),
                               encoding='utf-8'))['short']
        self.assertEqual(set(v1), set(fr.WEIGHT_KEYS),
                         f"v1.json 键集与注册表不一致: "
                         f"{set(v1) ^ set(fr.WEIGHT_KEYS)}")
        sm = ScoringModel()
        self.assertEqual(set(sm.get_weights('short')), set(fr.WEIGHT_KEYS),
                         '运行时权重键集与注册表不一致')

    def test_weight_keys_covered_by_diag(self):
        """全部权重键必须在诊断清单内（否则恒值检测假绿灯——09-22 事故）。"""
        from core import factor_registry as fr
        missing = set(fr.WEIGHT_KEYS) - set(fr.DIAG_FACTORS)
        self.assertEqual(missing, set(),
                         f'诊断清单缺: {missing}')


class TestR3CostsSingleSource(unittest.TestCase):

    def test_costs_module_matches_config(self):
        import yaml
        from core.costs import load_costs
        cfg = yaml.safe_load(io.open(os.path.join(ROOT, 'config.yml'),
                                     encoding='utf-8'))
        bt = cfg.get('backtest', {}) or {}
        costs = load_costs()
        for k in ('commission_rate', 'slippage', 'stamp_duty', 'transfer_fee'):
            if k in bt:
                self.assertAlmostEqual(costs[k], float(bt[k]), places=8,
                                       msg=f'costs.{k} 与 config.yml 不一致')

    def test_consumers_delegate_to_costs(self):
        """三个脚本的成本常量必须由 core.costs 派生（禁止再硬编码）。"""
        for rel, anchor in (
            ('core/oos_validator.py', "COMMISSION = _cost['commission_rate']"),
            ('scripts/capacity_check.py', "COMMISSION = _costs['commission_rate']"),
            ('scripts/analyze_trade_decomposition.py',
             "COMMISSION = _costs['commission_rate']"),
        ):
            self.assertIn(anchor, _read(rel), f'{rel} 缺少 core.costs 委托')

    def test_no_hardcoded_cost_constants_left(self):
        offenders = []
        for rel, pat in (
            ('core/oos_validator.py', r'^COMMISSION = 0\.'),
            ('scripts/capacity_check.py', r'^COMMISSION = 0\.'),
            ('scripts/analyze_trade_decomposition.py', r'^COMMISSION = 0\.'),
        ):
            if re.search(pat, _read(rel), re.M):
                offenders.append(rel)
        self.assertEqual(offenders, [], f'仍存在硬编码成本常量: {offenders}')


class TestUpsertHistoryWiring(unittest.TestCase):
    """§3.1：point-in-time 历史表写入端接线（此前 upsert_history 零调用）。"""

    def test_prefetch_tdx_writes_history(self):
        src = _read('scripts/prefetch_tdx.py')
        self.assertIn('upsert_history', src,
                      'prefetch_tdx fundamentals 必须调用 upsert_history'
                      '（否则 fundamentals_history 恒 0 行，回测基本面因子恒缺失）')
        self.assertIn('update_date', src, '接线需按 update_date 过滤（契约：时点定位）')

    def test_provider_history_api_exists(self):
        from core.fundamental_provider import FundamentalProvider
        self.assertTrue(hasattr(FundamentalProvider, 'upsert_history'))
        self.assertTrue(hasattr(FundamentalProvider, 'get_many'))


class TestDynamicMinScoreWiring(unittest.TestCase):
    """门槛覆盖通道接线：config.yml 必须有 dynamic_min_score 且与代码/统计表一致。"""

    def test_config_has_dynamic_min_score(self):
        import yaml
        cfg = yaml.safe_load(io.open(os.path.join(ROOT, 'config.yml'),
                                     encoding='utf-8'))
        raw = (cfg.get('short_term', {}).get('buy', {}) or {}).get(
            'dynamic_min_score')
        self.assertIsNotNone(raw, 'config.yml 缺 dynamic_min_score（覆盖通道是死的）')
        self.assertEqual({k: int(v) for k, v in raw.items()},
                         {'强市': 65, '中性市': 70, '弱市': 75, '极差市': 75})


if __name__ == '__main__':
    unittest.main()
