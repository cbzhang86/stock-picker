---
name: stock-picker
description: 全栈多因子量化选股系统 — 8数据源/16网络端点/30+因子/短线尾盘+长线持股/历史快照回测/OOS验证/方案G权重/专家Ensemble第二意见(覆盖率门控)/复盘对账闭环/回归门禁
origin: custom
version: 4.4
---

# A股智能选股系统 V4.4（稳定版 v-G · 代码冻结观察期）

8 数据源 / 16 网络端点 · 30+ 量化因子 · 短线/长线双策略 · 回测引擎（历史快照） · OOS 验证 · 方案G 权重（hot_theme 0.55 主导 + 缩量/波动偏离新因子） · 专家 Ensemble 第二意见（覆盖率门控） · 数据源级独立熔断 · 复盘笔记预测-对账闭环

兼容 Claude Code · OpenClaw · Codex · Hermes

**状态（2026-09-19）**：代码冻结（稳定版 v-G），进入观察期只观察不开发。438 单元测试全过 + 门禁 9/0 + 六维验证（含零让渡无副作用证明）。下个交易日 14:45 起为新版权重首次实盘。

---

**项目路径**

- 工作目录：`C:\Users\Administrator\Documents\stock-picker-v2`（生产目录；`Documents\stock-picker` v1 已冻结为回滚基线，两目录绑同一 GitHub remote，git 操作前先确认所在目录）
- Python 执行路径：`C:\Users\Administrator\AppData\Local\Programs\Python\Python311\python.exe`
  （注意：`Python310` 目录在本机不存在，早期文档写法有误）
- 配置文件：`config.yml`（权重段已同步为 G 值，含完整论证注释）
- AI 技能文件：`SKILL.md`（本文件）；使用备忘录：`CHEATSHEET.md`
- 新手导览：`docs/项目全景导览_20260919.md`（从零讲起，含小词典）
- 深度材料：权重论证 `docs/因子扩容与权重论证_20260918.md`、深查报告 `docs/全项目深查报告_20260919.md`、自动化运维 `docs/自动化任务_尾盘选股推送.md`

**快速命令表**

```bash
# 短线策略（交易日 14:45 cron 自动跑；端到端 ~113s）
python scripts/eod_stock_picker.py --mode short

# 统一 CLI 入口（pick/backfill/health/oos/backtest/prefetch/calibrate/slippage）
python scripts/pick.py pick        # 尾盘选股
python scripts/pick.py health      # 快速门禁（9 项）
python scripts/pick.py oos         # 全量 OOS 因子诊断
python scripts/pick.py calibrate   # 权重校准建议（不自动生效）

# 每日统一调度（交易日→选股 / 非交易日→配额预取）
python scripts/daily_job.py

# 回归门禁（任何代码/权重改动后必跑）
python scripts/evaluate_all.py

# 复盘笔记（2026-09-19 新增：预测-对账闭环，19:35 自动化调用）
python scripts/postmortem.py add --date D --code C --rank N --thesis T --key-factors F --prediction up|down|flat
python scripts/postmortem.py backfill        # T+1 回填 realized_outcome（程序写入，禁止手工指定）
python scripts/postmortem.py stats --days 7  # 命中率统计；≤50% 熔断，退出码 1

# 门槛重校（2026-09-19 新增：run_context 实测 / OOS 代理双模式，样本不足返回码 1 不产伪结论）
python scripts/recalibrate_thresholds.py --from-run-context
python scripts/recalibrate_thresholds.py --oos-proxy

# 查看状态和近期表现
python scripts/eod_stock_picker.py --status

# 长线策略 / 回测（默认区间 2026-04-01 ~ 2026-06-27）
python scripts/eod_stock_picker.py --mode long
python scripts/run_backtest.py --mode short --start 2026-04-01 --end 2026-06-27
python scripts/run_backtest.py --list

# 健康检查 9 项
python scripts/verify.py
```

---

**飞书格式铁律（每条消息前自检）**

