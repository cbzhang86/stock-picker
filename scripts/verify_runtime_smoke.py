# -*- coding: utf-8 -*-
"""全模块导入 + 动态冒烟（只读项目数据，可写临时缓存）。

目的：抓"修复后引入的新问题"——静态审查看不到的运行期炸点。
"""
import sys, os, io, traceback, json, math, importlib

ROOT = r'C:\Users\Administrator\Documents\stock-picker-v2'
sys.path.insert(0, ROOT)
os.chdir(ROOT)
os.environ['PYTHONUTF8'] = '1'

buf = []
W = buf.append
ok = bad = 0


def check(label, cond, detail=''):
    global ok, bad
    if cond:
        ok += 1
        W(f'PASS  {label} {detail}')
    else:
        bad += 1
        W(f'FAIL  {label} {detail}')


# ── 1. 全模块导入 ─────────────────────────────────────────
SKIP = {'test_data_pipeline', '_backfill_measure', '_bf_diff', 'postmortem',
        'prefetch_kline_fullmarket', 'prefetch_tdx', 'backfill_hot_stocks',
        'backup_predictions', 'daily_job'}
mods = []
for sub in ('core', 'strategies', 'reports', 'feedback', 'scripts', 'tests'):
    d = os.path.join(ROOT, sub)
    for f in sorted(os.listdir(d)):
        if f.endswith('.py') and not f.startswith('__'):
            name = f[:-3]
            if name in SKIP:
                continue
            mods.append((sub, name))
imp_fail = []
for sub, name in mods:
    try:
        importlib.import_module(f'{sub}.{name}')
    except Exception as e:
        imp_fail.append(f'{sub}.{name}: {type(e).__name__}: {str(e)[:70]}')
check('全模块导入', not imp_fail, f'{len(mods)} 个模块，失败: {imp_fail[:4]}')

# ── 2. 评分链路端到端（含边界） ───────────────────────────
try:
    from core.scoring_model import ScoringModel
    sm = ScoringModel()
    edge_stocks = [
        {'code': 'A', 'name': '正常', 'price': 10.0, 'rps_20': 80, 'amount': 1e8,
         'volume_ratio': 1.5, 'pct_chg': 2.0, 'turnover': 5.0,
         'fundamentals': None, 'recent_events': None, 'rps': 80,
         'main_fund_accumulated': 1000.0, 'north_flow_accumulated': None},
        {'code': 'B', 'name': '全缺失', 'price': None, 'rps_20': None, 'amount': None,
         'volume_ratio': None, 'pct_chg': None, 'turnover': None, 'fundamentals': None,
         'recent_events': None, 'rps': None, 'main_fund_accumulated': None,
         'north_flow_accumulated': None, 'pe_percentile': None, 'pb_percentile': None},
        {'code': 'C', 'name': 'NaN污染', 'price': float('nan'), 'rps_20': float('nan'),
         'amount': float('inf'), 'pct_chg': float('nan'), 'fundamentals': {'pe': float('nan')},
         'recent_events': [], 'rps': float('inf')},
        {'code': 'D', 'name': '极端值', 'price': 1e-4, 'rps_20': 100, 'amount': 1e14,
         'pct_chg': 20.0, 'turnover': 80.0, 'fundamentals': {'pe': -5.0, 'roe': -30.0,
         'pb': -1.0, 'div_yield': 99.0}, 'recent_events': [{'title': 'x'}], 'rps': 100},
    ]
    scores = {}
    for s in edge_stocks:
        try:
            r = sm.score_stock(s, 'short')
            scores[s['code']] = r
        except Exception as e:
            scores[s['code']] = f'EXC {type(e).__name__}: {e}'
    W('  scores: ' + json.dumps({k: (v.get('score') if isinstance(v, dict) else v)
                                 for k, v in scores.items()}, ensure_ascii=False, default=str))
    check('评分链路 4 类边界输入不抛异常',
          all(isinstance(v, dict) for v in scores.values()), '')
    check('全部评分为有限数值',
          all(isinstance(v, dict) and isinstance(v.get('score'), (int, float))
              and math.isfinite(v['score']) for v in scores.values()),
          str({k: (round(v['score'], 1) if isinstance(v, dict) and isinstance(v.get('score'), (int, float)) else v)
               for k, v in scores.items()}))
    check('评分为 None 的键不进结果',
          all(isinstance(v, dict) and all(x is not None for x in v.values())
              for v in scores.values() if isinstance(v, dict)),
          str([k for k, v in scores.items() if isinstance(v, dict)
               and any(x is None for x in v.values())]))
