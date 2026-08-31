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

_REUSE = Path(__file__).resolve().parents[2] / 'hartos' / 'reuse_recipe.py'


class ToolApiStyleIsTool(unittest.TestCase):

    def test_no_function_api_style_registrations(self):
        src = _REUSE.read_text(encoding='utf-8', errors='replace')
        n_dead = src.count('api_style="function"') + src.count("api_style='function'")
        self.assertEqual(
            n_dead, 0,
            'api_style="function" populates the `functions` request field, '
            'which llama-server ignores - the tool never reaches the model. '
            'Register with api_style="tool" instead.')

    def test_guard_is_not_vacuous(self):
        """The file must still register tools at all, and the tool style
        must actually be in use - otherwise this guard checks nothing."""
        src = _REUSE.read_text(encoding='utf-8', errors='replace')
        self.assertGreater(src.count('register_for_llm'), 20,
                           'register_for_llm vocabulary changed - re-point this guard')
        self.assertGreater(src.count('api_style="tool"'), 20,
                           'expected the 26 flipped registrations to use api_style="tool"')


if __name__ == '__main__':
    unittest.main()
