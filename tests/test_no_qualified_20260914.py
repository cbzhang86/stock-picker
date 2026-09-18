"""
零推荐时透出"最高分标的" — 单元测试（2026-09-14）

覆盖需求：因市场原因（市况偏弱 → 动态门槛上浮）导致无推荐标的时，
除说明无推荐外，还需展示当日得分最高的单个标的并明确标注分数。
其余逻辑与既有实现保持一致。

全部离线可跑（无网络、Dummy DataEngine）：

  1. TestRankStocksDiagnostics   rank_stocks 门槛清零时外送 top_unqualified；
                                 不传 diagnostics 时行为与历史完全一致
  2. TestBriefingNoQualified     简报在无推荐分支展示最高分标的与差距
  3. TestDailyReportNoQualified  日报生成器遇到元信息条目不产生 N/A 行
  4. TestMarkerContract          元信息条目无 code（下游据此区分推荐/元信息）

运行：
  python -m unittest tests.test_no_qualified_20260914 -v
"""

import os
import sys
import unittest

import pandas as pd

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)

from core.scoring_model import ScoringModel


def _stocks():
    """最小可用候选集：字段缺失走中性逻辑，分数不会超过 101 的门槛。"""
    return [
        {'code': '000001', 'name': '测试甲', 'price': 10.0, 'pct_chg': 1.0},
        {'code': '000002', 'name': '测试乙', 'price': 20.0, 'pct_chg': -1.0,
         'main_fund_accumulated': 12000},
    ]


class TestRankStocksDiagnostics(unittest.TestCase):
    """rank_stocks 的门槛诊断外送机制"""

    def setUp(self):
        self.sm = ScoringModel()

    def test_diagnostics_filled_when_all_filtered(self):
        diag = {}
        res = self.sm.rank_stocks(_stocks(), mode='short', top_n=3,
                                  min_score=101, diagnostics=diag)
        self.assertEqual(res, [], '返回列表应与历史一致（空）')
        top = diag.get('top_unqualified')
        self.assertIsNotNone(top, '门槛清零时应写入 top_unqualified')
        for key in ('code', 'name', 'score', 'min_score', 'gap', 'candidates'):
            self.assertIn(key, top)
        self.assertEqual(top['min_score'], 101.0)
        self.assertGreater(top['gap'], 0, '未达门槛时差距应为正')
        self.assertEqual(top['candidates'], len(_stocks()))

    def test_no_side_effect_without_diagnostics(self):
        """不传 diagnostics：签名兼容、无任何副作用（回测等调用方不受影响）"""
        res = self.sm.rank_stocks(_stocks(), mode='short', top_n=3, min_score=101)
        self.assertEqual(res, [])

    def test_no_top_unqualified_when_qualified(self):
        """有达标标的时不写诊断（该分支只在清零时触发）"""
        diag = {}
        res = self.sm.rank_stocks(_stocks(), mode='short', top_n=3,
                                  min_score=0, diagnostics=diag)
        self.assertTrue(res, '门槛为 0 时应返回候选')
        self.assertNotIn('top_unqualified', diag)


class _DummyDataEngine:
    """避免简报生成器触网"""

    def get_all_quotes(self):
        return pd.DataFrame()

    def get_north_flow_summary(self):
        return {}

    def get_ths_hot_stocks(self):
        return pd.DataFrame()

    def get_data_source_summary(self):
        return {}


def _marker():
    """策略在门槛清零时返回的元信息条目（与 short_term.run 保持一致）"""
    return {
        'no_qualified': True,
        'min_score': 65,
        'market_assessment': {'total': 52.0, 'level': '中性市', 'details': {}},
        'data_source_status': {},
        'strategy_health': {'triggered': False, 'cum_return': None, 'n_days': 0},
        'top_candidate': {'code': '002517', 'name': '恺英网络', 'score': 64.9,
                          'min_score': 65.0, 'gap': 0.1, 'candidates': 132},
    }


class TestBriefingNoQualified(unittest.TestCase):
    """简报：无推荐分支展示最高分标的"""

    def test_market_briefing_shows_top_candidate(self):
        import reports.market_briefing as mb
        import strategies.long_term as lt

        orig_de, orig_lt = mb.DataEngine, lt.LongTermStrategy
        mb.DataEngine = _DummyDataEngine
        lt.LongTermStrategy = lambda cfg: type('StubLT', (), {'run': lambda self: []})()
        try:
            text = mb.generate_market_briefing([_marker()], mode='short')
        finally:
            mb.DataEngine, lt.LongTermStrategy = orig_de, orig_lt

        self.assertIn('今日无评分达标标的', text)
        self.assertIn('最高分标的', text)
        self.assertIn('002517', text)
        self.assertIn('64.90', text, '分数必须明确标注')
        self.assertIn('差 0.10 分', text)
        self.assertNotIn('N/A', text, '元信息条目不应落入逐条推荐渲染')

    def test_market_briefing_plain_empty_list(self):
        """既无推荐也无元信息（老行为）时仍输出说明行，不报错"""
        import reports.market_briefing as mb
        import strategies.long_term as lt

        orig_de, orig_lt = mb.DataEngine, lt.LongTermStrategy
        mb.DataEngine = _DummyDataEngine
        lt.LongTermStrategy = lambda cfg: type('StubLT', (), {'run': lambda self: []})()
        try:
            text = mb.generate_market_briefing([], mode='short')
        finally:
            mb.DataEngine, lt.LongTermStrategy = orig_de, orig_lt

        self.assertIn('今日无评分达标标的', text)
        self.assertNotIn('最高分标的', text)


class TestDailyReportNoQualified(unittest.TestCase):
    """日报生成器：元信息条目单独渲染"""

    def test_daily_report_marker(self):
        from reports.backtest_report import generate_daily_report
        md = generate_daily_report([_marker()], 'short')
        self.assertIn('最高分标的', md)
        self.assertIn('64.90', md)
        self.assertNotIn('N/A', md)

    def test_daily_report_empty(self):
        from reports.backtest_report import generate_daily_report
        md = generate_daily_report([], 'short')
        self.assertIn('没有评分合格的标的', md)


class TestMarkerContract(unittest.TestCase):
    """契约：元信息条目无 code —— 下游据此区分推荐与元信息"""

    def test_marker_has_no_code(self):
        self.assertNotIn('code', _marker())
        self.assertTrue(_marker().get('no_qualified'))

    def test_briefing_filter_excludes_marker(self):
        """简报的推荐筛选条件（r.get('code')）必须排除元信息条目"""
        recs = [_marker(), {'code': '600000', 'name': '真推荐', 'score': 80}]
        real = [r for r in recs if r.get('code')]
        self.assertEqual(len(real), 1)
        self.assertEqual(real[0]['code'], '600000')


if __name__ == '__main__':
    unittest.main(verbosity=2)
