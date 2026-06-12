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
import time
import random
import logging
from datetime import datetime, timedelta
from typing import Optional, List, Dict
from contextlib import contextmanager
from concurrent.futures import ThreadPoolExecutor, as_completed

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
        time.sleep(wait + random.uniform(0.1, 0.5))
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
        self._mootdx_client = None  # 懒加载，重用TCP连接
        self._kline_cache_path = os.path.join(
            os.path.dirname(os.path.dirname(__file__)), 'data', 'kline_cache.db'
        )
        self._init_kline_cache()

        self.SH_PREFIXES = ('600', '601', '603', '605', '688')
        self.SZ_PREFIXES = ('000', '001', '002', '003', '300', '301')

        # 数据源状态追踪
        self._source_status = {
            'akshare_codes':     {'available': True,  'last_error': None, 'label': 'A股代码列表'},
            'tencent_quote':     {'available': True,  'last_error': None, 'label': '腾讯实时行情'},
            'mootdx_kline':      {'available': True,  'last_error': None, 'label': 'Mootdx K线'},
            'baostock_kline':    {'available': True,  'last_error': None, 'label': 'Baostock K线'},
            'akshare_fund_flow': {'available': False, 'last_error': None, 'label': '主力资金流(东方财富)'},
            'akshare_north_flow':{'available': False, 'last_error': None, 'label': '北向资金(东方财富)'},
            'ths_hot':           {'available': True,  'last_error': None, 'label': '同花顺强势股'},
            'eastmoney_blocks':  {'available': True,  'last_error': None, 'label': '东财板块归属'},
            'dragon_tiger':      {'available': True,  'last_error': None, 'label': '龙虎榜'},
        }

    def _init_kline_cache(self):
        """初始化K线本地缓存表"""
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
                    bvps REAL, PRIMARY KEY (code, report_date)
                )
            """)
            conn.commit()
            conn.close()
        except Exception as e:
            logger.warning(f"K线缓存初始化失败: {e}")

    def _get_kline_from_cache(self, code: str, start_date: str, end_date: str) -> Optional[pd.DataFrame]:
        """从本地缓存读取K线"""
        try:
            import sqlite3
            conn = sqlite3.connect(self._kline_cache_path)
            df = pd.read_sql_query(
                "SELECT date,open,high,low,close,volume,amount FROM kline_cache "
                "WHERE code=? AND date>=? AND date<=? ORDER BY date",
                conn, params=(code, start_date, end_date)
            )
            conn.close()
            if not df.empty:
                df['date'] = pd.to_datetime(df['date'])
                for c in ['open', 'high', 'low', 'close', 'volume', 'amount']:
                    df[c] = pd.to_numeric(df[c], errors='coerce')
                return df
            return None
        except Exception:
            return None

    def _save_kline_to_cache(self, code: str, df: pd.DataFrame):
        """将K线写入本地缓存"""
        if df is None or df.empty:
            return
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
            conn.close()
        except Exception as e:
            logger.warning(f"K线缓存写入失败 {code}: {e}")

    def _fetch_kline_mootdx(self, code: str, start_date: str, end_date: str) -> Optional[pd.DataFrame]:
        """
        用 mootdx (通达信 TCP 协议) 拉取个股日K线
        来源：a-stock-data §1.1
        优势：TCP 二进制协议，永不封 IP，~0.01s/只（复用连接更接近0.01s）
        """
        try:
            from mootdx.quotes import Quotes
            if self._mootdx_client is None:
                self._mootdx_client = Quotes.factory(market='std')
            client = self._mootdx_client
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
            os.path.dirname(os.path.dirname(__file__)), 'data', 'codes_cache.json'
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
        except Exception:
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

    def get_all_quotes(self) -> pd.DataFrame:
        """全市场实时行情快照 — 腾讯API，每批200只，失败自动重试一次"""
        cache_key = f"quotes_{datetime.now().strftime('%Y-%m-%d_%H:%M')}"
        if cache_key in self.cache:
            return self.cache[cache_key]

        codes = self._get_all_codes()
        if not codes:
            return pd.DataFrame()

        results = []
        batch_size = 200
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
                            'amount': self._safe_float(fields[37]),
                            'turnover': self._safe_float(fields[38]),
                            'pe': self._safe_float(fields[39]),
                            'pb': self._safe_float(fields[46]) if len(fields) > 46 else None,
                            'total_market_cap': self._safe_float(fields[45]) if len(fields) > 45 else None,
                            'circulating_market_cap': self._safe_float(fields[44]) if len(fields) > 44 else None,
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
        self._update_source_status('tencent_quote', True)
        logger.info(f"全市场行情: {len(df)} 只股票")
        return df

    # ========== 3. 个股K线（mootdx → Sina → baostock 三源回退） ==========

    def get_kline(self, code: str, start_date: str = None,
                  end_date: str = None) -> pd.DataFrame:
        """
        个股历史K线（含本地缓存）

        数据源优先级（参考 a-stock-data V3.2）：
          1. mootdx (TCP 通达信) — 永不封IP，~0.01s/只
          2. 新浪HTTP — ~0.2s/只，40x快于baostock
          3. baostock — ~8s/只，降级备用

        返回字段: date,open,high,low,close,volume,amount
        含 pct_chg 和技术指标
        """
        if start_date is None:
            start_date = (datetime.now() - timedelta(days=200)).strftime('%Y-%m-%d')
        if end_date is None:
            end_date = datetime.now().strftime('%Y-%m-%d')

        # 1. 先读缓存
        cached = self._get_kline_from_cache(code, start_date, end_date)
        if cached is not None and not cached.empty:
            return cached

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
        code6 = str(code).zfill(6)
        bs_code = f"sh.{code6}" if code6.startswith(self.SH_PREFIXES) else f"sz.{code6}"

        try:
            import baostock as bs
            bs.login()
            rs = bs.query_history_k_data_plus(
                bs_code, 'date,open,high,low,close,volume,amount',
                start_date=start_date, end_date=end_date,
                frequency='d', adjustflag='2')
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
                df = df.sort_values('date')
                self._save_kline_to_cache(code, df)
                return self._calc_kline_indicators(df)
        except Exception as e:
            logger.warning(f"baostock K线失败 {code}: {str(e)[:60]}")
            try:
                bs.logout()
            except:
                pass
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
            from mootdx.quotes import Quotes
            if self._mootdx_client is None:
                self._mootdx_client = Quotes.factory(market='std')
            client = self._mootdx_client

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
                # 写入缓存
                try:
                    import sqlite3
                    conn = sqlite3.connect(self._kline_cache_path)
                    conn.execute(
                        "INSERT OR REPLACE INTO fin_cache (code,report_date,eps,roe,profit,income,bvps) "
                        "VALUES (?,?,?,?,?,?,?)",
                        (code6, result['report_date'], result['eps'], result['roe'],
                         result['profit'], result['income'], result['bvps'])
                    )
                    conn.commit()
                    conn.close()
                except Exception:
                    pass
                return result
        except Exception as e:
            logger.warning(f"财务快照失败 {code}: {str(e)[:50]}")
        return {}

    def _get_fin_from_cache(self, code: str) -> Optional[Dict]:
        """从SQLite缓存读取财务数据"""
        try:
            import sqlite3
            conn = sqlite3.connect(self._kline_cache_path)
            cursor = conn.execute(
                "SELECT eps,roe,profit,income,bvps,report_date FROM fin_cache "
                "WHERE code=? ORDER BY report_date DESC LIMIT 1", (code,)
            )
            row = cursor.fetchone()
            conn.close()
            if row and row[0] is not None:
                return {
                    'eps': float(row[0] or 0),
                    'roe': float(row[1] or 0),
                    'profit': float(row[2] or 0),
                    'income': float(row[3] or 0),
                    'bvps': float(row[4] or 0),
                    'report_date': str(row[5] or ''),
                }
        except Exception:
            pass
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

    # ========== 资金面（akshare，可能被网络限制） ==========

    def get_main_fund_accumulated(self, code: str, days: int = 10) -> Optional[float]:
        """
        主力资金近N日累计 — 多源回退链：
        1. 大单交易汇总（今日大单净流向，最快）
        2. akshare (东方财富 SDK，备用)
        3. 直连东财 push2 API (绕过SDK，待启用)
        调用方自动降权处理
        """
        # 源1: 全市场大单交易汇总（内存缓存，毫秒级）
        result = self._get_capital_flow_big_deal(code)
        if result is not None:
            return result

        # 源2: akshare (带短超时)
        try:
            import akshare as ak
            market = 'sh' if str(code).zfill(6).startswith(self.SH_PREFIXES) else 'sz'
            df = ak.stock_individual_fund_flow(stock=str(code).zfill(6), market=market)
            if df is not None and not df.empty:
                result = float(df.tail(days)['主力净流入-净额'].sum())
                self._update_source_status('akshare_fund_flow', True)
                return result
        except Exception as e:
            self._update_source_status('akshare_fund_flow', False, str(e)[:60])

        # 源2: 直连东财 push2 API（当前网络环境不通，保留代码待以后启用）
        # result = self._get_capital_flow_push2(code, days)
        # if result is not None:
        #     return result

        # 源3: 全市场大单交易汇总（今日净流入）
        result = self._get_capital_flow_big_deal(code)
        if result is not None:
            self._update_source_status('akshare_fund_flow', True)
            return result

        return None

    def _get_capital_flow_big_deal(self, code: str) -> Optional[float]:
        """从全市场大单交易汇总中推算个股今日主力净流入（买入-卖出差额）"""
        try:
            # 缓存：整个 session 只拉一次全量大单数据，持久化到磁盘
            if not hasattr(self, '_big_deal_cache') or self._big_deal_cache is None:
                self._big_deal_cache = {}
                import akshare as ak
                import pandas as pd
                df = ak.stock_fund_flow_big_deal()
                df.columns = ['成交时间', '股票代码', '股票简称', '成交价格',
                               '成交量', '成交金额', '大单性质', '涨跌幅', '涨跌额']
                df['成交金额'] = pd.to_numeric(df['成交金额'], errors='coerce').fillna(0) * 10000
                buy_mask = df['大单性质'].str.contains('买|主', na=False)
                sell_mask = df['大单性质'].str.contains('卖', na=False)
                buy_sum = df[buy_mask].groupby('股票代码')['成交金额'].sum()
                sell_sum = df[sell_mask].groupby('股票代码')['成交金额'].sum()
                net = buy_sum.subtract(sell_sum, fill_value=0)
                self._big_deal_cache = net
                self._update_source_status('akshare_fund_flow', True)
                logger.info(f"全市场大单数据已加载: {len(net)} 只股票，每只约{len(df)//len(net)}笔大单")

            code_int = int(code)
            if code_int in self._big_deal_cache.index:
                net_val = float(self._big_deal_cache.loc[code_int])
                if abs(net_val) > 0:
                    return net_val
            return None
        except Exception as e:
            logger.warning(f"大单资金流失败 {code}: {str(e)[:80]}")
            self._big_deal_cache = None
        return None

    def _get_capital_flow_push2(self, code: str, days: int = 10) -> Optional[float]:
        """直连东方财富 push2his 资金流 API（绕过 akshare SDK）"""
        try:
            market = 1 if str(code).zfill(6).startswith(('6', '9')) else 0
            url = "https://push2his.eastmoney.com/api/qt/stock/fflow/daykline/get"
            params = {
                "secid": f"{market}.{str(code).zfill(6)}",
                "fields1": "f1,f2,f3,f7",
                "fields2": "f51,f52,f53,f54,f55,f56,f57",
                "lmt": str(max(days, 120)),
            }
            headers = {
                "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                               "AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36"),
                "Referer": "https://quote.eastmoney.com/",
            }
            r = _session.get(url, params=params, headers=headers, timeout=3)
            data = r.json()
            klines = data.get("data", {}).get("klines", [])
            if klines:
                total = 0.0
                count = 0
                for line in klines[-days:]:
                    parts = line.split(",")
                    if len(parts) >= 2 and parts[1] != "-":
                        total += float(parts[1])
                        count += 1
                if count > 0:
                    return total
        except Exception as e:
            logger.warning(f"push2资金流失败 {code}: {str(e)[:60]}")
        return None

    def get_north_flow_accumulated(self, code: str, days: int = 10) -> Optional[float]:
        """
        北向资金近N日累计（东方财富源，网络受限时返回None）
        """
        try:
            import akshare as ak
            df = ak.stock_hsgt_individual_em(symbol=str(code).zfill(6))
            if df is not None and not df.empty:
                self._update_source_status('akshare_north_flow', True)
                return df.tail(days)['当日净流入'].sum()
        except Exception as e:
            self._update_source_status('akshare_north_flow', False, str(e)[:60])
        return None

    def get_north_flow_summary(self) -> Optional[Dict]:
        """
        全市场北向资金当日汇总（沪股通+深股通净买入）
        来源：同花顺 hexin.cn（实时，无鉴权）
        返回：{'hgt': -9.28, 'sgt': -31.1, 'total': -40.38, 'time': '15:00'}
              单位：亿元，None表示数据不可用
        """
        try:
            import requests as req
            url = "https://data.hexin.cn/market/hsgtApi/method/dayChart/"
            headers = {
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/131.0.0.0 Safari/537.36',
                'Referer': 'https://data.hexin.cn/',
            }
            r = req.get(url, headers=headers, timeout=10)
            d = r.json()
            times = d.get('time', [])
            hgt = d.get('hgt', [])
            sgt = d.get('sgt', [])
            if not times:
                return None
            # 找最后一个有数据的点
            for t, h, s in zip(reversed(times), reversed(hgt), reversed(sgt)):
                if h is not None and s is not None:
                    return {
                        'hgt': round(float(h), 2),
                        'sgt': round(float(s), 2),
                        'total': round(float(h) + float(s), 2),
                        'time': t,
                    }
            return None
        except Exception as e:
            logger.warning(f"北向汇总获取失败: {str(e)[:60]}")
        return None

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
            df = pd.DataFrame(rows)
            rename_map = {
                "name": "名称", "code": "代码", "reason": "题材归因",
                "close": "收盘价", "zhangdie": "涨跌额", "zhangfu": "涨幅%",
                "huanshou": "换手率%", "chengjiaoe": "成交额",
                "chengjiaoliang": "成交量", "ddejingliang": "大单净量",
                "market": "市场",
            }
            df = df.rename(columns=rename_map)
            self._update_source_status('ths_hot', True)
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
        theme_stocks = defaultdict(list)
        for _, row in ths_df.iterrows():
            reason = str(row.get('题材归因', ''))
            if reason and reason != 'nan':
                tags = [t.strip() for t in reason.split('+') if t.strip()]
                for tag in tags:
                    theme_stocks[tag].append({
                        'code': str(row.get('代码', '')).zfill(6),
                        'name': row.get('名称', ''),
                        'pct_chg': row.get('涨幅%', 0),
                    })
        result = []
        for theme, stocks in sorted(theme_stocks.items(), key=lambda x: len(x[1]), reverse=True):
            top_stocks = sorted(stocks, key=lambda s: float(s['pct_chg'] or 0), reverse=True)[:3]
            result.append({
                'theme': theme,
                'count': len(stocks),
                'top_stocks': top_stocks,
            })
        return result

    # ========== 9. 东财板块归属（a-stock-data §3.3） ==========

    def get_stock_blocks(self, code: str) -> Dict:
        """个股所属板块/概念归属（东财 slist，已走 em_get 限流）"""
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

    # ========== 10. 龙虎榜（a-stock-data §3.5） ==========

    def get_dragon_tiger(self, code: str, trade_date: str = None, look_back: int = 30) -> Dict:
        """龙虎榜：上榜记录 + 买卖席位 + 机构动向"""
        if trade_date is None:
            trade_date = datetime.now().strftime('%Y-%m-%d')
        start = (datetime.now() - timedelta(days=look_back)).strftime('%Y-%m-%d')
        datacenter_url = "https://datacenter-web.eastmoney.com/api/data/v1/get"

        records = []
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
            data = r.json().get("result", {}).get("data", [])
            for row in data:
                records.append({
                    "date": str(row.get("TRADE_DATE", ""))[:10],
                    "reason": row.get("EXPLANATION", ""),
                    "net_buy_wan": round((row.get("BILLBOARD_NET_AMT") or 0) / 10000, 1),
                    "turnover": round(float(row.get("TURNOVERRATE") or 0), 2),
                })
        except Exception as e:
            logger.warning(f"龙虎榜记录失败 {code}: {str(e)[:60]}")

        seats = {"buy": [], "sell": []}
        institution = {"buy_amt": 0, "sell_amt": 0, "net_amt": 0}

        if records:
            latest = records[0]["date"]
            for side, (report, col) in enumerate([
                ("RPT_BILLBOARD_DAILYDETAILSBUY", "BUY"),
                ("RPT_BILLBOARD_DAILYDETAILSSELL", "SELL")
            ]):
                try:
                    params = {
                        "reportName": report, "columns": "ALL",
                        "filter": f"(TRADE_DATE='{latest}')(SECURITY_CODE=\"{code}\")",
                        "pageNumber": "1", "pageSize": "10",
                        "sortColumns": col, "sortTypes": "-1",
                        "source": "WEB", "client": "WEB",
                    }
                    r = em_get(datacenter_url, params=params, timeout=15)
                    sdata = r.json().get("result", {}).get("data", [])
                    key = "buy" if side == 0 else "sell"
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
        try:
            return float(val) if val else 0.0
        except (ValueError, TypeError):
            return 0.0
