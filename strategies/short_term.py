"""
短线尾盘策略

核心逻辑：
  尾盘（14:50-15:00）全市场扫描 → 多因子评分 → 推荐Top N
  如果市场赚钱效应差，自动跳过推荐。

权重分配（config.yml 实际权重）：
  主力资金流 25% + 动量 25% + 技术形态 15% + 量价配合 10%
  + 北向资金 10% + 热点题材 10% + 龙虎榜 5%
  （risk 是过滤层，不占权重）

卖出规则：
  - 止盈：T+1 开盘+2% 以上分批止盈
  - 止损：T+1 开盘-2% 或 收盘跌破MA5
  - 时间止损：T+3 日无表现
"""

import json
import logging
import os
import time
from typing import Dict, List, Optional
from datetime import datetime, timedelta
from concurrent.futures import ThreadPoolExecutor, as_completed

import pandas as pd

from strategies.base import BaseStrategy
from core.data_engine import DataEngine
from core.scoring_model import ScoringModel
from core.risk_filter import RiskFilter
from core.portfolio_optimizer import PortfolioOptimizer
from core.expert_ensemble import ExpertEnsemble, confidence_to_weight_factor

logger = logging.getLogger(__name__)

# 市场档位名（2026-09-17 修复：此前 _assess_market 产出 '中性市' 而阈值表键为
# '中性'，导致中性市门槛静默回落到 self.min_score=75 而非设计的 70）
LEVEL_VERY_WEAK = '极差市'
LEVEL_WEAK = '弱市'
LEVEL_NEUTRAL = '中性市'
LEVEL_STRONG = '强市'
# 动态评分门槛默认表（config: short_term.buy.dynamic_min_score 可覆盖）
DEFAULT_DYNAMIC_MIN_SCORE = {
    LEVEL_STRONG: 65, LEVEL_NEUTRAL: 70, LEVEL_WEAK: 75,
}


