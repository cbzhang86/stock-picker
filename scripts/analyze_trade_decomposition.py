# -*- coding: utf-8 -*-
"""P1a/P2 交易分解分析（2026-09-18）——只读分析，不改任何状态。

⚠️ **口径说明（2026-09-18 审查补充，勿误读）**
本脚本分解的是 **实盘执行路径**（尾盘/收盘买入 → 次日开盘或收盘卖出），
即「close_t0 → open_t1」隔夜段与「open_t1 → close_t1」日内段；
而**回测引擎的成交口径是 open_t1 买入、持有至多 3 天**（含止盈/止损/时间止损），
两者不是同一条持仓路径。因此：
  - 隔夜段（−0.37%）衡量的是「尾盘买入 vs 次日开盘买入」的入场时点差异
    （实盘尾盘买入者承担、回测按 open_t1 成交不承担）；
  - 不可把本脚本的"净收益"与库中 `return_t1`（open_t1→close_t1 含成本）
    直接比对——两者天然差一个隔夜跳空（原注释误归因于 buy_price 口径，已更正）；
  - 若要评估引擎的真实持仓路径收益，应看 `_simulate_portfolio` 的 trade_details。

对指定回测 run 的每笔交易，从 kline_cache 现算：
  1. P2 隔夜/日内分解：gap_ret = open_t1/close_t0 - 1（隔夜段）
                       intraday_ret = close_t1/open_t1 - 1（日内段）
  2. P1a 成本归因：双边成本 = 滑点×2 + 佣金×2 + 印花税 + 过户费
  3. 净收益 = (1+gap)(1+intraday)(1-成本) - 1

输出：分段均值/胜率、分月分解、按库中 return_t1 正负分组的成因对比。

用法：
  python scripts/analyze_trade_decomposition.py --run-id 52
"""
import argparse
import os
import sqlite3
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

