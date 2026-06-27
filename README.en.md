<div align="center">
  
# 🏛️ A-Share Stock Picker

**Stock-Picker — Full-Stack Multi-Factor Quantitative Stock Selection Framework**

[![Python](https://img.shields.io/badge/python-3.10%2B-blue)](https://www.python.org/)
[![License](https://img.shields.io/badge/license-MIT-green)](LICENSE)
[![Compatible with](https://img.shields.io/badge/Claude%20Code-OpenClaw-Hermes-8A2BE2)](SKILL.md)

14 Direct Data Sources · 30+ Factors · Short-Term + Long-Term Strategies · Backtest Engine · Self-Learning Weight Optimization

**English** · [简体中文](README.md)

</div>

---

## 📋 Table of Contents

- [Introduction](#-introduction)
- [Features](#-features)
- [Quick Start](#-quick-start)
- [Usage Examples](#-usage-examples)
- [System Architecture](#-system-architecture)
- [Strategy Details](#-short-term-strategy-details)
- [Data Sources](#-data-sources)
- [Backtest Engine](#-backtest-engine)
- [Weight Self-Learning](#-weight-self-learning)
- [File Reference](#-file-reference)
- [AI Assistant Integration](#-ai-assistant-integration)
- [Acknowledgments](#-acknowledgments)
- [License](#-license)

---

## 📖 Introduction

Stock-Picker is a **full-stack quantitative stock selection system** for the Chinese A-share market, covering the complete closed loop from data acquisition, factor computation, strategy scoring to backtest validation.

### Problem Statement

A-share quantitative investing faces three core challenges:

1. **Scattered data sources** — real-time quotes, capital flows, dragon-tiger list, north-bound capital are spread across different platforms
2. **API blocking** — East Money's WAF frequently blocks non-browser HTTP requests
3. **Hard to validate** — lack of real historical data for proper backtesting

This project solves these through a **priority fallback chain** (mootdx TCP never blocked → Tencent HTTP → East Money throttled), a **unified rate limiter** (`em_get()`), and a **real K-line backtest engine** with zero look-ahead bias.

### Core Value

| Dimension | Description |
|-----------|-------------|
| **Data** | 14 direct data sources, mootdx TCP never blocked, East Money APIs unified rate-limited, ASHareHub 100/day budget |
| **Strategy** | Short-term (8 factors) + Long-term (6 factors), market-aware skip |
| **Risk Control** | 6-layer filter (ST/limit-up-down/liquidity/turnover), anti-overfitting score truncation |
| **Backtest** | Real K-line data (zero `np.random`), historical snapshot replay, slippage+commission included, equity curve/Sharpe/IC output |
| **Self-Learning** | Ridge regression auto-optimizes factor weights after 60+ records with outcomes |
| **Integrable** | Compatible with Claude Code / OpenClaw / Hermes via SKILL.md |

---

## ✨ Features

<details open>
<summary><b>📊 Data Capabilities</b></summary>

| Capability | Source | Latency | IP Ban Risk |
|-----------|--------|:-------:|:-----------:|
| Full Market Quotes (5205 stocks) | Tencent Finance | ~46s | 🟢 Never |
| Daily K-Line | mootdx TCP | ~0.1s/stock | 🟢 Never |
| Fundamental Snapshot (37 fields) | mootdx TCP | ~0.1s/stock | 🟢 Never |
| Hot Stocks + Theme Attribution | 10jqka (Tonghuashun) | ~0.22s | 🟢 Minimal |
| North-bound Capital Summary | hexin.cn | Realtime | 🟢 Minimal |
| Per-stock Capital Flow | ASHareHub moneyflow | ~4s/stock | 🟢 Free API Key |
| Technical Factors | ASHareHub technical | ~4s/stock | 🟢 Dual-source validation |
| Concept/Sector Membership | ASHareHub concepts | ~4s/stock | 🟢 3-source fusion |
| Financial Indicators | ASHareHub financial | ~4s/stock | 🟢 baostock fallback |
| Per-stock North-bound Holdings | asharehub (API Key) | ~4s/stock | 🟢 Free API Key |
| Sector Membership | East Money slist | ~2.3s/stock | 🟡 Rate-limited |
| Dragon-Tiger List + Seats | East Money datacenter | ~6s/stock | 🟡 Rate-limited |
| Large-Deal Capital Flow (685 stocks) | akshare big_deal | ~25s full load | 🟢 Low |

</details>

<details open>
<summary><b>🧠 Strategy Capabilities</b></summary>

- **Short-Term (EOD)** : 8-factor weighted scoring + market assessment → pre-filter → preliminary score → Top 200 deep evaluation → position allocation
- **Long-Term Holding** : ROE + PE + PB fundamental scoring, 3-6 month holding period
- **Market-Aware** : Up/down ratio, limit-up/down count, median return, north-bound flow → composite score, auto-skip in extremely weak markets
- **Pattern Recognition** : 9 K-line patterns auto-detected (golden cross, turtle breakout, high-tight flag, etc.)
- **Technical Scoring** : 6-dimension systematic scoring (Trend 30 + Bias 20 + Volume 15 + Support 10 + MACD 15 + RSI 10)
- **Portfolio Optimization** : Score-weighted position allocation, max 40% per stock, min 10% floor
- **Anti-Overfitting** : Trims tail if 3rd-place score is 20+ points below 1st

</details>

<details open>
<summary><b>🔄 Self-Learning Capabilities</b></summary>

- SQLite persistent storage of every recommendation with factor breakdown
- Auto backfill of T+1/T+5/T+20 realized returns
- Ridge regression mapping factor scores to actual returns
- Auto-triggered optimization after 60+ records with outcomes (`(date, code, mode)` UNIQUE constraint prevents duplicates)
- Weights persisted to `v1.json`, auto-loaded on next startup

</details>

---

## 🚀 Quick Start

### Prerequisites

- Python 3.10+
- Windows / Linux / macOS
- Network: works both inside and outside China (mootdx TCP and Tencent API are globally accessible)

### Installation

```bash
git clone https://github.com/cbzhang86/stock-picker.git
cd stock-picker
pip install -r requirements.txt
```

### First Run

```bash
# Run short-term strategy (~4 minutes total)
python scripts/eod_stock_picker.py --mode short
```

**Console output during run:**

```
PREHEAT_DONE 347

A股代码列表: 5205 只
全市场行情: 5205 只股票
同花顺强势股: 100 只有题材归因标签
TOP5 热门题材: ['医药', '一带一路', '军工航天', '工业母机', '数字经济']
预过滤后 4927 只进入详评
初步评分排序，前10只: [('600141', '兴发集团', 82.99), ...]
详评 200 只（初步评分前200）
全市场大单数据已加载: 685 只股票
详评完成: 200 只
推荐 3 只股票
```

**Final daily briefing output:**

```
┌─────────────────────────────────────────────┐
│  A股智能监测 · 每日简报 2026-06-12          │
└─────────────────────────────────────────────┘

📊 市场概览
    上涨/下跌: 2836/1872
    成交额: 10,842 亿
    北向资金: 净流出 -40.38亿（沪-9.28 / 深-31.1）

🔥 题材热度 TOP 10
    1. 医药（15只）→ 片仔癀 +3.2%
    2. 一带一路（12只）→ 中国交建 +2.8%
    3. 军工航天（10只）→ 中航沈飞 +4.1%

📈 短线评分排名
    1. 000001 平安银行 评分85/100 仓位39%
       因子: capital_flow 85 | momentum 80 | risk 100
       板块: 银行、破净股
       龙虎榜: 净买入 50,290 万
       
📌 长线关注（ROE+PE 评分）
    1. 600519 贵州茅台 评分78/100 仓位25%
       ROE 10.1% EPS 217.9 PE 25
```

### Other Commands

```bash
# View model status and recent performance
python scripts/eod_stock_picker.py --status

# Long-term strategy
python scripts/eod_stock_picker.py --mode long

# Run backtest
python scripts/run_backtest.py --mode short --start 2025-06-01 --end 2026-06-27

# Run 23-point health check
python scripts/verify.py
```

---

## 🎯 Usage Examples

### Example 1: Market Overview

```python
from core.data_engine import DataEngine

de = DataEngine()
quotes = de.get_all_quotes()

# Gainers vs losers
up = (quotes['pct_chg'] > 0).sum()
down = (quotes['pct_chg'] <= 0).sum()
turnover = quotes['amount'].sum()

print(f"Up {up} / Down {down}, Turnover {turnover:.0f} B CNY")

# Top 10 by volume
top = quotes.nlargest(10, 'amount')
print(top[['code', 'name', 'price', 'pct_chg', 'amount']])
```

### Example 2: Stock Fundamental Analysis

```python
code = '600519'  # Kweichow Moutai

# Financial snapshot
fin = de.get_financial_snapshot(code)
print(f"ROE: {fin['roe']:.1f}%")
print(f"EPS: {fin['eps']:.2f}")
print(f"Revenue: {fin['income']/1e8:.2f}B")

# Technical score
from core.technical_scorer import TechnicalScorer
kline = de.get_kline(code)
tech = TechnicalScorer().score(kline)
print(f"Technical Score: {tech.total}/100 ({tech.signal})")

# Sector membership
blocks = de.get_stock_blocks(code)
print(f"Sectors: {', '.join(blocks['concept_tags'][:5])}")

# Dragon-tiger list
dt = de.get_dragon_tiger(code)
if dt['records']:
    print(f"Dragon-Tiger: Net buy {dt['records'][0]['net_buy_wan']:.0f}K CNY")
```

### Example 3: Backtest

```bash
python scripts/run_backtest.py --mode short --start 2026-04-01 --end 2026-06-27
```

Output:

```
# 回测报告：short_strategy
**区间**: 2026-04-01 ~ 2026-06-27

## 📊 基本统计
- **总交易次数**: 95 次
- **胜率**: 57.9%
- **平均收益(T+1)**: +1.63%
- **最大回撤**: -13.93%
- **夏普比率**: 4.36

### 收益对比
- **沪深300**: +7.56%
- **策略收益**: +62.50%
- **超额收益**: +54.94%

## 📈 权益曲线
```
▁▁▁▁▁▁▁▂▃▂▂▂▃▄▅▅▅▅▄▄▄▄▄▅▅▅▅▄▄▄▄▅▅▅█▆▆▆▄▄
¥1000000 ~ ¥1339783 (最终: ¥1625008)
```

---

## 🏗 System Architecture

```
stock-picker/
│
├── core/                          # Core Engine
│   ├── data_engine.py             Multi-source data fusion (14 sources)
│   ├── factor_library.py          30+ factor calculations
│   ├── scoring_model.py           Weighted scoring + rating + weight loading
│   ├── technical_scorer.py        Systematic technical scoring (6-dim 100-pt)
│   ├── risk_filter.py             6-layer risk check
│   ├── backtest_engine.py         Backtest engine (real K-line, zero random)
│   ├── backtest_store.py          SQLite backtest persistence, version compare
│   └── portfolio_optimizer.py     Position allocation (score-weighted)
│
├── strategies/                    # Strategy Layer
│   ├── short_term.py              Short-term EOD strategy (8 factors)
│   ├── long_term.py               Long-term holding strategy (ROE+PE)
│   ├── sequoia_patterns.py        9 K-line pattern detection functions
│   └── base.py                    Abstract strategy base class
│
├── reports/                       # Report Layer
│   ├── market_briefing.py         Daily market briefing generator
│   ├── backtest_report.py         Backtest report + daily report renderer
│   └── daily_report.py            Markdown report file I/O
│
├── feedback/                      # Feedback Loop
│   ├── tracker.py                 SQLite prediction tracking
│   ├── optimizer.py               Ridge regression weight optimizer
│   └── data_collector.py          Factor warehouse (daily capital/north/hot/dragon-tiger snapshots)
│
├── scripts/                       # User Entry Points
│   ├── eod_stock_picker.py        Main entry (strategy/status/briefing/factor collection)
│   ├── run_backtest.py            Backtest entry
│   └── verify.py                  23-item health check
│
├── config.yml                     Central configuration
├── SKILL.md                       AI assistant skill definition
├── README.md                      Project documentation (Chinese)
├── README.en.md                   Project documentation (English)
├── requirements.txt               Python dependencies
├── CHEATSHEET.md                  Usage cheatsheet (for AI assistants)
│
└── data/                          Runtime data (auto-created)
    ├── cache/                     K-line / codes / backtest / factor cache
    │   ├── kline_cache.db         K-line cache (SQLite WAL)
    │   ├── codes_cache.json       A-share code list cache
    │   ├── backtest_cache.db      Backtest result cache
    │   └── factor_daily.db        Factor warehouse (daily capital/north/hot/dragon-tiger snapshots)
    ├── db/                        Database
    │   └── predictions.db         Recommendations + outcomes (SQLite)
    ├── reports/                   Daily reports + briefings
    └── weights/                   Optimizer weight files
```

### Data Flow

```
Data Layer                Factor Layer            Strategy Layer          Output Layer
──────────                ────────────            ──────────────          ────────────
Tencent API ──→ Quotes    FactorLibrary           ShortTermStrategy       market_briefing
mootdx  ──────→ K-line    ├─ calc_main_fund       ├─ prefilter()          generate_daily_report
10jqka  ──────→ Hot       ├─ calc_macd            ├─ enrich_data()        generate_backtest_report
EastMoney ───→ Blocks     ├─ calc_rps             ├─ rank_stocks()        data_collector.collect()
hexin   ──────→ North     ├─ calc_rsi             └─ allocate()           ├─ capital_flow
akshare ─────→ BigDeal    ├─ calc_bollinger                               ├─ north_flow
asharehub ──→ Tech/Con    └─ calc_fundamental                             ├─ hot_theme
                                │                                          └─ dragon_tiger
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

## 📈 Short-Term Strategy Details

### Full Pipeline

```
⏱ 14:50 Start
   │
   ▼
📡 get_all_codes()         Read code cache (0.001s, pulled from akshare on first run)
   │
   ▼
📡 get_all_quotes()        Tencent API: 5205 stocks (~46s)
   │
   ▼
📡 get_ths_hot_stocks()    Tonghuashun hot stocks + themes (~0.22s)
   │
   ▼
🔍 _prefilter()            Remove ST / low-volume / limit-up stocks (~4900 remain)
   │
   ▼
🔍 5-Dim Preliminary       Liquidity 30 + Activity 20 + Momentum 15 + Risk 20 + PE 15 → Top 200
   │
   ▼
📡 get_main_fund()         Large-deal fund flow cache (~25s, subsequent 199 calls in ms)
   │
   ▼
📡 get_kline() × 200       mootdx TCP 3-thread parallel K-line + technical scoring (~25s)
   │
   ▼
🔍 Cross-sectional RPS     200-stock 20-day return percentile ranking
   │
   ▼
🔍 scoring_model.score()   8-factor weighted score + anti-overfitting check
   │
   ▼
📊 portfolio_optimizer     Score-weighted position allocation (max 40% / min 10%)
   │
   ▼
📡 get_stock_blocks()      Sector attribution (Top 3 only, rate-limited)
📡 get_dragon_tiger()      Dragon-tiger list (Top 3 only)
   │
   ▼
✅ Output briefing + save  Complete before 15:00
```

### Factor Weights

| Factor | Weight | Data Source | Scoring Logic |
|--------|:------:|-------------|---------------|
| Capital Flow | **25%** | ASHareHub moneyflow → big_deal cache → ths | Net inflow > 3M = high score |
| Momentum/RPS | **25%** | Market-wide percentile | Percentile → 0-100 |
| Technical | **15%** | Dual-source (K-line calc + ASHareHub) | Consistent → avg+5, disagree → conservative |
| Volume-Price | **10%** | Volume ratio + tail-trading structure | 0.8~2.0 = 80pts, tail-bonus ×0.3 |
| Risk Control | **10%** | Risk filter | Pass = 100, each violation penalizes |
| North-bound | **10%** | hexin.cn summary / asharehub holdings | Directional / volume change score |
| Hot Theme | **10%** | 3-source fusion (10jqka + EastMoney + ASHareHub) | In hot list + has theme tags = bonus |
| Dragon-Tiger | **5%** | East Money datacenter | Listed + institution net buy > 0 = bonus |

---

## 🔧 Data Sources

| Data | Source | Priority | Protocol | Anti-Ban | Status |
|------|--------|:-------:|----------|----------|:------:|
| K-Line + Financials | mootdx (Tongdaxin) | 🥇 **Primary** | TCP 7709 | Never banned | ✅ |
| Real-time Quotes | Tencent Finance | 🥇 **Primary** | HTTP | Never banned | ✅ |
| Hot Stocks | 10jqka (Tonghuashun) | 🥈 | HTTP | Zero auth | ✅ |
| North-bound Capital Summary | hexin.cn | 🥈 | HTTP | Zero auth | ✅ |
| Large-Deal Flow (685 stocks) | akshare big_deal | 🥈 | HTTP | Independent fuse | ✅ |
| Per-stock Capital Flow | ASHareHub moneyflow | 🥈 | HTTP | 100/day budget, silent degrade | ✅ |
| Technical Factors | ASHareHub technical | 🥈 | HTTP | Dual-source validation | ✅ |
| Concept/Sector Membership | ASHareHub concepts | 🥈 | HTTP | 3-source fusion | ✅ |
| Financial Indicators | ASHareHub financial | 🥈 | HTTP | baostock fallback | ✅ |
| Per-stock North-bound | asharehub (API Key) | 🥈 | HTTP | Free API key needed | ✅ |
| Sector Membership | East Money slist | 🥉 | HTTP | em_get rate-limited | ✅ |
| Dragon-Tiger List | East Money datacenter | 🥉 | HTTP | em_get rate-limited | ✅ |
| Per-stock Fund Flow | akshare | — | HTTP | ❌ Blocked overseas, independent fuse | ⛔ |

### Independent Source-Level Fuse + Auto Recovery

The system uses `_source_available` dict for source-level independent fusing — each endpoint is isolated. **Fuses auto-recover every 10 minutes** (`_recover_sources()`) so network glitches don't permanently disable a source:

```python
_source_available = {
    'big_deal': True,
    'capital_flow': False,
    'north_flow': True,
    'push2': False,
    'asharehub_moneyflow': True,
    'asharehub_tech_factors': True,
    'asharehub_concepts': True,
    'asharehub_financial': True,
}
```

### ASHareHub Daily Budget

4 ASHareHub endpoints share a **100 calls/day** budget. Depleted endpoints silently return None (data-unavailable). Budget resets automatically the next day.

---

## ⚙️ Backtest Engine

### Core Features

| Feature | Implementation |
|---------|---------------|
| Data Source | mootdx real K-line, **zero `np.random`** |
| Execution | Next-day open price |
| Slippage | 0.1% (configurable) |
| Commission | 0.03% (configurable) |
| Trading Rules | T+1 take-profit +2%, stop-loss -2%, time-stop T+3 |
| Position Simulation | Score-weighted allocation, daily T+1 open buy / close sell |
| Benchmark | CSI 300 Index |

### Output Metrics

```
✓ Total Trades        ✓ Win Rate           ✓ Avg Return (T+1/T+5)
✓ Max Gain            ✓ Max Loss           ✓ Max Drawdown
✓ Sharpe Ratio        ✓ Total Return       ✓ Excess Return
✓ Equity Curve       ✓ Monthly Returns    ✓ Factor Attribution (IC/spread)
✓ Trade Details      ✓ Optimization Tips
```

### Factor IC Example

| Factor | Avg Win Score | Avg Loss Score | Spread | IC | Verdict |
|--------|:------------:|:-------------:|:-----:|:--:|:-------:|
| volume_price | 55.0 | 54.5 | +0.4 | +0.1202 | Strong ✅ |
| technical | 77.2 | 75.9 | +1.3 | +0.0916 | Strong ✅ |
| hot_theme | 64.3 | 64.8 | -0.4 | -0.0394 | Reverse (investigate) |
| momentum | 96.7 | 97.4 | -0.7 | -0.0382 | Reverse (investigate) |

### Factor Warehouse

`feedback/data_collector.py` auto-collects capital flow, north-bound, hot stocks, and dragon-tiger data daily into `factor_daily.db`. Data accumulates naturally — day 30 has 30 days of factor data for backtest, day 90 has 90 days. No batch backfill needed.

### Known Limitations

- Capital flow, themes, dragon-tiger, and north-bound data are **not available** in backtest (current-day large-deal data cannot be retrospected)
- Backtest only validates price-derived factors (volume-price, momentum, technical). The optimizer on live data handles the rest
- mootdx provides ~600 trading days (~2.5 years)
- Uses next-day open price for execution, cannot simulate intraday fills

---

## 🔄 Weight Self-Learning

```
Live Run → Record Recommendation → Auto-fill T+1 Returns
  → 60+ records with outcomes → Ridge Regression
  → Factor variance audit → Collapse check → New Weights → v1.json
                                                              ↓
                                                       Auto-loaded on next startup
```

The weight optimizer (`feedback/optimizer.py`):

- **Algorithm**: `sklearn.linear_model.Ridge(alpha=1.0)`
- **Input**: Factor raw scores → Actual T+1 returns
- **Output**: Normalized new weights (negative coefficients zeroed out, positives normalized to sum 1)
- **Trigger**: Win rate < 50% or every 50 records, data freshness check (hot_theme/dragon_tiger coverage > 30%)
- **Protection**: Single-factor collapse guard (≥ 80% → skip), missing column auto-fill 0.5
- **Workflow**: 3-stage `check_and_report()` → approval → `apply_from_report()`, does not auto-write weights
- **Rollback**: Old versions preserved in `data/weights/` (timestamped)

---

## 📁 File Reference

| File | Role | Description |
|------|------|-------------|
| `core/data_engine.py` | Data Engine | 14-source unified access, caching, status, failover |
| `core/factor_library.py` | Factor Library | 30+ factor 0-100 scoring logic |
| `core/scoring_model.py` | Scoring Model | Weighted sum → rating → reasoning, weight redistribution |
| `core/technical_scorer.py` | Technical Scoring | 6-dim 100-pt systematic technical analysis |
| `core/risk_filter.py` | Risk Control | 6 checks, penalty coefficients, skip logic |
| `core/backtest_engine.py` | Backtest Engine | Historical snapshot replay, real K-line returns, factor IC |
| `core/backtest_store.py` | Backtest Storage | SQLite persistence, --list/--compare version diff |
| `core/portfolio_optimizer.py` | Portfolio Optimizer | Score-weighted / equal-weight position allocation |
| `strategies/short_term.py` | Short Strategy | 8-factor full-market scan, pre-score, deep evaluation |
| `strategies/long_term.py` | Long Strategy | 6-factor fundamental + north + momentum |
| `strategies/sequoia_patterns.py` | Pattern Detection | 9 K-line pattern detection functions |
| `reports/market_briefing.py` | Market Briefing | 5-section market panorama generator |
| `reports/backtest_report.py` | Report Renderer | Backtest report + daily recommendation in Markdown |
| `reports/daily_report.py` | Report Storage | Markdown file read/write |
| `feedback/tracker.py` | Prediction Tracker | SQLite persistent recommendation + outcomes |
| `feedback/optimizer.py` | Weight Optimizer | Ridge regression, 3-stage workflow |
| `feedback/data_collector.py` | Factor Warehouse | Daily capital/north/hot/dragon-tiger snapshots |
| `scripts/eod_stock_picker.py` | Main Entry | Strategy + briefing + backfill + optimizer + factor collection |
| `scripts/run_backtest.py` | Backtest Entry | Full backtest + --list + --compare |
| `scripts/verify.py` | Health Check | 23-item verification (modules/connectivity/scoring/pipeline/integrity) |
| `config.yml` | Configuration | Weights / risk / backtest / model params |
| `SKILL.md` | AI Skill | Claude Code / OpenClaw / Hermes interface definition |
| `CHEATSHEET.md` | Cheatsheet | Data sources / fuses / budget / coding conventions quick reference |

---

## 🤖 AI Assistant Integration

This project supports conversational interaction via `SKILL.md` with AI coding assistants:

| Platform | Usage |
|----------|-------|
| **Claude Code** | Auto-detected from `SKILL.md` in repo root |
| **OpenClaw** | Place `SKILL.md` in `~/.claude/skills/stock-picker/` |
| **Hermes** | Point skill config to `SKILL.md` path |

In conversation, try:

> "What's today's pick?" → Run short-term strategy
> "Check Moutai fundamentals" → Show ROE/EPS/valuation
> "What's trending today?" → Tonghuashun hot stocks
> "Run a backtest" → Execute backtest engine
> "Compare two backtest runs" → View historical backtest diff
> "Check system health" → Run verify.py 23-point check

---

## 🙏 Acknowledgments

This project references and draws inspiration from:

| Project | Contribution | Repository |
|---------|-------------|------------|
| **a-stock-data** (Simon Lin) | Data source architecture, em_get rate limiting, hot/sector/dragon-tiger API reference | [simonlin1212/a-stock-data](https://github.com/simonlin1212/a-stock-data) |
| **Sequoia-X** | Pattern recognition (golden cross/turtle/flag), RPS ranking | Reference project |
| **daily-stock-analysis** | StockTrendAnalyzer technical scoring system | Reference project |
| **mootdx** | Tongdaxin TCP protocol Python wrapper, stable K-line + financials | [mootdx](https://github.com/bopo/mootdx) |
| **akshare** | A-share data interface standard, large-deal data and code list | [akshare](https://github.com/jindaxiang/akshare) |

---

## 📄 License

[MIT](LICENSE)

---

<div align="center">

**If this project helps you, feel free to Star ⭐**

Maintained by [cbzhang86](https://github.com/cbzhang86) · Built with Claude Code

</div>