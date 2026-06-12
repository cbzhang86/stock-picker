"""
系统化技术评分 — 多维度技术面 100 分制评分

评分结构：
  趋势形态    30分  MA5/MA10/MA20 排列 + 角度
  乖离率      20分  价格相对 MA5 的偏离程度
  量能形态    15分  缩量回调/放量上涨/放量下跌
  支撑位置    10分  价格在关键 MA 附近
  MACD 状态   15分  金叉/多头/空头/死叉
  RSI 区间    10分  超卖/中性/超买

总分 0-100 →
  STRONG_BUY (>=75) / BUY (>=60) / HOLD (>=45) / WAIT (>=30) / SELL

参考：daily-stock-analysis StockTrendAnalyzer._generate_signal()
"""

from dataclasses import dataclass, field
from typing import List, Optional
from enum import Enum

import numpy as np
import pandas as pd


class TrendStatus(Enum):
    STRONG_BULL = "强势多头"
    BULL = "多头排列"
    WEAK_BULL = "弱势多头"
    CONSOLIDATION = "盘整"
    BEAR = "空头排列"


class TechnicalSignal(Enum):
    STRONG_BUY = "STRONG_BUY"
    BUY = "BUY"
    HOLD = "HOLD"
    WAIT = "WAIT"
    SELL = "SELL"


@dataclass
class TechnicalScore:
    total: float = 0.0
    trend_score: float = 0.0
    bias_score: float = 0.0
    volume_score: float = 0.0
    support_score: float = 0.0
    macd_score: float = 0.0
    rsi_score: float = 0.0
    trend_status: str = ""
    volume_status: str = ""
    macd_status: str = ""
    signal: str = "WAIT"
    reasons: List[str] = field(default_factory=list)
    risks: List[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            'total': self.total,
            'trend_score': self.trend_score,
            'bias_score': self.bias_score,
            'volume_score': self.volume_score,
            'support_score': self.support_score,
            'macd_score': self.macd_score,
            'rsi_score': self.rsi_score,
            'trend_status': self.trend_status,
            'signal': self.signal,
        }


