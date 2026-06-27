# 🏛️ A股智能选股系统

**Stock-Picker V4 — 全栈多因子量化选股框架**

14 个直连数据源 · 30+ 量化因子 · 短线尾盘 + 长线持股双策略 · 真实 K 线回测引擎 · Ridge 自学习权重优化

**简体中文** · [English](README.en.md)

[![Python](https://img.shields.io/badge/python-3.10%2B-blue)](https://www.python.org/) [![License](https://img.shields.io/badge/license-MIT-green)](LICENSE) [![Compatible with](https://img.shields.io/badge/Claude%20Code-OpenClaw-Hermes-8A2BE2)](SKILL.md)

---

**项目简介**

Stock-Picker 是一个面向 A 股市场的全栈量化选股系统，覆盖从数据采集、因子计算、策略评分到回测验证的完整闭环。

A 股量化投资面临三大核心问题：数据源分散（行情、资金流、龙虎榜、北向资金在不同平台）、接口易被封（东方财富 WAF 风控）、策略验证难（缺乏真实回测数据）。本项目通过以下方案解决：

- **数据源优先级回退链** — mootdx TCP 永不封 IP → 腾讯 HTTP → 东财限流
- **统一限流层** — 所有东财接口经 `em_get()` 串行限流
- **真实 K 线回测** — 零 `np.random`，无前视偏差

---

**功能特性**

**📊 数据能力 — 14 数据源统一接入**

- **mootdx TCP 直连** — K 线 + 财务快照 37 字段，永不封 IP，~0.1s/只
- **腾讯财经 HTTP** — 全市场 5205 只实时行情，不封 IP，~46s
- **同花顺 10jqka** — 强势股 + 题材归因，零鉴权 73ms
- **ASHareHub** — 个股北向持仓 / 资金流 / 技术因子 / 概念板块 / 财务指标，日配额 100次/天
- **hexin.cn** — 北向资金实时汇总，零鉴权
- **东财（em_get 限流）** — 板块归属 / 龙虎榜，WAF 风控保护
- **akshare** — 大单资金流 / A 股代码列表
- **独立熔断** — 每数据源独立熔断，10 分钟自动恢复

**🧠 策略能力**

1. **短线尾盘** — 7 因子加权评分 + 市场环境评估 → 初筛 5205→4900 → 预评分 → Top 200 详评 → 仓位分配。极差市自动跳过。
2. **长线持股** — ROE + PE + PB 基本面评分，3-6 个月持有周期
3. **市场环境感知** — 涨跌比 / 涨停跌停比 / 中位数涨幅 / 强势股数 / 北向资金 → 综合分
4. **技术评分** — 6 维度系统化评分（趋势 30 + 乖离 20 + 量能 15 + 支撑 10 + MACD 15 + RSI 10）
5. **形态识别** — 9 种 K 线形态自动检测（金叉 / 海龟突破 / 高旗形等）
6. **组合优化** — 评分加权仓位分配，单只上限 40%、保底 10%
7. **防凑数** — 第 3 名与第 1 名分差 > 20 分自动裁掉

**🔄 自学习能力 — 反馈闭环**

1. SQLite 持久化存储每次推荐及 7 因子分解得分
2. 自动回填 T+1 / T+5 / T+20 真实收益
3. Ridge 回归将因子得分映射到实际收益率
4. 积累 60 条有 T+1 结果的推荐后每 50 条自动触发优化
5. 三段式工作流：`check_and_report()` 产出报告 → 审批 → `apply_from_report()` 写入
6. 新权重持久化到 `v1.json`，下次启动自动加载
7. 坍缩保护（单因子 ≥ 80% 跳过）、列名不匹配自动填充 0.5

---

**快速开始**

**环境要求**

- Python 3.10+，Windows / Linux / macOS
- 网络：国内国外均可（mootdx TCP 和腾讯 API 不受地域限制）

**安装**

```bash
git clone https://github.com/cbzhang86/stock-picker.git
cd stock-picker
pip install -r requirements.txt

# 可选：注册 ASHareHub 免费 API Key
export ASHAREHUB_API_KEY="ash_your_key_here"
```

**第一次运行**

```bash
python scripts/eod_stock_picker.py --mode short
```

运行过程会看到 A 股代码列表 5205 只 → 全市场行情 → 同花顺强势股 → 预过滤 → 初步评分 → 详评 200 只 → 大单缓存加载 → 最终推荐 3 只。约 4 分钟后输出每日简报，包含市场概览、题材热度 TOP10、短线评分排名、长线关注。

**其他命令**

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

**系统架构**

```
stock-picker/
│
├── core/                          核心引擎
│   ├── data_engine.py             多源数据融合（14个数据源统一接入、缓存、熔断）
│   ├── factor_library.py          30+量化因子 0-100 评分映射
│   ├── scoring_model.py           多因子加权评分 + 评级映射 + 权重加载
│   ├── technical_scorer.py        6维度100分制系统化技术分析
│   ├── risk_filter.py             6道风险检查（ST/流动性/涨跌停/换手率）
│   ├── backtest_engine.py         回测引擎（mootdx真实K线快照，零随机数）
│   ├── backtest_store.py          回测结果SQLite持久化，版本对比
│   └── portfolio_optimizer.py     仓位分配（评分加权/等权）
│
├── strategies/                    策略层
│   ├── short_term.py              短线尾盘策略（7因子 + 市场评估）
│   ├── long_term.py               长线持股策略（6因子基本面 + 北向 + 动量）
│   └── base.py                    策略抽象基类
│
├── reports/                       报告层
│   ├── market_briefing.py         每日市场简报生成器（5板块全景）
│   ├── backtest_report.py         回测报告 + 每日推荐报告Markdown渲染
│   └── daily_report.py            Markdown报告文件存储
│
├── feedback/                      反馈循环
│   ├── tracker.py                 SQLite预测追踪（predictions + outcomes）
│   ├── optimizer.py               Ridge回归权重优化器（三段式工作流）
│   └── data_collector.py          因子仓库（资金流/北向/热点/龙虎榜逐日快照）
│
├── scripts/                       用户入口
│   ├── eod_stock_picker.py        唯一主入口（策略/状态/简报/因子采集）
│   ├── run_backtest.py            全量回测入口（--list/--compare）
│   └── verify.py                  23项综合健康检查
│
├── config.yml                     核心配置（权重/风控/回测参数）
├── SKILL.md                       AI助手技能文件
├── README.md                      项目自述（本文档）
├── README.en.md                   英文自述
├── CHEATSHEET.md                  使用备忘录（数据源/熔断/编码规范速查）
├── requirements.txt               Python依赖清单
│
└── data/                          运行时数据（自动创建）
    ├── cache/                     K线/代码/回测/因子缓存
    ├── db/                        predictions.db 推荐记录 + 收益结果
    ├── reports/                   每日报告 + 简报
    └── weights/                   优化器输出的权重文件
```

---

**短线策略评分详解**

**完整流程**

1. `get_all_codes()` — 读取代码缓存（0.001s，首次从 akshare 拉取）
2. `get_all_quotes()` — 腾讯 API 拉取 5205 只行情（~46s）
3. `get_ths_hot_stocks()` — 同花顺强势股 + 题材归因（~0.22s）
4. `_prefilter()` — 初筛：非 ST / 成交额 > 3000 万 / 非涨跌停（→ ~4900 只）
5. 5 维预评分 — 流动性 30 + 活跃度 20 + 动量 15 + 风险 20 + PE 15（→ Top 200）
6. `get_main_fund()` — 大单资金流全量缓存（~25s，后续 199 次调用毫秒级）
7. `get_kline()` × 200 — mootdx TCP 3 线程并行拉取 K 线 + 技术评分（~25s）
8. RPS 横截面排名 — 200 只的 20 日涨幅百分位排名
9. `scoring_model.score()` — 7 因子加权评分 + 防凑数检查
10. `portfolio_optimizer` — 评分加权仓位分配（单只上限 40%/保底 10%）
11. `get_stock_blocks()` + `get_dragon_tiger()` — 板块归属和龙虎榜补充（仅 Top 3）
12. 输出简报 + 保存报告 — 15:00 前完成

**因子权重表**

| 因子 | 权重 | 数据源 | 计算方式 |
|------|------|--------|---------|
| 主力资金流 | 25% | 大单交易汇总 / ASHareHub / 同花顺 | 横截面百分位排名，净流入 > 300 万 = 高分 |
| 动量/RPS | 25% | 全市场涨幅排位 | 百分位排名映射 0-100 |
| 技术形态 | 15% | mootdx K 线 6 维评分 | 趋势 30 + 乖离 20 + 量能 15 + 支撑 10 + MACD 15 + RSI 10 |
| 量价配合 | 10% | 量比 + 尾盘成交结构 | 0.8~2.0 = 80 分，尾盘资金集中度增强 |
| 北向资金 | 10% | hexin.cn 汇总 / asharehub 个股持仓 | 持股量变化方向评分 |
| 同花顺热点 | 10% | 10jqka 强势股 + 题材归因 | 在强势股列表中且有题材标签 = 加分 |
| 龙虎榜 | 5% | 东财 datacenter | 上榜 + 机构净买入为正 = 加分 |
| 风险 | 过滤层 | risk_filter.py | 前置拦截严重风险，不占权重 |

---

**回测引擎**

**核心特性**

1. **数据源** — mootdx 真实 K 线，零 `np.random`，无前视偏差
2. **成交价** — 次日开盘价
3. **滑点** — 0.1%（可配置）
4. **佣金** — 万三（可配置）
5. **交易规则** — T+1 止盈 +2%，止损 -2%（从 `config.yml sell` 段读取），T+3 时间止损
6. **仓位模拟** — 按 `allocation_pct` 分配资金，逐日 T+1 开盘买 / 收盘卖
7. **基准** — 沪深 300

**输出指标**

总交易次数 / 胜率 / 平均收益 T+1/T+5 / 最大单笔盈利 / 最大单笔亏损 / 最大回撤 / 夏普比率 / 策略总收益 / 超额收益 / 权益曲线 / 月度收益表 / 因子 IC 信息系数 / 因子赢输分差 / 交易明细 / 优化建议

**已知限制**

- 资金流 / 题材 / 龙虎榜 / 北向数据在回测中不可用（当日大单不可回溯），回测仅验证量价 + 动量 + 技术因子的逻辑有效性
- 收益因子（资金流/北向等）的最终权重验证通过优化器从实盘收益学习
- mootdx 提供约 600 个交易日（约 2.5 年），无法回测 2019 年以前的策略
- 以次日开盘价成交，无法模拟盘中即时成交

---

**权重自学习**

**学习流程**

实盘推荐 → 自动回填 T+1 收益 → 积累 60 条有 outcome 的记录 → Ridge 回归 → 因子方差审计 → 坍缩检查 → 新权重 → `v1.json`

**优化器（`feedback/optimizer.py`）**

- **算法** — `sklearn.linear_model.Ridge(alpha=1.0)`
- **输入** — 各因子原始得分 → 实际 T+1 收益率
- **输出** — 归一化新权重（负系数截断为 0，正系数归一化到总和 1）
- **触发条件** — 胜率 < 50% 或自上次优化以来新增 ≥ 50 条，同时数据新鲜度检查（hot_theme/dragon_tiger 覆盖率 > 30%）
- **坍缩保护** — 单因子 ≥ 80% 跳过优化
- **列名不匹配** — 新因子在旧记录中无数据，自动填充 0.5
- **三段式工作流** — `check_and_report()` 只产出报告不写入 → 用户审批 → `apply_from_report()` 执行写入
- **版本追溯** — 旧版本保留在 `data/weights/`（带时间戳），可手动对比

**最新回测数据（2026-04-01 ~ 2026-06-27）**

- 总交易次数：95 次
- 胜率：57.9%
- 平均收益 T+1：+1.63%
- 平均收益 T+5：+5.87%
- 最大回撤：-13.93%
- 夏普比率：4.36
- 策略收益：+62.50%
- 沪深 300：+7.56%
- 超额收益：+54.94%

---

**数据源详表**

- **K线 + 财务（Mootdx TCP）** — 优先级首选，永不封 IP，~0.1s/只
- **实时行情（腾讯财经）** — 5205 只，不封 IP，~46s
- **强势股 + 题材归因（10jqka）** — 零鉴权 73ms
- **北向资金汇总（hexin.cn）** — 实时，零鉴权
- **大单资金流（akshare big_deal）** — 全量加载一次后毫秒级查询，独立熔断
- **个股资金流（ASHareHub moneyflow）** — 日配额 100 次，配额用满静默降级，独立熔断
- **技术因子（ASHareHub technical）** — 双源校验（一致加分/分歧保守），独立熔断
- **概念板块（ASHareHub concepts）** — 三源融合（同花顺+东财+ASHareHub），独立熔断
- **财务指标（ASHareHub financial）** — 长线基本面优先源，baostock 回退，独立熔断
- **个股北向持仓（asharehub）** — 需免费 API Key，持股量变化计算增减仓，独立熔断
- **板块归属（东财 push2）** — em_get 限流
- **龙虎榜（东财 datacenter）** — em_get 限流
- **个股资金流（akshare）** — 境外不通，独立熔断不影响其他源
- **限售解禁（akshare）** — 辅助数据

每个数据源有**独立熔断**，互不影响。熔断每 10 分钟自动恢复（`_recover_sources()`），网络抖动不会永久禁用源。被熔断的因子自动中性化为 50 分，权重自动分配给其他活跃因子。

---

**AI 助手集成**

本项目通过 `SKILL.md` 支持与 AI 编程助手对话式交互：

- **Claude Code** — 仓库根目录有 `SKILL.md`，自动识别
- **OpenClaw** — 将 `SKILL.md` 放入 `~/.claude/skills/stock-picker/`
- **Hermes** — 通过技能配置指向 `SKILL.md` 路径

在对话中可以说：

"今天有什么推荐？" → 运行短线策略
"茅台基本面怎么样" → 显示 ROE / EPS / 估值
"跑一下回测" → 执行回测引擎
"对比两次回测" → 查看历史回测记录对比
"检查系统健康" → 运行 verify.py 23 项检查

---

**致谢**

项目参考和借鉴了以下开源项目和社区的思路：

- **a-stock-data（Simon 林）** — 数据源架构模式、em_get 限流设计、同花顺热点/板块归属/龙虎榜 API 参考实现
- **Sequoia-X** — 形态识别策略（金叉/海龟/高旗形等）RPS 排位逻辑
- **daily-stock-analysis** — StockTrendAnalyzer 技术评分体系设计思路
- **mootdx** — 通达信 TCP 行情协议 Python 封装，提供稳定 K 线 + 财务数据
- **akshare** — A 股数据接口标准，提供大单数据和代码列表

---

**许可证**

[MIT](LICENSE)

---

如果这个项目对你有帮助，欢迎 Star ⭐

由 [cbzhang86](https://github.com/cbzhang86) 维护 · 使用 Claude Code 辅助开发