class ShortTermStrategy(BaseStrategy):
    """短线尾盘策略"""

    def __init__(self, config: dict = None):
        super().__init__(config)
        config = config or {}
        self.data_engine = DataEngine()
        # 修复：从 config.yml 的 weights 键加载，而非不存在的 weights_model
        weights_cfg = config.get('weights', config.get('weights_model'))
        self.scoring_model = ScoringModel(
            weights=weights_cfg if weights_cfg else None,
            sell_config=config.get('sell', {})
        )
        # 从嵌套的 buy 段读取参数
        buy_cfg = config.get('buy', {})
        self.buy_cfg = buy_cfg   # B 方案动态门槛需要（2026-09-07）
        self.risk_filter = RiskFilter(config=buy_cfg)
        self.top_n = buy_cfg.get('max_candidates', 3)
        self.min_score = buy_cfg.get('min_score', 60)
        # 动量硬过滤阈值（候选池内 rps_20 百分位上限），0=关闭（2026-09-05 胜率对齐）
        self.momentum_filter_rps_max = buy_cfg.get('momentum_filter_rps_max', 80)
        # 组合层风控（2026-09-05 架构对标 #2）
        self.max_per_board = buy_cfg.get('max_per_board', 2)          # 同板块上限，0=关闭
        # P3（2026-09-18）：拥挤度断路器——全市场日均涨幅（绝对值）达到阈值的
        # 极端日停推。依据 run52 分解：市场日均涨幅 >=2.0% 的 9 笔 0 胜
        # （−4.52%/笔，合计 −40.7pp）；机制 = 全市场暴涨日尾盘追题材 → 次日回落
        # （study-vault F02 拥挤度逻辑）。0=关闭；默认 config 2.0。
        self.crowding_avg_pct = buy_cfg.get('crowding_avg_pct', 0)
        # R1（2026-09-18）：拥挤度分档仓位压缩——[[市场日均涨幅阈值, 仓位系数], ...]，
        # 按阈值降序匹配首个满足项。依据 run52 分桶的单调亏损关系（见 config 注释）。
        # 默认空列表 = 关闭（保持历史行为，需 config 显式启用）。
        self.crowding_scale_levels = buy_cfg.get('crowding_scale_levels', []) or []
        # 弱市仓位系数（2026-09-18 参数化）：原为硬编码 0.5，无法 A/B 验证。
        # 1.0 = 关闭弱市压缩（用于仓位机制的对照实验）。
        self.weak_market_scale = buy_cfg.get('weak_market_scale', 0.5)
        # 影子推荐（2026-09-18）：极端市况（极差市/拥挤度断路器）照常评分并标记 shadow，
        # 由 eod 侧以 mode='shadow' 落库、不下发、不进正式统计（下游 7 处硬滤 mode='short'）。
        self.shadow_enabled = bool(config.get('shadow_enabled', True))
        self._shadow_mode = False
        self._shadow_reason = None
        # P4（2026-09-18）：尾盘急拉过滤——收盘位置 (close-low)/(high-low) 高于
        # 阈值的票剔除。依据 run61 分解（104 笔）：close_pos>=0.8 的 35 笔均值
        # −1.11%（合计 ~−39pp），而 [0.3,0.8) 为 +0.10%；机制 = 收在当日高位
        # 附近的票次日回落（study-vault F04 尾盘急拉诱多）。0=关闭。
        self.max_close_pos = buy_cfg.get('max_close_pos', 0)
        # T4（2026-09-06 第一梯队）：组合相关性约束 + 波动率保险丝
        self.max_correlation = buy_cfg.get('max_correlation', 0.85)   # 持仓两两相关上限，0=关闭
        self.vol_breaker_median_abs = buy_cfg.get('vol_breaker_median_abs', 3.0)  # 全市场中位|涨幅|%触发线
        self.vol_breaker_scale = buy_cfg.get('vol_breaker_scale', 0.8)            # 触发后的仓位缩放
        self.losing_streak_days = buy_cfg.get('losing_streak_days', 3)   # 连续亏损日阈值
        self.losing_streak_scale = buy_cfg.get('losing_streak_scale', 0.5)  # 触发后仓位系数
        # 策略级 kill-switch（2026-09-05 审查报告 P3-R）：
        # 最近 N 个有 T+1 结果的交易日累计收益 <= -drawdown 时停止当日推荐。
        # drawdown 小数（0.10 = 10%），0 = 关闭；样本不足自动放行。
        self.killswitch_window_days = buy_cfg.get('killswitch_window_days', 20)
        self.killswitch_drawdown = buy_cfg.get('killswitch_drawdown', 0.10)
        # 2026-09-17 T5：_enrich_data 写入最近一次动量硬过滤的 before/after/removed，
        # 供 run_context 持久化（运行期信息存档）。None 表示本运行尚未执行过滤。
        self._last_momentum_filter_info = None

    def run(self, market_data: Dict = None) -> List[Dict]:
        """
        运行短线尾盘策略

        参数：
          market_data: 可选，外部传入的全市场数据

        返回：
          推荐列表 [{code, name, score, rating, decision, ...}]
        """
        logger.info("=" * 50)
        logger.info("短线尾盘策略运行中...")

        # 回测模式标志（从 market_data 传入，非回测时为 False）
        is_backtest = market_data and market_data.get('backtest_mode', False)
        backtest_date = market_data.get('backtest_date') if is_backtest else None

        # T5（2026-09-17）：运行期信息持久化所需的局部状态（默认值先行，
        # 保证各提前 return 分支也能写出合法 run_context 且不报错）。
        _run_id = datetime.now().strftime('%Y%m%d')
        _market = None
        _eff_top_n = None
        _eff_min = None
        _pos_scale = None
        _vb_scale = 1.0
        _ls_scale = 1.0
        _killswitch = {'triggered': False, 'cum_return': None, 'n_days': 0}
        _recommended = []

        # 1. 获取全市场行情
        if market_data:
            quotes_df = market_data.get('quotes_df')
        else:
            quotes_df = self.data_engine.get_all_quotes()

        if quotes_df is None or quotes_df.empty:
            logger.warning("无可用的行情数据，策略跳过")
            self._emit_run_context(_run_id, _market, _eff_top_n, _eff_min, _pos_scale,
                                   _vb_scale, _ls_scale, _killswitch, _recommended, is_backtest)
            return []

        logger.info(f"全市场共 {len(quotes_df)} 只股票")

        # 1.5 市场环境评估（0额外API成本，从行情数据计算）
        # 同花顺强势股（回测模式下跳过实时API）
        if is_backtest:
            # 2026-09-17 修复：此前回测无条件清空热点 → hot_theme（权重 0.50）在
            # 回测中恒中性，既导致"回测 0 交易"（评分上限被锁在 ~61 < 门槛 65），
            # 也使该因子从未被回测验证。现在从因子仓库 hot_stocks 历史（2024-01
            # 起 658 天）按日读取，hot_theme 在回测中与实盘同语义。
            hot_codes = set(market_data.get('hot_codes') or ())
            hot_df = (pd.DataFrame({'代码': sorted(hot_codes)})
                      if hot_codes else pd.DataFrame())
            logger.info(f"回测模式：热点历史命中 {len(hot_codes)} 只")
        else:
            hot_df = self.data_engine.get_ths_hot_stocks()
            hot_codes = set()
            if not hot_df.empty:
                hot_codes = set(str(c).zfill(6) for c in hot_df['代码'].tolist() if pd.notna(c))
                logger.info(f"同花顺强势股: {len(hot_codes)} 只有题材归因标签")
        market_assessment = self._assess_market(quotes_df, hot_df, is_backtest=is_backtest)
        _market = market_assessment
        logger.info(f"市场环境综合评分: {market_assessment['total']}/100 "
                     f"({market_assessment['level']})")

        if market_assessment['skip']:
            _skip_reason = (f"市场赚钱效应较差({market_assessment['level']})，"
                            f"建议空仓观望或减仓")
            # 影子推荐（2026-09-18）：极端市况不再提前返回，改为照常评分并标记 shadow，
            # 由 eod 侧以 mode='shadow' 落库、不下发、不进正式统计（下游 7 处硬滤
            # mode='short'）。目的：破解"样本删失"——为"冰点期该不该推"攒直接证据。
            # 开关 short_term.shadow_enabled（默认 true）；回测（is_backtest）不受影响。
            if self.shadow_enabled and not is_backtest:
                self._shadow_mode = True
                self._shadow_reason = _skip_reason
                logger.info(f"影子模式：{_skip_reason} → 照常评分落库（不下发、不进正式统计）")
            else:
                logger.warning(f"市场赚钱效应较差({market_assessment['level']})，策略跳过")
                # 仍然把市场评估信息传出去（供报告显示）
                self._emit_run_context(_run_id, _market, _eff_top_n, _eff_min, _pos_scale,
                                       _vb_scale, _ls_scale, _killswitch, _recommended, is_backtest)
                return [{
                    'market_assessment': market_assessment,
                    'data_source_status': self.data_engine.get_data_source_summary(),
                    'skip_reason': _skip_reason,
                }]

        # P3（2026-09-18）：拥挤度断路器——全市场日均涨幅极端日停推。
        # 证据（run52 分解，4-8 月 111 笔）：市场日均涨幅 >=2.0% 的决策日
        # 9 笔 0 胜（−4.52%/笔）；机制 = 暴涨日尾盘追题材 → 次日拥挤回落
        # （study-vault F02）。对称用 |avg_chg|（暴跌日本就无交易，对称无额外影响）。
        _crowding_reason = self._crowding_breaker_reason(market_assessment)
        try:
            _crowding_avg = abs(float((market_assessment.get('details') or {}).get('avg_chg', 0) or 0))
        except (TypeError, ValueError):
            _crowding_avg = 0.0
        if _crowding_reason:
            # 2026-09-18 审查补充：记录断路器触发（run_context 可追溯）
            self._last_crowding = {'breaker': True, 'reason': _crowding_reason,
                                   'scale': 1.0, 'avg_chg': _crowding_avg}
            # 影子推荐（2026-09-18）：与极差市 skip 同一处理——照常评分并标记 shadow，
            # 落库但不该下发（配置 short_term.shadow_enabled 控制；回测不受影响）。
            if self.shadow_enabled and not is_backtest:
                self._shadow_mode = True
                self._shadow_reason = _crowding_reason
                logger.info(f"影子模式：{_crowding_reason} → 照常评分落库（不下发）")
            else:
                logger.warning(f"{_crowding_reason} → 停推")
                self._emit_run_context(_run_id, _market, _eff_top_n, _eff_min, _pos_scale,
                                       _vb_scale, _ls_scale, _killswitch, _recommended, is_backtest)
                return [{
                    'market_assessment': market_assessment,
                    'data_source_status': self.data_engine.get_data_source_summary(),
                    'skip_reason': _crowding_reason,
                }]

        # 策略健康度检查（2026-09-06 改，原 P3-R kill-switch）：
        # 【行为变更】原来触发时直接 return 停推；现按用户决策改为"软提示"——
        # 仍计算最近 N 个有 T+1 结果交易日的累计收益并随推荐下发
        # （rec['strategy_health']，简报据此渲染风险提示），但不再剥夺当日推荐。
        # 失效期信息展示给使用者自行权衡；市场评估的极差市 skip（上方）仍然生效。
        killswitch = self._check_killswitch(
            self.killswitch_window_days, self.killswitch_drawdown, is_backtest)
        _killswitch = killswitch
        if killswitch['triggered']:
            logger.warning(
                f"策略健康提示: 最近 {killswitch['n_days']} 个交易日推荐标的 "
                f"T+1 累计 {killswitch['cum_return']:+.2f}% "
                f"≤ -{self.killswitch_drawdown*100:.0f}%（不停推，简报提示谨慎参考）")

        # 根据市场环境调整参数（使用局部变量，不修改实例属性）
        # B 方案（2026-09-07 论证后实施）：动态评分门槛——hot_theme 0.50 权重
        # 使评分尺度压缩（非热点票天花板 ≈70），旧 min_score=75（资金权重
        # 时代定的）语义漂移为"只接受热点票"，导致 09-07 强市零推荐。
        # 改为随市场评估等级动态设定：强市 65 / 中性 70 / 弱市 75，
        # 语义自洽且与既有弱市收紧逻辑对称。config 可覆盖（dynamic_min_score）。
        _level = market_assessment['level']
        _dyn_min = self.buy_cfg.get('dynamic_min_score') or DEFAULT_DYNAMIC_MIN_SCORE
        if _level == LEVEL_WEAK:
            effective_top_n = min(self.top_n, 2)   # 弱市最多推荐2只
        else:
            effective_top_n = self.top_n
        effective_min_score = int(_dyn_min.get(_level, self.min_score))
        _eff_top_n = effective_top_n
        _eff_min = effective_min_score
        logger.info(f"市场({_level}) 动态门槛: 最低评分{effective_min_score} "
                    f"(配置基准 {self.min_score}), 推荐上限{effective_top_n}")

        # 市场状态仓位开关（2026-09-05 评估改进6）：弱市把总仓位压缩到 50%
        # （其余留现金），把低迷月的亏损半径减半而不是指望完全避开——
        # 实测 run23 弱月（1月胜率17%）稳定亏损，全停会错过 3 月类修复，
        # 半仓是亏损控制与机会成本的折中。极差市仍走上方 skip 全停。
        position_scale = (self.weak_market_scale
                          if market_assessment['level'] == LEVEL_WEAK else 1.0)
        _pos_scale = position_scale
        # 组合层风控④（T4，2026-09-06 第一梯队）：波动率保险丝——全市场当日
        # |中位数涨幅| 超过阈值（默认 3%，历史极罕见）时再压缩仓位。
        # 正常交易日中位 |涨幅| 约 0.5-1.5%，此保险丝平时不触发。
        _vb_scale = self._get_vol_circuit_scale(market_assessment)
        position_scale *= _vb_scale
        # 组合层风控②（架构对标 #2）：连续亏损日序列风控——最近 N 个有结果的
        # 交易日日均收益连续为负时再压缩仓位（入场后的序列风险此前无人管）
        _ls_scale = self._get_losing_streak_scale(
            self.losing_streak_days, self.losing_streak_scale, is_backtest)
        position_scale *= _ls_scale
        # R1（2026-09-18）：拥挤度分档仓位压缩——P3 硬停（>=2.0%）只覆盖最极端档，
        # 其余档位此前无响应。依据 run52 分桶：市场日均涨幅 [-0.5,0.5) 桶 +1.31%/笔，
        # [0.5,1.0) -1.26%，[1.0,1.5) -0.11%，[1.5,2.0) -0.41%（单调走弱）。
        # 档位与系数由 config buy.crowding_scale_levels 配置（[[阈值, 系数], ...] 降序）。
        _cg_scale = self._crowding_position_scale(market_assessment)
        position_scale *= _cg_scale
        # 2026-09-18 审查补充：记录 crowding 取值供 run_context 追溯
        self._last_crowding = {'breaker': False, 'reason': None,
                               'scale': _cg_scale, 'avg_chg': _crowding_avg}

        # 2. 批量过滤 + 预评分
        # 2.0 预热 lockup 缓存（一次 akshare 调用 ~3-4s，让 5000+ 只预过滤不走 IO）
        try:
            self.data_engine._ensure_lockup_cache()
        except Exception as e:
            logger.warning(f"lockup 缓存预热失败(已熔断): {str(e)[:60]}")

        candidates = self._prefilter(quotes_df)
        logger.info(f"预过滤后 {len(candidates)} 只进入详评")

        if len(candidates) == 0:
            logger.info("今日尾盘策略跳过：没有足够合格的标的")
            self._emit_run_context(_run_id, _market, _eff_top_n, _eff_min, _pos_scale,
                                   _vb_scale, _ls_scale, _killswitch, _recommended, is_backtest)
            return []

        # 3. 获取详细数据（回测模式传入日期限制）
        enriched = self._enrich_data(candidates, hot_codes, is_backtest=is_backtest, backtest_date=backtest_date)

        # 4. 评分 + 排序
        # _diag：接收 rank_stocks 的门槛诊断（全部候选被 min_score 清零时，
        # 携带最高分标的），供下方"零推荐"分支透出给简报展示。
        _diag = {}
        recommendations = self.scoring_model.rank_stocks(
            enriched, mode='short',
            top_n=effective_top_n, min_score=effective_min_score,
            diagnostics=_diag
        )

        # 暴露详评数据给外部（用于因子采集）
        # 2026-09-14：提前到 null 判断之前——"零推荐"分支会提前 return，
        # 若仍留在下方会导致零推荐日因子采集拿不到 enriched 而断采。
        self._last_enriched = enriched

        if not recommendations:
            logger.info("今日尾盘策略跳过：没有评分达标的标的")
            # 2026-09-14 需求：因市场原因（市况偏弱→动态门槛上浮）导致零推荐时，
            # 除说明无推荐外，额外透出"最高分标的"及其分数，供人判断是
            # "没有好票"还是"门槛偏高"。回测模式不返回该元信息条目——
            # 回测按 rec['code'] 聚合统计，混入无 code 条目会污染交易样本。
            if not is_backtest and _diag.get('top_unqualified'):
                self._emit_run_context(_run_id, _market, _eff_top_n, _eff_min, _pos_scale,
                                       _vb_scale, _ls_scale, _killswitch, _recommended, is_backtest)
                return [{
                    'market_assessment': market_assessment,
                    'data_source_status': self.data_engine.get_data_source_summary(),
                    'no_qualified': True,
                    'min_score': effective_min_score,
                    'top_candidate': _diag['top_unqualified'],
                    'strategy_health': killswitch,
                }]
        else:
            logger.info(f"推荐 {len(recommendations)} 只股票 (上限{effective_top_n})")

        # 附加数据源状态到结果
        source_status = self.data_engine.get_data_source_summary()
        for rec in recommendations:
            rec['data_source_status'] = source_status
            rec['market_data'] = {}
            rec['strategy_health'] = killswitch  # 健康度随推荐下发，供简报渲染提示

        # 附加市场级数据（回测模式下跳过实时北向API）
        if not is_backtest:
            try:
                north_summary = self.data_engine.get_north_flow_summary()
                if north_summary:
                    for rec in recommendations:
                        rec['market_data'] = {'north_flow': north_summary}
            except Exception as e:
                logger.warning(f"北向数据补充失败: {e}")

        # 对推荐结果补充板块归属和龙虎榜（仅对 top N 做，走 em_get 限流）
        # 回测模式跳过实时拉取（避免回测因子 IC 被实时/stale 数据前视污染，
        # 回测推荐股 blocks 沿用评分前的状态：top30 空/未补）
        if not is_backtest:
            for rec in recommendations:
                code = rec['code']
                # rank_stocks 附带的全字段源 dict（含 main_fund_accumulated/rps_20/
                # macd_status/volume_ratio/risk_check/is_hot_stock 等评分所需输入）。
                # 瘦 rec 只有 code/name/score/breakdown，直接 score_stock 会让
                # compute_all_factors 全部 .get() 静默退化（capital_flow 假中性等）
                src = rec.get('_src')
                if not src:
                    # 防御：_src 缺失时跳过补算，保留排序时的原始分（避免静默塌缩）
                    logger.warning(f"{code} 缺 _src（非 rank_stocks 产物？），跳过板块补算")
                    continue
                # 评分前已 force_live 预加载的 blocks 直接复用（保持与排序一致），
                # 未就绪（如 top30 外）才补拉
                if src.get('blocks', {}).get('total', 0) > 0:
                    blocks = src['blocks']
                else:
                    try:
                        blocks = self.data_engine.get_stock_blocks(code, force_live=True)
                        src['blocks'] = blocks
                    except Exception as e:
                        logger.warning(f"板块获取失败 {code}: {str(e)[:80]}")
                        src['blocks'] = {"total": 0, "boards": [], "concept_tags": []}
                        blocks = src['blocks']
                try:
                    dt = self.data_engine.get_dragon_tiger(code)
                    src['dragon_tiger'] = dt
                except Exception as e:
                    logger.warning(f"龙虎榜获取失败 {code}: {str(e)[:80]}")
                # 板块和龙虎榜数据已补全，在【全字段源 dict】上整体重算 score_stock：
                # weighted/effective_weight/总分一起更新，避免"展示 raw 新值、
                # 排序 weighted 旧值"的脱节（补算前只改 raw_score 的残留 bug）
                try:
                    updated = self.scoring_model.score_stock(src, 'short')
                except Exception as e:
                    # 防御：重算失败保留排序时原始分（崩溃可见非静默污染，但别拖垮整个 run）
                    logger.warning(f"{code} 补算重算失败，保留排序时分数: {str(e)[:80]}")
                    continue
                rec['score'] = updated['score']
                rec['rating'] = updated['rating']
                rec['rating_cn'] = updated.get('rating_cn', rec.get('rating_cn'))
                rec['breakdown'] = updated['breakdown']
                # 同步决策字段：decision/target_price/stop_price/reasoning 由重算前 score 生成，
                # 不更新会出现"评级新、决策旧"的日报展示脱节（daily_report 展示这 4 项）
                rec['decision'] = updated.get('decision', rec.get('decision'))
                rec['target_price'] = updated.get('target_price', rec.get('target_price'))
                rec['stop_price'] = updated.get('stop_price', rec.get('stop_price'))
                rec['reasoning'] = updated.get('reasoning', rec.get('reasoning'))
                # 回写 blocks/dragon_tiger 到瘦 rec：日报展示（market_briefing）和
                # 因子采集（data_collector）读的是瘦 rec，不写会丢板块/龙虎榜展示 + 龙虎榜表全记 0
                rec['blocks'] = src.get('blocks', {"total": 0, "boards": [], "concept_tags": []})
                rec['dragon_tiger'] = src.get('dragon_tiger', {"records": [], "seats": {"buy": [], "sell": []}, "institution": {}})
                # breakdown 中 hot_theme / dragon_tiger 的实际得分通过 score_stock 重算，
                # weighted/effective_weight 与 raw_score 严格一致，让配置里 0.10+0.05 这 15% 权重真的生效

        # ========== 专家评分 Ensemble 第二意见 ==========
        # 用 5 维独立评分（fundamental/technical/capital/valuation/event）
        # 与 7 因子加权模型做差异检测。一致时微调上拉、分歧时降权、严重冲突标记 conflict。
        # 输出 ensemble_score / confidence / expert_delta 三字段供 portfolio_optimizer 调仓使用。
        try:
            ensemble = ExpertEnsemble()
            for rec in recommendations:
                # P0-B 修复（2026-09-05 审查报告）：rec 是瘦结构，fundamentals/rps/
                # pe_percentile/main_fund 等字段恒 None → 专家 5 维全部退化 50 分、
                # expert_score 恒 50，score≥75 时 Δ≤-25 恒触发 conflict，
                # ensemble 数学上退化为 no-op（第二意见从未真正发声）。
                # 改从 _src（rank_stocks 附带的全字段源 dict）取专家维所需输入；
                # score 仍用瘦 rec 的加权模型分（补算环节已同步重算，是最新基准分）。
                src_full = rec.get('_src') or rec
                src_for_ensemble = {
                    'code': rec.get('code'),
                    'name': rec.get('name'),
                    'price': rec.get('price', src_full.get('price')),
                    'score': rec.get('score', 50.0),
                    'fundamentals': src_full.get('fundamentals'),
                    'recent_events': src_full.get('recent_events'),
                    'main_fund_accumulated': src_full.get('main_fund_accumulated'),
                    'north_flow_accumulated': src_full.get('north_flow_accumulated'),
                    'pct_chg': src_full.get('pct_chg'),
                    'turnover': src_full.get('turnover'),
                    'rps': src_full.get('rps_20'),   # 字段名映射：源 dict 用 rps_20
                    'pe_percentile': src_full.get('pe_percentile'),
                    'pb_percentile': src_full.get('pb_percentile'),
                    'amount_ratio': src_full.get('volume_ratio'),
                    '_decision_date': rec.get('_decision_date') or src_full.get('_decision_date'),
                }
                verdict = ensemble.fuse(src_for_ensemble)
                rec['expert_score'] = verdict.expert_score
                rec['expert_delta'] = verdict.delta
                rec['ensemble_score'] = verdict.ensemble_score
                rec['confidence'] = verdict.confidence
                rec['weight_factor'] = confidence_to_weight_factor(verdict.confidence)
                rec['expert_breakdown'] = verdict.expert_breakdown
                if verdict.confidence == 'conflict':
                    logger.warning(
                        f"[{rec.get('code')}] 专家 ensemble 冲突: "
                        f"model={verdict.model_score} expert={verdict.expert_score} "
                        f"Δ={verdict.delta:+.1f}  ensemble={verdict.ensemble_score}"
                    )
            logger.info(
                f"ensemble 完成: "
                f"{sum(1 for r in recommendations if r.get('confidence')=='high')} 高一致 / "
                f"{sum(1 for r in recommendations if r.get('confidence')=='medium')} 中度 / "
                f"{sum(1 for r in recommendations if r.get('confidence')=='conflict')} 冲突"
            )
        except Exception as e:
            logger.warning(f"ExpertEnsemble 融合失败，降级到仅主模型分数: {str(e)[:120]}")

        # 组合优化：评分加权仓位分配（传入 ensemble_score 让 portfolio_optimizer 可选使用）
        # 组合层风控①（架构对标 #2）：同板块数量上限——top3 若全是同一板块
        # 热点，单日相关性≈1。无板块信息（回测/补算失败）的票不受约束。
        recommendations = self._apply_board_diversification(recommendations, self.max_per_board)
        # 组合层风控③（T4，2026-09-06 第一梯队）：持仓两两相关性上限——
        # 用详评 K 线最近 20 日收益相关性，贪心保留高分票。
        # 阈值默认 0.85（只拦"几乎同涨同跌"的极端同质持仓，正常场景不触发）。
        recommendations = self._apply_correlation_filter(recommendations)
        recommendations = PortfolioOptimizer.allocate(recommendations)

        # 弱市总仓位压缩（2026-09-16 P1-2 后：allocate 输出已含单票上限约束，
        # 对 ≤2 只推荐不足 100% 的部分已记为现金；本步再按市况 ×scale，
        # 剩余比例同样留现金）
        recommendations = self._apply_position_scale(recommendations, position_scale)

        # 影子模式标记（2026-09-18）：极差市/拥挤度断路器触发时仍走完整评分，
        # 在此给每条推荐打 shadow 标记 → eod 侧据此以 mode='shadow' 落库、不下发。
        if self._shadow_mode:
            for rec in recommendations:
                rec['shadow'] = True
                rec['shadow_reason'] = self._shadow_reason
            logger.info(f"影子模式输出 {len(recommendations)} 条（标记 shadow，仅落库不下发）")

        # T5（2026-09-17）：推荐确定后持久化运行期信息（纯新增，不改选股逻辑）
        _recommended = recommendations
        self._emit_run_context(_run_id, _market, _eff_top_n, _eff_min, _pos_scale,
                               _vb_scale, _ls_scale, _killswitch, _recommended, is_backtest)
        return recommendations

    # ========== T5（2026-09-17）：运行期信息持久化 ==========
    # ========== P3（2026-09-18）：拥挤度断路器 ==========
    def _crowding_breaker_reason(self, market_assessment: dict):
        """全市场日均涨幅极端日 → 返回停推原因字符串；未触发返回 None。

        阈值 self.crowding_avg_pct（config buy.crowding_avg_pct，0=关闭）。
        对称用 |avg_chg|：暴跌日本就无交易（动态门槛+风险过滤），对称无额外影响。
        """
        if not self.crowding_avg_pct:
            return None
        details = (market_assessment or {}).get('details') or {}
        try:
            avg_chg = abs(float(details.get('avg_chg', 0) or 0))
            _thr = float(self.crowding_avg_pct)
        except (TypeError, ValueError):
            # 2026-09-18 审查修复：阈值非数值时不得抛异常（原实现只护了 avg_chg，
            # float(self.crowding_avg_pct) 未护 → 配置写成 "2.0%" 之类会直接崩策略）
            logger.warning(f"crowding_avg_pct 非数值({self.crowding_avg_pct!r})，断路器本次跳过")
            return None
        if avg_chg >= _thr:
            return (f"拥挤度断路器: 全市场日均涨幅 {avg_chg:.2f}% ≥ "
                    f"{_thr}%，暴涨日追题材次日回落风险高，建议空仓")
        return None

    def _crowding_position_scale(self, market_assessment: dict) -> float:
        """拥挤度分档仓位系数（R1，2026-09-18）。

        依据 run52 分桶（市场日均涨幅 → 次日单笔均值）：
          [-0.5, 0.5) +1.31% | [0.5, 1.0) -1.26% | [1.0, 1.5) -0.11%
          | [1.5, 2.0) -0.41% | >=2.0 -4.52%（0 胜，由 P3 硬停覆盖）
        档位由 config buy.crowding_scale_levels = [[阈值, 系数], ...] 提供，
        按阈值降序匹配首个满足 |avg_chg| >= 阈值 的档；未配置/无数据 → 1.0。
        """
        levels = self.crowding_scale_levels
        if not levels:
            return 1.0
        details = (market_assessment or {}).get('details') or {}
        try:
            avg_chg = abs(float(details.get('avg_chg', 0) or 0))
        except (TypeError, ValueError):
            return 1.0
        try:
            ordered = sorted(levels, key=lambda x: float(x[0]), reverse=True)
        except (TypeError, IndexError, ValueError):
            return 1.0
        for threshold, scale in ordered:
            try:
                if avg_chg >= float(threshold):
                    return float(scale)
            except (TypeError, ValueError):
                continue
        return 1.0

    def _safe_data_source_status(self) -> dict:
        """安全获取数据源健康状态（data_engine 缺失/异常时回退空 dict）。"""
        try:
            if self.data_engine is not None:
                return self.data_engine.get_data_source_summary() or {}
        except Exception:
            pass
        return {}

    def _emit_run_context(self, run_id: str, market, eff_top_n, eff_min, pos_scale,
                          vb_scale, ls_scale, killswitch, recommended, is_backtest: bool):
        """持久化运行期信息到 data/reports/run_context_YYYYMMDD.json（2026-09-17 T5）。

        纯新增写入，完全不改选股逻辑。记录当日：市场档位/生效门槛/regime 开关取值/
        动量过滤明细，使 56 个交易日的档位可复原、与回测口径对齐。

        任何异常都被吞掉（仅 warning），绝不影响选股主流程。
        """
        try:
            from datetime import datetime as _dt
            date_str = _dt.now().strftime('%Y-%m-%d')
            reports_dir = os.path.join(
                os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                'data', 'reports')
            os.makedirs(reports_dir, exist_ok=True)
            path = os.path.join(reports_dir, f"run_context_{run_id}.json")

            # 关联 run_id 到推荐（使 predictions DB 与 run_context 可关联）
            recs_out = []
            for r in (recommended or []):
                recs_out.append({'code': r.get('code'), 'score': r.get('score')})
                try:
                    r['run_id'] = run_id
                except Exception:
                    pass

            # regime：各开关取值（从已算出的 scale 反推 triggered，与 helper 口径一致）
            median_abs = None
            if isinstance(market, dict):
                median_abs = abs(float((market.get('details') or {}).get('median_chg', 0) or 0))
            vol_breaker = {
                'triggered': bool(vb_scale is not None and vb_scale != 1.0),
                'median_abs_pct': round(median_abs, 4) if median_abs is not None else None,
            }
            losing_streak = {
                'triggered': bool(ls_scale is not None and ls_scale != 1.0),
                'scale': ls_scale,
            }
            momentum_info = getattr(self, '_last_momentum_filter_info', None) or {
                'before': None, 'after': None, 'removed': None}
            regime = {
                'skip': bool(market.get('skip')) if isinstance(market, dict) else False,
                'position_scale': pos_scale,
                'vol_breaker': vol_breaker,
                'losing_streak': losing_streak,
                'killswitch': killswitch or {'triggered': False, 'cum_return': None, 'n_days': 0},
                'momentum_filter': momentum_info,
                # 2026-09-18 审查补充：拥挤度断路器与分档取值（可追溯）
                'crowding': getattr(self, '_last_crowding', None)
                            or {'breaker': False, 'reason': None, 'scale': 1.0, 'avg_chg': None},
            }
            ctx = {
                'run_id': run_id,
                'date': date_str,
                'mode': 'short',
                'market': {
                    'total': market.get('total') if isinstance(market, dict) else None,
                    'level': market.get('level') if isinstance(market, dict) else None,
                    'details': market.get('details') if isinstance(market, dict) else None,
                },
                'thresholds': {
                    'effective_min_score': eff_min,
                    'effective_top_n': eff_top_n,
                    'config_min_score': self.min_score,
                },
                'regime': regime,
                'recommended': recs_out,
                'data_source_status': self._safe_data_source_status(),
            }
            with open(path, 'w', encoding='utf-8') as f:
                json.dump(ctx, f, ensure_ascii=False, indent=2, default=str)
            logger.info(f"run_context 已写入: {path}")
        except Exception as e:
            logger.warning(f"run_context 写入失败（不影响选股）: {str(e)[:80]}")

    @staticmethod
    def _apply_position_scale(recommendations: list, scale: float) -> list:
        """按市场状态压缩总仓位：allocation_pct × scale，其余比例留现金。

        回测组合模拟按日内归一化（_simulate_portfolio 的 capital_ratio），
        同比例缩放对回测净值无影响；该开关作用于实盘仓位决策。
        """
        if scale >= 1.0 or not recommendations:
            return recommendations
        total = 0.0
        for rec in recommendations:
            if rec.get('allocation_pct'):
                rec['allocation_pct'] = round(rec['allocation_pct'] * scale, 2)
            rec['position_scale'] = scale
            total += rec.get('allocation_pct') or 0
        # 压缩后同步现金口径（2026-09-16 P1-2：暴露"剩下的钱在哪"）
        cash = round(max(0.0, 100.0 - total), 1)
        for rec in recommendations:
            rec['cash_pct'] = cash
        logger.info(f"弱市仓位压缩: 总仓位 ×{scale}（其余留现金）")
        return recommendations

    @staticmethod
    def _apply_momentum_filter(enriched: list, rps_max: float) -> list:
        """剔除非热点·高动量票（胜率对齐实验结论）；rps_max<=0 关闭。

        热点票（is_hot_stock=True）豁免——热点的高动量段仍是正期望段。
        """
        if not rps_max or rps_max <= 0 or not enriched:
            return enriched
        kept = []
        for s in enriched:
            rps = s.get('rps_20')
            is_hot = bool(s.get('is_hot_stock'))
            if (not is_hot) and rps is not None and rps >= rps_max:
                continue
            kept.append(s)
        return kept

    @staticmethod
    def _apply_board_diversification(recommendations: list, max_per_board: int) -> list:
        """同板块数量上限（组合层风控①）。板块名取 blocks.boards[].name，
        剔除地域板块（'XX板块'结尾）与风格/指数标签（融资融券/沪股通/MSCI 等），
        与 factor_library.calc_hot_theme_score 的过滤口径一致。
        无板块归属的票不参与约束；max_per_board<=0 关闭。"""
        if not max_per_board or max_per_board <= 0 or len(recommendations) <= 1:
            return recommendations
        style_kw = ('融资融券', '沪股通', '深股通', 'MSCI', '中证', '标普', '富时', '同花顺')
        kept, board_count = [], {}
        for rec in recommendations:
            boards = (rec.get('blocks') or {}).get('boards') or []
            board_key = None
            for b in boards:
                if not isinstance(b, dict):
                    continue
                bname = str(b.get('name', '')).strip()
                if not bname or bname.endswith('板块'):
                    continue
                if any(k in bname for k in style_kw):
                    continue
                board_key = bname
                break
            if board_key is None:
                kept.append(rec)
                continue
            if board_count.get(board_key, 0) >= max_per_board:
                logger.info(f"板块分散约束: 剔除 {rec.get('code')}（板块[{board_key}]已达 {max_per_board} 只）")
                continue
            board_count[board_key] = board_count.get(board_key, 0) + 1
            kept.append(rec)
        return kept

    def _apply_correlation_filter(self, recommendations: list) -> list:
        """持仓两两相关性约束（组合层风控③，T4 2026-09-06）。

        对推荐列表按分数降序贪心保留：候选与任一已入选票的最近 20 日
        收益相关系数 > max_correlation → 剔除。K 线缺失的票不参与约束
        （直接保留），与板块分散的宽容口径一致。max_correlation<=0 关闭。

        设计为"保险丝"：默认阈值 0.85 只拦极端同质持仓，正常推荐不受影响。
        """
        if (not self.max_correlation or self.max_correlation <= 0
                or len(recommendations) <= 1):
            return recommendations
        series_map = {}
        for rec in recommendations:
            src = rec.get('_src') or {}
            kl = src.get('kline_df')
            if kl is not None and len(kl) >= 21 and 'close' in kl.columns:
                try:
                    s = pd.to_numeric(kl['close'], errors='coerce').tail(21)
                    series_map[rec.get('code')] = s.reset_index(drop=True).pct_change().dropna()
                except Exception:
                    continue
        if len(series_map) < 2:
            return recommendations

        def _corr(a, b):
            try:
                joined = pd.concat([a, b], axis=1, join='inner').dropna()
                if len(joined) < 10:
                    return None
                return float(joined.corr().iloc[0, 1])
            except Exception:
                return None

        ordered = sorted(recommendations, key=lambda r: r.get('score', 0), reverse=True)
        kept, kept_series = [], []
        for rec in ordered:
            s = series_map.get(rec.get('code'))
            if s is None:
                kept.append(rec)
                continue
            clash = False
            for ks in kept_series:
                c = _corr(s, ks)
                if c is not None and c > self.max_correlation:
                    logger.info(f"相关性约束: 剔除 {rec.get('code')}"
                                f"（与已选票相关 {c:.2f} > {self.max_correlation}）")
                    clash = True
                    break
            if not clash:
                kept.append(rec)
                kept_series.append(s)
        return kept

    def _get_vol_circuit_scale(self, market_assessment: dict) -> float:
        """波动率保险丝（组合层风控④，T4 2026-09-06）。

        全市场当日中位数涨幅的绝对值超过 vol_breaker_median_abs（默认 3%）
        → 返回 vol_breaker_scale（默认 0.8）再压缩仓位；正常交易日
        中位 |涨幅| 约 0.5-1.5%，此保险丝平时恒为 1.0（不改变任何输出）。
        """
        try:
            median_abs = abs(float(market_assessment.get('details', {}).get('median_chg', 0)))
            if median_abs > self.vol_breaker_median_abs:
                logger.warning(
                    f"波动率保险丝触发: 全市场中位|涨幅| {median_abs:.2f}% > "
                    f"{self.vol_breaker_median_abs}% → 仓位 ×{self.vol_breaker_scale}")
                return self.vol_breaker_scale
        except Exception as e:
            logger.warning(f"波动率保险丝计算失败（忽略）: {str(e)[:50]}")
        return 1.0

    def _recent_daily_returns(self, days: int, is_backtest: bool = False,
                              mode: str = 'short'):
        """公共查询（2026-09-06 审查重构；2026-09-07 回访修复 mode 参数化）：
        最近 days 个有 T+1 结果交易日的日均收益列表（按日期降序），
        供 _get_losing_streak_scale 与 _check_killswitch 复用。
        注意：当前 kill-switch/连亏风控仅作用于短线（mode='short'），
        长线模式未接入该风控链路（数据链路审查结论，属已知设计现状）。

        返回 [(date, day_ret), ...]；回测模式 / 无库 / 异常时返回 None（调用方按放行处理）。
        """
        if is_backtest or not days or days <= 0:
            return None
        try:
            import sqlite3
            db = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                              'data', 'db', 'predictions.db')
            if not os.path.exists(db):
                return None
            conn = sqlite3.connect(db)
            try:
                return conn.execute("""
                    SELECT p.date, AVG(o.t1_return) AS day_ret
                    FROM predictions p JOIN outcomes o ON o.prediction_id = p.id
                    WHERE p.mode=? AND o.t1_return IS NOT NULL
                    GROUP BY p.date ORDER BY p.date DESC LIMIT ?
                """, (mode, days)).fetchall()
            finally:
                conn.close()
        except Exception as e:
            logger.warning(f"交易日收益查询失败: {str(e)[:60]}")
            return None

    def _get_losing_streak_scale(self, days: int, scale: float,
                                 is_backtest: bool = False) -> float:
        """连续亏损日序列风控（组合层风控②）：最近 days 个有 T+1 结果的交易日，
        日均收益连续为负 → 返回 scale（再压缩仓位）；否则 1.0。
        样本不足 / 回测模式 / 查询异常一律返回 1.0（不误伤）。"""
        rows = self._recent_daily_returns(days, is_backtest)
        if not rows:
            return 1.0
        try:
            if len(rows) < days:
                return 1.0
            if all(r[1] is not None and r[1] < 0 for r in rows):
                logger.warning(f"连续 {len(rows)} 个交易日日均收益为负，仓位再压缩 ×{scale}")
                return scale
        except Exception as e:
            logger.warning(f"连亏检查失败（忽略，不阻塞选股）: {str(e)[:60]}")
        return 1.0

    def _check_killswitch(self, window_days: int, drawdown: float,
                          is_backtest: bool = False) -> Dict:
        """策略健康度检查（2026-09-06 改，原 P3-R kill-switch 硬熔断 → 软提示）。

        最近 window_days 个有 T+1 结果的交易日累计收益 <= -drawdown 时
        triggered=True（仅作为展示标记，不再拦截推荐——由调用方决定渲染提示）。
        与 _get_losing_streak_scale 复用同一查询口径（predictions.db 日均 t1_return）。
        回测模式 / drawdown<=0 一律不触发（不误伤）。
        样本不足窗口时仍回填 cum_return/n_days（按实际可得天数），方便展示，
        但 triggered 恒 False。
        """
        result = {'triggered': False, 'cum_return': None, 'n_days': 0}
        if not window_days or window_days <= 0 or not drawdown or drawdown <= 0:
            return result
        rows = self._recent_daily_returns(window_days, is_backtest)
        if rows is None:
            return result
        try:
            if rows:
                cum = 1.0
                valid = 0
                for r in rows:
                    if r[1] is None:
                        continue
                    cum *= (1 + r[1] / 100.0)   # t1_return 存储为百分数
                    valid += 1
                if valid:
                    result['cum_return'] = round((cum - 1.0) * 100, 2)
                    result['n_days'] = valid
            if len(rows) < window_days:
                return result       # 样本不足：展示实际值，但不触发
            cum_ret = (result['cum_return'] / 100.0) if result['cum_return'] is not None else 0.0
            if cum_ret <= -abs(drawdown):
                result['triggered'] = True
        except Exception as e:
            logger.warning(f"策略健康度检查失败（放行）: {str(e)[:60]}")
        return result

    def _prefilter(self, quotes_df: pd.DataFrame) -> list:
        """
        初筛过滤

        快速过滤掉明显不合格的股票，减少后续API调用

        过滤条件：
        - 非ST
        - 非涨停封死
        - 成交额 > 3000万
        - 非停牌（有价格）
        """
        candidates = []

        for _, row in quotes_df.iterrows():
            stock = {
                'code': str(row['code']).zfill(6),
                'name': row.get('name', ''),
                'price': row.get('price', 0),
                'pct_chg': row.get('pct_chg', 0),
                'amount': row.get('amount', 0),
                'turnover': row.get('turnover', 0),
                'volume_ratio': row.get('volume_ratio', 1),
                'pe': row.get('pe', None),
                'pb': row.get('pb', None),
                'name_raw': row.get('name', ''),
            }

            # 历史因子快照透传（回测关键路径）
            # backtest_engine._load_historical_snapshots() 已把 factor_daily.db 里的
            # capital_flow / north_flow / hot_stocks 历史值挂到当日快照行上。
            # 此前这里只取固定 10 个字段，快照因子被静默丢弃 → 回测中 capital_flow
            # （权重 0.35，最大因子）恒为 None、被中性化，回测实际只验证了 K 线因子。
            # 2026-09-05 修复：如实透传，让最大因子真正进入回测。
            #
            # 注意：pandas 缺失值在这里表现为 NaN 而非 None。必须显式转成 None，
            # 否则下游 `x is None` 判定全部失效 —— NaN 会被当成"有数据"走进
            # 数值计算分支，产出 NaN 因子分并污染总分（静默且难排查）。
            _cf = row.get('main_fund_accumulated')
            _nf = row.get('north_flow_accumulated')
            _hot = row.get('is_hot_stock')
            stock['main_fund_accumulated'] = float(_cf) if pd.notna(_cf) else None
            stock['north_flow_accumulated'] = float(_nf) if pd.notna(_nf) else None
            stock['is_hot_stock'] = bool(_hot) if pd.notna(_hot) else False

            # P4（2026-09-18）：尾盘急拉过滤——收盘位置过高的票剔除。
            # close_pos = (price - low) / (high - low)，1.0 = 收在全天最高。
            # 实盘 quotes 与回测快照均提供 high/low；缺失（NaN/最高=最低）→ 不过滤。
            if self.max_close_pos:
                _hi = row.get('high')
                _lo = row.get('low')
                if pd.notna(_hi) and pd.notna(_lo):
                    _hi, _lo = float(_hi), float(_lo)
                    if _hi > _lo > 0 and float(row.get('price', 0) or 0) > 0:
                        close_pos = (float(row['price']) - _lo) / (_hi - _lo)
                        stock['close_pos'] = round(close_pos, 4)
                        if close_pos > float(self.max_close_pos):
                            continue  # 尾盘收在高位，次日回落风险高（F04 诱多形态）

            # 风险过滤
            risk_result = self.risk_filter.check_stock(stock, data_engine=self.data_engine)
            stock['risk_check'] = risk_result

            if risk_result['passed'] or risk_result['score_penalty'] < 0.8:
                # 设置风险评分（0-100，越高越安全）
                stock['risk_score'] = 100.0 * (1.0 - risk_result.get('score_penalty', 0))
                candidates.append(stock)

        return candidates

    def _assess_market(self, quotes_df: pd.DataFrame, hot_df: pd.DataFrame,
                       is_backtest: bool = False) -> Dict:
        """
        市场环境评估 — 判断今日是否适合短线操作

        从全市场行情数据计算5个维度指标，综合评分。
        0额外API成本（全从已有数据计算）。

        返回：{
            total: 综合评分(0-100),
            level: 强市/中性市/弱市/极差市,
            skip: 是否跳过推荐,
            details: {各分项得分},
            summary: 文字描述,
        }
        """
        import numpy as np

        pct = quotes_df['pct_chg'].dropna()
        advancing = int((pct > 0).sum())
        declining = int((pct < 0).sum())
        flat = int((pct == 0).sum())
        total = advancing + declining + flat
        ad_ratio = advancing / max(declining, 1)

        # 修复（2026-09-05 审查 P2-4）：涨停/跌停按板块取涨跌幅上限。
        # 旧代码统一用 ±9.5%，20cm 板（创业板/科创板）涨停 19.9% 不被计入，
        # 北交所 30cm 同理 —— 结构性行情（20cm 主导日）市场情绪被系统性
        # 低估，skip 判定与仓位档位都会失真。板块划分与 risk_filter 一致。
        # 阈值取各板上限的 98%（10%→9.5 / 20%→19.6 / 30%→29.4），
        # 容忍行情源对涨跌幅的四舍五入。
        codes_b = quotes_df['code'].astype(str).str.zfill(6)
        pct_all = quotes_df['pct_chg']
        is_20cm = codes_b.str.startswith(('300', '301', '688', '689'))
        is_30cm = codes_b.str.startswith(('4', '8', '92'))
        lim_10 = pct_all.where(~is_20cm & ~is_30cm).dropna()
        lim_20 = pct_all.where(is_20cm).dropna()
        lim_30 = pct_all.where(is_30cm).dropna()
        limit_ups = int((lim_10 >= 9.5).sum() + (lim_20 >= 19.6).sum()
                        + (lim_30 >= 29.4).sum())
        limit_downs = int((lim_10 <= -9.5).sum() + (lim_20 <= -19.6).sum()
                          + (lim_30 <= -29.4).sum())
        ud_ratio = limit_ups / max(limit_downs, 1)

        median_chg = float(pct.median())
        # P3（2026-09-18）：全市场日均涨幅（拥挤度断路器用，与回测分析口径一致）
        avg_chg = float(pct.mean())
        pct_up_3 = int((pct >= 3).sum())
        hot_count = len(hot_df) if hot_df is not None and not hot_df.empty else 0

        # 北向资金（回测模式下跳过实时API）
        north_total = 0
        if not is_backtest:
            try:
                north = self.data_engine.get_north_flow_summary()
                north_total = north['total'] if north else 0
            except Exception as e:
                # 修复（2026-09-05 审查）：原为静默 pass。北向取 0 会把市场评估往
                # 弱市方向拉偏，必须留下日志才能发现实盘/回测评估环境被侵蚀。
                logger.warning(f"北向资金获取失败（市场评估按 0 处理）: {str(e)[:60]}")

        # 各维度评分（0-100）
        def scale(value, thresholds):
            """thresholds: [(下限, 上限, 100分时值), ...] 线性内插"""
            for lo, hi, score_at_hi in thresholds:
                if lo <= value < hi:
                    return score_at_hi
            return 50  # 默认中性

        # 涨跌比评分 (35分权重, 2026-09-06 北向降权后承接释放权重): >2=满分, 1-2=线性, <0.5=0分
        ad_score = scale(ad_ratio, [
            (0, 0.3, 0), (0.3, 0.5, 10), (0.5, 0.8, 30),
            (0.8, 1.0, 50), (1.0, 1.5, 70), (1.5, 2.0, 85),
            (2.0, 999, 100),
        ])

        # 涨停跌停比评分 (25分权重): >5=满分, <1=0分
        # T-C（2026-09-07 P1 论证后实施）：升级为"打板情绪"复合分——
        # 封板率×0.4 + 连板晋级率×0.3 + 昨日涨停溢价×0.3（akshare 东财
        # 涨停三池，免费、市场级、3 次调用）。数据不可用/回测模式回退
        # 旧涨停跌停比口径（行为与历史一致）。
        ud_score = scale(ud_ratio, [
            (0, 0.5, 0), (0.5, 1.0, 15), (1.0, 2.0, 40),
            (2.0, 3.0, 60), (3.0, 5.0, 80), (5.0, 999, 100),
        ])
        ud_source = 'ud_ratio'
        if not is_backtest:
            try:
                emo = self.data_engine.get_limit_up_emotion()
                if emo and emo.get('available'):
                    seal = float(emo['seal_rate'])                                    # 0-100
                    promo = max(0.0, min(float(emo['promotion_rate']), 100.0))        # 0-100
                    premium = max(0.0, min((float(emo['prev_premium']) + 5.0) / 10.0 * 100.0, 100.0))
                    emo_score = seal * 0.4 + promo * 0.3 + premium * 0.3
                    if emo_score >= 0:
                        ud_score = round(emo_score, 1)
                        ud_source = 'zt_emotion'
                        logger.info(f"打板情绪复合分: 封板率{seal:.0f} 晋级率{promo:.0f} "
                                    f"昨日溢价{emo['prev_premium']:+.2f}% → {ud_score}")
            except Exception as e:
                logger.warning(f"打板情绪获取失败（回退涨停跌停比）: {str(e)[:50]}")

        # 中位数涨幅评分 (20分权重): >1%=满分, <-1%=0分
        median_score = scale(median_chg, [
            (-10, -2, 0), (-2, -1, 15), (-1, -0.5, 35),
            (-0.5, 0, 50), (0, 0.5, 70), (0.5, 1.0, 85),
            (1.0, 10, 100),
        ])

        # 强势股数量评分 (15分权重): >100=满分, <20=0分
        hot_score = scale(hot_count, [
            (0, 10, 0), (10, 20, 15), (20, 30, 30),
            (30, 50, 50), (50, 80, 70), (80, 100, 85),
            (100, 9999, 100),
        ])

        # 北向资金评分 (5分权重, 2026-09-06 降权): 该口径为东财组织估算
        # （2024-08 起官方停止实时披露），属低频参考值，不再允许其极端单日
        # 读数对总分造成两极化影响：
        #   权重 0.10 → 0.05（释放的 0.05 归还给市场宽度 ad_score）；
        #   极端流出下限 0 分 → 20 分（温和化）。
        north_score = scale(north_total, [
            (-999, -80, 20), (-80, -40, 35), (-40, -10, 50),
            (-10, 10, 60), (10, 40, 80), (40, 999, 100),
        ])

        # 权重和 = 0.35 + 0.25 + 0.20 + 0.15 + 0.05 = 1.00
        # ad_score 涨跌宽度为双源验证可靠指标，承接北向释放的权重
        total_score = (
            ad_score * 0.35 + ud_score * 0.25 + median_score * 0.20
            + hot_score * 0.15 + north_score * 0.05
        )

        if total_score < 40:
            level = LEVEL_VERY_WEAK
            skip = True
        elif total_score < 55:
            level = LEVEL_WEAK
            skip = False
        elif total_score < 70:
            level = LEVEL_NEUTRAL
            skip = False
        else:
            level = LEVEL_STRONG
            skip = False

        logger.info(f"  市场环境: 涨跌比{ad_ratio:.2f}({advancing}/{declining}) "
                     f"涨停{limit_ups}跌停{limit_downs} "
                     f"中位数涨幅{median_chg:+.2f}% "
                     f"强势股{hot_count}只 北向{north_total:+.0f}亿")

        return {
            'total': round(total_score, 1),
            'level': level,
            'skip': skip,
            'summary': (
                f"涨跌比{ad_ratio:.2f}（沪深口径），涨停{limit_ups}跌停{limit_downs}，"
                f"中位数涨幅{median_chg:+.2f}%，强势股{hot_count}只"
            ),
            'details': {
                'ad_ratio': round(ad_ratio, 2),
                'advancing': advancing, 'declining': declining,
                'breadth_caliber': 'shsz_only',  # 涨跌家数为沪深口径，不含北交所
                'limit_ups': limit_ups, 'limit_downs': limit_downs,
                'ud_source': ud_source,
                'median_chg': median_chg,
                'avg_chg': round(avg_chg, 3),
                'hot_count': hot_count,
                'north_total': north_total,
                'north_caliber': 'eastmoney_estimate',  # 东财估算口径，参考值
                'ad_score': round(ad_score, 1),
                'ud_score': round(ud_score, 1),
                'median_score': round(median_score, 1),
                'hot_score': round(hot_score, 1),
                'north_score': round(north_score, 1),
            },
        }

    def _enrich_data(self, candidates: list, hot_codes: set = None,
                     is_backtest: bool = False, backtest_date: str = None) -> list:
        """
        获取详细数据 — 多维度初筛后取前 200 只拉取完整数据

        初筛评分（利用已有数据，不额外请求）：
          - 流动性 30分：成交额排名百分位
          - 活跃度 20分：换手率排名百分位
          - 短期动量 15分：当日涨幅（正合理，过高扣分）
          - 风险 20分：risk_score
          - 估值 15分：PE合理区间得分
          - 合计 100分

        取前 200 只进入详评阶段
        """
        # 1. 先给所有候选股算初步评分
        scored = []
        # 收集排名数据
        amounts = [s.get('amount', 0) or 0 for s in candidates]
        turnovers = [s.get('turnover', 0) or 0 for s in candidates]
        pct_chgs = [s.get('pct_chg', 0) or 0 for s in candidates]

        import numpy as np
        # 改进：用numpy的percentile替代O(n²)的循环
        amount_arr = np.array(amounts)
        turnover_arr = np.array(turnovers)

        # T7（2026-09-17）性能优化：pct_rank_arr 原每次调用内部 np.sort(arr)，
        # 逐股调用 → O(m²·log m)。改为在循环外各排序一次，传入已排序数组，
        # 整体降为 O(m·log m)（searchsorted 在有序数组上 O(log m)）。
        amount_sorted = np.sort(amount_arr)
        turnover_sorted = np.sort(turnover_arr)

        def pct_rank_arr(sorted_arr, val):
            if len(sorted_arr) == 0:
                return 50
            return np.searchsorted(sorted_arr, val) / len(sorted_arr) * 100

        for stock in candidates:
            amount = stock.get('amount', 0) or 0
            turnover = stock.get('turnover', 0) or 0
            pct_chg = stock.get('pct_chg', 0) or 0
            risk_score = stock.get('risk_score', 50)
            pe = stock.get('pe')

            # 流动性评分：成交额越高分越高
            amount_score = pct_rank_arr(amount_sorted, amount) * 0.30

            # 活跃度评分：换手率1%-10%最佳，过低冷清，过高异常
            if 1 <= turnover <= 10:
                turnover_score = pct_rank_arr(turnover_sorted, turnover) * 0.20
            elif 0.5 <= turnover < 1:
                turnover_score = 30 * 0.20
            elif turnover > 10:
                turnover_score = 20 * 0.20
            else:
                turnover_score = 5 * 0.20

            # 短期动量：涨跌幅在1%~5%最佳，过大回调风险高，过小无动量
            if 1 <= pct_chg <= 5:
                momentum_score = 85 * 0.15
            elif -1 < pct_chg < 1:
                momentum_score = 60 * 0.15
            elif 5 < pct_chg <= 9:
                momentum_score = 50 * 0.15
            elif -5 < pct_chg <= -1:
                momentum_score = 30 * 0.15
            else:
                momentum_score = 15 * 0.15

            # 风险评分
            risk_score_component = risk_score * 0.20

            # 估值评分：PE在10-30合理区间得分高
            if pe and pe > 0:
                if 10 <= pe <= 30:
                    pe_score = 85 * 0.15
                elif 5 <= pe < 10 or 30 < pe <= 50:
                    pe_score = 65 * 0.15
                elif 50 < pe <= 100:
                    pe_score = 40 * 0.15
                else:
                    pe_score = 20 * 0.15
            else:
                pe_score = 30 * 0.15  # PE负或缺失

            preliminary_score = amount_score + turnover_score + momentum_score + risk_score_component + pe_score

            stock['preliminary_score'] = round(preliminary_score, 2)
            scored.append(stock)

        # 按初步评分排序取前 200
        scored.sort(key=lambda x: x.get('preliminary_score', 0), reverse=True)
        top_candidates = scored[:200]
        logger.info(f"初步评分排序，前10只: {[(s['code'], s.get('name',''), s['preliminary_score']) for s in top_candidates[:10]]}")

        enriched = []
        all_raw_returns = {}
        total = len(top_candidates)
        logger.info(f"详评 {total} 只（初步评分前200）")

        # 预加载大单缓存 — 详评开始前预热全市场大单数据（实测 ~26s，akshare 100页），
        # 之后 200 只候选股的 capital_flow 全部命中缓存，避免第一批股票
        # fallback 拿不到数据被中性化。失败不影响流程（个股走同花顺降级）。
        if not is_backtest:
            try:
                self.data_engine.preload_big_deal()
            except Exception as e:
                logger.warning(f"big_deal 预加载异常: {str(e)[:80]}")

        # 先集中获取所有资金流（回测模式下跳过实时API，改用历史快照）
        # 2026-09-05 修复：回测分支此前无条件把 main_fund_accumulated 置 None，
        # 导致 capital_flow（权重 0.35）在回测中 100% 中性化。现在改为沿用
        # _prefilter 从快照透传进来的历史值（factor_daily.db），缺失才置 None。
        _bt_cf_hit = 0
        _bt_hot_hit = 0
        for idx, stock in enumerate(top_candidates):
            code = stock['code']
            if is_backtest:
                # 保留 _prefilter 透传的快照值；无快照时本来就是 None（走中性化路径）
                # （2026-09-06 审查清理：删除两处对 None 的无意义自赋值，保留计数逻辑）
                if stock.get('main_fund_accumulated') is not None:
                    _bt_cf_hit += 1
                if stock.get('is_hot_stock'):
                    _bt_hot_hit += 1
                stock['tail_end_stats'] = {'available': False}
            else:
                try:
                    # AShareHub moneyflow 配额保护：
                    # 仅 top 10 候选股调 AShareHub（消耗 10 次配额）
                    # 其余直接走 big_deal + 同花顺 fallback（0 配额消耗）
                    # 资金流是短线最高权重因子(0.35)，但 big_deal 缓存已能覆盖主力资金
                    # 所以 ASHareHub moneyflow 只给可能进入推荐池的前 10 只用精确数据
                    # 预算：moneyflow 10 + 技术 10 + 概念 20 + 财务 15 = 55/次，留 45 余量
                    skip_ash = idx >= 10
                    main_accum = self.data_engine.get_main_fund_accumulated(code, days=10, skip_asharehub=skip_ash)
                except Exception:
                    main_accum = None
                stock['main_fund_accumulated'] = main_accum

                # 北向个股持股数据于 2024-08-16 起停公开（港交所不再披露）
                # 不再调用 get_north_flow_accumulated 以节省 AShareHub 配额
                stock['north_flow_accumulated'] = None

                # 尾盘成交结构（从已缓存的大单数据提取，不走额外API）
                try:
                    tail_end = self.data_engine.get_tail_end_stats(code)
                    stock['tail_end_stats'] = tail_end
                except Exception:
                    stock['tail_end_stats'] = {'available': False}

        if is_backtest:
            n_top = len(top_candidates)
            logger.info(
                f"回测因子快照命中: 资金流 {_bt_cf_hit}/{n_top} "
                f"({_bt_cf_hit/max(n_top,1)*100:.0f}%), 热点 {_bt_hot_hit}/{n_top}"
            )
            if _bt_cf_hit == 0:
                logger.warning(
                    "当日无资金流历史快照 → capital_flow 将被中性化，"
                    "回测成绩不代表含资金流的完整模型"
                )

        # 通达信估值/基本面 + 事件催化预加载（2026-09-05 新增）
        # 回测模式必须传 backtest_date 作为事件衰减基准日，否则历史事件用
        # datetime.now() 计算 age 会全错（回测变成前视泄漏）
        self.scoring_model.factor_lib.attach_tdx_signals(
            top_candidates, as_of=backtest_date if is_backtest else None
        )

        # K线 + 技术指标（并行拉取，每只独立线程）
        # 缓存命中的毫秒级返回，未命中的走baostock
        from threading import Lock
        enrich_lock = Lock()

        def fetch_kline(stock, rank_index):
            code = stock['code']
            stock['_rank_index'] = rank_index  # 记录初步评分排名，供板块预加载按 top N 补
            try:
                # 回测模式：只取回测日期之前的K线，防止前瞻偏差
                if is_backtest and backtest_date:
                    from datetime import datetime, timedelta
                    bt_end = datetime.strptime(backtest_date, '%Y-%m-%d').strftime('%Y-%m-%d')
                    bt_start = (datetime.strptime(backtest_date, '%Y-%m-%d') - timedelta(days=120)).strftime('%Y-%m-%d')
                    kline = self.data_engine.get_kline(code, start_date=bt_start, end_date=bt_end)
                else:
                    kline = self.data_engine.get_kline(code)
                if kline is not None and not kline.empty and len(kline) >= 20:
                    close = kline['close']
                    high = kline.get('high', close)
                    low = kline.get('low', close)
                    macd = self.scoring_model.factor_lib.calc_macd_status(close)
                    stock['macd_status'] = macd
                    stock['kline_df'] = kline
                    raw_return_20 = (close.iloc[-1] / close.iloc[-20] - 1) * 100
                    with enrich_lock:
                        all_raw_returns[code] = raw_return_20
                    stock['raw_return_20'] = raw_return_20
                else:
                    stock['macd_status'] = {'score': 50, 'status': 'unknown'}
                    stock['raw_return_20'] = 0

                # AShareHub 分级配额分配（日限100次，按优先级分四档）
                # top 10:  资金流 moneyflow（10次）— 短线最高权重(0.35)，但 big_deal 兜底
                # top 10:  技术因子双源校验（10次）
                # top 15:  财务指标（15次）
                # top 20:  概念板块（20次）
                # 合计 55 次（10+10+20+15），预算闸门 90/天（data_engine 配额管理）
                # 2026-08-07 优化：moneyflow top 15→10（预算 60→55），
                # 配额账本已持久化跨进程共享 + 线程锁 + 原子写（见 data_engine）

                # 双源技术校验：AShareHub 技术因子（仅 top 10，独立熔断）
                if rank_index < 10:
                    try:
                        asharehub_tech = self.data_engine.get_technical_factors_asharehub(code)
                        if asharehub_tech is not None:
                            stock['asharehub_tech'] = asharehub_tech
                    except Exception as e:
                        logger.warning(f"{code} AShareHub技术因子失败: {e}")

                # AShareHub 概念板块（top 20，hot_theme 增强，独立熔断）
                if rank_index < 20:
                    try:
                        concepts = self.data_engine.get_concept_members(code)
                        if concepts is not None:
                            stock['concept_names'] = concepts
                    except Exception as e:
                        logger.warning(f"{code} AShareHub概念板块失败: {e}")

                # AShareHub 财务指标（top 15，长线基本面，独立熔断）
                if rank_index < 15:
                    try:
                        fin = self.data_engine.get_financial_indicators(code)
                        if fin is not None:
                            stock['financial_indicators'] = fin
                    except Exception as e:
                        logger.warning(f"{code} AShareHub财务指标失败: {e}")
            except Exception as e:
                logger.warning(f"{code} 技术面失败: {str(e)[:60]}")
                stock['macd_status'] = {'score': 50, 'status': 'unknown'}
                stock['raw_return_20'] = 0
            return stock

        # baostock 是全局单例，只能用1个线程。但大部分已缓存，串行走就行。
        # 用 max_workers=3 让少量未命中并行，大部分已命中毫秒返回
        with ThreadPoolExecutor(max_workers=3) as executor:
            futures = {executor.submit(fetch_kline, s, i): s for i, s in enumerate(top_candidates)}
            for future in as_completed(futures):
                try:
                    enriched.append(future.result(timeout=30))
                except Exception:
                    # 超时或异常：跳过该股票
                    stock = futures[future]
                    stock['macd_status'] = {'score': 50, 'status': 'unknown'}
                    stock['raw_return_20'] = 0
                    enriched.append(stock)

        # 横截面RPS计算
        # 修复（2026-09-05 审查 P3-3）：O(N²) 双重循环 → dict 一次映射 O(N)
        if all_raw_returns:
            rps_map = dict(zip(
                all_raw_returns.keys(),
                pd.Series(list(all_raw_returns.values())).rank(pct=True) * 100))
            for s in enriched:
                v = rps_map.get(s['code'])
                if v is not None:
                    s['rps_20'] = float(v)

        # P0-B 补充（2026-09-05 审查报告）：pe_percentile/pb_percentile 此前从未被
        # 任何环节计算（ensemble 映射里 .get() 恒 None → 专家估值维 15% 恒中性 50）。
        # 从 tdx fundamentals 的 pe/pb 做候选池横截面百分位（0-100，越高越贵），
        # 与 rps_20 同口径；缺 fundamentals 或 pe<=0 的票保持 None（估值维走中性，不打假分）。
        for key, field in (('pe_percentile', 'pe'), ('pb_percentile', 'pb')):
            valid = {}
            for s in enriched:
                v = (s.get('fundamentals') or {}).get(field)
                if v is not None and v > 0:
                    valid[s['code']] = float(v)
            if not valid:
                continue
            ranks = pd.Series(list(valid.values())).rank(pct=True) * 100
            rank_map = dict(zip(valid.keys(), ranks))
            for s in enriched:
                if s['code'] in rank_map:
                    s[key] = float(rank_map[s['code']])

        # 补充新数据源信号（不额外请求 API，只打标签）
        # 板块归属：评分前对初步评分 top N（30 只）补真实板块（东财 em_get 限流，
        # 只补可能进推荐池的候选，避免 200 只全补阻塞流程），让 hot_theme 的
        # 板块涨幅加权+龙头加成真正参与排序（§33 修复）。回测模式无实时板块数据，
        # 保持空 blocks 不改变回测路径。评分后推荐股会基于全字段源 dict 整体重算
        # （见 run_short_term 的补算环节，score_stock 重算 weighted/总分）。
        for s in enriched:
            # 修复（2026-09-05 审查 P3-2）：`hot_codes and ...` 在 hot_codes 为
            # 空集时返回 []（list），类型不纯。显式布尔化。
            s['is_hot_stock'] = bool(hot_codes) and s['code'] in hot_codes
            s['blocks'] = {"total": 0, "boards": [], "concept_tags": []}
            s['dragon_tiger'] = {"records": [], "seats": {"buy": [], "sell": []}, "institution": {}}

        # 动量硬过滤（2026-09-05 评估·胜率对齐）：非热点且动量处于候选池前 20%
        # （rps_20>=80）的票实测胜率仅 40.3%、日均 -1.09%（890 只×48 日面板，
        # n=7370 大样本）。热点票豁免——热点·Q5 实测胜率 55.9%/+1.44%，
        # 是核心 alpha 不可误伤。config: short_term.buy.momentum_filter_rps_max，
        # 置 0 关闭。注意 rps_20 是候选池内百分位（约 200 只），比全市场
        # 分位更严，方向一致。
        n_before = len(enriched)
        enriched = self._apply_momentum_filter(enriched, self.momentum_filter_rps_max)
        # T5（2026-09-17）：记录动量硬过滤的 before/after/removed，供 run_context 持久化
        self._last_momentum_filter_info = {
            'before': n_before, 'after': len(enriched), 'removed': n_before - len(enriched)}
        if len(enriched) < n_before:
            logger.info(f"动量硬过滤: {n_before} -> {len(enriched)} "
                        f"(剔除非热点 rps_20>={self.momentum_filter_rps_max})")

        if not is_backtest:
            # 按初步评分排名取 top 30 补板块归属（东财 slist，每只 ~0.5s 限流）
            top_ranked = sorted(
                (s for s in enriched if s.get('_rank_index') is not None),
                key=lambda s: s['_rank_index']
            )[:30]
            for s in top_ranked:
                code = s['code']
                for attempt in (1, 2):
                    try:
                        # force_live：板块涨幅是日频数据，评分必须当日实时，
                        # 不能用周末预取缓存的 stale change_pct
                        blocks = self.data_engine.get_stock_blocks(code, force_live=True)
                        if blocks and blocks.get('total', 0) > 0:
                            s['blocks'] = blocks
                            break
                    except Exception as e:
                        if attempt == 1:
                            time.sleep(1.0)
                            logger.warning(f"{code} 评分前板块预加载失败(重试): {str(e)[:80]}")
                        else:
                            logger.warning(f"{code} 评分前板块预加载失败×2: {str(e)[:80]}")
            loaded = sum(1 for s in top_ranked if s['blocks'].get('total', 0) > 0)
            logger.info(f"评分前板块预加载完成: {loaded}/{len(top_ranked)} 只有真实板块")
            if loaded < len(top_ranked) * 0.8:
                logger.warning(f"板块预加载成功率过低 {loaded}/{len(top_ranked)}，hot_theme 板块涨幅加权可能部分失效")

        logger.info(f"详评完成: {len(enriched)} 只")
        return enriched

    def get_required_fields(self) -> list:
        return [
            'code', 'name', 'price', 'pct_chg', 'amount',
            'turnover', 'volume_ratio', 'pe', 'pb', 'name_raw'
        ]

    def describe(self) -> str:
        # P0-A 修复（2026-09-05 审查报告）：权重说明此前硬编码为旧研报中位数权重
        # （25/25/15/10...），与实际生效权重（v1.json 校准值）严重背离。
        # 改为从 ScoringModel 实际加载的权重动态生成，杜绝展示与生效脱节。
        try:
            w = self.scoring_model.get_weights('short')
            parts = [f"{name}({pct*100:.0f}%)" for name, pct in
                     sorted(w.items(), key=lambda kv: -kv[1]) if pct and pct > 0]
            weight_desc = '+'.join(parts) if parts else '见 data/weights/v1.json'
        except Exception:
            weight_desc = '见 data/weights/v1.json'
        return (f"短线尾盘策略: T+0尾盘选股 → T+1开盘卖出。"
                f"实际生效因子权重: {weight_desc}。"
                f"含市场环境评估，极差市自动跳过。")
