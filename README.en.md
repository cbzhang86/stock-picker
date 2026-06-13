<div align="center">
  
# 🏛️ A-Share Stock Picker

**Stock-Picker — Full-Stack Multi-Factor Quantitative Stock Selection Framework**

[![Python](https://img.shields.io/badge/python-3.10%2B-blue)](https://www.python.org/)
[![License](https://img.shields.io/badge/license-MIT-green)](LICENSE)
[![Compatible with](https://img.shields.io/badge/Claude%20Code-OpenClaw-Hermes-8A2BE2)](SKILL.md)

13 Direct Data Sources · 30+ Factors · Short-Term + Long-Term Strategies · Backtest Engine · Self-Learning Weight Optimization

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
2. **API blocking** — East Money's WAF frequently blocks non-browser HTTP requests from overseas IPs
3. **Hard to validate** — lack of real historical data for proper backtesting

This project solves these through a **priority fallback chain** (mootdx TCP never blocked → Tencent HTTP → East Money throttled), a **unified rate limiter** (`em_get()`), and a **real K-line backtest engine**.

### Core Value

| Dimension | Description |
|-----------|-------------|
| **Data** | 13 direct data sources, mootdx TCP never blocked, East Money APIs unified rate-limited |
| **Strategy** | Short-term (8 factors) + Long-term (6 factors), customizable weights |
| **Risk Control** | 6-layer filter (ST/limit-up-down/liquidity/turnover), anti-overfitting score truncation |
| **Backtest** | Real K-line data (zero `np.random`), slippage+commission included, equity curve output |
| **Self-Learning** | Ridge regression auto-optimizes factor weights after 30+ records |
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
| Stock Sector Membership | East Money slist | ~2.3s/stock | 🟡 Rate-limited |
| Dragon-Tiger List + Seats | East Money datacenter | ~6s/stock | 🟡 Rate-limited |
| Large-Deal Capital Flow (685 stocks) | akshare | ~25s full load | 🟢 Low |

</details>

<details open>
<summary><b>🧠 Strategy Capabilities</b></summary>

- **Short-Term (EOD)** : 8-factor weighted scoring → pre-filter → preliminary score → Top 200 deep evaluation → position allocation
- **Long-Term Holding** : ROE + PE + PB fundamental scoring, 3-6 month holding period
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
- Auto-triggered optimization after 30+ records
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
python scripts/run_backtest.py --mode short --start 2025-06-01 --end 2026-06-11
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
python scripts/run_backtest.py --mode short --start 2026-01-01 --end 2026-06-11
```

Output:

```
# 回测报告：short_strategy
**区间**: 2026-01-01 ~ 2026-06-11

## 📊 基本统计
- **总交易次数**: 156 次
- **胜率**: 63.5%
- **平均收益(T+1)**: +1.2%
- **最大回撤**: -8.3%
- **夏普比率**: 1.35

### 收益对比
- **沪深300**: +5.2%
- **策略收益**: +18.7%
- **超额收益**: +13.5%

## 📈 权益曲线
```
▁▂▃▃▄▅▆▇██▇▆▅▆▇▇██▇▇▆▅▆▇▇██▇▆▅▅▆▇▇████
¥980000 ~ ¥1187000 (最终: ¥1187000)
```
```

---

## 🏗 System Architecture

```
stock-picker/
│
├── core/                          # Core Engine
│   ├── data_engine.py             Multi-source data fusion (13 sources)
│   ├── factor_library.py          30+ factor calculations
│   ├── scoring_model.py           Weighted scoring + rating + weight loading
│   ├── technical_scorer.py        Systematic technical scoring (6-dim 100-pt)
│   ├── risk_filter.py             6-layer risk check
│   ├── backtest_engine.py         Backtest engine (real K-line, zero random)
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
│   └── optimizer.py               Ridge regression weight optimizer
│
├── scripts/                       # User Entry Points
│   ├── eod_stock_picker.py        Main entry (strategy/status/briefing)
│   └── run_backtest.py            Backtest entry
│
├── config.yml                     Central configuration
├── SKILL.md                       AI assistant skill definition
├── README.md                      Project documentation (Chinese)
├── README.en.md                   Project documentation (English)
├── requirements.txt               Python dependencies
│
└── data/                          Runtime data (auto-created)
    ├── kline_cache.db             K-line cache (SQLite WAL)
    ├── predictions.db             Recommendations + outcomes (SQLite)
    ├── codes_cache.json           A-share code list cache
    └── model_weights/             Optimizer weight files
```

### Data Flow

```
Data Layer                Factor Layer            Strategy Layer          Output Layer
──────────                ────────────            ──────────────          ────────────
Tencent API ──→ Quotes    FactorLibrary           ShortTermStrategy       market_briefing
mootdx  ──────→ K-line    ├─ calc_main_fund       ├─ prefilter()          generate_daily_report
10jqka  ──────→ Hot       ├─ calc_macd            ├─ enrich_data()        generate_backtest_report
EastMoney ───→ Blocks     ├─ calc_rps             ├─ rank_stocks()
hexin   ──────→ North     ├─ calc_rsi             └─ allocate()
akshare ─────→ BigDeal    ├─ calc_bollinger
                          └─ calc_fundamental
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
                            └─ outcomes.db
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
| Capital Flow | **27%** | Large-deal summary | Net inflow > 3M = high score |
| Momentum/RPS | **15%** | Market-wide percentile | Percentile → 0-100 |
| Technical | **15%** | 6-dim scoring | Trend 30 + Bias 20 + Volume 15 + Support 10 + MACD 15 + RSI 10 |
| Volume-Price | **10%** | Volume ratio | 0.8~2.0 = 80pts, >5 or <0.5 = low |
| Risk Control | **10%** | Risk filter | Pass = 100, each violation penalizes |
| North-bound | **10%** | hexin.cn summary | Directional score |
| Hot Theme | **8%** | Tonghuashun tags | In hot list + has theme tags = bonus |
| Dragon-Tiger | **5%** | East Money datacenter | Listed + institution net buy > 0 = bonus |

---

## 🔧 Data Sources

| Data | Source | Priority | Protocol | Anti-Ban | Status |
|------|--------|:-------:|----------|----------|:------:|
| K-Line + Financials | mootdx (Tongdaxin) | 🥇 **Primary** | TCP 7709 | Never banned | ✅ |
| Real-time Quotes | Tencent Finance | 🥇 **Primary** | HTTP | Never banned | ✅ |
| Hot Stocks | 10jqka (Tonghuashun) | 🥈 | HTTP | Zero auth | ✅ |
| North-bound Capital Summary | hexin.cn | 🥈 | HTTP | Zero auth | ✅ |
| Large-Deal Flow (685 stocks) | akshare big_deal | 🥈 | HTTP | Independent fuse, not shared | ✅ |
| Sector Membership | East Money slist | 🥉 | HTTP | em_get rate-limited | ✅ |
| Dragon-Tiger List | East Money datacenter | 🥉 | HTTP | em_get rate-limited | ✅ |
| Per-stock Capital Flow | akshare | — | HTTP | ❌ Blocked overseas, independent fuse | ⛔ |
| Per-stock North-bound | akshare | — | HTTP | ❌ Blocked overseas, independent fuse | ⛔ |

### Independent Source-Level Fuse

The system uses `_source_available` dict for source-level independent fusing — each endpoint is isolated:

```python
_source_available = {
    'big_deal': True,      # stock_fund_flow_big_deal — works
    'capital_flow': True,  # stock_individual_fund_flow — blocked
    'north_flow': True,    # stock_hsgt_individual_em — blocked
    'push2': True,         # push2 direct — blocked
}
```

`big_deal` and `north_flow` are completely independent. 200 north-bound timeouts only affect `north_flow`, never the big deal cache. A previous bug used a shared single variable (`_akshare_available`), which caused the big deal cache to be fused off when north-bound failed — wasting the 25s load.

### Large-Deal Cache Coverage

`stock_fund_flow_big_deal()` only covers stocks **with large deals today** (~685 stocks), not all 5205. Stocks without large deals get `capital_flow` factor neutralized to 50, with weight redistributed to other factors. This is expected behavior.

### Failure Protection

### Failure Protection

When a data source is unavailable, the corresponding factor is automatically neutralized to 50 points. Its weight is redistributed to the remaining active factors. The overall score is not compressed.

---

## ⚙️ Backtest Engine

### Features

| Feature | Implementation |
|---------|---------------|
| Data Source | mootdx real K-line, **zero `np.random`** |
| Execution | Next-day open price |
| Slippage | 0.1% (configurable) |
| Commission | 0.03% (configurable) |
| Trading Rules | T+1 take-profit +2%, stop-loss -2%, time-stop T+3 |
| Benchmark | CSI 300 Index |

### Metrics

```
✓ Total Trades        ✓ Win Rate           ✓ Avg Return (T+1/T+5)
✓ Max Gain            ✓ Max Loss           ✓ Max Drawdown
✓ Sharpe Ratio        ✓ Total Return       ✓ Excess Return
✓ Equity Curve       ✓ Monthly Returns    ✓ Factor Attribution
✓ Trade Details      ✓ Optimization Tips
```

### Known Limitations

- Capital flow and north-bound data are not available in backtest (current-day large-deal data cannot be retrospected)
- mootdx provides ~600 trading days (~2.5 years)
- Uses next-day open price for execution, cannot simulate intraday fills

---

## 🔄 Weight Self-Learning

```
Live Run → Record Recommendation → Auto-fill T+1 Returns
  → Accumulate 30+ records → Ridge Regression → New Weights → v1.json
                                                                ↓
                                                         Auto-loaded on next startup
```

The weight optimizer (`feedback/optimizer.py`):
- **Algorithm**: `sklearn.linear_model.Ridge(alpha=1.0)`
- **Input**: Factor raw scores → Actual T+1 returns
- **Output**: Normalized new weights (negative coefficients zeroed out, positives normalized to sum 1)
- **Trigger**: Win rate < 50% or every 50 records
- **Rollback**: Old versions preserved in `model_weights/` (timestamped)

---

## 📁 File Reference

| File | Role | Description |
|------|------|-------------|
| `core/data_engine.py` | Data Fusion Engine | 13-source unified access, caching, status tracking, failover |
| `core/factor_library.py` | Factor Library | 30+ factor 0-100 score mappings |
| `core/scoring_model.py` | Scoring Model | Weighted sum → rating → reasoning, weight redistribution |
| `core/technical_scorer.py` | Technical Scorer | 6-dim 100-pt systematic technical analysis |
| `core/risk_filter.py` | Risk Control | 6 checks, penalty scoring, skip decision |
| `core/backtest_engine.py` | Backtest Engine | Day-by-day simulation, real K-lines, no random |
| `core/portfolio_optimizer.py` | Portfolio Optimization | Score-weighted / equal-weight, allocation constraints |
| `strategies/short_term.py` | Short-Term Strategy | 8-factor market scan, pre-filter, deep eval, ranking |
| `strategies/long_term.py` | Long-Term Strategy | 6-factor fundamental + north-bound + momentum |
| `strategies/sequoia_patterns.py` | Pattern Recognition | 9 independent K-line pattern functions |
| `reports/market_briefing.py` | Market Briefing | 5-section panorama report generator |
| `reports/backtest_report.py` | Report Renderer | Backtest + daily recommendation Markdown rendering |
| `reports/daily_report.py` | Report Storage | Markdown file read/write |
| `feedback/tracker.py` | Prediction Tracker | SQLite persistent recommendations + outcomes |
| `feedback/optimizer.py` | Weight Optimizer | Ridge regression auto-tuning |
| `scripts/eod_stock_picker.py` | Main Entry | Strategy run + briefing + backfill + optimize |
| `scripts/run_backtest.py` | Backtest Entry | Full backtest launch |
| `config.yml` | Configuration Hub | Weights / risk / backtest / model params |
| `SKILL.md` | AI Skill | Claude Code / OpenClaw / Hermes interface |

---

## 🤖 AI Assistant Integration

This project supports conversational interaction via `SKILL.md` with the following AI coding assistants:

| Platform | Setup |
|----------|-------|
| **Claude Code** | Auto-detected from `SKILL.md` in project root |
| **OpenClaw** | Place `SKILL.md` into `~/.claude/skills/stock-picker/` |
| **Hermes** | Point skill config to `SKILL.md` path |

Example conversation prompts:

> "What's the market looking like today?" → Runs `get_all_quotes() + market briefing`
> "Any stock recommendations for today?" → Runs full short-term strategy
> "Show me fundamentals for 600519" → ROE/EPS/valuation snapshot
> "What are today's hot themes?" → Tonghuashun hot stock attribution
> "Run a backtest for the last 6 months" → Executes backtest engine

---

## 🙏 Acknowledgments

This project references and draws inspiration from the following open-source projects and communities:

| Project | Contribution | Repository |
|---------|-------------|------------|
| **a-stock-data** (Simon Lin) | Data source architecture patterns, `em_get()` rate limiter design, Tonghuashun hot stocks / sector attribution / dragon-tiger list API reference | [simonlin1212/a-stock-data](https://github.com/simonlin1212/a-stock-data) |
| **Sequoia-X** | Pattern recognition strategies (golden cross, turtle breakout, high-tight flag), RPS ranking logic | Reference project |
| **daily-stock-analysis** | StockTrendAnalyzer technical scoring system design | Reference project |
| **mootdx** | Tongdaxin TCP protocol Python wrapper, stable K-line + financial data | [mootdx](https://github.com/bopo/mootdx) |
| **akshare** | A-share data interface standard, large-deal data and code list | [akshare](https://github.com/jindaxiang/akshare) |

---

## 📄 License

[MIT](LICENSE)

---

<div align="center">

**If you find this project helpful, please Star ⭐**

Maintained by [cbzhang86](https://github.com/cbzhang86) · Developed with Claude Code

</div>
