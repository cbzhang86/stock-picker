#!/usr/bin/env python3
"""
verify.py — stock-picker system health check.

Runs 9 checks covering data source connectivity, config consistency,
quota headroom, cron health, Python deps, recent backtest freshness, etc.

Each check prints a one-line status:  ✅ OK / ⚠️ WARN / 🔴 FAIL
Exit code = number of FAILs (0 = all green, >0 = something broken).

Usage:
    python scripts/verify.py             # brief summary
    python scripts/verify.py --verbose   # include per-check details

Caveat: this is a first-pass health check, not a substitute for
manual review. It only catches the "obvious" failures.
"""

import argparse
import json
import os
import platform
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

# 东财公开 Web token：统一取自 core.data_engine（2026-09-18 审查 P3 外置，
# 支持环境变量 EASTMONEY_TOKEN 覆盖）。导入失败时回退内联默认值，保证 verify 可独立运行。
try:
    from core.data_engine import EASTMONEY_WEB_TOKEN as _EM_TOKEN
except Exception:  # pragma: no cover - 仅在异常环境触发
    _EM_TOKEN = os.environ.get('EASTMONEY_TOKEN',
                               '894050c76af8597a853f5b408b759f5d')

# Make project root importable.
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

# Where things live.
DATA_DIR = PROJECT_ROOT / "data"
WEIGHTS_V1 = DATA_DIR / "weights" / "v1.json"
CONFIG_YML = PROJECT_ROOT / "config.yml"
CRON_RUNLOG = Path(os.environ.get("LOCALAPPDATA", "")) / "hermes" / "cron" / "runlog.jsonl"
CRON_HEALTH_SCRIPT = Path(os.environ.get("LOCALAPPDATA", "")) / "hermes" / "scripts" / "cron_health_panel.py"
DATA_REPORTS = DATA_DIR / "reports"

# ANSI helpers — degrade gracefully on dumb terminals.
USE_COLOR = sys.stdout.isatty()


def emit(level, name, detail=""):
    """Print a one-line status line. level = 'OK'|'WARN'|'FAIL'."""
    icon = {'OK': '✅', 'WARN': '⚠️ ', 'FAIL': '🔴'}[level]
    msg = f"{icon} {name}"
    if detail:
        msg += f" — {detail}"
    print(msg)


# --- Checks ---------------------------------------------------------------
# Each check returns (level, detail). level ∈ {OK, WARN, FAIL}.


def check_python_env():
    """Check Python version + key importable packages."""
    problems = []
    # Python version
    major, minor = sys.version_info[:2]
    if (major, minor) < (3, 9):
        problems.append(f"Python {major}.{minor} too old (need >=3.9)")
    # Key imports
    for pkg in ('akshare', 'requests', 'bs4', 'yaml', 'pandas', 'numpy'):
        try:
            __import__(pkg)
        except ImportError as e:
            problems.append(f"{pkg}: {e}")
    if problems:
        return 'FAIL', "; ".join(problems) if len(problems) <= 2 else f"{len(problems)} issues: {problems[0]}..."
    plat = platform.platform()
    return 'OK', f"Python {major}.{minor} on {plat.split('-')[0]}"


