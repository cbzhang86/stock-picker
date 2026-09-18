# -*- coding: utf-8 -*-
"""统一 CLI 入口（2026-09-07 顺手项）：一个命令记住所有常用操作。

用法：
  python scripts/pick.py pick      [--mode short|long] [--skip-non-trading-day]
  python scripts/pick.py backfill                          # T+1/T+5/T+20 结果回填
  python scripts/pick.py health                            # 快速门禁（9 项）
  python scripts/pick.py oos [--start ... --end ...]       # 全量 OOS 诊断（含 daily_ics）
  python scripts/pick.py backtest --start ... --end ... [--ret hold1d]
  python scripts/pick.py prefetch [--limit N] [--dry-run]  # 非交易日配额预取
  python scripts/pick.py calibrate [--method icir]         # 权重校准建议（不自动生效）
  python scripts/pick.py slippage [--days N]               # 尾盘滑点校准（只读）

设计：以子进程方式调用对应脚本（隔离与转发参数，不重复实现逻辑），
退出码原样透传。
"""
import argparse
import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if os.path.isdir(os.path.join(ROOT, 'core')):
    sys.path.insert(0, ROOT)
PY311 = r'C:\Users\Administrator\AppData\Local\Programs\Python\Python311\python.exe'
PY = PY311 if os.path.exists(PY311) else sys.executable


def run(script: str, extra: list) -> int:
    cmd = [PY, os.path.join(ROOT, 'scripts', script)] + extra
    print(f"> {' '.join(cmd)}", flush=True)
    return subprocess.call(cmd, cwd=ROOT)


def main() -> int:
    ap = argparse.ArgumentParser(description='A股尾盘选股系统 · 统一入口')
    sub = ap.add_subparsers(dest='cmd', required=True)

    sp = sub.add_parser('pick', help='尾盘选股（生成简报，交易日自动分流请用 daily_job）')
    sp.add_argument('--mode', choices=['short', 'long'], default='short')
    sp.add_argument('--skip-non-trading-day', action='store_true')

    sub.add_parser('backfill', help='回填 T+1/T+5/T+20 结果')

    sub.add_parser('health', help='快速门禁（9 项断言）')

    sp = sub.add_parser('oos', help='全量 OOS 因子诊断')
    sp.add_argument('--start'); sp.add_argument('--end')
    sp.add_argument('--ret', default='hold1d')

    sp = sub.add_parser('backtest', help='历史回测')
    sp.add_argument('--start', required=True); sp.add_argument('--end', required=True)
    sp.add_argument('--ret', default='hold1d')

    sp = sub.add_parser('prefetch', help='ASHareHub 配额预取（非交易日）')
    sp.add_argument('--limit', type=int, default=30)
    sp.add_argument('--dry-run', action='store_true')

    sp = sub.add_parser('calibrate', help='权重校准建议（不自动生效）')
    sp.add_argument('--method', choices=['consensus', 'icir'], default='consensus')

    sp = sub.add_parser('slippage', help='尾盘滑点校准（只读）')
    sp.add_argument('--days', type=int, default=5)

    args, extra = ap.parse_known_args()

    if args.cmd == 'pick':
        e = ['--mode', args.mode]
        if args.skip_non_trading_day:
            e.append('--skip-non-trading-day')
        return run('eod_stock_picker.py', e)
    if args.cmd == 'backfill':
        return run('backfill_pending.py', [])
    if args.cmd == 'health':
        return run('evaluate_all.py', [])
    if args.cmd == 'oos':
        e = ['--ret', args.ret]
        if args.start:
            e += ['--start', args.start]
        if args.end:
            e += ['--end', args.end]
        return run('evaluate_all.py', e + ['--full'])
    if args.cmd == 'backtest':
        return run('run_backtest.py', ['--start', args.start, '--end', args.end,
                                       '--ret', args.ret])
    if args.cmd == 'prefetch':
        e = ['--limit', str(args.limit)]
        if args.dry_run:
            e.append('--dry-run')
        return run('prefetch_asharehub.py', e)
    if args.cmd == 'calibrate':
        return run('calibrate_weights.py', ['--method', args.method])
    if args.cmd == 'slippage':
        return run('calibrate_slippage.py', ['--days', str(args.days)])
    return 1


if __name__ == '__main__':
    sys.exit(main())
