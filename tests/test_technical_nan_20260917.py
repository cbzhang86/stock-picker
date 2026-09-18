# -*- coding: utf-8 -*-
"""回归测试：技术评分对 NaN 的处理（2026-09-17）

缺陷：K 线含 NaN（停牌缺口/数据缺失）时
  - `_score_rsi`：avg_gain/avg_loss 均为 NaN → RSI 为 NaN → 所有阈值比较为 False
    → 此前落到 `return 4`（"超买"，最低分档）—— 把"无数据"伪装成最高风险；
  - `_score_macd`：dif/dea 为 NaN → 同理落到 `return 6, "空头"`。
两者都应**中性化**（与"数据不足"同档），不得参与方向判断。
"""
import os
import sys
import unittest

import numpy as np
import pandas as pd

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from core.technical_scorer import TechnicalScorer  # noqa: E402


def _clean_series(n=60, start=10.0):
    return pd.Series([start + i * 0.05 for i in range(n)])


def _series_with_nan(n=60, start=10.0, nan_positions=(55,)):
    s = _clean_series(n, start)
    for p in nan_positions:
        s.iloc[p] = np.nan
    return s


class TestScoreRsiNan(unittest.TestCase):
    def setUp(self):
        self.scorer = TechnicalScorer()

    def test_clean_data_returns_normal_score(self):
        s = _clean_series()
        v = self.scorer._score_rsi(s)
        self.assertTrue(np.isfinite(v))
        self.assertTrue(0 <= v <= 10)

    def test_nan_window_gives_neutral_not_overbought(self):
        # NaN 落在 RSI 窗口内 → 必须中性(5)，不得是"超买"(4)
        s = _series_with_nan(nan_positions=(50, 52, 55))
        v = self.scorer._score_rsi(s)
        self.assertEqual(v, 5, f"NaN 数据应中性 5，实际 {v}")

    def test_all_nan_gives_neutral(self):
        s = pd.Series([np.nan] * 60)
        self.assertEqual(self.scorer._score_rsi(s), 5)


class TestScoreMacdNan(unittest.TestCase):
    def setUp(self):
        self.scorer = TechnicalScorer()

    def test_clean_data_returns_normal_score(self):
        s = _clean_series(60)
        score, label = self.scorer._score_macd(s)
        self.assertTrue(np.isfinite(score))
        self.assertNotIn("缺失", label)

    def test_nan_tail_gives_neutral(self):
        # 注：pandas ewm 对 NaN 是"跳过并延续"，末尾 NaN 通常不会产生 NaN 的
        # dif/dea；且正常管线里 close 已在 _load_kline dropna。
        # 本用例验证的是**全 NaN 输入**这一防御性护栏（管线变化时的兜底）。
        s = pd.Series([np.nan] * 60)
        score, label = self.scorer._score_macd(s)
        self.assertEqual((score, label), (7, "数据缺失（中性）"))

    def test_all_nan_gives_neutral(self):
        s = pd.Series([np.nan] * 60)
        score, label = self.scorer._score_macd(s)
        self.assertEqual((score, label), (7, "数据缺失（中性）"))


if __name__ == '__main__':
    unittest.main(verbosity=2)
