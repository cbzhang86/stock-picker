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

    # 准备数据
    X = historical_results[factor_columns].fillna(0.5).values
    y = historical_results[target_column].fillna(0).values

    # 裁剪异常收益
    y = np.clip(y, -20, 20)

    # 岭回归
    model = Ridge(alpha=alpha)
    model.fit(X, y)

    coefficients = model.coef_

    # 归一化（负系数→0）
    weights = {}
    total_positive = sum(max(c, 0) for c in coefficients)

    if total_positive > 0:
        for i, factor in enumerate(factor_columns):
            weights[factor] = round(max(coefficients[i], 0) / total_positive, 4)
    else:
        # 全部负相关→均匀分布
        n = len(factor_columns)
        weights = {f: round(1.0 / n, 4) for f in factor_columns}

    return weights


class WeightsOptimizer:
    """权重优化调度器"""

    def __init__(self, weights_dir: str = "data/model_weights",
                 min_records: int = 30):
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
