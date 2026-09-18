# -*- coding: utf-8 -*-
"""回归测试：简报必须显式呈现运行期数据源降级（2026-09-16 P1-1）

背景：
  2026-09-16 崩溃重试烧尽 AShareHub 日配额后，第 3 次运行的四源（个股资金流 /
  技术因子 / 概念板块 / 财务指标）全部降为中性，却照常输出"建议买入"且推送无警示。
  根因：generate_market_briefing 内部 `DataEngine()` 新建实例的 _source_status
  在 __init__ 中默认全绿，与运行期真实熔断状态无关 → 简报永远不显示降级。

本文件锁定：
  A. 传入运行期快照（含 unavailable 源）→ 首屏必须出现警示 + 源名称；
  B. 未传入且内部快照全绿 → 不得凭空出现警示（防误报）；
  C. 警示必须排在市场概览/推荐之前（首屏位置，避免被长文淹没）。
"""
import re
import unittest

import pandas as pd


class _DummyDataEngine:
    """避免简报生成器触网；模拟"运行期无故障"的内部快照"""

    def get_all_quotes(self):
        return pd.DataFrame()

    def get_north_flow_summary(self):
        return {}

    def get_ths_hot_stocks(self):
        return pd.DataFrame()

    def get_data_source_summary(self):
        return {
            'akshare_codes': {'available': True, 'last_error': None, 'label': 'A股代码列表'},
            'tencent_quote': {'available': True, 'last_error': None, 'label': '腾讯实时行情'},
        }


def _quota_exhausted_snapshot():
    """复刻 2026-09-16 第三次运行的运行期快照（AShareHub 四源全不可用）"""
    snap = {
        'akshare_codes': {'available': True, 'last_error': None, 'label': 'A股代码列表'},
        'tencent_quote': {'available': True, 'last_error': None, 'label': '腾讯实时行情'},
    }
    err = 'AShareHub 已达本地安全闸门(90/100，预留 10 次)，因子降为中性'
    for k, label in (('asharehub_moneyflow', '个股资金流(AShareHub)'),
                     ('asharehub_tech_factors', '技术因子(AShareHub)'),
                     ('asharehub_concepts', '概念板块(AShareHub)'),
                     ('asharehub_financial', '财务指标(AShareHub)')):
        snap[k] = {'available': False, 'last_error': err, 'label': label}
    return snap


def _rec():
    return {'code': '600163', 'name': '中闽能源', 'score': 65.06,
            'rating_cn': '增持', 'allocation_pct': 40.0, 'breakdown': {}}


class TestBriefingDegradationBanner(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        import reports.market_briefing as mb
        import strategies.long_term as lt
        cls._mb, cls._lt = mb, lt
        cls._orig_de, cls._orig_lt = mb.DataEngine, lt.LongTermStrategy
        mb.DataEngine = _DummyDataEngine
        lt.LongTermStrategy = lambda cfg: type('StubLT', (), {'run': lambda self: []})()

    @classmethod
    def tearDownClass(cls):
        cls._mb.DataEngine = cls._orig_de
        cls._lt.LongTermStrategy = cls._orig_lt

    def _render(self, recs, source_status=None):
        return self._mb.generate_market_briefing(
            recs, mode='short', source_status=source_status)

    # ── A. 降级必须可见 ──

    def test_warns_when_runtime_snapshot_has_failures(self):
        text = self._render([_rec()], _quota_exhausted_snapshot())
        self.assertIn('数据源降级警示', text)
        self.assertIn('个股资金流(AShareHub)', text)
        self.assertIn('技术因子(AShareHub)', text)
        self.assertIn('概念板块(AShareHub)', text)

    def test_reports_affected_factor_weight_share(self):
        """必须把"接口不可用"翻译成"影响了多少因子权重" """
        text = self._render([_rec()], _quota_exhausted_snapshot())
        self.assertRegex(text, r'受影响因子权重合计约 \d+%')
        # hot_theme 0.50 + capital_flow 0.15 + technical 0.16 + fundamental/valuation
        # 取到的是生效权重，断言其显著大于 0 且不超过 100%
        m = re.search(r'受影响因子权重合计约 (\d+)%', text)
        self.assertGreater(int(m.group(1)), 30)
        self.assertLessEqual(int(m.group(1)), 100)
        self.assertIn('谨慎参考', text)

    def test_whole_market_source_failure_says_all_factors(self):
        snap = dict(_quota_exhausted_snapshot())
        snap['tencent_quote'] = {'available': False, 'last_error': 'timeout',
                                 'label': '腾讯实时行情'}
        text = self._render([_rec()], snap)
        self.assertIn('全部因子', text)

    # ── B. 无故障不得误报 ──

    def test_no_banner_when_all_sources_healthy(self):
        healthy = {'tencent_quote': {'available': True, 'last_error': None,
                                     'label': '腾讯实时行情'}}
        text = self._render([_rec()], healthy)
        self.assertNotIn('数据源降级警示', text)

    def test_falls_back_to_internal_when_not_passed(self):
        """未传快照（如长线/外部调用）：回落内部实例，全绿时不得出现警示"""
        text = self._render([_rec()], None)
        self.assertNotIn('数据源降级警示', text)

    def test_empty_snapshot_is_falsy_and_safe(self):
        text = self._render([_rec()], {})
        self.assertNotIn('数据源降级警示', text)

    # ── C. 首屏位置 ──

    def test_banner_precedes_market_overview(self):
        text = self._render([_rec()], _quota_exhausted_snapshot())
        self.assertLess(text.index('数据源降级警示'), text.index('市场概览'))
        self.assertLess(text.index('数据源降级警示'), text.index('600163'))

    # ── D. 与既有分支兼容 ──

    def test_works_with_no_qualified_marker(self):
        marker = {'no_qualified': True, 'min_score': 65, 'top_candidate': {},
                  'market_assessment': {'total': 52.0, 'level': '中性市', 'details': {}},
                  'data_source_status': _quota_exhausted_snapshot()}
        text = self._render([marker], _quota_exhausted_snapshot())
        self.assertIn('数据源降级警示', text)
        self.assertIn('无评分达标标的', text)

    def test_works_with_skip_reason_entry(self):
        entry = {'skip_reason': '市场赚钱效应较差(极差市)，建议空仓观望或减仓',
                 'market_assessment': {'total': 10.0, 'level': '极差市', 'details': {}}}
        text = self._render([entry], _quota_exhausted_snapshot())
        self.assertIn('数据源降级警示', text)
        self.assertIn('停推', text)

    def test_empty_recommendations_no_crash(self):
        text = self._render([], _quota_exhausted_snapshot())
        self.assertIn('数据源降级警示', text)


if __name__ == '__main__':
    unittest.main()
