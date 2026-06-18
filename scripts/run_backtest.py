"""
回测入口

用法：
  # 短线策略回测（默认）
  python scripts/run_backtest.py

  # 指定区间
  python scripts/run_backtest.py --start 2025-01-01 --end 2025-12-31

  # 长线策略
  python scripts/run_backtest.py --mode long

  # 保存回测报告
  python scripts/run_backtest.py --out reports/backtest_2025.md
"""

import argparse
import logging
import sys
import os

# 解决 Windows 控制台 GBK 编码不支持 emoji 的问题
if sys.platform == 'win32':
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    sys.stderr.reconfigure(encoding='utf-8', errors='replace')

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

import yaml

from core.backtest_engine import BacktestEngine
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

    args = parser.parse_args()

    config = load_config(args.config)

    # 从 config.yml 加载回测参数，合并命令行参数
    bt_config = config.get('backtest', {})
    # 从 config.yml 加载策略配置
    strategy_config = config.get(args.mode + '_term', {})
    # 合并
    full_config = {**bt_config, **strategy_config}

    engine = BacktestEngine(full_config)

    if args.mode == 'kfactor':
        # K 因子回测：直接调新方法，不走 run()
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
