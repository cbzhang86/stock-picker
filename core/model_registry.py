"""
模型版本管理

功能：
  - 版本存档（每次权重更新打tag）
  - 版本对比（新旧权重差异）
  - 自动回滚（新版胜率下降超阈值）
  - 版本提升（A/B测试通过后提升为生产版）
"""

import json
import os
import logging
from datetime import datetime
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)


class ModelRegistry:
    """模型版本注册表"""

    def __init__(self, registry_dir: str = "data/model_registry",
                 weights_dir: str = "data/model_weights"):
        self.registry_dir = registry_dir
        self.weights_dir = weights_dir
        os.makedirs(registry_dir, exist_ok=True)

    def register_version(self, version: str, weights: dict,
                         metrics: dict, notes: str = "") -> str:
        """注册一个模型版本"""
        meta = {
            'version': version,
            'created_at': datetime.now().isoformat(),
            'weights': weights,
            'metrics': metrics,
            'notes': notes
        }
        path = os.path.join(self.registry_dir, f"{version}.json")
        with open(path, 'w', encoding='utf-8') as f:
            json.dump(meta, f, ensure_ascii=False, indent=2)

        logger.info(f"▼ 模型版本已注册: {version}")
        logger.info(f"  路径: {path}")
        return path

    def get_latest_version(self) -> Optional[str]:
        """获取最新版本号"""
        if not os.path.exists(self.registry_dir):
            return None
        files = sorted(
            [f for f in os.listdir(self.registry_dir) if f.endswith('.json')],
            reverse=True
        )
        return files[0].replace('.json', '') if files else None

    def get_version_metrics(self, version: str) -> Optional[Dict]:
        """获取某版本的指标"""
        path = os.path.join(self.registry_dir, f"{version}.json")
        if os.path.exists(path):
            with open(path) as f:
                return json.load(f).get('metrics')
        return None

    def should_rollback(self, current_metrics: Dict,
                        threshold: float = -5.0) -> Optional[str]:
        """
        判断是否需要回滚

        如果当前版本胜率比上一个版本下降超过 threshold(百分点)，
        建议回滚到上一个版本
        """
        current_win_rate = current_metrics.get('win_rate_t1', 0)
        current_total = current_metrics.get('total_records', 0)

        history = self.get_metrics_history(n=2)
        if len(history) < 2:
            return None

        prev = history[-2]
        prev_win_rate = prev.get('win_rate_t1', 0)
        prev_total = prev.get('total_records', 0)

        # 样本太少不判断
        if prev_total < 10 or current_total < 10:
            return None

        delta = current_win_rate - prev_win_rate
        if delta < threshold:
            rollback_version = self.get_latest_version()
            logger.warning(
                f"胜率下降 {delta:.1f}% (阈值{threshold}%)，"
                f"建议回滚到 {rollback_version}"
            )
            return rollback_version

        logger.info(f"当前版本稳定: 胜率变化 {delta:+.1f}%")
        return None

    def get_metrics_history(self, n: int = 10) -> List[Dict]:
        """获取历史指标"""
        if not os.path.exists(self.registry_dir):
            return []

        files = sorted(
            [f for f in os.listdir(self.registry_dir) if f.endswith('.json')],
            reverse=True
        )

        history = []
        for f in files[:n]:
            with open(os.path.join(self.registry_dir, f)) as fh:
                meta = json.load(fh)
                history.append({
                    'version': meta.get('version'),
                    'date': meta.get('created_at', ''),
                    **meta.get('metrics', {})
                })

        return history

    def list_versions(self) -> List[Dict]:
        """列出所有已注册版本"""
        if not os.path.exists(self.registry_dir):
            return []

        files = sorted(
            [f for f in os.listdir(self.registry_dir) if f.endswith('.json')]
        )

        versions = []
        for f in files:
            path = os.path.join(self.registry_dir, f)
            with open(path) as fh:
                meta = json.load(fh)
            versions.append({
                'version': meta.get('version'),
                'created_at': meta.get('created_at', ''),
                'win_rate': meta.get('metrics', {}).get('win_rate_t1', 'N/A'),
                'total_records': meta.get('metrics', {}).get('total_records', 0),
                'notes': meta.get('notes', ''),
            })

        return versions