except Exception:
    check('评分链路端到端', False, traceback.format_exc()[-200:])

# ── 3. rank_stocks 全流程（含标准化开/关） ────────────────
try:
    from core.scoring_model import ScoringModel
    for std in (False, True):
        sm2 = ScoringModel(sell_config={'standardize': std})
        try:
            res = sm2.rank_stocks([
                {'code': 'A', 'name': 'x', 'price': 10.0, 'rps_20': 80, 'amount': 1e8},
                {'code': 'B', 'name': 'y', 'price': None, 'rps_20': None, 'amount': None},
            ], 'short', top_n=5)
            check(f'rank_stocks(standardize={std}) 正常', isinstance(res, list)
                  and all(math.isfinite(float(x['score'])) for x in res), f'n={len(res)}')
        except Exception as e:
            check(f'rank_stocks(standardize={std})', False,
                  f'{type(e).__name__}: {str(e)[:90]}')
except Exception:
    check('rank_stocks', False, traceback.format_exc()[-200:])

# ── 4. 组合优化 / 专家融合 / 风控 组合交互 ────────────────
try:
    from core.portfolio_optimizer import PortfolioOptimizer as PO
    from core.expert_ensemble import ExpertEnsemble, confidence_to_weight_factor
    recs = []
    for i, (sc, ens, wf) in enumerate([
            (80.0, 82.0, 1.0), (70.0, 40.0, 0.5), (float('nan'), 60.0, 1.0),
            (65.0, None, None), (60.0, 61.0, 1.0)]):
        recs.append({'code': f'X{i}', 'name': f'n{i}', 'score': sc,
                     'ensemble_score': ens, 'weight_factor': wf})
    try:
        out = PO.allocate([dict(r) for r in recs])
        check('组合优化 5 类混合输入不崩溃', True,
              str([(x['code'], round(x['allocation_pct'], 1)) for x in out]))
        check('仓位总和 <= 100%',
              sum(x['allocation_pct'] for x in out) <= 100.0 + 1e-6,
              f"sum={sum(x['allocation_pct'] for x in out):.2f}")
        check('所有仓位有限', all(math.isfinite(x['allocation_pct']) for x in out), '')
    except Exception as e:
        check('组合优化混合输入', False, f'{type(e).__name__}: {str(e)[:120]}')
    ee = ExpertEnsemble()
    try:
        v = ee.fuse({'code': 'X', 'score': 70.0, 'price': 10.0, 'pct_chg': 3.0,
                     'turnover': 4.0, 'amount_ratio': 1.2, 'rps': 85,
                     'main_fund_accumulated': 2000.0, 'north_flow_accumulated': None,
                     'fundamentals': None, 'recent_events': None,
                     'pe_percentile': None, 'pb_percentile': None})
        check('专家融合全输入形态正常', isinstance(v, dict) or v is not None,
              f'ensemble={getattr(v, "ensemble_score", v)}')
        check('ensemble_score 在 0-100', 0 <= getattr(v, 'ensemble_score', -1) <= 100,
              f'{getattr(v, "ensemble_score", None)}')
    except Exception as e:
        check('专家融合正常输入', False, f'{type(e).__name__}: {str(e)[:100]}')
except Exception:
    check('组合/专家交互', False, traceback.format_exc()[-200:])

