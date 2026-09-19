# stock-picker-v2 项目长期记忆

## 2026-09-19 深查修复完成（6 项）+ 两条遗留

- **P1-1 已修**：`score_stock` 中性让渡白名单扩至「百分位驱动」因子（liq_dev/vol_dev/volatility/liquidity/size）→ 缺失时 `data_available=False`、有效权重 0，不再以中性 50 冒充真实值（与 OOS 的 NaN 剔除口径对齐）。**momentum/reversal_20d 有意未改**：`data_engine.py:1272` 无 K 线时返回恒 50 而非 None，改用 kline_df 判定会变 0.12 权重动量腿行为，属未验证语义变更 → 遗留项
- **P1-2 已修**：`_SOURCE_FACTOR_IMPACT` 的 mootdx/baostock_kline 映射补齐新因子（此前 K 线源故障时简报漏算 0.27 权重）
- **P1-3 已定性并建通道**：OOS 代理显示分数分布**分化而非平移**（池内 60 分位 68.3→63.0、80 分位 69.5→66.0）；**弱市门槛 75 的通过率 92.5%→97.5%（+5.0pp）**；门槛数值不改，用 `scripts/recalibrate_thresholds.py`（--from-run-context 需 ≥15 交易日实测 / --oos-proxy 即时代理）待数据后重校
- **P1-4 已修**：`calibrate_weights.py` 补 `sys.exit(main())`（原 `main()` 丢失 return 2 → 自动化把"拒绝写入"误判为成功）。全库扫描确认仅此脚本 main 返回非零码
- **P2-1/2/3 已修**：校准器 `--apply` 增加 `--accept-ic-objective` fail-fast 闸门（IC 目标与尾部收益判据冲突）；briefing source_status 回退加 warning；主题榜排序改 `math.isfinite`
- **验证基线更新：unittest 438 passed + 门禁 9/0**；六维验证（单元/门禁/集成/**无副作用**/行为退出码/量化代理）全通过
- **防漂移守卫**：`tests/test_audit_fixes_20260919.py`（16 例）——百分位因子让渡白名单守卫、数据源依赖族守卫（不用"全部因子"哨兵做判据）、退出码守卫

## 2026-09-19 第三轮：专家五维修复 + 复盘系统上线

- **ExpertEnsemble 失明修复已生效**（用户决策"完善专家五维"）：`MIN_EXPERT_COVERAGE=0.40` 覆盖率门控（不足两维有数据 → `low_coverage` 弃权，ensemble=model，仓位系数 1.0）+ 已覆盖维重归一（`Σ(raw×w)/Σw`）。生产最常见场景（仅 technical 有数据）从"恒 conflict −8 分 + 仓位 0.5"变为"弃权不干预"。`confidence_to_weight_factor` 增 `low_coverage→1.00`，其余档不变。遗留课题：confidence 分档回测次日收益
- **复盘系统已上线**：`scripts/postmortem.py`（add/backfill/stats/similar/candidates 五命令）+ 自动化 ID `52413995-57ab-46c9-aaec-20ee5995d794`（周一~五 19:35，周五出周命中率）。规则：LLM 只填 thesis/key_factors/prediction/missed_risk；verdict 与收益程序按真实 K 线写入；**周命中率 ≤50% 熔断（退出码 1）**；月度蒸馏 = bad 笔记标签聚类 → 候选规则 → 历史回测 → 审批。笔记库 `data/cache/postmortem_notes.db`，运维说明 `docs/自动化任务_尾盘选股推送.md` 第 8 节
- **验证基线更新：unittest 422 passed + 门禁 9/0**

## 2026-09-19 方案G 已落地（权重三层 + 新因子，全部验证通过）★当前生效配置

