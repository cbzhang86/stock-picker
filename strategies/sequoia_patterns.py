"""
Sequoia-X 策略模式 — 可独立调用的短线信号检测函数

每个函数返回 (score_boost: float, reasoning: str)：
  - score_boost: 0~20 的加分（供选股理由引用）
  - reasoning: 中文描述，为空表示不触发

参考来源：Sequoia-X (sequoia_x/strategy/)
"""

from typing import Tuple, Optional
import numpy as np
import pandas as pd


def check_ma_volume_golden_cross(
    ma5: float, ma10: float, ma20: float,
    volume: float, avg_volume_20: float
) -> Tuple[float, str]:
    """
    均线金叉 + 放量确认

    条件：
      - MA5 > MA20（当日金叉或已金叉）
      - 成交量 > 20日均量 × 1.5

    来源：Sequoia-X ma_volume.py
    """
    if ma5 > ma20 and volume > avg_volume_20 * 1.5:
        return 12.0, "均线金叉+放量确认"
    return 0.0, ""


def check_limit_up_shakeout(
    prev_close: float, prev2_close: float,
    today_open: float, today_close: float, today_low: float,
    today_volume: float, prev_volume: float
) -> Tuple[float, str]:
    """
    涨停洗盘：昨日涨停 → 今日阴线放量但不破支撑

    条件：
      - 昨日收盘 >= 前日收盘 × 1.095（涨停）
      - 今日收盘 < 今日开盘（阴线）
      - 今日成交量 > 昨日 × 2.0
      - 今日最低价 >= 昨日收盘（支撑不破）

    来源：Sequoia-X limit_up_shakeout.py
    """
    limit_up = prev_close >= prev2_close * 1.095
    bearish = today_close < today_open
    volume_surge = today_volume > prev_volume * 2.0
    support = today_low >= prev_close
    if limit_up and bearish and volume_surge and support:
        return 18.0, "涨停洗盘形态：涨停后放量收阴，支撑有效"
    return 0.0, ""


def check_turtle_breakout(
    close: float, high_20_max: float, amount: float,
    today_open: float
) -> Tuple[float, str]:
    """
    海龟交易突破：20日高点突破 + 成交额过亿 + 阳线

    条件：
      - 收盘价 > 20日最高价
      - 成交额 > 1亿
      - 收盘 > 开盘（阳线）

    来源：Sequoia-X turtle_trade.py
    """
    if close > high_20_max and amount > 100_000_000 and close > today_open:
        return 10.0, "海龟突破：突破20日高点，成交额过亿"
    return 0.0, ""


def check_high_tight_flag(
    high_40: float, low_40: float,
    high_10: float, low_10: float,
    volume: float, avg_volume_20: float
) -> Tuple[float, str]:
    """
    高旗形整理：强动量后极度收敛 + 缩量

    条件：
      - 40日最高/最低 > 1.6（涨幅超 60%）
      - 10日最高/最低 < 1.15（振幅小于 15%）
      - 当日成交量 < 20日均量 × 0.6

    来源：Sequoia-X high_tight_flag.py
    """
    momentum = (high_40 / low_40) > 1.6 if low_40 > 0 else False
    consolidation = (high_10 / low_10) < 1.15 if low_10 > 0 else False
    shrink = volume < avg_volume_20 * 0.6
    if momentum and consolidation and shrink:
        return 14.0, "高旗形整理：强动量后极度收敛缩量"
    return 0.0, ""


def check_rps_breakout(
    rps_value: float, close: float, high_120: float
) -> Tuple[float, str]:
    """
    RPS 突破形态：RPS >= 90 且价格接近 120 日高点

    来源：Sequoia-X rps_breakout.py
    """
    if rps_value >= 90 and close >= high_120 * 0.90:
        return 15.0, "RPS突破形态：强度达90分位以上且接近120日高点"
    return 0.0, ""


def check_uptrend_limit_down(
    ma20: float, ma60: float,
    close: float, prev_close: float,
    volume: float, avg_volume_20: float
) -> Tuple[float, str]:
    """
    上升趋势跌停：MA20 > MA60（上升趋势）+ 放量跌停

    来源：Sequoia-X uptrend_limit_down.py
    """
    uptrend = ma20 > ma60
    limit_down = close <= prev_close * 0.905  # 约等于跌停
    volume_surge = volume > avg_volume_20 * 2.0
    if uptrend and limit_down and volume_surge:
        return 8.0, "上升趋势中放量跌停（可能为洗盘）"
    return 0.0, ""


