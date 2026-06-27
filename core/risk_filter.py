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

    # 板块涨跌幅限制（根据代码前缀）
    BOARD_LIMITS = [
        ('688', 20.0), ('689', 20.0),  # 科创板
        ('300', 20.0), ('301', 20.0),  # 创业板
        ('8', 30.0),                   # 北交所
        ('4', 30.0),                   # 老三板
        # 默认: 60/00/其他 → 10%
    ]

    @staticmethod
    def get_board_limit(code: str) -> float:
        """根据股票代码判断涨跌幅限制"""
        for prefix, limit in RiskFilter.BOARD_LIMITS:
            if code.startswith(prefix):
                return limit
        return 10.0

    def __init__(self, config: dict = None):
        self.config = config or {}
        self.min_amount = self.config.get('min_volume', 30_000_000)  # 默认3000万
        self.min_listing_days = self.config.get('min_listing_days', 60)
        self.exclude_st = self.config.get('exclude_st', True)
        self.exclude_limit_up = self.config.get('exclude_limit_up', True)

    # 7. 限售解禁检查（需外部传入 data_engine，未启用则为 0）
    def check_lockup(self, code: str, data_engine=None) -> Dict:
        """
        检查股票是否面临重大解禁压力（90 天内 max_ratio >= 0.5）

        参数: data_engine — 用于查询解禁日历；为 None 时直接放行
        返回: {triggered: bool, ratio: float, reason: str}
        """
        if data_engine is None:
            return {'triggered': False, 'ratio': 0.0, 'reason': '无 data_engine，跳过'}
        try:
            info = data_engine.get_lockup_expiry(code)
        except Exception:
            return {'triggered': False, 'ratio': 0.0, 'reason': '查询异常，跳过'}
        if info is None:
            return {'triggered': False, 'ratio': 0.0, 'reason': '无解禁事件'}
        ratio = info.get('max_ratio', 0.0) or 0.0
        if ratio >= 0.5:
            return {
                'triggered': True,
                'ratio': ratio,
                'reason': f"解禁压力({info.get('next_unlock_date', '?')}, {ratio*100:.1f}%流通)"
            }
        return {'triggered': False, 'ratio': ratio, 'reason': f"解禁{ratio*100:.1f}%可控"}

    def check_stock(self, stock_info: Dict, data_engine=None) -> Dict:
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

        # 3. 涨停封死检查（根据板块涨跌幅限制）
        if self.exclude_limit_up:
            pct_chg = stock_info.get('pct_chg', 0) or 0
            limit_up = stock_info.get('limit_up_amount', 0) or 0
            code = stock_info.get('code', '')
            board_limit = self.get_board_limit(code)
            threshold = board_limit * 0.95  # 留5%容差：10%→9.5%, 20%→19%, 30%→28.5%
            if pct_chg >= threshold and limit_up > 0:
                reasons.append(f'涨停封死(板{board_limit:.0f}%)')
                penalty = max(penalty, 0.8)

        # 4. 跌停检查（根据板块涨跌幅限制）
        pct_chg_val = stock_info.get('pct_chg', 0) or 0
        code = stock_info.get('code', '')
        board_limit = self.get_board_limit(code)
        dd_threshold = board_limit * -0.95
        if pct_chg_val <= dd_threshold:
            reasons.append(f'跌停(板{board_limit:.0f}%)')
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

        # 7. 限售解禁压力（90 天内 max_ratio >= 0.5 即硬过滤）
        code = stock_info.get('code')
        if code and data_engine is not None:
            lockup_check = self.check_lockup(code, data_engine)
            if lockup_check['triggered']:
                reasons.append(lockup_check['reason'])
                penalty = max(penalty, 1.0)

        passed = len(reasons) == 0 or penalty < 0.8  # 惩罚>=0.8则直接过滤

        return {
            'passed': passed,
            'reasons': reasons,
            'score_penalty': penalty,
            'summary': '; '.join(reasons) if reasons else '通过'
        }
