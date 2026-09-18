# -*- coding: utf-8 -*-
"""P2-8 DSR/PBO 接入 evaluate_all.py 门禁 — 单元测试

验收清单：
  - DSR/PBO 缺失（无 trials）时标 SKIP 而非 FAIL
  - 新增检查不会让整体返回 FAIL（除非真的失败）
  - 提供试验矩阵时能算出 DSR/PBO 实际数值并以 WARN 呈现（仍不阻断）
"""
import csv
import os
import subprocess
import sys
import tempfile
import unittest

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PY = sys.executable
EVAL = os.path.join(PROJECT_ROOT, 'scripts', 'evaluate_all.py')


def _run(env=None):
    e = dict(os.environ)
    if env:
        e.update(env)
    # 确保不污染：明确清除可能的 trial matrix 环境变量（除非测试要设）
    if env is None or 'EVAL_TRIAL_MATRIX' not in env:
        e.pop('EVAL_TRIAL_MATRIX', None)
    return subprocess.run([PY, EVAL], capture_output=True, text=True,
                          cwd=PROJECT_ROOT, env=e)


class TestDsrPboGate(unittest.TestCase):

    def test_no_trials_marks_skip_not_fail(self):
        r = _run()
        self.assertEqual(r.returncode, 0, "无 trials 时不应 FAIL（退出码应 0）")
        self.assertIn('SKIP', r.stdout, "DSR/PBO 缺失应标 SKIP")
        self.assertIn('0 失败', r.stdout, "不应出现失败项")
        self.assertNotIn('FAIL:', r.stdout, "DSR/PBO 缺失绝不应产生 FAIL")
        # 既有 9 项行为不变
        self.assertIn('9 通过', r.stdout, "既有 9 项应通过，行为不变")

    def test_no_trials_both_items_skipped(self):
        r = _run()
        # 两项诊断均出现 SKIP（DSR 与 PBO 各一项）
        self.assertIn('DSR 多重检验校正', r.stdout)
        self.assertIn('PBO 过拟合概率', r.stdout)

    def test_with_trial_matrix_reports_warn(self):
        # 构造合成试验矩阵（5 候选 × 40 日），经临时文件 + 环境变量注入
        fd, csv_path = tempfile.mkstemp(suffix='.csv')
        os.close(fd)
        try:
            import random
            random.seed(3)
            days, keys = 40, ['A', 'B', 'C', 'D', 'E']
            with open(csv_path, 'w', newline='') as f:
                w = csv.writer(f)
                w.writerow(['date'] + keys)
                for d in range(days):
                    w.writerow([f'2026-07-{d + 1:02d}'] +
                               [round(0.1 + i * 0.04 + random.uniform(-1, 1), 4)
                                for i in range(len(keys))])
            r = _run({'EVAL_TRIAL_MATRIX': csv_path})
            self.assertEqual(r.returncode, 0, "有矩阵也不应阻断门禁")
            self.assertIn('WARN', r.stdout, "有矩阵应标 WARN")
            self.assertIn('DSR=', r.stdout, "应打印 DSR 实际数值")
            self.assertIn('PBO=', r.stdout, "应打印 PBO 实际数值")
            self.assertIn('0 失败', r.stdout, "即使有 WARN 也不应 FAIL")
        finally:
            if os.path.exists(csv_path):
                os.remove(csv_path)


if __name__ == '__main__':
    unittest.main(verbosity=2)
