<div align="center">

# 🏛️ A-Share Stock Picker

**Stock-Picker V4 — Full-Stack Multi-Factor Quantitative Stock Selection Framework**

14 Direct Data Sources · 30+ Factors · Short-Term + Long-Term Strategies · Real K-Line Backtest Engine · Ridge Self-Learning Weight Optimization

**English** · [简体中文](README.md)

[![Python](https://img.shields.io/badge/python-3.10%2B-blue)](https://www.python.org/) [![License](https://img.shields.io/badge/license-MIT-green)](LICENSE) [![Compatible with](https://img.shields.io/badge/Claude%20Code-OpenClaw-Hermes-8A2BE2)](SKILL.md)

</div>

---

**Introduction**

Stock-Picker is a full-stack quantitative stock selection system for the Chinese A-share market, covering the complete closed loop from data acquisition, factor computation, strategy scoring to backtest validation.

Three core problems of A-share quantitative investing:

- **Scattered data sources** — quotes, capital flow, dragon-tiger list, north-bound flow are on different platforms
- **API blocking** — East Money's WAF blocks non-browser HTTP requests
- **Hard to validate** — lack of real historical data for proper backtesting

Solved by:

- **Data source priority chain** — mootdx TCP (never banned) → Tencent HTTP → East Money rate-limited
- **Unified rate limiter** — all East Money APIs go through `em_get()` serial throttling
- **Real K-line backtest** — zero `np.random`, no look-ahead bias

---

**Features**

**📊 Data — 14 Sources Unified**

- **mootdx TCP** — K-line + financial snapshots (37 fields), never banned, ~0.1s/stock
- **Tencent Finance HTTP** — 5205 stocks real-time quotes, never banned, ~46s
- **Tonghuashun 10jqka** — hot stocks + theme attribution, zero auth 73ms
- **ASHareHub** — northbound holdings / capital flow / technical factors / concepts / financials, 100 calls/day quota
- **hexin.cn** — north-bound capital summary, zero auth
- **East Money (em_get throttled)** — sector membership / dragon-tiger list
- **akshare** — big-deal capital flow / stock code list
- **Independent fuses** — per-source, auto-recover after 10 minutes

**🧠 Strategy**

1. **Short-Term EOD** — 7-factor scoring + market assessment → prefilter → Top 200 deep evaluation → position allocation. Auto-skip on extremely weak markets.
2. **Long-Term Holding** — ROE + PE + PB fundamental scoring, 3-6 month holding period
3. **Market Environment** — advance/decline ratio / limit-up-down ratio / median gain / hot stock count / north-bound flow → composite score
4. **Technical Scoring** — 6-dimension: Trend 30 + Bias 20 + Volume 15 + Support 10 + MACD 15 + RSI 10
5. **Pattern Recognition** — 9 K-line patterns (golden cross, turtle breakout, high-tight flag, etc.)
6. **Portfolio Optimization** — score-weighted position allocation, max 40% per stock, min 10% floor
7. **Anti-Overfitting** — trims tail if 3rd-place is 20+ points below 1st

**🔄 Self-Learning Feedback Loop**

1. SQLite persists every recommendation with 7-factor breakdown
2. Auto backfill T+1/T+5/T+20 realized returns
3. Ridge regression maps factor scores to actual returns
4. Triggers after 60+ records with T+1 outcomes, every 50 new records
5. Three-stage workflow: `check_and_report()` → approval → `apply_from_report()`
6. New weights persisted to `v1.json`, auto-loaded on next startup
7. Collapse protection (skip if single factor ≥ 80%), missing columns auto-fill 0.5

---

**Quick Start**

**Requirements**

- Python 3.10+, Windows / Linux / macOS
- Network: works both inside and outside China (mootdx TCP and Tencent API are globally accessible)

**Installation**

```bash
git clone https://github.com/cbzhang86/stock-picker.git
cd stock-picker
pip install -r requirements.txt

# Optional: register free ASHareHub API Key
export ASHAREHUB_API_KEY="ash_your_key_here"
```

**First Run**

```bash
python scripts/eod_stock_picker.py --mode short
```

You'll see: 5205 stock codes → full market quotes → Tonghuashun hot stocks → prefilter → preliminary scoring → Top 200 deep evaluation → big-deal cache → final recommendations. About 4 minutes later, the daily briefing is output with market overview, theme heat TOP10, short-term rankings, long-term picks.

**Other Commands**

```bash
# View model status
python scripts/eod_stock_picker.py --status

# Long-term strategy
python scripts/eod_stock_picker.py --mode long

# Backtest (default last 3 months)
python scripts/run_backtest.py --mode short

# Backtest version comparison
python scripts/run_backtest.py --list
python scripts/run_backtest.py --compare 1 2

# Health check (23 items)
python scripts/verify.py
```

---

**System Architecture**

```
stock-picker/
│
├── core/                          Core Engine
│   ├── data_engine.py             Multi-source data fusion (14 sources, cache, fuses)
│   ├── factor_library.py          30+ factors 0-100 scoring
│   ├── scoring_model.py           Weighted scoring + rating + weight loading
│   ├── technical_scorer.py        6-dim 100pt technical analysis
│   ├── risk_filter.py             6-level risk check
│   ├── backtest_engine.py         Backtest engine (mootdx real K-line snapshots)
│   ├── backtest_store.py          Backtest result persistence + version compare
│   └── portfolio_optimizer.py     Position allocation (scoring-weighted/equal)
│
├── strategies/                    Strategy Layer
│   ├── short_term.py              Short-term EOD strategy (7 factors + market assessment)
│   ├── long_term.py               Long-term strategy (6 factors fundamental)
│   └── base.py                    Abstract strategy base class
│
├── reports/                       Report Layer
│   ├── market_briefing.py         Daily market briefing generator (5 sections)
│   ├── backtest_report.py         Backtest report + daily recommendation rendering
│   └── daily_report.py            Markdown report file I/O
│
├── feedback/                      Feedback Loop
│   ├── tracker.py                 SQLite prediction tracking (predictions + outcomes)
│   ├── optimizer.py               Ridge regression weight optimizer (3-stage workflow)
│   └── data_collector.py          Factor warehouse (daily snapshots)
│
├── scripts/                       User Entry Points
│   ├── eod_stock_picker.py        Main entry (strategy/status/briefing/collector)
│   ├── run_backtest.py            Backtest entry (--list/--compare)
│   └── verify.py                  23-item health check
│
├── config.yml                     Central configuration
├── SKILL.md                       AI assistant skill definition
├── README.md                      Project documentation (Chinese)
├── README.en.md                   Project documentation (English)
├── CHEATSHEET.md                  Quick reference
├── requirements.txt               Python dependencies
│
└── data/                          Runtime data (auto-created)
    ├── cache/                     K-line/code/backtest/factor cache
    ├── db/                        predictions.db
    ├── reports/                   Daily reports + briefings
    └── weights/                   Optimizer weight files
```

---

**Short-Term Strategy Details**

**Full Pipeline**

1. `get_all_codes()` — read code cache (0.001s)
2. `get_all_quotes()` — Tencent API 5205 stocks (~46s)
3. `get_ths_hot_stocks()` — Tonghuashun hot stocks + themes (~0.22s)
4. `_prefilter()` — remove ST/low-volume/limit-up-down (~4900 remain)
5. 5-dim preliminary: liquidity 30 + activity 20 + momentum 15 + risk 20 + PE 15 → Top 200
6. `get_main_fund()` — big-deal fund flow cache (~25s, subsequent 199 calls in ms)
7. `get_kline()` × 200 — mootdx TCP 3-thread parallel (~25s)
8. Cross-sectional RPS ranking
9. `scoring_model.score()` — 7-factor weighted + gap check
10. `portfolio_optimizer` — score-weighted position allocation (max 40%/min 10%)
11. `get_stock_blocks()` + `get_dragon_tiger()` — sector and dragon-tiger for Top 3
12. Output briefing + save report — complete before 15:00

**Factor Weights**

| Factor | Weight | Data Source | Logic |
|--------|--------|-------------|-------|
| Capital Flow | 25% | Big deal / ASHareHub / THS | Cross-sectional percentile ranking |
| Momentum/RPS | 25% | Market-wide percentile | 20-day return → 0-100 |
| Technical | 15% | mootdx K-line 6-dim | Trend 30 + Bias 20 + Volume 15 + Support 10 + MACD 15 + RSI 10 |
| Volume-Price | 10% | Volume ratio + tail structure | 0.8~2.0 = 80pt |
| North-bound | 10% | hexin.cn / asharehub | Shareholding volume change direction |
| Hot Theme | 10% | 10jqka hot stocks | In hot list + has theme tags |
| Dragon-Tiger | 5% | East Money datacenter | Listed + institution net buy > 0 |
| Risk | filter | risk_filter.py | Blocks severe risks, no weight |

---

**Backtest Engine**

**Core Characteristics**

1. **Data** — mootdx real K-line, zero `np.random`, no look-ahead bias
2. **Execution** — next day open price
3. **Slippage** — 0.1% (configurable)
4. **Commission** — 0.03% (configurable)
5. **Trading Rules** — T+1 take-profit +2%, stop-loss -2% (from `config.yml sell`), T+3 time stop
6. **Position Simulation** — by `allocation_pct`, daily T+1 buy open / sell close
7. **Benchmark** — CSI 300

**Known Limitations**

- Capital flow / themes / dragon-tiger / north-bound data are not available in backtest
- mootdx covers ~600 trading days (~2.5 years)
- Uses next-day open price, cannot simulate intraday fills

---

**Weight Self-Learning**

Live run → auto backfill T+1 → 60+ records → Ridge regression → factor variance audit → collapse check → new weights → `v1.json`

**Optimizer (`feedback/optimizer.py`)**

- **Algorithm** — `sklearn.linear_model.Ridge(alpha=1.0)`
- **Input** — factor raw scores → actual T+1 returns
- **Output** — normalized weights (negative → 0, positive → sum 1)
- **Trigger** — win rate < 50% or 50+ new records since last optimization
- **Collapse protection** — skip if single factor ≥ 80%
- **Missing columns** — auto-fill 0.5
- **3-stage workflow** — `check_and_report()` → approval → `apply_from_report()`
- **Version tracking** — old versions in `data/weights/` (timestamped)

**Latest Backtest (2026-04-01 ~ 2026-06-27)**

- Total trades: 95
- Win rate: 57.9%
- Avg T+1 return: +1.63%
- Avg T+5 return: +5.87%
- Max drawdown: -13.93%
- Sharpe ratio: 4.36
- Strategy return: +62.50%
- CSI 300: +7.56%
- Excess return: +54.94%

---

**AI Assistant Integration**

Supported via `SKILL.md`:

- **Claude Code** — auto-detected from project root
- **OpenClaw** — place `SKILL.md` into `~/.claude/skills/stock-picker/`
- **Hermes** — point skill config to `SKILL.md` path

Example prompts:

> "What's the market like today?" → runs full pipeline
> "Show me fundamentals for 600519" → ROE/EPS/valuation snapshot
> "Run a backtest" → executes backtest engine
> "Compare two backtest runs" → version comparison
> "Check system health" → runs verify.py 23 checks

---

**Acknowledgments**

- **a-stock-data (Simon Lin)** — data source architecture, em_get rate limiter, THS hot/block/dragon API reference
- **Sequoia-X** — pattern recognition strategies, RPS ranking
- **daily-stock-analysis** — StockTrendAnalyzer technical scoring system
- **mootdx** — Tongdaxin TCP protocol Python wrapper
- **akshare** — A-share data API standard

---

**License**

[MIT](LICENSE)

---

Maintained by [cbzhang86](https://github.com/cbzhang86) · Developed with Claude Code
