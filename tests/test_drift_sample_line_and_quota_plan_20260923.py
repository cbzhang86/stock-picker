# -*- coding: utf-8 -*-
"""D1 结构性样本数据线 + A5 配额动态推导 的守卫测试（2026-09-23 交接单）。

全部在临时库/临时路径上验证：零 ASHareHub 配额、不触碰生产数据。
"""
import io
import json
import os
import sqlite3
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from feedback import drift_sample as ds  # noqa: E402


def _mk_kline(path, closes):
    """closes: {code: [(date, close), ...]}"""
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE kline_cache (code TEXT, date TEXT, open REAL, "
                 "high REAL, low REAL, close REAL, volume REAL, amount REAL)")
    for code, seq in closes.items():
        for d, c in seq:
            conn.execute("INSERT INTO kline_cache VALUES (?,?,?,?,?,?,?,?)",
                         (code, d, c, c, c, c, 1e6, 1e7))
    conn.commit()
    conn.close()


class TestD1RecordCandidatePool(unittest.TestCase):

    def test_records_and_rejects_bad_codes(self):
        with tempfile.TemporaryDirectory() as tmp:
            fdb = os.path.join(tmp, 'f.db')
            n = ds.record_candidate_pool('2026-09-22', [
                {'code': '600000', 'score': 70.0},
                {'code': '000001', 'score': 65.0},
                {'code': 'BAD', 'score': 60.0},       # 非 6 位
                {'code': None, 'score': 60.0},        # 缺失
                {'score': 60.0},                      # 无 code
            ], db_path=fdb)
            self.assertEqual(n, 2, '只应写入 2 个合法 6 位代码')
            conn = sqlite3.connect(fdb)
            rows = conn.execute("SELECT date, code, rank FROM candidate_daily "
                                "ORDER BY rank").fetchall()
            conn.close()
            self.assertEqual([(r[1], r[2]) for r in rows],
                             [('600000', 1), ('000001', 2)])

    def test_idempotent(self):
        with tempfile.TemporaryDirectory() as tmp:
            fdb = os.path.join(tmp, 'f.db')
            pool = [{'code': '600000', 'score': 70.0}]
            ds.record_candidate_pool('2026-09-22', pool, db_path=fdb)
            ds.record_candidate_pool('2026-09-22', pool, db_path=fdb)
            conn = sqlite3.connect(fdb)
            n = conn.execute("SELECT COUNT(*) FROM candidate_daily").fetchone()[0]
            conn.close()
            self.assertEqual(n, 1, '同 (date,code) 应覆盖而非重复')

    def test_empty_pool_safe(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(ds.record_candidate_pool('2026-09-22', [],
                                                      db_path=os.path.join(tmp, 'f.db')), 0)
            self.assertEqual(ds.record_candidate_pool('', [{'code': '600000'}],
                                                      db_path=os.path.join(tmp, 'f.db')), 0)


class TestD1Backfill(unittest.TestCase):

    def _prep(self, tmp, seq=None):
        fdb = os.path.join(tmp, 'f.db')
        kdb = os.path.join(tmp, 'k.db')
        seq = seq or {'600000': [('2026-09-21', 10.0), ('2026-09-22', 10.5)],
                      '000001': [('2026-09-21', 20.0), ('2026-09-22', 19.6)],
                      '300750': [('2026-09-21', 30.0), ('2026-09-22', 30.0)]}
        _mk_kline(kdb, seq)
        ds.record_candidate_pool('2026-09-21', [
            {'code': c, 'score': 60.0} for c in seq], db_path=fdb)
        return fdb, kdb

    def test_computes_close_to_close_t1(self):
        """口径：close(D) → close(D+1)（与 outcomes.t1_return 一致）。"""
        with tempfile.TemporaryDirectory() as tmp:
            fdb, kdb = self._prep(tmp)
            self.assertEqual(ds.backfill_daily_samples(db_path=fdb, kline_db=kdb), 1)
            s = ds.load_daily_samples(db_path=fdb)[0]
            # (10.5/10 −1)=+5%, (19.6/20 −1)=−2%, (30/30 −1)=0 → 均值 +1.0%
            self.assertAlmostEqual(s['day_ret'], 1.0, places=3)
            self.assertAlmostEqual(s['win_rate'], 1 / 3, places=3)
            self.assertEqual(len(s['rets']), 3)
            self.assertEqual(s['n_rec'], 3)

    def test_idempotent_and_skips_pending(self):
        with tempfile.TemporaryDirectory() as tmp:
            fdb, kdb = self._prep(tmp)
            ds.backfill_daily_samples(db_path=fdb, kline_db=kdb)
            self.assertEqual(ds.backfill_daily_samples(db_path=fdb, kline_db=kdb), 0,
                             '已算过的日期应跳过（幂等）')
            # 只有一个交易日 K 线（缺 D+1）→ 不产样本，等下次
            fdb2 = os.path.join(tmp, 'f2.db')
            kdb2 = os.path.join(tmp, 'k2.db')
            _mk_kline(kdb2, {'600000': [('2026-09-21', 10.0)]})
            ds.record_candidate_pool('2026-09-21', [{'code': '600000'}], db_path=fdb2)
            self.assertEqual(ds.backfill_daily_samples(db_path=fdb2, kline_db=kdb2), 0,
                             'D+1 未就绪时不得产出样本（不能伪造）')

    def test_missing_db_safe(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(ds.backfill_daily_samples(
                db_path=os.path.join(tmp, 'none.db')), 0)
            self.assertEqual(ds.load_daily_samples(
                db_path=os.path.join(tmp, 'none.db')), [])


class TestD1RankStocksTopPool(unittest.TestCase):
    """rank_stocks 必须无条件外送 top_pool（含零达标日）；diagnostics=None 时零副作用。"""

    def test_top_pool_written_normal_and_zero_qualified(self):
        from core.scoring_model import ScoringModel
        sm = ScoringModel()
        stocks = [{'code': '600000', 'name': 'a', 'price': 10.0, 'rps_20': 80,
                   'amount': 1e8},
                  {'code': '000001', 'name': 'b', 'price': 10.0, 'rps_20': 60,
                   'amount': 5e7}]
        # 门槛设为正常值 → 可能有推荐；再设为 100 → 必然零达标
        for min_score in (60.0, 100.0):
            diag = {}
            sm.rank_stocks(stocks, mode='short', top_n=3, min_score=min_score,
                           diagnostics=diag)
            self.assertIn('top_pool', diag,
                          f'min_score={min_score} 时也必须外送候选池（D1 解耦要求）')
            self.assertEqual(len(diag['top_pool']), 2)
            self.assertIn('code', diag['top_pool'][0])
            self.assertIn('score', diag['top_pool'][0])

    def test_no_side_effect_without_diagnostics(self):
        from core.scoring_model import ScoringModel
        sm = ScoringModel()
        stocks = [{'code': '600000', 'name': 'a', 'price': 10.0, 'rps_20': 80,
                   'amount': 1e8}]
        r = sm.rank_stocks(stocks, mode='short', top_n=3, min_score=60.0)
        self.assertIsInstance(r, list)   # 不传 diagnostics 不崩、行为不变

    def test_short_term_exposes_diagnostics(self):
        src = io.open(os.path.join(ROOT, 'strategies', 'short_term.py'),
                      encoding='utf-8').read()
        self.assertIn('self._last_diagnostics = _diag', src)

    def test_eod_wires_sample_line(self):
        src = io.open(os.path.join(ROOT, 'scripts', 'eod_stock_picker.py'),
                      encoding='utf-8').read()
        self.assertIn('record_candidate_pool', src)
        self.assertIn('backfill_daily_samples', src)


class TestD1DriftMonitorPrefersSamples(unittest.TestCase):

    def _make_sample_db(self, path, n_days, ret=1.0):
        conn = sqlite3.connect(path)
        conn.execute("""CREATE TABLE drift_daily_sample (
            date TEXT PRIMARY KEY, n_cand INTEGER, avg_t1_return REAL,
            win_rate REAL, rets TEXT, computed_at TEXT)""")
        for i in range(n_days):
            d = f"2026-06-{i + 1:02d}"
            conn.execute("INSERT INTO drift_daily_sample VALUES (?,?,?,?,?,?)",
                         (d, 3, ret, 1.0, json.dumps([ret] * 3), '2026-09-23'))
        conn.commit()
        conn.close()

    def test_prefers_sample_line(self):
        from core.drift_monitor import DriftMonitor
        with tempfile.TemporaryDirectory() as tmp:
            sdb = os.path.join(tmp, 'f.db')
            self._make_sample_db(sdb, 45)
            dm = DriftMonitor(db_path=os.path.join(tmp, 'no_pred.db'),
                              sample_db_path=sdb)
            r = dm.check()
            self.assertEqual(r['sample_source'], 'drift_daily_sample')
            self.assertNotEqual(r['verdict'], 'NO_SAMPLE',
                                '有日度样本时不得因 predictions 为空而判 NO_SAMPLE')
            self.assertIn('候选池日度线', r['note'])

    def test_falls_back_when_samples_insufficient(self):
        from core.drift_monitor import DriftMonitor
        with tempfile.TemporaryDirectory() as tmp:
            sdb = os.path.join(tmp, 'f.db')
            self._make_sample_db(sdb, 5)      # 不足 recent*2=40
            pdb = os.path.join(tmp, 'p.db')
            conn = sqlite3.connect(pdb)
            conn.execute("CREATE TABLE predictions (id INTEGER PRIMARY KEY, "
                         "date TEXT, code TEXT, mode TEXT)")
            conn.execute("CREATE TABLE outcomes (prediction_id INTEGER, t1_return REAL)")
            conn.commit()
            conn.close()
            r = DriftMonitor(db_path=pdb, sample_db_path=sdb).check()
            self.assertEqual(r['sample_source'], 'predictions',
                             '样本线不足时应回退 predictions 口径')
            self.assertEqual(r['verdict'], 'NO_SAMPLE')


class TestA5QuotaRemaining(unittest.TestCase):
    """A5：批大小按剩余额度动态推导；交易日给生产 eod 留地板。"""

    def _fresh(self, quota_path, used=0):
        from core.data_engine import DataEngine
        from datetime import datetime
        de = DataEngine(config={})
        de._asharehub_quota_path = quota_path
        if used:
            with io.open(quota_path, 'w', encoding='utf-8') as f:
                json.dump({'date': datetime.now().strftime('%Y-%m-%d'),
                           'used': used}, f)
        return de

    def test_constants_are_self_consistent(self):
        from core.data_engine import DataEngine
        de = DataEngine({})
        self.assertEqual(de.ASHAREHUB_PROD_FLOOR, 55)
        self.assertLess(de.ASHAREHUB_RESERVE + de.ASHAREHUB_PROD_FLOOR,
                        de._asharehub_budget,
                        '预留 + 生产地板必须小于日配额，否则预取永不可用')

    def test_remaining_math(self):
        with tempfile.TemporaryDirectory() as tmp:
            qp = os.path.join(tmp, 'q.json')
            de = self._fresh(qp, used=0)
            # 空闲：100 − 10 − 0 = 90
            self.assertEqual(de.quota_remaining(), 90)
            # 交易日（给 eod 留 55）：100 − 10 − 0 − 55 = 35
            self.assertEqual(de.quota_remaining(extra_floor=55), 35)
            # 已用 60：100 − 10 − 60 = 30
            de2 = self._fresh(qp, used=60)
            self.assertEqual(de2.quota_remaining(), 30)
            # 已用 85（余量不足）：100 − 10 − 85 = 5 → 5//3 = 1 只
            de3 = self._fresh(qp, used=85)
            self.assertEqual(de3.quota_remaining(), 5)
            # 已用 92 → 负数按 0
            de4 = self._fresh(qp, used=92)
            self.assertEqual(de4.quota_remaining(), 0)

    def test_prefetch_uses_dynamic_limit(self):
        src = io.open(os.path.join(ROOT, 'scripts', 'prefetch_asharehub.py'),
                      encoding='utf-8').read()
        self.assertIn('quota_remaining(extra_floor=_floor)', src)
        self.assertIn('ASHAREHUB_PROD_FLOOR', src)
        self.assertIn('_limit_eff', src)
        self.assertIn('_skip_ash_only', src)

    def test_never_exceeds_daily_budget(self):
        """硬约束：无论请求多大，动态上限都不会让当日消耗超过 100。"""
        with tempfile.TemporaryDirectory() as tmp:
            qp = os.path.join(tmp, 'q.json')
            de = self._fresh(qp, used=0)
            for floor, used in ((0, 0), (55, 0), (0, 60), (0, 88)):
                de = self._fresh(qp, used=used)
                usable = de.quota_remaining(extra_floor=floor)
                planned = min(30, usable // 3) * 3        # 每只 3 次
                self.assertLessEqual(used + planned,
                                     de._asharehub_budget - de.ASHAREHUB_RESERVE,
                                     f'floor={floor} used={used} 计划超闸门')


if __name__ == '__main__':
    unittest.main()
