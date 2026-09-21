# -*- coding: utf-8 -*-
"""DSR / PBO 多重检验诊断 —— 直调门禁诊断段的回归测试（2026-09-21）

此前的问题
----------
3 个用例都 `subprocess.run([python, scripts/evaluate_all.py])` 跑**完整门禁**，
再断言 `returncode == 0`。门禁本身只要有一项失败（依赖缺失、任一回归检查
崩），这 3 个用例就全红 —— 与 DSR/PBO 诊断本身无关，属于误报；
且 3 个用例 = 3 次完整门禁，把快检耦进单测。

另两处脆弱点：`assertIn('9 通过', stdout)` / `assertIn('0 失败', stdout)`
断言的是控制台**文案格式**，而非门禁的判定结果 —— 文案改版即误红。

改法
----
用 `runpy.run_path` 加载门禁模块，直接观测 `PASS / WARN / SKIP / FAIL`
四个结构化列表，断言状态而非文案。无矩阵 → 两项 SKIP；有矩阵 → 两项 WARN
且带出实际数值；两者均不得进 FAIL。
"""
import logging
import os
import runpy
import sys
import tempfile
import unittest

import pandas as pd

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)


def _load_gate():
    """加载门禁脚本为模块对象（脚本式，无法 import）。

    ⚠️ 必须恢复 `logging.disable` 状态：`evaluate_all.py` 在模块级调用
    `logging.disable(logging.CRITICAL)`（第 20 行），而 `runpy.run_path` 与
    当前进程共享 logging 全局状态 —— 一旦不恢复，后续所有依赖
    `assertLogs(..., level='WARNING')` 的用例都会静默失败（2026-09-21 实测：
    本文件把 9 个无关的 assertLogs 用例全部打红）。
    """
    saved = logging.getLogger(None).manager.disable
    try:
        return runpy.run_path(
            os.path.join(ROOT, 'scripts', 'evaluate_all.py'),
            run_name='__gate_module__')
    finally:
        logging.disable(saved)


def _mkpanel(seed=3, n_days=40, n_factors=5):
    """合成试验矩阵 CSV：每列 = 一个因子/权重候选配置的历史日收益。"""
    import numpy as np
    rng = np.random.default_rng(seed)
    cols = ['A', 'B', 'C', 'D', 'E'][:n_factors]
    rows = {'date': [f'2026-07-{d+1:02d}' for d in range(n_days)]}
    for i, c in enumerate(cols):
        rows[c] = [round(0.1 + i * 0.04 + rng.uniform(-1.0, 1.0), 4)
                   for _ in range(n_days)]
    return pd.DataFrame(rows)


class TestTrialMatrixDiscovery(unittest.TestCase):
    """`_find_trial_matrix` 的发现顺序与不可用时的返回"""

    def setUp(self):
        self._saved = os.environ.pop('EVAL_TRIAL_MATRIX', None)

    def tearDown(self):
        if self._saved is not None:
            os.environ['EVAL_TRIAL_MATRIX'] = self._saved
        else:
            os.environ.pop('EVAL_TRIAL_MATRIX', None)

    def test_env_var_wins_and_is_honored(self):
        mod = _load_gate()
        with tempfile.TemporaryDirectory() as td:
            a = os.path.join(td, 'a.csv')
            b = os.path.join(td, 'b.csv')
            _mkpanel(1).to_csv(a, index=False)
            _mkpanel(9).to_csv(b, index=False)
            os.environ['EVAL_TRIAL_MATRIX'] = a
            self.assertEqual(mod['_find_trial_matrix'](), a)
            os.environ['EVAL_TRIAL_MATRIX'] = b
            self.assertEqual(mod['_find_trial_matrix'](), b)

    def test_missing_env_returns_none(self):
        """无环境变量 → 返回 None（不编造 n_trials 口径）"""
        mod = _load_gate()
        self.assertIsNone(mod['_find_trial_matrix']())


class TestDsrPboGate(unittest.TestCase):
    """诊断三态：SKIP / WARN / PASS，任何情况下都不得进 FAIL"""

    def setUp(self):
        self._saved = os.environ.pop('EVAL_TRIAL_MATRIX', None)

    def tearDown(self):
        if self._saved is not None:
            os.environ['EVAL_TRIAL_MATRIX'] = self._saved
        else:
            os.environ.pop('EVAL_TRIAL_MATRIX', None)

    def _reset(self, mod):
        for k in ('PASS', 'FAIL', 'WARN', 'SKIP'):
            mod[k].clear()

    def test_no_matrix_marks_skip_not_fail(self):
        """无试验矩阵 → 两项 SKIP，不阻断门禁"""
        mod = _load_gate()
        self._reset(mod)
        self.assertIsNone(mod['_find_trial_matrix']())
        mod['_dsr_pbo_gate']()
        names = [n for n, _ in mod['SKIP']]
        self.assertIn('DSR 多重检验校正', names)
        self.assertIn('PBO 过拟合概率', names)
        self.assertEqual(mod['FAIL'], [], 'SKIP 不得进 FAIL')
        self.assertEqual(mod['WARN'], [], '无矩阵时不应产生 WARN')

    def test_with_matrix_marks_warn_with_values(self):
        """有试验矩阵 → 两项 WARN 且打印实际数值，仍不阻断"""
        mod = _load_gate()
        self._reset(mod)
        path = None
        try:
            fd, path = tempfile.mkstemp(suffix='.csv')
            os.close(fd)
            _mkpanel().to_csv(path, index=False)
            os.environ['EVAL_TRIAL_MATRIX'] = path
            mod['_dsr_pbo_gate']()
        finally:
            os.environ.pop('EVAL_TRIAL_MATRIX', None)
            if path and os.path.exists(path):
                os.remove(path)
        text = ' | '.join(f'{n}: {m}' for n, m in mod['WARN'])
        self.assertIn('DSR=', text, f'WARN 应带 DSR 实际数值，实际: {text}')
        self.assertIn('PBO=', text, f'WARN 应带 PBO 实际数值，实际: {text}')
        self.assertEqual(mod['FAIL'], [], 'WARN 不得阻断门禁')
        self.assertEqual(mod['SKIP'], [], '有矩阵时不应产生 SKIP')

    def test_broken_matrix_degrades_to_warn_not_fail(self):
        """矩阵可读但无因子列 → 降级 WARN，不污染 FAIL

        （此前若这里进 FAIL，门禁整体红 → 本文件 3 个用例全红，与诊断无关。）
        """
        mod = _load_gate()
        before = list(mod['FAIL'])
        self._reset(mod)
        path = None
        try:
            fd, path = tempfile.mkstemp(suffix='.csv')
            os.close(fd)
            pd.DataFrame({'date': [f'2026-07-{d+1:02d}' for d in range(5)]}) \
                .to_csv(path, index=False)
            os.environ['EVAL_TRIAL_MATRIX'] = path
            mod['_dsr_pbo_gate']()
        finally:
            os.environ.pop('EVAL_TRIAL_MATRIX', None)
            if path and os.path.exists(path):
                os.remove(path)
        self.assertEqual(mod['FAIL'], before,
                         '诊断失败应降级 WARN，不得进 FAIL')


if __name__ == '__main__':
    unittest.main(verbosity=2)
