# -*- coding: utf-8 -*-
"""OOS 验证器主路径回归锁（2026-09-21）

覆盖此前零测试的 OOS 计算层（`core/oos_validator.py`，1210 行）：

  A. `_compute_forward_returns` 收益口径的手工算术核对
     + 次日无成交 → NaN（一字板/停牌不得伪造 0 收益）
  B. `daily_ic` 的方向语义：全同 +1.0 / 全反 −1.0 / 逐日符号翻转 ≈0
  C. `tail_spread` 与 `daily_ic` 的分歧（P2-6：IC≈0 但头尾价差强）
  D. `ic_decay` 的口径映射与无有效样本时的 None 语义
  E. `factor_corr` 对"因子当日退化为常数"的容错（不污染整张矩阵）
  F. `walk_forward` 的扩展式切分顺序 + `stable` 语义 + 数据不足提示
  G. `neutralized_ic` 的桶内去均值：原始 IC 强、控制流动性后消失
  H. `_technical_score_vec` 输出界 [0,100]、无 NaN、量比除零不崩

不覆盖：`build_panel` / `_load_kline`（需真实 K 线缓存）、
`_attach_snapshot_factors`（需 factor_daily.db 快照）——二者是数据接入层，
由 scripts/run_backtest.py --mode oos 的实盘运行覆盖。
"""
import os
import sys
import unittest

import numpy as np
import pandas as pd

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from core.oos_validator import (  # noqa: E402
    COMMISSION, SLIPPAGE, STAMP_DUTY, TRANSFER_FEE,
    OOSValidator, RET_HOLD1D, RET_HOLD3D, RET_HOLD5D,
    RET_INTRADAY, RET_OVERNIGHT,
)


def _panel(rets_by_day, codes=6):
    """构造最小面板：**按 date 主序**铺行（每日 codes 只），因子列按日块赋值。

    必须 date 主序：`daily_ic` / `tail_spread` 都按日分组算横截面，
    若按 code 主序排列，groupby('date') 每组只剩 1 行，单日样本 <5 只被跳过，
    daily_ic 返回空 —— 断言会"空转通过"。
    """
    rows = []
    day_dates = sorted(rets_by_day)
    prev_close = {f'C{c}': 10.0 for c in range(codes)}
    for d in day_dates:
        for code in range(codes):
            key = f'C{code}'
            ret = rets_by_day[d].get(key)
            rows.append({'code': key, 'date': d,
                         'open': prev_close[key], 'close': prev_close[key],
                         'volume': 1e6, 'amount': 1e8, RET_HOLD1D: ret})
            if ret is not None:
                prev_close[key] = prev_close[key] * 1.01 * (1 + ret / 100.0)
    return pd.DataFrame(rows)


def _add_factor(p, n_days, codes=6):
    """按 date 主序写入单调因子列：每日 C0 最低、C{codes-1} 最高。"""
    return p.assign(f=[c * 10.0 for _ in range(n_days) for c in range(codes)])


