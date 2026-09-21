# -*- coding: utf-8 -*-
"""动态门槛三重一致性守卫（2026-09-21）

`scripts/recalibrate_thresholds.py` 的文档说明：动态门槛（强市 65 / 中性市 70 /
弱市 75）是按**旧权重体系**的评分尺度定的；权重体系迁移（方案 G）后评分整体
上移（热点股底座 0.42×70≈29.4 → 0.55×70≈38.5），**阈值是否需要同步上调尚未
用数据验证**。

这些阈值目前以纯常量形式分散在 3 处代码 + 2 处文档：

  1. `scripts/recalibrate_thresholds.py`      — CURRENT_THRESHOLDS（诊断基线）
  2. `strategies/short_term.py`               — DEFAULT_DYNAMIC_MIN_SCORE（运行时生效）
  3. `README.md` / `SKILL.md`                 — 文档

⚠️ `config.yml` **没有** `dynamic_min_score` 覆盖项（已核实 2026-09-21）：
运行时门槛实际取自 2 的代码默认值。文档中"config.yml 基准 75"指的是
`short_term.buy.min_score = 75`（静态兜底，非动态表）。故本文件不在
config.yml 里找动态表 —— 找不到是正确状态，不是缺陷。

本文件锁的是**一致性**，不判定该不该改：

  · 若 1 与 2 漂移 → 报错。这才是真正的缺陷：诊断脚本算出的"通过率分布"
    对应的是旧门槛，而运行时用的是新门槛，决策依据直接失效。
  · 若 3 与 2 漂移 → 报错（文档与运行时口径分叉，是误导）。
  · 阈值*数值*是否合理 → 由 `recalibrate_thresholds.py` 的通过率分布分析决定，
    本文件不越权。

之所以写守卫而非改数值：这是"待用数据验证"的决策项，凭感觉改阈值
属于伪学术包装，会被叫停。
"""
import os
import re
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

KEYS = ('强市', '中性市', '弱市')


def _read(path):
    with open(os.path.join(ROOT, path), encoding='utf-8') as fh:
        return fh.read()


def _script_thresholds():
    """`recalibrate_thresholds.py` 的 CURRENT_THRESHOLDS（合法 Python 字面量）"""
    import ast
    src = _read('scripts/recalibrate_thresholds.py')
    m = re.search(r'CURRENT_THRESHOLDS\s*=\s*\{([^}]*)\}', src, re.S)
    if not m:
        return None, ['找不到 CURRENT_THRESHOLDS 常量定义']
    try:
        pairs = ast.literal_eval('{' + m.group(1) + '}')
    except (ValueError, SyntaxError) as e:
        return None, [f'CURRENT_THRESHOLDS 无法解析为字面量: {e}']
    missing = [k for k in KEYS if k not in pairs]
    if missing:
        return pairs, [f'CURRENT_THRESHOLDS 缺少档位 {missing}（实际键: {list(pairs)}）']
    return {k: int(pairs[k]) for k in KEYS}, []


def _strategy_thresholds():
    """`strategies/short_term.py` 的 DEFAULT_DYNAMIC_MIN_SCORE

    该表用常量别名做键（`LEVEL_STRONG: 65`），故先解析别名再取值。
    """
    src = _read('strategies/short_term.py')
    aliases = {}
    for m in re.finditer(r"(LEVEL_[A-Z]+)\s*=\s*'([^']+)'", src):
        aliases[m.group(1)] = m.group(2)
    m = re.search(r'DEFAULT_DYNAMIC_MIN_SCORE\s*=\s*\{([^}]*)\}', src, re.S)
    if not m:
        return None, ['找不到 DEFAULT_DYNAMIC_MIN_SCORE 定义']
    out = {}
    for m2 in re.finditer(r"([A-Za-z_]+)\s*:\s*(\d+)", m.group(1)):
        name, val = m2.group(1), int(m2.group(2))
        level = aliases.get(name, name)
        if level in KEYS:
            out[level] = val
    missing = [k for k in KEYS if k not in out]
    if missing:
        return out, [f'DEFAULT_DYNAMIC_MIN_SCORE 缺少档位 {missing}'
                     f'（别名表: {aliases}）']
    return out, []


def _doc_thresholds(path):
    """文档中的三档门槛（形如 "强市 **65** / 中性 **70** / 弱市 **75**"）"""
    src = _read(path)
    m = re.search(r'强市[^\d]{0,12}(\d{2})[^\d]{0,16}中性[^\d]{0,12}(\d{2})'
                  r'[^\d]{0,16}弱市[^\d]{0,12}(\d{2})', src)
    if m:
        return {k: int(v) for k, v in zip(KEYS, m.groups())}
    m = re.search(r'(\d{2})\s*[/、]\s*(\d{2})\s*[/、]\s*(\d{2})', src)
    return {k: int(v) for k, v in zip(KEYS, m.groups())} if m else None


