"""
事件催化数据提供者 — 公告 / 业绩预告 / 新闻

补的缺口
--------
专家评分卡里"事件 15%"这一维，在现有 7 因子模型中完全没有对应信号。
事件是短线最直接的催化来源（业绩预增、重大合同、回购、政策利好），
也是最大的雷区（减持、诉讼、业绩预亏、问询函）。

数据来源与写入方式
------------------
与 FundamentalProvider 同一模式：**只读缓存，数据由 Agent 通过问财
（wenda_*）等 MCP 工具离线灌入**。

    agent（问财 wenda_* 取公告/业绩预告/新闻）
        → scripts/prefetch_tdx.py --events
        → 本模块的 SQLite 缓存

事件极性判定
------------
写入时给出 impact（-100 ~ +100）。可以由 Agent 判定后写入，
也可以用 `classify_event()` 按关键词自动打标（作为兜底）。

缓存缺失时：返回 50（中性）并标记 data_available=False。
"""

import logging
import os
import re
import sqlite3
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)

DEFAULT_DB_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    'data', 'cache', 'events.db'
)

SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    code        TEXT NOT NULL,
    date        TEXT NOT NULL,
    event_type  TEXT,
    title       TEXT,
    impact      REAL DEFAULT 0,
    source      TEXT,
    UNIQUE(code, date, title)
)
"""

# 事件关键词 → 影响极性（用于 title 自动打标兜底）
POSITIVE_PATTERNS = [
    (r'预增|预盈|扭亏|业绩.{0,4}(大增|增长|提升)', 30),
    (r'中标|签订|重大合同|订单', 25),
    (r'回购|增持', 20),
    (r'重组|并购|收购', 15),
    (r'新品|获批|突破|量产|投产', 15),
    (r'分红|派息|高送转', 10),
    (r'政策.{0,6}(支持|利好)|补贴', 12),
]
NEGATIVE_PATTERNS = [
    (r'预亏|预减|业绩.{0,4}(下滑|下降|亏损)', -30),
    (r'减持|套现', -22),
    (r'问询|监管|立案|调查|处罚', -30),
    (r'诉讼|仲裁|冻结', -20),
    (r'质押|爆仓|平仓', -18),
    (r'退市|st|ST|风险警示', -35),
    (r'商誉减值|计提', -15),
]


class EventProvider:
    """事件催化缓存读写"""

    def __init__(self, db_path: str = None):
        self.db_path = db_path or DEFAULT_DB_PATH
        os.makedirs(os.path.dirname(self.db_path), exist_ok=True)
        self._init_db()

    def _init_db(self):
        conn = sqlite3.connect(self.db_path)
        try:
            conn.execute(SCHEMA)
            conn.execute("CREATE INDEX IF NOT EXISTS idx_events_code_date "
                         "ON events(code, date)")
            conn.commit()
        finally:
            conn.close()

    # ── 读 ──────────────────────────────────────────────────

    def get_recent(self, code: str, days: int = 30,
                   as_of: str = None) -> List[Dict]:
        """读取某只股票近 N 天的事件（as_of 之前，防前视）"""
        conn = sqlite3.connect(self.db_path)
        try:
            conn.row_factory = sqlite3.Row
            if as_of:
                from datetime import datetime, timedelta
                start = (datetime.strptime(as_of, '%Y-%m-%d')
                         - timedelta(days=days)).strftime('%Y-%m-%d')
                rows = conn.execute(
                    "SELECT * FROM events WHERE code=? AND date>=? AND date<=? "
                    "ORDER BY date DESC", (str(code).zfill(6), start, as_of)
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM events WHERE code=? ORDER BY date DESC LIMIT ?",
                    (str(code).zfill(6), days * 3)
                ).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()

    # ── 写 ──────────────────────────────────────────────────

    def upsert(self, rows: List[Dict]) -> int:
        """
        批量写入事件（同 code+date+title 视为同一条，覆盖）

        rows: [{code, date, event_type?, title, impact?, source?}, ...]
        未给 impact 时用 classify_event() 按标题关键词自动判定。
        """
        if not rows:
            return 0
        sql = """
            INSERT OR REPLACE INTO events (code, date, event_type, title, impact, source)
            VALUES (?,?,?,?,?,?)
        """
        payload = []
        for r in rows:
            title = r.get('title', '')
            impact = r.get('impact')
            if impact is None:
                impact = self.classify_event(title)
            payload.append((
                str(r.get('code', '')).zfill(6), r.get('date'),
                r.get('event_type'), title, impact, r.get('source', 'wenda'),
            ))
        conn = sqlite3.connect(self.db_path)
        try:
            conn.executemany(sql, payload)
            conn.commit()
            return len(payload)
        finally:
            conn.close()

    # ── 打标 ────────────────────────────────────────────────

    @staticmethod
    def classify_event(title: str) -> float:
        """按标题关键词判定事件极性（-100 ~ +100），无匹配返回 0"""
        if not title:
            return 0.0
        score = 0.0
        for pat, val in POSITIVE_PATTERNS:
            if re.search(pat, title, re.IGNORECASE):
                score += val
        for pat, val in NEGATIVE_PATTERNS:
            # 修复（2026-09-06 审查）：负向匹配补 re.IGNORECASE，与正向一致
            # （原大小写敏感会让"Risk/退市变体"等漏匹配）
            if re.search(pat, title, re.IGNORECASE):
                score += val
        return max(-100.0, min(100.0, score))

    # ── 统计 ────────────────────────────────────────────────

    def coverage(self) -> Dict:
        conn = sqlite3.connect(self.db_path)
        try:
            total = conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]
            codes = conn.execute("SELECT COUNT(DISTINCT code) FROM events").fetchone()[0]
            latest = conn.execute("SELECT MAX(date) FROM events").fetchone()[0]
            pos = conn.execute(
                "SELECT COUNT(*) FROM events WHERE impact>0").fetchone()[0]
            neg = conn.execute(
                "SELECT COUNT(*) FROM events WHERE impact<0").fetchone()[0]
        finally:
            conn.close()
        return {'total_events': total, 'codes': codes, 'latest_date': latest,
                'positive': pos, 'negative': neg}

    # ── 评分 ────────────────────────────────────────────────

    @staticmethod
    def score(events: List[Dict], decay_days: int = 10,
              reference_date: str = None) -> float:
        """
        事件列表 → 0-100 催化分

        参数：
          reference_date: 计算"事件距今多少天"的基准日。
            **回测必须传 backtest_date**，否则会用 datetime.now() 计算年龄，
            历史事件的衰减权重全错（回测变成前视）。

        规则：
          - 无事件 → 50（中性，不加不减）
          - 事件按距基准日的天数线性衰减（超过 decay_days 天不再计入）
          - 正负事件可相互抵消
          - 净冲击映射到 50±40 的区间后截断

        为什么用衰减：短线催化有时效性，一个月前的业绩预告对今天
        的 T+1 收益基本没有边际影响。
        """
        if not events:
            return 50.0

        from datetime import datetime
        ref = (datetime.strptime(reference_date, '%Y-%m-%d')
               if reference_date else datetime.now())
        net = 0.0
        for e in events:
            try:
                d = datetime.strptime(e.get('date', ''), '%Y-%m-%d')
            except (ValueError, TypeError):
                continue
            age = (ref - d).days
            if age < 0 or age > decay_days:
                continue
            weight = 1.0 - age / decay_days
            net += (e.get('impact') or 0) * weight

        # 净冲击 ±100 → 分数 ±40
        return max(0.0, min(100.0, 50.0 + net * 0.4))
