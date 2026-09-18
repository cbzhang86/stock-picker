#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Block Bootstrap 蒙特卡洛（A 股单边做多口径）
================================================

回答"这条收益曲线有多少是运气"——对逐笔收益序列做分块有放回重采样，
在保留（部分）序列相关性的前提下构造大量等价长度的伪收益曲线，再统计：

  - prob_profit : 终值 > 起点 的伪曲线占比（盈利路径概率）
  - final_P5/P50/P95 : 终值分布的分位数
  - observed_final : 原始曲线的终值（与重采样分布对照）

输入口径（务必明确，已打印）
------------------------------
逐笔收益序列。本系统 `backtest_store` / `trade_details` 中的 `return_t1`
是**百分数**（例如 +1.04 表示 +1.04%）。因此本脚本默认 `--unit percent`：
内部用 `1 + r/100` 作为每笔的权益乘数。若你给的是小数（0.0104）请用
`--unit decimal`（内部用 `1 + r`）。同一口径下结果可对比，跨口径不可比。

方法论局限（如实写明）
----------------------
1. 以"每笔交易"为重采样单位（trade-level MC）。A 股单边做多、各笔交易在
   时间上可能重叠，这里按成交序列处理，是业界常见近似，但会忽略"同一交易日
   多笔并进"的并发仓位结构。EP007（加密永续/多空）也是这个近似。
2. block 只保留**块内**的短程序列相关性，块与块之间独立。若真实收益存在
   更长周期的 régime（牛熊切换），本方法会**低估**运气成分（即 prob_profit
   可能偏高）。block 越大相关性保留越久，但样本效率越低；默认 10 笔/块。
3. 这是"重采样已有样本"的非参数方法，不假设收益分布形态；结论的置信区间
   来自 bootstrap 本身，而非正态分布假设。

0 交易处理
----------
逐笔收益为空（0 笔）时，脚本打印明确提示并**以非 0 退出码退出**，不输出一堆 0
（避免"看起来合理的假阳性"）。当前本仓库回测恒为 0 交易，故 `--from-store`
默认就会走到这条路径——这是预期行为，不是 bug。

用法
----
  python scripts/mc_bootstrap.py --trades trades.json --iters 5000 --block 10 \
      --seed 7 --start 1000 --out mc.json [--plot mc.png]
  python scripts/mc_bootstrap.py --from-store        # 取 backtest_store 最近一次 run
  python scripts/mc_bootstrap.py --trades trades.json --unit decimal

trades.json 支持多种结构：
  - 纯数字数组 [1.04, -0.5, ...]
  - 对象数组 [{"return_t1": 1.04}, {"ret": -0.5}, ...]
  - {"trade_details":[{"date":..., "return_t1":1.04}, ...]}
  - {"trades":[{"return_t1":1.04}, ...]}
