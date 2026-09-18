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
        conn = None
        try:
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
        finally:
            if conn:
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

        conn = None
        # 防御性初始化（非缺陷修复）：当前结构为 try/finally 无 except，
        # INSERT 失败时异常直接传播，并不会真正执行到下面的 logger/return，
        # 因此 UnboundLocalError 目前无法发生。
        # 此处的价值在于：若日后有人为本段补上 except 分支来吞掉异常，
        # 未初始化的 run_id 会抛 UnboundLocalError 掩盖真实错误。提前初始化可豁免该类回归。
        run_id = None
        try:
            conn = sqlite3.connect(self.db_path)
            # 从 strategy_name 提取 mode（如 "short_strategy" → "short"）
            mode = result.strategy_name.replace('_strategy', '') if result.strategy_name else ''
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
                    result.strategy_name,
                    mode,
                    str(result.period[0]) if hasattr(result, 'period') and result.period else '',
                    str(result.period[1]) if hasattr(result, 'period') and result.period else '',
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

            # 存储交易明细（全量；此前 [:50] 截断 + return_t1 字段名错位导致明细表数据不全）
            # 性能（2026-09-06）：逐条 execute → executemany 批量提交
            # （明细数百~上千笔时，单次事务内 N 次 execute 的语句开销明显）
            conn.executemany(
                "INSERT INTO backtest_trades (run_id, date, code, name, score, buy_price, return_t1) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                [(run_id, td.get('date', ''), td.get('code', ''), td.get('name', ''),
                  td.get('score', 0), td.get('buy_price', 0), td.get('return_t1'))
                 for td in result.trade_details]
            )

            # T6 实验追踪归档（2026-09-06 第一梯队）：run_meta 表记录
            # 代码版本指纹 + 完整参数快照，保证历史回测结果可复现、可对比。
            self._save_run_meta(conn, run_id, config)

            conn.commit()
        finally:
            if conn:
                conn.close()
        logger.info(f"回测结果已保存: run_id={run_id}")
        return run_id

    def _save_run_meta(self, conn, run_id: int, config: dict):
        """T6 实验追踪（2026-09-06 第一梯队）：

        为每次回测归档"可复现性快照"：
          - git_commit / git_dirty：代码版本（.git 存在时）
          - code_hashes：关键模块文件的 sha1 前 12 位（比 git 更细粒度的
            版本指纹，防止"提交了但未含全部文件"的假象）
          - sell_config / fill_convention / slippage_tiers：影响收益数字的
            全部参数（weight/config_snapshot 已有基础字段，这里补齐卖出侧）
        失败仅告警不阻塞主流程。
        """
        try:
            import hashlib
            import subprocess
            conn.execute("""
                CREATE TABLE IF NOT EXISTS run_meta (
                    run_id INTEGER PRIMARY KEY,
                    git_commit TEXT,
                    git_dirty INTEGER,
                    code_hashes TEXT,
                    sell_config TEXT,
                    fill_convention TEXT,
                    slippage_tiers TEXT,
                    created_at TEXT
                )
            """)
            root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
            git_commit, git_dirty = None, None
            try:
                git_commit = subprocess.run(
                    ['git', 'rev-parse', '--short', 'HEAD'], cwd=root,
                    capture_output=True, text=True, timeout=5).stdout.strip() or None
                dirty = subprocess.run(
                    ['git', 'status', '--porcelain'], cwd=root,
                    capture_output=True, text=True, timeout=5).stdout.strip()
                git_dirty = 1 if dirty else 0
            except Exception as e:
                # 2026-09-14 整体审查 P3-3：原为静默 pass——git 不可用（非仓库/无 git
                # 命令/超时）会让 run_meta 的 git_commit/git_dirty 恒为 None，
                # 事后无法区分"没记录"与"记录失败"。改为 debug 级留痕，不改变行为。
                logging.getLogger(__name__).debug(
                    f"run_meta git 指纹采集失败（按无 git 信息记录）: {str(e)[:80]}")
            code_hashes = {}
            for rel in ('core/backtest_engine.py', 'core/scoring_model.py',
                        'core/factor_library.py', 'strategies/short_term.py'):
                p = os.path.join(root, rel)
                if os.path.exists(p):
                    with open(p, 'rb') as f:
                        code_hashes[rel] = hashlib.sha1(f.read()).hexdigest()[:12]
            sell = (config or {}).get('sell', {})
            meta = (
                run_id, git_commit, git_dirty,
                json.dumps(code_hashes, ensure_ascii=False),
                json.dumps(sell, ensure_ascii=False, default=str),
                (config or {}).get('fill_convention'),
                json.dumps((config or {}).get('slippage_tiers'), default=str),
                datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
            )
            conn.execute(
                "INSERT OR REPLACE INTO run_meta (run_id, git_commit, git_dirty, "
                "code_hashes, sell_config, fill_convention, slippage_tiers, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)", meta)
        except Exception as e:
            logger.warning(f"run_meta 归档失败（不阻塞回测保存）: {str(e)[:60]}")

    def get_run_meta(self, run_id: int) -> Dict:
        """读取某次回测的可复现性快照；无记录返回空 dict。"""
        conn = None
        try:
            conn = sqlite3.connect(self.db_path)
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                "SELECT * FROM run_meta WHERE run_id=?", (run_id,)).fetchone()
            if row is None:
                return {}
            d = dict(row)
            for k in ('code_hashes', 'sell_config', 'slippage_tiers'):
                if d.get(k):
                    try:
                        d[k] = json.loads(d[k])
                    except Exception as e:
                        # 2026-09-18 审查 P2-7：溯源字段解析失败必须留痕（否则回测
                        # 可复现性悄悄降级：config/滑点快照变成原始字符串）。
                        logger.warning(f"run_meta 字段 {k} 解析失败（保留原值）: {str(e)[:60]}")
            return d
        except Exception as e:
            logger.warning(f"run_meta 读取失败: {str(e)[:60]}")
            return {}
        finally:
            if conn:
                conn.close()

    def list_runs(self, limit: int = 20) -> List[Dict]:
        """列出最近的回测运行记录"""
        conn = None
        try:
            conn = sqlite3.connect(self.db_path)
            rows = conn.execute(
                """SELECT id, strategy_name, mode, start_date, end_date,
                          total_trades, win_rate, avg_return_t1, sharpe_ratio,
                          max_drawdown, created_at
                   FROM backtest_runs
                   ORDER BY id DESC LIMIT ?""",
                (limit,)
            ).fetchall()
        finally:
            if conn:
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
        conn = None
        try:
            conn = sqlite3.connect(self.db_path)
            row = conn.execute(
                "SELECT * FROM backtest_runs WHERE id=?", (run_id,)
            ).fetchone()
            if not row:
                return None
            cols = [d[0] for d in conn.execute("PRAGMA table_info(backtest_runs)").fetchall()]
        finally:
            if conn:
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
