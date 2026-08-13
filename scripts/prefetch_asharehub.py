"""非交易日 AShareHub 配额预取脚本

背景：ASHareHub 日配额 100 次/天，非交易日（周末/节假日）cron 不跑，配额闲置。
本脚本在非交易日运行，用闲置配额预取低频数据（technical_factors / concepts /
financial_indicators）到本地缓存，交易日详评时 data_engine 先读缓存、命中不烧配额。

预算分配（100 次/日，预留 10 次给手动验证/其他）：
  - technical_factors: 40 次（短线双源校验，最优先）
  - concepts:          30 次（hot_theme 增强）
  - financial_indicators: 20 次（长线基本面）

股票池：从 predictions.db 取历史推荐/候选股（近 90 天去重），这些才是交易日
详评阶段真正会调 AShareHub 的股票。补充：最近推荐的活跃股票优先。

用法：
  source ~/.bashrc   # 加载 ASHAREHUB_API_KEY
  python scripts/prefetch_asharehub.py [--limit N] [--dry-run]

退出码：0=成功，1=失败（cron 可感知）
"""
import argparse
import logging
import os
import re
import subprocess
import sys
from datetime import datetime

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
logger = logging.getLogger('prefetch_asharehub')


def _ensure_python311():
    """cron no_agent 脚本用 gateway 的 venv Python 跑（无 pandas），
    检测到 pandas 缺失时 re-exec 自己到项目全局 Python311（有 pandas/numpy）。
    否则 `from core.data_engine import DataEngine` 在 data_engine.py:29
    `import pandas as pd` 处直接 ModuleNotFoundError。
    """
    try:
        import pandas  # noqa: F401
        return  # 当前解释器有 pandas，直接用
    except ImportError:
        pass
    py311 = r'C:\Users\Administrator\AppData\Local\Programs\Python\Python311\python.exe'
    if os.path.exists(py311) and os.path.abspath(sys.executable) != os.path.abspath(py311):
        logger.warning(f"当前解释器无 pandas，re-exec 到 {py311}")
        rc = subprocess.call([py311] + sys.argv)
        sys.exit(rc)
    # 兜底：找不到 Python311 就继续，让后续 import 报错自然失败


_ensure_python311()

# cron 副本位于 ~/.hermes/scripts/，__file__ 推导会指向 hermes 目录而非项目。
# 固定用绝对项目路径（本机部署路径唯一）。
PROJECT_ROOT = r'C:\Users\Administrator\Documents\stock-picker'
if not os.path.isdir(os.path.join(PROJECT_ROOT, 'core')):
    # 项目内直接运行时（scripts/ 子目录），推导回项目根
    PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)

from core.data_engine import DataEngine


def load_api_key() -> str:
    """加载 ASHAREHUB_API_KEY：环境变量优先，否则从 ~/.bashrc 解析
    （cron 环境不 source bashrc，key 只存在于 bashrc 里）
    """
    key = os.environ.get('ASHAREHUB_API_KEY', '')
    if key:
        return key
    # Windows Python 的 expanduser 展开成 C:\Users\...，git-bash 的 ~ 是 /c/Users/...
    # 两个路径都试
    candidates = [
        os.path.expanduser('~/.bashrc'),
        '/c/Users/Administrator/.bashrc',
        os.path.expanduser('~') + '/.bashrc',
    ]
    for bashrc in candidates:
        try:
            with open(bashrc, encoding='utf-8') as f:
                for line in f:
                    m = re.match(r'\s*export\s+ASHAREHUB_API_KEY\s*[=:]\s*["\']?([^"\'\s]+)', line)
                    if m:
                        return m.group(1)
        except Exception:
            continue
    logger.warning("解析 ~/.bashrc 失败（多个候选路径都不可读）")
    return ''


def load_stock_pool(limit: int = 100) -> list:
    """从 predictions.db 取近 90 天推荐/候选股（去重），这些最可能在交易日详评被调用"""
    import sqlite3
    db_path = os.path.join(PROJECT_ROOT, 'data', 'db', 'predictions.db')
    codes = []
    try:
        conn = sqlite3.connect(db_path)
        rows = conn.execute(
            "SELECT code FROM predictions WHERE date >= date('now', '-90 day') "
            "ORDER BY date DESC LIMIT 500"
        ).fetchall()
        conn.close()
        seen = set()
        for (code,) in rows:
            c = str(code).zfill(6)
            if c not in seen:
                seen.add(c)
                codes.append(c)
    except Exception as e:
        logger.warning(f"读取 predictions.db 失败（用空池）: {e}")
    return codes[:limit]