1. **禁止 `|` 符号** — 飞书表格降级成纯文本，不用表格线
2. **禁止 `###` `####` 标题** — 用 `**粗体**` 替代
3. **禁止报告体开头** — 不要 "以下是..." / "下面展示..." / "为您生成..."，直接输出内容
4. **每段 emoji 不超过 2 个** — 保持克制
5. **段落用 `---` 分隔** — 保持阅读节奏
6. **列表用数字 / 无序列表** — 不用表格
7. **代码块用 ` ``` ` 包裹**

（本 SKILL.md / README 是 GitHub 文档，标准 Markdown 表格正常用——两个场景不要混，见陷阱 31）

---

**因子架构（方案G — 2026-09-19 生效，唯一来源 data/weights/v1.json）**

⚠️ 权重治理铁律：生效权重唯一来源是 `data/weights/v1.json`（ScoringModel 加载优先级
v1.json > config.yml > DEFAULT_WEIGHTS，三层已同步一致，有测试锁定）。config.yml 权重段
已同步为 G 值。权重改动唯一入口 `scripts/calibrate_weights.py`——按 OOS IC 校准，
`--apply` 有双闸门：人工审批 + `--accept-ic-objective` 显式确认（IC 目标与 2026-09-19
尾部收益判据存在已知分歧，--apply 前必须人工对照）；被拒时退出码 2 且不动 v1.json。
**禁止手改数字。**

**权重判据（本项目最重要的经验）**：不是 IC 有多高，而是"选出来的那几只赚不赚钱"
（Top-N 尾部收益）。实证过：等权 4 腿方案 IC 最高（0.0743）却几乎不赚钱（+0.13%/日），
IC 低的方案反而排第二。方案 G 用"训练期寻优 → 持有期盲测"确认（500 组随机搜索无一
显著超越），不是拍脑袋。

- **hot_theme 题材热度 55%** — 同花顺强势股 + 题材归因三源融合（同花顺 + 东财 + ASHareHub concepts）。热点股（剔掉买不进的）T+1 平均 +1.72%（全市场 −0.23%），稀疏二值信号尾部置信度高。旧配比（hot/rev 0.42/0.42 等权）把非热点极端反转股放进 Top5 拖累收益 → 提到 0.55 主导。
- **liq_dev 缩量偏离 14%**（2026-09-19 新增）— 当日成交额相对自身 60 日常态的偏离取反（`log(amount) − median(log amount, 60)`，横截面百分位，kline_df 60 日窗口零 AShareHub 配额）。IC +0.0654 / ICIR 0.535 / t +12.81，与规模代理相关仅 −0.088（正交）。**注意：原始 liquidity（−log amount）已证伪**——与规模相关 0.838，84% 是小市值效应，故 liquidity 保持 0 不启用，起用 liq_dev 替代。
- **reversal_20d 反转 10%** — 近 20 日跌幅百分位映射（跌多了易反弹，A 股 T+1 经典效应）。OOS +0.0506（t=7.3）。与 liq_dev 相关 0.530（缩量与下跌同步）→ 只给 0.10 防重复计分。
- **vol_dev 波动收敛偏离 7%**（2026-09-19 新增）— 20 日波动率相对自身常态的偏离取反。IC +0.0263 / ICIR 0.225 / t +5.37。偏离成分优于水平成分（vol_dev ICIR 0.225 > vol_level 0.126）。
- **volatility 低波动 6%** — 20 日波动率取反。低波动异象，与 hot_theme 正交。
- **momentum / technical / volume_price 各 2%** — 实证负贡献或不显著（OOS IC −0.0346 / −0.0251 / −0.0172），保留小权重 purely 作噪声分散（用户决策"不归零"，实证代价仅 0.031 pp/日）。
- **dragon_tiger / capital_flow 各 1%** — 探索位。capital_flow OOS IC +0.0057 弱正但历史 coverage 仅 0.19%（ASHareHub 配额）。
- **north_flow 0%** — 2024-08 政策停公开，因子恒返回 50 中性值，不占权重（东财估算口径，简报标注 T-1）。
- **size / liquidity 0%** — size 随估值快照积累（2026-09-18 起，≥60 交易日后 OOS 验证再定）；liquidity 已证伪不启用。
- **零权重采集因子** — valuation_fundamental / event_catalyst（链路已通，快照积累中）。
- **risk（过滤层，不占权重）** — risk_filter.py 前置拦截 ST / 接近涨停（代理规则 pct_chg ≥ 板块上限×98%）/ 跌停 / 流动性不足。硬拦截直接 0 分（v1 是 ×0.5 折减）；剩余软风险走 scoring_model penalty 路径。

**缺失数据语义（2026-09-19 深查后契约化）**：缺失数据绝不冒充中性值参与打分。
因子缺失 → **让渡**（权重分给有数据的因子，`data_available=False` / `effective_weight=0`，
中性让渡白名单已扩至全部百分位驱动因子：liq_dev / vol_dev / volatility / liquidity / size，
与 OOS 验证口径 NaN 剔除对齐）；专家维度覆盖率 < 40% → **弃权**（`low_coverage`，
ensemble=model，不构成第二意见、不做任何调整）。

**V4.4 关键行为参数**

- 动态门槛：强市 65 / 中性 70 / 弱市 75（config.yml 基准 75，`dynamic_min_score` 覆盖）。**G 权重下分数分布是分化不是平移**（热点簇更集中、非热点体下移）：OOS 代理显示弱市 75 的"有推荐天数"由 92.5%→97.5%（+5.0pp），强/中性市几乎不变。门槛数值未改——无实测数据不改门槛，重校等 ≥15 个交易日 run_context 实测（`recalibrate_thresholds.py --from-run-context`）
- 影子推荐（2026-09-18）：极端市况（极差市停推 / 拥挤度断路器 / 动态门槛零达标）当日**照常评分并以 mode='shadow' 落库但不下发、不进正式统计**——破解"停推日不落库 → 永远攒不出冰点期该不该推证据"的样本删失。开关 `short_term.shadow_enabled`（默认 true，置 false 恢复旧行为）
- 追高惩罚：当日涨幅 >7% 线性砍分，≥9.5% 最多砍 50%
- 动量硬过滤：非热点票 rps_20 ≥80 剔除；同板块 ≤2 只（max_per_board）；两两 20 日收益相关 >0.85 拦截；波动率保险丝全组合 ×0.8；连亏降仓 ×0.5
- kill-switch 已软化：只随推荐下发 strategy_health 提示，**不再停推**
- 专家 Ensemble：覆盖率门控 `MIN_EXPERT_COVERAGE=0.40`（数据不足弃权，修复前会失明压分）；冲突 Δ>25 → ensemble 分 −8 + 仓位 ×0.5
- 回测成交口径：**唯一有效口径 open_t1**（决策日 T 推荐 → 下一交易日开盘买入，严格 T+1）；close_t0（当日尾盘买）分支已于 2026-09-17 移除。成本模型：佣金万三 + 分级滑点 + 印花税 0.05%（仅卖出）+ 过户费
- 北向资金：东财估算口径，简报标注 `[上一交易日 T-1]`

---

**数据源熔断与配额**

- ASHareHub 4 端共享日配额 100 次（本地安全闸门 90，预留 10 次），用满静默降级 + 简报首屏警示，熔断 10 分钟自动恢复（`_recover_sources()`）
- 独立熔断源：`_source_available` 8 个（big_deal / ths_fund_flow / north_flow / lockup / asharehub_moneyflow / asharehub_tech_factors / asharehub_concepts / asharehub_financial）；健康报告 `_source_status` 16 键全登记
- **新增数据源/新因子必须同步登记**：两个字典键名一致（漏登记 = 熔断生效但报告全绿的静默缺陷，2026-09-03/09-19 两轮修复）；`_SOURCE_FACTOR_IMPACT` 降级影响表（K 线源故障波及 liq_dev / vol_dev / volatility / liquidity / reversal_20d，漏登记 = 警示严重低估）——两者均有守卫测试锁定**V4.4 新增架构（2026-09-18 ~ 09-19）**

- `core/factor_library.py` / `core/scoring_model.py` — liq_dev / vol_dev 偏离因子 + rank_stocks 百分位计算（kline_df 60 日窗口，零 AShareHub 配额）
- `scripts/postmortem.py` — 复盘笔记系统：结构化落库（LLM 只填 thesis / missed_risk / key_factors / prediction 四字段）+ 程序回填 realized_outcome/verdict 防自评自嗨 + 周命中率 ≤50% 熔断 + 月度 bad 笔记蒸馏候选规则（LLM 产生假设，量化验证决定采纳）；笔记库 `data/cache/postmortem_notes.db`
- `scripts/recalibrate_thresholds.py` — 门槛重校双模式（run_context 实测 / OOS 代理），输出为指示性，最终以实测为准；样本不足返回码 1 不产出伪结论
- `scripts/snapshot_valuation_daily.py` — 估值快照（PE/PB/市值，2026-09-18 起每日积累）
- `scripts/backup_predictions.py` — predictions.db 每日备份（实盘唯一真源此前无任何备份）
- 守卫测试 `tests/test_audit_fixes_20260919.py`（16 例）：百分位驱动因子让渡白名单守卫 / 数据源依赖表守卫 / 退出码守卫

**V4.3 架构（保留生效）**：expert_ensemble（覆盖率门控见上）/ oos_validator（walk-forward 三口径取悲观值 + daily_ics）/ trading_calendar / drift_monitor（PSI>0.25 告警）/ data_quality_monitor（五类异常接入简报）/ fundamental_provider + event_provider（点时化防前视，权重 0 待验证）/ factor_standardizer（实验开关默认关）/ pick.py / daily_job.py / evaluate_all.py / calibrate_weights.py / calibrate_slippage.py / capacity_check.py / multiple_testing.py / prefetch_tdx.py

**V4.3 行为变更（有数据支撑）**

- kill-switch 从"硬熔断停推"改为"健康度展示+谨慎提示"（strategy_health 随推荐下发）
- 市场评估 52.5→45.0：打板情绪新口径揭示旧涨停跌停比高估情绪
- 追高惩罚（>7% 线性砍分）/ 相关性约束 / 波动率保险丝三层新风控（极端场景保险丝，正常日零影响）
- 性能：端到端选股 226s → 113s（sqlite 线程级复用、行情二级缓存、批量翻倍、节流降档）

**审计框架**

**业务 4 层**

1. **数据层** — 8 源统一接入（16 网络端点）→ 独立熔断 → SQLite WAL 缓存 → 失效降级
   - 检查点：mootdx TCP 连接是否复用、em_get 是否串行、ASHareHub 配额是否监控、缓存是否命中
2. **策略层** — 初筛 → 预评分 → 详评 → 评分 → 门槛 → 仓位分配
   - 检查点：市场评估是否跳过、影子模式三态、预过滤条件是否合理、防凑数是否生效、极差市是否输出简报
3. **回测层** — 历史快照 → 逐日回放（open_t1 唯一口径）→ 真实 K 线收益 → 仓位模拟 → 因子 IC
   - 检查点：是否用真实 K 线而非 np.random、是否用历史当日行情而非今日数据、sell_config 是否从 config 读取
4. **反馈层** — 推荐入库（批次级防重）→ 回填收益 → Ridge 优化 → 审批写入
   - 检查点：三段式是否合规（只报告不自动写）、坍缩保护是否触发、防重是否按"批"粒度（按条检查会误拦同批第 2/3 只）

**工程 3 维**

1. **代码规范** — except 必须 log / SQLite 必须 try/finally / import 必须文件顶部 / 禁止方法体内 import
2. **配置规范** — config.yml 权重段必须 `short_term.weights` / API Key 必须环境变量 / sell 参数必须从 config 读取而非硬编码
3. **文档规范** — 飞书格式铁律 / 文档与代码同步更新 / 陷阱编号可追溯

**2026-09-19 深查教训（审计流程自身的系统性缺口，五条薄弱环节）**

1. **效果验收缺失** — 意见类组件（专家/ensemble）只验收了"接线层"（字段接入、不 crash），从无"信息增量层"（第二意见有没有区分度）的验收；对照因子权重有 OOS IC 验收，意见类没有。**改进：意见类组件上线必须绑定可回测的区分度指标（如 confidence 分档 vs 次日收益单调性），纳入月度复检。**
2. **测试固化缺陷** — `test_expert_neutral_baseline` 把"全维无数据 → expert_score=50"断言为正确行为，固化了一个存续 14 天的缺陷。**改进：对"缺失数据"类用例，断言必须显式写明期望语义（让渡/弃权/如实计入三选一）。**
3. **日志健康度只有存在性检查** — `ensemble 完成: 0 高一致 / 2 中度 / 1 冲突` 的退化模式每天出现却无告警。**改进：周检加恒定值检测、分布异常（conflict/弃权占比超阈）规则。**
4. **跨模块数据流审查缺位** — 改了因子体系没查消费侧登记点（P1-2 的 `_SOURCE_FACTOR_IMPACT` 漏登记就是现场重演）。
5. **"缺失→中性 50"语义从未清单化** — 全库 20+ 处，哪些该让渡/弃权/如实计入没有决策表，专家缺陷与本轮 A1 缺陷同源于此。

**质量保障体系**：438 单元测试（含 09-16 七条硬契约锁、09-19 十六个守卫测试）+ 门禁 9 道 + 硬契约 7 条 + OOS 验证器（2.2 年 × 5225 只）+ 复盘对账（每日，命中率≤50% 熔断）

---

**代码改动纪律（三步走）**

1. **审** — 先读目标文件全文，理解现有逻辑和数据流，搜索相关引用
2. **改** — 最小改动原则：改函数不改架构，除非有明确重构需求
3. **验** — 改后跑 `evaluate_all.py` 门禁 + 全量单测（`python -m unittest discover -s tests`）

**禁止的改动模式**

- 不要手改 `data/weights/v1.json`——唯一入口 `calibrate_weights.py`（双闸门）
- 不要修改 `predictions.db` 的 SQLite 结构——那是校准/对账的数据来源
- 不要给东财开多线程/协程并发——`em_get` 已经是串行的
- 不要在策略层硬编码止盈止损值——必须从 `config.yml sell` 段读取
- 不要在回测里用 `np.random`——必须用真实 K 线
- 不要在方法体内写 `import`——统一放文件顶部
- **新增数据源 → 两个字典 + `_SOURCE_FACTOR_IMPACT` 三处同步登记**（`_source_available` 熔断与 `_source_status` 健康报告键名必须一致——`_update_source_status()` 内部 `if source_key in self._source_status` 判断会静默丢弃未登记键：熔断已生效但报告仍显示正常，故障不可见。2026-09-03 已补齐 3 个漏登记键 ths_fund_flow / big_deal / lockup 并统一北向键名 akshare_north_flow）
- **新增因子 → 四处同步**：让渡白名单 + OOS 注册（`oos_validator.K_FACTORS`）+ 权重三层 + 数据源影响表，漏一处守卫测试失败
- 门槛数值：无 ≥15 交易日实测数据不改（`recalibrate_thresholds.py` 产出是指示性的）

---

**所有常见陷阱（编号 1-31）**

1. **json.loads 不能 ast.literal_eval** — JSON 的 `true`/`false` 不是 Python 字面量。`optimizer._load_history()` 已修复。
2. **Optimizer 列缺失填充 0.5** — Ridge 回归时新因子列在旧记录中不存在，自动填 0.5。
3. **权重坍缩保护** — 单因子 ≥ 80% 跳过优化，防止单一因子主导。
4. **不要多线程并发东财** — `em_get` 已经是串行的。
5. **不要手动改 predictions.db** — SQLite 结构固定。
6. **回测不要用 np.random** — 必须用真实 K 线。
7. **不要同时跑多个策略实例** — mootdx TCP 和 SQLite 缓存有状态。
8. **不要直接调 akshare 东财接口** — 直连会被 WAF 拦，走大单缓存 / em_get 限流。
9. **Config 权重字段名** — `short_term.weights`，不是 `short_term.weights_model`。
10. **Baostock 复权参数** — 回测用 `adjustflag='1'`（后复权），不是 `'2'`（前复权）。
11. **北向回测语义** — 回测中北向因子恒定为 50（中性值），因北向数据不可回溯。
12. **两状态同步** — 策略退出前调用 `self._save_state()` 保存运行状态。
13. **CLI 默认日期** — `run_backtest.py --start` 默认 `2026-04-01`，`--end` 默认 `2026-06-27`。
14. **push2 直连已删除** — `_get_capital_flow_push2()` 方法已移除。`push2.eastmoney.com` 仅存在于板块归属 URL 中（走 em_get 限流），不是数据源。
15. **ModelRegistry 已删除** — `core/model_registry.py` 整文件移除（144 行死代码），版本管理通过带时间戳的权重文件实现。
16. **Optimizer 三段式工作流** — `check_and_report()` 只产出报告不写入 → 审批 → `apply_from_report()` 写入。`maybe_optimize()` 保留原签名但降级为只报告；`data/reports/` 下累积的建议报告不落地是预期行为。
17. **Optimizer 缓存目录** — `.last_optimize_short` 计数文件在 `data/cache/`，不在 `data/weights/`。
18. **Tracker UNIQUE 约束** — `predictions(date, code, mode)` 有 UNIQUE 索引，重复插入会抛异常（批次级防重 `has_predictions(date, mode)`）。
19. **factor_scores JSON 序列化** — `json.dumps(factor_scores, default=str)` 处理 numpy 类型。
20. **backtest_engine SQLite** — `_load_factor_data()` 连接已补 try/finally（2026-09-06 修复，此前为已知遗留）。
21. **ScoringModel 权重加载顺序** — v1.json > config 传入 > DEFAULT_WEIGHTS。三层已全部对齐（2026-09-14 起 config.yml 同步 v1.json 值，告警改为语义比较——只在真不一致时打印）。改权重走 `calibrate_weights.py`。
22. **止盈止损从 config 读取** — `sell_config` 参数传入 ScoringModel，`short_term.sell.take_profit` / `stop_loss`，不再硬编码。
23. **IC 不是权重的判据** — 等权 4 腿 IC 最高（0.0743）却几乎不赚钱；hot_theme 是稀疏二值信号（尾部置信度高），连续因子极端尾部充满噪声。权重必须以 Top-N 尾部收益为准（2026-09-18 实证）。
24. **liquidity ≠ liq_dev** — 原始 liquidity（−log amount）84% 是小市值效应（与规模代理相关 0.838），已证伪保持 0；起用与其正交（−0.088）的 liq_dev。Amihud 同理（与规模相关 0.900）不启用。
25. **NaN 是 float** — `isinstance(v, float)` 判不出 NaN，过滤必须用 `math.isfinite`（2026-09-19 P2-3）。
26. **缺失数据三语义** — 让渡（因子权重转移）/ 弃权（专家 low_coverage）/ 如实计入，每处必须显式声明，不能默认"缺失→中性 50"（2026-09-19 深查根因；新因子漏登记让渡白名单 = 中性 50 被当真实值参与排序，P1-1 级缺陷）。
27. **close_t0 回测分支已移除** — 回测唯一成交口径 open_t1（次日开盘买）；引用旧文档"尾盘买入口径回测"时注意版本（2026-09-17 移除）。权重论证用 ret_hold1d（T 日尾盘买）口径，与回测引擎 open_t1 口径不可直接混比。
28. **影子推荐落库 mode='shadow'** — 全工程 7 处统计读取硬滤 mode='short'，影子行不进正式统计但自动补收益结果；`get_pending_outcomes` 不滤 mode。
29. **双写源与批次防重** — 批次级防重 `has_predictions(date, mode)`；`created_at` 是 UTC（+8h 才是北京时间）；同日多跑时报告文件会被覆盖（报告侧无防重），对账以 db 为准。
30. **calibrate --apply 双闸门** — 人工审批 + `--accept-ic-objective` 显式确认；被拒退出码 2 且 v1.json 不动（退出码守卫测试锁定）。
31. **飞书消息 vs GitHub 文档格式** — 两个场景不要混：飞书禁表格 `|` 和 `###`；GitHub 文档（README/SKILL/CHEATSHEET）用标准 Markdown 表格与多级标题正常。

