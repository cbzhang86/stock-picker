# -*- coding: utf-8 -*-
"""数值契约工具 —— 硬契约 A / B 的**唯一实现**（2026-09-23 新增）。

背景（审查报告 §2.1 A-3）
------------------------
契约 A（非有限值 ≡ 缺失）此前在项目里有 **12 个手写实现、4 种命名**
（`_finite`×5、`_as_float`×2、`_safe_float`/`_safe_float_opt`、`_is_finite_number`、
`_num`、`_f`×2），全部靠"各处手写 + 测试兜底"。最危险的形态是
`_safe_float`（缺失→0.0）与 `_safe_float_opt`（缺失→None）**只差后缀、静态不可区分**
—— 选错即静默违反契约 B（把"无数据"写成"0"）。

本模块提供三个语义明确的函数，各消费方以**同名薄包装**委托（保持 100+ 调用点不变），
并由守卫测试禁止再出现新的本地实现：

  · `is_finite(v)`        —— 是否为「可用数值」（唯一判据）
  · `opt_float(v)`        —— 缺失 → None（契约 B：字段缺失写 None 不写 0）
  · `float_or(v, default)`—— 缺失 → 显式默认值（仅在"默认值有业务含义"时使用）
  · `sanitize_nan(obj)`   —— 递归把 NaN/±Inf 归一为 None（序列化边界用）

实现要点：**不能用 `isinstance(v, float)`** —— NaN 本身是 float 会被漏判；
必须走 `math.isfinite(float(v))`。用 `numbers.Real`（而非 `(int,float)`）以覆盖
numpy 标量（`np.int64` 不是 `int`，会被白名单漏掉）。
"""

import math
import numbers
from typing import Any, Optional

__all__ = ['is_finite', 'opt_float', 'float_or', 'sanitize_nan']


def is_finite(v) -> bool:
    """非有限值 ≡ 缺失：None / NaN / ±Inf / 非数值 / bool 一律 False。

    bool 视为缺失（`True` 参与算术是几乎必然的语义错误）。
    """
    if v is None or isinstance(v, bool):
        return False
    try:
        return math.isfinite(float(v))
    except (TypeError, ValueError):
        return False


def opt_float(v) -> Optional[float]:
    """缺失 → None（契约 B）。**业务值为 0 / 负数一律保留**。

    用于"字段缺失必须与业务零值区分"的场景（如 PE/PB、换手率、市值）。
    """
    if v is None or isinstance(v, bool):
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def float_or(v, default: float = 0.0) -> float:
    """缺失 → `default`（显式默认值）。仅在默认值有明确业务含义时使用。

    注意：默认值**不是**"安全的兜底" —— 契约 B 要求缺失写 None。
    仅在调用方确实需要数值（如算术入参）且已声明默认语义时使用 `float_or`。
    """
    f = opt_float(v)
    return default if f is None else f


def sanitize_nan(obj: Any) -> Any:
    """递归清洗 NaN/Inf → None（JSON/SQLite 序列化边界的唯一入口）。

    配合 `json.dumps(..., allow_nan=False)` 形成双保险：清洗遗漏会直接抛错
    （写入失败进日志），而不是把 `NaN` 字面量写进缓存/库（非严格 JSON，
    读回后所有比较静默为 False，污染因子分）。

    **保留原语义**：bool 原样返回；`numbers.Real`（含 numpy 数值类型）经有限性判定；
    dict/list/tuple 递归；其余类型原样返回。
    """
    if isinstance(obj, bool):
        return obj
    if isinstance(obj, numbers.Real):
        f = float(obj)
        return f if math.isfinite(f) else None
    if isinstance(obj, dict):
        return {k: sanitize_nan(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [sanitize_nan(v) for v in obj]
    return obj