def check_asharehub_endpoints(do_live: bool = False):
    """Check ASHareHub availability WITHOUT consuming daily quota.

    Default (do_live=False): read last-known source status from DataEngine's
    in-memory state — zero quota cost. This is what cron / routine runs use.

    do_live=True (--live flag): actually hit 3 endpoints (moneyflow/technical/
    concepts, 1 call each) to verify current connectivity. Costs 3 quota.
    Only use when diagnosing an outage.
    """
    key = os.environ.get('ASHAREHUB_API_KEY', '')
    if not key:
        return 'WARN', "ASHAREHUB_API_KEY not in env (skip live test)"

    if not do_live:
        # Zero-quota path: read last-known source status from DataEngine.
        try:
            sys.path.insert(0, str(PROJECT_ROOT))
            from core.data_engine import DataEngine
            de = DataEngine()
            summary = de.get_data_source_summary()
            # summary is dict like {'asharehub_moneyflow': {...}, ...}
            statuses = []
            for k in ('asharehub_moneyflow', 'asharehub_tech_factors',
                      'asharehub_concepts', 'asharehub_financial'):
                entry = summary.get(k, {})
                avail = entry.get('available', False) if isinstance(entry, dict) else False
                statuses.append(f"{k.split('_', 1)[-1]}={'OK' if avail else 'DOWN'}")
            return 'OK', f"last-known: {', '.join(statuses)} (no quota used)"
        except Exception as e:
            return 'WARN', f"can't read source status: {type(e).__name__} (no quota used)"

    # Live path (--live): actually consume quota.
    import requests
    base = "https://asharehub.com"
    headers = {'X-API-Key': key}
    test_calls = [
        ('moneyflow', f"{base}/v2/flows/moneyflow?symbol=600519.SH&limit=1"),
        ('technical', f"{base}/v2/factors/technical?symbol=600519.SH&limit=1"),
        ('concepts',  f"{base}/v2/flows/concepts?symbol=600519.SH&limit=1"),
    ]
    statuses = {}
    for name, url in test_calls:
        try:
            r = requests.get(url, headers=headers, timeout=10)
            statuses[name] = r.status_code
        except Exception as e:
            statuses[name] = f"ERR: {type(e).__name__}"
    fails = [n for n, s in statuses.items() if isinstance(s, str) and 'ERR' in s]
    quota = [n for n, s in statuses.items() if s == 429]
    ok = [n for n, s in statuses.items() if s == 200]
    if fails:
        return 'FAIL', f"{','.join(fails)} network error"
    if quota:
        return 'WARN', f"{','.join(quota)} 429 quota-exhausted (3 quota used)"
    return 'OK', f"{len(ok)} endpoints HTTP 200 (3 quota used)"


def check_north_flow_freshness():
    """Call securities API directly, confirm data date <= 3 trading days old."""
    import requests
    try:
        url = ("https://datacenter-web.eastmoney.com/securities/api/data/v1/get"
                "?reportName=RPT_MUTUAL_NETINFLOW_DETAILS"
                "&columns=DIRECTION_TYPE,TRADE_DATE,NET_INFLOW_SH,NET_INFLOW_SZ,NET_INFLOW_BOTH"
                "&token=" + _EM_TOKEN + "&client=WEB"
                "&filter=(DIRECTION_TYPE=%222%22)(TIME_TYPE=%221%22)"
                "&sortColumns=TRADE_DATE&sortTypes=-1&pageSize=1")
        r = requests.get(url, headers={'User-Agent': 'Mozilla/5.0'}, timeout=10)
        d = r.json()
        result = d.get('result')
        if not result or not result.get('data'):
            return 'FAIL', "securities API returned no data"
        date_str = str(result['data'][0].get('TRADE_DATE', ''))[:10]
        try:
            data_date = datetime.fromisoformat(date_str)
        except Exception:
            return 'FAIL', f"can't parse trade date: {date_str}"
        # 3 trading days ≈ 5 calendar days (weekends). Tolerance 7 days.
        age = (datetime.now() - data_date).days
        if age > 7:
            return 'FAIL', f"data date {date_str} is {age} days stale"
        if age > 5:
            return 'WARN', f"data date {date_str} is {age} days old"
        return 'OK', f"last data {date_str} ({age}d ago)"
    except Exception as e:
        return 'FAIL', f"securities API error: {type(e).__name__}: {str(e)[:60]}"


