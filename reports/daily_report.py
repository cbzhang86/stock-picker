"""
每日报告生成 — 短线/长线推荐输出
"""

import os
import logging
import re
from datetime import datetime

from reports.backtest_report import generate_daily_report

logger = logging.getLogger(__name__)


class DailyReportGenerator:
    """每日报告生成器"""

    def __init__(self, output_dir: str = "data/reports"):
        self.output_dir = output_dir
        os.makedirs(output_dir, exist_ok=True)

    @staticmethod
    def _today_str() -> str:
        """北京日期（契约 H：本地一律北京时间）。"""
        try:
            from core.trading_calendar import beijing_now
            return beijing_now().strftime('%Y%m%d')
        except Exception:
            return datetime.now().strftime('%Y%m%d')

    def save_report(self, recommendations: list, mode: str = 'short') -> str:
        """
        生成并保存每日报告

        返回：文件路径
        """
        today = self._today_str()
        report = generate_daily_report(recommendations, mode)

        fname = f"{mode}_{today}.md"
        path = os.path.join(self.output_dir, fname)
        with open(path, 'w', encoding='utf-8') as f:
            f.write(report)

        logger.info(f"报告已保存: {path}")
        return path

    def get_latest_report(self, mode: str = 'short') -> str:
        """获取最新报告内容。

        2026-09-22 修复（缺陷排查 P2-20）：原实现用 `startswith(mode)` +
        文件名倒序 —— 同日同时存在 `short_YYYYMMDD.md` 与 `short_YYYYMMDD_briefing.md`
        时，因 `'_' > '.'` 使 `_briefing` 排在前面 → 返回的"最新日报"其实是简报文件。
        现只认规范命名 `{mode}_YYYYMMDD.md`，并按日期取最新。
        """
        if not os.path.exists(self.output_dir):
            return "暂无报告"

        pat = re.compile(rf'^{re.escape(mode)}_(\d{{8}})\.md$')
        cands = [f for f in os.listdir(self.output_dir) if pat.match(f)]
        if not cands:
            return "暂无报告"

        files = sorted(cands, key=lambda f: pat.match(f).group(1), reverse=True)
        path = os.path.join(self.output_dir, files[0])
        with open(path, 'r', encoding='utf-8') as f:
            return f.read()
