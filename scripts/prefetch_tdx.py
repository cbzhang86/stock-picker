"""
TDX 数据灌入脚本 — 由 Agent 用通达信/问财 MCP 拉数后写入本地缓存

设计意图
--------
估值/基本面和事件数据来自通达信（tdx_quotes/tdx_indicator_select）
与问财（wenda_*）MCP 端点。Python 进程无法直接调 MCP，所以数据是
"Agent 取数 → 本脚本写入"模式：

    agent（调用 TDX MCP 批量取数）
        → 拼出 rows 列表
        → 调用本脚本的子命令写入 SQLite

用法
----
# 写入基本面（agent 应在调用前用 tdx_indicator_select 拼好 rows）
python scripts/prefetch_tdx.py fundamentals --rows '[{"code":"600183","pe":15.3,...}]'

# 从 JSON 文件批量写入
python scripts/prefetch_tdx.py fundamentals --file events.json

# 写入事件（可选 --classify-only 用标题关键词打标）
python scripts/prefetch_tdx.py events --rows '[{"code":"000001","date":"2026-09-03","title":"业绩预增公告"}]'

# 查看缓存覆盖
python scripts/prefetch_tdx.py status
"""

import argparse
import json
import logging
import os
import sys

if sys.platform == 'win32':
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..'))

logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')
logger = logging.getLogger(__name__)


def load_rows(args) -> list:
    """从 --rows JSON 字符串 或 --file JSON 文件 加载行"""
    if args.rows:
        return json.loads(args.rows)
    if args.file:
        with open(args.file, 'r', encoding='utf-8') as f:
            data = json.load(f)
            return data if isinstance(data, list) else data.get('rows', [])
    return []


def cmd_fundamentals(args):
    from core.fundamental_provider import FundamentalProvider
    rows = load_rows(args)
    if not rows:
        logger.error("未提供 rows 或 file")
        return
    n = FundamentalProvider().upsert(rows)
    logger.info(f"基本面已写入: {n} 条")


def cmd_events(args):
    from core.event_provider import EventProvider
    rows = load_rows(args)
    if not rows:
        logger.error("未提供 rows 或 file")
        return
    if args.classify_only:
        for r in rows:
            r['impact'] = EventProvider.classify_event(r.get('title', ''))
    n = EventProvider().upsert(rows)
    logger.info(f"事件已写入: {n} 条")


def cmd_status(args):
    from core.fundamental_provider import FundamentalProvider
    from core.event_provider import EventProvider
    f = FundamentalProvider().coverage()
    e = EventProvider().coverage()
    print("=== 通达信/问财数据缓存状态 ===\n")
    print(f"基本面 ({f.get('latest_update', 'N/A')} 更新): "
          f"覆盖 {f.get('total', 0)} 只 / 含 PE {f.get('with_pe', 0)} / "
          f"含 ROE {f.get('with_roe', 0)}")
    print(f"事件   ({e.get('latest_date', 'N/A')} 最新): "
          f"覆盖 {e.get('codes', 0)} 只 / {e.get('total_events', 0)} 条 "
          f"(正面 {e.get('positive', 0)} / 负面 {e.get('negative', 0)})")


def main():
    parser = argparse.ArgumentParser(description='TDX/问财数据预取写入')
    sub = parser.add_subparsers(dest='cmd', required=True)

    for name in ['fundamentals', 'events']:
        p = sub.add_parser(name)
        p.add_argument('--rows', help='JSON 字符串直接传 rows')
        p.add_argument('--file', help='JSON 文件路径')
    sub.add_parser('status')

    sub.choices_map = {'fundamentals': cmd_fundamentals, 'events': cmd_events,
                       'status': cmd_status}
    args = parser.parse_args()

    if args.cmd == 'events':
        args.classify_only = False  # 接受外部已打标的 impact
    sub.choices_map[args.cmd](args)


if __name__ == '__main__':
    main()