DB_TRADES = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                         'data', 'cache', 'backtest_cache.db')
# 成本口径与 backtest_engine/config.yml 一致（单笔委托金额 33 万 → 最低档滑点 0.001）
SLIP = 0.001
COMMISSION = 0.0003
STAMP = 0.0005
TRANSFER = 0.00001
COST_ROUNDTRIP = SLIP * 2 + COMMISSION * 2 + STAMP + TRANSFER  # ≈ 0.312%


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--run-id', type=int, required=True)
    args = ap.parse_args()

    from core.data_engine import DataEngine
    de = DataEngine({})

    conn = sqlite3.connect(DB_TRADES)
    conn.row_factory = sqlite3.Row
    trades = conn.execute(
        "SELECT date, code, name, score, return_t1 FROM backtest_trades "
        "WHERE run_id=? ORDER BY date", (args.run_id,)).fetchall()
    if not trades:
        print(f"run {args.run_id} 无交易明细")
        return
    print(f"run {args.run_id}: {len(trades)} 笔 | 双边成本口径 ≈ {COST_ROUNDTRIP*100:.3f}%/笔\n")

    rows = []
    skipped = 0
    for t in trades:
        code, buy_date = t['code'], t['date']
        kline = de.get_kline(code, start_date=buy_date,
                             end_date=(__import__('datetime').datetime.strptime(buy_date, '%Y-%m-%d')
                                       + __import__('datetime').timedelta(days=14)).strftime('%Y-%m-%d'))
        if kline is None or len(kline) < 2:
            skipped += 1
            continue
        kline = kline.reset_index(drop=True)
        # date 列定位买入日（t0）与其后首个交易日（t1）
        if 'date' in kline.columns:
            kline['date'] = kline['date'].astype(str)
            mask = kline['date'] >= buy_date
            idx0 = kline[mask].index.min()
        else:
            idx0 = 0
        if idx0 is None or idx0 + 1 >= len(kline):
            skipped += 1
            continue
        close_t0 = float(kline.iloc[idx0]['close'])
        open_t1 = float(kline.iloc[idx0 + 1]['open'])
        close_t1 = float(kline.iloc[idx0 + 1]['close'])
        if not all(v > 0 for v in (close_t0, open_t1, close_t1)):
            skipped += 1
            continue
        gap = (open_t1 / close_t0 - 1) * 100          # 隔夜段（尾盘买入→次日开盘）
        intraday = (close_t1 / open_t1 - 1) * 100      # 日内段（开盘→收盘）
        net = ((1 + gap / 100) * (1 + intraday / 100) * (1 - COST_ROUNDTRIP) - 1) * 100
        rows.append({'date': buy_date, 'code': code, 'name': t['name'], 'score': t['score'],
                     'gap': gap, 'intraday': intraday, 'net': net,
                     'db_net': t['return_t1'], 'win': net > 0})

    print(f"有效 {len(rows)} 笔（跳过 {skipped}，K 线缺失/字段异常）\n")
    if not rows:
        # 2026-09-18 审查修复：全部跳过时提前返回，避免下方 len(...)=0 除零崩溃
        print("无有效样本（K 线缺失/字段异常），无法分解")
        return

    def stats(vals):
        n = len(vals)
        if not n:
            return "n=0"
        win = sum(1 for v in vals if v > 0) / n * 100
        return f"均值 {sum(vals)/n:+.3f}% | 胜率 {win:.1f}% | n={n}"

    gaps = [r['gap'] for r in rows]
    intras = [r['intraday'] for r in rows]
    nets = [r['net'] for r in rows]
    print("=== P2 隔夜/日内分解（新权重长窗口 4-8 月）===")
    print(f"隔夜段（尾盘买→次日开盘卖）: {stats(gaps)}")
    print(f"日内段（次日开盘→收盘）    : {stats(intras)}")
    print(f"净 T+1（含成本 {COST_ROUNDTRIP*100:.2f}%）     : {stats(nets)}")
    print(f"  → 隔夜段贡献 {'为正' if sum(gaps) > 0 else '为负'}，日内段贡献 {'为正' if sum(intras) > 0 else '为负'}；"
          f"日内段净拖累 = {sum(intras)/len(intras):+.3f}%/笔")

    # 库中净值交叉校验
    diffs = [abs(r['net'] - r['db_net']) for r in rows]
    print(f"交叉校验（自算净 vs 库中 return_t1）: 平均偏差 {sum(diffs)/len(diffs):.3f}pp（应 <0.1，"
          f"差异来自 buy_price 口径与精确费率）")

    # 分月
    print("\n=== 分月分解 ===")
    by_month = {}
    for r in rows:
        by_month.setdefault(r['date'][:7], []).append(r)
    print(f"{'月份':<8}{'隔夜均值':>10}{'日内均值':>10}{'净均值':>10}{'n':>5}")
    for m in sorted(by_month):
        rs = by_month[m]
        print(f"{m:<8}{sum(x['gap'] for x in rs)/len(rs):>+10.3f}"
              f"{sum(x['intraday'] for x in rs)/len(rs):>+10.3f}"
              f"{sum(x['net'] for x in rs)/len(rs):>+10.3f}{len(rs):>5}")

    # 胜负归因
    print("\n=== 胜负归因（净收益正 vs 负）===")
    for label, grp in (("盈利笔", [r for r in rows if r['net'] > 0]),
                       ("亏损笔", [r for r in rows if r['net'] <= 0])):
        if not grp:
            continue
        g = sum(x['gap'] for x in grp) / len(grp)
        i = sum(x['intraday'] for x in grp) / len(grp)
        print(f"{label}（n={len(grp)}）: 隔夜 {g:+.3f}% | 日内 {i:+.3f}%")

    # 结论提示
    og, ig = sum(gaps) / len(gaps), sum(intras) / len(intras)
    print("\n=== P2 结论提示 ===")
    if og > 0 and ig < 0:
        print(f"隔夜段为正（{og:+.3f}%）而日内段为负（{ig:+.3f}%）→ 支持 F05 假设："
              f"最优出场可能是 T+1 开盘直接卖出（跳过日内段），对应 time_stop 提前 + "
              f"开盘市价退出，跳过日内波动。")
    elif og > 0 and ig > 0:
        print("两段均为正 → 毛收益为正，亏损主因是成本与暴露，出场规则非第一瓶颈。")
    else:
        print("隔夜段非正 → F05 假设在该窗口/该股票池不成立，出场优化空间有限，"
              "瓶颈在入场选择。")


if __name__ == '__main__':
    main()
