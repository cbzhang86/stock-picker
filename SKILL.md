---
name: stock-picker
description: A股智能选股系统 — 全市场多因子评分/短线尾盘/长线持股/每日简报/同花顺热点/龙虎榜追踪/板块归属/基本面财务快照/市场环境评估
origin: custom
version: 4.1
---

# A股智能选股系统 V4.1

13 个直连数据源 · 30+ 量化因子 · 短线/长线双策略 · 回测引擎（历史快照） · 自学习权重 · 市场环境评估

兼容 [Claude Code](https://github.com/anthropics/claude-code) · [OpenClaw](https://github.com/anthropics/openclaw) · [Codex](https://github.com/openai/codex) · [Hermes](https://github.com/nez/pericles)

---

## When to Activate

| 用户说什么 | 触发内容 |
|-----------|---------|
| "看一下今天的行情/大盘" | `DataEngine.get_all_quotes()` — 5205只实时快照 |
| "今天有什么推荐/短线/尾盘" | `ShortTermStrategy.run()` — 8因子评分+仓位分配 |
| "长线/基本面/中线推荐" | `LongTermStrategy.run()` — ROE+PE基本面评分 |
| "简报/市场/今天整体怎么样" | `generate_market_briefing()` — 5维度全景简报 |
| "热点/题材/今天哪些板块走强" | `get_ths_hot_stocks()` — 同花顺强势股+人工标注题材 |
| "北向/外资流入流出" | `get_north_flow_summary()` — 沪/深实时外资 |
| "龙虎榜/席位" | `get_dragon_tiger(code)` — 上榜记录+买卖席位TOP5+机构动向 |
| "板块/概念归属" | `get_stock_blocks(code)` — 行业/概念/地域 |
| "K线/技术/评分" | `TechnicalScorer.score()` — 6维100分制技术评分 |
| "财务/ROE/EPS" | `get_financial_snapshot(code)` — mootdx 37字段 |
| "基本面/估值" | `FactorLibrary.calc_fundamental_score()` / `calc_valuation_score()` |
| "回测/验证/历史表现" | `BacktestEngine.run()` — 真实K线逐日模拟 |
| "回测历史/对比回测" | `BacktestStore.list_runs()` / `compare_runs()` — 版本对比 |
| "状态/模型/权重" | `scripts/eod_stock_picker.py --status` |

---

## Prerequisites

```bash
pip install mootdx requests pandas numpy scikit-learn cachetools akshare pyyaml
```

| 依赖 | 用途 |
|------|------|
| mootdx >= 0.10 | 通达信TCP行情（K线+财务快照，永不封IP） |
| scikit-learn | Ridge回归权重自学习 |
| akshare | 仅用于A股代码列表和大单数据 |
| 其余 | HTTP请求/数据处理/缓存/配置 |

---

## 快速使用

```bash
# 短线策略（含完整数据源）
python scripts/eod_stock_picker.py --mode short

# 查看状态和近期表现
python scripts/eod_stock_picker.py --status

# 长线策略
python scripts/eod_stock_picker.py --mode long

# 回测
python scripts/run_backtest.py --mode short --start 2025-06-01 --end 2026-06-11
```

### 一键调用（给AI助手用）

```python
import sys; sys.path.insert(0, '/path/to/stock-picker')
from core.data_engine import DataEngine
from strategies.short_term import ShortTermStrategy
from reports.market_briefing import generate_market_briefing
from core.technical_scorer import TechnicalScorer

de = DataEngine()

# 常用数据
quotes = de.get_all_quotes()                  # 全市场行情
hot = de.get_ths_hot_stocks()                 # 今日热点
north = de.get_north_flow_summary()           # 北向资金
blocks = de.get_stock_blocks('600519')        # 板块归属
dt = de.get_dragon_tiger('002475')             # 龙虎榜
fin = de.get_financial_snapshot('600519')     # 财务快照
kline = de.get_kline('600519')                # K线
tech = TechnicalScorer().score(kline)         # 技术评分

# 策略
recs = ShortTermStrategy({'buy': {'max_candidates': 3}}).run()
briefing = generate_market_briefing(recs)
```

---

## 系统架构

```
stock-picker/                13个数据源 · 30+因子 · 短线/长线双策略
├── core/                    核心引擎
│   ├── data_engine.py       多源数据融合 (mootdx/腾讯/同花顺/东财)
│   ├── factor_library.py    30+量化因子计算
│   ├── scoring_model.py     加权评分 + 评级 + 权重自学习
│   ├── technical_scorer.py  6维系统化技术评分 (100分制)
│   ├── risk_filter.py       风险过滤器
│   ├── backtest_engine.py   回测引擎 (历史快照+真实K线)
│   ├── backtest_store.py    回测结果持久化+版本对比
│   └── portfolio_optimizer.py  仓位分配 (评分加权)
├── strategies/
│   ├── short_term.py        短线尾盘策略
│   ├── long_term.py         长线持股策略
│   ├── sequoia_patterns.py  9种K线形态识别
│   └── base.py
├── reports/
│   ├── market_briefing.py   每日市场简报
│   ├── backtest_report.py   回测报告 + 每日报告
│   └── daily_report.py      报告存储
├── feedback/
│   ├── tracker.py           预测追踪 (SQLite)
│   └── optimizer.py         权重优化 (Ridge回归)
├── scripts/
│   ├── eod_stock_picker.py  唯一用户入口
│   └── run_backtest.py      回测入口
├── config.yml               核心配置
├── SKILL.md                 AI助手技能文件
└── data/                    自动创建 (SQLite缓存)
```

---

## 数据层

### 全市场实时行情（5205只）

```python
de = DataEngine()
quotes = de.get_all_quotes()
up = (quotes['pct_chg'] > 0).sum()
top_volume = quotes.nlargest(10, 'amount')[['code','name','price','pct_chg','amount']]
```

数据源：腾讯财经（HTTP，不封IP），~46秒全市场。

### 个股日K线

```python
kline = de.get_kline('600519')  # 自动缓存到SQLite
```

优先级：mootdx TCP ~0.1s → Sina HTTP ~0.2s → baostock ~8s

### 同花顺强势股 + 题材归因

```python
hot_df = de.get_ths_hot_stocks()         # ~0.22秒, 零鉴权
themes = de.extract_hot_themes(hot_df)   # 词频统计
# 题材列格式: "算力租赁+Token工厂+AI政务"
```

### 其他数据

| 接口 | 函数 | 来源 | 耗时 |
|------|------|------|------|
| 北向资金 | `get_north_flow_summary()` | hexin.cn | 实时 |
| 板块归属 | `get_stock_blocks(code)` | 东财slist | ~2.3s（含限流） |
| 龙虎榜 | `get_dragon_tiger(code)` | 东财datacenter | ~6s（含限流） |
| 财务快照 | `get_financial_snapshot(code)` | mootdx | ~0.1s |
| 大单资金流 | `get_main_fund_accumulated(code)` | 大单汇总 | 毫秒级 |
| 数据源状态 | `get_data_source_summary()` | 自追踪 | 立即 |

---

## 策略层

### 短线尾盘（8因子，评分加权）

```
全市场5205只 → 初筛(非ST/流动性/涨跌停)
  → 5维预评分(流动性30+活跃度20+动量15+风险20+PE15)
    → Top 200 详评(资金流+北向+K线+技术评分+RPS)
      → 评分排序(防凑数) → 仓位分配 → 板块/龙虎榜补充
```

| 因子 | 权重 | 数据源 |
|------|:---:|--------|
| 主力资金流 | 27% | 大单交易汇总 |
| 动量/RPS | 15% | 全市场横截面百分位 |
| 技术形态 | 15% | 6维系统化评分 |
| 量价配合 | 10% | 量比 |
| 风险过滤 | 10% | ST/成交额/涨跌停 |
| 北向资金 | 10% | 当日汇总 |
| 同花顺热点 | 8% | 题材归因标签 |
| 龙虎榜 | 5% | 上榜+机构净买入 |

```python
from strategies.short_term import ShortTermStrategy

st = ShortTermStrategy({
    'weights': {'capital_flow': 0.27, 'momentum': 0.15, 'technical': 0.15,
                'volume_price': 0.10, 'risk': 0.10, 'north_flow': 0.10,
                'hot_theme': 0.08, 'dragon_tiger': 0.05},
    'buy': {'max_candidates': 3, 'min_score': 60}
})
recs = st.run()

for r in recs:
    print(f"{r['code']} {r['name']}: {r['score']}/100 | 仓位{r.get('allocation_pct',0)}%")
    print(f"  目标{r['target_price']} / 止损{r['stop_price']}")
    print(f"  理由: {r['reasoning'][:120]}")
```

### 长线持股（ROE+PE基本面评分）

```python
from strategies.long_term import LongTermStrategy

st = LongTermStrategy({'buy': {'max_candidates': 5, 'min_score': 50}})
recs = st.run()

for r in recs:
    print(f"{r['code']} {r['name']}: {r['score']}/100 | 仓位{r.get('allocation_pct',0)}%")
    if r.get('roe'):
        print(f"  ROE={r['roe']:.1f}% EPS={r['eps']:.2f}")
```

**基本面评分（FactorLibrary）：**
- `calc_fundamental_score(pe, pb, roe, eps, mcap)` — ROE优先，PE+PB+市值辅助
- `calc_valuation_score(pe, pb, roe)` — PE分位为主，PB+ROE验证
- `calc_institutional_score(dragon_tiger, main_fund)` — 无数据返回中性50

---

## 因子库

### 技术评分（6维100分制）

```python
from core.technical_scorer import TechnicalScorer

result = TechnicalScorer().score(kline)
# result.total: 0-100
# 趋势30 + 乖离20 + 量能15 + 支撑10 + MACD15 + RSI10
# result.signal: STRONG_BUY / BUY / HOLD / WAIT / SELL
```

### 形态识别（9种）

```python
from strategies.sequoia_patterns import check_turtle_breakout, check_high_tight_flag, ...

boost, reason = check_turtle_breakout(close, high_20_max, amount, today_open)
# → (10.0, "海龟突破：突破20日高点，成交额过亿")
```

---

## 报告

### 每日市场简报

```python
from reports.market_briefing import generate_market_briefing
briefing = generate_market_briefing(recommendations, mode='short')
# 输出: 市场概览 → 题材热度TOP10 → 短线评分排名 → 长线关注 → 数据源状态
```

### 回测报告

```python
from core.backtest_engine import BacktestEngine
from reports.backtest_report import generate_backtest_report

result = BacktestEngine({
    'initial_capital': 1_000_000,
    'commission_rate': 0.0003,
    'slippage': 0.001,
}).run(mode='short', start_date='2025-06-01', end_date='2026-06-11')

print(generate_backtest_report(result))
# 胜率 / 平均收益 / 夏普 / 回撤 / 权益曲线块状图 / 月度表现 / 因子归因 / 交易明细
```

**回测说明（已知限）：** 资金流/北向不可回溯（当日大单数据）；mootdx约600个交易日；以次日开盘价成交。

---

## 数据源

| 数据 | 来源 | 协议 | 封IP风险 | 速度 |
|------|------|------|---------|------|
| K线+财务 | mootdx | TCP 7709 | 永不封 | ~0.1s/只 |
| 实时行情 | 腾讯财经 | HTTP | 不封 | ~46s全市场 |
| 同花顺热点 | 10jqka | HTTP | 极低 | ~0.22s |
| 北向资金 | hexin.cn | HTTP | 极低 | 实时 |
| 板块归属 | 东财push2 | HTTP | 限流 | ~2.3s/只 |
| 龙虎榜 | 东财datacenter | HTTP | 限流 | ~6s/只 |
| 资金流 | akshare(大单) | HTTP | 低 | ~25s全量 |

所有东财接口已走 `em_get()` 串行限流 + 会话复用。

---

## FAQ

**数据源被封？** 因子自动降权至中性50分，权重分配给其他活跃因子，评分不压缩。

**回测真实？** 全 mootdx 真实K线，零 `np.random`。

**仓位分配？** 评分加权，单只上限40%下限10%。

**权重自学习？** Ridge回归，积累30条推荐后自动优化，结果存 `v1.json`。

**代码列表慢？** 每日首次从akshare拉取后缓存到 `codes_cache.json`，后续秒级加载。
