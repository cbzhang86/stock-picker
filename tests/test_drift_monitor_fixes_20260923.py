# -*- coding: utf-8 -*-
"""交接单（2026-09-23）DriftMonitor 修复的守卫测试（D1~D8）。

全部使用临时 sqlite 库，不触碰生产数据。
"""
import io
import os
import sqlite3
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from core.drift_monitor import (DriftMonitor, PSI_BINS, MEAN_SHIFT_FLOOR,
                                MEAN_SHIFT_REL)  # noqa: E402


def _make_db(path, days_ret=None, n_pred_rows=0, with_outcomes=True):
    """days_ret: {date_str: [ret, ...]}；n_pred_rows: 额外插入无 outcome 的预测行。"""
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE predictions (id INTEGER PRIMARY KEY, "
                 "date TEXT, code TEXT, mode TEXT)")
    conn.execute("CREATE TABLE outcomes (prediction_id INTEGER, t1_return REAL)")
    for d, rets in (days_ret or {}).items():
        for r in rets:
            cur = conn.execute(
                "INSERT INTO predictions (date, code, mode) VALUES (?,?,?)",
                (d, '600000', 'short'))
            if with_outcomes:
                conn.execute("INSERT INTO outcomes VALUES (?,?)", (cur.lastrowid, r))
    for i in range(n_pred_rows):
        conn.execute("INSERT INTO predictions (date, code, mode) VALUES (?,?,?)",
                     ('2026-09-22', '600000', 'short'))
    conn.commit()
    conn.close()


def _two_window_db(path, n_early=20, ret_early=1.0, n_late=20, ret_late=1.0,
                   n_rec_per_day=1):
    d = {}
    for i in range(n_early):
        d[f"2026-06-{i + 1:02d}"] = [ret_early] * n_rec_per_day
    for i in range(n_late):
        d[f"2026-08-{i + 1:02d}"] = [ret_late] * n_rec_per_day
    _make_db(path, d)
    return path


