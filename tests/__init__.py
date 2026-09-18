# -*- coding: utf-8 -*-
"""测试包标记文件。

2026-09-14 整体审查 P3-2：此前 tests/ 缺少 __init__.py，
`python -m unittest discover -s tests -p "test_*.py" -t .` 会报
`ImportError: Start directory is not importable: '...\\tests'`，
只能逐个指定模块名运行（易漏）。补上本文件后 discover 可正常工作。

统一运行方式（任选）：
  python -m unittest discover -s tests -p "test_*.py" -t . -v
  python -m unittest tests.test_improvements_20260905 tests.test_no_data_lifecycle \
                    tests.test_no_qualified_20260914 tests.test_weight_alignment_20260914
"""
