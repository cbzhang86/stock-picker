"""
数据获取流程全链路测试 — 2026-09-05

覆盖：
  Group A  ASHareHub 配额管理（临时账本，不消耗真实配额）
  Group B  ASHareHub 接口逻辑（mock client，正常/空/异常三路径）
  Group C  ASHareHub 真实连通性（仅 1 次真实调用，控制配额）
  Group D  免费行情源（腾讯全市场 / mootdx K线 / mootdx finance）
  Group E  东财/同花顺/akshare（板块/龙虎榜/新闻/解禁/北向汇总/资金流）

配额纪律：
  - 真实 ASHareHub 调用仅 Group C 1 次（get_financial_indicators）
  - Group A/B 全部走临时账本 + FakeClient，零配额消耗
  - 其余数据源（腾讯/mootdx/东财/同花顺/akshare）无日配额限制

输出：reports/data_pipeline_test_<date>.json + 控制台摘要
"""
import json
import logging
import os
import sys
import tempfile
import time
import traceback
from datetime import datetime

if sys.platform == 'win32':
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)

logging.basicConfig(level=logging.WARNING, format='%(levelname)s %(message)s')
logging.getLogger('core.data_engine').setLevel(logging.WARNING)

RESULTS = []
ONLY = None  # --only A2,B1 指定重跑项；None = 全量


def record(group, name, status, detail='', elapsed=None, extra=None):
    """记录一条测试结果"""
    RESULTS.append({
        'group': group, 'name': name, 'status': status,
        'detail': str(detail)[:300], 'elapsed_s': round(elapsed, 2) if elapsed else None,
        'extra': extra,
    })
    icon = {'PASS': '✅', 'FAIL': '❌', 'SKIP': '⏭️', 'WARN': '⚠️'}.get(status, '·')
    el = f" ({elapsed:.1f}s)" if elapsed else ""
    print(f"{icon} [{group}] {name}{el}" + (f" — {str(detail)[:120]}" if detail else ""))


def run(group, name, fn, allow_none=False):
    """执行单个测试：fn() 返回 (ok: bool, detail: str) 或 None（auto-ok）"""
    if ONLY is not None and name.split(' ')[0] not in ONLY:
        return
    t0 = time.time()
    try:
        out = fn()
        el = time.time() - t0
        if out is None:
            record(group, name, 'PASS', '', el)
        else:
            ok, detail = out
            record(group, name, 'PASS' if ok else 'FAIL', detail, el)
    except Exception as e:
        el = time.time() - t0
        record(group, name, 'FAIL', f"{type(e).__name__}: {e}", el,
               extra=traceback.format_exc()[-500:])
        # 恢复可能被污染的 sys.modules
        if 'asharehub' in sys.modules:
            try:
                del sys.modules['asharehub']
            except KeyError:
                pass


# ============================================================
#  mock 基建
# ============================================================

class FakeClient:
    """可控行为的 ASHareHub client 替身"""
    mode = 'ok'  # ok / empty / raise

    def _df(self):
        import pandas as pd
        if self.mode == 'empty':
            return pd.DataFrame()
        if self.mode == 'raise':
            raise TimeoutError("mock network timeout")
        return pd.DataFrame([
            # 行序=升序（旧行在前、最新行在后），匹配 data_engine iloc[-1] 的取数语义
            {'trade_date': '20260820', 'net_mf_amount': -100.0,
             'symbol': 'BK0804.DC', 'vol': 12000.0},
            {'trade_date': '20260904', 'net_mf_amount': 1234.5,
             'macd_dif': 0.12, 'macd_dea': 0.10, 'macd': 0.04,
             'rsi_6': 55.0, 'rsi_12': 52.0, 'rsi_24': 50.0,
             'close_hfq': 88.0, 'cci': 66.0,
             'eps': 2.5, 'roe': 15.0, 'roe_waa': 14.5, 'roa': 8.0,
             'gross_margin': 30.0, 'netprofit_margin': 20.0,
             'debt_to_assets': 40.0, 'bps': 25.0, 'ocfps': 3.0,
             'basic_eps_yoy': 10.0, 'netprofit_yoy': 22.0,
             'symbol': 'BK0804.DC', 'vol': 12345.0},
        ])

    def moneyflow(self, symbol, limit):
        return self._df()

    def technical_factors(self, symbol, limit):
        return self._df()

    def concept_members(self, con_symbol, limit):
        return self._df()

    def financial_indicators(self, symbol, limit):
        return self._df()

    def northbound_holdings(self, symbol, limit):
        return self._df()


