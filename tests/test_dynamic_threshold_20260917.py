"""
2026-09-17 回归测试：动态评分门槛（缺陷1修复）

背景：此前 _assess_market 产出 level='中性市'，但动态门槛默认表的键是 '中性'，
导致中性市在 .get(level, self.min_score) 中未命中，门槛静默回落到 min_score=75
（config.yml），而非设计的 70，中性市被凭空收紧 5 分。

本测试锁死两个不变量：
  1. 产出值 ⊆ 已知档位常量，且三个非 skip 档位必须能命中 DEFAULT_DYNAMIC_MIN_SCORE。
  2. 中性市→70 / 强市→65 / 弱市→75 / 极差市→skip；config 的 dynamic_min_score 可覆盖。
"""
import unittest

import pandas as pd

from strategies.short_term import (
    ShortTermStrategy,
    LEVEL_STRONG,
    LEVEL_NEUTRAL,
    LEVEL_WEAK,
    LEVEL_VERY_WEAK,
    DEFAULT_DYNAMIC_MIN_SCORE,
)


def _make_quotes(values):
    codes = [f"600{i:03d}" for i in range(len(values))]
    return pd.DataFrame({"code": codes, "pct_chg": values})


def _make_hot(n):
    return pd.DataFrame({"代码": [f"600{i:03d}" for i in range(n)]})


# 各档位构造数据（回测模式，北向按 0 处理，打板情绪走涨停跌停比口径）
_STRONG = [3.0] * 80 + [-3.0] * 10 + [9.6] * 5 + [0.0] * 5
_NEUTRAL = [1.0] * 28 + [-1.0] * 20 + [0.0] * 10 + [9.6] + [-9.6]
_WEAK = [1.0] * 17 + [-1.0] * 19 + [0.0] * 4 + [9.6] * 3 + [-9.6] * 2
_VERY_WEAK = [1.0] * 5 + [-1.0] * 30 + [0.0] * 5 + [9.6] + [-9.6]

_HOT = {LEVEL_STRONG: 150, LEVEL_NEUTRAL: 50, LEVEL_WEAK: 30, LEVEL_VERY_WEAK: 5}


class TestDynamicThresholdInvariants(unittest.TestCase):
    """不变量：产出值必须 ⊆ 阈值表键（或 极差市 skip）。"""

    def _assess(self, values, hot_n):
        st = ShortTermStrategy(config={"buy": {}})
        quotes = _make_quotes(values)
        hot = _make_hot(hot_n)
        return st._assess_market(quotes, hot, is_backtest=True)

    def test_all_levels_are_known_constants(self):
        """_assess_market 产出的每一个档位都必须是四个已知常量之一。"""
        specs = [
            (_STRONG, _HOT[LEVEL_STRONG]),
            (_NEUTRAL, _HOT[LEVEL_NEUTRAL]),
            (_WEAK, _HOT[LEVEL_WEAK]),
            (_VERY_WEAK, _HOT[LEVEL_VERY_WEAK]),
        ]
        for values, hot_n in specs:
            res = self._assess(values, hot_n)
            self.assertIn(
                res["level"],
                (LEVEL_STRONG, LEVEL_NEUTRAL, LEVEL_WEAK, LEVEL_VERY_WEAK),
                f"产出未知档位: {res['level']}",
            )

    def test_non_skip_levels_resolve_in_threshold_table(self):
        """三个非 skip 档位必须能命中 DEFAULT_DYNAMIC_MIN_SCORE（核心防回归）。"""
        specs = [
            (_STRONG, _HOT[LEVEL_STRONG], LEVEL_STRONG),
            (_NEUTRAL, _HOT[LEVEL_NEUTRAL], LEVEL_NEUTRAL),
            (_WEAK, _HOT[LEVEL_WEAK], LEVEL_WEAK),
        ]
        for values, hot_n, lvl in specs:
            res = self._assess(values, hot_n)
            self.assertEqual(res["level"], lvl)
            self.assertIn(res["level"], DEFAULT_DYNAMIC_MIN_SCORE,
                           "产出档位未命中阈值表键（缺陷1回归）")
            # 生产代码用的解析逻辑：buy_cfg 无覆盖时回落默认表
            eff = int((DEFAULT_DYNAMIC_MIN_SCORE).get(res["level"], 99))
            self.assertNotEqual(eff, 99, "门槛解析回落到 fallback，档位/键不一致")

    def test_default_threshold_table_mapping(self):
        """默认门槛表映射固定为 强市65 / 中性市70 / 弱市75。"""
        self.assertEqual(DEFAULT_DYNAMIC_MIN_SCORE[LEVEL_STRONG], 65)
        self.assertEqual(DEFAULT_DYNAMIC_MIN_SCORE[LEVEL_NEUTRAL], 70)
        self.assertEqual(DEFAULT_DYNAMIC_MIN_SCORE[LEVEL_WEAK], 75)
        # 极差市按设计不进门槛表（走 skip 全停）
        self.assertNotIn(LEVEL_VERY_WEAK, DEFAULT_DYNAMIC_MIN_SCORE)


