"""Guard: the MAIN reuse group's state_transition never returns the string
"auto".

Returning "auto" from a custom speaker_selection_method tells autogen to run
_auto_select_speaker, which builds an internal `checking_agent` and asks the
model to pick the next speaker.  That agent is outside the
AgentLightningWrapper, and with llama --jinja the small local model 500s
"Failed to parse tool call arguments as JSON" whenever it emits a stray
<tool_call> in that reply (measured live 2026-09-03 23:26 on the installed
build: groupchat.py:743 _auto_select_speaker -> openai.InternalServerError
500).  The uncatchable 500 kills the reuse turn and Nunba falls back to a
direct-4B answer with the real tool results unused.

The main reuse group is the state_transition whose agent-mention map routes
"@statusverifier" to `verify`.  Its fallthrough must hand to a concrete agent
(verify / assistant), and its except handler must too — never "auto".  This
walks the AST of that specific nested function so the timer/persona groups
(which have their own state_transition) are not asserted here.
"""
import ast
import os
import unittest

_SRC = os.path.join(os.path.dirname(__file__), '..', '..', 'hartos', 'reuse_recipe.py')


def _main_reuse_state_transition():
    with open(_SRC, encoding='utf-8') as fh:
        tree = ast.parse(fh.read())
    for node in ast.walk(tree):
        if not (isinstance(node, ast.FunctionDef) and node.name == 'state_transition'):
            continue
        src = ast.get_source_segment(open(_SRC, encoding='utf-8').read(), node) or ''
        if '"@statusverifier"' in src or "'@statusverifier'" in src:
            return node, src
    raise AssertionError('main reuse state_transition (with @statusverifier map) not found')


class NoAutoSpeakerSelection(unittest.TestCase):
    def test_main_group_never_returns_auto_string(self):
        node, _src = _main_reuse_state_transition()
        auto_returns = [
            n for n in ast.walk(node)
            if isinstance(n, ast.Return)
            and isinstance(n.value, ast.Constant)
            and n.value.value == 'auto'
        ]
        self.assertEqual(
            auto_returns, [],
            'main reuse state_transition still returns the string "auto" — that '
            'routes speaker selection to the unwrapped LLM checking_agent which '
            '500s on the local model')

    def test_deterministic_fallback_present(self):
        _node, src = _main_reuse_state_transition()
        self.assertIn('return verify if last_speaker is assistant else assistant', src)


if __name__ == '__main__':
    unittest.main()
