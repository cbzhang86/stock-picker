# -*- coding: utf-8 -*-
"""回归测试：同花顺强势股接口字段漂移（2026-09-16 P2-1）

背景：
  上游 2026-09 起 `getharden` 接口只返回 6 个字段
  （id / name / code / reason / date / market），旧 schema 的行情字段
  （close / zhangfu / huanshou / ddejingliang …）全部消失。
  旧实现 `row.get('涨幅%', 0)` 键不存在即静默回落 0 → 简报"题材热度 TOP10"
  整列渲染成 `+0.0% ➖`（伪造数据、无告警、源状态仍报"可用"）。

本文件锁定：
  A. 字段漂移必须产生 WARNING（不许静默）；
  B. 缺失涨跌幅一律为 None，绝不再回落 0；
  C. 简报不得输出伪造的 `+0.0%`；
  D. 真正的功能性字段（reason / 题材归因）缺失时才判定源不可用；
  E. 新旧两种 schema 下均不得抛异常。
"""
import logging
import unittest

import pandas as pd

from core.data_engine import DataEngine, sanitize_nan  # noqa: F401  (导入即触发模块级初始化)


# ── 上游最小响应体 ──

def _rows_new_schema():
    """2026-09 起的真实形态：仅 6 字段"""
    return [
        {'id': 1, 'name': '甲股份', 'code': '600001', 'reason': '算力+数据中心',
         'date': '2026-09-16', 'market': '沪A'},
        {'id': 2, 'name': '乙科技', 'code': '300002', 'reason': '算力+液冷',
         'date': '2026-09-16', 'market': '深A'},
        {'id': 3, 'name': '丙电子', 'code': '002003', 'reason': '液冷',
         'date': '2026-09-16', 'market': '深A'},
    ]


def _rows_old_schema():
    """旧 schema（含涨幅字段）——用于验证向后兼容"""
    return [
        {'id': 1, 'name': '甲股份', 'code': '600001', 'reason': '算力',
         'close': '10.00', 'zhangdie': '1.00', 'zhangfu': '11.11',
         'huanshou': '5.0', 'chengjiaoe': '1e8', 'chengjiaoliang': '1e7',
         'ddejingliang': '0.5', 'date': '2026-09-16', 'market': '沪A'},
        {'id': 2, 'name': '乙科技', 'code': '300002', 'reason': '液冷',
         'close': '20.00', 'zhangdie': '-0.50', 'zhangfu': '-2.44',
         'huanshou': '3.0', 'chengjiaoe': '2e8', 'chengjiaoliang': '2e7',
         'ddejingliang': '-0.2', 'date': '2026-09-16', 'market': '深A'},
    ]


class _FakeResp:
    def __init__(self, payload):
        self._payload = payload

    def json(self):
        return self._payload


class _FakeSession:
    def __init__(self, payload):
        self._payload = payload

    def get(self, *a, **kw):
        return _FakeResp(self._payload)


def _de_with(payload):
    """构造 DataEngine 并把上游请求打桩为给定的最小响应体"""
    import core.data_engine as de_mod
    eng = DataEngine.__new__(DataEngine)          # 跳过 __init__（避免建库/读配额）
    eng._source_status = {
        'ths_hot': {'available': True, 'last_error': None, 'label': '同花顺强势股'},
    }
    eng._source_available = {'ths_hot': True}
    de_mod._session = _FakeSession(payload)
    return eng


