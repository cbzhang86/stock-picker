"""
每日报告生成 — 短线/长线推荐输出
"""

import os
import logging
from datetime import datetime

from reports.backtest_report import generate_daily_report

logger = logging.getLogger(__name__)


class DailyReportGenerator:
    """每日报告生成器"""

    def __init__(self, output_dir: str = "data/daily_reports"):
        self.output_dir = output_dir
        os.makedirs(output_dir, exist_ok=True)

    def save_report(self, recommendations: list, mode: str = 'short') -> str:
        """
        生成并保存每日报告

        返回：文件路径
        """
        today = datetime.now().strftime('%Y%m%d')
        report = generate_daily_report(recommendations, mode)

        fname = f"{mode}_{today}.md"
        path = os.path.join(self.output_dir, fname)
        with open(path, 'w', encoding='utf-8') as f:
            f.write(report)

        logger.info(f"报告已保存: {path}")
        return path

    def get_latest_report(self, mode: str = 'short') -> str:
        """获取最新报告内容"""
        if not os.path.exists(self.output_dir):
            return "暂无报告"

        files = sorted(
            [f for f in os.listdir(self.output_dir)
             if f.startswith(mode) and f.endswith('.md')],
            reverse=True
        )

        if not files:
            return "暂无报告"

        path = os.path.join(self.output_dir, files[0])
        with open(path, 'r', encoding='utf-8') as f:
            return f.read()