---

**数据状态（截至 2026-09-19）**

**方案G 实证成绩（OOS 面板：2024-01 ~ 2026-09，299.9 万行 / 647 交易日 / 5225 只，剔除接近涨停，扣全部成本，Top5 日均超额）**

- 全样本：**+1.845%（t=21.4）**（旧基线 A +0.850%，t=9.4；E 纯净版 +1.876%，t=21.9，G 仅低 0.031 pp）
- 子区间全部成立：2024 全年 +2.069% / 2025 全年 +1.896% / 2026 年内 +1.502% / 后半段（检验期）+1.768%
- 训练/持有切分（≤2025-08 寻优 / ≥2025-09 验证）：500 组随机搜索 + 坐标精修的训练期最优解持有期 +1.754%，**低于 G 的 +1.768%** → G 已在持有期前沿（过拟合防护通过）
- ⚠️ hot_theme 数据源失效压力测试：E/G 都退化到接近零 alpha（+0.04~0.07%/日）——真正的防线是数据源熔断警示机制（`source_status` + `_SOURCE_FACTOR_IMPACT`），不是噪声腿

**历史回测参照（2026-04-01 ~ 2026-06-27，min_score=60 旧配置，run#11–14）**

> ️ 产生于 `min_score=60` + 旧权重。当前 min_score 动态 65/70/75 + G 权重下结果完全不同，历史高收益不可复现（"挤水分"：momentum 降权 + 涨停代理过滤 + 回测口径 hot_theme 中性化），非代码退化。引用必须带上 min_score 与权重版本。