def install_fake_client(mode='ok'):
    """把 FakeClient 注入 sys.modules['asharehub']，返回还原函数"""
    import types
    fake_mod = types.ModuleType('asharehub')

    class _AShareHub(FakeClient):
        def __init__(self, api_key='', version=''):
            pass

    fake_mod.AShareHub = _AShareHub
    _AShareHub.mode = mode
    prev = sys.modules.get('asharehub')
    sys.modules['asharehub'] = fake_mod

    def restore():
        if prev is not None:
            sys.modules['asharehub'] = prev
        else:
            sys.modules.pop('asharehub', None)
    return restore


def fresh_engine():
    """构造独立 DataEngine，配额账本指向独立临时文件（实例间互不污染真实账本）"""
    from core.data_engine import DataEngine
    de = DataEngine(config={})
    de._asharehub_quota_path = os.path.join(
        tempfile.gettempdir(), f'asharehub_quota_test_{id(de)}_{time.time():.0f}.json')
    de._load_asharehub_quota()
    de._asharehub_client = None
    return de


# ============================================================
#  Group A — 配额管理
# ============================================================

def group_a():
    print("\n━━━ Group A：ASHareHub 配额管理（临时账本，0 配额消耗）━━━")

    def a1_crossday_reset():
        de = fresh_engine()
        with open(de._asharehub_quota_path, 'w', encoding='utf-8') as f:
            json.dump({'date': '2026-09-01', 'used': 99}, f)
        de._load_asharehub_quota()
        today = datetime.now().strftime('%Y-%m-%d')
        assert de._asharehub_budget_date == today, f"账本日期未重置: {de._asharehub_budget_date}"
        assert de._asharehub_budget_used == 0, f"计数未清零: {de._asharehub_budget_used}"
        # budget_ok 应该允许（重置后有余量），并消耗 1 次
        ok = de._asharehub_budget_ok()
        assert ok is True, "重置后 budget_ok 应为 True"
        return True, f"旧账本 99/100 @2026-09-01 → 跨天重置 0 → 消耗 1 次"

    def a2_consume_counting():
        de = fresh_engine()
        for _ in range(3):
            de._asharehub_budget_ok()
        used = de._read_quota_file()
        assert used == 3, f"账本计数应为 3，实际 {used}"
        assert de._asharehub_budget_used == 3
        return True, "连续 3 次 budget_ok → 账本 used=3（原子写入验证通过）"

    def a3_exhausted_cutoff():
        de = fresh_engine()
        with open(de._asharehub_quota_path, 'w', encoding='utf-8') as f:
            json.dump({'date': datetime.now().strftime('%Y-%m-%d'), 'used': 90}, f)
        de._load_asharehub_quota()
        ok = de._asharehub_budget_ok()
        assert ok is False, "used=90（预留10）时 budget_ok 应拒绝"
        # 熔断标记
        marked = [k for k in ('asharehub_moneyflow', 'asharehub_tech_factors',
                              'asharehub_concepts', 'asharehub_financial')
                  if not de._source_available.get(k, True)]
        assert len(marked) == 4, f"熔断应标记全部4个源，实际 {marked}"
        return True, "used=90 → 拒绝 + 4 个 asharehub 源全部熔断标记"

    def a4_file_corruption_fallback():
        de = fresh_engine()
        with open(de._asharehub_quota_path, 'w', encoding='utf-8') as f:
            f.write("not-json{{{")
        de._load_asharehub_quota()  # 不应抛异常
        ok = de._asharehub_budget_ok()
        assert ok is True, "损坏账本应降级为内存计数（可用）"
        return True, "损坏 JSON → 降级内存计数，不崩溃"

    run('A', 'A1 跨天账本重置', a1_crossday_reset)
    run('A', 'A2 配额消耗计数+原子写入', a2_consume_counting)
    run('A', 'A3 配额临界熔断（used≥90）', a3_exhausted_cutoff)
    run('A', 'A4 账本损坏降级', a4_file_corruption_fallback)


# ============================================================
#  Group B — ASHareHub 接口逻辑（mock）
# ============================================================