class TestDocTestCountFreshness(unittest.TestCase):
    """文档里的"单元测试 N 个"必须等于实际用例数（Finding 8）

    2026-09-19 的文档写着 438，实际已 439 —— 文档与代码静默漂移一天无人察觉。
    本用例用**下界**而非精确值：文档写的是"至少 N 个"，实际多于它不算错
    （补用例是常态）；但实际少于文档声明值必须报错（意味着删除了测试或文档虚报）。
    同时反向检查：文档数字落后实际值超过 10% 时报错，提示更新。
    """

    DOC_PATTERNS = {
        'README.md': r'(\d{3})\s*单元测试',
        'README.en.md': r'(\d{3})\s*unit tests',
        'SKILL.md': r'(\d{3})\s*单元测试',
    }

    @classmethod
    def _actual(cls):
        n = 0
        for name in os.listdir(os.path.join(ROOT, 'tests')):
            if not name.startswith('test_') or not name.endswith('.py'):
                continue
            with open(os.path.join(ROOT, 'tests', name), encoding='utf-8') as fh:
                n += len(re.findall(r'^\s*def test_', fh.read(), re.M))
        return n

    def test_doc_count_not_overstated(self):
        actual = self._actual()
        for doc, pat in self.DOC_PATTERNS.items():
            with self.subTest(doc=doc):
                src = _read(doc)
                m = re.search(pat, src)
                self.assertIsNotNone(m,
                                     f'{doc} 未声明单元测试数量，无法校验')
                claimed = int(m.group(1))
                self.assertLessEqual(claimed, actual,
                                     f'{doc} 声明 {claimed} 个测试，实际只有 {actual} '
                                     f'—— 文档虚报或测试被删')

    def test_doc_count_not_stale(self):
        """文档数字落后实际值超过 10% → 报错提示更新（防再次静默漂移）"""
        actual = self._actual()
        stale = []
        for doc, pat in self.DOC_PATTERNS.items():
            m = re.search(pat, _read(doc))
            if not m:
                continue
            claimed = int(m.group(1))
            if actual >= claimed * 1.10:
                stale.append(f'{doc} 声明 {claimed} 实际 {actual}')
        self.assertEqual(stale, [],
                         '文档测试数量落后实际值 >10%，请更新: ' + '; '.join(stale))


class TestThresholdConsistency(unittest.TestCase):

    def test_script_matches_runtime(self):
        """诊断基线必须等于运行时默认值

        否则 recalibrate_thresholds.py 算出的通过率分布对应旧门槛，
        而运行时用的是新门槛 —— 决策依据失效。
        """
        a, ea = _script_thresholds()
        b, eb = _strategy_thresholds()
        self.assertEqual(ea, [], f'recalibrate_thresholds.py: {ea}')
        self.assertEqual(eb, [], f'strategies/short_term.py: {eb}')
        for k in KEYS:
            self.assertEqual(a[k], b[k],
                             f'{k} 门槛漂移：诊断脚本 {a[k]} vs 运行时 {b[k]}')

    def test_readme_matches_runtime(self):
        """README 文档门槛必须等于运行时默认值"""
        b, eb = _strategy_thresholds()
        self.assertEqual(eb, [], f'strategies/short_term.py: {eb}')
        doc = _doc_thresholds('README.md')
        self.assertIsNotNone(doc,
                             'README.md 未找到三档门槛，无法校验文档一致性')
        for k in KEYS:
            self.assertEqual(doc[k], b[k],
                             f'README {k}={doc[k]} 与运行时 {b[k]} 不一致')

    def test_skill_matches_runtime(self):
        """SKILL.md 门槛必须等于运行时默认值"""
        b, eb = _strategy_thresholds()
        self.assertEqual(eb, [], f'strategies/short_term.py: {eb}')
        doc = _doc_thresholds('SKILL.md')
        self.assertIsNotNone(doc,
                             'SKILL.md 未找到三档门槛，无法校验文档一致性')
        for k in KEYS:
            self.assertEqual(doc[k], b[k],
                             f'SKILL.md {k}={doc[k]} 与运行时 {b[k]} 不一致')

    def test_config_has_no_dynamic_override(self):
        """config.yml 目前不含 dynamic_min_score（运行时取代码默认值）

        这条断言是**反向守卫**：一旦有人在 config.yml 加了 dynamic_min_score，
        这里会失败，提醒同步更新本文件的对账来源（否则本文件仍在对代码默认值
        做校验，而实际生效值已变成 config 覆盖值）。
        """
        src = _read('config.yml')
        self.assertNotIn('dynamic_min_score', src,
                         'config.yml 出现了 dynamic_min_score 覆盖项：'
                         '运行时门槛已不再取自代码默认值，请更新本文件'
                         '的对账来源（改为读取 config 值）')

    def test_thresholds_monotonic(self):
        """弱市 > 中性 > 强市（弱市更严格，否则阈值语义反转）"""
        b, eb = _strategy_thresholds()
        self.assertEqual(eb, [], f'strategies/short_term.py: {eb}')
        self.assertGreater(b['弱市'], b['中性市'],
                           msg='弱市门槛应高于中性市（弱市更难通过）')
        self.assertGreater(b['中性市'], b['强市'],
                           msg='中性市门槛应高于强市（强市更容易通过）')

    def test_gap_between_levels_is_reasonable(self):
        """相邻档位间隔应为正整数且 < 20（间隔过大会让档位几乎不生效）"""
        b, eb = _strategy_thresholds()
        self.assertEqual(eb, [], f'strategies/short_term.py: {eb}')
        for hi, lo in (('中性市', '强市'), ('弱市', '中性市')):
            gap = b[hi] - b[lo]
            self.assertTrue(0 < gap < 20,
                            f'{hi}-{lo} 间隔 {gap} 过大或反号，'
                            f'动态档位可能名存实亡（实际值 {b}）')


if __name__ == '__main__':
    unittest.main(verbosity=2)
