"""
PR Guardian — Autonomous code quality enforcement for HART OS.

AST-based static analysis providing cyclomatic complexity, function length,
nesting depth, and import analysis. Zero external dependencies — stdlib only.

Integrates with PRReviewService to provide enhanced reviews that go beyond
simple LOC counting.

Usage:
    from core.platform.pr_guardian import PRGuardian, CodeMetrics

    # Analyze a single file
    metrics = CodeMetrics.analyze(source)
    violations = PRGuardian.check_thresholds(metrics)

    # Full PR analysis
    report = PRGuardian.analyze_diff(diff_text, changed_files)
    comment = PRGuardian.generate_review_comment(report)
"""

import ast
import re
from typing import Any, Dict, List, Tuple

# ─── Thresholds (frozen by convention) ────────────────────────────

MAX_CYCLOMATIC_COMPLEXITY = 15
MAX_FUNCTION_LENGTH = 100
MAX_NESTING_DEPTH = 5
MAX_FILE_LENGTH = 1000
BLOCKED_IMPORTS = frozenset({
    'subprocess', 'ctypes', 'multiprocessing',
    'pickle', 'shelve', 'marshal',
})

# PR checklist keys
_CHECKLIST_KEYS = [
    'tests_added', 'docs_updated', 'no_protected_files',
    'manifest_validated', 'sandbox_passes',
]


# ─── CodeMetrics ─────────────────────────────────────────────────

class CodeMetrics:
    """AST-based code quality metrics. All static methods, stdlib only."""

    @staticmethod
    def cyclomatic_complexity(source: str) -> List[Dict[str, Any]]:
        """Compute cyclomatic complexity per function/method.

        CC = 1 + number of decision points (if, elif, for, while, and, or,
        except, with, assert, ternary IfExp, boolean ops).

        Returns list of {name, line, complexity}.
        """
        try:
            tree = ast.parse(source)
        except SyntaxError:
            return []

        results = []
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                cc = 1 + _count_decisions(node)
                results.append({
                    'name': node.name,
                    'line': node.lineno,
                    'complexity': cc,
                })
        return results

    @staticmethod
    def function_lengths(source: str) -> List[Dict[str, Any]]:
        """Compute line count per function/method.

        Returns list of {name, line, length}.
        """
        try:
            tree = ast.parse(source)
        except SyntaxError:
            return []

        results = []
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                length = _function_line_count(node)
                results.append({
                    'name': node.name,
                    'line': node.lineno,
                    'length': length,
                })
        return results

    @staticmethod
    def nesting_depth(source: str) -> List[Dict[str, Any]]:
        """Compute maximum nesting depth per function/method.

        Nesting: if/for/while/with/try inside each other.

        Returns list of {name, line, max_depth}.
        """
        try:
            tree = ast.parse(source)
        except SyntaxError:
            return []

        results = []
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                depth = _max_nesting(node)
                results.append({
                    'name': node.name,
                    'line': node.lineno,
                    'max_depth': depth,
                })
        return results

    @staticmethod
    def import_analysis(source: str) -> Dict[str, Any]:
        """Analyze imports in a source file.

        Returns {total, stdlib_count, blocked_imports, all_imports}.
        """
        try:
            tree = ast.parse(source)
        except SyntaxError:
            return {'total': 0, 'stdlib_count': 0,
                    'blocked_imports': [], 'all_imports': []}

        all_imports = []
        blocked = []

        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    mod = alias.name.split('.')[0]
                    all_imports.append(mod)
                    if mod in BLOCKED_IMPORTS:
                        blocked.append(mod)
            elif isinstance(node, ast.ImportFrom):
                if node.module:
                    mod = node.module.split('.')[0]
                    all_imports.append(mod)
                    if mod in BLOCKED_IMPORTS:
                        blocked.append(mod)

        return {
            'total': len(all_imports),
            'stdlib_count': sum(1 for m in all_imports if _is_stdlib(m)),
            'blocked_imports': list(set(blocked)),
            'all_imports': list(set(all_imports)),
        }

    @staticmethod
    def analyze(source: str) -> Dict[str, Any]:
        """Run all metrics on a source string.

        Returns combined dict of all metric results.
        """
        return {
            'cyclomatic_complexity': CodeMetrics.cyclomatic_complexity(source),
            'function_lengths': CodeMetrics.function_lengths(source),
            'nesting_depth': CodeMetrics.nesting_depth(source),
            'import_analysis': CodeMetrics.import_analysis(source),
            'total_lines': len(source.splitlines()),
        }


