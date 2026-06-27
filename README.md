# 🏛️ A股智能选股系统

[![Python](https://img.shields.io/badge/python-3.10%2B-blue)](https://www.python.org/)
[![License](https://img.shields.io/badge/license-MIT-green)](LICENSE)
[![Compatible with](https://img.shields.io/badge/Claude%20Code-Skill-8A2BE2)](SKILL.md)

**Stock-Picker V4 — 全栈多因子量化选股框架**

14 个直连数据源 · 30+ 量化因子 · 短线尾盘 + 长线持股双策略 · 真实 K 线回测引擎 · Ridge 自学习权重优化

[简体中文](README.md) · [English](README.en.md)

---

## 目录

- [项目简介](#项目简介)
- [功能特性](#功能特性)
- [快速开始](#快速开始)
- [短线策略](#短线策略)
- [回测引擎](#回测引擎)
- [权重自学习](#权重自学习)
- [系统架构](#系统架构)
- [数据源](#数据源)
- [AI 助手集成](#ai-助手集成)
- [致谢](#致谢)
- [许可证](#许可证)

---

## 项目简介

Stock-Picker 是一个面向 A 股市场的全栈量化选股系统，覆盖从数据采集、因子计算、策略评分到回测验证的完整闭环。

A 股量化投资面临三大核心问题：

- **数据源分散** — 行情、资金流、龙虎榜、北向资金分布在 14 个不同平台
- **接口易被封** — 东方财富 WAF 风控拦截非浏览器 HTTP 请求
- **策略验证难** — 缺乏真实历史数据用于回测验证

本项目通过以下方案解决：

- **数据源优先级回退链** — mootdx TCP（永不封 IP）→ 腾讯 HTTP → 东财限流
- **统一限流层** — 所有东财接口经 `em_get()` 串行限流
- **真实 K 线回测** — 零 `np.random`，无前视偏差

---

## 功能特性

### 📊 数据能力 — 14 数据源统一接入

- **mootdx TCP 直连** — K 线 + 财务快照 37 字段，永不封 IP，~0.1s/只
- **腾讯财经 HTTP** — 全市场 5205 只实时行情，不封 IP，~46s
- **同花顺 10jqka** — 强势股 + 题材归因，零鉴权 73ms
- **ASHareHub** — 个股北向持仓 / 资金流 / 技术因子 / 概念板块 / 财务指标，日配额 100 次/天
- **hexin.cn** — 北向资金实时汇总，零鉴权
- **东财（em_get 限流）** — 板块归属 / 龙虎榜
- **akshare** — 大单资金流 / A 股代码列表
- **独立熔断** — 每数据源独立熔断，10 分钟自动恢复

### 🧠 策略能力

1. **短线尾盘** — 7 因子加权评分 + 市场环境评估 → 初筛 5205→4900 → 预评分 → Top 200 详评 → 仓位分配。极差市自动跳过
2. **长线持股** — ROE + PE + PB 基本面评分，3-6 个月持有周期
3. **市场环境感知** — 涨跌比 / 涨停跌停比 / 中位数涨幅 / 强势股数 / 北向资金 → 综合分
4. **技术评分** — 6 维度系统化评分（趋势 30 + 乖离 20 + 量能 15 + 支撑 10 + MACD 15 + RSI 10）
5. **形态识别** — 9 种 K 线形态自动检测（金叉 / 海龟突破 / 高旗形等）
6. **组合优化** — 评分加权仓位分配，单只上限 40%、保底 10%
7. **防凑数** — 第 3 名与第 1 名分差 > 20 分自动裁掉

### 🔄 自学习反馈闭环

1. SQLite 持久化存储每次推荐及 7 因子分解得分
2. 自动回填 T+1 / T+5 / T+20 真实收益
3. Ridge 回归将因子得分映射到实际收益率
4. 积累 60 条有 T+1 结果的推荐后，每 50 条自动触发优化
5. 三段式工作流：`check_and_report()` 产出报告 → 审批 → `apply_from_report()` 写入
6. 新权重持久化到 `v1.json`，下次启动自动加载
7. 坍缩保护（单因子 ≥ 80% 跳过）、列名不匹配自动填充 0.5

---

## 快速开始

### 环境要求

- Python 3.10+，Windows / Linux / macOS
- 网络：国内国外均可（mootdx TCP 和腾讯 API 不受地域限制）

### 安装

```bash
git clone https://github.com/cbzhang86/stock-picker.git
cd stock-picker
pip install -r requirements.txt

# 可选：注册免费 ASHareHub API Key
export ASHAREHUB_API_KEY="ash_your_key_here"
```

### 第一次运行

```bash
python scripts/eod_stock_picker.py --mode short
```

运行过程会看到：A 股代码列表 5205 只 → 全市场行情 → 同花顺强势股 → 预过滤 → 初步评分 → Top 200 详评 → 大单缓存加载 → 最终推荐。约 4 分钟后输出每日简报，包含市场概览、题材热度 TOP10、短线评分排名、长线关注。

### 其他命令

```bash
# 查看模型状态和近期表现
python scripts/eod_stock_picker.py --status

# 长线策略
python scripts/eod_stock_picker.py --mode long

# 回测验证（默认近 3 个月）
python scripts/run_backtest.py --mode short

# 回测版本对比
python scripts/run_backtest.py --list
python scripts/run_backtest.py --compare 1 2

# 健康检查（23 项）
python scripts/verify.py
```

---

## 短线策略

### 完整流程

| 步骤 | 操作 | 耗时 |
|------|------|------|
| 1 | `get_all_codes()` — 读取代码缓存 | 0.001s |
| 2 | `get_all_quotes()` — 腾讯 API 拉取 5205 只行情 | ~46s |
| 3 | `get_ths_hot_stocks()` — 同花顺强势股 + 题材归因 | ~0.22s |
| 4 | `_prefilter()` — 初筛：非 ST / 成交额 > 3000 万 / 非涨跌停 | ~0.5s |
| 5 | 5 维预评分 — 流动性 30 + 活跃度 20 + 动量 15 + 风险 20 + PE 15 → Top 200 | ~0.3s |
| 6 | `get_main_fund()` — 大单资金流全量缓存 | ~25s |
| 7 | `get_kline()` × 200 — mootdx TCP 3 线程并行拉取 K 线 | ~25s |
| 8 | RPS 横截面排名 — 200 只的 20 日涨幅百分位排名 | ~0.1s |
| 9 | `scoring_model.score()` — 7 因子加权评分 + 防凑数检查 | ~0.2s |
| 10 | `portfolio_optimizer` — 评分加权仓位分配 | ~0.05s |
| 11 | 板块 + 龙虎榜补充（仅 Top 3） | ~8s |
| 12 | 输出简报 + 保存报告 | 15:00 前完成 |

### 因子权重

| 因子 | 权重 | 数据源 | 计算方式 |
|------|------|--------|---------|
| 主力资金流 | 25% | 大单交易 / ASHareHub / 同花顺 | 横截面百分位排名 |
| 动量/RPS | 25% | 全市场涨幅排位 | 20 日收益百分位 → 0-100 |
| 技术形态 | 15% | mootdx K 线 6 维评分 | 趋势 30 + 乖离 20 + 量能 15 + 支撑 10 + MACD 15 + RSI 10 |
| 量价配合 | 10% | 量比 + 尾盘结构 | 0.8~2.0 = 80 分 |
| 北向资金 | 10% | hexin.cn / asharehub | 持股量变化方向评分 |
| 同花顺热点 | 10% | 10jqka | 强势股 + 题材标签 |
| 龙虎榜 | 5% | 东财 datacenter | 上榜且机构净买入为正 |
| 风险 | 过滤层 | risk_filter.py | 前置拦截，不占权重 |

---

## 回测引擎

### 核心特性

1. **数据源** — mootdx 真实 K 线，零 `np.random`，无前视偏差
2. **成交价** — 次日开盘价
3. **滑点** — 0.1%（可配置）
4. **佣金** — 万三（可配置）
5. **交易规则** — T+1 止盈 +2%、止损 -2%（从 `config.yml sell` 段读取）、T+3 时间止损
6. **仓位模拟** — 按 `allocation_pct` 分配资金，逐日 T+1 开盘买 / 收盘卖
7. **基准** — 沪深 300

### 输出指标

总交易次数 / 胜率 / 平均收益 T+1/T+5 / 最大单笔盈亏 / 最大回撤 / 夏普比率 / 策略总收益 / 超额收益 / 权益曲线 / 月度收益表 / 因子 IC 信息系数 / 赢输分差 / 交易明细 / 优化建议

### 最新回测数据（2026-04-01 ~ 2026-06-27）

| 指标 | 数值 |
|------|------|
| 总交易次数 | 95 次 |
| 胜率 | 57.9% |
| 平均收益 T+1 | +1.63% |
| 平均收益 T+5 | +5.87% |
| 最大回撤 | -13.93% |
| 夏普比率 | 4.36 |
| 策略收益 | +62.50% |
| 沪深 300 | +7.56% |
| 超额收益 | +54.94% |

### 已知限制

- 资金流 / 题材 / 龙虎榜 / 北向数据在回测中不可用 —— 当日大单不可回溯，回测仅验证量价 + 动量 + 技术因子
- mootdx 提供约 600 个交易日（~2.5 年），无法回测 2019 年以前的策略
- 以次日开盘价成交，无法模拟盘中即时成交

---

## 权重自学习

实盘推荐 → 自动回填 T+1 收益 → 积累 60 条有 outcome 的记录 → Ridge 回归 → 因子方差审计 → 坍缩检查 → 新权重 → `v1.json`

### 优化器（`feedback/optimizer.py`）

- **算法**：`sklearn.linear_model.Ridge(alpha=1.0)`
- **输入**：因子原始得分 → 实际 T+1 收益率
- **输出**：归一化新权重（负系数截断为 0，正系数归一化到总和 1）
- **触发条件**：胜率 < 50% 或自上次优化以来新增 ≥ 50 条，且数据新鲜度检查通过
- **坍缩保护**：单因子 ≥ 80% 跳过优化
- **列名不匹配**：新因子在旧记录中无数据，自动填充 0.5
- **三段式工作流**：`check_and_report()` 只报告不写入 → 用户审批 → `apply_from_report()` 执行写入
- **版本追溯**：旧版本保留在 `data/weights/`（带时间戳）

---

## 系统架构

```
stock-picker/
│
├── core/                          # 核心引擎
│   ├── data_engine.py             # 多源数据融合（14 数据源、缓存、熔断）
│   ├── factor_library.py          # 30+ 因子 0-100 评分映射
│   ├── scoring_model.py           # 多因子加权评分 + 权重加载
│   ├── technical_scorer.py        # 6 维度 100 分制技术分析
│   ├── risk_filter.py             # 6 道风险检查
│   ├── backtest_engine.py         # 回测引擎（mootdx 真实 K 线快照）
│   ├── backtest_store.py          # 回测结果 SQLite 持久化
│   └── portfolio_optimizer.py     # 仓位分配
│
├── strategies/                    # 策略层
│   ├── short_term.py              # 短线尾盘策略（7 因子 + 市场评估）
│   ├── long_term.py               # 长线策略（6 因子基本面）
│   └── base.py                    # 策略抽象基类
│
├── reports/                       # 报告层
│   ├── market_briefing.py         # 每日市场简报
│   ├── backtest_report.py         # 回测报告渲染
│   └── daily_report.py            # Markdown 报告文件存储
│
├── feedback/                      # 反馈循环
│   ├── tracker.py                 # SQLite 预测追踪
│   ├── optimizer.py               # Ridge 回归权重优化器
│   └── data_collector.py          # 因子仓库（逐日快照）
│
├── scripts/                       # 用户入口
│   ├── eod_stock_picker.py        # 主入口
│   ├── run_backtest.py            # 回测入口
│   └── verify.py                  # 23 项健康检查
│
├── config.yml                     # 中心配置
├── SKILL.md                       # AI 助手技能文件
├── CHEATSHEET.md                  # 使用备忘录
├── requirements.txt               # Python 依赖
│
└── data/                          # 运行时数据（自动创建）
    ├── cache/                     # K 线 / 代码 / 回测 / 因子缓存
    ├── db/                        # predictions.db
    ├── reports/                   # 每日报告 + 简报
    └── weights/                   # 优化器输出权重
```

---

## 数据源

| 数据源 | 用途 | 协议 | 特性 |
|--------|------|------|------|
| mootdx TCP | K 线 + 财务 37 字段 | TCP 7709 | 永不封 IP，~0.1s/只 |
| 腾讯财经 HTTP | 全市场实时行情 | HTTP | 5205 只，不封 IP，~46s |
| 同花顺 10jqka | 强势股 + 题材归因 | HTTP | 零鉴权，73ms |
| hexin.cn | 北向资金汇总 | HTTP | 实时，零鉴权 |
| ASHareHub | 北向持仓/资金流/技术/概念/财务 | HTTP | 4 端共享 100 次/天 |
| 东财 em_get | 板块归属 / 龙虎榜 | HTTP | 串行限流，WAF 保护 |
| akshare | 大单资金流 / 代码列表 | HTTP | 独立熔断 |

每个数据源有 **独立熔断**，互不影响。熔断每 10 分钟自动恢复（`_recover_sources()`）。被熔断的因子自动中性化为 50 分，权重自动分配给其他活跃因子。

---

## AI 助手集成

本项目通过 `SKILL.md` 支持与 AI 编程助手对话式交互。兼容：

- **Claude Code** — 仓库根目录 `SKILL.md`，自动识别
- **OpenClaw** — 将 `SKILL.md` 放入 `~/.claude/skills/stock-picker/`
- **Hermes** — 技能配置指向 `SKILL.md` 路径

### 示例对话

> "今天有什么推荐？" → 运行短线策略
> "茅台基本面怎么样" → 显示 ROE / EPS / 估值
> "跑一下回测" → 执行回测引擎
> "对比两次回测" → 查看历史回测记录对比
> "检查系统健康" → 运行 verify.py 23 项检查

---

## 致谢

本项目参考和借鉴了以下开源项目和社区的思路：

- **[a-stock-data](https://github.com/simonlin1212/a-stock-data)** (Simon Lin) — 数据源架构模式、`em_get` 限流设计、同花顺热点/板块归属/龙虎榜 API 参考实现
- **[Sequoia-X](https://github.com/sngyai/Sequoia-X)** — 形态识别策略（金叉/海龟/高旗形等）RPS 排位逻辑
- **[daily-stock-analysis](https://github.com/ZhuLinsen/daily_stock_analysis)** — StockTrendAnalyzer 技术评分体系设计思路
- **[mootdx](https://github.com/mootdx/mootdx)** — 通达信 TCP 行情协议 Python 封装，提供稳定 K 线 + 财务数据
- **[akshare](https://github.com/akfamily/akshare)** — A 股数据接口标准，提供大单数据和代码列表

---

## 许可证

[MIT](LICENSE)

---

<div align="center">
由 <a href="https://github.com/cbzhang86">cbzhang86</a> 维护 · 使用 <a href="https://claude.ai/code">Claude Code</a> 辅助开发

如果这个项目对你有帮助，欢迎 ⭐
</div>
