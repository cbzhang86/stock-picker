# Stock-Picker 使用备忘录（给 AI 助手）

数据源优先级速查、熔断机制、编码铁律、常见陷阱。每次操作前快速过一遍。

> 版本：V4.4（稳定版 v-G，2026-09-19 代码冻结，观察期）。生效权重 = 方案G
> （`data/weights/v1.json`：hot_theme 0.55 主导 + liq_dev 0.14 / reversal_20d 0.10 /
> vol_dev 0.07 / volatility 0.06 + 噪声腿 0.08）。新系统：复盘笔记 `postmortem.py`、
> 门槛重校 `recalibrate_thresholds.py`。完整陷阱清单（编号 1-31）见 SKILL.md。

---

**数据源优先级（从高到低）**

1. **mootdx (TCP 7709)** — K线 + 财务快照，永不封 IP，复用连接后 ~0.1s/只
2. **腾讯财经 (HTTP)** — 实时行情 5205 只，不封 IP，~46s 全市场
3. **同花顺 10jqka** — 强势股 + 题材归因，零鉴权 73ms
4. **东财 datacenter-web** — 北向资金日度净流入汇总，零鉴权
   （早期文档写作 `hexin.cn`，代码中并无该域名，已更正）
5. **ASHareHub (免费 API Key)** — 个股北向持仓 / 个股资金流 / 技术因子 / 概念板块 / 财务指标，4 端共享 100次/天
6. **东财 (em_get 限流)** — 板块归属 / 龙虎榜，有 WAF 风控，必须经 `em_get()` 串行限流

铁律：K 线不要走 baostock！baostock 是全局单例线程不安全，~8s/只。mootdx TCP ~0.1s/只。

---

**em_get() 限流 — 东财数据的唯一入口**

所有 `eastmoney.com` 的请求必须走 `core/data_engine.py` 里的 `em_get()`：

```python
# 正确
r = em_get("https://push2.eastmoney.com/api/qt/slist/get", params=params, timeout=15)

# 错误 — 不要裸用 requests.get()
r = requests.get("https://push2.eastmoney.com/...")  # 会被封 IP
```

`em_get` 内置了串行执行（不并发）、最小间隔 0.5s + 随机抖动 0.1-0.5s、复用 Keep-Alive 会话、默认浏览器 UA。

---

**ASHareHub 日配额管理**

4 个 ASHareHub endpoint 共享 **100 次/天** 的日预算计数器：

- `moneyflow` → capital_flow 因子优先源
- `technical_factors` → 双源技术评分校验
- `concept_members` → hot_theme 三源融合增强
- `financial_indicators` → 长线基本面优先源

配额耗尽后静默返回 None，等同于该源不可用。第二天自动重置。三闸齐下：`check_src_available()` → `check_budget()` → `call_api()`。

---

**熔断机制 + 10 分钟自动恢复**

系统用 `_source_available` 字典实现数据源级别的独立熔断，各 endpoint 互不影响：

```python
_source_available = {
    'big_deal': True,               # akshare 大单
    'ths_fund_flow': True,          # 同花顺全市场资金流
    'north_flow': True,             # ASHareHub 北向持仓
    'lockup': True,                 # 限售解禁
    'asharehub_moneyflow': True,    # ASHareHub 个股资金流
    'asharehub_tech_factors': True, # ASHareHub 技术因子
    'asharehub_concepts': True,     # ASHareHub 概念板块
    'asharehub_financial': True,    # ASHareHub 财务指标
}
```

熔断每 10 分钟自动恢复一次（`_recover_sources()`），网络抖动不会永久禁用源。`big_deal` 和 `north_flow` 的熔断完全独立。

大单缓存只包含当日有大单交易的股票（约 685 只），不是全市场 5205 只。未命中 → 返回 None → 因子降权中性 50 分→ 权重自动分配给其他因子。不需要手动处理。

---

**三大编码铁律**

1. **except 必须 log** — 禁止 `except: pass`，必须 `logger.warning(f"...: {e}")`。已修复 12 处。
2. **SQLite 必须 try/finally** — `conn = None` → `try:` → `finally: if conn: conn.close()`。已修复 5 处（backtest_engine + tracker）。
3. **import 必须文件顶部** — 禁止方法体内 `import`，统一放文件顶部。已修复（`ThreadPoolExecutor` / `as_completed`）。

---

**仓库纪律：API Key 安全**

- ASHareHub API Key 通过环境变量 `ASHAREHUB_API_KEY` 传入
- 禁止写死在代码或配置文件中
- `config.yml` 不存储任何密钥

---

**权重加载顺序**

```python
# 生效权重唯一来源 = data/weights/v1.json（方案G，2026-09-19）
# 正确 — 从 config.yml 的 weights 段加载
weights_cfg = config.get('weights', config.get('weights_model'))
ScoringModel(weights=weights_cfg if weights_cfg else None, sell_config=config.get('sell', {}))
```

