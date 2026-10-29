# -*- coding: utf-8 -*-
"""交接单（2026-09-23）ASHareHub 配额机制修复的守卫测试。

覆盖：R1 配额冻结 / R2 消费点守卫 / A2 归属 / A3 分级熔断 / A6 常量一致 /
A7-② 闸门快路径（不空转）。

**本测试不消耗任何真实配额**：全部在临时目录的账本路径上操作，
且 `_quota_frozen` 用例下根本不会写盘。
"""
import io
import json
import os
import sys
import tempfile
import time
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, 'scripts'))


def _fresh_engine(quota_path: str):
    from core.data_engine import DataEngine
    de = DataEngine(config={})
    de._asharehub_quota_path = quota_path
    de._asharehub_budget_date = ''
    de._asharehub_budget_used = 0
    de._asharehub_gate_hit_date = ''
    de._quota_gate_warned = False
    return de


class TestR1QuotaFrozen(unittest.TestCase):
    """R1：回测/离线路径冻结配额 —— 不扣额、不写生产账本、不标记源故障。"""

    def test_frozen_never_consumes_and_never_writes(self):
        with tempfile.TemporaryDirectory() as tmp:
            qp = os.path.join(tmp, 'quota.json')
            de = _fresh_engine(qp)
            de._quota_frozen = True
            for _ in range(20):
                self.assertFalse(de._asharehub_budget_ok(),
                                 '冻结状态下不得消耗配额')
            self.assertEqual(de._asharehub_budget_used, 0)
            self.assertFalse(os.path.exists(qp),
                             '冻结状态下不得写配额账本（生产归因不被回测污染）')
            # 源状态不得被标记为故障（只是"回测选择不调用"）
            for k in ('asharehub_moneyflow', 'asharehub_tech_factors',
                      'asharehub_concepts', 'asharehub_financial',
                      'akshare_north_flow'):
                self.assertTrue(de._source_available.get(k, True),
                                f'冻结不应把 {k} 标记为故障')

    def test_frozen_covers_north_flow_key(self):
        """R1 核心：冻结覆盖 akshare_north_flow（旧做法的跳过集合遗漏它）。"""
        with tempfile.TemporaryDirectory() as tmp:
            de = _fresh_engine(os.path.join(tmp, 'quota.json'))
            de._quota_frozen = True
            self.assertFalse(de._asharehub_budget_ok())
            # 北向取数路径在 budget 检查处即返回 None（不触达实盘 API）
            self.assertIsNone(de.get_north_flow_accumulated('600519'))

    def test_unfrozen_consumes_normally(self):
        """未冻结时行为不变（记账 + 自增）。"""
        with tempfile.TemporaryDirectory() as tmp:
            qp = os.path.join(tmp, 'quota.json')
            de = _fresh_engine(qp)
            self.assertTrue(de._asharehub_budget_ok())
            self.assertEqual(de._read_quota_file(), 1)
            self.assertEqual(de._asharehub_budget_used, 1)


class TestA7GateFastPath(unittest.TestCase):
    """A7-②/闸门快路径：命中后不再节流空转（原 0.3s×N）。"""

    def test_gate_hit_short_circuits_throttle(self):
        with tempfile.TemporaryDirectory() as tmp:
            de = _fresh_engine(os.path.join(tmp, 'quota.json'))
            from datetime import datetime
            de._asharehub_gate_hit_date = datetime.now().strftime('%Y-%m-%d')
            de._asharehub_budget_date = de._asharehub_gate_hit_date
            t0 = time.time()
            for _ in range(5):
                self.assertFalse(de._asharehub_budget_ok())
            elapsed = time.time() - t0
            self.assertLess(elapsed, 0.25,
                            f'闸门命中后应走快路径（跳过 0.3s 节流），实际 {elapsed:.2f}s')

    def test_cross_day_resets_gate_flag(self):
        with tempfile.TemporaryDirectory() as tmp:
            de = _fresh_engine(os.path.join(tmp, 'quota.json'))
            de._asharehub_gate_hit_date = '2000-01-01'   # 过期标记
            self.assertTrue(de._asharehub_budget_ok(),
                            '跨天后闸门标记应失效并恢复消耗')
            self.assertNotEqual(de._asharehub_gate_hit_date, '2000-01-01')


class TestA3TieredMarking(unittest.TestCase):
    """A3：配额耗尽按源价值分级 —— concepts 保活（hot_theme 0.55 走缓存）。"""

    def test_concepts_kept_others_marked(self):
        with tempfile.TemporaryDirectory() as tmp:
            de = _fresh_engine(os.path.join(tmp, 'quota.json'))
            de.set_quota_owner('unit-test')
            de._mark_asharehub_quota_exhausted()
            self.assertFalse(de._source_available['asharehub_moneyflow'])
            self.assertFalse(de._source_available['asharehub_tech_factors'])
            self.assertFalse(de._source_available['asharehub_financial'])
            self.assertTrue(de._source_available['asharehub_concepts'],
                            'concepts 必须保活（其 7 天预取缓存供 hot_theme 0.55）')
            err = (de._source_status.get('asharehub_moneyflow') or {}).get('last_error', '')
            self.assertIn('unit-test', err, '降级文案必须含归属（A2 可归因）')
            self.assertIn('ASHAREHUB', err.replace('AShareHub', 'ASHAREHUB').upper())


