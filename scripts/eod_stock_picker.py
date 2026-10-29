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
import math
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

    # no_data 低频重试（2026-09-12）：满 7 天的终态行复活进池探测一次。
    # 仍无 K 线 → bump（已有 30 底数，一次即 >=30）+ mark_no_data 回终态
    # （updated_at 刷新，下轮复活又是 7 天后）；有 K 线 → update_outcomes
    # INSERT OR REPLACE 整行重写，status 归 NULL 自愈离开终态。
    # no_data 行因 status!='no_data' 不在 pending 池，复活后进入本轮回填。
    try:
        revived = tracker.revive_stale_no_data(stale_days=7)
        if revived:
            logger.info(f"no_data 复活 {len(revived)} 条进入本轮探测")
    except Exception as e:
        logger.warning(f"no_data 复活步骤失败(忽略): {e}")

    pending = tracker.get_pending_outcomes()

    if not pending:
        logger.info("没有待处理的推荐结果")
        return (0, 0)   # 2026-09-22：返回 (ok, fail) 供调用方判定（P2-27）

    logger.info(f"自动回填 {len(pending)} 条推荐结果...")
    # 终态标记（2026-09-07 论证后实施）：attempts 计数随回填递增（内存态，
    # 按调用轮次计），同一票连续 30 轮回填仍无 K 线 → 标记 no_data 退出
    # pending 池。停牌票复牌后自然恢复回填；退市票不再消耗每夜网络扫描。
    # （2026-09-12 清理：attempts={} 内存字典是落库计数改造后的死变量，删）
    # 2026-09-22 修复（缺陷排查 P2-27）：统计成功/失败并返回 —— 原实现把每条
    # 失败都吞成 warning、函数恒返回 None → 凌晨脚本无法判定回填是否真的发生。
    _ok = 0
    _fail = 0
    for pred in pending:
        code = pred['code']
        pred_date = pred['date']
        try:
            # 回填走前复权口径（adjust='qfq'）：除权日价差不失真，
            # 避免 mootdx raw 价把送转/分红当暴跌。qfq 绕开 raw 缓存直接 baostock。
            kline = data_engine.get_kline(code, start_date=pred_date, adjust='qfq')
            if kline is not None and not kline.empty:
                tracker.update_outcomes(pred['id'], kline,
                                         pred_date=pred['date'])
                _ok += 1
            else:
                # 计数持久化（2026-09-07 审计修复）：原内存态计数在独立
                # 进程每次从 0 开始，终态永不触发；改为落库累计
                n = tracker.bump_backfill_attempts(pred['id'])
                if n >= 30:
                    tracker.mark_no_data(pred['id'])
                    logger.info(f"{code}@{pred_date} 连续 {n} 次回填无 K 线 → no_data 终态")
                else:
                    _fail += 1
        except Exception as e:
            _fail += 1
            logger.warning(f"回填失败 {code}: {e}")
    return (_ok, _fail)


def _shadow_rows_from(recommendations: list) -> list:
    """提取影子落库行（2026-09-18 影子推荐；2026-09-20 审查 P1-1 修键名）。

    三种情况：
      - 影子模式（极差市/拥挤度断路器触发但仍完成评分）：run() 返回带 shadow 标记的
        推荐 → 全部取出（有 code 的）；
      - 零达标（no_qualified）：返回元信息条目，取其中的最高分候选（含 code/name/score；
        该路径无价格，buy_price 记 0 —— update_outcomes 会从 K 线重算买入价，安全）。
        键名契约：策略端（strategies/short_term.py）零达标分支写入的是
        **'top_candidate'**（2026-09-20 审查 P1-1：原读 'top_unqualified' 恒取空，
        零达标影子台账从未落过库）。保留 'top_unqualified' 回退兼容旧快照/旧调用方；
      - 正常推荐日 / 非交易日 / 其他：返回空列表（不写 shadow）。
    """
    if not recommendations:
        return []
    first = recommendations[0] or {}
    if first.get('shadow'):
        return [r for r in recommendations if r.get('code')]
    if first.get('no_qualified'):
        # 键名契约：生产写 top_candidate（backtest_report 渲染亦读此键）；
        # top_unqualified 仅作历史兼容回退。
        top = first.get('top_candidate') or first.get('top_unqualified') or {}
        if top.get('code'):
            return [{'code': top.get('code'), 'name': top.get('name', ''),
                     'score': top.get('score', 0), 'rating': '', 'price': 0,
                     'breakdown': {}}]
    return []


