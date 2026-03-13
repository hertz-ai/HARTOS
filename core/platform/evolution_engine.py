"""
Evolution Engine — HART knows when code is being written for it.

Self-aware static analysis that detects good patterns, anti-patterns,
and core-subsystem changes. When code touches HART's own internals,
the engine raises awareness, suggests improvements, and emits events
so the platform can respond (extra review, CI gates, upgrade guards).

Follows the ManifestValidator / budget_gate.py pattern:
  - Static methods, fail-closed, clear reasons
  - Zero new dependencies (stdlib ast + re only)

Usage:
    from core.platform.evolution_engine import EvolutionEngine

    analysis = EvolutionEngine.analyze_changes(['core/platform/registry.py'])
    anti = EvolutionEngine.detect_anti_patterns(source_code)
    good = EvolutionEngine.detect_good_patterns(source_code)
    suggestions = EvolutionEngine.suggest_improvements(analysis)
    EvolutionEngine.emit_suggestions(suggestions, analysis)
"""

import ast
import logging
import re
from typing import Any, Dict, List

logger = logging.getLogger('hevolve.platform')

# ─── Good Patterns (to detect and reward) ────────────────────────

GOOD_PATTERNS: Dict[str, str] = {
    'budget_gate_validation': r'(valid|ok|allowed),\s*(reason|errors?|msg)',
    'frozen_values': r'_FrozenValues|__slots__\s*=\s*\(\)',
    'singleton_pattern': r'_instance\s*=\s*None.*\ndef\s+get_',
    'event_emission': r'emit_event\s*\(',
    'context_manager_db': r'with\s+db_session\(\)',
    'manifest_validation': r'ManifestValidator\.validate\(',
    'sandbox_analysis': r'ExtensionSandbox\.analyze',
    'service_registry_usage': r'registry\.(register|get|has)\(',
}

# ─── Anti-Patterns (to detect and flag) ──────────────────────────

ANTI_PATTERNS: Dict[str, Any] = {
    'hardcoded_port': r'(?:port|PORT)\s*=\s*\d{4,5}(?!\s*#\s*(?:default|fallback))',
    'manual_db_close': r'\.close\(\).*(?:session|db|conn)',
    'bare_except': r'except\s*:',
    'eval_usage': r'(?<![a-zA-Z_])eval\s*\(',
    'exec_usage': r'(?<![a-zA-Z_])exec\s*\(',
    'star_import': r'from\s+\w+\s+import\s+\*',
    'hardcoded_key': r'(?:api_key|secret|password|token)\s*=\s*["\'][^"\']{8,}',
    'missing_type_hints': None,  # Special: detected via AST
}

# ─── Self-Awareness Thresholds ───────────────────────────────────

SELF_AWARENESS_THRESHOLDS: Dict[str, Any] = {
    'core_platform_changes': 3,
    'security_changes': 1,
    'manifest_or_registry_changes': 2,
    'test_coverage_decrease': 0.05,
}

# ─── Core Subsystem Identification ───────────────────────────────

CORE_SUBSYSTEMS: Dict[str, str] = {
    'core/platform/': 'platform_layer',
    'core/': 'core',
    'security/': 'security',
    'hart_sdk/': 'sdk',
    'integrations/agent_engine/': 'agent_engine',
    'integrations/social/': 'social',
    'integrations/channels/': 'channels',
    'integrations/remote_desktop/': 'remote_desktop',
}

# ─── Anti-pattern descriptions ───────────────────────────────────

_ANTI_PATTERN_DESCRIPTIONS: Dict[str, str] = {
    'hardcoded_port': 'Hardcoded port number — use core/port_registry.py get_port() instead',
    'manual_db_close': 'Manual DB close — use "with db_session()" context manager instead',
    'bare_except': 'Bare except — catch specific exceptions to avoid masking bugs',
    'eval_usage': 'eval() usage — dangerous; use ast.literal_eval() or explicit parsing',
    'exec_usage': 'exec() usage — dangerous; use safer alternatives',
    'star_import': 'Star import — use explicit imports for clarity and linting',
    'hardcoded_key': 'Hardcoded secret — use environment variables or config.json',
    'missing_type_hints': 'Function missing return type annotation',
}