# ─── PRGuardian ──────────────────────────────────────────────────

class PRGuardian:
    """Autonomous PR review with code quality enforcement.

    Analyzes changed files, checks thresholds, generates review comments.
    """

    @staticmethod
    def analyze_file_source(source: str,
                            filename: str = '') -> Dict[str, Any]:
        """Analyze a single file's source code.

        Returns metrics + violations dict.
        """
        metrics = CodeMetrics.analyze(source)
        violations = PRGuardian.check_thresholds(metrics, filename)
        return {
            'filename': filename,
            'metrics': metrics,
            'violations': violations,
            'passed': len(violations) == 0,
        }

    @staticmethod
    def analyze_diff(diff_text: str,
                     changed_files: List[Dict[str, str]]) -> Dict[str, Any]:
        """Analyze a full PR diff.

        Args:
            diff_text: Raw unified diff text.
            changed_files: List of {filename, source} dicts for each file.

        Returns:
            Structured report with per-file analysis and overall verdict.
        """
        file_reports = []
        all_violations = []

        for cf in changed_files:
            filename = cf.get('filename', '')
            source = cf.get('source', '')
            if not source or not filename.endswith('.py'):
                continue

            report = PRGuardian.analyze_file_source(source, filename)
            file_reports.append(report)
            all_violations.extend(report['violations'])

        # Diff-level stats — count lines starting with +/-
        additions = 0
        deletions = 0
        if diff_text:
            for line in diff_text.splitlines():
                if line.startswith('+') and not line.startswith('+++'):
                    additions += 1
                elif line.startswith('-') and not line.startswith('---'):
                    deletions += 1

        overall_passed = len(all_violations) == 0

        result = {
            'files_analyzed': len(file_reports),
            'file_reports': file_reports,
            'total_violations': len(all_violations),
            'all_violations': all_violations,
            'diff_stats': {
                'additions': additions,
                'deletions': deletions,
            },
            'passed': overall_passed,
        }

        # Emit event (non-blocking, best-effort)
        try:
            from core.platform.events import emit_event
            emit_event('pr_review.analysis_complete', {
                'files_analyzed': len(file_reports),
                'passed': overall_passed,
                'violation_count': len(all_violations),
            })
        except Exception:
            pass

        # Audit log error-severity violations
        if not overall_passed:
            try:
                from security.immutable_audit_log import get_audit_log
                errors = [v for v in all_violations
                          if v.get('severity') == 'error']
                if errors:
                    get_audit_log().log_event(
                        'code_review', 'pr_guardian',
                        f'{len(errors)} error-severity violations',
                        detail={'violations': errors[:10]})
            except Exception:
                pass

        return result

    @staticmethod
    def check_thresholds(metrics: Dict[str, Any],
                         filename: str = '') -> List[Dict[str, str]]:
        """Check metrics against quality thresholds.

        Returns list of violation dicts: {rule, message, severity}.
        """
        violations = []

        # Cyclomatic complexity
        for func in metrics.get('cyclomatic_complexity', []):
            if func['complexity'] > MAX_CYCLOMATIC_COMPLEXITY:
                violations.append({
                    'rule': 'cyclomatic_complexity',
                    'message': (
                        f"{filename}:{func['line']} "
                        f"'{func['name']}' has CC={func['complexity']} "
                        f"(max {MAX_CYCLOMATIC_COMPLEXITY})"),
                    'severity': 'error',
                })

        # Function length
        for func in metrics.get('function_lengths', []):
            if func['length'] > MAX_FUNCTION_LENGTH:
                violations.append({
                    'rule': 'function_length',
                    'message': (
                        f"{filename}:{func['line']} "
                        f"'{func['name']}' is {func['length']} lines "
                        f"(max {MAX_FUNCTION_LENGTH})"),
                    'severity': 'warning',
                })

        # Nesting depth
        for func in metrics.get('nesting_depth', []):
            if func['max_depth'] > MAX_NESTING_DEPTH:
                violations.append({
                    'rule': 'nesting_depth',
                    'message': (
                        f"{filename}:{func['line']} "
                        f"'{func['name']}' has depth={func['max_depth']} "
                        f"(max {MAX_NESTING_DEPTH})"),
                    'severity': 'warning',
                })

        # Blocked imports
        blocked = metrics.get('import_analysis', {}).get('blocked_imports', [])
        for mod in blocked:
            violations.append({
                'rule': 'blocked_import',
                'message': (
                    f"{filename}: blocked import '{mod}' "
                    f"(security risk)"),
                'severity': 'error',
            })

        # File too long
        total = metrics.get('total_lines', 0)
        if total > MAX_FILE_LENGTH:
            violations.append({
                'rule': 'file_length',
                'message': (
                    f"{filename}: {total} lines "
                    f"(max {MAX_FILE_LENGTH})"),
                'severity': 'warning',
            })

        return violations

    @staticmethod
    def generate_review_comment(analysis: Dict[str, Any]) -> str:
        """Generate a human/agent-readable review comment.

        Tries ModelBusService for AI-enhanced summary, falls back to template.
        """
        passed = analysis.get('passed', False)
        violations = analysis.get('all_violations', [])
        files = analysis.get('files_analyzed', 0)

        # Try AI-enhanced summary
        ai_summary = ''
        if violations:
            try:
                from integrations.agent_engine.model_bus_service import (
                    get_model_bus_service,
                )
                bus = get_model_bus_service()
                if bus:
                    prompt = (
                        f"Summarize these code review violations in 2-3 "
                        f"sentences for a developer:\n"
                        f"{violations[:10]}")
                    result = bus.infer(prompt)
                    if result and 'response' in result:
                        ai_summary = result['response']
            except Exception:
                pass

        # Template
        lines = []
        lines.append('## HART PR Guardian Review\n')

        if passed:
            lines.append(f'All {files} files pass quality checks.\n')
        else:
            lines.append(
                f'Found **{len(violations)} violation(s)** '
                f'across {files} file(s).\n')

        if ai_summary:
            lines.append(f'### Summary\n{ai_summary}\n')

        # Group by severity
        errors = [v for v in violations if v.get('severity') == 'error']
        warnings = [v for v in violations if v.get('severity') == 'warning']

        if errors:
            lines.append('### Errors (must fix)')
            for v in errors:
                lines.append(f"- **{v['rule']}**: {v['message']}")

        if warnings:
            lines.append('\n### Warnings')
            for v in warnings:
                lines.append(f"- **{v['rule']}**: {v['message']}")

        lines.append(
            '\n### Thresholds')
        lines.append(
            f'- Cyclomatic Complexity: <= {MAX_CYCLOMATIC_COMPLEXITY}')
        lines.append(
            f'- Function Length: <= {MAX_FUNCTION_LENGTH} lines')
        lines.append(
            f'- Nesting Depth: <= {MAX_NESTING_DEPTH}')
        lines.append(
            f'- Blocked Imports: {sorted(BLOCKED_IMPORTS)}')

        lines.append('\n*Automated by HART PR Guardian*')

        return '\n'.join(lines)

    @staticmethod
    def check_pr_checklist(pr_body: str) -> Dict[str, bool]:
        """Parse a PR body for checklist items.

        Looks for markdown checkboxes like:
          - [x] Tests added
          - [ ] Docs updated

        Returns dict of checklist key -> checked status.
        """
        result = {k: False for k in _CHECKLIST_KEYS}

        if not pr_body:
            return result

        body_lower = pr_body.lower()

        # Match checked checkboxes: [x] or [X]
        checked = set()
        for match in re.finditer(r'\[x\]\s*(.+)', body_lower):
            checked.add(match.group(1).strip())

        # Map known phrases to checklist keys
        phrase_map = {
            'tests_added': ['tests added', 'test added', 'tests included'],
            'docs_updated': ['docs updated', 'documentation updated',
                             'docs included'],
            'no_protected_files': ['no protected file',
                                   'protected files unchanged'],
            'manifest_validated': ['manifest valid', 'manifest validated'],
            'sandbox_passes': ['sandbox pass', 'sandbox check'],
        }

        for key, phrases in phrase_map.items():
            for phrase in phrases:
                if any(phrase in item for item in checked):
                    result[key] = True
                    break

        return result


