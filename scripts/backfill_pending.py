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

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

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
    try:
        backfill_pending_outcomes()
        logger.info("凌晨回填完成")
    except Exception as e:
        logger.error(f"凌晨回填失败: {e}")
        sys.exit(1)
