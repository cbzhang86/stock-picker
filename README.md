<div align="center">
  
# 🏛️ A股智能选股系统

**Stock-Picker V4 — 全栈多因子量化选股框架**

[![Python](https://img.shields.io/badge/python-3.10%2B-blue)](https://www.python.org/)
[![License](https://img.shields.io/badge/license-MIT-green)](LICENSE)
[![Compatible with](https://img.shields.io/badge/Claude%20Code-OpenClaw-Hermes-8A2BE2)](SKILL.md)

13 个直连数据源 · 30+ 因子 · 短线尾盘 + 长线持股双策略 · 回测引擎 · 自学习权重优化

**简体中文** · [English](README.en.md) *(可选)*

</div>

---

## 📋 目录

- [项目简介](#-项目简介)
- [功能特性](#-功能特性)
- [快速开始](#-快速开始)
- [使用示例](#-使用示例)
- [系统架构](#-系统架构)
- [短线策略评分详解](#-短线策略评分详解)
- [数据源](#-数据源)
- [回测引擎](#-回测引擎)
- [权重自学习](#-权重自学习)
- [项目文件说明](#-项目文件说明)
- [AI 助手集成](#-ai-助手集成)
- [致谢](#-致谢)
- [许可证](#-许可证)

---

## 📖 项目简介

Stock-Picker 是一个面向 A 股市场的**全栈量化选股系统**，覆盖从数据采集、因子计算、策略评分到回测验证的完整闭环。

### 背景

A 股量化投资面临的核心问题是：数据源分散（行情、资金流、龙虎榜、北向资金分散在不同平台）、接口易被封（东方财富 WAF 风控）、策略验证难（缺乏真实回测数据）。本项目通过**数据源优先级回退链 + 统一限流层 + mootdx TCP 直连**解决了这些问题。

### 核心价值

| 维度 | 说明 |
|------|------|
| **数据** | 13 个直连数据源，mootdx TCP 永不封 IP 优先，东财接口统一限流 |
| **策略** | 短线尾盘（7 因子）+ 长线持股（5 因子）+ 市场环境评估自动跳过 |
| **风控** | 6 道风险过滤（ST/涨跌停/流动性/换手率），防凑数评分截断 |
| **回测** | 全真实 K 线数据（零 `np.random`），含历史行情快照回放，计入滑点佣金，输出权益曲线/夏普/IC |
| **自学习** | Ridge 回归自动优化因子权重 + 因子 IC 归因，积累 30 条推荐后触发 |
| **可集成** | 兼容 Claude Code / OpenClaw / Hermes，通过 SKILL.md 对话式调用 |

---

## ✨ 功能特性

<details open>
<summary><b>📊 数据能力（点击展开）</b></summary>

| 能力 | 数据源 | 耗时 | 封 IP 风险 |
|------|--------|:---:|:---------:|
| 全市场实时行情（5205 只） | 腾讯财经 | ~46s | 🟢 不封 |
| 个股日 K 线 | mootdx TCP | ~0.1s/只 | 🟢 永不封 |
| 基本面财务快照（37 字段） | mootdx TCP | ~0.1s/只 | 🟢 永不封 |
| 同花顺强势股 + 题材归因 | 10jqka | ~0.22s | 🟢 极低 |
| 北向资金实时汇总 | hexin.cn | 实时 | 🟢 极低 |
| 个股板块归属 | 东财 slist | ~2.3s/只 | 🟡 限流 |
| 龙虎榜 + 席位 + 机构动向 | 东财 datacenter | ~6s/只 | 🟡 限流 |
| 主力资金流（5189 只） | 同花顺 | ~19s 全量 | 🟢 低 |

</details>

<details open>
<summary><b>🧠 策略能力</b></summary>

- **短线尾盘**：7 因子加权评分 + 市场环境评估 → 初筛 → 预评分 → Top 200 详评 → 仓位分配
- **长线持股**：ROE + PE + PB 基本面评分，3-6 个月持有周期
- **市场环境感知**：涨跌比/涨停跌停比/中位数涨幅/强势股数量/北向资金 → 综合分，极差市自动跳过
- **形态识别**：9 种 K 线形态自动检测（金叉、海龟突破、高旗形等）
- **技术评分**：6 维度系统化评分（趋势 30 + 乖离 20 + 量能 15 + 支撑 10 + MACD 15 + RSI 10）
- **组合优化**：评分加权仓位分配，单只上限 40%、保底 10%
- **防凑数**：第 3 名与第 1 名分差 > 20 分自动裁掉

</details>

<details open>
<summary><b>🔄 自学习能力</b></summary>

- SQLite 持久化存储每次推荐及因子分解
- 自动回填 T+1/T+5/T+20 真实收益
- Ridge 回归将因子得分映射到实际收益率
- 积累 30 条推荐后自动触发优化
- 新权重持久化到 `v1.json`，下次启动自动加载

</details>

---

## 🚀 快速开始

### 环境要求

- Python 3.10+
- Windows / Linux / macOS
- 网络：国内国外均可（mootdx TCP 和腾讯 API 不受地域限制）

### 安装

```bash
# 克隆仓库
git clone https://github.com/cbzhang86/stock-picker.git
cd stock-picker

# 安装依赖
pip install -r requirements.txt
```

### 第一次运行

```bash
# 运行短线策略（全流程约 4 分钟）
python scripts/eod_stock_picker.py --mode short
```

**运行过程会看到：**

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

**最终输出每日简报：**

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

### 其他命令

```bash
# 查看模型状态和近期表现
python scripts/eod_stock_picker.py --status

# 长线策略
python scripts/eod_stock_picker.py --mode long

# 回测验证
python scripts/run_backtest.py --mode short --start 2025-06-01 --end 2026-06-11
```

---

## 🎯 使用示例

### 示例 1：查看全市场行情

```python
from core.data_engine import DataEngine

de = DataEngine()
quotes = de.get_all_quotes()

# 涨跌统计
up = (quotes['pct_chg'] > 0).sum()
down = (quotes['pct_chg'] <= 0).sum()
total_yi = quotes['amount'].sum()

print(f"上涨 {up} / 下跌 {down}，成交额 {total_yi:.0f} 亿")

# 成交额 TOP 10
top = quotes.nlargest(10, 'amount')
print(top[['code', 'name', 'price', 'pct_chg', 'amount']])
```

### 示例 2：个股基本面分析

```python
code = '600519'  # 贵州茅台

# 财务快照
fin = de.get_financial_snapshot(code)
print(f"ROE: {fin['roe']:.1f}%")
print(f"EPS: {fin['eps']:.2f}")
print(f"营收: {fin['income']/1e8:.2f}亿")

# 技术评分
from core.technical_scorer import TechnicalScorer
kline = de.get_kline(code)
tech = TechnicalScorer().score(kline)
print(f"技术评分: {tech.total}/100 ({tech.signal})")

# 板块归属
blocks = de.get_stock_blocks(code)
print(f"板块: {', '.join(blocks['concept_tags'][:5])}")

# 龙虎榜
dt = de.get_dragon_tiger(code)
if dt['records']:
    print(f"龙虎榜: 净买入 {dt['records'][0]['net_buy_wan']:.0f} 万")
```

### 示例 3：回测并查看报告

```bash
python scripts/run_backtest.py --mode short --start 2026-01-01 --end 2026-06-11
```

输出：

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

## 🏗 系统架构

```
stock-picker/
│
├── core/                          # 核心引擎层
│   ├── data_engine.py             多源数据融合（13个数据源统一接入）
│   ├── factor_library.py          30+量化因子计算（资金/动量/技术/量价/估值/风险）
│   ├── scoring_model.py           多因子加权评分 + 评级映射 + 权重加载
│   ├── technical_scorer.py        系统化技术评分（6维度100分制）
│   ├── risk_filter.py             6道风险检查（ST/流动性/涨跌停/换手率）
│   ├── backtest_engine.py         回测引擎（mootdx真实K线，零随机数）
│   └── portfolio_optimizer.py     仓位分配（评分加权/等权）
│
├── strategies/                    # 策略层
│   ├── short_term.py              短线尾盘策略（8因子 → 评分 → 仓位）
│   ├── long_term.py               长线持股策略（ROE+PE基本面评分）
│   ├── sequoia_patterns.py        9种K线形态识别函数
│   └── base.py                    策略抽象基类
│
├── reports/                       # 报告层
│   ├── market_briefing.py         每日市场简报生成器
│   ├── backtest_report.py         回测报告 + 每日推荐报告渲染
│   └── daily_report.py            Markdown报告文件存储
│
├── feedback/                      # 反馈循环
│   ├── tracker.py                 SQLite预测追踪（predictions + outcomes）
│   └── optimizer.py               Ridge回归权重优化器
│
├── scripts/                       # 用户入口
│   ├── eod_stock_picker.py        唯一主入口（策略/状态/简报）
│   └── run_backtest.py            全量回测入口
│
├── config.yml                     核心配置（权重/风控/回测参数）
├── SKILL.md                        AI 助手技能文件
├── README.md                       项目自述（本文档）
├── requirements.txt                Python 依赖清单
│
└── data/                          运行时数据（自动创建）
    ├── kline_cache.db              K 线缓存 (SQLite WAL)
    ├── predictions.db              推荐记录 + 收益结果 (SQLite)
    ├── codes_cache.json            A 股代码列表缓存
    └── model_weights/              优化器输出的权重文件
```

### 数据流

```
数据采集层              因子计算层              策略层               输出层
──────────              ─────────              ──────               ──────
腾讯API ──→ 行情        FactorLibrary          ShortTermStrategy    market_briefing
mootdx  ──→ K线/财务     ├─ calc_main_fund     ├─ prefilter()       generate_daily_report
同花顺  ──→ 热点        ├─ calc_macd          ├─ enrich_data()     generate_backtest_report
东财    ──→ 板块/龙虎    ├─ calc_rps           ├─ rank_stocks()
hexin   ──→ 北向        ├─ calc_rsi           └─ allocate()
akshare ──→ 大单/代码   ├─ calc_bollinger
                        └─ calc_fundamental
                               │
                               ▼
                          scoring_model
                          ├─ 权重求和
                          ├─ 评级映射
                          └─ 理由生成
                               │
                               ▼
                          feedback/tracker
                          ├─ predictions.db
                          └─ outcomes.db
                               │
                               ▼
                          feedback/optimizer
                          └─ Ridge回归 → v1.json
```

---

## 📈 短线策略评分详解

### 完整流程

```
⏱ 14:50 启动
   │
   ▼
📡 get_all_codes()         读取代码缓存（0.001s，首次从 akshare 拉取）
   │
   ▼
📡 get_all_quotes()        腾讯API 拉取 5205 只行情（~46s）
   │
   ▼
📡 get_ths_hot_stocks()    同花顺强势股 + 题材归因（~0.22s）
   │
   ▼
🔍 _prefilter()            初筛：非ST / 成交额>3000万 / 非涨跌停（→ ~4900只）
   │
   ▼
🔍 5维预评分               流动性30 + 活跃度20 + 动量15 + 风险20 + PE15（→ Top 200）
   │
   ▼
📡 get_main_fund()         大单资金流全量缓存（~25s，后续 199 次调用毫秒级）
   │
   ▼
📡 get_kline() × 200       mootdx TCP 3线程并行拉取 K 线 + 技术评分（~25s）
   │
   ▼
🔍 RPS 横截面排名          200只的20日涨幅百分位排名
   │
   ▼
🔍 scoring_model.score()   8 因子加权评分 + 防凑数检查
   │
   ▼
📊 portfolio_optimizer     评分加权仓位分配（单只上限 40% / 保底 10%）
   │
   ▼
📡 get_stock_blocks()      板块归属补充（仅 Top 3，走 em_get 限流）
📡 get_dragon_tiger()      龙虎榜补充（仅 Top 3）
   │
   ▼
✅ 输出简报 + 保存报告     15:00 前完成
```

### 因子权重表

| 因子 | 权重 | 数据源 | 评分逻辑 |
|------|:---:|--------|---------|
| 主力资金流 | **27%** | 大单交易汇总 | 净流入 > 300万 = 高分，净流出 = 低分 |
| 动量/RPS | **15%** | 全市场涨幅排位 | 百分位排名映射 0-100 |
| 技术形态 | **15%** | 6维技术评分 | 趋势30 + 乖离20 + 量能15 + 支撑10 + MACD15 + RSI10 |
| 量价配合 | **10%** | 量比 | 0.8~2.0 = 80分，> 5 或 < 0.5 = 低分 |
| 风险过滤 | **10%** | 风控检查 | 通过 = 100，每项违规累计扣分 |
| 北向资金 | **10%** | hexin.cn 汇总 | 当日净流向方向评分 |
| 同花顺热点 | **8%** | 题材归因标签 | 在强势股列表中 + 有题材标签 = 加分 |
| 龙虎榜 | **5%** | 东财 datacenter | 上榜 + 机构净买入为正 = 加分 |

---

## 🔧 数据源

| 数据 | 数据源 | 优先级 | 协议 | 防封措施 | 状态 |
|------|--------|:-----:|------|---------|:----:|
| K 线 + 财务 | mootdx（通达信） | 🥇 **首选** | TCP 7709 | 永不封 IP | ✅ |
| 实时行情 | 腾讯财经 | 🥇 **首选** | HTTP | 不封 IP | ✅ |
| 同花顺强势股 | 10jqka | 🥈 | HTTP | 零鉴权 73ms | ✅ |
| 北向资金实时汇总 | hexin.cn | 🥈 | HTTP | 零鉴权 | ✅ |
| 大单资金流（685 只） | akshare big_deal | 🥈 | HTTP | 独立熔断，不与其他源共享 | ✅ |
| 板块归属 | 东财 push2 | 🥉 | HTTP | em_get 限流 | ✅ |
| 龙虎榜 | 东财 datacenter | 🥉 | HTTP | em_get 限流 | ✅ |
| 个股北向 | akshare | — | HTTP | ❌ 境外不通，独立熔断，不影响其他源 | ⛔ |
| 个股资金流 | akshare | — | HTTP | ❌ 境外不通，独立熔断，不影响其他源 | ⛔ |

### 独立数据源级熔断

系统使用 `_source_available` 字典实现数据源级别的独立熔断，各 endpoint 互不影响：

```python
_source_available = {
    'big_deal': True,      # stock_fund_flow_big_deal — 通的
    'capital_flow': True,  # stock_individual_fund_flow — 不通
    'north_flow': True,    # stock_hsgt_individual_em — 不通
    'push2': True,         # push2 直连 — 不通
}
```

`big_deal` 和 `north_flow` 的熔断完全独立。北向 200 只超时只影响 `north_flow` 标志位，不影响大单缓存。之前用单变量共享熔断时，北向失败连带大单缓存也被熔断，25s 的大单加载白花了——已修复。

### 大单缓存覆盖范围

`stock_fund_flow_big_deal()` 只包含**当日有大单交易**的股票（约 685 只），不是全市场 5205 只。没有大单交易的股票 `capital_flow` 因子取中性 50 分，权重自动分配给其他因子。这是正常行为，无需处理。

### 失效保护

---

## ⚙️ 回测引擎

### 核心特性

| 特性 | 实现方式 |
|------|---------|
| 数据源 | mootdx 真实 K 线，**零 `np.random`** |
| 成交价 | 次日开盘价 |
| 滑点 | 0.1%（可配置） |
| 佣金 | 万三（可配置） |
| 交易周期 | T+1 止盈 +2%，止损 -2%，T+3 时间止损 |
| 基准 | 沪深 300 |

### 输出指标

```
✓ 总交易次数      ✓ 胜率            ✓ 平均收益(T+1/T+5)
✓ 最大单笔盈利     ✓ 最大单笔亏损     ✓ 最大回撤
✓ 夏普比率        ✓ 策略总收益       ✓ 超额收益
✓ 权益曲线        ✓ 月度收益表       ✓ 因子归因
✓ 交易明细        ✓ 优化建议
```

### 已知限制

- 资金流 / 北向数据在回测中不可用（当日大单不可回溯），回测仅用量价 + 技术因子
- mootdx 提供约 600 个交易日（约 2.5 年），无法回测 2019 年以前的策略
- 以次日开盘价成交，无法模拟盘中即时成交

---

## 🔄 权重自学习

```
实盘推荐 → 自动回填 T+1 → 积累 30 条 → Ridge 回归 → 新权重 → v1.json
                                                                ↓
                                                         下次启动自动加载
```

权重优化器（`feedback/optimizer.py`）：
- **算法**：`sklearn.linear_model.Ridge(alpha=1.0)`
- **输入**：各因子原始得分 → 实际 T+1 收益率
- **输出**：归一化新权重（负系数截断为 0，正系数归一化到总和 1）
- **触发**：胜率 < 50% 或每 50 条定期检查
- **回滚**：旧版本保留在 `model_weights/`（带时间戳），可手动对比

---

## 📁 项目文件说明

| 文件 | 角色 | 说明 |
|------|------|------|
| `core/data_engine.py` | 数据融合引擎 | 13 个数据源的统一接入、缓存、状态追踪、失效降级 |
| `core/factor_library.py` | 因子计算库 | 30+量化因子的 0-100 评分映射逻辑 |
| `core/scoring_model.py` | 评分模型 | 加权求和 → 评级映射 → 理由生成，支持权重重分配 |
| `core/technical_scorer.py` | 技术评分 | 6 维度 100 分制系统化技术分析 |
| `core/risk_filter.py` | 风控 | 6 道检查、惩罚系数、跳过判定 |
| `core/backtest_engine.py` | 回测引擎 | 历史K线快照逐日模拟、真实K线收益、因子IC归因 |
| `core/backtest_store.py` | 回测存储 | SQLite持久化回测结果，支持 --list/--compare 版本对比 |
| `core/portfolio_optimizer.py` | 组合优化 | 评分加权 / 等权，仓位约束管理 |
| `strategies/short_term.py` | 短线策略 | 8 因子全市场扫描、预评分、详评、排序 |
| `strategies/long_term.py` | 长线策略 | 6 因子基本面 + 北向 + 动量 |
| `strategies/sequoia_patterns.py` | 形态识别 | 9 种独立 K 线形态检测函数 |
| `reports/market_briefing.py` | 市场简报 | 5 板块全景报告生成 |
| `reports/backtest_report.py` | 报告渲染 | 回测报告 + 每日推荐报告 Markdown 渲染 |
| `reports/daily_report.py` | 报告存储 | Markdown 文件读写 |
| `feedback/tracker.py` | 预测追踪 | SQLite 持久化推荐 + 收益 |
| `feedback/optimizer.py` | 权重优化 | Ridge 回归自动调参 |
| `scripts/eod_stock_picker.py` | 唯一入口 | 策略运行 + 简报 + 回填 + 优化 |
| `scripts/run_backtest.py` | 回测入口 | 全量回测 + --list 查看历史 + --compare 版本对比 |
| `config.yml` | 配置中心 | 权重 / 风控 / 回测 / 模型参数 |
| `SKILL.md` | AI 技能 | Claude Code / OpenClaw / Hermes 接口定义 |

---

## 🤖 AI 助手集成

本项目支持通过 `SKILL.md` 与以下 AI 编程助手对话式交互：

| 平台 | 使用方法 |
|------|---------|
| **Claude Code** | 仓库根目录有 `SKILL.md`，自动识别 |
| **OpenClaw** | 将 `SKILL.md` 放入 `~/.claude/skills/stock-picker/` |
| **Hermes** | 通过技能配置指向 `SKILL.md` 路径 |

在对话中可以说：

> "今天有什么推荐？" → 运行短线策略
> "茅台基本面怎么样" → 显示 ROE/EPS/估值
> "有什么热点题材" → 同花顺强势股归因
> "跑一下回测" → 执行回测引擎
> "对比两次回测" → 查看历史回测记录对比
> "查看回测历史" → 列出所有回测记录

---

## 🙏 致谢

本项目参考和借鉴了以下开源项目和社区的思路：

| 项目 | 贡献 | 仓库 |
|------|------|------|
| **a-stock-data** （Simon 林） | 数据源架构模式、em_get 限流设计、同花顺热点/板块归属/龙虎榜 API 参考实现 | [simonlin1212/a-stock-data](https://github.com/simonlin1212/a-stock-data) |
| **Sequoia-X** | 形态识别策略（金叉/海龟/高旗形等）RPS 排位逻辑 | 参考项目 |
| **daily-stock-analysis** | StockTrendAnalyzer 技术评分体系设计思路 | 参考项目 |
| **mootdx** | 通达信 TCP 行情协议 Python 封装，提供稳定 K 线 + 财务数据 | [mootdx](https://github.com/bopo/mootdx) |
| **akshare** | A 股数据接口标准，提供大单数据和代码列表 | [akshare](https://github.com/jindaxiang/akshare) |

---

## 📄 许可证

[MIT](LICENSE)

---

<div align="center">

**如果这个项目对你有帮助，欢迎 Star ⭐**

由 [cbzhang86](https://github.com/cbzhang86) 维护 · 使用 Claude Code 辅助开发

</div>
