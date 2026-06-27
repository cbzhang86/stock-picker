# Stock-Picker 使用备忘录（给 AI 助手）

## 1. 数据源优先级（最关键）

系统的数据源按优先级排列，**不要倒过来用**：

| 优先级 | 数据源 | 用途 | 为什么 |
|:-----:|--------|------|--------|
| **🥇 首选** | **mootdx (TCP 7709)** | K线 + 财务快照 | **永不封 IP**，复用连接后 ~0.1s/只 |
| **🥇 首选** | **腾讯财经 (HTTP)** | 实时行情 5205 只 | **不封 IP**，~46s 全市场 |
| 🥈 | 同花顺热点 10jqka | 强势股 + 题材归因 | 零鉴权 73ms |
| 🥈 | hexin.cn | 北向资金汇总 | 零鉴权 |
| 🥈 | asharehub (需免费API Key) | 个股北向持仓 | HTTP |
| 🥉 | **东财 (em_get 限流)** | 板块归属/龙虎榜 | 有风控，必须经 `em_get()` 串行限流 |

**铁律：K 线不要走 baostock！** baostock 是全局单例，线程不安全，且 ~8s/只。mootdx TCP ~0.1s/只，复用连接更快。

---

## 2. `em_get()` 限流 — 东财数据的唯一入口

所有 `eastmoney.com` 的请求**必须**走 `core/data_engine.py` 里的 `em_get()`：

```python
# 正确
r = em_get("https://push2.eastmoney.com/api/qt/slist/get", params=params, timeout=15)

# 错误 — 不要裸用 requests.get()
r = requests.get("https://push2.eastmoney.com/...")  # 会被封 IP
```

`em_get` 内置了：
- 串行执行（不并发）
- 最小间隔 0.5s + 随机抖动 0.1-0.5s
- 复用 Keep-Alive 会话
- 默认浏览器 UA

---

## 3. Config 权重传递 — 踩过坑的

```python
# 正确 — 从 config.yml 的 weights 段加载
weights_cfg = config.get('weights', config.get('weights_model'))
ScoringModel(weights=weights_cfg if weights_cfg else None)

# 错误 — 永远读不到
ScoringModel(weights=config.get('weights_model'))  # config.yml 里没有这个键
```

ScoringModel 的权重加载顺序：
```
① 构造参数 weights（来自 config.yml 的 weights 段）
② data/weights/v1.json（优化器写入的）
③ DEFAULT_WEIGHTS（代码硬编码）


请求 K 线 → ① SQLite 缓存查询 (data/cache/kline_cache.db)

## 4. 数据源失效保护 + 熔断机制

### 失效保护

系统已经内置：某个数据源不通时，对应因子中性化为 50 分，权重重分配给其他活跃因子。

**不需要手动处理数据源失败。** 如果看到状态里有 "主力资金流(东方财富): 不可用"，这是正常的——东财在境外网络下被 WAF 挡了，系统会自动降权。

当前确实不通的有：
- ~~北向资金个股(东财)~~ → ✅ **已解决**：接入 asharehub northbound_holdings，基于持股量变化计算北向增减仓
- 直连东财 push2 API → 保留代码待以后启用

### 独立数据源级熔断 + 自动恢复

系统使用 `_source_available` 字典实现**数据源级别的独立熔断**，各 endpoint 互不影响。**熔断每 10 分钟自动恢复一次**（`_recover_sources()`），网络抖动不会永久禁用源：

```python
_source_available = {
    'big_deal': True,      # stock_fund_flow_big_deal — ✅ 通
    'capital_flow': True,  # stock_individual_fund_flow — ❌ 不通
    'north_flow': True,    # northbound_holdings (asharehub) — ✅ 通
    'push2': True,         # push2 直连 — ❌ 不通
    'asharehub_moneyflow': True,    # AShareHub 个股资金流
    'asharehub_tech_factors': True, # AShareHub 技术因子
    'asharehub_concepts': True,     # AShareHub 概念板块
    'asharehub_financial': True,    # AShareHub 财务指标
}
```

**关键设计：** `big_deal` 和 `north_flow` 的熔断完全独立。北向数据不可用不影响大单缓存。

### 大单缓存覆盖范围

`stock_fund_flow_big_deal()` 只包含**当日有大单交易**的股票（约 685 只），不是全市场 5205 只。如果一只票今天没有大单交易，它就不在缓存里，`get_main_fund_accumulated()` 返回 `None`，`capital_flow` 因子得 50 分中性值。

降级路径：
1. **大单缓存命中**（685 只）→ 返回真实净流入 → capital_flow 正常评分
2. **大单缓存未命中**（其余 4520+ 只）→ 返回 None → 因子降权中性 50 分，权重分配给其他因子

**不需要处理——这是正常行为，没有大单交易不意味着没有主力资金，系统会通过权重分配自动补偿。**

---

## 5. ASHareHub 日配额管理

ASHareHub 有 **100 次/天** 的 API 调用上限。4 个 AShareHub endpoint 共享同一个日预算计数器：

| Endpoint | 调用位置 | 用途 |
|----------|---------|------|
| `moneyflow` | `_get_capital_flow_asharehub()` | capital_flow 因子优先源 |
| `technical_factors` | `get_technical_factors_asharehub()` | 双源技术评分校验 |
| `concept_members` | `get_concept_members()` | hot_theme 三源融合增强 |
| `financial_indicators` | `get_financial_indicators()` | 长线基本面优先源 |

**消费耗尽后静默返回 None**，等同于该源不可用（独立熔断单元）。第二天自动重置计数器。

三闸齐下：`check_src_available()` → `check_budget()` → `call_api()`。

---

## 6. Mootdx 客户端要复用

```python
# 正确 — 懒加载，复用TCP连接
if self._mootdx_client is None:
    self._mootdx_client = Quotes.factory(market='std')
