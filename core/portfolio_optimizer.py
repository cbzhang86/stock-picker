"""
组合优化器 — 给推荐结果分配仓位百分比

当前仅实现评分加权分配策略。
可扩展：等权分配、风险平价。

用法：
  optimizer = PortfolioOptimizer()
  recs = optimizer.allocate(recommendations)
  # 每只股票增加 allocation_pct 字段
"""

import logging
from typing import Dict, List

logger = logging.getLogger(__name__)


class PortfolioOptimizer:
    """组合优化器 — 评分加权仓位分配"""

    # 仓位约束
    MAX_ALLOCATION = 0.40   # 单只最大 40%
    MIN_ALLOCATION = 0.10   # 单只最小 10%

    @classmethod
    def allocate(cls, recommendations: List[Dict],
                 strategy: str = 'scoring_weight') -> List[Dict]:
        """
        给推荐列表中的每只股票分配仓位百分比

        参数：
          recommendations: 评分降序排列的推荐列表
          strategy: 分配策略
            - 'scoring_weight': 评分加权（默认）
            - 'equal_weight': 等权分配

        返回：同列表，每条附加 allocation_pct: float 字段
        """
        if not recommendations:
            return recommendations

        if strategy == 'equal_weight':
            return cls._equal_weight(recommendations)

        return cls._scoring_weight(recommendations)

    @classmethod
    def _scoring_weight(cls, recs: List[Dict]) -> List[Dict]:
        """
        评分加权分配

        总分 = sum(score) — 所有评分为 >= 60 的推荐参与
        每只仓位% = score / 总分 × 100
        保底下限：MIN_ALLOCATION
        风控上限：MAX_ALLOCATION
        """
        total_score = sum(r.get('score', 0) for r in recs)

        if total_score <= 0:
            return cls._equal_weight(recs)

        # 初始分配
        raw_alloc = []
        for r in recs:
            pct = r.get('score', 0) / total_score
            raw_alloc.append(pct)

        # 应用上限约束
        capped = [min(p, cls.MAX_ALLOCATION) for p in raw_alloc]

        # 回收超限部分并重新分配（迭代收敛）
        excess = sum(raw_alloc) - sum(capped)
        while excess > 0.001:
            # 将超额按比例分配给未超限的
            uncapped_total = sum(p for p in capped if p < cls.MAX_ALLOCATION)
            if uncapped_total <= 0:
                break
            for i in range(len(capped)):
                if capped[i] < cls.MAX_ALLOCATION:
                    capped[i] += excess * (capped[i] / uncapped_total)
            # 重新截断上限，计算新的超额
            new_capped = [min(p, cls.MAX_ALLOCATION) for p in capped]
            excess = sum(capped) - sum(new_capped)
            capped = new_capped

        # 应用下限约束
        result = []
        for r, pct in zip(recs, capped):
            r['allocation_pct'] = round(
                max(pct, cls.MIN_ALLOCATION) * 100, 1
            )
            result.append(r)

        # 归一化确保总和 = 100%
        total = sum(r['allocation_pct'] for r in result)
        if total > 0:
            for r in result:
                r['allocation_pct'] = round(r['allocation_pct'] / total * 100, 1)

        return result

    @classmethod
    def _equal_weight(cls, recs: List[Dict]) -> List[Dict]:
        """等权分配（保证总和=100%）"""
        n = len(recs)
        base = int(100 / n)
        remainder = 100 - base * n
        for i, r in enumerate(recs):
            r['allocation_pct'] = base + (1 if i < remainder else 0)
        return recs

    @staticmethod
    def format_allocation(recs: List[Dict]) -> str:
        """格式化仓位分配文本（供报告使用）"""
        parts = []
        for r in recs:
            name = r.get('name', r.get('code', ''))
            pct = r.get('allocation_pct', 0)
            score = r.get('score', 0)
            parts.append(f"{name} {pct:.0f}%（评分{score:.0f}）")
        return " | ".join(parts)
