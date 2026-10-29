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

2026-09-23 交接单修订（D1~D8）
----------------------------
· **D1 幸存者偏差（已知未解，需新数据线）**：predictions 只在"有正式推荐"
  时落库 → 弱市/门槛清零日不产生样本 → 监控在最需要它的行情里冻结。
  本轮已做：**覆盖率可见化**（`n_trading_days` / `coverage_ratio` 进结果与文案）
  + INSUFFICIENT 三态化（D6），使"监控已冻结"本身可见。
  ⚠️ 结构性解法（按日记录与是否推荐解耦的候选池样本）属新数据线，**待用户批准**。
· **D2**：判定门槛保持 `recent_days*2`，但显式输出实际基准窗口天数并标注
  "窗口未满"（原为静默截短：声明 60+20，实到 40 天时基准只有 20 天）。
· **D3**：PSI 改用**逐笔** t1_return（原为未加权日度均值 → 20 vs 20 个点分 8 箱，
  日均还把同日多只推荐平均掉）；均值/胜率仍按日展示。
· **D4**：分箱改为"中心加密 + 尾部加粗"（原 -2~2 每 0.5 一档，A 股日收益集中，
  实际常用箱仅 3~4 个）。
· **D5**：`mean_shift` 改**相对阈值**（`max(绝对下限, 0.5×|基准均值|)`），
  原绝对 -1.0 在"基准 +3%/近期 +2%"时无感、在"基准 -0.5%/近期 -1.5%"时误报。
· **D6**：`NO_SAMPLE` / `PENDING_BACKFILL` / `INSUFFICIENT` 三态拆分。
· **D7**：结论已接入 `evaluate_all`（ALERT→WARN 不阻断）与简报尾部一行。
· **D8**（设计澄清）：与 `run_context.regime.killswitch` 的分工 ——
  kill-switch 管**尾部风险**（近 20 日累计收益 ≤ -10% → 降仓，是**动作**），
  DriftMonitor 管**分布漂移**（近 20 日 vs 基准的 PSI，是**告警**）。
  两者锚同一窗口但语义不同，不互相压制；简报两行并列展示，由使用者判断。
