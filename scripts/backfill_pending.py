"""
凌晨独立回填脚本 — T+1/T+5 结果回填（2026-08-15 从 14:45 主流程拆出）

背景：
  - 原逻辑在 eod_stock_picker.py 14:45 主流程第 4 步跑，qfq 走 baostock ~8s/条，
    待回填多时拖延报告生成（14:45 cron 贴收盘，越早出报告越好）
  - 回填与策略无依赖、时序无关（回填的是已到期 T+1/T+5，凌晨数据已完整）
  - 挪到凌晨 3:15 跑（collector 3:00 后），14:45 主流程不再做回填

用法：
  /c/Users/Administrator/AppData/Local/Programs/Python/Python311/python.exe scripts/backfill_pending.py
"""

import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
# 2026-09-22 修复（回归验证发现的潜在 bug）：原只把**项目根**加进 sys.path，
# 而 `eod_stock_picker.py` 位于 scripts/ —— 作为脚本执行（python scripts/xxx.py）
# 时 Python 会自动把脚本目录加进 path 所以能跑，但**作为模块 import**（如测试、
# 或被其他脚本 `import scripts.backfill_pending`）时 `from eod_stock_picker import …`
# 会 ModuleNotFoundError。现显式补上 scripts 目录。
sys.path.insert(0, os.path.join(_HERE, '..'))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

# 解决 Windows 控制台 GBK 编码问题
if sys.platform == 'win32':
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    sys.stderr.reconfigure(encoding='utf-8', errors='replace')

import logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s', datefmt='%H:%M:%S')
logger = logging.getLogger(__name__)

# 复用原回填函数（逻辑完全一致，只是调用时机从 14:45 挪到凌晨）
from eod_stock_picker import backfill_pending_outcomes


if __name__ == '__main__':
    # 2026-09-22 修复（缺陷排查 P2-27）：原实现无条件打印"凌晨回填完成"且
    # `main` 无返回值 —— `eod_stock_picker.backfill_pending_outcomes` 内部对每条
    # 失败只 `logger.warning` 不抛出，于是**上游整体不可用时每条都 warning、
    # 最终仍 rc=0**，pending 只增不减却无人发现。
    # 现让回填函数返回 (ok, fail)，fail>0 时以非 0 退出（自动化可感知）。
    failed = 0
    try:
        _r = backfill_pending_outcomes()
        if isinstance(_r, tuple) and len(_r) >= 2:
            _ok, failed = _r[0], _r[1]
            logger.info(f"凌晨回填完成：成功 {_ok} / 失败 {failed}")
        else:
            logger.info("凌晨回填完成")
    except Exception as e:
        logger.error(f"凌晨回填失败: {e}")
        sys.exit(1)
    if failed:
        logger.error(f"凌晨回填存在 {failed} 条失败（退出码 1，供自动化感知）")
        sys.exit(1)
