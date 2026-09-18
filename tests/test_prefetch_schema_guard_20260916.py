# -*- coding: utf-8 -*-
"""回归测试：预取写入 schema 校验，拒收技术因子"壳记录"（2026-09-16 P0-1 预防）

背景：
  上游 ASHareHub `technical-factors` 对约 18.3% 的股票返回"壳记录"
  （close_hfq 有值，macd_* / rsi_* / cci 全空）。此类记录不含任何可用信息，
  一旦写入预取缓存就会借 7 天 TTL 长期驻留，是 2026-09-16 流水线崩溃的
  直接弹药来源（崩溃票 001309 命中的正是 09-13 预取的壳记录）。

本文件锁定：
  A. 壳记录必须被拒收（返回 False 且不落库），并产生 WARNING；
  B. 正常记录写入行为不变（返回 True、可读回）；
  C. 校验只作用于 tech_factors 表，不得误伤 concepts / financial；
  D. 判定口径与消费端（score_from_asharehub）的字段集合一致。
"""
import os
import shutil
import sqlite3
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from core.data_engine import DataEngine  # noqa: E402

SHELL = {'macd_dif': None, 'macd_dea': None, 'macd': None,
         'rsi_6': None, 'rsi_12': None, 'rsi_24': None,
         'close_hfq': 60.94, 'cci': None}

GOOD = {'macd_dif': 0.244, 'macd_dea': 0.163, 'macd': 0.162,
        'rsi_6': 70.0, 'rsi_12': 64.68, 'rsi_24': 60.688,
        'close_hfq': 60.94, 'cci': 123.629}


class _Engine(DataEngine):
    """只暴露写入/读取所需的最小状态（跳过 DataEngine.__init__ 的重初始化）"""

    def __init__(self, path):
        self._asharehub_prefetch_path = path
        for t in ('tech_factors', 'concepts', 'financial'):
            conn = sqlite3.connect(path)
            conn.execute(f"CREATE TABLE IF NOT EXISTS {t} "
                         f"(code TEXT PRIMARY KEY, data TEXT, fetched_at TEXT)")
            conn.commit()
            conn.close()

    def write(self, table, code, data):
        return self._write_asharehub_prefetch(table, code, data)

    def rows(self, table):
        conn = sqlite3.connect(self._asharehub_prefetch_path)
        try:
            return conn.execute(f"SELECT code, data FROM {table}").fetchall()
        finally:
            conn.close()


class TestShellRecordRejection(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='prefetch_schema_')
        self.eng = _Engine(os.path.join(self.tmp, 'prefetch.db'))

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    # ── A. 壳记录拒收 ──

    def test_shell_record_rejected_and_not_persisted(self):
        with self.assertLogs('core.data_engine', level='WARNING') as cm:
            ok = self.eng.write('tech_factors', '001309', dict(SHELL))
        self.assertFalse(ok, '壳记录必须返回 False')
        self.assertEqual(self.eng.rows('tech_factors'), [])
        self.assertIn('壳记录', '\n'.join(cm.output))

    def test_nan_shell_record_rejected(self):
        nan_rec = {k: (float('nan') if k != 'close_hfq' else 60.94)
                   for k in SHELL}
        with self.assertLogs('core.data_engine', level='WARNING'):
            self.assertFalse(self.eng.write('tech_factors', '001309', nan_rec))
        self.assertEqual(self.eng.rows('tech_factors'), [])

    def test_inf_record_rejected(self):
        rec = {k: (float('inf') if k != 'close_hfq' else 60.94) for k in SHELL}
        with self.assertLogs('core.data_engine', level='WARNING'):
            self.assertFalse(self.eng.write('tech_factors', '001309', rec))

    def test_non_dict_rejected(self):
        with self.assertLogs('core.data_engine', level='WARNING'):
            self.assertFalse(self.eng.write('tech_factors', '001309', None))
        with self.assertLogs('core.data_engine', level='WARNING'):
            self.assertFalse(self.eng.write('tech_factors', '001309', {}))

    # ── B. 正常记录不受影响 ──

    def test_good_record_written(self):
        self.assertTrue(self.eng.write('tech_factors', '600163', dict(GOOD)))
        rows = self.eng.rows('tech_factors')
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0][0], '600163')

    def test_partially_valid_record_still_written(self):
        """只要有一个可用因子就应缓存（不得过度拒收）"""
        rec = dict(SHELL)
        rec['rsi_12'] = 45.0
        rec['rsi_24'] = 47.0
        self.assertTrue(self.eng.write('tech_factors', '600163', rec))

    def test_record_with_zero_macd_but_valid_rsi_kept(self):
        """边界：macd 全 0（合法值）但 RSI 有值 → 属正常数据，必须缓存"""
        rec = {'macd_dif': 0.0, 'macd_dea': 0.0, 'macd': 0.0,
               'rsi_6': 50.0, 'rsi_12': 50.0, 'rsi_24': 50.0, 'cci': 0.0}
        self.assertTrue(self.eng.write('tech_factors', '600163', rec))

    # ── C. 不得误伤其它表 ──

    def test_concepts_and_financial_not_affected(self):
        """concepts 的包装结构 {'names': [...]} 与 financial 结构都不含技术因子字段"""
        self.assertTrue(self.eng.write('concepts', '600163',
                                       {'names': ['BK1722.DC']}))
        self.assertTrue(self.eng.write('concepts', '600164', {}))
        self.assertTrue(self.eng.write('financial', '600163',
                                       {'eps': None, 'roe': None}))
        self.assertTrue(self.eng.write('financial', '600164', {}))
        self.assertEqual(len(self.eng.rows('concepts')), 2)
        self.assertEqual(len(self.eng.rows('financial')), 2)

    # ── D. 判定口径 ──

    def test_is_shell_predicate_matrix(self):
        f = DataEngine._is_shell_tech_record
        self.assertTrue(f(dict(SHELL)))
        self.assertTrue(f({}))
        self.assertTrue(f(None))
        self.assertTrue(f('x'))
        self.assertTrue(f({'close_hfq': 10.0}))
        self.assertTrue(f({'rsi_12': None, 'cci': None}))
        self.assertFalse(f(dict(GOOD)))
        self.assertFalse(f({'rsi_12': 50.0}))
        self.assertFalse(f({'cci': -150.0}))
        self.assertFalse(f({'macd': 0.0}))

    def test_predicate_covers_scorer_consumed_fields(self):
        """判定字段集合必须与 score_from_asharehub 消费的字段一致（防漂移）"""
        import inspect
        from core.technical_scorer import TechnicalScorer
        src = inspect.getsource(TechnicalScorer.score_from_asharehub)
        for field in ('macd_dif', 'macd_dea', 'macd', 'rsi_12', 'rsi_24', 'cci'):
            self.assertIn(field, src, f'消费端字段 {field} 未在测试中覆盖')
            self.assertFalse(
                DataEngine._is_shell_tech_record({field: 1.0}),
                f'{field} 为有效值时不应判为壳记录')


if __name__ == '__main__':
    unittest.main()
