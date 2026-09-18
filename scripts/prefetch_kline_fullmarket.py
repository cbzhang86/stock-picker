# -*- coding: utf-8 -*-
"""全市场 K 线预取（修复 kline_cache 覆盖塌缩）

背景（2026-09-17 发现的验证基础设施缺陷）
------------------------------------------
`kline_cache` 的覆盖从 5 月的 4,587 只塌缩到 7-8 月的 ~999 只、9 月的 43 只。
原因：只有被 `DataEngine.get_kline` 调用过的票才入缓存，而实盘流程只对
预筛后的 ~200 只详评票调用；5 月的全市场覆盖来自某一次性的全量抓取。

后果：OOS 验证面板的 universe 剧烈漂移（4-6 月窗口 3000-4587 只 vs
7-9 月窗口 999→657 只）→ **跨窗口的因子 IC 不可比**，
"momentum 上窗口强有效 / 下窗口强反向"很可能是 universe 漂移的产物。

本脚本：用新浪财经接口把**全市场**日 K 写入 kline_cache（INSERT OR REPLACE），
恢复覆盖。可断点续传（跳过缓存中已有近期数据的票）。

用法：
    python scripts/prefetch_kline_fullmarket.py                # 全量（断点续传）
    python scripts/prefetch_kline_fullmarket.py --workers 4    # 控制并发
    python scripts/prefetch_kline_fullmarket.py --force        # 忽略续传，全部重抓

注意：
- 新浪接口返回**不复权**价格（与既有 kline_cache 一致）。复权修复是独立事项。
- 北交所（43/83/87/92 开头）新浪不支持，自动跳过（与既有覆盖口径一致）。
- 限流：单线程 ~0.2-2s/只，6 线程约 5-30 分钟跑完全市场；失败自动重试 1 次。
"""
import argparse
import logging
import os
import sqlite3
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s',
                    datefmt='%H:%M:%S')
logger = logging.getLogger('prefetch_kline')

KLINE_DB = os.path.join(ROOT, 'data', 'cache', 'kline_cache.db')
DATELEN = 600                    # ~2.4 年日K，一次拉够
FRESH_DAYS = 7                   # 缓存里最近 7 天内有数据的票视为"新鲜"，跳过
RECENT_MARK = None               # 运行时计算


def _sina_fetch(symbol: str) -> list:
    """新浪日K（与 DataEngine._fetch_kline_sina 同源，但 datalen=600、ma=no）"""
    import requests
    url = ("http://money.finance.sina.com.cn/quotes_service/api/json_v2.php/"
           "CN_MarketData.getKLineData")
    r = requests.get(url, params={"symbol": symbol, "scale": "240",
                                  "ma": "no", "datalen": str(DATELEN)},
                     headers={"User-Agent": "Mozilla/5.0 Chrome/131.0.0.0"},
                     timeout=15)
    if r.status_code != 200 or len(r.text) <= 50:
        return []
    import json
    data = json.loads(r.text)
    rows = []
    for d in data or []:
        try:
            vol = float(d.get('volume', 0))
            close = float(d.get('close', 0))
            # 新浪不提供成交额。volume 为股数，×收盘价 ≈ 当日成交额（元）。
            # VWAP 通常非常接近收盘价，误差 ~1-3%，足够流动性评分/滑点分档使用。
            # （2026-09-17 修复：此前 amount 置 0 → 初步评分的流动性分全灭，
            #   全部候选恒定 28.25 分、按代码序取前 200，候选池退化为字母序。）
            amt = vol * close if vol > 0 and close > 0 else 0.0
            rows.append((d.get('day', ''), float(d.get('open', 0)), float(d.get('high', 0)),
                         float(d.get('low', 0)), close, vol, amt))
        except (ValueError, TypeError):
            continue
    return rows


