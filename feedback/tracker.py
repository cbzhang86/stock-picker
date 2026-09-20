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
                    status TEXT,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY (prediction_id) REFERENCES predictions(id)
                )
            """)

            # 增量迁移（2026-09-07 论证后实施）：存量库补 status 列
            # （no_data 终态：连续回填无 K 线的停牌/退市票）
            cols = {r[1] for r in c.execute("PRAGMA table_info(outcomes)").fetchall()}
            if 'status' not in cols:
                c.execute("ALTER TABLE outcomes ADD COLUMN status TEXT")

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

    def has_predictions(self, date: str, mode: str) -> bool:
        """检查指定日期+模式是否已有预测记录。

        批次级防重入口：调用方在写推荐循环前调用一次，
        已有记录则跳过整批写入（防止 study-a 等二次运行污染 predictions 表）。
        """
        conn = None
        try:
            conn = sqlite3.connect(self.db_path)
            c = conn.cursor()
            n = c.execute(
                "SELECT COUNT(*) FROM predictions WHERE date = ? AND mode = ?",
                (date, mode)
            ).fetchone()[0]
            return n > 0
        finally:
            if conn:
                conn.close()

    def log_prediction(self, date: str, code: str, name: str, mode: str,
                       score: float, rating: str, buy_price: float,
                       model_version: str = 'v1',
                       factor_scores: dict = None) -> int:
        """
        记录一次推荐。

        注意：本方法不做防重（同一批推荐的第2/3条也会正常写入）。
        批次级防重请先调用 has_predictions(date, mode) 判断，
        见 eod_stock_picker.py run_short_term/run_long_term 的用法。

        返回 prediction_id；若命中 UNIQUE(date, code, mode) 唯一索引（并发/竞态
    窗口：has_predictions 检查通过后另一进程先写入），返回 None 并记 warning
    （2026-09-20 审查 P2-3：原裸 INSERT 会让第二个进程抛 IntegrityError，
    连带 run_short_term 中断 → 当日简报不生成；冲突比"炸掉整个 eod"更可接受）。
        """
        conn = None
        try:
            conn = sqlite3.connect(self.db_path)
            c = conn.cursor()
            # created_at 显式写入北京时间（2026-09-16 P2-3）
            # 背景：建表用的是 `DEFAULT CURRENT_TIMESTAMP`，而 SQLite 的
            # CURRENT_TIMESTAMP 恒为 UTC → 本列为 UTC(如 07:02) 与全工程
            # 北京时间口径(15:02) 混用，事后取证与任何"基于 created_at 的
            # 时效判断"都会失真。去重键用 date 字段故此前未造成数据错误。
            # 注：存量行仍是 UTC，不做回填（无法无损判定历史行的真实时区）。
            try:
                from core.trading_calendar import beijing_now
                _created_at = beijing_now().strftime('%Y-%m-%d %H:%M:%S')
            except Exception:
                from datetime import datetime
                _created_at = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            c.execute(
                """INSERT OR IGNORE INTO predictions
                   (date, code, name, mode, score, rating, buy_price, model_version,
                    factor_scores, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (date, code, name, mode, score, rating, buy_price,
                 model_version, json.dumps(factor_scores, ensure_ascii=False, default=str),
                 _created_at)
            )
            conn.commit()
            if c.rowcount == 0:
                # 命中唯一索引：该 (date, code, mode) 已存在（并发写入/竞态窗口）
                logger.warning(
                    f"推荐记录已存在（UNIQUE 命中，跳过写入）: {date} {code} mode={mode}")
                return None
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

        # T2（2026-09-17）：按已回填的最高 T 档位写入正状态 filled_*，
        # 使 outcomes 表从"全 NULL / 仅 no_data 墓碑"变为有 filled 正状态，
        # 便于统计"已回填覆盖度"而非仅看 t1_return IS NOT NULL。
        if t20_return is not None:
            _fill_status = 'filled_t20'
        elif t5_return is not None:
            _fill_status = 'filled_t5'
        elif t1_return is not None:
            _fill_status = 'filled_t1'
        else:
            _fill_status = 'pending'

        conn = sqlite3.connect(self.db_path)
        try:
            c = conn.cursor()
            c.execute(
                """INSERT OR REPLACE INTO outcomes
                   (prediction_id, t1_date, t1_close, t1_return,
                    t5_date, t5_close, t5_return,
                    t20_date, t20_close, t20_return, status)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (prediction_id, t1_date, t1_close, t1_return,
                 t5_date, t5_close, t5_return,
                 t20_date, t20_close, t20_return, _fill_status)
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

        终态标记（2026-09-07 论证后实施）：outcomes.status='no_data' 的行
        （连续 30 次回填无 K 线的停牌/退市票终态）不再进入 pending 池——
        它们的 t1_return 为 NULL，天然被 accuracy/drift/kill-switch 的
        IS NOT NULL 过滤排除，不污染任何统计。
        """
        conn = None
        try:
            conn = sqlite3.connect(self.db_path)
            c = conn.cursor()
            c.execute(
                """SELECT p.id, p.date, p.code, p.buy_price
                   FROM predictions p
                   LEFT JOIN outcomes o ON p.id = o.prediction_id
                   WHERE (o.t1_close IS NULL
                      OR o.t5_close IS NULL
                      OR o.t20_close IS NULL)
                     AND (o.status IS NULL OR o.status != 'no_data')
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

    def bump_backfill_attempts(self, prediction_id: int) -> int:
        """回填尝试计数 +1（持久化）——2026-09-07 自动化审计发现的修复。

        原设计用内存态 attempts，但 backfill 每次以独立进程运行，计数
        恒为 1，导致"连续 30 次无 K 线 → 标记终态"永远不会触发
        （实测剩余 37 条 pending、23 条滞留超 20 天）。改为落库计数。

        predictions 表新增 backfill_attempts 列（存量库增量迁移、幂等）。
        """
        conn = None
        try:
            conn = sqlite3.connect(self.db_path)
            cols = {r[1] for r in conn.execute("PRAGMA table_info(predictions)").fetchall()}
            if 'backfill_attempts' not in cols:
                conn.execute("ALTER TABLE predictions ADD COLUMN backfill_attempts INTEGER DEFAULT 0")
            conn.execute(
                "UPDATE predictions SET backfill_attempts = COALESCE(backfill_attempts, 0) + 1 "
                "WHERE id = ?", (prediction_id,))
            conn.commit()
            n = conn.execute(
                "SELECT backfill_attempts FROM predictions WHERE id = ?", (prediction_id,)).fetchone()
            return int(n[0]) if n and n[0] is not None else 0
        except Exception as e:
            logger.warning(f"回填计数更新失败 pred={prediction_id}: {str(e)[:60]}")
            return 0
        finally:
            if conn:
                conn.close()

    def mark_no_data(self, prediction_id: int):
        """终态标记：连续多次回填仍无 K 线（停牌/退市票）→ 退出 pending 池。

        2026-09-07 论证后实施（pending 永久滞留修复）：只写 outcomes.status，
        t1_return 保持 NULL——所有统计口径（calc_accuracy/drift/kill-switch）
        均按 t1_return IS NOT NULL 过滤，零污染。长停牌票复牌后如需恢复
        统计，可人工清除 status 重新回填。
        """
        conn = None
        try:
            conn = sqlite3.connect(self.db_path)
            # 用 UPSERT 而非 INSERT OR REPLACE：后者会整行删除重插，
            # 若该 prediction_id 已有部分回填数据（如 t1_close）会被清掉
            conn.execute(
                """INSERT INTO outcomes (prediction_id, status) VALUES (?, 'no_data')
                   ON CONFLICT(prediction_id) DO UPDATE SET
                     status='no_data', updated_at=CURRENT_TIMESTAMP""",
                (prediction_id,))
            conn.commit()
            logger.info(f"prediction {prediction_id} 标记 no_data 终态（连续回填无 K 线）")
        except Exception as e:
            logger.warning(f"no_data 终态标记失败 pred={prediction_id}: {str(e)[:60]}")
        finally:
            if conn:
                conn.close()

    def revive_stale_no_data(self, stale_days: int = 7) -> list:
        """no_data 终态低频重试（2026-09-12）：满 stale_days 天的 no_data 行
        清除 status 重新进入 pending 池，探测复牌/缓存愈合。

        背景：no_data 是"连续 30 次回填无 K 线"的终态，但 K 线缓存可能后来
        才补上（如 9/3 招商南油/新亚电子被 4:00 K 线巡逻治愈后仍被终态挡住，
        需人工清 status）。此方法让终态行每 7 天自动获得一次探测机会。

        判定 = 纯 updated_at 间隔（2026-09-12 审查轮 REJECT 修复）：不设
        attempts 上限——no_data 的唯一自然产生路径就是 bump 满 30 轮，
        attempts<30 的复活条件会把自然终态行全部挡死（死代码）且复活行
        重标后即死锁。真退市票每 7 天一次单票 get_kline 探测（缓存优先）
        成本可忽略，不设放弃线。updated_at IS NULL 的行按最老行处理
        （现实为 0 行：schema DEFAULT + 两个写点均显式写值，兜底防漂移）。

        复活后：回填成功走 update_outcomes 的 INSERT OR REPLACE 整行重写
        （status 列不在写入列表，自然归 NULL，行离开终态）；仍无 K 线则当夜
        bump 一次即 >=30 → mark_no_data（updated_at 刷新），保持"每周一探"
        节奏，不回到逐夜扫描池。

        返回复活的 prediction_id 列表（供调用方日志/测试断言）。
        """
        conn = None
        try:
            conn = sqlite3.connect(self.db_path)
            # 先取复活集合（UPDATE 之后 status 已清，无从查询）
            ids = [r[0] for r in conn.execute(
                """SELECT o.prediction_id FROM outcomes o
                   WHERE o.status = 'no_data'
                     AND (o.updated_at IS NULL
                          OR o.updated_at <= datetime('now', ?))""",
                (f'-{stale_days} days',)).fetchall()]
            if ids:
                conn.executemany(
                    "UPDATE outcomes SET status = NULL, updated_at = CURRENT_TIMESTAMP "
                    "WHERE prediction_id = ?", [(i,) for i in ids])
                conn.commit()
                logger.info(f"no_data 低频重试: 复活 {len(ids)} 条（满 {stale_days} 天）: {ids}")
            return ids
        except Exception as e:
            logger.warning(f"no_data 复活失败(忽略): {str(e)[:80]}")
            return []
        finally:
            if conn:
                conn.close()

    def get_recent_predictions(self, limit: int = 20,
                               mode: str = None) -> pd.DataFrame:
        """获取最近N条推荐记录（含结果）

        mode（2026-09-20 审查 P3-5）：可选按模式过滤（show_status 传 'short'，
        避免未来 shadow/其他模式行混入展示）。None = 不过滤（历史行为不变）。
        """
        conn = None
        try:
            conn = sqlite3.connect(self.db_path)
            query = """
                SELECT p.date, p.code, p.name, p.mode, p.score, p.rating,
                       p.buy_price,
                       o.t1_return, o.t5_return, o.t20_return
                FROM predictions p
                LEFT JOIN outcomes o ON p.id = o.prediction_id
            """
            params = []
            if mode:
                query += " WHERE p.mode = ?"
                params.append(mode)
            query += " ORDER BY p.id DESC LIMIT ?"
            params.append(limit)
            df = pd.read_sql_query(query, conn, params=tuple(params))
        finally:
            if conn:
                conn.close()
        return df