ScoringModel 权重加载优先级：
1. `data/weights/v1.json`（方案G 生效层，优先于 config 传入）
2. 构造参数 `weights`（来自 config.yml 的 weights 段，已同步为 G 值）
3. `DEFAULT_WEIGHTS`（代码硬编码，已同步为 G 值）

三层已一致；不一致时 config 权重被忽略并记录 warning（语义比较，只报真不一致）。
ScoringModel 不写入 v1.json——权重改动唯一入口 `calibrate_weights.py`（双闸门）。

止盈止损值从 `sell_config` 读取（即 `config.yml` 的 `short_term.sell` 段），不再硬编码 2%/2%。

---

**常见陷阱（编号 1-22）**

1. **json.loads 不能 ast.literal_eval** — JSON 的 `true`/`false` 不是 Python 字面量。`optimizer._load_history()` 已修复。
2. **Optimizer 列缺失填充 0.5** — Ridge 回归时新因子列（如 hot_theme）在旧记录中不存在，自动填 0.5。
3. **权重坍缩保护** — 单因子 ≥ 80% 跳过优化，防止单一因子主导。
4. **不要多线程并发东财** — `em_get` 已经是串行的，外层再开线程会被封 IP。
5. **不要手动改 predictions.db** — SQLite 结构固定，改坏影响权重优化。
6. **回测不要用 np.random** — 回测引擎已全部用真实 K 线。
7. **不要同时跑多个策略实例** — mootdx TCP 连接和 SQLite 缓存有状态。
8. **不要直接调 akshare 东财接口** — 直连会被 WAF 拦，走大单缓存 / em_get 限流。
9. **Config 权重字段名是 `short_term.weights`** — 不是 `short_term.weights_model`。
10. **Baostock 复权参数** — 回测用 `adjustflag='1'`（后复权），不是 `'2'`（前复权）。
11. **北向回测语义** — 回测中北向因子恒定为 50（中性值），因北向数据不可回溯。
12. **两状态同步** — 策略退出前调用 `self._save_state()` 保存运行状态。
13. **CLI 默认日期** — `run_backtest.py --start` 默认 `2026-04-01`，`--end` 默认 `2026-06-27`，与当前季度对齐。
14. **push2 直连已删除** — 原 `_get_capital_flow_push2()` 方法已移除，不要引用。
15. **ModelRegistry 已删除** — `core/model_registry.py` 整文件移除（144 行死代码），版本管理通过带时间戳的权重文件实现。
16. **Optimizer 三段式工作流** — `check_and_report()` 只产出报告不写入 → 审批 → `apply_from_report()` 写入。`maybe_optimize()` 保留原签名但降级为只报告。
17. **Optimizer 缓存目录** — `.last_optimize_short` 计数文件在 `data/cache/`，不在 `data/weights/`。
18. **Tracker UNIQUE 约束** — `predictions(date, code, mode)` 有 UNIQUE 索引，重复插入会抛异常，需用 INSERT OR REPLACE。
19. **factor_scores JSON 序列化** — `json.dumps(factor_scores, default=str)` 处理 numpy 类型。
20. **backtest_engine SQLite** — `_load_factor_data()` 连接已补 try/finally（2026-09-06 修复，此前为已知遗留）。
21. **ScoringModel 权重加载顺序** — v1.json > config 传入 > DEFAULT_WEIGHTS。三层已全部对齐（2026-09-14 起：config.yml 的 short_term.weights 同步为 v1.json 值，告警改为语义比较——只在真不一致时打印，不再恒假阳性）。改权重必须改 `data/weights/v1.json`。
22. **止盈止损从 config 读取** — `sell_config` 参数传入 ScoringModel，`short_term.sell.take_profit` / `stop_loss`，不再硬编码。

---

**V4.4 速查（2026-09-18 ~ 09-19 新架构）**

- `scripts/postmortem.py` — 复盘笔记（预测-对账闭环）：LLM 只填 thesis/missed_risk/key_factors/prediction；程序回填 realized_outcome/verdict；周命中率 ≤50% 熔断（退出码 1）；笔记库 `data/cache/postmortem_notes.db`
- `scripts/recalibrate_thresholds.py` — 门槛重校双模式（`--from-run-context` 实测 / `--oos-proxy` OOS 代理），样本不足返回码 1 不产伪结论
- `scripts/snapshot_valuation_daily.py` — 估值快照（2026-09-18 起每日积累，≥60 交易日启用 OOS）
- `scripts/backup_predictions.py` — predictions.db 每日备份
- `core/scoring_model.py` — 方案G 权重三层 + liq_dev/vol_dev 偏离因子 + rank_stocks 百分位（kline_df 60 日窗口，零 AShareHub 配额）
- `core/expert_ensemble.py` — 覆盖率门控 `MIN_EXPERT_COVERAGE=0.40`（数据不足弃权 `low_coverage`，不再失明压分）
- `scripts/calibrate_weights.py` — `--apply` 双闸门（人工审批 + `--accept-ic-objective`）；被拒退出码 2 且 v1.json 不动
- 影子推荐：极端市况照常评分落库 `mode='shadow'` 但不下发（开关 `short_term.shadow_enabled`，默认 true）
- 守卫测试 `tests/test_audit_fixes_20260919.py`（16 例）：让渡白名单 / 数据源依赖表 / 退出码三守卫
- **回测口径**：open_t1 唯一有效（次日开盘买）；close_t0 已于 2026-09-17 移除