- **生效权重（short，Σ=1.00）**：hot_theme .55 / **liq_dev .14** / reversal_20d .10 / **vol_dev .07** / volatility .06 / momentum .02 / technical .02 / volume_price .02 / dragon_tiger .01 / capital_flow .01（north_flow/size/liquidity=0）。三层（v1.json > config.yml > DEFAULT_WEIGHTS）已同步，报告 `docs/因子扩容与权重论证_20260918.md`
- **口径已锁定**：用户确认实盘=**尾盘买入** → ret_hold1d 是唯一正确口径；09-18 基于 ret_intraday 的"hot/rev 等权基线最优"结论已作废
- **寻优确认**：训练/持有切分（≤2025-08 寻优 / ≥2025-09 验证）+ 500 组 Dirichlet + 坐标精修，训练期最优解持有期反而低于 G（−0.014 pp/日，过拟合特征）→ G 在持有期前沿；寻优解 liq_dev .14 / vol_dev .07 与 G 独立收敛到同一值
- **新因子实现**：`rank_stocks` 写 `_liq_dev_raw`（log(amount_t)−60日中位数）/ `_vol_dev_raw`（vol20−60日中位数）→ 横截面百分位；factor_library `(1−pct)×100`（缩量/波动收敛=高分），缺失→中性 50；oos_validator K_FACTORS 已注册（liq_dev IC +0.0538 / vol_dev IC +0.0268 正式复验）
- **liquidity 方向未翻转、权重恒 0**：OOS 证据属 log(amount) 口径，生产 liquidity 是 volume_ratio×turnover 口径，不可套用——勿再提议"修正 liquidity 方向"
- **验证基线更新**：unittest **404 passed**（无 pytest，用 `python -m unittest discover -s tests -q`）+ 门禁 9/0
- **ExpertEnsemble 已落地但实效存疑**：生产每日运行（daily_job 日志有"ensemble 完成"），但 expert 恒≈48-50 vs model≈74-75 恒冲突——专家 5 维因数据缺失退化为中性 50，"失明专家"系统性压低高分票仓位。待验证课题：按 confidence 分档回测次日收益；若冲突档不更差 → 改"专家维度覆盖率≥阈值才参与融合"
- **多 AI 辩论机制：项目内不存在**（出处=外部参考文档对 TradingAgents-CN 的架构分析），决策=不引入运行时 LLM 辩论（不可回测/成本延迟/形态不匹配）；复盘知识闭环设计在报告第九章（结构化落库+预测对账+月度蒸馏，命中率≤50% 熔断）

## 2026-09-19 因子扩容实证（ret_hold1d 口径，647 天 / 5225 只）

- **不要用历史 K 线以外的数据"等积累"**：kline_cache（2024-01 起，OHLCV+amount）足以验证波动率/流动性类因子，不必等落库数据
- **分解法证伪规模混淆**：`−pct(log amount)` 与 60 日滚动中位数（规模代理）相关 **0.838** → 84% 是小市值效应。真正有效的是剥离后的**偏离成分** `liq_dev = log(amount_t) − median(log amount,60)` 取反：IC 0.0654 / **ICIR 0.535** / t 12.81，与规模正交（−0.088）。`amihud20` 与规模代理相关 0.900 → 只是规模化身，不启用。波动率同理：`vol_dev`(ICIR 0.225) > `vol_level`(0.126)
- **权重判据不能用 IC**：等权 4 腿 IC 最高（0.0743）但 Top5 超额仅 0.13%/日；纯 hot_theme IC 最低（0.0284）却 1.57%/日。稀疏二值信号尾部置信度高，连续因子极端尾部是噪声 → **必须以 Top-N 尾部收益为判据**
- **hot_stocks 无前视偏差但 86.4% 买不进**：热点股当日已涨 +10.50%，其中 86.4% 接近涨停；可买子集 T+1 仍 +1.72%（全市场 −0.23%）。每日可买热点池仅约 11 只
- **`ret_hold1d` 单位是百分数**（`*100`，见 oos_validator `_compute_forward_returns`），中位数 −0.31% = 每日成本。做收益/回撤统计必须 /100；IC 是秩相关不受影响
- 分析脚本证据链（2026-09-19 目录清理后）：`archive/analysis_202609/scripts/`（_tmp_liq_decomp / _tmp_weight_sim / _tmp_robust / _tmp_scheme_g / _tmp_weight_opt）与 `archive/analysis_202609/reports/`；面板缓存 `data/cache/_tmp_panel_2023-10_2026-09.pkl`（361MB）保留——`recalibrate_thresholds.py --oos-proxy` 依赖它，门槛重校完成后可删（重建约 4 分钟）