class TestThsSchemaDrift(unittest.TestCase):

    def _call(self, rows, payload=None):
        """返回 (df, engine)；payload 缺省时用 rows 作为完整响应"""
        if payload is None:
            payload = {'errocode': 0, 'data': rows}
        eng = _de_with(payload)
        return eng.get_ths_hot_stocks(), eng

    # ── A. 漂移必须响一声 ──

    def test_drift_raises_warning(self):
        with self.assertLogs('core.data_engine', level='WARNING') as cm:
            df, _ = self._call(_rows_new_schema())
        joined = '\n'.join(cm.output)
        self.assertIn('字段漂移', joined)
        self.assertIn('不再展示涨跌幅', joined)

    def test_drift_recorded_in_source_status(self):
        df, eng = self._call(_rows_new_schema())
        st = eng._source_status['ths_hot']
        # 数据仍可用（reason 在），但备注里必须留痕
        self.assertTrue(st['available'])
        self.assertIn('schema_changed', st['last_error'] or '')

    def test_no_drift_warning_on_old_schema(self):
        records = []
        handler = logging.Handler()
        handler.emit = lambda r: records.append(r)
        lg = logging.getLogger('core.data_engine')
        lg.addHandler(handler)
        try:
            self._call(_rows_old_schema())
        finally:
            lg.removeHandler(handler)
        self.assertFalse([r for r in records if '字段漂移' in r.getMessage()],
                         '旧 schema 不应报漂移')

    # ── B. 缺失不得回落 0 ──

    def test_missing_pct_is_none_not_zero(self):
        df, _ = self._call(_rows_new_schema())
        self.assertNotIn('涨幅%', df.columns)
        themes = DataEngine.extract_hot_themes(self._call(_rows_new_schema())[1], df)
        self.assertTrue(themes)
        for t in themes:
            for s in t['top_stocks']:
                self.assertIsNone(s['pct_chg'],
                                  '缺失涨幅必须是 None，不能伪造成 0')

    def test_old_schema_pct_parsed(self):
        df, eng = self._call(_rows_old_schema())
        self.assertIn('涨幅%', df.columns)
        themes = DataEngine.extract_hot_themes(eng, df)
        by_theme = {t['theme']: t for t in themes}
        # 有数据时按涨幅降序取 top（各题材内比较）
        self.assertAlmostEqual(by_theme['算力']['top_stocks'][0]['pct_chg'], 11.11)
        self.assertAlmostEqual(by_theme['液冷']['top_stocks'][0]['pct_chg'], -2.44)

    def test_nan_and_bad_values_become_none(self):
        rows = [{'id': 1, 'name': '甲', 'code': '600001', 'reason': '算力',
                 'date': 'x', 'market': '沪A'}]
        df, eng = self._call(rows)
        df = df.copy()
        df['涨幅%'] = float('nan')
        themes = DataEngine.extract_hot_themes(eng, df)
        self.assertIsNone(themes[0]['top_stocks'][0]['pct_chg'])

    # ── C. 简报不得输出伪造 0.0% ──

    def test_briefing_does_not_print_fake_zero_pct(self):
        import reports.market_briefing as mb
        import strategies.long_term as lt
        df, eng = self._call(_rows_new_schema())

        class _DE:
            def get_all_quotes(self):
                return pd.DataFrame()

            def get_north_flow_summary(self):
                return {}

            def get_ths_hot_stocks(self):
                return df

            def extract_hot_themes(self, d):
                return DataEngine.extract_hot_themes(eng, d)

            def get_data_source_summary(self):
                return {}

        orig_de, orig_lt = mb.DataEngine, lt.LongTermStrategy
        mb.DataEngine, lt.LongTermStrategy = _DE, (
            lambda cfg: type('S', (), {'run': lambda self: []})())
        try:
            text = mb.generate_market_briefing([], mode='short')
        finally:
            mb.DataEngine, lt.LongTermStrategy = orig_de, orig_lt
        self.assertNotIn('+0.0%', text, '不得输出伪造的 +0.0%')
        self.assertIn('题材热度 TOP 10', text)
        self.assertIn('算力', text)
        self.assertIn('代表:', text)

    def test_briefing_still_shows_pct_when_available(self):
        import reports.market_briefing as mb
        import strategies.long_term as lt
        df, eng = self._call(_rows_old_schema())

        class _DE:
            def get_all_quotes(self):
                return pd.DataFrame()

            def get_north_flow_summary(self):
                return {}

            def get_ths_hot_stocks(self):
                return df

            def extract_hot_themes(self, d):
                return DataEngine.extract_hot_themes(eng, d)

            def get_data_source_summary(self):
                return {}

        orig_de, orig_lt = mb.DataEngine, lt.LongTermStrategy
        mb.DataEngine, lt.LongTermStrategy = _DE, (
            lambda cfg: type('S', (), {'run': lambda self: []})())
        try:
            text = mb.generate_market_briefing([], mode='short')
        finally:
            mb.DataEngine, lt.LongTermStrategy = orig_de, orig_lt
        self.assertIn('+11.1%', text)

    # ── D. 功能性字段缺失 → 真不可用 ──

    def test_missing_reason_marks_source_unavailable(self):
        rows = [{'id': 1, 'name': '甲', 'code': '600001',
                 'date': 'x', 'market': '沪A'}]
        with self.assertLogs('core.data_engine', level='WARNING') as cm:
            df, eng = self._call(rows)
        self.assertTrue(df.empty)
        self.assertFalse(eng._source_status['ths_hot']['available'])
        self.assertIn('reason', '\n'.join(cm.output) + str(
            eng._source_status['ths_hot']['last_error']))

    # ── E. 异常与空响应路径 ──

    def test_empty_rows_returns_empty(self):
        df, eng = self._call([])
        self.assertTrue(df.empty)
        self.assertFalse(eng._source_status['ths_hot']['available'])

    def test_errocode_nonzero_returns_empty(self):
        df, eng = self._call([], payload={'errocode': 1, 'errormsg': 'x'})
        self.assertTrue(df.empty)
        self.assertFalse(eng._source_status['ths_hot']['available'])

    def test_extract_on_empty_df(self):
        eng = _de_with({'errocode': 0, 'data': []})
        self.assertEqual(DataEngine.extract_hot_themes(eng, pd.DataFrame()), [])


if __name__ == '__main__':
    unittest.main()