class TestD2WindowConsistency(unittest.TestCase):
    """D2：门槛 40 与声明窗口 80 的落差必须显式可见（原先静默截短）。"""

    def test_window_not_full_is_disclosed(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = _two_window_db(os.path.join(tmp, 'p.db'), n_early=20, ret_early=1.0,
                                n_late=20, ret_late=1.0)
            dm = DriftMonitor(db_path=db, baseline_days=60, recent_days=20)
            r = dm.check()
            self.assertFalse(r['window_ok'], '40 天样本 < 声明 80 天 → window_ok=False')
            self.assertIn('基准窗口未满', r['note'])
            self.assertEqual(r['window_declared'], 80)
            self.assertEqual(r['n_baseline'], 20)

    def test_window_ok_when_full(self):
        with tempfile.TemporaryDirectory() as tmp:
            d = {}
            for i in range(90):
                d[f"2026-{6 + i // 30}-{i % 30 + 1:02d}"] = [1.0]
            db = os.path.join(tmp, 'p.db')
            _make_db(db, d)
            r = DriftMonitor(db_path=db, baseline_days=60, recent_days=20).check()
            self.assertTrue(r['window_ok'])
            self.assertEqual(r['n_baseline'], 60)


class TestD3PerTradePsi(unittest.TestCase):
    """D3：PSI 用逐笔样本（不再被日度均值平均掉）。"""

    def test_trade_counts_exceed_day_counts(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = _two_window_db(os.path.join(tmp, 'p.db'), n_early=20, n_late=20,
                                n_rec_per_day=3)
            r = DriftMonitor(db_path=db, baseline_days=60, recent_days=20).check()
            self.assertEqual(r['n_recent'], 20)
            self.assertEqual(r['n_recent_trades'], 60,
                             '逐笔样本应为 20 天 × 3 笔')
            self.assertEqual(r['n_baseline_trades'], 60)


class TestD5MeanShiftThreshold(unittest.TestCase):
    """D5：mean_shift 相对阈值（交接单两个场景）。"""

    def test_no_alert_when_baseline_high(self):
        """基准 +3% / 近期 +2%：下移 1.0pp 但相对基准很小 → 不 ALERT。"""
        with tempfile.TemporaryDirectory() as tmp:
            db = _two_window_db(os.path.join(tmp, 'p.db'), n_early=20, ret_early=3.0,
                                n_late=20, ret_late=2.0)
            r = DriftMonitor(db_path=db, baseline_days=60, recent_days=20).check()
            self.assertNotEqual(r['verdict'], 'ALERT',
                                f"高基准下的 1pp 下移不应 ALERT（阈值 "
                                f"{r.get('mean_shift_threshold')}）")

    def test_alert_when_baseline_near_zero(self):
        """基准 -0.5% / 近期 -1.5%：下移 1.0pp = 亏损三倍 → ALERT。"""
        with tempfile.TemporaryDirectory() as tmp:
            db = _two_window_db(os.path.join(tmp, 'p.db'), n_early=20, ret_early=-0.5,
                                n_late=20, ret_late=-1.5)
            r = DriftMonitor(db_path=db, baseline_days=60, recent_days=20).check()
            self.assertEqual(r['verdict'], 'ALERT',
                             f"亏损翻三倍必须 ALERT（阈值 "
                             f"{r.get('mean_shift_threshold')}）")

    def test_threshold_formula(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = _two_window_db(os.path.join(tmp, 'p.db'), n_early=20, ret_early=4.0,
                                n_late=20, ret_late=4.0)
            r = DriftMonitor(db_path=db, baseline_days=60, recent_days=20).check()
            expect = max(MEAN_SHIFT_FLOOR, MEAN_SHIFT_REL * 4.0)
            self.assertAlmostEqual(r['mean_shift_threshold'], round(expect, 3), places=3)


class TestD6VerdictStates(unittest.TestCase):
    """D6：NO_SAMPLE / PENDING_BACKFILL / INSUFFICIENT 三态可区分。"""

    def test_no_sample_when_predictions_empty(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = os.path.join(tmp, 'p.db')
            _make_db(db)
            self.assertEqual(DriftMonitor(db_path=db).check()['verdict'], 'NO_SAMPLE')

    def test_pending_backfill_when_no_outcomes(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = os.path.join(tmp, 'p.db')
            _make_db(db, n_pred_rows=3, with_outcomes=False)
            r = DriftMonitor(db_path=db).check()
            self.assertEqual(r['verdict'], 'PENDING_BACKFILL')
            self.assertIn('回填', r['note'])

    def test_insufficient_when_partial(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = _two_window_db(os.path.join(tmp, 'p.db'), n_early=5, n_late=5)
            r = DriftMonitor(db_path=db, baseline_days=60, recent_days=20).check()
            self.assertEqual(r['verdict'], 'INSUFFICIENT')

    def test_missing_db_stays_insufficient(self):
        """库不存在属"未知"，不得误报为 NO_SAMPLE（沿用既有语义）。"""
        r = DriftMonitor(db_path=os.path.join(tempfile.gettempdir(),
                                              'no_such_db_drift.db')).check()
        self.assertEqual(r['verdict'], 'INSUFFICIENT')


class TestD1CoverageVisibility(unittest.TestCase):
    """D1：覆盖率可见化（监控冻结必须能被看见）。"""

    def test_coverage_fields_present(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = _two_window_db(os.path.join(tmp, 'p.db'), n_early=20, n_late=20)
            r = DriftMonitor(db_path=db, baseline_days=60, recent_days=20).check()
            self.assertIn('n_trading_days', r)
            self.assertIn('coverage_ratio', r)
            self.assertGreater(r['n_trading_days'], 0)
            self.assertLessEqual(r['coverage_ratio'], 1.0)

    def test_coverage_shown_in_insufficient_note(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = _two_window_db(os.path.join(tmp, 'p.db'), n_early=5, n_late=5)
            r = DriftMonitor(db_path=db, baseline_days=60, recent_days=20).check()
            self.assertIn('样本覆盖', r['note'])


class TestD4Bins(unittest.TestCase):
    """D4：中心加密（原 -2~2 每 0.5 一档，实际仅用 3~4 箱）。"""

    def test_center_is_denser(self):
        center = [b for b in PSI_BINS if -1.0 <= b <= 1.0]
        self.assertGreaterEqual(len(center), 4,
                                f'中心区分辨率不足: {PSI_BINS}')
        self.assertEqual(PSI_BINS[0], -100.0)
        self.assertEqual(PSI_BINS[-1], 100.0)

    def test_psi_still_sane(self):
        s = [1.0, 2.0, 3.0] * 10
        self.assertEqual(DriftMonitor._psi(s, s), 0.0)
        self.assertGreater(DriftMonitor._psi([0.0] * 10, [3.0] * 10), 0.25)


class TestD7Wiring(unittest.TestCase):
    """D7：结论接入 evaluate_all（WARN 不阻断）与简报尾部。"""

    def test_evaluate_all_has_drift_gate(self):
        src = io.open(os.path.join(ROOT, 'scripts', 'evaluate_all.py'),
                      encoding='utf-8').read()
        self.assertIn('_drift_gate()', src)
        self.assertIn('绩效漂移监控', src)

    def test_briefing_appends_drift_state(self):
        src = io.open(os.path.join(ROOT, 'scripts', 'eod_stock_picker.py'),
                      encoding='utf-8').read()
        self.assertIn('绩效漂移: ', src,
                      '简报需追加漂移状态行（原只写 JSON+日志）')
        self.assertIn("'NO_SAMPLE', 'INSUFFICIENT'", src,
                      'D6 新 verdict 需在 eod 分支中可见（提到 warning）')


if __name__ == '__main__':
    unittest.main()