class TechnicalScorer:
    """系统化技术分析评分器"""

    def score(self, df: pd.DataFrame) -> TechnicalScore:
        """
        对单只股票的 K 线 DataFrame 做技术评分

        参数：
          df: 包含 open, high, low, close, volume 列的 DataFrame
              至少需要 60 行数据

        返回：
          TechnicalScore
        """
        result = TechnicalScore()

        if df is None or len(df) < 20:
            result.total = 50.0
            result.signal = "HOLD"
            return result

        close = df['close'].astype(float)
        volume = df['volume'].astype(float) if 'volume' in df.columns else pd.Series([0] * len(df))
        high = df['high'].astype(float) if 'high' in df.columns else close
        low = df['low'].astype(float) if 'low' in df.columns else close

        # 计算均线
        ma5 = close.rolling(5).mean()
        ma10 = close.rolling(10).mean()
        ma20 = close.rolling(20).mean()

        # 1. 趋势形态 (30分)
        result.trend_score, result.trend_status = self._score_trend(ma5, ma10, ma20, close)

        # 2. 乖离率 (20分)
        result.bias_score = self._score_bias(close, ma5)

        # 3. 量能形态 (15分)
        result.volume_score, result.volume_status = self._score_volume(close, volume, ma5)

        # 4. 支撑位置 (10分)
        result.support_score = self._score_support(close, ma5, ma10)

        # 5. MACD 状态 (15分)
        result.macd_score, result.macd_status = self._score_macd(close)

        # 6. RSI 区间 (10分)
        result.rsi_score = self._score_rsi(close)

        # 汇总
        result.total = min(max(sum([
            result.trend_score, result.bias_score, result.volume_score,
            result.support_score, result.macd_score, result.rsi_score
        ]), 0), 100)

        # 信号判定
        if result.total >= 75 and result.trend_status in ("强势多头", "多头排列"):
            result.signal = "STRONG_BUY"
        elif result.total >= 60:
            result.signal = "BUY"
        elif result.total >= 45:
            result.signal = "HOLD"
        elif result.total >= 30:
            result.signal = "WAIT"
        else:
            result.signal = "SELL"

        return result

    # ---- 各维度评分 ----

    def _score_trend(self, ma5: pd.Series, ma10: pd.Series,
                     ma20: pd.Series, close: pd.Series) -> tuple:
        """趋势形态评分 (30分)"""
        c5 = ma5.iloc[-1] if not ma5.isna().iloc[-1] else 0
        c10 = ma10.iloc[-1] if not ma10.isna().iloc[-1] else 0
        c20 = ma20.iloc[-1] if not ma20.isna().iloc[-1] else 0

        # 均线角度（用过去N日的斜率）
        def slope(s, n=5):
            if len(s) < n or s.isna().iloc[-1]:
                return 0
            return (s.iloc[-1] - s.iloc[-n]) / s.iloc[-n] * 100

        slope5 = slope(ma5)
        slope20 = slope(ma20)

        if c5 > c10 > c20 and slope5 > 0:
            # 强势多头 + 角度向上
            angle_bonus = min(slope5 * 3, 5)
            return min(30 + angle_bonus, 30), "强势多头"
        elif c5 > c10 > c20:
            return 24, "多头排列"
        elif c5 > c10 and c10 > c20 * 0.98:
            return 18, "弱势多头"
        elif c5 < c10 < c20:
            return 8, "空头排列"
        elif c5 < c20 and c10 < c20:
            return 12, "偏空排列"
        else:
            return 15, "盘整"

    def _score_bias(self, close: pd.Series, ma5: pd.Series) -> float:
        """乖离率评分 (20分)"""
        if ma5.isna().iloc[-1] or ma5.iloc[-1] == 0:
            return 10
        bias = (close.iloc[-1] - ma5.iloc[-1]) / ma5.iloc[-1] * 100

        if -0.5 <= bias <= 0.5:
            return 20  # 紧贴MA5，最佳
        elif 0.5 < bias <= 2:
            return 18  # 略偏高
        elif -2 <= bias < -0.5:
            return 16  # 偏下，可能有支撑
        elif 2 < bias <= 5:
            return 12  # 偏高（但趋势强时仍可接受）
        elif -5 <= bias < -2:
            return 10  # 偏弱
        elif bias > 8 or bias < -8:
            return 4   # 严重偏离
        else:
            return 8

    def _score_volume(self, close: pd.Series, volume: pd.Series,
                      ma5: pd.Series) -> tuple:
        """量能形态评分 (15分)"""
        if len(volume) < 10:
            return 7, "数据不足"

        avg_vol_5 = volume.rolling(5).mean()
        avg_vol_20 = volume.rolling(20).mean()

        current_v = volume.iloc[-1]
        avg5 = avg_vol_5.iloc[-1] if not avg_vol_5.isna().iloc[-1] else 1
        avg20 = avg_vol_20.iloc[-1] if not avg_vol_20.isna().iloc[-1] else 1

        # 当日涨跌
        if len(close) >= 2:
            pct_chg = (close.iloc[-1] - close.iloc[-2]) / close.iloc[-2] * 100
        else:
            pct_chg = 0

        v_ratio = current_v / avg20 if avg20 > 0 else 1

        if pct_chg < 0 and v_ratio < 0.8:
            return 14, "缩量回调"       # 健康
        elif pct_chg > 0 and v_ratio > 1.2:
            return 12, "放量上涨"       # 强势
        elif 0.8 <= v_ratio <= 1.2:
            return 9, "量能正常"
        elif pct_chg > 0 and v_ratio < 0.8:
            return 7, "缩量上涨"       # 可能乏力
        elif pct_chg < 0 and v_ratio > 1.5:
            return 3, "放量下跌"       # 危险
        else:
            return 5, "量价背离"

    def _score_support(self, close: pd.Series,
                       ma5: pd.Series, ma10: pd.Series) -> float:
        """支撑位置评分 (10分)"""
        c = close.iloc[-1]
        sup_ma5 = ma5.iloc[-1] if not ma5.isna().iloc[-1] else None
        sup_ma10 = ma10.iloc[-1] if not ma10.isna().iloc[-1] else None

        if sup_ma5 and sup_ma10:
            dist_to_ma5 = abs(c - sup_ma5) / sup_ma5 * 100
            dist_to_ma10 = abs(c - sup_ma10) / sup_ma10 * 100

            if dist_to_ma5 < 0.5:
                return 10  # 紧贴MA5
            elif dist_to_ma10 < 0.5:
                return 9   # 紧贴MA10
            elif dist_to_ma5 < 2:
                return 8   # 在MA5附近
            elif dist_to_ma5 < 5:
                return 6
            elif dist_to_ma5 > 10:
                return 3   # 远离MA
            else:
                return 5
        return 5

    def _score_macd(self, close: pd.Series) -> tuple:
        """MACD 状态评分 (15分)"""
        if len(close) < 35:
            return 7, "数据不足"

        exp1 = close.ewm(span=12, adjust=False).mean()
        exp2 = close.ewm(span=26, adjust=False).mean()
        dif = exp1 - exp2
        dea = dif.ewm(span=9, adjust=False).mean()

        cur_dif = dif.iloc[-1]
        cur_dea = dea.iloc[-1]
        prev_dif = dif.iloc[-2] if len(dif) > 1 else cur_dif
        prev_dea = dea.iloc[-2] if len(dea) > 1 else cur_dea

        # 金叉
        if prev_dif < prev_dea and cur_dif > cur_dea:
            if cur_dif > 0:
                return 15, "零轴上金叉"
            return 13, "金叉"
        elif cur_dif > cur_dea:
            if cur_dif > 0:
                return 12, "多头(零轴上)"
            return 10, "多头"
        elif prev_dif > prev_dea and cur_dif < cur_dea:
            return 4, "死叉"
        else:
            return 6, "空头"

    def _score_rsi(self, close: pd.Series) -> float:
        """RSI 区间评分 (10分)"""
        if len(close) < 15:
            return 5

        delta = close.diff()
        gain = delta.clip(lower=0)
        loss = -delta.clip(upper=0)
        avg_gain = gain.rolling(14).mean().iloc[-1]
        avg_loss = loss.rolling(14).mean().iloc[-1]

        if avg_loss == 0 or avg_loss is None or np.isnan(avg_loss):
            return 8  # 一直在涨

        rs = avg_gain / avg_loss
        rsi = 100 - (100 / (1 + rs))

        if rsi < 25:
            return 9   # 超卖区，可能反弹
        elif rsi < 40:
            return 8   # 偏弱但有空间
        elif rsi < 60:
            return 7   # 中性正常
        elif rsi < 75:
            return 6   # 偏强
        else:
            return 4   # 超买，回调风险
