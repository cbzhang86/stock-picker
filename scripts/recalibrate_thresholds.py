# -*- coding: utf-8 -*-
"""
动态门槛重校器（2026-09-19 全项目深查 P1-3 的修复件）

背景：动态门槛（强市 65 / 中性 70 / 弱市 75）是按**旧权重体系**的评分尺度定的。
2026-09-19 生效的方案 G 把 hot_theme 从 0.42 提到 0.55、reversal 从 0.42 降到 0.10，
分数分布整体上移（热点股底座 0.42×70≈29.4 → 0.55×70≈38.5），
"同样的分数更容易过线" → 推荐数量/弱市保护/零推荐行为会静默变化。

**为什么不在本次直接改门槛**：改门槛是语义变更，若无数据支撑就是"拍数字"——
正是本项目反复踩过的坑（未经 OOS 验证的改动）。故本脚本提供两条可验证路径：

  路径 A（首选，随数据积累自动可用）：读 data/reports/run_context_*.json
      （T5 已持久化：档位 / 生效门槛 / 推荐分数），按档位统计通过率与分数线，
      用**分位匹配**反推等效门槛（保持切换前的通过率语义）。
      要求：同档位 ≥15 个交易日。

  路径 B（即时可用，指示性）：用 OOS 面板（kline_cache 全市场 2024-01~）
      在旧权重 A 与 G 下分别算每日综合分，比较同一分位点的分数位移，
      给出门槛需要平移的方向与幅度区间（指示性，非最终值）。

用法：
  python scripts/recalibrate_thresholds.py --from-run-context
  python scripts/recalibrate_thresholds.py --oos-proxy
"""
import argparse
import glob
import json
import os
import sys

import numpy as np
import pandas as pd

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)

RUN_CONTEXT_GLOB = os.path.join(PROJECT_ROOT, 'data', 'reports', 'run_context_*.json')
PANEL_CACHE = os.path.join(PROJECT_ROOT, 'data', 'cache',
                           '_tmp_panel_2023-10_2026-09.pkl')
# 档位标签与 short_term 的 LEVEL_* 常量一致（'强市'/'中性市'/'弱市'），
# 门槛从短_term.DEFAULT_DYNAMIC_MIN_SCORE 对齐（此处硬编码以便离线运行，测试锁一致性）
CURRENT_THRESHOLDS = {'强市': 65, '中性市': 70, '弱市': 75}
MIN_DAYS = 15

W_OLD = {'hot_theme': .42, 'reversal_20d': .42, 'momentum': .03,
         'technical': .03, 'volume_price': .03, 'dragon_tiger': .02}
W_NEW = {'hot_theme': .55, 'liq_dev': .14, 'reversal_20d': .10, 'vol_dev': .07,
         'volatility': .06, 'momentum': .02, 'technical': .02,
         'volume_price': .02, 'dragon_tiger': .01, 'capital_flow': .01}
PANEL_FACTORS = ['hot_theme', 'liq_dev', 'reversal_20d', 'vol_dev', 'volatility',
                 'momentum', 'technical', 'volume_price', 'dragon_tiger']


# ── 路径 A：run_context 实测 ────────────────────────────