def main():
    parser = argparse.ArgumentParser(description='非交易日 AShareHub 配额预取')
    parser.add_argument('--limit', type=int, default=30,
                        help='本批预取股票数（默认 30：30 只 × 3 因子 = 90 次配额刚好用完）')
    parser.add_argument('--offset', type=int, default=None,
                        help='股票池偏移量。不传时自动推断：周六=0（第1批）、周日=30（第2批）。'
                             'cron 的 no_agent 脚本模式不支持传参，靠这个自动分流')
    parser.add_argument('--dry-run', action='store_true', help='只打印计划不执行')
    args = parser.parse_args()

    # cron 环境无 env 变量，从 ~/.bashrc 加载 key
    key = load_api_key()
    if not key:
        logger.error("ASHAREHUB_API_KEY 未找到（环境变量 + ~/.bashrc 都为空），退出")
        return 1
    os.environ['ASHAREHUB_API_KEY'] = key

    de = DataEngine(config={})
    # 预取模式：强制 live 拉取（绕过缓存读，否则命中上周缓存→写回旧数据→永不刷新）
    de._prefetch_mode = True
    # offset 自动推断：周六=0、周日=30（cron 无参跑，靠星期分流两批）
    if args.offset is None:
        wd = datetime.now().weekday()  # 5=周六, 6=周日
        args.offset = 30 if wd == 6 else 0
        logger.info(f"自动分流: 星期{wd+1}（{'周日' if wd == 6 else '周六/其他'}）→ offset={args.offset}")
    codes = load_stock_pool(args.limit + args.offset)
    codes = codes[args.offset:]
    if not codes:
        logger.info(f"股票池为空（offset={args.offset} 超出池大小 {len(load_stock_pool(1000))}），退出")
        return 0
    logger.info(f"本批股票 {len(codes)} 只（offset={args.offset}，limit={args.limit}）")
    # 每只股票 3 因子 = 3 次配额；预算：30 只 × 3 = 90（留 10 余量）
    logger.info(f"预计配额消耗: {len(codes) * 3} 次（上限 {de._asharehub_budget - 10}）")

    if args.dry_run:
        logger.info("[dry-run] 不实际调用")
        return 0

    # 先检查配额是否可消费（非消耗检查：读账本，不扣额度）
    # 注意：不能用 _asharehub_budget_ok()——它会消耗 1 次配额，导致预算实际 91>90
    import json as _json
    quota_used = 0
    try:
        with open(de._asharehub_quota_path, encoding='utf-8') as f:
            qd = _json.load(f)
            if qd.get('date') == datetime.now().strftime('%Y-%m-%d'):
                quota_used = int(qd.get('used', 0))
    except Exception as e:
        logger.warning(f"配额账本读取失败（按 0 处理）: {e}")
    if quota_used >= de._asharehub_budget - 10:
        logger.warning(f"ASHareHub 配额已被占用（{quota_used}/100）→ 跳过 ASHareHub 预取，仅预取东财")
        skip_asharehub = True
    else:
        skip_asharehub = False

    # 预取阶段 1：ASHareHub 三因子（烧配额，每只 3 次）
    # 配额不够时跳过（东财不限配额，仍继续）
    stats = {'tech': 0, 'concepts': 0, 'financial': 0, 'miss': 0, 'fail': 0, 'blocks': 0, 'dragon_tiger': 0}
    for i, code in enumerate(codes):
        if not skip_asharehub:
            data = de.get_technical_factors_asharehub(code)
            if data is not None:
                de._write_asharehub_prefetch('tech_factors', code, data)
                stats['tech'] += 1
            else:
                stats['miss'] += 1

            data = de.get_concept_members(code)
            if data is not None:
                de._write_asharehub_prefetch('concepts', code, {'names': data})
                stats['concepts'] += 1
            else:
                stats['miss'] += 1

            data = de.get_financial_indicators(code)
            if data is not None:
                de._write_asharehub_prefetch('financial', code, data)
                stats['financial'] += 1
            else:
                stats['miss'] += 1
        else:
            stats['miss'] += 3  # asharehub 跳过，标记 3 次 miss

        # 预取阶段 2：东财板块归属 + 龙虎榜（不限配额，em_get 内置 0.5s 限流）
        # 板块归属短期稳定、龙虎榜是 T+1 数据，周末拉到的就是周五最新
        try:
            blocks = de.get_stock_blocks(code)
            if blocks and blocks.get('total', 0) > 0:
                de._write_eastmoney_prefetch('blocks', code, blocks)
                stats['blocks'] += 1
        except Exception as e:
            logger.warning(f"东财板块预取失败 {code}: {e}")

        try:
            dt = de.get_dragon_tiger(code)
            de._write_eastmoney_prefetch('dragon_tiger', code, dt)
            stats['dragon_tiger'] += 1
        except Exception as e:
            logger.warning(f"东财龙虎榜预取失败 {code}: {e}")

        if (i + 1) % 10 == 0:
            logger.info(f"进度 {i+1}/{len(codes)}: {stats}")

    logger.info(f"预取完成: {stats}")
    # 报告配额使用（仅 asharehub）
    used = de._asharehub_budget_used
    logger.info(f"当日ASHareHub配额已用: {used}/100（东财不限配额）")
    return 0


if __name__ == '__main__':
    sys.exit(main())
