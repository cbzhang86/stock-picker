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

# 排名徽章（微信端一眼看出名次）
_MEDALS = ['🥇', '🥈', '🥉', '4️⃣', '5️⃣']

# 降级影响映射（2026-09-16 P1-1 新增）
# 数据源 → 因该源不可用而降为中性/缺失的因子。仅用于把"XX接口不可用"
# 翻译成"影响了哪些因子、合计多少权重"，让人能一眼判断该不该参考今日推荐。
# 维护约定：新增数据源时必须在此登记，否则降级警示里不会体现其影响面。
_SOURCE_FACTOR_IMPACT = {
    'akshare_codes': ['全部因子'],       # 代码清单缺失 → 无法选股
    'tencent_quote': ['全部因子'],       # 实时行情缺失 → 无法评分
    # 2026-09-19（全项目深查 P1-2）：K 线派生的因子不止 technical/volume_price/momentum
    # ——2026-09-19 方案G 启用的 liq_dev / vol_dev / volatility 同源（都依赖
    # kline_df 的 amount/close 序列），合计权重 0.27。此前未登记 → K 线源故障时
    # 简报"受影响因子权重合计"漏算近 1/3，违反契约 #3"新增数据源需登记"。
    # 守卫测试 tests/test_source_factor_impact_20260919.py 确保未来新增因子不再漂移。
    # liquidity 权重 0，登记以保持映射完整。
    'mootdx_kline': ['technical', 'volume_price', 'momentum', 'reversal_20d',
                     'liq_dev', 'vol_dev', 'volatility', 'liquidity'],
    'baostock_kline': ['technical', 'volume_price', 'momentum', 'reversal_20d',
                       'liq_dev', 'vol_dev', 'volatility', 'liquidity'],
    'akshare_fund_flow': ['capital_flow'],
    'ths_fund_flow': ['capital_flow'],
    'big_deal': ['capital_flow'],
    'asharehub_moneyflow': ['capital_flow'],
    'akshare_north_flow': ['north_flow'],
    'asharehub_tech_factors': ['technical'],
    'asharehub_concepts': ['hot_theme'],
    'ths_hot': ['hot_theme'],
    'eastmoney_blocks': ['hot_theme'],
    'asharehub_financial': ['fundamental', 'valuation'],
    'dragon_tiger': ['dragon_tiger'],
    'lockup': [],
}


def _degradation_note(failed: Dict, mode: str) -> List[str]:
    """把运行期数据源故障翻译成"影响哪些因子、合计权重多少"的说明行。

    权重取 ScoringModel 的**生效权重**（v1.json 优先，与评分口径一致），
    失败时静默降级为只列因子名（警示块本身不能因为算不出权重而消失）。
    """
    affected = set()
    for key in failed:
        for f in _SOURCE_FACTOR_IMPACT.get(key, []):
            if f == '全部因子':
                return ['    📉 影响范围: 全部因子（行情/代码清单缺失，本次评分不可用）']
            affected.add(f)
    if not affected:
        return []
    try:
        from core.scoring_model import ScoringModel
        weights = ScoringModel().get_weights(mode) or {}
    except Exception:
        weights = {}
    share = 0.0
    for f in affected:
        try:
            share += float(weights.get(f) or 0)
        except (TypeError, ValueError):
            continue
    out = []
    names = '、'.join(sorted(affected))
    if share > 0:
        out.append(f"    📉 受影响因子权重合计约 {share * 100:.0f}%（{names}）")
    else:
        out.append(f"    📉 受影响因子: {names}")
    if share >= 0.30:
        out.append("    ⚠️ 降级因子权重占比偏高，本次结果的关键加分项已失效，请谨慎参考")
    return out


