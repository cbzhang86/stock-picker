"""
样本外因子验证器 — 按时间切分训练/测试集，逐因子计算样本外 IC

为什么需要这个模块
------------------
在 2026-09-05 之前，系统只有两种验证，各自都有致命缺陷：

1. `run_kline_factor_backtest`（--mode kfactor）
   - 只覆盖 momentum / technical / volume_price 三个 K 线因子
   - capital_flow 权重 0.35（最大因子）从不进回测 → 系统胜负押在未验证因子上
   - **全样本 IC，无训练/测试切分** → IC 里混着过拟合成分，无法判断泛化

2. 策略级回测 `_calc_factor_performance`
   - 样本是"被推荐出来的股票"（通常每批 1-3 只）
   - 经过 min_score 阈值筛选后因子方差被大幅压缩 → IC 天然趋近 0
   - 用它来判断因子好坏同样不可靠

本模块的做法（业界标准的横截面 IC 检验）
--------------------------------------
- **面板构造**：全市场每只股票每日一个 (因子值, T+1 收益) 样本对
- **因子口径对齐实盘**：momentum 用横截面 RPS 排位（不是原始涨幅）、
  capital_flow 用当日横截面百分位、volume_price 用与 factor_library 相同的
  量比映射函数 —— 避免"回测算 A 口径、实盘用 B 口径"的错位
- **收益口径（重要：与实盘执行存在已知差异，勿混用）**：
  主口径 `open_t1 → close_t1`（尾盘选股 → **T+1 开盘买入** → 当日收盘卖出）。
  ⚠️ 该口径**不等于**实盘执行方式：实盘是**尾盘（14:50）买入**，两者相差一个
  **隔夜跳空（close_t0 → open_t1，实测均值 −0.21%/笔，t=−1.14 不显著但方向为负）**；
  即回测数字对应的是"次日开盘买入"策略，对"尾盘买入"实盘存在系统性偏差
  （2026-09-18 全项目审查 P1-1 更正，原文误称"与实盘成交一致"）。
  同时输出 `close_t0 → open_t1` 隔夜口径做对照。
- **时间切分**：按日期排序切 train/test，只用训练集挑因子、在测试集上报 IC
- **每日横截面 IC**：先在每个交易日内部算 Spearman 相关，得到 IC 时间序列，
  再对序列求均值/标准差 → ICIR、t 值。这比把全部日期池化算一个相关系数
  严谨得多（池化会被跨日收益方差污染）
- **Walk-forward**：滚动多折，每折都是"训练在前、测试在后"，报告 OOS IC 均值

输出用途
--------
- 判断每个因子是真有效、噪声还是反向
- 为权重校准（scripts/calibrate_weights.py）提供可信的 IC 输入

用法：
    validator = OOSValidator()
    panel = validator.build_panel('2026-06-30', '2026-09-03')
    result = validator.evaluate(panel)
    print(validator.report(result))
"""

import logging
import os
import sqlite3
from typing import Dict, List, Optional, Sequence

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)

# 全部可验证因子（K 线类 + 快照类）
K_FACTORS = ['momentum', 'technical', 'volume_price',
             # P3-2（2026-09-17）：跳日动量研究因子 —— 仅验证，权重恒 0。
             # 动机：momentum 是 OOS 最强信号（hold1d test t=-2.20）但方向反向；
             # 外部实测（EP007，5/14 模型）动量符号对"是否跳过最近 1~3 日"极敏感。
             # A 股 T+1 + 次日反转强，skip=1/3 可能让符号翻正 → 成为第二条正 alpha 腿。
             # 若验证为正，才能安全降低 hot_theme 的集中度（当前 0.50 是因子池
             # 只有唯一正 alpha 腿的"症状"）。
             'skip_momentum_1', 'skip_momentum_3',
             # P3-3（2026-09-17）：反转因子 —— momentum 取负。
             # 依据：2.2 年全市场面板 momentum IC -0.0298 (t=-2.11)，三折符号稳定，
             # 与华泰 A 股短线反转研究一致（沪深300 1 个月反转 IC 27.69%）。
             # 不是"赌它翻正"，而是**把已证实的反向信号显式翻转为正向因子**。
             # 华泰研报口径："反转因子"= 负动量，是 A 股长期 IC 最强的经典因子之一。
             'reversal_20d']
# P1-I（2026-09-05 审查报告）：新因子 valuation_fundamental / event_catalyst
# 此前不在验证清单 → "链路已通但永远拿不到 OOS 证据 → 永远不能给权重"死锁。
SNAPSHOT_FACTORS = ['capital_flow', 'hot_theme', 'dragon_tiger',
                    'valuation_fundamental', 'event_catalyst']
ALL_FACTORS = K_FACTORS + SNAPSHOT_FACTORS

# 实盘成交成本（与 config.yml backtest 段一致）
# P0-C 关联修复：补齐印花税（仅卖出）+ 过户费（双向），此前 OOS 收益口径
# 只扣佣金+滑点，与回测引擎不一致（2026-09-05 起两处统一）。
COMMISSION = 0.0003
SLIPPAGE = 0.001
STAMP_DUTY = 0.0005     # 印花税，仅卖出收取
TRANSFER_FEE = 0.00001  # 过户费，买卖双向

# 收益口径列
RET_HOLD1D = 'ret_hold1d'       # close_t0 → close_t1（尾盘买入、持有 1 天，默认口径）
RET_HOLD3D = 'ret_hold3d'       # close_t0 → close_t3（IC 衰减曲线用，2026-09-05）
RET_HOLD5D = 'ret_hold5d'       # close_t0 → close_t5（同上）
RET_INTRADAY = 'ret_intraday'   # open_t1 → close_t1（次日开盘买 → 收盘卖）
RET_OVERNIGHT = 'ret_overnight'  # close_t0 → open_t1（隔夜跳空，旧 kfactor 口径）

RET_LABELS = {
    RET_HOLD1D: '尾盘买入→T+1收盘卖（持有完整1天，扣滑点佣金）',
    RET_HOLD3D: '尾盘买入→T+3收盘卖（IC衰减分析用）',
    RET_HOLD5D: '尾盘买入→T+5收盘卖（IC衰减分析用）',
    RET_INTRADAY: 'T+1开盘买→T+1收盘卖（只吃日内，扣滑点佣金）',
    RET_OVERNIGHT: 'T日收盘→T+1开盘（只吃隔夜跳空，旧kfactor口径）',
}


