# -*- coding: utf-8 -*-
"""回归测试：技术因子缺失值（null / NaN / ±Inf）的兜底与读写往返一致性

背景（2026-09-16 P0-1 / P1-3）：
  1) ASHareHub `technical-factors` 对部分股票返回"壳记录"——close_hfq 有值，
     而 macd_* / rsi_* / cci 为 null。实测预取缓存 126 条中 23 条（18.3%）如此。
  2) 消费端原用 `dict.get(k, 50)`，默认值只在**键缺失**时生效；键存在值为 None
     时 `None + None` → TypeError，导致整条选股流水线崩溃（当日 2 次，退出码 1）。
  3) 收尾修复：兜底改用 math.isfinite，使 NaN/±Inf 与 None 同等待遇；
     live 路径出口统一 sanitize_nan，与"写缓存→读缓存"形态等价。

本文件锁定以下不变量（防回归）：
  A. 任何非有限/缺失输入都不得抛异常；
  B. 同一份坏数据经 live 路径与缓存路径必须得到**完全相同**的技术分；
  C. 正常数据分值不得被改变（零语义变化）。
"""
import json
import math
import unittest

import pandas as pd

from core.technical_scorer import TechnicalScorer


# 真实坏记录（预取缓存 001309 德明利的实盘形态：close_hfq 有值，其余全空）
SHELL_RECORD = {
    'macd_dif': None, 'macd_dea': None, 'macd': None,
    'rsi_6': None, 'rsi_12': None, 'rsi_24': None,
    'close_hfq': 60.94, 'cci': None,
}

# 正常记录（对照；2026-09-16 审计实测分 65.0）
NORMAL_RECORD = {
    'macd_dif': 0.244, 'macd_dea': 0.163, 'macd': 0.162,
    'rsi_6': 70.0, 'rsi_12': 64.68, 'rsi_24': 60.688,
    'close_hfq': 60.94, 'cci': 123.629,
}


def _all_nan_record():
    return {k: (float('nan') if k != 'close_hfq' else 60.94)
            for k in SHELL_RECORD}


def _all_inf_record():
    return {k: (float('inf') if k != 'close_hfq' else 60.94)
            for k in SHELL_RECORD}


class TestNonFiniteFallback(unittest.TestCase):
    """A 类：非有限/缺失输入不崩溃"""

    def setUp(self):
        self.ts = TechnicalScorer()

    def test_shell_record_no_crash(self):
        """真实壳记录（全 None）此前直接 TypeError，现须返回数值"""
        score = self.ts.score_from_asharehub(dict(SHELL_RECORD))
        self.assertIsInstance(score, float)
        self.assertTrue(math.isfinite(score))
        self.assertAlmostEqual(score, 47.0, places=6)

    def test_nan_record_no_crash(self):
        """NaN 也是 float，原 isinstance 判断会放行 → 静默得 37 分"""
        score = self.ts.score_from_asharehub(_all_nan_record())
        self.assertTrue(math.isfinite(score))
        self.assertNotAlmostEqual(score, 37.0, places=6,
                                  msg="NaN 不得再落入 else 分支产生 37 分")

    def test_pos_inf_record_no_crash(self):
        score = self.ts.score_from_asharehub(_all_inf_record())
        self.assertTrue(math.isfinite(score))

    def test_negative_inf_record_no_crash(self):
        rec = {k: (float('-inf') if k != 'close_hfq' else 60.94)
               for k in SHELL_RECORD}
        score = self.ts.score_from_asharehub(rec)
        self.assertTrue(math.isfinite(score))

    def test_empty_dict_and_none_values(self):
        self.assertTrue(math.isfinite(self.ts.score_from_asharehub({})))
        self.assertTrue(math.isfinite(
            self.ts.score_from_asharehub({'rsi_12': None, 'rsi_24': None})))

    def test_string_value_falls_back(self):
        """非数值类型（上游偶发返回字符串）同样按缺省处理"""
        rec = dict(SHELL_RECORD)
        rec['rsi_12'] = 'null'
        rec['cci'] = '--'
        self.assertTrue(math.isfinite(self.ts.score_from_asharehub(rec)))


