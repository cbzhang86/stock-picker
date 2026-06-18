"""
回测报告生成器

输出格式参考：
  Sequoia-X feishu.py 卡片设计
  原提示词风格的简洁Markdown
"""

import logging
from datetime import datetime
from typing import Dict, List

from core.backtest_engine import BacktestResult

logger = logging.getLogger(__name__)


def generate_backtest_report(result: BacktestResult) -> str:
    """
    生成回测报告（Markdown格式）
    """
    lines = []
    lines.append(f"# 回测报告：{result.strategy_name}")
    lines.append(f"**区间**: {result.period[0]} ~ {result.period[1]}")
    lines.append(f"**生成时间**: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    lines.append("")

    # === 数据来源说明（明确告知回测局限） ===
    if getattr(result, 'mode', '') == 'kfactor':
        lines.append("## 📋 模式说明：K 线因子回测")
        lines.append("")
        lines.append("- 基于 `data/kline_cache.db` 中各股的日 K 线，**逐日重算技术类因子**（momentum / technical / volume_price）")
        lines.append("- 资金流 / 题材 / 龙虎榜 / 北向 / 基本面因子在 K 线维度下**没有数据**，无法校验")
        lines.append("- 输出每个因子的 IC（Spearman rank 相关系数）+ 胜率分桶 + verdict 解读")
        lines.append("- 适用于因子筛选验证，不替代策略级回测")
        lines.append("")
    else:
        lines.append("## ⚠️ 数据来源说明（必读）")
        lines.append("")
        lines.append("- **当前回测是\"近似回测\"**，并非严格历史复现")
        lines.append("- 引擎 `_get_day_snapshot()` 简化实现：拉取的是**当日实时全市场行情**而非历史某日的 snapshot")
        lines.append("- 资金流 / 题材 / 题材归因 / 龙虎榜 / 北向因子在回测期间**没有真实历史数据可用**（这些数据是当日 akshare 截面，回溯时只能用今日数据）")
        lines.append("- 因此这些因子的\"赢单/输单分差\"会趋近于 0 —— **不代表因子无效**，是当前数据条件下的事实")
        lines.append("- 因子分析新增 **IC（信息系数）** + verdict 解读：|IC| > 0.03 算有效，便于对照真实 alpha")
        lines.append("- 如要严谨验证某个因子，请用 `--mode kfactor` 进入 K 线因子回测")
        lines.append("")

    if result.total_trades == 0:
        lines.append("⚠️ 回测区间内无交易记录")
        lines.append("可能原因：")
        lines.append("- 策略条件过于严格")
        lines.append("- 筛选条件过滤了所有股票")
        lines.append("- 回测区间市场环境不适合")
        return "\n".join(lines)

    lines.append("---")
    lines.append("## 📊 基本统计")
    lines.append("")
    stats = [
        ("总交易次数", f"{result.total_trades} 次"),
        ("胜率", f"{result.win_rate:.1f}%"),
        ("平均收益(T+1)", f"{result.avg_return_t1:+.2f}%"),
        ("平均收益(T+5)", f"{result.avg_return_t5:+.2f}%"),
        ("最大单笔盈利", f"{result.max_win_t1:+.2f}%"),
        ("最大单笔亏损", f"{result.max_loss_t1:+.2f}%"),
        ("最大回撤", f"{result.max_drawdown:.2f}%"),
        ("夏普比率", f"{result.sharpe_ratio:.2f}"),
    ]
    for label, value in stats:
        lines.append(f"- **{label}**: {value}")

    lines.append("")
    lines.append("### 收益对比")
    lines.append(f"- **沪深300**: {result.benchmark_return:+.2f}%")
    lines.append(f"- **策略收益**: {result.strategy_return:+.2f}%")
    lines.append(f"- **超额收益**: {result.excess_return:+.2f}%")
    lines.append("")

    # 权益曲线（文本块状图）
    if result.equity_curve and len(result.equity_curve) > 1:
        lines.append("---")
        lines.append("## 📈 权益曲线")
        lines.append("")
        equity_chart = _render_equity_chart(result.equity_curve)
        lines.append(equity_chart)
        lines.append("")

    # 因子归因分析
    if result.factor_performance:
        lines.append("---")
        lines.append("## 🔬 因子分析（含 IC 信息系数）")
        lines.append("")
        lines.append("**赢单** = return_t1 ≥ 0；**输单** = return_t1 < 0；**IC** = Spearman(rank(raw_score), rank(return_t1))")
        lines.append("")
        lines.append("| 因子 | 赢单平均分 | 输单平均分 | 分差 | IC | 样本 | 判定 |")
        lines.append("|------|-----------|-----------|------|------|------|------|")
        sorted_factors = sorted(
            result.factor_performance.items(),
            key=lambda x: (abs(x[1].get('ic') or 0), abs(x[1].get('spread', 0))),
            reverse=True
        )
        for factor, perf in sorted_factors:
            ic_str = f"{perf.get('ic', 0):+.4f}" if perf.get('ic') is not None else "—"
            n = perf.get('n_samples', 0)
            verdict = perf.get('verdict', '—')
            lines.append(f"| {factor} | {perf.get('win_avg', 0):.1f} | "
                        f"{perf.get('lose_avg', 0):.1f} | "
                        f"{perf.get('spread', 0):+.1f} | "
                        f"{ic_str} | {n} | {verdict} |")

    # 月度表现
    if result.monthly_returns:
        lines.append("")
        lines.append("---")
        lines.append("## 📅 月度表现")
        lines.append("")
        lines.append("| 月份 | 平均收益 | 胜率 | 交易次数 |")
        lines.append("|------|---------|------|---------|")
        for m in result.monthly_returns:
            lines.append(f"| {m['month']} | {m['avg_return']:+.2f}% | "
                        f"{m['win_rate']:.0f}% | {m['trades']} |")

    # 交易明细（前10条）
    if result.trade_details:
        lines.append("")
        lines.append("---")
        lines.append("## 📋 交易明细（前10条）")
        lines.append("")
        lines.append("| 日期 | 代码 | 名称 | 评分 | 买入价 | T+1收益 |")
        lines.append("|------|------|------|------|--------|---------|")
        for td in result.trade_details[:10]:
            ret = td.get('return_t1', 0)
            mark = "✅" if ret and ret > 0 else "❌"
            lines.append(f"| {td['date']} | {td['code']} | {td.get('name','')} | "
                        f"{td.get('score', 0):.0f} | "
                        f"{td.get('buy_price', 0):.2f} | "
                        f"{mark} {ret:+.2f}% |")

    # 优化建议
    lines.append("")
    lines.append("---")
    lines.append("## 💡 建议优化方向")
    lines.append("")
    if result.win_rate < 40:
        lines.append("- ⚠️ 胜率偏低，建议放宽买入条件或调整因子权重")
    elif result.win_rate < 55:
        lines.append("- 胜率中等，可通过权重优化提升")
    else:
        lines.append("- ✅ 胜率良好，可继续积累数据完善模型")

    if result.max_drawdown < -15:
        lines.append("- ⚠️ 回撤过大，建议增加止损条件或风险过滤")
    if result.sharpe_ratio < 0.5:
        lines.append("- 夏普比率偏低，建议优化风险调整后收益")

    return "\n".join(lines)


def _render_equity_chart(values: List[float], width: int = 40) -> str:
    """
    用 unicode 块状图渲染权益曲线
    ▁▂▃▄▅▆▇█ 8级亮度
    """
    if not values:
        return "(无数据)"
    bars = ['▁', '▂', '▃', '▄', '▅', '▆', '▇', '█']

    # 取均匀间隔的点
    step = max(1, len(values) // width)
    sampled = values[::step][:width]
    if len(sampled) < 2:
        return f"最终权益: ¥{values[-1]:.0f}"

    min_v = min(sampled)
    max_v = max(sampled)
    if max_v == min_v:
        chart_line = bars[4] * len(sampled)
    else:
        normalized = [(v - min_v) / (max_v - min_v) for v in sampled]
        chart_line = ''.join(bars[min(int(n * 7), 7)] for n in normalized)

    return (f"```\n{chart_line}\n"
            f"¥{min_v:.0f} ~ ¥{max_v:.0f} (最终: ¥{values[-1]:.0f})\n```")


def generate_daily_report(recommendations: List[Dict], mode: str = 'short') -> str:
    """生成每日选股推荐报告"""
    from datetime import date

    today = date.today()
    lines = []

    if mode == 'short':
        lines.append("┌─────────────────────────────────────────────┐")
        lines.append(f"│ 今日尾盘策略推荐（{today}）               │")
        lines.append("└─────────────────────────────────────────────┘")
        lines.append("")
    else:
        lines.append("┌─────────────────────────────────────────────┐")
        lines.append(f"│ 长线持仓建议（{today} 月度更新）           │")
        lines.append("└─────────────────────────────────────────────┘")
        lines.append("")

    if not recommendations:
        lines.append("今日尾盘策略跳过")
        lines.append("原因：当前没有评分合格的标的")
        return "\n".join(lines)

    # 市场环境诊断（跳过交易但仍有信息的场景）
    skip_reason = recommendations[0].get('skip_reason', '')
    market_assessment = recommendations[0].get('market_assessment', {})
    if skip_reason:
        lines.append(f"  ⚠️ {skip_reason}")
        lines.append("")
        if market_assessment:
            d = market_assessment.get('details', {})
            lines.append(f"  市场环境综合评分: {market_assessment.get('total', 0)}/100 "
                         f"({market_assessment.get('level', '')})")
            lines.append(f"  涨跌比: {d.get('advancing', 0)}/{d.get('declining', 0)} "
                         f"涨停{d.get('limit_ups', 0)}跌停{d.get('limit_downs', 0)} "
                         f"中位数涨幅{d.get('median_chg', 0):+.2f}%")
            lines.append(f"  强势股: {d.get('hot_count', 0)}只 "
                         f"北向: {d.get('north_total', 0):+.0f}亿")
        lines.append("")
        return "\n".join(lines)

    # 数据源状态诊断
    source_status = recommendations[0].get('data_source_status', {})
    failed_sources = {k: v for k, v in source_status.items()
                      if not v.get('available', True)}
    if failed_sources:
        lines.append("  ⚠️ 数据源状态：")
        for source_name, status in failed_sources.items():
            label = status.get('label', source_name)
            err = status.get('last_error', '')
            err_str = f" ({err})" if err else ""
            lines.append(f"    - {label}: 接口不可用{err_str}")
        lines.append("  ⚠️ 缺失数据源的因子已自动降权。")
        lines.append("")

    # 市场级数据
    market_data = recommendations[0].get('market_data', {})
    north = market_data.get('north_flow')
    if north:
        direction = "净流入" if north['total'] > 0 else "净流出"
        lines.append(f"  🌐 北向资金：{direction} {north['total']}亿元"
                     f"（沪股通{north['hgt']:+.2f}亿 / 深股通{north['sgt']:+.2f}亿）")
        lines.append("")

    for i, rec in enumerate(recommendations, 1):
        code = rec.get('code', 'N/A')
        name = rec.get('name', 'N/A')
        price = rec.get('price', 0)
        score = rec.get('score', 0)
        rating_cn = rec.get('rating_cn', 'N/A')
        decision = rec.get('decision', '')
        target_price = rec.get('target_price', 0)
        stop_price = rec.get('stop_price', 0)
        reasoning = rec.get('reasoning', '')
        allocation = rec.get('allocation_pct', 0)  # Phase 3: 组合优化

        lines.append(f"  {'─' * 50}")
        alloc_str = f" 建议仓位: {allocation:.0f}%" if allocation else ""
        lines.append(f"  {i}. **{code} {name}** ￥{price:.2f}{alloc_str}")
        lines.append(f"     评分：{score}/100 | 评级：{rating_cn}")
        lines.append(f"     决策：{decision}")
        lines.append(f"     目标价：￥{target_price:.2f}  止损价：￥{stop_price:.2f}")

        # 因子得分
        breakdown = rec.get('breakdown', {})
        if breakdown:
            top_factors = sorted(
                breakdown.items(),
                key=lambda x: x[1].get('weighted', 0),
                reverse=True
            )
            desc_parts = []
            for factor, data in top_factors:
                raw = data.get('raw_score', 0)
                desc_parts.append(f"{factor} {raw:.0f}")
            lines.append(f"     关键因子：{' | '.join(desc_parts)}")

        # 选股理由
        if reasoning:
            lines.append(f"     分析：{reasoning}")

        # 板块归属
        blocks = rec.get('blocks', {})
        if blocks and blocks.get('concept_tags'):
            tags = blocks['concept_tags'][:5]
            lines.append(f"     概念板块：{'、'.join(tags)}")

        lines.append("")

    lines.append(f"  {'─' * 45}")
    lines.append(f"  共推荐 {len(recommendations)} 只标的")
    lines.append("  📌 短线：T+1开盘+2%止盈 / -2%止损 / T+3时间止损")
    if mode == 'long':
        lines.append("  📌 长线：3-6个月持股周期，按月跟踪北向+季报")

    return "\n".join(lines)
