#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
分区间 / 分市场状态检验（A 股单边做多口径）
============================================

回答"这笔钱是均匀赚出来的，还是集中在某一两个月"——把逐笔收益按时间区间
（月/季）聚合，看盈利是否集中、是否由单期贡献。

输入：逐笔数据 {date, ret}（ret 为百分数，同 backtest_store.return_t1）
      + 可选大盘日收益序列 {date, ret} 或 {date, close}

区间聚合口径（务必明确）
------------------------
每个区间：
  - n        : 该区间笔数
  - pnl      : Σ 该区间逐笔 ret（等权近似，单位=百分点；无仓位数据，按每笔等权）
  - ret_pct  : pnl / n（区间平均单笔收益 %）
  - win_rate : 盈利笔数占比 %
  - market_pct : （若有大盘）该区间大盘收益 %（日收益求和）
汇总：
  - 盈利期数 / 总期数
  - 最赚一期占总盈亏比（仅 tot>0 时打印）
  - 去掉最赚一期后的结果（tot - best）
  - 正 / 负盈亏期合计
JSON 落盘：{tag, freq, rows, total_pnl, n_positive, n_periods}

⚠️ 一条来自 EP007 的真实教训（已写进代码与报告）
------------------------------------------------
EP007 的 `--end 2026-06-30` 被解析成 `2026-06-30 00:00:00`，把最后一整天切掉
→ 冠军收益从 **+1.74% 变成 −0.78%，符号翻转**，且**不报任何错**（两个数字都"看起来合理"）。
本脚本的防御：
  1. `--end` 默认值 = 完整窗口末尾（缺省取全部交易日的最大值，先天含最后一天）。
  2. 日期一律按**自然日(date)**比较，区间是闭区间 [start, end]，绝不把 end 当天截断。
  3. 运行必打印"实际生效起止日期"，并要求使用者与权威口径对账（见输出末行）。

0 交易处理
----------
逐笔为空（0 笔）时打印提示并以非 0 退出，不输出误导性统计。

用法
----
  python scripts/subperiod.py --trades trades.json --freq M
  python scripts/subperiod.py --trades trades.json --freq Q --market index.json --out sp.json
  python scripts/subperiod.py --from-store --freq M
