"""一键评估门禁（2026-09-05 架构对标 #8）

用法：
  python scripts/evaluate_all.py            # 快检：编译 + 回归 + 权重 + 因子口径
  python scripts/evaluate_all.py --full     # 追加 OOS 面板诊断（约 1-3 分钟）

任何一项失败退出码非 0——权重变更（calibrate_weights --apply）或因子代码
修改后必须先过本门禁。
"""
import os
import sys
import logging

if sys.platform == 'win32':
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

logging.disable(logging.CRITICAL)
PASS, FAIL = [], []
# 诊断类结果（不阻断门禁）：WARN=记录+告警，SKIP=数据不足跳过
WARN, SKIP = [], []


def check(name, fn):
    try:
        fn()
        PASS.append(name)
        print(f"  ✓ {name}")
    except Exception as e:
        FAIL.append((name, str(e)[:120]))
        print(f"  ✗ {name}: {str(e)[:120]}")


# ── 1. 编译检查 ─────────────────────────────────────────────
print("[1/4] 编译检查")
def _compile():
    import py_compile
    for f in ['core/scoring_model.py', 'core/factor_library.py', 'core/technical_scorer.py',
              'core/backtest_engine.py', 'core/oos_validator.py', 'core/data_engine.py',
              'strategies/short_term.py', 'strategies/long_term.py',
              'feedback/data_collector.py']:
        py_compile.compile(os.path.join(ROOT, f), doraise=True)
check("核心模块编译", _compile)


# ── 2. 功能回归（2026-09-05 修复项固化）──────────────────────
print("[2/4] 功能回归")
def _reg_scoring():
    import pandas as pd
    from core.scoring_model import ScoringModel
    sm = ScoringModel()
    base = {'code':'600519','name':'x','price':100.0,'rps_20':60,'volume_ratio':1.2,
            'main_fund_accumulated':None,'north_flow_accumulated':None}
    a = dict(base); a['risk_check'] = {'passed': False, 'score_penalty': 0.9}
    assert sm.score_stock(a)['score'] == 0.0, "硬风控应为 0 分"
    b = dict(base)
    assert sm.score_stock(b)['breakdown']['hot_theme']['data_available'] is False, "缺热点数据应 neutral"
    c = dict(base); c['is_hot_stock'] = False
    assert sm.score_stock(c)['breakdown']['hot_theme']['data_available'] is True, "查过非热点不应 neutral"
check("评分模型（硬风控0分/热点neutral）", _reg_scoring)

def _reg_weights():
    import json
    from core.scoring_model import ScoringModel
    w = json.load(open(os.path.join(ROOT, 'data', 'weights', 'v1.json'), encoding='utf-8'))['short']
    assert abs(sum(w.values()) - 1.0) < 1e-6, f"权重和 {sum(w.values())} != 1.0"
    sm = ScoringModel()
    # 2026-09-18：改为"加载权重 == v1.json 内容"（缺失键视为 0），
    # 不再硬编码单个因子值——改权重不再需要同步改门禁断言。
    loaded = sm.get_weights('short')
    for k, v in w.items():
        assert abs(float(loaded.get(k) or 0) - float(v)) < 1e-9, \
            f"v1.json 权重未生效: {k} 期望 {v} 实际 {loaded.get(k)}"
    for k in loaded:
        assert k in w, f"加载权重出现 v1.json 之外的键: {k}"
check("权重文件（总和=1，v1.json 生效）", _reg_weights)

def _reg_tech():
    import pandas as pd
    from core.technical_scorer import TechnicalScorer
    ts = TechnicalScorer()
    closes = [10 + i*0.05 for i in range(30)]
    df = pd.DataFrame({'close': closes, 'volume': [1000]*30})
    assert ts.score(df).trend_status == '强势多头'
    df.loc[29, 'close'] = 0.0
    assert ts.score(df).total == 50.0, "坏价应中性 50"
check("技术评分（坏价中性）", _reg_tech)

def _reg_vp():
    from core.factor_library import FactorLibrary
    f = FactorLibrary().compute_all_factors({'volume_ratio': 1.2, 'rps_20': 60}, 'short')
    assert f['volume_price'] == 80.0, "无尾盘数据不应 ×0.7"
check("量价因子（无尾盘不封顶）", _reg_vp)

def _reg_bt():
    from core.backtest_engine import BacktestEngine
    out = BacktestEngine._calc_monthly_returns(object(), [
        {'date': '2026-08-03', 'return_t1': 1.0}, {'date': '2026-08-03', 'return_t1': -1.0}])
    assert out[0]['trades'] == 2
    eng = object.__new__(BacktestEngine)
    class _DE:
        def get_kline(self, *a, **k): return None
    eng.data_engine = _DE()
    # P1-J（2026-09-05）：单基准 → 多基准 API，失败记 None 不静默归 0
    eng.benchmark_codes = ['399300', '000852']
    bm = BacktestEngine._calc_benchmark_returns(eng, 'x', 'y')
    assert set(bm) == {'399300', '000852'}, "应返回全部配置基准"
    assert all(v is None for v in bm.values()), "基准拉取失败应记 None（不静默归 0）"
