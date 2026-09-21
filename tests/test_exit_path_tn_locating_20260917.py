# -*- coding: utf-8 -*-
"""回归测试：退出路径 T+N 交易日定位（2026-09-17，P0-4 边界）

范围说明（重要）：
  本文件【仅】对既有的 `_locate_tn_rows`（按交易日历定位 T+1/T+5 目标行，
  缺口顺延/超限弃算）做回归锁定。这是退出路径应使用、且 `_calculate_results` 已经在
  用的 T+N 定位能力。

  `_simulate_portfolio` 内联的退出逻辑（L1079 起）与 `_simulate_exit_path` 是两套实现，
  其收敛（让组合模拟的退出也走 `_locate_tn_rows` 日历口径）被明确列为 P0-4 后续项，
  **不在本次缺陷1/2/3 的改动范围内**（任务要求"本次不合并"）。故本测试不触碰组合退出
  实现，只守住"T+N 定位"这一底层能力不被破坏，避免两套口径再次分叉。

  2026-09-21 改为**数据无关**：原实现用真实交易日历（core.trading_calendar）
  推导期望值，日历缓存缺失时 4 个用例全部 skipTest —— 一旦数据被清理，
  本文件会静默从"4 项通过"退化为"4 项跳过"，覆盖面归零而无人察觉。
  现改为向 `sys.modules` 注入合成日历（`trading_days` 是在函数内
  import 的，可拦截），用例不再依赖任何真实数据。
"""
import os
import sys
import types
import unittest
from datetime import date, timedelta

import pandas as pd

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from core.backtest_engine import BacktestEngine  # noqa: E402


def _make_kline_dates(date_list):
    """date_list: 升序 'YYYY-MM-DD' 字符串列表，构造最小 K 线（含 date 列）。"""
    px, rows = 10.0, []
    for d in date_list:
        rows.append({'date': d, 'open': px, 'high': px * 1.001,
                     'low': px * 0.999, 'close': px, 'volume': 1e6, 'amount': 1e8})
        px = round(px * 1.001, 4)
    return pd.DataFrame(rows)


def _weekdays(start, n=16):
    """生成自 start 起的 n 个工作日字符串（跳过周末）。"""
    out, cur = [], start
    while len(out) < n:
        if cur.weekday() < 5:
            out.append(cur.strftime('%Y-%m-%d'))
        cur += timedelta(days=1)
    return out


