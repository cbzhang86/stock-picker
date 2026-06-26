"""
尾盘选股主入口

用法：
  # 运行短线尾盘策略（每日简报模式）
  python scripts/eod_stock_picker.py --mode short

  # 运行长线策略
  python scripts/eod_stock_picker.py --mode long

  # 查看模型状态
  python scripts/eod_stock_picker.py --status

  # 保存简报文件
  python scripts/eod_stock_picker.py --out reports/daily.md
"""

import argparse
import json
import logging
import sys
import os

# 全局socket超时15秒，防止akshare/mootdx在境外网络挂死
import socket; socket.setdefaulttimeout(15)

# 解决 Windows 控制台 GBK 编码不支持 emoji 的问题
if sys.platform == 'win32':
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    sys.stderr.reconfigure(encoding='utf-8', errors='replace')

# 添加项目根目录到路径
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

import yaml

from strategies.short_term import ShortTermStrategy
from strategies.long_term import LongTermStrategy
from reports.daily_report import DailyReportGenerator
from reports.market_briefing import generate_market_briefing
from feedback.tracker import PredictionTracker
from feedback.optimizer import WeightsOptimizer
from core.data_engine import DataEngine

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    datefmt='%H:%M:%S'
)
logger = logging.getLogger(__name__)


def load_config(path: str = "config.yml") -> dict:
    """加载配置文件"""
    if os.path.exists(path):
        with open(path, 'r', encoding='utf-8') as f:
            return yaml.safe_load(f)
    logger.warning(f"配置文件 {path} 不存在，使用默认配置")
    return {}


def backfill_pending_outcomes():
    """自动回填待处理的 T+1/T+5 结果"""
    tracker = PredictionTracker()
    data_engine = DataEngine()
    pending = tracker.get_pending_outcomes()

    if not pending:
        logger.info("没有待处理的推荐结果")
        return

    logger.info(f"自动回填 {len(pending)} 条推荐结果...")
    for pred in pending:
        code = pred['code']
        pred_date = pred['date']
        try:
            kline = data_engine.get_kline(code, start_date=pred_date)
            if kline is not None and not kline.empty:
                tracker.update_outcomes(pred['id'], kline)
        except Exception as e:
            logger.warning(f"回填失败 {code}: {e}")


def run_short_term(config: dict) -> list:
    """运行短线尾盘策略"""
    short_cfg = config.get('short_term', {})
    strategy = ShortTermStrategy(short_cfg)
    recommendations = strategy.run()

    # 记录推荐
    tracker = PredictionTracker()
    from datetime import date
    today = date.today().isoformat()

    for rec in recommendations:
        tracker.log_prediction(
            date=today,
            code=rec.get('code', ''),
            name=rec.get('name', ''),
            mode='short',
            score=rec.get('score', 0),
            rating=rec.get('rating', ''),
            buy_price=rec.get('price', 0),
            model_version='v1',
            factor_scores=rec.get('breakdown', {})
        )

    return recommendations


def run_long_term(config: dict) -> list:
    """运行长线策略"""
    long_cfg = config.get('long_term', {})
    strategy = LongTermStrategy(long_cfg)
    recommendations = strategy.run()

    tracker = PredictionTracker()
    from datetime import date
    today = date.today().isoformat()

    for rec in recommendations:
        tracker.log_prediction(
            date=today,
            code=rec.get('code', ''),
            name=rec.get('name', ''),
            mode='long',
            score=rec.get('score', 0),
            rating=rec.get('rating', ''),
            buy_price=rec.get('price', 0),
            model_version='v1',
            factor_scores=rec.get('breakdown', {})
        )

    return recommendations