def _get_codes() -> list:
    """全市场代码清单：优先 akshare，失败回退 kline_cache 已有代码"""
    try:
        import akshare as ak
        df = ak.stock_info_a_code_name()
        codes = []
        for _, row in df.iterrows():
            code = str(row['code']).zfill(6)
            name = str(row.get('code_name', ''))
            if ('ST' in name or '退' in name):
                continue
            # 指数（39 开头，如 399001 深证成指 / 399300 沪深300）不是股票：
            # 其"volume"是指数成交量、close 是指数点位，× 出来的"成交额"会达到
            # 1e15 量级，严重污染流动性评分与滑点分档（2026-09-17 实测踩坑）
            if code.startswith(('39',)):
                continue
            if code.startswith(('43', '83', '87', '92')):   # 北交所，新浪不支持
                continue
            codes.append(code)
        logger.info(f"代码清单（akshare）: {len(codes)} 只")
        return codes
    except Exception as e:
        logger.warning(f"akshare 清单失败（{e}），回退 kline_cache 已有代码")
        conn = sqlite3.connect(KLINE_DB)
        try:
            rows = conn.execute("SELECT DISTINCT code FROM kline_cache").fetchall()
            return [r[0] for r in rows]
        finally:
            conn.close()


def _fresh_codes() -> set:
    """缓存里最近 FRESH_DAYS 天内有数据的代码（断点续传跳过用）"""
    conn = sqlite3.connect(KLINE_DB)
    try:
        rows = conn.execute(
            "SELECT DISTINCT code FROM kline_cache WHERE date >= ?",
            (RECENT_MARK,)).fetchall()
        return {r[0] for r in rows}
    finally:
        conn.close()


def _write_rows(code: str, rows: list) -> int:
    conn = sqlite3.connect(KLINE_DB)
    try:
        conn.executemany(
            "INSERT OR REPLACE INTO kline_cache "
            "(code,date,open,high,low,close,volume,amount) VALUES (?,?,?,?,?,?,?,?)",
            [(code, *r) for r in rows])
        conn.commit()
        return len(rows)
    finally:
        conn.close()


def _worker(code: str, results: dict, lock: threading.Lock):
    prefix = 'sh' if code.startswith(('6', '9')) else 'sz'
    rows = []
    for attempt in (1, 2):
        try:
            rows = _sina_fetch(f'{prefix}{code}')
            if rows:
                break
            time.sleep(0.5 * attempt)
        except Exception:
            if attempt == 2:
                break
            time.sleep(0.5 * attempt)
    n = _write_rows(code, rows) if rows else 0
    with lock:
        results['done'] += 1
        results['rows'] += n
        results['ok'] += 1 if rows else 0
        results['fail'] += 0 if rows else 1
        if results['done'] % 200 == 0:
            el = time.time() - results['t0']
            logger.info(f"进度 {results['done']}/{results['total']} "
                        f"({results['done']/results['total']*100:.0f}%) "
                        f"成功 {results['ok']} 失败 {results['fail']} "
                        f"累计 {results['rows']:,} 行 | {el:.0f}s "
                        f"| 预计剩余 {el/max(results['done'],1)*(results['total']-results['done']):.0f}s")


def main():
    global RECENT_MARK
    ap = argparse.ArgumentParser()
    ap.add_argument('--workers', type=int, default=6)
    ap.add_argument('--force', action='store_true', help='忽略断点续传，全部重抓')
    args = ap.parse_args()

    from datetime import datetime, timedelta
    RECENT_MARK = (datetime.now() - timedelta(days=FRESH_DAYS)).strftime('%Y-%m-%d')

    os.makedirs(os.path.dirname(KLINE_DB), exist_ok=True)
    codes = _get_codes()
    if not codes:
        logger.error("拿不到代码清单，退出")
        return 2
    skip = set() if args.force else _fresh_codes()
    todo = [c for c in codes if c not in skip]
    logger.info(f"全市场 {len(codes)} 只 | 新鲜跳过 {len(skip)} | 待抓取 {len(todo)}")

    if not todo:
        logger.info("无待抓取项，退出")
        return 0

    results = {'done': 0, 'rows': 0, 'ok': 0, 'fail': 0,
               'total': len(todo), 't0': time.time()}
    lock = threading.Lock()
    with ThreadPoolExecutor(max_workers=max(1, args.workers)) as pool:
        futs = [pool.submit(_worker, c, results, lock) for c in todo]
        for _ in as_completed(futs):
            pass

    el = time.time() - results['t0']
    logger.info(f"完成: {results['done']} 只 | 成功 {results['ok']} | "
                f"失败 {results['fail']} | 写入 {results['rows']:,} 行 | 耗时 {el:.0f}s")
    return 0 if results['fail'] == 0 else 1


if __name__ == '__main__':
    sys.exit(main())
