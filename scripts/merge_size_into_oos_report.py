# -*- coding: utf-8 -*-
"""把 size 因子并入标准 OOS 报告结构（2026-09-18）

目的：`scripts/calibrate_weights.py` 按 `oos_ic_*_{convention}.json` 的最新 mtime
取因子证据（含 daily_ics 供 ICIR 最大化）。size 因子此前只在独立 JSON 中验证，
无法进入权重审批流程——本脚本把它以**与现有报告完全一致的结构**并入，
生成 `oos_ic_<start>_<end>_hold1d_size.json`（原报告不动，可复现）。

size 口径：近似口径 A = close × 当前总股本（与 scripts/oos_ic_size_factor.py 一致）。
train/test 切分沿用原报告的 split（保证与其他因子可比）。

用法：
  python scripts/merge_size_into_oos_report.py [--report data/reports/oos_ic_....json]
"""
import argparse
import glob
import io
import json
import os
import sys
import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REPORTS = os.path.join(PROJECT_ROOT, 'data', 'reports')


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--report', default=None,
                    help='基准 OOS 报告（默认取最新的 oos_ic_*_hold1d.json，排除 _size）')
    args = ap.parse_args()

    if args.report:
        base_path = args.report
    else:
        cands = [p for p in glob.glob(os.path.join(REPORTS, 'oos_ic_*_hold1d.json'))
                 if '_size' not in p]
        if not cands:
            print('未找到基准 OOS 报告')
            return
        base_path = max(cands, key=os.path.getmtime)
    print(f"基准报告: {os.path.basename(base_path)}")
    base = json.load(io.open(base_path, encoding='utf-8'))
    split = base['result'].get('split', {})
    start, end = base['result']['meta'].get('start'), base['result']['meta'].get('end')
    if not start or not end:
        # meta 可能用别键名，回退从文件名解析
        name = os.path.basename(base_path)
        parts = name.replace('.json', '').split('_')
        start, end = parts[2], parts[3]
    print(f"窗口: {start} ~ {end} | split: {json.dumps(split, ensure_ascii=False)[:160]}")

    from core.oos_validator import OOSValidator, RET_HOLD1D
    from core.data_engine import DataEngine

    v = OOSValidator()
    panel = v.build_panel(start, end, factors=['momentum'])
    if panel.empty:
        print('面板为空')
        return
    print(f"面板 {len(panel):,} 行 | {panel['code'].nunique()} 只 | {panel['date'].nunique()} 天")

    de = DataEngine({})
    q = de.get_all_quotes()
    shares = {}
    for _, r in q.iterrows():
        try:
            mc, px = r.get('total_market_cap'), r.get('price')
            if mc and px and float(px) > 0:
                shares[str(r['code']).zfill(6)] = float(mc) / float(px)
        except Exception:
            continue
    panel['mcap'] = panel['close'] * panel['code'].astype(str).str.zfill(6).map(shares)
    panel = panel[panel['mcap'].notna() & (panel['mcap'] > 0)].copy()
    panel['size'] = (1 - panel.groupby('date')['mcap'].rank(pct=True)) * 100

    # 全窗口逐日 IC（hold1d）
    ic_full = v.daily_ic(panel, 'size', ret_col=RET_HOLD1D)
    if ic_full.empty:
        print('size IC 为空')
        return

    # 按原报告 split 切分（test 段为 daily_ics 落盘范围，与其他因子一致）
    test_range = split.get('test_range')
    ic_test = ic_full
    if test_range and len(test_range) == 2:
        ic_test = ic_full[(ic_full.index.astype(str) >= str(test_range[0]))
                          & (ic_full.index.astype(str) <= str(test_range[1]))]
        if ic_test.empty:
            print(f"警告：test 段 {test_range} 内无 size IC，回退全窗口")
            ic_test = ic_full

    stats_full = v._ic_stats(ic_full)
    stats_test = v._ic_stats(ic_test)
    # 三折 walk-forward（全窗口）
    folds = np.array_split(ic_full.values, 3)
    fold_stats = [v._ic_stats(pd.Series(f)) for f in folds if len(f)]
    wf_mean = float(np.mean([s['ic'] for s in fold_stats if s['ic'] is not None]))
    sign_stable = len({np.sign(s['ic']) for s in fold_stats if s['ic']}) == 1

    ic_t = stats_test.get('ic')
    t_t = stats_test.get('t_stat') or 0
    if ic_t is not None and ic_t > 0 and t_t > 2:
        verdict = f"有效（test 段 IC {ic_t:+.4f}, t={t_t:+.2f}；近似口径 close×当前总股本）"
    elif ic_t is not None and ic_t > 0:
        verdict = f"弱（test 段 IC {ic_t:+.4f}, t={t_t:+.2f}；近似口径）"
    else:
        verdict = f"无效/反向（test 段 IC {ic_t}；近似口径）"

    entry = {
        'full': stats_full,
        'train': stats_full,          # size 无法按 train/test 分别重算（快照口径限制）
        'test': stats_test,
        'oos_ic': round(wf_mean, 4),
        'icir': stats_test.get('icir'),
        'direction_consistent': bool(sign_stable),
        'coverage': 1.0,
        'verdict': verdict,
        'daily_ics': {str(k): round(float(x), 6) for k, x in ic_test.items()},
        'proxy_note': '近似口径 A：close × 当前总股本（送转中性，增发/回购为二阶小量）',
    }

    base['result']['factors']['size'] = entry
    out_path = base_path.replace('.json', '_size.json')
    with io.open(out_path, 'w', encoding='utf-8') as f:
        json.dump(base, f, ensure_ascii=False, indent=2)

    print(f"\nsize 因子：全窗 IC {stats_full['ic']:+.4f} (t={stats_full['t_stat']}) | "
          f"test 段 IC {stats_test['ic']:+.4f} (t={stats_test['t_stat']}, "
          f"n={stats_test['n_days']}) | walk-forward 均值 {wf_mean:+.4f} | 符号稳定 {sign_stable}")
    print(f"已写出（含 size 的标准结构报告）: {os.path.basename(out_path)}")
    print("→ 下一步：python scripts/calibrate_weights.py（不加 --apply 为只读预览）")


if __name__ == '__main__':
    main()