class TestMissingEqualsNonFinite(unittest.TestCase):
    """B 类：非有限值 ≡ 键缺失（形态等价），保证两条读路径行为一致"""

    def setUp(self):
        self.ts = TechnicalScorer()

    def test_none_equals_nan_equals_inf(self):
        s_none = self.ts.score_from_asharehub(dict(SHELL_RECORD))
        s_nan = self.ts.score_from_asharehub(_all_nan_record())
        s_inf = self.ts.score_from_asharehub(_all_inf_record())
        self.assertAlmostEqual(s_none, s_nan, places=9)
        self.assertAlmostEqual(s_none, s_inf, places=9)

    def test_none_equals_key_absent(self):
        """键存在值为 None 与键缺失必须同分（原缺陷正是二者不同）"""
        absent = {k: v for k, v in SHELL_RECORD.items() if v is not None}
        self.assertAlmostEqual(
            self.ts.score_from_asharehub(dict(SHELL_RECORD)),
            self.ts.score_from_asharehub(absent), places=9)


class TestCacheRoundtripEquivalence(unittest.TestCase):
    """C 类：live 路径出口 与 缓存写读往返 必须得到同一形态/同一分数"""

    def setUp(self):
        self.ts = TechnicalScorer()

    def test_live_sanitized_equals_cache_readback(self):
        from core.data_engine import sanitize_nan
        # live 路径原始产出（含 NaN）
        live_raw = _all_nan_record()
        live_out = sanitize_nan(live_raw)                  # 修复后的 live 出口
        # 缓存路径：写入前 sanitize → json 序列化 → 读回
        cached_out = json.loads(json.dumps(sanitize_nan(live_raw),
                                           ensure_ascii=False, allow_nan=False))
        self.assertEqual(live_out, cached_out)
        self.assertAlmostEqual(
            self.ts.score_from_asharehub(live_out),
            self.ts.score_from_asharehub(cached_out), places=9)

    def test_sanitize_nan_converts_all_nonfinite(self):
        from core.data_engine import sanitize_nan
        out = sanitize_nan({'a': float('nan'), 'b': float('inf'),
                            'c': float('-inf'), 'd': 1.5, 'e': None})
        self.assertIsNone(out['a'])
        self.assertIsNone(out['b'])
        self.assertIsNone(out['c'])
        self.assertEqual(out['d'], 1.5)
        self.assertIsNone(out['e'])


class TestNoRegressionOnNormalData(unittest.TestCase):
    """D 类：正常数据零语义变化"""

    def setUp(self):
        self.ts = TechnicalScorer()

    def test_normal_record_unchanged(self):
        self.assertAlmostEqual(
            self.ts.score_from_asharehub(dict(NORMAL_RECORD)), 65.0, places=6)

    def test_partial_record_uses_finite_values(self):
        """混入有效值时必须被正常消费，不能被整体拉平为缺省"""
        rec = dict(SHELL_RECORD)
        rec['rsi_12'] = 20.0
        rec['rsi_24'] = 20.0
        # macd 全空 → -10 (40)；rsi 20 → 超卖 +15 (55)；cci 空 → +2 (57)
        self.assertAlmostEqual(self.ts.score_from_asharehub(rec), 57.0, places=6)

    def test_score_bounds(self):
        for rec in (dict(SHELL_RECORD), dict(NORMAL_RECORD), {}):
            s = self.ts.score_from_asharehub(rec)
            self.assertGreaterEqual(s, 0.0)
            self.assertLessEqual(s, 100.0)


class TestDualSourceEndToEnd(unittest.TestCase):
    """E 类：崩溃点的真实调用栈（score_dual_source）端到端不崩溃"""

    def setUp(self):
        self.ts = TechnicalScorer()

    @staticmethod
    def _kline(n=60, price=60.0):
        import numpy as np
        base = np.linspace(price * 0.92, price, n)
        return pd.DataFrame({
            'date': pd.date_range('2026-06-01', periods=n, freq='D'),
            'open': base, 'high': base * 1.01, 'low': base * 0.99,
            'close': base, 'volume': np.full(n, 1_000_000.0),
        })

    def test_dual_source_with_shell_record(self):
        res = self.ts.score_dual_source(self._kline(), dict(SHELL_RECORD))
        self.assertTrue(math.isfinite(res.total))
        self.assertGreaterEqual(res.total, 0.0)
        self.assertLessEqual(res.total, 100.0)

    def test_dual_source_with_nan_record(self):
        res = self.ts.score_dual_source(self._kline(), _all_nan_record())
        self.assertTrue(math.isfinite(res.total))

    def test_dual_source_normal_unchanged(self):
        res = self.ts.score_dual_source(self._kline(), dict(NORMAL_RECORD))
        self.assertTrue(math.isfinite(res.total))
        self.assertGreaterEqual(res.total, 0.0)


if __name__ == '__main__':
    unittest.main()
