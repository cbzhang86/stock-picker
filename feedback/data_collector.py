"""
每日因子数据采集器

将每日交易中获取的资金流、北向、热点、龙虎榜等因子数据持久化
到 SQLite，供后续回测读取。使这些因子逐步积累历史数据。

用法：
  collector = FactorDataCollector()
  collector.collect(data_engine, enriched_stocks, hot_df, recommendations)
"""

import json
import logging
import os
import sqlite3
from datetime import date, datetime
from typing import Dict, List, Optional

import pandas as pd

logger = logging.getLogger(__name__)

# 默认数据库路径
DEFAULT_DB_PATH = os.path.join(
    os.path.dirname(os.path.dirname(__file__)),
    'data', 'cache', 'factor_daily.db'
)

# 建表 SQL
SCHEMA = {
    'capital_flow': """
        CREATE TABLE IF NOT EXISTS capital_flow (
            date TEXT NOT NULL,
            code TEXT NOT NULL,
            accumulated_net REAL,
            PRIMARY KEY (date, code)
        )
    """,
    'north_flow': """
        CREATE TABLE IF NOT EXISTS north_flow (
            date TEXT NOT NULL,
            code TEXT NOT NULL,
            holding_change REAL,
            PRIMARY KEY (date, code)
        )
    """,
    'hot_stocks': """
        CREATE TABLE IF NOT EXISTS hot_stocks (
            date TEXT NOT NULL,
            code TEXT NOT NULL,
            name TEXT,
            PRIMARY KEY (date, code)
        )
    """,
    'dragon_tiger': """
        CREATE TABLE IF NOT EXISTS dragon_tiger (
            date TEXT NOT NULL,
            code TEXT NOT NULL,
            net_buy_wan REAL DEFAULT 0,
            institution_net_wan REAL DEFAULT 0,
            has_record INTEGER DEFAULT 0,
            PRIMARY KEY (date, code)
        )
    """,
}