- 总交易次数 95 次｜胜率 57.9%｜avg T+1 +1.63%｜avg T+5 +5.87%｜最大回撤 -13.93%
- 夏普比率 4.36｜策略收益 +62.50%（vs 沪深 300 +7.56%）｜超额 +54.94%

**关于回测口径的重要限制**：修复前（2026-09-05 前）回测中 `capital_flow` 被强制中性化（回测分支将 `main_fund_accumulated` 置 `None`）；2026-09-05 已修复为沿用 `_prefilter` 从 `factor_daily.db` 快照透传的历史值，缺失才置 `None`。但 `factor_daily.db` 历史覆盖率仅 ~10%，多数票仍无历史资金流可用——历史成绩实际检验的是 K 线三因子（动量+技术+量价），与实盘多因子模型不是同一套打分，不可直接比较。

**系统运行状态**

- K 线缓存：全市场 ~5200 只（`prefetch_kline_fullmarket.py` 全量预取）；估值快照：2 个交易日（2026-09-18 起，≥60 交易日启用 OOS）
- predictions.db：135 条（outcomes 132/135 已回填 T+1）；回填由 `dream-backfill` cron 每天 3:15 跑
- 单元测试 438 个 / 门禁 9 项 / 硬契约 7 条 / 守卫测试 16 个
- 版本：稳定版 v-G（2026-09-19 冻结）；origin/main 落后 11 commits（待推）

