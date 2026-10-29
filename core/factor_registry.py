# -*- coding: utf-8 -*-
"""因子注册表 —— 因子清单的**唯一来源**（2026-09-23 新增）。

背景（审查报告 §2.1 A-1）
------------------------
因子清单此前分散在 **≥5 处且互不一致**：`scoring_model.DEFAULT_WEIGHTS`(15)、
`config.yml`/`v1.json`(15)、`oos_validator.K_FACTORS+SNAPSHOT_FACTORS`(15 但含
`skip_momentum_1/3` 且缺 `size`)、`factor_diag.FACTORS`(12)、
`recalibrate_thresholds.PANEL_FACTORS`(9)。
**已经真实发生过后果**：`factor_diag` 漏掉方案 G 启用的 4 个因子（生产权重合计 0.27），
恒值检测打印"未检测到恒值因子"并 `rc=0` —— 给运维一个**假绿灯**。

本模块把清单集中到一处，各消费方**导入派生列表**（而不是各自手写），
并由 `tests/test_factor_registry_20260923.py` 断言各消费方 == 注册表派生结果。
新增因子的唯一动作：在本文件登记一行 + 在 `factor_library` 产出该因子。

分组说明（保持与既有行为**逐项一致**，顺序亦保持不变）
---------------------------------------------------
· `WEIGHT_KEYS`        —— 权重表键集（production；含 5 个显式 0 权重键）
· `OOS_KLINE_FACTORS`  —— OOS 面板可由 K 线自算的因子（含 2 个**仅实验**变体）
· `OOS_SNAPSHOT_FACTORS` —— OOS 需外部快照的因子
· `OOS_ONLY_FACTORS`   —— 仅存在于 OOS 面板的**实验派生列**（非生产因子！
   `calibrate_weights` 必须把它们排除出权重提案，否则写入真空额度）
· `DIAG_FACTORS`       —— factor_diag 恒值/相关诊断清单
· `PANEL_FACTORS`      —— recalibrate_thresholds 路径 B 的面板代理清单
· `SPARSE_FACTORS`     —— 天生稀疏（多数样本恒中性，豁免恒值告警）
"""

# ── 生产权重表键集（与 data/weights/v1.json 逐键一致；守卫测试锁定）──
WEIGHT_KEYS = (
    # 方案 G 非零权重
    'hot_theme', 'liq_dev', 'reversal_20d', 'vol_dev', 'volatility',
    'momentum', 'technical', 'volume_price', 'dragon_tiger', 'capital_flow',
    # 显式 0 权重（"链路先通、验证后再启用"）
    'north_flow',          # 2024-08 起北向官方停发
    'size',                # 等估值快照 ≥60 交易日
    'liquidity',           # 已证伪：84% 是规模效应（与 size 相关 0.838）
    'valuation_fundamental',
    'event_catalyst',
)

# ── OOS 验证清单（面板构造用；顺序即列顺序，勿随意调整）──
OOS_KLINE_FACTORS = (
    'momentum', 'technical', 'volume_price',
    'skip_momentum_1', 'skip_momentum_3',      # 仅实验：验证跳日动量
    'reversal_20d', 'volatility', 'liquidity',
    'liq_dev', 'vol_dev',
)
OOS_SNAPSHOT_FACTORS = (
    'capital_flow', 'hot_theme', 'dragon_tiger',
    'valuation_fundamental', 'event_catalyst',
)
ALL_OOS_FACTORS = OOS_KLINE_FACTORS + OOS_SNAPSHOT_FACTORS

# ── 仅 OOS 实验派生列（**非生产因子**，禁止进入权重提案）──
OOS_ONLY_FACTORS = ('skip_momentum_1', 'skip_momentum_3')

# ── 暂缓进入 OOS 面板的权重键（有登记、暂不验证，理由如下）──
DEFERRED_OOS_FACTORS = (
    'north_flow',   # 北向官方口径 2024-08 停发 → 无面板数据源可算 IC
    'size',         # 依赖 valuation_snapshot 历史积累（启用契约 ≥60 交易日）
)

# ── 诊断清单（factor_diag）—— 必须覆盖**全部**权重键，否则恒值检测会假绿灯 ──
DIAG_FACTORS = (
    'capital_flow', 'north_flow', 'momentum', 'technical', 'volume_price',
    'hot_theme', 'dragon_tiger', 'liq_dev', 'vol_dev', 'volatility',
    'reversal_20d', 'size', 'liquidity', 'valuation_fundamental',
    'event_catalyst',
)

# ── 面板代理清单（recalibrate_thresholds 路径 B）──
PANEL_FACTORS = (
    'hot_theme', 'liq_dev', 'reversal_20d', 'vol_dev', 'volatility',
    'momentum', 'technical', 'volume_price', 'dragon_tiger',
)

# ── 天生稀疏（恒值告警豁免）──
SPARSE_FACTORS = ('dragon_tiger', 'north_flow')


def production_factor_names() -> tuple:
    """注册表中的**生产因子**（排除仅 OOS 实验列）。

    含"暂缓进入 OOS"的键（north_flow/size）—— 它们是生产因子，
    只是没有可用的 OOS 面板数据源。
    """
    return tuple(f for f in OOS_KLINE_FACTORS + OOS_SNAPSHOT_FACTORS + DEFERRED_OOS_FACTORS
                 if f not in OOS_ONLY_FACTORS)


def check_consistency() -> list:
    """自检：返回不一致项清单（空 = 一致）。供门禁/守卫测试调用。

    校验：
      ① 注册表权重键集 == OOS 清单 ∪ 暂缓清单（双向）；
      ② 所有权重键都被诊断清单覆盖 —— **否则恒值检测会出现假绿灯**
         （2026-09-22 真实事故：factor_diag 漏 4 个启用因子，打印
          "未检测到恒值因子" 且 rc=0）；
      ③ 面板清单 ⊆ 诊断清单。
    """
    problems = []
    wk, prod = set(WEIGHT_KEYS), set(production_factor_names())
    if wk - prod:
        problems.append(f'权重键未登记进 OOS/暂缓清单: {sorted(wk - prod)}')
    if prod - wk:
        problems.append(f'OOS 清单含未登记权重键的因子: {sorted(prod - wk)}')
    missing_diag = wk - set(DIAG_FACTORS)
    if missing_diag:
        problems.append(f'权重键未被诊断清单覆盖（恒值检测会漏报）: {sorted(missing_diag)}')
    if not set(PANEL_FACTORS) <= set(DIAG_FACTORS):
        problems.append(f'面板清单不是诊断清单子集: '
                        f'{sorted(set(PANEL_FACTORS) - set(DIAG_FACTORS))}')
    return problems
