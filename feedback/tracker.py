"""
预测结果追踪器 — 记录推荐 → 跟踪表现 → 计算成功率

核心流程：
  1. 每次推荐写入 predictions 表
  2. T+1/T+5/T+20 从K线更新 outcomes 表
  3. 定期计算胜率/平均收益
"""

import sqlite3
import json
import os
import logging
from datetime import datetime, timedelta
from typing import Dict, List, Optional

import pandas as pd

logger = logging.getLogger(__name__)


class PredictionTracker:
    """预测结果追踪器"""

    def __init__(self, db_path: str = "data/db/predictions.db"):
        self.db_path = db_path
        os.makedirs(os.path.dirname(db_path) if os.path.dirname(db_path) else '.', exist_ok=True)
        self._init_db()

    def _init_db(self):
        """初始化SQLite数据库表结构"""
        conn = sqlite3.connect(self.db_path)
        c = conn.cursor()

        c.execute("""
            CREATE TABLE IF NOT EXISTS predictions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                date TEXT NOT NULL,
                code TEXT NOT NULL,
                name TEXT,
                mode TEXT NOT NULL,
                score REAL,
                rating TEXT,
                buy_price REAL,
                model_version TEXT,
                factor_scores TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)

        c.execute("""
            CREATE TABLE IF NOT EXISTS outcomes (
                prediction_id INTEGER PRIMARY KEY,
                t1_date TEXT,
                t1_close REAL,
                t1_return REAL,
                t5_date TEXT,
                t5_close REAL,
                t5_return REAL,
                t20_date TEXT,
                t20_close REAL,
                t20_return REAL,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (prediction_id) REFERENCES predictions(id)
            )
        """)

        c.execute("""
            CREATE INDEX IF NOT EXISTS idx_predictions_date
            ON predictions(date)
        """)

        c.execute("""
            CREATE INDEX IF NOT EXISTS idx_predictions_mode
            ON predictions(mode)
        """)

        c.execute("""
            CREATE UNIQUE INDEX IF NOT EXISTS idx_predictions_unique
            ON predictions(date, code, mode)
        """)

        conn.commit()
        conn.close()

    def log_prediction(self, date: str, code: str, name: str, mode: str,
                       score: float, rating: str, buy_price: float,
                       model_version: str = 'v1',
                       factor_scores: dict = None) -> int:
        """
        记录一次推荐

        返回 prediction_id
        """
        conn = sqlite3.connect(self.db_path)
        c = conn.cursor()
        c.execute(
            """INSERT INTO predictions
               (date, code, name, mode, score, rating, buy_price, model_version, factor_scores)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (date, code, name, mode, score, rating, buy_price,
             model_version, json.dumps(factor_scores, ensure_ascii=False, default=str))
        )
        conn.commit()
        prediction_id = c.lastrowid
        conn.close()

        logger.debug(f"记录推荐: {code} {name} 评分{score} ID={prediction_id}")
        return prediction_id

    def update_outcomes(self, prediction_id: int, kline: pd.DataFrame):
        """
        更新推荐的结果（T+1, T+5, T+20平仓收益）

        参数：
          prediction_id: log_prediction返回的ID
          kline: 包含推荐日之后K线的DataFrame，需含 date, close 列
        """
        if kline is None or kline.empty:
            logger.warning(f"K线为空，无法更新结果 ID={prediction_id}")
            return

        buy_price = float(kline.iloc[0]['close'])
        buy_date = kline.iloc[0]['date']

        # 确保索引访问
        def get_data(idx):
            if idx < len(kline):
                return str(kline.iloc[idx]['date']), float(kline.iloc[idx]['close'])
            return None, None

        # T+1
        t1_date, t1_close = get_data(1)
        t1_return = round((t1_close - buy_price) / buy_price * 100, 2) if t1_close else None

        # T+5
        t5_date, t5_close = get_data(4)
        t5_return = round((t5_close - buy_price) / buy_price * 100, 2) if t5_close else None

        # T+20
        t20_date, t20_close = get_data(19)
        t20_return = round((t20_close - buy_price) / buy_price * 100, 2) if t20_close else None

        conn = sqlite3.connect(self.db_path)
        try:
            c = conn.cursor()
            c.execute(
                """INSERT OR REPLACE INTO outcomes
                   (prediction_id, t1_date, t1_close, t1_return,
                    t5_date, t5_close, t5_return,
                    t20_date, t20_close, t20_return)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (prediction_id, t1_date, t1_close, t1_return,
                 t5_date, t5_close, t5_return,
                 t20_date, t20_close, t20_return)
            )
            conn.commit()
        finally:
            conn.close()

    def calc_accuracy(self, mode: str = 'short', days: int = None) -> Dict:
        """
        计算策略胜率统计

        参数：
          mode: 'short' / 'long'
          days: 仅统计近N天（可选）

        返回：
          {win_rate_t1, win_rate_t5, avg_return_t1, avg_return_t5, total_records}
        """
        conn = sqlite3.connect(self.db_path)
        c = conn.cursor()

        # 构建查询
        date_filter = ""
        params = [mode]
        if days:
            date_filter = "AND p.date >= date('now', ?)"
            params.append(f"-{days} days")

        # T+1 统计
        c.execute(
            f"""SELECT COUNT(*),
                       SUM(CASE WHEN o.t1_return > 0 THEN 1 ELSE 0 END),
                       AVG(o.t1_return)
                FROM predictions p
                JOIN outcomes o ON p.id = o.prediction_id
                WHERE p.mode = ? AND o.t1_return IS NOT NULL
                {date_filter}""",
            params
        )
        row = c.fetchone()
        total_t1 = row[0] or 0
        win_t1 = row[1] or 0
        avg_t1 = row[2] or 0.0

        # T+5 统计
        c.execute(
            f"""SELECT COUNT(*),
                       SUM(CASE WHEN o.t5_return > 0 THEN 1 ELSE 0 END),
                       AVG(o.t5_return)
                FROM predictions p
                JOIN outcomes o ON p.id = o.prediction_id
                WHERE p.mode = ? AND o.t5_return IS NOT NULL
                {date_filter}""",
            params
        )
        row = c.fetchone()
        total_t5 = row[0] or 0
        win_t5 = row[1] or 0
        avg_t5 = row[2] or 0.0

        conn.close()

        return {
            'win_rate_t1': round(win_t1 / total_t1 * 100, 2) if total_t1 else 0,
            'win_rate_t5': round(win_t5 / total_t5 * 100, 2) if total_t5 else 0,
            'avg_return_t1': round(avg_t1, 2) if avg_t1 else 0,
            'avg_return_t5': round(avg_t5, 2) if avg_t5 else 0,
            'total_records': total_t1,
        }

    def get_pending_outcomes(self) -> List[Dict]:
        """获取还没有T+1结果的推荐"""
        conn = sqlite3.connect(self.db_path)
        c = conn.cursor()
        c.execute(
            """SELECT p.id, p.date, p.code, p.buy_price
               FROM predictions p
               LEFT JOIN outcomes o ON p.id = o.prediction_id
               WHERE o.t1_close IS NULL
               ORDER BY p.date"""
        )
        rows = c.fetchall()
        conn.close()

        return [
            {'id': r[0], 'date': r[1], 'code': r[2], 'buy_price': r[3]}
            for r in rows
        ]

    def get_recent_predictions(self, limit: int = 20) -> pd.DataFrame:
        """获取最近N条推荐记录（含结果）"""
        conn = sqlite3.connect(self.db_path)
        query = """
            SELECT p.date, p.code, p.name, p.mode, p.score, p.rating,
                   p.buy_price,
                   o.t1_return, o.t5_return, o.t20_return
            FROM predictions p
            LEFT JOIN outcomes o ON p.id = o.prediction_id
            ORDER BY p.id DESC
            LIMIT ?
        """
        df = pd.read_sql_query(query, conn, params=(limit,))
        conn.close()
        return df