def check_weights_consistency():
    """Check weight governance (2026-09-05 审查 P0-A)：

    1. v1.json 存在、按模式 sum=1.0、north_flow=0（北向停公开）
    2. ScoringModel 实际加载权重 == v1.json（验证 v1 > config 优先级链路真正生效）
    3. 单因子集中度：任一因子权重 > 0.60 → WARN（报告 P2-L 单因子集中度风险）
    4. config.yml 仅查 sum（其权重段已声明为"历史草稿不生效"，与 v1 的因子级
       差异是设计如此，不再作为问题报告）
    """
    problems = []
    notes = []
    # v1.json
    if not WEIGHTS_V1.exists():
        return 'FAIL', f"missing {WEIGHTS_V1.name}"
    try:
        with open(WEIGHTS_V1, encoding='utf-8') as f:
            v1 = json.load(f)
    except Exception as e:
        return 'FAIL', f"v1.json parse: {e}"
    for mode in ('short', 'long'):
        s = sum(v1.get(mode, {}).values())
        if abs(s - 1.0) > 0.001:
            problems.append(f"v1.json {mode} sum={s:.3f}")
        if v1.get(mode, {}).get('north_flow', -1) != 0.0:
            problems.append(f"v1.json {mode}.north_flow={v1[mode]['north_flow']} (expect 0)")
        # P0-A/P2-L：单因子集中度
        for fac, w in v1.get(mode, {}).items():
            if w > 0.60:
                problems.append(f"v1.json {mode}.{fac}={w:.2f} >0.60 集中度风险")

    # P0-A：ScoringModel 实际加载权重必须与 v1.json 一致（否则 v1>config 优先级失效）
    try:
        from core.scoring_model import ScoringModel
        dw = ScoringModel.DEFAULT_WEIGHTS
        for mode in ('short', 'long'):
            s = sum(dw.get(mode, {}).values())
            if abs(s - 1.0) > 0.001:
                problems.append(f"DEFAULT_WEIGHTS {mode} sum={s:.3f}")
        sm = ScoringModel()
        for mode in ('short', 'long'):
            loaded = sm.get_weights(mode)
            expected = v1.get(mode, {})
            for fac in set(loaded) | set(expected):
                lw, ew = loaded.get(fac, 0.0), expected.get(fac, 0.0)
                if abs(lw - ew) > 0.005:
                    problems.append(f"ScoringModel 加载 {mode}.{fac}={lw:.4f} != v1.json {ew:.4f}（v1 优先级失效?）")
    except Exception as e:
        problems.append(f"can't import/instantiate ScoringModel: {e}")

    # config.yml — check sum only (its weights section is DECLARED as a
    # non-effective historical draft since 2026-09-05 P0-A; factor-level
    # divergence from v1.json is by design).
    try:
        import yaml
        with open(CONFIG_YML, encoding='utf-8') as f:
            cfg = yaml.safe_load(f)
        short_cfg = cfg.get('short_term', {}).get('weights', {})
        long_cfg = cfg.get('long_term', {}).get('weights', {})
        for name, w in (('short', short_cfg), ('long', long_cfg)):
            s = sum(w.values()) if w else 0
            if abs(s - 1.0) > 0.001:
                problems.append(f"config.yml {name} sum={s:.3f}")
        notes.append("config weights=historical draft (non-effective)")
    except Exception as e:
        problems.append(f"config.yml parse: {e}")

    if problems:
        return 'WARN', "; ".join(problems)
    msg = "v1/DEFAULT sums=1.00; loaded==v1.json; caps OK"
    if notes:
        msg += "; " + "; ".join(notes)
    return 'OK', msg


def check_cron_health():
    """Examine runlog.jsonl: count runs in last 24h, flag errors."""
    if not CRON_RUNLOG.exists():
        return 'WARN', "cron/runlog.jsonl not yet populated (collector hasn't run)"
    now = datetime.now()
    runs_24h = []
    errors_24h = []
    try:
        with open(CRON_RUNLOG, encoding='utf-8') as f:
            for line in f:
                try:
                    row = json.loads(line)
                except Exception:
                    continue
                ts = row.get('recorded_at')
                if not ts:
                    continue
                try:
                    dt = datetime.fromisoformat(ts.split('+')[0])
                except Exception:
                    continue
                if (now - dt).total_seconds() < 24 * 3600:
                    runs_24h.append(row)
                    if row.get('status') == 'error':
                        errors_24h.append(row)
    except Exception as e:
        return 'FAIL', f"can't read runlog: {e}"
    if not runs_24h:
        return 'WARN', "no cron runs recorded in the last 24h"
    detail = f"{len(runs_24h)} runs"
    # Distinguish "delivery error" (real infra issue) from "exit != 0 but delivered"
    # (script-level signal like skill_audit returning 1 on HIGH+).
    real_errors = [r for r in errors_24h if r.get('delivery_error')]
    if real_errors:
        names = set(r.get('name', '?') for r in real_errors)
        return 'WARN', f"{detail}; {len(real_errors)} delivery errors: {','.join(list(names)[:3])}"
    return 'OK', detail


