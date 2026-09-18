# -*- coding: utf-8 -*-
"""A 股交易日历（集中式，带本地持久化缓存）

背景（2026-09-06 性能与稳定性迭代）：
  - 预取脚本原来靠 `weekday()` 猜"周六/周日"分流批次，法定节假日
    （春节/国庆连休、调休补班）完全无法覆盖，长假的闲置配额白白浪费；
  - 回测引擎在日历获取失败时降级为"周一到周五"，会把休市日与调休日
    误判为交易日，污染回测日期轴；
  - 全工程大量朴素 `datetime.now()`，部署时区非 Asia/Shanghai 时日期口径漂移。

设计：
  - 优先读本地缓存 data/cache/trade_calendar.json（{years: {...}, updated: ...}）；
  - 缓存缺失/过期才联网（akshare tool_trade_date_hist_sina），拉取后写盘；
  - 联网失败返回 None 表示"未知"，**绝不回退 weekday 猜测**（由调用方决定
    保守行为），避免把休市日当交易日。

对外接口（新增模块，不改动任何既有接口的返回结构）：
    is_trading_day(d) -> bool | None      None=未知
    is_trading_day_strict(d) -> bool      未知时抛 ValueError
    last_trading_day(d) -> date | None
    next_trading_day(d) -> date | None
    trading_days(start, end) -> list[str] | None
    beijing_now() -> datetime             统一时区入口
"""
from __future__ import annotations

import json
import logging
import os
import threading
from datetime import datetime, date, timedelta, timezone

logger = logging.getLogger(__name__)

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_CACHE_PATH = os.path.join(_ROOT, 'data', 'cache', 'trade_calendar.json')
_LOCK = threading.Lock()

# 缓存覆盖年份范围（跨年后自动扩展）
_MIN_YEAR = 2024
_CALENDAR_TTL_DAYS = 30  # 日历更新频率：节假日前会公布，30 天足够

_memory = {'years': {}, 'updated': None}


def beijing_now() -> datetime:
    """统一时区入口：返回 Asia/Shanghai 当前时间（naive，方便与既有代码比较）。

    全工程应以此替代朴素 datetime.now()，避免部署时区非北京时日期口径漂移。
    """
    return datetime.now(timezone(timedelta(hours=8))).replace(tzinfo=None)


def _ensure_loaded(force_refresh: bool = False) -> bool:
    """加载日历到内存。返回 True=内存中有可用日历数据。"""
    with _LOCK:
        if _memory['years'] and not force_refresh:
            return True
        # 1) 本地缓存
        try:
            if os.path.exists(_CACHE_PATH):
                with open(_CACHE_PATH, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                years = data.get('years') or {}
                updated = data.get('updated')
                if years:
                    _memory['years'] = years
                    _memory['updated'] = updated
                    stale = True
                    if updated:
                        try:
                            age = (datetime.now() - datetime.strptime(updated, '%Y-%m-%d')).days
                            stale = age > _CALENDAR_TTL_DAYS
                        except Exception:
                            stale = True
                    if not stale and not force_refresh:
                        return True
        except Exception as e:
            logger.warning(f"交易日历缓存读取失败: {str(e)[:60]}")

        # 2) 联网刷新（akshare）；失败则保留旧缓存（可能过期但可用）
        try:
            import akshare as ak
            df = ak.tool_trade_date_hist_sina()
            years = {}
            for v in df['trade_date']:
                s = str(v)
                if len(s) >= 8:
                    years.setdefault(s[:4], set()).add(s[:10])
            _memory['years'] = {y: sorted(days) for y, days in years.items()}
            _memory['updated'] = datetime.now().strftime('%Y-%m-%d')
            try:
                os.makedirs(os.path.dirname(_CACHE_PATH), exist_ok=True)
                tmp = _CACHE_PATH + '.tmp'
                with open(tmp, 'w', encoding='utf-8') as f:
                    json.dump({'updated': _memory['updated'],
                               'years': {y: days for y, days in _memory['years'].items()}}, f)
                os.replace(tmp, _CACHE_PATH)
            except Exception as e:
                logger.warning(f"交易日历缓存写入失败: {str(e)[:60]}")
            return True
        except Exception as e:
            logger.warning(f"交易日历联网获取失败: {str(e)[:80]}")
            return bool(_memory['years'])


def _to_date(d) -> date:
    if d is None:
        return beijing_now().date()
    if isinstance(d, datetime):
        return d.date()
    if isinstance(d, date):
        return d
    return datetime.strptime(str(d)[:10], '%Y-%m-%d').date()


def _all_days() -> set:
    s = set()
    for days in _memory['years'].values():
        s.update(days)
    return s


def trading_days(start, end) -> list:
    """返回 [start, end] 闭区间内的交易日列表（升序），未知时返回 None。"""
    if not _ensure_loaded():
        return None
    sd, ed = _to_date(start), _to_date(end)
    all_days = _all_days()
    out, cur = [], sd
    while cur <= ed:
        ds = cur.strftime('%Y-%m-%d')
        if ds in all_days:
            out.append(ds)
        cur += timedelta(days=1)
    # 区间超出缓存年份范围
    if ed.year > max(int(y) for y in _memory['years']) + 1:
        logger.warning("交易日历缓存未覆盖查询区间末端，结果可能不完整")
    return out


def is_trading_day(d=None):
    """是否为交易日。返回 True/False，日历不可用时返回 None（未知，不猜）。"""
    if not _ensure_loaded():
        return None
    return _to_date(d).strftime('%Y-%m-%d') in _all_days()


def is_trading_day_strict(d=None) -> bool:
    """严格判定：日历不可用时抛异常（用于"绝不能误判"的场景）。"""
    r = is_trading_day(d)
    if r is None:
        raise ValueError("交易日历不可用（缓存缺失且联网失败），拒绝猜测交易日")
    return r


def last_trading_day(d=None):
    """不晚于 d 的最后一个交易日（d 为交易日时返回 d 自身）；未知返回 None。"""
    if not _ensure_loaded():
        return None
    all_days = _all_days()
    cur = _to_date(d)
    for _ in range(30):
        ds = cur.strftime('%Y-%m-%d')
        if ds in all_days:
            return cur
        cur -= timedelta(days=1)
    return None


def next_trading_day(d=None):
    """严格晚于 d 的下一个交易日；未知返回 None。"""
    if not _ensure_loaded():
        return None
    all_days = _all_days()
    cur = _to_date(d) + timedelta(days=1)
    for _ in range(30):
        ds = cur.strftime('%Y-%m-%d')
        if ds in all_days:
            return cur
        cur += timedelta(days=1)
    return None


def refresh() -> bool:
    """强制刷新日历（可挂到月度 cron）。"""
    return _ensure_loaded(force_refresh=True)


if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO)
    today = beijing_now().date()
    print('今天:', today, '是否交易日:', is_trading_day(today))
    print('上一交易日:', last_trading_day(today))
    print('下一交易日:', next_trading_day(today))