def from_run_context():
    files = sorted(glob.glob(RUN_CONTEXT_GLOB))
    if not files:
        print(f"[A] 未找到 {RUN_CONTEXT_GLOB}（run_context 自 2026-09-17 起写入）")
        return 1
    buckets = {}
    for fp in files:
        try:
            with open(fp, encoding='utf-8') as f:
                d = json.load(f)
        except Exception:
            continue
        lvl = (d.get('market') or {}).get('level') or d.get('level')
        thr = d.get('thresholds') or {}
        eff = thr.get('effective_min_score', d.get('eff_min_score'))
        recs = d.get('recommended') or []
        if lvl is None or eff is None:
            continue
        scores = [r.get('score') for r in recs
                  if isinstance(r, dict) and isinstance(r.get('score'), (int, float))]
        buckets.setdefault(lvl, []).append({'eff': eff, 'n': len(recs), 'scores': scores})

    if not buckets:
        print("[A] run_context 文件存在但缺少 level / eff_min_score 字段，无法统计")
        return 1

    print("=" * 74)
    print("路径 A：run_context 实测统计")
    print("=" * 74)
    ready = False
    for lvl, rows in sorted(buckets.items()):
        n_days = len(rows)
        n_pass = sum(1 for r in rows if r['n'] > 0)
        all_scores = [s for r in rows for s in r['scores']]
        top = [max(r['scores']) for r in rows if r['scores']]
        gap = [max(r['scores']) - r['eff'] for r in rows if r['scores']]
        print(f"\n档位 [{lvl}]  门槛={CURRENT_THRESHOLDS.get(lvl, '?')}  "
              f"样本 {n_days} 天（需 ≥{MIN_DAYS}）")
        print(f"  有推荐天数: {n_pass}/{n_days}"
              f"（通过率 {n_pass / n_days:.0%}）")
        if all_scores:
            print(f"  推荐分数: 均值 {np.mean(all_scores):.1f} / "
                  f"中位 {np.median(all_scores):.1f} / 最低 {min(all_scores):.1f}")
        if top:
            print(f"  当日最高分: 均值 {np.mean(top):.1f}（门槛 {CURRENT_THRESHOLDS.get(lvl)}）")
        if gap:
            print(f"  最高分−门槛: 均值 {np.mean(gap):+.1f} 分")
        if n_days >= MIN_DAYS and top:
            # 分位匹配：保持与旧体系相近的通过率（旧体系下"当日最高分≥门槛"比例）
            q = np.percentile(top, 40)
            print(f"  → 分位匹配建议门槛 ≈ {q:.0f} 分（保持约 60% 天数有推荐）")
            ready = True
    if not ready:
        print(f"\n[A] 样本不足（各档位均需 ≥{MIN_DAYS} 天）→ 暂不产出最终门槛，"
              f"请继续积累 run_context 后用本命令复算。")
        return 1
    return 0


# ── 路径 B：OOS 面板代理 ───────────────────────────────