def maybe_sync_weights(config: dict, mode: str):
    """如果优化器写入了新权重，同步到 v1.json"""
    weights_dir = 'data/weights'
    v1_path = os.path.join(weights_dir, 'v1.json')

    # 找最新的权重文件（按时间排序）
    try:
        wfiles = sorted([f for f in os.listdir(weights_dir)
                        if f.endswith('.json') and f != 'v1.json' and mode in f])
    except (FileNotFoundError, OSError):
        return

    if not wfiles:
        return

    latest = os.path.join(weights_dir, wfiles[-1])
    try:
        with open(latest) as f:
            new_weights_data = json.load(f)

        # 读取或创建 v1.json
        existing = {}
        if os.path.exists(v1_path):
            with open(v1_path) as f:
                existing = json.load(f)

        # 合并该 mode 的新权重
        existing[mode] = new_weights_data.get(mode, new_weights_data)

        with open(v1_path, 'w', encoding='utf-8') as f:
            json.dump(existing, f, indent=2, ensure_ascii=False)

        logger.info(f"权重同步到 v1.json（来源: {wfiles[-1]}）")
    except Exception as e:
        logger.warning(f"权重同步失败: {e}")


def show_status(config: dict):
    """显示模型状态"""
    tracker = PredictionTracker()
    accuracy = tracker.calc_accuracy(mode='short', days=30)
    accuracy_long = tracker.calc_accuracy(mode='long', days=30)

    print("\n=== 模型状态 ===")
    print(f"\n📈 短线策略（近30天）")
    print(f"  总记录: {accuracy.get('total_records', 0)}")
    print(f"  胜率(T+1): {accuracy.get('win_rate_t1', 0):.1f}%")
    print(f"  平均收益(T+1): {accuracy.get('avg_return_t1', 0):+.2f}%")
    print(f"  胜率(T+5): {accuracy.get('win_rate_t5', 0):.1f}%")
    print(f"  平均收益(T+5): {accuracy.get('avg_return_t5', 0):+.2f}%")

    print(f"\n📊 长线策略（近30天）")
    print(f"  总记录: {accuracy_long.get('total_records', 0)}")
    print(f"  胜率: {accuracy_long.get('win_rate_t1', 0):.1f}%")

    print(f"\n⚙️  模型版本: v1")
    print(f"  权重: {config.get('short_term', {}).get('weights', {})}")

    recent = tracker.get_recent_predictions(limit=5)
    if not recent.empty:
        print(f"\n📋 最近推荐:")
        for _, row in recent.iterrows():
            t1 = row.get('t1_return', 'N/A')
            mark = '✅' if (t1 is not None and t1 > 0) else '❌'
            print(f"  {row['date']} {row['code']} {row['name']} "
                  f"评分{row['score']} T+1:{t1}% {mark}")
    else:
        print(f"\n📋 暂无推荐记录")


def main():
    parser = argparse.ArgumentParser(description='A股智能选股系统')
    parser.add_argument('--mode', choices=['short', 'long'], default='short',
                        help='策略模式: short(短线) / long(长线)')
    parser.add_argument('--out', type=str, help='输出文件路径')
    parser.add_argument('--status', action='store_true',
                        help='查看模型状态')
    parser.add_argument('--config', type=str, default='config.yml',
                        help='配置文件路径')

    args = parser.parse_args()

    config = load_config(args.config)

    if args.status:
        show_status(config)
        return

    # 1. 自动回填 T+1 结果（每天运行策略时自动更新历史推荐）
    backfill_pending_outcomes()

    # 2. 运行策略
    if args.mode == 'short':
        recommendations = run_short_term(config)
    else:
        recommendations = run_long_term(config)

    # 3. 生成每日市场简报（包含完整信息，不只是推荐列表）
    briefing_text = generate_market_briefing(recommendations, mode=args.mode)
    print(briefing_text)

    # 4. 保存简报文件
    report_generator = DailyReportGenerator()
    path = report_generator.save_report(recommendations, args.mode)
    # 同时保存完整简报版本
    briefing_path = path.replace('.md', '_briefing.md')
    with open(briefing_path, 'w', encoding='utf-8') as f:
        f.write(briefing_text)
    print(f"\n📝 报告已保存: {path}")
    print(f"📊 简报已保存: {briefing_path}")

    # 5. 检查是否触发优化
    if recommendations:
        tracker = PredictionTracker()
        optimizer = WeightsOptimizer()
        optimizer.maybe_optimize(
            tracker,
            {'short': config.get('short_term', {}).get('weights', {}),
             'long': config.get('long_term', {}).get('weights', {})},
            mode=args.mode
        )

        # 同步权重到 v1.json
        maybe_sync_weights(config, args.mode)


if __name__ == '__main__':
    main()
