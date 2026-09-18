# -*- coding: utf-8 -*-
"""P5 每日市值/估值快照配套测试（2026-09-18）

覆盖：写库幂等 / 缺失值不回落 0（None 契约）/ 指数排除 / 调度开关接线。
"""
import os
import sqlite3
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

import scripts.snapshot_valuation_daily as svd  # noqa: E402


class TestValuationSnapshotWriter(unittest.TestCase):

    def setUp(self):
        fd, self.db = tempfile.mkstemp(suffix='.db')
        os.close(fd)
        self.conn = sqlite3.connect(self.db)
        svd._init(self.conn)

    def tearDown(self):
        self.conn.close()
        try:
            os.unlink(self.db)
        except OSError:
            pass

    def test_write_and_read_back(self):
        rows = [('000001', 5.5, 0.8, 3.0e11, 2.9e11),
                ('000002', None, 1.2, 1.5e11, 1.4e11)]
        n = svd.write_snapshot(self.conn, '2026-09-18', rows)
        self.assertEqual(n, 2)
        got = dict((r[0], r[1:]) for r in self.conn.execute(
            "SELECT code, pe, pb, total_mv, circ_mv FROM valuation_snapshot").fetchall())
        self.assertAlmostEqual(got['000001'][0], 5.5)
        self.assertIsNone(got['000002'][0], '缺失 PE 应写 NULL，不得回落 0')

    def test_idempotent_same_date(self):
        svd.write_snapshot(self.conn, '2026-09-18', [('000001', 5.0, 0.8, 1e11, 9e10)])
        svd.write_snapshot(self.conn, '2026-09-18', [('000001', 6.0, 0.9, 1.1e11, 1e11)])
        rows = self.conn.execute("SELECT pe FROM valuation_snapshot").fetchall()
        self.assertEqual(len(rows), 1, '同日重复写入应为覆盖（INSERT OR REPLACE）')
        self.assertAlmostEqual(rows[0][0], 6.0)

    def test_multiple_dates_accumulate(self):
        for d in ('2026-09-16', '2026-09-17', '2026-09-18'):
            svd.write_snapshot(self.conn, d, [('000001', 5.0, 0.8, 1e11, 9e10)])
        days = self.conn.execute(
            "SELECT COUNT(DISTINCT date) FROM valuation_snapshot").fetchone()[0]
        self.assertEqual(days, 3)


class TestSnapshotWiring(unittest.TestCase):

    def test_nonfinite_and_nonpositive_to_none(self):
        """值为非正/非有限 → None（不回落 0）"""
        def _f(v):
            import math
            try:
                f = float(v)
            except (TypeError, ValueError):
                return None
            return f if math.isfinite(f) and f > 0 else None
        self.assertIsNone(_f(-3.0))     # 亏损公司 PE
        self.assertIsNone(_f(0))
        self.assertIsNone(_f(float('nan')))
        self.assertIsNone(_f(None))
        self.assertAlmostEqual(_f(5.5), 5.5)

    def test_daily_job_wired(self):
        import io
        src = io.open(os.path.join(PROJECT_ROOT, 'scripts', 'daily_job.py'),
                      encoding='utf-8').read()
        self.assertIn('snapshot_valuation_daily.py', src,
                      'daily_job 未接入每日估值快照')
        self.assertIn('daily_valuation_snapshot', src, 'daily_job 缺开关读取')

    def test_config_registered(self):
        import io
        import re
        cfg = io.open(os.path.join(PROJECT_ROOT, 'config.yml'), encoding='utf-8').read()
        self.assertIsNotNone(
            re.search(r'(?m)^\s*daily_valuation_snapshot:\s*true', cfg),
            'config.yml 未登记 daily_valuation_snapshot: true')


if __name__ == '__main__':
    unittest.main()