class OOSValidator:
    """样本外因子验证器"""

    def __init__(self, project_root: str = None):
        if project_root is None:
            project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        self.project_root = project_root
        self.kline_db = os.path.join(project_root, 'data', 'cache', 'kline_cache.db')
        self.factor_db = os.path.join(project_root, 'data', 'cache', 'factor_daily.db')

    # ── 面板构造 ─────────────────────────────────────────────

    def build_panel(self, start_date: str, end_date: str,
                    factors: Sequence[str] = None,
                    max_codes: int = None) -> pd.DataFrame:
        """
        构造因子面板：每行 = (date, code, 各因子值, T+1 收益)

        参数：
          start_date / end_date: 区间
          factors: 要计算的因子，默认全部
          max_codes: 限制股票数（调试用）

        返回：
          DataFrame，列 = [date, code, close, <factors...>, ret_intraday, ret_overnight]
          某因子无数据的行为 NaN（不填充，避免把"无数据"伪装成中性值）
        """
        if factors is None:
            factors = ALL_FACTORS

        df = self._load_kline(start_date, end_date, max_codes)
        if df.empty:
            logger.warning("K线数据为空，无法构造面板")
            return pd.DataFrame()

        df = self._compute_forward_returns(df)
        df = self._compute_kline_factors(df, factors)
        df = self._attach_snapshot_factors(df, start_date, end_date, factors)

        # 只保留至少有一个未来收益的行
        # 注意：lookback 窗口（用于计算 20 日动量/均线）的行必须在这里裁掉，
        # 否则面板会混入 start_date 之前的数据，训练/测试切分日期全错。
        panel = df[(df['date'] >= start_date) & (df['date'] <= end_date)].copy()
        panel = panel[panel[RET_HOLD1D].notna()
                      | panel[RET_INTRADAY].notna()
                      | panel[RET_OVERNIGHT].notna()].copy()
        logger.info(
            f"因子面板构造完成: {len(panel)} 行 | "
            f"{panel['date'].nunique()} 个交易日 | {panel['code'].nunique()} 只股票"
        )
        return panel

    def _load_kline(self, start_date: str, end_date: str,
                    max_codes: int = None) -> pd.DataFrame:
        """
        加载 K 线。向前多取 60 个自然日，保证区间首日就有 20 日窗口可用；
        向后多取 10 天，保证区间末日也能算 T+1 收益。
        """
        if not os.path.exists(self.kline_db):
            logger.error(f"K线缓存不存在: {self.kline_db}")
            return pd.DataFrame()

        lookback = (pd.Timestamp(start_date) - pd.Timedelta(days=90)).strftime('%Y-%m-%d')
        lookfwd = (pd.Timestamp(end_date) + pd.Timedelta(days=15)).strftime('%Y-%m-%d')

        conn = sqlite3.connect(self.kline_db)
        try:
            df = pd.read_sql_query(
                "SELECT code, date, open, high, low, close, volume, amount "
                "FROM kline_cache WHERE date>=? AND date<=? ORDER BY code, date",
                conn, params=(lookback, lookfwd)
            )
        finally:
            conn.close()

        if df.empty:
            return df

        for c in ['open', 'high', 'low', 'close', 'volume', 'amount']:
            df[c] = pd.to_numeric(df[c], errors='coerce')
        df = df.dropna(subset=['close', 'open'])
        df = df.sort_values(['code', 'date']).reset_index(drop=True)

        # T11（2026-09-17）：排除指数代码（39 开头，如 399001 深证成指 / 399300 沪深300）。
        # 指数"成交额"= 点位 × 成交量，量级达 1e15，会严重污染流动性/滑点分档与因子 IC。
        # prefetch_kline_fullmarket.py 已从源头排除 39 前缀，此处为二次保险，
        # 确保 OOS 验证 universe 永不含指数行（仅剔 39 前缀，不误伤正常股票）。
        if not df.empty and 'code' in df.columns:
            try:
                mask = ~df['code'].astype(str).str.startswith('39')
                df = df[mask].reset_index(drop=True)
            except Exception:
                pass

        if max_codes:
            keep = sorted(df['code'].unique())[:max_codes]
            df = df[df['code'].isin(keep)]

        return df

    @staticmethod
    def _compute_forward_returns(df: pd.DataFrame) -> pd.DataFrame:
        """
        T+1 收益（三种口径）

        为什么必须同时看三种：这是本模块最关键的发现（2026-09-05）。
        同一个因子在不同口径下 IC 符号都可能翻转 —— 只用一种口径下结论，
        等于把"成交假设"这个错误来源隐藏起来。

        ① ret_hold1d（默认）：T 日尾盘买入 → T+1 收盘卖出，持有完整一天。
          这才是"尾盘选股策略"的原意：14:50 选出来，尾盘就该买。
        ② ret_intraday：T+1 开盘买 → T+1 收盘卖，只吃日内。
          backtest_engine._calculate_results 用的就是这个口径 —— 它隐含假设
          "选出来后等到次日开盘才买"，与尾盘策略的初衷是矛盾的。
        ③ ret_overnight：T 日收盘 → T+1 开盘，只吃隔夜跳空。
          旧 kfactor 回测用的口径，与实盘持仓收益同样不是一回事。

        三者关系（毛收益近似）：hold1d ≈ overnight + intraday
        """
        g = df.groupby('code', sort=False)
        open_t1 = g['open'].shift(-1)
        close_t1 = g['close'].shift(-1)

        # 成本口径（P0-C 修复后，与 backtest_engine 完全一致）：
        # 买入端 = 佣金 + 过户费；卖出端 = 佣金 + 过户费 + 印花税
        buy_fee = 1 + COMMISSION + TRANSFER_FEE
        sell_fee = 1 - COMMISSION - TRANSFER_FEE - STAMP_DUTY

        # ① 持有完整一天：尾盘以收盘价买入 → T+1 收盘卖出
        buy_c = df['close'] * (1 + SLIPPAGE) * buy_fee
        sell_c = close_t1 * (1 - SLIPPAGE) * sell_fee
        df[RET_HOLD1D] = (sell_c / buy_c - 1) * 100

        # ② 只吃日内：T+1 开盘买 → T+1 收盘卖
        buy_o = open_t1 * (1 + SLIPPAGE) * buy_fee
        df[RET_INTRADAY] = (sell_c / buy_o - 1) * 100

        # ③ 只吃隔夜：T 日收盘 → T+1 开盘（不扣成本，裸跳空）
        df[RET_OVERNIGHT] = (open_t1 / df['close'] - 1) * 100

        # ④⑤ 多日持有（2026-09-05 架构对标 #4：IC 衰减曲线——决定持仓期设计）
        for n, col in ((3, RET_HOLD3D), (5, RET_HOLD5D)):
            close_tn = g['close'].shift(-n)
            sell_n = close_tn * (1 - SLIPPAGE) * sell_fee
            df[col] = (sell_n / (df['close'] * (1 + SLIPPAGE) * buy_fee) - 1) * 100
            df.loc[close_tn <= 0, col] = np.nan

        # 剔除次日无成交的行（一字板/停牌）
        df.loc[open_t1 <= 0, [RET_INTRADAY, RET_OVERNIGHT]] = np.nan
        df.loc[close_t1 <= 0, RET_HOLD1D] = np.nan
        return df

    def _compute_kline_factors(self, df: pd.DataFrame,
                               factors: Sequence[str]) -> pd.DataFrame:
        """向量化计算 K 线类因子（不逐股循环）"""
        g = df.groupby('code', sort=False)

        # ── momentum：20 日涨幅 → 当日横截面百分位（对齐实盘 rps_20）──
        if 'momentum' in factors:
            ret20 = (df['close'] / g['close'].shift(20) - 1) * 100
            df['_ret20'] = ret20
            # 每日横截面百分位（0-100），与实盘 rank_stocks 的 RPS 语义一致
            df['momentum'] = ret20.groupby(df['date']).rank(pct=True) * 100

        # ── skip momentum：跳日动量（P3-2 研究因子）──
        # 与 momentum 同窗（20 日），但跳过最近 skip 日：
        #   skip=1 → close[t-1] / close[t-21] - 1（不含当日，规避次日反转污染）
        #   skip=3 → close[t-3] / close[t-23] - 1
        # 同样做每日横截面百分位（0-100），与 momentum 语义对齐、可直接比较。
        for _skip in (1, 3):
            _name = f'skip_momentum_{_skip}'
            if _name in factors:
                _ret_sk = (g['close'].shift(_skip)
                           / g['close'].shift(_skip + 20) - 1) * 100
                df[_name] = _ret_sk.groupby(df['date']).rank(pct=True) * 100

        # ── reversal_20d：20 日反转因子（P3-3）──
        # 定义：momentum 百分位的反转 = 100 - RPS 百分位。
        # 依据：momentum 在 2.2 年全市场上 IC -0.0298 (t=-2.11)，三折符号稳定；
        # 华泰研报确认 A 股短线呈反转而非动量。**把已证实的反向信号显式翻转**
        # ——不是赌它翻正，而是按 A 股市场特征命名方向。
        if 'reversal_20d' in factors and 'momentum' in factors:
            df['reversal_20d'] = 100.0 - df['momentum']

        # ── volume_price：量比（当日量 / 5 日均量）→ 与实盘同一映射函数 ──
        if 'volume_price' in factors:
            vol_ma5 = self._grp_rolling(df['volume'], df['code'], 5)
            vol_ratio = df['volume'] / vol_ma5.replace(0, np.nan)
            df['_vol_ratio'] = vol_ratio
            df['volume_price'] = self._volume_ratio_score_vec(vol_ratio)

        # ── technical：对齐 TechnicalScorer 6 维的向量化近似 ──
        if 'technical' in factors:
            df['technical'] = self._technical_score_vec(df, g)

        return df

    @staticmethod
    def _volume_ratio_score_vec(vol_ratio: pd.Series) -> pd.Series:
        """
        量比 → 0-100 分。与 factor_library.calc_volume_ratio_score 完全同构
        （分段线性：0.3→25, 0.8→65, 1.0→75, 1.4→85, 3.0→65, 5.0→40, 15→15）
        """
        r = vol_ratio
        score = pd.Series(np.nan, index=r.index, dtype=float)

        score[r < 0.3] = 25.0
        m = (r >= 0.3) & (r < 0.8)
        score[m] = 25 + (r[m] - 0.3) / 0.5 * 40
        m = (r >= 0.8) & (r < 1.0)
        score[m] = 65 + (r[m] - 0.8) / 0.2 * 10
        m = (r >= 1.0) & (r <= 1.4)
        score[m] = 75 + (r[m] - 1.0) / 0.4 * 10
        m = (r > 1.4) & (r <= 3.0)
        score[m] = 85 - (r[m] - 1.4) / 1.6 * 20
        m = (r > 3.0) & (r <= 5.0)
        score[m] = 65 - (r[m] - 3.0) / 2.0 * 25
        m = r > 5.0
        score[m] = np.maximum(15, 40 - (r[m] - 5.0) / 10.0 * 25)

        return score.clip(0, 100)

    @staticmethod
    def _grp_rolling(s: pd.Series, by: pd.Series, window: int) -> pd.Series:
        """
        分组滚动均值（C 实现）

        不要用 `groupby().transform(lambda x: x.rolling(n).mean())`：
        那会为每组触发一次 Python 回调，在 4500+ 只股票上慢到分钟级。
        原生 `groupby().rolling().mean()` 是 C 实现，快两个数量级。
        """
        return (s.groupby(by).rolling(window, min_periods=1).mean()
                 .reset_index(level=0, drop=True))

    @staticmethod
    def _grp_ewm(s: pd.Series, by: pd.Series, span: int) -> pd.Series:
        """分组指数加权均值（C 实现，理由同 _grp_rolling）"""
        return (s.groupby(by).ewm(span=span, adjust=False).mean()
                 .reset_index(level=0, drop=True))

    @staticmethod
    def _technical_score_vec(df: pd.DataFrame, g) -> pd.Series:
        """
        技术形态评分（0-100）— 对齐 core/technical_scorer.py 的六维结构

        维度与权重：
          趋势形态 30 / 乖离率 20 / 量能 15 / 支撑 10 / MACD 15 / RSI 10

        这里用全向量化实现（TechnicalScorer 是逐股循环，全市场逐日跑会极慢）。
        各分段阈值与 TechnicalScorer 保持一致，绝对值可能与逐股版有几分偏差，
        但横截面排序高度相关，用于 IC 检验足够。
        """
        code = df['code']
        close = df['close']
        ma5 = OOSValidator._grp_rolling(close, code, 5)
        ma10 = OOSValidator._grp_rolling(close, code, 10)
        ma20 = OOSValidator._grp_rolling(close, code, 20)
        ma60 = OOSValidator._grp_rolling(close, code, 60)
        vma5 = OOSValidator._grp_rolling(df['volume'], code, 5)

        # ① 趋势形态 30 分：均线多空排列 + MA20 斜率
        trend = pd.Series(15.0, index=df.index)  # 默认中性
        bull = (ma5 > ma10) & (ma10 > ma20)
        strong_bull = bull & (close > ma60)
        bear = (ma5 < ma10) & (ma10 < ma20)
        strong_bear = bear & (close < ma60)
        trend[strong_bull] = 30.0
        trend[bull & ~strong_bull] = 25.0
        trend[strong_bear] = 2.0
        trend[bear & ~strong_bear] = 6.0
        trend[(close > ma20) & ~bull & ~bear] = 19.0
        trend[(close <= ma20) & ~bull & ~bear] = 11.0

        # ② 乖离率 20 分：偏离 MA5 越极端越扣分（均值回归视角）
        bias = (close - ma5) / ma5.replace(0, np.nan) * 100
        bias_score = pd.Series(20.0, index=df.index)
        bias_score[bias.abs() > 12] = 6.0
        bias_score[(bias.abs() > 8) & (bias.abs() <= 12)] = 11.0
        bias_score[(bias.abs() > 5) & (bias.abs() <= 8)] = 15.0
        bias_score[(bias > 0) & (bias <= 3)] = 20.0
        bias_score[(bias < -3) & (bias >= -5)] = 14.0

        # ③ 量能 15 分：放量上涨最优，放量下跌最差
        vr = df['volume'] / vma5.replace(0, np.nan)
        chg = close / g['close'].shift(1) - 1
        vol_score = pd.Series(9.0, index=df.index)
        vol_score[(vr > 1.2) & (chg > 0)] = 15.0
        vol_score[(vr > 1.2) & (chg <= 0)] = 4.0
        vol_score[(vr < 0.8) & (chg < 0)] = 11.0   # 缩量回调
        vol_score[(vr < 0.8) & (chg >= 0)] = 7.0

        # ④ 支撑 10 分：站上 MA20 加分
        sup_score = pd.Series(4.0, index=df.index)
        sup_score[close > ma20] = 8.0
        sup_score[(close > ma20) & (close > ma60)] = 10.0
        sup_score[close < ma5] = 2.0

        # ⑤ MACD 15 分
        dif = (OOSValidator._grp_ewm(close, code, 12)
               - OOSValidator._grp_ewm(close, code, 26))
        dea = OOSValidator._grp_ewm(dif, code, 9)
        macd_score = pd.Series(7.5, index=df.index)
        macd_score[dif > dea] = 11.0
        macd_score[dif > 0] = 13.0
        macd_score[(dif > dea) & (dif > 0)] = 15.0
        macd_score[dif < dea] = 5.0
        macd_score[(dif < dea) & (dif < 0)] = 2.0

        # ⑥ RSI 10 分：50-65 最健康，超买超卖都扣分
        prev_close = g['close'].shift(1)
        delta = close - prev_close
        gain = delta.clip(lower=0)
        loss = (-delta).clip(lower=0)
        avg_gain = OOSValidator._grp_rolling(gain, code, 14)
        avg_loss = OOSValidator._grp_rolling(loss, code, 14)
        rs = avg_gain / avg_loss.replace(0, np.nan)
        rsi = 100 - 100 / (1 + rs)
        rsi_score = pd.Series(5.0, index=df.index)
        rsi_score[(rsi >= 50) & (rsi < 70)] = 10.0
        rsi_score[(rsi >= 40) & (rsi < 50)] = 7.0
        rsi_score[(rsi >= 30) & (rsi < 40)] = 5.0
        rsi_score[rsi >= 80] = 3.0
        rsi_score[rsi < 20] = 4.0   # 超卖有反弹可能，略高于超买

        total = trend + bias_score + vol_score + sup_score + macd_score + rsi_score
        return total.clip(0, 100)

    def _attach_snapshot_factors(self, df: pd.DataFrame, start_date: str,
                                  end_date: str, factors: Sequence[str]) -> pd.DataFrame:
        """
        挂接历史因子快照（capital_flow / hot_theme / dragon_tiger）

        重要口径说明：快照数据只覆盖当日进入详评的候选股（约 200 只），
        不是全市场。因此这些因子的 IC 是在"候选池内部"的有效性 —— 这恰好
        与实盘用法一致（实盘也只在候选池内做横截面排名），不是缺陷。

        填充口径（2026-09-05 审查 P2-8 澄清，此前文档与代码矛盾）：
        - capital_flow：未覆盖行保持 NaN，不进该因子的 IC（无数据≠中净流入）
        - hot_theme / dragon_tiger：未覆盖行 fillna(50) —— 这是有意为之，
          与实盘口径一致（实盘非热点/无龙虎榜记录的票就是拿基线 50 分，
          见 factor_library 的 hot_theme=70/50 两档、dragon_tiger 基线 50）。
          若不填充，IC 会只反映"有数据票"的排序而高估区分度。
        """
        if not any(f in factors for f in SNAPSHOT_FACTORS):
            return df
        if not os.path.exists(self.factor_db):
            logger.warning(f"因子仓库不存在: {self.factor_db}")
            return df

        conn = sqlite3.connect(self.factor_db)
        try:
            if 'capital_flow' in factors:
                cf = pd.read_sql_query(
                    "SELECT date, code, accumulated_net FROM capital_flow "
                    "WHERE date>=? AND date<=?", conn, params=(start_date, end_date))
                if not cf.empty:
                    # 实盘口径：当日横截面百分位 × 100（rank_stocks._capital_flow_percentile）
                    cf['capital_flow'] = cf.groupby('date')['accumulated_net'].rank(pct=True) * 100
                    df = df.merge(cf[['date', 'code', 'capital_flow']],
                                  on=['date', 'code'], how='left')

            if 'hot_theme' in factors:
                hot = pd.read_sql_query(
                    "SELECT date, code FROM hot_stocks WHERE date>=? AND date<=?",
                    conn, params=(start_date, end_date))
                if not hot.empty:
                    # 实盘口径：is_hot → +20（无 blocks/concepts 时的基线）
                    # ⚠️ 2026-09-18 全项目审查 P2-3（已知口径差）：实盘
                    # `factor_library.calc_hot_theme_score` 还会叠加"板块涨幅 +15 /
                    # 板块龙头 +5"（可达 85-90 分），而 OOS 面板**无板块快照**，
                    # 只能用 70/50 二值近似 → **OOS 会低估 hot_theme 的区分度**，
                    # 进而可能低估其 IC 与应有权重。待 blocks/concepts 历史快照
                    # 积累后应改用与实盘同口径复算（见 docs 全项目审查报告 P2-3）。
                    hot['hot_theme'] = 70.0
                    df = df.merge(hot[['date', 'code', 'hot_theme']],
                                  on=['date', 'code'], how='left')
                    df['hot_theme'] = df['hot_theme'].fillna(50.0)
                else:
                    df['hot_theme'] = 50.0

            if 'dragon_tiger' in factors:
                try:
                    dt = pd.read_sql_query(
                        "SELECT date, code, institution_net_wan, has_record "
                        "FROM dragon_tiger WHERE date>=? AND date<=?",
                        conn, params=(start_date, end_date))
                except pd.errors.DatabaseError:
                    dt = pd.DataFrame()
                if not dt.empty:
                    # 实盘口径（2026-08-14 修正后）：只认机构真金白银方向
                    base = pd.Series(50.0, index=dt.index)
                    base[dt['institution_net_wan'] > 0] = 75.0
                    base[dt['institution_net_wan'] > 5000] = 100.0
                    dt['dragon_tiger'] = base
                    df = df.merge(dt[['date', 'code', 'dragon_tiger']],
                                  on=['date', 'code'], how='left')
                    df['dragon_tiger'] = df['dragon_tiger'].fillna(50.0)

            # ── P1-I（2026-09-05 审查报告）：新因子 OOS 验证路径 ──
            # valuation_fundamental：点时基本面（merge_asof 取 update_date<=当日
            # 的最新快照），无历史数据的行为补 50（与实盘"无估值数据→中性"一致）。
            if 'valuation_fundamental' in factors:
                fund_conn = None
                try:
                    from core.fundamental_provider import FundamentalProvider
                    fp = FundamentalProvider()
                    fund_conn = sqlite3.connect(fp.db_path)
                    hist = pd.read_sql_query(
                        "SELECT code, update_date, pe, pb, roe, div_yield, market_cap, "
                        "revenue_growth, profit_growth FROM fundamentals_history",
                        fund_conn)
                    if not hist.empty:
                        hist['code'] = hist['code'].astype(str).str.zfill(6)
                        hist['update_date'] = pd.to_datetime(hist['update_date'])
                        hist = hist.sort_values('update_date')
                        px = (df[['date', 'code']].drop_duplicates()
                              .assign(_d=lambda x: pd.to_datetime(x['date']))
                              .sort_values('_d'))
                        merged = pd.merge_asof(
                            px, hist, left_on='_d', right_on='update_date',
                            by='code', direction='backward')
                        merged['valuation_fundamental'] = merged.apply(
                            lambda r: FundamentalProvider.score({
                                'pe': r.get('pe'), 'pb': r.get('pb'),
                                'roe': r.get('roe'), 'div_yield': r.get('div_yield'),
                                'market_cap': r.get('market_cap'),
                                'revenue_growth': r.get('revenue_growth'),
                                'profit_growth': r.get('profit_growth')}), axis=1)
                        df = df.merge(merged[['date', 'code', 'valuation_fundamental']],
                                      on=['date', 'code'], how='left')
                        df['valuation_fundamental'] = df['valuation_fundamental'].fillna(50.0)
                        logger.info(
                            f"OOS 估值因子: 点时快照 {len(hist)} 条 "
                            f"({hist['code'].nunique()} 只)")
                    else:
                        logger.warning("fundamentals_history 为空 → "
                                       "valuation_fundamental 恒中性50（待数据积累后重跑）")
                        df['valuation_fundamental'] = 50.0
                except Exception as e:
                    logger.warning(f"valuation_fundamental 挂接失败（该因子本轮跳过）: {e}")
                finally:
                    if fund_conn:
                        fund_conn.close()

            # event_catalyst：事件衰减分。取每个 (date, code) 前 10 天（decay 窗口）
            # 内的事件 → EventProvider.score(reference_date=当日)（无前视：只看过去）。
            if 'event_catalyst' in factors:
                ev_conn = None
                try:
                    from core.event_provider import EventProvider
                    from datetime import timedelta
                    ep = EventProvider()
                    ev_conn = sqlite3.connect(ep.db_path)
                    lo = (pd.Timestamp(start_date) - timedelta(days=10)).strftime('%Y-%m-%d')
                    ev = pd.read_sql_query(
                        "SELECT code, date, event_type, title, impact FROM events "
                        "WHERE date>=? AND date<=?", ev_conn,
                        params=(lo, end_date))
                    if not ev.empty:
                        ev['code'] = ev['code'].astype(str).str.zfill(6)
                        events_by_code = {}
                        for r in ev.itertuples(index=False):
                            events_by_code.setdefault(r.code, []).append(
                                {'date': r.date, 'event_type': r.event_type,
                                 'title': r.title, 'impact': r.impact})
                        dts = pd.to_datetime(df['date'])
                        scores = []
                        for d, code in zip(dts, df['code']):
                            elist = events_by_code.get(code, [])
                            hi_s = d.strftime('%Y-%m-%d')
                            lo_s = (d - timedelta(days=10)).strftime('%Y-%m-%d')
                            recent = [e for e in elist if lo_s <= e['date'] <= hi_s]
                            scores.append(EventProvider.score(recent, reference_date=hi_s))
                        df['event_catalyst'] = scores
                    else:
                        logger.warning("events 表区间内无数据 → event_catalyst 恒中性50")
                        df['event_catalyst'] = 50.0
                except Exception as e:
                    logger.warning(f"event_catalyst 挂接失败（该因子本轮跳过）: {e}")
                finally:
                    if ev_conn:
                        ev_conn.close()
        finally:
            conn.close()

        return df

    # ── IC 计算 ──────────────────────────────────────────────

    @staticmethod
    def daily_ic(df: pd.DataFrame, factor: str,
                 ret_col: str = RET_INTRADAY) -> pd.Series:
        """
        逐日横截面 IC（Spearman rank 相关）

        返回：index=date, value=当日 IC 的 Series（样本不足的交易日为 NaN）

        实现：先在每个交易日内对因子和收益做秩变换，再算 Pearson ——
        数学上等价于 Spearman，但向量化后比逐日调 spearmanr 快一个量级。
        """
        sub = df[[factor, ret_col, 'date']].dropna()
        # 单日样本 < 5 只无法给出有意义的秩相关
        cnt = sub.groupby('date')[factor].transform('size')
        sub = sub[cnt >= 5]
        if sub.empty:
            return pd.Series(dtype=float)

        rk_f = sub.groupby('date')[factor].rank()
        rk_r = sub.groupby('date')[ret_col].rank()
        tmp = pd.DataFrame({'date': sub['date'], 'f': rk_f, 'r': rk_r})
        return tmp.groupby('date').apply(
            lambda g: g['f'].corr(g['r']) if len(g) >= 5 else np.nan
        ).dropna()

    def _ic_stats(self, ic_series: pd.Series) -> Dict:
        """IC 时间序列 → 均值 / 标准差 / ICIR / t 值 / 正比例"""
        ic = ic_series.dropna()
        n = len(ic)
        if n == 0:
            return {'ic': None, 'ic_std': None, 'icir': None,
                    't_stat': None, 'pos_ratio': None, 'n_days': 0}
        mean = float(ic.mean())
        std = float(ic.std(ddof=1)) if n > 1 else 0.0
        icir = mean / std if std > 0 else None
        t_stat = mean / (std / np.sqrt(n)) if std > 0 else None
        return {
            'ic': round(mean, 4),
            'ic_std': round(std, 4),
            'icir': round(icir, 4) if icir is not None else None,
            't_stat': round(t_stat, 3) if t_stat is not None else None,
            'pos_ratio': round(float((ic > 0).mean()), 3),
            'n_days': n,
        }

    # ── 因子诊断（2026-09-05 架构对标 #4）─────────────────────

    def factor_corr(self, panel: pd.DataFrame, factors: Sequence[str] = None,
                    min_stocks: int = 15) -> pd.DataFrame:
        """
        因子相关性矩阵：每日横截面 Spearman 相关的时序均值。

        用途：权重 50%+30% 集中在两个"热点资金"类因子时，若二者相关性高，
        有效分散远低于表面（评估报告 R4）。
        """
        if factors is None:
            factors = [f for f in ALL_FACTORS if f in panel.columns]
        factors = [f for f in factors if f in panel.columns]
        if len(factors) < 2:
            return pd.DataFrame()
        sub = panel[['date'] + factors].dropna()
        if sub.empty:
            return pd.DataFrame()
        cnt = sub.groupby('date')[factors[0]].transform('size')
        sub = sub[cnt >= min_stocks]
        if sub.empty:
            return pd.DataFrame()
        # 注意：groupby(date)[factors].rank() 的结果不含 date 列，
        # 用对齐索引的日期 Series 做分组键（修复 KeyError）
        dates = sub['date']
        ranked = sub[factors].groupby(dates).rank()
        corr_sum = np.zeros((len(factors), len(factors)))
        corr_cnt = np.zeros((len(factors), len(factors)))
        n_days = 0
        for _, g in ranked.groupby(dates):
            if len(g) < min_stocks:
                continue
            n_days += 1
            # 某些日某因子可能退化为常数（如当日面板内无热点命中 → hot_theme 全 50）
            # → corr=NaN。按"因子对"分别累计有效日，避免 NaN 污染整张矩阵。
            c = g.corr().values
            mask = ~np.isnan(c)
            corr_sum[mask] += c[mask]
            corr_cnt[mask] += 1
        if n_days == 0 or (corr_cnt == 0).all():
            return pd.DataFrame()
        with np.errstate(invalid='ignore', divide='ignore'):
            mat = corr_sum / np.where(corr_cnt == 0, np.nan, corr_cnt)
        return pd.DataFrame(mat, index=factors, columns=factors).round(3)

    def ic_decay(self, panel: pd.DataFrame, factors: Sequence[str] = None,
                 horizons: Sequence[int] = (1, 3, 5)) -> Dict:
        """
        IC 衰减曲线：同一因子在 T+1/T+3/T+5 持有口径下的逐日 IC 对比。

        用途：衰减快 → 短持仓快换手；衰减慢 → 可持有更久（影响卖出规则设计）。
        返回 {因子: {'T+1': ic, 'T+3': ic, 'T+5': ic}}
        """
        ret_cols = {1: RET_HOLD1D, 3: RET_HOLD3D, 5: RET_HOLD5D}
        if factors is None:
            factors = [f for f in ALL_FACTORS if f in panel.columns]
        out = {}
        for f in factors:
            if f not in panel.columns:
                continue
            row = {}
            for n in horizons:
                rc = ret_cols.get(int(n))
                if rc is None or rc not in panel.columns:
                    continue
                st = self._ic_stats(self.daily_ic(panel, f, rc))
                row[f'T+{n}'] = st.get('ic')
            out[f] = row
        return out

    def tail_spread(self, panel: pd.DataFrame, factor: str,
                    ret_col: str = RET_HOLD1D, k: int = 3,
                    n_quantile: float = None) -> Dict:
        """
        尾差价差（top-k vs bottom-k 前视收益差）—— 衡量因子"可交易性"的口径。

        为什么需要（P2-6）：daily_ic 是**全截面**回归视角（因子排序与收益排序
        的整体相关），但本系统**只买评分 top-3**。外部实证（低波动因子 rank IC
        最高却多空尾差价差为负）表明 **IC 高 ≠ 可交易**。此口径取每个交易日
        因子降序排列的 top-k 与 bottom-k，算两组平均前视收益之差，直接对应
        "我们实际下单的头尾"能否拉开价差。

        与 daily_ic / ic_decay 复用同一套按日分组 + 数据校验模式（单日样本
        < 2k 只跳过，避免无意义分组），不另起口径。

        参数：
          k：取 top/bottom 的股票数（默认 3，对应实际推荐数）；同时固定输出
             k=5 便于对照。n_quantile 非 None 时改用"前/后 n_quantile 分位"
             选股（如 0.1 → 头尾各 10%），覆盖两种 tail 定义。
        返回：{'k3': stats, 'k5': stats}，stats 含
          mean_spread / std / t_stat / n_days / positive_ratio /
          top_mean / bottom_mean（与 _ic_stats 口径对齐）。
        """
        ks = [k] if isinstance(k, int) else list(k)
        for extra in (3, 5):  # 实际推荐数 3~5，固定对照
            if extra not in ks:
                ks.append(extra)
        ks = sorted(set(ks))
        return {f'k{kk}': self._tail_spread_single(panel, factor, ret_col, kk,
                                                    n_quantile)
                for kk in ks}

    def _tail_spread_single(self, panel: pd.DataFrame, factor: str,
                            ret_col: str, k: int,
                            n_quantile: float = None) -> Dict:
        """单 k 的尾差价差统计（tail_spread 的内部实现）。"""
        sub = panel[[factor, ret_col, 'date']].dropna()
        # 单日样本不足 2k 只无法取头尾，跳过（与 daily_ic 的 >=5 校验同源）
        cnt = sub.groupby('date')[factor].transform('size')
        sub = sub[cnt >= 2 * k]
        if sub.empty:
            return {'mean_spread': 0.0, 'std': 0.0, 't_stat': None,
                    'n_days': 0, 'positive_ratio': None,
                    'top_mean': None, 'bottom_mean': None}

        spreads, top_means, bot_means = [], [], []
        for _, g in sub.groupby('date'):
            n = len(g)
            if n < 2 * k:
                continue
            if n_quantile is not None:
                kk = max(1, int(round(n_quantile * n)))
            else:
                kk = k
            top = g.nlargest(kk, factor)[ret_col]
            bot = g.nsmallest(kk, factor)[ret_col]
            if len(top) < 1 or len(bot) < 1:
                continue
            spreads.append(float(top.mean() - bot.mean()))
            top_means.append(float(top.mean()))
            bot_means.append(float(bot.mean()))

        sp = pd.Series(spreads)
        n = len(sp)
        if n == 0:
            return {'mean_spread': 0.0, 'std': 0.0, 't_stat': None,
                    'n_days': 0, 'positive_ratio': None,
                    'top_mean': None, 'bottom_mean': None}
        mean = float(sp.mean())
        std = float(sp.std(ddof=1)) if n > 1 else 0.0
        t_stat = mean / (std / np.sqrt(n)) if std > 0 else None
        return {
            'mean_spread': round(mean, 4),
            'std': round(std, 4),
            't_stat': round(t_stat, 3) if t_stat is not None else None,
            'n_days': n,
            'positive_ratio': round(float((sp > 0).mean()), 3),
            'top_mean': round(float(np.mean(top_means)), 4),
            'bottom_mean': round(float(np.mean(bot_means)), 4),
        }

    def neutralized_ic(self, panel: pd.DataFrame, factor: str,
                       ret_col: str = RET_HOLD1D, n_buckets: int = 5,
                       min_stocks: int = 20) -> Dict:
        """
        流动性中性化 IC：先在 (date, 成交额分位) 桶内对因子和收益去均值
        （剔除流动性共同暴露），再对残差算逐日横截面 Spearman。

        用途：检验因子 IC 是否只是"热点票偏小盘/偏活跃"的代理
        （评估报告 2.2 的中性化检验缺口）。与原始 IC 对比：
        大幅下降 → 因子信息主要来自流动性暴露；基本不变 → 独立信息成立。
        """
        if factor not in panel.columns or 'amount' not in panel.columns:
            return {'ic': None, 'n_days': 0}
        sub = panel[[factor, ret_col, 'date', 'amount']].dropna()
        if sub.empty:
            return {'ic': None, 'n_days': 0}
        sub = sub.copy()
        sub['_bucket'] = sub.groupby('date')['amount'].transform(
            lambda x: pd.qcut(x.rank(method='first'), n_buckets, labels=False))
        gmean = sub.groupby(['date', '_bucket'])[[factor, ret_col]].transform('mean')
        sub['_rf'] = sub[factor] - gmean[factor]
        sub['_rr'] = sub[ret_col] - gmean[ret_col]
        cnt = sub.groupby('date')['_rf'].transform('size')
        sub = sub[cnt >= min_stocks]
        if sub.empty:
            return {'ic': None, 'n_days': 0}
        # 关键：残差 rank 必须在【桶内】做——若跨桶 rank，桶均值微差会把
        # 二分信号（hot=70/non=50 去均值后）稀释掉，得到虚假的"中性化后 IC≈0"。
        # 桶内 rank 再合并，与"同桶 hot vs non 对照"同构，结果才可比。
        rk_f = sub.groupby(['date', '_bucket'])['_rf'].rank()
        rk_r = sub.groupby(['date', '_bucket'])['_rr'].rank()
        tmp = pd.DataFrame({'date': sub['date'].values, 'f': rk_f.values, 'r': rk_r.values})
        ic = tmp.groupby('date').apply(
            lambda g: g['f'].corr(g['r']) if len(g) >= min_stocks else np.nan
        ).dropna()
        return self._ic_stats(ic)

    # 默认 purge 天数：来自最长因子回看窗口。
    # _compute_kline_factors 中实际用到的最大回看：
    #   - momentum：20 日 RPS（ret20 = close / close.shift(20)，L234）
    #   - volume_price：5 日均量（L241）
    #   - technical：MA60 为最长（ma60 = groupby.rolling(60)，L312）；RSI14、
    #     MACD ewm(12/26/9) 需 ~35 日，均 ≤ 60
    # 取最大值 60 交易日作为 purge 下限，避免训练段末尾样本的因子窗口
    # （最多 60 日）跨入测试段、或其 T+1~T+5 标签落入测试段造成标签重叠
    # （P2-2：系统性低估过拟合风险）。
    DEFAULT_PURGE_DAYS = 60

    # 训练段必须保留的最少交易日数。purge 不得把训练段清空。
    # 2026-09-17 修复：此前 `train_end = cut - purge_days` 无可行性检查，
    # 当窗口较短时（实测 48 日窗口 cut=33 < purge 60）train_end 变负 →
    # 训练段被清空 → report() 取 train_range[0] 抛 IndexError（OOS 路径整条崩溃）；
    # 即使不崩溃，"标签重叠保护"也会在无人知晓的情况下失效。
    MIN_TRAIN_DAYS = 10

    @staticmethod
    def _resolve_purge(n_days: int, cut: int, purge_days: int):
        """把请求的 purge_days 收敛到当前数据能支撑的值。

        返回 (purge_effective, note)。note 为 None 表示按请求原值执行；
        否则说明被收敛的原因与后果（供报告显式披露，避免静默失效）。
        """
        max_purge = max(0, cut - OOSValidator.MIN_TRAIN_DAYS)
        p = max(0, int(purge_days))
        if p <= max_purge:
            return p, None
        cost = '已关闭（窗口过短，标签重叠风险未消除）' if max_purge == 0 else '力度不足'
        note = (f"purge 被收敛：请求 {p} 日 > 可用上限 {max_purge} 日"
                f"（窗口 {n_days} 日、训练段 {cut} 日、需保留 ≥{OOSValidator.MIN_TRAIN_DAYS} 日）"
                f" → 实际 purge {max_purge} 日，标签重叠保护{cost}")
        return max_purge, note

    @staticmethod
    def split_by_time(panel: pd.DataFrame, train_ratio: float = 0.7,
                      purge_days: int = None, embargo_days: int = None):
        """按日期排序切分（不做随机切分 —— 时序数据随机切分等于前视泄漏）。

        purge_days：从训练段末尾剔除的交易日数。这些样本的因子窗口（最长 60
            日）跨入测试段、或其 T+1~T+5 标签落入测试段，造成标签重叠 →
            系统性低估过拟合风险。默认取 DEFAULT_PURGE_DAYS（由最长因子窗口
            推导）；显式传 0 可关闭（退化为旧 70/30 纯切分，用于对比）。
        embargo_days：从测试段开头额外跳过的交易日数（可选），与训练段尾部
            仍可能相关的样本进一步隔离。

        返回：(train_df, test_df, train_dates, test_dates)，dates 为已排序列表。
        """
        dates = sorted(panel['date'].unique())
        n = len(dates)
        cut = int(n * train_ratio)
        cut = max(1, min(cut, n - 1))

        # purge 默认开启（取最长因子窗口）；显式 0 关闭（向后兼容对比）
        if purge_days is None:
            purge_days = OOSValidator.DEFAULT_PURGE_DAYS
        purge_days = int(purge_days)
        embargo_days = int(embargo_days) if embargo_days else 0

        # 可行化：purge 不得把训练段清空（2026-09-17 修复，见 MIN_TRAIN_DAYS 注释）
        purge_days, _purge_note = OOSValidator._resolve_purge(n, cut, purge_days)
        if _purge_note:
            logger.warning(_purge_note)

        # 训练段末尾剔除 purge_days 个交易日（这些样本的标签会跨入测试段）
        train_end = cut - purge_days
        train_dates = set(dates[:train_end]) if train_end > 0 else set()
        # 测试段开头跳过 embargo_days（可选）
        test_start = cut + embargo_days
        test_dates = set(dates[test_start:]) if test_start < n else set()

        return (panel[panel['date'].isin(train_dates)].copy(),
                panel[panel['date'].isin(test_dates)].copy(),
                sorted(train_dates), sorted(test_dates))

    # ── 主入口 ──────────────────────────────────────────────

    def evaluate(self, panel: pd.DataFrame, factors: Sequence[str] = None,
                 train_ratio: float = 0.7,
                 ret_col: str = RET_HOLD1D,
                 purge_days: int = None,
                 embargo_days: int = None) -> Dict:
        """
        训练/测试切分下的逐因子 IC 评估

        返回结构：
          {
            'factors': {因子名: {
                'full': {...}, 'train': {...}, 'test': {...},
                'oos_ic': 测试集 IC（= 样本外）,
                'direction_consistent': 训练/测试 IC 符号是否一致,
                'coverage': 该因子在面板中的非缺失占比,
                'verdict': 判定文字
            }},
            'split': {'train_dates': [...], 'test_dates': [...]},
            'meta': {...}
          }
        """
        if factors is None:
            factors = [f for f in ALL_FACTORS if f in panel.columns]

        train, test, train_dates, test_dates = self.split_by_time(
            panel, train_ratio, purge_days=purge_days, embargo_days=embargo_days)

        out = {'factors': {}, 'split': {}, 'meta': {}}
        for f in factors:
            if f not in panel.columns:
                continue
            ic_full = self.daily_ic(panel, f, ret_col)
            ic_train = self.daily_ic(train, f, ret_col)
            ic_test = self.daily_ic(test, f, ret_col)

            full, tr, te = (self._ic_stats(ic_full), self._ic_stats(ic_train),
                            self._ic_stats(ic_test))
            coverage = float(panel[f].notna().mean())

            tr_ic, te_ic = tr['ic'], te['ic']
            if tr_ic is None or te_ic is None:
                consistent = None
            else:
                consistent = bool((tr_ic > 0) == (te_ic > 0))

            out['factors'][f] = {
                'full': full, 'train': tr, 'test': te,
                'oos_ic': te['ic'],
                'icir': te['icir'],
                'direction_consistent': consistent,
                'coverage': round(coverage, 4),
                'verdict': self._judge(te, consistent, coverage),
                # T-A（2026-09-07 P1 论证后实施）：输出逐日 IC 序列（对齐
                # 测试窗口），供校准脚本构建 IC 协方差矩阵做最大化 ICIR
                # 加权。增量字段，旧消费方不受影响。
                'daily_ics': {str(k): round(float(v), 6)
                              for k, v in ic_test.dropna().items()},
            }

        out['split'] = {
            'train_range': [train_dates[0], train_dates[-1]] if train_dates else [],
            'test_range': [test_dates[0], test_dates[-1]] if test_dates else [],
            'n_train_days': len(train_dates), 'n_test_days': len(test_dates),
            'train_ratio': train_ratio,
        }
        # purge 实际生效情况（2026-09-17）：必须随报告落盘，否则"标签重叠保护
        # 因窗口过短而未生效"这件事无人知晓 —— 属静默失效，比崩溃更危险。
        _all_dates = sorted(panel['date'].unique())
        _n_all = len(_all_dates)
        _cut = max(1, min(int(_n_all * train_ratio), _n_all - 1)) if _n_all else 0
        _req_purge = int(OOSValidator.DEFAULT_PURGE_DAYS if purge_days is None
                         else purge_days)
        _eff_purge, _purge_note = OOSValidator._resolve_purge(_n_all, _cut, _req_purge)
        out['split'].update({
            'purge_days_requested': _req_purge,
            'purge_days_effective': _eff_purge,
            'purge_note': _purge_note,
            'embargo_days': int(embargo_days) if embargo_days else 0,
        })
        out['meta'] = {
            'n_rows': len(panel), 'n_codes': int(panel['code'].nunique()),
            'n_days': int(panel['date'].nunique()), 'ret_col': ret_col,
        }
        return out

    def walk_forward(self, panel: pd.DataFrame, factors: Sequence[str] = None,
                     n_splits: int = 3, train_ratio: float = 0.7,
                     ret_col: str = RET_HOLD1D) -> Dict:
        """
        滚动前进验证（walk-forward）

        把日期序列切成 n_splits 个连续块，第 k 折用前 k 块训练、第 k+1 块测试。
        每折的"测试"都在训练之后，严格无前视。
        返回每折的 OOS IC 及其均值 —— 均值比单次切分稳健得多。
        """
        if factors is None:
            factors = [f for f in ALL_FACTORS if f in panel.columns]

        dates = sorted(panel['date'].unique())
        if len(dates) < n_splits + 1:
            return {'folds': [], 'factors': {}, 'note': '交易日数量不足以做 walk-forward'}

        # 扩展式切分：fold k 的测试窗口是第 k 个块
        fold_size = len(dates) // (n_splits + 1)
        folds = []
        for k in range(1, n_splits + 1):
            test_start = fold_size * k
            test_end = fold_size * (k + 1) if k < n_splits else len(dates)
            test_d = dates[test_start:test_end]
            train_d = dates[:test_start]
            if not test_d or not train_d:
                continue
            folds.append({'fold': k, 'train': train_d, 'test': test_d})

        per_factor = {}
        for f in factors:
            if f not in panel.columns:
                continue
            fold_ics = []
            for fd in folds:
                tr = panel[panel['date'].isin(set(fd['train']))]
                te = panel[panel['date'].isin(set(fd['test']))]
                ic = self.daily_ic(te, f, ret_col)
                if len(ic) == 0:
                    continue
                fold_ics.append(round(float(ic.mean()), 4))

            if fold_ics:
                arr = np.array(fold_ics)
                per_factor[f] = {
                    'fold_ics': fold_ics,
                    'mean_oos_ic': round(float(arr.mean()), 4),
                    'std_oos_ic': round(float(arr.std(ddof=1)), 4) if len(arr) > 1 else None,
                    'stable': bool((arr > 0).all() or (arr < 0).all()) if len(arr) > 1 else None,
                }
            else:
                per_factor[f] = {'fold_ics': [], 'mean_oos_ic': None,
                                 'std_oos_ic': None, 'stable': None}

        return {
            'folds': [{'fold': f['fold'],
                       'train': [f['train'][0], f['train'][-1]],
                       'test': [f['test'][0], f['test'][-1]]} for f in folds],
            'factors': per_factor,
        }

    @staticmethod
    def _judge(test_stats: Dict, consistent, coverage: float) -> str:
        """
        因子有效性判定（以【样本外】IC 为唯一依据）

        阈值参考业界惯例：|IC| >= 0.03 视为有效，ICIR >= 0.3 视为稳定。
        训练/测试方向不一致 → 极可能是噪声，标为不稳定。
        """
        ic = test_stats.get('ic')
        icir = test_stats.get('icir')
        n_days = test_stats.get('n_days', 0)

        if ic is None or n_days < 3:
            return '样本不足'
        if coverage < 0.05:
            return '覆盖过低（<5%，结论不可靠）'
        if consistent is False:
            return f'不稳定（训练/测试反向，IC {ic:+.4f}）'
        if ic <= -0.03:
            return f'反向（IC {ic:+.4f}，应降权或取反）'
        if ic >= 0.05:
            return f'强有效（OOS IC {ic:+.4f}）'
        if ic >= 0.03:
            return f'有效（OOS IC {ic:+.4f}）'
        if icir is not None and icir >= 0.3:
            return f'弱但稳定（IC {ic:+.4f}, ICIR {icir:+.2f}）'
        return f'噪声（OOS IC {ic:+.4f}）'

    # ── 报告 ────────────────────────────────────────────────

    def report(self, result: Dict, wf: Dict = None) -> str:
        """生成可读报告（Markdown）"""
        meta = result.get('meta', {})
        split = result.get('split', {})
        lines = []
        lines.append('# 样本外因子 IC 验证报告')
        lines.append('')
        lines.append(f"- 样本：{meta.get('n_rows', 0)} 行 / {meta.get('n_codes', 0)} 只 / "
                     f"{meta.get('n_days', 0)} 个交易日")
        lines.append(f"- 收益口径：**{meta.get('ret_col', '')}** "
                     f"（{RET_LABELS.get(meta.get('ret_col', ''), '未知')}）")
        # 2026-09-17 修复：原写法 `.get('train_range', ['?'])[0]` 在「键存在但值为
        # 空列表」时默认值不生效 → IndexError（purge 清空训练段时实测崩溃）。
        # 改为 `(x or ['?'])` 口径，空列表也能安全渲染。
        _tr = split.get('train_range') or ['?']
        _te = split.get('test_range') or ['?']
        lines.append(f"- 训练区间：{_tr[0]} ~ {_tr[-1]}（{split.get('n_train_days', 0)} 天）")
        lines.append(f"- 测试区间（样本外）：{_te[0]} ~ {_te[-1]}（{split.get('n_test_days', 0)} 天）")
        # purge 披露：保护是否真正生效必须写在报告里，不能静默
        if 'purge_days_effective' in split:
            _pn = split.get('purge_note')
            lines.append(f"- purge（标签重叠保护）：请求 {split.get('purge_days_requested')} 日"
                         f" → 实际 {split.get('purge_days_effective')} 日"
                         + (f"　⚠️ {_pn}" if _pn else "　✅ 按请求执行"))
            if split.get('embargo_days'):
                lines.append(f"- embargo：{split.get('embargo_days')} 日")
        if not _tr or _tr == ['?']:
            lines.append("- ⚠️ **训练段为空**：purge 超过窗口可支撑范围，"
                         "本次训练/测试对照结论不可用，请缩短 purge 或扩大窗口")
        lines.append('')
        lines.append('| 因子 | 覆盖 | 全样本IC | 训练集IC | **测试集IC(样本外)** | ICIR | t值 | 方向一致 | 判定 |')
        lines.append('|---|---|---|---|---|---|---|---|---|')

        for fname, d in sorted(result.get('factors', {}).items(),
                               key=lambda x: (x[1]['test']['ic'] is None,
                                              -(x[1]['test']['ic'] or 0))):
            def fmt(k):
                v = d[k].get('ic')
                return f"{v:+.4f}" if v is not None else '—'
            icir = d.get('icir')
            t = d['test'].get('t_stat')
            cons = d.get('direction_consistent')
            cons_s = '—' if cons is None else ('是' if cons else '**否**')
            lines.append(
                f"| {fname} | {d['coverage']*100:.1f}% | {fmt('full')} | {fmt('train')} | "
                f"**{fmt('test')}** | {icir:+.2f} | {t:+.2f} | {cons_s} | {d['verdict']} |"
                if icir is not None and t is not None else
                f"| {fname} | {d['coverage']*100:.1f}% | {fmt('full')} | {fmt('train')} | "
                f"**{fmt('test')}** | — | — | {cons_s} | {d['verdict']} |"
            )

        if wf and wf.get('factors'):
            lines.append('')
            lines.append('## Walk-forward 滚动验证')
            lines.append('')
            for fd in wf.get('folds', []):
                lines.append(f"- 折 {fd['fold']}：训练 {fd['train'][0]}~{fd['train'][-1]} "
                             f"→ 测试 {fd['test'][0]}~{fd['test'][-1]}")
            lines.append('')
            lines.append('| 因子 | 各折 OOS IC | 均值 | 标准差 | 符号稳定 |')
            lines.append('|---|---|---|---|---|')
            for fname, d in sorted(wf['factors'].items(),
                                   key=lambda x: (x[1]['mean_oos_ic'] is None,
                                                  -(x[1]['mean_oos_ic'] or 0))):
                ics = ', '.join(f"{v:+.4f}" for v in d['fold_ics']) or '—'
                mean = f"{d['mean_oos_ic']:+.4f}" if d['mean_oos_ic'] is not None else '—'
                std = f"{d['std_oos_ic']:.4f}" if d['std_oos_ic'] is not None else '—'
                stable = '—' if d['stable'] is None else ('是' if d['stable'] else '否')
                lines.append(f"| {fname} | {ics} | **{mean}** | {std} | {stable} |")

        lines.append('')
        lines.append('> 判定标准：|IC|>=0.03 有效，>=0.05 强有效，ICIR>=0.3 稳定；'
                     '训练/测试方向不一致判为噪声。')
        return '\n'.join(lines)
