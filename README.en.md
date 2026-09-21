# 🏛️ A-Share Stock Picker

[![Python](https://img.shields.io/badge/python-3.10%2B-blue)](https://www.python.org/)
[![License](https://img.shields.io/badge/license-MIT-green)](LICENSE)
[![Compatible with](https://img.shields.io/badge/Claude%20Code-Skill-8A2BE2)](SKILL.md)

**Stock-Picker V4.4 (stable v-G) — Full-Stack Multi-Factor Quantitative Stock Selection Framework**

8 Direct Data Sources / 16 Endpoints · 30+ Factors · Short-Term + Long-Term Strategies · Real K-Line Backtest Engine · OOS-Validated Weights · Expert Ensemble Second Opinion (coverage gate) · Postmortem Prediction-Reconciliation Loop

[简体中文](README.md) · [English](README.en.md)

> **Status (2026-09-21)**: Code frozen (stable v-G), entering observation period. 497 unit tests + 9/0 gate all green.

---

## Table of Contents

- [Introduction](#introduction)
- [Features](#features)
- [Quick Start](#quick-start)
- [Short-Term Strategy](#short-term-strategy)
- [Backtest Engine](#backtest-engine)
- [Weight System](#weight-system)
- [System Architecture](#system-architecture)
- [Data Sources](#data-sources)
- [Automation Schedule](#automation-schedule)
- [AI Assistant Integration](#ai-assistant-integration)
- [Acknowledgments](#acknowledgments)
- [License](#license)

---

## Introduction

Stock-Picker is a full-stack quantitative stock selection system for the Chinese A-share market, covering the complete closed loop from data acquisition, factor computation, strategy scoring to backtest validation.

Three core problems of A-share quantitative investing:

- **Scattered data sources** — quotes, capital flow, dragon-tiger list, north-bound flow live on different platforms
- **API blocking** — East Money's WAF blocks non-browser HTTP requests
- **Hard to validate** — lack of real historical data for proper backtesting

Solved by:

- **Data source priority chain** — mootdx TCP (never banned) → Tencent HTTP → East Money rate-limited
- **Unified rate limiter** — all East Money APIs go through `em_get()` serial throttling
- **Real K-line backtest** — zero `np.random`, no look-ahead bias, strict T+1 under the open_t1 convention
- **Weights follow tail returns** (V4.4) — not broker-report medians, and not IC magnitude either: allocation is validated by "train-period optimization → holdout blind test" on top-N realized returns. Plan G passed 2.2 years of market-wide OOS plus six sub-period robustness checks

---

## Features

### 📊 Data — 8 Sources / 16 Endpoints Unified

> Note on counting: 8 upstream providers (mootdx / Tencent / Tonghuashun / ASHareHub /
> East Money / akshare / baostock / Sina), 16 network endpoints, 16 registered in
> `_source_status` (health report), 8 registered in `_source_available` (circuit breaker).
> Earlier docs said "14 sources", which has no basis in the code — corrected.

- **mootdx TCP** — K-line + financial snapshots (37 fields), never banned, ~0.1s/stock
- **Tencent Finance HTTP** — 5200+ stocks real-time quotes, never banned, ~46s
- **Tonghuashun 10jqka** — hot stocks + theme attribution, zero auth 73ms
- **ASHareHub** — northbound holdings / capital flow / technical factors / concepts / financials, 100 calls/day quota (local safety gate at 90)
- **East Money datacenter-web** — daily north-bound net inflow summary, zero auth (estimated caliber, flagged T-1 in the briefing)
- **East Money (em_get throttled)** — sector membership / dragon-tiger list
- **akshare** — big-deal capital flow / stock code list
- **Independent fuses** — 8 sources fused independently; recovery hook lives in `get_all_quotes()` and fires at most once every 10 minutes
- **Degradation impact registry** — `_SOURCE_FACTOR_IMPACT` explicitly declares which factors (and what aggregate weight) each source failure affects, so the briefing's top-of-screen warning cannot under-count (locked by guard tests)

### 🧠 Strategy

1. **Short-Term EOD** — multi-factor scoring + expert ensemble second opinion + market assessment → prefilter 5200+ → preliminary scoring → Top 200 deep evaluation → dynamic threshold → position allocation
2. **Long-Term Holding** — ROE + PE + PB fundamental scoring, 3-6 month holding period
3. **Strategy Health Display** — the kill-switch moved from "trigger = stop recommending" to a health notice: the trailing 20-outcome-day cumulative return ships with each recommendation (`rec['strategy_health']`), rendered as a risk note in the briefing
4. **Shadow Recommendations** (V4.4) — on extreme market regimes (skip / crowding breaker / zero names above threshold) the run still scores and persists rows with `mode='shadow'`, but does not push them and keeps them out of official stats — this breaks the "sample censoring" where no-push days left no evidence about whether the freeze was right
5. **Market Environment** — advance/decline ratio / limit-up sentiment composite (seal rate ×0.4 + promotion rate ×0.3 + yesterday's limit-up premium ×0.3) / median gain / hot stock count / north-bound flow → composite score
6. **Technical Scoring** — 6-dimension: Trend 30 + Bias 20 + Volume 15 + Support 10 + MACD 15 + RSI 10
7. **Portfolio Optimization** — score-weighted allocation, max 40% per stock, min 10% floor
8. **Three Missing-Data Semantics** (V4.4) — a missing factor **cedes** its weight to factors that do have data (never faked as a neutral 50), an under-covered expert dimension **abstains**, everything else is counted as-is; every site declares which one applies, aligned with the NaN-dropping caliber used by OOS
9. **Expert Ensemble Second Opinion** — 5 independent dimensions cross-checked against the main model: agreement nudges up, disagreement downweights, severe conflicts (Δ>25) flagged and position cut; dimension coverage below 40% means **abstention** instead of blind score-dragging (`MIN_EXPERT_COVERAGE=0.40`)
10. **Portfolio Risk Triple** — max 2 stocks per sector + pairwise correlation cap 0.85 + volatility fuse (market median abs-chg > 3% scales positions down)
11. **Chase-high Penalty** — same-day gain >7% linearly cuts the score (up to -50%), grounded in the A-share short-term reversal effect
12. **Postmortem Reconciliation Loop** (V4.4) — a structured note per Top-5 name daily (the LLM fills only thesis / key_factors / prediction / missed_risk), with realized outcome backfilled programmatically and cross-checked; a weekly hit rate ≤50% trips a circuit breaker; monthly distillation turns frequent tags on bad notes into candidate rules (LLM proposes hypotheses, quantitative validation decides adoption)

### 🔄 Self-Learning Feedback Loop

1. SQLite persists every recommendation with its factor breakdown (`(date, code, mode)` unique index, batch-level dedup)
2. Auto backfill T+1/T+5/T+20 realized returns (dedicated `backfill_pending.py` script, 3:15 AM cron)
3. Ridge regression maps factor scores to actual returns (`check_and_report()` produces reports only — **no auto-write**)
4. V4.4 primary calibration path: `calibrate_weights.py` allocates by walk-forward OOS IC (pessimistic value across three return conventions, negative-IC zeroed, single-factor cap 0.50); `--apply` has a **dual gate** — manual approval plus explicit `--accept-ic-objective` (the IC objective is known to diverge from the tail-return criterion, so a human must reconcile them first). A refusal exits with code 2 and leaves `v1.json` untouched
5. New weights persist to `v1.json` (load order v1.json > config.yml > DEFAULT_WEIGHTS), auto-loaded on next startup
6. Collapse protection (skip if single factor ≥ 80%), missing columns auto-fill 0.5

---

## Quick Start

### Requirements

- Python 3.10+, Windows / Linux / macOS
- Network: works both inside and outside China (mootdx TCP and Tencent API are globally accessible)

### Installation

```bash
git clone https://github.com/cbzhang86/stock-picker.git
cd stock-picker
pip install -r requirements.txt

# Optional: register free ASHareHub API Key
export ASHAREHUB_API_KEY="ash_your_key_here"
```

### First Run

```bash
python scripts/eod_stock_picker.py --mode short
```

You'll see: 5200+ stock codes → full market quotes → Tonghuashun hot stocks → prefilter → preliminary scoring → Top 200 deep evaluation → big-deal cache → final recommendations. About 2 minutes later, the daily briefing is output with market overview, theme heat TOP10, short-term rankings, long-term picks.

### Other Commands

```bash
# View model status
python scripts/eod_stock_picker.py --status

# Long-term strategy
python scripts/eod_stock_picker.py --mode long

# Unified CLI entry: one command for all common ops
python scripts/pick.py pick          # EOD stock picking
python scripts/pick.py health        # fast gate (9 checks)
python scripts/pick.py backfill      # T+1/T+5/T+20 return backfill
python scripts/pick.py oos           # full OOS factor diagnostics
python scripts/pick.py calibrate     # weight calibration proposal (no auto-apply)

# Daily unified scheduler (trading day → pick / non-trading day → quota prefetch)
python scripts/daily_job.py

# Backtest (default last 3 months)
python scripts/run_backtest.py --mode short

# Backtest version comparison
python scripts/run_backtest.py --list
python scripts/run_backtest.py --compare 1 2

# Regression gate (must pass after any code/weight change; --full adds OOS panel)
python scripts/evaluate_all.py

# Postmortem notes (prediction-reconciliation loop)
python scripts/postmortem.py stats --days 7      # hit rate; exit code 1 = circuit breaker
python scripts/postmortem.py candidates --month 2026-09

# Threshold recalibration (after ≥15 trading days of observations)
python scripts/recalibrate_thresholds.py --from-run-context

# Health check (9 items)
python scripts/verify.py
```

---

## Short-Term Strategy

### Pipeline

| Step | Operation | Time |
|------|-----------|------|
| 1 | `get_all_codes()` — read code cache | 0.001s |
| 2 | `get_all_quotes()` — Tencent API, whole market | ~46s |
| 3 | `get_ths_hot_stocks()` — Tonghuashun hot stocks + themes | ~0.22s |
| 4 | `_prefilter()` — remove ST/low-volume/limit-up-down | ~0.5s |
| 5 | 5-dim preliminary → Top 200 | ~0.3s |
| 6 | `get_main_fund()` — big-deal fund flow cache | ~25s |
| 7 | `get_kline()` × 200 — mootdx TCP 3-thread parallel | ~25s |
| 8 | Cross-sectional percentiles — RPS / liq_dev / vol_dev (60-day window from kline_df, zero quota) | ~0.1s |
| 9 | `scoring_model.score()` — weighted scoring + gap check | ~0.2s |
| 10 | `expert_ensemble` — coverage gate (<0.40 abstains) | ~0.1s |
| 11 | `portfolio_optimizer` — score-weighted allocation | ~0.05s |
| 12 | Sector + dragon-tiger for Top 3 | ~8s |
| 13 | Output briefing + save report | before 15:00 |

### Factor Weights (Plan G)

> **Single source of truth = `data/weights/v1.json`** (ScoringModel load order: v1.json > config.yml > code defaults; the three layers are synchronized and locked by tests).
> The only entry point for weight changes is `python scripts/calibrate_weights.py` (`--apply` requires manual approval + explicit `--accept-ic-objective`).
> **Never hand-edit the numbers.**

| Factor | Weight | Data Source | Logic | OOS IC |
|--------|--------|-------------|-------|--------|
| Hot Theme | **55%** | 10jqka 3-source fusion | In hot list + theme tags | Core factor; buyable hot subset T+1 +1.72% |
| Volume-shock liq_dev | **14%** | mootdx K-line 60d | Inverted percentile of `log(amount) − 60d norm` | +0.0654 (ICIR 0.535, orthogonal to size at −0.088) |
| Reversal reversal_20d | 10% | market-wide ranks | Inverted 20-day return percentile | +0.0506 (t=7.3) |
| Vol-shock vol_dev | 7% | mootdx K-line 60d | Inverted percentile of `vol20 − 60d norm` | +0.0263 (ICIR 0.225) |
| Low Volatility | 6% | mootdx K-line | Inverted 20-day volatility | low-vol anomaly, orthogonal to hot_theme |
| Momentum/RPS | 2% | market-wide ranks | 20-day return percentile → 0-100 | −0.0346 negative, kept as noise spread |
| Technical | 2% | mootdx K-line 6-dim | Trend 30 + Bias 20 + Volume 15 + Support 10 + MACD 15 + RSI 10 | −0.0251 negative |
| Volume-Price | 2% | volume ratio + tail structure | 0.8~2.0 = 80pt | −0.0172 negative |
| Dragon-Tiger | 1% | East Money datacenter | Listed + institution net buy > 0 | −0.0019 noise |
| Capital Flow | 1% | big deal / ASHareHub / THS | Cross-sectional percentile | +0.0057 weakly positive (historical coverage only 0.19%) |
| North-bound | 0% | — | Disclosure halted 2024-08, always neutral 50 | — |
| size / liquidity | 0% | valuation snapshot / — | size awaits ≥60 trading days of snapshots; liquidity falsified (84% size effect) | — |
| Valuation / Event (pending) | 0% | TDX / announcements | Pipeline ready, accumulating snapshots | pending |
| Risk | filter | risk_filter.py | Pre-filter, no weight (hard blocks score 0) | — |

**Evidence behind Plan G** (2024-01 ~ 2026-09, 2.99M rows / 647 trading days / 5,225 stocks, near-limit-up names excluded, all costs deducted):

- Top5 daily excess **+1.845% (t=21.4)** vs the old baseline (equal hot/reversal) +0.850% (t=9.4)
- Holds across all six sub-periods; train/holdout optimization confirms G sits on the holdout frontier (none of 500 random searches beat it significantly)
- **Key finding: high IC ≠ high profit.** The equal-weight 4-leg plan had the highest IC (0.0743) yet earned only +0.13%/day — weights must follow tail returns, not IC magnitude

### Dynamic Threshold

Strong market **65** / neutral **70** / weak **75** (`config.yml` baseline 75, overridable via `dynamic_min_score`).

⚠️ After switching to Plan G the score distribution **divides rather than shifts** (the hot cluster concentrates higher, non-hot mass moves down): the weak-market 75 threshold's "days with a recommendation" rises from 92.5% to 97.5% (+5.0pp), while strong/neutral markets barely move. **The threshold values were NOT changed** — no threshold edits without real data; recalibration waits for ≥15 trading days of `run_context` observations (`recalibrate_thresholds.py --from-run-context`).

---

## Backtest Engine

### Core Characteristics

1. **Data** — mootdx real K-line, zero `np.random`, no look-ahead bias
2. **Execution convention** — **open_t1 (the only valid convention)**: decision day T → next trading day's open, enforcing strict T+1; the close_t0 (same-day close buy) branch was removed on 2026-09-17
3. **Cost model** — commission 0.03% + tiered slippage + stamp duty 0.05% (sell only) + transfer fee (earlier versions omitted stamp duty and transfer fee, understating round-trip cost by ~0.051%; fixed)
4. **Trading Rules** — take-profit +2%, stop-loss -2% (read from `config.yml sell`), T+3 time stop
5. **Position Simulation** — by `allocation_pct`, daily T+1 buy open / sell close
6. **Benchmarks** — CSI 300 + CSI 1000 multi-benchmark comparison

### Output Metrics

Total trades / win rate / avg T+1 & T+5 return / largest single P&L / max drawdown / Sharpe / strategy return / excess return / equity curve / monthly returns / factor IC / win-loss score gap / trade details / optimization suggestions

### Historical Reference (2026-04-01 ~ 2026-06-27, min_score=60 legacy config)

> ⚠️ Caveat: the numbers below were produced under the old `min_score=60` + legacy weights. Today's setup is a dynamic threshold (65/70/75) with Plan G weights, so **a rerun of the same window looks entirely different** — the historical high returns are not reproducible. This is deliberate de-watering (momentum downweight + limit-up proxy filter + hot_theme neutralized in backtest), not code regression. Always quote results with their min_score and weight version.

| Metric | Value |
|--------|-------|
| Total trades | 95 (over 260 trading days, ~1.5/day) |
| Win rate | 57.9% |
| Avg T+1 return | +1.63% |
| Avg T+5 return | +5.87% |
| Max drawdown | -13.93% |
| Sharpe ratio | 4.36 |
| Strategy return | +62.50% |
| CSI 300 | +7.56% |
| Excess return | +54.94% |

**Important caveat on backtest caliber**: before 2026-09-05, `capital_flow` was force-neutralized in backtest (the backtest branch set `main_fund_accumulated` to `None`); the fix makes it reuse the historical values passed through by `_prefilter` from `factor_daily.db` snapshots, falling back to `None` only when absent. But `factor_daily.db` historical coverage is only ~10%, so most names still have no historical capital flow — the figures above actually test the three K-line factors (momentum + technical + volume-price), which is not the same scoring system as the live multi-factor model. The two are not directly comparable.

### Known Limitations

- Capital flow / themes / dragon-tiger / north-bound data are unavailable in backtest (intraday big-deal data cannot be reconstructed)
- mootdx covers ~600 trading days (~2.5 years)
- A full-range backtest (260 trading days) is CPU-bound and takes 6+ hours; validate changes on short windows against `--list` historical baselines

---

## Weight System

### Criterion and Validation Flow

```
hypothesis → OOS panel validation (2.2 years × 5,225 stocks) → train-period optimization → holdout blind test → human approval → three-layer sync
```

**Two iron rules**:

1. **No factor or weight goes live without passing OOS** — unvalidated factors start at zero weight and only accumulate data
2. **The criterion is top-N tail return, not IC** — highest IC ≠ most profitable (evidence: equal-weight 4-leg IC 0.0743, Top5 excess only +0.13%/day)

### The Three Weight Layers (hard contract, locked by tests)

| Layer | File | Notes |
|---|---|---|
| Active | `data/weights/v1.json` | What ScoringModel actually loads; the only effective source |
| Fallback | `config.yml` `short_term.weights` | Synced to G values with full rationale comments |
| Fallback | `core/scoring_model.py` `DEFAULT_WEIGHTS` | In-code defaults |

All three must agree; changing one alone raises a warning (semantic comparison, printed only on genuine divergence). The only entry point for changes is `calibrate_weights.py`.

### Optimizer (`feedback/optimizer.py`)

- **Algorithm**: `sklearn.linear_model.Ridge(alpha=1.0)`
- **Input**: factor raw scores → actual T+1 returns
- **Output**: normalized weights (negative → 0)
- **Trigger**: win rate < 50% or 50+ new records since last optimization, with freshness check passing
- **Collapse protection**: skip if single factor ≥ 80%
- **3-stage workflow**: `check_and_report()` → approval → `apply_from_report()`
- **Current state**: `apply_from_report()` has no caller (deliberately disabled); accumulated proposal reports under `data/reports/` not landing is expected behavior

### Falsified Factors (avoid re-treading these)

| Candidate | Verdict | Evidence |
|---|---|---|
| `liquidity` (−log amount) | Not enabled | Correlation **0.838** with the size proxy — 84% is a small-cap effect; ICIR 0.418 below the orthogonalized liq_dev (0.535) |
| `amihud20` (illiquidity) | Not enabled | Correlation **0.900** with the size proxy — just size in another guise |
| Volatility **level** component | Superseded by the deviation | vol_dev ICIR 0.225 > vol_level 0.126 |
| Equal-weight 4-leg (best-IC plan) | Rejected | Highest IC yet barely profitable (+0.13%/day, t=2.4) |
| Coordinate-refined "train optimum" | Rejected | Higher train t (18.0), worse holdout (+1.754%) — a textbook overfitting signature |

---

## System Architecture

```
stock-picker/
│
├── core/                          Core Engine
│   ├── data_engine.py             Multi-source fusion (8 sources/16 endpoints, cache, fuses, degradation registry)
│   ├── factor_library.py          30+ factors 0-100 scoring (incl. liq_dev / vol_dev deviation factors)
│   ├── scoring_model.py           Weighted scoring + weight loading + missing-data ceding + chase-high penalty
│   ├── technical_scorer.py        6-dim 100pt technical analysis
│   ├── risk_filter.py             Pre-trade risk filter
│   ├── backtest_engine.py         Backtest engine (open_t1, full costs, multi-benchmark)
│   ├── backtest_store.py          Backtest result persistence
│   ├── portfolio_optimizer.py     Position allocation
│   ├── expert_ensemble.py         5-dim expert second opinion (coverage gate)
│   ├── oos_validator.py           Walk-forward out-of-sample IC validation
│   ├── trading_calendar.py        Centralized trading calendar
│   ├── drift_monitor.py           Performance drift monitoring (PSI)
│   ├── data_quality_monitor.py    Data quality patrol
│   ├── fundamental_provider.py    Valuation/fundamentals, point-in-time
│   ├── event_provider.py          Announcements/event catalysts
│   └── factor_standardizer.py     Cross-sectional standardization (experimental)
│
├── strategies/                    Strategy Layer
│   ├── short_term.py              Short-term EOD (multi-factor + market assessment + ensemble + risk + shadow mode)
│   ├── long_term.py               Long-term strategy (fundamentals)
│   └── base.py                    Abstract strategy base class
│
├── reports/                       Report Layer
│   ├── market_briefing.py         Daily briefing (with degradation warning block)
│   ├── backtest_report.py         Backtest report rendering
│   └── daily_report.py            Markdown report I/O
│
├── feedback/                      Feedback Loop
│   ├── tracker.py                 SQLite prediction tracking (batch-level dedup)
│   ├── optimizer.py               Ridge regression weight optimizer
│   └── data_collector.py          Factor warehouse (daily snapshots)
│
├── scripts/                       User Entry Points
│   ├── eod_stock_picker.py        Main entry
│   ├── run_backtest.py            Backtest entry
│   ├── verify.py                  9-item health check
│   ├── pick.py                    Unified CLI entry
│   ├── daily_job.py               Daily unified scheduler
│   ├── evaluate_all.py            Regression gate (9 checks)
│   ├── calibrate_weights.py       OOS weight calibration (dual gate)
│   ├── postmortem.py              Postmortem notes (prediction-reconciliation loop)
│   ├── recalibrate_thresholds.py  Threshold recalibration (empirical / OOS proxy)
│   ├── backfill_pending.py        T+1/T+5/T+20 backfill
│   ├── snapshot_valuation_daily.py Daily valuation snapshot
│   ├── backup_predictions.py      Daily DB backup
│   ├── calibrate_slippage.py      EOD slippage calibration
│   ├── capacity_check.py          Capital capacity estimation
│   ├── multiple_testing.py        DSR/PBO multiple-testing audit
│   ├── prefetch_tdx.py            TDX valuation/event prefetch
│   └── prefetch_asharehub.py      Non-trading-day quota prefetch
│
├── tests/                         497 unit tests (incl. hard-contract locks + guard tests)
├── config.yml                     Central configuration
├── SKILL.md                       AI assistant skill definition
├── CHEATSHEET.md                  Quick reference
├── requirements.txt               Python dependencies
│
└── data/                          Runtime data (auto-created)
    ├── cache/                     K-line/code/backtest/factor cache + postmortem note DB
    ├── db/                        predictions.db
    ├── reports/                   Daily reports + briefings + run_context
    └── weights/                   v1.json active weights + history
```

---

## Data Sources

| Source | Purpose | Protocol | Notes |
|--------|---------|----------|-------|
| mootdx TCP | K-line + financials (37 fields) | TCP 7709 | Never banned, ~0.1s/stock |
| Tencent Finance | Real-time quotes | HTTP | 5200+ stocks, never banned, ~46s |
| Tonghuashun 10jqka | Hot stocks + themes | HTTP | Zero auth, 73ms |
| East Money datacenter-web | North-bound summary | HTTP | Daily net inflow (estimated caliber, T-1), zero auth |
| ASHareHub | Holdings/flow/tech/concepts/financials | HTTP | 100 calls/day shared across 4 endpoints (local gate 90) |
| East Money em_get | Sector membership / dragon-tiger | HTTP | Serial throttled, WAF protected |
| akshare | Big-deal flow / stock codes | HTTP | Independent fuse |

Each data source has an **independent fuse** — failures are isolated to their source. All fuses auto-recover every 10 minutes (`_recover_sources()`); the recovery hook lives in `get_all_quotes()`.

**Three-place registration required when adding a source or factor (hard requirement)**:

1. `_source_available` (fuse) and `_source_status` (health report) must use **identical keys** — `_update_source_status()` guards with `if source_key in self._source_status`, so an unregistered key is **silently dropped**: the fuse has tripped but the report still shows green, hiding the failure
2. `_SOURCE_FACTOR_IMPACT` (degradation registry) — omitting a factor makes the briefing's "total affected weight" badly under-count (a K-line source outage actually hits liq_dev / vol_dev / volatility / liquidity / reversal_20d)
3. OOS registration (`oos_validator.K_FACTORS`) — otherwise the new factor can never be validated out-of-sample

All three are locked by **guard tests**: forget any one when adding a factor and the suite fails immediately.

---

## Automation Schedule

| Time | Task | What it does |
|------|------|--------------|
| Trading days 14:45 | EOD stock picking | Select + push briefing (push channel belongs to the host environment; the script does not embed it) |
| Daily 3:15 | T+1 backfill | Dedicated `backfill_pending.py`; overnight data is complete and independent of the strategy |
| Trading days 19:35 | Postmortem notes | `postmortem.py` add/backfill; Friday also emits the weekly hit rate |
| Friday 16:00 | Weekly backtest | `stock-picker-weekly-backtest` |
| Monthly 1st | Monthly review | IC trend detection + downweight proposals (gated, never silently applied) |
| Sat/Sun 10:00 | Quota prefetch | Non-trading-day ASHareHub quota prefetch (`daily_job.py` routes automatically) |

---

## Observation Period (after the 2026-09-19 freeze)

Code: **frozen**. All known issues fixed (6 P1/P2 + 3 P2), no pending changes. Observation only, no development:

1. **Watch the briefing** — from the next trading day's 14:45 run the new weights and factor semantics go live for the first time; confirm the push works and the picks are sensible
2. **Accumulate data** — postmortem hit rate (first weekly report) + `run_context` threshold distribution; after 15 trading days run `recalibrate_thresholds.py --from-run-context`
3. **Two quantified effects** (features, not bugs) — weak-market days "with a recommendation" rise by ~5 percentage points; hot-stock entries cluster on names already up 5-9% but not yet limit-up

**When to touch code again**: ① threshold recalibration gives a clear recommendation; ② the postmortem hit rate trips the breaker repeatedly; ③ a data source fails long-term and raises a degradation alert; ④ you want to add a new factor direction (the process is fixed: OOS first → then weights → linkage points covered by guard tests)

---

## AI Assistant Integration

Supported via `SKILL.md`:

- **Claude Code** — auto-detected from project root
- **OpenClaw** — place `SKILL.md` into `~/.claude/skills/stock-picker/`
- **Hermes** — point skill config to `SKILL.md` path

Example prompts:

> "What's the market like today?" → runs full pipeline
> "Show me fundamentals for 600519" → ROE/EPS/valuation snapshot
> "Run a backtest" → executes backtest engine
> "Compare two backtest runs" → version comparison
> "Check system health" → runs verify.py 9 checks
> "What's the postmortem hit rate?" → runs postmortem.py stats

---

## Acknowledgments

This project draws inspiration and design patterns from the following open-source projects:

- **[a-stock-data](https://github.com/simonlin1212/a-stock-data)** (Simon Lin) — data source architecture, `em_get` rate limiter, THS hot/block/dragon API reference
- **[Sequoia-X](https://github.com/sngyai/Sequoia-X)** — pattern recognition strategies (golden cross, turtle breakout, high-tight flag), RPS ranking
- **[daily-stock-analysis](https://github.com/ZhuLinsen/daily_stock_analysis)** — StockTrendAnalyzer technical scoring system
- **[mootdx](https://github.com/mootdx/mootdx)** — Tongdaxin TCP protocol Python wrapper, providing stable K-line + financial data
- **[akshare](https://github.com/akfamily/akshare)** — A-share data interface standard, providing big-deal data and stock code list

---

## License

[MIT](LICENSE)

---

<div align="center">
Maintained by <a href="https://github.com/cbzhang86">cbzhang86</a> · Built with <a href="https://claude.ai/code">Claude Code</a>

If you find this project helpful, please ⭐
</div>
