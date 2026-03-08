"""Tests for core.platform.pr_guardian — Autonomous PR Quality Enforcement (WS7)."""

import sys
import os
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..'))

from core.platform.pr_guardian import (
    CodeMetrics,
    PRGuardian,
    MAX_CYCLOMATIC_COMPLEXITY,
    MAX_FUNCTION_LENGTH,
    MAX_NESTING_DEPTH,
    MAX_FILE_LENGTH,
    BLOCKED_IMPORTS,
)


# ─── CodeMetrics: Cyclomatic Complexity ──────────────────────────

class TestCyclomaticComplexity(unittest.TestCase):

    def test_simple_function(self):
        source = "def hello():\n    return 'hi'\n"
        results = CodeMetrics.cyclomatic_complexity(source)
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]['name'], 'hello')
        self.assertEqual(results[0]['complexity'], 1)

    def test_if_branches(self):
        source = (
            "def check(x):\n"
            "    if x > 0:\n"
            "        return 'pos'\n"
            "    elif x < 0:\n"
            "        return 'neg'\n"
            "    else:\n"
            "        return 'zero'\n"
        )
        results = CodeMetrics.cyclomatic_complexity(source)
        self.assertEqual(results[0]['name'], 'check')
        # 1 + 2 ifs = 3
        self.assertEqual(results[0]['complexity'], 3)

    def test_loops_and_conditions(self):
        source = (
            "def process(items):\n"
            "    for item in items:\n"
            "        if item > 0:\n"
            "            while item > 10:\n"
            "                item -= 1\n"
            "    return items\n"
        )
        results = CodeMetrics.cyclomatic_complexity(source)
        # 1 + for + if + while = 4
        self.assertEqual(results[0]['complexity'], 4)

    def test_boolean_ops(self):
        source = (
            "def multi(a, b, c):\n"
            "    if a and b or c:\n"
            "        return True\n"
        )
        results = CodeMetrics.cyclomatic_complexity(source)
        # 1 + if + (and adds 1) + (or adds 1) = 4
        self.assertEqual(results[0]['complexity'], 4)

    def test_except_handler(self):
        source = (
            "def safe():\n"
            "    try:\n"
            "        risky()\n"
            "    except ValueError:\n"
            "        pass\n"
            "    except TypeError:\n"
            "        pass\n"
        )
        results = CodeMetrics.cyclomatic_complexity(source)
        # 1 + 2 except handlers = 3
        self.assertEqual(results[0]['complexity'], 3)

    def test_multiple_functions(self):
        source = (
            "def a():\n    return 1\n\n"
            "def b():\n    if True:\n        pass\n"
        )
        results = CodeMetrics.cyclomatic_complexity(source)
        self.assertEqual(len(results), 2)
        self.assertEqual(results[0]['complexity'], 1)  # a
        self.assertEqual(results[1]['complexity'], 2)  # b

    def test_syntax_error_returns_empty(self):
        results = CodeMetrics.cyclomatic_complexity("def broken(")
        self.assertEqual(results, [])

    def test_async_function(self):
        source = (
            "async def fetch(url):\n"
            "    if url:\n"
            "        return url\n"
        )
        results = CodeMetrics.cyclomatic_complexity(source)
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]['name'], 'fetch')
        self.assertEqual(results[0]['complexity'], 2)


# ─── CodeMetrics: Function Lengths ───────────────────────────────

class TestFunctionLengths(unittest.TestCase):

    def test_short_function(self):
        source = "def f():\n    return 1\n"
        results = CodeMetrics.function_lengths(source)
        self.assertEqual(len(results), 1)
        self.assertLessEqual(results[0]['length'], 3)

    def test_long_function(self):
        lines = ["def big():"]
        for i in range(120):
            lines.append(f"    x{i} = {i}")
        lines.append("    return x0")
        source = '\n'.join(lines) + '\n'
        results = CodeMetrics.function_lengths(source)
        self.assertGreater(results[0]['length'], 100)

    def test_syntax_error_returns_empty(self):
        results = CodeMetrics.function_lengths("def broken(")
        self.assertEqual(results, [])