def run_short_term(config: dict, allow_non_trading_day: bool = False) -> list:
    """运行短线尾盘策略"""
    short_cfg = config.get('short_term', {})
    strategy = ShortTermStrategy(short_cfg)
    # 2026-09-23（交接单 A2）：声明配额归属，使额度消耗可在账本/简报中归因
    try:
        strategy.data_engine.set_quota_owner('eod:short')
    except Exception:
        pass
    recommendations = strategy.run()
    from datetime import date
    today = date.today().isoformat()

    # 非交易日守卫（2026-09-17 修复：改为默认开启）。
    # 原默认关闭（--skip-non-trading-day），手工直跑会污染 predictions 库
    # （06-13 周六 / 06-19 端午 / 07-04 周六 三条历史污染记录即此成因）。
    # 现默认跳过非交易日写入，仅显式 --allow-non-trading-day 才允许落库。
    guard_skip = False
    if not allow_non_trading_day:
        try:
            from core.trading_calendar import is_trading_day
            _td = is_trading_day(today)
            if _td is False:
                logger.warning(f"{today} 为非交易日（非交易日守卫默认开启）→ "
                               f"报告照常生成，但不写入 predictions/factor 采集")
                guard_skip = True
            elif _td is None:
                logger.warning("交易日历不可用 → 无法执行非交易日守卫，按交易日继续")
        except Exception as e:
            logger.warning(f"非交易日守卫执行失败（按交易日继续）: {e}")

    # 数据链路修复（2026-09-07 回访审查#1/#7）：因子采集（热点/龙虎榜为
    # 市场级数据，与是否 skip/防重无关）提前到所有 return 之前——
    # 原逻辑 skip/防重分支直接 return，导致当日热点与龙虎榜数据断采，
    # 因子库出现日期空洞。enriched 为空时 collect 只写市场级部分（安全）。
    # 非交易日守卫跳过时整段跳过（与"不写入"语义一致）。
    if not guard_skip:
        try:
            enriched = getattr(strategy, '_last_enriched', None) or []
            collector = FactorDataCollector()
            collector.collect(
                data_engine=strategy.data_engine,
                enriched_stocks=enriched,
                hot_df=strategy.data_engine.get_ths_hot_stocks(),
                recommendations=recommendations,
                trade_date=today,
            )
        except Exception as e:
            logger.warning(f"因子数据采集失败(不影响选股): {e}")

        # T6（2026-09-17）：因子采集已消费 _last_enriched，释放其持有的 ~200 个
        # DataFrame 引用以避免内存常驻。注意：必须在采集之后（eod 依赖 run() 返回后
        # 读取 _last_enriched），故清理放在此处而非 run() 内。
        try:
            strategy._last_enriched = None
        except Exception:
            pass

        # 漂移监控日度样本（2026-09-23 交接单 D1 结构性解法）：落当日**候选池 top-N**
        # （含零达标日），并在下次运行/本步顺带补算历史缺样本日的 T+1。
        # 零 ASHareHub 配额、幂等、失败不影响主流程。解读口径见
        # feedback/drift_sample.py 模块 docstring。
        try:
            from feedback.drift_sample import (record_candidate_pool,
                                              backfill_daily_samples)
            _pool = (getattr(strategy, '_last_diagnostics', None) or {}).get('top_pool')
            if _pool:
                _n = record_candidate_pool(today, _pool)
                logger.info(f"漂移样本：候选池已落库 {_n} 只（{today}）")
            _nd = backfill_daily_samples()
            if _nd:
                logger.info(f"漂移样本：补算 {_nd} 个交易日的 T+1 样本")
        except Exception as e:
            logger.warning(f"漂移样本数据线失败(忽略): {str(e)[:80]}")
        finally:
            try:
                strategy._last_diagnostics = None
            except Exception:
                pass

    # 影子推荐落库（2026-09-18）：极端市况（极差市停推 / 动态门槛零达标）当日照常评分
    # 并以 mode='shadow' 落库，但**不下发、不进正式统计**（全工程 7 处统计读取硬滤
    # mode='short'；get_pending_outcomes 不滤 mode → 影子行会自动补 T+1/T+5/T+20 结果，
    # 这正是"攒冰点期直接证据"所需）。目的：破解样本删失。
    # 约束：整个块 try/except 包裹（失败仅日志）；遵守非交易日守卫；批次级防重。
    if not guard_skip and short_cfg.get('shadow_enabled', True):
        try:
            _shadow_rows = _shadow_rows_from(recommendations)
            if _shadow_rows:
                _st = PredictionTracker()
                if _st.has_predictions(today, 'shadow'):
                    logger.info(f"防重: {today} mode=shadow 已有影子记录，跳过写入")
                else:
                    for _r in _shadow_rows:
                        _st.log_prediction(
                            date=today,
                            code=_r.get('code', ''),
                            name=_r.get('name', ''),
                            mode='shadow',
                            score=_r.get('score', 0),
                            rating=_r.get('rating', ''),
                            buy_price=_r.get('price', 0),
                            model_version='v1',
                            factor_scores=_r.get('breakdown', {}) or {}
                        )
                    logger.info(f"影子推荐已落库 {len(_shadow_rows)} 条（mode=shadow，不下发、"
                                f"不进正式统计；原因：{recommendations[0].get('shadow_reason') or recommendations[0].get('skip_reason') or '零达标'}）")
        except Exception as _e:
            logger.warning(f"影子推荐落库失败（不影响主流程）: {_e}")

    # 影子模式（极差市/拥挤度断路器触发但完成评分）：只以 mode='shadow' 落库，
    # **不得写 mode='short'**（否则影子样本混入正式统计）。守卫必须是纯新增判断，
    # 正常推荐日 recommendations[0] 无 shadow 键 → 行为与历史完全一致。
    if recommendations and recommendations[0].get('shadow'):
        return recommendations

    # 元信息条目不落库（2026-09-14 扩展）：
    #   - skip_reason：市场赚钱效应极差，策略停推
    #   - no_qualified：全部候选被动态门槛过滤，零推荐（携带最高分标的供简报展示）
    # 二者都没有 code/score，写入 predictions 会产生空记录并污染胜率/漂移统计。
    if recommendations and (recommendations[0].get('skip_reason')
                            or recommendations[0].get('no_qualified')):
        return recommendations

    # 记录推荐
    tracker = PredictionTracker()

    # 非交易日守卫已在函数顶部统一判定（guard_skip），此处不再重复。

    # 批次级防重：当日已有推荐则跳过整批写入
    # （防止 study-a 等二次运行污染 predictions 表，保证每日只有 14:45 正式 cron 的推荐落库）
    if tracker.has_predictions(today, 'short'):
        logger.info(f"防重: {today} mode=short 已有推荐记录，跳过写入")
        return recommendations

    if guard_skip:
        return recommendations

    # 2026-09-22 修复（缺陷排查 P2-24/契约 A）：
    #   · `rec.get('code', '')` 的默认值只在键缺失时生效 —— 值为 None 时返回 None，
    #     落库成 NULL；而唯一索引 `(date,code,mode)` 对 NULL 不去重 → 可重复插入空行。
    #   · 逐条写入的条数若与入参不一致（中途异常/去重），原实现无任何校验，
    #     批次级防重又只看"当日是否有任意一行"→ 当日推荐集残缺且不可自愈。
    _written = 0
    for rec in recommendations:
        _code = str(rec.get('code') or '').strip()
        if not _code:
            logger.warning("推荐条目缺少 code，跳过落库（避免 NULL 行污染统计）")
            continue
        _ok = tracker.log_prediction(
            date=today,
            code=_code.zfill(6),
            name=rec.get('name', ''),
            mode='short',
            score=rec.get('score', 0),
            rating=rec.get('rating', ''),
            buy_price=rec.get('price', 0),
            model_version='v1',
            factor_scores=rec.get('breakdown', {})
        )
        if _ok is not None:
            _written += 1
    if _written != len(recommendations):
        logger.warning(
            f"落库条数 {_written} != 推荐条数 {len(recommendations)}"
            f"（差额为去重命中/缺 code 条目）→ 当日推荐集可能残缺，请人工核对")

    return recommendations