class TestForwardReturns(unittest.TestCase):

    def test_calculated_columns_are_present(self):
        v = OOSValidator()
        p = pd.DataFrame({'code': ['C0', 'C0'], 'date': ['2026-01-01', '2026-01-02'],
                          'open': [10.0, 10.5], 'close': [10.2, 10.7],
                          'volume': [1e6, 1e6], 'amount': [1e8, 1e8]})
        out = v._compute_forward_returns(p)
        for col in (RET_HOLD1D, RET_HOLD3D, RET_HOLD5D, RET_INTRADAY, RET_OVERNIGHT):
            self.assertIn(col, out.columns, f'缺收益口径列 {col}')

    def test_hold1d_matches_manual_arithmetic(self):
        """close_t0 买 → close_t1 卖，扣滑点与佣金/过户费/印花税，须等于手工算式"""
        v = OOSValidator()
        p = pd.DataFrame({'code': ['C0'] * 3,
                          'date': ['2026-01-01', '2026-01-02', '2026-01-03'],
                          'open': [10.0, 10.2, 10.4],
                          'close': [10.0, 11.0, 9.0],
                          'volume': [1e6] * 3, 'amount': [1e8] * 3})
        out = v._compute_forward_returns(p)
        buy = 10.0 * (1 + SLIPPAGE) * (1 + COMMISSION + TRANSFER_FEE)
        sell = 11.0 * (1 - SLIPPAGE) * (1 - COMMISSION - TRANSFER_FEE - STAMP_DUTY)
        self.assertAlmostEqual(out[RET_HOLD1D].iloc[0], (sell / buy - 1) * 100,
                               places=8)

    def test_intraday_uses_next_open(self):
        """ret_intraday 必须以 T+1 开盘价为买入价，不是 T 日收盘"""
        v = OOSValidator()
        p = pd.DataFrame({'code': ['C0'] * 2,
                          'date': ['2026-01-01', '2026-01-02'],
                          'open': [10.0, 12.0], 'close': [10.0, 13.0],
                          'volume': [1e6, 1e6], 'amount': [1e8, 1e8]})
        out = v._compute_forward_returns(p)
        buy_o = 12.0 * (1 + SLIPPAGE) * (1 + COMMISSION + TRANSFER_FEE)
        sell = 13.0 * (1 - SLIPPAGE) * (1 - COMMISSION - TRANSFER_FEE - STAMP_DUTY)
        self.assertAlmostEqual(out[RET_INTRADAY].iloc[0], (sell / buy_o - 1) * 100,
                               places=8)

    def test_no_next_day_becomes_nan(self):
        """次日无成交（一字板/停牌）→ 收益必须 NaN，不得伪造成 0"""
        v = OOSValidator()
        p = pd.DataFrame({'code': ['C0'] * 2,
                          'date': ['2026-01-01', '2026-01-02'],
                          'open': [10.0, 0.0], 'close': [10.0, 0.0],
                          'volume': [1e6, 0], 'amount': [1e8, 0]})
        out = v._compute_forward_returns(p)
        self.assertTrue(pd.isna(out[RET_HOLD1D].iloc[0]),
                        '次日 close<=0 时 ret_hold1d 应为 NaN')
        self.assertTrue(pd.isna(out[RET_INTRADAY].iloc[0]),
                        '次日 open<=0 时 ret_intraday 应为 NaN')


class TestDailyIcSemantics(unittest.TestCase):

    def test_perfect_alignment_is_one(self):
        v = OOSValidator()
        days = {f'2026-01-{d+1:02d}': {f'C{c}': (c + 1) * 2.0 for c in range(6)}
                for d in range(12)}
        p = _add_factor(_panel(days), 12)
        ic = v.daily_ic(p, 'f', RET_HOLD1D)
        self.assertAlmostEqual(ic.mean(), 1.0, places=6)

    def test_inverted_alignment_is_negative(self):
        v = OOSValidator()
        days = {f'2026-01-{d+1:02d}': {f'C{c}': (5 - c) * 2.0 for c in range(6)}
                for d in range(12)}
        p = _add_factor(_panel(days), 12)
        ic = v.daily_ic(p, 'f', RET_HOLD1D)
        self.assertAlmostEqual(ic.mean(), -1.0, places=6)

    def test_sign_flipping_averages_out(self):
        """逐日符号翻转 → IC 均值 ≈ 0。

        IC 无法区分"稳定弱信号"与"符号乱跳的噪声"——这正是 walk_forward
        要按折看方向一致性的原因。
        """
        v = OOSValidator()
        days = {}
        for d in range(12):
            sign = 1.0 if d % 2 == 0 else -1.0
            days[f'2026-01-{d+1:02d}'] = {f'C{c}': sign * (c + 1) * 2.0
                                          for c in range(6)}
        p = _add_factor(_panel(days), 12)
        ic = v.daily_ic(p, 'f', RET_HOLD1D)
        self.assertGreater(len(ic), 1,
                           '必须先产出逐日 IC 序列（否则下面的 0 是空转通过）')
        self.assertAlmostEqual(ic.mean(), 0.0, places=6,
                               msg='符号逐日翻转的因子 IC 应抵消为 0')

    def test_small_cross_section_returns_empty(self):
        """单日样本 < 5 只 → 跳过（不给无意义的秩相关）"""
        v = OOSValidator()
        p = pd.DataFrame({'date': ['2026-01-01'] * 4, 'code': ['C0'] * 4,
                          'f': [1, 2, 3, 4], RET_HOLD1D: [1.0, 2.0, 3.0, 4.0]})
        self.assertTrue(v.daily_ic(p, 'f', RET_HOLD1D).empty)