class FactorDataCollector:
    """每日因子数据采集器"""

    def __init__(self, db_path: str = None):
        self.db_path = db_path or DEFAULT_DB_PATH
        os.makedirs(os.path.dirname(self.db_path), exist_ok=True)
        self._init_db()

    def _init_db(self):
        """初始化数据库表"""
        conn = sqlite3.connect(self.db_path)
        try:
            for sql in SCHEMA.values():
                conn.execute(sql)
            conn.commit()
        finally:
            conn.close()
        logger.debug(f"因子仓库已初始化: {self.db_path}")

    def collect(self, data_engine, enriched_stocks: List[Dict],
                hot_df: pd.DataFrame, recommendations: List[Dict],
                trade_date: str = None):
        """
        采集当日所有因子数据

        参数：
          data_engine: DataEngine 实例
          enriched_stocks: 详评阶段的候选股列表（含 factor_library 所需数据）
          hot_df: 同花顺强势股 DataFrame
          recommendations: 最终推荐列表
          trade_date: 交易日期（默认今天）
        """
        if trade_date is None:
            trade_date = date.today().isoformat()

        conn = sqlite3.connect(self.db_path)

        try:
            # 1. 资金流
            self._save_capital_flow(conn, enriched_stocks, trade_date)
            # 2. 北向
            self._save_north_flow(conn, enriched_stocks, trade_date)
            # 3. 热点
            self._save_hot_stocks(conn, hot_df, trade_date)
            # 4. 龙虎榜
            self._save_dragon_tiger(conn, recommendations, trade_date)

            conn.commit()
            n_flow = sum(1 for s in enriched_stocks
                         if s.get('main_fund_accumulated') is not None)
            n_north = sum(1 for s in enriched_stocks
                          if s.get('north_flow_accumulated') is not None)
            logger.info(
                f"因子数据已采集: {trade_date} | "
                f"资金流 {n_flow}/{len(enriched_stocks)} | "
                f"北向 {n_north}/{len(enriched_stocks)} | "
                f"热点 {len(hot_df) if hot_df is not None else 0} | "
                f"龙虎榜 {len(recommendations)}"
            )
        except Exception as e:
            conn.rollback()
            logger.warning(f"因子数据采集失败: {e}")
        finally:
            conn.close()

    # ── 各因子写入 ──────────────────────────────────────────

    def _save_capital_flow(self, conn, stocks: list, trade_date: str):
        """保存主力资金流"""
        rows = []
        for s in stocks:
            net = s.get('main_fund_accumulated')
            if net is not None:
                rows.append((trade_date, str(s['code']).zfill(6), float(net)))
        if rows:
            conn.executemany(
                "INSERT OR REPLACE INTO capital_flow (date, code, accumulated_net) VALUES (?, ?, ?)",
                rows
            )

    def _save_north_flow(self, conn, stocks: list, trade_date: str):
        """保存北向资金"""
        rows = []
        for s in stocks:
            net = s.get('north_flow_accumulated')
            if net is not None:
                rows.append((trade_date, str(s['code']).zfill(6), float(net)))
        if rows:
            conn.executemany(
                "INSERT OR REPLACE INTO north_flow (date, code, holding_change) VALUES (?, ?, ?)",
                rows
            )

    def _save_hot_stocks(self, conn, hot_df: pd.DataFrame, trade_date: str):
        """保存同花顺强势股（自适应列名）"""
        if hot_df is None or hot_df.empty:
            return
        # 自适应列名：中文 > 英文 > 前两列
        code_col = None
        name_col = None
        for cand in ['代码', 'code', 'Code', '股票代码']:
            if cand in hot_df.columns:
                code_col = cand
                break
        for cand in ['名称', 'name', 'Name', '股票名称']:
            if cand in hot_df.columns:
                name_col = cand
                break
        if code_col is None:
            # 兜底：取前两列
            cols = hot_df.columns.tolist()
            code_col = cols[0] if len(cols) > 0 else None
            name_col = cols[1] if len(cols) > 1 else None
        if code_col is None:
            return

        rows = []
        for _, row in hot_df.iterrows():
            code = str(row.get(code_col, '')).zfill(6)
            name = str(row.get(name_col, '')) if name_col else ''
            if code:
                rows.append((trade_date, code, name))
        if rows:
            conn.executemany(
                "INSERT OR REPLACE INTO hot_stocks (date, code, name) VALUES (?, ?, ?)",
                rows
            )

    def _save_dragon_tiger(self, conn, recommendations: list, trade_date: str):
        """保存推荐标的的龙虎榜数据"""
        rows = []
        for rec in recommendations:
            code = str(rec.get('code', '')).zfill(6)
            dt = rec.get('dragon_tiger', {})
            if dt and dt.get('records'):
                latest = dt['records'][0]
                inst = dt.get('institution', {})
                rows.append((
                    trade_date, code,
                    float(latest.get('net_buy_wan', 0)),
                    float(inst.get('net_amt', 0)),
                    1
                ))
            else:
                # 也记录「没有龙虎榜」的事实
                rows.append((trade_date, code, 0, 0, 0))
        if rows:
            conn.executemany(
                "INSERT OR REPLACE INTO dragon_tiger "
                "(date, code, net_buy_wan, institution_net_wan, has_record) "
                "VALUES (?, ?, ?, ?, ?)",
                rows
            )

    # ── 读取（供回测使用） ──────────────────────────────────

    def load_capital_flow(self, date_str: str, code: str) -> Optional[float]:
        """读取某日某股的主力资金数据"""
        conn = sqlite3.connect(self.db_path)
        row = conn.execute(
            "SELECT accumulated_net FROM capital_flow WHERE date=? AND code=?",
            (date_str, code)
        ).fetchone()
        conn.close()
        return float(row[0]) if row else None

    def load_north_flow(self, date_str: str, code: str) -> Optional[float]:
        """读取某日某股的北向数据"""
        conn = sqlite3.connect(self.db_path)
        row = conn.execute(
            "SELECT holding_change FROM north_flow WHERE date=? AND code=?",
            (date_str, code)
        ).fetchone()
        conn.close()
        return float(row[0]) if row else None

    def load_hot_stocks(self, date_str: str) -> set:
        """读取某日的强势股列表"""
        conn = sqlite3.connect(self.db_path)
        rows = conn.execute(
            "SELECT code FROM hot_stocks WHERE date=?", (date_str,)
        ).fetchall()
        conn.close()
        return set(r[0] for r in rows)

    def load_dragon_tiger(self, date_str: str, code: str) -> Optional[Dict]:
        """读取某日某股的龙虎榜数据"""
        conn = sqlite3.connect(self.db_path)
        row = conn.execute(
            "SELECT net_buy_wan, institution_net_wan, has_record "
            "FROM dragon_tiger WHERE date=? AND code=?",
            (date_str, code)
        ).fetchone()
        conn.close()
        if row:
            return {
                'net_buy_wan': row[0],
                'institution_net_wan': row[1],
                'has_record': bool(row[2]),
            }
        return None

    def get_stats(self) -> Dict:
        """返回仓库统计信息"""
        conn = sqlite3.connect(self.db_path)
        stats = {}
        for table in ['capital_flow', 'north_flow', 'hot_stocks', 'dragon_tiger']:
            row = conn.execute(
                f"SELECT COUNT(DISTINCT date), COUNT(*) FROM {table}"
            ).fetchone()
            stats[table] = {'days': row[0], 'rows': row[1]}
        conn.close()
        return stats