def run_long_term(config: dict) -> list:
    """运行长线策略"""
    long_cfg = config.get('long_term', {})
    strategy = LongTermStrategy(long_cfg)
    # 2026-09-23（交接单 A2）：同上
    try:
        strategy.data_engine.set_quota_owner('eod:long')
    except Exception:
        pass
    recommendations = strategy.run()

    tracker = PredictionTracker()
    from datetime import date
    today = date.today().isoformat()

    # 非交易日守卫（2026-09-20 审查 P3-1）：short 侧 2026-09-17 已修，long 侧漏改 —
    # 周末手工 --mode long 会把当日快照污染 predictions/factor 库。与 short 侧同口径：
    # 日历不可用（None）时按交易日继续并在日志"响一声"。
    try:
        from core.trading_calendar import is_trading_day
        _td = is_trading_day(today)
        if _td is False:
            logger.warning(f"{today} 为非交易日（long 侧非交易日守卫）→ "
                           f"不写入 predictions/因子采集")
            return recommendations
        elif _td is None:
            logger.warning("交易日历不可用 → long 侧守卫跳过，按交易日继续")
    except Exception as e:
        logger.warning(f"非交易日守卫执行失败（按交易日继续）: {e}")

    # 批次级防重：当日已有推荐则跳过整批写入
    if tracker.has_predictions(today, 'long'):
        logger.info(f"防重: {today} mode=long 已有推荐记录，跳过写入")
        return recommendations

    for rec in recommendations:
        if rec.get('skip_reason') or rec.get('market_assessment'):
            continue
        # 2026-09-22 修复（契约 A）：同 short 侧 —— `get('code','')` 对值为 None
        # 无效，且唯一索引对 NULL 不去重。
        _lcode = str(rec.get('code') or '').strip()
        if not _lcode:
            logger.warning("长线推荐条目缺少 code，跳过落库")
            continue
        tracker.log_prediction(
            date=today,
            code=_lcode.zfill(6),
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

    # T6（2026-09-17）：长线因子采集完成后释放 _last_enriched（见 run_short_term 注释）
    try:
        strategy._last_enriched = None
    except Exception:
        pass

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

    # 绩效趋势小节（2026-09-07 补齐）：分日明细弥补"快照数字看不出趋势"。
    # 这是手动复盘入口的轻量替代——正式的可视化看板推迟到阶段二
    # （样本 ≥300 且 daily_ics ≥60 日后），避免当前稀疏数据的空壳曲线。
    try:
        import sqlite3
        db = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                          'data', 'db', 'predictions.db')
        if os.path.exists(db):
            conn = sqlite3.connect(db)
            rows = conn.execute("""
                SELECT p.date, AVG(o.t1_return) AS day_ret,
                       SUM(CASE WHEN o.t1_return > 0 THEN 1 ELSE 0 END) AS wins,
                       COUNT(o.t1_return) AS n
                FROM predictions p JOIN outcomes o ON o.prediction_id = p.id
                WHERE p.mode='short' AND o.t1_return IS NOT NULL
                GROUP BY p.date ORDER BY p.date DESC LIMIT 20
            """).fetchall()
            conn.close()
            if rows:
                cum = 1.0
                for _, r, w, n in reversed(rows):
                    cum *= (1 + r / 100)
                streak = 0
                for _, r, w, n in rows:           # rows 按日期降序，数最近连亏
                    if r < 0:
                        streak += 1
                    else:
                        break
                print(f"\n📉 绩效趋势（最近 {len(rows)} 个有结果交易日，按日期倒序）")
                print(f"  {'日期':<12}{'均收益':>8}{'胜率':>8}{'票数':>5}")
                for d, r, w, n in rows:
                    arrow = '↑' if r > 0 else ('↓' if r < 0 else '—')
                    print(f"  {d:<12}{r:>+7.2f}%{arrow} {w}/{n:<4}")
                print(f"  合计: 20日复合收益 {(cum-1)*100:+.2f}% | "
                      f"胜率 {sum(w for _,r,w,n in rows)/max(sum(n for _,r,w,n in rows),1)*100:.0f}% | "
                      f"当前连亏 {streak} 天")
            else:
                print("\n📉 绩效趋势: 暂无已回填的 T+1 结果")
    except Exception as e:
        print(f"  绩效趋势生成失败: {e}")

    # P0-A 一致性：展示实际生效权重（v1.json > config），而非 config 历史草稿
    try:
        from core.scoring_model import ScoringModel
        effective_weights = ScoringModel().get_weights('short')
    except Exception:
        effective_weights = config.get('short_term', {}).get('weights', {})
    print(f"\n⚙️  模型版本: v1")
    print(f"  权重(生效): {effective_weights}")

    # P3-5（2026-09-20 审查）：只展示 short 正式推荐，避免未来 shadow 行混入展示
    recent = tracker.get_recent_predictions(limit=5, mode='short')
    if not recent.empty:
        print(f"\n📋 最近推荐:")
        for _, row in recent.iterrows():
            t1 = row.get('t1_return', None)
            # 2026-09-22 修复（缺陷排查 P2-24）：`recent` 是 DataFrame，SQL NULL
            # 读回来是 NaN —— `t1 is None` 为 False，`NaN > 0` 也为 False
            # → 未回填的记录被显示成 "❌ nan%"，人工复盘结论被反向误导。
            if t1 is None or not math.isfinite(float(t1)):
                mark, t1_disp = '⏳', '待回填'
            else:
                mark, t1_disp = ('✅' if t1 > 0 else '❌'), f"{t1}%"
            print(f"  {row['date']} {row['code']} {row['name']} "
                  f"评分{row['score']} T+1:{t1_disp} {mark}")
    else:
        print(f"\n📋 暂无推荐记录")