## 项目定位

本目录是 `stock-picker`（原项目，路径 `C:\Users\Administrator\Documents\stock-picker`）的**实验副本**，用于模型有效性优化。

- 原项目 = 基线，**保持不动**。
- 副本 = 所有优化改动的落点，可自由修改、回测、实验。

## 副本创建信息

- 创建时间：2026-09-03 17:45
- 方式：`cp -a stock-picker stock-picker-v2`（全量复制，含 .git / data 缓存 / 权重）
- 副本已清空原项目继承的工作日志（`.workbuddy/memory/2026-09-03.md`）

## 优化方向（执行状态：全部完成 2026-09-05）

1. **验证方法修复** ✅ — 新增 `core/oos_validator.py`（walk-forward / 时间切分 OOS IC / 三种收益惯例），`scripts/run_backtest.py` 加 `--mode oos`，`core/backtest_engine.py` 修复 capital_flow 静默丢弃 bug
2. **权重对齐真实 IC** ✅ — 新增 `scripts/calibrate_weights.py`（共识+一致+可靠折扣+分级负向惩罚），`data/weights/v1.json` 已应用 OOS 校准权重
3. **TDX 补估值/基本面 + 事件催化** ✅ — 新增 `core/fundamental_provider.py` / `core/event_provider.py` / `scripts/prefetch_tdx.py`，`core/factor_library.py` 加新因子，`core/scoring_model.py` 加中性检测
4. **TdxStockHunter 专家 5 维 ensemble** ✅ — 新增 `core/expert_ensemble.py`（5 维独立评分 + 4 档 confidence + 融合），集成进 `strategies/short_term.py` 与 `core/portfolio_optimizer.py`

详见 `2026-09-05.md` 工作日志。

## 关键事实（沿用原项目）

- Python：`C:\Users\Administrator\AppData\Local\Programs\Python\Python311\python.exe`（pandas/numpy 只装在这里；**该解释器无 pytest**，测试用 `python -m unittest discover -s tests -p "test_*.py"`）
- ASHareHub Key 有效（52 字符）。注意：**agent 非交互 shell 不加载 ~/.bashrc**，进程内 key 为空会导致数据源 401 降级；2026-09-05 已重新写入 Windows 用户环境变量（HKCU\Environment，需重启 WorkBuddy 生效），agent 兜底命令：`export ASHAREHUB_API_KEY=$(grep -oP 'ASHAREHUB_API_KEY="\K[^"]+' ~/.bashrc)`
- 权重从 `data/weights/v1.json` 加载（存在时 config.yml 权重被忽略）

## 工作区未追踪文件 —— 切勿用 git diff 判断改动（2026-09-17 踩过两次）

`scripts/calibrate_weights.py` 等多份文件是 **untracked（`git status` 显示 `??`）**，
`git diff` 对它们**永远返回空**。2026-09-17 我据此两次误判"某 worker 完全没落地"，
实际代码早已改好（第一次因此白写了更正、第二次误报）。

**规则**：判断本仓库某文件是否被改动，**必须读文件内容**（Read/Grep）或看 mtime，
**不要依赖 `git diff` / `git status`**。`git status --short` 里 `??` 即为未追踪。


## agent 环境坑（2026-09-17 记录）

