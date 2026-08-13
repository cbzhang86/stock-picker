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
        conn = None
        try:
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
        finally:
            if conn:
                conn.close()

    def log_prediction(self, date: str, code: str, name: str, mode: str,
                       score: float, rating: str, buy_price: float,
                       model_version: str = 'v1',
                       factor_scores: dict = None) -> int:
        """
        记录一次推荐

        返回 prediction_id
        """
        conn = None
        try:
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
            logger.debug(f"记录推荐: {code} {name} 评分{score} ID={prediction_id}")
            return prediction_id
        finally:
            if conn:
                conn.close()

    def update_outcomes(self, prediction_id: int, kline: pd.DataFrame,
                        pred_date: str = None):
        """
        更新推荐的结果（T+1, T+5, T+20平仓收益）

        参数：
          prediction_id: log_prediction返回的ID
          kline: 包含推荐日及之后K线的DataFrame，需含 date, close 列
          pred_date: 推荐日（YYYY-MM-DD），用于精确定位买入日。
                     缺省时退化用 kline.iloc[0]['date']（不安全，
                     mootdx/缓存缺失可能导致首行日期 != 推荐日）。
        """
        if kline is None or kline.empty:
            logger.warning(f"K线为空，无法更新结果 ID={prediction_id}")
            return

        # 规整：确保 date 列为 datetime 并按日期排序
        kline = kline.copy()
        kline['date'] = pd.to_datetime(kline['date'], errors='coerce')
        kline = kline.dropna(subset=['date']).sort_values('date').reset_index(drop=True)
        if kline.empty:
            logger.warning(f"K线无有效日期，无法更新结果 ID={prediction_id}")
            return

        # 定位推荐日：
        #   - 有 pred_date → 优先匹配 kline 中 pred_date 当天
        #   - 匹配不到（缓存缺失）→ 警告，退化用 pred_date 之前最后一个交易日作为买入参考，
        #     但不取 iloc[0] 的 close（首行日期可能 != 推荐日，导致 buy_price 失真）
        #   - 没有 pred_date → 用 kline.iloc[0]['date']（旧行为，不安全，保留向后兼容）
        if pred_date is not None:
            pred_ts = pd.Timestamp(pred_date)
            match = kline[kline['date'] == pred_ts]
            if not match.empty:
                buy_idx = match.index[0]
                buy_price = float(kline.loc[buy_idx, 'close'])
                buy_date = kline.loc[buy_idx, 'date']
            else:
                # 推荐日不在K线中：用 K 线里 <= pred_date 的最后一天作为买入参考价
                # （mootdx offet=600 或缓存缺口可能导致推荐日缺失，但前后日的收盘价
                #   可作为近似的买入参考；不强行取 iloc[0]，避免抓到几天后的价）
                prior = kline[kline['date'] <= pred_ts]
                if not prior.empty:
                    buy_idx = prior.index[-1]
                    buy_price = float(kline.loc[buy_idx, 'close'])
                    buy_date = kline.loc[buy_idx, 'date']
                    logger.warning(
                        f"推荐日 {pred_date} 不在K线中，用 {buy_date.strftime('%Y-%m-%d')} "
                        f"收盘作为买入参考 ID={prediction_id}"
                    )
                else:
                    # K线最早也晚于推荐日：此前 get_kline(code, start_date=pred_date)
                    # 应当能抓到 pred_date 当天，首行 == pred_date 是常态；只有 mootdx
                    # 缓存残缺到连推荐日都缺才会走到这里。如果硬把"比推荐日晚的某天"
                    # 当买入日算 T+N，相当于买入日顺延一个交易日 → T+N 全部 off-by-one。
                    # 选择"置 None 不写 outcomes"：让该 prediction 继续留在 pending 池，
                    # 下次 cron 触发 backfill 时，缓存已被方案 B 的 fall-through 愈合，
                    # 即可按正确 pred_date 取 T+N。（subagent id=22 实证此分支会 off-by-one）
                    logger.warning(
                        f"K线最早日期晚于推荐日 {pred_date}，置 None 不写 outcomes "
                        f"ID={prediction_id}（等数据愈合后重算）"
                    )
                    return  # 不写 outcomes，避免 off-by-one 污染统计
        else:
            # 没传 pred_date：向后兼容旧行为
            buy_idx = 0
            buy_price = float(kline.iloc[0]['close'])
            buy_date = kline.iloc[0]['date']
            logger.debug(
                f"未传 pred_date，用 K线首行 {buy_date.strftime('%Y-%m-%d')} "
                f"作为买入日 ID={prediction_id}"
            )

        # 按日期查找 buy_date 之后的真实第 N 个交易日
        # （不用 iloc[N]，避免K线缺口导致抓到非T+N的价格）
        future = kline[kline['date'] > buy_date].reset_index(drop=True)

        # T+N 残缺检测：第 N 个交易日距 buy_date 的最大合理自然日间隔。
        # 单一阈值（如 12+N）会在 T+20 撞春节/国庆（合法 36 天）误杀 → 改为按 N 分段，
        # 每段覆盖对应最长假期 + 余量。
        # 实测依据：春节 T+20 间隔 36 天（T+1=11、T+5=15-17），阈值取
        #   T+1  → 13 (满 1 周 + 节假日 + 6.5 余量)
        #   T+5  → 18 (春节 T+5 ≈ 15)
        #   T+20 → 45 (春节 T+20 ≈ 36)
        MAX_GAP = {1: 13, 5: 18, 20: 45}

        def fetch_future(idx_offset: int):
            """返回 future 中第 idx_offset 个交易日的 (date_str, close)。

            残缺检测：第 N 个交易日距 buy_date 的自然日间隔超过 MAX_GAP[N] →
            K线缓存残缺（中间缺了交易日），返回 (None, None) 避免把"几周后"
            的价格当 T+N 写进 outcomes 污染统计。
            """
            if idx_offset - 1 < len(future):
                row = future.iloc[idx_offset - 1]
                natural_gap = (row['date'] - buy_date).days
                threshold = MAX_GAP.get(idx_offset, 12 + idx_offset * 2)
                if natural_gap > threshold:
                    logger.warning(
                        f"T+{idx_offset} 距 buy_date {buy_date.strftime('%Y-%m-%d')} "
                        f"间隔 {natural_gap} 天 > 阈值 {threshold}（K线残缺），置 None "
                        f"ID={prediction_id}"
                    )
                    return None, None
                return row['date'].strftime('%Y-%m-%d'), float(row['close'])
            return None, None

        # T+1: buy_date 之后的第 1 个交易日
        t1_date, t1_close = fetch_future(1)
        t1_return = round((t1_close - buy_price) / buy_price * 100, 2) if t1_close else None

        # T+5: 第 5 个交易日
        t5_date, t5_close = fetch_future(5)
        t5_return = round((t5_close - buy_price) / buy_price * 100, 2) if t5_close else None

        # T+20: 第 20 个交易日
        t20_date, t20_close = fetch_future(20)
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
        conn = None
        try:
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
        finally:
            if conn:
                conn.close()

        return {
            'win_rate_t1': round(win_t1 / total_t1 * 100, 2) if total_t1 else 0,
            'win_rate_t5': round(win_t5 / total_t5 * 100, 2) if total_t5 else 0,
            'avg_return_t1': round(avg_t1, 2) if avg_t1 else 0,
            'avg_return_t5': round(avg_t5, 2) if avg_t5 else 0,
            'total_records': total_t1,
        }

    def get_pending_outcomes(self) -> List[Dict]:
        """获取未补齐结果的推荐（任一 T+1/T+5/T+20 为 NULL 即返回）。

        原谓词仅 `o.t1_close IS NULL` → T+1 写满后行离开 pending 池，
        T+5/T+20 永远不会被回填（即使到期）。改为 OR 任一个为 NULL，
        让 update_outcomes 的幂等 INSERT OR REPLACE 滚动补齐多阶结果。
        """
        conn = None
        try:
            conn = sqlite3.connect(self.db_path)
            c = conn.cursor()
            c.execute(
                """SELECT p.id, p.date, p.code, p.buy_price
                   FROM predictions p
                   LEFT JOIN outcomes o ON p.id = o.prediction_id
                   WHERE o.t1_close IS NULL
                      OR o.t5_close IS NULL
                      OR o.t20_close IS NULL
                   ORDER BY p.date"""
            )
            rows = c.fetchall()
        finally:
            if conn:
                conn.close()

        return [
            {'id': r[0], 'date': r[1], 'code': r[2], 'buy_price': r[3]}
            for r in rows
        ]

    def get_recent_predictions(self, limit: int = 20) -> pd.DataFrame:
        """获取最近N条推荐记录（含结果）"""
        conn = None
        try:
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
        finally:
            if conn:
                conn.close()
        return df
