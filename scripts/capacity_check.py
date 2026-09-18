"""
策略资金容量测算 — 审查报告 2026-09-05 P3-S

问题
----
回测默认 0.1% 统一滑点隐含"资金规模无关"假设。当单票分配资金相对该票
日成交额（ADV）不再可忽略时，实际冲击成本远高于回测假设——策略在回测里
赚钱、实盘里被冲击成本吃掉。

测算方法（对每只持仓票）
------------------------
1. ADV        = 近 N 日（默认 20）平均成交额（元），来自 kline_cache 本地缓存
2. 参与率      = 单票分配资金 / ADV（一次性建仓占当日成交额比例）
3. 分档滑点    = 按 config backtest.slippage_tiers 查表（与回测口径一致）
4. sqrt 冲击   = impact_const × σ_daily × sqrt(参与率)
   （平方根定律 Almgren-Chriss 经验近似；σ_daily 用同期收盘价收益标准差，
    不拍脑袋给常数；impact_const 默认 1.0）
5. 单边成本 bps = 分档滑点 + 冲击 + 佣金万三；双边再加印花税卖出 0.05%

容量结论
--------
- 按参与率上限（默认 10%）倒推最大资金容量：
  max_capital ≈ 持仓数 × min(ADV) × 参与率上限（用最差票保守估计，
  另给 ADV 中位数口径作参考）
- 单票参与率 > 10% 记 WARN，> 25% 记 DANGER

用法
----
  # 默认：资金取 config backtest.initial_capital，股票取最近一次 short 推荐
  python scripts/capacity_check.py

  # 指定资金与股票
  python scripts/capacity_check.py --capital 3000000 --stocks 000001,600519

  # 离线模式（只用 kline_cache 缓存，不触网）
  python scripts/capacity_check.py --offline
"""

import argparse
import json
import logging
import math
import os
import sqlite3
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

import yaml

logging.basicConfig(level=logging.INFO,
                    format='%(asctime)s [%(levelname)s] %(message)s',
                    datefmt='%H:%M:%S')
logger = logging.getLogger(__name__)

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

COMMISSION = 0.0003      # 佣金万三（与回测口径一致）
STAMP_DUTY = 0.0005      # 印花税（仅卖出）
PARTICIPATION_WARN = 0.10
PARTICIPATION_DANGER = 0.25
IMPACT_CONST = 1.0       # sqrt 冲击经验常数（Almgren-Chriss 近似）


def load_config(path: str = None) -> dict:
    path = path or os.path.join(PROJECT_ROOT, 'config.yml')
    if os.path.exists(path):
        with open(path, 'r', encoding='utf-8') as f:
            return yaml.safe_load(f) or {}
    return {}


def lookup_slippage(order_value: float, tiers) -> float:
    """按 [[成交额上限, 滑点], ...] 升序档位查滑点（与 backtest_engine 同规则）"""
    if not tiers:
        return 0.001
    for cap, slip in sorted(tiers, key=lambda t: float(t[0])):
        if order_value <= float(cap):
            return float(slip)
    return float(sorted(tiers, key=lambda t: float(t[0]))[-1][1])


def load_kline_from_cache(code: str, days: int) -> 'object':
    """离线直读 kline_cache（data/cache/kline_cache.db），返回 DataFrame 或 None"""
    import pandas as pd
    db = os.path.join(PROJECT_ROOT, 'data', 'cache', 'kline_cache.db')
    if not os.path.exists(db):
        return None
    conn = sqlite3.connect(db)
    try:
        df = pd.read_sql_query(
            "SELECT date, close, amount FROM kline_cache WHERE code=? "
            "ORDER BY date DESC LIMIT ?",
            conn, params=(code, days))
        return df if not df.empty else None
    finally:
        conn.close()


def get_kline(code: str, days: int, offline: bool):
    """取近 days 根K线（含当日）。offline=True 只读缓存。"""
    if offline:
        return load_kline_from_cache(code, days)
    try:
        from core.data_engine import DataEngine
        kline = DataEngine().get_kline(code)
        if kline is not None and not kline.empty:
            return kline.tail(days)
    except Exception as e:
        logger.warning(f"{code} 在线取K线失败，回退缓存: {e}")
        return load_kline_from_cache(code, days)
    return load_kline_from_cache(code, days)


