"""
数据引擎 — 多源A股数据汇聚层

数据源：
  1. 腾讯API (qt.gtimg.cn) — 实时行情快照（✅ 通，不封IP）
  2. mootdx (通达信TCP 7709) — K线、财务快照、F10（✅ 通，永不封IP）
  3. baostock — 历史K线（❌ 慢，降级备用）
  4. akshare — 股票代码列表（✅ 通），资金流/北向（❌ 被网络限制）

注意：akshare 底层走东方财富服务器，在中国大陆以外的网络环境下
所有东方财富 API 可能不可用。代码中已做好降权处理。

数据源优先级原则（参考 a-stock-data V3.2）：
  1. mootdx (TCP) — 永不封IP，K线首选
  2. 腾讯财经 (HTTP) — 不封IP，实时行情首选
  3. 新浪/同花顺 — 低封禁风险
  4. 东财 — 仅用于独有数据（龙虎榜/板块归属等），已内置限流
"""

import os
import atexit
import math
import time
import random
import logging
import threading
import json
from datetime import datetime, timedelta
from typing import Optional, List, Dict

import pandas as pd
import numpy as np
import requests
from cachetools import TTLCache

# 全局绕过系统代理
_session = requests.Session()
_session.trust_env = False
os.environ['NO_PROXY'] = '*'
os.environ['no_proxy'] = '*'

logger = logging.getLogger(__name__)

# 东财 datacenter 公开 Web token（2026-09-18 全项目审查 P3）：原硬编码在多处 URL 中，
# 现集中定义并支持环境变量覆盖（限流/吊销时可替换）。该 token 是东财网页端公开值，
# 非私密凭据；仍不应散落在业务代码里。
EASTMONEY_WEB_TOKEN = os.environ.get('EASTMONEY_TOKEN',
                                     '894050c76af8597a853f5b408b759f5d')


