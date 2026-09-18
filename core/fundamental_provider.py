"""
估值 / 基本面数据提供者 — 通达信（TDX）数据源

补的缺口
--------
原有数据体系里，PE/PB 只有实时行情接口给的快照（且大量缺失），
ROE / 股息率 / 营收增长 完全没有 —— ASHareHub financial 端点曾返回 401。
短线模型里"估值安全垫"这一维长期是空的。

通达信 tdx_quotes(hasCwInfo=1) 与 tdx_indicator_select 能直接给：
  PE(TTM) / PB / ROE / 股息率 / 总市值 / 营收同比 / 净利同比

数据来源与写入方式
------------------
本模块**只负责读取缓存**，不直接调用通达信接口 —— TDX 是通过 MCP 暴露给
Agent 的工具，Python 进程无法直接调用。因此数据是"离线灌入"的：

    agent（用 TDX MCP 批量取数）→ scripts/prefetch_tdx.py → 本模块的 SQLite 缓存

这样既保留了回测可复现性（数据在库里，随时可重放），
也不让策略运行时依赖 Agent 是否在线。

缓存缺失时的行为
----------------
`get()` 返回 None，因子层返回中性 50 并标记 data_available=False。
绝不返回 0 或猜测值 —— 缺失必须显式可见。
"""

import logging
import os
import sqlite3
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)

DEFAULT_DB_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    'data', 'cache', 'tdx_fundamentals.db'
)

SCHEMA = """
CREATE TABLE IF NOT EXISTS fundamentals (
    code            TEXT PRIMARY KEY,
    name            TEXT,
    pe              REAL,
    pb              REAL,
    roe             REAL,
    div_yield       REAL,
    market_cap      REAL,
    revenue_growth  REAL,
    profit_growth   REAL,
    update_date     TEXT,
    source          TEXT DEFAULT 'tdx'
)
"""

# P1-I（2026-09-05 审查报告）：point-in-time 历史表。
# 主表 fundamentals 是"最新快照"（PRIMARY KEY=code，反复 REPLACE），
# 用它做回测 = 用今天的估值回答昨天的决策（前视泄漏）。历史表按
# (code, update_date) 累积每日灌入的快照，支持 as_of 时点查询。
HISTORY_SCHEMA = """
CREATE TABLE IF NOT EXISTS fundamentals_history (
    code            TEXT,
    update_date     TEXT,
    name            TEXT,
    pe              REAL,
    pb              REAL,
    roe             REAL,
    div_yield       REAL,
    market_cap      REAL,
    revenue_growth  REAL,
    profit_growth   REAL,
    source          TEXT DEFAULT 'tdx',
    PRIMARY KEY (code, update_date)
)
"""