# ─── AST Helpers (module-private) ────────────────────────────────

_DECISION_NODES = (
    ast.If, ast.IfExp,
    ast.For, ast.AsyncFor,
    ast.While,
    ast.ExceptHandler,
    ast.With, ast.AsyncWith,
    ast.Assert,
)


def _count_decisions(node: ast.AST) -> int:
    """Count decision points in an AST subtree."""
    count = 0
    for child in ast.walk(node):
        if child is node:
            continue
        # Skip nested functions — they get their own CC
        if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if isinstance(child, _DECISION_NODES):
            count += 1
        elif isinstance(child, ast.BoolOp):
            # Each `and`/`or` adds (num_values - 1)
            count += len(child.values) - 1
    return count


def _function_line_count(node: ast.AST) -> int:
    """Compute approximate line count for a function node."""
    if not hasattr(node, 'end_lineno') or node.end_lineno is None:
        # Fallback for Python < 3.8
        lines = set()
        for child in ast.walk(node):
            if hasattr(child, 'lineno'):
                lines.add(child.lineno)
        return len(lines) if lines else 1
    return node.end_lineno - node.lineno + 1


_NESTING_NODES = (ast.If, ast.For, ast.AsyncFor, ast.While,
                  ast.With, ast.AsyncWith, ast.Try)