class TestTailSpreadVsIc(unittest.TestCase):
    """P2-6 设计动机：尾差价差看的是"头尾"，全截面 IC 看的是整条分布"""

    @staticmethod
    def _days(ret_of, n_codes=8):
        return {f'2026-01-{d+1:02d}': {f'C{c}': ret_of(c) for c in range(n_codes)}
                for d in range(12)}

    def test_monotone_gives_ic_one_and_positive_spread(self):
        v = OOSValidator()
        p = _add_factor(_panel(self._days(lambda c: c * 1.0), codes=8), 12, codes=8)
        self.assertAlmostEqual(v.daily_ic(p, 'f', RET_HOLD1D).mean(), 1.0, places=6)
        res = v.tail_spread(p, 'f', RET_HOLD1D, k=2)
        self.assertAlmostEqual(res['k2']['mean_spread'], 6.0, places=3,
                               msg='头 6/7 → 6.0/7.0，尾 0/1 → 0.0/1.0，价差应为 6.0%')
        self.assertGreater(res['k2']['top_mean'], res['k2']['bottom_mean'])

    def test_middle_of_distribution_shrinks_ic_but_not_tail_spread(self):
        """分布中段被污染时：全截面 IC 明显下降，而头尾价差不变。

        这是 tail_spread 存在的理由（P2-6）—— 本系统只买评分 top-3，
        "能不能拉开头尾"与"整条分布是否单调"是两个口径。若两者恒等，
        这个指标就是多余的。
        """
        v = OOSValidator()
        clean = _add_factor(_panel(self._days(lambda c: c * 1.0), codes=8), 12, codes=8)
        dirty = _add_factor(_panel(self._days(lambda c: (
            c * 1.0) if c in (0, 1, 6, 7) else (1e6 if c % 2 == 0 else -1e6)),
            codes=8), 12, codes=8)
        ic_clean = v.daily_ic(clean, 'f', RET_HOLD1D).mean()
        ic_dirty = v.daily_ic(dirty, 'f', RET_HOLD1D).mean()
        self.assertGreater(ic_clean, ic_dirty,
                           f'中段污染应拉低全截面 IC（{ic_clean:+.4f} → {ic_dirty:+.4f}）')
        s_clean = v.tail_spread(clean, 'f', RET_HOLD1D, k=2)['k2']['mean_spread']
        s_dirty = v.tail_spread(dirty, 'f', RET_HOLD1D, k=2)['k2']['mean_spread']
        self.assertAlmostEqual(s_clean, s_dirty, places=9,
                               msg='头尾 4 只未被污染 → 尾差价差不应受影响')

    def test_constant_factor_gives_zero_spread(self):
        """因子当日退化为常数（如无热点命中 → 全 50）→ 价差为 0，不抛异常"""
        v = OOSValidator()
        p = _panel({f'2026-01-{d+1:02d}': {f'C{c}': (c + 1) * 2.0 for c in range(6)}
                    for d in range(12)})
        p = p.assign(f=50.0)
        res = v.tail_spread(p, 'f', RET_HOLD1D, k=2)
        self.assertAlmostEqual(res['k2']['mean_spread'], 0.0, places=6)

    def test_too_few_per_day_returns_zero_fill(self):
        """单日样本 < 2k → 跳过全部日，返回零填充而非崩溃"""
        v = OOSValidator()
        p = pd.DataFrame({'date': ['2026-01-01'] * 2, 'code': ['C0', 'C1'],
                          'f': [1.0, 2.0], RET_HOLD1D: [1.0, 2.0]})
        res = v.tail_spread(p, 'f', RET_HOLD1D, k=2)
        self.assertEqual(res['k2']['n_days'], 0)
        self.assertIsNone(res['k2']['t_stat'])


