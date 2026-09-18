# -*- coding: utf-8 -*-
"""尾盘 VWAP 滑点校准（2026-09-06 第一梯队 T2）

目的：回测滑点模型当前是"按日成交额分档"的经验值。本工具用通达信 1 分钟线
回放最近 N 个交易日 14:45-15:00 的实际成交结构，量化"14:50 快照价 vs 尾盘
VWAP"的偏差分布，检验现滑点参数是否合理。

方法：
  - 样本：流动性分层抽样（成交额 top / 中位 / 尾部各若干只）
  - 每只每交易日：尾盘窗口 VWAP = Σ(price×vol) / Σ(vol)；
    参考价 = 14:50 所在 1 分钟 K 的收盘价（模拟尾盘快照成交）
  - 偏差 = (参考价 - VWAP) / VWAP（>0 = 快照价高于均价 = 尾盘追高成交不利）
  - 输出分层 P50/P90，与 config 滑点分档对照，给出建议（**不自动改参数**）

用法：
  python scripts/calibrate_slippage.py [--days 5] [--per-tier 6]
只读分析，不写任何业务数据。
"""
import argparse
import logging
import os
import sys

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
logger = logging.getLogger('calibrate_slippage')

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import pandas as pd  # noqa: E402


def pick_samples(de, per_tier: int):
    """按当日成交额三档抽样（用已缓存的全市场快照，不耗任何配额）。"""
    quotes = de.get_all_quotes()
    if quotes is None or quotes.empty or 'amount' not in quotes.columns:
        logger.error("全市场快照不可用")
        return []
    q = quotes.dropna(subset=['amount']).sort_values('amount', ascending=False)
    n = len(q)
    tiers = {
        '高流动性(top10%)': q.head(max(n // 10, per_tier)),
        '中流动性(40-60%)': q.iloc[int(n * 0.4):int(n * 0.4) + per_tier * 2],
        '低流动性(80-90%)': q.iloc[int(n * 0.8):int(n * 0.8) + per_tier * 2],
    }
    samples = []
    for tier, sub in tiers.items():
        for _, row in sub.head(per_tier).iterrows():
            samples.append((tier, str(row['code']).zfill(6), float(row['amount'])))
    return samples


def fetch_min1(de, code: str, days: int) -> pd.DataFrame:
    """拉 1 分钟 K 线（mootdx category=8），返回最近 days 个交易日的 14:40-15:00 窗口。"""
    try:
        client = de._get_mootdx_client()
        code6 = str(code).zfill(6)
        raw = client.bars(symbol=code6, category=8, offset=days * 240 + 300)
        if raw is None or len(raw) == 0:
            return pd.DataFrame()
        df = pd.DataFrame(raw)
        df['dt'] = pd.to_datetime(df['datetime'].astype(str), errors='coerce')
        df = df.dropna(subset=['dt'])
        for c in ('close', 'open', 'volume', 'amount'):
            if c in df.columns:
                df[c] = pd.to_numeric(df[c], errors='coerce')
        # 尾盘窗口：14:40 之后（覆盖 14:45-15:00 需求并留余量）
        win = df[(df['dt'].dt.time >= pd.Timestamp('14:40').time())]
        return win
    except Exception as e:
        logger.warning(f"1分钟线获取失败 {code}: {type(e).__name__} {str(e)[:50]}")
        return pd.DataFrame()


def calibrate_daily_mode(de, days: int, per_tier: int):
    """日线近似校验（分钟数据不可用时的降级模式）。

    提供两个滑点相关的量级参考：
      1. 隔夜跳空分布 |open_t1/close_t0 - 1|：尾盘价与次日实际可成交价的差异下界；
      2. 收盘价相对全天 VWAP（amount/volume手/100）的偏离：尾盘买点相对
         全天均价的系统性位置（追高/抄底倾向）。
    """
    from core.backtest_engine import BacktestEngine
    be = BacktestEngine.__new__(BacktestEngine)
    be.config = {}
    be.commission = 0.0003
    be.transfer_fee = 0.00001
    be.stamp_duty = 0.0005
    import yaml
    with open(os.path.join(ROOT, 'config.yml'), encoding='utf-8') as f:
        cfg = yaml.safe_load(f) or {}
    be.slippage_tiers = cfg.get('backtest', {}).get('slippage_tiers', [])

    samples = pick_samples(de, per_tier)
    if not samples:
        return
    rows = []
    for tier, code, _ in samples:
        k = de.get_kline(code, start_date=(pd.Timestamp.now() - pd.Timedelta(days=days * 3 + 10)).strftime('%Y-%m-%d'))
        if k is None or len(k) < days + 1:
            continue
        k = k.reset_index(drop=True)
        for i in range(1, len(k)):
            prev, cur = k.iloc[i - 1], k.iloc[i]
            try:
                c0, o1 = float(prev['close']), float(cur['open'])
                vol, amt = float(cur['volume']), float(cur.get('amount', 0) or 0)
            except (TypeError, ValueError):
                continue
            if c0 <= 0 or o1 <= 0 or vol <= 0:
                continue
            rows.append({'tier': tier, 'code': code,
                         'gap_pct': (o1 / c0 - 1) * 100,
                         'close_vs_vwap_pct': ((c0 / (amt / vol / 100) - 1) * 100) if amt > 0 else None})
    if not rows:
        logger.error("日线近似校验未取得有效样本")
        return
    df = pd.DataFrame(rows).dropna(subset=['close_vs_vwap_pct'], how='any')
    print("\n" + "=" * 64)
    print("日线近似校验（分钟数据不可用时的降级口径）")
    print("=" * 64)
    print("  ① 隔夜跳空 |open_t1/close_t0-1| —— 尾盘价与次日可成交价差异量级：")
    g = df['gap_pct'].abs()
    print(f"     全样本 P50={g.quantile(0.5):.3f}%  P90={g.quantile(0.9):.3f}%  "
          f"P99={g.quantile(0.99):.3f}%  max={g.max():.3f}%")
    print("  ② 收盘价相对全天 VWAP 偏离 —— 尾盘买点系统性位置：")
    v = df['close_vs_vwap_pct']
    print(f"     全样本 P50={v.quantile(0.5):+.3f}%  P90={v.quantile(0.9):+.3f}%  "
          f"P10={v.quantile(0.1):+.3f}%")
    print(f"  当前回测滑点分档: {be.slippage_tiers}")
    print("  解读：跳空是隔夜风险不是滑点，但若 P90 跳空远大于滑点参数，")
    print("        说明'尾盘快照价=成交价'假设在隔夜端完全不成立；")
    print("        收盘相对 VWAP 的偏离反映尾盘追高倾向，可与分档滑点交叉对照。")


def calibrate(de, days: int, per_tier: int):
    samples = pick_samples(de, per_tier)
    if not samples:
        return
    rows = []
    for tier, code, day_amount in samples:
        win = fetch_min1(de, code, days)
        if win.empty:
            continue
        # 按交易日分组，只保留最近 days 个交易日
        win = win.assign(d=win['dt'].dt.date)
        trade_dates = sorted(win['d'].unique())[-days:]
        for d in trade_dates:
            g = win[win['d'] == d]
            if g.empty:
                continue
            # 参考价：14:50 或其后第一分钟的收盘（模拟 14:50 快照成交）
            ref_row = g[g['dt'].dt.strftime('%H:%M') >= '14:50'].head(1)
            if ref_row.empty:
                continue
            ref_price = float(ref_row.iloc[0]['close'])
            # 尾盘 VWAP：14:50 至收盘
            tail = g[g['dt'].dt.strftime('%H:%M') >= '14:50']
            if tail.empty or float(tail['volume'].sum()) <= 0:
                continue
            # VWAP = Σ(close×volume)/Σvolume：收盘价按成交量加权。
            # 不用 amount/volume——mootdx 分钟线 volume 单位为"手"且各板块
            # 每手股数不一致，直接相除会出现 100 倍量纲偏差（首轮实测踩坑）
            vwap = float((tail['close'] * tail['volume']).sum() / tail['volume'].sum())
            if vwap <= 0 or ref_price <= 0:
                continue
            rows.append({
                'tier': tier, 'code': code, 'date': str(d),
                'ref': ref_price, 'vwap': vwap,
                'bias_pct': (ref_price / vwap - 1) * 100,
                'day_amount': day_amount,
            })

    if not rows:
        logger.error("未取得有效样本（1分钟线可能不可用）")
        return
    df = pd.DataFrame(rows)
    print("\n" + "=" * 64)
    print("尾盘快照价 vs 尾盘 VWAP 偏差（% 正数=快照价高于尾盘均价）")
    print("=" * 64)
    for tier, sub in df.groupby('tier'):
        p50 = sub['bias_pct'].quantile(0.5)
        p90 = sub['bias_pct'].quantile(0.9)
        print(f"  {tier}: n={len(sub):3d}  P50={p50:+.3f}%  P90={p90:+.3f}%  "
              f"mean={sub['bias_pct'].mean():+.3f}%")
    overall_p90 = df['bias_pct'].quantile(0.9)
    print(f"  全样本 P90 = {overall_p90:+.3f}%")
    try:
        import yaml
        with open(os.path.join(ROOT, 'config.yml'), encoding='utf-8') as f:
            cfg = yaml.safe_load(f) or {}
        tiers_cfg = cfg.get('backtest', {}).get('slippage_tiers', [])
        print(f"  当前回测滑点分档: {tiers_cfg}")
    except Exception as e:
        print(f"  （滑点配置读取失败: {e}）")
    print("  解读：P90 偏差若显著大于所配滑点，说明高流动性档滑点被低估；")
    print("        反之则过于保守。参数调整请人工决策后改 config.yml。")


def main():
    ap = argparse.ArgumentParser(description='尾盘 VWAP 滑点校准（只读分析）')
    ap.add_argument('--days', type=int, default=5, help='回放最近 N 个交易日（默认 5）')
    ap.add_argument('--per-tier', type=int, default=6, help='每流动性档抽样股票数（默认 6）')
    ap.add_argument('--mode', choices=['auto', 'min1', 'day'], default='auto',
                    help='auto=分钟线可用则用，否则自动降级日线近似')
    args = ap.parse_args()

    from core.data_engine import DataEngine, close_thread_conns
    de = DataEngine({})
    try:
        if args.mode in ('auto', 'min1'):
            # 探测分钟线尾盘数据是否完整（免费源可能只有收盘一根）
            probe = fetch_min1(de, '600519', 2)
            usable = not probe.empty and len(probe) >= 3
            if args.mode == 'min1' and not usable:
                logger.error("分钟线尾盘数据不可用且已强制 --mode min1，退出")
                return 1
            if args.mode == 'auto' and not usable:
                logger.warning("分钟线尾盘数据不完整（免费源限制）→ 自动降级日线近似校验")
                calibrate_daily_mode(de, args.days, args.per_tier)
                return 0
            calibrate(de, args.days, args.per_tier)
        else:
            calibrate_daily_mode(de, args.days, args.per_tier)
    finally:
        close_thread_conns()
    return 0


if __name__ == '__main__':
    sys.exit(main())