def oos_proxy(top_n=5, pool=200):
    if not os.path.exists(PANEL_CACHE):
        print(f"[B] 面板缓存缺失：{PANEL_CACHE}")
        print("    先运行任意 OOS 分析脚本（如 scripts/run_backtest.py --mode oos "
              "会另存正式报告），或复用 _tmp 分析脚本重建缓存。")
        return 1
    df = pd.read_pickle(PANEL_CACHE)
    # 面板缓存可能不含 liq_dev / vol_dev（它们是 2026-09-19 才进 OOS 面板的）。
    # 缓存保留了 amount / close 原始列 → 在脚本内按 OOS 同口径补齐，
    # 否则 G 侧估算会静默丢掉 0.21 权重（失真）。
    if 'liq_dev' not in df.columns and 'amount' in df.columns:
        _code = df['code']
        _amt = np.log1p(df['amount'].clip(lower=0))
        _level = (_amt.groupby(_code, sort=False).rolling(60, min_periods=30).median()
                  .reset_index(level=0, drop=True))
        df['liq_dev'] = 100.0 - ((_amt - _level).groupby(df['date']).rank(pct=True) * 100)
    if 'vol_dev' not in df.columns and 'close' in df.columns:
        _code = df['code']
        _ret = df.groupby('code', sort=False)['close'].pct_change()
        _vol = (_ret.groupby(df['code'], sort=False).rolling(20).std()
                .reset_index(level=0, drop=True))
        _vlevel = (_vol.fillna(0).groupby(df['code'], sort=False)
                   .rolling(60, min_periods=30).median().reset_index(level=0, drop=True))
        df['vol_dev'] = 100.0 - ((_vol - _vlevel).groupby(df['date']).rank(pct=True) * 100)
    fac = [f for f in PANEL_FACTORS if f in df.columns]
    missing = [f for f in PANEL_FACTORS if f not in df.columns]
    print(f"[B] 面板 {len(df)} 行 / {df['date'].nunique()} 天 / 因子 {len(fac)} 个")
    if missing:
        print(f"    ⚠️ 缺因子 {missing} —— 其权重会在两侧等比归一，估算精度下降")
    print("    注：面板因子为横截面百分位（0-100），与实盘因子分同尺度；"
          "本估算给出**位移方向与幅度**，非最终门槛值。")

    for c in fac:
        df[c] = df.groupby('date')[c].rank(pct=True) * 100
    df[fac] = df[fac].fillna(50.0)
    df['_src'] = df[fac].mean(axis=1)   # 粗代理：当日候选池的"典型分数"参照

    def daily_scores(w):
        wv = {k: v for k, v in w.items() if k in fac}
        tot = sum(wv.values())
        sc = sum(df[k] * (v / tot) for k, v in wv.items())
        return df.assign(_s=sc)

    need = set(list(W_OLD) + list(W_NEW))
    df = df.dropna(subset=[c for c in need if c in df.columns]).copy()
    out = {}
    for tag, w in (('旧权重A', W_OLD), ('新权重G', W_NEW)):
        d = daily_scores(w)
        tops, q60s, q80s = [], [], []
        for _, g in d.groupby('date', sort=True):
            if len(g) < 100:
                continue
            # 候选池代理：按旧权重综合分取前 pool 只（近似"进入详评"的集合）
            s_all = g['_s'].to_numpy()
            k = min(pool, len(s_all))
            idx = np.argpartition(-s_all, k - 1)[:k]
            pool_s = s_all[idx]
            tops.append(pool_s.max())
            q60s.append(np.percentile(pool_s, 60))
            q80s.append(np.percentile(pool_s, 80))
        out[tag] = dict(top=float(np.mean(tops)), q60=float(np.mean(q60s)),
                        q80=float(np.mean(q80s)), tops_list=tops)
    out['_tops_a'] = out['旧权重A']['tops_list']
    out['_tops_g'] = out['新权重G']['tops_list']

    a, g = out['旧权重A'], out['新权重G']
    print("\n" + "=" * 74)
    print("路径 B：同一分位点在旧/新权重下的分数（候选池前 200 只代理）")
    print("=" * 74)
    print(f"{'指标':<22}{'旧权重A':>12}{'新权重G':>12}{'位移':>12}")
    for k, label in (('top', '当日最高分'), ('q60', '池内 60 分位'),
                     ('q80', '池内 80 分位')):
        print(f"{label:<22}{a[k]:>12.1f}{g[k]:>12.1f}{g[k] - a[k]:>+12.1f}")

    # 通过率代理：当日池内最高分 ≥ 门槛 的天数占比（决定"是否有推荐"）
    print("\n" + "=" * 74)
    print("通过率代理：当日候选池最高分 ≥ 门槛 的天数占比（决定当日是否有推荐）")
    print("=" * 74)
    print(f"{'门槛':>6}{'旧权重A':>14}{'新权重G':>14}{'变化':>12}")
    for t in (58, 60, 62, 65, 68, 70, 72, 75):
        pa = float(np.mean([m >= t for m in out['_tops_a']]))
        pg = float(np.mean([m >= t for m in out['_tops_g']]))
        mark = ' ←弱市' if t == 75 else (' ←中性/强市' if t in (65, 70) else '')
        print(f"{t:>6}{pa:>13.1%}{pg:>14.1%}{pg - pa:>+12.1%}{mark}")

    print(f"\n指示性结论：分数分布不是整体平移而是**分化**——")
    print(f"  上尾（热点股簇）上移，下体（非热点股）下移。池内 80 分位 {a['q80']:.1f}→{g['q80']:.1f}，")
    print(f"  池内 60 分位 {a['q60']:.1f}→{g['q60']:.1f}。故**不能用单一偏移量重设门槛**。")
    print("  决策依据应为上表的通过率变化：若高门槛（弱市 75）通过率上升，即为"
          "「弱市保护减弱」的量化证据。")
    print("  ⚠️ 最终门槛应以路径 A 的实测通过率为准（≥15 个交易日 run_context 数据）。")
    return 0


def main():
    p = argparse.ArgumentParser(description='动态门槛重校（P1-3）')
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument('--from-run-context', action='store_true', help='读实测 run_context')
    g.add_argument('--oos-proxy', action='store_true', help='OOS 面板尺度位移估算')
    a = p.parse_args()
    return from_run_context() if a.from_run_context else oos_proxy()


if __name__ == '__main__':
    sys.exit(main())
