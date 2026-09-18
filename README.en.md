# 🏛️ A-Share Stock Picker

[![Python](https://img.shields.io/badge/python-3.10%2B-blue)](https://www.python.org/)
[![License](https://img.shields.io/badge/license-MIT-green)](LICENSE)
[![Compatible with](https://img.shields.io/badge/Claude%20Code-Skill-8A2BE2)](SKILL.md)

**Stock-Picker V4.3 — Full-Stack Multi-Factor Quantitative Stock Selection Framework**

8 Direct Data Sources / 16 Endpoints · 30+ Factors · Short-Term + Long-Term Strategies · Real K-Line Backtest Engine · OOS IC-Calibrated Weights · Expert Ensemble Second Opinion

[简体中文](README.md) · [English](README.en.md)

---

## Table of Contents

- [Introduction](#introduction)
- [Features](#features)
- [Quick Start](#quick-start)
- [Short-Term Strategy](#short-term-strategy)
- [Backtest Engine](#backtest-engine)
- [Weight Self-Learning](#weight-self-learning)
- [System Architecture](#system-architecture)
- [Data Sources](#data-sources)
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
- **Real K-line backtest** — zero `np.random`, no look-ahead bias
- **Weights follow OOS IC** (V4.3) — every weight is calibrated against walk-forward out-of-sample IC; negative-IC factors are downweighted to exploratory slots instead of copying broker-report medians

---

## Features

### 📊 Data — 8 Sources / 16 Endpoints Unified

> Note on counting: 8 upstream providers (mootdx / Tencent / Tonghuashun / ASHareHub /
> East Money / akshare / baostock / Sina), 16 network endpoints, 16 registered in
> `_source_status` (health report; after the 2026-09-03 key backfill it matches the endpoint count), 8 registered in `_source_available` (circuit breaker).
> Earlier docs said "14 sources", which has no basis in the code — corrected.

- **mootdx TCP** — K-line + financial snapshots (37 fields), never banned, ~0.1s/stock
- **Tencent Finance HTTP** — 5205 stocks real-time quotes, never banned, ~46s
- **Tonghuashun 10jqka** — hot stocks + theme attribution, zero auth 73ms
- **ASHareHub** — northbound holdings / capital flow / technical factors / concepts / financials, 100 calls/day quota
- **East Money datacenter-web** — daily north-bound net inflow summary, zero auth
  (earlier docs said `hexin.cn`; that domain does not exist in the code — corrected)
- **East Money (em_get throttled)** — sector membership / dragon-tiger list
- **akshare** — big-deal capital flow / stock code list
- **Independent fuses** — 8 sources fused independently; recovery hook lives in
  `get_all_quotes()` and fires at most once every 10 minutes

### 🧠 Strategy

1. **Short-Term EOD** — 7-factor scoring + expert ensemble second opinion + market assessment → prefilter → Top 200 deep evaluation → position allocation. Auto-skip on extremely weak markets
2. **Long-Term Holding** — ROE + PE + PB fundamental scoring, 3-6 month holding period
3. **Strategy Health Display** (V4.3 behavior change) — the kill-switch moved from "trigger = stop recommending" to a health notice: the trailing 20-outcome-day cumulative return ships with each recommendation (`rec['strategy_health']`), rendered as a risk note in the briefing; participation stays the user's call
4. **Market Environment** — advance/decline ratio / **limit-up sentiment composite** (seal rate ×0.4 + promotion rate ×0.3 + yesterday's limit-up premium ×0.3, from akshare limit-up pools, 2026-09-07) / median gain / hot stock count / north-bound flow → composite score
5. **Technical Scoring** — 6-dimension: Trend 30 + Bias 20 + Volume 15 + Support 10 + MACD 15 + RSI 10
6. **Pattern Recognition** — 9 K-line patterns (golden cross, turtle breakout, high-tight flag, etc.)
7. **Portfolio Optimization** — score-weighted position allocation, max 40% per stock, min 10% floor
8. **Anti-Overfitting** — trims tail if 3rd-place is 20+ points below 1st
9. **Expert Ensemble Second Opinion** (V4.3) — 5 independent dimensions (fundamental/technical/capital/valuation/event) cross-checked against the 7-factor model: agreement nudges scores up, disagreement downweights, severe conflicts (Δ>25) flagged and position cut
10. **Portfolio Risk Triple** (V4.3) — max 2 stocks per sector + pairwise correlation cap 0.85 + volatility fuse (market median |chg| > 3% scales positions down)
11. **Chase-high Penalty** (V4.3) — same-day gain >7% linearly cuts the score (up to -50%), grounded in the A-share short-term reversal effect (CSI-300 1-month reversal IC 27.69%, per Huatain monthly tracking)

### 🔄 Self-Learning Feedback Loop

1. SQLite persists every recommendation with 7-factor breakdown
2. Auto backfill T+1/T+5/T+20 realized returns
3. Ridge regression maps factor scores to actual returns (`check_and_report()` produces reports only — **no auto-write**; `apply_from_report()` is deliberately disabled, landing a weight change takes a human)
4. V4.3 primary calibration path: `calibrate_weights.py` allocates by walk-forward OOS IC (pessimistic value across three return conventions), zeroing negative-IC factors, single-factor cap 0.50, `--apply` requires manual approval
5. New weights persisted to `v1.json` (load order v1.json > config.yml), auto-loaded on next startup
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

You'll see: 5205 stock codes → full market quotes → Tonghuashun hot stocks → prefilter → preliminary scoring → Top 200 deep evaluation → big-deal cache → final recommendations. About 4 minutes later, the daily briefing is output with market overview, theme heat TOP10, short-term rankings, long-term picks.

### Other Commands

```bash
# View model status
python scripts/eod_stock_picker.py --status

# Long-term strategy
python scripts/eod_stock_picker.py --mode long

# Unified CLI entry (2026-09-07): one command for all common ops
python scripts/pick.py pick          # EOD stock picking
python scripts/pick.py health         # fast gate (9 checks)
python scripts/pick.py backfill       # T+1/T+5/T+20 return backfill
python scripts/pick.py oos            # full OOS factor diagnostics
python scripts/pick.py calibrate      # weight calibration proposal (no auto-apply)

# Daily unified scheduler (trading day → pick / non-trading day → quota prefetch)
python scripts/daily_job.py

# Backtest (default last 3 months)
python scripts/run_backtest.py --mode short

# Backtest version comparison
python scripts/run_backtest.py --list
python scripts/run_backtest.py --compare 1 2

# Regression gate (must pass after any code/weight change; --full adds OOS panel)
python scripts/evaluate_all.py

# Health check (9 items)
python scripts/verify.py
```

---

## Short-Term Strategy

### Pipeline

| Step | Operation | Time |
|------|-----------|------|
| 1 | `get_all_codes()` — read code cache | 0.001s |
| 2 | `get_all_quotes()` — Tencent API 5205 stocks | ~46s |
| 3 | `get_ths_hot_stocks()` — Tonghuashun hot stocks + themes | ~0.22s |
| 4 | `_prefilter()` — remove ST/low-volume/limit-up-down | ~0.5s |
| 5 | 5-dim preliminary → Top 200 | ~0.3s |
| 6 | `get_main_fund()` — big-deal fund flow cache | ~25s |
| 7 | `get_kline()` × 200 — mootdx TCP 3-thread parallel | ~25s |
| 8 | Cross-sectional RPS ranking | ~0.1s |
| 9 | `scoring_model.score()` — 7-factor weighted + gap check | ~0.2s |
| 10 | `portfolio_optimizer` — score-weighted allocation | ~0.05s |
| 11 | Sector + dragon-tiger for Top 3 | ~8s |
| 12 | Output briefing + save report | before 15:00 |

### Factor Weights

> **Single source of truth = `data/weights/v1.json`** (ScoringModel load order: v1.json > config.yml).
> Values below are the OOS-calibrated weights as of 2026-09-05; the only entry point for weight changes is `python scripts/calibrate_weights.py` (`--apply` requires manual approval). The config.yml weight block is a historical draft.

| Factor | Weight | Data Source | Logic | OOS IC |
|--------|--------|-------------|-------|--------|
| Hot Theme | 50% | 10jqka 3-source fusion | In hot list + has theme tags | +0.0651 (t=+2.17), only significant factor |
| Technical | 16% | mootdx K-line 6-dim | Trend 30 + Bias 20 + Volume 15 + Support 10 + MACD 15 + RSI 10 | -0.0462 mildly negative, on watch |
| Capital Flow | 15% | Big deal / ASHareHub / THS | Cross-sectional percentile ranking | +0.0056 weakly positive |
| Volume-Price | 12% | Volume ratio + tail structure | 0.8~2.0 = 80pt | -0.0460 consistently negative, downweighted |
| Momentum/RPS | 4% | Market-wide percentile | 20-day return → 0-100 | -0.0926 strongly negative, exploratory slot |
| Dragon-Tiger | 3% | East Money datacenter | Listed + institution net buy > 0 | noise-level |
| North-bound | 0% | — | Disclosure halted 2024-08, always neutral 50 | — |
| Valuation / Event (pending) | 0% | TDX / announcements | Pipeline ready; enable after ≥60 trading days of snapshots pass OOS | pending |
| Risk | filter | risk_filter.py | Blocks severe risks, no weight | — |

---

## Backtest Engine

### Core Characteristics

1. **Data** — mootdx real K-line, zero `np.random`, no look-ahead bias
2. **Execution** — next day open price
3. **Slippage** — 0.1% (configurable)
4. **Commission** — 0.03% (configurable)
5. **Trading Rules** — T+1 take-profit +2%, stop-loss -2% (from `config.yml sell`), T+3 time stop
6. **Position Simulation** — by `allocation_pct`, daily T+1 buy open / sell close
7. **Benchmark** — CSI 300

### Latest Backtest (2026-04-01 ~ 2026-06-27)

> ⚠️ Caveat: the numbers below were produced under the old `min_score=60` config. With the current `min_score=75` baseline, a fixed-code rerun of the same window (run#26, static threshold as of 2026-09-05) yields **0 qualifying trades** — the historical high returns are not reproducible. This is deliberate de-watering (momentum downweight + limit-up proxy filter + hot_theme neutralized in backtest), not code regression. Since 2026-09-07 the live scoring threshold floats with the market regime (strong 65 / neutral 70 / weak 75; overridable via `dynamic_min_score`), so a rerun today is not guaranteed to stay at 0. Always quote results with their min_score.

| Metric | Value |
|--------|-------|
| Total trades | 95 |
| Win rate | 57.9% |
| Avg T+1 return | +1.63% |
| Avg T+5 return | +5.87% |
| Max drawdown | -13.93% |
| Sharpe ratio | 4.36 |
| Strategy return | +62.50% |
| CSI 300 | +7.56% |
| Excess return | +54.94% |

### Known Limitations

- Capital flow / themes / dragon-tiger / north-bound data are not available in backtest (no historical snapshots)
- mootdx covers ~600 trading days (~2.5 years)
- Uses next-day open price, cannot simulate intraday fills

---

## Weight Self-Learning

Live run → auto backfill T+1 → 60+ records → Ridge regression → factor variance audit → collapse check → new weights → `v1.json`

### Optimizer (`feedback/optimizer.py`)

- **Algorithm**: `sklearn.linear_model.Ridge(alpha=1.0)`
- **Input**: factor raw scores → actual T+1 returns
- **Output**: normalized weights (negative → 0, positive → sum 1)
- **Trigger**: win rate < 50% or 50+ new records since last optimization
- **Collapse protection**: skip if single factor ≥ 80%
- **Missing columns**: auto-fill 0.5
- **3-stage workflow**: `check_and_report()` → approval → `apply_from_report()`
- **Version tracking**: old versions in `data/weights/` (timestamped)

---

## System Architecture

```
stock-picker/
│
├── core/                          Core Engine
│   ├── data_engine.py             Multi-source data fusion (8 sources/16 endpoints, cache, fuses)
│   ├── factor_library.py          30+ factors 0-100 scoring
│   ├── scoring_model.py           Weighted scoring + rating + weight loading + chase-high penalty
│   ├── technical_scorer.py        6-dim 100pt technical analysis
│   ├── risk_filter.py             6-level risk check
│   ├── backtest_engine.py         Backtest engine (mootdx real K-line snapshots)
│   ├── backtest_store.py          Backtest result persistence + version compare
│   ├── portfolio_optimizer.py     Position allocation (scoring-weighted/equal)
│   ├── expert_ensemble.py         5-dim expert second opinion (V4.3)
│   ├── oos_validator.py           Walk-forward out-of-sample IC validation (V4.3)
│   ├── trading_calendar.py        Centralized trading calendar (V4.3)
│   ├── drift_monitor.py           Performance drift monitoring, PSI (V4.3)
│   ├── data_quality_monitor.py    Data quality patrol, 5 anomaly classes (V4.3)
│   ├── fundamental_provider.py    Valuation/fundamentals, point-in-time (V4.3)
│   ├── event_provider.py          Announcements/event catalysts (V4.3)
│   └── factor_standardizer.py    Cross-sectional standardization, experimental (V4.3)
│
├── strategies/                    Strategy Layer
│   ├── short_term.py              Short-term EOD (7 factors + market assessment + ensemble + portfolio risk)
│   ├── long_term.py               Long-term strategy (6 factors fundamental)
│   └── base.py                    Abstract strategy base class
│
├── reports/                       Report Layer
│   ├── market_briefing.py         Daily market briefing generator
│   ├── backtest_report.py         Backtest report + daily recommendation rendering
│   └── daily_report.py            Markdown report file I/O
│
├── feedback/                      Feedback Loop
│   ├── tracker.py                 SQLite prediction tracking
│   ├── optimizer.py               Ridge regression weight optimizer
│   └── data_collector.py          Factor warehouse (daily snapshots)
│
├── scripts/                       User Entry Points
│   ├── eod_stock_picker.py        Main entry
│   ├── run_backtest.py            Backtest entry
│   ├── verify.py                  9-item health check
│   ├── pick.py                    Unified CLI entry (V4.3)
│   ├── daily_job.py               Daily unified scheduler (V4.3)
│   ├── evaluate_all.py            Regression gate (V4.3)
│   ├── calibrate_weights.py       OOS weight calibration (V4.3)
│   ├── calibrate_slippage.py      EOD slippage calibration (V4.3)
│   ├── capacity_check.py          Capital capacity estimation (V4.3)
│   ├── multiple_testing.py        DSR/PBO multiple-testing audit (V4.3)
│   ├── prefetch_tdx.py            TDX valuation/event prefetch (V4.3)
│   └── prefetch_asharehub.py     Non-trading-day quota prefetch
│
├── config.yml                     Central configuration
├── SKILL.md                       AI assistant skill definition
├── CHEATSHEET.md                  Quick reference
├── requirements.txt               Python dependencies
│
└── data/                          Runtime data (auto-created)
    ├── cache/                     K-line/code/backtest/factor cache
    ├── db/                        predictions.db
    ├── reports/                   Daily reports + briefings
    └── weights/                   v1.json active weights + history
```

---

## Data Sources

| Source | Purpose | Protocol | Notes |
|--------|---------|----------|-------|
| mootdx TCP | K-line + financials | TCP 7709 | Never banned, ~0.1s/stock |
| Tencent Finance | Real-time quotes | HTTP | 5205 stocks, never banned, ~46s |
| Tonghuashun 10jqka | Hot stocks + themes | HTTP | Zero auth, 73ms |
| East Money datacenter-web | North-bound summary | HTTP | Daily net inflow, zero auth |
| ASHareHub | Holdings/flow/tech/concepts/financials | HTTP | 100 calls/day shared across 4 endpoints |
| East Money em_get | Sector membership / dragon-tiger | HTTP | Serial throttled, WAF protected |
| akshare | Big-deal flow / stock codes | HTTP | Independent fuse |

Each data source has an **independent fuse** — failures are isolated to their source. All fuses auto-recover every 10 minutes (`_recover_sources()`). A fused factor is neutralized to 50 points, with its weight redistributed to active factors.

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