def check_data_reports_freshness():
    """Is there a backtest/strategy report dated within last 7 days?"""
    if not DATA_REPORTS.exists():
        return 'WARN', f"no {DATA_REPORTS.name}/ directory"
    cutoff = datetime.now() - timedelta(days=7)
    recent = []
    for f in DATA_REPORTS.iterdir():
        if not f.is_file():
            continue
        mtime = datetime.fromtimestamp(f.stat().st_mtime)
        if mtime > cutoff:
            recent.append(f.name)
    if not recent:
        return 'WARN', "no reports in last 7 days"
    return 'OK', f"{len(recent)} reports in last 7d (latest: {recent[0][:30]})"


def check_disk_project():
    """Total size of project dir; warn if >5GB (backtest artifacts pile up)."""
    total = 0
    for root, _, files in os.walk(PROJECT_ROOT):
        if '__pycache__' in root or '.git' in root:
            continue
        for f in files:
            try:
                total += os.path.getsize(os.path.join(root, f))
            except OSError:
                pass
    if total > 5 * 1024 * 1024 * 1024:
        return 'WARN', f"{total / 1024**3:.2f}GB (cleanup recommended)"
    return 'OK', f"{total / 1024**2:.1f}MB"


def check_strategy_import():
    """Can ShortTermStrategy actually instantiate (catch bad config / import errors)?"""
    try:
        import yaml
        with open(CONFIG_YML, encoding='utf-8') as f:
            cfg = yaml.safe_load(f)
        # Don't run the full strategy (takes minutes); just construct.
        from strategies.short_term import ShortTermStrategy
        st = ShortTermStrategy(config=cfg)
        return 'OK', f"ShortTermStrategy constructed; weights short={list(st.scoring_model.DEFAULT_WEIGHTS['short'].keys())[:3]}..."
    except Exception as e:
        return 'FAIL', f"strategy import: {type(e).__name__}: {str(e)[:80]}"


def check_backtest_recent():
    """Try to import backtest_engine and run a tiny smoke backtest if supported.
    We don't actually run backtest (slow) — just verify it imports cleanly.
    """
    try:
        from core.backtest_engine import BacktestEngine
        # ok if class loads
        return 'OK', "BacktestEngine importable"
    except Exception as e:
        return 'FAIL', f"BacktestEngine: {type(e).__name__}: {str(e)[:80]}"


# --- Runner ---------------------------------------------------------------

CHECKS = [
    ("Python env", check_python_env),
    ("ASHareHub endpoints", check_asharehub_endpoints),
    ("North-flow freshness", check_north_flow_freshness),
    ("Weights consistency", check_weights_consistency),
    ("Cron health (24h)", check_cron_health),
    ("Reports freshness (7d)", check_data_reports_freshness),
    ("Disk project size", check_disk_project),
    ("Strategy import", check_strategy_import),
    ("Backtest engine import", check_backtest_recent),
]


def main():
    parser = argparse.ArgumentParser(description='stock-picker system health check')
    parser.add_argument('--verbose', action='store_true', help='include extra detail')
    parser.add_argument('--live', action='store_true',
                        help='actually hit ASHareHub endpoints (costs 3 quota). Default reads last-known status, 0 quota.')
    args = parser.parse_args()

    print(f"stock-picker health check — {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    print("=" * 72)
    fails = 0
    warns = 0
    for name, fn in CHECKS:
        try:
            # ASHareHub check gets do_live flag; others called with no args.
            if name == "ASHareHub endpoints":
                level, detail = fn(args.live)
            else:
                level, detail = fn()
        except Exception as e:
            level, detail = 'FAIL', f"check crashed: {type(e).__name__}: {str(e)[:80]}"
        emit(level, name, detail if args.verbose else "")
        if level == 'FAIL':
            fails += 1
        elif level == 'WARN':
            warns += 1
    print("=" * 72)
    total = len(CHECKS)
    summary = f"{total} checks: {total - fails - warns} OK, {warns} WARN, {fails} FAIL"
    print(summary)
    return fails  # exit code = number of FAILs


if __name__ == "__main__":
    sys.exit(main())
