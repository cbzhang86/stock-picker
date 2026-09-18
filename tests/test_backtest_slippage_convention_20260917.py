# -*- coding: utf-8 -*-
"""回归测试：分级滑点入参口径（2026-09-17 缺陷1 / A1）

背景：
  `_slippage_for` 的入参应是【单笔委托金额】，而非个股当日成交额。
  旧实现三个调用点传 `kline.iloc[i]['amount']`（量级 1e8），导致所有候选日成交额
  ≥ 3e7 全落最高两档（0.4%~0.8%/边），分级滑点退化为近似固定高滑点。

  档位表语义（config.yml backtest.slippage_tiers）与
  scripts/capacity_check.py::lookup_slippage 完全一致：键是【单笔委托金额(元)】。

本文件锁定：
  A. `_slippage_for` 以单笔金额为入参，落到正确档位；
  B. `_slippage_for` 与 `lookup_slippage` 对同一组输入返回【完全一致】（防两套口径再次分叉）；
  C. `assumed_order_value` 默认 ≈ initial_capital / assumed_positions(默认3)。
"""
import os
import sys
import unittest

import yaml

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from core.backtest_engine import BacktestEngine          # noqa: E402
from scripts.capacity_check import lookup_slippage        # noqa: E402

CONFIG_PATH = os.path.join(ROOT, 'config.yml')

# 与 config.yml backtest.slippage_tiers 同步（升序：[单笔委托金额上限(元), 滑点]）
TIERS = [
    [2_000_000, 0.001],
    [10_000_000, 0.002],
    [100_000_000, 0.004],
    [1_000_000_000_000, 0.008],
]


def _load_config_tiers():
    """尽力从 config.yml 读取真实档位表；不可用时回退到 TIERS 常量。"""
    try:
        with open(CONFIG_PATH, 'r', encoding='utf-8') as f:
            cfg = yaml.safe_load(f) or {}
        tiers = (cfg.get('backtest', {}) or {}).get('slippage_tiers')
        if tiers:
            return tiers
    except Exception:
        pass
    return TIERS


class TestSlippageConvention20260917(unittest.TestCase):

    def setUp(self):
        self.tiers = _load_config_tiers()
        self.eng = BacktestEngine(config={
            'initial_capital': 1_000_000,
            'slippage_tiers': self.tiers,
        })

    # ── A. 单笔金额 → 正确档位 ──

    def test_single_order_amount_tiers(self):
        # 档位键是【单笔委托金额】(元)，不是股票日成交额
        # 初始资金 100万 / 约3只 ≈ 33万 → 应落最低档 0.1%
        self.assertAlmostEqual(self.eng._slippage_for(1_000_000), 0.001, places=6)
        self.assertAlmostEqual(self.eng._slippage_for(5_000_000), 0.002, places=6)
        self.assertAlmostEqual(self.eng._slippage_for(50_000_000), 0.004, places=6)
        self.assertAlmostEqual(self.eng._slippage_for(2_000_000_000), 0.008, places=6)

    def test_engine_tiers_match_config_yml(self):
        # 引擎实际使用的档位应与 config.yml 一致（防止两处配置漂移）
        self.assertEqual(self.eng.slippage_tiers, _load_config_tiers())

    # ── B. 与 capacity_check.lookup_slippage 完全一致（防分叉）──

    def test_consistency_with_capacity_check(self):
        cases = [1_000_000, 5_000_000, 50_000_000, 2_000_000_000,
                 200_000, 9_999_999, 99_999_999, 1_000_000_000_000,
                 333_333, 0, 1_000_000_000_001]
        for v in cases:
            self.assertEqual(
                self.eng._slippage_for(v),
                lookup_slippage(v, self.tiers),
                msg=f'回测滑点口径与 capacity_check 分叉 @ 单笔金额={v}')

    # ── C. assumed_order_value 默认口径 ──

    def test_assumed_order_value_default(self):
        # 默认：initial_capital / assumed_positions(默认3) = 约 33.3万
        eng = BacktestEngine(config={'initial_capital': 1_000_000,
                                     'slippage_tiers': self.tiers})
        self.assertAlmostEqual(eng.assumed_order_value, 1_000_000 / 3, places=2)

    def test_assumed_order_value_explicit(self):
        eng = BacktestEngine(config={'initial_capital': 1_000_000,
                                     'assumed_order_value': 250_000,
                                     'slippage_tiers': self.tiers})
        self.assertAlmostEqual(eng.assumed_order_value, 250_000, places=2)

    def test_assumed_positions_override(self):
        eng = BacktestEngine(config={'initial_capital': 1_000_000,
                                     'assumed_positions': 5,
                                     'slippage_tiers': self.tiers})
        self.assertAlmostEqual(eng.assumed_order_value, 200_000, places=2)


if __name__ == '__main__':
    unittest.main()
