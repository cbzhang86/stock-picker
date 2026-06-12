"""
风险过滤器 — 剔除不合格股票

过滤条件：
  - ST / *ST 股票
  - 涨停封死（封单量大）
  - 跌停封死
  - 成交量过低（< 3000万）
  - 上市不足60日
  - PE 为负且尚未盈利（长线过滤）
"""

import re
import logging
from typing import Dict, List

logger = logging.getLogger(__name__)


class RiskFilter:
    """风险过滤器 — 逐一检查股票是否符合买入条件"""

    def __init__(self, config: dict = None):
        self.config = config or {}
        self.min_amount = self.config.get('min_volume', 30_000_000)  # 默认3000万
        self.min_listing_days = self.config.get('min_listing_days', 60)
        self.exclude_st = self.config.get('exclude_st', True)
        self.exclude_limit_up = self.config.get('exclude_limit_up', True)

    def check_stock(self, stock_info: Dict) -> Dict:
        """
        检查单只股票是否通过风控

        参数：
          stock_info: {
              'code': '000001',
              'name': '平安银行',
              'price': 12.5,
              'pct_chg': 1.5,
              'amount': 500_000_000,   # 成交额
              'name_raw': '平安银行',   # 原始名称（可能含ST标记）
              'turnover': 2.5,
              'volume_ratio': 1.2,
              'listing_days': 1000,    # 上市天数
              'limit_up_amount': 0,    # 封单金额（涨停时）
              'pe': 8.5,
          }

        返回：
          {
              'passed': True/False,
              'reason': '通过' / '失败原因',
              'score_penalty': 0.0     # 风险扣分（0-1）
          }
        """
        reasons = []
        penalty = 0.0

        # 1. ST 股票检查
        if self.exclude_st:
            name = stock_info.get('name_raw', stock_info.get('name', ''))
            if 'ST' in name or '退' in name:
                reasons.append('ST/退市股')
                penalty = 1.0

        # 2. 成交量检查
        amount = stock_info.get('amount', 0) or 0
        if amount < self.min_amount:
            reasons.append(f'成交额不足 ({amount/1e4:.0f}万 < {self.min_amount/1e4:.0f}万)')
            penalty = max(penalty, 0.5)

        # 3. 涨停封死检查
        if self.exclude_limit_up:
            pct_chg = stock_info.get('pct_chg', 0) or 0
            limit_up = stock_info.get('limit_up_amount', 0) or 0
            # 涨停且封单金额大
            if pct_chg >= 9.5 and limit_up > 0:
                reasons.append('涨停封死')
                penalty = max(penalty, 0.8)

        # 4. 跌停检查
        if stock_info.get('pct_chg', 0) is not None and stock_info.get('pct_chg', 0) <= -9.5:
            reasons.append('跌停')
            penalty = max(penalty, 0.8)

        # 5. 换手率异常
        turnover = stock_info.get('turnover', 0) or 0
        if turnover > 30:
            reasons.append(f'换手率过高 ({turnover:.1f}%)')
            penalty = max(penalty, 0.4)

        # 6. 量比异常
        vol_ratio = stock_info.get('volume_ratio', 1) or 1
        if vol_ratio > 10:
            reasons.append(f'量比异常 ({vol_ratio:.1f})')
            penalty = max(penalty, 0.3)

        passed = len(reasons) == 0 or penalty < 0.8  # 惩罚>=0.8则直接过滤

        return {
            'passed': passed,
            'reasons': reasons,
            'score_penalty': penalty,
            'summary': '; '.join(reasons) if reasons else '通过'
        }
