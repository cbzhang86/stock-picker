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
import math
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

        返回：同列表，每条附加
              allocation_pct: float — 建议仓位%（0-100，<= MAX_ALLOCATION*100）
              cash_pct:       float — 组合建议保留的现金%（1 - Σ仓位，可为 0）
              注：仓位合计**不保证**等于 100%（2026-09-16 P1-2：上限优先，
                  不足部分为现金，不再回补归一化）。
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
        风控上限：MAX_ALLOCATION（**上限优先**，不足部分留现金，不做回补归一化）

        Ensemble 兼容（2026-09-05 新增）：
          若 rec 含 'ensemble_score'（由 ExpertEnsemble 生成），按 0.4/0.6 混合
          ensemble_score 与 score 后再做加权 —— 让冲突档股票被显著降仓。
          公式：effective_score = 0.6 * score + 0.4 * ensemble_score * weight_factor
        """
        # 优先使用 ensemble_score（如果存在）作为加权基础
        def effective_score(r):
            base = r.get('score', 0)
            if 'ensemble_score' in r:
                wf = r.get('weight_factor', 1.0)
                return 0.6 * base + 0.4 * r['ensemble_score'] * wf
            return base

        total_score = sum(effective_score(r) for r in recs)

        if total_score <= 0:
            return cls._equal_weight(recs)

        # 初始分配
        raw_alloc = []
        for r in recs:
            pct = effective_score(r) / total_score
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

        # 应用下限约束 + 现金口径（2026-09-16 P1-2 修复）
        # 修复说明：上限优先于"合计=100%"。原实现在此后还有一句
        #   `allocation_pct / total * 100` 的归一化，会把已被截断到 40% 的结果
        #   重新放大回 100%（total 恒为被截断后的和）——荐股 ≤2 只时必然突破
        #   MAX_ALLOCATION（实测 n=1 → 100%、n=2 → 50%），风控上限形同虚设。
        # 现改为：截断结果直接作为最终仓位，不足 100% 的部分记为现金，不回补。
        # 该语义与仓库既有设计一致：_apply_position_scale 注释即"其余比例留现金"，
        # backtest_engine._simulate_portfolio 亦按日内归一化处理，不依赖 sum==100。
        pct_list = [min(max(p, cls.MIN_ALLOCATION), cls.MAX_ALLOCATION)
                    for p in capped]

        # 约束优先级：单票上限(硬) > 合计 ≤100%(硬) > 单票下限(软)
        # 归一化被移除后，下限的副作用会显形：荐股较多且分数接近时
        # （如 10 只等分 11% 被下限抬到 ≥10% 后合计 102.8%），下限会把总和
        # 顶过 100%。此处从**高于下限**的标的按可降空间等比回收超额；
        # 极端情形（n × 下限 > 100%，即 n≥11）退化为等分并记录告警。
        if sum(pct_list) > 1.0 + 1e-9:
            excess = sum(pct_list) - 1.0
            headroom = [max(0.0, p - cls.MIN_ALLOCATION) for p in pct_list]
            total_headroom = sum(headroom)
            if total_headroom > 0 and total_headroom >= excess - 1e-9:
                for i, h in enumerate(headroom):
                    if h > 0:
                        pct_list[i] -= excess * (h / total_headroom)
            else:
                logger.warning(
                    f"荐股 {len(recs)} 只：单票下限 "
                    f"{cls.MIN_ALLOCATION * 100:.0f}% 与总仓位 100% 不可兼得，"
                    f"总仓位硬约束优先 → 退化为等分并放宽下限")
                pct_list = [min(1.0 / len(recs), cls.MAX_ALLOCATION)] * len(recs)

        result = []
        for r, pct in zip(recs, pct_list):
            r['allocation_pct'] = round(pct * 100, 1)
            result.append(r)

        # 四舍五入残差修正：保证合计不超过 100%（下调最大的一只）
        _total = sum(r['allocation_pct'] for r in result)
        if _total > 100.0 + 1e-9:
            _worst = max(result, key=lambda r: r['allocation_pct'])
            _worst['allocation_pct'] = round(
                _worst['allocation_pct'] - (_total - 100.0), 1)

        cash_pct = round(max(0.0, 100.0 - sum(r['allocation_pct'] for r in result)), 1)
        for r in result:
            r['cash_pct'] = cash_pct

        # 出口不变量（2026-09-16 P1-2）：把"约束被后置处理破坏"变成显式失败，
        # 而不是静默放行。任何新增的后置步骤都必须维持这两条不变量。
        _cap = cls.MAX_ALLOCATION * 100
        _worst = max(r['allocation_pct'] for r in result)
        assert _worst <= _cap + 1e-6, (
            f"单票仓位 {_worst}% 突破上限 {_cap}%")
        _sum = sum(r['allocation_pct'] for r in result)
        assert _sum <= 100.0 + 1e-6, f"总仓位 {_sum}% 超过 100%"

        return result

    @classmethod
    def _equal_weight(cls, recs: List[Dict]) -> List[Dict]:
        """等权分配（同样受单票上限约束，超出部分留现金）

        2026-09-16 P1-2：原实现硬编码"保证总和=100%"，n≤2 时单票 50%~100%，
        同样突破 MAX_ALLOCATION。现统一按上限截断，不足部分记现金。
        取整向下截到 0.1%（避免 n=6 时 16.7×6=100.2% 超出总仓位）。
        """
        n = len(recs)
        if n <= 0:
            return recs
        pct = min(math.floor(100.0 / n * 10) / 10, cls.MAX_ALLOCATION * 100)
        cash_pct = round(max(0.0, 100.0 - pct * n), 1)
        for r in recs:
            r['allocation_pct'] = pct
            r['cash_pct'] = cash_pct
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
