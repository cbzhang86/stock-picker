# -*- coding: utf-8 -*-
"""成交成本常量 —— 契约 O（OOS 口径与实盘一致）的**唯一来源**（2026-09-23 新增）。

背景（审查报告 §2.1 A-2）
------------------------
这 4 个常量此前在 **6 处重复定义**：
  · `core/oos_validator.py`（模块级、不读 config）
  · `config.yml` backtest 段（实盘权威）
  · `core/backtest_engine.py.__init__`（从 config 读，兜底同值）
  · `scripts/capacity_check.py`、`scripts/analyze_trade_decomposition.py`、
    `scripts/calibrate_slippage.py`
契约 O（OOS 与实盘/回测口径一致）此前完全靠"人肉保持 6 处相等 + 测试兜底"，
且**已经发生过真实事故**：P0-C 修复前 OOS 只扣佣金+滑点、漏印花税/过户费，
与回测引擎不一致（`oos_validator.py` 注释有记录）。

用法
----
  from core.costs import load_costs
  costs = load_costs()                 # 读 config.yml backtest 段
  costs = load_costs(config_dict)      # 或显式传入（测试/已加载 config 的调用方）
  COMMISSION, SLIPPAGE = costs['commission_rate'], costs['slippage']

单次收益成本 = 佣金(双向) + 滑点(双向) + 印花税(仅卖) + 过户费(双向)。
"""

import os
from typing import Dict, Optional

import yaml

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFIG_PATH = os.path.join(ROOT, 'config.yml')

# 兜底默认（与 config.yml backtest 段同值；仅当配置缺失/损坏时使用，
# 且 load_costs 会打 warning —— 保证"配置漂移"不静默）
DEFAULTS: Dict[str, float] = {
    'commission_rate': 0.0003,   # 佣金，双向
    'slippage': 0.001,           # 滑点，双向
    'stamp_duty': 0.0005,        # 印花税，仅卖出
    'transfer_fee': 0.00001,     # 过户费，买卖双向
}

_WARNED = set()


def load_costs(config: Optional[dict] = None,
               config_path: str = None) -> Dict[str, float]:
    """读取成交成本四常量（优先级：显式 config > config.yml > DEFAULTS）。

    返回 dict：{commission_rate, slippage, stamp_duty, transfer_fee}。
    同一 (来源, 键) 的缺失只告警一次，避免 6 个调用方刷屏。
    """
    src = None
    if config is not None:
        src = (config or {}).get('backtest', {}) or {}
        where = '显式 config'
    else:
        path = config_path or CONFIG_PATH
        try:
            with open(path, encoding='utf-8') as f:
                src = (yaml.safe_load(f) or {}).get('backtest', {}) or {}
            where = f'config.yml({os.path.basename(path)})'
        except Exception as e:
            src = {}
            where = f'config.yml 读取失败({type(e).__name__})'
    out = {}
    for k, dv in DEFAULTS.items():
        v = src.get(k, dv)
        if not isinstance(v, (int, float)) or isinstance(v, bool):
            v = dv
            key = (where, k)
            if key not in _WARNED:
                _WARNED.add(key)
                import logging
                logging.getLogger(__name__).warning(
                    f"成交成本 {k} 配置无效（{where}）→ 回落默认 {dv}")
        out[k] = float(v)
    return out
