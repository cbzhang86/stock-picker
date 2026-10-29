# -*- coding: utf-8 -*-
"""第二轮回归发现缺陷的守卫测试（2026-09-22）。

覆盖：
  1. BacktestStore.get_run 返回的 dict 必须用**列名**做键
     （存量 P1：PRAGMA table_info 取 d[0]=cid → 键全是整数 →
      compare_runs / 报告层所有 result.get(...) 恒取默认值 → 指标对比恒 0）。
  2. compare_runs 能读到 config_snapshot 里的 sizing_mode 并在口径不一致时给出阻断性警告。
  3. daily_job/tracker 的北京时区回填窗口（此前 NameError 走了兜底分支，等于没修）。
  4. eod_stock_picker 长线模式把 run_long_term 结果透传给简报（P1-12 修复的接线完整性）。
"""
import io
import os
import re
import sqlite3
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)


class TestBacktestStoreGetRun(unittest.TestCase):
    """get_run 的键必须是列名（回归：PRAGMA d[0] → 整数键）。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        from core.backtest_store import BacktestStore
        self.store = BacktestStore(db_path=os.path.join(self.tmp.name, 'bt.db'))
        self._ensure_schema()

    def tearDown(self):
        self.tmp.cleanup()

    def _ensure_schema(self):
        """BacktestStore 若无建表逻辑，则手工建最小表。"""
        conn = sqlite3.connect(self.store.db_path)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS backtest_runs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                strategy_name TEXT, mode TEXT,
                start_date TEXT, end_date TEXT,
                weight_snapshot TEXT, config_snapshot TEXT,
                total_trading_days INTEGER, total_trades INTEGER,
                win_rate REAL, avg_return_t1 REAL, avg_return_t5 REAL,
                max_drawdown REAL, sharpe_ratio REAL,
                benchmark_return REAL, strategy_return REAL, excess_return REAL,
                factor_performance TEXT
            )
        """)
        conn.execute("""
            INSERT INTO backtest_runs (strategy_name, mode, start_date, end_date,
                weight_snapshot, config_snapshot, total_trading_days, total_trades,
                win_rate, avg_return_t1, avg_return_t5, max_drawdown, sharpe_ratio,
                benchmark_return, strategy_return, excess_return, factor_performance)
            VALUES ('short_strategy','short','2026-09-07','2026-09-18',
                '{"short":{"hot_theme":0.55}}',
                '{"sizing_mode":"normalized","slippage":0.001}',
                10, 2, 50.0, -0.61, -1.80, -1.66, 0.5, 7.49, 2.50, 5.01, '{}')
        """)
        # 第二条：absolute 口径（用于 compare_runs 口径警告）
        conn.execute("""
            INSERT INTO backtest_runs (strategy_name, mode, start_date, end_date,
                weight_snapshot, config_snapshot, total_trading_days, total_trades,
                win_rate, avg_return_t1, avg_return_t5, max_drawdown, sharpe_ratio,
                benchmark_return, strategy_return, excess_return, factor_performance)
            VALUES ('short_strategy','short','2026-09-07','2026-09-18',
                '{"short":{"hot_theme":0.55}}',
                '{"sizing_mode":"absolute","slippage":0.001}',
                10, 2, 50.0, -0.61, -1.80, -1.66, 0.5, 7.49, 2.50, 5.01, '{}')
        """)
        conn.commit()
        conn.close()

    def test_get_run_keys_are_column_names(self):
        r = self.store.get_run(1)
        self.assertIsNotNone(r, 'get_run 应能取到记录')
        for key in ('strategy_name', 'win_rate', 'total_trades', 'config_snapshot',
                    'weight_snapshot', 'sizing_mode_guard'):
            if key == 'sizing_mode_guard':
                continue
            self.assertIn(key, r,
                          f'get_run 返回的 dict 缺少列名键 {key!r} —— '
                          f'若键是整数说明 PRAGMA table_info 又取了 d[0]')
        self.assertEqual(r['strategy_name'], 'short_strategy')
        self.assertEqual(r['total_trades'], 2)

    def test_get_run_parses_config_snapshot(self):
        r = self.store.get_run(1)
        self.assertIsInstance(r.get('config_snapshot'), dict,
                              'config_snapshot 应被解析为 dict（否则 compare_runs 读不到 sizing_mode）')
        self.assertEqual(r['config_snapshot'].get('sizing_mode'), 'normalized')

    def test_compare_runs_flags_sizing_mode_mismatch(self):
        txt = self.store.compare_runs(1, 2)
        self.assertIn('仓位口径不一致', txt,
                      'sizing_mode 不同（normalized vs absolute）时必须给出不可比警告')


class TestTimezoneAwareSince(unittest.TestCase):
    """tracker 的 since 日期必须是北京时区（此前 NameError → 恒走 UTC 兜底）。"""

    def test_tracker_source_uses_timedelta_import(self):
        src = io.open(os.path.join(ROOT, 'feedback', 'tracker.py'),
                      encoding='utf-8').read()
        # try 分支里必须显式导入 timedelta（不能只导入 beijing_now）
        m = re.search(
            r"from core\.trading_calendar import beijing_now, timedelta",
            src)
        self.assertIsNotNone(
            m, 'tracker.py 中 `from core.trading_calendar import` 必须同时导入 '
               'timedelta，否则回填窗口计算 NameError 后静默退回 UTC 兜底')

    def test_backfill_pending_importable_as_module(self):
        """修复点：backfill_pending 作为模块导入不得 ModuleNotFoundError。"""
        try:
            if 'scripts' not in sys.path:
                sys.path.insert(0, os.path.join(ROOT, 'scripts'))
            import importlib
            mod = importlib.import_module('backfill_pending')
            self.assertTrue(hasattr(mod, 'backfill_pending_outcomes'))
        except ModuleNotFoundError as e:
            self.fail(f'scripts/backfill_pending.py 作为模块导入失败: {e}')


class TestBriefingLongRecsWiring(unittest.TestCase):
    """P1-12 修复的接线完整性：eod 长线模式必须把 run_long_term 结果透传给简报。"""

    def test_eod_passes_long_recs(self):
        src = io.open(os.path.join(ROOT, 'scripts', 'eod_stock_picker.py'),
                      encoding='utf-8').read()
        self.assertRegex(
            src,
            r"long_recs\s*=\s*\(?recommendations\s+if\s+args\.mode\s*==\s*'long'",
            'eod_stock_picker 调用 generate_market_briefing 时应透传 '
            'long_recs=recommendations if args.mode=="long"，否则简报会内联重跑长线'
            '（P1-12 修复只完成一半）')


if __name__ == '__main__':
    unittest.main()