def recent_recommendation_codes(limit: int = 10) -> list:
    """从 predictions.db 取最近一次 short 推荐的股票代码（默认测算对象）"""
    db = os.path.join(PROJECT_ROOT, 'data', 'db', 'predictions.db')
    if not os.path.exists(db):
        return []
    conn = sqlite3.connect(db)
    try:
        rows = conn.execute(
            "SELECT code FROM predictions WHERE mode='short' "
            "ORDER BY date DESC, id DESC LIMIT ?", (limit,)).fetchall()
        seen, codes = set(), []
        for (c,) in rows:
            if c and c not in seen:
                seen.add(c)
                codes.append(c)
        return codes
    finally:
        conn.close()


def measure_stock(code: str, alloc: float, adv_days: int,
                  offline: bool, tiers) -> dict:
    """单票容量测算。数据不可用时返回 {'code':..., 'error':...}"""
    kline = get_kline(code, adv_days, offline)
    if kline is None or len(kline) < 5:
        return {'code': code, 'error': 'K线数据不足（缓存缺失或离线）'}

    amounts = [float(a) for a in kline['amount'].tolist() if a]
    closes = [float(c) for c in kline['close'].tolist() if c]
    if len(amounts) < 5:
        return {'code': code, 'error': '成交额数据不足'}

    adv = sum(amounts) / len(amounts)
    participation = alloc / adv if adv > 0 else float('inf')

    # 日波动率：近窗口收盘价对数收益标准差（无前视，只用已发生数据）
    vol = None
    if len(closes) >= 6:
        rets = [math.log(closes[i] / closes[i - 1])
                for i in range(1, len(closes)) if closes[i - 1] > 0]
        if len(rets) >= 2:
            mean = sum(rets) / len(rets)
            var = sum((r - mean) ** 2 for r in rets) / (len(rets) - 1)
            vol = math.sqrt(var)

    slip = lookup_slippage(alloc, tiers)
    impact = (IMPACT_CONST * vol * math.sqrt(participation)
              if vol is not None and participation > 0 else 0.0)
    one_side = slip + impact + COMMISSION
    round_trip = 2 * COMMISSION + slip * 2 + impact * 2 + STAMP_DUTY

    if participation > PARTICIPATION_DANGER:
        flag = 'DANGER'
    elif participation > PARTICIPATION_WARN:
        flag = 'WARN'
    else:
        flag = 'OK'

    return {
        'code': code,
        'adv_yuan': round(adv, 0),
        'alloc_yuan': round(alloc, 0),
        'participation': round(participation, 4),
        'daily_vol': round(vol, 4) if vol is not None else None,
        'tier_slippage_bps': round(slip * 1e4, 1),
        'impact_bps': round(impact * 1e4, 1),
        'one_side_cost_bps': round(one_side * 1e4, 1),
        'round_trip_cost_bps': round(round_trip * 1e4, 1),
        'flag': flag,
    }


