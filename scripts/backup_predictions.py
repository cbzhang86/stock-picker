# -*- coding: utf-8 -*-
"""predictions.db 备份（2026-09-18 审查 P1-3 修复）

背景：`data/db/predictions.db` 是实盘推荐 + outcomes 的**唯一真源**，此前无任何
备份机制（对比 `data/weights/v1.json` 有自动时间戳备份）。该库损坏会同时中断
feedback/optimizer 闭环与全部历史验证数据。

实现要点：
  - 用 **sqlite3 backup API**（不是文件拷贝）→ WAL 模式下也能得到一致快照；
  - 目标：`data/backup/predictions_YYYYMMDD.db`，同日重复执行覆盖（幂等）；
  - 保留最近 N 份（默认 14），更旧的自动清理；
  - 失败只 warning（不阻断主流程），退出码 0/1 供调度判断。

用法：
  python scripts/backup_predictions.py [--keep 14] [--src data/db/predictions.db]
"""
import argparse
import glob
import os
import re
import sqlite3
import sys
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_SRC = os.path.join(PROJECT_ROOT, 'data', 'db', 'predictions.db')
BACKUP_DIR = os.path.join(PROJECT_ROOT, 'data', 'backup')


def backup_db(src: str, backup_dir: str = None, keep: int = 14,
              date_str: str = None) -> str:
    """创建一致快照并返回备份路径。同日重复调用覆盖同一文件（幂等）。"""
    backup_dir = backup_dir or BACKUP_DIR
    os.makedirs(backup_dir, exist_ok=True)
    if not os.path.exists(src):
        raise FileNotFoundError(f"源库不存在: {src}")
    date_str = date_str or datetime.now().strftime('%Y%m%d')
    dst = os.path.join(backup_dir, f"predictions_{date_str}.db")

    # sqlite3 backup API：WAL 模式下安全（文件拷贝可能丢 WAL 未 checkpoint 的数据）
    src_conn = sqlite3.connect(src)
    try:
        dst_conn = sqlite3.connect(dst)
        try:
            src_conn.backup(dst_conn)
            dst_conn.commit()
        finally:
            dst_conn.close()
    finally:
        src_conn.close()
    return dst


def prune_backups(backup_dir: str = None, keep: int = 14) -> list:
    """只保留最近 keep 份（按日期文件名排序），返回被删除的文件列表。"""
    backup_dir = backup_dir or BACKUP_DIR
    files = sorted(glob.glob(os.path.join(backup_dir, 'predictions_*.db')))
    # 只处理符合命名规范的（predictions_YYYYMMDD.db），避免误删其他备份
    files = [f for f in files
             if re.search(r'predictions_(\d{8})\.db$', os.path.basename(f))]
    removed = []
    if keep > 0 and len(files) > keep:
        for f in files[:len(files) - keep]:
            try:
                os.remove(f)
                removed.append(f)
            except OSError:
                pass
    return removed


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument('--src', default=DEFAULT_SRC)
    ap.add_argument('--dir', default=None, help='备份目录（默认 data/backup）')
    ap.add_argument('--keep', type=int, default=14, help='保留份数（默认 14）')
    args = ap.parse_args()
    try:
        dst = backup_db(args.src, args.dir, args.keep)
        size = os.path.getsize(dst) / 1024
        removed = prune_backups(args.dir, args.keep)
        print(f"predictions.db 备份完成: {os.path.basename(dst)}（{size:.0f} KB）"
              + (f" | 清理旧备份 {len(removed)} 份" if removed else ""))
        return 0
    except Exception as e:
        print(f"⚠ 备份失败（不阻断主流程）: {type(e).__name__}: {str(e)[:120]}")
        return 1


if __name__ == '__main__':
    sys.exit(main())