def generate_market_briefing(recommendations: List[Dict],
                              mode: str = 'short',
                              source_status: Dict = None) -> str:
    """
    生成每日市场简报

    参数：
      recommendations: 策略推荐的股票列表（含评分/仓位/板块等数据）
      mode: 'short' / 'long'
      source_status: 策略**运行期**的数据源状态快照
                     （recommendations[0]['data_source_status']）。
                     2026-09-16 P1-1：此前本函数内部 `DataEngine()` 新建实例，
                     其 _source_status 在 __init__ 中默认全绿，与运行期真实熔断
                     状态无关 → 简报（即微信推送内容）永远不会出现降级提示，
                     导致"四源全降级的中性化推荐"被当作正常结果推送出去。
                     传入时优先使用运行期快照；缺省（如长线/测试）回落内部实例。

    返回：格式化的市场简报字符串
    """
    de = DataEngine()
    today = date.today()
    lines = []

    # ── 标题 ──（2026-09-07：微信端阅读为主，标题与各行加 emoji 提升可读性）
    lines.append("┌─────────────────────────────────────────────┐")
    lines.append(f"│  📈 A股智能监测 · 每日简报  📅 {today}    │")
    lines.append("└─────────────────────────────────────────────┘")
    lines.append("")

    # ── 0. 数据源降级警示（2026-09-16 P1-1：必须在首屏，先于任何结论）──
    # 2026-09-19 深查 P2-2：source_status 缺失时回退到新建 DataEngine 的 summary
    # （其 _source_status 恒全绿）→ 降级警示会静默消失。回退必须留痕。
    if source_status:
        snapshot = source_status
    else:
        snapshot = de.get_data_source_summary()
        logger.warning(
            "generate_market_briefing 未收到运行期 source_status（调用方漏传）→ "
            "回退到新建实例的默认全绿状态，本次降级警示可能不完整，请检查调用侧")
    failed = {k: v for k, v in (snapshot or {}).items()
              if not v.get('available', True)}
    if failed:
        lines.append("!" * 45)
        lines.append("  ⚠️ 数据源降级警示：本次结果可信度下降")
        lines.append("!" * 45)
        for sname, status in failed.items():
            label = status.get('label', sname)
            err = status.get('last_error', '')
            err_str = f" ({err})" if err else ""
            lines.append(f"    🔌 {label}: 不可用{err_str}")
        lines.extend(_degradation_note(failed, mode))
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
            down_count = (quotes['pct_chg'] < 0).sum()
            flat_count = (quotes['pct_chg'] == 0).sum()
            total_amount = quotes['amount'].sum()
            # 修复（2026-09-06）：下跌原用 <=0 把平盘并入，与行情软件口径不符
            # 2026-09-07：红涨绿跌（A股口径），便于微信端一眼分辨
            lines.append(f"    🔴 上涨 {up_count}  🟢 下跌 {down_count}  ⚪ 平盘 {flat_count}"
                         f"（沪深口径，不含北交所）")
            # amount 单位已是元（data_engine 腾讯实时已转元），元 → 亿 = /1e8
            lines.append(f"    💰 成交额: {total_amount / 1e8:,.0f} 亿")
    except Exception as e:
        lines.append("    ⚠️ 市场数据暂不可用")
        logger.warning(f"市场行情获取失败: {e}")

    # 北向资金
    try:
        north = de.get_north_flow_summary()
        if north and north.get('available'):
            inflow = north['total'] > 0
            direction = "净流入" if inflow else "净流出"
            lines.append(f"    🧭 北向资金: {'📈' if inflow else '📉'} {direction} "
                         f"{north['total']:+.2f}亿"
                         f"（沪{north['hgt']:+.2f} / 深{north['sgt']:+.2f}）"
                         f" [上一交易日 {north.get('time','')}]"
                         f"（东财估算口径，仅供参考）")
        elif north and not north.get('available'):
            err = north.get('error', '')
            if err == 'securities_token_expired':
                lines.append("    🧭 北向资金: ⚠️ 数据源暂不可用（token疑似失效，东财datacenter在线）")
            else:
                lines.append("    🧭 北向资金: ⚠️ 数据源暂不可用（东财datacenter不可达）")
        else:
            lines.append("    🧭 北向资金: ⚠️ 数据未获取")
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
                    lead_pct = lead.get('pct_chg')
                    if isinstance(lead_pct, (int, float)):
                        arrow = '📈' if lead_pct > 0 else (
                            '📉' if lead_pct < 0 else '➖')
                        tail = f" {lead_pct:+.1f}% {arrow}"
                    else:
                        # 2026-09-16 P2-1：上游已不再提供涨跌幅字段。
                        # 此前静默回落 0 → 整列显示 +0.0%（伪造数据）；
                        # 现改为展示"关联股数 + 代表个股名"，不虚构数值。
                        names = [s.get('name', '') for s in t['top_stocks'][:3]
                                 if s.get('name')]
                        tail = f" 代表: {'、'.join(names)}" if names else ""
                    lines.append(f"    {i:2d}. 🔥 {t['theme']}（{t['count']}只）"
                                 f"→ {lead_name}{tail}")
            else:
                lines.append("    （今日无题材归因数据）")
        else:
            lines.append("    ⚠️ 题材数据暂不可用")
    except Exception as e:
        lines.append("    ⚠️ 题材数据暂不可用")
        logger.warning(f"题材热度获取失败: {e}")
    lines.append("")

    # ── 3. 短线推荐排名（skip/熔断条目单独展示停推原因，不渲染空条目） ──
    # 真实推荐 = 带 code 的条目；无 code 的是"元信息条目"（市场跳过 /
    # 门槛清零），它们只承载说明信息，不参与推荐渲染（2026-09-14）。
    rec_recs = [r for r in (recommendations or []) if r.get('code')]
    has_skip = any(r.get('skip_reason') for r in (recommendations or []))
    for r in (recommendations or []):
        if r.get('skip_reason'):
            lines.append("-" * 45)
            lines.append("  ⏸️ 短线策略今日停推")
            lines.append("-" * 45)
            lines.append(f"    🚫 {r['skip_reason']}")
            lines.append("")
            break
    if rec_recs:
        lines.append("-" * 45)
        lines.append("  📈 短线评分排名（因子加权）")
        lines.append("-" * 45)
        # 策略健康提示（2026-09-06 改：kill-switch 由"硬熔断停推"改为"软提示"，
        # 失效期照常推荐但显著标注近 20 个交易日实际 T+1 累计收益）
        _health = next((r.get('strategy_health') for r in rec_recs
                        if r.get('strategy_health')), None)
        if _health and _health.get('triggered'):
            lines.append(f"    ⚠️ 策略健康提示：最近 {_health.get('n_days', 20)} 个交易日"
                         f"推荐标的 T+1 累计收益 {_health.get('cum_return', 0):+.2f}%，"
                         f"模型近期表现不佳，请谨慎参考短线推荐")
            lines.append("")
        for i, rec in enumerate(rec_recs[:5], 1):
            code = rec.get('code', '')
            name = rec.get('name', '')
            score = rec.get('score', 0)
            rating = rec.get('rating_cn', '')
            allocation = rec.get('allocation_pct', 0)
            bd = rec.get('breakdown', {})
            # 全部因子：数据缺失的因子打 [缺] 标记，让"分数低因为没数据"和"分数低因为股票差"可区分
            top_factors = sorted(bd.items(),
                                 key=lambda x: x[1].get('weighted', 0),
                                 reverse=True)
            factors_str = ' '.join(
                f"{f}({d.get('raw_score',0):.0f})" + ('' if d.get('data_available', True) else '[缺]')
                for f, d in top_factors)
            medal = _MEDALS[i - 1] if i <= len(_MEDALS) else f"{i}."
            # 现金口径（2026-09-16 P1-2）：单票上限 40% 生效后，荐股 ≤2 只时
            # 仓位合计 <100%，必须显式说明剩余部分为现金，否则"仓位 40%"会被
            # 误读为"仓位被算少了"。
            cash = rec.get('cash_pct', 0) or 0
            cash_str = f"（另留现金 {cash:.0f}%）" if cash > 0 else ""
            lines.append(f"    {medal} {code} {name}")
            lines.append(f"       ⭐ 评分 {score}/100 | 🎯 {rating} | "
                         f"💼 仓位 {allocation:.0f}%{cash_str}")
            lines.append(f"       🧩 因子: {factors_str}")

            # 概念板块
            blocks = rec.get('blocks', {})
            if blocks and blocks.get('concept_tags'):
                tags = blocks['concept_tags'][:4]
                lines.append(f"       🏷️ 板块: {'、'.join(tags)}")

            # 龙虎榜亮点
            dt = rec.get('dragon_tiger', {})
            if dt and dt.get('records'):
                r = dt['records'][0]
                net = r.get('net_buy_wan', 0)
                if abs(net) > 0:
                    lines.append(f"       🐉 龙虎榜: 净买入 {net:,.0f} 万")
            lines.append("")

    elif recommendations is not None and not has_skip:
        # 既无 skip 也无推荐条目：min_score 质量门槛自然过滤（非熔断停推），
        # 显式说明避免简报短线区块静默消失
        lines.append("-" * 45)
        lines.append("  📈 短线评分排名（因子加权）")
        lines.append("-" * 45)
        lines.append("    🚫 今日无评分达标标的（min_score 质量门槛过滤）")
        # 2026-09-14 需求：零推荐时展示当日最高分标的（明确标注分数），
        # 用于区分"市场确实没有好票"与"门槛相对当前评分尺度偏高"。
        # 数据来自策略透出的元信息条目（no_qualified + top_candidate）。
        _marker = next((r for r in (recommendations or [])
                        if r.get('no_qualified')), None) or {}
        _top = _marker.get('top_candidate') or {}
        if _top:
            lines.append(f"    📌 最高分标的（未达门槛，仅供观察）: "
                         f"{_top.get('code', '')} {_top.get('name', '')} "
                         f"⭐ {_top.get('score', 0):.2f}/100")
            lines.append(f"       距门槛 {_marker.get('min_score', 0):.0f} 差 "
                         f"{_top.get('gap', 0):.2f} 分 | "
                         f"已评分候选 {_top.get('candidates', 0)} 只")
            lines.append("       ⚠️ 未达标，不作为推荐；仅用于判断「没好票」还是「门槛偏高」")
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
                cash = rec.get('cash_pct', 0) or 0
                lines.append(f"    {i}. 🎯 {code} {name}  ⭐{score}/100"
                             f"  💼{allocation:.0f}%"
                             + (f"（现金 {cash:.0f}%）" if cash > 0 else ""))
                if roe and str(roe) != 'N/A':
                    # 修复（2026-09-06 审查）：eps 可能为 None（long_term fin.get('eps')），
                    # 直接 :.2f 会抛 ValueError 并被外层 except 吞掉导致长线区块整体消失
                    eps_str = f"{eps:.2f}" if isinstance(eps, (int, float)) else str(eps or 'N/A')
                    roe_str = f"{roe:.1f}" if isinstance(roe, (int, float)) else str(roe)
                    lines.append(f"       💹 ROE {roe_str}%  |  EPS {eps_str}  |  PE {pe}")
            lines.append("")
    except Exception as e:
        logger.warning(f"长线策略运行失败: {e}")

    # ── 5. 数据源状态 ──
    # 2026-09-16 P1-1：该区块已上移至首屏「⚠️ 数据源降级警示」。
    # 原实现读本函数内部新建的 DataEngine（_source_status 恒为全绿）→ 永不触发，
    # 且与首屏警示重复。现统一使用运行期快照，只在首屏渲染一处。

    # ── 底部说明 ──
    lines.append("=" * 45)
    if mode == 'short':
        lines.append("  💡 短线：T+1 开盘 ⬆️+2% 止盈 / ⬇️-2% 止损 / ⏳T+3 时间止损")
    else:
        lines.append("  💡 长线：⏳3-6 个月，按月跟踪 🧭北向 + 📊季报")
    lines.append(f"  ⏰ 生成时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    lines.append("  📌 仅供参考，不构成投资建议；买入请手动执行")
    lines.append("=" * 45)

    return "\n".join(lines)
