# 🏛️ A股智能选股系统

[![Python](https://img.shields.io/badge/python-3.10%2B-blue)](https://www.python.org/)
[![License](https://img.shields.io/badge/license-MIT-green)](LICENSE)
[![Compatible with](https://img.shields.io/badge/Claude%20Code-Skill-8A2BE2)](SKILL.md)

**Stock-Picker V4.4（稳定版 v-G）— 全栈多因子量化选股框架**

8 个直连数据源 / 16 网络端点 · 30+ 量化因子 · 短线尾盘 + 长线持股双策略 · 真实 K 线回测引擎 · OOS 验证权重 · 专家 Ensemble 第二意见（覆盖率门控） · 复盘笔记预测-对账闭环

[简体中文](README.md) · [English](README.en.md)

> **状态（2026-09-19）**：代码冻结（稳定版 v-G），进入观察期。438 单元测试 + 门禁 9/0 全绿。

---

## 目录

- [项目简介](#项目简介)
- [功能特性](#功能特性)
- [快速开始](#快速开始)
- [短线策略](#短线策略)
- [回测引擎](#回测引擎)
- [权重体系](#权重体系)
- [系统架构](#系统架构)
- [数据源](#数据源)
- [自动化时间表](#自动化时间表)
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
- **真实 K 线回测** — 零 `np.random`，无前视偏差，open_t1 严格 T+1 口径
- **权重按尾部收益说话**（V4.4）— 不照搬研报中位数，也不以 IC 高低分配权重，而是用「训练期寻优 → 持有期盲测」验证 Top-N 实际收益；方案 G 已通过 2.2 年全市场 OOS 验证与 6 个区间稳健性检验

---

## 功能特性

### 📊 数据能力 — 8 数据源 / 16 网络端点统一接入

> 口径说明：接入实体 8 个（mootdx / 腾讯 / 同花顺 / ASHareHub / 东财 / akshare / baostock / 新浪），
> 网络端点 16 个，纳入健康状态登记 16 个（`_source_status`），纳入独立熔断 8 个（`_source_available`）。
> 早期版本写作"14 数据源"，无代码依据，已更正。

- **mootdx TCP 直连** — K 线 + 财务快照 37 字段，永不封 IP，~0.1s/只
- **腾讯财经 HTTP** — 全市场 5200+ 只实时行情，不封 IP，~46s
- **同花顺 10jqka** — 强势股 + 题材归因，零鉴权 73ms
- **ASHareHub** — 个股北向持仓 / 资金流 / 技术因子 / 概念板块 / 财务指标，日配额 100 次/天（本地安全闸门 90）
- **东财 datacenter-web** — 北向资金日度净流入汇总，零鉴权（估算口径，简报标注 T-1）
- **东财（em_get 限流）** — 板块归属 / 龙虎榜
- **akshare** — 大单资金流 / A 股代码列表
- **独立熔断** — 8 个数据源独立熔断；恢复入口挂在 `get_all_quotes()`，10 分钟自动恢复
- **降级影响登记** — `_SOURCE_FACTOR_IMPACT` 显式声明每个数据源故障时波及的因子与权重合计，简报首屏警示不漏算（有守卫测试锁定）

### 🧠 策略能力

1. **短线尾盘** — 多因子加权评分 + 专家 Ensemble 第二意见 + 市场环境评估 → 初筛 5200+ → 预评分 → Top 200 详评 → 动态门槛 → 仓位分配
2. **长线持股** — ROE + PE + PB 基本面评分，3-6 个月持有周期
3. **策略健康度展示** — kill-switch 从"触发即停推"改为"健康度提示"：近 20 个有结果交易日的累计收益随推荐下发（`rec['strategy_health']`），简报渲染风险提示，是否参与由使用者权衡
4. **影子推荐**（V4.4）— 极端市况（极差市停推 / 拥挤度断路器 / 门槛零达标）当日照常评分并以 `mode='shadow'` 落库，但不下发、不进正式统计——破解"停推日不落库 → 永远攒不出冰点期该不该推证据"的样本删失
5. **市场环境感知** — 涨跌比 / 打板情绪复合分（封板率×0.4 + 连板晋级率×0.3 + 昨日涨停溢价×0.3）/ 中位数涨幅 / 强势股数 / 北向资金 → 综合分
6. **技术评分** — 6 维度系统化评分（趋势 30 + 乖离 20 + 量能 15 + 支撑 10 + MACD 15 + RSI 10）
7. **组合优化** — 评分加权仓位分配，单只上限 40%、保底 10%
8. **缺失数据三语义**（V4.4）— 因子缺失**让渡**（权重分给有数据的因子，不冒充中性 50）、专家覆盖率不足**弃权**、其余如实计入；每处显式声明，与 OOS 的 NaN 剔除口径对齐
9. **专家 Ensemble 第二意见** — 5 维独立评分与主模型差异检测：一致微调上拉、分歧降权、严重冲突（Δ>25）标记并降仓；维度覆盖率 < 40% 时**弃权**而非失明压分（`MIN_EXPERT_COVERAGE=0.40`）
10. **组合层风控三件套** — 同板块最多 2 只 + 持仓两两相关性 ≤0.85 + 波动率保险丝（全市场中位涨幅超 3% 触发降仓）
11. **追高惩罚** — 当日涨幅 >7% 按线性砍分（最高 -50%），依据 A 股短线反转效应
12. **复盘对账闭环**（V4.4）— 每日为 Top-5 写结构化复盘笔记（LLM 只填 thesis / key_factors / prediction / missed_risk），T+1 由程序回填真实涨跌并对照，周命中率 ≤50% 即熔断；月度把 bad 笔记高频标签蒸馏为候选规则（LLM 产生假设，量化验证决定采纳）

### 🔄 自学习反馈闭环

1. SQLite 持久化存储每次推荐及因子分解得分（`(date, code, mode)` 唯一索引防重，批次级）
2. 自动回填 T+1 / T+5 / T+20 真实收益（独立脚本 `backfill_pending.py`，凌晨 3:15 cron）
3. Ridge 回归将因子得分映射到实际收益率（`check_and_report()` 只产报告，**不自动写入**）
4. V4.4 权重校准主路径：`calibrate_weights.py` 按 walk-forward OOS IC 分配（三口径取悲观值、负 IC 归零、单因子上限 0.50）；`--apply` 有**双闸门**——人工审批 + `--accept-ic-objective` 显式确认（IC 目标与尾部收益判据存在已知分歧，必须人工对照），被拒时退出码 2 且不动 `v1.json`
5. 新权重持久化到 `v1.json`（加载优先级 v1.json > config.yml > DEFAULT_WEIGHTS），下次启动自动加载
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

运行过程会看到：A 股代码列表 5200+ 只 → 全市场行情 → 同花顺强势股 → 预过滤 → 初步评分 → Top 200 详评 → 大单缓存加载 → 最终推荐。约 2 分钟后输出每日简报，包含市场概览、题材热度 TOP10、短线评分排名、长线关注。

### 其他命令

```bash
# 查看模型状态和近期表现
python scripts/eod_stock_picker.py --status

# 长线策略
python scripts/eod_stock_picker.py --mode long

# 统一 CLI 入口：一个命令记住所有常用操作
python scripts/pick.py pick          # 尾盘选股
python scripts/pick.py health        # 快速门禁（9 项）
python scripts/pick.py backfill      # T+1/T+5/T+20 结果回填
python scripts/pick.py oos           # 全量 OOS 因子诊断
python scripts/pick.py calibrate     # 权重校准建议（不自动生效）

# 每日统一调度（交易日→选股 / 非交易日→配额预取，内置交易日历分流）
python scripts/daily_job.py

# 回测验证（默认近 3 个月）
python scripts/run_backtest.py --mode short

# 回测版本对比
python scripts/run_backtest.py --list
python scripts/run_backtest.py --compare 1 2

# 回归门禁（任何代码/权重改动后必跑；--full 追加 OOS 面板诊断）
python scripts/evaluate_all.py

# 复盘笔记（预测-对账闭环）
python scripts/postmortem.py stats --days 7      # 命中率；退出码 1 = 熔断
python scripts/postmortem.py candidates --month 2026-09

# 门槛重校（≥15 个交易日实测后使用）
python scripts/recalibrate_thresholds.py --from-run-context

# 健康检查（9 项）
python scripts/verify.py
```---

## 短线策略

### 完整流程

| 步骤 | 操作 | 耗时 |
|------|------|------|
| 1 | `get_all_codes()` — 读取代码缓存 | 0.001s |
| 2 | `get_all_quotes()` — 腾讯 API 拉取全市场行情 | ~46s |
| 3 | `get_ths_hot_stocks()` — 同花顺强势股 + 题材归因 | ~0.22s |
| 4 | `_prefilter()` — 初筛：非 ST / 成交额 > 3000 万 / 非涨跌停 | ~0.5s |
| 5 | 5 维预评分 — 流动性 30 + 活跃度 20 + 动量 15 + 风险 20 + PE 15 → Top 200 | ~0.3s |
| 6 | `get_main_fund()` — 大单资金流全量缓存 | ~25s |
| 7 | `get_kline()` × 200 — mootdx TCP 3 线程并行拉取 K 线 | ~25s |
| 8 | 横截面百分位 — RPS / liq_dev / vol_dev（kline_df 60 日窗口，零配额） | ~0.1s |
| 9 | `scoring_model.score()` — 加权评分 + 防凑数检查 | ~0.2s |
| 10 | `expert_ensemble` — 覆盖率门控（<0.40 弃权） | ~0.1s |
| 11 | `portfolio_optimizer` — 评分加权仓位分配 | ~0.05s |
| 12 | 板块 + 龙虎榜补充（仅 Top 3） | ~8s |
| 13 | 输出简报 + 保存报告 | 15:00 前完成 |

### 因子权重（方案 G）

> **权重唯一事实源 = `data/weights/v1.json`**（ScoringModel 加载优先级 v1.json > config.yml > 代码默认值，三层已同步一致有测试锁定）。
> 调整权重唯一入口 `python scripts/calibrate_weights.py`（`--apply` 需人工审批 + `--accept-ic-objective` 显式确认）。
> **禁止手改数字。**

| 因子 | 权重 | 数据源 | 计算方式 | OOS IC |
|------|------|--------|---------|--------|
| 题材热度 hot_theme | **55%** | 10jqka 三源融合 | 强势股 + 题材标签 | 唯一核心；热点股可买子集 T+1 +1.72% |
| 缩量偏离 liq_dev | **14%** | mootdx K 线 60 日 | `log(amount) − 60日常态` 取反百分位 | +0.0654（ICIR 0.535，与规模正交 −0.088） |
| 反转 reversal_20d | 10% | 全市场涨幅排位 | 20 日收益百分位取反 | +0.0506（t=7.3） |
| 波动收敛偏离 vol_dev | 7% | mootdx K 线 60 日 | `vol20 − 60日常态` 取反百分位 | +0.0263（ICIR 0.225） |
| 低波动 volatility | 6% | mootdx K 线 | 20 日波动率取反 | 低波动异象，与 hot_theme 正交 |
| 动量/RPS | 2% | 全市场涨幅排位 | 20 日收益百分位 → 0-100 | −0.0346 负贡献，保留作噪声分散 |
| 技术形态 | 2% | mootdx K 线 6 维 | 趋势 30 + 乖离 20 + 量能 15 + 支撑 10 + MACD 15 + RSI 10 | −0.0251 负贡献 |
| 量价配合 | 2% | 量比 + 尾盘结构 | 0.8~2.0 = 80 分 | −0.0172 负贡献 |
| 龙虎榜 | 1% | 东财 datacenter | 上榜且机构净买入为正 | −0.0019 噪声 |
| 主力资金流 | 1% | 大单 / ASHareHub / 同花顺 | 横截面百分位排名 | +0.0057 弱正（历史 coverage 仅 0.19%） |
| 北向资金 | 0% | — | 2024-08 政策停公开，恒中性 50 | — |
| size / liquidity | 0% | 估值快照 / — | size 待快照 ≥60 交易日验证；liquidity 已证伪（84% 是小市值效应） | — |
| 估值/事件（待验证） | 0% | 通达信 / 公告 | 链路已通，快照积累中 | 待验证 |
| 风险 | 过滤层 | risk_filter.py | 前置拦截，不占权重（硬拦截直接 0 分） | — |

**方案 G 的实证依据**（2024-01 ~ 2026-09，299.9 万行 / 647 交易日 / 5225 只，剔除接近涨停，扣全部成本）：

- Top5 日均超额 **+1.845%（t=21.4）**，对比旧基线（hot/rev 等权）+0.850%（t=9.4）
- 6 个子区间全部成立；训练/持有切分寻优确认 G 已在持有期前沿（500 组随机搜索无一显著超越）
- **关键结论：IC 高 ≠ 赚钱多**。等权 4 腿方案 IC 最高（0.0743）但 Top5 超额仅 +0.13%/日——权重必须以尾部收益为准，不能按 IC 分配

### 动态门槛

强市 **65** / 中性 **70** / 弱市 **75**（`config.yml` 基准 75，`dynamic_min_score` 可覆盖）。

⚠️ 换 G 权重后分数分布**是分化不是平移**（热点簇更集中、非热点体下移），弱市 75 门槛的"有推荐天数"由 92.5% 升至 97.5%（+5.0pp），强/中性市几乎不变。**门槛数值未改**——无实测数据不改门槛；重校等 ≥15 个交易日 `run_context` 实测后跑 `recalibrate_thresholds.py --from-run-context`。

---

## 回测引擎

### 核心特性

1. **数据源** — mootdx 真实 K 线，零 `np.random`，无前视偏差
2. **成交口径** — **open_t1（唯一有效口径）**：决策日 T 推荐 → 下一交易日开盘买入，严格 T+1 制度；close_t0（当日尾盘买）分支已于 2026-09-17 移除
3. **成本模型** — 佣金万三 + 分级滑点 + 印花税 0.05%（仅卖出）+ 过户费（早期版本漏计印花税/过户费，单笔双边低估约 0.051%，已修复）
4. **交易规则** — 止盈 +2%、止损 -2%（从 `config.yml sell` 段读取）、T+3 时间止损
5. **仓位模拟** — 按 `allocation_pct` 分配资金，逐日 T+1 开盘买 / 收盘卖
6. **基准** — 沪深 300 + 中证 1000 多基准对标

### 输出指标

总交易次数 / 胜率 / 平均收益 T+1/T+5 / 最大单笔盈亏 / 最大回撤 / 夏普比率 / 策略总收益 / 超额收益 / 权益曲线 / 月度收益表 / 因子 IC 信息系数 / 赢输分差 / 交易明细 / 优化建议

### 历史回测参照（2026-04-01 ~ 2026-06-27，min_score=60 旧配置）

> ⚠️ 口径警示：以下成绩产生于 `min_score=60` + 旧权重体系。当前为动态门槛（65/70/75）+ 方案 G 权重，**同区间重跑结果完全不同**——历史高收益不可复现，是"挤水分"（momentum 降权 + 涨停代理过滤 + 回测口径 hot_theme 中性化）而非代码退化。引用时必须带上 min_score 与权重版本。

| 指标 | 数值 |
|------|------|
| 总交易次数 | 95 次（区间 260 个交易日，日均 ~1.5 笔） |
| 胜率 | 57.9% |
| 平均收益 T+1 | +1.63% |
| 平均收益 T+5 | +5.87% |
| 最大回撤 | -13.93% |
| 夏普比率 | 4.36 |
| 策略收益 | +62.50% |
| 沪深 300 | +7.56% |
| 超额收益 | +54.94% |

**关于回测口径的重要限制**：修复前（2026-09-05 前）回测中 `capital_flow` 被强制中性化；2026-09-05 已修复为沿用 `_prefilter` 从 `factor_daily.db` 快照透传的历史值，缺失才置 `None`。但 `factor_daily.db` 历史覆盖率仅 ~10%，多数票仍无历史资金流可用——上述成绩实际检验的是 K 线三因子（动量 + 技术 + 量价），与实盘多因子模型不是同一套打分，两者不可直接比较。

### 已知限制

- 资金流 / 题材 / 龙虎榜 / 北向数据在回测中不可用 —— 当日大单不可回溯
- mootdx 提供约 600 个交易日（~2.5 年），无法回测更早区间
- 全区间回测（260 交易日）为 CPU 密集，需 6+ 小时；验证改动请用短窗口对比 `--list` 历史基准---

## 权重体系

### 判据与验证流程

```
假设 → OOS 面板验证（2.2 年 × 5225 只）→ 训练期寻优 → 持有期盲测 → 人工审批 → 三层同步写入
```

**两条铁律**：

1. **任何新因子/新权重必须先过 OOS 才能上岗**——未验证的因子一律零权重先采集
2. **判据是 Top-N 尾部收益，不是 IC**——IC 最高 ≠ 最赚钱（实证：等权 4 腿 IC 0.0743，Top5 超额仅 +0.13%/日）

### 权重三层（硬契约，有测试锁定）

| 层 | 文件 | 说明 |
|---|---|---|
| 生效层 | `data/weights/v1.json` | ScoringModel 实际加载，唯一生效来源 |
| 兜底层 | `config.yml` `short_term.weights` | 已同步为 G 值，含完整论证注释 |
| 兜底层 | `core/scoring_model.py` `DEFAULT_WEIGHTS` | 代码内置默认值 |

三层必须一致；任一层被单独改动会触发告警（语义比较，只在真不一致时打印）。改动唯一入口是 `calibrate_weights.py`。

### 优化器（`feedback/optimizer.py`）

- **算法**：`sklearn.linear_model.Ridge(alpha=1.0)`
- **输入**：因子原始得分 → 实际 T+1 收益率
- **输出**：归一化新权重（负系数截断为 0）
- **触发条件**：胜率 < 50% 或自上次优化以来新增 ≥ 50 条，且数据新鲜度检查通过
- **坍缩保护**：单因子 ≥ 80% 跳过优化
- **三段式工作流**：`check_and_report()` 只报告不写入 → 用户审批 → `apply_from_report()` 执行写入
- **当前状态**：`apply_from_report()` 无调用方（刻意停用），`data/reports/` 下累积的建议报告不落地是预期行为

### 已证伪的因子（避免重复踩坑）

| 候选 | 结论 | 证据 |
|---|---|---|
| `liquidity`（−log amount） | 不启用 | 与规模代理相关 **0.838**，84% 是小市值效应；ICIR 0.418 低于正交化后的 liq_dev（0.535） |
| `amihud20`（非流动性） | 不启用 | 与规模代理相关 **0.900**，只是规模的另一个化身 |
| 波动率**水平**成分 | 让位于偏离成分 | vol_dev ICIR 0.225 > vol_level 0.126 |
| 等权 4 腿（IC 最优方案） | 不采纳 | IC 最高但几乎不赚钱（+0.13%/日，t=2.4） |
| 坐标精修的"训练期最优" | 不采纳 | 训练期 t 更高（18.0）、持有期更差（+1.754%）——教科书式过拟合特征 |

---

## 系统架构

```
stock-picker/
│
├── core/                          # 核心引擎
│   ├── data_engine.py             # 多源数据融合（8 数据源/16 端点、缓存、熔断、降级影响登记）
│   ├── factor_library.py          # 30+ 因子 0-100 评分映射（含 liq_dev / vol_dev 偏离因子）
│   ├── scoring_model.py           # 加权评分 + 权重加载 + 缺失让渡 + 追高惩罚
│   ├── technical_scorer.py        # 6 维度 100 分制技术分析
│   ├── risk_filter.py             # 风险前置过滤
│   ├── backtest_engine.py         # 回测引擎（open_t1 口径、完整成本、多基准）
│   ├── backtest_store.py          # 回测结果 SQLite 持久化
│   ├── portfolio_optimizer.py     # 仓位分配
│   ├── expert_ensemble.py         # 5 维专家第二意见（覆盖率门控）
│   ├── oos_validator.py           # walk-forward 样本外 IC 验证
│   ├── trading_calendar.py        # 集中式交易日历
│   ├── drift_monitor.py           # 绩效漂移监控 PSI
│   ├── data_quality_monitor.py    # 数据质量巡检
│   ├── fundamental_provider.py    # 估值/基本面（点时化防前视）
│   ├── event_provider.py          # 公告/事件催化
│   └── factor_standardizer.py     # 横截面标准化（实验开关）
│
├── strategies/                    # 策略层
│   ├── short_term.py              # 短线尾盘策略（多因子 + 市场评估 + ensemble + 组合风控 + 影子模式）
│   ├── long_term.py               # 长线策略（基本面）
│   └── base.py                    # 策略抽象基类
│
├── reports/                       # 报告层
│   ├── market_briefing.py         # 每日市场简报（含降级警示块）
│   ├── backtest_report.py         # 回测报告渲染
│   └── daily_report.py            # Markdown 报告文件存储
│
├── feedback/                      # 反馈循环
│   ├── tracker.py                 # SQLite 预测追踪（批次级防重）
│   ├── optimizer.py               # Ridge 回归权重优化器
│   └── data_collector.py          # 因子仓库（逐日快照）
│
├── scripts/                       # 用户入口
│   ├── eod_stock_picker.py        # 主入口
│   ├── run_backtest.py            # 回测入口
│   ├── verify.py                  # 9 项健康检查
│   ├── pick.py                    # 统一 CLI 入口
│   ├── daily_job.py               # 每日统一调度
│   ├── evaluate_all.py            # 回归门禁（9 项）
│   ├── calibrate_weights.py       # OOS 权重校准（双闸门）
│   ├── postmortem.py              # 复盘笔记（预测-对账闭环）
│   ├── recalibrate_thresholds.py  # 门槛重校（实测/OOS 代理双模式）
│   ├── backfill_pending.py        # T+1/T+5/T+20 回填
│   ├── snapshot_valuation_daily.py# 估值快照
│   ├── backup_predictions.py      # 数据库每日备份
│   ├── calibrate_slippage.py      # 尾盘滑点校准
│   ├── capacity_check.py          # 资金容量测算
│   ├── multiple_testing.py        # DSR/PBO 多重检验审计
│   ├── prefetch_tdx.py            # 通达信估值/事件预取
│   └── prefetch_asharehub.py      # 非交易日配额预取
│
├── tests/                         # 438 个单元测试（含硬契约锁 + 守卫测试）
├── config.yml                     # 中心配置
├── SKILL.md                       # AI 助手技能文件
├── CHEATSHEET.md                  # 使用备忘录
├── requirements.txt               # Python 依赖
│
└── data/                          # 运行时数据（自动创建）
    ├── cache/                     # K 线 / 代码 / 回测 / 因子缓存 + 复盘笔记库
    ├── db/                        # predictions.db
    ├── reports/                   # 每日报告 + 简报 + run_context
    └── weights/                   # v1.json 生效权重 + 历史版本
```

---

## 数据源

| 数据源 | 用途 | 协议 | 特性 |
|--------|------|------|------|
| mootdx TCP | K 线 + 财务 37 字段 | TCP 7709 | 永不封 IP，~0.1s/只 |
| 腾讯财经 HTTP | 全市场实时行情 | HTTP | 5200+ 只，不封 IP，~46s |
| 同花顺 10jqka | 强势股 + 题材归因 | HTTP | 零鉴权，73ms |
| 东财 datacenter-web | 北向资金汇总 | HTTP | 日度净流入（估算口径，T-1），零鉴权 |
| ASHareHub | 北向持仓/资金流/技术/概念/财务 | HTTP | 4 端共享 100 次/天（本地闸门 90） |
| 东财 em_get | 板块归属 / 龙虎榜 | HTTP | 串行限流，WAF 保护 |
| akshare | 大单资金流 / 代码列表 | HTTP | 独立熔断 |

纳入 `_source_available` 的 8 个源有**独立熔断**，互不影响。熔断每 10 分钟自动恢复（`_recover_sources()`），恢复入口在 `get_all_quotes()`。

**新增数据源/新因子的三处同步登记（硬要求）**：

1. `_source_available`（熔断）与 `_source_status`（健康报告）**键名必须一致** —— `_update_source_status()` 内部有 `if source_key in self._source_status` 判断，未登记的键会被**静默丢弃**：熔断已生效但报告仍显示正常，故障不可见
2. `_SOURCE_FACTOR_IMPACT`（降级影响表）—— 漏登记会导致简报首屏的"受影响因子权重合计"严重低估（K 线源故障实际波及 liq_dev / vol_dev / volatility / liquidity / reversal_20d）
3. OOS 注册（`oos_validator.K_FACTORS`）—— 否则新因子无法参与样本外校验

以上三点均有**守卫测试**锁定：新增因子漏登记任何一处，测试直接失败。

---

## 自动化时间表

| 时间 | 任务 | 内容 |
|------|------|------|
| 交易日 14:45 | 尾盘选股 | 选股 + 简报推送（推送通道属宿主环境，脚本不内嵌） |
| 每天 3:15 | T+1 结果回填 | 独立脚本 `backfill_pending.py`，凌晨数据完整、与策略无时序依赖 |
| 交易日 19:35 | 复盘笔记 | `postmortem.py` add/backfill；周五额外输出周命中率 |
| 周五 16:00 | 周度回测 | `stock-picker-weekly-backtest` |
| 每月 1 日 | 月度复检 | IC 趋势检测 + 降权提案（有闸门，不静默生效） |
| 周六/周日 10:00 | 配额预取 | 非交易日 ASHareHub 配额预取（`daily_job.py` 自动分流） |

---

## 观察期安排（2026-09-19 代码冻结后）

代码：**冻结**。所有已知问题已修复（6 项 P1/P2 + 3 项 P2），无待办改动。进入观察期只观察不开发：

1. **看简报** —— 下个交易日 14:45 起为新版权重 + 新因子语义首次实盘，确认推送正常、推荐合理
2. **攒数据** —— 复盘命中率（周五首报）+ `run_context` 门槛分布，15 个交易日后跑 `recalibrate_thresholds.py --from-run-context`
3. **两个已量化效应**（特性非 bug）—— 弱市"有推荐"天数比以前多约 5 个百分点；热点股买卖集中在"当日已涨 5-9% 但未涨停"的票上

**什么时候才需要再动代码**：① 门槛重校给出明确建议；② 复盘命中率连续熔断；③ 某个数据源长期失效触发降级警示；④ 想扩充新的因子方向（流程已固化：先 OOS 验证 → 再进权重 → 联动点有守卫测试兜底）

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
> "复盘命中率多少？" → 运行 postmortem.py stats

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