"""
每日市场简报生成器

生成 A 股全景简报，包含：
  - 市场概览（涨跌家数、成交额、北向资金）
  - 题材热度 TOP 10
  - 技术评分排名 TOP 5
  - 短线推荐（含仓位分配）
  - 长线关注（基本面评分）
  - 龙虎榜亮点

所有数据源已在 DataEngine 中实现，不新增 API 调用。
"""

import logging
from datetime import date, datetime
from typing import Dict, List

from core.data_engine import DataEngine

logger = logging.getLogger(__name__)


def generate_market_briefing(recommendations: List[Dict],
                              mode: str = 'short') -> str:
    """
    生成每日市场简报

    参数：
      recommendations: 策略推荐的股票列表（含评分/仓位/板块等数据）
      mode: 'short' / 'long'

    返回：格式化的市场简报字符串
    """
    de = DataEngine()
    today = date.today()
    lines = []

    # ── 标题 ──
    lines.append("┌─────────────────────────────────────────────┐")
    lines.append(f"│  A股智能监测 · 每日简报 {today}          │")
    lines.append("└─────────────────────────────────────────────┘")
    lines.append("")

    # ── 1. 市场概览 ──
    lines.append("=" * 45)
    lines.append("  📊 市场概览")
    lines.append("=" * 45)

    # 涨跌家数 + 成交额
    try:
        quotes = de.get_all_quotes()
        if quotes is not None and not quotes.empty:
            up_count = (quotes['pct_chg'] > 0).sum()
            down_count = (quotes['pct_chg'] <= 0).sum()
            total_amount = quotes['amount'].sum()
            lines.append(f"    上涨/下跌: {up_count}/{down_count}")
            lines.append(f"    成交额: {total_amount / 10000:,.0f} 亿")
    except Exception as e:
        lines.append("    市场数据暂不可用")
        logger.warning(f"市场行情获取失败: {e}")

    # 北向资金
    try:
        north = de.get_north_flow_summary()
        if north:
            direction = "净流入" if north['total'] > 0 else "净流出"
            lines.append(f"    北向资金: {direction} {north['total']:+.2f}亿"
                         f"（沪{north['hgt']:+.2f} / 深{north['sgt']:+.2f}）")
    except Exception as e:
        logger.warning(f"北向数据获取失败: {e}")
    lines.append("")

    # ── 2. 题材热度 TOP 10 ──
    lines.append("-" * 45)
    lines.append("  🔥 题材热度 TOP 10")
    lines.append("-" * 45)
    try:
        hot_df = de.get_ths_hot_stocks()
        if hot_df is not None and not hot_df.empty:
            themes = de.extract_hot_themes(hot_df)
            if themes:
                for i, t in enumerate(themes[:10], 1):
                    lead = t['top_stocks'][0] if t['top_stocks'] else {}
                    lead_name = lead.get('name', '')
                    lead_pct = lead.get('pct_chg', '')
                    lines.append(f"    {i:2d}. {t['theme']}（{t['count']}只）"
                                 f"→ {lead_name} {lead_pct:+.1f}%")
    except Exception as e:
        lines.append("    题材数据暂不可用")
        logger.warning(f"题材热度获取失败: {e}")
    lines.append("")

    # ── 3. 技术评分排名 TOP 5（从推荐中提取） ──
    if recommendations:
        lines.append("-" * 45)
        lines.append("  📈 短线评分排名（因子加权）")
        lines.append("-" * 45)
        for i, rec in enumerate(recommendations[:5], 1):
            code = rec.get('code', '')
            name = rec.get('name', '')
            score = rec.get('score', 0)
            rating = rec.get('rating_cn', '')
            allocation = rec.get('allocation_pct', 0)
            bd = rec.get('breakdown', {})
            # 全部因子
            top_factors = sorted(bd.items(),
                                 key=lambda x: x[1].get('weighted', 0),
                                 reverse=True)
            factors_str = ' '.join(f"{f}({d.get('raw_score',0):.0f})"
                                   for f, d in top_factors)
            lines.append(f"    {i}. {code} {name}")
            lines.append(f"       评分 {score}/100 | {rating} | 仓位 {allocation:.0f}%")
            lines.append(f"       因子: {factors_str}")

            # 概念板块
            blocks = rec.get('blocks', {})
            if blocks and blocks.get('concept_tags'):
                tags = blocks['concept_tags'][:4]
                lines.append(f"       板块: {'、'.join(tags)}")

            # 龙虎榜亮点
            dt = rec.get('dragon_tiger', {})
            if dt and dt.get('records'):
                r = dt['records'][0]
                net = r.get('net_buy_wan', 0)
                if abs(net) > 0:
                    lines.append(f"       龙虎榜: 净买入 {net:,.0f} 万")
            lines.append("")

    # ── 4. 长线关注（基于基本面评分） ──
    try:
        from strategies.long_term import LongTermStrategy
        long_cfg = {'buy': {'max_candidates': 5, 'min_score': 50}}
        long_st = LongTermStrategy(long_cfg)
        long_recs = long_st.run()
        if long_recs:
            lines.append("-" * 45)
            lines.append("  📌 长线关注（ROE+PE 评分）")
            lines.append("-" * 45)
            for i, rec in enumerate(long_recs[:5], 1):
                code = rec.get('code', '')
                name = rec.get('name', '')
                score = rec.get('score', 0)
                roe = rec.get('roe', 'N/A')
                eps = rec.get('eps', 'N/A')
                pe = rec.get('pe', 'N/A')
                allocation = rec.get('allocation_pct', 0)
                lines.append(f"    {i}. {code} {name}  评分{score}/100"
                             f"  仓位{allocation:.0f}%")
                if roe and str(roe) != 'N/A':
                    lines.append(f"       ROE {roe:.1f}%  EPS {eps:.2f}  PE {pe}")
            lines.append("")
    except Exception as e:
        logger.warning(f"长线策略运行失败: {e}")

    # ── 5. 数据源状态 ──
    try:
        source_status = de.get_data_source_summary()
        failed = {k: v for k, v in source_status.items()
                  if not v.get('available', True)}
        if failed:
            lines.append("-" * 45)
            lines.append("  ⚠️ 数据源状态")
            lines.append("-" * 45)
            for sname, status in failed.items():
                label = status.get('label', sname)
                err = status.get('last_error', '')
                err_str = f" ({err})" if err else ""
                lines.append(f"    - {label}: 不可用{err_str}")
            lines.append("")
    except Exception as e:
        logger.warning(f"数据源状态获取失败: {e}")

    # ── 底部说明 ──
    lines.append("=" * 45)
    if mode == 'short':
        lines.append("  💡 短线：T+1 开盘+2%止盈 / -2%止损 / T+3时间止损")
    else:
        lines.append("  💡 长线：3-6个月，按月跟踪北向+季报")
    lines.append(f"  生成时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    lines.append("=" * 45)

    return "\n".join(lines)
