# -*- coding: utf-8 -*-
"""predictions.db 备份脚本测试（2026-09-18 审查 P1-3）"""
import os
import sqlite3
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

import scripts.backup_predictions as bp  # noqa: E402


class TestBackup(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.src = os.path.join(self.tmp, 'predictions.db')
        conn = sqlite3.connect(self.src)
        conn.execute("CREATE TABLE predictions (id INTEGER PRIMARY KEY, code TEXT)")
        conn.execute("INSERT INTO predictions (code) VALUES ('600000')")
        conn.commit()
        conn.close()

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_backup_creates_readable_snapshot(self):
        dst = bp.backup_db(self.src, self.tmp, date_str='20260918')
        self.assertTrue(os.path.exists(dst))
        conn = sqlite3.connect(dst)
        try:
            n = conn.execute("SELECT COUNT(*) FROM predictions").fetchone()[0]
        finally:
            conn.close()
        self.assertEqual(n, 1, '备份快照内容应与源库一致')

    def test_idempotent_same_date(self):
        bp.backup_db(self.src, self.tmp, date_str='20260918')
        # 源库再追加一行，同日再备份应覆盖为最新
        conn = sqlite3.connect(self.src)
        conn.execute("INSERT INTO predictions (code) VALUES ('600001')")
        conn.commit()
        conn.close()
        dst = bp.backup_db(self.src, self.tmp, date_str='20260918')
        conn = sqlite3.connect(dst)
        try:
            n = conn.execute("SELECT COUNT(*) FROM predictions").fetchone()[0]
        finally:
            conn.close()
        self.assertEqual(n, 2, '同日重复备份应覆盖为最新快照（幂等）')
        self.assertEqual(len([f for f in os.listdir(self.tmp)
                              if f.startswith('predictions_')]), 1)

    def test_prune_keeps_latest_n(self):
        for d in ('20260901', '20260902', '20260903', '20260904'):
            bp.backup_db(self.src, self.tmp, date_str=d)
        removed = bp.prune_backups(self.tmp, keep=2)
        self.assertEqual(len(removed), 2, '应清理 2 份最旧备份')
        left = sorted(f for f in os.listdir(self.tmp) if f.startswith('predictions_'))
        self.assertEqual(left, ['predictions_20260903.db', 'predictions_20260904.db'])

    def test_missing_source_raises(self):
        with self.assertRaises(FileNotFoundError):
            bp.backup_db(os.path.join(self.tmp, 'nope.db'), self.tmp)

    def test_daily_job_wired(self):
        import io
        src = io.open(os.path.join(PROJECT_ROOT, 'scripts', 'daily_job.py'),
                      encoding='utf-8').read()
        self.assertIn('backup_predictions.py', src, 'daily_job 未接入每日备份')
        self.assertIn('daily_db_backup', src, 'daily_job 缺备份开关')

    def test_config_registered(self):
        import io
        import re
        cfg = io.open(os.path.join(PROJECT_ROOT, 'config.yml'), encoding='utf-8').read()
        self.assertIsNotNone(
            re.search(r'(?m)^\s*daily_db_backup:\s*true', cfg),
            'config.yml 未登记 daily_db_backup: true')


if __name__ == '__main__':
    unittest.main()