- 本机 bash 的 **`find` / `head` / `grep` 被 Windows 同名程序劫持**（报 `FIND: 参数格式不正确`）；`gh` CLI 未安装；PATH 初始异常需先 `export PATH="/c/Windows/System32:/c/Windows:/usr/bin:/bin:/mingw64/bin:$PATH"`。
  → 文件遍历/搜索**直接用 Read/Glob/Grep 工具**或 Python 脚本；需要 GitHub 数据时用 `curl api.github.com` 或 `git clone --depth 1`。

## 数据源可用性（2026-09-06 实测）

- **腾讯 qt.gtimg.cn 不支持北交所**：`bj_` 前缀返回 `v_pv_none_match`；新浪 bj_ 前缀也返回空 → 系统北交所覆盖缺口无法靠补前缀修复，必须换源
- **东财行情可用域**：`push2delay.eastmoney.com`（延迟行情，含北交所全市场）；`push2.eastmoney.com` 主域直连被拒（连接被断），勿用
- 北交所全量清单：bse.cn `nqxxController/nqxxCnzq.do`（POST，返回体带 `null(...)` 前缀需清洗）
- 涨跌家数口径：全市场（含北交所）与行情软件一致；系统当前仅沪深（2249/2771 vs 实际 2444/2914，差值=北交所 195/141）
- 本机代理环境变量会干扰直连，Python requests 需 `Session().trust_env = False`

## 微信推送与定时选股（2026-09-05 确立）

- 用户定位：**不对接实盘、不自动下单**，尾盘筛选 + 推送，买入手动执行
- 微信输出通道 = **ClawBot 主动推送，且只写在自动化提示词里**（2026-09-07 17:05 定案）：提示词内直接调 `python C:/Users/Administrator/.workbuddy/skills/wechat-clawbot-push/scripts/send_wechat.py send [--file 简报|"文本"]`，凭据自动从 `~/.workbuddy/settings.json` 读。5 条自动化的完整提示词见 `docs/自动化任务_尾盘选股推送.md` 第六章
- **推送不得进业务代码**（用户明确要求，考虑未来迁移到别的 harness）：曾短暂新增 `scripts/push_wechat.py` 并在 `daily_job.py` 内嵌推送，已全部撤除；`daily_job.py` 保持纯调度、退出码语义不变。换 harness 时只改提示词里那一行推送命令
- **推送可观测性（2026-09-14 新增）**：ClawBot 通道**不进宿主 `automation_delivery_outbox`**，唯一持久依据是 `~/.clawbot/delivery_log.jsonl`（send_wechat.py 每次发送追加一行，含 ok/tag/error）。查健康度：`send_wechat.py log --days 7`（有失败退出码 1）。推送命令带 `--tag`（eod/t1/health/oos/calib）便于按任务核对；周检提示词已含该核对步骤
- **推送失败典型错误**：`ilink ret=-2 errmsg=prepare failed` = 推送会话失效（长期无交互），需用户在微信给 ClawBot 发一条消息激活；脚本内置"缓存令牌→空串→getupdates harvest"三级重试
- **ClawBot 会话窗口 ≈24h，锚定"用户最后一次 inbound"（2026-09-15 标定完成）**：窗口长度落在 (21.5h, 28.0h)；**bot→user 发送不延窗，已实测证伪**（09-14 23:04 成功发送后仅 15.8h 即 `ret=-2 prepare failed`）。机制来自微信平台对 ClawBot 的保活限制，非本系统缺陷。→ **"由 bot 定时推送保活"在机制上不可行**；`getupdates` 只能 harvest 用户入向消息令牌，无法自行激活。防断三层现为：① 原生「推送到小程序」兜底（不受窗口约束，报告不丢）；② 保活推送**降级为探针**（仅用于提早发现断窗）；③ 失败即提示用户"发一条消息激活"。彻底解决只剩换无窗口通道（企业微信/第三方/邮件）或用户每日一次 inbound
- 次要通道 = WorkBuddy 原生「推送到小程序」+「微信助理集成」（扫码绑定）
- 尾盘选股入口：`python scripts/eod_stock_picker.py --mode short`（约 5-8 分钟，含当日防重落库）
- 定时任务配置文档：`docs/自动化任务_尾盘选股推送.md`（周一~五 14:50；automation_update 工具在部分会话不可用，需用户在自动化界面创建；权限模式必须选"完全访问"）

