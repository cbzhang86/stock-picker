"""
回测入口

用法：
  # 短线策略回测（默认）
  python scripts/run_backtest.py

  # 指定区间
  python scripts/run_backtest.py --start 2026-01-01 --end 2026-06-11

  # 长线策略
  python scripts/run_backtest.py --mode long

  # 回测 + 对比
  python scripts/run_backtest.py --compare 1 2

  # 查看历史回测记录
  python scripts/run_backtest.py --list

  # 保存回测报告
  python scripts/run_backtest.py --out reports/backtest.md
"""

import argparse
import logging
import sys
import os

if sys.platform == 'win32':
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    sys.stderr.reconfigure(encoding='utf-8', errors='replace')

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

import yaml

from core.backtest_engine import BacktestEngine
from core.backtest_store import BacktestStore
from reports.backtest_report import generate_backtest_report

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    datefmt='%H:%M:%S'
)
logger = logging.getLogger(__name__)


def main():
    parser = argparse.ArgumentParser(description='A股选股策略回测')
    parser.add_argument('--mode', choices=['short', 'long', 'kfactor'], default='short',
                        help='策略模式: short(短线) / long(长线) / kfactor(K线因子回测)')
    parser.add_argument('--start', default='2025-06-01', help='开始日期')
    parser.add_argument('--end', default='2026-06-11', help='结束日期')
    parser.add_argument('--out', help='输出文件路径')
    parser.add_argument('--config', default='config.yml', help='配置文件')
    parser.add_argument('--list', action='store_true', help='查看历史回测记录')
    parser.add_argument('--compare', nargs=2, type=int, metavar=('RUN_ID_A', 'RUN_ID_B'),
                        help='对比两次回测记录')

    args = parser.parse_args()
    config = load_config(args.config)

    store = BacktestStore()

    if args.list:
        runs = store.list_runs(20)
        if not runs:
            print("暂无回测记录")
            return
        print(f"{'ID':>4} {'策略':<16} {'模式':<8} {'区间':<24} {'交易':<6} {'胜率':<8} {'平均收益':<10} {'夏普':<8}")
        print("-" * 90)
        for r in runs:
            print(f"{r['id']:>4} {r['strategy']:<16} {r['mode']:<8} "
                  f"{r['start']}~{r['end']:<14} "
                  f"{r['trades']:<6} {r['win_rate']:.1f}%    "
                  f"{r['avg_return']:+.2f}%    {r['sharpe']:<8.2f}")
        return

    if args.compare:
        diff = store.compare_runs(args.compare[0], args.compare[1])
        print(diff)
        return

    # 正常回测
    bt_config = config.get('backtest', {})
    strategy_config = config.get(args.mode + '_term', {})
    full_config = {**bt_config, **strategy_config}

    engine = BacktestEngine(full_config)

    if args.mode == 'kfactor':
        result = engine.run_kline_factor_backtest(
            factors=['momentum', 'technical', 'volume_price'],
            start_date=args.start,
            end_date=args.end,
        )
    else:
        result = engine.run(
            mode=args.mode,
            start_date=args.start,
            end_date=args.end,
        )

    # 保存回测结果
    store.save_run(result, full_config)

    report = generate_backtest_report(result)

    if args.out:
        os.makedirs(os.path.dirname(args.out) or '.', exist_ok=True)
        with open(args.out, 'w', encoding='utf-8') as f:
            f.write(report)
        logger.info(f"报告已保存: {args.out}")
    else:
        print(report)


def load_config(path: str) -> dict:
    if os.path.exists(path):
        with open(path, 'r', encoding='utf-8') as f:
            return yaml.safe_load(f)
    return {}


if __name__ == '__main__':
    main()