"""

import json
import logging
import math
import os
import sqlite3
from typing import Dict, List

logger = logging.getLogger(__name__)


def _today_str() -> str:
    """北京日期（契约 H）。"""
    try:
        from core.trading_calendar import beijing_now
        return beijing_now().strftime('%Y-%m-%d')
    except Exception:
        import datetime as _dt
        return _dt.date.today().strftime('%Y-%m-%d')

# PSI 分箱边界（逐笔/日均收益，百分数单位）
# 2026-09-23（D4）：由 [-100,-2,-1,-0.5,0,0.5,1,2,100]（中心 0.5 步长、尾箱极宽）
# 改为"中心加密 + 尾部加粗"——A 股个股 T+1 收益高度集中于 ±2%，
# 原分箱实际只用到 3~4 个箱，PSI 分辨率不足。
PSI_BINS = [-100.0, -1.5, -0.75, -0.25, 0.25, 0.75, 1.5, 100.0]
PSI_WATCH = 0.10
PSI_ALERT = 0.25
# D5：mean_shift 的相对阈值系数与绝对下限（百分数）。
# 下限取 0.5pp：交接单场景核对 ——
#   基准 +3% / 近期 +2%（下移 1.0pp）→ 阈值 max(0.5, 1.5)=1.5 → **不误报**；
#   基准 -0.5% / 近期 -1.5%（下移 1.0pp，亏损三倍）→ 阈值 max(0.5, 0.25)=0.5
#     → **正确 ALERT**（原绝对阈值 -1.0 恰好卡在边界上漏报/误报各半）。
MEAN_SHIFT_REL = 0.5
MEAN_SHIFT_FLOOR = 0.5


class DriftMonitor:
    """实盘推荐收益的分布漂移监控"""

    def __init__(self, db_path: str = None, baseline_days: int = 60,
                 recent_days: int = 20, sample_db_path: str = None,
                 prefer_daily_samples: bool = True):
        self.db_path = db_path or os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            'data', 'db', 'predictions.db')
        # 基准窗口 = 近期窗口之前的 baseline_days 天；近期窗口 = 最近 recent_days 天
        self.baseline_days = baseline_days
        self.recent_days = recent_days
        # 2026-09-23（交接单 D1 结构性解法）：日度样本数据线
        # （factor_daily.db::drift_daily_sample，每个交易日一行、与是否推荐解耦）。
        # 该表样本充足时**优先**使用（覆盖率≈100%，消除幸存者偏差）；
        # 表不存在/样本不足时自动回退到 predictions 口径（零行为回归）。
        self.sample_db_path = sample_db_path or os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            'data', 'cache', 'factor_daily.db')
        self.prefer_daily_samples = prefer_daily_samples

    def _load_daily_returns(self) -> List[Dict]:
        """按日聚合的推荐 t1_return（百分数），时间升序。

        每天附带 `rets`（该日逐笔 t1_return 列表）—— PSI 用逐笔样本（D3），
        均值/胜率用日聚合（保持与 kill-switch 口径一致）。
        """
        if not os.path.exists(self.db_path):
            return []
        conn = sqlite3.connect(self.db_path)
        try:
            rows = conn.execute("""
                SELECT p.date, o.t1_return
                FROM predictions p JOIN outcomes o ON o.prediction_id = p.id
                WHERE p.mode='short' AND o.t1_return IS NOT NULL
                ORDER BY p.date ASC
            """).fetchall()
        finally:
            conn.close()
        by_day: Dict[str, List[float]] = {}
        for d, r in rows:
            if r is None:
                continue
            by_day.setdefault(d, []).append(float(r))
        out = []
        for d in sorted(by_day):
            rets = by_day[d]
            out.append({
                'date': d,
                'day_ret': sum(rets) / len(rets),
                'n_rec': len(rets),
                'win_rate': sum(1 for x in rets if x > 0) / len(rets),
                'rets': rets,
            })
        return out

    def _sample_state(self) -> str:
        """区分 NO_SAMPLE / PENDING_BACKFILL / INSUFFICIENT（D6）。

        原实现把"从未发声""预测已落库但 T+1 未回填""有结果但样本不足"压成同一
        verdict，运维无法判断该"再等等"还是"监控已结构性失效"。
        · 库不存在/读失败 → 'UNKNOWN'（沿用 INSUFFICIENT 语义，不误报为"零推荐"）
        · 库在、short 预测行 = 0 → 'NO_SAMPLE'
        · 有预测行但全部无 T+1 → 'PENDING_BACKFILL'
        · 否则 → 'HAS_RESULTS'
        """
        if not os.path.exists(self.db_path):
            return 'UNKNOWN'
        conn = sqlite3.connect(self.db_path)
        try:
            n_pred = conn.execute(
                "SELECT COUNT(*) FROM predictions WHERE mode='short'").fetchone()[0]
            n_out = conn.execute(
                "SELECT COUNT(*) FROM predictions p JOIN outcomes o "
                "ON o.prediction_id=p.id "
                "WHERE p.mode='short' AND o.t1_return IS NOT NULL").fetchone()[0]
        except Exception:
            return 'UNKNOWN'
        finally:
            conn.close()
        if n_pred == 0:
            return 'NO_SAMPLE'
        if n_out == 0:
            return 'PENDING_BACKFILL'
        return 'HAS_RESULTS'

    def _trading_days_since(self, first_date: str) -> int:
        """建仓以来（首个样本日 → 今天）的交易日数，用于覆盖率分母（D1 可见化）。"""
        try:
            from core.trading_calendar import trading_days
            days = trading_days(first_date, _today_str())
            if days:
                return len(days)
        except Exception:
            pass
        # 回退：自然日 × 5/7 近似
        try:
            from datetime import date as _d
            y, m, dd = (int(x) for x in str(first_date)[:10].split('-'))
            delta = (_d.today() - _d(y, m, dd)).days
            return max(1, int(delta * 5 / 7))
        except Exception:
            return 0

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
        """执行漂移检查。

        样本不足返回 `INSUFFICIENT`；从未产生预测返回 `NO_SAMPLE`；
        预测已落库但 T+1 未回填返回 `PENDING_BACKFILL`（D6 三态）。

        2026-09-23 交接单：D1（覆盖率可见化）/ D2（窗口未满显式标注）/
        D3（PSI 用逐笔）/ D5（相对 mean_shift 阈值）已落地。
        """
        result = {
            'verdict': 'INSUFFICIENT',
            'psi': None, 'recent_mean': None, 'baseline_mean': None,
            'recent_win_rate': None, 'baseline_win_rate': None,
            'n_recent': 0, 'n_baseline': 0,
            'n_recent_trades': 0, 'n_baseline_trades': 0,
            'n_trading_days': 0, 'coverage_ratio': None,
            'window_ok': False, 'window_declared': self.baseline_days + self.recent_days,
            'mean_shift_threshold': None,
            'sample_source': None,
            'note': '',
        }
        try:
            # D1：优先用"与是否推荐解耦"的日度样本线（覆盖率≈100%）；
            # 不足时回退 predictions 口径（保持既有语义，零回归）。
            daily, state = [], self._sample_state()
            if self.prefer_daily_samples:
                try:
                    from feedback.drift_sample import load_daily_samples
                    _s = load_daily_samples(self.sample_db_path)
                    if len(_s) >= self.recent_days * 2:
                        daily, result['sample_source'] = _s, 'drift_daily_sample'
                except Exception as e:
                    logger.debug(f"日度样本线不可用（回退 predictions 口径）: {e}")
            if not daily:
                daily = self._load_daily_returns()
                result['sample_source'] = 'predictions'
            min_needed = self.recent_days * 2
            # D1：覆盖率可见化 —— "监控已冻结"必须能被看见
            if daily:
                n_td = self._trading_days_since(daily[0]['date'])
                result['n_trading_days'] = n_td
                if n_td:
                    result['coverage_ratio'] = round(len(daily) / n_td, 3)
            cov = (f"（样本覆盖 {len(daily)}/{result['n_trading_days']} 个交易日"
                   f"{'，' + format(result['coverage_ratio'], '.0%') if result['coverage_ratio'] is not None else ''}）"
                   if daily and result['n_trading_days'] else '')

            if state == 'NO_SAMPLE' and not daily:
                result['verdict'] = 'NO_SAMPLE'
                result['note'] = ('策略从未产生 short 预测记录（predictions 表为空）'
                                  '—— 非"样本不足"，属结构性无样本')
                return result
            if state == 'PENDING_BACKFILL' and not daily:
                result['verdict'] = 'PENDING_BACKFILL'
                result['note'] = ('已有 short 预测记录但均无 T+1 结果'
                                  '—— 回填任务（03:15/23:05）尚未完成，稍后重试即可')
                return result
            if len(daily) < min_needed:
                result['note'] = (f"有结果交易日仅 {len(daily)} 天，不足以做漂移对比"
                                  f"（需 ≥{min_needed}）{cov}")
                return result
            recent = daily[-self.recent_days:]
            baseline = daily[max(0, len(daily) - self.recent_days - self.baseline_days):
                             len(daily) - self.recent_days]
            if not baseline:
                result['note'] = '无基准窗口数据'
                return result

            # D3：PSI 用逐笔样本（统计功效高于未加权的日度均值）
            recent_rets = [d['day_ret'] for d in recent]
            base_rets = [d['day_ret'] for d in baseline]
            recent_trades = [r for d in recent for r in d.get('rets', [])] or recent_rets
            base_trades = [r for d in baseline for r in d.get('rets', [])] or base_rets
            psi = self._psi(base_trades, recent_trades)

            # D2：声明窗口 = baseline_days + recent_days；未满时显式标注（原为静默截短）
            window_ok = len(baseline) >= self.baseline_days
            mean_shift = (sum(recent_rets) / len(recent_rets)
                          - sum(base_rets) / len(base_rets))
            # D5：相对阈值（含绝对下限），避免"基准非零时无感/误报"
            thresh = max(MEAN_SHIFT_FLOOR,
                         MEAN_SHIFT_REL * abs(sum(base_rets) / len(base_rets)))

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
                'n_recent_trades': len(recent_trades),
                'n_baseline_trades': len(base_trades),
                'window_ok': window_ok,
                'mean_shift_threshold': round(thresh, 3),
            })

            if psi > PSI_ALERT or mean_shift < -thresh:
                result['verdict'] = 'ALERT'
                result['note'] = (f"显著漂移：PSI={psi:.3f}（>{PSI_ALERT}）或"
                                  f"近期日均收益较基准下移 {mean_shift:+.2f}%"
                                  f"（阈值 -{thresh:.2f}%）。"
                                  f"建议复核权重有效性并考虑重跑 OOS 校准")
            elif psi > PSI_WATCH:
                result['verdict'] = 'WATCH'
                result['note'] = f"轻度漂移：PSI={psi:.3f}，关注后续走势"
            else:
                result['verdict'] = 'OK'
                result['note'] = f"分布稳定：PSI={psi:.3f}"
            # D2/D1：窗口与覆盖率披露（不改变 verdict，只让口径可见）
            suffix = []
            if result.get('sample_source') == 'drift_daily_sample':
                suffix.append('样本源=候选池日度线（每交易日 1 条，与是否推荐解耦；'
                              '收益为该池次日收盘口径，非成交口径）')
            else:
                suffix.append('样本源=predictions（仅"有正式推荐"的交易日，存在幸存者偏差）')
            if not window_ok:
                suffix.append(f"⚠️ 基准窗口未满：实际 {len(baseline)} 天 < 声明 "
                              f"{self.baseline_days} 天（PSI 参照系偏短）")
            suffix.append(f"逐笔样本 {len(base_trades)} vs {len(recent_trades)}")
            if cov:
                suffix.append(cov.strip('（）'))
            if suffix:
                result['note'] += ' | ' + ' | '.join(suffix)
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