## 零推荐展示（2026-09-14 确立）

- **门槛过滤清零**（市况偏弱→动态门槛上浮）时：简报/日报在"无评分达标标的"之外，**展示当日最高分单个标的及其分数与距门槛差值**，并标注"未达标不作为推荐"。实现：`rank_stocks(diagnostics=)` 外送 `top_unqualified` → `short_term` 返回元信息条目（`no_qualified`，无 `code`）→ 简报/日报单独渲染；该条目**不落库、不触发优化器**
- **极差市硬停推分支刻意不展示最高分标的**（用户 2026-09-14 明确决策）：该分支在评分前 return 是为省掉全市场详评成本，不应为展示再加跑一遍评分。**勿再提议补这个功能**
- 契约：元信息条目一律**无 `code`**，下游据此区分"真实推荐"与"元信息"（`reports/market_briefing.py` 筛选用 `r.get('code')`）

## 权重三层一致性（2026-09-14 修复并锁死）

- 加载优先级 **`data/weights/v1.json` > `config.yml` 传入 > `ScoringModel.DEFAULT_WEIGHTS`**，三层必须**语义一致**（缺失键视为 0、容差 1e-9）
- 2026-09-14 发现 config.yml 中间层仍停留在"资金权重时代"（capital_flow 0.3036 / technical 0.0619 / volume_price 0.062），v1.json 一旦丢失会静默回落、与动态门槛 65/70/75 错配 → 已同步为 v1.json 值（Σ=1.00）
- 同时修掉 `scoring_model.py` 的恒真假阳性告警（原 `weights != loaded` 是扁平 dict vs `{short,long}` 嵌套比较）：现 `_weights_equivalent()` 只在**真不一致**时告警 → 该告警若出现即为"需同步 config.yml"的信号
- **回归测试锁死**：`tests/test_weight_alignment_20260914.py`（11 用例）
- 改权重时的硬规则：**必须改 `data/weights/v1.json`**，并同步 config.yml 兜底段
- **零推荐/停推 ≠ 故障**：2026-09-08~09-11 连续 4 个交易日无预测落库，已核实是市场原因（09-10、09-11 为「极差市停推」，09-08/09/14 为门槛过滤零达标），14:50 自动化在此期间每天正常执行（automation_runs 全 ACCEPTED、退出码 0）。**勿再把"predictions 无新批次"当成静默失败去排查**
- **周检 A 指标缺陷（2026-09-14 已改提示词版，待用户粘贴到自动化）**：原用「predictions 近 7 日有记录天数 < 3」判"疑似静默失败"，但零推荐日不落库 → 弱市周必然误报（09-13 周检即误报）。已改为按**简报文件存在性**判定（A/P/N + 回填 B/C），`docs/自动化任务_尾盘选股推送.md` 6.3③ / 6.5 变更记录为准

## 2026-09-16 修复确立的硬契约（改代码前必读）

**1. 非有限值 ≡ 缺失（三层一致）**
`None` / `NaN` / `±Inf` / 非数值 在「写缓存 → 读缓存 → 消费端」三处必须同义。
- 消费端一律用 `math.isfinite` 判断，**不要用 `isinstance(v, float)`**（NaN 就是 float，会漏）
- live 出口统一走 `sanitize_nan`（`data_engine.get_technical_factors_asharehub`），与缓存读形态等价
- `dict.get(key, default)` 的默认值**只在键缺失时生效**，值为 null 时无效 —— 这是当日崩溃根因
- 锁：`tests/test_null_factor_roundtrip_20260916.py`

