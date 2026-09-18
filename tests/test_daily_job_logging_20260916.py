# -*- coding: utf-8 -*-
"""回归测试：调度器可观测性与配额预检（2026-09-16 P2-2 / P1-1）

背景：
  当日崩溃的定位成本 = 重跑整条流水线（含 AShareHub 配额），因为 `daily_job`
  是裸 `subprocess.call`：不落盘、不预检配额、失败无追溯线索。两次重跑把
  当日配额烧光，第三次运行四源全部降级却照常出推荐。

本文件锁定：
  A. 每次执行都落盘运行日志（命令 / 子进程输出 / 退出码 / 耗时）；
  B. 子进程失败时退出码透传，且日志中留有可定位的原始报错；
  C. 配额预检能识别"已达本地闸门"，明确告警（不阻断，避免推送旧简报）；
  D. 日志保留期清理可用；
  E. 跨日配额账本按"今日未消耗"处理。
"""
import json
import os
import shutil
import sys
import tempfile
import time
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, 'scripts'))

import daily_job as dj  # noqa: E402


class TestDailyJobObservability(unittest.TestCase):

    def setUp(self):
        self._orig_log_dir = dj.LOG_DIR
        self._orig_quota_file = dj.QUOTA_FILE
        self.tmp = tempfile.mkdtemp(prefix='dj_test_')
        dj.LOG_DIR = os.path.join(self.tmp, 'logs')
        dj.QUOTA_FILE = os.path.join(self.tmp, 'asharehub_quota.json')

    def tearDown(self):
        dj.LOG_DIR = self._orig_log_dir
        dj.QUOTA_FILE = self._orig_quota_file
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _write_quota(self, used, date=None):
        from core.trading_calendar import beijing_now
        d = date or beijing_now().strftime('%Y-%m-%d')
        with open(dj.QUOTA_FILE, 'w', encoding='utf-8') as f:
            json.dump({'date': d, 'used': used}, f)

    def _read_log(self):
        p = dj._log_path()
        if not os.path.exists(p):
            return ''
        with open(p, encoding='utf-8') as f:
            return f.read()

    # ── A. 日志落盘 ──

    def test_log_path_is_dated_and_under_log_dir(self):
        p = dj._log_path()
        self.assertTrue(p.startswith(dj.LOG_DIR))
        self.assertRegex(os.path.basename(p), r'^daily_job_\d{8}\.log$')

    def test_successful_run_is_logged(self):
        rc = dj._run('verify.py')          # 只读脚本，不触网、不消耗配额
        self.assertIn(rc, (0, 1))          # verify 可能报缺数据，但流程须走完
        text = self._read_log()
        self.assertIn('执行:', text)
        self.assertIn('verify.py 退出码', text)
        self.assertIn('耗时', text)

    # ── B. 失败透传 + 原始报错留痕 ──

    def test_failing_run_logs_error_and_returns_nonzero(self):
        rc = dj._run('__definitely_not_a_script__.py')
        self.assertNotEqual(rc, 0, '不存在的脚本必须返回非 0')
        text = self._read_log()
        self.assertIn('退出码', text)
        # 子进程原始报错必须落在日志里（否则无法事后定位）
        self.assertTrue(('.py' in text) and ('Error' in text or 'error' in text
                                            or 'can' in text),
                        f'日志未见原始报错:\n{text}')

    # ── C. 配额预检 ──

    def test_precheck_warns_when_gate_reached(self):
        self._write_quota(dj.ASHAREHUB_BUDGET - dj.QUOTA_RESERVE)   # 90
        with self.assertLogs('daily_job', level='ERROR') as cm:
            dj._precheck_quota()
        msg = '\n'.join(cm.output)
        self.assertIn('降级', msg)
        self.assertIn('90/100', msg)

    def test_precheck_warns_when_low_but_not_blocking(self):
        self._write_quota(int((dj.ASHAREHUB_BUDGET - dj.QUOTA_RESERVE) * 0.6))
        with self.assertLogs('daily_job', level='WARNING') as cm:
            dj._precheck_quota()
        self.assertIn('余量偏低', '\n'.join(cm.output))

    def test_precheck_silent_when_quota_plentiful(self):
        self._write_quota(3)
        records = []
        import logging as _lg
        h = _lg.Handler()
        h.emit = lambda r: records.append(r)
        lg = _lg.getLogger('daily_job')
        lg.addHandler(h)
        try:
            dj._precheck_quota()
        finally:
            lg.removeHandler(h)
        self.assertFalse([r for r in records
                          if r.levelno >= _lg.WARNING and '配额' in r.getMessage()])

    def test_precheck_never_raises_when_file_missing(self):
        if os.path.exists(dj.QUOTA_FILE):
            os.remove(dj.QUOTA_FILE)
        dj._precheck_quota()               # 不抛异常即通过

    # ── E. 跨日账本 ──

    def test_stale_quota_file_treated_as_zero(self):
        self._write_quota(95, date='2000-01-01')
        self.assertEqual(dj._read_quota(), (0, dj.ASHAREHUB_BUDGET))

    def test_current_quota_read(self):
        self._write_quota(42)
        self.assertEqual(dj._read_quota(), (42, dj.ASHAREHUB_BUDGET))

    # ── D. 日志清理 ──

    def test_prune_removes_only_expired_logs(self):
        os.makedirs(dj.LOG_DIR, exist_ok=True)
        old = os.path.join(dj.LOG_DIR, 'daily_job_20000101.log')
        new = os.path.join(dj.LOG_DIR, 'daily_job_29990101.log')
        keep = os.path.join(dj.LOG_DIR, 'other_file.txt')
        for p in (old, new, keep):
            with open(p, 'w', encoding='utf-8') as f:
                f.write('x')
        stale = time.time() - (dj.LOG_KEEP_DAYS + 1) * 86400
        os.utime(old, (stale, stale))
        dj._prune_logs()
        self.assertFalse(os.path.exists(old), '过期日志应被清理')
        self.assertTrue(os.path.exists(new), '未过期日志不得被删')
        self.assertTrue(os.path.exists(keep), '非日志文件不得被删')

    # ── 常量一致性（与 data_engine 的闸门口径对齐）──

    def test_gate_matches_data_engine(self):
        from core.data_engine import DataEngine
        de = DataEngine.__new__(DataEngine)
        budget = getattr(de, '_asharehub_budget', dj.ASHAREHUB_BUDGET)
        self.assertEqual(budget, dj.ASHAREHUB_BUDGET,
                         '预算常量与 data_engine 不一致，预检会失真')


if __name__ == '__main__':
    unittest.main()