client = self._mootdx_client
# 后续调用 ~0.1s/只

# 错误 — 每次都新建连接
client = Quotes.factory(market='std')  # 每次 ~1.2s TCP握手
```

---

## 7. K 线数据流

```
请求 K 线 → ① SQLite 缓存查询 (data/kline_cache.db)
         → ② mootdx TCP 拉取（命中缓存则跳过）
         → ③ 新浪 HTTP（mootdx 失败时）
         → ④ baostock（最后降级）
```

缓存 key 是 `(code, date)`，WAL 模式支持并发读。

---

## 8. 运行全流程

```bash
# 完整策略（~4分钟）
python scripts/eod_stock_picker.py --mode short

# 只看状态
python scripts/eod_stock_picker.py --status

# 回测
python scripts/run_backtest.py --mode short --start 2025-06-01 --end 2026-06-11
```

全流程耗时分布：
- 行情拉取 ~46s（腾讯 API 200只/批，不可并行）
- ASHareHub 配额 ~100 次（约 30s，含 0.3s 间隔限流）
- 大单缓存 ~0s（首次 ~25s，后续毫秒）
- K 线 200只 ~25s（mootdx 3线程并行）
- 板块+龙虎榜 ~15s（em_get 限流）
- **总计 ~4-5 分钟**，可以在 14:50-15:00 窗口内完成

---

## 9. 文件操作规范

| 操作 | 路径 | 说明 |
|------|------|------|
| 每日报告 | `data/reports/{mode}_{日期}.md` | 自动生成 |
| 简报 | `data/reports/{mode}_{日期}_briefing.md` | 带市场概览的完整版 |
| K 线缓存 | `data/cache/kline_cache.db` | SQLite WAL，可安全删除重建 |
| 预测追踪 | `data/db/predictions.db` | **保留**，存历史推荐和 T+1 结果。`(date,code,mode)` 唯一约束 |
| 代码缓存 | `data/cache/codes_cache.json` | 每天刷新一次 |
| 模型权重 | `data/weights/v1.json` | 优化器输出，自动加载 |
| 文档 | `docs/` | 参考文档，不影响运行 |

---

## 10. 不要做的

- ❌ 不要对东财开多线程/协程并发请求（`em_get` 已经是串行的）
- ❌ 不要手动改 `data/db/predictions.db`（SQLite 结构固定，改坏会影响权重优化）
- ❌ 不要在回测里用 `np.random`（回测引擎已全部用真实 K 线）
- ❌ 不要直接调 `akshare.stock_individual_fund_flow()`（境外网络不通，走大单缓存）
- ❌ 不要同时跑多个策略实例（mootdx TCP 连接和 SQLite 缓存有状态）
- ❌ 不要用 `except: pass` 吞异常（必须 `logger.warning` 记录）
- ❌ 不要内联 `import` 在方法体里（统一放文件顶部）
- ❌ 不要用 `ast.literal_eval` 解析 JSON（用 `json.loads`，JSON 有 `true`/`false` 不是 Python 字面量）

---

## 11. 如果需要调试

```python
# 检查所有数据源状态
from core.data_engine import DataEngine
de = DataEngine()
print(de.get_data_source_summary())

# 测试 mootdx K 线
df = de._fetch_kline_mootdx('600519', '2025-01-01', '2026-06-11')
print(f"{len(df)} 条 K 线")

# 测试同花顺热点
hot = de.get_ths_hot_stocks()
print(f"{len(hot)} 只强势股")

# 查看当前加载的权重
from core.scoring_model import ScoringModel
sm = ScoringModel()
print(sm.get_weights('short'))
```

## 12. 已落地的速度优化

| 优化项 | 节省时间 | 状态 |
|--------|:-------:|:----:|
| akshare 代码列表本地 JSON 缓存 | 省 ~4s | ✅ 已落地（`data/cache/codes_cache.json`，每天刷新一次） |
| em_get 限流间隔 1.0s → 0.5s | 板块+龙虎榜省 ~15s | ✅ 已落地（`EM_MIN_INTERVAL = 0.5`） |
| mootdx TCP 连接复用 | 每只 K 线省 ~1s | ✅ 已落地（`_mootdx_client` 懒加载） |
| SQLite WAL 模式 | 并发读不阻塞 | ✅ 已落地（`PRAGMA journal_mode=WAL`） |
| 大单缓存 | 199 次调用毫秒级 | ✅ 已落地（全量加载一次，后续查询 O(1)） |
