# 🏛️ A-Share Stock Picker

**Stock-Picker — Full-Stack Multi-Factor Quantitative Stock Selection Framework**

14 Direct Data Sources · 30+ Quantitative Factors · Short-Term EOD + Long-Term Holding · Real K-Line Backtest Engine · Ridge Regression Self-Learning Weight Optimization

**English** · [简体中文](README.md)

[![Python](https://img.shields.io/badge/python-3.10%2B-blue)](https://www.python.org/) [![License](https://img.shields.io/badge/license-MIT-green)](LICENSE) [![Compatible with](https://img.shields.io/badge/Claude%20Code-OpenClaw-Hermes-8A2BE2)](SKILL.md)

---

**Introduction**

Stock-Picker is a full-stack quantitative stock selection system for the Chinese A-share market, covering the complete closed loop from data acquisition, factor computation, strategy scoring to backtest validation.

A-share quantitative investing faces three core challenges: scattered data sources (real-time quotes, capital flows, dragon-tiger list, north-bound capital across different platforms), API blocking (East Money WAF protection), and difficulty in validation (lack of real historical data for proper backtesting). This project solves these through:

- **Priority fallback chain** — mootdx TCP never blocked → Tencent HTTP → East Money rate-limited
- **Unified rate limiter** — all East Money requests through `em_get()` serial throttling
- **Real K-line backtesting** — zero `np.random`, no look-ahead bias

---

**Features**

**📊 Data — 14 Sources Unified**

- **mootdx TCP direct** — K-line + financial snapshots (37 fields), never banned, ~0.1s/stock
- **Tencent Finance HTTP** — full market 5205 stocks, never banned, ~46s
- **Tonghuashun 10jqka** — hot stocks + theme attribution, zero auth 73ms
- **ASHareHub** — per-stock north-bound holdings / capital flow / technical factors / concepts / financials, 100 calls/day budget
- **hexin.cn** — real-time north-bound capital summary, zero auth
- **East Money (em_get)** — sector membership / dragon-tiger list, WAF protected
- **akshare** — large-deal capital flow / A-share code list
- **Independent circuit breakers** — per-source fusing, auto-recover every 10 min

**🧠 Strategy**

1. **Short-Term EOD** — 7-factor weighted scoring + market assessment → pre-filter 5205→4900 → pre-score → Top 200 deep evaluation → position allocation. Auto-skips in extremely weak markets.
2. **Long-Term Holding** — ROE + PE + PB fundamental scoring, 3-6 month holding period
3. **Market-Aware** — up/down ratio, limit up/down count, median return, strong stock count, north-bound flow → composite score
4. **Technical Scoring** — 6-dimension systematic scoring (Trend 30 + Bias 20 + Volume 15 + Support 10 + MACD 15 + RSI 10)
5. **Pattern Recognition** — 9 K-line patterns auto-detected (golden cross, turtle breakout, high-tight flag, etc.)
6. **Portfolio Optimization** — score-weighted position allocation, max 40% per stock, min 10% floor
7. **Anti-Overfitting** — trims tail if 3rd-place score is 20+ points below 1st

**🔄 Self-Learning — Feedback Loop**

1. SQLite persistent storage of every recommendation with 7-factor breakdown
2. Auto backfill of T+1 / T+5 / T+20 realized returns
3. Ridge regression mapping factor scores to actual returns
4. Auto-triggered optimization after 60+ records with outcomes, then every 50 new records
5. 3-stage workflow: `check_and_report()` generates report → human approval → `apply_from_report()` writes
6. Weights persisted to `v1.json`, auto-loaded on next startup
7. Collapse protection (single factor ≥ 80% skipped), missing columns auto-fill 0.5

---

**Quick Start**

**Prerequisites**

- Python 3.10+, Windows / Linux / macOS
- Network: works both inside and outside China (mootdx TCP and Tencent API globally accessible)

**Installation**

```bash
git clone https://github.com/cbzhang86/stock-picker.git
cd stock-picker
pip install -r requirements.txt

# Optional: register for free ASHareHub API Key
export ASHAREHUB_API_KEY="ash_your_key_here"
```

**First Run**

```bash
python scripts/eod_stock_picker.py --mode short
```

You will see A-share code list 5205 → full market quotes → Tonghuashun hot stocks → pre-filter → preliminary score → deep evaluate 200 → large-deal cache load → top 3 recommendations. About 4 minutes later, the daily briefing outputs market overview, theme heatmap TOP 10, short-term ranking, and long-term picks.

**Other Commands**

```bash
# View model status and recent performance
python scripts/eod_stock_picker.py --status

# Long-term strategy
python scripts/eod_stock_picker.py --mode long

# Run backtest (default last 3 months)
python scripts/run_backtest.py --mode short

# Backtest version comparison
python scripts/run_backtest.py --list
python scripts/run_backtest.py --compare 1 2

# 23-point health check
python scripts/verify.py
```

---

**System Architecture**

```
stock-picker/
│
├── core/                          Core Engine
│   ├── data_engine.py             Multi-source data fusion (14 sources, cache, circuit breakers)
│   ├── factor_library.py          30+ factor 0-100 scoring logic
│   ├── scoring_model.py           Weighted scoring + rating mapping + weight loading
│   ├── technical_scorer.py        6-dim 100-pt systematic technical analysis
│   ├── risk_filter.py             6-layer risk check (ST/liquidity/limit-up-down/turnover)
│   ├── backtest_engine.py         Backtest engine (mootdx real K-line snapshots, zero random)
│   ├── backtest_store.py          SQLite backtest persistence, version compare
│   └── portfolio_optimizer.py     Position allocation (score-weighted / equal-weight)
│
├── strategies/                    Strategy Layer
│   ├── short_term.py              Short-term EOD strategy (7 factors + market assessment)
│   ├── long_term.py               Long-term holding strategy (6 factors fundamental + north + momentum)
│   └── base.py                    Abstract strategy base class
│
├── reports/                       Report Layer
│   ├── market_briefing.py         Daily market briefing generator (5-section panorama)
│   ├── backtest_report.py         Backtest report + daily recommendation Markdown renderer
│   └── daily_report.py            Markdown report file I/O
│
├── feedback/                      Feedback Loop
│   ├── tracker.py                 SQLite prediction tracking (predictions + outcomes)
│   ├── optimizer.py               Ridge regression weight optimizer (3-stage workflow)
│   └── data_collector.py          Factor warehouse (daily capital/north/hot/dragon-tiger snapshots)
│
├── scripts/                       User Entry Points
│   ├── eod_stock_picker.py        Main entry (strategy/status/briefing/factor collection)
│   ├── run_backtest.py            Backtest entry (--list/--compare)
│   └── verify.py                  23-item health check
│
├── config.yml                     Central configuration (weights/risk/backtest params)
├── SKILL.md                       AI assistant skill definition
├── README.md                      Project documentation (Chinese)
├── README.en.md                   Project documentation (English)
├── CHEATSHEET.md                  Usage cheatsheet (data sources/fuses/budget/coding conventions)
├── requirements.txt               Python dependencies
│
└── data/                          Runtime data (auto-created)
    ├── cache/                     K-line / codes / backtest / factor cache
    ├── db/                        predictions.db (recommendations + outcomes)
    ├── reports/                   Daily reports + briefings
    └── weights/                   Optimizer weight files
```

**Data Flow**

```
Data Layer                 Factor Layer             Strategy Layer           Output Layer
─────────                 ────────────             ──────────────           ────────────
Tencent API ──→ Quotes    FactorLibrary            ShortTermStrategy        market_briefing
mootdx  ──────→ K-line    ├─ calc_main_fund        ├─ prefilter()           generate_daily_report
10jqka  ──────→ Hot       ├─ calc_macd             ├─ enrich_data()         generate_backtest_report
EastMoney ───→ Blocks     ├─ calc_rps              ├─ rank_stocks()         data_collector.collect()
hexin   ──────→ North     ├─ calc_rsi              └─ allocate()
akshare ─────→ BigDeal    ├─ calc_bollinger
asharehub ──→ Tech/Con    └─ calc_fundamental
                                    │
                                    ▼
                              scoring_model
                              ├─ Weighted sum
                              ├─ Rating mapping
                              └─ Reasoning generation
                                    │
                                    ▼
                              feedback/tracker
                              ├─ predictions.db
                              └─ JOIN outcomes
                                    │
                                    ▼
                              feedback/optimizer
                              └─ Ridge → v1.json
```

---

**Data Sources**

- **K-line + Financials (Mootdx TCP)** — primary, never banned, ~0.1s/stock
- **Real-time Quotes (Tencent Finance)** — 5205 stocks, never banned, ~46s
- **Hot Stocks + Themes (10jqka)** — zero auth 73ms
- **North-bound Capital Summary (hexin.cn)** — real-time, zero auth
- **Large-Deal Flow (akshare big_deal)** — full load once then ms queries, independent fuse
- **Per-stock Capital Flow (ASHareHub moneyflow)** — 100/day budget, silent degrade when exhausted, independent fuse
- **Technical Factors (ASHareHub technical)** — dual-source validation, independent fuse
- **Concepts/Sectors (ASHareHub concepts)** — 3-source fusion (10jqka + EastMoney + ASHareHub), independent fuse
- **Financial Indicators (ASHareHub financial)** — long-term primary source, baostock fallback, independent fuse
- **Per-stock North-bound Holdings (asharehub)** — free API Key needed, share volume delta based, independent fuse
- **Sector Membership (East Money push2)** — em_get rate-limited
- **Dragon-Tiger List (East Money datacenter)** — em_get rate-limited
- **Per-stock Fund Flow (akshare)** — blocked overseas, independent fuse
- **Lockup Shares (akshare)** — auxiliary data

Each data source has an **independent circuit breaker** — no source affects another. Fuses auto-recover every 10 minutes (`_recover_sources()`). A tripped source's factor scores default to 50 (neutral), and weights redistribute to active factors.

---

**Short-Term Strategy Details**

**Full Pipeline**

1. `get_all_codes()` — read code cache (0.001s, pulled from akshare on first run)
2. `get_all_quotes()` — Tencent API: 5205 stocks (~46s)
3. `get_ths_hot_stocks()` — Tonghuashun hot stocks + themes (~0.22s)
4. `_prefilter()` — remove ST / low-volume / limit-up stocks (~4900 remain)
5. 5-Dim Preliminary — Liquidity 30 + Activity 20 + Momentum 15 + Risk 20 + PE 15 → Top 200
6. `get_main_fund()` — large-deal fund flow cache (~25s, subsequent 199 calls in ms)
7. `get_kline()` × 200 — mootdx TCP 3-thread parallel K-line + technical scoring (~25s)
8. Cross-sectional RPS — 200-stock 20-day return percentile ranking
9. `scoring_model.score()` — 7-factor weighted score + anti-overfitting check
10. `portfolio_optimizer` — score-weighted position allocation (max 40% / min 10%)
11. `get_stock_blocks()` + `get_dragon_tiger()` — sector and dragon-tiger lookup (Top 3 only)
12. Output briefing + save report — complete before 15:00 CST

**Factor Weights**

- **Capital Flow 25%** — large-deal net inflow > 3M CNY = high score
- **Momentum/RPS 25%** — market-wide percentile → 0-100
- **Technical 15%** — 6-dim technical score (Trend 30 + Bias 20 + Volume 15 + Support 10 + MACD 15 + RSI 10)
- **Volume-Price 10%** — volume ratio + tail-trading structure, 0.8~2.0 = 80pts
- **North-bound 10%** — hexin.cn summary / asharehub holdings, share volume delta direction
- **Hot Theme 10%** — in hot stock list + has theme tags = bonus
- **Dragon-Tiger 5%** — listed + institution net buy > 0 = bonus
- Risk is a filter layer, not a weight factor

---

**Backtest Engine**

**Core Features**

1. **Data** — mootdx real K-line, zero `np.random`, no look-ahead bias
2. **Execution** — next-day open price
3. **Slippage** — 0.1% (configurable)
4. **Commission** — 0.03% (configurable)
5. **Trading Rules** — T+1 take-profit +2%, stop-loss -2% (from config.yml sell section), time-stop T+3
6. **Position Simulation** — allocation_pct weighted, daily T+1 open buy / close sell
7. **Benchmark** — CSI 300 Index

**Output Metrics**

Total trades / Win rate / Avg return T+1/T+5 / Max gain / Max loss / Max drawdown / Sharpe ratio / Strategy total return / Excess return / Equity curve / Monthly returns / Factor IC / Factor win-loss spread / Trade details / Optimization suggestions

**Factor IC Example (2026-04-01 ~ 2026-06-27)**

- **volume_price** — win 55.0 / loss 54.5 / spread +0.4 / IC +0.1202 / Strong
- **technical** — win 77.2 / loss 75.9 / spread +1.3 / IC +0.0916 / Strong
- **hot_theme** — win 64.3 / loss 64.8 / spread -0.4 / IC -0.0394 / Reverse signal (investigate)
- **momentum** — win 96.7 / loss 97.4 / spread -0.7 / IC -0.0382 / Reverse signal (investigate)

IC > 0.03 is a valid signal; IC < -0.03 is a reverse signal.

**Factor Warehouse**

`feedback/data_collector.py` auto-collects capital flow, north-bound, hot stocks, and dragon-tiger data daily into `factor_daily.db`. Data accumulates naturally — day 30 has 30 days of factor data, day 90 has 90 days. No batch backfill needed.

**Known Limitations**

- Capital flow, themes, dragon-tiger, and north-bound data are not available in backtest (current-day large-deal data cannot be retrospected). Backtest only validates price-derived factors (volume-price, momentum, technical). The optimizer on live data handles the rest.
- mootdx provides ~600 trading days (~2.5 years), cannot backtest before 2019.
- Uses next-day open price for execution, cannot simulate intraday fills.

---

**Weight Self-Learning**

**Pipeline**

Live Run → Record Recommendation → Auto-fill T+1 Returns → 60+ records with outcomes → Ridge Regression → Factor variance audit → Collapse check → New Weights → `v1.json`

**Optimizer (`feedback/optimizer.py`)**

- **Algorithm** — `sklearn.linear_model.Ridge(alpha=1.0)`
- **Input** — Factor raw scores → Actual T+1 returns
- **Output** — Normalized new weights (negative coefficients zeroed out, positives normalized to sum 1)
- **Trigger** — Win rate < 50% or 50+ new records since last optimization, with freshness check (hot_theme/dragon_tiger coverage > 30%)
- **Collapse Protection** — Single factor ≥ 80% skipped
- **Missing Columns** — New factors in old records auto-fill 0.5
- **3-Stage Workflow** — `check_and_report()` report only → human approval → `apply_from_report()` executes write
- **Version History** — Old versions timestamped in `data/weights/`, manually comparable

**Latest Backtest Results (2026-04-01 ~ 2026-06-27)**

- Total trades: 95
- Win rate: 57.9%
- Avg return T+1: +1.63%
- Avg return T+5: +5.87%
- Max drawdown: -13.93%
- Sharpe ratio: 4.36
- Strategy return: +62.50%
- CSI 300: +7.56%
- Excess return: +54.94%

---

**File Reference**

- `core/data_engine.py` — Data engine, 14-source unified access, cache, status tracking, failover
- `core/factor_library.py` — Factor library, 30+ factor 0-100 scoring
- `core/scoring_model.py` — Scoring model, weighted sum → rating → reasoning; take-profit/stop-loss from config
- `core/technical_scorer.py` — Technical scoring, 6-dim 100-pt systematic technical analysis
- `core/risk_filter.py` — Risk control, 6 checks, penalty coefficients, skip logic
- `core/backtest_engine.py` — Backtest engine, historical snapshot replay, real K-line returns, factor IC
- `core/backtest_store.py` — Backtest storage, SQLite persistence, --list/--compare version diff
- `core/portfolio_optimizer.py` — Portfolio optimizer, score-weighted / equal-weight position allocation
- `strategies/short_term.py` — Short strategy, 7-factor full-market scan + market assessment
- `strategies/long_term.py` — Long strategy, 6-factor fundamental + north + momentum
- `reports/market_briefing.py` — Market briefing, 5-section panorama report generator
- `reports/backtest_report.py` — Report renderer, backtest + daily recommendation Markdown
- `feedback/tracker.py` — Prediction tracker, SQLite persistent recommendation + outcomes, UNIQUE(date, code, mode)
- `feedback/optimizer.py` — Weight optimizer, Ridge regression 3-stage workflow
- `feedback/data_collector.py` — Factor warehouse, daily capital/north/hot/dragon-tiger snapshots
- `scripts/eod_stock_picker.py` — Main entry, strategy + briefing + backfill + optimizer + factor collection
- `scripts/run_backtest.py` — Backtest entry, full backtest + --list + --compare
- `scripts/verify.py` — Health check, 23-item verification
- `config.yml` — Central configuration, weights / risk / backtest / model params
- `SKILL.md` — AI assistant skill definition, Claude Code / OpenClaw / Hermes interface
- `CHEATSHEET.md` — Usage cheatsheet, data sources / fuses / budget / coding conventions

---

**AI Assistant Integration**

This project supports conversational interaction via `SKILL.md` with AI coding assistants:

- **Claude Code** — auto-detected from `SKILL.md` in repo root
- **OpenClaw** — place `SKILL.md` in `~/.claude/skills/stock-picker/`
- **Hermes** — point skill config to `SKILL.md` path

In conversation, try:

"What's today's pick?" → Run short-term strategy
"Check Moutai fundamentals" → Show ROE / EPS / valuation
"Run a backtest" → Execute backtest engine
"Compare two backtest runs" → View historical backtest diff
"Check system health" → Run verify.py 23-point check

---

**Acknowledgments**

This project references and draws inspiration from:

- **a-stock-data (Simon Lin)** — Data source architecture, em_get rate limiting, hot/sector/dragon-tiger API reference
- **Sequoia-X** — Pattern recognition (golden cross / turtle / flag), RPS ranking
- **daily-stock-analysis** — StockTrendAnalyzer technical scoring system
- **mootdx** — Tongdaxin TCP protocol Python wrapper, stable K-line + financials
- **akshare** — A-share data interface standard, large-deal data and code list

---

**License**

[MIT](LICENSE)

---

If this project helps you, feel free to Star ⭐

Maintained by [cbzhang86](https://github.com/cbzhang86) · Built with Claude Code
