# -*- coding: utf-8 -*-
"""T 系列优化项配套测试（2026-09-17）

覆盖 T1（fin_cache TTL）/ T4（配额跨午夜口径）/ T5（run_context 持久化）
/ T8（reversal_20d 接入因子管线）/ T12（min_listing_days 接通）。

T2（outcomes 正状态）由 tests/test_no_data_lifecycle.py 的
test_successful_backfill_self_heals 更新版覆盖（filled_t1 断言）。
"""
import io
import json
import os
import sqlite3
import sys
import tempfile
import unittest
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class TestT1FinCacheTTL(unittest.TestCase):
    """T1：fin_cache 读取比对 fetched_at，超 TTL(90天) 返回 None 触发回源"""

    def _close_engine_conn(self, db_path):
        """关闭 DataEngine 线程级缓存的只读连接，否则 Windows 下临时文件无法删除"""
        from core import data_engine as de_mod
        conns = getattr(getattr(de_mod, '_thread_local', None), 'conns', None)
        if conns and db_path in conns:
            conns.pop(db_path).close()

    def _make_engine(self, db_path):
        from core.data_engine import DataEngine
        engine = DataEngine({})
        engine._kline_cache_path = db_path
        conn = sqlite3.connect(db_path)
        conn.execute(
            "CREATE TABLE IF NOT EXISTS fin_cache ("
            "code TEXT, eps REAL, roe REAL, profit REAL, income REAL, "
            "bvps REAL, report_date TEXT, fetched_at TEXT)")
        conn.commit()
        conn.close()
        return engine

    def _insert_fin(self, db_path, code, fetched_at):
        conn = sqlite3.connect(db_path)
        conn.execute(
            "INSERT INTO fin_cache VALUES (?,?,?,?,?,?,?,?)",
            (code, 1.23, 15.0, 1e9, 5e9, 10.0, '2026-06-30', fetched_at))
        conn.commit()
        conn.close()

    def test_fresh_cache_returned(self):
        """fetched_at 在 TTL 内 → 返回缓存数据"""
        fd, db = tempfile.mkstemp(suffix='.db')
        os.close(fd)
        try:
            engine = self._make_engine(db)
            self._insert_fin(db, '000001',
                             datetime.now().strftime('%Y-%m-%d %H:%M:%S'))
            got = engine._get_fin_from_cache('000001')
            self.assertIsNotNone(got, 'TTL 内的缓存被误判过期')
            self.assertAlmostEqual(got['eps'], 1.23)
        finally:
            self._close_engine_conn(db)
            os.unlink(db)

    def test_stale_cache_returns_none(self):
        """fetched_at 超过 90 天 → 返回 None（触发回源重查）"""
        fd, db = tempfile.mkstemp(suffix='.db')
        os.close(fd)
        try:
            engine = self._make_engine(db)
            stale = (datetime.now() - timedelta(days=91)).strftime('%Y-%m-%d %H:%M:%S')
            self._insert_fin(db, '000002', stale)
            self.assertIsNone(engine._get_fin_from_cache('000002'),
                              '过期缓存未触发回源（T1 未生效）')
        finally:
            self._close_engine_conn(db)
            os.unlink(db)


class TestT4QuotaCrossMidnight(unittest.TestCase):
    """T4：_read_quota_file 接受显式 today，跨午夜读取与写盘同口径"""

    def test_explicit_date_respected(self):
        from core.data_engine import DataEngine
        engine = DataEngine({})
        fd, path = tempfile.mkstemp(suffix='.json')
        os.close(fd)
        try:
            with io.open(path, 'w', encoding='utf-8') as f:
                json.dump({'date': '2026-01-01', 'used': 7}, f)
            engine._asharehub_quota_path = path
            self.assertEqual(engine._read_quota_file(today='2026-01-01'), 7,
                             '显式传入当日日期时未正确读取已用次数')
            self.assertEqual(engine._read_quota_file(today='2026-01-02'), 0,
                             '跨天后未按新日期归零（跨午夜口径未修复）')
        finally:
            os.unlink(path)


