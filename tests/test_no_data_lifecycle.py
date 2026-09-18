# -*- coding: utf-8 -*-
"""no_data 终态生命周期回归测试（2026-09-12 审查轮要求固化；2026-09-14 改为 unittest）。

覆盖审查发现的两类致命缺陷防回归：
1. 死锁缺陷：自然路径（bump 满 30 → mark_no_data）产生的终态行必须能被
   revive_stale_no_data 复活——修复前的 attempts<30 复活条件把自然终态
   行全部挡死（死代码），且复活行重标后循环即死锁。
2. 每周一探循环：复活 → 仍无 K 线 → 当夜 bump+mark 回终态 → 满 7 天再
   复活，循环不死锁；有 K 线 → update_outcomes 整行重写 status 归 NULL
   （INSERT OR REPLACE 语义），行自愈离开终态。

2026-09-14 整体审查 P2-4：原实现是**独立脚本**（自带 main/check），
`python -m unittest tests.test_no_data_lifecycle` 收集到 **0 个用例**、
门禁也不调用 → 这些断言实际从未在回归中被执行过。现改写为 TestCase，
每个用例自建临时库，可被 unittest / discover 正常收集。

运行：
  python -m unittest tests.test_no_data_lifecycle -v
  python -m unittest discover -s tests -p "test_*.py" -t . -v
"""

import os
import shutil
import sqlite3
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pandas as pd

from feedback.tracker import PredictionTracker

_SCHEMA = """
    CREATE TABLE IF NOT EXISTS predictions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        date TEXT, code TEXT, name TEXT, mode TEXT,
        score REAL, rating TEXT, buy_price REAL,
        model_version TEXT, factor_scores TEXT,
        created_at TEXT DEFAULT CURRENT_TIMESTAMP,
        backfill_attempts INTEGER DEFAULT 0);
    CREATE TABLE IF NOT EXISTS outcomes (
        prediction_id INTEGER PRIMARY KEY,
        t1_date TEXT, t1_close REAL, t1_return REAL,
        t5_date TEXT, t5_close REAL, t5_return REAL,
        t20_date TEXT, t20_close REAL, t20_return REAL,
        updated_at TEXT DEFAULT CURRENT_TIMESTAMP,
        status TEXT);
"""


class _LifecycleBase(unittest.TestCase):
    """提供临时库 + 三行测试 prediction 的公共装置"""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmpdir, True)
        self.db = os.path.join(self.tmpdir, 'lifecycle.db')
        self.tracker = PredictionTracker(db_path=self.db)
        conn = sqlite3.connect(self.db)
        conn.executescript(_SCHEMA)
        for i, code in enumerate(('600001', '600002', '600003'), 1):
            conn.execute(
                "INSERT INTO predictions (date, code, name, mode, score, buy_price) "
                "VALUES ('2026-09-01', ?, ?, 'short', 70, 10.0)", (code, f'测试{i}'))
        conn.commit()
        conn.close()

    def _age_no_data(self, pid, days):
        conn = sqlite3.connect(self.db)
        conn.execute("UPDATE outcomes SET updated_at = datetime('now', ?) "
                     "WHERE prediction_id = ?", (f'-{days} days', pid))
        conn.commit()
        conn.close()

    def _row(self, pid, cols='status, t1_close'):
        conn = sqlite3.connect(self.db)
        try:
            return conn.execute(
                f"SELECT {cols} FROM outcomes WHERE prediction_id=?", (pid,)).fetchone()
        finally:
            conn.close()


class TestNoDataLifecycle(_LifecycleBase):
    """no_data 终态的行生命周期（防死锁回归）"""

    def test_natural_terminal_state_can_be_revived(self):
        """死锁点 #1：自然路径（attempts 满 30 → mark_no_data）必须可复活"""
        pid = 1
        attempts = 0
        for _ in range(30):
            attempts = self.tracker.bump_backfill_attempts(pid)
        self.assertEqual(attempts, 30)
        self.tracker.mark_no_data(pid)
        self._age_no_data(pid, 8)
        revived = self.tracker.revive_stale_no_data(stale_days=7)
        self.assertEqual(revived, [pid], '自然终态行满 7 天未被复活（死代码/死锁回归）')

    def test_weekly_probe_loop_does_not_deadlock(self):
        """死锁点 #2：复活 → 仍无 K 线 → 重标 → 满 7 天可再次复活（循环存活）"""
        pid = 1
        for _ in range(30):
            self.tracker.bump_backfill_attempts(pid)
        self.tracker.mark_no_data(pid)
        self._age_no_data(pid, 8)
        self.tracker.revive_stale_no_data(stale_days=7)

        attempts2 = self.tracker.bump_backfill_attempts(pid)   # 复活后当夜一次 bump
        self.assertGreaterEqual(attempts2, 30, 'attempts 被重置（应只增不减）')
        self.tracker.mark_no_data(pid)
        self._age_no_data(pid, 8)
        revived2 = self.tracker.revive_stale_no_data(stale_days=7)
        self.assertEqual(revived2, [pid], '重标后无法再次复活（循环死锁）')

    def test_successful_backfill_self_heals(self):
        """有 K 线时 update_outcomes 整行重写 → status 进入 filled_t1 正状态（T2 新语义）。

        2026-09-17 更新：旧语义为"status 归 NULL"；T2 落地 filled_* 正状态后，
        自愈的判定升级为"从 no_data 墓碑进入 filled 正状态"——行不再处于终态，
        且携带已回填档位信息（2 天 K 线只够 T+1 → filled_t1）。
        """
        pid = 1
        self.tracker.mark_no_data(pid)
        kline = pd.DataFrame({'date': ['2026-09-01', '2026-09-02'],
                              'close': [10.0, 10.5]})
        self.tracker.update_outcomes(pid, kline, pred_date='2026-09-01')
        status, t1_close = self._row(pid)
        self.assertEqual(status, 'filled_t1',
                         '回填成功后 status 应为 filled_t1 正状态（已离开 no_data 终态）')
        self.assertEqual(t1_close, 10.5)

    def test_not_revived_within_7_days(self):
        """刚标记（不满 stale_days）不应被复活"""
        self.tracker.mark_no_data(2)
        got = self.tracker.revive_stale_no_data(stale_days=7)
        self.assertNotIn(2, got, '不满 7 天的终态行被误复活')

    def test_null_updated_at_revived_as_oldest(self):
        """updated_at 为 NULL 的终态行按"最老"处理，应被复活"""
        conn = sqlite3.connect(self.db)
        conn.execute("INSERT INTO outcomes (prediction_id, status, updated_at) "
                     "VALUES (3, 'no_data', NULL)")
        conn.commit()
        conn.close()
        got = self.tracker.revive_stale_no_data(stale_days=7)
        self.assertIn(3, got, 'updated_at NULL 的终态行未被复活')


if __name__ == '__main__':
    unittest.main(verbosity=2)
