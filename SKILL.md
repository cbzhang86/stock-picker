---
name: stock-picker
description: 全栈多因子量化选股系统 — 13直连数据源/30+因子/短线尾盘+长线持股/历史快照回测/自学习权重
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
| "今天有什么推荐/短线/尾盘" | `ShortTermStrategy.run()` — 7因子评分+仓位分配 |
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
# 短线策略（含完整数据源简报）
python scripts/eod_stock_picker.py --mode short

# 查看状态和近期表现
python scripts/eod_stock_picker.py --status

# 长线策略
python scripts/eod_stock_picker.py --mode long

# 回测
python scripts/run_backtest.py --mode short --start 2026-01-01 --end 2026-06-11

# 回测版本对比
python scripts/run_backtest.py --list
python scripts/run_backtest.py --compare 1 2
```

```python
# 一键调用（给AI助手用）
import sys; sys.path.insert(0, '/path/to/stock-picker')
from core.data_engine import DataEngine
from strategies.short_term import ShortTermStrategy
from reports.market_briefing import generate_market_briefing
from core.technical_scorer import TechnicalScorer

de = DataEngine()
quotes = de.get_all_quotes()
hot = de.get_ths_hot_stocks()
north = de.get_north_flow_summary()
blocks = de.get_stock_blocks('600519')
dt = de.get_dragon_tiger('002475')
fin = de.get_financial_snapshot('600519')
kline = de.get_kline('600519')
tech = TechnicalScorer().score(kline)
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
│   ├── short_term.py        短线尾盘策略 (7因子+市场评估)
│   ├── long_term.py         长线持股策略 (ROE+PE基本面)
│   ├── sequoia_patterns.py  9种K线形态识别
│   └── base.py
├── reports/
│   ├── market_briefing.py   每日市场简报
│   ├── backtest_report.py   回测报告 + 每日报告
│   └── daily_report.py      报告存储
├── feedback/
│   ├── tracker.py           预测追踪 (SQLite)
│   └── optimizer.py         权重优化 (Ridge+常数列跳过)
├── scripts/
│   ├── eod_stock_picker.py  用户入口
│   └── run_backtest.py      回测入口 (含--list/--compare)
├── config.yml               核心配置
├── SKILL.md                 AI助手技能文件
└── data/                    运行时数据 (自动创建)
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

**耗时**: ~7s（腾讯财经 HTTP，不封IP）

### 个股日K线

```python
kline = de.get_kline('600519')  # 自动缓存到 SQLite
```

**数据源**: mootdx TCP ~0.007s/只 → Sina HTTP ~0.2s/只 → baostock ~8s/只（降级）
首次拉取约 0.1s，缓存命中后 0.007s。

### 同花顺强势股 + 题材归因

```python
hot_df = de.get_ths_hot_stocks()         # ~0.3秒, 零鉴权
themes = de.extract_hot_themes(hot_df)   # 词频统计
# 题材列格式: "算力租赁+Token工厂+AI政务"
```

### 主力资金流（同花顺全市场5189只）

```python
val = de.get_main_fund_accumulated('600519')
# 首次调用拉取全量5189只（~19s），后续毫秒级查询
```

### 其他数据

| 接口 | 函数 | 来源 | 耗时 |
|------|------|------|------|
| 北向资金汇总 | `get_north_flow_summary()` | hexin.cn | ~2.3s |
| 板块归属 | `get_stock_blocks(code)` | 东财slist（限流） | ~2.3s/只 |
| 龙虎榜 | `get_dragon_tiger(code)` | 东财datacenter（限流） | ~6s/只 |
| 财务快照 | `get_financial_snapshot(code)` | mootdx | ~0.1s/只 |
| 数据源状态 | `get_data_source_summary()` | 自追踪 | 立即 |
| 尾盘成交信号 | `get_tail_end_stats(code)` | 大单缓存 | 立即（0额外API） |

---

## 策略层

### 短线尾盘（7因子，评分加权）

```
5205只 → 市场评估 → 初筛 → 预评分 → Top200详评 → 评分 → 仓位分配 → 板块/龙虎榜补充
```

| 因子 | 权重 | 数据源 | 评分方式 |
|------|:----:|--------|---------|
| 主力资金流 | 25% | 同花顺全市场 | 横截面百分位排名（避免全员满分） |
| 动量/RPS | 25% | 全市场涨幅排位 | 20日百分位 |
| 技术形态 | 15% | mootdx K线 | 6维100分制: 趋势30+乖离20+量能15+支撑10+MACD15+RSI10 |
| 量价配合 | 10% | 量比+尾盘结构 | 连续分段线性评分 |
| 题材热度 | 10% | 同花顺热点 | 强势股+题材归因标签 |
| 北向资金 | 10% | hexin.cn汇总 | 全市场方向评分 |
| 龙虎榜 | 5% | 东财datacenter | 上榜+机构净买入 |

**市场环境评估**: 涨跌比(30%)+涨停跌停比(25%)+中位数涨幅(20%)+强势股数(15%)+北向(10%) → 极差市(<40)/弱市(40-55)/中性/强市。极差市跳过推荐输出简报，弱市最多推荐2只、min_score提至65。

```python
from strategies.short_term import ShortTermStrategy
st = ShortTermStrategy({'buy': {'max_candidates': 3, 'min_score': 60}})
recs = st.run()
for r in recs:
    print(f"{r['code']} {r['name']}: {r['score']}/100 | 仓位{r.get('allocation_pct',0)}%")
    print(f"  因子: {r['breakdown']}")
    print(f"  板块: {r.get('blocks',{}).get('concept_tags',[])}")
```

