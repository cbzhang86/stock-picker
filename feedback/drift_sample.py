# -*- coding: utf-8 -*-
"""漂移监控的**日度样本数据线**（2026-09-23 交接单 D1 结构性解法）。

问题（D1）
---------
绩效漂移监控原先只统计 `predictions` 表中"有正式推荐且有 T+1"的交易日 →
零达标日（弱市/动态门槛上浮）用 `mode='shadow'` 落库或根本不落 → 样本只在
"策略发声"的日子增长。实测 2026-09 覆盖率仅 5/17 ≈ 29%，监控在最需要它的
行情里冻结（幸存者偏差）。

解法
----
按日记录一条**与是否推荐解耦**的样本：当日候选池 top-N（含未达标池）的
**次日收益均值**。零 ASHareHub 配额、幂等、可在 eod 收尾处调用。

三张表（都在 `data/cache/factor_daily.db`，`CREATE TABLE IF NOT EXISTS` 幂等）
--------------------------------------------------------------------------
· `candidate_daily(date, code, rank, score)` —— 每日候选池快照（谁被看过）
· `drift_daily_sample(date, n_cand, avg_t1_return, win_rate, rets, computed_at)`
  —— 每交易日一行的监控样本（`rets` 为该日逐笔 T+1 收益的 JSON，供 PSI 逐笔口径）

口径一致性（重要）
-----------------
T+1 收益 = `(close(D+1) − close(D)) / close(D) × 100`，与 `outcomes.t1_return`
的口径一致（tracker 用决策日收盘作买入基准 → 次日收盘）。数据源为本地
`kline_cache.db`（既有缓存，**零配额、零联网**）。
本表是**监控样本**，不用于交易绩效结算（结算仍以 predictions/outcomes 为准）。

用法
----
  from feedback.drift_sample import record_candidate_pool, backfill_daily_samples
  record_candidate_pool('2026-09-23', pool)   # eod 收尾：落当日候选池
  backfill_daily_samples()                    # 幂等：给已有池、缺样本的日期补 T+1
"""
import json
import logging
import os
import sqlite3
from datetime import datetime
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FACTOR_DB = os.path.join(ROOT, 'data', 'cache', 'factor_daily.db')
KLINE_DB = os.path.join(ROOT, 'data', 'cache', 'kline_cache.db')

DDL = (
    """
    CREATE TABLE IF NOT EXISTS candidate_daily (
        date TEXT NOT NULL,
        code TEXT NOT NULL,
        rank INTEGER,
        score REAL,
        PRIMARY KEY (date, code)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS drift_daily_sample (
        date TEXT PRIMARY KEY,
        n_cand INTEGER,
        avg_t1_return REAL,
        win_rate REAL,
        rets TEXT,
        computed_at TEXT
    )
    """,
)


def _connect(path: str) -> Optional[sqlite3.Connection]:
    if not os.path.exists(path):
        return None
    return sqlite3.connect(path)


def _ensure_tables(conn: sqlite3.Connection):
    for ddl in DDL:
        conn.execute(ddl)
    conn.commit()


def record_candidate_pool(trade_date: str, pool: List[Dict],
                          db_path: str = None) -> int:
    """落当日候选池 top-N（幂等：同 (date, code) 覆盖）。

    pool: [{'code','name'?, 'score'}...]，来自 `rank_stocks` 的
    `diagnostics['top_pool']`（含零推荐日）。返回写入行数。
    """
    if not trade_date or not pool:
        return 0
    db = db_path or FACTOR_DB
    os.makedirs(os.path.dirname(db), exist_ok=True)
    rows = []
    for i, item in enumerate(pool, 1):
        code = str(item.get('code') or '').strip()
        if len(code) != 6 or not code.isdigit():
            continue
        sc = item.get('score')
        rows.append((str(trade_date), code, i,
                     float(sc) if isinstance(sc, (int, float)) else None))
    if not rows:
        return 0
    conn = sqlite3.connect(db)
    try:
        _ensure_tables(conn)
        conn.executemany(
            "INSERT OR REPLACE INTO candidate_daily (date, code, rank, score) "
            "VALUES (?,?,?,?)", rows)
        conn.commit()
        return len(rows)
    except Exception as e:
        logger.warning(f"候选池落库失败（不影响主流程）: {str(e)[:80]}")
        return 0
    finally:
        conn.close()


def _t1_returns(pairs: List[tuple], kline_db: str = None) -> List[float]:
    """(date, code) 列表 → 各自的 T+1 收益（百分数），算不出的跳过。

    T+1 = kline_cache 中"该决策日之后的下一个交易日"收盘 / 决策日收盘 − 1。
    与 outcomes.t1_return 同口径（决策日收盘 → 次日收盘）。
    """
    kdb = kline_db or KLINE_DB
    if not os.path.exists(kdb):
        return []
    out = []
    conn = sqlite3.connect(kdb)
    try:
        for d, code in pairs:
            try:
                rows = conn.execute(
                    "SELECT date, close FROM kline_cache WHERE code=? AND date>=? "
                    "AND close>0 ORDER BY date ASC LIMIT 2", (code, d)).fetchall()
            except Exception:
                continue
            if len(rows) < 2 or str(rows[0][0]) != str(d):
                # 决策日无 K 线（停牌/未缓存）→ 该票当日无法计入样本
                continue
            c0, c1 = float(rows[0][1]), float(rows[1][1])
            if c0 > 0:
                out.append((c1 - c0) / c0 * 100.0)
    finally:
        conn.close()
    return out