class TestT5RunContext(unittest.TestCase):
    """T5：_emit_run_context 写出含全部必需键的 run_context JSON"""

    REQUIRED_TOP_KEYS = {'run_id', 'date', 'mode', 'market', 'thresholds',
                         'regime', 'recommended', 'data_source_status'}
    REQUIRED_REGIME_KEYS = {'skip', 'position_scale', 'vol_breaker',
                            'losing_streak', 'killswitch', 'momentum_filter'}

    def test_emit_writes_required_keys(self):
        from strategies.short_term import ShortTermStrategy
        strategy = ShortTermStrategy({})
        run_id = 'unittest_rc_20260917'
        market = {'total': 5000, 'level': '中性市',
                  'details': {'up': 2400, 'down': 2500, 'median_chg': 0.31}}
        recommended = [{'code': '600000', 'score': 72.5}]
        strategy._emit_run_context(run_id, market, 3, 65.0, 1.0, 1.0, 1.0,
                                   None, recommended, is_backtest=False)
        path = os.path.join(PROJECT_ROOT, 'data', 'reports',
                            f'run_context_{run_id}.json')
        try:
            self.assertTrue(os.path.exists(path), 'run_context 文件未写出')
            with io.open(path, encoding='utf-8') as f:
                ctx = json.load(f)
            missing = self.REQUIRED_TOP_KEYS - set(ctx.keys())
            self.assertFalse(missing, f'run_context 缺键: {missing}')
            missing_regime = self.REQUIRED_REGIME_KEYS - set(ctx['regime'].keys())
            self.assertFalse(missing_regime,
                             f'run_context.regime 缺键: {missing_regime}')
            self.assertEqual(ctx['thresholds']['effective_min_score'], 65.0)
            self.assertEqual(ctx['thresholds']['effective_top_n'], 3)
            # run_id 回写关联：推荐条目应携带 run_id（predictions 可关联）
            self.assertEqual(recommended[0].get('run_id'), run_id,
                             'run_id 未回写到推荐条目')
            self.assertEqual(ctx['recommended'][0]['code'], '600000')
        finally:
            if os.path.exists(path):
                os.unlink(path)


class TestT8Reversal20dFactor(unittest.TestCase):
    """T8：reversal_20d = 100 - momentum 接入因子管线，权重 0 不影响评分"""

    def test_reversal_is_inverted_momentum(self):
        from core.factor_library import FactorLibrary
        lib = FactorLibrary()
        factors = lib.compute_all_factors({'rps_20': 80}, mode='short')
        self.assertIn('reversal_20d', factors, 'reversal_20d 未接入因子管线')
        self.assertAlmostEqual(factors['reversal_20d'],
                               100.0 - factors['momentum'])

    def test_weight_registered_and_applied(self):
        """reversal_20d 已登记且 v1.json/config.yml 一致、Σ=1.00（值无关断言）

        2026-09-18 改：原断言写死 0.42，每次调权重都要改测试；现只校验三条不变量
        （已登记 / 两层一致 / 合计 1.00），权重数值变更无需再改测试。
        """
        import io as _io
        import re as _re
        cfg_text = _io.open(os.path.join(PROJECT_ROOT, 'config.yml'),
                            encoding='utf-8').read()
        m = _re.search(r'(?m)^\s*reversal_20d:\s*([0-9.]+)', cfg_text)
        self.assertIsNotNone(m, 'config.yml 未登记 reversal_20d')
        cfg_val = float(m.group(1))
        v1 = json.load(_io.open(os.path.join(PROJECT_ROOT, 'data', 'weights', 'v1.json'),
                                encoding='utf-8'))
        short = v1.get('short', {})
        self.assertIn('reversal_20d', short, 'v1.json short 缺 reversal_20d')
        self.assertAlmostEqual(float(short['reversal_20d']), cfg_val, places=9,
                               msg='v1.json 与 config.yml 的 reversal_20d 不一致')
        self.assertGreater(cfg_val, 0, 'reversal_20d 权重应 > 0')
        self.assertAlmostEqual(sum(v for v in short.values()
                                   if isinstance(v, (int, float))), 1.0,
                               msg='v1.json short 权重合计应为 1.00')


class TestT12MinListingDays(unittest.TestCase):
    """T12：check_stock 使用 min_listing_days 校验 listing_days"""

    def _check(self, listing_days):
        from core.risk_filter import RiskFilter
        rf = RiskFilter({'min_listing_days': 60})
        info = {'code': '000001', 'name': '平安银行', 'price': 12.5,
                'pct_chg': 1.5, 'amount': 500_000_000,
                'name_raw': '平安银行', 'turnover': 2.5}
        if listing_days is not None:
            info['listing_days'] = listing_days
        return rf.check_stock(info)

    def test_insufficient_listing_days_rejected(self):
        result = self._check(30)
        reasons = result.get('reasons') or []
        self.assertTrue(any('上市不足' in str(r) for r in reasons),
                        f'listing_days=30 未触发上市不足拒绝: {reasons}')

    def test_sufficient_listing_days_not_rejected_for_this_rule(self):
        result = self._check(100)
        reasons = result.get('reasons') or []
        self.assertFalse(any('上市不足' in str(r) for r in reasons),
                          f'listing_days=100 被误判上市不足: {reasons}')

    def test_missing_listing_days_skipped_not_falsely_rejected(self):
        """上游无 listing_days 数据时跳过校验（不误伤）"""
        result = self._check(None)
        reasons = result.get('reasons') or []
        self.assertFalse(any('上市不足' in str(r) for r in reasons),
                          '缺失 listing_days 被误判上市不足（违反"缺失则跳过"约定）')


if __name__ == '__main__':
    unittest.main()
