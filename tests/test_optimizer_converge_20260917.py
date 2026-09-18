# -*- coding: utf-8 -*-
"""收敛唯一权重写入口 — 回归测试（2026-09-17）

覆盖 feedback/optimizer.py 的收敛改造：
  T4-1  apply_from_report 不再写 v1.json（断言 mtime/内容不变）
  T4-2  _load_history 修复 ORDER BY p.id 缺 DESC → 返回最新 500 条
  T4-3  Grep 静态断言：全项目只有一个 .py 文件能写 data/weights/v1.json
        （即 scripts/calibrate_weights.py）

约束：本文件不修改 data/weights/v1.json。
"""
import os
import re
import sys
import sqlite3
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from feedback.optimizer import WeightsOptimizer, RETURN_CONVENTION_NOTE  # noqa: E402

WEIGHTS_DIR = os.path.join(ROOT, 'data', 'weights')
V1_PATH = os.path.join(WEIGHTS_DIR, 'v1.json')


class TestApplyFromReportNoWrite(unittest.TestCase):
    """T4-1：apply_from_report 收敛后不再写 v1.json"""

    def test_apply_from_report_does_not_mutate_v1(self):
        self.assertTrue(os.path.exists(V1_PATH), "真实 v1.json 必须存在（基线）")
        before_mtime = os.path.getmtime(V1_PATH)
        with open(V1_PATH, 'rb') as f:
            before = f.read()

        opt = WeightsOptimizer(weights_dir=WEIGHTS_DIR, min_records=1)
        report = {'mode': 'short',
                  'proposed_weights': {'capital_flow': 0.5, 'momentum': 0.5}}
        result = opt.apply_from_report(report)

        after_mtime = os.path.getmtime(V1_PATH)
        with open(V1_PATH, 'rb') as f:
            after = f.read()

        self.assertFalse(result, "收敛后 apply_from_report 应返回 False（未写入）")
        self.assertEqual(before_mtime, after_mtime, "v1.json mtime 不应改变")
        self.assertEqual(before, after, "v1.json 内容不应改变")


class TestLoadHistoryLatest(unittest.TestCase):
    """T4-2：_load_history 在 >500 条时返回最新的 500 条（修复缺 DESC）"""

    def _make_tracker(self, n, tmp):
        db = os.path.join(tmp, 'predictions.db')
        conn = sqlite3.connect(db)
        conn.execute("CREATE TABLE predictions ("
                     "id INTEGER PRIMARY KEY, date TEXT, score REAL, "
                     "rating TEXT, factor_scores TEXT, mode TEXT)")
        conn.execute("CREATE TABLE outcomes ("
                     "prediction_id INTEGER, t1_return REAL, t5_return REAL)")
        for i in range(1, n + 1):
            conn.execute("INSERT INTO predictions VALUES (?,?,?,?,?,?)",
                         (i, '2026-01-01', 1.0, 'B', '{}', 'short'))
            conn.execute("INSERT INTO outcomes VALUES (?,?,?)", (i, 0.01 * i, 0.02))
        conn.commit()
        conn.close()

        class _T:
            db_path = db
        return _T()

    def test_returns_latest_500_when_over_cap(self):
        tmp = tempfile.mkdtemp()
        try:
            n = 700
            tracker = self._make_tracker(n, tmp)
            opt = WeightsOptimizer(weights_dir=tmp, min_records=1)
            df = opt._load_history(tracker, 'short')
            self.assertIsNotNone(df)
            self.assertEqual(len(df), 500, "应只取最新 500 条")
            ids = df['id'].tolist()
            # 最新 500 条 = id 201..700
            self.assertEqual(min(ids), n - 499)
            self.assertEqual(max(ids), n)
            # 且时间正序排列（修复后按 id 升序）
            self.assertEqual(ids, sorted(ids))
        finally:
            pass

    def test_returns_all_when_under_cap(self):
        tmp = tempfile.mkdtemp()
        try:
            n = 120
            tracker = self._make_tracker(n, tmp)
            opt = WeightsOptimizer(weights_dir=tmp, min_records=1)
            df = opt._load_history(tracker, 'short')
            self.assertEqual(len(df), n)
            self.assertEqual(df['id'].min(), 1)
            self.assertEqual(df['id'].max(), n)
        finally:
            pass


class TestOnlyOneV1Writer(unittest.TestCase):
    """T4-3：Grep 静态断言——全项目只有一个 .py 能写 data/weights/v1.json

    精确判定"写 v1.json"：存在 `open(<含 v1 的变量>, 'w'|'a')` 调用。
    仅 scripts/calibrate_weights.py 的 --apply 分支满足；其余文件即便出现
    v1.json 字面量也只是读取/注释，不会被判为写入口。
    """
    # 匹配 open(v1_path, 'w' ...) / open(v1Path, "a" ...) 这类写调用
    WRITE_RE = re.compile(r"open\(\s*[^,)]*[vV]1[^,)]*\s*,\s*['\"](?:w|a)")

    def test_only_calibrate_weights_writes_v1(self):
        writers = []
        for dirpath, _, files in os.walk(ROOT):
            if 'archive' in dirpath or 'docs' in dirpath or 'tests' in dirpath:
                continue
            for fn in files:
                if not fn.endswith('.py'):
                    continue
                path = os.path.join(dirpath, fn)
                try:
                    with open(path, 'r', encoding='utf-8', errors='ignore') as f:
                        src = f.read()
                except OSError:
                    continue
                if self.WRITE_RE.search(src):
                    rel = os.path.relpath(path, ROOT).replace('\\', '/')
                    writers.append(rel)
        self.assertEqual(
            writers, ['scripts/calibrate_weights.py'],
            f"写 v1.json 的文件应为且仅为 scripts/calibrate_weights.py，实际: {writers}"
        )


class TestReturnConventionNote(unittest.TestCase):
    """T4-4：optimizer 报告携带收益口径不同源声明"""

    def test_note_present_in_module(self):
        self.assertIn('T 日收盘', RETURN_CONVENTION_NOTE)
        self.assertIn('T+1 开盘', RETURN_CONVENTION_NOTE)
        self.assertIn('不可互相印证', RETURN_CONVENTION_NOTE)


if __name__ == '__main__':
    unittest.main()
