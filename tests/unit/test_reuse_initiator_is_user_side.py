"""Group steering messages in reuse_recipe must be initiated USER-side.

A message initiated by an AssistantAgent lands as role='assistant' in
every other agent's view.  When the group history collapses to such a
message alone, an LLM-backed speaker generates against
[system, assistant] — no user-role message anywhere — and llama.cpp's
Qwen3.5 template hard-raises ("No user query found in messages.", jinja
line 79), returned as HTTP 500.  Captured live 2026-08-30 20:15 on the
installed build: body [system 28154 chars, assistant 247], 3x 500 (one
bounded remedy-replay burst).

The reuse action loop's canonical steering initiator is chat_instructor
(a UserProxyAgent).  Two sites had drifted to `assistant.initiate_chat`
and `helper.initiate_chat`; this guard keeps them from coming back.

    python -m pytest tests/unit/test_reuse_initiator_is_user_side.py --noconftest -q
"""
import ast
from pathlib import Path
import unittest

_REUSE = Path(__file__).resolve().parents[2] / 'hartos/reuse_recipe.py'
# LLM-backed agents in reuse_recipe.  Steering messages initiated by any
# of these enter the group as assistant-role turns.
_LLM_AGENT_NAMES = {'assistant', 'helper'}


class ReuseInitiatorIsUserSide(unittest.TestCase):

    def test_no_llm_backed_agent_initiates_a_group_chat(self):
        tree = ast.parse(_REUSE.read_text(encoding='utf-8', errors='replace'))
        offenders = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            fn = node.func
            if (isinstance(fn, ast.Attribute) and fn.attr == 'initiate_chat'
                    and isinstance(fn.value, ast.Name)
                    and fn.value.id in _LLM_AGENT_NAMES):
                offenders.append(f'line {node.lineno}: '
                                 f'{fn.value.id}.initiate_chat(...)')
        self.assertEqual(
            offenders, [],
            'LLM-backed agents must not initiate group steering messages - '
            'route them through chat_instructor (UserProxyAgent) so a '
            'user-role turn always exists: ' + '; '.join(offenders))

    def test_guard_is_not_vacuous(self):
        """The file must still contain initiate_chat calls at all —
        otherwise this guard silently stops guarding anything."""
        tree = ast.parse(_REUSE.read_text(encoding='utf-8', errors='replace'))
        n = sum(1 for node in ast.walk(tree)
                if isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == 'initiate_chat')
        self.assertGreater(n, 3, 'initiate_chat vocabulary changed - '
                                 're-point this guard')


if __name__ == '__main__':
    unittest.main()