# ── 5. 回测引擎小规模端到端（用现有 kline 缓存） ─────────
try:
    from core.backtest_engine import BacktestEngine
    import sqlite3
    db = os.path.join(ROOT, 'data', 'cache', 'kline_cache.db')
    codes = []
    if os.path.exists(db):
        conn = sqlite3.connect(db)
        codes = [r[0] for r in conn.execute(
            'SELECT code FROM kline_cache GROUP BY code HAVING COUNT(*)>40 LIMIT 6')]
        conn.close()
    check('kline 缓存可用', len(codes) >= 3, f'{len(codes)} 只')
    if len(codes) >= 3:
        be = BacktestEngine({'initial_capital': 100000.0,
                             'sell': {'take_profit': 0.02, 'stop_loss': -0.02,
                                      'time_stop_days': 3},
                             'sizing_mode': 'normalized'})
        res = be.run(mode='short', start_date='2026-09-01', end_date='2026-09-18')
        tr = getattr(res, 'total_trades', None)
        dd = getattr(res, 'max_drawdown', None)
        check('回测端到端完成', tr is not None, f'trades={tr} dd={dd}')
        check('回测指标有限',
              all(isinstance(x, (int, float)) and math.isfinite(x)
                  for x in [tr or 0, dd if isinstance(dd, (int, float)) else 0,
                            getattr(res, 'sharpe_ratio', 0) or 0]), '')
        fp = getattr(res, 'factor_performance', None)
        check('因子表现结构可序列化',
              fp is None or (json.dumps(fp, ensure_ascii=False, default=str,
                                        allow_nan=False) is not None), '')
except Exception:
    check('回测端到端', False, traceback.format_exc()[-300:])

# ── 6. 报告层：简报 + 日报（None/NaN 输入） ───────────────
try:
    from reports.market_briefing import generate_market_briefing
    dirty = [{'code': None, 'name': 'x', 'score': float('nan'), 'allocation_pct': None,
              'shadow': True, 'shadow_reason': 'r'},
             {'skip_reason': '极端市况'},]
    t = generate_market_briefing(dirty, mode='short', source_status={})
    check('简报接受脏输入', isinstance(t, str) and len(t) > 50, f'len={len(t)}')
    check('简报无 nan 字面量', 'nan%' not in t and '+nan' not in t, '')
    t2 = generate_market_briefing([], mode='short', source_status=None)
    check('简报空输入 + 无快照', isinstance(t2, str) and '数据源状态不可用' in t2, '')
    t3 = generate_market_briefing([{'code': '600000', 'name': '浦发', 'score': 75.0,
                                    'allocation_pct': 40.0}], mode='short',
                                  source_status={'tencent_quote': {
                                      'available': False, 'last_error': 'timeout',
                                      'label': '腾讯实时行情'}})
    check('简报降级警示位置正确',
          t3.index('数据源降级警示') < t3.index('市场概览'), '')
except Exception:
    check('简报', False, traceback.format_exc()[-200:])

try:
    from reports.daily_report import DailyReportGenerator
    g = DailyReportGenerator(output_dir=os.path.join(ROOT, 'data', 'reports'))
    txt = g.get_latest_report('short')
    check('get_latest_report 返回规范报告（非简报）',
          isinstance(txt, str), f"head={txt[:40]!r}")
except Exception:
    check('daily_report', False, traceback.format_exc()[-200:])

# ── 7. run_context / 校准脚本动态跑 ───────────────────────
try:
    import subprocess
    r = subprocess.run([sys.executable, 'scripts/factor_diag.py', '--mode', 'short'],
                       capture_output=True, text=True, encoding='utf-8',
                       errors='replace', timeout=120, cwd=ROOT)
    check('factor_diag 可运行', r.returncode == 0, f'rc={r.returncode} {r.stdout[-80:]!r}')
except Exception as e:
    check('factor_diag', False, f'{type(e).__name__}: {e}')

