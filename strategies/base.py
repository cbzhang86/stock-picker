"""
策略基类 — 所有策略的抽象接口

参考：Sequoia-X strategy/base.py
"""

from abc import ABC, abstractmethod
from typing import Dict, List, Optional


class BaseStrategy(ABC):
    """策略抽象基类"""

    def __init__(self, config: dict = None):
        self.config = config or {}
        self.name = self.__class__.__name__

    @abstractmethod
    def run(self, market_data: Dict) -> List[Dict]:
        """
        运行策略

        参数：
          market_data: 包含全市场数据的字典

        返回：
          推荐列表 [{code, name, score, rating, ...}]
        """
        pass

    @abstractmethod
    def get_required_fields(self) -> List[str]:
        """返回策略所需的数据字段列表"""
        pass

    def describe(self) -> str:
        """策略描述"""
        return f"{self.name}: {self.__doc__}"