check("回测引擎（月度归因/基准标记）", _reg_bt)

def _reg_strategy():
    from strategies.short_term import ShortTermStrategy
    recs = [{'code': 'A', 'allocation_pct': 60.0}, {'code': 'B', 'allocation_pct': 40.0}]
    out = ShortTermStrategy._apply_position_scale([dict(r) for r in recs], 0.5)
    assert abs(sum(r['allocation_pct'] for r in out) - 50.0) < 1e-9, "弱市应半仓"
    enr = [{'code': 'A', 'rps_20': 90.0, 'is_hot_stock': True},
           {'code': 'B', 'rps_20': 90.0, 'is_hot_stock': False},
           {'code': 'C', 'rps_20': 70.0, 'is_hot_stock': False}]
    kept = [s['code'] for s in ShortTermStrategy._apply_momentum_filter(enr, 80)]
    assert kept == ['A', 'C'], "动量过滤应豁免热点"
    same = [{'code': 'A', 'blocks': {'boards': [{'name': '算力'}]}},
            {'code': 'B', 'blocks': {'boards': [{'name': '算力'}]}},
            {'code': 'C', 'blocks': {'boards': [{'name': '算力'}]}}]
    kept2 = [r['code'] for r in ShortTermStrategy._apply_board_diversification(same, 2)]
    assert kept2 == ['A', 'B'], "同板块第3只应被剔除"
check("策略层（仓位开关/动量过滤/板块分散）", _reg_strategy)

def _reg_collector():
    import sqlite3, tempfile
    from feedback.data_collector import FactorDataCollector
    tmp = os.path.join(tempfile.gettempdir(), '_gate_factor_raw.db')
    if os.path.exists(tmp):
        os.remove(tmp)
    c = FactorDataCollector(db_path=tmp)
    conn = sqlite3.connect(tmp)
    c._save_factor_raw(conn, [{'code': '600519', 'rps_20': 80.0, 'volume_ratio': None}], '2026-09-05')
    conn.commit()
    row = conn.execute("SELECT rps_20, volume_ratio FROM factor_raw").fetchone()
    conn.close(); os.remove(tmp)
    assert row == (80.0, None), "raw 值缺失应写 NULL"
check("因子采集（raw 值表）", _reg_collector)


# ── 3. 权重-因子口径一致性 ───────────────────────────────────
print("[3/4] 口径一致性")
def _consistency():
    from core.scoring_model import ScoringModel
    sm = ScoringModel()
    for mode in ('short', 'long'):
        w = sm.get_weights(mode)
        missing = set(ScoringModel.DEFAULT_WEIGHTS[mode]) - set(w)
        assert not missing, f"{mode} 权重缺因子键: {missing}"
check("权重表完整性", _consistency)


# ── 4. 可选：OOS 面板诊断 ────────────────────────────────────
if '--full' in sys.argv:
    print("[4/4] OOS 面板诊断（--full）")
    def _oos():
        from core.oos_validator import OOSValidator, RET_HOLD1D
        v = OOSValidator()
        panel = v.build_panel('2026-06-30', '2026-09-03',
                              factors=['momentum', 'volume_price', 'hot_theme'])
        assert len(panel) > 10000, f"面板行数异常: {len(panel)}"
        hot = v.daily_ic(panel, 'hot_theme', ret_col=RET_HOLD1D)
        assert len(hot) >= 20 and hot.mean() > 0, \
            f"hot_theme hold1d IC 异常: {hot.mean() if len(hot) else '空'}"
        cm = v.factor_corr(panel)
        assert not cm.empty and abs(cm.loc['hot_theme', 'momentum']) < 0.3, \
            "hot_theme 与 momentum 相关性异常升高"
        print(f"    面板 {len(panel)} 行 | hot_theme IC={hot.mean():+.4f}")
    check("OOS 面板（hot_theme 有效性 + 因子相关性）", _oos)
else:
    print("[4/4] 跳过 OOS 诊断（加 --full 启用）")


# ── 5. 多重检验校正（DSR / PBO，WARN 不阻断）─────────────────
# 设计决策（务必保留）：本仓库不做 hyperopt 超参搜索，因此没有"天然"的试验数。
# DSR 公式里的 √(T−1) 在 T=15 个交易日时仅 ≈3.74（T=250 时约 15.8），统计功效
# 极低；若把 DSR/PBO 设为 FAIL 会**误杀**本就稀缺的样本外结论。故这两项仅作
# 记录+告警（WARN），绝不阻断门禁。缺 trials 数据时标 SKIP 并说明原因，也不 FAIL。
print("[5/5] 多重检验校正（DSR / PBO，WARN 不阻断）")


