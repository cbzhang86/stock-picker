"""
权重优化器 — 基于历史推荐结果自动调参

原理：
  1. 收集历史推荐的各因子得分 vs 实际收益
  2. 用 Ridge 回归计算各因子的真实预测能力
  3. 上调命中率高的因子权重，下调命中率低的

参考：scikit-learn Ridge 回归（防过拟合）

触发条件：
  积累 >= N 条推荐记录后自动检查
  当前胜率 < 50% 时强制触发

工作流（2026-06-27 重构）：
  check_and_report() → 产出报告不写入 → 用户审批 → apply_from_report() 写入
  maybe_optimize() 保留原逻辑但不自动写入（降级为只报告）
"""

import json
import os
import logging
from datetime import datetime
from typing import Dict, List, Optional

import numpy as np
import pandas as pd
from sklearn.linear_model import Ridge

logger = logging.getLogger(__name__)


def optimize_weights(historical_results: pd.DataFrame,
                     factor_columns: List[str],
                     target_column: str = 't1_return',
                     alpha: float = 1.0) -> Dict[str, float]:
    """
    基于历史推荐结果，用岭回归优化因子权重

    参数：
      historical_results: 包含因子得分和收益的DataFrame
      factor_columns: 因子列名列表
      target_column: 目标列（'t1_return' / 't5_return'）
      alpha: 正则化强度

    返回：
      {因子名: 新权重}  总和=1

    原理：
      y = w1*x1 + w2*x2 + ... + wn*xn
      Ridge回归的系数即因子对收益的解释力
    """
    if len(historical_results) < 10:
        logger.warning(f"记录不足 {len(historical_results)} < 10，跳过优化")
        return {}

    # 补齐可能缺失的因子列（历史数据可能没有某些字段）
    for col in factor_columns:
        if col not in historical_results.columns:
            historical_results[col] = 0.5

    # ---- 列过滤：剔除常数列，防止坍缩 ----
    valid_columns = []
    skipped = []
    for col in factor_columns:
        uniq = historical_results[col].nunique()
        if uniq >= 3:
            valid_columns.append(col)
        else:
            skipped.append(col)
            logger.info(f"因子 {col} 跳过回归（唯一值={uniq} < 3）")

    if not valid_columns:
        logger.warning("所有因子均为常数列，跳过优化")
        return {}

    # 准备数据（仅有效列）
    X = historical_results[valid_columns].fillna(0.5).values
    y = historical_results[target_column].fillna(0).values

    # 裁剪异常收益
    y = np.clip(y, -20, 20)

    # 岭回归
    model = Ridge(alpha=alpha)
    model.fit(X, y)

    coefficients = model.coef_

    # 构建全量权重：有效列用 Ridge 归一化系数，跳过列给 0
    weights = {f: 0.0 for f in factor_columns}
    total_positive = sum(max(c, 0) for c in coefficients)

    if total_positive > 0:
        for i, factor in enumerate(valid_columns):
            weights[factor] = round(max(coefficients[i], 0) / total_positive, 4)
    else:
        # 全部负相关→有效因子均匀分布
        for i, factor in enumerate(valid_columns):
            weights[factor] = round(1.0 / len(valid_columns), 4)

    # ---- 坍缩保护：任一因子权重 > 0.8 则跳过本轮 ----
    if any(w > 0.8 for w in weights.values()):
        logger.warning(f"权重坍缩检测（权重 > 0.8）：{weights}，跳过本轮优化")
        return {}

    return weights


