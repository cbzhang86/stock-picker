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
        os.makedirs(weights_dir, exist_ok=True)

    def maybe_optimize(self, tracker, current_weights: dict,
                       mode: str = 'short') -> Optional[Dict]:
        """
        检查条件并决定是否优化

        参数：
          tracker: PredictionTracker 实例
          current_weights: 当前权重字典
          mode: 'short' / 'long'

        返回：
          新权重（如果优化了）或 None（跳过）
        """
        accuracy = tracker.calc_accuracy(mode=mode)
        total = accuracy.get('total_records', 0)

        if total < self.min_records:
            logger.info(f"记录不足 {total}/{self.min_records}，跳过优化")
            return None

        # 加载历史数据
        results = self._load_history(tracker, mode)
        if results is None or len(results) < self.min_records:
            return None

        current_win_rate = accuracy.get('win_rate_t1', 0)

        # 触发条件：胜率低 或 每50条定期检查
        should_optimize = (current_win_rate < 50) or (total % 50 == 0)

        if not should_optimize:
            return None

        logger.info(f"触发优化: 当前胜率 {current_win_rate}%, 总记录 {total}")

        factor_columns = list(current_weights.get(mode, {}).keys())
        if not factor_columns:
            logger.warning("无因子列可优化")
            return None

        new_weights = optimize_weights(results, factor_columns)

        if new_weights:
            version_tag = f"v{datetime.now().strftime('%Y%m%d_%H%M%S')}_{mode}"
            path = os.path.join(self.weights_dir, f"{version_tag}.json")
            with open(path, 'w', encoding='utf-8') as f:
                json.dump({mode: new_weights}, f, ensure_ascii=False, indent=2)
            logger.info(f"新权重已保存: {path}")
            return new_weights

        return None

    def _load_history(self, tracker, mode: str = 'short') -> Optional[pd.DataFrame]:
        """从追踪器加载带因子得分的推荐记录"""
        import sqlite3

        conn = sqlite3.connect(tracker.db_path)

        query = """
            SELECT p.score, p.rating, p.factor_scores,
                   o.t1_return, o.t5_return
            FROM predictions p
            JOIN outcomes o ON p.id = o.prediction_id
            WHERE p.mode = ? AND o.t1_return IS NOT NULL
            ORDER BY p.id DESC
            LIMIT 500
        """

        df = pd.read_sql_query(query, conn, params=(mode,))
        conn.close()

        if df.empty:
            return None

        # 从JSON列解析因子得分
        import ast
        factor_rows = []
        for _, row in df.iterrows():
            fs = row.get('factor_scores', '{}')
            try:
                factor_dict = ast.literal_eval(fs) if isinstance(fs, str) else fs
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
