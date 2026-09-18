---
name: stock-picker
description: 全栈多因子量化选股系统 — 8数据源/16网络端点/30+因子/短线尾盘+长线持股/历史快照回测/OOS IC权重校准/专家Ensemble第二意见/回归门禁
origin: custom
version: 4.3
---

# A股智能选股系统 V4.3

8 数据源 / 16 网络端点 · 30+ 量化因子 · 短线/长线双策略 · 回测引擎（历史快照） · OOS IC 校准权重 · 专家 Ensemble 第二意见 · 数据源级独立熔断

兼容 Claude Code · OpenClaw · Codex · Hermes

---

**项目路径**

- 工作目录：`C:\Users\Administrator\Documents\stock-picker-v2`（生产目录；`Documents\stock-picker` v1 已冻结为回滚基线）
- Python 执行路径：`C:\Users\Administrator\AppData\Local\Programs\Python\Python311\python.exe`
  （注意：`Python310` 目录在本机不存在，早期文档写法有误）
- 配置文件：`config.yml`
- AI 技能文件：`SKILL.md`
- 使用备忘录：`CHEATSHEET.md`

**快速命令表**

```bash
# 短线策略（2026-09-06 性能优化后端到端 ~113s）
python scripts/eod_stock_picker.py --mode short

# 统一 CLI 入口（2026-09-07）
python scripts/pick.py pick        # 尾盘选股
python scripts/pick.py health      # 快速门禁（9 项）
python scripts/pick.py oos         # 全量 OOS 因子诊断
python scripts/pick.py calibrate   # 权重校准建议（不自动生效）

# 每日统一调度（交易日→选股 / 非交易日→配额预取）
python scripts/daily_job.py

# 回归门禁（任何代码/权重改动后必跑）
python scripts/evaluate_all.py

# 查看状态和近期表现
python scripts/eod_stock_picker.py --status

# 长线策略
python scripts/eod_stock_picker.py --mode long

# 回测验证（默认近 3 个月 2026-04-01 ~ 2026-06-27）
python scripts/run_backtest.py --mode short

# 指定区间回测
python scripts/run_backtest.py --mode short --start 2026-01-01 --end 2026-06-27

# 回测版本查看 / 对比
python scripts/run_backtest.py --list
python scripts/run_backtest.py --compare 1 2

# K 线因子专项回测（仅量价+动量+技术）
python scripts/run_backtest.py --mode kfactor --start 2026-01-01 --end 2026-06-27

# 健康检查 9 项
python scripts/verify.py

# 权重优化（仅报告，不写入）
python -c "from feedback.optimizer import WeightsOptimizer; from feedback.tracker import PredictionTracker; import yaml; cfg = yaml.safe_load(open('config.yml')); t = PredictionTracker(); o = WeightsOptimizer(); r = o.check_and_report(t, cfg.get('weights',{}), 'short'); print(r['summary'])"
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

---

**因子架构表（7 因子 + risk）**

⚠️ 权重唯一事实源 = `data/weights/v1.json`（ScoringModel 加载优先级 v1.json > config.yml，
SKILL.md 陷阱 21）。下表为 2026-09-05 OOS 校准后的生效值；调整权重唯一入口
`python scripts/calibrate_weights.py`（--apply 需人工审批）。config.yml 权重段是历史草稿。

- **hot_theme 同花顺热点 50%** — 10jqka 强势股 + 题材归因（列格式 "算力租赁+Token工厂+AI政务"）。三源融合（同花顺 + 东财 + ASHareHub concepts）。OOS IC +0.0651 (t=+2.17)，唯一统计显著有效因子。
- **technical 技术形态 16%** — mootdx K 线 6 维评分（趋势 30 + 乖离 20 + 量能 15 + 支撑 10 + MACD 15 + RSI 10）。实测 OOS IC -0.0462 弱负向，保留作观察。
- **capital_flow 主力资金流 15%** — ASHareHub moneyflow → 大单缓存 → 同花顺全市场。独立熔断，日配额共享。评分方式：净流入横截面百分位排名。OOS IC +0.0056 弱正。
- **volume_price 量价配合 12%** — 量比（5 日均量口径，2026-09-05 统一）+ 尾盘成交结构。实测 OOS IC -0.0460 稳定反向，降权观察。
- **momentum 动量/RPS 4%** — 全市场涨幅排位计算，20 日百分位映射 0-100。实测强反向 IC -0.0926，降至探索位（4%）。
- **dragon_tiger 龙虎榜 3%** — 东财 datacenter，机构净买入为正。实测 IC 噪声级，最低保留权重。
- **north_flow 北向资金 0%** — 2024-08 政策停公开，因子恒返回 50 中性值，不占权重。
- **待验证因子（权重 0，链路已通）** — valuation_fundamental（通达信估值/基本面）、event_catalyst（公告/事件）。快照积累 ≥60 交易日后跑 OOS 验证再启用。
- **risk 风险（过滤层，不占权重）** — risk_filter.py 前置拦截 ST / 接近涨停（代理规则 pct_chg ≥ 板块上限×98%，2026-09-05 P0-E）/ 跌停 / 流动性不足 / 换手率异常。剩余软风险由 scoring_model 的 penalty 路径在总分上额外扣减。

**数据源熔断状态（当前）**

- `akshare_north_flow` — 通（asharehub；键名 2026-09-03 从 `north_flow` 统一，两字典对齐）
- `big_deal` — 通
- `ths_fund_flow` — 通
- `lockup` — 通
- `asharehub_moneyflow` — 通
- `asharehub_tech_factors` — 通
- `asharehub_concepts` — 通
- `asharehub_financial` — 通

**ASHareHub 4 端共享日配额 100 次**，用满静默降级，独立熔断。熔断每 10 分钟自动恢复。

---

**V4.3 新增架构（2026-09-05 ~ 09-07 三轮迭代）**

- `core/expert_ensemble.py` — 5 维专家第二意见（基本面/技术/资金/估值/事件），一致微调、分歧降权、冲突降仓
- `core/oos_validator.py` — walk-forward 样本外 IC（hold1d/intraday/overnight 三口径取悲观值 + daily_ics 输出）
- `core/trading_calendar.py` — 集中式交易日历（本地缓存+联网刷新，统一 beijing_now 时区）
- `core/drift_monitor.py` — 绩效漂移监控（PSI > 0.25 告警）
- `core/data_quality_monitor.py` — 数据质量巡检（五类异常自动检出，接入简报）
- `core/fundamental_provider.py` / `core/event_provider.py` — 估值/事件因子链路（点时化防前视，权重 0 待验证）
- `core/factor_standardizer.py` — 横截面标准化（rank/winsor_z，实验开关，默认关）
- `scripts/pick.py` — 统一 CLI 入口（pick/backfill/health/oos/backtest/prefetch/calibrate/slippage）
- `scripts/daily_job.py` — 每日统一调度（交易日→选股 / 非交易日→配额预取，时区安全）
- `scripts/evaluate_all.py` — 回归门禁（9 项；任何代码/权重改动后必跑）
- `scripts/calibrate_weights.py` — OOS 权重校准（--apply 需人工审批；--method icir 待 daily_ics ≥60 日）
- `scripts/calibrate_slippage.py` / `scripts/capacity_check.py` / `scripts/multiple_testing.py` — 滑点校准 / 容量测算 / DSR/PBO 多重检验
- `scripts/prefetch_tdx.py` — 通达信估值/事件数据预取

**V4.3 行为变更（有数据支撑）**

- kill-switch 从"硬熔断停推"改为"健康度展示+谨慎提示"（strategy_health 随推荐下发）
- 市场评估 52.5→45.0：打板情绪新口径揭示旧涨停跌停比高估情绪
- 追高惩罚（>7% 线性砍分）/ 相关性约束 / 波动率保险丝三层新风控（极端场景保险丝，正常日零影响）
- 性能：端到端选股 226s → 113s（sqlite 线程级复用、行情二级缓存、批量翻倍、节流降档）

**审计框架**

**业务 4 层**

1. **数据层** — 8 源统一接入（16 网络端点）→ 独立熔断 → SQLite WAL 缓存 → 失效降级
   - 检查点：mootdx TCP 连接是否复用、em_get 是否串行、ASHareHub 配额是否监控、缓存是否命中
2. **策略层** — 初筛 → 预评分 → 详评 → 评分 → 仓位分配
   - 检查点：市场评估是否跳过、预过滤条件是否合理、防凑数是否生效、极差市是否输出简报
3. **回测层** — 历史快照 → 逐日回放 → 真实 K 线收益 → 仓位模拟 → 因子 IC
   - 检查点：是否用真实 K 线而非 np.random、是否用历史当日行情而非今日数据、sell_config 是否从 config 读取
4. **反馈层** — 推荐入库 → 回填收益 → Ridge 优化 → 审批写入
   - 检查点：三段式是否合规（只报告不自动写）、坍缩保护是否触发、新鲜度检查是否通过、ast.literal_eval 是否已替换为 json.loads

**工程 3 维**

1. **代码规范** — except 必须 log / SQLite 必须 try/finally / import 必须文件顶部 / 禁止方法体内 import
2. **配置规范** — config.yml 权重段必须 `short_term.weights` / API Key 必须环境变量 / sell 参数必须从 config 读取而非硬编码
3. **文档规范** — 飞书格式铁律 / 文档与代码同步更新 / 陷阱编号可追溯

---

**回测与优化器的关系**

**回测能验证的（量价 + 动量 + 技术因子）**

- `volume_price` / `momentum` / `technical` 从历史 K 线重新计算，在回测中有真实方差
- IC 信息系数 > 0.03 视为有效信号
- 仓位模拟：按 PortfolioOptimizer 分配比例，T+1 开盘买 / 收盘卖，含滑点万十 + 佣金万三

**回测不能验证的（资金流 + 北向 + 热点 + 龙虎榜）**

- 这些因子在回测期间没有真实历史数据，恒定为中性 50 分
- 赢单/输单分差趋近于零，IC 接近零——这不代表因子无效，是数据条件下的必然
- 这些因子的有效性通过优化器从实盘收益学习验证

**优化器的工作**

- 积累 >= 60 条有 T+1 结果的推荐后触发
- 用 Ridge 回归将因子原始得分映射到实际收益率
- RI 系数 → 归一化新权重（负系数截断为 0）
- 三段式：`check_and_report()` → 审批 → `apply_from_report()`
- 不自动写入，只产出报告供人工审批
- **当前状态：设计未启用（刻意停用）**。`apply_from_report()` 目前无调用方，
  `data/reports/` 下累积的建议报告**不落地**是预期行为，不是缺陷。
  如需启用，须先补一个执行入口（CLI 参数或独立脚本）。

**最佳实践**：先用默认权重跑实盘积累记录 → 让 Ridge 回归纳一化调整 → 审批后 apply → 下次回测对比效果。

---

**代码改动纪律（三步走）**

1. **审** — 先读目标文件全文，理解现有逻辑和数据流，搜索相关引用
2. **改** — 最小改动原则：改函数不改架构，除非有明确重构需求
3. **验** — 改后跑一次完整流程验证不报错

**禁止的改动模式**

- 不要修改 `predictions.db` 的 SQLite 结构——那是优化器的数据来源
- 不要给东财开多线程/协程并发——`em_get` 已经是串行的
- 不要在策略层硬编码止盈止损值——必须从 `config.yml sell` 段读取
- 不要在回测里用 `np.random`——必须用真实 K 线
- 不要手动改权重文件——通过优化器三段式流程
- 不要在方法体内写 `import`——统一放文件顶部
- **新增数据源时，两个字典必须同时登记** —— `_source_available`（熔断）与
  `_source_status`（健康报告）键名要一致。`_update_source_status()` 内部有
  `if source_key in self._source_status` 判断，未登记的键会被**静默丢弃**：
  熔断已生效但报告仍显示正常，故障不可见。2026-09-03 已补齐 3 个漏登记键
  （`ths_fund_flow` / `big_deal` / `lockup`），并统一北向键名为 `akshare_north_flow`。

---

**所有常见陷阱（编号 1-22）**

1. **json.loads 不能 ast.literal_eval** — JSON 的 `true`/`false` 不是 Python 字面量。`optimizer._load_history()` 已修复。
2. **Optimizer 列缺失填充 0.5** — Ridge 回归时新因子列（如 hot_theme）在旧记录中不存在，自动填 0.5。
3. **权重坍缩保护** — 单因子 ≥ 80% 跳过优化，防止单一因子主导。
4. **不要多线程并发东财** — `em_get` 已经是串行的。
5. **不要手动改 predictions.db** — SQLite 结构固定。
6. **回测不要用 np.random** — 必须用真实 K 线。
7. **不要同时跑多个策略实例** — mootdx TCP 和 SQLite 缓存有状态。
8. **不要直接调 akshare 东财接口** — 境外网络不通，走大单缓存。
9. **Config 权重字段名** — `short_term.weights`，不是 `short_term.weights_model`。
10. **Baostock 复权参数** — 回测用 `adjustflag='1'`（后复权），不是 `'2'`（前复权）。
11. **北向回测语义** — 回测中北向因子恒定为 50（中性值），因北向数据不可回溯。
12. **两状态同步** — 策略退出前调用 `self._save_state()` 保存运行状态。
13. **CLI 默认日期** — `run_backtest.py --start` 默认 `2026-04-01`，`--end` 默认 `2026-06-27`。
14. **push2 直连已删除** — `_get_capital_flow_push2()` 方法已移除。`push2.eastmoney.com` 仅存在于板块归属 URL 中（走 em_get 限流），不是数据源。
15. **ModelRegistry 已删除** — `core/model_registry.py` 整文件移除（144 行死代码），版本管理通过带时间戳的权重文件实现。
16. **Optimizer 三段式工作流** — `check_and_report()` 只产出报告不写入 → 审批 → `apply_from_report()` 写入。`maybe_optimize()` 保留原签名但降级为只报告。
17. **Optimizer 缓存目录** — `.last_optimize_short` 计数文件在 `data/cache/`，不在 `data/weights/`。
18. **Tracker UNIQUE 约束** — `predictions(date, code, mode)` 有 UNIQUE 索引，重复插入会抛异常。
19. **factor_scores JSON 序列化** — `json.dumps(factor_scores, default=str)` 处理 numpy 类型。
20. **backtest_engine SQLite** — `_load_factor_data()` 连接已补 try/finally（2026-09-06 修复，此前为已知遗留）。
21. **ScoringModel 权重加载顺序** — v1.json > config 传入 > DEFAULT_WEIGHTS。三层已全部对齐（2026-09-14 起：config.yml 的 short_term.weights 同步为 v1.json 值，告警改为语义比较——只在真不一致时打印，不再恒假阳性）。改权重必须改 `data/weights/v1.json`。
22. **止盈止损从 config 读取** — `sell_config` 参数传入 ScoringModel，`short_term.sell.take_profit` / `stop_loss`，不再硬编码。

---

**数据状态（截至 2026-06-27）**

**最新回测成绩（2026-04-01 ~ 2026-06-27，对应 min_score=60 版本，run#11–14）**

> ⚠️ 前提条件：以下成绩产生于 `min_score=60` 的配置。当前 `config.yml` 基准已提高至 **75**，
> 修复后同区间重跑（run#26，2026-09-05 后代码）为 **0 笔达标**——历史高收益不可复现，
> 是"挤水分"（momentum 降权 + 涨停代理过滤 + 回测口径 hot_theme 中性化）而非代码退化。
> 另一参照：修复前代码的 run#22 为 52 笔 / 胜率 53.85% / 策略收益 51.29%，
> 量级与修复后断崖落差，进一步印证"挤水分"结论。引用时务必带上 `min_score` 值。

- 总交易次数：95 次（min_score=60）
- 胜率：57.9%
- 平均收益 T+1：+1.63%
- 平均收益 T+5：+5.87%
- 最大回撤：-13.93%
- 夏普比率：4.36
- 策略收益：+62.50%（vs 沪深 300 +7.56%）
- 超额收益：+54.94%

**关于回测口径的重要限制**：修复前（2026-09-05 前）的回测中 `capital_flow`（时为 config 草稿权重 0.35）被强制中性化——
`strategies/short_term.py` 回测分支将 `main_fund_accumulated` 置为 `None`，初筛亦不保留该字段。
2026-09-05 已修复：回测分支改为沿用 `_prefilter` 从 `factor_daily.db` 快照透传的历史值，缺失才置 `None`
（`short_term.py:907`）；但 `factor_daily.db` 历史覆盖率仅 ~10%（见 `data/reports/weight_calibration_20260905.md`），
多数票仍无历史资金流可用。上述成绩实际检验的是**动量 + 技术 + 量价三个 K 线因子**，与实盘 7 因子模型并非同一套打分，两者不可直接比较。

**因子 IC（近 3 个月回测）**

- `volume_price` — IC +0.1202 / 强有效
- `technical` — IC +0.0916 / 强有效
- `hot_theme` — IC -0.0394 / 反向
- `momentum` — IC -0.0382 / 反向
- `dragon_tiger` — IC -0.0076 / 噪声

**系统运行状态**

- K 线缓存：4483 只股票，2025-06 至 2026-06
- 大单缓存：685 只每日更新
- 同花顺资金流：5189 只全量缓存
- 代码列表缓存：5205 只
- 回测版本：最新为 v2（95 笔，57.9% 胜率）

---

**策略运行时序**

```
14:50 启动
  ├── get_all_codes()        0.001s
  ├── get_all_quotes()       ~46s
  ├── get_ths_hot_stocks()   ~0.22s
  ├── _prefilter()           ~0.5s
  ├── 5维预评分              ~0.3s
  ├── get_main_fund()        ~25s（首次）/ 毫秒（有缓存）
  ├── get_kline() × 200      ~25s（3线程并行）
  ├── RPS 排名               ~0.1s
  ├── scoring_model.score()  ~0.2s
  ├── portfolio_optimizer    ~0.05s
  ├── 板块 + 龙虎榜（Top 3） ~8s（em_get 限流）
  └── 输出简报               15:00 前完成
```

---

**数据源优先级速查**

1. **mootdx TCP 7709** — K线 + 财务，永不封 IP，~0.1s/只
2. **腾讯财经 HTTP** — 实时行情 5205 只，不封 IP，~46s
3. **同花顺 10jqka** — 强势股 + 题材，零鉴权 73ms
4. **同花顺/东财 资金流** — 主力资金，零鉴权（注：北向汇总实际走东财 `datacenter-web`，
   早期文档写 `hexin.cn`，代码中并不存在该域名）
5. **ASHareHub** — 北向持仓 / 资金流 / 技术 / 概念 / 财务，100次/天
6. **东财 em_get** — 板块归属 / 龙虎榜，限流

铁律：K 线不要走 baostock（~8s/只），用 mootdx TCP（~0.1s/只）。
