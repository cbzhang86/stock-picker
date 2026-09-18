"""
P2-6 验证：OOSValidator.tail_spread 尾差价差口径。

daily_ic 是全截面回归视角（IC 高 ≠ 可交易）。tail_spread 取每个交易日因子
top-k / bottom-k 的平均前视收益差，对应"我们只买 top-3"的真实交易口径。

本测试覆盖三种合成数据：
  - factor 与未来收益正相关 → mean_spread > 0 且 t 显著
  - factor 与未来收益负相关 → mean_spread < 0 且 t 显著（负）
  - "IC 精确但尾部无价差"极端数据（仅中间分位有区分度）→ mean_spread ≈ 0
    而 rank IC 显著不为 0（这条正是该口径存在的意义）
全部用合成数据驱动。
"""
import os
import sys
import unittest

import numpy as np
import pandas as pd

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from core.oos_validator import OOSValidator  # noqa: E402

N_DAYS = 60
N_STOCKS = 30


def _make_panel(signal='positive', seed=11):
    """合成面板。

    signal='positive'：return = fc + noise（正相关）
    signal='negative'：return = -fc + noise（负相关）
    signal='flat_tail'：仅中间分位 return 随 fc 单调，头尾（top/bottom-k）
        收益被压成噪声 → IC 显著但尾差价差 ≈ 0
    """
    rng = np.random.default_rng(seed)
    dates = [d.strftime('%Y-%m-%d')
             for d in pd.date_range('2026-03-01', periods=N_DAYS, freq='B')]
    codes = [f'{i:06d}' for i in range(N_STOCKS)]
    center = (N_STOCKS - 1) / 2
    rows = []
    for d in dates:
        for c in range(N_STOCKS):
            fc = c - center  # 因子值（居中，便于对称构造）
            if signal == 'positive':
                rc = fc + rng.normal(scale=0.5)
            elif signal == 'negative':
                rc = -fc + rng.normal(scale=0.5)
            else:  # flat_tail
                if c < 3 or c >= N_STOCKS - 3:
                    rc = rng.normal(scale=0.5)  # 头尾：无区分度（噪声）
                else:
                    rc = fc + rng.normal(scale=0.5)  # 中间：单调
            rows.append((d, codes[c], fc, rc))
    return pd.DataFrame(rows, columns=['date', 'code', 'factor', 'ret'])


class TestTailSpread(unittest.TestCase):

    def setUp(self):
        self.v = OOSValidator()

    def test_positive_correlation_spread_positive(self):
        panel = _make_panel('positive')
        res = self.v.tail_spread(panel, 'factor', 'ret')
        self.assertIn('k3', res)
        self.assertIn('k5', res)
        s3 = res['k3']
        self.assertGreater(s3['mean_spread'], 0, "正相关应 mean_spread > 0")
        self.assertIsNotNone(s3['t_stat'])
        self.assertGreater(abs(s3['t_stat']), 2.0, "正相关价差应显著")
        self.assertGreater(s3['positive_ratio'], 0.8)
        # 结构字段齐全
        for key in ('std', 'n_days', 'positive_ratio', 'top_mean', 'bottom_mean'):
            self.assertIn(key, s3)
        print(f"\n[正相关] k3 mean_spread={s3['mean_spread']:+.3f} "
              f"t={s3['t_stat']:+.2f} | k5 mean_spread={res['k5']['mean_spread']:+.3f}")

    def test_negative_correlation_spread_negative(self):
        panel = _make_panel('negative')
        res = self.v.tail_spread(panel, 'factor', 'ret')
        s3 = res['k3']
        self.assertLess(s3['mean_spread'], 0, "负相关应 mean_spread < 0")
        self.assertIsNotNone(s3['t_stat'])
        self.assertLess(s3['t_stat'], -2.0, "负相关价差应显著为负")
        self.assertLess(s3['positive_ratio'], 0.2)
        print(f"\n[负相关] k3 mean_spread={s3['mean_spread']:+.3f} "
              f"t={s3['t_stat']:+.2f}")

    def test_precise_ic_but_no_tail_spread(self):
        panel = _make_panel('flat_tail')
        res = self.v.tail_spread(panel, 'factor', 'ret')
        s3 = res['k3']
        # 尾差价差 ≈ 0（头尾收益被压成噪声）
        self.assertAlmostEqual(s3['mean_spread'], 0.0, delta=0.5,
                               msg="头尾无区分度时尾差价差应 ≈ 0")
        # 但 rank IC 显著不为 0（中间分位仍单调）
        ic = self.v.daily_ic(panel, 'factor', 'ret')
        ic_mean = float(ic.mean())
        self.assertGreater(abs(ic_mean), 0.3,
                          "中间分位单调 → rank IC 应显著不为 0")
        print(f"\n[IC准/尾差无] tail mean_spread={s3['mean_spread']:+.3f} "
              f"| rank IC={ic_mean:+.3f}（IC 显著而尾差 ≈ 0，正是该口径意义）")

    def test_empty_panel_safe(self):
        empty = pd.DataFrame(columns=['date', 'code', 'factor', 'ret'])
        res = self.v.tail_spread(empty, 'factor', 'ret')
        self.assertEqual(res['k3']['n_days'], 0)
        self.assertEqual(res['k3']['mean_spread'], 0.0)


if __name__ == '__main__':
    unittest.main()