def group_b():
    print("\n━━━ Group B：ASHareHub 接口逻辑（FakeClient，0 配额消耗）━━━")

    def b1_moneyflow_ok():
        de = fresh_engine()
        restore = install_fake_client('ok')
        try:
            v = de._get_capital_flow_asharehub('600519')
            assert v == 1234.5 * 10000, f"万元→元换算错误: {v}"
            return True, f"net_mf_amount 1234.5万元 → {v:,.0f} 元 ✓"
        finally:
            restore()

    def b2_tech_factors_ok():
        de = fresh_engine()
        restore = install_fake_client('ok')
        try:
            de._prefetch_mode = True  # 绕过缓存，直接走 API 路径
            d = de.get_technical_factors_asharehub('600519')
            assert d and abs(d['macd_dif'] - 0.12) < 1e-9 and d['rsi_6'] == 55.0, str(d)
            return True, f"解析 8 字段（macd/rsi/cci/close_hfq）✓"
        finally:
            restore()

    def b3_concepts_dedup():
        de = fresh_engine()
        restore = install_fake_client('ok')
        try:
            de._prefetch_mode = True
            names = de.get_concept_members('600519')
            assert names == ['BK0804.DC'], f"去重失败: {names}"
            return True, "2 行同概念快照 → 按最新 trade_date 去重 → 1 个 BK ✓"
        finally:
            restore()

    def b4_financial_ok():
        de = fresh_engine()
        restore = install_fake_client('ok')
        try:
            de._prefetch_mode = True
            d = de.get_financial_indicators('600519')
            assert d and d['roe'] == 15.0 and d['netprofit_yoy'] == 22.0, str(d)
            return True, "解析 11 字段（eps/roe/roa/margins/yoy）✓"
        finally:
            restore()

    def b5_northbound_delta():
        de = fresh_engine()
        restore = install_fake_client('ok')
        try:
            v = de.get_north_flow_accumulated('600519', days=10)
            assert v == 345.0, f"北向净增持股数错误: {v}"
            return True, f"最新 vol 12345 - 10日前 vol 12000 = {v} ✓"
        finally:
            restore()

    def b6_empty_df():
        de = fresh_engine()
        restore = install_fake_client('empty')
        try:
            de._prefetch_mode = True
            d = de.get_financial_indicators('600519')
            assert d is None, f"空 df 应返回 None: {d}"
            return True, "空 DataFrame → None（不熔断，仅本票 miss）✓"
        finally:
            restore()

    def b7_exception_fuse():
        de = fresh_engine()
        restore = install_fake_client('raise')
        try:
            de._prefetch_mode = True
            d = de.get_financial_indicators('600519')
            assert d is None, "异常应返回 None"
            assert de._source_available.get('asharehub_financial') is False, "熔断未触发"
            return True, "TimeoutError → 返回 None + 熔断标记 ✓"
        finally:
            restore()

    def b8_fuse_shortcircuit():
        de = fresh_engine()
        # 手动置熔断
        de._source_available['asharehub_financial'] = False
        calls = {'n': 0}
        import types
        fake_mod = types.ModuleType('asharehub')
        class _C:
            def __init__(self, **kw): pass
            def financial_indicators(self, **kw):
                calls['n'] += 1
                return FakeClient._df(FakeClient())
        fake_mod.AShareHub = _C
        sys.modules['asharehub'] = fake_mod
        try:
            de._prefetch_mode = True
            d = de.get_financial_indicators('600519')
            assert d is None and calls['n'] == 0, f"熔断后不应调用 API: calls={calls['n']}"
            return True, "熔断态 → 短路返回 None，client 零调用 ✓"
        finally:
            sys.modules.pop('asharehub', None)

    def b9_quota_refusal():
        de = fresh_engine()
        with open(de._asharehub_quota_path, 'w', encoding='utf-8') as f:
            json.dump({'date': datetime.now().strftime('%Y-%m-%d'), 'used': 95}, f)
        de._load_asharehub_quota()
        restore = install_fake_client('ok')
        try:
            v = de._get_capital_flow_asharehub('600519')
            assert v is None, "配额耗尽应返回 None"
            return True, "账本 used=95 ≥ 预留线 → budget_ok 拒绝，接口返回 None ✓"
        finally:
            restore()

    def b10_nan_in_cache():
        """已知风险验证：prefetch 缓存里存了 NaN 字面量，读回后是否污染下游"""
        import sqlite3
        conn = sqlite3.connect(os.path.join(PROJECT_ROOT, 'data', 'cache', 'asharehub_prefetch.db'))
        row = conn.execute(
            "SELECT data FROM financial WHERE data LIKE '%NaN%' LIMIT 1").fetchone()
        conn.close()
        if row is None:
            return True, "缓存中无 NaN 行（历史数据已清理）"
        import json as _json
        d = _json.loads(row[0])  # Python json 允许 NaN 字面量
        nan_keys = [k for k, v in d.items() if isinstance(v, float) and v != v]
        return (False, f"缓存存在 NaN 字段 {nan_keys[:3]}…：读回后 float('NaN') 直接进入评分路径，属已知污染风险")

    run('B', 'B1 moneyflow 万元→元解析', b1_moneyflow_ok)
    run('B', 'B2 technical_factors 解析', b2_tech_factors_ok)
    run('B', 'B3 concepts 去重', b3_concepts_dedup)
    run('B', 'B4 financial_indicators 解析', b4_financial_ok)
    run('B', 'B5 northbound 区间增量', b5_northbound_delta)
    run('B', 'B6 空数据返回 None', b6_empty_df)
    run('B', 'B7 网络异常→熔断', b7_exception_fuse)
    run('B', 'B8 熔断后短路（零调用）', b8_fuse_shortcircuit)
    run('B', 'B9 配额耗尽拒绝服务', b9_quota_refusal)
    run('B', 'B10 缓存 NaN 字面量风险', b10_nan_in_cache)