# ─── CodeMetrics: Nesting Depth ──────────────────────────────────

class TestNestingDepth(unittest.TestCase):

    def test_flat_function(self):
        source = "def flat():\n    return 1\n"
        results = CodeMetrics.nesting_depth(source)
        self.assertEqual(results[0]['max_depth'], 0)

    def test_deeply_nested(self):
        source = (
            "def deep():\n"
            "    if True:\n"
            "        for i in range(10):\n"
            "            while i > 0:\n"
            "                if i == 5:\n"
            "                    with open('f'):\n"
            "                        try:\n"
            "                            pass\n"
            "                        except:\n"
            "                            pass\n"
        )
        results = CodeMetrics.nesting_depth(source)
        self.assertGreater(results[0]['max_depth'], 4)

    def test_moderate_nesting(self):
        source = (
            "def moderate():\n"
            "    if True:\n"
            "        for i in range(10):\n"
            "            pass\n"
        )
        results = CodeMetrics.nesting_depth(source)
        self.assertEqual(results[0]['max_depth'], 2)

    def test_syntax_error(self):
        results = CodeMetrics.nesting_depth("def broken(")
        self.assertEqual(results, [])


# ─── CodeMetrics: Import Analysis ────────────────────────────────

class TestImportAnalysis(unittest.TestCase):

    def test_stdlib_imports(self):
        source = "import os\nimport json\nimport re\n"
        result = CodeMetrics.import_analysis(source)
        self.assertEqual(result['total'], 3)
        self.assertEqual(result['stdlib_count'], 3)
        self.assertEqual(result['blocked_imports'], [])

    def test_blocked_import(self):
        source = "import subprocess\nimport os\n"
        result = CodeMetrics.import_analysis(source)
        self.assertIn('subprocess', result['blocked_imports'])

    def test_from_import_blocked(self):
        source = "from ctypes import cdll\n"
        result = CodeMetrics.import_analysis(source)
        self.assertIn('ctypes', result['blocked_imports'])

    def test_no_imports(self):
        source = "x = 1\n"
        result = CodeMetrics.import_analysis(source)
        self.assertEqual(result['total'], 0)

    def test_syntax_error(self):
        result = CodeMetrics.import_analysis("def broken(")
        self.assertEqual(result['total'], 0)


# ─── CodeMetrics: Full Analyze ───────────────────────────────────

class TestFullAnalyze(unittest.TestCase):

    def test_analyze_returns_all_keys(self):
        source = "def f():\n    return 1\n"
        result = CodeMetrics.analyze(source)
        self.assertIn('cyclomatic_complexity', result)
        self.assertIn('function_lengths', result)
        self.assertIn('nesting_depth', result)
        self.assertIn('import_analysis', result)
        self.assertIn('total_lines', result)


# ─── PRGuardian: Threshold Checking ─────────────────────────────