class TestA2QuotaOwner(unittest.TestCase):
    """A2：账本归属 + 追加字段不破坏既有签名/读取方。"""

    def test_owner_and_last_ts_written_readable(self):
        with tempfile.TemporaryDirectory() as tmp:
            qp = os.path.join(tmp, 'quota.json')
            de = _fresh_engine(qp)
            de.set_quota_owner('backtest:short')
            self.assertTrue(de._asharehub_budget_ok())
            with io.open(qp, encoding='utf-8') as f:
                d = json.load(f)
            self.assertEqual(d.get('owner'), 'backtest:short')
            self.assertIn('last_ts', d)
            # _read_quota_file 仍返回 int（既有签名不变）
            self.assertIsInstance(de._read_quota_file(), int)
            self.assertEqual(de._read_quota_file(), 1)

    def test_old_ledger_without_owner_is_tolerated(self):
        with tempfile.TemporaryDirectory() as tmp:
            qp = os.path.join(tmp, 'quota.json')
            from datetime import datetime
            with io.open(qp, 'w', encoding='utf-8') as f:
                json.dump({'date': datetime.now().strftime('%Y-%m-%d'),
                           'used': 7}, f)
            de = _fresh_engine(qp)
            self.assertEqual(de._read_quota_file(), 7)   # 旧账本可读
            self.assertEqual(de._quota_owner, 'unknown')  # 默认兜底

    def test_set_quota_owner_truncates(self):
        with tempfile.TemporaryDirectory() as tmp:
            de = _fresh_engine(os.path.join(tmp, 'quota.json'))
            de.set_quota_owner('x' * 200)
            self.assertLessEqual(len(de._quota_owner), 60)


class TestA6ReserveConsistency(unittest.TestCase):
    """A6：预留量单一常量，守卫两侧必须相等（原先只断言 budget）。"""

    def test_reserve_constants_match(self):
        from core.data_engine import DataEngine
        import daily_job
        self.assertEqual(DataEngine({}).ASHAREHUB_RESERVE,
                         daily_job.QUOTA_RESERVE,
                         'DataEngine.ASHAREHUB_RESERVE 必须与 daily_job.QUOTA_RESERVE 一致')
        self.assertEqual(DataEngine({})._asharehub_budget,
                         daily_job.ASHAREHUB_BUDGET,
                         '日配额常量两侧必须一致')

    def test_no_hardcoded_reserve_left(self):
        """锚点守卫：data_engine / prefetch 内不得再出现 `- 10` 的预留硬编码。"""
        for rel in ('core/data_engine.py', 'scripts/prefetch_asharehub.py'):
            src = io.open(os.path.join(ROOT, rel), encoding='utf-8').read()
            self.assertNotIn('_asharehub_budget - 10', src,
                             f'{rel} 仍存在预留硬编码 `- 10`，应引用 ASHAREHUB_RESERVE')


class TestR2ConsumptionGuards(unittest.TestCase):
    """R2：short_term 的 4 个 AShareHub 消费点都必须带回测守卫。"""

    def test_guards_present(self):
        src = io.open(os.path.join(ROOT, 'strategies', 'short_term.py'),
                      encoding='utf-8').read()
        for anchor in ('if (not is_backtest) and rank_index < 10:',
                       'if (not is_backtest) and rank_index < 20:',
                       'if (not is_backtest) and rank_index < 15:',
                       'skip_ash = is_backtest or idx >= 10'):
            self.assertIn(anchor, src,
                          f'AShareHub 消费点缺少回测守卫: {anchor}')

    def test_backtest_sets_freeze(self):
        src = io.open(os.path.join(ROOT, 'core', 'backtest_engine.py'),
                      encoding='utf-8').read()
        self.assertIn('_quota_frozen = True', src,
                      '回测入口必须冻结配额（不得再依赖逐 key 置 _source_available）')
        self.assertNotIn("'akshare_north_flow')", src.split('_quota_frozen = True')[0][-800:],
                         '不应再使用逐 key 置 False 的旧做法（R1 假保护）')


class TestA3VisibilityInBriefing(unittest.TestCase):
    """A3 配套：预算受限但保活的源必须在简报可见（否则漏报 hot_theme 0.55 风险）。"""

    def test_degraded_flag_set_on_gate_message(self):
        de = _fresh_engine(os.path.join(tempfile.gettempdir(), 'x_quota.json'))
        de._update_source_status('asharehub_concepts', True,
                                 'AShareHub 已达本地安全闸门(90/100)，预算受限：仅缓存')
        st = de._source_status['asharehub_concepts']
        self.assertTrue(st['available'])
        self.assertTrue(st.get('degraded'),
                        '闸门相关文案必须把该源标记为 degraded（可用但受预算限制）')

    def test_briefing_renders_budget_limited_block(self):
        from reports.market_briefing import generate_market_briefing
        snap = {
            'asharehub_moneyflow': {'available': False, 'degraded': True,
                                    'last_error': 'AShareHub 已达本地安全闸门(90/100)',
                                    'label': '个股资金流(AShareHub)'},
            'asharehub_concepts': {'available': True, 'degraded': True,
                                   'last_error': 'AShareHub 已达本地安全闸门(90/100)，预算受限',
                                   'label': '概念板块(AShareHub)'},
        }
        txt = generate_market_briefing([], mode='short', source_status=snap)
        self.assertIn('数据源预算受限', txt)
        self.assertIn('概念板块(AShareHub)', txt)


if __name__ == '__main__':
    unittest.main()