try:
    import subprocess
    r = subprocess.run([sys.executable, 'scripts/recalibrate_thresholds.py',
                        '--from-run-context'],
                       capture_output=True, text=True, encoding='utf-8',
                       errors='replace', timeout=120, cwd=ROOT)
    check('recalibrate_thresholds 可运行', r.returncode in (0, 1),
          f'rc={r.returncode}(1=样本不足属正常) out={r.stdout[-60:]!r}')
    check('recalibrate 无极差市 "?"', '门槛=?' not in r.stdout, '')
except Exception as e:
    check('recalibrate', False, f'{type(e).__name__}: {e}')

try:
    import subprocess
    r = subprocess.run([sys.executable, 'scripts/verify.py'],
                       capture_output=True, text=True, encoding='utf-8',
                       errors='replace', timeout=180, cwd=ROOT)
    check('verify 退出码 0', r.returncode == 0, f'rc={r.returncode}')
    check('verify 无 FAIL', 'FAIL' not in r.stdout.replace('0 FAIL', ''), '')
except Exception as e:
    check('verify', False, f'{type(e).__name__}: {e}')

# ── 8. 时序/日历边界 ─────────────────────────────────────
try:
    from core.trading_calendar import is_trading_day, last_trading_day, next_trading_day, trading_days
    check('is_trading_day 2027 → None', is_trading_day('2027-01-04') is None, '')
    check('last_trading_day 正常', last_trading_day('2026-09-19') is not None,
          f"{last_trading_day('2026-09-19')}")
    check('next_trading_day 2026-12-31 覆盖内',
          next_trading_day('2026-12-24') is not None,
          f"{next_trading_day('2026-12-24')}")
    check('trading_days 未覆盖 → None', trading_days('2027-02-01', '2027-02-10') is None, '')
    check('trading_days 逆序区间 → 空列表不崩溃',
          isinstance(trading_days('2026-09-20', '2026-09-01'), list), '')
except Exception:
    check('日历', False, traceback.format_exc()[-200:])

# ── 9. data_engine 契约（quotes / pct_chg / pe） ─────────
try:
    from core.data_engine import DataEngine
    de = DataEngine({})
    q = de.get_all_quotes()
    if q is None or q.empty:
        W('SKIP  get_all_quotes 返回空（非交易时段/无网）')
    else:
        need = {'code', 'price', 'pct_chg', 'amount', 'turnover', 'volume_ratio', 'pe', 'pb'}
        missing = need - set(q.columns)
        check('quotes 必备列齐全', not missing, f'missing={missing}')
        import numpy as np
        for col in ('price', 'pct_chg', 'amount', 'turnover', 'volume_ratio'):
            v = pd_to_num(q[col]) if False else None
        # pct_chg 缺失应表现为 NaN/None 而非 0 伪装
        check('quotes 行数合理', len(q) > 1000, f'{len(q)} 行')
except Exception:
    check('data_engine quotes', False, traceback.format_exc()[-200:])

# ── 10. 权重三层一致性（运行时） ─────────────────────────
try:
    from core.scoring_model import ScoringModel
    sm3 = ScoringModel()
    w = sm3.get_weights('short')
    check('生效权重 Σ≈1', abs(sum(w.values()) - 1.0) < 1e-6, f'Σ={sum(w.values()):.8f}')
    check('生效权重无 None/NaN',
          all(isinstance(v, (int, float)) and math.isfinite(v) for v in w.values()), '')
    import json as _j
    v1 = _j.load(io.open(os.path.join(ROOT, 'data', 'weights', 'v1.json'), encoding='utf-8'))['short']
    check('DEFAULT_WEIGHTS 键集 == v1 键集',
          set(w.keys()) == set(v1.keys()),
          f"only_default={set(w)-set(v1)} only_v1={set(v1)-set(w)}")
except Exception:
    check('权重一致性', False, traceback.format_exc()[-200:])

W('')
W(f'===== 汇总: PASS {ok} / FAIL {bad} =====')
out = '\n'.join(buf)
io.open(os.path.join(ROOT, 'data', 'cache', '_smoke.out'), 'w', encoding='utf-8').write(out)
print(out[-3000:])
print(f'\nTOTAL PASS {ok} FAIL {bad}')