def _max_nesting(func_node: ast.AST) -> int:
    """Compute max nesting depth within a function."""
    return _nesting_depth_recursive(func_node, 0, is_root=True)


def _nesting_depth_recursive(node: ast.AST, current: int,
                              is_root: bool = False) -> int:
    """Recursively compute nesting depth."""
    if isinstance(node, _NESTING_NODES) and not is_root:
        current += 1

    # Skip nested functions (they get their own depth count)
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and not is_root:
        return current

    max_depth = current
    for child in ast.iter_child_nodes(node):
        child_depth = _nesting_depth_recursive(child, current)
        if child_depth > max_depth:
            max_depth = child_depth

    return max_depth


# Common stdlib top-level modules (subset for quick classification)
_STDLIB_MODULES = frozenset({
    'abc', 'ast', 'asyncio', 'collections', 'contextlib', 'copy',
    'csv', 'datetime', 'enum', 'functools', 'hashlib', 'io',
    'itertools', 'json', 'logging', 'math', 'os', 'pathlib',
    'pickle', 're', 'shutil', 'socket', 'sqlite3', 'string',
    'struct', 'subprocess', 'sys', 'tempfile', 'textwrap',
    'threading', 'time', 'traceback', 'typing', 'unittest',
    'urllib', 'uuid', 'warnings', 'xml', 'zipfile',
})


def _is_stdlib(module_name: str) -> bool:
    """Quick check if a module is likely stdlib."""
    return module_name in _STDLIB_MODULES