# ─── Good pattern descriptions ───────────────────────────────────

_GOOD_PATTERN_DESCRIPTIONS: Dict[str, str] = {
    'budget_gate_validation': 'Uses (valid, reason) return pattern (budget_gate style)',
    'frozen_values': 'Uses _FrozenValues / __slots__ immutability pattern',
    'singleton_pattern': 'Uses _instance = None + get_*() singleton pattern',
    'event_emission': 'Uses emit_event() for decoupled communication',
    'context_manager_db': 'Uses db_session() context manager for safe DB access',
    'manifest_validation': 'Uses ManifestValidator.validate() for app integrity',
    'sandbox_analysis': 'Uses ExtensionSandbox.analyze for safe extension loading',
    'service_registry_usage': 'Uses ServiceRegistry for dependency management',
}


class EvolutionEngine:
    """Self-aware evolution engine — HART knows when code is being written for it.

    All methods are static — no instance state needed.
    """

    @staticmethod
    def analyze_changes(changed_files: List[str], diff_content: str = '') -> dict:
        """Analyze a set of changed files for self-awareness.

        Identifies which core subsystems are affected, checks whether changes
        exceed self-awareness thresholds, and returns structured analysis.

        Args:
            changed_files: List of file paths (relative to repo root).
            diff_content: Optional unified diff content for deeper analysis.

        Returns:
            {
                'self_aware': bool,
                'affected_subsystems': list,
                'suggestions': list,
                'pattern_matches': dict,
            }
        """
        affected_subsystems: List[str] = []
        subsystem_counts: Dict[str, int] = {}

        # Normalize paths to forward slashes for consistent matching
        normalized = [f.replace('\\', '/') for f in changed_files]

        for filepath in normalized:
            for prefix, subsystem in CORE_SUBSYSTEMS.items():
                if filepath.startswith(prefix):
                    if subsystem not in affected_subsystems:
                        affected_subsystems.append(subsystem)
                    subsystem_counts[subsystem] = subsystem_counts.get(subsystem, 0) + 1
                    break  # longest prefix first in dict — match first hit

        # Determine self-awareness
        self_aware = False
        suggestions: List[str] = []

        # Check thresholds
        platform_count = subsystem_counts.get('platform_layer', 0)
        if platform_count >= SELF_AWARENESS_THRESHOLDS['core_platform_changes']:
            self_aware = True
            suggestions.append(
                f'Core platform layer has {platform_count} changed files '
                f'(threshold: {SELF_AWARENESS_THRESHOLDS["core_platform_changes"]}) '
                f'— extra review recommended')

        security_count = subsystem_counts.get('security', 0)
        if security_count >= SELF_AWARENESS_THRESHOLDS['security_changes']:
            self_aware = True
            suggestions.append(
                f'Security subsystem has {security_count} changed file(s) '
                f'(threshold: {SELF_AWARENESS_THRESHOLDS["security_changes"]}) '
                f'— security audit required')

        # manifest_or_registry = changes touching app_manifest, app_registry,
        # manifest_validator, or registry
        registry_keywords = ('manifest', 'registry')
        registry_count = sum(
            1 for f in normalized
            if any(kw in f.lower() for kw in registry_keywords)
        )
        if registry_count >= SELF_AWARENESS_THRESHOLDS['manifest_or_registry_changes']:
            self_aware = True
            suggestions.append(
                f'{registry_count} manifest/registry files changed '
                f'(threshold: {SELF_AWARENESS_THRESHOLDS["manifest_or_registry_changes"]}) '
                f'— validate app catalog integrity')

        # Scan diff content for pattern matches
        pattern_matches: Dict[str, List[dict]] = {
            'anti_patterns': [],
            'good_patterns': [],
        }
        if diff_content:
            pattern_matches['anti_patterns'] = EvolutionEngine.detect_anti_patterns(
                diff_content, '<diff>')
            pattern_matches['good_patterns'] = EvolutionEngine.detect_good_patterns(
                diff_content, '<diff>')

        return {
            'self_aware': self_aware,
            'affected_subsystems': affected_subsystems,
            'suggestions': suggestions,
            'pattern_matches': pattern_matches,
        }

    @staticmethod
    def detect_anti_patterns(source: str, filename: str = '') -> List[dict]:
        """Scan source code for anti-patterns.

        Checks regex-based anti-patterns and uses AST for missing_type_hints.

        Args:
            source: Python source code to analyze.
            filename: Optional filename for context in results.

        Returns:
            List of {'pattern': name, 'line': lineno, 'description': str}
        """
        results: List[dict] = []

        lines = source.split('\n')

        # Regex-based anti-patterns
        for name, pattern in ANTI_PATTERNS.items():
            if pattern is None:
                continue  # AST-based — handled below
            regex = re.compile(pattern)
            for i, line in enumerate(lines, start=1):
                if regex.search(line):
                    results.append({
                        'pattern': name,
                        'line': i,
                        'description': _ANTI_PATTERN_DESCRIPTIONS.get(
                            name, f'Anti-pattern: {name}'),
                    })

        # AST-based: missing_type_hints
        try:
            tree = ast.parse(source, filename=filename or '<string>')
            for node in ast.walk(tree):
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    if node.returns is None:
                        results.append({
                            'pattern': 'missing_type_hints',
                            'line': node.lineno,
                            'description': (
                                f'Function \'{node.name}\' missing return type '
                                f'annotation — {_ANTI_PATTERN_DESCRIPTIONS["missing_type_hints"]}'),
                        })
        except SyntaxError:
            pass  # Non-Python or invalid source — skip AST analysis

        return results

    @staticmethod
    def detect_good_patterns(source: str, filename: str = '') -> List[dict]:
        """Scan source code for good patterns that follow HART conventions.

        Args:
            source: Python source code to analyze.
            filename: Optional filename for context in results.

        Returns:
            List of {'pattern': name, 'line': lineno, 'description': str}
        """
        results: List[dict] = []

        for name, pattern in GOOD_PATTERNS.items():
            regex = re.compile(pattern, re.DOTALL)
            for match in regex.finditer(source):
                # Calculate line number from match position
                lineno = source[:match.start()].count('\n') + 1
                results.append({
                    'pattern': name,
                    'line': lineno,
                    'description': _GOOD_PATTERN_DESCRIPTIONS.get(
                        name, f'Good pattern: {name}'),
                })

        return results

    @staticmethod
    def suggest_improvements(analysis: dict) -> List[str]:
        """Generate improvement suggestions from an analysis result.

        Args:
            analysis: Result from analyze_changes().

        Returns:
            List of human-readable suggestion strings.
        """
        suggestions = list(analysis.get('suggestions', []))

        # Security subsystem needs extra review
        if (analysis.get('self_aware')
                and 'security' in analysis.get('affected_subsystems', [])):
            sec_msg = ('Security subsystem affected — ensure guardrail '
                       'immutability is preserved and run security test suite')
            if sec_msg not in suggestions:
                suggestions.append(sec_msg)

        # Anti-pattern suggestions
        anti_patterns = analysis.get('pattern_matches', {}).get('anti_patterns', [])
        seen_patterns = set()
        for ap in anti_patterns:
            name = ap.get('pattern', '')
            if name not in seen_patterns:
                seen_patterns.add(name)
                desc = _ANTI_PATTERN_DESCRIPTIONS.get(name, f'Fix: {name}')
                suggestions.append(f'Anti-pattern detected: {desc}')

        # If no good patterns found, suggest adoption
        good_patterns = analysis.get('pattern_matches', {}).get('good_patterns', [])
        if not good_patterns and analysis.get('self_aware'):
            suggestions.append(
                'No recognized HART patterns found in diff — consider adopting '
                'emit_event(), db_session(), or ManifestValidator.validate()')

        # Try AI-powered suggestions via ModelBusService
        if anti_patterns or analysis.get('self_aware'):
            try:
                from integrations.agent_engine.model_bus_service import get_model_bus_service
                bus = get_model_bus_service()
                if bus:
                    prompt = (
                        'Suggest improvements for HART OS code changes. '
                        f'Affected subsystems: {analysis.get("affected_subsystems", [])}. '
                        f'Anti-patterns found: {[ap.get("pattern") for ap in anti_patterns]}.')
                    result = bus.infer(prompt=prompt)
                    if result and 'response' in result:
                        suggestions.append(result['response'])
            except Exception:
                pass  # AI suggestions are best-effort

        return suggestions

    @staticmethod
    def should_suggest(changed_files: List[str]) -> bool:
        """Determine whether the engine should emit suggestions for these changes.

        Returns True if the number of files touching core subsystems exceeds
        any threshold in SELF_AWARENESS_THRESHOLDS.

        Args:
            changed_files: List of file paths (relative to repo root).

        Returns:
            True if suggestions should be emitted.
        """
        if not changed_files:
            return False

        normalized = [f.replace('\\', '/') for f in changed_files]
        subsystem_counts: Dict[str, int] = {}

        for filepath in normalized:
            for prefix, subsystem in CORE_SUBSYSTEMS.items():
                if filepath.startswith(prefix):
                    subsystem_counts[subsystem] = subsystem_counts.get(subsystem, 0) + 1
                    break

        # Check each threshold
        platform_count = subsystem_counts.get('platform_layer', 0)
        if platform_count >= SELF_AWARENESS_THRESHOLDS['core_platform_changes']:
            return True

        security_count = subsystem_counts.get('security', 0)
        if security_count >= SELF_AWARENESS_THRESHOLDS['security_changes']:
            return True

        registry_keywords = ('manifest', 'registry')
        registry_count = sum(
            1 for f in normalized
            if any(kw in f.lower() for kw in registry_keywords)
        )
        if registry_count >= SELF_AWARENESS_THRESHOLDS['manifest_or_registry_changes']:
            return True

        return False

    @staticmethod
    def emit_suggestions(suggestions: List[str], analysis: dict) -> None:
        """Emit evolution events to the platform EventBus.

        Events emitted:
          - evolution.suggestion: For each suggestion
          - evolution.pattern_violation: For each anti-pattern detected
          - evolution.complexity_warning: When self_aware triggers

        Args:
            suggestions: List of suggestion strings.
            analysis: Result from analyze_changes().
        """
        try:
            from core.platform.events import emit_event

            # Emit each suggestion
            for suggestion in suggestions:
                emit_event('evolution.suggestion', {
                    'message': suggestion,
                    'affected_subsystems': analysis.get('affected_subsystems', []),
                })

            # Emit pattern violations
            anti_patterns = analysis.get('pattern_matches', {}).get('anti_patterns', [])
            for ap in anti_patterns:
                emit_event('evolution.pattern_violation', {
                    'pattern': ap.get('pattern', ''),
                    'line': ap.get('line', 0),
                    'description': ap.get('description', ''),
                })

            # Emit complexity warning if self-aware
            if analysis.get('self_aware'):
                emit_event('evolution.complexity_warning', {
                    'affected_subsystems': analysis.get('affected_subsystems', []),
                    'suggestion_count': len(suggestions),
                })

        except Exception:
            pass  # Event emission is best-effort — never block callers