**V4.4 陷阱补充（23-31，完整版见 SKILL.md）**

23. **IC 不是权重的判据** — 等权 4 腿 IC 最高（0.0743）却几乎不赚钱；权重以 Top-N 尾部收益为准
24. **liquidity ≠ liq_dev** — 原始 liquidity 84% 是小市值效应（相关 0.838）已证伪保持 0；起用正交的 liq_dev（相关 −0.088）
25. **NaN 是 float** — `isinstance(v, float)` 判不出 NaN，过滤用 `math.isfinite`
26. **缺失数据三语义** — 让渡/弃权/如实计入三选一显式声明，不能默认"缺失→中性 50"
27. **close_t0 回测分支已移除** — 唯一口径 open_t1；权重论证 ret_hold1d 口径与回测不可直接混比
28. **影子推荐 mode='shadow'** — 7 处统计读取硬滤 mode='short'；影子行不进正式统计但自动补收益
29. **双写源与批次防重** — 防重按"批"粒度 `has_predictions(date, mode)`；`created_at` 是 UTC（+8h 才是北京时间）；报告侧无防重会被覆盖，对账以 db 为准
30. **calibrate --apply 双闸门** — 人工审批 + `--accept-ic-objective`；被拒退出码 2
31. **飞书消息 vs GitHub 文档格式** — 飞书禁表格 `|` 和 `###`；GitHub 文档标准 Markdown 正常

**V2 速查（旧架构，仍生效）**

- `core/expert_ensemble.py` — 5 维专家第二意见：一致微调、分歧降权、冲突（Δ>25）降仓
- `core/oos_validator.py` — walk-forward OOS IC（三口径取悲观值 + daily_ics）
- `core/trading_calendar.py` — 集中式交易日历（本地缓存+联网刷新）
- `scripts/pick.py` — 统一 CLI：pick/backfill/health/oos/backtest/prefetch/calibrate/slippage
- `scripts/daily_job.py` — 每日统一调度（交易日→选股 / 非交易日→预取）
- `scripts/evaluate_all.py` — 回归门禁（9 项；任何代码/权重改动后必跑）
- `scripts/calibrate_weights.py` — OOS 权重校准（--apply 需人工审批）

**快速调试命令**

```bash
# 完整策略（2026-09-06 性能优化后端到端 ~113s）
python scripts/eod_stock_picker.py --mode short

# 统一 CLI 入口（2026-09-07）：pick/backfill/health/oos/backtest/prefetch/calibrate/slippage
python scripts/pick.py health          # 快速门禁（9 项）
python scripts/pick.py pick            # 尾盘选股

# 每日统一调度（交易日→选股，非交易日→配额预取，内置交易日历分流）
python scripts/daily_job.py

# 回归门禁（任何代码/权重改动后必跑）
python scripts/evaluate_all.py

# 查看状态和近期表现
python scripts/eod_stock_picker.py --status

# 回测验证（默认近 3 个月）
python scripts/run_backtest.py --mode short

# 健康检查（9 项）
python scripts/verify.py
```

```python
# 检查所有数据源状态
from core.data_engine import DataEngine
de = DataEngine()
print(de.get_data_source_summary())

# 测试 mootdx K 线
df = de._fetch_kline_mootdx('600519', '2026-01-01', '2026-06-27')
print(f"{len(df)} 条 K 线")

# 测试同花顺热点
hot = de.get_ths_hot_stocks()
print(f"{len(hot)} 只强势股")

# 查看当前加载的权重
from core.scoring_model import ScoringModel
sm = ScoringModel()
print(sm.get_weights('short'))
```

---

**K 线数据流**

```
请求 K 线 → ① SQLite 缓存查询（data/cache/kline_cache.db）
         → ② mootdx TCP 拉取（命中缓存则跳过）
         → ③ 新浪 HTTP（mootdx 失败时）
         → ④ baostock（最后降级）
```

缓存 key 是 `(code, date)`，WAL 模式支持并发读。

---

**速度优化已落地**

- akshare 代码列表本地 JSON 缓存（省 ~4s）
- em_get 限流间隔 1.0s → 0.5s（板块+龙虎榜省 ~15s）
- mootdx TCP 连接复用（每只 K 线省 ~1s）
- SQLite WAL 模式（并发读不阻塞）
- 大单缓存（199 次调用毫秒级）

全流程耗时：行情 ~46s + ASHareHub ~30s + 大单 ~25s（首次） + K 线 ~25s + 板块+龙虎榜 ~15s。总计约 4-5 分钟，可在 14:50-15:00 窗口内完成。