def sanitize_nan(obj):
    """递归清洗 NaN/Inf → None（2026-09-05 审查 P2-7）

    背景：Python 的 json.dumps 默认允许 NaN 字面量（不符合 JSON 标准），
    SQLite JSON 列读回时 json.loads 会还原成 float('nan')，进入评分路径后
    所有数值比较静默为 False，悄悄污染因子分。此函数在写入前把
    float('nan')/float('inf')/float('-inf') 统一转为 None（JSON null）。
    配合 json.dumps(..., allow_nan=False) 形成双保险：若清洗遗漏会直接抛错
    （写入失败进日志），而不是把毒数据写进缓存。
    """
    if isinstance(obj, float):
        return None if (obj != obj or obj in (float('inf'), float('-inf'))) else obj
    if isinstance(obj, dict):
        return {k: sanitize_nan(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [sanitize_nan(v) for v in obj]
    return obj

# ── 线程级 sqlite 只读连接缓存（性能迭代 2026-09-06，见 _get_read_conn）──
import threading as _threading
_thread_local = _threading.local()


def close_thread_conns():
    """关闭当前线程复用的只读连接（长任务结束/进程退出前调用，释放 WAL 句柄）。"""
    conns = getattr(_thread_local, 'conns', None)
    if not conns:
        return
    for c in list(conns.values()):
        try:
            c.close()
        except Exception:
            pass
    _thread_local.conns = {}


atexit.register(close_thread_conns)

# 模块级全市场行情二级缓存（跨 DataEngine 实例共享，键为分钟级快照）。
# 2026-09-07 回访修复：改 TTLCache 有界（防长进程内存增长），返回副本
# （防调用方原地修改污染共享缓存）
from cachetools import TTLCache as _TTLCache
_QUOTES_CACHE = _TTLCache(maxsize=8, ttl=300)


# ── 东财防封：全局节流 + 会话复用 ────────────────────────────────
# 参考 a-stock-data V3.2 的 em_get() 设计
# 所有 eastmoney.com 接口一律走 em_get()：串行限流 + 复用 Keep-Alive 会话
EM_SESSION = requests.Session()
EM_SESSION.headers.update({
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/131.0.0.0 Safari/537.36"
})
EM_SESSION.trust_env = False
EM_MIN_INTERVAL = 0.5          # 两次东财请求最小间隔(秒) — 0.5s实测安全，原1.0s太保守
_em_last_call = [0.0]          # 模块级上次请求时间戳


def em_get(url: str, params: dict = None, headers: dict = None,
           timeout: int = 15, **kwargs):
    """东财统一请求入口：自动节流 + 复用 session + 默认 UA。
    所有 eastmoney.com 接口都应通过它请求，避免高频被封 IP。"""
    wait = EM_MIN_INTERVAL - (time.time() - _em_last_call[0])
    if wait > 0:
        # 性能（2026-09-06）：随机抖动 0.1~0.5s → 0.05~0.25s。实测端到端
        # em_get 29 次共睡眠 23.2s（平均 0.8s/次），抖动降档后单次间隔仍为
        # 0.55~0.75s，防封余量保留，预计省 ~4s
        time.sleep(wait + random.uniform(0.05, 0.25))
    try:
        return EM_SESSION.get(url, params=params, headers=headers, timeout=timeout, **kwargs)
    finally:
        _em_last_call[0] = time.time()


class DataEngine:
    """核心数据引擎 — 汇聚多源A股数据"""

    def __init__(self, config: dict = None):
        self.config = config or {}
        self.cache = TTLCache(maxsize=100, ttl=60)
        self._all_codes = None
        self._big_deal_cache = None
        self._ths_fund_flow_cache = None
        self._asharehub_client = None  # 懒加载
        self._northbound_cache = {}   # {code: {date: vol}}
        self._lockup_cache = {}           # 解禁日历缓存（2026-06-16 新增）
        self._lockup_cache_date = None
        self._lockup_cache_horizon = 0
        self._mootdx_client = None  # 懒加载，重用TCP连接
        # 修复（2026-09-06 审查）：熔断标志原为类级可变字典，多实例会共享/交叉
        # 污染熔断状态。改为 __init__ 中实例化副本（类级保留作为默认模板）
        self._source_available = dict(DataEngine._source_available)
        # 稳定性（2026-09-06 迭代）：客户端懒初始化锁——3 线程池并发下
        # check-then-act 竞态会创建多个连接实例（mootdx TCP 客户端非线程安全）
        self._client_init_lock = threading.Lock()
        # 熔断分级：连续失败计数（单次瞬时/脏数据错误不再整源熔断一整天）
        self._asharehub_fail_counts = {}
        self._asharehub_fail_threshold = 3
        self._kline_cache_path = os.path.join(
            os.path.dirname(os.path.dirname(__file__)), 'data', 'cache', 'kline_cache.db'
        )
        self._init_kline_cache()

        # AShareHub 非交易日预取缓存（technical/concepts/financial 低频数据，
        # 周六用闲置配额预取 → 交易日读缓存不烧配额）
        self._asharehub_prefetch_path = os.path.join(
            os.path.dirname(os.path.dirname(__file__)), 'data', 'cache', 'asharehub_prefetch.db'
        )
        self._init_asharehub_prefetch()

        # 东财预取缓存（板块归属/龙虎榜，限流+WAF，非交易日预取到本地缓存）
        self._eastmoney_prefetch_path = os.path.join(
            os.path.dirname(os.path.dirname(__file__)), 'data', 'cache', 'eastmoney_prefetch.db'
        )
        self._init_eastmoney_prefetch()

        # 预取模式：预取脚本置 True，强制 live 拉取（绕过缓存读）。
        # 否则周六预取会命中上周缓存 → 写回同样内容 → 数据永不刷新。
        self._prefetch_mode = False

        self.SH_PREFIXES = ('600', '601', '603', '605', '688')
        self.SZ_PREFIXES = ('000', '001', '002', '003', '300', '301')

        # 数据源状态追踪
        self._latest_recovery_time = time.time()
        self._last_asharehub_call = 0.0
        self._asharehub_lock = threading.Lock()  # 节流 + 配额计数共用一把锁
        self._asharehub_budget = 100  # 日配额
        # fin_cache 财务快照 TTL（天）：超过则视为过期，回源重查。
        # 理由（2026-09-17 T1）：此前 fin_cache 无 TTL，"最新季报"缓存一旦写入
        # 永不刷新 → 新季报发布后基本面/估值因子永久陈旧。季报披露期最长约 4 个月，
        # 取 90 天留足余量，确保跨披露期必回源。
        self._fin_cache_ttl_days = 90
        self._asharehub_budget_date = ""
        self._asharehub_budget_used = 0
        # 2026-09-17 T3：文件锁不可用（只读盘等）降级为内存计数时置 True，
        # 表示跨进程配额计数已不可靠（多进程会超发），供健康报告/调用方感知。
        self._asharehub_lock_degraded = False
        # 配额持久化：跨进程共享同一账本（cron/手动/回测各自进程不再各算各的）
        self._asharehub_quota_path = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            'data', 'cache', 'asharehub_quota.json'
        )
        self._load_asharehub_quota()
        self._source_status = {
            'akshare_codes':     {'available': True,  'last_error': None, 'label': 'A股代码列表'},
            'tencent_quote':     {'available': True,  'last_error': None, 'label': '腾讯实时行情'},
            'mootdx_kline':      {'available': True,  'last_error': None, 'label': 'Mootdx K线'},
            'baostock_kline':    {'available': True,  'last_error': None, 'label': 'Baostock K线'},
            'akshare_fund_flow': {'available': True,  'last_error': None, 'label': '主力资金流(同花顺+大单)'},
            'akshare_north_flow':{'available': True,  'last_error': None, 'label': '北向资金(asharehub持仓)'},
            # 下方 3 个键于 2026-09-03 补齐：此前它们只登记在 _source_available（熔断）中，
            # 未登记在此处，导致 _update_source_status() 的 `if source_key in self._source_status`
            # 判断失败而静默丢弃 —— 这 3 个源的故障永远不会出现在健康报告中。
            'ths_fund_flow':     {'available': True,  'last_error': None, 'label': '资金流(同花顺)'},
            'big_deal':          {'available': True,  'last_error': None, 'label': '大单资金流(东财)'},
            'lockup':            {'available': True,  'last_error': None, 'label': '限售解禁日历'},
            'ths_hot':           {'available': True,  'last_error': None, 'label': '同花顺强势股'},
            'eastmoney_blocks':  {'available': True,  'last_error': None, 'label': '东财板块归属'},
            'dragon_tiger':      {'available': True,  'last_error': None, 'label': '龙虎榜'},
            'asharehub_moneyflow':{'available': True,  'last_error': None, 'label': '个股资金流(AShareHub)'},
            'asharehub_tech_factors':{'available': True, 'last_error': None, 'label': '技术因子(AShareHub)'},
            'asharehub_concepts':   {'available': True, 'last_error': None, 'label': '概念板块(AShareHub)'},
            'asharehub_financial':  {'available': True, 'last_error': None, 'label': '财务指标(AShareHub)'},
        }

    def _init_kline_cache(self):
        """初始化K线本地缓存表"""
        conn = None
        try:
            import sqlite3
            conn = sqlite3.connect(self._kline_cache_path)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS kline_cache (
                    code TEXT NOT NULL,
                    date TEXT NOT NULL,
                    open REAL, high REAL, low REAL, close REAL,
                    volume REAL, amount REAL,
                    PRIMARY KEY (code, date)
                )
            """)
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            conn.execute("""
                CREATE TABLE IF NOT EXISTS fin_cache (
                    code TEXT NOT NULL,
                    report_date TEXT NOT NULL,
                    eps REAL, roe REAL, profit REAL, income REAL,
                    bvps REAL, fetched_at TEXT,
                    PRIMARY KEY (code, report_date)
                )
            """)
            # 迁移（2026-09-17 T1）：存量库 fin_cache 可能无 fetched_at 列，
            # 补列以便 TTL 逻辑生效。SQLite 不支持 ALTER ... IF NOT EXISTS，
            # 用 try/except 吞掉"列已存在"的 OperationalError。
            try:
                conn.execute("ALTER TABLE fin_cache ADD COLUMN fetched_at TEXT")
            except Exception:
                pass
            # 2026-09-18 审查修复（性能 P2）：kline_cache 主键为 (code,date)，
            # `WHERE date>=? AND date<=?` 无可用索引 → 对 300 万行**全表 SCAN**
            # （面板构建/回测预热/预取均受影响，EXPLAIN 已实测确认）。补 date 索引。
            try:
                conn.execute("CREATE INDEX IF NOT EXISTS idx_kline_date "
                             "ON kline_cache(date)")
            except Exception as e:
                logger.warning(f"kline date 索引创建失败（不影响功能，仅性能）: {str(e)[:60]}")
            conn.commit()
        except Exception as e:
            logger.warning(f"K线缓存初始化失败: {e}")
        finally:
            if conn:
                conn.close()

    # ── AShareHub 非交易日预取缓存 ──────────────────────────────
    # 背景：ASHareHub 日配额 100 次，非交易日（周末/节假日）cron 不跑，配额闲置。
    # technical_factors / concepts / financial_indicators 都是低频数据（不随交易日
    # 变化），周六用闲置配额批量预取到本地，交易日详评时先查缓存命中则不消耗配额。
    # 注意：moneyflow 是当日资金流，预取无用，不在预取范围内。

    def _init_asharehub_prefetch(self):
        """初始化预取缓存表：tech_factors / concepts / financial 三张表"""
        conn = None
        try:
            import sqlite3
            conn = sqlite3.connect(self._asharehub_prefetch_path)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS tech_factors (
                    code TEXT PRIMARY KEY,
                    data TEXT NOT NULL,
                    fetched_at TEXT NOT NULL
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS concepts (
                    code TEXT PRIMARY KEY,
                    data TEXT NOT NULL,
                    fetched_at TEXT NOT NULL
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS financial (
                    code TEXT PRIMARY KEY,
                    data TEXT NOT NULL,
                    fetched_at TEXT NOT NULL
                )
            """)
            conn.execute("PRAGMA journal_mode=WAL")
            conn.commit()
        except Exception as e:
            logger.warning(f"AShareHub预取缓存初始化失败: {e}")
        finally:
            if conn:
                conn.close()

    def _init_eastmoney_prefetch(self):
        """初始化东财预取缓存 SQLite（板块归属/龙虎榜）"""
        import sqlite3
        conn = None
        try:
            conn = sqlite3.connect(self._eastmoney_prefetch_path)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS blocks (
                    code TEXT PRIMARY KEY, data TEXT, fetched_at TEXT
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS dragon_tiger (
                    code TEXT PRIMARY KEY, data TEXT, fetched_at TEXT
                )
            """)
            conn.commit()
        except Exception as e:
            logger.warning(f"东财预取缓存初始化失败: {e}")
        finally:
            if conn:
                conn.close()

    def _read_eastmoney_prefetch(self, table: str, code: str, max_age_days: int = 7) -> Optional[dict]:
        """读东财预取缓存。table: 'blocks' | 'dragon_tiger'。过期返回 None。"""
        try:
            # 性能（2026-09-06）：复用线程级只读连接
            conn = self._get_read_conn(self._eastmoney_prefetch_path)
            row = conn.execute(
                "SELECT data, fetched_at FROM {} WHERE code = ?".format(table),
                (str(code).zfill(6),)
            ).fetchone()
            if row:
                from datetime import datetime as _dt
                age = (_dt.now() - _dt.strptime(row[1][:19], '%Y-%m-%d %H:%M:%S')).days
                if age <= max_age_days:
                    return json.loads(row[0])
        except Exception:
            pass
        return None

    def _write_eastmoney_prefetch(self, table: str, code: str, data: dict):
        """写入东财预取缓存（INSERT OR REPLACE）"""
        import sqlite3
        from datetime import datetime as _dt
        conn = None
        try:
            conn = sqlite3.connect(self._eastmoney_prefetch_path)
            conn.execute(
                "INSERT OR REPLACE INTO {} (code, data, fetched_at) VALUES (?, ?, ?)".format(table),
                (str(code).zfill(6), json.dumps(data, ensure_ascii=False),
                 _dt.now().strftime('%Y-%m-%d %H:%M:%S'))
            )
            conn.commit()
        except Exception as e:
            logger.warning(f"东财预取缓存写入失败: {e}")
        finally:
            if conn:
                conn.close()

    def _read_asharehub_prefetch(self, table: str, code: str, max_age_days: int = 14) -> Optional[dict]:
        """读预取缓存。table: 'tech_factors' | 'concepts' | 'financial'
        返回 dict 或 None（未命中/过期）。过期时间按表设置：
        technical/concepts 周末预取周一用，7天内都新鲜；financial 季度级，14天宽限。
        """
        conn = None
        try:
            import sqlite3, json
            from datetime import datetime
            # 性能（2026-09-06）：复用线程级只读连接
            conn = self._get_read_conn(self._asharehub_prefetch_path)
            row = conn.execute(
                f"SELECT data, fetched_at FROM {table} WHERE code=?",
                (code,)
            ).fetchone()
            if not row:
                return None
            fetched = datetime.strptime(row[1], '%Y-%m-%d %H:%M:%S')
            if (datetime.now() - fetched).days > max_age_days:
                return None  # 过期，忽略（下次调用走实时API）
            return json.loads(row[0])
        except Exception as e:
            logger.warning(f"AShareHub预取缓存读取失败 {table}/{code}: {str(e)[:60]}")
            return None

    @staticmethod
    def _is_shell_tech_record(data) -> bool:
        """判定技术因子"壳记录"：所有数值因子字段都无有效值（2026-09-16）

        上游 ASHareHub `technical-factors` 对部分股票返回只有 close_hfq 的
        空壳记录（macd_* / rsi_* / cci 全为空）。实测占预取缓存 126 条中的
        23 条（18.3%）。此类记录**不含任何可用信息**，写进缓存后会借 7 天
        TTL 长期驻留，是 2026-09-16 流水线崩溃的直接弹药来源。
        判定口径与 `TechnicalScorer.score_from_asharehub` 的消费字段一致。
        """
        if not isinstance(data, dict):
            return True

        def _finite(v):
            return (isinstance(v, (int, float)) and not isinstance(v, bool)
                    and math.isfinite(v))
        usable = ('macd_dif', 'macd_dea', 'macd', 'rsi_6', 'rsi_12', 'rsi_24', 'cci')
        return not any(_finite(data.get(k)) for k in usable)

    def _write_asharehub_prefetch(self, table: str, code: str, data: dict) -> bool:
        """写入预取缓存（预取脚本专用，INSERT OR REPLACE 覆盖）

        2026-09-16 P0-1 预防：`tech_factors` 写入前做 schema 校验，**拒收壳记录**
        （MACD/RSI/CCI 全空的记录）。返回 False 表示未写入，调用方应计入 miss。
        权衡：拒收意味着该股在下一个交易日会走实时请求（多做一次配额消耗，按
        实测壳记录占比 18.3%、单批 30 只估算约 +5~6 次/日），换来的是不让
        无信息记录占据缓存并借 TTL 长期驻留——记录本身不含数据，缓存与实时
        取值的效果相同，因此"不缓存"只损失少量配额、不损失信息。
        """
        if table == 'tech_factors' and self._is_shell_tech_record(data):
            logger.warning(
                f"预取拒收壳记录 tech_factors/{code}: MACD/RSI/CCI 全空 "
                f"（不写缓存，避免无信息记录借 TTL 驻留）")
            return False
        conn = None
        try:
            import sqlite3, json
            from datetime import datetime
            conn = sqlite3.connect(self._asharehub_prefetch_path)
            # P2-7：写入前清洗 NaN/Inf → None；allow_nan=False 兜底（遗漏即抛错不落盘）
            safe_data = sanitize_nan(data)
            conn.execute(
                f"INSERT OR REPLACE INTO {table} (code, data, fetched_at) VALUES (?, ?, ?)",
                (code, json.dumps(safe_data, ensure_ascii=False, allow_nan=False),
                 datetime.now().strftime('%Y-%m-%d %H:%M:%S'))
            )
            conn.commit()
            return True
        except Exception as e:
            logger.warning(f"AShareHub预取缓存写入失败 {table}/{code}: {str(e)[:60]}")
            return False
        finally:
            if conn:
                conn.close()

    def _get_read_conn(self, path: str):
        """性能（2026-09-06 迭代）：按线程复用只读 sqlite 连接。

        背景：cProfile 实测 sqlite3.Connection.close 单次约 100ms（WAL 下
        close 会触发 checkpoint），详评 50 只票 × 2 次读（kline + fin）
        就是 ~10.7s，占端到端 168s 的 6.4%。改为线程级长连接后，
        同一线程内后续读取不再开关连接。

        线程安全：连接存于 threading.local，各线程独立（sqlite 默认
        check_same_thread=True，不可跨线程共享）。写路径仍用独立短连接，
        避免长事务与锁竞争。
        """
        conns = getattr(_thread_local, 'conns', None)
        if conns is None:
            conns = {}
            _thread_local.conns = conns
        conn = conns.get(path)
        if conn is None:
            import sqlite3
            conn = sqlite3.connect(path, timeout=30)
            try:
                conn.execute("PRAGMA journal_mode=WAL")
            except Exception:
                pass
            conns[path] = conn
        return conn

    def _get_kline_from_cache(self, code: str, start_date: str, end_date: str) -> Optional[pd.DataFrame]:
        """从本地缓存读取K线"""
        try:
            conn = self._get_read_conn(self._kline_cache_path)
            df = pd.read_sql_query(
                "SELECT date,open,high,low,close,volume,amount FROM kline_cache "
                "WHERE code=? AND date>=? AND date<=? ORDER BY date",
                conn, params=(code, start_date, end_date)
            )
            if not df.empty:
                df['date'] = pd.to_datetime(df['date'])
                for c in ['open', 'high', 'low', 'close', 'volume', 'amount']:
                    df[c] = pd.to_numeric(df[c], errors='coerce')
                return df
            return None
        except Exception as e:
            logger.warning(f"K线缓存读取失败 {code}: {str(e)[:80]}")
            return None

    def _save_kline_to_cache(self, code: str, df: pd.DataFrame):
        """将K线写入本地缓存"""
        if df is None or df.empty:
            return
        conn = None
        try:
            import sqlite3
            conn = sqlite3.connect(self._kline_cache_path)
            rows = []
            for _, row in df.iterrows():
                rows.append((
                    code,
                    row['date'].strftime('%Y-%m-%d') if hasattr(row['date'], 'strftime') else str(row['date']),
                    row.get('open', 0), row.get('high', 0), row.get('low', 0),
                    row.get('close', 0), row.get('volume', 0), row.get('amount', 0),
                ))
            conn.executemany(
                "INSERT OR REPLACE INTO kline_cache (code,date,open,high,low,close,volume,amount) "
                "VALUES (?,?,?,?,?,?,?,?)", rows
            )
            conn.commit()
        except Exception as e:
            logger.warning(f"K线缓存写入失败 {code}: {e}")
        finally:
            if conn:
                conn.close()

    def _fetch_kline_mootdx(self, code: str, start_date: str, end_date: str) -> Optional[pd.DataFrame]:
        """
        用 mootdx (通达信 TCP 协议) 拉取个股日K线
        来源：a-stock-data §1.1
        优势：TCP 二进制协议，永不封 IP，~0.01s/只（复用连接更接近0.01s）
        """
        import socket
        old_timeout = socket.getdefaulttimeout()
        socket.setdefaulttimeout(10.0)
        try:
            client = self._get_mootdx_client()
            code6 = str(code).zfill(6)
            klines = client.bars(symbol=code6, category=4, offset=600)
            if klines is not None and len(klines) > 0:
                import pandas as _pd
                df = _pd.DataFrame(klines)
                # mootdx datetime 格式 "2023-12-18 15:00"，取前10字符为日期
                df['date'] = _pd.to_datetime(df['datetime'].astype(str).str[:10], errors='coerce')
                df = df[df['date'].between(_pd.Timestamp(start_date), _pd.Timestamp(end_date))]
                df = df.sort_values('date').reset_index(drop=True)
                if not df.empty:
                    for c in ['open', 'high', 'low', 'close', 'volume', 'amount']:
                        df[c] = _pd.to_numeric(df[c], errors='coerce')
                    return df
            return None
        except Exception as e:
            logger.warning(f"mootdx K线失败 {code}: {str(e)[:50]}")
            return None
        finally:
            # 2026-09-18 审查修复（P2-8）：socket.setdefaulttimeout 是**进程级**全局设置，
            # 原实现设置后从不复原 → 污染同进程内所有后续网络调用（长进程 cron / 测试）。
            # 用 finally 保证任何路径（含异常/提前 return）都恢复原值。
            socket.setdefaulttimeout(old_timeout)

    def _fetch_kline_sina(self, code: str, start_date: str, end_date: str) -> Optional[pd.DataFrame]:
        """
        用新浪财经 API 拉取个股日K线
        来源：Ashare 项目
        速度：~0.2s/只
        """
        try:
            import requests as req
            prefix = 'sh' if str(code).zfill(6).startswith(('6', '9')) else 'sz'
            symbol = f"{prefix}{str(code).zfill(6)}"
            url = "http://money.finance.sina.com.cn/quotes_service/api/json_v2.php/CN_MarketData.getKLineData"
            params = {"symbol": symbol, "scale": "240", "ma": "5", "datalen": "200"}
            r = req.get(url, params=params,
                        headers={"User-Agent": "Mozilla/5.0 Chrome/131.0.0.0"},
                        timeout=10)
            if r.status_code == 200 and len(r.text) > 50:
                import json
                data = json.loads(r.text)
                if data:
                    rows = []
                    for d in data:
                        try:
                            rows.append({
                                'date': d.get('day', ''),
                                'open': float(d.get('open', 0)),
                                'high': float(d.get('high', 0)),
                                'low': float(d.get('low', 0)),
                                'close': float(d.get('close', 0)),
                                'volume': float(d.get('volume', 0)),
                            })
                        except (ValueError, TypeError):
                            continue
                    if rows:
                        df = pd.DataFrame(rows)
                        df['date'] = pd.to_datetime(df['date'])
                        df = df.sort_values('date').reset_index(drop=True)
                        return df
            return None
        except Exception as e:
            logger.warning(f"新浪K线失败 {code}: {str(e)[:50]}")
        return None

    # ========== 1. A股代码列表（本地缓存加速） ==========

    def _get_all_codes(self) -> list:
        """A股代码列表 — 优先读本地JSON缓存，每天刷新一次"""
        if self._all_codes is not None:
            return self._all_codes

        # 从本地缓存读取
        cached = self._read_codes_cache()
        if cached:
            self._all_codes = cached
            self._update_source_status('akshare_codes', True)
            return cached

        # 远程拉取
        for attempt in range(3):
            try:
                import akshare as ak
                df = ak.stock_info_a_code_name()
                codes = []
                for _, row in df.iterrows():
                    code = str(row['code']).zfill(6)
                    name = str(row.get('code_name', ''))
                    if ('ST' not in name and '退' not in name
                        and (code.startswith(self.SH_PREFIXES) or code.startswith(self.SZ_PREFIXES))):
                        codes.append(code)
                self._all_codes = codes
                self._write_codes_cache(codes)
                self._update_source_status('akshare_codes', True)
                logger.info(f"A股代码列表: {len(codes)} 只")
                return codes
            except Exception as e:
                self._update_source_status('akshare_codes', False, str(e)[:60])
                if attempt < 2:
                    logger.warning(f"代码列表获取失败(将重试第{attempt+2}次): {e}")
                    time.sleep(2)
                else:
                    logger.warning(f"代码列表获取失败(已重试3次): {e}")

        # 最后尝试：读缓存（哪怕过期了也比没有好）
        cached = self._read_codes_cache(ignore_expiry=True)
        if cached:
            logger.warning("使用过期的代码列表缓存（远程获取失败）")
            self._all_codes = cached
            return cached
        return []

    def _codes_cache_path(self) -> str:
        return os.path.join(
            os.path.dirname(os.path.dirname(__file__)), 'data', 'cache', 'codes_cache.json'
        )

    def _read_codes_cache(self, ignore_expiry: bool = False) -> Optional[list]:
        """读本地代码列表缓存（默认需当天有效）"""
        path = self._codes_cache_path()
        try:
            if not os.path.exists(path):
                return None
            with open(path, 'r', encoding='utf-8') as f:
                import json as _json
                data = _json.load(f)
            if not ignore_expiry:
                cached_date = data.get('date', '')
                if cached_date != datetime.now().strftime('%Y-%m-%d'):
                    return None  # 过期
            return data.get('codes', [])
        except Exception as e:
            logger.warning(f"代码缓存读取失败: {str(e)[:80]}")
            return None

    def _write_codes_cache(self, codes: list):
        """写本地代码列表缓存"""
        path = self._codes_cache_path()
        try:
            import json as _json
            with open(path, 'w', encoding='utf-8') as f:
                _json.dump({
                    'date': datetime.now().strftime('%Y-%m-%d'),
                    'codes': codes,
                    'count': len(codes),
                }, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.warning(f"代码列表缓存写入失败: {e}")

    # ========== 2. 全市场实时行情（腾讯API） ==========

    def _recover_sources(self):
        """每 10 分钟自动恢复所有已熔断数据源，避免网络抖动永久禁用一个源。
        asharehub 系列源除外：它们由配额管理控制（耗尽→跨天重置），
        不能被这里的自动恢复抹掉"配额已用完"的报告标注。"""
        now = time.time()
        if now - self._latest_recovery_time < 600:
            return
        self._latest_recovery_time = now
        asharehub_keys = {'asharehub_moneyflow', 'asharehub_tech_factors',
                          'asharehub_concepts', 'asharehub_financial'}
        for key in self._source_available:
            if key in asharehub_keys:
                continue  # 配额源由 _asharehub_budget_ok 跨天管理
            if not self._source_available[key]:
                self._source_available[key] = True
                logger.info(f"数据源自动恢复: {key}")
        for name, status in self._source_status.items():
            if name in asharehub_keys:
                continue
            if not status['available']:
                status['available'] = True
                status['last_error'] = None
                logger.info(f"数据源自动恢复: {status.get('label', name)}")

    # ── ASHareHub 配额管理（跨进程共享 + 线程安全 + 原子账本） ──────────
    # 背景：ASHareHub 免费档日配额 100 次。此前配额计数是进程内存变量，
    # 每个进程（cron/手动/回测）独立从 0 计数，且详评阶段 3 线程并发
    # 下计数竞态 → 实际调用数远超配额 → 服务端 429 → 因子全中性 → 报告"跳过"。
    # 修复（V2，2026-08-07 审查后加强）：
    #   - 配额账本持久化 data/cache/asharehub_quota.json，跨进程共享
    #   - 每次消耗前加跨进程文件锁（msvcrt/fcntl），锁内重读文件再自增，
    #     杜绝"每个进程各自预留到 90 → 合计超 100"的丢失更新
    #   - 写盘用 temp + os.replace 原子替换，避免并发写截断损坏账本
    #   - 节流(0.3s)并入消耗路径，真正生效（此前是死代码从未被调用）

    def _load_asharehub_quota(self):
        """从磁盘加载当日配额账本（仅内存缓存）；跨天则重置"""
        today = datetime.now().strftime('%Y-%m-%d')
        try:
            if os.path.exists(self._asharehub_quota_path):
                with open(self._asharehub_quota_path, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                if data.get('date') == today:
                    self._asharehub_budget_date = today
                    self._asharehub_budget_used = int(data.get('used', 0))
                    return
        except Exception as e:
            logger.warning(f"asharehub配额文件读取失败: {str(e)[:60]}")
        # 跨天或文件损坏：重置内存缓存（不覆写磁盘，等下次消耗时锁内重建）
        self._asharehub_budget_date = today
        self._asharehub_budget_used = 0

    def _read_quota_file(self, today: str = None) -> int:
        """锁内读取配额账本（文件锁已持有），返回今日已用次数（损坏/跨天归零）。

        2026-09-17 T4：today 可由调用方传入，确保与 _write_quota_atomic 使用同一
        日期口径（跨午夜场景读取与写盘不再各取各的"当前日期"）。
        """
        if today is None:
            today = datetime.now().strftime('%Y-%m-%d')
        try:
            if os.path.exists(self._asharehub_quota_path):
                with open(self._asharehub_quota_path, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                if data.get('date') == today:
                    return int(data.get('used', 0))
        except Exception as e:
            # 2026-09-18 审查 P2-7：配额账本读失败若静默归 0 → 可能超发（触发服务端
            # 429 → 因子静默中性化）。这里必须"响一声"，便于事后归因。
            logger.warning(f"配额账本读取失败（按 0 计数，存在超发风险）: {str(e)[:80]}")
        return 0

    def _write_quota_atomic(self, used: int, today: str = None):
        """原子写配额账本：temp 文件 + os.replace（账本文件不持有句柄，可被替换）。

        2026-09-17 T4：today 由调用方在进入函数时计算并传入，锁内不再重新取当前
        时间 —— 否则 23:59 发起、00:01 写盘的请求会把账本日期写成次日，与读取时
        的日期口径不一致导致跨午夜配额错乱/重复计数。today 缺省时回退到当前日期
        （兼容其他调用点）。
        """
        if today is None:
            today = datetime.now().strftime('%Y-%m-%d')
        tmp_path = self._asharehub_quota_path + '.tmp'
        try:
            with open(tmp_path, 'w', encoding='utf-8') as f:
                json.dump({'date': today, 'used': used}, f)
            os.replace(tmp_path, self._asharehub_quota_path)
        except Exception as e:
            logger.warning(f"asharehub配额文件写入失败: {str(e)[:60]}")

    def _consume_asharehub_quota(self) -> bool:
        """跨进程原子消耗一次配额（独立锁文件 + 锁内重读 + 原子写回）。

        返回 True=消耗成功；False=配额已满（90/100 预留余量）。
        调用方必须先持有 self._asharehub_lock（线程锁），保证进程内串行。

        设计：用独立的 .lock 文件做跨进程互斥（Windows msvcrt / POSIX fcntl），
        账本文件本身通过 temp+os.replace 原子替换。锁文件句柄常驻但不被替换，
        避免 Windows 上 os.replace 无法覆盖已打开文件的限制。
        """
        # 2026-09-17 T4：进入函数即固定 today，作为读取与写盘的统一年份口径，
        # 锁内不再各自 datetime.now()——杜绝 23:59 发起、00:01 写盘把账本日期写成
        # 次日导致的跨午夜配额错乱。
        today = datetime.now().strftime('%Y-%m-%d')
        path = self._asharehub_quota_path
        lock_path = path + '.lock'
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o644)
            try:
                # 锁文件必须至少 1 字节才能加锁（Windows msvcrt 要求）
                if os.fstat(fd).st_size == 0:
                    os.write(fd, b'{}')
                # 跨进程文件锁
                if os.name == 'nt':
                    import msvcrt
                    os.lseek(fd, 0, os.SEEK_SET)
                    msvcrt.locking(fd, msvcrt.LK_LOCK, 1)
                else:
                    import fcntl
                    fcntl.flock(fd, fcntl.LOCK_EX)
                try:
                    used = self._read_quota_file(today)
                    if used >= self._asharehub_budget - 10:
                        # 配额已满：同步内存缓存 + 标记源状态（报告显示原因）
                        self._asharehub_budget_used = used
                        self._mark_asharehub_quota_exhausted()
                        return False
                    used += 1
                    self._write_quota_atomic(used, today)
                    self._asharehub_budget_used = used
                    self._asharehub_budget_date = today
                    return True
                finally:
                    if os.name == 'nt':
                        os.lseek(fd, 0, os.SEEK_SET)
                        msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
                    else:
                        fcntl.flock(fd, fcntl.LOCK_UN)
            finally:
                os.close(fd)
        except Exception as e:
            # 文件锁不可用（如只读盘）：降级为内存计数。
            # 注意：调用方 _asharehub_budget_ok 已持线程锁，这里不能再
            # with self._asharehub_lock（非重入锁会死锁），直接改内存即可。
            # 2026-09-17 T3：此前静默 warning，多进程会超发配额（各进程独立内存
            # 计数互不可见）。升级为 error 并标记 lock_degraded=True，便于调用方/
            # 健康报告感知"配额计数已不可靠"。
            logger.error(f"asharehub配额文件锁失败(降级内存计数,多进程可能超发): {str(e)[:60]}")
            self._asharehub_lock_degraded = True
            today = datetime.now().strftime('%Y-%m-%d')
            if self._asharehub_budget_date != today:
                self._asharehub_budget_date = today
                self._asharehub_budget_used = 0
            if self._asharehub_budget_used >= self._asharehub_budget - 10:
                # 降级模式配额耗尽也标记源状态，避免 4 个 wrapper 对 200 股各
                # 调一次 budget_ok 都走完整重试 → 800 条 warning + 240s 空转
                self._mark_asharehub_quota_exhausted()
                return False
            self._asharehub_budget_used += 1
            return True

    def _mark_asharehub_quota_exhausted(self):
        """配额耗尽：同步 _source_available 熔断 + _source_status 报告标注

        文案口径（2026-09-16 P2-3）：必须说明是**本地安全闸门**而非服务端配额。
        真实闸门 = budget - 10（预留 10 次给手动验证），默认 90/100；原文案写
        "日配额已用完(100次/天)"会让人误判为服务端 429 限流，从而去查上游状态。
        """
        keys = ('asharehub_moneyflow', 'asharehub_tech_factors',
                'asharehub_concepts', 'asharehub_financial')
        gate = self._asharehub_budget - 10
        msg = (f'AShareHub 已达本地安全闸门({gate}/{self._asharehub_budget}，'
               f'预留 10 次)，因子降为中性')
        for k in keys:
            self._source_available[k] = False
            if hasattr(self, '_source_status'):
                self._update_source_status(k, False, msg)

    def _asharehub_budget_ok(self) -> bool:
        """检查 ASHareHub 日配额是否还有余额，消耗一次。

        线程锁（进程内串行）+ 跨进程文件锁（原子账本）。
        配额耗尽时同步标记 _source_available + _source_status，报告可显示原因。
        """
        with self._asharehub_lock:
            # 节流：两次调用间隔不少于 0.3s（此前 _rate_limit_asharehub 是
            # 死代码从未被调用，节流从未生效；并入此处真正生效）
            elapsed = time.time() - self._last_asharehub_call
            if elapsed < 0.3:
                time.sleep(0.3 - elapsed)
            self._last_asharehub_call = time.time()

            today = datetime.now().strftime('%Y-%m-%d')
            if self._asharehub_budget_date != today:
                # 跨天重置：恢复 asharehub 源状态（配额恢复）
                self._asharehub_budget_date = today
                self._asharehub_budget_used = 0
                for k in ('asharehub_moneyflow', 'asharehub_tech_factors',
                          'asharehub_concepts', 'asharehub_financial'):
                    self._source_available[k] = True
                    if hasattr(self, '_source_status'):
                        self._update_source_status(k, True)
            return self._consume_asharehub_quota()

    def get_all_quotes(self) -> pd.DataFrame:
        """全市场实时行情快照 — 腾讯API，每批200只，失败自动重试一次"""
        self._recover_sources()
        cache_key = f"quotes_{datetime.now().strftime('%Y-%m-%d_%H:%M')}"
        if cache_key in self.cache:
            return self.cache[cache_key]
        # 模块级二级缓存（跨 DataEngine 实例共享，见下方写入处说明）
        if cache_key in _QUOTES_CACHE:
            cached_df = _QUOTES_CACHE[cache_key]
            self.cache[cache_key] = cached_df
            return cached_df.copy()

        codes = self._get_all_codes()
        if not codes:
            return pd.DataFrame()

        results = []
        # 性能（2026-09-06）：批大小 200 → 400。实测腾讯单请求 400 只与 200 只
        # 耗时相同（~0.28s），批次间 0.1s 节流次数由 26 次降为 13 次，
        # 每次全市场拉取省约 4s（端到端实测 sleep 占 8.1s → ~4s）
        batch_size = 400
        total = len(codes)

        for i in range(0, total, batch_size):
            batch = codes[i:i+batch_size]
            t_codes = ['sh' + c if c.startswith(self.SH_PREFIXES) else 'sz' + c for c in batch]

            time.sleep(0.1)  # 腾讯不封IP，保留少量间隔
            for attempt in range(2):
                try:
                    r = _session.get(f"http://qt.gtimg.cn/q={','.join(t_codes)}", timeout=15)
                    r.encoding = 'gbk'
                    for line in r.text.strip().split('\n'):
                        if '~' not in line:
                            continue
                        fields = line.split('"')[1].split('~')
                        if len(fields) < 40:
                            continue
                        code6 = line.split('=')[0].strip().replace('v_', '').replace('"', '')[2:]
                        price = self._safe_float(fields[3])
                        if price <= 0:
                            continue
                        pre_close = self._safe_float(fields[4])
                        results.append({
                            'code': code6, 'name': fields[1], 'price': price,
                            'pct_chg': round((price - pre_close) / pre_close * 100, 2) if pre_close else 0.0,
                            'pre_close': pre_close,
                            'open': self._safe_float(fields[5]),
                            'high': self._safe_float(fields[33]),
                            'low': self._safe_float(fields[34]),
                            'volume': self._safe_float(fields[6]),
                            # 腾讯 fields[37] 成交额单位是万元，统一转为元
                            # （risk_filter min_amount 按元比较，此前单位错位导致
                            #   全市场股票都触发"成交额不足" penalty=0.5 → 分数被 ×0.75）
                            'amount': self._safe_float(fields[37]) * 10000,
                            'turnover': self._safe_float(fields[38]),
                            # 腾讯 fields[49] 量比（无量纲）。此前漏解析导致
                            # volume_ratio 恒 1.0 → volume_price 恒 52.5 零区分度。
                            # 无效值回退 1.0（保持旧行为），避免数据未就绪时误压低分。
                            'volume_ratio': self._safe_float(fields[49]) if len(fields) > 49 and self._safe_float(fields[49]) > 0 else 1.0,
                            'pe': self._safe_float(fields[39]),
                            'pb': self._safe_float(fields[46]) if len(fields) > 46 else None,
                            # 腾讯市值字段单位是亿元，统一转为元（与 factor_library 的
                            # calc_fundamental_score "mcap: 总市值（元）" 文档一致；此前
                            # 亿级数值永远过不了 >1000亿 阈值，大市值加分死代码）
                            'total_market_cap': self._safe_float(fields[45]) * 1e8 if len(fields) > 45 and self._safe_float(fields[45]) > 0 else None,
                            'circulating_market_cap': self._safe_float(fields[44]) * 1e8 if len(fields) > 44 and self._safe_float(fields[44]) > 0 else None,
                        })
                    break  # 成功则跳出重试循环
                except Exception as e:
                    if attempt == 0:
                        logger.warning(f"腾讯批次{i}失败(将重试): {str(e)[:60]}")
                        time.sleep(1.0)
                    else:
                        logger.warning(f"腾讯批次{i}重试仍失败: {str(e)[:60]}")

            if (i // batch_size) % 5 == 0:
                logger.info(f"  行情进度: {min(100, (i+batch_size)*100//total)}% ({len(results)}只)")

        if not results:
            return pd.DataFrame()
        df = pd.DataFrame(results)
        self.cache[cache_key] = df
        # 性能（2026-09-06）：模块级共享。短线与长线策略各自持有独立
        # DataEngine 实例，实例内 TTL 缓存不共享 → 同一分钟内全市场 5213 只
        # 被拉取两次（各约 7s）。改为模块级二级缓存（同样按分钟失效，
        # 数据陈旧 ≤1 分钟，与单实例行为一致）
        _QUOTES_CACHE[cache_key] = df
        self._update_source_status('tencent_quote', True)
        logger.info(f"全市场行情: {len(df)} 只股票")
        return df.copy()

    # ========== 3. 个股K线（mootdx → Sina → baostock 三源回退） ==========

    def get_kline(self, code: str, start_date: str = None,
                  end_date: str = None, adjust: str = 'none') -> pd.DataFrame:
        """
        个股历史K线（含本地缓存）

        数据源优先级（参考 a-stock-data V3.2）：
          1. mootdx (TCP 通达信) — 永不封IP，~0.01s/只
          2. 新浪HTTP — ~0.2s/只，40x快于baostock
          3. baostock — ~8s/只，降级备用

        adjust 参数（2026-08-13 加，复权口径统一）：
          - 'none'（默认）：不复权原始价。策略评分路径用——缓存存原始价，
            详评 200 只快速拉取，短线 T+1 收益从实盘买入价算，不跨除权日没问题。
          - 'qfq'：前复权。仅供 tracker.update_outcomes 回填用——绕开 raw 缓存，
            直接 baostock adjustflag='2' 拉前复权价，不读不写缓存（避免污染
            raw 缓存口径）。除权日价差不失真。
          - 'hfq'：后复权。备用（baostock adjustflag='1'，同旧降级路径）。

        返回字段: date,open,high,low,close,volume,amount
        含 pct_chg 和技术指标

        缓存残缺检测（2026-08-12 修，§34）：缓存命中后比较返回行数 vs 期望行数，
        残缺超过阈值 → 缓存不可信，fall through 去 mootdx 补抓。
        背景：之前缓存的 K 线可能只存了零散几天（mootdx offset/cron 间隔/停牌等），
        导致 tracker.update_outcomes 拿到 K 线缺月 → T+N 找到的"下一交易日"实际
        是几周后的某天，污染 outcome 统计。
        """
        if start_date is None:
            start_date = (datetime.now() - timedelta(days=200)).strftime('%Y-%m-%d')
        if end_date is None:
            end_date = datetime.now().strftime('%Y-%m-%d')

        # 复权路径：qfq/hfq 绕开 raw 缓存，直接 baostock（mootdx/新浪无复权参数）
        if adjust in ('qfq', 'hfq'):
            return self._fetch_kline_baostock(code, start_date, end_date,
                                              adjustflag='2' if adjust == 'qfq' else '1',
                                              use_cache=False)

        # 1. 先读缓存
        # 注意：检测"整体连续性"需往前多拉一段（缺口可能落在窗口起点附近/之外）。
        # 查询范围 = [start_date - 60 天, end_date]，检测完整缓存断档，再裁切回原窗口。
        _probe_start = (pd.Timestamp(start_date) - pd.Timedelta(days=60)).strftime('%Y-%m-%d')
        cached = self._get_kline_from_cache(code, _probe_start, end_date)
        if cached is not None and not cached.empty:
            # 缓存残缺检测：完整缓存内相邻交易日间隔 > 8 个工作日 → 视为残缺
            # （缺口），fall through 到 mootdx 补抓。这比行数比例更准：
            #  - 短窗口（几天）行数少是正常，但相邻间隔能暴露"中间缺了一整段"
            #  - 停牌会形成长间隔，但 outcome 计算本来就需要完整交易日序列，
            #    残缺的缓存不该被直接使用（置 None 由 tracker 处理）
            # 修复（2026-09-06）：原用自然日间隔 >7 天，A股国庆/春节长假（8-11
            # 自然日）必然误报"残缺"导致全市场重抓。改为按工作日间隔计算
            # （np.busday_count 排除周末，需 datetime64[D] 输入），阈值 10 个
            # 工作日：A股休市最长约 9 个工作日（国庆中秋极端调休），不引入
            # 节假日历的前提下取 10 不会误报；而"缓存零散缺段"的真实缺口
            # 通常达数周~数月，仍能稳定检出。
            # 注意：裁切回原窗口前先检测。若检测通过，仅返回原窗口数据。
            dates_sorted = cached['date'].sort_values().reset_index(drop=True)
            if len(dates_sorted) >= 2:
                _dv = dates_sorted.values.astype('datetime64[D]')
                bday_gaps = np.busday_count(_dv[:-1], _dv[1:])
                max_gap = int(bday_gaps.max()) if bday_gaps.size else 0
                if max_gap > 10:
                    logger.warning(
                        f"K线缓存残缺 {code}: 缓存 {len(cached)} 行（探测范围 {_probe_start}~{end_date}），"
                        f"最大相邻间隔 {max_gap} 个工作日，fall through 重抓 mootdx"
                    )
                    # 不 return，继续走 mootdx → 缓存会被 mootdx 实际结果覆写
                else:
                    # 检测通过 → 裁切回原窗口返回
                    window = cached[(cached['date'] >= pd.Timestamp(start_date)) &
                                    (cached['date'] <= pd.Timestamp(end_date))]
                    if not window.empty:
                        return self._ensure_kline_indicators(window)
                    # 窗口内无数据（缓存有更早/更晚的数据但窗口空）→ fall through
                    logger.warning(
                        f"K线缓存 {code} 窗口 [{start_date}~{end_date}] 无数据，fall through 重抓 mootdx"
                    )
            else:
                # 单行缓存无法判断间隔 → 裁切回窗口，若空则 fall through
                window = cached[(cached['date'] >= pd.Timestamp(start_date)) &
                                (cached['date'] <= pd.Timestamp(end_date))]
                if not window.empty:
                    return self._ensure_kline_indicators(window)

        # 2. mootdx (TCP 通达信，最快，永不封IP)
        df = self._fetch_kline_mootdx(code, start_date, end_date)
        if df is not None and not df.empty:
            self._save_kline_to_cache(code, df)
            self._update_source_status('mootdx_kline', True)
            # 计算技术指标
            df = self._calc_kline_indicators(df)
            return df

        # 3. 新浪K线（HTTP，快）
        df = self._fetch_kline_sina(code, start_date, end_date)
        if df is not None and not df.empty:
            self._save_kline_to_cache(code, df)
            return self._calc_kline_indicators(df)

        # 4. 降级：baostock（慢，~8s）
        return self._fetch_kline_baostock(code, start_date, end_date,
                                          adjustflag='1', use_cache=True)

    def _fetch_kline_baostock(self, code: str, start_date: str, end_date: str,
                              adjustflag: str = '1', use_cache: bool = False) -> pd.DataFrame:
        """baostock 日K线拉取（2026-08-13 抽出复权专用路径）

        adjustflag: '1'=后复权 '2'=前复权 '3'=不复权
        use_cache: True=写回 raw 缓存（仅旧降级路径 adjustflag='1' 用，raw 口径）；
                   False（默认）=不读不写缓存（qfq/hfq 专用，避免污染 raw 缓存口径；
                   ⚠️ 缓存表无复权列，任何复权价写缓存都会污染口径）
        """
        code6 = str(code).zfill(6)
        bs_code = f"sh.{code6}" if code6.startswith(self.SH_PREFIXES) else f"sz.{code6}"

        try:
            import baostock as bs
            bs.login()
            rs = bs.query_history_k_data_plus(
                bs_code, 'date,open,high,low,close,volume,amount',
                start_date=start_date, end_date=end_date,
                frequency='d', adjustflag=adjustflag)
            data = []
            while (rs.error_code == '0') and rs.next():
                data.append(rs.get_row_data())
            bs.logout()

            if data:
                df = pd.DataFrame(data, columns=['date', 'open', 'high', 'low',
                                                  'close', 'volume', 'amount'])
                df['date'] = pd.to_datetime(df['date'])
                for c in ['open', 'high', 'low', 'close', 'volume', 'amount']:
                    df[c] = pd.to_numeric(df[c], errors='coerce')
                df = df.sort_values('date').reset_index(drop=True)
                if use_cache:
                    self._save_kline_to_cache(code, df)
                self._update_source_status('baostock_kline', True)
                return self._calc_kline_indicators(df)
        except Exception as e:
            logger.warning(f"baostock K线失败 {code}: {str(e)[:60]}")
            try:
                bs.logout()
            except Exception:
                logger.warning("baostock logout 失败（可能未登录）")
        return pd.DataFrame()

    def _calc_kline_indicators(self, df: pd.DataFrame) -> pd.DataFrame:
        """为K线DataFrame计算常用技术指标"""
        if df.empty:
            return df
        # 计算涨跌幅
        df['pct_chg'] = df['close'].pct_change() * 100
        # 计算常用技术指标
        df['ma5'] = df['close'].rolling(5).mean()
        df['ma10'] = df['close'].rolling(10).mean()
        df['ma20'] = df['close'].rolling(20).mean()
        # 量比（相对5日均量）
        df['avg_volume_5'] = df['volume'].rolling(5).mean()
        df['volume_ratio'] = df['volume'].fillna(1) / df['avg_volume_5'].replace(0, np.nan)
        df['volume_ratio'] = df['volume_ratio'].fillna(1)
        return df

    def _ensure_kline_indicators(self, df: pd.DataFrame) -> pd.DataFrame:
        """确保 K 线已补算技术指标；幂等（指标列已存在则跳过），不改变
        _calc_kline_indicators 的计算逻辑。

        用途：统一 get_kline 的所有返回路径（含缓存命中路径），避免同一 DataFrame
        在冷热缓存下出现列集不一致（缺陷3 修复：缓存命中路径原先直接返回 window，
        缺 pct_chg/ma5/ma10/ma20/avg_volume_5/volume_ratio）。缓存表本身只存 8 个
        原始列，故命中路径必须在此补算。
        """
        if df is None or df.empty:
            return df
        _indicator_cols = {'pct_chg', 'ma5', 'ma10', 'ma20',
                           'avg_volume_5', 'volume_ratio'}
        if _indicator_cols.issubset(set(df.columns)):
            return df
        return self._calc_kline_indicators(df)

    # ========== 4. 基本面财务快照（mootdx） ==========

    def get_financial_snapshot(self, code: str) -> Dict:
        """
        获取个股最新季报财务快照（mootdx finance 37字段）

        来源：a-stock-data §6.1 mootdx finance
        返回: {eps, roe, profit, income, bvps, ...} 关键字段
              空字典表示数据不可用
        """
        code6 = str(code).zfill(6)
        # 先读缓存
        cached = self._get_fin_from_cache(code6)
        if cached:
            return cached

        try:
            client = self._get_mootdx_client()

            fin = client.finance(symbol=code6)
            if fin is not None and not fin.empty:
                data = fin.iloc[-1].to_dict() if len(fin) > 0 else {}
                # mootdx 列名为中文拼音：jinglirun=净利润, zhuyingshouru=主营收入,
                # zongguben=总股本, jingzichan=净资产, meigujingzichan=每股净资产
                profit_val = float(data.get('jinglirun', 0) or 0)
                income_val = float(data.get('zhuyingshouru', 0) or 0)
                total_shares = float(data.get('zongguben', 0) or 0)
                net_assets = float(data.get('jingzichan', 0) or 0)
                bvps_val = float(data.get('meigujingzichan', 0) or 0)
                # 计算 EPS 和 ROE
                eps_val = profit_val / total_shares if total_shares > 0 else 0
                roe_val = (profit_val / net_assets * 100) if net_assets > 0 else 0
                result = {
                    'eps': round(eps_val, 4),
                    'roe': round(roe_val, 2),
                    'profit': profit_val,
                    'income': income_val,
                    'bvps': bvps_val,
                    'total_shares': total_shares,
                    'report_date': str(fin.iloc[-1].name)[:7] if hasattr(fin.iloc[-1], 'name') else '',
                }
                # 写入缓存（含 fetched_at，供 TTL 判定）
                conn = None
                try:
                    import sqlite3
                    _now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
                    conn = sqlite3.connect(self._kline_cache_path)
                    conn.execute(
                        "INSERT OR REPLACE INTO fin_cache "
                        "(code,report_date,eps,roe,profit,income,bvps,fetched_at) "
                        "VALUES (?,?,?,?,?,?,?,?)",
                        (code6, result['report_date'], result['eps'], result['roe'],
                         result['profit'], result['income'], result['bvps'], _now)
                    )
                    conn.commit()
                except Exception as e:
                    logger.warning(f"fin_cache写入失败 {code}: {str(e)[:80]}")
                finally:
                    if conn:
                        conn.close()
                return result
        except Exception as e:
            logger.warning(f"财务快照失败 {code}: {str(e)[:50]}")
        return {}

    def _get_fin_from_cache(self, code: str) -> Optional[Dict]:
        """从SQLite缓存读取财务数据（带 TTL）。

        2026-09-17 T1：读取时比对 fetched_at，超过 _fin_cache_ttl_days 视为过期
        → 返回 None 触发回源重查（新季报发布后基本面/估值因子不再永久陈旧）。
        仅作用于"最新快照"缓存；fundamentals_history（point-in-time）不受影响。
        """
        try:
            # 性能（2026-09-06）：复用线程级只读连接（见 _get_read_conn）
            conn = self._get_read_conn(self._kline_cache_path)
            cursor = conn.execute(
                "SELECT eps,roe,profit,income,bvps,report_date,fetched_at FROM fin_cache "
                "WHERE code=? ORDER BY report_date DESC LIMIT 1", (code,)
            )
            row = cursor.fetchone()
            if row and row[0] is not None:
                fetched_at = row[6]
                if fetched_at:
                    try:
                        age = (datetime.now() - datetime.strptime(fetched_at, '%Y-%m-%d %H:%M:%S')).days
                    except Exception:
                        # 2026-09-18 审查补充：解析失败按不过期处理（避免误回源），
                        # 但必须告警——静默容忍会让写库端的格式 bug 永远不被发现
                        logger.warning(
                            f"fin_cache.fetched_at 格式异常(按不过期处理): {code} "
                            f"raw={str(fetched_at)[:30]}")
                        age = 0  # 解析失败按不过期处理，避免误回源
                    if age > self._fin_cache_ttl_days:
                        logger.info(
                            f"fin_cache 过期({age}天>{self._fin_cache_ttl_days}天) → 回源重查 {code}")
                        return None
                return {
                    'eps': float(row[0] or 0),
                    'roe': float(row[1] or 0),
                    'profit': float(row[2] or 0),
                    'income': float(row[3] or 0),
                    'bvps': float(row[4] or 0),
                    'report_date': str(row[5] or ''),
                }
        except Exception as e:
            logger.warning(f"fin_cache读取失败 {code}: {str(e)[:80]}")
            return None
        return None
    # ========== 5. 技术指标快捷版 ==========

    def get_technical_summary(self, code: str) -> dict:
        """获取个股技术面摘要（基于baostock）"""
        kline = self.get_kline(code, start_date=(datetime.now()-timedelta(days=200)).strftime('%Y-%m-%d'))
        if kline.empty or len(kline) < 20:
            return {'rps_20': 50, 'macd_score': 50, 'ma_status': 'unknown'}

        close = kline['close']

        # MACD
        exp1 = close.ewm(span=12, adjust=False).mean()
        exp2 = close.ewm(span=26, adjust=False).mean()
        dif = exp1 - exp2
        dea = dif.ewm(span=9, adjust=False).mean()

        last_dif = dif.iloc[-1]
        last_dea = dea.iloc[-1]
        prev_dif = dif.iloc[-2] if len(dif) > 1 else last_dif

        if prev_dif < last_dea and last_dif > last_dea:
            macd_score = 80
        elif last_dif > last_dea:
            macd_score = 65
        else:
            macd_score = 35

        # 20日涨幅（模拟RPS，实际RPS需要全市场比较）
        rps_20 = (close.iloc[-1] / close.iloc[-20] - 1) * 100

        # 均线状态
        ma5 = close.iloc[-1] / kline['ma5'].iloc[-1] - 1 if not kline['ma5'].isna().iloc[-1] else 0
        ma20 = close.iloc[-1] / kline['ma20'].iloc[-1] - 1 if not kline['ma20'].isna().iloc[-1] else 0

        return {
            'rps_20': rps_20,
            'macd_score': macd_score,
            'ma_bias': round(ma20 * 100, 2),
            'price_above_ma5': ma5 > -0.01,
            'price_above_ma20': ma20 > -0.01,
        }

        # ========== 资金面 ==========
    # 数据源级别的熔断标志，各 endpoint 独立，互不影响
    _source_available = {
        'big_deal': True,         # stock_fund_flow_big_deal — 东财大单
        'ths_fund_flow': True,    # stock_fund_flow_individual — 同花顺全市场资金流（通）
        # 键名 2026-09-03 统一为 akshare_north_flow，与 _source_status 对齐
        # （此前此处为 'north_flow'，两个字典键名不一致，恢复/报告无法对应同一数据源）
        'akshare_north_flow': True, # northbound_holdings — 北向（asharehub/通）
        'lockup': True,
        'asharehub_moneyflow': True, # AShareHub个股资金流（独立熔断）
        'asharehub_tech_factors': True, # AShareHub技术因子（独立熔断）
        'asharehub_concepts': True,    # AShareHub概念板块（独立熔断）
        'asharehub_financial': True,   # AShareHub财务指标（独立熔断）
    }

    def get_main_fund_accumulated(self, code: str, days: int = 10,
                                  skip_asharehub: bool = False) -> Optional[float]:
        """
        主力资金近N日累计 — 多源回退链：
        1. AShareHub moneyflow（优先，独立熔断，skip_asharehub=True 时跳过）
        2. 大单交易汇总（今日大单净流向，最快，缓存685只）
        3. 同花顺全市场资金流排行（全量5189只，独立熔断）

        skip_asharehub=True 用于配额保护：策略层只给 top N 候选股调 AShareHub，
        其余直接走 big_deal + 同花顺回退，不消耗日配额。
        """
        # 源1: AShareHub moneyflow（优先，独立熔断，可跳过）
        if not skip_asharehub:
            result = self._get_capital_flow_asharehub(code)
            if result is not None:
                return result

        # 源2: 大单交易汇总（内存缓存，毫秒级）
        result = self._get_capital_flow_big_deal(code)
        if result is not None:
            return result

        # 源3: 同花顺全市场资金流（一次性拉取5189只，独立熔断）
        if self._source_available.get('ths_fund_flow', True):
            result = self._get_ths_fund_flow(code)
            if result is not None:
                return result

        return None

    def _register_asharehub_failure(self, key: str, err: str):
        """稳定性（2026-09-06 迭代）：熔断分级。

        原实现：任何一次异常（网络抖动、脏响应 float('--')、JSON 解析失败）
        都立即把整个 asharehub 子源熔断，且 _recover_sources 不恢复 asharehub 键
        → 一个坏票让该源当天剩余全部候选股失效、因子全中性。
        现分级：
          - 致命错误（认证失败/配额 429/_key 无效）→ 立即熔断；
          - 瞬态/脏数据错误 → 连续 >= threshold 次才熔断；成功一次即清零计数。
        """
        fatal_pat = ('401', '403', 'invalid token', 'unauthorized', '429')
        err_low = str(err).lower()
        if any(p in err_low for p in fatal_pat):
            self._source_available[key] = False
            self._update_source_status(key, False, f'致命错误熔断: {str(err)[:60]}')
            return
        cnt = self._asharehub_fail_counts.get(key, 0) + 1
        self._asharehub_fail_counts[key] = cnt
        if cnt >= self._asharehub_fail_threshold:
            logger.warning(f"asharehub源 {key} 连续失败 {cnt} 次 → 熔断")
            self._source_available[key] = False
            self._update_source_status(key, False, f'连续失败{cnt}次熔断: {str(err)[:50]}')

    def _register_asharehub_success(self, key: str):
        self._asharehub_fail_counts[key] = 0

    def _get_asharehub_client(self):
        """获取 AShareHub 客户端——线程级实例（2026-09-07 回访修复）。

        修复：双检锁（2026-09-06）只保证"只创建一个实例"，但 3 线程池内
        并发调用 client.moneyflow/technical_factors 等 HTTP 方法时并未
        串行化，共享 session 会被多线程交错使用。改为 threading.local
        每线程一个实例（HTTP 客户端轻量，3 线程 = 3 个会话；配额账本
        独立于客户端实例，不受影响）。
        """
        client = getattr(_thread_local, 'asharehub_client', None)
        if client is None:
            with self._client_init_lock:
                client = getattr(_thread_local, 'asharehub_client', None)
                if client is None:
                    from asharehub import AShareHub
                    client = AShareHub(
                        api_key=os.environ.get('ASHAREHUB_API_KEY', ''),
                        version='v2'
                    )
                    _thread_local.asharehub_client = client
        return client

    def _get_mootdx_client(self):
        """获取 mootdx 客户端——线程级实例（2026-09-07 回访修复）。

        同 _get_asharehub_client：mootdx TCP 客户端非线程安全（代码注释
        自认），双检锁只防重创建、不防并发使用。改为每线程独立 TCP 连接。
        """
        client = getattr(_thread_local, 'mootdx_client', None)
        if client is None:
            with self._client_init_lock:
                client = getattr(_thread_local, 'mootdx_client', None)
                if client is None:
                    from mootdx.quotes import Quotes
                    client = Quotes.factory(market='std')
                    _thread_local.mootdx_client = client
        return client

    def _get_capital_flow_asharehub(self, code: str) -> Optional[float]:
        """AShareHub 个股资金流（按订单规模），独立熔断

        返回个股当日主力净流入（元），与 big_deal / ths_fund_flow 互不影响。
        """
        if not self._source_available.get('asharehub_moneyflow', True):
            return None
        if not self._asharehub_budget_ok():
            return None
        try:
            client = self._get_asharehub_client()
            code6 = str(code).zfill(6)
            symbol = f"{code6}.SH" if code6.startswith(('6', '9')) else f"{code6}.SZ"
            df = client.moneyflow(symbol=symbol, limit=1)
            if df is not None and not df.empty:
                # net_mf_amount 单位为万元，转为元
                net_amount_yuan = float(df.iloc[0]['net_mf_amount']) * 10000
                if abs(net_amount_yuan) > 0:
                    self._register_asharehub_success('asharehub_moneyflow')
                    return net_amount_yuan
            return None
        except Exception as e:
            logger.warning(f"asharehub资金流失败 {code}: {str(e)[:60]}")
            # 熔断分级（2026-09-06 迭代）：单次瞬时/脏数据错误不再整源熔断
            self._register_asharehub_failure('asharehub_moneyflow', str(e))
        return None

    def get_technical_factors_asharehub(self, code: str) -> Optional[dict]:
        """AShareHub 预计算技术因子，独立熔断

        返回最新一日的 MACD/RSI 等因子，用于双源技术评分校验。
        与 K 线自算技术分互不影响。
        """
        # 非交易日预取缓存优先（technical 低频，7 天新鲜）
        if not self._prefetch_mode:
            cached = self._read_asharehub_prefetch('tech_factors', str(code).zfill(6), max_age_days=7)
            if cached is not None:
                return cached
        if not self._source_available.get('asharehub_tech_factors', True):
            return None
        if not self._asharehub_budget_ok():
            return None
        try:
            client = self._get_asharehub_client()
            code6 = str(code).zfill(6)
            symbol = f"{code6}.SH" if code6.startswith(('6', '9')) else f"{code6}.SZ"
            df = client.technical_factors(symbol=symbol, limit=1)
            if df is not None and not df.empty:
                row = df.iloc[-1]
                # 2026-09-16 P1-3：出口统一走 sanitize_nan，把 NaN/±Inf 归一为 None，
                # 使 live 路径与「写缓存(sanitize_nan) → 读缓存(json.loads)」的形态
                # 完全等价。此前 live 返回 NaN、缓存返回 None，同一份数据两条路径
                # 消费行为不一致（NaN 在比较中恒 False，静默落入 else 分支）。
                return sanitize_nan({
                    'macd_dif': float(row.get('macd_dif', 0)),
                    'macd_dea': float(row.get('macd_dea', 0)),
                    'macd': float(row.get('macd', 0)),
                    'rsi_6': float(row.get('rsi_6', 50)),
                    'rsi_12': float(row.get('rsi_12', 50)),
                    'rsi_24': float(row.get('rsi_24', 50)),
                    'close_hfq': float(row.get('close_hfq', 0)),
                    'cci': float(row.get('cci', 0)),
                })
            return None
        except Exception as e:
            logger.warning(f"asharehub技术因子失败 {code}: {str(e)[:60]}")
            self._register_asharehub_failure('asharehub_tech_factors', str(e))
        return None

    def get_concept_members(self, code: str) -> Optional[list]:
        """AShareHub 个股所属概念板块列表，独立熔断

        返回概念标识列表（BK 概念指数代码，如 BK1722.DC），用于 hot_theme 评分
        的概念数量计数（calc_hot_theme_score 只 len() 不关心可读名）。
        失败时不影响同花顺强势股 / 东财板块归属。
        """
        # 非交易日预取缓存优先（concepts 低频，7 天新鲜）
        # 注意：预取缓存存的是 {'names': [...]} 包装结构，读回时解包还原成 list
        if not self._prefetch_mode:
            cached = self._read_asharehub_prefetch('concepts', str(code).zfill(6), max_age_days=7)
            if cached is not None:
                names = cached.get('names') if isinstance(cached, dict) else cached
                return names if names else None
        if not self._source_available.get('asharehub_concepts', True):
            return None
        if not self._asharehub_budget_ok():
            return None
        try:
            client = self._get_asharehub_client()
            code6 = str(code).zfill(6)
            symbol = f"{code6}.SH" if code6.startswith(('6', '9')) else f"{code6}.SZ"
            # 2026-08-17 修复：con_symbol 才是"查该股票所属概念"的参数。
            # 原用 symbol=股票代码 是查"股票代码作为概念指数"的成分股 → 恒空 DataFrame，
            # 导致 concept_names 恒 None → hot_theme 恒 65（50基准+15板块顶格，缺概念加分）。
            # 返回列: trade_date/symbol/con_symbol/name — symbol 列才是概念指数代码(BK)，
            # name 列是股票自身名称（同名重复），所以按 symbol 去重取概念数。
            df = client.concept_members(con_symbol=symbol, limit=200)
            if df is not None and not df.empty:
                # 返回含历史多日快照（实测单只 4681 行），取最新 trade_date 当日去重
                if 'trade_date' in df.columns:
                    latest = df['trade_date'].dropna().max()
                    if pd.isna(latest):
                        logger.warning(f"asharehub概念板块 {code}: trade_date 全为空，跳过去重取全部 symbol")
                        latest = None
                    else:
                        df = df[df['trade_date'] == latest]
                # 概念代码列：symbol（BKxxx.DC），回退 con_name/name 兼容（name 是股票名，仅兜底）
                if 'symbol' in df.columns:
                    names = df['symbol'].dropna().unique().tolist()
                else:
                    name_col = 'con_name' if 'con_name' in df.columns else 'name'
                    names = df[name_col].dropna().unique().tolist()
                return names if names else None
            return None
        except Exception as e:
            logger.warning(f"asharehub概念板块失败 {code}: {str(e)[:60]}")
            self._register_asharehub_failure('asharehub_concepts', str(e))
        return None

    def get_financial_indicators(self, code: str) -> Optional[dict]:
        """AShareHub 核心财务指标，独立熔断

        返回最新一期的 EPS/ROE/ROA/毛利率等，用于长线策略基本面评分。
        失败时回退到 baostock / mootdx 财务数据。
        """
        # 非交易日预取缓存优先（财务季度级低频，14 天新鲜）
        if not self._prefetch_mode:
            cached = self._read_asharehub_prefetch('financial', str(code).zfill(6), max_age_days=14)
            if cached is not None:
                return cached
        if not self._source_available.get('asharehub_financial', True):
            return None
        if not self._asharehub_budget_ok():
            return None
        try:
            client = self._get_asharehub_client()
            code6 = str(code).zfill(6)
            symbol = f"{code6}.SH" if code6.startswith(('6', '9')) else f"{code6}.SZ"
            df = client.financial_indicators(symbol=symbol, limit=1)
            if df is not None and not df.empty:
                row = df.iloc[-1]
                return {
                    'eps': float(row.get('eps', 0)),
                    'roe': float(row.get('roe', 0)),
                    'roe_waa': float(row.get('roe_waa', 0)),
                    'roa': float(row.get('roa', 0)),
                    'gross_margin': float(row.get('gross_margin', 0)),
                    'netprofit_margin': float(row.get('netprofit_margin', 0)),
                    'debt_to_assets': float(row.get('debt_to_assets', 0)),
                    'bps': float(row.get('bps', 0)),
                    'ocfps': float(row.get('ocfps', 0)),
                    'basic_eps_yoy': float(row.get('basic_eps_yoy', 0)),
                    'netprofit_yoy': float(row.get('netprofit_yoy', 0)),
                }
            return None
        except Exception as e:
            logger.warning(f"asharehub财务指标失败 {code}: {str(e)[:60]}")
            self._register_asharehub_failure('asharehub_financial', str(e))
        return None

    def _get_ths_fund_flow(self, code: str) -> Optional[float]:
        """
        同花顺全市场资金流排行，一次性拉取并缓存。
        从 '主力净流入' 列提取个股当日主力资金净额。
        """
        if not self._source_available.get('ths_fund_flow', True):
            return None
        try:
            if not hasattr(self, '_ths_fund_flow_cache') or self._ths_fund_flow_cache is None:
                self._ths_fund_flow_cache = {}
                import akshare as ak
                df = ak.stock_fund_flow_individual()
                if df is not None and not df.empty:
                    for _, row in df.iterrows():
                        code_str = str(row.iloc[1]).zfill(6)
                        raw = str(row.iloc[6])
                        try:
                            if '亿' in raw:
                                val = float(raw.replace('亿', '')) * 1e8
                            elif '万' in raw:
                                val = float(raw.replace('万', '')) * 1e4
                            else:
                                val = float(raw)
                        except (ValueError, TypeError):
                            val = 0
                        self._ths_fund_flow_cache[code_str] = val
                    self._update_source_status('akshare_fund_flow', True)
                    logger.info(f"同花顺全市场资金流已加载: {len(self._ths_fund_flow_cache)} 只")

            code_str = str(code).zfill(6)
            if code_str in self._ths_fund_flow_cache:
                val = self._ths_fund_flow_cache.get(code_str, 0)
                if abs(val) > 0:
                    return val
            return None
        except Exception as e:
            logger.warning(f"同花顺资金流失败 {code}: {str(e)[:60]}")
            self._source_available['ths_fund_flow'] = False
            self._update_source_status('ths_fund_flow', False, str(e)[:60])
            self._ths_fund_flow_cache = None
        return None

    def _get_capital_flow_big_deal(self, code: str) -> Optional[float]:
        """从全市场大单交易汇总中推算个股今日主力净流入（买入-卖出差额）

        独立熔断：不和 get_north_flow_accumulated 共享标志位。
        因为 big_deal 数据本身是通的，不能被个股北向的失败连带熔断。
        """
        if not self._source_available.get('big_deal', True):
            return None
        try:
            # 缓存：整个 session 只拉一次全量大单数据
            if not hasattr(self, '_big_deal_cache') or self._big_deal_cache is None:
                self._big_deal_cache = {}
                import akshare as ak
                import pandas as pd
                df = ak.stock_fund_flow_big_deal()
                df.columns = ['成交时间', '股票代码', '股票简称', '成交价格',
                               '成交量', '成交金额', '大单性质', '涨跌幅', '涨跌额']
                df['股票代码'] = df['股票代码'].astype(str).str.zfill(6)  # 统一为6位字符串
                buy_mask = df['大单性质'].str.contains('买|主', na=False)
                sell_mask = df['大单性质'].str.contains('卖', na=False)
                buy_sum = df[buy_mask].groupby('股票代码')['成交金额'].sum()
                sell_sum = df[sell_mask].groupby('股票代码')['成交金额'].sum()
                net = buy_sum.subtract(sell_sum, fill_value=0)
                # 修复（2026-09-05 审查 D8）：akshare stock_fund_flow_big_deal 的成交额
                # 单位是"万元"（成交量单位是"股"，见 akshare 官方文档），此前未换算，
                # 导致回退链量纲混杂：ASHareHub 源返回元（net_mf_amount×1e4）、
                # THS 源返回元（亿/万后缀换算），唯独本源返回万元 —— 同一只票在
                # 不同回退路径下 main_fund_accumulated 相差 1e4 倍，直接摧毁
                # calc_main_fund_score 绝对阈值与横截面百分位排序。×1e4 统一为元。
                net = net * 1e4
                self._big_deal_cache = net
                # 保留原始 df 用于尾盘成交结构分析
                # 排序：保证 get_tail_end_stats 中 iloc[-1] 一定是当日最后一笔成交
                # （akshare 返回顺序不保证，且 na_position='first' 让无效时间行沉不下去）
                self._big_deal_raw = df.sort_values('成交时间', na_position='first').copy()
                # 预建 分组索引（仅存行标签，避免逐股 O(N) 全表扫描）
                self._big_deal_code_groups = self._big_deal_raw.groupby(
                    '股票代码', sort=False).groups
                self._update_source_status('akshare_fund_flow', True)
                logger.info(f"全市场大单数据已加载: {len(net)} 只股票，每只约{len(df)//len(net)}笔大单")

            code_str = str(code).zfill(6)
            if code_str in self._big_deal_cache.index:
                net_val = float(self._big_deal_cache.loc[code_str])
                if abs(net_val) > 0:
                    return net_val
            return None
        except Exception as e:
            logger.warning(f"大单资金流失败 {code}: {str(e)[:80]}")
            self._source_available['big_deal'] = False
            self._update_source_status('big_deal', False, str(e)[:80])
            self._big_deal_cache = None
        return None

    def preload_big_deal(self) -> bool:
        """预热全市场大单资金流缓存（stock_fund_flow_big_deal）。

        详评循环前调用一次（~18s），之后 200 只候选股的 capital_flow 全部
        命中内存缓存（毫秒级），避免"第一批股票拿不到 fallback 数据被中性化"。

        返回 True=加载成功/已缓存；False=失败（调用方继续流程，个股走同花顺降级）。
        """
        try:
            # 触发一次查询即会加载全量大单数据到 _big_deal_cache
            if self._big_deal_cache is None:
                self._get_capital_flow_big_deal('000001')
            return self._big_deal_cache is not None
        except Exception as e:
            logger.warning(f"big_deal 预加载失败: {str(e)[:80]}")
            return False

    def get_tail_end_stats(self, code: str) -> Dict:
        """
        尾盘成交结构统计 — 从已缓存的大单数据中提取尾盘信号

        三个子维度：
          1. 尾盘30分钟成交占比（> 25% 说明尾盘资金集中介入）
          2. 尾盘资金逆转（全天净流出但尾盘30min净流入 = 强信号）
          3. VWAP位置强度（收盘价在当日成交均价之上）

        返回：{'available': True/False, 'tail_volume_ratio': float, ...}
        """
        if not hasattr(self, '_big_deal_raw') or self._big_deal_raw is None:
            return {'available': False}

        code_str = str(code).zfill(6)
        # 修复（2026-09-05 审查）：逐股索引预建后 O(1) 定位，替代 170 次 ×O(N) 全表布尔扫描
        idx = getattr(self, '_big_deal_code_groups', {}).get(code_str)
        if idx is None or len(idx) == 0:
            return {'available': False}
        stock_deals = self._big_deal_raw.loc[idx]
        if stock_deals.empty:
            return {'available': False}

        # 全天的成交额和净流向
        total_volume = stock_deals['成交金额'].sum()
        buy_mask = stock_deals['大单性质'].str.contains('买|主', na=False)
        total_buy = stock_deals[buy_mask]['成交金额'].sum()
        total_sell = stock_deals[~buy_mask]['成交金额'].sum()
        total_net = total_buy - total_sell

        # 尾盘30分钟（14:30-15:00）
        # 成交时间格式类似 '2025-01-15 14:35:00'
        time_str = stock_deals['成交时间'].astype(str)
        tail_mask = time_str.str.contains(r'14:3[0-9]|14:4[0-9]|14:5[0-9]', na=False)
        tail_deals = stock_deals[tail_mask]
        tail_volume = tail_deals['成交金额'].sum()
        # 修复（2026-09-05 审查）：原先 tail_deals[buy_mask & tail_mask] 触发
        # "Boolean Series key will be reindexed" —— 掩码索引与已过滤 DataFrame 不对齐，
        # 依赖 pandas 隐式重索引（重复索引时会直接报错）。改为在 tail_deals 上
        # 直接求性质掩码，语义完全等价（买=含'买|主'，卖=其余），零重索引。
        tail_prop = tail_deals['大单性质'].str.contains('买|主', na=False)
        tail_buy = tail_deals[tail_prop]['成交金额'].sum()
        tail_sell = tail_deals[~tail_prop]['成交金额'].sum()
        tail_net = tail_buy - tail_sell

        # 1. 尾盘成交占比
        tail_volume_ratio = tail_volume / total_volume if total_volume > 0 else 0

        # 2. 尾盘资金逆转
        # 修复（2026-09-05 审查）：bool() 强转 —— numpy bool 标量无法被 json.dump 序列化
        tail_reversal = bool(tail_net > 0 and total_net < 0)

        # 3. 收盘位置（用大单的成交均价近似VWAP）
        #    price_position > 0.67 表示收盘在均价上方（强势收尾）
        # 修复（2026-09-05 审查 D7）：原 vwap = Σ成交金额/Σ成交量。
        # akshare stock_fund_flow_big_deal 官方文档：成交量单位是"股"、成交额单位
        # 是"万元"（同花顺实页样例验证 51.57元×14662股=75.61万元 吻合），
        # 万元/股 = 真实均价/1e4 → price_position ≈ +9999 恒成立，
        # "强势收尾"子信号对所有有大单数据的票永远误触发（白拿 +6 分）。
        # 改为量加权均价 Σ(价格×量)/Σ量 —— 单位无关，成交价格列恒为元。
        vol_sum = stock_deals['成交量'].sum()
        if vol_sum > 0:
            vwap = float((stock_deals['成交价格'] * stock_deals['成交量']).sum() / vol_sum)
        else:
            vwap = 0.0
        last_price = stock_deals['成交价格'].iloc[-1] if not stock_deals.empty else 0
        price_position = (last_price - vwap) / vwap if vwap > 0 else 0

        # 修复（2026-09-05 审查）：pandas sum()/numpy 标量统一转 Python 原生类型，
        # 保证返回值可直接 JSON 序列化（np.int64 会让 json.dumps 直接 TypeError）
        # 修复（2026-09-05 审查 D8）：成交金额列为万元口径，统一换算为元，
        # 与 main_fund_accumulated（ASHareHub/THS 源）保持一致；tail_reversal
        # 与 tail_volume_ratio 基于符号/比值，不受常数缩放影响。
        return {
            'available': True,
            'tail_volume_ratio': round(float(tail_volume_ratio), 4),
            'total_net': round(float(total_net) * 1e4, 2),
            'tail_net': round(float(tail_net) * 1e4, 2),
            'tail_reversal': tail_reversal,
            'vwap': round(float(vwap), 2),
            'last_price': float(last_price),
            'price_position': round(float(price_position), 4),
        }

    def get_north_flow_accumulated(self, code: str, days: int = 10) -> Optional[float]:
        """
        北向资金近N日累计（asharehub northbound_holdings，已通）
        计算逻辑：最新持股量 - N日前持股量 = 区间净增持股数（正=加仓）

        熔断：
          - 独立于 big_deal / ths_fund_flow，互不影响
        """
        if not self._source_available.get('akshare_north_flow', True):
            return None
        if not self._asharehub_budget_ok():
            return None

        try:
            from asharehub import AShareHub
            if self._asharehub_client is None:
                import os as _os
                self._asharehub_client = self._get_asharehub_client()

            code6 = str(code).zfill(6)
            symbol = f"{code6}.SH" if code6.startswith(('6', '9')) else f"{code6}.SZ"
            client = self._get_asharehub_client()

            # 拉取足够的历史数据（按季度频率，拉120条足够）
            cache_key = f"nb_{code6}"
            if cache_key in self._northbound_cache:
                records = self._northbound_cache[cache_key]
            else:
                df = client.northbound_holdings(symbol=symbol, limit=120)
                if df is not None and not df.empty:
                    records = {}
                    for _, row in df.iterrows():
                        records[str(row['trade_date'])] = float(row['vol'])
                    self._northbound_cache[cache_key] = records
                    self._update_source_status('akshare_north_flow', True)
                else:
                    return None

            if not records:
                return None

            # 找最新和 N 天前的 vol
            sorted_dates = sorted(records.keys(), reverse=True)
            if len(sorted_dates) < 2:
                return None

            latest_date = sorted_dates[0]
            latest_vol = records[latest_date]

            # 找 N 天前的数据（按日历推算 N 天前的最接近记录）
            from datetime import datetime, timedelta
            target_date = (datetime.strptime(latest_date, '%Y%m%d') - timedelta(days=days)).strftime('%Y%m%d')
            prev_vol = None
            for d in sorted_dates:
                if d <= target_date:
                    prev_vol = records[d]
                    break

            if prev_vol is None or prev_vol == 0:
                return None

            vol_change = latest_vol - prev_vol
            # 正=加仓，负=减仓。用 vol 变化率归一化到 0-100 分值
            return vol_change

        except Exception as e:
            logger.warning(f"asharehub北向失败 {code}: {str(e)[:60]}")
            self._source_available['akshare_north_flow'] = False
            self._update_source_status('akshare_north_flow', False, str(e)[:60])
        return None

    def get_north_flow_summary(self) -> Optional[Dict]:
        """
        全市场北向资金当日汇总（沪股通+深股通净买入）
        来源：东方财富 securities API（含token，日级别净流入，最新到昨日）
        2024-08起港交所停止披露实时买卖额，但东财整理的日级别净流入仍可用
        返回：{'hgt': -33.43, 'sgt': -26.51, 'total': -59.95, 'time': '2026-07-10', 'available': True}
              单位：亿元，available=True/False 标注数据可用性
        失败降级：API 超时或返回异常时，尝试 akshare 备用探测（同域名 datacenter-web，
                  不消耗 AShareHub 配额），仍失败则返回 available=False 标记
        """
        # 主数据源：东财 securities API
        try:
            import requests as req
            url = ("https://datacenter-web.eastmoney.com/securities/api/data/v1/get"
                   "?reportName=RPT_MUTUAL_NETINFLOW_DETAILS"
                   "&columns=DIRECTION_TYPE,TRADE_DATE,NET_INFLOW_SH,NET_INFLOW_SZ,NET_INFLOW_BOTH,TIME_TYPE"
                   + "&token=" + EASTMONEY_WEB_TOKEN
                   + "&client=WEB"
                   "&filter=(DIRECTION_TYPE=%222%22)(TIME_TYPE=%221%22)"
                   "&sortColumns=TRADE_DATE&sortTypes=-1&pageSize=1")
            headers = {
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/131.0.0.0',
                'Referer': 'https://data.eastmoney.com/',
            }
            r = req.get(url, headers=headers, timeout=10)
            d = r.json()
            result = d.get('result')
            if not result or not result.get('data'):
                raise ValueError("东财 securities API 返回空数据")
            row = result['data'][0]
            hgt = row.get('NET_INFLOW_SH')
            sgt = row.get('NET_INFLOW_SZ')
            if hgt is None or sgt is None:
                raise ValueError("字段值为空")
            return {
                'hgt': round(hgt / 100, 2),
                'sgt': round(sgt / 100, 2),
                'total': round((hgt + sgt) / 100, 2),
                'time': str(row.get('TRADE_DATE', ''))[:10],
                'available': True,
            }
        except Exception as e:
            logger.warning(f"东财 securities API 失败: {str(e)[:60]}")
            # P3: 备用探测 — 用 akshare 测试东财 datacenter 整体是否在线
            # akshare 北向字段值为 0（政策停），但接口通说明东财 datacenter 没挂
            # 用于区分"token 失效"还是"东财 datacenter 整体宕机"
            try:
                import akshare as ak
                ak.stock_hsgt_fund_flow_summary_em()
                logger.warning("东财 datacenter 在线（akshare 可调），疑似 securities token 失效")
                datacenter_online = True
            except Exception:
                logger.warning("东财 datacenter 整体不可达（akshare 也失败）")
                datacenter_online = False
            return {
                'hgt': 0.0, 'sgt': 0.0, 'total': 0.0,
                'time': '', 'available': False,
                'error': 'securities_token_expired' if datacenter_online else 'datacenter_offline',
            }

    def get_data_source_summary(self) -> Dict:
        """返回数据源状态摘要，供报告标注"""
        return {k: dict(v) for k, v in self._source_status.items()}

    # ========== 7. 新闻舆情 ==========

    def get_stock_news(self, code: str, page_size: int = 10) -> List[Dict]:
        """
        获取个股相关新闻（akshare stock_news_em）
        来源：a-stock-data §5.1, 通过akshare
        """
        try:
            import akshare as ak
            df = ak.stock_news_em(symbol=str(code).zfill(6))
            results = []
            for i in range(min(len(df), page_size)):
                row = df.iloc[i]
                results.append({
                    "title": str(row.iloc[1])[:100] if len(row) > 1 else "",
                    "content": str(row.iloc[2])[:200] if len(row) > 2 else "",
                    "time": str(row.iloc[3]) if len(row) > 3 else "",
                    "source": str(row.iloc[4]) if len(row) > 4 else "",
                    "url": str(row.iloc[5]) if len(row) > 5 else "",
                })
            return results
        except Exception as e:
            logger.warning(f"个股新闻获取失败 {code}: {str(e)[:60]}")
        return []

    def get_market_news(self, page_size: int = 20) -> List[Dict]:
        """获取全市场财经新闻（东财 7x24 全球资讯）"""
        try:
            import uuid
            url = "https://np-weblist.eastmoney.com/comm/web/getFastNewsList"
            params = {
                "client": "web", "biz": "web_724",
                "fastColumn": "102", "sortEnd": "",
                "pageSize": str(page_size),
                "req_trace": str(uuid.uuid4()),
            }
            r = _session.get(url, params=params,
                             headers={"User-Agent": "Mozilla/5.0 Chrome/131.0.0.0",
                                      "Referer": "https://kuaixun.eastmoney.com/"},
                             timeout=10)
            d = r.json()
            items = d.get("data", {}).get("fastNewsList", [])
            return [{"title": item.get("title", ""),
                     "summary": item.get("summary", "")[:200],
                     "time": item.get("showTime", "")} for item in items]
        except Exception as e:
            logger.warning(f"市场新闻获取失败: {str(e)[:60]}")
        return []

    # ========== 8. 同花顺强势股 + 题材归因（a-stock-data §3.1） ==========

    def get_limit_up_emotion(self, trade_date: str = None) -> Optional[dict]:
        """打板情绪三指标（2026-09-07 P1 论证后实施）——基于 akshare 东财涨停三池。

        免费数据源（东财 push2ex，市场级数据，每次 3 次调用、0 个股配额）：
          - 封板率   = 涨停数 / (涨停数 + 炸板数)
          - 连板晋级率 = 今日连板(≥2板)数 / 昨日涨停数
          - 昨日涨停溢价 = 昨日涨停股今日平均涨跌幅(%)

        返回 {'available': True, 'seal_rate': 0-100, 'promotion_rate': 0-100,
              'prev_premium': ±%}；任一数据缺失/异常返回 None（调用方回退旧口径）。
        结果按 trade_date 缓存（市场级数据当日不变）。
        """
        import akshare as ak
        from core.trading_calendar import beijing_now
        if trade_date is None:
            trade_date = beijing_now().strftime('%Y-%m-%d')
        ymd = trade_date.replace('-', '')
        cache_key = f"zt_emotion_{ymd}"
        cached = getattr(self, '_zt_emotion_cache', {})
        if cache_key in cached:
            return cached[cache_key]

        result = None
        try:
            zt = ak.stock_zt_pool_em(date=ymd)
            zb = ak.stock_zt_pool_zbgc_em(date=ymd)
            prev = ak.stock_zt_pool_previous_em(date=ymd)
            n_zt, n_zb, n_prev = len(zt), len(zb), len(prev)
            if n_zt + n_zb == 0 or n_prev == 0:
                logger.warning(f"涨停三池数据为空 {trade_date}: zt={n_zt} zb={n_zb} prev={n_prev}")
                result = None
            else:
                seal_rate = n_zt / (n_zt + n_zb) * 100.0
                promo_num = 0
                if n_zt and '连板数' in zt.columns:
                    promo_num = int((pd.to_numeric(zt['连板数'], errors='coerce') >= 2).sum())
                promotion_rate = promo_num / n_prev * 100.0
                prev_premium = None
                if '涨跌幅' in prev.columns:
                    prem = pd.to_numeric(prev['涨跌幅'], errors='coerce').dropna()
                    if len(prem):
                        prev_premium = float(prem.mean())
                if prev_premium is None:
                    result = None
                else:
                    result = {'available': True,
                              'seal_rate': round(seal_rate, 1),
                              'promotion_rate': round(promotion_rate, 1),
                              'prev_premium': round(prev_premium, 2),
                              'n_zt': n_zt, 'n_zb': n_zb, 'n_prev': n_prev}
        except Exception as e:
            logger.warning(f"涨停三池获取失败 {trade_date}: {type(e).__name__} {str(e)[:50]}")
            result = None

        cached[cache_key] = result
        # 2026-09-07 回访修复：有界化，只保留最近 5 个交易日，防长进程膨胀
        self._zt_emotion_cache = dict(sorted(cached.items())[-5:])
        return result

    def get_ths_hot_stocks(self, date: str = None) -> pd.DataFrame:
        """同花顺当日强势股 + 题材归因 reason tags"""
        if date is None:
            date = datetime.now().strftime('%Y-%m-%d')
        try:
            url = (f"http://zx.10jqka.com.cn/event/api/getharden/"
                   f"date/{date}/orderby/date/orderway/desc/charset/GBK/")
            headers = {
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/131.0.0.0 Safari/537.36",
                "Referer": "http://zx.10jqka.com.cn/",
            }
            r = _session.get(url, headers=headers, timeout=10)
            data = r.json()
            if data.get("errocode", 0) != 0:
                self._update_source_status('ths_hot', False, data.get('errormsg', ''))
                return pd.DataFrame()
            rows = data.get("data") or []
            if not rows:
                self._update_source_status('ths_hot', False, 'empty')
                return pd.DataFrame()

            # schema 漂移检测（2026-09-16 P2-1）
            # 背景：上游 2026-09 起只返回 id/name/code/reason/date/market 六个字段，
            # 旧 schema 的行情字段（close/zhangfu/huanshou/chengjiaoe/ddejingliang…）
            # 全部消失。此前消费端 `row.get('涨幅%', 0)` 键不存在即静默回落 0，
            # 简报"题材热度 TOP10"整列渲染成 `+0.0% ➖`，无异常、无告警、源状态仍报"可用"。
            # 现分级处置：
            #   - reason（题材归因，唯一被消费的功能性字段）缺失 → 真不可用，熔断；
            #   - 仅行情字段缺失 → 数据仍可用，但必须"响一声"（WARNING + 状态备注），
            #     让漂移可被事后追溯，而不是静默产出伪 0 值。
            _expected_market_fields = (
                "close", "zhangdie", "zhangfu", "huanshou",
                "chengjiaoe", "chengjiaoliang", "ddejingliang")
            if not any("reason" in r for r in rows[:20]):
                self._update_source_status(
                    'ths_hot', False, 'schema_changed: 缺少题材归因(reason)字段')
                logger.warning("同花顺强势股接口 schema 变更：reason 字段缺失，题材热度不可用")
                return pd.DataFrame()
            _present = {k for k in _expected_market_fields
                        if any(k in r for r in rows[:50])}
            if len(_present) < len(_expected_market_fields) * 0.5:
                _missing = sorted(set(_expected_market_fields) - _present)
                logger.warning(
                    f"同花顺强势股接口字段漂移：期望 {len(_expected_market_fields)} 个"
                    f"行情字段，实际仅 {len(_present)} 个 → 缺失 {_missing}。"
                    f"题材热度将不再展示涨跌幅（不伪造 0.0%）。")
                self._update_source_status(
                    'ths_hot', True, f'schema_changed: 缺失 {_missing}')
            else:
                self._update_source_status('ths_hot', True)
            df = pd.DataFrame(rows)
            rename_map = {
                "name": "名称", "code": "代码", "reason": "题材归因",
                "close": "收盘价", "zhangdie": "涨跌额", "zhangfu": "涨幅%",
                "huanshou": "换手率%", "chengjiaoe": "成交额",
                "chengjiaoliang": "成交量", "ddejingliang": "大单净量",
                "market": "市场",
            }
            df = df.rename(columns=rename_map)
            return df
        except Exception as e:
            self._update_source_status('ths_hot', False, str(e)[:60])
            logger.warning(f"同花顺热点获取失败: {str(e)[:60]}")
        return pd.DataFrame()

    def extract_hot_themes(self, ths_df: pd.DataFrame) -> List[Dict]:
        """从同花顺强势股数据中提取当日热门题材及关联股票"""
        if ths_df.empty or '题材归因' not in ths_df.columns:
            return []
        from collections import Counter, defaultdict

        def _opt_pct(v):
            """涨幅可缺失（2026-09-16 P2-1：上游已不再提供该字段）。
            缺失必须是 None —— 绝不能回落 0，否则展示层会把它渲染成
            `+0.0%`，把"没有数据"伪装成"涨幅为零"。"""
            if isinstance(v, bool) or v is None:
                return None
            try:
                f = float(v)
            except (TypeError, ValueError):
                return None
            return f if f == f else None   # NaN → None

        theme_stocks = defaultdict(list)
        for _, row in ths_df.iterrows():
            reason = str(row.get('题材归因', ''))
            if reason and reason != 'nan':
                tags = [t.strip() for t in reason.split('+') if t.strip()]
                for tag in tags:
                    theme_stocks[tag].append({
                        'code': str(row.get('代码', '')).zfill(6),
                        'name': row.get('名称', ''),
                        'pct_chg': _opt_pct(row.get('涨幅%')),
                    })
        result = []
        for theme, stocks in sorted(theme_stocks.items(), key=lambda x: len(x[1]), reverse=True):
            # 有涨幅的按涨幅降序排在前，无涨幅的保持上游原始顺序（稳定排序）
            def _rank_key(s):
                v = s.get('pct_chg')
                return (1, v) if isinstance(v, float) else (0, 0.0)
            top_stocks = sorted(stocks, key=_rank_key, reverse=True)[:3]
            result.append({
                'theme': theme,
                'count': len(stocks),
                'top_stocks': top_stocks,
            })
        return result

    # ========== 9. 东财板块归属（a-stock-data §3.3） ==========

    def get_stock_blocks(self, code: str, force_live: bool = False) -> Dict:
        """个股所属板块/概念归属（东财 slist，已走 em_get 限流）

        force_live=True: 跳过预取缓存直接拉实时（板块涨幅 change_pct 是日频数据，
        预取缓存是周末的 stale 涨幅，评分路径必须当日实时）。
        """
        # 东财预取缓存优先（板块归属短期稳定，非交易日预取→交易日读缓存避开限流+WAF）
        # 注意：预取缓存存的是预取当日（通常周六）的 change_pct，展示可用，评分需 force_live
        if not force_live and not self._prefetch_mode:
            cached = self._read_eastmoney_prefetch('blocks', str(code).zfill(6), max_age_days=7)
            if cached is not None:
                return cached
        market_code = 1 if str(code).zfill(6).startswith('6') else 0
        params = {
            "fltt": "2", "invt": "2",
            "secid": f"{market_code}.{str(code).zfill(6)}",
            "spt": "3", "pi": "0", "pz": "200", "po": "1",
            "fields": "f12,f14,f3,f128",
        }
        headers = {"Referer": "https://quote.eastmoney.com/"}
        try:
            r = em_get("https://push2.eastmoney.com/api/qt/slist/get",
                       params=params, headers=headers, timeout=15)
            d = r.json()
            diff = (d.get("data") or {}).get("diff") or {}
            items = diff.values() if isinstance(diff, dict) else diff
            boards = []
            for it in items:
                boards.append({
                    "name": it.get("f14", ""),
                    "code": it.get("f12", ""),
                    "change_pct": it.get("f3", ""),
                    "lead_stock": it.get("f128", ""),
                })
            self._update_source_status('eastmoney_blocks', True)
            return {
                "total": len(boards),
                "boards": boards,
                "concept_tags": [b["name"] for b in boards],
            }
        except Exception as e:
            self._update_source_status('eastmoney_blocks', False, str(e)[:60])
            logger.warning(f"东财板块归属失败 {code}: {str(e)[:60]}")
        return {"total": 0, "boards": [], "concept_tags": []}

    # ========== 9.5. 限售股解禁日历（2026-06-16 新增） ==========

    def _ensure_lockup_cache(self, days_ahead: int = 90) -> None:
        """按需拉取当前日期后 N 天解禁日历，加载到模块级 dict 缓存。

        AkShare stock_restricted_release_detail_em(start, end) 一次拿若干天的全市场解禁列表 ~3-4s。
        200 只候选票直接 O(1) 查询，无需重拉。 缓存失效：当日 16:00 后、跨日重置。
        """
        from datetime import date, timedelta
        if not self._source_available.get('lockup', True):
            self._lockup_cache = {}
            self._lockup_cache_date = None
            return
        today = date.today()
        # 已经缓存，且是今天的 -> 复用
        if (self._lockup_cache_date == today
                and self._lockup_cache_horizon >= days_ahead
                and self._lockup_cache):
            return
        try:
            import akshare as ak
            # 包含昨日 + 未来 days_ahead 天，兼容复盘和当日评分
            start = (today - timedelta(days=1)).strftime("%Y%m%d")
            end = (today + timedelta(days=days_ahead)).strftime("%Y%m%d")
            df = ak.stock_restricted_release_detail_em(start_date=start, end_date=end)
            cache = {}
            if df is not None and not df.empty:
                col_code = "股票代码" if "股票代码" in df.columns else df.columns[1]
                col_date = "解禁时间" if "解禁时间" in df.columns else df.columns[3]
                col_ratio = "占解禁前流通市值比例"
                col_amt = "实际解禁市值"
                col_type = "限售股类型"
                for _, row in df.iterrows():
                    code = str(row[col_code]).zfill(6)
                    if code not in cache:
                        cache[code] = {
                            "next_unlock_date": str(row[col_date])[:10],
                            "max_ratio": 0.0,
                            "total_amount": 0.0,
                            "type": str(row[col_type]) if col_type in df.columns else "",
                        }
                    ratio = float(row[col_ratio]) if col_ratio in df.columns else 0.0
                    amount = float(row[col_amt]) if col_amt in df.columns else 0.0
                    if ratio > cache[code]["max_ratio"]:
                        cache[code]["max_ratio"] = ratio
                        cache[code]["next_unlock_date"] = str(row[col_date])[:10]
                    cache[code]["total_amount"] += amount
            self._lockup_cache = cache
            self._lockup_cache_date = today
            self._lockup_cache_horizon = days_ahead
            logger.info(f"[lockup] 已缓存 {len(cache)} 只票未来 {days_ahead} 天解禁日历")
            self._update_source_status('lockup', True)
        except Exception as e:
            logger.warning(f"解禁日历缓存失败: {str(e)[:80]}")
            self._source_available['lockup'] = False
            self._update_source_status('lockup', False, str(e)[:80])
            self._lockup_cache = {}
            self._lockup_cache_date = None

    def get_lockup_expiry(self, code: str) -> Optional[Dict]:
        """返回该票未来 90 天内的解禁压力概况。

        Returns
        -------
        Optional[Dict]
            {
                "next_unlock_date": "2026-07-15",
                "max_ratio": 0.085,        # 未来解禁数量/解禁前流通市值最大比例
                "total_amount": 1.2e8,     # 累计解禁市值
                "type": "首发原股东限售股份"
            }
            或 None（无解禁事件或缓存不可用）
        """
        if not self._source_available.get('lockup', True):
            return None
        self._ensure_lockup_cache()
        code = str(code).zfill(6)
        return self._lockup_cache.get(code)

    # ========== 10. 龙虎榜（a-stock-data §3.5） ==========

    def get_dragon_tiger(self, code: str, trade_date: str = None, look_back: int = 30) -> Dict:
        """龙虎榜：上榜记录 + 买卖席位 + 机构动向"""
        # 东财预取缓存优先（龙虎榜 T+1 数据，周末拉到的就是周五最新榜，周一可用）
        if not self._prefetch_mode and trade_date is None:
            cached = self._read_eastmoney_prefetch('dragon_tiger', str(code).zfill(6), max_age_days=7)
            if cached is not None:
                return cached
        if trade_date is None:
            trade_date = datetime.now().strftime('%Y-%m-%d')
        start = (datetime.now() - timedelta(days=look_back)).strftime('%Y-%m-%d')
        datacenter_url = "https://datacenter-web.eastmoney.com/api/data/v1/get"

        records = []
        # 修复（2026-09-06 审查）：seats/institution 初始化上移——原先在 try 块
        # 之后才赋值，em_get 返回 None 的提前 return 会触发 NameError
        seats = {"buy": [], "sell": []}
        institution = {"buy_amt": 0, "sell_amt": 0, "net_amt": 0}
        try:
            params = {
                "reportName": "RPT_DAILYBILLBOARD_DETAILSNEW",
                "columns": "ALL",
                "filter": f"(TRADE_DATE>='{start}')(TRADE_DATE<='{trade_date}')(SECURITY_CODE=\"{code}\")",
                "pageNumber": "1", "pageSize": "50",
                "sortColumns": "TRADE_DATE", "sortTypes": "-1",
                "source": "WEB", "client": "WEB",
            }
            r = em_get(datacenter_url, params=params, timeout=15)
            if r is None:
                logger.warning(f"龙虎榜记录失败 {code}: em_get 返回 None")
                return {"records": [], "seats": seats, "institution": institution}
            data = (r.json().get("result") or {}).get("data", [])
            for row in data:
                records.append({
                    "date": str(row.get("TRADE_DATE", ""))[:10],
                    "reason": row.get("EXPLANATION", ""),
                    "net_buy_wan": round((row.get("BILLBOARD_NET_AMT") or 0) / 10000, 1),
                    "turnover": round(float(row.get("TURNOVERRATE") or 0), 2),
                })
        except Exception as e:
            logger.warning(f"龙虎榜记录失败 {code}: {str(e)[:60]}")

        if records:
            latest = records[0]["date"]
            for side, (report, col) in enumerate([
                ("RPT_BILLBOARD_DAILYDETAILSBUY", "BUY"),
                ("RPT_BILLBOARD_DAILYDETAILSSELL", "SELL")
            ]):
                key = "buy" if side == 0 else "sell"
                try:
                    params = {
                        "reportName": report, "columns": "ALL",
                        "filter": f"(TRADE_DATE='{latest}')(SECURITY_CODE=\"{code}\")",
                        "pageNumber": "1", "pageSize": "10",
                        "sortColumns": col, "sortTypes": "-1",
                        "source": "WEB", "client": "WEB",
                    }
                    r = em_get(datacenter_url, params=params, timeout=15)
                    if r is None:
                        logger.warning(f"龙虎榜{key}席位失败 {code}: em_get 返回 None")
                        continue
                    sdata = (r.json().get("result") or {}).get("data", [])
                    for row in sdata[:5]:
                        seats[key].append({
                            "name": row.get("OPERATEDEPT_NAME", ""),
                            "buy_amt": round((row.get("BUY") or 0) / 10000, 1),
                            "sell_amt": round((row.get("SELL") or 0) / 10000, 1),
                            "net": round((row.get("NET") or 0) / 10000, 1),
                        })
                        if str(row.get("OPERATEDEPT_CODE", "")) == "0":
                            amt = (row.get("BUY") or 0) if side == 0 else (row.get("SELL") or 0)
                            if side == 0:
                                institution["buy_amt"] += amt
                            else:
                                institution["sell_amt"] += amt
                except Exception as e:
                    logger.warning(f"龙虎榜{key}席位失败 {code}: {str(e)[:60]}")

            institution["buy_amt"] = round(institution["buy_amt"] / 10000, 1)
            institution["sell_amt"] = round(institution["sell_amt"] / 10000, 1)
            institution["net_amt"] = round(institution["buy_amt"] - institution["sell_amt"], 1)
            self._update_source_status('dragon_tiger', True)
        else:
            self._update_source_status('dragon_tiger', False, '无上榜记录')

        return {"records": records, "seats": seats, "institution": institution}

    def _update_source_status(self, source_key: str, success: bool, error: str = None):
        """更新数据源状态"""
        if source_key in self._source_status:
            self._source_status[source_key]['available'] = success
            if error:
                self._source_status[source_key]['last_error'] = str(error)[:100]
            else:
                self._source_status[source_key]['last_error'] = None

    @staticmethod
    def _safe_float(val) -> float:
        # 修复（2026-09-06 迭代）：NaN 原样穿透——float('nan') 不进 except 且
        # bool(nan) 为 True，nan<=0 为 False，会绕过所有行情防护继续污染计算
        try:
            f = float(val) if val else 0.0
            if f != f:  # NaN 检测（NaN != NaN）
                return 0.0
            return f
        except (ValueError, TypeError):
            return 0.0
