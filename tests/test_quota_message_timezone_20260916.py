# -*- coding: utf-8 -*-
"""回归测试：配额文案口径与 created_at 时区（2026-09-16 P2-3）

背景（两处都是"数值正确但口径易误读"，代价是误导排查方向）：
  1. 配额告警文案写"日配额已用完(100次/天)"，而真实闸门是本地预留策略
     budget-10=90。看到该文案会误判为服务端 429 限流，转而去查上游状态。
  2. predictions.created_at 由建表默认 `CURRENT_TIMESTAMP` 写入，SQLite 恒为
     UTC（实测 07:02 = 北京时间 15:02），与全工程北京时间日志混用。

本文件锁定：
  A. 配额告警必须写明"本地安全闸门"及真实闸门数值，不得再出现"100次/天"；
  B. 新写入的 created_at 必须与北京时间一致（不依赖宿主机时区）；
  C. 时区修复不得破坏原有写入字段（date/code/score 等）。
"""
import os
import shutil
import sqlite3
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from core.trading_calendar import beijing_now  # noqa: E402


class TestQuotaMessageWording(unittest.TestCase):
    """A. 文案口径"""

    def test_message_says_local_gate_not_server_quota(self):
        from core.data_engine import DataEngine
        de = DataEngine.__new__(DataEngine)
        de._asharehub_budget = 100
        de._source_available = {}
        de._source_status = {
            k: {'available': True, 'last_error': None, 'label': k}
            for k in ('asharehub_moneyflow', 'asharehub_tech_factors',
                      'asharehub_concepts', 'asharehub_financial')
        }
        DataEngine._mark_asharehub_quota_exhausted(de)
        msg = de._source_status['asharehub_moneyflow']['last_error']
        self.assertIn('本地安全闸门', msg)
        self.assertIn('90/100', msg)
        self.assertNotIn('100次/天', msg,
                         '不得再声称"日配额已用完(100次/天)"，会误导为服务端限流')
        for k in ('asharehub_moneyflow', 'asharehub_tech_factors',
                  'asharehub_concepts', 'asharehub_financial'):
            self.assertFalse(de._source_available[k], '四个源必须同时熔断')

    def test_gate_follows_budget(self):
        """闸门必须随预算变化，不能硬编码 90"""
        from core.data_engine import DataEngine
        de = DataEngine.__new__(DataEngine)
        de._asharehub_budget = 50
        de._source_available = {}
        de._source_status = {'asharehub_concepts': {'available': True,
                                                    'last_error': None,
                                                    'label': 'x'}}
        DataEngine._mark_asharehub_quota_exhausted(de)
        self.assertIn('40/50', de._source_status['asharehub_concepts']['last_error'])


class TestPredictionCreatedAtTimezone(unittest.TestCase):
    """B/C. created_at 时区"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='tracker_tz_')
        self.db = os.path.join(self.tmp, 'predictions.db')

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _log(self):
        from feedback.tracker import PredictionTracker
        t = PredictionTracker(db_path=self.db)
        return t.log_prediction(date='2026-09-16', code='600163', name='中闽能源',
                                mode='short', score=65.06, rating='增持',
                                buy_price=6.28, model_version='v1',
                                factor_scores={'hot_theme': 65})

    def test_created_at_matches_beijing_time(self):
        self._log()
        conn = sqlite3.connect(self.db)
        row = conn.execute(
            "SELECT created_at FROM predictions").fetchone()
        conn.close()
        stored = datetime.strptime(row[0], '%Y-%m-%d %H:%M:%S')
        delta = abs((stored - beijing_now()).total_seconds())
        self.assertLess(delta, 60,
                        f'created_at({row[0]}) 与北京时间偏差 {delta:.0f}s，'
                        f'疑似仍写入 UTC')

    def test_created_at_not_utc_when_host_tz_differs(self):
        """宿主机非 UTC+8 时，必须与"朴素 datetime.now()"区分开（锁定回归）"""
        self._log()
        conn = sqlite3.connect(self.db)
        stored = conn.execute(
            "SELECT created_at FROM predictions").fetchone()[0]
        # 对照：SQLite CURRENT_TIMESTAMP 的口径（UTC naive）
        sqlite_now = conn.execute(
            "SELECT strftime('%Y-%m-%d %H:%M:%S','now')").fetchone()[0]
        conn.close()
        utc_naive = datetime.now(timezone.utc).replace(tzinfo=None)
        # 写入值应与北京时间一致，而不是与 UTC 一致
        beijing_delta = abs(
            (datetime.strptime(stored, '%Y-%m-%d %H:%M:%S') - beijing_now()).total_seconds())
        utc_delta = abs((datetime.strptime(stored, '%Y-%m-%d %H:%M:%S')
                         - utc_naive).total_seconds())
        self.assertLess(beijing_delta, 60)
        if abs((beijing_now() - utc_naive).total_seconds()) > 60:
            # 本机时区非 UTC+8 时，两者必然不同
            self.assertGreater(utc_delta, 60)
        self.assertIsNotNone(sqlite_now)

    def test_other_fields_written_unchanged(self):
        pid = self._log()
        self.assertIsInstance(pid, int)
        conn = sqlite3.connect(self.db)
        cols = [r[1] for r in conn.execute('PRAGMA table_info(predictions)')]
        row = dict(zip(cols, conn.execute(
            "SELECT * FROM predictions WHERE id=?", (pid,)).fetchone()))
        conn.close()
        self.assertEqual(row['date'], '2026-09-16')
        self.assertEqual(row['code'], '600163')
        self.assertEqual(row['name'], '中闽能源')
        self.assertEqual(row['mode'], 'short')
        self.assertAlmostEqual(row['score'], 65.06)
        self.assertAlmostEqual(row['buy_price'], 6.28)
        self.assertEqual(row['model_version'], 'v1')
        self.assertIn('hot_theme', row['factor_scores'])

    def test_batch_dedup_still_works(self):
        """时区修复不得影响去重键（date, mode）的语义"""
        from feedback.tracker import PredictionTracker
        t = PredictionTracker(db_path=self.db)
        self.assertFalse(t.has_predictions('2026-09-16', 'short'))
        self._log()
        self.assertTrue(t.has_predictions('2026-09-16', 'short'))
        self.assertFalse(t.has_predictions('2026-09-17', 'short'))


if __name__ == '__main__':
    unittest.main()