def run(capital: float, codes: list, adv_days: int, offline: bool,
        config: dict) -> dict:
    tiers = (config.get('backtest', {}) or {}).get('slippage_tiers') or []
    n_stocks = max(len(codes), 1)
    alloc = capital / n_stocks  # 等权简化：单票分配 = 总资金 / 持仓数

    results = [measure_stock(c, alloc, adv_days, offline, tiers) for c in codes]

    ok = [r for r in results if 'error' not in r]
    summary = {
        'capital': capital, 'n_stocks': len(codes), 'alloc_per_stock': alloc,
        'adv_days': adv_days, 'measured': len(ok),
        'max_participation': max((r['participation'] for r in ok), default=None),
        'avg_round_trip_cost_bps': (round(sum(r['round_trip_cost_bps']
                                               for r in ok) / len(ok), 1)
                                    if ok else None),
        'n_warn': sum(1 for r in ok if r['flag'] == 'WARN'),
        'n_danger': sum(1 for r in ok if r['flag'] == 'DANGER'),
    }
    # 容量上限：参与率 ≤ 10% 倒推（保守用最差票 ADV，参考用 ADV 中位数）
    if ok:
        advs = sorted(r['adv_yuan'] for r in ok)
        cap = PARTICIPATION_WARN
        summary['capacity_conservative'] = round(advs[0] * len(ok) / cap, 0)
        mid = advs[len(advs) // 2] if len(advs) % 2 else \
            (advs[len(advs) // 2 - 1] + advs[len(advs) // 2]) / 2
        summary['capacity_median_adv'] = round(mid * len(ok) / cap, 0)

    return {'summary': summary, 'stocks': results}


def print_report(report: dict):
    s = report['summary']
    print("\n" + "=" * 72)
    print(f"容量测算：总资金 ¥{s['capital']:,.0f} × {s['n_stocks']} 只等权 "
          f"= 单票 ¥{s['alloc_per_stock']:,.0f}（ADV窗口 {s['adv_days']} 日）")
    print("=" * 72)
    if not report['stocks']:
        print("  无可测算股票（无推荐记录且未指定 --stocks）")
        return
    header = (f"  {'代码':<8} {'ADV(万)':>10} {'参与率':>8} {'档滑点':>7} "
              f"{'冲击':>7} {'双边成本':>8}  状态")
    print(header)
    print("  " + "-" * 66)
    for r in report['stocks']:
        if 'error' in r:
            print(f"  {r['code']:<8} — {r['error']}")
            continue
        mark = {'OK': '✅', 'WARN': '⚠️ ', 'DANGER': '🚫'}[r['flag']]
        print(f"  {r['code']:<8} {r['adv_yuan']/1e4:>10,.0f} "
              f"{r['participation']*100:>7.1f}% "
              f"{r['tier_slippage_bps']:>6.0f}bp {r['impact_bps']:>6.0f}bp "
              f"{r['round_trip_cost_bps']:>7.0f}bp  {mark}")
    if s.get('capacity_conservative'):
        print(f"\n  参与率上限 10% 倒推容量："
              f"保守（最差票ADV）≈ ¥{s['capacity_conservative']/1e4:,.0f} 万 | "
              f"中位ADV口径 ≈ ¥{s['capacity_median_adv']/1e4:,.0f} 万")
    if s.get('n_warn') or s.get('n_danger'):
        print(f"  ⚠️ {s['n_warn']} 只超过 10% 参与率、"
              f"🚫 {s['n_danger']} 只超过 25%：实际冲击成本将显著高于回测假设，"
              f"建议缩小单票仓位或提高 min_volume 流动性门槛")
    print("=" * 72 + "\n")


def main():
    parser = argparse.ArgumentParser(description='策略资金容量测算（P3-S）')
    parser.add_argument('--capital', type=float, default=None,
                        help='账户总资金（默认取 config backtest.initial_capital）')
    parser.add_argument('--stocks', type=str, default=None,
                        help='逗号分隔股票代码（默认取最近一次 short 推荐）')
    parser.add_argument('--adv-days', type=int, default=20,
                        help='ADV/波动率窗口（默认20日）')
    parser.add_argument('--offline', action='store_true',
                        help='只用 kline_cache 缓存，不触网')
    parser.add_argument('--out', type=str, default=None,
                        help='结果 JSON 保存路径（默认 data/reports/capacity_latest.json）')
    parser.add_argument('--config', type=str, default=None)
    args = parser.parse_args()

    config = load_config(args.config)
    capital = args.capital or (config.get('backtest', {}) or {}).get(
        'initial_capital', 1_000_000)
    codes = ([c.strip() for c in args.stocks.split(',') if c.strip()]
             if args.stocks else recent_recommendation_codes())
    if not codes:
        print("没有可测算的股票：predictions.db 无推荐记录，也未指定 --stocks")
        return

    report = run(capital, codes, args.adv_days, args.offline, config)
    print_report(report)

    out = args.out or os.path.join(PROJECT_ROOT, 'data', 'reports',
                                   'capacity_latest.json')
    os.makedirs(os.path.dirname(out), exist_ok=True)
    with open(out, 'w', encoding='utf-8') as f:
        json.dump(report, f, ensure_ascii=False, indent=2)
    print(f"📄 结果已保存: {out}")


if __name__ == '__main__':
    main()