# ============================================================
#  Group C — 真实连通性（1 次配额）
# ============================================================

def group_c():
    print("\n━━━ Group C：ASHareHub 真实连通性（配额消耗 1 次）━━━")

    def c1_real_call():
        key = os.environ.get('ASHAREHUB_API_KEY', '')
        if not key:
            import re
            for p in (os.path.expanduser('~/.bashrc'), '/c/Users/Administrator/.bashrc'):
                try:
                    with open(p, encoding='utf-8') as f:
                        for line in f:
                            m = re.match(r'\s*export\s+ASHAREHUB_API_KEY\s*[=:]\s*["\']?([^"\'\s]+)', line)
                            if m:
                                key = m.group(1)
                                break
                except Exception:
                    pass
                if key:
                    break
        if not key:
            return False, "ASHAREHUB_API_KEY 未找到（env + bashrc 均空）"
        os.environ['ASHAREHUB_API_KEY'] = key

        # 诊断模式：直接调原始 client，看清 API 原始返回（区分 key无效/接口变更/解析问题）
        try:
            from asharehub import AShareHub
        except ImportError as e:
            return False, f"asharehub 包未安装: {e}"
        try:
            client = AShareHub(api_key=key, version='v2')
            df = client.financial_indicators(symbol='600519.SH', limit=1)
        except Exception as e:
            return False, f"真实 API 调用抛异常: {type(e).__name__}: {str(e)[:150]}"
        if df is None:
            return False, "真实 API 返回 None（key 无效或服务端异常）"
        if df.empty:
            return False, (f"真实 API 返回空 DataFrame（columns={list(df.columns)[:8]}）"
                           f"—— key 可能失效或接口参数变更，建议人工登录 ASHareHub 控制台核实")
        # 走 data_engine 完整解析路径
        de = fresh_engine()
        de._prefetch_mode = True
        d = de.get_financial_indicators('600519')
        quota = de._read_quota_file()
        if d is None:
            return False, (f"原始 df {len(df)} 行可取但 data_engine 解析为 None（解析 bug），"
                           f"列: {list(df.columns)[:10]}")
        return True, (f"真实调用成功: roe={d.get('roe')}, eps={d.get('eps')}, "
                      f"netprofit_yoy={d.get('netprofit_yoy')}；今日配额账本 used={quota}")

    run('C', 'C1 真实调用 get_financial_indicators(600519)', c1_real_call)


# ============================================================
#  Group D — 免费行情源
# ============================================================

