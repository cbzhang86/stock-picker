# -*- coding: utf-8 -*-
"""P2-7 Block Bootstrap 蒙特卡洛 — 单元测试（合成数据驱动）

覆盖验收清单：
  - 全正收益 → prob_profit 高 (>0.9)
  - 全负收益 → prob_profit 低 (<0.1)
  - 同 seed 结果可复现
  - 0 交易 → 非 0 退出码且不产生误导性输出
"""
import json
import os
import sys
import tempfile
import unittest

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(PROJECT_ROOT, 'scripts'))
import mc_bootstrap as m


class TestMcBootstrap(unittest.TestCase):

    def test_all_positive_high_prob(self):
        res = m.block_bootstrap([1.0] * 60, seed=7)
        self.assertGreater(res['prob_profit'], 0.9,
                           "全正收益曲线 prob_profit 应很高")

    def test_all_negative_low_prob(self):
        res = m.block_bootstrap([-1.0] * 60, seed=7)
        self.assertLess(res['prob_profit'], 0.1,
                        "全负收益曲线 prob_profit 应很低")

    def test_reproducible_same_seed(self):
        seq = [0.5, -0.3, 1.2, -0.8, 0.9, 0.2, -0.4] * 8
        a = m.block_bootstrap(seq, seed=7)
        b = m.block_bootstrap(seq, seed=7)
        self.assertEqual(a, b, "同 seed 两次调用结果应完全一致（可复现）")

    def test_zero_trades_raises(self):
        with self.assertRaises(ValueError):
            m.block_bootstrap([])

    def test_cli_zero_trades_nonzero_exit(self):
        # 0 交易 → 非 0 退出码，不输出误导性 0
        fd, path = tempfile.mkstemp(suffix='.json')
        os.close(fd)
        with open(path, 'w', encoding='utf-8') as f:
            json.dump([], f)
        try:
            rc = m.main(['--trades', path])
            self.assertNotEqual(rc, 0, "0 交易应非 0 退出码")
        finally:
            os.remove(path)

    def test_cli_success_returns_zero(self):
        fd, path = tempfile.mkstemp(suffix='.json')
        os.close(fd)
        with open(path, 'w', encoding='utf-8') as f:
            json.dump({'trade_details': [{'date': '2026-01-01', 'return_t1': 1.2},
                                        {'date': '2026-01-02', 'return_t1': -0.4},
                                        {'date': '2026-01-03', 'return_t1': 0.8}]}, f)
        try:
            rc = m.main(['--trades', path])
            self.assertEqual(rc, 0, "有效输入应退出码 0")
        finally:
            os.remove(path)

    def test_output_fields_present(self):
        res = m.block_bootstrap([0.3, -0.2, 0.5] * 20, seed=7)
        for k in ('n_trades', 'iters', 'block', 'observed_final', 'prob_profit',
                  'final_P5', 'final_P50', 'final_P95', 'mean_trade_ret'):
            self.assertIn(k, res, f"输出缺少字段 {k}")


if __name__ == '__main__':
    unittest.main(verbosity=2)
