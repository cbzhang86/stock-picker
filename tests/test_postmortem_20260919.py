# -*- coding: utf-8 -*-
"""复盘笔记系统测试（2026-09-19 落地）——落库/对账/熔断/相似检索/蒸馏候选"""
import json
import os
import sqlite3
import sys
import tempfile
import unittest
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from scripts.postmortem import (cmd_add, cmd_backfill, cmd_candidates,  # noqa: E402
                                cmd_similar, cmd_stats, get_conn, _verdict_of, _hit)


def mk_args(**kw):
    """构造 argparse 命名空间（沿用 main() 的字段名）"""
    from argparse import Namespace
    base = dict(date='2026-09-18', code='300001', name='测试股', rank=1,
                thesis='题材+缩量', missed_risk='解禁', key_factors='缩量,龙头',
                prediction='up', pred_confidence=0.6, llm_note='', db=None,
                limit=3, month='2026-09', days=7, kline_db=None)
    base.update(kw)
    return Namespace(**base)


class TestPostmortem(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.db = os.path.join(self.tmp, 'notes.db')
        self.kline_db = os.path.join(self.tmp, 'kline.db')
        # 造一个迷你 K 线库：300001 09-18 收 10.0，09-21 收 10.8（+8%）
        c = sqlite3.connect(self.kline_db)
        c.executescript("""
            CREATE TABLE kline_cache (code TEXT, date TEXT, open REAL, high REAL,
                                      low REAL, close REAL, volume REAL, amount REAL);
        """)
        c.executemany("INSERT INTO kline_cache VALUES (?,?,?,?,?,?,?,?)", [
            ('300001', '2026-09-18', 10, 10, 10, 10.0, 1, 1),
            ('300001', '2026-09-21', 10.5, 11, 10.5, 10.8, 1, 1),
            ('300002', '2026-09-18', 5, 5, 5, 5.0, 1, 1),
            ('300002', '2026-09-21', 4.9, 5, 4.8, 4.7, 1, 1),   # -6%
        ])
        c.commit()
        c.close()

    def tearDown(self):
        for f in (self.db, self.kline_db):
            if os.path.exists(f):
                os.remove(f)
        os.rmdir(self.tmp)

    # ── add ──

    def test_add_and_unique(self):
        self.assertEqual(cmd_add(mk_args(db=self.db)), 0)
        # 同 (date, code) 再写 → 跳过不覆盖
        self.assertEqual(cmd_add(mk_args(db=self.db, prediction='down')), 0)
        conn = get_conn(self.db)
        n = conn.execute("SELECT COUNT(*) n FROM notes").fetchone()['n']
        row = conn.execute("SELECT * FROM notes LIMIT 1").fetchone()
        conn.close()
        self.assertEqual(n, 1, 'UNIQUE(date,code) 应防重')
        self.assertEqual(json.loads(row['key_factors']), ['缩量', '龙头'])
        self.assertEqual(row['prediction'], 'up')
        self.assertIsNone(row['realized_outcome'])

    def test_add_rejects_bad_prediction(self):
        # 非法 prediction → 返回码 2，且不落库（连接无泄漏，tearDown 可删库）
        self.assertEqual(cmd_add(mk_args(db=self.db, prediction='sideways')), 2)
        conn = get_conn(self.db)
        n = conn.execute("SELECT COUNT(*) n FROM notes").fetchone()['n']
        conn.close()
        self.assertEqual(n, 0, '非法 prediction 不应落库')

    # ── backfill ──

    def test_backfill_fills_outcome_and_verdict(self):
        cmd_add(mk_args(db=self.db, code='300001', prediction='up'))
        cmd_add(mk_args(db=self.db, code='300002', prediction='up', rank=2))
        self.assertEqual(cmd_backfill(mk_args(db=self.db, kline_db=self.kline_db)), 0)
        conn = get_conn(self.db)
        rows = {r['code']: r for r in conn.execute("SELECT * FROM notes")}
        conn.close()
        self.assertAlmostEqual(rows['300001']['realized_outcome'], 8.0, places=2)
        self.assertEqual(rows['300001']['verdict'], 'good')
        self.assertAlmostEqual(rows['300002']['realized_outcome'], -6.0, places=2)
        self.assertEqual(rows['300002']['verdict'], 'bad')

    def test_backfill_skips_when_t1_missing(self):
        cmd_add(mk_args(db=self.db, code='999999'))
        self.assertEqual(cmd_backfill(mk_args(db=self.db, kline_db=self.kline_db)), 0)
        conn = get_conn(self.db)
        row = conn.execute("SELECT realized_outcome FROM notes").fetchone()
        conn.close()
        self.assertIsNone(row['realized_outcome'], 'T+1 未到应保持 NULL 留待下次')

    # ── stats（熔断闸门）──

    def test_stats_hit_rate_and_circuit_breaker(self):
        # 命中 1 条（up +8%），未命中 1 条（up -6%）→ 50% ≤ 熔断线 → 退出码 1
        cmd_add(mk_args(db=self.db, code='300001', prediction='up'))
        cmd_add(mk_args(db=self.db, code='300002', prediction='up', rank=2))
        cmd_backfill(mk_args(db=self.db, kline_db=self.kline_db))
        rc = cmd_stats(mk_args(db=self.db, days=7))
        self.assertEqual(rc, 1, '命中率 50% 应触发熔断退出码')

    def test_stats_pass_when_informational(self):
        cmd_add(mk_args(db=self.db, code='300001', prediction='up'))
        cmd_add(mk_args(db=self.db, code='300002', prediction='down', rank=2))
        cmd_backfill(mk_args(db=self.db, kline_db=self.kline_db))
        rc = cmd_stats(mk_args(db=self.db, days=7))
        self.assertEqual(rc, 0, '命中率 100% 不熔断')

    # ── similar ──

    def test_similar_ranks_by_overlap(self):
        cmd_add(mk_args(db=self.db, code='300001', key_factors='缩量,龙头'))
        cmd_add(mk_args(db=self.db, code='300002', key_factors='缩量,解禁', rank=2))
        cmd_backfill(mk_args(db=self.db, kline_db=self.kline_db))
        # 捕获 stdout 验证排序：重合 2 的排前
        import io, contextlib
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            cmd_similar(mk_args(db=self.db, key_factors='缩量,龙头'))
        out = buf.getvalue()
        self.assertIn('300001', out)
        self.assertLess(out.index('300001'), out.index('300002'),
                        '重合度高的案例应排前')

    # ── candidates ──

    def test_candidates_counts_bad_labels(self):
        cmd_add(mk_args(db=self.db, code='300002', key_factors='缩量,解禁', rank=2))
        cmd_backfill(mk_args(db=self.db, kline_db=self.kline_db))  # 300002 → bad
        import io, contextlib
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = cmd_candidates(mk_args(db=self.db, month='2026-09'))
        out = buf.getvalue()
        self.assertEqual(rc, 0)
        self.assertIn('解禁', out)
        self.assertIn('缩量', out)

    # ── 纯函数 ──

    def test_verdict_thresholds(self):
        self.assertEqual(_verdict_of(0.6), 'good')
        self.assertEqual(_verdict_of(0.5), 'good')
        self.assertEqual(_verdict_of(-0.5), 'bad')
        self.assertEqual(_verdict_of(-0.6), 'bad')
        self.assertEqual(_verdict_of(0.3), 'neutral')
        self.assertEqual(_verdict_of(-0.3), 'neutral')

    def test_hit_semantics(self):
        self.assertTrue(_hit('up', 0.1))
        self.assertFalse(_hit('up', -0.1))
        self.assertTrue(_hit('down', -0.1))
        self.assertFalse(_hit('down', 0.1))
        self.assertTrue(_hit('flat', 0.4))
        self.assertFalse(_hit('flat', 0.6))


if __name__ == '__main__':
    unittest.main()
