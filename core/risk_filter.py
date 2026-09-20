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

import logging
from typing import Dict, List

logger = logging.getLogger(__name__)


class RiskFilter:
    """风险过滤器 — 逐一检查股票是否符合买入条件"""

    # 板块涨跌幅限制（根据代码前缀）
    BOARD_LIMITS = [
        ('688', 20.0), ('689', 20.0),  # 科创板
        ('300', 20.0), ('301', 20.0),  # 创业板
        ('8', 30.0), ('92', 30.0),     # 北交所（43/83/87/88 老前缀 + 920 新代码段，2026-09-20 审查 P3-4）
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
        # 当日涨幅区间硬过滤（2026-09-18 审查 P1-1）：西部证券尾盘策略研报
        # 实证 2%-5% 为"资金已表态但未透支"的黄金区间。默认 0=关闭（待 A/B 验证）。
        self.pct_chg_min = self.config.get('pct_chg_min', 0)   # 最低涨幅%（0=关闭）
        self.pct_chg_max = self.config.get('pct_chg_max', 0)   # 最高涨幅%（0=关闭）

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

        # 3. 涨停封死检查（2026-09-05 审查报告 P0-E）
        #    原逻辑 `pct_chg >= threshold and limit_up > 0` 为死代码：limit_up_amount
        #    无任何数据源填充，恒为 0，涨停股从未被过滤 → 回测收益虚高（涨停价买不进）。
        #    修复：改用代理规则 pct_chg >= 板块上限×0.98 直接硬过滤（回测/实盘统一口径）：
        #    10%板→9.8%、20%板→19.6%、30%板→29.4%，接近涨停即视为次日无法以合理价格成交。
        if self.exclude_limit_up:
            pct_chg = stock_info.get('pct_chg', 0) or 0
            code = stock_info.get('code', '')
            board_limit = self.get_board_limit(code)
            proxy_threshold = board_limit * 0.98
            if pct_chg >= proxy_threshold:
                reasons.append(f'接近涨停(板{board_limit:.0f}%, {pct_chg:.2f}%≥{proxy_threshold:.2f}%)')
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

        # 8. 最低上市天数（2026-09-17 T12）：config 的 min_listing_days 此前仅在
        #    __init__ 读取、check_stock 从未使用 → 新上市次新股（上市 < N 天）未过滤。
        #    上游在 stock_info 传入 listing_days 时校验；缺失则跳过（不误伤）。
        #    说明：data_engine 当前无上市日期查询接口，故依赖上游提供 listing_days；
        #    若后续接入 get_stock_basic_info 可在此回退查询（TODO）。
        listing_days = stock_info.get('listing_days')
        if listing_days is not None and self.min_listing_days and listing_days < self.min_listing_days:
            reasons.append(f'上市不足({listing_days}天 < {self.min_listing_days}天)')
            penalty = max(penalty, 0.8)

        # 9. 当日涨幅区间硬过滤（2026-09-18 审查 P1-1）：2%-5% 为尾盘策略黄金区间。
        #    pct_chg_min/pct_chg_max 任一 > 0 即启用；0 = 关闭。
        pct = stock_info.get('pct_chg')
        if pct is not None:
            try:
                pct = float(pct)
                if self.pct_chg_min > 0 and pct < self.pct_chg_min:
                    reasons.append(f'涨幅不足({pct:.1f}% < {self.pct_chg_min}%)')
                    penalty = max(penalty, 0.8)
                if self.pct_chg_max > 0 and pct > self.pct_chg_max:
                    reasons.append(f'涨幅过高({pct:.1f}% > {self.pct_chg_max}%)')
                    penalty = max(penalty, 0.8)
            except (TypeError, ValueError):
                pass

        passed = len(reasons) == 0 or penalty < 0.8  # 惩罚>=0.8则直接过滤

        return {
            'passed': passed,
            'reasons': reasons,
            'score_penalty': penalty,
            'summary': '; '.join(reasons) if reasons else '通过'
        }
