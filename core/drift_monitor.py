"""
绩效漂移监控 — 检测实盘表现相对历史的分布漂移（2026-09-05 审查报告 P3-Q）

问题
----
OOS 校准是某个时点的一次性快照。实盘运行后市场结构会漂移（regime change、
因子拥挤、数据源口径变化），权重会静默过期——没有任何环节会告诉你
"模型已经不再工作在它被校准时的那个分布上"。

监控方法
--------
1. PSI（Population Stability Index，群体稳定性指数）：
   把日度推荐收益分箱，比较"基准窗口"（较早期历史）与"近期窗口"的分布差：
       PSI = Σ (actual_i% − expected_i%) × ln(actual_i% / expected_i%)
   经验阈值：< 0.1 稳定；0.1–0.25 关注；> 0.25 显著漂移（应复核权重）。
2. 滚动均值/胜率漂移：近期窗口日均收益、胜率 vs 基准窗口。

输出
----
check() 返回 dict（verdict ∈ OK / WATCH / ALERT / INSUFFICIENT），
hook 到 scripts/eod_stock_picker.py：结果写 data/reports/drift_latest.json
并打日志告警，**不阻塞选股**（监控是事后视角，不改变当日决策）。

样本口径
--------
与 kill-switch / 连亏降仓一致：predictions.db 中 short 模式、有 T+1 结果
的交易日，日均 t1_return（百分数）。
"""

import json
import logging
import math
import os
import sqlite3
from typing import Dict, List

logger = logging.getLogger(__name__)

# PSI 分箱边界（日均收益，百分数单位；覆盖 A 股推荐组合的典型日收益范围）
PSI_BINS = [-100.0, -2.0, -1.0, -0.5, 0.0, 0.5, 1.0, 2.0, 100.0]
PSI_WATCH = 0.10
PSI_ALERT = 0.25


class DriftMonitor:
    """实盘推荐收益的分布漂移监控"""

    def __init__(self, db_path: str = None, baseline_days: int = 60,
                 recent_days: int = 20):
        self.db_path = db_path or os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            'data', 'db', 'predictions.db')
        # 基准窗口 = 近期窗口之前的 baseline_days 天；近期窗口 = 最近 recent_days 天
        self.baseline_days = baseline_days
        self.recent_days = recent_days

    def _load_daily_returns(self) -> List[Dict]:
        """按日聚合的推荐 t1_return（百分数），时间升序"""
        if not os.path.exists(self.db_path):
            return []
        conn = sqlite3.connect(self.db_path)
        try:
            rows = conn.execute("""
                SELECT p.date, AVG(o.t1_return) AS day_ret,
                       COUNT(*) AS n_rec,
                       SUM(CASE WHEN o.t1_return > 0 THEN 1 ELSE 0 END)*1.0/COUNT(*) AS win_rate
                FROM predictions p JOIN outcomes o ON o.prediction_id = p.id
                WHERE p.mode='short' AND o.t1_return IS NOT NULL
                GROUP BY p.date ORDER BY p.date ASC
            """).fetchall()
            return [{'date': r[0], 'day_ret': r[1], 'n_rec': r[2],
                     'win_rate': r[3]} for r in rows if r[1] is not None]
        finally:
            conn.close()

    @staticmethod
    def _psi(expected: List[float], actual: List[float],
             bins: List[float] = PSI_BINS) -> float:
        """PSI：expected/actual 为日均收益样本（百分数）。
        空箱平滑：计数 +0.5（Jeffreys 先验），避免 ln(0) 爆炸。"""
        if not expected or not actual:
            return 0.0
        edges = bins[1:-1]
        e_counts = [0.0] * (len(bins) - 1)
        a_counts = [0.0] * (len(bins) - 1)
        # （2026-09-06 审查）用 <= 落箱消除边界不对称：原 v < b 会把恰好等于
        # 边界值（如 0.0 日收益）的样本归入右侧箱，左闭右开 → 左闭右闭
        for v in expected:
            for i, b in enumerate(edges):
                if v <= b:
                    e_counts[i] += 1
                    break
            else:
                e_counts[-1] += 1
        for v in actual:
            for i, b in enumerate(edges):
                if v <= b:
                    a_counts[i] += 1
                    break
            else:
                a_counts[-1] += 1
        # Jeffreys 平滑
        e = [c + 0.5 for c in e_counts]
        a = [c + 0.5 for c in a_counts]
        e_total, a_total = sum(e), sum(a)
        psi = 0.0
        for ec, ac in zip(e, a):
            ep, ap = ec / e_total, ac / a_total
            psi += (ap - ep) * math.log(ap / ep)
        return psi

    def check(self) -> Dict:
        """执行漂移检查。样本不足返回 INSUFFICIENT（不告警）。"""
        result = {
            'verdict': 'INSUFFICIENT',
            'psi': None, 'recent_mean': None, 'baseline_mean': None,
            'recent_win_rate': None, 'baseline_win_rate': None,
            'n_recent': 0, 'n_baseline': 0,
            'note': '',
        }
        try:
            daily = self._load_daily_returns()
            if len(daily) < self.recent_days * 2:
                result['note'] = (f"有结果交易日仅 {len(daily)} 天，"
                                  f"不足以做漂移对比（需 ≥{self.recent_days*2}）")
                return result
            recent = daily[-self.recent_days:]
            baseline = daily[max(0, len(daily) - self.recent_days - self.baseline_days):
                             len(daily) - self.recent_days]
            if not baseline:
                result['note'] = '无基准窗口数据'
                return result

            recent_rets = [d['day_ret'] for d in recent]
            base_rets = [d['day_ret'] for d in baseline]
            psi = self._psi(base_rets, recent_rets)

            result.update({
                'psi': round(psi, 4),
                'recent_mean': round(sum(recent_rets) / len(recent_rets), 3),
                'baseline_mean': round(sum(base_rets) / len(base_rets), 3),
                'recent_win_rate': round(sum(d['win_rate'] for d in recent)
                                         / len(recent) * 100, 1),
                'baseline_win_rate': round(sum(d['win_rate'] for d in baseline)
                                           / len(baseline) * 100, 1),
                'n_recent': len(recent),
                'n_baseline': len(baseline),
            })

            mean_shift = result['recent_mean'] - result['baseline_mean']
            if psi > PSI_ALERT or mean_shift < -1.0:
                result['verdict'] = 'ALERT'
                result['note'] = (f"显著漂移：PSI={psi:.3f}（>{PSI_ALERT}）或"
                                  f"近期日均收益较基准下移 {mean_shift:+.2f}%。"
                                  f"建议复核权重有效性并考虑重跑 OOS 校准")
            elif psi > PSI_WATCH:
                result['verdict'] = 'WATCH'
                result['note'] = f"轻度漂移：PSI={psi:.3f}，关注后续走势"
            else:
                result['verdict'] = 'OK'
                result['note'] = f"分布稳定：PSI={psi:.3f}"
        except Exception as e:
            result['note'] = f'漂移检查异常: {str(e)[:80]}'
            logger.warning(f"漂移检查异常: {e}")
        return result

    def save(self, result: Dict) -> str:
        """结果落盘 data/reports/drift_latest.json（供日报/审计引用）"""
        out_dir = os.path.join(os.path.dirname(os.path.dirname(
            os.path.abspath(__file__))), 'data', 'reports')
        os.makedirs(out_dir, exist_ok=True)
        path = os.path.join(out_dir, 'drift_latest.json')
        import datetime as _dt
        result['checked_at'] = _dt.datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        with open(path, 'w', encoding='utf-8') as f:
            json.dump(result, f, ensure_ascii=False, indent=2)
        return path