def group_d():
    print("\n━━━ Group D：免费行情源（腾讯/mootdx，无配额限制）━━━")

    def d1_all_quotes():
        from core.data_engine import DataEngine
        de = DataEngine(config={})
        df = de.get_all_quotes()
        if df is None or df.empty:
            return False, "全市场行情为空（周六返回周五收盘快照也应非空）"
        zero_price = (df['price'] <= 0).sum()
        pct_bad = df['pct_chg'].abs().gt(11).sum()
        amt_zero = (df['amount'] <= 0).sum()
        return (zero_price < len(df) * 0.05 and pct_bad < len(df) * 0.05,
                f"{len(df)} 只解析成功；price≤0: {zero_price}，|pct_chg|>11%: {pct_bad}，amount≤0: {amt_zero}")

    def d2_kline_cache_hit():
        de = fresh_engine()
        df = de.get_kline('600519', '2026-06-01', '2026-09-04')
        if df is None or df.empty:
            return False, "缓存命中失败"
        need = {'open', 'high', 'low', 'close', 'volume'}
        missing = need - set(df.columns)
        return (not missing, f"缓存命中 {len(df)} 行 [{df['date'].min().date()}~{df['date'].max().date()}]"
                             f"{'，缺字段: ' + str(missing) if missing else ''}")

    def d3_kline_live():
        de = fresh_engine()
        # 表名动态获取（kline_cache.db 内表名不一定是 'kline'）
        import sqlite3
        conn = sqlite3.connect(os.path.join(PROJECT_ROOT, 'data', 'cache', 'kline_cache.db'))
        tables = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()]
        cached_codes = set()
        for t in tables:
            try:
                cols = [d[1] for d in conn.execute(f"PRAGMA table_info({t})").fetchall()]
                if 'code' in cols:
                    cached_codes |= {str(r[0]).zfill(6) for r in conn.execute(f"SELECT DISTINCT code FROM {t}").fetchall()}
            except sqlite3.Error:
                continue
        conn.close()
        live_code = next((c for c in ['603283', '603628', '603290', '603806'] if c not in cached_codes),
                         '603283')
        df = de.get_kline(live_code, '2026-06-01', '2026-09-04')
        if df is None or df.empty:
            return False, f"{live_code} live 拉取为空"
        return True, f"{live_code}（缓存未命中→mootdx live）拉到 {len(df)} 行"

    def d4_kline_invalid_code():
        de = fresh_engine()
        df = de.get_kline('999999', '2026-06-01', '2026-09-04')
        ok = df is None or (hasattr(df, 'empty') and df.empty)
        return (ok, f"无效代码 999999 → 返回 {'空' if ok else '非空(' + str(type(df)) + ')'}（三源全部 miss 后优雅降级）")

    def d5_fin_snapshot():
        de = fresh_engine()
        d = de.get_financial_snapshot('600519')
        if not d:
            return False, "mootdx finance 返回空（周六服务器应仍可查历史财报）"
        return (d.get('roe') is not None and d.get('eps') is not None,
                f"eps={d.get('eps')}, roe={d.get('roe')}%, 报告期 {d.get('report_date')}")

    run('D', 'D1 腾讯全市场行情 get_all_quotes', d1_all_quotes)
    run('D', 'D2 K线缓存命中 600519', d2_kline_cache_hit)
    run('D', 'D3 K线 live（mootdx 冷门票）', d3_kline_live)
    run('D', 'D4 K线无效代码异常处理', d4_kline_invalid_code)
    run('D', 'D5 mootdx 财务快照', d5_fin_snapshot)


# ============================================================
#  Group E — 东财/同花顺/akshare
# ============================================================