---

**自动化时间表（生产实际状态）**

- 交易日 14:45 Hermes cron `stock-picker-daily`：选股 + 简报（推送通道属宿主环境 WorkBuddy ClawBot，脚本不内嵌推送）
- 每天 3:15 `dream-backfill`：T+1/T+5/T+20 结果回填（独立脚本 `backfill_pending.py`，凌晨数据完整、与策略无时序依赖）
- 交易日 19:35 复盘笔记自动化（WorkBuddy）：postmortem add/backfill，周五额外输出周命中率
- 周五 16:00 `stock-picker-weekly-backtest`；每月 1 日 `stock-picker-monthly-review`（IC 趋势 + 降权提案，有闸门不静默生效）
- 周六/周日 10:00 `stock-picker-asharehub-prefetch[-sun]`：非交易日配额预取（daily_job 非交易日自动分流）

**观察期安排（2026-09-19 冻结后）**

1. 看简报：下个交易日 14:45 起为新版权重 + 新因子语义首次实盘，确认推送正常、推荐合理
2. 攒数据：复盘命中率（周五首报）+ run_context 门槛分布（当前 2 天），15 个交易日后跑 `recalibrate_thresholds.py --from-run-context`
3. 两个已量化效应（特性非 bug）：弱市"有推荐"天数多约 5pp；热点股集中在"当日已涨 5-9% 但未涨停"的票上