class WeightsOptimizer:
    """权重优化调度器"""

    def __init__(self, weights_dir: str = "data/weights",
                 min_records: int = 60):
        self.weights_dir = weights_dir
        self.min_records = min_records
        self.cache_dir = "data/cache"
        os.makedirs(weights_dir, exist_ok=True)
        os.makedirs(self.cache_dir, exist_ok=True)

    def maybe_optimize(self, tracker, current_weights: dict,
                       mode: str = 'short') -> Optional[Dict]:
        """
        检查条件并决定是否优化

        返回：
          新权重（如果优化了）或 None（跳过）
        """
        report = self.check_and_report(tracker, current_weights, mode)
        if report and report['proposed_weights']:
            return report['proposed_weights']
        return None

    # ── 新方法：只检查，不写入 ──────────────────────────────────

    def check_and_report(self, tracker, current_weights: dict,
                         mode: str = 'short') -> Optional[Dict]:
        """
        检查条件 → 如果触发则跑 Ridge → 返回报告字典

        不写入任何文件。报告包含：
          - 数据诊断（样本量、胜率、因子覆盖、新鲜度）
          - 当前权重 vs 建议权重
          - Ridge 系数明细
          - 哪些因子被跳过及原因

        返回报告 dict 或 None（条件不满足时）
        """
        accuracy = tracker.calc_accuracy(mode=mode)
        total = accuracy.get('total_records', 0)

        if total < self.min_records:
            logger.info(f"训练样本不足 {total}/{self.min_records}，跳过")
            return None

        # 加载历史数据（以实际返回行数为准）
        results = self._load_history(tracker, mode)
        if results is None or len(results) < self.min_records:
            logger.info(f"实际可用训练数据 {len(results) if results is not None else 0}/{self.min_records}，跳过")
            return None

        current_win_rate = accuracy.get('win_rate_t1', 0)
        avg_return = accuracy.get('avg_return_t1', 0)

        # 触发条件：胜率低 或 自上次优化以来新增 >= 50 条
        last_count = self._load_last_optimize_count(mode)
        since_last = len(results) - last_count
        should_optimize = (current_win_rate < 50) or (since_last >= 50)
        if not should_optimize:
            logger.info(f"条件未触发: 胜率{current_win_rate}% OK, 距上次优化{since_last}条")
            return None

        logger.info(f"触发优化: 胜率 {current_win_rate}%, 总记录 {total}")

        # ---- 数据新鲜度检查 ----
        fresh_ratio = self._calc_fresh_ratio(results)

        # 构建因子列名（从 config weights 取）
        factor_columns = list(current_weights.get(mode, {}).keys())
        if not factor_columns:
            logger.warning("无因子列可优化")
            return None

        old_weights = {k: current_weights.get(mode, {}).get(k, 0)
                       for k in factor_columns}

        # ---- 数据诊断 ----
        diagnostics = self._diagnose_data(results, factor_columns, fresh_ratio)
        diagnostics['total_records'] = total
        diagnostics['date_range'] = (
            f"{results['date'].min() if 'date' in results.columns else '?'}"
            f" ~ {results['date'].max() if 'date' in results.columns else '?'}"
        )
        diagnostics['win_rate'] = current_win_rate
        diagnostics['avg_return_t1'] = avg_return
        diagnostics['fresh_ratio'] = round(fresh_ratio, 3)

        # ---- 跑 Ridge ----
        new_weights = optimize_weights(results, factor_columns)

        report = {
            'generated_at': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
            'mode': mode,
            'triggered': True,
            'summary': (
                f"触发优化: 胜率 {current_win_rate}%, "
                f"总记录 {total}, 新鲜度 {fresh_ratio:.0%}"
            ),
            'data_diagnostics': diagnostics,
            'old_weights': old_weights,
            'proposed_weights': new_weights or {},
            'changed': False,
        }

        if not new_weights:
            report['summary'] += ' → Ridge 未产出有效权重'
            logger.info(report['summary'])
            return report

        # ---- 算 delta ----
        deltas = {}
        changed = False
        for k in factor_columns:
            old = old_weights.get(k, 0)
            new = new_weights.get(k, 0)
            delta = round(new - old, 4)
            deltas[k] = {'old': old, 'new': new, 'delta': delta}
            if abs(delta) > 0.005:
                changed = True

        report['factor_deltas'] = deltas
        report['changed'] = changed

        major_changes = [k for k, v in deltas.items()
                         if abs(v['delta']) >= 0.05]
        report['major_changes'] = major_changes

        # ---- Ridge 细节 ----
        report['ridge_detail'] = self._ridge_detail(
            results, factor_columns, new_weights
        )

        report['summary'] += ' → 已生成权重建议，等待审批'
        logger.info(report['summary'])

        # 记录本次触发时的训练记录数，供下次定期检查使用
        self._save_last_optimize_count(len(results), mode)

        return report

    # ── 新方法：执行写入 ───────────────────────────────────────

    def apply_from_report(self, report: Dict) -> bool:
        """
        根据报告中的建议权重写入版本文件 + v1.json

        参数：
          report: check_and_report 返回的报告

        返回：
          bool 写入是否成功
        """
        if not report or not report.get('proposed_weights'):
            logger.warning("报告为空或无建议权重，跳过写入")
            return False

        mode = report.get('mode', 'short')
        new_weights = report['proposed_weights']

        # 写入版本文件（带时间戳）
        version_tag = f"v{datetime.now().strftime('%Y%m%d_%H%M%S')}_{mode}"
        version_path = os.path.join(self.weights_dir, f"{version_tag}.json")
        with open(version_path, 'w', encoding='utf-8') as f:
            json.dump({mode: new_weights}, f, ensure_ascii=False, indent=2)
        logger.info(f"权重版本已保存: {version_path}")

        # 同步到 v1.json
        v1_path = os.path.join(self.weights_dir, 'v1.json')
        existing = {}
        if os.path.exists(v1_path):
            with open(v1_path) as f:
                existing = json.load(f)
        existing[mode] = new_weights
        with open(v1_path, 'w', encoding='utf-8') as f:
            json.dump(existing, f, ensure_ascii=False, indent=2)
        logger.info(f"权重已同步到 v1.json: {v1_path}")

        return True

    # ── 新鲜度检查 ──────────────────────────────────────────────

    def _calc_fresh_ratio(self, results: pd.DataFrame) -> float:
        """计算新版因子名（hot_theme/dragon_tiger）在训练集中的覆盖率"""
        if results is None or results.empty:
            return 0.0
        # prefer exact column from factor_scores parsing
        col = None
        for candidate in ['hot_theme', 'hot_theme_raw']:
            if candidate in results.columns:
                col = candidate
                break
        if col is None:
            return 0.0
        series = results[col]
        # if the values are all the fillna default (0.5/50) they're not real
        non_default = series[series.notna() & (series != 0.5) & (series != 50.0)]
        return round(len(non_default) / max(len(results), 1), 3)

    # ── 优化触发次数跟踪 ─────────────────────────────────────────

    def _load_last_optimize_count(self, mode: str) -> int:
        """读取上次优化时的训练记录数"""
        path = os.path.join(self.cache_dir, f'.last_optimize_{mode}')
        try:
            with open(path) as f:
                return int(f.read().strip())
        except (FileNotFoundError, ValueError):
            return 0

    def _save_last_optimize_count(self, count: int, mode: str):
        """记录本次优化时的训练记录数"""
        path = os.path.join(self.cache_dir, f'.last_optimize_{mode}')
        try:
            with open(path, 'w') as f:
                f.write(str(count))
        except Exception as e:
            logger.warning(f"记录优化触发计数失败: {e}")

    # ── 诊断辅助 ────────────────────────────────────────────────

    def _diagnose_data(self, results: pd.DataFrame,
                       factor_columns: List[str],
                       fresh_ratio: float) -> Dict:
        """生成因子数据质量诊断"""
        diag = {
            'factor_stats': {},
            'valid_signals': [],
            'low_signal': [],
            'missing': [],
        }
        for col in factor_columns:
            if col not in results.columns:
                diag['missing'].append(col)
                continue
            vals = results[col].dropna()
            uniq = vals.nunique()
            stats = {
                'n_samples': len(vals),
                'unique_values': uniq,
                'min': round(vals.min(), 2) if len(vals) > 0 else 'N/A',
                'max': round(vals.max(), 2) if len(vals) > 0 else 'N/A',
            }
            diag['factor_stats'][col] = stats
            if uniq >= 3:
                diag['valid_signals'].append(col)
            else:
                diag['low_signal'].append(col)

        return diag

    def _ridge_detail(self, results: pd.DataFrame,
                      factor_columns: List[str],
                      new_weights: Dict) -> Dict:
        """跑一次完整的 Ridge 细节，用于报告"""
        detail = {'coefficients': {}, 'r2_score': None}
        try:
            valid = [c for c in factor_columns
                     if c in results.columns
                     and results[c].nunique() >= 3]
            if len(valid) < 2:
                return detail

            X = results[valid].fillna(0.5).values
            y = results['t1_return'].fillna(0).values
            y = np.clip(y, -20, 20)

            model = Ridge(alpha=1.0)
            model.fit(X, y)

            for i, f in enumerate(valid):
                detail['coefficients'][f] = round(model.coef_[i], 6)

            # 简单 R²
            y_pred = model.predict(X)
            ss_res = np.sum((y - y_pred) ** 2)
            ss_tot = np.sum((y - np.mean(y)) ** 2)
            detail['r2_score'] = round(1 - ss_res / ss_tot, 4) if ss_tot > 0 else 0
        except Exception as e:
            detail['error'] = str(e)

        return detail

    # ── 历史数据加载 ──────────────────────────────────────────

    def _load_history(self, tracker, mode: str = 'short') -> Optional[pd.DataFrame]:
        """从追踪器加载带因子得分的推荐记录"""
        import sqlite3

        conn = sqlite3.connect(tracker.db_path)

        query = """
            SELECT p.id, p.date, p.score, p.rating, p.factor_scores,
                   o.t1_return, o.t5_return
            FROM predictions p
            JOIN outcomes o ON p.id = o.prediction_id
            WHERE p.mode = ? AND o.t1_return IS NOT NULL
            ORDER BY p.id
            LIMIT 500
        """

        df = pd.read_sql_query(query, conn, params=(mode,))
        conn.close()

        if df.empty:
            return None

        # 从JSON列解析因子得分（注意：存储用的 json.dumps，必须用 json.loads 解析）
        factor_rows = []
        for _, row in df.iterrows():
            fs = row.get('factor_scores', '{}')
            try:
                factor_dict = json.loads(fs) if isinstance(fs, str) else fs
            except Exception:
                factor_dict = {}
            # 展平：因子值可能是 dict（如 {'raw_score': 85}），提取纯数值
            if factor_dict:
                flat = {}
                for k, v in factor_dict.items():
                    if isinstance(v, dict):
                        flat[k] = v.get('raw_score', v.get('score', 50.0))
                    else:
                        flat[k] = v
                factor_rows.append(flat)
            else:
                factor_rows.append(factor_dict)

        if not factor_rows:
            return None

        factor_df = pd.DataFrame(factor_rows)
        result = pd.concat([df, factor_df], axis=1)

        return result

    def compare_versions(self, version_files: List[str]) -> Dict:
        """对比多个版本的权重"""
        versions = {}
        for vf in version_files:
            path = os.path.join(self.weights_dir, vf)
            if os.path.exists(path):
                with open(path) as f:
                    versions[vf] = json.load(f)
        return versions