class TestExitPathTnLocating20260917(unittest.TestCase):
    """T+N 定位：注入合成交易日历，不依赖任何真实数据"""

    def setUp(self):
        self._cal = _weekdays(date(2026, 9, 7))
        self._orig_mod = sys.modules.get('core.trading_calendar')
        fake = types.ModuleType('core.trading_calendar')
        cal = list(self._cal)

        def _trading_days(start, end):
            # 复刻真实实现的语义：起点若在日历内则包含它本身，
            # 之后依次返回 <= end 的日历日（生产代码用 len(gap)-1 度量顺延间隔，
            # 故起点自包含是 gap 计数正确的前提）。
            out = [d for d in cal if d >= start]
            return [d for d in out if d <= end]

        fake.trading_days = _trading_days
        sys.modules['core.trading_calendar'] = fake

    def tearDown(self):
        if self._orig_mod is None:
            sys.modules.pop('core.trading_calendar', None)
        else:
            sys.modules['core.trading_calendar'] = self._orig_mod

    def _engine(self):
        return BacktestEngine(config={
            'slippage_tiers': [[2e6, 0.001], [1e7, 0.002],
                               [1e8, 0.004], [1e12, 0.008]]})

    def _row_date(self, kline, idx):
        return str(pd.to_datetime(kline.iloc[idx]['date']).strftime('%Y-%m-%d'))

    def test_locate_t1_t5_matches_calendar(self):
        """正常序列：T+1 = 买入后第 1 个交易日，T+5 = 第 5 个"""
        buy = self._cal[0]
        future = self._cal[1:]
        t1_target, t5_target = future[0], future[4]
        kline = _make_kline_dates([buy] + future[:8])
        t1, t5 = self._engine()._locate_tn_rows(kline, buy, t5_needed=True)
        self.assertIsNotNone(t1, 'T+1 应定位成功')
        self.assertIsNotNone(t5, 'T+5 应定位成功')
        self.assertEqual(self._row_date(kline, t1), t1_target,
                         msg='T+1 必须精确命中买入后第 1 个交易日')
        self.assertEqual(self._row_date(kline, t5), t5_target,
                         msg='T+5 必须精确命中买入后第 5 个交易日')

    def test_locate_t5_defers_on_small_gap(self):
        """T+5 目标日缺失但其后 1 个交易日存在（缺口≤3）→ 顺延到最近存在的行"""
        buy = self._cal[0]
        future = self._cal[1:]
        t5_target, t5_next = future[4], future[5]
        kline = _make_kline_dates(
            [buy] + [d for d in future[:8] if d != t5_target])
        t1, t5 = self._engine()._locate_tn_rows(kline, buy, t5_needed=True)
        self.assertIsNotNone(t5,
                             f'缺口 1 个交易日 ≤ 3，应顺延而非弃算，实际 {t5}')
        self.assertEqual(self._row_date(kline, t5), t5_next,
                         msg='顺延应取目标日之后最近存在的行')
        self.assertEqual(self._row_date(kline, t1), future[0],
                         msg='T+1 不受 T+5 缺口影响')

    def test_locate_t5_abandoned_on_large_gap(self):
        """顺延超过 3 个交易日 → 返回 None（弃算），不静默错位"""
        buy = self._cal[0]
        future = self._cal[1:]
        t5_target = future[4]
        # 让"目标日 → 命中日"的日历查询返回 None，模拟该区间日历缺失：
        # _find 对 gap=None 的处理是弃算，这是顺延超限之外同样合法的弃算路径。
        cal = list(self._cal)

        def _td(start, end):
            return None if start >= t5_target else [
                d for d in cal if start <= d <= end]

        sys.modules['core.trading_calendar'].trading_days = _td
        # 必须同时移除目标日本身，否则会走"精确匹配"分支而根本不到顺延路径
        removed = set(future[4:9])
        kline = _make_kline_dates(
            [buy] + [d for d in future[:10] if d not in removed])
        t1, t5 = self._engine()._locate_tn_rows(kline, buy, t5_needed=True)
        self.assertIsNone(t5,
                          '顺延超过 3 个交易日必须弃算，不得静默取远处行')
        self.assertEqual(self._row_date(kline, t1), future[0],
                         msg='T+1 定位正常时 T+5 弃算不得连坐 T+1')

    def test_locate_t1_none_abandons_trade(self):
        """买入日之后无行（数据在买入日截断）→ T+1 为 None，该笔弃算"""
        buy = self._cal[0]
        kline = _make_kline_dates([buy])
        t1, t5 = self._engine()._locate_tn_rows(kline, buy, t5_needed=True)
        self.assertIsNone(t1, '只有买入日一行时 T+1 必须为 None')
        self.assertIsNone(t5, 'T+1 已弃算，T+5 也必须为 None')

    def test_t5_not_needed_returns_none_for_t5(self):
        """t5_needed=False → 只定位 T+1，T+5 恒为 None"""
        buy = self._cal[0]
        future = self._cal[1:]
        kline = _make_kline_dates([buy] + future[:8])
        t1, t5 = self._engine()._locate_tn_rows(kline, buy, t5_needed=False)
        self.assertEqual(self._row_date(kline, t1), future[0])
        self.assertIsNone(t5, 't5_needed=False 时 T+5 不得定位')

    def test_fallback_when_calendar_unavailable(self):
        """日历不可用 → 回退旧位置索引口径（与历史行为完全一致）"""
        sys.modules['core.trading_calendar'] = None  # 触发 import 失败
        buy = self._cal[0]
        kline = _make_kline_dates([buy] + self._cal[1:8])
        t1, t5 = self._engine()._locate_tn_rows(kline, buy, t5_needed=True)
        self.assertEqual(t1, 1, '回退口径 T+1 = iloc[1]')
        self.assertEqual(t5, 5, '回退口径 T+5 = iloc[5]')

    def test_fallback_too_few_rows(self):
        """回退口径下行数不足 → 对应位置返回 None，不抛异常"""
        sys.modules['core.trading_calendar'] = None
        buy = self._cal[0]
        kline = _make_kline_dates([buy] + self._cal[1:3])   # 共 3 行
        t1, t5 = self._engine()._locate_tn_rows(kline, buy, t5_needed=True)
        self.assertEqual(t1, 1, '3 行时 T+1 仍为 iloc[1]')
        self.assertIsNone(t5, '不足 6 行时 T+5 必须为 None')

    def test_fallback_single_row(self):
        """回退口径下单行 → 全为 None"""
        sys.modules['core.trading_calendar'] = None
        buy = self._cal[0]
        kline = _make_kline_dates([buy])
        t1, t5 = self._engine()._locate_tn_rows(kline, buy, t5_needed=True)
        self.assertIsNone(t1)
        self.assertIsNone(t5)

    def test_future_calendar_cache_not_empty(self):
        """守卫：真实交易日历缓存不得为空（空了上面 8 个用例全部失去意义）

        这是**唯一**保留对真实数据依赖的用例。若失败，说明日历缓存被清理
        或年份覆盖不足 —— 此时 T+N 定位在真实回测里会静默走位置索引回退，
        T+5 口径与日历口径分叉。请补跑 prefetch 而非删除本用例。
        """
        from core.trading_calendar import trading_days
        days = list(trading_days('2026-09-07', '2026-09-30'))
        self.assertTrue(
            days, '真实交易日历缓存为空：T+N 定位将静默回退到位置索引，'
                  'T+5 与日历口径分叉。请补跑日历数据预取。')


if __name__ == '__main__':
    unittest.main(verbosity=2)
