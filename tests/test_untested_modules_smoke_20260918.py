# -*- coding: utf-8 -*-
"""零测试模块的最小冒烟测试（2026-09-18 全项目审查 P2-12）

覆盖此前无任何测试引用的 4 个 core 模块：
  backtest_store / data_quality_monitor / event_provider / fundamental_provider
目标不是完整覆盖，而是**建立回归网**：接口可导入、可在临时库上读写、缺失数据不崩。
"""
import os
import sys
import tempfile
import unittest

import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


class TestBacktestStoreSmoke(unittest.TestCase):
    """回测结果持久化 / 溯源（run_meta）——回测可复现性的关键路径"""

    def test_import_and_temp_db_roundtrip(self):
        from core.backtest_store import BacktestStore
        fd, db = tempfile.mkstemp(suffix='.db')
        os.close(fd)
        os.unlink(db)
        try:
            store = BacktestStore(db_path=db) if _accepts_db_path(BacktestStore) else BacktestStore()
            # 不依赖具体字段：仅验证实例化 + 列表查询可用（空库应返回空列表而非异常）
            runs = store.list_runs(5)
            self.assertIsInstance(runs, list)
        finally:
            for suf in ('', '-wal', '-shm'):
                p = db + suf
                if os.path.exists(p):
                    os.unlink(p)

    def test_get_run_meta_missing_returns_dict(self):
        """不存在的 run_id → 返回 dict（空）而非抛异常（只读，不写库）"""
        from core.backtest_store import BacktestStore
        store = BacktestStore()
        meta = store.get_run_meta(99999999)
        self.assertIsInstance(meta, dict)


class TestDataQualityMonitorSmoke(unittest.TestCase):

    def test_check_quotes_handles_empty_and_normal(self):
        from core.data_quality_monitor import DataQualityMonitor
        m = DataQualityMonitor()
        empty = pd.DataFrame(columns=['code', 'price', 'pct_chg', 'amount'])
        out = m.check_quotes(empty)
        self.assertIsInstance(out, dict, '空行情应返回结果字典而非异常')

        normal = pd.DataFrame({
            'code': ['600000', '000001'], 'price': [10.0, 20.0],
            'pct_chg': [1.0, -0.5], 'amount': [5e7, 6e7]})
        out2 = m.check_quotes(normal)
        self.assertIsInstance(out2, dict)


class TestEventProviderSmoke(unittest.TestCase):

    def test_score_and_classify_pure_functions(self):
        from core.event_provider import EventProvider
        # 事件强度分类：不依赖数据库
        v = EventProvider.classify_event('业绩预增 净利润同比增长 50%')
        self.assertIsInstance(v, (int, float))
        # 空事件列表打分应为中性/零（不崩）
        s = EventProvider.score([])
        self.assertIsInstance(s, (int, float))

    def test_upsert_and_read_on_temp_db(self):
        from core.event_provider import EventProvider
        fd, db = tempfile.mkstemp(suffix='.db')
        os.close(fd)
        os.unlink(db)
        try:
            p = _instantiate_with_db(EventProvider, db)
            if p is None:
                self.skipTest('EventProvider 不接受 db_path 参数')
            p.upsert([{'code': '600000', 'date': '2026-09-17',
                       'event_type': '业绩预增', 'title': '净利润预增 50%',
                       'impact': 1.0, 'source': 'unit-test'}])
            got = p.get_recent('600000', days=30)
            self.assertIsInstance(got, list)
        finally:
            for suf in ('', '-wal', '-shm'):
                q = db + suf
                if os.path.exists(q):
                    os.unlink(q)


class TestFundamentalProviderSmoke(unittest.TestCase):

    def test_get_missing_code_returns_none_or_dict(self):
        from core.fundamental_provider import FundamentalProvider
        p = FundamentalProvider()
        v = p.get('999999')
        self.assertTrue(v is None or isinstance(v, dict))

    def test_coverage_returns_dict(self):
        from core.fundamental_provider import FundamentalProvider
        p = FundamentalProvider()
        cov = p.coverage()
        self.assertIsInstance(cov, dict)


def _accepts_db_path(cls) -> bool:
    import inspect
    try:
        return 'db_path' in inspect.signature(cls.__init__).parameters
    except (TypeError, ValueError):
        return False


def _instantiate_with_db(cls, db_path):
    import inspect
    try:
        params = inspect.signature(cls.__init__).parameters
    except (TypeError, ValueError):
        return None
    if 'db_path' in params:
        return cls(db_path=db_path)
    if 'db' in params:
        return cls(db=db_path)
    return None


if __name__ == '__main__':
    unittest.main()
