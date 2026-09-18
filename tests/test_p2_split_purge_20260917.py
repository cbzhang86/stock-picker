"""
P2-2 验证：OOSValidator.split_by_time 加 purge / embargo。

问题：纯 70/30 时序切分无 purge/embargo，而因子窗口 20~60 日 → 训练段末尾
样本的因子窗口跨入测试段 / 标签重叠 → 系统性低估过拟合风险。

本测试：
  - 加 purge 后，训练段最后一天 + purge_days < 测试段第一天（存在隔离间隙）
  - 训练样本的"因子窗口末端 + 预测期(最长 T+5)"不越过测试段首日（无标签跨界）
  - purge_days=0 退化为旧 70/30 纯切分（证明是"加保护"而非"改口径"）
  - embargo 从测试段开头跳过若干天
  - 对比加/不加 purge 的 IC 变化（预期下降，这是诚实的代价），两者打印
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

from core.oos_validator import OOSValidator, RET_HOLD1D  # noqa: E402

N_DAYS = 200
N_STOCKS = 40
PURGE = 60


def _make_panel(n_days=N_DAYS, n_stocks=N_STOCKS, seed=7):
    """合成面板：factor 与 ret 均由 'ramp[d] * beta[c]' 信号 + 噪声构成。

    ramp[d] 随日期递增（0.05 -> 2.0）：越靠后的交易日信号相对噪声越强 →
    当日横截面 IC 越高。这样"剔除靠后的高 IC 训练日"(purge) 会让训练集平均
    IC 下降 —— 用于演示 purge 的诚实代价。
    """
    rng = np.random.default_rng(seed)
    dates = [d.strftime('%Y-%m-%d')
             for d in pd.date_range('2026-01-01', periods=n_days, freq='B')]
    codes = [f'{i:06d}' for i in range(n_stocks)]
    beta = np.linspace(0.1, 1.0, n_stocks)  # 横截面排序来源
    rows = []
    for di, d in enumerate(dates):
        ramp = 0.05 + (di / (n_days - 1)) * 1.95
        for ci, c in enumerate(codes):
            signal = ramp * beta[ci] * 10.0
            f = signal + rng.normal(scale=1.0)
            ret = signal + rng.normal(scale=1.0)
            rows.append((d, c, f, ret))
    return pd.DataFrame(rows, columns=['date', 'code', 'momentum', 'ret'])


class TestSplitByTimePurge(unittest.TestCase):

    def setUp(self):
        self.panel = _make_panel()
        self.v = OOSValidator()
        self.all_dates = sorted(self.panel['date'].unique())
        self.cut = int(len(self.all_dates) * 0.7)

    def test_purge_creates_gap_before_test(self):
        tr, te, trd, ted = self.v.split_by_time(self.panel, 0.7, purge_days=PURGE)
        self.assertTrue(trd, "purge 后训练集不应为空（面板足够长）")
        self.assertTrue(ted, "测试集不应为空")
        last_train_idx = self.all_dates.index(trd[-1])
        first_test_idx = self.all_dates.index(ted[0])
        # 训练段最后一天 + purge_days < 测试段第一天
        self.assertLess(last_train_idx + PURGE, first_test_idx,
                        "加 purge 后训练段与测试段之间应有 purge_days 隔离间隙")

    def test_no_label_leakage_across_boundary(self):
        tr, te, trd, ted = self.v.split_by_time(self.panel, 0.7, purge_days=PURGE)
        last_train_idx = self.all_dates.index(trd[-1])
        first_test_idx = self.all_dates.index(ted[0])
        # 训练样本的因子窗口末端 + 预测期（最长 T+5）不越过测试段首日
        # （purge=60 已远大于 5，这里显式验证前瞻窗口不跨界）
        self.assertLess(last_train_idx + 5, first_test_idx,
                        "训练样本的 T+5 标签不应落入测试段")

    def test_purge_days_zero_is_legacy_behavior(self):
        tr, te, trd, ted = self.v.split_by_time(self.panel, 0.7, purge_days=0)
        # 与旧 70/30 纯切分完全一致
        self.assertEqual(trd, self.all_dates[:self.cut])
        self.assertEqual(ted, self.all_dates[self.cut:])

    def test_purge_shortens_train_vs_legacy(self):
        _, _, trd0, _ = self.v.split_by_time(self.panel, 0.7, purge_days=0)
        _, _, trdp, _ = self.v.split_by_time(self.panel, 0.7, purge_days=PURGE)
        self.assertLess(len(trdp), len(trd0),
                        "purge 应从训练段末尾剔除交易日（加保护）")

    def test_embargo_skips_test_head(self):
        tr, te, trd, ted = self.v.split_by_time(
            self.panel, 0.7, purge_days=PURGE, embargo_days=10)
        # 测试段开头跳过 embargo_days
        self.assertEqual(ted, self.all_dates[self.cut + 10:],
                         "embargo 应从测试段开头跳过指定天数")

    def test_default_purge_is_protected(self):
        # 默认（purge_days=None）应启用保护，等价于 DEFAULT_PURGE_DAYS
        _, _, trd_def, _ = self.v.split_by_time(self.panel, 0.7)
        _, _, trd_exp, _ = self.v.split_by_time(
            self.panel, 0.7, purge_days=OOSValidator.DEFAULT_PURGE_DAYS)
        self.assertEqual(trd_def, trd_exp)
        self.assertLess(len(trd_def),
                        int(len(self.all_dates) * 0.7),
                        "默认应加保护（训练段比纯 70% 短）")

    def test_purge_lowers_train_ic(self):
        # 对比加/不加 purge 的 IC：预期 purge 后训练集平均 IC 下降（诚实代价）
        train_nop, _, _, _ = self.v.split_by_time(self.panel, 0.7, purge_days=0)
        train_pur, _, _, _ = self.v.split_by_time(self.panel, 0.7, purge_days=PURGE)
        ic_nop = self.v.daily_ic(train_nop, 'momentum', 'ret').mean()
        ic_pur = self.v.daily_ic(train_pur, 'momentum', 'ret').mean()
        print(f"\n[IC 对比] 无 purge 训练集 IC={ic_nop:+.4f} | "
              f"purge={PURGE} 训练集 IC={ic_pur:+.4f} | "
              f"Δ={ic_pur - ic_nop:+.4f}")
        self.assertLess(ic_pur, ic_nop,
                        "purge 移除高 IC 边界训练日后，训练集平均 IC 应下降")

    def test_evaluate_still_runs_with_default_purge(self):
        # 回归：默认开启 purge 后 evaluate 仍能正常产出（不崩溃）
        result = self.v.evaluate(self.panel, ret_col='ret')
        self.assertIn('factors', result)
        self.assertIn('momentum', result['factors'])


if __name__ == '__main__':
    unittest.main()
