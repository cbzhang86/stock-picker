"""
2026-09-17 回归测试：非交易日守卫默认开启（缺陷2修复）

背景：eod_stock_picker.py 原有 --skip-non-trading-day 默认 False，仅 daily_job
显式带上；手工直跑 eod_stock_picker.py 会在非交易日把假日日期写入 predictions.db，
污染"最近 N 个交易日"口径（kill-switch / 权重校准）。库里 06-13 周六 / 06-19 端午
/ 07-04 周六 三条记录即此成因。

修复后：守卫默认开启（非交易日不落库、不采集因子，仅生成报告）；仅显式
--allow-non-trading-day 才允许落库。旧 --skip-non-trading-day 保留为废弃别名
（语义与默认一致=跳过）以兼容 pick.py。
"""
import sys
import unittest
from unittest import mock

import scripts.eod_stock_picker as eod


class _StopMain(Exception):
    """用于在 main() 调起 run_short_term 后提前终止，便于断言解析结果。"""


_REC = {
    'code': '000001', 'name': '测试', 'score': 80.0, 'rating': 'A',
    'price': 10.0, 'breakdown': {},
}


def _patch_runner(test_case, allow_value, is_trading_day_return):
    """构造 run_short_term 所需的全套 mock，返回供断言的句柄。"""
    strat = mock.MagicMock()
    strat.run.return_value = [_REC]
    collector = mock.MagicMock()
    tracker = mock.MagicMock()
    tracker.has_predictions.return_value = False  # 不触发批次防重提前 return

    ctx = mock.patch.object(eod, 'ShortTermStrategy', return_value=strat)
    ctx_collector = mock.patch.object(eod, 'FactorDataCollector', return_value=collector)
    ctx_tracker = mock.patch.object(eod, 'PredictionTracker', return_value=tracker)
    ctx_cal = mock.patch('core.trading_calendar.is_trading_day',
                         return_value=is_trading_day_return)
    ctx.__enter__()
    ctx_collector.__enter__()
    ctx_tracker.__enter__()
    ctx_cal.__enter__()
    return {
        'strat': strat, 'collector': collector, 'tracker': tracker,
        'exit': [ctx, ctx_collector, ctx_tracker, ctx_cal],
    }


class TestNonTradingGuardBehavior(unittest.TestCase):
    """行为级：非交易日 + 默认值 → 不落库；+ --allow → 落库。"""

    def _run(self, allow_value, is_trading_day_return=False):
        handles = _patch_runner(self, allow_value, is_trading_day_return)
        try:
            eod.run_short_term({}, allow_non_trading_day=allow_value)
        finally:
            for c in handles['exit']:
                c.__exit__(None, None, None)
        return handles

    def test_nontrading_day_default_does_not_write(self):
        """非交易日 + 默认参数（守卫默认开启）→ 不写 predictions、不采集因子。"""
        h = self._run(allow_value=False, is_trading_day_return=False)
        h['collector'].collect.assert_not_called()
        h['tracker'].log_prediction.assert_not_called()

    def test_nontrading_day_allow_flag_writes(self):
        """非交易日 + --allow-non-trading-day → 落库（因子采集 + 写入预测）。"""
        h = self._run(allow_value=True, is_trading_day_return=False)
        h['collector'].collect.assert_called_once()
        h['tracker'].log_prediction.assert_called_once()

    def test_trading_day_always_writes(self):
        """交易日（无论 allow 标志）→ 正常落库。"""
        h = self._run(allow_value=False, is_trading_day_return=True)
        h['collector'].collect.assert_called_once()
        h['tracker'].log_prediction.assert_called_once()


class TestNonTradingGuardArgparse(unittest.TestCase):
    """参数解析层面：默认 allow=False；--allow-non-trading-day 置 True；
    --skip-non-trading-day 仍被接受（废弃别名，不改变默认跳过行为）。"""

    def _parse_allow(self, argv):
        captured = {}

        def fake_run(config, allow_non_trading_day=False):
            captured['allow'] = allow_non_trading_day
            raise _StopMain()

        with mock.patch.object(eod, 'run_short_term', fake_run), \
             mock.patch.object(eod, 'load_config', return_value={}), \
             mock.patch.object(eod, 'show_status', return_value=None), \
             mock.patch.object(sys, 'argv', argv):
            try:
                eod.main()
            except _StopMain:
                pass
        return captured.get('allow')

    def test_default_is_skip(self):
        allow = self._parse_allow(['eod_stock_picker.py', '--mode', 'short'])
        self.assertFalse(allow)

    def test_allow_flag_parsed(self):
        allow = self._parse_allow(
            ['eod_stock_picker.py', '--mode', 'short', '--allow-non-trading-day'])
        self.assertTrue(allow)

    def test_deprecated_skip_flag_still_accepted_and_default_skip(self):
        # --skip-non-trading-day 仍被 argparse 接受；且不改变默认跳过（allow 仍为 False）
        allow = self._parse_allow(
            ['eod_stock_picker.py', '--mode', 'short', '--skip-non-trading-day'])
        self.assertFalse(allow)


if __name__ == "__main__":
    unittest.main()