"""
import argparse
import json
import os
import sys

import numpy as np

if sys.platform == 'win32':
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


# ── 数据装载 ─────────────────────────────────────────────────
def _extract_returns(data):
    """从多种 JSON 结构里抽出逐笔收益（百分数）列表。"""
    trades = None
    if isinstance(data, list):
        trades = data
    elif isinstance(data, dict):
        if isinstance(data.get('trade_details'), list):
            trades = data['trade_details']
        elif isinstance(data.get('trades'), list):
            trades = data['trades']

    if trades is None:
        raise ValueError("JSON 中未找到 trades / trade_details 列表")

    rs = []
    for t in trades:
        if isinstance(t, (int, float)):
            rs.append(float(t))
            continue
        if isinstance(t, dict):
            v = t.get('return_t1')
            if v is None:
                v = t.get('ret')
            if v is None:
                v = t.get('ret_t1')
            if v is None:
                v = t.get('profit_ratio')   # 兼容 EP007/Freqtrade 小数口径
            if v is not None:
                rs.append(float(v))
    if not rs:
        raise ValueError("未从输入中提取到任何逐笔收益（字段应为 return_t1/ret/ret_t1/profit_ratio）")
    return rs


def load_returns_from_json(path):
    with open(path, 'r', encoding='utf-8') as f:
        data = json.load(f)
    return _extract_returns(data)


def _load_from_store():
    """从 backtest_store 最近一次 run 取 trade_details.return_t1（百分数）。"""
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
            "SELECT return_t1 FROM backtest_trades WHERE run_id=? ORDER BY id",
            (rid,)).fetchall()
    finally:
        conn.close()
    if not rows:
        raise ValueError(f"最近一次 run#{rid} 没有逐笔交易明细（0 交易）")
    rs = [float(x[0]) for x in rows if x[0] is not None]
    if not rs:
        raise ValueError(f"最近一次 run#{rid} 的逐笔明细无有效 return_t1")
    return rs


# ── Block Bootstrap ──────────────────────────────────────────
def block_bootstrap(r, iters=5000, block=10, seed=7, start=1000.0, unit='percent'):
    """分块有放回重采样，按块保留序列相关性。

    返回 dict（字段见模块 docstring / 验收清单）：
      n_trades, iters, block, observed_final, prob_profit,
      final_P5, final_P50, final_P95, mean_trade_ret, unit

    0 笔（空序列）时显式抛 ValueError——调用方据此非 0 退出，不输出 0。
    """
    r = np.asarray(r, dtype=float)
    n = len(r)
    if n == 0:
        raise ValueError("逐笔收益序列为空（0 笔交易），无法做 Bootstrap；"
                         "请检查输入或回测是否真的有成交。")

    if unit == 'percent':
        mult = 1.0 + r / 100.0
    elif unit == 'decimal':
        mult = 1.0 + r
    else:
        raise ValueError(f"未知 unit: {unit!r}（应为 'percent' 或 'decimal'）")

    # 原始曲线终值
    observed_final = float(start * np.prod(mult))

    rng = np.random.default_rng(seed)
    finals = np.empty(iters, dtype=float)
    max_start = max(1, n - block + 1)
    for k in range(iters):
        idx = []
        while len(idx) < n:
            s = int(rng.integers(0, max_start))   # 随机块起点（moving-block）
            idx.extend(range(s, min(s + block, n)))
        idx = np.asarray(idx[:n], dtype=int)
        eq = start * np.prod(mult[idx])
        finals[k] = float(eq)

    finals.sort()
    p5 = float(np.percentile(finals, 5))
    p50 = float(np.percentile(finals, 50))
    p95 = float(np.percentile(finals, 95))
    prob_profit = float(np.mean(finals > start))

    return {
        'n_trades': int(n),
        'iters': int(iters),
        'block': int(block),
        'observed_final': round(observed_final, 4),
        'prob_profit': round(prob_profit, 4),
        'final_P5': round(p5, 4),
        'final_P50': round(p50, 4),
        'final_P95': round(p95, 4),
        'mean_trade_ret': round(float(np.mean(r)), 4),
        'unit': unit,
    }


# ── 可选出图 ─────────────────────────────────────────────────
def _maybe_plot(path, r, res, start, seed, block):
    if not path:
        return
    try:
        import matplotlib
        matplotlib.use('Agg')
        import matplotlib.pyplot as plt
        plt.rcParams['axes.unicode_minus'] = False

        rng = np.random.default_rng(seed)
        n = len(r)
        max_start = max(1, n - block + 1)
        paths = []
        keep = min(400, res['iters'])
        for _ in range(keep):
            idx = []
            while len(idx) < n:
                s = int(rng.integers(0, max_start))
                idx.extend(range(s, min(s + block, n)))
            idx = np.asarray(idx[:n])
            mult = (1.0 + np.asarray(r) / 100.0) if res['unit'] == 'percent' \
                else (1.0 + np.asarray(r))
            paths.append(start * np.cumprod(mult[idx]))

        mult = (1.0 + np.asarray(r) / 100.0) if res['unit'] == 'percent' \
            else (1.0 + np.asarray(r))
        orig = start * np.cumprod(mult)

        fig, ax = plt.subplots(figsize=(10, 5.5), dpi=130)
        for eq in paths:
            ax.plot(eq, color='#3fb6ff', alpha=0.05, lw=0.7)
        ax.plot(orig, color='#ffd24d', lw=2.4, label='原始曲线')
        ax.axhline(start, color='#888', lw=0.8, ls='--', alpha=0.6)
        ax.set_title(f"Block Bootstrap MC · {res['iters']} 路径 · "
                     f"prob(profit)={res['prob_profit']*100:.0f}%")
        ax.set_xlabel('trade #')
        ax.set_ylabel(f'equity (start {start:.0f})')
        ax.legend(loc='upper left')
        ax.text(0.985, 0.06,
                f"P5={res['final_P5']:.0f}  P50={res['final_P50']:.0f}  "
                f"P95={res['final_P95']:.0f}",
                transform=ax.transAxes, ha='right', fontsize=10)
        plt.tight_layout()
        plt.savefig(path)
        print(f"[mc] 图已写出: {path}")
    except Exception as e:   # 缺 matplotlib 或被禁用时优雅跳过，不报错
        print(f"[mc] 跳过出图（matplotlib 不可用或被禁用）: {str(e)[:80]}")


def main(argv=None):
    ap = argparse.ArgumentParser(description='Block Bootstrap 蒙特卡洛（A 股单边做多）')
    ap.add_argument('--trades', default=None,
                    help='逐笔收益 JSON 路径（见模块 docstring 结构）')
    ap.add_argument('--from-store', action='store_true',
                    help='从 backtest_store 最近一次 run 取 return_t1')
    ap.add_argument('--iters', type=int, default=5000)
    ap.add_argument('--block', type=int, default=10)
    ap.add_argument('--seed', type=int, default=7)
    ap.add_argument('--start', type=float, default=1000.0)
    ap.add_argument('--unit', choices=['percent', 'decimal'], default='percent',
                    help='逐笔收益口径：percent(默认, 1+r/100) 或 decimal(1+r)')
    ap.add_argument('--out', default=None, help='summary JSON 落盘路径')
    ap.add_argument('--plot', default=None, help='png 图输出路径（缺 matplotlib 时跳过）')
    a = ap.parse_args(argv)

    try:
        if a.from_store:
            rs = _load_from_store()
        elif a.trades:
            rs = load_returns_from_json(a.trades)
        else:
            print("[mc] 错误：必须提供 --trades <json> 或 --from-store")
            return 2

        if len(rs) == 0:
            print("[mc] 错误：逐笔收益为空（0 笔交易）。"
                  "当前回测恒为 0 交易属已知现象，请先产生真实成交或传入合成数据。")
            return 2

        res = block_bootstrap(rs, iters=a.iters, block=a.block,
                              seed=a.seed, start=a.start, unit=a.unit)
    except (ValueError, FileNotFoundError) as e:
        # 0 交易 / 数据缺失 → 明确提示 + 非 0 退出，不输出误导性 0
        print(f"[mc] 无法执行 Block Bootstrap: {e}")
        return 2

    print(f"[mc] 口径: 逐笔收益以 {res['unit']} 计")
    print(json.dumps(res, ensure_ascii=False, indent=2))

    if a.out:
        with open(a.out, 'w', encoding='utf-8') as f:
            json.dump(res, f, ensure_ascii=False, indent=2)
        print(f"[mc] summary -> {a.out}")

    _maybe_plot(a.plot, rs, res, a.start, a.seed, a.block)
    return 0


if __name__ == '__main__':
    sys.exit(main())
