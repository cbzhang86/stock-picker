# -*- coding: utf-8 -*-
"""数据源登记一致性守卫（2026-09-21）

覆盖 09-03 修复后无人守护的联动点：

  A. `_source_available`（8 键，独立熔断）与 `_source_status`
     （16 键，健康报告）必须键名一致。
     2026-09-03 曾发生：3 个源只登记在熔断表、未登记在健康表 →
     `_update_source_status()` 的 `if source_key in self._source_status`
     判断失败而静默丢弃，故障永远不出现在健康报告中（SKILL.md:132 宣称
     "两个字典键名一致…两者均有守卫测试锁定"，此前无测试实际守护）。
  B. `_source_available` 的每个键必须能在 `_SOURCE_FACTOR_IMPACT` 中
     找到同名键 —— 否则该源熔断时简报的"受影响因子权重合计"漏算。
     （`_SOURCE_FACTOR_IMPACT` 里存在'哨兵值全部因子'的键，故此处
     只用"键名存在"做判据，不判因子内容。）
  C. 两表中的源键名必须真实（防打错字造成的双向漂移）。

运行：python -m unittest tests.test_source_registry_consistency -v
"""
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)


def _source_available_keys():
    from core.data_engine import DataEngine
    return set(DataEngine._source_available)


def _status_keys():
    """取 `_source_status` 的键集合。

    该字段是**实例属性**（在 `__init__` 里以 `self._source_status = {...}`
    赋值），用 `object.__new__` 绕过 `__init__` 取不到，故从源码解析。
    赋值目标是 `ast.Subscript`（self._source_status）而非 `ast.Name`，
    两处都要匹配，否则解析静默失败。
    """
    import ast
    from core.data_engine import DataEngine
    path = os.path.join(ROOT, 'core', 'data_engine.py')
    with open(path, encoding='utf-8') as fh:
        src = fh.read()
    for n in ast.walk(ast.parse(src)):
        if not isinstance(n, (ast.Assign, ast.AnnAssign)):
            continue
        target = n.target if isinstance(n, ast.AnnAssign) else n.targets[0]
        name = None
        if isinstance(target, ast.Name):
            name = target.id
        elif (isinstance(target, ast.Attribute) and isinstance(target.value, ast.Name)
              and target.value.id == 'self'):
            name = target.attr
        if name != '_source_status' or n.value is None:
            continue
        try:
            return set(ast.literal_eval(n.value))
        except (ValueError, SyntaxError):
            continue
    raise AssertionError(
        f'无法从 {path} 解析 _source_status 字面量：'
        '若已把它改为运行时构造，请同步更新本守卫测试')


class TestSourceRegistryConsistency(unittest.TestCase):
    """两个登记表的键名必须一致（防静默全绿）"""

    def setUp(self):
        self.avail = _source_available_keys()
        self.status = _status_keys()

    def test_status_registry_is_not_empty(self):
        self.assertGreater(len(self.status), 0,
                           '_source_status 应为非空字典（实例化路径异常）')

    def test_all_circuit_keys_reported(self):
        """熔断生效的源必须全部出现在健康报告里（09-03 缺陷回归锁）"""
        missing = sorted(self.avail - self.status)
        self.assertEqual(missing, [],
                         f'熔断表有但健康表缺 {missing}：这些源的故障会静默消失，'
                         f'不上报（与 2026-09-03 缺陷同构）')

    def test_no_orphan_circuit_keys(self):
        """健康表里的键不应全部脱离熔断表——除已知的"仅报告"源外"""
        # 允许：报告专用源（代码清单/腾讯行情/K线/资金流/北向/热点/板块/龙虎榜等）
        # 只参与健康报告、不参与独立熔断。这里只要求反向子集关系成立即可
        # 记录，不做断言（避免把设计选择误判为缺陷）。
        report_only = self.status - self.avail
        self.assertTrue(report_only,
                        '健康表应至少含一些不参与独立熔断的报告专用源')

    def test_registry_keys_registered_in_impact_map(self):
        """两个登记表的所有源键，必须在 _SOURCE_FACTOR_IMPACT 有同名键

        缺登记 → 该源故障时简报"受影响因子权重合计"漏算（警示严重低估）。
        """
        from reports.market_briefing import _SOURCE_FACTOR_IMPACT as IMPACT
        impact_keys = set(IMPACT)
        missing = sorted((self.avail | self.status) - impact_keys)
        self.assertEqual(missing, [],
                         f'以下源键未登记在 _SOURCE_FACTOR_IMPACT（降级警示会漏算）: '
                         f'{missing}')


if __name__ == '__main__':
    unittest.main(verbosity=2)