### 长线持股（5因子）

```python
from strategies.long_term import LongTermStrategy
st = LongTermStrategy({'buy': {'max_candidates': 5, 'min_score': 50}})
recs = st.run()
for r in recs:
    print(f"{r['code']} {r['name']}: {r['score']}/100 | 仓位{r.get('allocation_pct',0)}%")
    if r.get('roe'): print(f"  ROE={r['roe']:.1f}% EPS={r['eps']:.2f}")
```

**因子**: 基本面(ROE+PE+市值)30% + 北向20% + 估值(PE分位+PB)20% + 机构关注度15% + 动量15%

---

## 因子库

### 基本面因子

```python
from core.factor_library import FactorLibrary
fl = FactorLibrary()

# 基本面评分 (0-100)
fs = fl.calc_fundamental_score(pe=12, pb=1.5, roe=18, eps=2.5, mcap=1e11)
# ROE≥20 +20, 15-20 +15; PE<15+PB<3 +10; 千亿+5

# 估值评分 (0-100)
vs = fl.calc_valuation_score(pe=12, pb=1.5, roe=18)
# PE<10 +30, <15 +20; PB合理+PE低 +10; 高ROE+低PE +10

# 机构关注度 (0-100) — 无数据返回50
inst = fl.calc_institutional_score(
    {'institution': {'net_amt': 8000}, 'records': [{}]}, main_fund=1e7)
```

### 技术评分

```python
from core.technical_scorer import TechnicalScorer
result = TechnicalScorer().score(kline)
# result.total: 0-100
# result.signal: STRONG_BUY / BUY / HOLD / WAIT / SELL
```

---

## 组合优化

```python
from core.portfolio_optimizer import PortfolioOptimizer
recs = [{'code': 'A', 'name': 'A', 'score': 85},
        {'code': 'B', 'name': 'B', 'score': 72}]
result = PortfolioOptimizer.allocate(recs)
# 每只增加 allocation_pct 字段，单只上限40%/保底10%，总和100%
```

---

## 回测

### 核心做法

1. 用 mootdx 真实 K 线构建每交易日的**历史行情快照**（非今日实时数据）
2. 逐日回放：取历史快照 → 跑策略 → 记录推荐
3. 每条推荐查询 T+1/T+5 真实 K 线收益

**与前视偏差的关键区别**：快照数据来自历史当日收盘价，不是今天的数据。

### 限制

- 资金流/北向不可回溯（当日大单不可用），回测仅用量价+技术因子
- mootdx 提供约 600 交易日（约 2.5 年）
- 以次日开盘价成交，无法模拟盘中即时

```python
from core.backtest_engine import BacktestEngine
from reports.backtest_report import generate_backtest_report

result = BacktestEngine({
    'initial_capital': 1_000_000,
    'commission_rate': 0.0003,
    'slippage': 0.001,
}).run(mode='short', start_date='2026-01-01', end_date='2026-06-11')

print(generate_backtest_report(result))
# 胜率/平均收益/夏普/回撤/权益曲线/月度表现/因子IC归因/交易明细
```

### 版本对比

```python
from core.backtest_store import BacktestStore
store = BacktestStore()
runs = store.list_runs(10)    # 查看历史记录
diff = store.compare_runs(1, 2)  # 对比两次结果
```

---

## 数据源

| 数据 | 来源 | 协议 | 封IP风险 | 熔断 |
|------|------|:----:|:--------:|:----:|
| K线+财务 | mootdx | TCP 7709 | 永不封 | — |
| 实时行情 | 腾讯财经 | HTTP | 不封 | — |
| 同花顺热点 | 10jqka | HTTP | 极低 | — |
| 北向资金 | hexin.cn | HTTP | 极低 | — |
| 主力资金流 | 同花顺 | HTTP | 低 | 独立 |
| 板块归属 | 东财slist | HTTP(限流) | 低 | — |
| 龙虎榜 | 东财datacenter | HTTP(限流) | 低 | — |
| 个股北向 | akshare | HTTP | ❌不通 | 独立 |

**独立熔断机制**：每个数据源互不影响。个股北向不通不影响大单缓存，东财不通不影响同花顺。被熔断的因子自动中性化为50分，权重重分配给其他因子。

---

## FAQ

**全流程要多久？**  
当前约7分钟：行情~7s + 同花顺资金流~19s + K线并行~25s + 其他。缓存预热后二次运行更快。

**数据源被封怎么办？**  
系统自动降权。数据源级别独立熔断，互不影响。因子自动中性化为50分，权重分配给其他因子，评分不压缩。

**回测可信吗？**  
回测已消除前视偏差（使用历史当日快照而非今日数据），T+1收益用真实K线计算。但资金流/北向不可回溯，回测中这些因子恒定为50分。回测与实盘可能有偏差。

**权重怎么调？**  
先用默认权重跑实盘积累记录（当前已有93条），让Ridge回归自动优化。手动调权重后通过`--compare`对比效果。

**历史快照包含哪些股票？**  
kline_cache.db缓存了4483只股票、2025-06-03至2026-06-25的K线数据。5205只中约800只新股/退市/无数据，回测中自动跳过。

---

## 数据状态（截至2026-06-25）

| 指标 | 数据 |
|------|------|
| 实盘推荐记录 | 93条（短线88条，长线5条） |
| K线缓存 | 4483只，2025-06至2026-06 |
| 同花顺资金流 | 5189只全量缓存 |
| 大单缓存 | 685只每日更新 |
| 熔断状态 | big_deal✅ ths_fund_flow✅ north_flow⛔ |
| 权重优化触发 | 需30条（当前88短线）→ **已满足触发条件** |