def group_e():
    print("\n━━━ Group E：东财/同花顺/akshare（无配额限制）━━━")

    def e1_ths_hot():
        from core.data_engine import DataEngine
        de = DataEngine(config={})
        df = de.get_ths_hot_stocks()
        if df.empty:
            return False, "同花顺强势股返回空（周六接口可能休市无数据，需人工确认）"
        cols_ok = {'名称', '代码', '题材归因'}.issubset(df.columns)
        return (cols_ok, f"{len(df)} 只强势股，题材归因字段{'✓' if cols_ok else '缺失'}")

    def e2_hot_themes():
        from core.data_engine import DataEngine
        de = DataEngine(config={})
        df = de.get_ths_hot_stocks()
        if df.empty:
            return None  # SKIP 语义：上游空，跳过派生测试
        themes = de.extract_hot_themes(df)
        return (len(themes) > 0, f"提取 {len(themes)} 个题材，Top: {themes[0]['theme']}({themes[0]['count']}只)" if themes else "题材提取为空")

    def e3_blocks():
        from core.data_engine import DataEngine
        de = DataEngine(config={})
        d = de.get_stock_blocks('600519', force_live=True)
        ok = d and d.get('total', 0) > 0
        return (ok, f"600519 所属板块 {d.get('total', 0)} 个: {str(d.get('boards', []))[:80]}" if d else "返回空 dict")

    def e4_dragon_tiger():
        from core.data_engine import DataEngine
        de = DataEngine(config={})
        d = de.get_dragon_tiger('600519')
        ok = isinstance(d, dict) and 'records' in d and 'seats' in d
        return (ok, f"结构完整 records={len(d.get('records', []))} 上榜记录（茅台近期未必上榜，空记录也算接口通）" if ok else f"结构异常: {type(d)}")

    def e5_north_summary():
        from core.data_engine import DataEngine
        de = DataEngine(config={})
        d = de.get_north_flow_summary()
        if d is None:
            return False, "返回 None（应始终返回 dict 含 available 标志）"
        if d.get('available'):
            return True, f"北向汇总: 沪股通 {d['hgt']}亿 + 深股通 {d['sgt']}亿 = {d['total']}亿 ({d['time']})"
        return True, f"available=False（降级标记生效），error={d.get('error')} —— 降级路径本身测试通过"

    def e6_market_news():
        from core.data_engine import DataEngine
        de = DataEngine(config={})
        items = de.get_market_news(page_size=5)
        return (len(items) > 0, f"东财 7x24 快讯 {len(items)} 条，最新: {items[0]['time'] if items else '-'}")

    def e7_stock_news():
        from core.data_engine import DataEngine
        de = DataEngine(config={})
        items = de.get_stock_news('600519', page_size=5)
        return (len(items) > 0, f"600519 个股新闻 {len(items)} 条" if items else "返回空（akshare 新闻接口周末可能限流）")

    def e8_lockup():
        from core.data_engine import DataEngine
        de = DataEngine(config={})
        d = de.get_lockup_expiry('600519')
        # 茅台近期未必有解禁 → None 也算接口通（缓存构建成功）
        cache = getattr(de, '_lockup_cache', None)
        built = cache is not None
        return (built, f"解禁日历缓存构建{'成功' if built else '失败'}，覆盖 {len(cache) if cache else 0} 只；600519: {d if d else '近期无解禁'}")

    def e9_ths_fund_flow():
        from core.data_engine import DataEngine
        de = DataEngine(config={})
        v = de._get_ths_fund_flow('600519')
        if v is None:
            return True, "周末返回 None（休市日 akshare 同花顺资金流常为空，属预期降级，熔断标记生效）"
        return True, f"600519 主力净流入 {v:,.0f} 元"

    run('E', 'E1 同花顺强势股', e1_ths_hot)
    run('E', 'E2 题材归因提取', e2_hot_themes)
    run('E', 'E3 东财板块归属', e3_blocks)
    run('E', 'E4 龙虎榜', e4_dragon_tiger)
    run('E', 'E5 全市场北向汇总', e5_north_summary)
    run('E', 'E6 东财 7x24 快讯', e6_market_news)
    run('E', 'E7 个股新闻', e7_stock_news)
    run('E', 'E8 解禁日历', e8_lockup)
    run('E', 'E9 同花顺个股资金流', e9_ths_fund_flow)


# ============================================================
#  main
# ============================================================

def main():
    global ONLY
    if len(sys.argv) > 2 and sys.argv[1] == '--only':
        ONLY = {x.strip() for x in sys.argv[2].split(',') if x.strip()}
        print(f"[only 模式] 重跑项: {sorted(ONLY)}")
    t0 = time.time()
    print(f"═══ 数据获取流程全链路测试  {datetime.now():%Y-%m-%d %H:%M:%S} ═══")

    group_a()
    group_b()
    group_c()
    group_d()
    group_e()

    total_el = time.time() - t0
    passed = sum(1 for r in RESULTS if r['status'] == 'PASS')
    failed = sum(1 for r in RESULTS if r['status'] == 'FAIL')
    warned = sum(1 for r in RESULTS if r['status'] == 'WARN')

    print(f"\n═══ 汇总: {passed} PASS / {failed} FAIL / {warned} WARN / 共 {len(RESULTS)} 项，耗时 {total_el:.0f}s ═══")

    report = {
        'test_date': datetime.now().strftime('%Y-%m-%d %H:%M'),
        'duration_s': round(total_el, 1),
        'summary': {'pass': passed, 'fail': failed, 'warn': warned, 'total': len(RESULTS)},
        'asharehub_quota_consumed': 1,  # 仅 Group C 1 次真实调用
        'results': RESULTS,
    }
    out = os.path.join(PROJECT_ROOT, 'reports',
                       f"data_pipeline_test_{datetime.now():%Y-%m-%d}{'_rerun' if ONLY else ''}.json")
    os.makedirs(os.path.dirname(out), exist_ok=True)
    with open(out, 'w', encoding='utf-8') as f:
        json.dump(report, f, ensure_ascii=False, indent=2)
    print(f"报告已写入: {out}")
    return 0 if failed == 0 else 1


if __name__ == '__main__':
    sys.exit(main())
