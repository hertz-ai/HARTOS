"""
Tests for HART OS Evolution Engine — Self-aware code analysis.

Covers:
- Anti-pattern detection (regex + AST)
- Good pattern detection
- Change analysis and self-awareness thresholds
- should_suggest gating
- Improvement suggestion generation
- Event emission
"""

import sys
import os
import unittest
from unittest.mock import patch, MagicMock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..'))

from core.platform.evolution_engine import (
    EvolutionEngine, GOOD_PATTERNS, ANTI_PATTERNS,
    SELF_AWARENESS_THRESHOLDS, CORE_SUBSYSTEMS,
)


# ═══════════════════════════════════════════════════════════════
# Anti-Pattern Detection
# ═══════════════════════════════════════════════════════════════

class TestDetectAntiPatterns(unittest.TestCase):
    """Anti-pattern regex + AST detection."""

    def test_hardcoded_port(self):
        source = 'port = 8080\napp.run()'
        results = EvolutionEngine.detect_anti_patterns(source)
        patterns = [r['pattern'] for r in results]
        self.assertIn('hardcoded_port', patterns)

    def test_bare_except(self):
        source = 'try:\n    x = 1\nexcept:\n    pass'
        results = EvolutionEngine.detect_anti_patterns(source)
        patterns = [r['pattern'] for r in results]
        self.assertIn('bare_except', patterns)

    def test_eval_usage(self):
        source = 'result = eval("2+2")\n'
        results = EvolutionEngine.detect_anti_patterns(source)
        patterns = [r['pattern'] for r in results]
        self.assertIn('eval_usage', patterns)

    def test_star_import(self):
        source = 'from os import *\nimport sys\n'
        results = EvolutionEngine.detect_anti_patterns(source)
        patterns = [r['pattern'] for r in results]
        self.assertIn('star_import', patterns)

    def test_hardcoded_key(self):
        source = 'api_key = "sk-abcdefgh12345678"\n'
        results = EvolutionEngine.detect_anti_patterns(source)
        patterns = [r['pattern'] for r in results]
        self.assertIn('hardcoded_key', patterns)


# ═══════════════════════════════════════════════════════════════
# Good Pattern Detection
# ═══════════════════════════════════════════════════════════════

class TestDetectGoodPatterns(unittest.TestCase):
    """Good pattern regex detection."""

    def test_budget_gate_validation(self):
        source = 'return valid, reason\n'
        results = EvolutionEngine.detect_good_patterns(source)
        patterns = [r['pattern'] for r in results]
        self.assertIn('budget_gate_validation', patterns)

    def test_singleton_pattern(self):
        source = '_instance = None\n\ndef get_engine():\n    return _instance\n'
        results = EvolutionEngine.detect_good_patterns(source)
        patterns = [r['pattern'] for r in results]
        self.assertIn('singleton_pattern', patterns)

    def test_event_emission(self):
        source = 'emit_event("theme.changed", {"preset": "dark"})\n'
        results = EvolutionEngine.detect_good_patterns(source)
        patterns = [r['pattern'] for r in results]
        self.assertIn('event_emission', patterns)

    def test_manifest_validation(self):
        source = 'valid, errors = ManifestValidator.validate(manifest)\n'
        results = EvolutionEngine.detect_good_patterns(source)
        patterns = [r['pattern'] for r in results]
        self.assertIn('manifest_validation', patterns)


# ═══════════════════════════════════════════════════════════════
# Analyze Changes
# ═══════════════════════════════════════════════════════════════

class TestAnalyzeChanges(unittest.TestCase):
    """Change analysis and self-awareness thresholds."""

    def test_core_platform_changes_trigger_self_aware(self):
        """3+ core/platform/ files should trigger self-awareness."""
        files = [
            'core/platform/events.py',
            'core/platform/registry.py',
            'core/platform/config.py',
        ]
        result = EvolutionEngine.analyze_changes(files)
        self.assertTrue(result['self_aware'])
        self.assertIn('platform_layer', result['affected_subsystems'])

    def test_security_change_triggers_self_aware(self):
        """Even 1 security/ file should trigger self-awareness."""
        files = ['security/hive_guardrails.py']
        result = EvolutionEngine.analyze_changes(files)
        self.assertTrue(result['self_aware'])
        self.assertIn('security', result['affected_subsystems'])

    def test_non_core_changes_dont_trigger(self):
        """Files outside core subsystems should not trigger self-awareness."""
        files = ['README.md', 'docs/guide.md', 'scripts/deploy.sh']
        result = EvolutionEngine.analyze_changes(files)
        self.assertFalse(result['self_aware'])
        self.assertEqual(result['affected_subsystems'], [])

    def test_empty_changes(self):
        """Empty file list should not trigger self-awareness."""
        result = EvolutionEngine.analyze_changes([])
        self.assertFalse(result['self_aware'])
        self.assertEqual(result['affected_subsystems'], [])


