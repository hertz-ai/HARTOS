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
import os
import tempfile
from pathlib import Path
import unittest

_ROOT = Path(__file__).resolve().parents[2]
# every package that registers autogen tools - agent_memory_tools.py held
# 5 more api_style="function" sites the first reuse-only sweep missed
_SWEEP_DIRS = ('hartos', 'core', 'integrations')


def _scan(root):
    """Files under `root` that register a tool with the dead `function` style."""
    offenders = []
    for d in _SWEEP_DIRS:
        base = root / d
        if not base.is_dir():
            continue
        for p in base.rglob('*.py'):
            src = p.read_text(encoding='utf-8', errors='replace')
            n = src.count('api_style="function"') + src.count("api_style='function'")
            if n:
                offenders.append(f'{p.relative_to(root)}: {n}')
    return offenders


class ToolApiStyleIsTool(unittest.TestCase):

    def test_no_function_api_style_registrations(self):
        offenders = _scan(_ROOT)
        self.assertEqual(
            offenders, [],
            'api_style="function" populates the `functions` request field, '
            'which llama-server ignores - the tool never reaches the model. '
            'Register with api_style="tool" instead: ' + '; '.join(offenders))

    def test_the_sweep_can_actually_catch_an_offender(self):
        """Non-vacuity, proven by DETECTION rather than by counting strings.

        This used to assert that reuse_recipe.py held >20 `register_for_llm`
        and >20 `api_style="tool"`. df6402e ("one canonical home each") then
        deleted 19 inline decorator stacks from create_agents_for_user because
        their bodies had drifted from their build_core_tool_closures twins, and
        the factory supplies them now. So the count fell 27 -> 9 by design and
        the guard went red on a refactor it was never about, while its own
        message said "re-point this guard".

        Pointing it at a file was the mistake: tools legitimately move. What
        must stay true is that the SWEEP still finds a violation when one
        exists, which is what actually makes the test above meaningful.
        """
        with tempfile.TemporaryDirectory() as td:
            fake = Path(td)
            (fake / 'hartos').mkdir()
            (fake / 'hartos' / 'planted.py').write_text(
                '@helper.register_for_llm(api_style="function")\n'
                'def dead_tool():\n    pass\n', encoding='utf-8')
            found = _scan(fake)
        self.assertEqual(
            found, ['hartos\\planted.py: 1'.replace('\\', os.sep)],
            'the sweep no longer detects a planted api_style="function" '
            'registration, so the guard above proves nothing')

    def test_the_tree_still_registers_tools_at_all(self):
        """The other half of non-vacuity: a tree that registered NOTHING would
        also sweep clean. Counted across the whole tree rather than in one
        file, so a move cannot turn this red again.

        Not asserting `api_style="tool"` appears: it is autogen's DEFAULT, and
        the canonical factory (core.agent_tools.build_core_tool_closures)
        correctly omits it. Requiring the explicit spelling would push callers
        back toward writing a style argument by hand, which is how the dead one
        got written in the first place.
        """
        sites = 0
        for d in _SWEEP_DIRS:
            for p in (_ROOT / d).rglob('*.py'):
                sites += p.read_text(
                    encoding='utf-8', errors='replace').count('register_for_llm')
        self.assertGreater(
            sites, 20,
            'only %d register_for_llm sites tree-wide - either tool '
            'registration moved to a vocabulary this guard cannot see, or the '
            'agents lost their tools' % sites)


if __name__ == '__main__':
    unittest.main()