class TestCheckThresholds(unittest.TestCase):

    def test_clean_metrics_no_violations(self):
        metrics = {
            'cyclomatic_complexity': [
                {'name': 'f', 'line': 1, 'complexity': 3}],
            'function_lengths': [
                {'name': 'f', 'line': 1, 'length': 20}],
            'nesting_depth': [
                {'name': 'f', 'line': 1, 'max_depth': 2}],
            'import_analysis': {'blocked_imports': []},
            'total_lines': 50,
        }
        violations = PRGuardian.check_thresholds(metrics, 'test.py')
        self.assertEqual(violations, [])

    def test_high_complexity_flagged(self):
        metrics = {
            'cyclomatic_complexity': [
                {'name': 'monster', 'line': 10,
                 'complexity': MAX_CYCLOMATIC_COMPLEXITY + 5}],
            'function_lengths': [],
            'nesting_depth': [],
            'import_analysis': {'blocked_imports': []},
            'total_lines': 50,
        }
        violations = PRGuardian.check_thresholds(metrics, 'test.py')
        self.assertTrue(any(v['rule'] == 'cyclomatic_complexity'
                            for v in violations))

    def test_long_function_flagged(self):
        metrics = {
            'cyclomatic_complexity': [],
            'function_lengths': [
                {'name': 'huge', 'line': 1,
                 'length': MAX_FUNCTION_LENGTH + 50}],
            'nesting_depth': [],
            'import_analysis': {'blocked_imports': []},
            'total_lines': 200,
        }
        violations = PRGuardian.check_thresholds(metrics)
        self.assertTrue(any(v['rule'] == 'function_length'
                            for v in violations))

    def test_deep_nesting_flagged(self):
        metrics = {
            'cyclomatic_complexity': [],
            'function_lengths': [],
            'nesting_depth': [
                {'name': 'deep', 'line': 1,
                 'max_depth': MAX_NESTING_DEPTH + 2}],
            'import_analysis': {'blocked_imports': []},
            'total_lines': 50,
        }
        violations = PRGuardian.check_thresholds(metrics)
        self.assertTrue(any(v['rule'] == 'nesting_depth'
                            for v in violations))

    def test_blocked_import_flagged(self):
        metrics = {
            'cyclomatic_complexity': [],
            'function_lengths': [],
            'nesting_depth': [],
            'import_analysis': {'blocked_imports': ['subprocess']},
            'total_lines': 10,
        }
        violations = PRGuardian.check_thresholds(metrics)
        self.assertTrue(any(v['rule'] == 'blocked_import'
                            for v in violations))

    def test_file_too_long_flagged(self):
        metrics = {
            'cyclomatic_complexity': [],
            'function_lengths': [],
            'nesting_depth': [],
            'import_analysis': {'blocked_imports': []},
            'total_lines': MAX_FILE_LENGTH + 200,
        }
        violations = PRGuardian.check_thresholds(metrics)
        self.assertTrue(any(v['rule'] == 'file_length'
                            for v in violations))


# ─── PRGuardian: Analyze File Source ─────────────────────────────

class TestAnalyzeFileSource(unittest.TestCase):

    def test_clean_file_passes(self):
        source = "def hello():\n    return 'hi'\n"
        result = PRGuardian.analyze_file_source(source, 'clean.py')
        self.assertTrue(result['passed'])
        self.assertEqual(len(result['violations']), 0)

    def test_bad_file_fails(self):
        # Build a function with high CC
        conditions = '\n'.join(
            f"    if x == {i}:\n        pass" for i in range(20))
        source = f"def monster(x):\n{conditions}\n"
        result = PRGuardian.analyze_file_source(source, 'bad.py')
        self.assertFalse(result['passed'])
        self.assertGreater(len(result['violations']), 0)


# ─── PRGuardian: Analyze Diff ────────────────────────────────────

class TestAnalyzeDiff(unittest.TestCase):

    def test_clean_diff(self):
        files = [{'filename': 'a.py', 'source': 'def f():\n    return 1\n'}]
        result = PRGuardian.analyze_diff('', files)
        self.assertTrue(result['passed'])
        self.assertEqual(result['files_analyzed'], 1)

    def test_non_python_skipped(self):
        files = [{'filename': 'data.json', 'source': '{}'}]
        result = PRGuardian.analyze_diff('', files)
        self.assertEqual(result['files_analyzed'], 0)

    def test_empty_source_skipped(self):
        files = [{'filename': 'empty.py', 'source': ''}]
        result = PRGuardian.analyze_diff('', files)
        self.assertEqual(result['files_analyzed'], 0)

    def test_diff_stats_counted(self):
        diff = "+added line\n+another\n-removed\n"
        result = PRGuardian.analyze_diff(diff, [])
        self.assertEqual(result['diff_stats']['additions'], 2)
        self.assertEqual(result['diff_stats']['deletions'], 1)


# ─── PRGuardian: Review Comment Generation ───────────────────────