class TestIcDecayMapping(unittest.TestCase):

    def test_horizon_columns_and_none_semantics(self):
        v = OOSValidator()
        p = pd.DataFrame({'code': ['C0'] * 3,
                          'date': ['2026-01-01', '2026-01-02', '2026-01-03'],
                          'open': [10.0, 10.1, 10.2],
                          'close': [10.0, 10.2, 9.8],
                          'volume': [1e6] * 3, 'amount': [1e8] * 3})
        p = v._compute_forward_returns(p)
        p = p.assign(f=[1.0, 2.0, 3.0])
        out = v.ic_decay(p, factors=['f'], horizons=(1, 3, 5))
        self.assertIn('f', out)
        for k in ('T+1', 'T+3', 'T+5'):
            self.assertIn(k, out['f'], f'缺口径 {k}')
        self.assertIsNone(out['f']['T+5'],
                          '单日样本不足 → IC 应为 None（不是 0，也不是缺键）')


class TestFactorCorrRobustness(unittest.TestCase):

    def test_constant_factor_does_not_poison_matrix(self):
        v = OOSValidator()
        p = pd.DataFrame({
            'date': ['2026-01-01'] * 6 + ['2026-01-02'] * 6,
            'fA': [1.0] * 12,                          # 当日恒为常数（无区分度）
            'fB': [1.0, 2.0, 3.0, 4.0, 5.0, 6.0] * 2,
        }).assign(fC=[6.0, 5.0, 4.0, 3.0, 2.0, 1.0] * 2)
        mat = v.factor_corr(p, factors=['fA', 'fB', 'fC'], min_stocks=3)
        self.assertIn('fA', mat.columns)
        self.assertTrue(pd.isna(mat.loc['fA', 'fA']),
                        '常数因子的自相关应为 NaN，不得伪造 1.0')
        self.assertTrue(pd.isna(mat.loc['fA', 'fB']))
        self.assertAlmostEqual(mat.loc['fB', 'fC'], -1.0, places=3,
                               msg='fB 与 fC 完全反向，相关性应为 -1.0')

    def test_too_few_factors_returns_empty(self):
        v = OOSValidator()
        p = pd.DataFrame({'date': ['2026-01-01'] * 6, 'fA': list(range(6))})
        self.assertTrue(v.factor_corr(p, factors=['fA'], min_stocks=3).empty)


class TestWalkForward(unittest.TestCase):

    @staticmethod
    def _panel(n_days=12, codes=6):
        days = {f'2026-01-{d+1:02d}': {f'C{c}': (c + 1) * 2.0 for c in range(codes)}
                for d in range(n_days)}
        return OOSValidator(), _add_factor(_panel(days), n_days)

    def test_insufficient_days_gives_note(self):
        v, p = self._panel(n_days=3)
        out = v.walk_forward(p, factors=['f'], n_splits=3)
        self.assertIn('note', out)
        self.assertEqual(out['folds'], [])

    def test_train_always_precedes_test(self):
        v, p = self._panel(n_days=12)
        out = v.walk_forward(p, factors=['f'], n_splits=3)
        self.assertEqual(len(out['folds']), 3)
        for fd in out['folds']:
            self.assertLess(fd['train'][-1], fd['test'][0],
                            f"折 {fd['fold']} 训练段必须严格在测试段之前")

    def test_stable_flag_semantics(self):
        v, p = self._panel(n_days=12)
        out = v.walk_forward(p, factors=['f'], n_splits=3)
        d = out['factors']['f']
        self.assertEqual(len(d['fold_ics']), 3)
        self.assertTrue(d['stable'] is True, '各折 IC 全正 → stable 应为 True')

    def test_fold_ids_ordered(self):
        v, p = self._panel(n_days=12)
        out = v.walk_forward(p, factors=['f'], n_splits=3)
        self.assertEqual([fd['fold'] for fd in out['folds']], [1, 2, 3])