**2. 仓位约束优先级：单票上限(硬) > 合计 ≤100%(硬) > 单票下限(软)**
- `PortfolioOptimizer.allocate` **绝不做后置归一化**（`pct/total*100` 会抹掉 MAX_ALLOCATION，n≤2 必突破）
- 输出含 `allocation_pct`（≤40%）+ `cash_pct`（不足 100% 的部分留现金，不回补）
- 出口有两条不变量断言；`_equal_weight` 同样受上限约束
- 回测不受影响（`_simulate_portfolio` 按 `alloc/total_alloc` 归一化 → 尺度不变），已由 `tests/test_portfolio_sim_scale_invariance_20260916.py` 证明
- 锁：`tests/test_allocation_cap_20260916.py`

**3. 简报必须使用运行期数据源快照**
`generate_market_briefing(..., source_status=...)` 由 `eod_stock_picker` 传入
`recommendations[0]['data_source_status']`。**禁止**在简报内新建 `DataEngine()` 取状态
（新实例 `_source_status` 恒全绿 → 结构性看不见运行期熔断）。
降级时首屏输出警示块 + 受影响因子权重合计；新增数据源需在 `_SOURCE_FACTOR_IMPACT` 登记。

**4. 缺失数据一律 None，不得回落 0**
`row.get('涨幅%', 0)` 这类写法会把"无数据"伪装成"零值"（当日题材热度整列 +0.0%）。
第三方接口须加字段存在性检测并 `logger.warning`，让漂移"响一声"。

**5. 预取写入拒收壳记录**
`_write_asharehub_prefetch('tech_factors', ...)` 会拒收 MACD/RSI/CCI 全空的记录（返回 False）。
调用方按返回值计数（`prefetch_asharehub.py` 的 `stats['reject']`），>20% 即需关注上游质量。

**6. 调度器不自动重试**
`daily_job._run` 失败即返回非 0，**任何情况下不重跑已启动的子进程**（日志写失败也不重跑）。
同一配额日内重试会挤占 eod 自身配额 —— 2026-09-16 降级事故的成因。
运行日志：`data/logs/daily_job_YYYYMMDD.log`（保留 30 天）；排查故障**先看这个文件**。
配额预检只告警不阻断（阻断会导致推送昨日简报）。

**7. 口径与时间**
- 配额告警文案 = "本地安全闸门(90/100，预留 10 次)"，**不是**服务端 100 次限流
- `predictions.created_at` 新写入为北京时间；**存量行仍是 UTC**（未回填），做历史取证时注意
- 幂等已由 `PredictionTracker.has_predictions(date, mode)` + 唯一索引 `(date,code,mode)` 实现，勿重复造

**现状基线（2026-09-16）**：单元测试 **177 passed**；门禁 `scripts/evaluate_all.py` **9 通过 0 失败**

## 权重 v2（2026-09-18 审批生效）

- 当前生效权重（三层已同步：v1.json / config.yml / DEFAULT_WEIGHTS，Σ=1.00）：hot_theme 0.42 / reversal_20d 0.42 / capital_flow 0.05 / technical 0.03 / volume_price 0.03 / momentum 0.03 / dragon_tiger 0.02 / north_flow 0.00 / size 0.00（链路先行）
- 依据：2.2 年 OOS + IC 衰减分析（hot_theme 衰减中、reversal_20d 上升、capital_flow 剧烈衰减）
- 效果（回测引擎修复后真实数据）：选股质量大幅改善（胜率 25→46.2%，单笔均值 −2.54→−0.21%）；组合收益仍负（−61%）——毛收益≈0，亏损≈双边成本+满仓暴露负期望。**下一瓶颈：出场规则/择时/成本，不是排序**
- 旧权重存档：`data/weights/v1_old_20260918.json`（可复现旧基线）；门禁权重检查已改为值无关断言（改权重无需同步改门禁）
- 回测引擎严重 bug 已修（09-18）：策略初始化块误缩进进 except 体内 → strategy 永不初始化 → 0 交易假象；run_backtest `ablated` 未初始化崩溃。09-17 晚间所有"基线"日志无效