# ═══════════════════════════════════════════════════════════════
# Should Suggest
# ═══════════════════════════════════════════════════════════════

class TestShouldSuggest(unittest.TestCase):
    """Gating: when should the engine emit suggestions?"""

    def test_above_threshold_true(self):
        """3+ core/platform/ files should suggest."""
        files = [
            'core/platform/a.py',
            'core/platform/b.py',
            'core/platform/c.py',
        ]
        self.assertTrue(EvolutionEngine.should_suggest(files))

    def test_below_threshold_false(self):
        """1 core/platform/ file is below threshold (3)."""
        files = ['core/platform/a.py']
        self.assertFalse(EvolutionEngine.should_suggest(files))

    def test_empty_list_false(self):
        """Empty file list should not suggest."""
        self.assertFalse(EvolutionEngine.should_suggest([]))


# ═══════════════════════════════════════════════════════════════
# Suggest Improvements
# ═══════════════════════════════════════════════════════════════

class TestSuggestImprovements(unittest.TestCase):
    """Improvement suggestion generation."""

    def test_anti_patterns_produce_suggestions(self):
        """Anti-patterns in analysis should yield fix suggestions."""
        analysis = {
            'self_aware': True,
            'affected_subsystems': ['platform_layer'],
            'suggestions': [],
            'pattern_matches': {
                'anti_patterns': [
                    {'pattern': 'bare_except', 'line': 5,
                     'description': 'Bare except'},
                ],
                'good_patterns': [],
            },
        }
        suggestions = EvolutionEngine.suggest_improvements(analysis)
        self.assertTrue(len(suggestions) > 0)
        combined = ' '.join(suggestions)
        self.assertIn('bare_except', combined.lower().replace('-', '_').replace(' ', '_')
                       ) or self.assertIn('Anti-pattern', combined)

    def test_no_issues_produce_empty(self):
        """Clean analysis with no patterns should yield no suggestions."""
        analysis = {
            'self_aware': False,
            'affected_subsystems': [],
            'suggestions': [],
            'pattern_matches': {
                'anti_patterns': [],
                'good_patterns': [
                    {'pattern': 'event_emission', 'line': 1,
                     'description': 'Uses emit_event()'},
                ],
            },
        }
        suggestions = EvolutionEngine.suggest_improvements(analysis)
        self.assertEqual(suggestions, [])


# ═══════════════════════════════════════════════════════════════
# Emit Suggestions
# ═══════════════════════════════════════════════════════════════

class TestEmitSuggestions(unittest.TestCase):
    """Event emission via EventBus."""

    @patch('core.platform.events.emit_event')
    def test_events_emitted_with_correct_topics(self, mock_emit):
        """Should emit evolution.suggestion, pattern_violation, complexity_warning."""
        analysis = {
            'self_aware': True,
            'affected_subsystems': ['security'],
            'suggestions': [],
            'pattern_matches': {
                'anti_patterns': [
                    {'pattern': 'eval_usage', 'line': 10,
                     'description': 'eval() usage'},
                ],
                'good_patterns': [],
            },
        }
        suggestions = ['Review security changes carefully']
        EvolutionEngine.emit_suggestions(suggestions, analysis)

        # Collect all emitted topics
        topics = [call[0][0] for call in mock_emit.call_args_list]
        self.assertIn('evolution.suggestion', topics)
        self.assertIn('evolution.pattern_violation', topics)
        self.assertIn('evolution.complexity_warning', topics)

    def test_no_crash_without_event_bus(self):
        """emit_suggestions should not raise even if EventBus is unavailable."""
        analysis = {
            'self_aware': False,
            'affected_subsystems': [],
            'suggestions': [],
            'pattern_matches': {'anti_patterns': [], 'good_patterns': []},
        }
        # Should not raise — event emission is best-effort
        try:
            EvolutionEngine.emit_suggestions(['test suggestion'], analysis)
        except Exception:
            self.fail('emit_suggestions raised an exception without EventBus')


# ═══════════════════════════════════════════════════════════════
# Missing Type Hints (AST-based)
# ═══════════════════════════════════════════════════════════════

class TestMissingTypeHints(unittest.TestCase):
    """AST-based detection of functions without return annotations."""

    def test_function_without_return_annotation(self):
        source = 'def greet(name):\n    return f"Hello {name}"\n'
        results = EvolutionEngine.detect_anti_patterns(source)
        type_hint_issues = [r for r in results if r['pattern'] == 'missing_type_hints']
        self.assertTrue(len(type_hint_issues) > 0)
        self.assertIn('greet', type_hint_issues[0]['description'])

    def test_function_with_annotation_clean(self):
        source = 'def greet(name: str) -> str:\n    return f"Hello {name}"\n'
        results = EvolutionEngine.detect_anti_patterns(source)
        type_hint_issues = [r for r in results if r['pattern'] == 'missing_type_hints']
        self.assertEqual(len(type_hint_issues), 0)


if __name__ == '__main__':
    unittest.main()