class FundamentalProvider:
    """估值 / 基本面缓存读写"""

    def __init__(self, db_path: str = None):
        self.db_path = db_path or DEFAULT_DB_PATH
        os.makedirs(os.path.dirname(self.db_path), exist_ok=True)
        self._init_db()

    def _init_db(self):
        conn = sqlite3.connect(self.db_path)
        try:
            conn.execute(SCHEMA)
            conn.execute(HISTORY_SCHEMA)
            conn.commit()
        finally:
            conn.close()

    # ── 读 ──────────────────────────────────────────────────

    def get(self, code: str) -> Optional[Dict]:
        """读取单只股票的估值/基本面数据，缺失返回 None"""
        conn = sqlite3.connect(self.db_path)
        try:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                "SELECT * FROM fundamentals WHERE code=?", (str(code).zfill(6),)
            ).fetchone()
            return dict(row) if row else None
        finally:
            conn.close()

    def get_many(self, codes: List[str], as_of: str = None) -> Dict[str, Dict]:
        """批量读取。

        as_of=None（实盘）：读主表 fundamentals（最新快照）。
        as_of='YYYY-MM-DD'（回测）：只查 fundamentals_history 中该日期之前
        的最新一条 —— 点时正确（point-in-time）。历史表无该股数据时**不回退**
        到主表（主表是未来数据，回退即前视），返回缺失由因子层中性化，
        缺失显式可见。

        P1-I（2026-09-05 审查报告）：此前 get_many 无 as_of 概念，回测直接
        读最新快照 = 前视泄漏；历史表为空时宁可全部中性，也不给未来数据。
        """
        if not codes:
            return {}
        zfilled = [str(c).zfill(6) for c in codes]
        placeholders = ','.join(['?'] * len(zfilled))
        conn = sqlite3.connect(self.db_path)
        try:
            conn.row_factory = sqlite3.Row
            if not as_of:
                rows = conn.execute(
                    f"SELECT * FROM fundamentals WHERE code IN ({placeholders})",
                    zfilled
                ).fetchall()
                return {r['code']: dict(r) for r in rows}
            # point-in-time：每股取 update_date <= as_of 的最新一条
            rows = conn.execute(
                f"""
                SELECT h.* FROM fundamentals_history h
                JOIN (
                    SELECT code, MAX(update_date) AS ud
                    FROM fundamentals_history
                    WHERE update_date <= ? AND code IN ({placeholders})
                    GROUP BY code
                ) latest ON h.code = latest.code AND h.update_date = latest.ud
                """,
                [as_of] + zfilled
            ).fetchall()
            return {r['code']: dict(r) for r in rows}
        finally:
            conn.close()

    # ── 写 ──────────────────────────────────────────────────

    def upsert(self, rows: List[Dict]) -> int:
        """
        批量写入（INSERT OR REPLACE）

        rows: [{code, name?, pe?, pb?, roe?, div_yield?, market_cap?,
                revenue_growth?, profit_growth?, update_date?, source?}, ...]
        """
        if not rows:
            return 0
        sql = """
            INSERT OR REPLACE INTO fundamentals
            (code, name, pe, pb, roe, div_yield, market_cap,
             revenue_growth, profit_growth, update_date, source)
            VALUES (?,?,?,?,?,?,?,?,?,?,?)
        """
        payload = []
        for r in rows:
            payload.append((
                str(r.get('code', '')).zfill(6), r.get('name'),
                r.get('pe'), r.get('pb'), r.get('roe'), r.get('div_yield'),
                r.get('market_cap'), r.get('revenue_growth'),
                r.get('profit_growth'), r.get('update_date'),
                r.get('source', 'tdx'),
            ))
        conn = sqlite3.connect(self.db_path)
        try:
            conn.executemany(sql, payload)
            conn.commit()
            return len(payload)
        finally:
            conn.close()

    def upsert_history(self, rows: List[Dict]) -> int:
        """批量写入 point-in-time 历史表（P1-I）。

        rows: [{code, update_date(必填 'YYYY-MM-DD'), name?, pe?, pb?, roe?,
                div_yield?, market_cap?, revenue_growth?, profit_growth?, source?}, ...]

        供 scripts/prefetch_tdx.py 每日灌入。与主表 upsert 并行调用：
        主表服务实盘（最新值），历史表服务回测（as_of 时点值）。
        """
        if not rows:
            return 0
        sql = """
            INSERT OR REPLACE INTO fundamentals_history
            (code, update_date, name, pe, pb, roe, div_yield, market_cap,
             revenue_growth, profit_growth, source)
            VALUES (?,?,?,?,?,?,?,?,?,?,?)
        """
        payload = []
        for r in rows:
            if not r.get('update_date'):
                continue  # 无日期的行无法定位时点，拒收（防前视的硬约束）
            payload.append((
                str(r.get('code', '')).zfill(6), r.get('update_date'), r.get('name'),
                r.get('pe'), r.get('pb'), r.get('roe'), r.get('div_yield'),
                r.get('market_cap'), r.get('revenue_growth'),
                r.get('profit_growth'), r.get('source', 'tdx'),
            ))
        conn = sqlite3.connect(self.db_path)
        try:
            conn.executemany(sql, payload)
            conn.commit()
            return len(payload)
        finally:
            conn.close()

    # ── 统计 ────────────────────────────────────────────────

    def coverage(self) -> Dict:
        """缓存覆盖情况"""
        conn = sqlite3.connect(self.db_path)
        try:
            total = conn.execute("SELECT COUNT(*) FROM fundamentals").fetchone()[0]
            with_pe = conn.execute(
                "SELECT COUNT(*) FROM fundamentals WHERE pe IS NOT NULL").fetchone()[0]
            with_roe = conn.execute(
                "SELECT COUNT(*) FROM fundamentals WHERE roe IS NOT NULL").fetchone()[0]
            latest = conn.execute(
                "SELECT MAX(update_date) FROM fundamentals").fetchone()[0]
        finally:
            conn.close()
        return {'total': total, 'with_pe': with_pe, 'with_roe': with_roe,
                'latest_update': latest}

    # ── 评分 ────────────────────────────────────────────────

    @staticmethod
    def score(fund: Optional[Dict]) -> float:
        """
        估值/基本面 → 0-100 分（短线视角的"估值安全垫"）

        与长线 valuation 因子的区别：
          长线 valuation 追求"便宜"（低 PB 高分）；
          短线这里追求的是"没有明显估值地雷 + 有盈利质量支撑"——
          不追求极度低估（破净股往往缺乏短线弹性），但重罚泡沫与亏损。

        维度：
          ROE       盈利质量（最重要，最高 +22）
          PE        估值合理性（亏损重罚，泡沫扣分）
          PB        资产溢价（适度即可，过高分不高）
          股息率    现金回报（健康区间加分）
          成长性    营收/净利同比（可选，有则计入）
        """
        if not fund:
            return 50.0

        score = 50.0
        pe = fund.get('pe')
        pb = fund.get('pb')
        roe = fund.get('roe')
        div = fund.get('div_yield')
        rev_g = fund.get('revenue_growth')
        prof_g = fund.get('profit_growth')

        # ROE：盈利质量核心
        if roe is not None:
            if roe >= 20:
                score += 22
            elif roe >= 15:
                score += 16
            elif roe >= 10:
                score += 10
            elif roe >= 5:
                score += 4
            elif roe < 0:
                score -= 15

        # PE：亏损重罚，泡沫扣分，合理区间小幅加分
        if pe is not None:
            if pe < 0:
                score -= 12
            elif 0 < pe < 15:
                score += 8
            elif 15 <= pe < 30:
                score += 5
            elif 30 <= pe < 60:
                score += 0
            elif 60 <= pe < 100:
                score -= 8
            else:
                score -= 15

        # PB：短线不需要极度低估，重点是别太贵
        if pb is not None and pb > 0:
            if pb < 1:
                score += 3      # 破净：安全但缺乏弹性，只小幅加分
            elif pb < 3:
                score += 8
            elif pb < 6:
                score += 2
            elif pb < 10:
                score -= 6
            else:
                score -= 12

        # 股息率：健康区间加分，异常高扣分
        if div is not None and div > 0:
            if 2 <= div <= 5:
                score += 5
            elif div > 8:
                score -= 5

        # 成长性（有则计入，权重小）
        if prof_g is not None:
            if prof_g >= 50:
                score += 6
            elif prof_g >= 20:
                score += 4
            elif prof_g <= -30:
                score -= 8
        if rev_g is not None:
            if rev_g >= 30:
                score += 3
            elif rev_g <= -20:
                score -= 5

        return max(0.0, min(100.0, score))