def check_parking_apron(
    close: pd.Series, high: pd.Series, low: pd.Series,
    volume: pd.Series
) -> Tuple[float, str]:
    """
    停机坪形态：15日内出现涨幅>9.5%的放量涨停，随后3天高开窄幅震荡缩量

    来源：stock 仓库 parking_apron 策略
    """
    if len(close) < 20:
        return 0.0, ""

    close_arr = close.values
    vol_arr = volume.values
    high_arr = high.values
    low_arr = low.values

    # 搜索最近15日内的涨停
    for i in range(-15, -3):
        pct = (close_arr[i] - close_arr[i-1]) / close_arr[i-1] * 100
        if pct > 9.5:
            # 找到涨停日，检查之后3天的形态
            vol_ratio = vol_arr[i] / max(np.mean(vol_arr[max(0, i-20):i]), 1)
            if vol_ratio < 1.2:
                continue  # 涨停当天没放量，不算
            # 涨停后3天：高开 + 窄幅震荡 + 缩量
            for j in range(i+1, min(i+4, len(close_arr))):
                if j >= len(close_arr) or i+3 >= len(close_arr):
                    break
            # 检查后3天振幅
            range_3d = max(high_arr[i+1:i+4]) - min(low_arr[i+1:i+4])
            avg_vol_3d = np.mean(vol_arr[i+1:i+4])
            vol_shrink = avg_vol_3d < vol_arr[i] * 0.6
            narrow_range = range_3d / close_arr[i] < 0.08

            if vol_shrink and narrow_range:
                return 15.0, "停机坪形态：涨停后缩量窄幅整理"

    return 0.0, ""


def check_backtrace_ma250(
    close: pd.Series, volume: pd.Series
) -> Tuple[float, str]:
    """
    回踩年线：股价从250日均线以下向上突破，然后回踩但始终在250日均线之上

    来源：stock 仓库 backtrace_ma250 策略
    """
    if len(close) < 260:
        return 0.0, ""

    ma250 = close.rolling(250).mean()
    ma60 = close.rolling(60).mean()

    cur_close = close.iloc[-1]
    cur_ma250 = ma250.iloc[-1] if not ma250.isna().iloc[-1] else 0
    cur_ma60 = ma60.iloc[-1] if not ma60.isna().iloc[-1] else 0

    if cur_ma250 <= 0:
        return 0.0, ""

    # 条件1：当前在250日线之上
    above_ma250 = cur_close > cur_ma250
    # 条件2：60日线在250日线之上（中期趋势走好）
    ma60_above_ma250 = cur_ma60 > cur_ma250
    # 条件3：曾经从下方突破（60日内有在250日线下的时候）
    was_below = any(close.iloc[-60:] < ma250.iloc[-60:])

    if above_ma250 and ma60_above_ma250 and was_below:
        return 12.0, "回踩年线形态：突破250日线后站稳"
    return 0.0, ""


def check_low_atr_growth(
    close: pd.Series, high: pd.Series, low: pd.Series,
    period: int = 10
) -> Tuple[float, str]:
    """
    低 ATR 成长：波动率低 + 区间涨幅 > 10%

    来源：stock 仓库 low_atr 策略
    """
    if len(close) < period + 5:
        return 0.0, ""

    # ATR 计算
    high_low = high - low
    high_close = abs(high.shift(1) - close)
    low_close = abs(low.shift(1) - close)
    tr = pd.concat([high_low, high_close, low_close], axis=1).max(axis=1)
    atr = tr.rolling(period).mean().iloc[-1]

    # 区间涨幅
    range_return = (close.iloc[-1] - close.iloc[-period]) / close.iloc[-period] * 100
    avg_price = close.iloc[-period:].mean()

    if avg_price > 0 and (atr / avg_price * 100) <= 10 and range_return > 10:
        return 10.0, f"低ATR成长：区间涨幅{range_return:.1f}%，波动率低"
    return 0.0, ""