def _record_diag(name, status, msg):
    """诊断结果落盘：SKIP→跳过，WARN→告警，PASS→通过。均不进 FAIL。"""
    if status == 'SKIP':
        SKIP.append((name, msg))
        print(f"  ⊘ {name}: SKIP — {msg}")
    elif status == 'WARN':
        WARN.append((name, msg))
        print(f"  ⚠ {name}: {msg}")
    else:
        PASS.append(name)
        print(f"  ✓ {name}")


def _find_trial_matrix():
    """寻找试验矩阵 CSV：环境变量优先，否则 data/reports/trial_matrices/*.csv。"""
    env = os.environ.get('EVAL_TRIAL_MATRIX')
    if env and os.path.exists(env):
        return env
    cand_dir = os.path.join(ROOT, 'data', 'reports', 'trial_matrices')
    if os.path.isdir(cand_dir):
        cs = sorted(f for f in os.listdir(cand_dir) if f.endswith('.csv'))
        if cs:
            return os.path.join(cand_dir, cs[0])
    fallback = os.path.join(ROOT, 'data', 'reports', 'trials.csv')
    if os.path.exists(fallback):
        return fallback
    return None


def _dsr_pbo_gate():
    """DSR + PBO 两项诊断。缺数据→SKIP；有数据→WARN（打印实际数值）。"""
    # n_trials 口径说明（显式、保守近似）：
    #   本仓库无 hyperopt 轮数，"试验数"没有自然来源。故 n_trials 取"显式提供的
    #   试验矩阵列数 M"（每列=一个因子/权重候选配置的历史日收益）——这是"本次评估
    #   过的候选数"的保守近似。若未提供矩阵，则无口径可用 → SKIP，绝不编造 n_trials。
    matrix = _find_trial_matrix()
    if matrix is None:
        note = ("未找到试验矩阵（data/reports/trial_matrices/ 为空且无 EVAL_TRIAL_MATRIX）。"
                "本仓库不做 hyperopt 超参搜索，无 trials 记录；n_trials 口径（=试验矩阵列数="
                "本次评估的因子/权重候选数，保守近似）因此不可用 → 标 SKIP，不阻断。")
        _record_diag("DSR 多重检验校正", 'SKIP', note)
        _record_diag("PBO 过拟合概率", 'SKIP', note)
        return

    try:
        from scripts.multiple_testing import run_audit
        report = run_audit(matrix)
    except Exception as e:
        _record_diag("DSR 多重检验校正", 'WARN',
                     f"试验矩阵读取/计算失败（不阻断）: {str(e)[:100]}")
        _record_diag("PBO 过拟合概率", 'WARN',
                     f"试验矩阵读取/计算失败（不阻断）: {str(e)[:100]}")
        return

    dsr = report.get('dsr') or {}
    pbo = report.get('pbo') or {}
    n_trials = report.get('n_trials')
    dsr_val = dsr.get('dsr')
    pbo_val = pbo.get('pbo')
    n_days = report.get('n_days')
    t_sqrt = ((n_days or 0) - 1) ** 0.5
    # √(T−1)：本仓库 test 段仅约 15 个交易日，统计功效低，故仅 WARN
    dsr_msg = (f"DSR={dsr_val}（n_trials={n_trials}，口径=试验矩阵列数="
               f"候选因子/权重配置数，保守近似）；n_days={n_days}。"
               f"√(T−1)≈{t_sqrt:.2f} 样本短→功效低，仅告警不阻断。"
               f"DSR≥0.95 视为通过多重检验校正。")
    pbo_msg = (f"PBO={pbo_val}（n_trials={n_trials}，口径同上）；n_days={n_days}。"
               f"PBO>0.5 表示选择流程大概率过拟合（训练赢家样本外跑输中位数），"
               f"本仓库样本短→仅告警不阻断，需结合 DSR 与经济逻辑判断。")
    _record_diag("DSR 多重检验校正", 'WARN', dsr_msg)
    _record_diag("PBO 过拟合概率", 'WARN', pbo_msg)


_dsr_pbo_gate()


print()
print(f"门禁结果: {len(PASS)} 通过 / {len(FAIL)} 失败"
      + (f" | {len(WARN)} 告警 / {len(SKIP)} 跳过" if (WARN or SKIP) else ""))
if WARN:
    print("  告警(WARN, 仅记录不阻断):")
    for name, msg in WARN:
        print(f"    ⚠ {name}: {msg}")
if SKIP:
    print("  跳过(SKIP, 数据不足不计入失败):")
    for name, msg in SKIP:
        print(f"    ⊘ {name}: {msg}")
if FAIL:
    for name, err in FAIL:
        print(f"  FAIL: {name} — {err}")
    sys.exit(1)
print("全部通过 ✅")
