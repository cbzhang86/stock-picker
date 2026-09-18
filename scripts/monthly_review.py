# -*- coding: utf-8 -*-
"""月度滚动复检（2026-09-18 P5③）——IC 趋势监测 + 降权提案（只读，不自动改权重）

做三件事：
  1. 读取最新 OOS 报告（`data/reports/oos_ic_*_hold1d.json`）的逐日 IC；
  2. 逐月分解 → 检测「连续 N 个月 IC 下降」的因子（默认 N=2，可配）；
  3. 生成降权提案（advisory：如 hot_theme 0.42 → 0.35），写入
     `data/reports/monthly_review_YYYY-MM.md`。

**输入 schema 约定（2026-09-18 审查补充）**：本脚本只消费
`python scripts/run_backtest.py --mode oos` 产出的报告（其结构为
`result.factors.<因子>.daily_ics`）。独立因子脚本（`oos_ic_size_factor.py` /
`oos_ic_valuation_factor.py`）的输出结构不同（只有 IC 统计、无 daily_ics），
直接喂入会因取不到 daily_ics 而**静默给出"无因子下降"的假阴性**——若需纳入，
先合并进标准报告（见 `scripts/merge_size_into_oos_report.py`）。

**硬约束**：本脚本只产出提案，**不写 v1.json**。权重变更必须人工审批后走
`scripts/calibrate_weights.py --apply`（项目既定规则）。

用法：
  python scripts/monthly_review.py                      # 用最新 OOS 报告
  python scripts/monthly_review.py --months 3           # 检测连续 3 月下降
  python scripts/monthly_review.py --report <path>      # 指定报告
"""
import argparse
import glob
import io
import json
import os
import sys
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REPORTS = os.path.join(PROJECT_ROOT, 'data', 'reports')
WEIGHTS = os.path.join(PROJECT_ROOT, 'data', 'weights', 'v1.json')

# 检测范围：生效权重中的因子（其余因子无权重可谈）
DECAY_FLOOR = 0.02      # 当前权重低于此值的因子不参与降权提案（已在探索位）
DECAY_STEP = 0.07       # 单次降权幅度（0.42 → 0.35）
DECAY_MIN_IC_DROP = 0.0  # 月度 IC 严格下降才算


def monthly_ic(daily_ics: dict) -> dict:
    """{date: ic} → {YYYY-MM: [ic, ...]}"""
    out = {}
    for d, v in (daily_ics or {}).items():
        # 先转换后建键：避免 setdefault 先执行导致"全无效值月份"留下空列表
        try:
            fv = float(v)
        except (TypeError, ValueError):
            continue
        out.setdefault(str(d)[:7], []).append(fv)
    return {m: vs for m, vs in sorted(out.items())}


def detect_decay(monthly: dict, months: int = 2) -> dict:
    """返回 {month: 月均 IC} 与是否连续 `months` 个月下降。"""
    means = {m: sum(vs) / len(vs) for m, vs in monthly.items() if vs}
    if len(means) < months + 1:
        return {'means': means, 'decaying': False, 'reason': f'月份不足（{len(means)}）'}
    ms = sorted(means)
    tail = ms[-(months + 1):]
    vals = [means[m] for m in tail]
    declining = all(vals[i + 1] < vals[i] - DECAY_MIN_IC_DROP for i in range(len(vals) - 1))
    return {'means': means, 'decaying': declining, 'tail': tail,
            'tail_values': vals,
            'reason': ('连续 %d 月下降 %s' % (months, [round(v, 4) for v in vals]))
                      if declining else '未见连续下降'}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--report', default=None, help='OOS 报告路径（默认取最新 hold1d）')
    ap.add_argument('--months', type=int, default=2, help='连续下降月数阈值（默认 2）')
    args = ap.parse_args()

    if args.report:
        path = args.report
    else:
        cands = [p for p in glob.glob(os.path.join(REPORTS, 'oos_ic_*_hold1d.json'))
                 if '_size' not in p]
        if not cands:
            print('未找到 OOS 报告（先跑 scripts/run_backtest.py --mode oos）')
            return 1
        path = max(cands, key=os.path.getmtime)
    print(f"OOS 报告: {os.path.basename(path)}")

    data = json.load(io.open(path, encoding='utf-8'))
    factors = (data.get('result') or {}).get('factors') or {}
    try:
        weights = json.load(io.open(WEIGHTS, encoding='utf-8'))['short']
    except Exception:
        weights = {}

    lines = [f"# 月度滚动复检 — {datetime.now().strftime('%Y-%m-%d')}", "",
             f"- 数据源：`{os.path.basename(path)}`",
             f"- 检测规则：连续 **{args.months}** 个月月均 IC 下降 → 生成降权提案",
             f"- 单次降权幅度：{DECAY_STEP}（提案值，需人工审批）", ""]

    proposals = []
    lines.append("## 因子月度 IC 趋势")
    lines.append("")
    lines.append("| 因子 | 当前权重 | 月均 IC（近 6 月） | 判定 |")
    lines.append("|---|---|---|---|")
    for f, v in sorted(factors.items()):
        dics = v.get('daily_ics') or {}
        if not dics:
            continue
        monthly = monthly_ic(dics)
        info = detect_decay(monthly, args.months)
        means = info['means']
        recent = list(sorted(means))[-6:]
        trend = ' '.join(f"{m[2:]}:{means[m]:+.3f}" for m in recent)
        w = weights.get(f)
        verdict = '持平/上升'
        if info['decaying']:
            verdict = f"⚠ **连续 {args.months} 月下降**（{info['reason']}）"
        lines.append(f"| {f} | {w if w is not None else '—'} | {trend} | {verdict} |")

        # 降权提案（仅对已生效权重、且不在探索位的因子）
        if info['decaying'] and isinstance(w, (int, float)) and w >= DECAY_FLOOR:
            new_w = max(0.0, round(w - DECAY_STEP, 2))
            proposals.append({'factor': f, 'current': w, 'proposed': new_w,
                              'why': info['reason']})
    lines.append("")

    lines.append("## 降权提案（advisory，需人工审批）")
    lines.append("")
    if proposals:
        lines.append("| 因子 | 当前 | 提案 | 依据 |")
        lines.append("|---|---|---|---|")
        for p in proposals:
            lines.append(f"| {p['factor']} | {p['current']} | **{p['proposed']}** | {p['why']} |")
        lines.append("")
        lines.append("审批后执行：`python scripts/calibrate_weights.py --apply`"
                     "（会先备份 v1.json），或手工改 `data/weights/v1.json` + 同步 config.yml + "
                     "跑 `tests/test_weight_alignment_20260914.py` 与全量测试。")
    else:
        lines.append("无（无因子满足连续下降条件）")
    lines.append("")

    lines.append("## 说明")
    lines.append("")
    lines.append("- 本复检**只读**：不修改任何权重文件；")
    lines.append("- 权重变更的唯一入口是 `calibrate_weights.py --apply`（人工审批）；")
    lines.append("- 建议每月初执行一次，结果与本文件历史版本对比看趋势。")

    out = os.path.join(REPORTS, f"monthly_review_{datetime.now().strftime('%Y-%m')}.md")
    with io.open(out, 'w', encoding='utf-8') as f:
        f.write('\n'.join(lines))
    print(f"复检报告已生成: {out}")
    if proposals:
        print(f"⚠ 降权提案 {len(proposals)} 项: "
              + ', '.join(f"{p['factor']} {p['current']}→{p['proposed']}" for p in proposals))
    else:
        print("无降权提案（无因子连续下降）")
    return 0


if __name__ == '__main__':
    sys.exit(main())
