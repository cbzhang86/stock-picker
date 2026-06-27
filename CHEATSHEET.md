# Stock-Picker 使用备忘录（给 AI 助手）

数据源优先级速查、熔断机制、编码铁律、常见陷阱。每次操作前快速过一遍。

---

**数据源优先级（从高到低）**

1. **mootdx (TCP 7709)** — K线 + 财务快照，永不封 IP，复用连接后 ~0.1s/只
2. **腾讯财经 (HTTP)** — 实时行情 5205 只，不封 IP，~46s 全市场
3. **同花顺 10jqka** — 强势股 + 题材归因，零鉴权 73ms
4. **hexin.cn** — 北向资金汇总，零鉴权
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
# 正确 — 从 config.yml 的 weights 段加载
weights_cfg = config.get('weights', config.get('weights_model'))
ScoringModel(weights=weights_cfg if weights_cfg else None, sell_config=config.get('sell', {}))

# 错误 — weights_model 在 config.yml 里不存在
ScoringModel(weights=config.get('weights_model'))
```

ScoringModel 权重加载优先级：
1. `data/weights/v1.json`（优化器写入的，优先于 config 传入）
2. 构造参数 `weights`（来自 config.yml 的 weights 段）
3. `DEFAULT_WEIGHTS`（代码硬编码）

如果 v1.json 与 config 不一致，config 权重会被忽略并记录 warning。
ScoringModel 不再写入 v1.json（仅优化器三段式写入）。

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
8. **不要直接调 akshare 东财接口** — 境外网络不通，走大单缓存。
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
20. **backtest_engine SQLite** — `_load_factor_data()` 连接无 try/finally（已知遗留，低风险）。
21. **ScoringModel 权重加载顺序** — v1.json > config 传入 > DEFAULT_WEIGHTS。v1.json 存在时 config 权重被忽略并记录 warning。
22. **止盈止损从 config 读取** — `sell_config` 参数传入 ScoringModel，`short_term.sell.take_profit` / `stop_loss`，不再硬编码。

---

**快速调试命令**

```bash
# 完整策略（~4分钟）
python scripts/eod_stock_picker.py --mode short

# 查看状态和近期表现
python scripts/eod_stock_picker.py --status

# 回测验证（默认近 3 个月）
python scripts/run_backtest.py --mode short

# 健康检查（23 项）
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
