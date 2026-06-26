"""
回测结果存储 — 将每次回测的结果持久化到 SQLite，支持版本对比

表结构：
  backtest_runs  — 每次回测运行记录（含权重快照、摘要指标、因子IC）
  backtest_trades — 交易明细（可选，回测规模不大时开启）

用法：
  store = BacktestStore()
  run_id = store.save_run(result, config)
  runs = store.list_runs(limit=10)
  diff = store.compare_runs(run_id_1, run_id_2)
"""

import json
import logging
import os
import sqlite3
from datetime import datetime
from typing import Dict, List, Optional

import pandas as pd

from core.backtest_engine import BacktestResult

logger = logging.getLogger(__name__)


class BacktestStore:
    """回测结果持久化与对比"""

    def __init__(self, db_path: str = None):
        if db_path is None:
            db_path = os.path.join(
                os.path.dirname(os.path.dirname(__file__)),
                'data', 'cache', 'backtest_cache.db'
            )
        self.db_path = db_path
        self._init_db()

    def _init_db(self):
        """初始化数据库表"""
        os.makedirs(os.path.dirname(self.db_path), exist_ok=True)
        conn = sqlite3.connect(self.db_path)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS backtest_runs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                strategy_name TEXT NOT NULL,
                mode TEXT NOT NULL,
                start_date TEXT NOT NULL,
                end_date TEXT NOT NULL,
                weight_snapshot TEXT,
                config_snapshot TEXT,
                total_trading_days INTEGER,
                total_trades INTEGER,
                win_rate REAL,
                avg_return_t1 REAL,
                avg_return_t5 REAL,
                max_drawdown REAL,
                sharpe_ratio REAL,
                benchmark_return REAL,
                strategy_return REAL,
                excess_return REAL,
                factor_performance TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS backtest_trades (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                run_id INTEGER NOT NULL,
                date TEXT NOT NULL,
                code TEXT NOT NULL,
                name TEXT,
                score REAL,
                buy_price REAL,
                return_t1 REAL,
                FOREIGN KEY (run_id) REFERENCES backtest_runs(id)
            )
        """)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.commit()
        conn.close()

    def save_run(self, result: BacktestResult, config: dict) -> int:
        """保存一次回测结果，返回 run_id"""
        # 提取权重快照
        weight_snapshot = {}
        if 'short_term' in config:
            weight_snapshot['short'] = config['short_term'].get('weights', {})
        if 'long_term' in config:
            weight_snapshot['long'] = config['long_term'].get('weights', {})

        # 配置快照（去敏）
        config_snapshot = {
            'commission_rate': config.get('commission_rate', 0.0003),
            'slippage': config.get('slippage', 0.001),
            'min_score': config.get('buy', {}).get('min_score', 60) if 'buy' in config else None,
            'max_candidates': config.get('buy', {}).get('max_candidates', 3) if 'buy' in config else None,
        }

        factor_perf_json = json.dumps(
            result.factor_performance,
            ensure_ascii=False, default=str
        )

        conn = sqlite3.connect(self.db_path)
        cur = conn.execute(
            """INSERT INTO backtest_runs
               (strategy_name, mode, start_date, end_date,
                weight_snapshot, config_snapshot,
                total_trading_days, total_trades,
                win_rate, avg_return_t1, avg_return_t5,
                max_drawdown, sharpe_ratio,
                benchmark_return, strategy_return, excess_return,
                factor_performance)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                result.strategy_name, result.period[0] if hasattr(result, 'period') and isinstance(result.period, tuple) else '',
                '',  # 暂时用后面
                '',
                json.dumps(weight_snapshot, ensure_ascii=False),
                json.dumps(config_snapshot, ensure_ascii=False),
                result.total_trading_days,
                result.total_trades,
                result.win_rate,
                result.avg_return_t1,
                result.avg_return_t5,
                result.max_drawdown,
                result.sharpe_ratio,
                result.benchmark_return,
                result.strategy_return,
                result.excess_return,
                factor_perf_json,
            )
        )
        run_id = cur.lastrowid

        # 写回 period
        conn.execute("UPDATE backtest_runs SET start_date=?, end_date=? WHERE id=?",
                     (str(result.period[0]) if hasattr(result, 'period') and result.period else '',
                      str(result.period[1]) if hasattr(result, 'period') and result.period else '',
                      run_id))

        # 存储交易明细（仅前 50 条）
        for td in result.trade_details[:50]:
            conn.execute(
                "INSERT INTO backtest_trades (run_id, date, code, name, score, buy_price, return_t1) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (run_id, td.get('date', ''), td.get('code', ''), td.get('name', ''),
                 td.get('score', 0), td.get('buy_price', 0), td.get('return_t1'))
            )

        conn.commit()
        conn.close()
        logger.info(f"回测结果已保存: run_id={run_id}")
        return run_id

    def list_runs(self, limit: int = 20) -> List[Dict]:
        """列出最近的回测运行记录"""
        conn = sqlite3.connect(self.db_path)
        rows = conn.execute(
            """SELECT id, strategy_name, mode, start_date, end_date,
                      total_trades, win_rate, avg_return_t1, sharpe_ratio,
                      max_drawdown, created_at
               FROM backtest_runs
               ORDER BY id DESC LIMIT ?""",
            (limit,)
        ).fetchall()
        conn.close()
        return [
            {
                'id': r[0], 'strategy': r[1], 'mode': r[2],
                'start': r[3], 'end': r[4],
                'trades': r[5], 'win_rate': r[6],
                'avg_return': r[7], 'sharpe': r[8],
                'drawdown': r[9], 'created': r[10],
            }
            for r in rows
        ]

    def get_run(self, run_id: int) -> Optional[Dict]:
        """获取单次回测详情"""
        conn = sqlite3.connect(self.db_path)
        row = conn.execute(
            "SELECT * FROM backtest_runs WHERE id=?", (run_id,)
        ).fetchone()
        if not row:
            conn.close()
            return None
        cols = [d[0] for d in conn.execute("PRAGMA table_info(backtest_runs)").fetchall()]
        conn.close()
        result = dict(zip(cols, row))
        # 解析 JSON 字段
        if result.get('weight_snapshot'):
            result['weight_snapshot'] = json.loads(result['weight_snapshot'])
        if result.get('factor_performance'):
            result['factor_performance'] = json.loads(result['factor_performance'])
        return result

    def compare_runs(self, run_id_a: int, run_id_b: int) -> str:
        """
        对比两次回测结果，返回格式化文本（可直接打印）
        """
        a = self.get_run(run_id_a)
        b = self.get_run(run_id_b)
        if not a or not b:
            return f"回测记录不存在: {run_id_a if not a else run_id_b}"

        lines = []
        lines.append(f"=== 回测对比: run#{run_id_a} vs run#{run_id_b} ===")
        lines.append("")

        # 参数差异
        wa = a.get('weight_snapshot', {})
        wb = b.get('weight_snapshot', {})
        # 找权重差异（先找 short 再找 long）
        for mode in ['short', 'long']:
            wa_m = wa.get(mode, wa) if isinstance(wa, dict) else {}
            wb_m = wb.get(mode, wb) if isinstance(wb, dict) else {}
            if wa_m and wb_m and wa_m != wb_m:
                lines.append(f"  权重差异 ({mode}):")
                all_keys = set(list(wa_m.keys()) + list(wb_m.keys()))
                for k in sorted(all_keys):
                    va = wa_m.get(k, '-')
                    vb = wb_m.get(k, '-')
                    if va != vb:
                        lines.append(f"    {k}: {va} → {vb}")
                lines.append("")

        # 指标对比
        metrics = [
            ('总交易次数', 'total_trades', '{:d}'),
            ('胜率', 'win_rate', '{:.1f}%'),
            ('平均收益(T+1)', 'avg_return_t1', '{:.2f}%'),
            ('平均收益(T+5)', 'avg_return_t5', '{:.2f}%'),
            ('最大回撤', 'max_drawdown', '{:.2f}%'),
            ('夏普比率', 'sharpe_ratio', '{:.2f}'),
            ('策略收益', 'strategy_return', '{:.2f}%'),
            ('沪深300', 'benchmark_return', '{:.2f}%'),
            ('超额收益', 'excess_return', '{:.2f}%'),
        ]
        lines.append(f"  {'指标':<16} {'run#{}'.format(run_id_a):>10} {'run#{}'.format(run_id_b):>10} {'变化':>10}")
        lines.append(f"  {'-'*16} {'-'*10} {'-'*10} {'-'*10}")
        for label, key, fmt in metrics:
            va = a.get(key, 0) or 0
            vb = b.get(key, 0) or 0
            diff = vb - va
            diff_str = f"{diff:+.2f}" if isinstance(diff, (int, float)) else '-'
            if '%' in fmt or '.2f' in fmt:
                diff_str = f"{diff:+.2f}%"
            elif '.1f' in fmt:
                diff_str = f"{diff:+.1f}%"
            else:
                diff_str = f"{diff:+.2f}"
            lines.append(f"  {label:<16} {fmt.format(va):>10} {fmt.format(vb):>10} {diff_str:>10}")

        lines.append("")

        # 因子 IC 对比
        fa = a.get('factor_performance', {})
        fb = b.get('factor_performance', {})
        if fa and fb:
            lines.append(f"  因子 IC 对比:")
            lines.append(f"  {'因子':<16} {'run#{}'.format(run_id_a):>14} {'run#{}'.format(run_id_b):>14}")
            lines.append(f"  {'-'*16} {'-'*14} {'-'*14}")
            all_factors = set(list(fa.keys()) + list(fb.keys()))
            for fn in sorted(all_factors):
                ia = fa.get(fn, {})
                ib = fb.get(fn, {})
                ic_a = ia.get('ic', '-')
                ic_b = ib.get('ic', '-')
                va = ia.get('verdict', '')
                vb = ib.get('verdict', '')
                ic_a_str = f"{ic_a:+.4f}" if isinstance(ic_a, (int, float)) else str(ic_a)
                ic_b_str = f"{ic_b:+.4f}" if isinstance(ic_b, (int, float)) else str(ic_b)
                lines.append(f"  {fn:<16} {ic_a_str:>8} {va:<8} {ic_b_str:>8} {vb:<8}")

        return "\n".join(lines)
