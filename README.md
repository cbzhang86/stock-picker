# 🏛️ A股智能选股系统

[![Python](https://img.shields.io/badge/python-3.10%2B-blue)](https://www.python.org/)
[![License](https://img.shields.io/badge/license-MIT-green)](LICENSE)
[![Compatible with](https://img.shields.io/badge/Claude%20Code-Skill-8A2BE2)](SKILL.md)

**Stock-Picker V4.3 — 全栈多因子量化选股框架**

8 个直连数据源 / 16 网络端点 · 30+ 量化因子 · 短线尾盘 + 长线持股双策略 · 真实 K 线回测引擎 · OOS IC 校准权重 · 专家 Ensemble 第二意见

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

- **数据源分散** — 行情、资金流、龙虎榜、北向资金分布在不同平台
- **接口易被封** — 东方财富 WAF 风控拦截非浏览器 HTTP 请求
- **策略验证难** — 缺乏真实历史数据用于回测验证

本项目通过以下方案解决：

- **数据源优先级回退链** — mootdx TCP（永不封 IP）→ 腾讯 HTTP → 东财限流
- **统一限流层** — 所有东财接口经 `em_get()` 串行限流
- **真实 K 线回测** — 零 `np.random`，无前视偏差
- **权重按 OOS IC 说话**（V4.3）— 全部权重经 walk-forward 样本外 IC 校准，反向因子降权至探索位，而非照搬研报中位数

---

## 功能特性

### 📊 数据能力 — 8 数据源 / 16 网络端点统一接入

> 口径说明：接入实体 8 个（mootdx / 腾讯 / 同花顺 / ASHareHub / 东财 / akshare / baostock / 新浪），
> 网络端点 16 个，纳入健康状态登记 16 个（`_source_status`，2026-09-03 补齐 3 键后与端点数一致），纳入独立熔断 8 个（`_source_available`）。
> 早期版本写作"14 数据源"，无代码依据，已更正。

- **mootdx TCP 直连** — K 线 + 财务快照 37 字段，永不封 IP，~0.1s/只
- **腾讯财经 HTTP** — 全市场 5205 只实时行情，不封 IP，~46s
- **同花顺 10jqka** — 强势股 + 题材归因，零鉴权 73ms
- **ASHareHub** — 个股北向持仓 / 资金流 / 技术因子 / 概念板块 / 财务指标，日配额 100 次/天
- **东财 datacenter-web** — 北向资金日度净流入汇总，零鉴权
  （早期文档写作 `hexin.cn`，代码中并无该域名，已更正）
- **东财（em_get 限流）** — 板块归属 / 龙虎榜
- **akshare** — 大单资金流 / A 股代码列表
- **独立熔断** — 8 个数据源独立熔断；恢复入口挂在 `get_all_quotes()`，10 分钟自动恢复

### 🧠 策略能力

1. **短线尾盘** — 7 因子加权评分 + 专家 Ensemble 第二意见 + 市场环境评估 → 初筛 5205→4900 → 预评分 → Top 200 详评 → 仓位分配。极差市自动跳过
2. **长线持股** — ROE + PE + PB 基本面评分，3-6 个月持有周期
3. **策略健康度展示**（V4.3 行为变更）— kill-switch 从"触发即停推"改为"健康度提示"：近 20 个有结果交易日的累计收益随推荐下发（`rec['strategy_health']`），简报渲染风险提示，是否参与由使用者权衡
4. **市场环境感知** — 涨跌比 / **打板情绪复合分**（封板率×0.4 + 连板晋级率×0.3 + 昨日涨停溢价×0.3，akshare 涨停三池，2026-09-07 升级）/ 中位数涨幅 / 强势股数 / 北向资金 → 综合分
5. **技术评分** — 6 维度系统化评分（趋势 30 + 乖离 20 + 量能 15 + 支撑 10 + MACD 15 + RSI 10）
6. **形态识别** — 9 种 K 线形态自动检测（金叉 / 海龟突破 / 高旗形等）
7. **组合优化** — 评分加权仓位分配，单只上限 40%、保底 10%
8. **防凑数** — 第 3 名与第 1 名分差 > 20 分自动裁掉
9. **专家 Ensemble 第二意见**（V4.3）— 5 维独立评分（基本面/技术/资金/估值/事件）与 7 因子加权模型差异检测：一致微调上拉、分歧降权、严重冲突（Δ>25）标记 conflict 并降仓
10. **组合层风控三件套**（V4.3）— 同板块最多 2 只 + 持仓两两相关性 ≤0.85 + 波动率保险丝（全市场中位涨幅超 3% 触发降仓）
11. **追高惩罚**（V4.3）— 当日涨幅 >7% 按线性砍分（最高 -50%），依据 A 股短线反转效应（华泰月度跟踪：沪深300 池 1 个月反转 IC 27.69%）

### 🔄 自学习反馈闭环

1. SQLite 持久化存储每次推荐及 7 因子分解得分
2. 自动回填 T+1 / T+5 / T+20 真实收益
3. Ridge 回归将因子得分映射到实际收益率（`check_and_report()` 只产报告，**不自动写入**——`apply_from_report()` 刻意停用，权重落地需人工执行）
4. V4.3 权重校准主路径：`calibrate_weights.py` 按 walk-forward OOS IC（三口径取悲观值）分配，负 IC 归零、单因子上限 0.50，`--apply` 需人工审批
5. 新权重持久化到 `v1.json`（加载优先级 v1.json > config.yml），下次启动自动加载
6. 坍缩保护（单因子 ≥ 80% 跳过）、列名不匹配自动填充 0.5

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

# 统一 CLI 入口（2026-09-07）：一个命令记住所有常用操作
python scripts/pick.py pick          # 尾盘选股
python scripts/pick.py health        # 快速门禁（9 项）
python scripts/pick.py backfill      # T+1/T+5/T+20 结果回填
python scripts/pick.py oos            # 全量 OOS 因子诊断
python scripts/pick.py calibrate      # 权重校准建议（不自动生效）

# 每日统一调度（交易日→选股 / 非交易日→配额预取，内置交易日历分流）
python scripts/daily_job.py

# 回测验证（默认近 3 个月）
python scripts/run_backtest.py --mode short

# 回测版本对比
python scripts/run_backtest.py --list
python scripts/run_backtest.py --compare 1 2

# 回归门禁（任何代码/权重改动后必跑；--full 追加 OOS 面板诊断）
python scripts/evaluate_all.py

# 健康检查（9 项）
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

> **权重唯一事实源 = `data/weights/v1.json`**（ScoringModel 加载优先级 v1.json > config.yml）。
> 下表为 2026-09-05 OOS 校准后的生效值；调整权重唯一入口 `python scripts/calibrate_weights.py`（`--apply` 需人工审批）。config.yml 权重段是历史草稿。

| 因子 | 权重 | 数据源 | 计算方式 | OOS IC |
|------|------|--------|---------|--------|
| 同花顺热点 | 50% | 10jqka 三源融合 | 强势股 + 题材标签 | +0.0651 (t=+2.17)，唯一显著 |
| 技术形态 | 16% | mootdx K 线 6 维评分 | 趋势 30 + 乖离 20 + 量能 15 + 支撑 10 + MACD 15 + RSI 10 | -0.0462 弱负向，观察 |
| 主力资金流 | 15% | 大单交易 / ASHareHub / 同花顺 | 横截面百分位排名 | +0.0056 弱正 |
| 量价配合 | 12% | 量比 + 尾盘结构 | 0.8~2.0 = 80 分 | -0.0460 稳定反向，降权观察 |
| 动量/RPS | 4% | 全市场涨幅排位 | 20 日收益百分位 → 0-100 | -0.0926 强反向，探索位 |
| 龙虎榜 | 3% | 东财 datacenter | 上榜且机构净买入为正 | 噪声级 |
| 北向资金 | 0% | — | 2024-08 政策停公开，恒中性 50 | — |
| 估值/事件（待验证） | 0% | 通达信 / 公告 | 链路已通，快照 ≥60 交易日 OOS 验证后启用 | 待验证 |
| 风险 | 过滤层 | risk_filter.py | 前置拦截，不占权重 | — |

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

> ⚠️ 口径警示：以下成绩产生于 `min_score=60` 旧配置。当前 `config.yml` 基准 `min_score=75`，修复后同区间重跑（run#26，2026-09-05 代码、静态门槛）为 **0 笔达标**——历史高收益不可复现，是"挤水分"（momentum 降权 + 涨停代理过滤 + 回测口径 hot_theme 中性化）而非代码退化。2026-09-07 起实盘评分门槛随市况动态浮动（强市 65 / 中性 70 / 弱市 75，`dynamic_min_score` 可覆盖），如今重跑未必再是 0 笔。引用时必须带上 min_score 值。

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
│   ├── data_engine.py             # 多源数据融合（8 数据源/16 端点、缓存、熔断）
│   ├── factor_library.py          # 30+ 因子 0-100 评分映射
│   ├── scoring_model.py           # 多因子加权评分 + 权重加载 + 追高惩罚
│   ├── technical_scorer.py        # 6 维度 100 分制技术分析
│   ├── risk_filter.py             # 6 道风险检查
│   ├── backtest_engine.py         # 回测引擎（mootdx 真实 K 线快照）
│   ├── backtest_store.py          # 回测结果 SQLite 持久化
│   ├── portfolio_optimizer.py     # 仓位分配
│   ├── expert_ensemble.py         # 5 维专家第二意见（V4.3 新增）
│   ├── oos_validator.py           # walk-forward 样本外 IC 验证（V4.3 新增）
│   ├── trading_calendar.py        # 集中式交易日历（V4.3 新增）
│   ├── drift_monitor.py           # 绩效漂移监控 PSI（V4.3 新增）
│   ├── data_quality_monitor.py    # 数据质量巡检（V4.3 新增）
│   ├── fundamental_provider.py    # 估值/基本面（点时化防前视，V4.3 新增）
│   ├── event_provider.py          # 公告/事件催化（V4.3 新增）
│   └── factor_standardizer.py     # 横截面标准化（实验开关，V4.3 新增）
│
├── strategies/                    # 策略层
│   ├── short_term.py              # 短线尾盘策略（7 因子 + 市场评估 + ensemble + 组合风控）
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
│   ├── verify.py                  # 9 项健康检查
│   ├── pick.py                    # 统一 CLI 入口（V4.3 新增）
│   ├── daily_job.py               # 每日统一调度（V4.3 新增）
│   ├── evaluate_all.py            # 回归门禁（V4.3 新增）
│   ├── calibrate_weights.py       # OOS 权重校准（V4.3 新增）
│   ├── calibrate_slippage.py      # 尾盘滑点校准（V4.3 新增）
│   ├── capacity_check.py          # 资金容量测算（V4.3 新增）
│   ├── multiple_testing.py        # DSR/PBO 多重检验审计（V4.3 新增）
│   ├── prefetch_tdx.py           # 通达信估值/事件预取（V4.3 新增）
│   └── prefetch_asharehub.py     # 非交易日配额预取
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
    └── weights/                   # v1.json 生效权重 + 历史版本
```

---

## 数据源

| 数据源 | 用途 | 协议 | 特性 |
|--------|------|------|------|
| mootdx TCP | K 线 + 财务 37 字段 | TCP 7709 | 永不封 IP，~0.1s/只 |
| 腾讯财经 HTTP | 全市场实时行情 | HTTP | 5205 只，不封 IP，~46s |
| 同花顺 10jqka | 强势股 + 题材归因 | HTTP | 零鉴权，73ms |
| 东财 datacenter-web | 北向资金汇总 | HTTP | 日度净流入，零鉴权 |
| ASHareHub | 北向持仓/资金流/技术/概念/财务 | HTTP | 4 端共享 100 次/天 |
| 东财 em_get | 板块归属 / 龙虎榜 | HTTP | 串行限流，WAF 保护 |
| akshare | 大单资金流 / 代码列表 | HTTP | 独立熔断 |

纳入 `_source_available` 的 8 个源有**独立熔断**，互不影响。熔断每 10 分钟自动恢复（`_recover_sources()`），
恢复入口在 `get_all_quotes()`。被熔断的因子自动中性化为 50 分，权重自动分配给其他活跃因子。

> ⚠️ 已知限制（2026-09-03 核实）：`_source_available` 是**类属性**，同进程内多实例共享
> 同一份熔断状态；而 `_source_status` 是实例属性。两者生命周期不一致，报告中显示的状态
> 可能与熔断状态不同步。当前行为（全局共享）可避免对故障源重复请求，属可接受状态，暂不修改。

**熔断与状态登记的键名必须一一对应**（2026-09-03 修复）：
`_update_source_status()` 内部有 `if source_key in self._source_status` 判断，
键未登记会被**静默丢弃**——熔断已生效，报告却显示为正常。
此前 `ths_fund_flow` / `big_deal` / `lockup` 三个键只登记在 `_source_available` 中，
这三个源的故障始终不可见；同时北向键名两个字典不一致（`north_flow` vs `akshare_north_flow`）。
现已在 `_source_status` 补齐 3 个键、统一北向键名为 `akshare_north_flow`，
并为 `lockup` 补上成功/失败的状态上报。新增数据源时请务必**两个字典同时登记**。

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
> "检查系统健康" → 运行 verify.py 9 项检查

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
