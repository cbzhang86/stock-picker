"""
P2-1 验证：run_backtest.py 的 OOS 报告落盘补上 factor_corr 矩阵。

此前 oos 模式只 dump `result` + `walk_forward` 两个键，导致
scripts/calibrate_weights.py::apply_correlation_discount 永远走
"报告未含矩阵，跳过相关性折扣" 分支。本测试验证 build_oos_report_payload
会把 factor_corr（OOSValidator.factor_corr 返回的 DataFrame）转成可 JSON
序列化的嵌套 dict，并放到顶层 + result 内，对角线为 1。

全部用合成数据驱动，不依赖真实回测/数据库。
"""
import json
import os
import sys
import unittest

import numpy as np
import pandas as pd

# 让 discover 能找到 scripts / core 包（项目根在 tests 的上一级）
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from core.oos_validator import OOSValidator  # noqa: E402
from scripts.run_backtest import build_oos_report_payload  # noqa: E402


def _make_corr_df():
    """构造一个 3 因子相关性矩阵 DataFrame（对角线应为 1）。"""
    rng = np.random.default_rng(42)
    n = 20
    dates = ['2026-01-02'] * n
    codes = [f'{i:06d}' for i in range(n)]
    base = rng.normal(size=n)
    # fA / fB 高度相关，fC 独立 → 矩阵非平凡但对角线=1
    fA = base + rng.normal(scale=0.1, size=n)
    fB = base + rng.normal(scale=0.1, size=n)
    fC = rng.normal(size=n)
    df = pd.DataFrame({'date': dates, 'code': codes,
                       'fA': fA, 'fB': fB, 'fC': fC})
    return OOSValidator().factor_corr(df, factors=['fA', 'fB', 'fC'], min_stocks=3)


class TestFactorCorrDump(unittest.TestCase):

    def test_payload_contains_factor_corr(self):
        corr_df = _make_corr_df()
        self.assertFalse(corr_df.empty, "合成面板应产生非空相关性矩阵")
        result = {'factors': {'fA': {'ic': 0.05}}, 'split': {}, 'meta': {}}
        wf = {'folds': [], 'factors': {}}
        payload = build_oos_report_payload(result, wf, corr_df)

        # 顶层与 result 内都应有 factor_corr（读侧 apply_correlation_discount
        # 先找顶层再找 result.factor_corr，二者兼容）
        self.assertIn('factor_corr', payload)
        self.assertIn('factor_corr', payload['result'])
        self.assertIsInstance(payload['factor_corr'], dict)

    def test_factor_corr_json_serializable(self):
        corr_df = _make_corr_df()
        result = {'factors': {'fA': {'ic': 0.05}}, 'split': {}, 'meta': {}}
        wf = {'folds': [], 'factors': {}}
        payload = build_oos_report_payload(result, wf, corr_df)
        # 必须可 JSON 序列化（值从 numpy 类型转成了 float）
        blob = json.dumps(payload, ensure_ascii=False)
        self.assertIsInstance(blob, str)
        reloaded = json.loads(blob)
        self.assertIn('factor_corr', reloaded)

    def test_factor_corr_diagonal_is_one(self):
        corr_df = _make_corr_df()
        payload = build_oos_report_payload({}, {}, corr_df)
        corr = payload['factor_corr']
        for f in corr.keys():
            self.assertIn(f, corr[f], f"因子 {f} 的相关性行应包含自身")
            self.assertAlmostEqual(corr[f][f], 1.0, places=6,
                                   msg=f"对角线（自相关）应为 1，实际 {corr[f][f]}")

    def test_empty_corr_does_not_crash(self):
        # corr_df 为空（无数据）时 factor_corr 落 {}，读侧静默跳过，不崩溃
        empty_df = pd.DataFrame()
        result = {'factors': {}, 'split': {}, 'meta': {}}
        wf = {'folds': [], 'factors': {}}
        payload = build_oos_report_payload(result, wf, empty_df)
        self.assertEqual(payload['factor_corr'], {})
        # 仍应可序列化
        blob = json.dumps(payload, ensure_ascii=False)
        self.assertIsInstance(blob, str)
        # 顶层与 result 内一致为空 dict
        self.assertEqual(json.loads(blob)['result']['factor_corr'], {})

    def test_off_diagonal_within_range(self):
        corr_df = _make_corr_df()
        payload = build_oos_report_payload({}, {}, corr_df)
        corr = payload['factor_corr']
        for f, row in corr.items():
            for g, v in row.items():
                if f != g:
                    self.assertTrue(-1.0 <= v <= 1.0,
                                    f"非对角相关系数应在 [-1,1]，{f},{g}={v}")


if __name__ == '__main__':
    unittest.main()
