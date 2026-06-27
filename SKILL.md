---
name: stock-picker
description: 全栈多因子量化选股系统 — 14直连数据源/30+因子/短线尾盘+长线持股/历史快照回测/自学习权重/Ridge三段式优化
origin: custom
version: 4.2
---

# A股智能选股系统 V4.2

14 个直连数据源 · 30+ 量化因子 · 短线/长线双策略 · 回测引擎（历史快照） · Ridge 自学习权重三段式 · 数据源级独立熔断

兼容 Claude Code · OpenClaw · Codex · Hermes

---

**项目路径**

- 工作目录：`C:\Users\Administrator\Documents\stock-picker`
- Python 执行路径：`C:\Users\Administrator\AppData\Local\Programs\Python\Python310\python.exe`
- 配置文件：`config.yml`
- AI 技能文件：`SKILL.md`
- 使用备忘录：`CHEATSHEET.md`

**快速命令表**

```bash
# 短线策略（全流程 ~4 分钟）
python scripts/eod_stock_picker.py --mode short

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

# 健康检查 23 项
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

- **capital_flow 主力资金流 25%** — ASHareHub moneyflow → 大单缓存 → 同花顺全市场。独立熔断，日配额共享。评分方式：净流入横截面百分位排名。
- **momentum 动量/RPS 25%** — 全市场涨幅排位计算，20 日百分位映射 0-100。无数据源依赖，从 K 线计算。
- **technical 技术形态 15%** — mootdx K 线 6 维评分（趋势 30 + 乖离 20 + 量能 15 + 支撑 10 + MACD 15 + RSI 10）。ASHareHub technical 双源校验。
- **volume_price 量价配合 10%** — 量比 + 尾盘成交结构，0.8~2.0 = 80 分。
- **north_flow 北向资金 10%** — ASHareHub northbound_holdings，持股量变化（股数）计算增减仓方向。独立熔断，日配额共享。回测中恒定为 50 分。
- **hot_theme 同花顺热点 10%** — 10jqka 强势股 + 题材归因（列格式 "算力租赁+Token工厂+AI政务"）。三源融合（同花顺 + 东财 + ASHareHub concepts）。
- **dragon_tiger 龙虎榜 5%** — 东财 datacenter，上榜 + 机构净买入为正。em_get 限流。
- **risk 风险（过滤层，不占权重）** — risk_filter.py 前置拦截 ST / 涨跌停封死 / 流动性不足 / 换手率异常。剩余软风险由 scoring_model 的 penalty 路径在总分上额外扣减。

**数据源熔断状态（当前）**

- `big_deal` — 通
- `ths_fund_flow` — 通
- `north_flow` — 通（asharehub）
- `lockup` — 通
- `asharehub_moneyflow` — 通
- `asharehub_tech_factors` — 通
- `asharehub_concepts` — 通
- `asharehub_financial` — 通

**ASHareHub 4 端共享日配额 100 次**，用满静默降级，独立熔断。熔断每 10 分钟自动恢复。

---

**审计框架**

**业务 4 层**

1. **数据层** — 14 源统一接入 → 独立熔断 → SQLite WAL 缓存 → 失效降级
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
20. **backtest_engine SQLite** — `_load_factor_data()` 连接无 try/finally（已知遗留，低风险）。
21. **ScoringModel 权重加载顺序** — v1.json > config 传入 > DEFAULT_WEIGHTS。v1.json 存在时 config 权重被忽略并记录 warning。
22. **止盈止损从 config 读取** — `sell_config` 参数传入 ScoringModel，`short_term.sell.take_profit` / `stop_loss`，不再硬编码。

---

**数据状态（截至 2026-06-27）**

**最新回测成绩（2026-04-01 ~ 2026-06-27）**

- 总交易次数：95 次
- 胜率：57.9%
- 平均收益 T+1：+1.63%
- 平均收益 T+5：+5.87%
- 最大回撤：-13.93%
- 夏普比率：4.36
- 策略收益：+62.50%（vs 沪深 300 +7.56%）
- 超额收益：+54.94%

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
4. **hexin.cn** — 北向汇总，零鉴权
5. **ASHareHub** — 北向持仓 / 资金流 / 技术 / 概念 / 财务，100次/天
6. **东财 em_get** — 板块归属 / 龙虎榜，限流

铁律：K 线不要走 baostock（~8s/只），用 mootdx TCP（~0.1s/只）。
