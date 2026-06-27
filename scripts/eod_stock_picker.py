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
from feedback.data_collector import FactorDataCollector
from core.data_engine import DataEngine
from datetime import datetime

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

    # 如果市场环境评估跳过，不记录推荐
    if recommendations and recommendations[0].get('skip_reason'):
        return recommendations

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

    # 采集当日因子数据供回测使用
    try:
        enriched = getattr(strategy, '_last_enriched', None)
        if enriched:
            collector = FactorDataCollector()
            collector.collect(
                data_engine=strategy.data_engine,
                enriched_stocks=enriched,
                hot_df=strategy.data_engine.get_ths_hot_stocks(),
                recommendations=recommendations,
                trade_date=today,
            )
    except Exception as e:
        logger.warning(f"因子数据采集失败: {e}")

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
        if rec.get('skip_reason') or rec.get('market_assessment'):
            continue
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

    # 采集当日因子数据
    try:
        enriched = getattr(strategy, '_last_enriched', None)
        if enriched:
            collector = FactorDataCollector()
            collector.collect(
                data_engine=strategy.data_engine,
                enriched_stocks=enriched,
                hot_df=strategy.data_engine.get_ths_hot_stocks(),
                recommendations=recommendations,
                trade_date=today,
            )
    except Exception as e:
        logger.warning(f"因子数据采集失败: {e}")

    return recommendations



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

    # 5. 检查优化器是否触发（仅产报告，不自动写入）
    if recommendations and args.mode == 'short':
        tracker = PredictionTracker()
        optimizer = WeightsOptimizer(
            min_records=config.get('model', {}).get('min_records_for_optimize', 60)
        )
        report = optimizer.check_and_report(
            tracker,
            {'short': config.get('short_term', {}).get('weights', {}),
             'long': config.get('long_term', {}).get('weights', {})},
            mode=args.mode
        )

        if report and report.get('triggered'):
            _print_optimizer_report(report)
            # 将报告写入文件供后续审批
            report_dir = os.path.join(os.path.dirname(os.path.dirname(__file__)),
                                      'data', 'reports')
            os.makedirs(report_dir, exist_ok=True)
            report_path = os.path.join(
                report_dir,
                f"optimizer_report_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
            )
            with open(report_path, 'w', encoding='utf-8') as f:
                json.dump(report, f, ensure_ascii=False, indent=2)
            print(f"📄 优化器报告已保存: {report_path}")
            print(f"⚠️  新权重未自动生效，需要审批后执行 apply_from_report()")


def _print_optimizer_report(report: dict):
    """打印优化器报告摘要到控制台"""
    print("\n" + "=" * 55)
    print("📊 权重优化报告")
    print("=" * 55)

    d = report.get('data_diagnostics', {})
    print(f"  训练数据: {d.get('total_records', '?')} 条 | "
          f"胜率 {d.get('win_rate', '?'):.1f}% | "
          f"平均收益 {d.get('avg_return_t1', 0):+.2f}%")
    print(f"  时间范围: {d.get('date_range', '?')}")
    print(f"  数据新鲜度: {d.get('fresh_ratio', 0)*100:.0f}%")

    print(f"\n  因子信号诊断:")
    for fname, stats in d.get('factor_stats', {}).items():
        sig = "✅" if stats.get('unique_values', 0) >= 3 else "❌"
        print(f"    {sig} {fname}: {stats.get('n_samples', 0)}条, "
              f"{stats.get('unique_values', 0)}个唯一值, "
              f"范围 {stats.get('min', '?')}~{stats.get('max', '?')}")

    old = report.get('old_weights', {})
    new = report.get('proposed_weights', {})
    deltas = report.get('factor_deltas', {})

    if new:
        print(f"\n  权重对比:")
        print(f"  {'因子':<18} {'当前':>6} {'建议':>6} {'变化':>6}")
        print(f"  {'-'*18} {'-'*6} {'-'*6} {'-'*6}")
        for fname in sorted(set(list(old.keys()) + list(new.keys()))):
            o = old.get(fname, 0)
            n = new.get(fname, 0)
            d = deltas.get(fname, {}).get('delta', n - o)
            mark = " ⚠️" if abs(d) >= 0.05 else ""
            print(f"  {fname:<18} {o:>6.0%} {n:>6.0%} {d:>+6.0%}{mark}")
    else:
        print(f"\n  ⚠️ Ridge 未产出有效权重")

    rd = report.get('ridge_detail', {})
    if rd.get('coefficients'):
        print(f"\n  Ridge 回归系数 (R²={rd.get('r2_score', '?'):})")
        for f, c in rd['coefficients'].items():
            print(f"    {f}: {c:+.6f}")

    print("=" * 55)


if __name__ == '__main__':
    main()