class TestNeutralizedIc(unittest.TestCase):

    def test_liquidity_proxy_factor_loses_ic_after_neutralizing(self):
        """原始 IC 强、控制成交额分位后 IC 消失 → 因子只是流动性代理

        构造：成交额分 3 档，收益完全由档位决定；因子在档内恒为常数
        （与档内收益无关），但跨档单调 → 原始 IC≈1.0，中性化 IC 归零。
        这是"因子是否只是规模/流动性代理"这条论证路径有效性的回归锁。
        """
        v = OOSValidator()
        rows = []
        for d in range(12):
            day = f'2026-01-{d+1:02d}'
            for bucket in range(3):
                for r in range(2):
                    rows.append({
                        'code': f'C{bucket}{r}', 'date': day,
                        'amount': (bucket + 1) * 1e8,
                        RET_HOLD1D: (bucket + 1) * 5.0,
                        'f': (bucket + 1) * 10.0,
                    })
        p = pd.DataFrame(rows)
        raw = v.daily_ic(p, 'f', RET_HOLD1D).mean()
        self.assertGreater(raw, 0.9, f'原始 IC 应为强正（实际 {raw:+.4f}）')
        neu = v.neutralized_ic(p, 'f', RET_HOLD1D, n_buckets=3, min_stocks=6)
        self.assertTrue(neu['ic'] is None or abs(neu['ic']) < 0.2,
                        f'控制成交额分位后 IC 应消失（实际 {neu}）')

    def test_missing_factor_returns_empty_stats(self):
        v = OOSValidator()
        p = pd.DataFrame({'code': ['C0'], 'date': ['2026-01-01'],
                          'amount': [1e8], RET_HOLD1D: [1.0]})
        self.assertEqual(v.neutralized_ic(p, 'not_exist', RET_HOLD1D)['n_days'], 0)


class TestTechnicalScoreVec(unittest.TestCase):

    def test_output_bounded_and_finite(self):
        """全维度向量化技术分必须落在 [0,100] 且不产生 NaN"""
        v = OOSValidator()
        rows = []
        px = 10.0
        for d in range(40):
            px *= 1.004
            rows.append({'code': 'C0', 'date': f'2026-01-{d+1:02d}',
                         'close': px, 'volume': 1e6 * (1 + 0.1 * (d % 3))})
        p = pd.DataFrame(rows)
        s = v._technical_score_vec(p, p.groupby('code', sort=False))
        self.assertFalse(s.isna().any(), f'技术分含 NaN: {s[s.isna()]}')
        self.assertGreaterEqual(s.min(), 0.0)
        self.assertLessEqual(s.max(), 100.0)

    def test_does_not_crash_on_degenerate_volume(self):
        """volume 全为 0（量比除零）时不得抛异常、不得产生 NaN"""
        v = OOSValidator()
        rows = [{'code': 'C0', 'date': f'2026-01-{d+1:02d}',
                 'close': 10.0 + 0.1 * d, 'volume': 0.0} for d in range(20)]
        p = pd.DataFrame(rows)
        s = v._technical_score_vec(p, p.groupby('code', sort=False))
        self.assertFalse(s.isna().any())
        self.assertGreaterEqual(s.min(), 0.0)
        self.assertLessEqual(s.max(), 100.0)


if __name__ == '__main__':
    unittest.main(verbosity=2)