class TestDynamicThresholdBands(unittest.TestCase):
    """逐个档位：产出档位 + 期望门槛 / skip 行为。"""

    def _assess(self, values, hot_n):
        st = ShortTermStrategy(config={"buy": {}})
        return st._assess_market(_make_quotes(values), _make_hot(hot_n), is_backtest=True)

    def test_strong_maps_to_65(self):
        res = self._assess(_STRONG, _HOT[LEVEL_STRONG])
        self.assertEqual(res["level"], LEVEL_STRONG)
        self.assertFalse(res["skip"])
        self.assertEqual(DEFAULT_DYNAMIC_MIN_SCORE[res["level"]], 65)

    def test_neutral_maps_to_70(self):
        res = self._assess(_NEUTRAL, _HOT[LEVEL_NEUTRAL])
        self.assertEqual(res["level"], LEVEL_NEUTRAL)
        self.assertFalse(res["skip"])
        # 缺陷1核心：此前 '中性市' 未命中 '中性' 键 → 回落 75；修复后应为 70
        self.assertEqual(DEFAULT_DYNAMIC_MIN_SCORE[res["level"]], 70)

    def test_weak_maps_to_75(self):
        res = self._assess(_WEAK, _HOT[LEVEL_WEAK])
        self.assertEqual(res["level"], LEVEL_WEAK)
        self.assertFalse(res["skip"])
        self.assertEqual(DEFAULT_DYNAMIC_MIN_SCORE[res["level"]], 75)

    def test_very_weak_skips(self):
        res = self._assess(_VERY_WEAK, _HOT[LEVEL_VERY_WEAK])
        self.assertEqual(res["level"], LEVEL_VERY_WEAK)
        self.assertTrue(res["skip"])


class TestDynamicThresholdOverride(unittest.TestCase):
    """config 的 dynamic_min_score 仍能覆盖默认表。"""

    def test_config_override_wins(self):
        override = {LEVEL_STRONG: 11, LEVEL_NEUTRAL: 22, LEVEL_WEAK: 33}
        st = ShortTermStrategy(config={"buy": {"dynamic_min_score": override}})
        dyn = st.buy_cfg.get("dynamic_min_score") or DEFAULT_DYNAMIC_MIN_SCORE
        self.assertIs(dyn, override)
        self.assertEqual(dyn[LEVEL_NEUTRAL], 22)

    def test_no_override_uses_default(self):
        st = ShortTermStrategy(config={"buy": {}})
        dyn = st.buy_cfg.get("dynamic_min_score") or DEFAULT_DYNAMIC_MIN_SCORE
        self.assertIs(dyn, DEFAULT_DYNAMIC_MIN_SCORE)
        self.assertEqual(dyn[LEVEL_NEUTRAL], 70)


if __name__ == "__main__":
    unittest.main()