## 2026-09-18 契约与设施增补（改回测/仓位相关代码前必读）

**1. 组合模拟有两个仓位口径（`backtest.sizing_mode`）**
- `normalized`（默认）：`allocated = base_equity × alloc/Σalloc` —— **尺度不变**（单测锁定），只衡量选股质量；**任何仓位/sizing 类改动在此口径下不可验证**（实测均匀缩放 ×0.5 结果完全相同）。
- `absolute`（新增）：`allocated = base_equity × alloc% × cap_scale`，未投出留现金；**超配（Σ>100%）按比例收缩而非顺序截断**（顺序截断会让结果依赖 pending_buys 排序 → 有单测锁顺序无关性）。仓位机制（弱市压缩/波动保险丝/连亏压缩/拥挤度分档）只能在此口径下 A/B。

**2. 现金泄漏 bug（2026-09-18 修复，历史净值因此偏低）**
`_simulate_portfolio` 买入段原为「先 `cash -= allocated` → 若持仓期内重复推荐则 `continue`」→ **仓位未建、现金已扣**（每次 ≈ 单笔仓位权益）。触发次数：8月 absolute 2 次、长窗口 3 次、部分运行 1 次。**凡触发过重复跳过的历史回测净值均不可信**（含断路器旧对照 −61.20%、size A/B −44.59%）。回归测试：`tests/test_sizing_mode_20260918.py::TestDuplicateBuyCashLeak`。

**3. 修复后可信基线（长窗口 2026-04-01~08-29，short）**
- normalized：−18.28%（回撤 −26.02%）
- absolute：−16.21%（回撤 −26.60%）
- 拥挤度断路器（唯一部署改动）：无断路器 −23.51%（回撤 −32.91%）vs 开启 −18.28%（回撤 −26.02%）→ **真实幅度 +5.2pp 收益 / 改善 6.9pp 回撤**
- 8月窗口：normalized +3.23%（回撤 −8.97%）/ absolute +6.25%（回撤 −6.81%）

**4. P5 数据线已上线（每日自动）**
`scripts/snapshot_valuation_daily.py` → `factor_daily.db::valuation_snapshot`（腾讯行情全市场 PE/PB/总市值/流通市值，零 ASHareHub 配额，幂等）。`daily_job.py` 每日追加执行（开关 `maintenance.daily_valuation_snapshot`，默认开；失败不覆盖主任务退出码）。缺口槛：**累计 ≥60 交易日后**可对 size（精确市值）/PE/PB 跑 OOS。注意：PE≤0（亏损股约 30%）、PB≤0 属业务语义，**不算字段缺失**（脚本已区分统计）。

**5. 新增回测 A/B CLI（均不改 config）**
`--ablate` / `--sell-tp|--sell-sl|--sell-days|--sell-stop-mode` / `--crowding-avg-pct` / `--crowding-scale` / `--max-close-pos` / `--weight-override FACTOR=VALUE` / `--sizing-mode` / `--position-scaling on|off`（总开关：同时关闭弱市压缩+波动保险丝+连亏压缩+分档）

**6. config 新增/参数化**
`buy.crowding_avg_pct: 2.0`（已部署）、`buy.crowding_scale_levels: []`（默认关）、`buy.max_close_pos: 0`（默认关）、`buy.weak_market_scale: 0.5`（由硬编码改为可配）

**用户风险偏好（2026-09-18 明确）**：风险优先——即使回测显示放宽止盈（+2%→+5%，两窗口 +3.5~6.9pp）能改善组合收益，用户选择**维持止盈 2% 以减少风险**。**不要提议放宽止盈/止损半径**（如 sl −3%）来换取收益。
