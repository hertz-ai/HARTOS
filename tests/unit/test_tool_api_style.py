"""Tools must be registered api_style="tool" - `functions` is a dead field.

Measured 2026-08-31 against the live llama-server (:8080, --jinja):
the legacy `functions` request field is SILENTLY IGNORED - identical
prompt_tokens (30) with and without it, and the model answers "I cannot
execute external tools".  The same definition sent via `tools` renders
into the template (287 tok) and the model emits a real tool_call on the
first attempt.

autogen 0.3.2 maps api_style="function" -> the `functions` field and
api_style="tool" (its default) -> `tools`.  reuse_recipe carried 26
explicit api_style="function" registrations, which made the agents'
entire core tool set (memory / messaging / vision / search / execute -
28 names) invisible to the model: 0 tool-call emissions in 1,601 logged
wire calls while 39 service tools registered via the default were
visible.  This guard keeps the dead style from coming back.

    python -m pytest tests/unit/test_tool_api_style.py --noconftest -q
"""
from pathlib import Path
import unittest

_ROOT = Path(__file__).resolve().parents[2]
_REUSE = _ROOT / 'hartos' / 'reuse_recipe.py'
# every package that registers autogen tools - agent_memory_tools.py held
# 5 more api_style="function" sites the first reuse-only sweep missed
_SWEEP_DIRS = ('hartos', 'core', 'integrations')


class ToolApiStyleIsTool(unittest.TestCase):

    def test_no_function_api_style_registrations(self):
        offenders = []
        for d in _SWEEP_DIRS:
            for p in (_ROOT / d).rglob('*.py'):
                src = p.read_text(encoding='utf-8', errors='replace')
                n = src.count('api_style="function"') + src.count("api_style='function'")
                if n:
                    offenders.append(f'{p.relative_to(_ROOT)}: {n}')
        self.assertEqual(
            offenders, [],
            'api_style="function" populates the `functions` request field, '
            'which llama-server ignores - the tool never reaches the model. '
            'Register with api_style="tool" instead: ' + '; '.join(offenders))

    def test_guard_is_not_vacuous(self):
        """The file must still register tools at all, and the tool style
        must actually be in use - otherwise the sibling guard above checks
        nothing.

        2026-09-03: most of reuse_recipe.py's own register_for_llm calls
        moved to core/agent_tools.py's register_core_tools()/register_dual()
        -- the single source of truth for the core tool closures shared
        with create_recipe.py -- so reuse_recipe.py now calls that instead
        of repeating 20+ decorators inline, and register_dual's own
        register_for_llm call (core/agent_tools.py) omits api_style
        entirely, relying on autogen 0.3.2's "tool" default rather than
        writing the literal api_style="tool" string. Counting either
        vocabulary in reuse_recipe.py alone therefore undercounts; check
        the canonical factory is wired in AND still builds a real tool set.
        """
        src = _REUSE.read_text(encoding='utf-8', errors='replace')
        self.assertIn(
            'register_core_tools', src,
            'reuse_recipe no longer wires the canonical core tool '
            'registration (core.agent_tools.register_core_tools) - without '
            'it there is no tool set left for the api_style guard above to '
            'check')
        _core_src = (_ROOT / 'core' / 'agent_tools.py').read_text(
            encoding='utf-8', errors='replace')
        self.assertGreater(
            _core_src.count('tools.append(('), 20,
            'core/agent_tools.py build_core_tool_closures() tool count '
            'dropped - re-point this guard')


if __name__ == '__main__':
    unittest.main()