class TestGenerateReviewComment(unittest.TestCase):

    def test_passing_comment(self):
        analysis = {
            'passed': True,
            'all_violations': [],
            'files_analyzed': 3,
        }
        comment = PRGuardian.generate_review_comment(analysis)
        self.assertIn('HART PR Guardian', comment)
        self.assertIn('pass', comment.lower())

    def test_failing_comment(self):
        analysis = {
            'passed': False,
            'all_violations': [
                {'rule': 'cyclomatic_complexity',
                 'message': 'test.py:10 CC=20',
                 'severity': 'error'},
            ],
            'files_analyzed': 1,
        }
        comment = PRGuardian.generate_review_comment(analysis)
        self.assertIn('violation', comment.lower())
        self.assertIn('cyclomatic_complexity', comment)

    def test_thresholds_in_comment(self):
        analysis = {'passed': True, 'all_violations': [],
                    'files_analyzed': 0}
        comment = PRGuardian.generate_review_comment(analysis)
        self.assertIn(str(MAX_CYCLOMATIC_COMPLEXITY), comment)
        self.assertIn(str(MAX_FUNCTION_LENGTH), comment)


# ─── PRGuardian: PR Checklist ────────────────────────────────────

class TestPRChecklist(unittest.TestCase):

    def test_empty_body(self):
        result = PRGuardian.check_pr_checklist('')
        self.assertFalse(result['tests_added'])
        self.assertFalse(result['docs_updated'])

    def test_checked_items(self):
        body = (
            "- [x] Tests added\n"
            "- [x] Docs updated\n"
            "- [ ] Sandbox passes\n"
        )
        result = PRGuardian.check_pr_checklist(body)
        self.assertTrue(result['tests_added'])
        self.assertTrue(result['docs_updated'])
        self.assertFalse(result['sandbox_passes'])

    def test_all_checked(self):
        body = (
            "- [x] Tests added\n"
            "- [x] Documentation updated\n"
            "- [x] No protected files modified\n"
            "- [x] Manifest validated\n"
            "- [x] Sandbox passes\n"
        )
        result = PRGuardian.check_pr_checklist(body)
        self.assertTrue(result['tests_added'])
        self.assertTrue(result['docs_updated'])
        self.assertTrue(result['no_protected_files'])
        self.assertTrue(result['manifest_validated'])
        self.assertTrue(result['sandbox_passes'])

    def test_none_body(self):
        result = PRGuardian.check_pr_checklist(None)
        self.assertFalse(result['tests_added'])


# ─── PRReviewService Integration ─────────────────────────────────

class TestPRReviewServiceIntegration(unittest.TestCase):

    def test_classify_change_guardian_bump(self):
        from integrations.agent_engine.pr_review_service import PRReviewService
        # Simple change, but with violations → moderate
        stats = {'files_changed': 1, 'additions': 10, 'deletions': 5,
                 'guardian_violations': 3}
        result = PRReviewService.classify_change(stats)
        self.assertEqual(result, 'moderate')

    def test_classify_change_no_violations(self):
        from integrations.agent_engine.pr_review_service import PRReviewService
        stats = {'files_changed': 1, 'additions': 10, 'deletions': 5}
        result = PRReviewService.classify_change(stats)
        self.assertEqual(result, 'simple')

    def test_enhanced_review_clean(self):
        from integrations.agent_engine.pr_review_service import PRReviewService
        files = [{'filename': 'ok.py',
                  'source': 'def f():\n    return 1\n'}]
        result = PRReviewService.enhanced_review(files)
        self.assertTrue(result['passed'])
        self.assertIn('review_comment', result)


# ─── Constants ───────────────────────────────────────────────────

class TestConstants(unittest.TestCase):

    def test_thresholds_sensible(self):
        self.assertEqual(MAX_CYCLOMATIC_COMPLEXITY, 15)
        self.assertEqual(MAX_FUNCTION_LENGTH, 100)
        self.assertEqual(MAX_NESTING_DEPTH, 5)
        self.assertEqual(MAX_FILE_LENGTH, 1000)

    def test_blocked_imports_frozen(self):
        self.assertIsInstance(BLOCKED_IMPORTS, frozenset)
        self.assertIn('subprocess', BLOCKED_IMPORTS)
        self.assertIn('ctypes', BLOCKED_IMPORTS)


if __name__ == '__main__':
    unittest.main()