"""
import argparse
import json
import os
import sys
from datetime import date, datetime

if sys.platform == 'win32':
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


# ── 日期解析 ─────────────────────────────────────────────────
def _parse_date(s):
    if isinstance(s, (datetime, date)):
        return s.date() if isinstance(s, datetime) else s
    s = str(s).strip()
    for fmt in ('%Y-%m-%d', '%Y/%m/%d', '%Y-%m-%d %H:%M:%S'):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    raise ValueError(f"无法解析日期: {s!r}（请用 YYYY-MM-DD）")


# ── 数据装载 ─────────────────────────────────────────────────
def _extract_trades(data):
    """从 JSON 抽出 [{date, ret}]（ret 百分数）。"""
    if isinstance(data, list):
        raw = data
    elif isinstance(data, dict):
        raw = data.get('trade_details') or data.get('trades')
    else:
        raw = None
    if not raw:
        raise ValueError("JSON 中未找到 trades / trade_details 列表")
    out = []
    for t in raw:
        if not isinstance(t, dict):
            continue
        d = t.get('date')
        if d is None:
            continue
        v = t.get('return_t1')
        if v is None:
            v = t.get('ret')
        if v is None:
            v = t.get('ret_t1')
        if v is None:
            v = t.get('profit_ratio')
        if v is None:
            continue
        out.append({'date': _parse_date(d), 'ret': float(v)})
    if not out:
        raise ValueError("未从输入提取到任何 (date, 收益)")
    return out


def load_trades_from_json(path):
    with open(path, 'r', encoding='utf-8') as f:
        return _extract_trades(json.load(f))


def _load_from_store():
    import sqlite3
    db = os.path.join(ROOT, 'data', 'cache', 'backtest_cache.db')
    if not os.path.exists(db):
        raise FileNotFoundError(f"未找到 backtest_store 数据库: {db}")
    conn = sqlite3.connect(db)
    try:
        row = conn.execute(
            "SELECT id FROM backtest_runs ORDER BY id DESC LIMIT 1").fetchone()
        if not row:
            raise ValueError("backtest_store 中没有任何回测记录")
        rid = row[0]
        rows = conn.execute(
            "SELECT date, return_t1 FROM backtest_trades WHERE run_id=? ORDER BY id",
            (rid,)).fetchall()
    finally:
        conn.close()
    if not rows:
        raise ValueError(f"最近一次 run#{rid} 没有逐笔交易明细（0 交易）")
    out = []
    for d, v in rows:
        if v is None:
            continue
        out.append({'date': _parse_date(d), 'ret': float(v)})
    if not out:
        raise ValueError(f"最近一次 run#{rid} 的明细无有效 (date, return_t1)")
    return out


def _load_market(path):
    """大盘序列 → {date: 日收益%}。支持 {date, ret} 或 {date, close}。"""
    with open(path, 'r', encoding='utf-8') as f:
        data = json.load(f)
    items = data if isinstance(data, list) else (data.get('rows') or [])
    ret_by_date = {}
    if items and 'close' in items[0]:
        srt = sorted(items, key=lambda x: _parse_date(x['date']))
        for i in range(1, len(srt)):
            d0, d1 = _parse_date(srt[i - 1]['date']), _parse_date(srt[i]['date'])
            c0, c1 = float(srt[i - 1]['close']), float(srt[i]['close'])
            if c0:
                ret_by_date[d1] = (c1 / c0 - 1.0) * 100.0
    else:
        for it in items:
            d = _parse_date(it['date'])
            r = it.get('ret')
            if r is None and it.get('return_t1') is not None:
                r = it['return_t1']
            if r is not None:
                ret_by_date[d] = float(r)
    return ret_by_date


# ── 核心分析 ─────────────────────────────────────────────────
def _period_key(d, freq):
    if freq == 'M':
        return f"{d.year:04d}-{d.month:02d}"
    if freq == 'Q':
        return f"{d.year:04d}-Q{(d.month - 1) // 3 + 1}"
    raise ValueError(f"未知 freq: {freq!r}（应为 'M' 或 'Q'）")


def analyze(trades, freq='M', market=None, start=None, end=None, tag='subperiod'):
    """分区间检验。trades: [{date, ret}]（ret 百分数）。

    返回 dict（结构见模块 docstring）。空输入抛 ValueError（调用方非 0 退出）。
    """
    if not trades:
        raise ValueError("逐笔数据为空（0 笔交易），无法做分区间检验；"
                         "请检查输入或回测是否真的有成交。")

    # 闭区间 [start, end]，按自然日比较——绝不截断 end 当天（EP007 教训）
    # 归一化：允许调用方直接传字符串日期；**不修改入参**（避免共享列表被就地篡改）
    norm = []
    for t in trades:
        d = t['date']
        if not isinstance(d, (datetime, date)):
            d = _parse_date(d)
        norm.append({'date': d, 'ret': float(t['ret'])})

    all_dates = [t['date'] for t in norm]
    if start is None:
        start = min(all_dates)
    else:
        start = _parse_date(start)
    if end is None:
        end = max(all_dates)          # 缺省 = 窗口末尾，先天含最后一天
    else:
        end = _parse_date(end)

    if market is not None:
        if not isinstance(market, dict):
            raise ValueError("market 应为 {date: 日收益%} 字典")
        # 归一化大盘键为 date 对象（兼容字符串键 / _load_market 的 date 键）
        market = {(_parse_date(k) if not isinstance(k, (datetime, date)) else k): float(v)
                  for k, v in market.items()}

    kept = [t for t in norm if start <= t['date'] <= end]
    if not kept:
        raise ValueError(f"给定区间 [{start},{end}] 内无交易（可能 end 早于全部交易）")

    rows = {}
    for t in kept:
        k = _period_key(t['date'], freq)
        rec = rows.setdefault(k, {'period': k, 'n': 0, 'pnl': 0.0, 'wins': 0,
                                  'market_pnl': 0.0})
        rec['n'] += 1
        rec['pnl'] += t['ret']
        if t['ret'] > 0:
            rec['wins'] += 1
        if market is not None and t['date'] in market:
            rec['market_pnl'] += market[t['date']]

    period_list = []
    for k in sorted(rows):
        rec = rows[k]
        rec['ret_pct'] = round(rec['pnl'] / rec['n'], 4) if rec['n'] else 0.0
        rec['win_rate'] = round(rec['wins'] / rec['n'] * 100.0, 2) if rec['n'] else 0.0
        rec['pnl'] = round(rec['pnl'], 4)
        rec['market_pct'] = round(rec['market_pnl'], 4) if market is not None else None
        period_list.append(rec)

    total_pnl = round(sum(r['pnl'] for r in period_list), 4)
    n_periods = len(period_list)
    n_positive = sum(1 for r in period_list if r['pnl'] > 0)
    best = max(period_list, key=lambda r: r['pnl'])
    best_share = round(best['pnl'] / total_pnl, 4) if total_pnl > 0 else None
    after_best = round(total_pnl - best['pnl'], 4)
    positive_pnl = round(sum(r['pnl'] for r in period_list if r['pnl'] > 0), 4)
    negative_pnl = round(sum(r['pnl'] for r in period_list if r['pnl'] < 0), 4)

    return {
        'tag': tag,
        'freq': freq,
        'start': start.isoformat(),
        'end': end.isoformat(),
        'rows': period_list,
        'total_pnl': total_pnl,
        'n_positive': n_positive,
        'n_periods': n_periods,
        'best_period': best['period'],
        'best_pnl': best['pnl'],
        'best_share': best_share,
        'after_best': after_best,
        'positive_pnl_total': positive_pnl,
        'negative_pnl_total': negative_pnl,
    }


# ── CLI ──────────────────────────────────────────────────────
def main(argv=None):
    ap = argparse.ArgumentParser(description='分区间 / 分市场状态检验')
    ap.add_argument('--trades', default=None)
    ap.add_argument('--from-store', action='store_true')
    ap.add_argument('--freq', choices=['M', 'Q'], default='M')
    ap.add_argument('--market', default=None, help='大盘序列 JSON（{date,ret} 或 {date,close}）')
    ap.add_argument('--start', default=None, help='起始日 YYYY-MM-DD（默认=首笔）')
    ap.add_argument('--end', default=None,
                    help='结束日 YYYY-MM-DD（默认=窗口末尾，含最后一天；勿截断）')
    ap.add_argument('--tag', default='subperiod')
    ap.add_argument('--out', default=None, help='JSON 落盘路径')
    a = ap.parse_args(argv)

    try:
        if a.from_store:
            trades = _load_from_store()
        elif a.trades:
            trades = load_trades_from_json(a.trades)
        else:
            print("[sp] 错误：必须提供 --trades <json> 或 --from-store")
            return 2

        if not trades:
            print("[sp] 错误：逐笔数据为空（0 笔交易）。"
                  "当前回测恒为 0 交易属已知现象，请先产生真实成交或传入合成数据。")
            return 2

        market = _load_market(a.market) if a.market else None
        res = analyze(trades, freq=a.freq, market=market,
                      start=a.start, end=a.end, tag=a.tag)
    except (ValueError, FileNotFoundError) as e:
        print(f"[sp] 无法执行分区间检验: {e}")
        return 2

    # 文本报告
    print(f"[sp] 区间口径: {res['freq']}  | 实际生效: {res['start']} ~ {res['end']}（闭区间，含首尾整天）")
    print(f"{'区间':<10}{'笔数':>6}{'pnl':>10}{'均收益%':>10}{'胜率%':>8}"
          + ("{'市场%':>10}" if any(r['market_pct'] is not None for r in res['rows']) else ""))
    for r in res['rows']:
        line = f"{r['period']:<10}{r['n']:>6}{r['pnl']:>10.2f}{r['ret_pct']:>10.2f}{r['win_rate']:>8.1f}"
        if r['market_pct'] is not None:
            line += f"{r['market_pct']:>10.2f}"
        print(line)
    print(f"[sp] 盈利期数/总期数: {res['n_positive']}/{res['n_periods']}")
    print(f"[sp] 总盈亏: {res['total_pnl']:+.2f}%  | 正盈亏期合计 {res['positive_pnl_total']:+.2f}  "
          f"负盈亏期合计 {res['negative_pnl_total']:+.2f}")
    if res['best_share'] is not None:
        print(f"[sp] 最赚一期 {res['best_period']} 占总结盈亏比: {res['best_share']*100:.1f}%"
              f"（pnl={res['best_pnl']:+.2f}）")
        print(f"[sp] 去掉最赚一期后: {res['after_best']:+.2f}%")
    print("[sp] ⚠ 请与权威口径（如交易所/券商对账单）对账实际生效起止日期，"
          "确保 end 当天未被截断。")

    if a.out:
        with open(a.out, 'w', encoding='utf-8') as f:
            json.dump(res, f, ensure_ascii=False, indent=2)
        print(f"[sp] json -> {a.out}")
    return 0


if __name__ == '__main__':
    sys.exit(main())
