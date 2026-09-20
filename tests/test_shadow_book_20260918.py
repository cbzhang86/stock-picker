# -*- coding: utf-8 -*-
"""影子推荐（shadow book）配套测试（2026-09-18）

背景：极端市况（极差市停推/拥挤度断路器/动态门槛零达标）当日不落库 → 样本删失，
永远攒不出"冰点期该不该推"的直接证据。方案：照常评分、以 mode='shadow' 落库、
不下发、不进正式统计。

覆盖：
  A. `_shadow_rows_from` 提取逻辑（影子标记 / no_qualified / 正常推荐 / 空）；
  B. 三态集成：正常推荐日（写 short 不写 shadow）/ 空仓日（写 shadow 不写 short）/
     非交易日（都不写）；
  C. 批次级防重（重复运行不产生重复 shadow 行）；
  D. 只加不改：helper 对正常推荐返回空 → 不影响正式 short 写入路径。
"""
import os
import sqlite3
import sys
import tempfile
import unittest
from functools import partial

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class _StubDE:
    def get_ths_hot_stocks(self):
        return None

    def get_data_source_summary(self):
        return {}


class _StubCollector:
    def collect(self, **kwargs):
        return None


class TestShadowRowsHelper(unittest.TestCase):

    def test_shadow_tagged_recs(self):
        from scripts.eod_stock_picker import _shadow_rows_from
        recs = [{'code': '600001', 'score': 70, 'shadow': True},
                {'code': '600002', 'score': 68, 'shadow': True}]
        rows = _shadow_rows_from(recs)
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]['code'], '600001')

    def test_no_qualified_meta(self):
        """P1-1 修键名（2026-09-20 审查）：生产键是 top_candidate；
        top_unqualified 仅作历史兼容回退（单独用例覆盖）"""
        from scripts.eod_stock_picker import _shadow_rows_from
        meta = [{'no_qualified': True,
                 'top_candidate': {'code': '600003', 'name': '松发股份', 'score': 66.8}}]
        rows = _shadow_rows_from(meta)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['code'], '600003')
        self.assertAlmostEqual(rows[0]['score'], 66.8)

    def test_no_qualified_fallback_legacy_key(self):
        """历史兼容：旧快照/旧调用方仍写 top_unqualified 也能提取（不丢失）"""
        from scripts.eod_stock_picker import _shadow_rows_from
        meta = [{'no_qualified': True,
                 'top_unqualified': {'code': '600003', 'name': '松发股份', 'score': 66.8}}]
        rows = _shadow_rows_from(meta)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['code'], '600003')

    def test_normal_recs_return_empty(self):
        """正常推荐（无 shadow 标记、无 no_qualified）→ 不写 shadow（只加不改）"""
        from scripts.eod_stock_picker import _shadow_rows_from
        self.assertEqual(_shadow_rows_from([{'code': '600001', 'score': 80}]), [])

    def test_empty_and_skip_reason_only(self):
        from scripts.eod_stock_picker import _shadow_rows_from
        self.assertEqual(_shadow_rows_from([]), [])
        # 旧式 skip_reason 元信息（无候选）→ 空（不回退，避免写空记录）
        self.assertEqual(_shadow_rows_from([{'skip_reason': '市场极差'}]), [])
        self.assertEqual(_shadow_rows_from([{'no_qualified': True}]), [])


class TestShadowIntegration(unittest.TestCase):
    """三态 + 防重：直接驱动 run_short_term（stub 策略/采集器/数据源）"""

    def setUp(self):
        fd, self.db = tempfile.mkstemp(suffix='.db')
        os.close(fd)
        os.unlink(self.db)          # 让 tracker 自行建库
        from feedback.tracker import PredictionTracker
        self._PT = PredictionTracker

    def tearDown(self):
        for suffix in ('', '-wal', '-shm'):
            p = self.db + suffix
            if os.path.exists(p):
                try:
                    os.unlink(p)
                except OSError:
                    pass

    def _run(self, recs, trading_day=True):
        from scripts import eod_stock_picker as eod
        import core.trading_calendar as cal

        class _StubStrategy:
            def __init__(self, cfg):
                self.config = cfg
                self.data_engine = _StubDE()
                self._last_enriched = []

            def run(self, *a, **k):
                return recs

        orig = (eod.ShortTermStrategy, eod.FactorDataCollector, eod.PredictionTracker,
                cal.is_trading_day)
        eod.ShortTermStrategy = _StubStrategy
        eod.FactorDataCollector = _StubCollector
        eod.PredictionTracker = partial(self._PT, db_path=self.db)
        cal.is_trading_day = lambda d: trading_day
        try:
            out = eod.run_short_term({'short_term': {'shadow_enabled': True}})
        finally:
            (eod.ShortTermStrategy, eod.FactorDataCollector, eod.PredictionTracker,
             cal.is_trading_day) = orig
        return out

    def _counts(self):
        conn = sqlite3.connect(self.db)
        try:
            try:
                short = conn.execute(
                    "SELECT COUNT(*) FROM predictions WHERE mode='short'").fetchone()[0]
                shadow = conn.execute(
                    "SELECT COUNT(*) FROM predictions WHERE mode='shadow'").fetchone()[0]
            except sqlite3.OperationalError:
                return 0, 0        # 库未建表（未发生任何写入）
        finally:
            conn.close()
        return short, shadow

    # ── B1 正常推荐日 ──
    def test_normal_day_writes_short_not_shadow(self):
        self._run([{'code': '600001', 'name': 'a', 'score': 80, 'rating': '增持',
                    'price': 10.0, 'breakdown': {}}])
        short, shadow = self._counts()
        self.assertEqual(short, 1, '正常日应写 mode=short')
        self.assertEqual(shadow, 0, '正常日不得写 shadow')

    # ── B2 空仓日（no_qualified）──
    def test_zero_qualified_day_writes_shadow_only(self):
        self._run([{'no_qualified': True, 'min_score': 70,
                    'top_candidate': {'code': '600003', 'name': 'x', 'score': 66.8}}])
        short, shadow = self._counts()
        self.assertEqual(short, 0, '零达标日不得写 short')
        self.assertEqual(shadow, 1, '零达标日应写 shadow')

    # ── B2b 影子模式（极差市/断路器触发但完成评分）──
    def test_shadow_mode_day_writes_shadow_only(self):
        self._run([{'code': '600004', 'name': 'y', 'score': 72, 'rating': '',
                    'price': 12.0, 'breakdown': {}, 'shadow': True,
                    'shadow_reason': '市场赚钱效应较差(极差市)'}])
        short, shadow = self._counts()
        self.assertEqual(short, 0)
        self.assertEqual(shadow, 1)

    # ── B3 非交易日 ──
    def test_non_trading_day_writes_nothing(self):
        self._run([{'no_qualified': True,
                    'top_candidate': {'code': '600005', 'name': 'z', 'score': 60}}],
                  trading_day=False)
        short, shadow = self._counts()
        self.assertEqual((short, shadow), (0, 0), '非交易日不得写入任何记录')

    # ── C 防重 ──
    def test_shadow_dedup_on_rerun(self):
        recs = [{'no_qualified': True,
                 'top_candidate': {'code': '600006', 'name': 'w', 'score': 65}}]
        self._run(recs)
        self._run(recs)                       # 同日重复运行
        short, shadow = self._counts()
        self.assertEqual(shadow, 1, '同日重复运行应保持 1 条 shadow（批次级防重）')


if __name__ == '__main__':
    unittest.main()