---

**策略运行时序**

```
14:45 启动（Hermes cron stock-picker-daily）
  ├── get_all_codes()        0.001s
  ├── get_all_quotes()       ~46s
  ├── get_ths_hot_stocks()   ~0.22s
  ├── _prefilter()           ~0.5s
  ├── 5维预评分              ~0.3s
  ├── get_main_fund()        ~25s（首次）/ 毫秒（有缓存）
  ├── get_kline() × 200      ~25s（3线程并行）
  ├── RPS / liq_dev / vol_dev 百分位（kline_df 60 日窗口，零配额）
  ├── scoring_model.score()  ~0.2s
  ├── expert_ensemble        覆盖率门控（<0.40 弃权）
  ├── portfolio_optimizer    ~0.05s
  ├── 板块 + 龙虎榜（Top 3） ~8s（em_get 限流）
  └── 输出简报               15:00 前完成
收盘后：19:35 复盘笔记（WorkBuddy）；次日 3:15 dream-backfill 回填
```

---

**数据源优先级速查**

1. **mootdx TCP 7709** — K线 + 财务，永不封 IP，~0.1s/只
2. **腾讯财经 HTTP** — 实时行情 5200+ 只，不封 IP，~46s
3. **同花顺 10jqka** — 强势股 + 题材，零鉴权 73ms
4. **同花顺/东财 资金流** — 主力资金，零鉴权（注：北向汇总实际走东财 `datacenter-web`，早期文档写 `hexin.cn`，代码中并不存在该域名）
5. **ASHareHub** — 北向持仓 / 资金流 / 技术 / 概念 / 财务，100次/天（本地闸门 90）
6. **东财 em_get** — 板块归属 / 龙虎榜，限流

铁律：K 线不要走 baostock（~8s/只），用 mootdx TCP（~0.1s/只）。