def backfill_daily_samples(db_path: str = None, kline_db: str = None,
                           limit_days: int = 60) -> int:
    """给"已有候选池但缺样本"的日期补算 T+1 样本（幂等）。返回补算天数。

    设计：只处理 `candidate_daily` 中存在、且 `drift_daily_sample` 中缺失的日期；
    单日样本量不足（<3 只可算）时跳过（次日重试，等 K 线缓存补齐）。
    """
    db = db_path or FACTOR_DB
    conn = _connect(db)
    if conn is None:
        return 0
    n_done = 0
    try:
        _ensure_tables(conn)
        dates = [r[0] for r in conn.execute(
            "SELECT DISTINCT date FROM candidate_daily "
            "WHERE date NOT IN (SELECT date FROM drift_daily_sample) "
            "ORDER BY date DESC LIMIT ?", (int(limit_days),)).fetchall()]
        for d in dates:
            codes = [r[0] for r in conn.execute(
                "SELECT code FROM candidate_daily WHERE date=? ORDER BY rank",
                (d,)).fetchall()]
            rets = _t1_returns([(d, c) for c in codes], kline_db=kline_db)
            if len(rets) < 3:
                continue          # K 线未就绪（如当日样本）→ 下次再算
            conn.execute(
                "INSERT OR REPLACE INTO drift_daily_sample "
                "(date, n_cand, avg_t1_return, win_rate, rets, computed_at) "
                "VALUES (?,?,?,?,?,?)",
                (d, len(rets), round(sum(rets) / len(rets), 4),
                 round(sum(1 for x in rets if x > 0) / len(rets), 4),
                 json.dumps([round(x, 4) for x in rets]),
                 datetime.now().strftime('%Y-%m-%d %H:%M:%S')))
            n_done += 1
        conn.commit()
    except Exception as e:
        logger.warning(f"漂移样本补算失败（不影响主流程）: {str(e)[:80]}")
    finally:
        conn.close()
    return n_done


def seed_from_factor_raw(db_path: str = None, kline_db: str = None) -> int:
    """用既有 `factor_raw` 表**自举**历史候选池（一次性/幂等）。

    背景：`candidate_daily` 从本功能上线起才有数据；而 `factor_raw`
    （feedback/data_collector 写）按日记录了同一口径的**详评池**（`_last_enriched`，
    约 160~200 只/日）—— 与 live 写入的 `diagnostics['top_pool']` 同源同规模，
    因此可直接作为历史锚点，提前若干交易日让样本线达到可用阈值。

    只插入 `candidate_daily` 中缺失的 (date, code)；不覆盖 live 已写的行。
    返回新增行数。零配额、零联网。
    """
    db = db_path or FACTOR_DB
    conn = _connect(db)
    if conn is None:
        return 0
    n = 0
    try:
        _ensure_tables(conn)
        rows = conn.execute(
            "SELECT f.date, f.code FROM factor_raw f "
            "WHERE f.code IS NOT NULL AND NOT EXISTS ("
            "  SELECT 1 FROM candidate_daily c "
            "  WHERE c.date = f.date AND c.code = f.code)").fetchall()
        payload = []
        seen_rank = {}
        for d, code in rows:
            code = str(code).zfill(6)
            if len(code) != 6 or not code.isdigit():
                continue
            seen_rank[d] = seen_rank.get(d, 0) + 1
            payload.append((str(d), code, seen_rank[d], None))
        if payload:
            conn.executemany(
                "INSERT OR IGNORE INTO candidate_daily (date, code, rank, score) "
                "VALUES (?,?,?,?)", payload)
            conn.commit()
            n = len(payload)
    except Exception as e:
        logger.warning(f"自举候选池失败: {str(e)[:80]}")
    finally:
        conn.close()
    return n


def load_daily_samples(db_path: str = None) -> List[Dict]:
    """读取日度样本（时间升序）。字段与 drift_monitor `_load_daily_returns` 对齐：

    [{'date','day_ret','n_rec','win_rate','rets'}...]
    """
    db = db_path or FACTOR_DB
    conn = _connect(db)
    if conn is None:
        return []
    try:
        rows = conn.execute(
            "SELECT date, n_cand, avg_t1_return, win_rate, rets "
            "FROM drift_daily_sample ORDER BY date ASC").fetchall()
    except Exception:
        return []
    finally:
        conn.close()
    out = []
    for d, n, avg, wr, rets in rows:
        try:
            parsed = json.loads(rets) if rets else []
        except Exception:
            parsed = []
        out.append({'date': d, 'day_ret': float(avg), 'n_rec': int(n or len(parsed)),
                    'win_rate': float(wr or 0.0), 'rets': parsed})
    return out