def main():
    parser = argparse.ArgumentParser(description='A股智能选股系统')
    parser.add_argument('--mode', choices=['short', 'long'], default='short',
                        help='策略模式: short(短线) / long(长线)')
    parser.add_argument('--out', type=str, help='输出文件路径')
    parser.add_argument('--allow-non-trading-day', action='store_true',
                        help='允许在非交易日写入 predictions/因子采集（默认跳过节假日，仅生成报告）')
    parser.add_argument('--skip-non-trading-day', action='store_true',
                        help='[已废弃] 非交易日跳过写入（2026-09-17 起已成为默认行为，无需显式传）')
    parser.add_argument('--status', action='store_true',
                        help='查看模型状态')
    parser.add_argument('--config', type=str, default='config.yml',
                        help='配置文件路径')

    args = parser.parse_args()

    config = load_config(args.config)

    if args.status:
        show_status(config)
        return

    # 1. 运行策略（先跑：14:45 cron 触发后尽快出推荐，避免回填拖延贴近收盘）
    if args.mode == 'short':
        recommendations = run_short_term(config,
                                         allow_non_trading_day=getattr(args, 'allow_non_trading_day', False))
    else:
        recommendations = run_long_term(config)

    # 1.5 绩效漂移监控（2026-09-05 审查报告 P3-Q）：事后视角，不阻塞选股。
    #     PSI 分布漂移检测：结果写 data/reports/drift_latest.json + 日志告警
    #     2026-09-23（交接单 D7/D8）：结论**追加进简报尾部**（原为只写文件+日志，
    #     简报里看不到）；与 run_context.regime.killswitch 并列展示 ——
    #     kill-switch 管尾部风险（累计收益→降仓动作），drift 管分布漂移（PSI→告警）。
    drift_result = None
    try:
        from core.drift_monitor import DriftMonitor
        drift_result = DriftMonitor().check()
        drift_path = DriftMonitor().save(drift_result)
        if drift_result.get('verdict') == 'ALERT':
            logger.warning(f"⚠️ 绩效漂移 ALERT: {drift_result.get('note')} "
                           f"(详情: {drift_path})")
        elif drift_result.get('verdict') == 'WATCH':
            logger.info(f"绩效漂移 WATCH: {drift_result.get('note')}")
        elif drift_result.get('verdict') == 'PENDING_BACKFILL':
            logger.info(f"绩效漂移: 待回填 — {drift_result.get('note', '')[:80]}")
        elif drift_result.get('verdict') in ('NO_SAMPLE', 'INSUFFICIENT'):
            # D6：把"监控已冻结/结构性无样本"提到 warning —— 这两个状态说明
            # 监控在最需要它的行情里失效，不能再按普通 info 淹没
            logger.warning(f"绩效漂移监控未就绪（{drift_result.get('verdict')}）: "
                           f"{drift_result.get('note', '')[:120]}")
        else:
            logger.info(f"绩效漂移: {drift_result.get('verdict')} - "
                        f"{drift_result.get('note', '')[:60]}")
    except Exception as e:
        logger.warning(f"漂移监控失败(忽略): {e}")

    # 2. 生成每日市场简报（包含完整信息，不只是推荐列表）
    # 2026-09-16 P1-1：把策略**运行期**的数据源快照透传给简报。
    # 策略在 run() 结束时把 self.data_engine.get_data_source_summary() 挂到
    # 每条推荐上（含 skip_reason / no_qualified 元信息条目），这里取首条即可；
    # 简报据此在首屏渲染降级警示，替代原先恒为全绿的内部实例快照。
    _runtime_src = (recommendations[0].get('data_source_status')
                    if recommendations else None)
    # 2026-09-22 修复（第二轮回归 P2-2 · P1-12 接线补全）：长线模式下
    # `recommendations` 就是 run_long_term 的结果，直接透传给简报，
    # 避免简报内联再跑一遍长线全流程（重复网络/配额消耗 + 口径可能不一致）。
    # 短线模式下简报仍按 config 阈值内联扫描（阈值已从硬编码 50 改为读 config）。
    briefing_text = generate_market_briefing(recommendations, mode=args.mode,
                                            source_status=_runtime_src,
                                            long_recs=(recommendations
                                                       if args.mode == 'long'
                                                       else None))

    # 2.5 数据质量巡检（T5，2026-09-06 第一梯队）：结果追加到简报尾部，
    # 异常同时打 WARNING 日志。只读巡检，不改变简报既有内容。
    # 注意：这里用独立 DataEngine——模块级行情缓存（分钟键）会命中刚才
    # 选股拉取的同一份快照，几乎零成本。
    try:
        from core.data_quality_monitor import DataQualityMonitor, format_quality_line
        dq = DataQualityMonitor().check_quotes(DataEngine().get_all_quotes())
        briefing_text = briefing_text.rstrip('\n') + '\n' + format_quality_line(dq) + '\n'
    except Exception as e:
        logger.warning(f"数据质量巡检失败(忽略): {e}")

    # 2.6 绩效漂移状态（2026-09-23 交接单 D7）：原结论只进日志/JSON，简报里没有。
    #     仅在非 OK 时追加（OK 时避免噪声；ALERT/WATCH/未就绪都需要可见）。
    try:
        if drift_result and drift_result.get('verdict') not in (None, 'OK'):
            _dv = drift_result.get('verdict')
            _icon = {'ALERT': '🔴', 'WATCH': '🟡'}.get(_dv, '⚪')
            briefing_text = briefing_text.rstrip('\n') + '\n' + (
                f"{_icon} 绩效漂移: {_dv} — {drift_result.get('note', '')[:160]}\n")
    except Exception as e:
        logger.warning(f"简报追加漂移状态失败(忽略): {e}")

    print(briefing_text)

    # 3. 保存简报文件
    report_generator = DailyReportGenerator()
    path = report_generator.save_report(recommendations, args.mode)
    # 同时保存完整简报版本
    briefing_path = path.replace('.md', '_briefing.md')
    with open(briefing_path, 'w', encoding='utf-8') as f:
        f.write(briefing_text)
    print(f"\n📝 报告已保存: {path}")
    print(f"📊 简报已保存: {briefing_path}")

    # 3.5 用户指定的 --out 路径（docstring 承诺的行为，此前为死参数未实现）
    if args.out:
        out_dir = os.path.dirname(os.path.abspath(args.out))
        os.makedirs(out_dir, exist_ok=True)
        with open(args.out, 'w', encoding='utf-8') as f:
            f.write(briefing_text)
        print(f"📤 简报已另存: {args.out}")

    # 4. 自动回填 T+1 结果（2026-08-15 起挪到凌晨独立跑 scripts/backfill_pending.py：
    #    qfq 走 baostock ~8s/条，14:45 贴收盘，回填拖延会推迟报告生成；
    #    凌晨数据已完整，回填与策略无依赖，时序无关。cron: dream-backfill 3:15）

    # 5. 检查优化器是否触发（仅产报告，不自动写入）
    #    零推荐日（元信息条目 = 无 code）跳过：优化器产出的是"权重调整建议"，
    #    当日没有真实推荐时触发它没有意义（与历史 `if recommendations` 语义一致）。
    if recommendations and args.mode == 'short' and recommendations[0].get('code'):
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

    # 2026-09-22 修复（缺陷排查 P2-24 · 契约 A）：`d.get(k, '?')` 的默认值只在
    # 键缺失时生效 —— 键存在而值为 null 时 `'?'.format` / `None * 100` 会
    # TypeError。本函数在"选股已成功落库"之后的收尾阶段执行，一旦抛出
    # 整个 eod 以非 0 退出，daily_job 会判为失败（简报已生成，语义误导）。
    def _fmt_num(v, fmt='{:.1f}', default='—'):
        try:
            fv = float(v)
        except (TypeError, ValueError):
            return default
        return fmt.format(fv) if math.isfinite(fv) else default

    print(f"  训练数据: {d.get('total_records') or '—'} 条 | "
          f"胜率 {_fmt_num(d.get('win_rate'), '{:.1f}', '—')}% | "
          f"平均收益 {_fmt_num(d.get('avg_return_t1'), '{:+.2f}', '—')}%")
    print(f"  时间范围: {d.get('date_range') or '—'}")
    print(f"  数据新鲜度: {_fmt_num((d.get('fresh_ratio') or 0) * 100, '{:.0f}', '—')}%")

    print(f"\n  因子信号诊断:")
    for fname, stats in (d.get('factor_stats') or {}).items():
        _uv = stats.get('unique_values') or 0
        sig = "✅" if _uv >= 3 else "❌"
        print(f"    {sig} {fname}: {stats.get('n_samples') or 0}条, "
              f"{_uv}个唯一值, "
              f"范围 {stats.get('min') if stats.get('min') is not None else '—'}"
              f"~{stats.get('max') if stats.get('max') is not None else '—'}")

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
