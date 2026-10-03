"""The live-n_ctx schema fit must not depend on the named-attach gates.

MEASURED 2026-09-30 .. 2026-10-01 on the owner's install (gui_app.log + 2
rotations, ~33 h):

    "TOOL SCHEMA alone is N tokens against an n_ctx of 12288"   296 times
    "tool schema bounded to the live n_ctx" (the fit deferring)    5 times

and llm_outbound.jsonl: 23 requests of 70-76 tools, every one the Helper seat
('You are Helper Agent. Help the Executor agent ...', source autogen.reuse),
returned HTTP 400 (the 17:01:19 one: 76 tools, 9,497 schema tokens against a
room of 12,288 - max(512, 12,288 // 4) = 9,216).  No "named attach" or "bounded"
line appears within 400 lines of it.

`_attach_named_tools_for_action` is the one per-turn door and the fit
(core.agent_tools.fit_schema_to_ctx) sat at its TAIL, behind three early
returns: no agents, an assistant that was never armed by arm_turn_attach
(`_hart_attached_tools` is None), and an unset `current_action`.  A session
that took any of them was never fitted, whatever its schema cost.  The fit
only ever removes a tool from the schema (the callable stays in the executor's
_function_map and request_tools re-arms it), and only when the schema does not
fit - a request that llama-server would have rejected anyway.

    python -m pytest tests/unit/test_fit_runs_without_a_named_attach.py --noconftest -q
"""
import ast
import io
import os
import sys
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

_HARTOS = os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))))
if _HARTOS not in sys.path:
    sys.path.insert(0, _HARTOS)

_SRC = os.environ.get('HARTOS_REUSE_SRC') or os.path.join(
    _HARTOS, 'hartos', 'reuse_recipe.py')


def _lift(*names):
    """The real function bodies from reuse_recipe.py, without importing the
    module (it needs flask's current_app at import)."""
    src = io.open(_SRC, encoding='utf-8', errors='replace').read()
    tree = ast.parse(src)
    out = {}
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name in names:
            out[node.name] = ast.get_source_segment(src, node)
    missing = set(names) - set(out)
    assert not missing, f'not found in reuse_recipe.py: {sorted(missing)}'
    return out


def _agent(name, with_tools=True):
    a = SimpleNamespace(name=name)
    a.llm_config = {'tools': [{'function': {'name': 't1'}}]} if with_tools else {}
    return a


class FitRunsWheneverTheSessionHasAgents(unittest.TestCase):

    def _door(self, *, armed, action, agents=True):
        fit = MagicMock(return_value=set())
        helper, assistant = _agent('helper'), _agent('assistant')
        if armed:
            assistant._hart_attached_tools = set()
        agents_list = [assistant, None, None, None, helper]
        ns = {
            'user_agents': {'sess': agents_list} if agents else {},
            'user_tasks': {'sess': SimpleNamespace(current_action=action)},
            'recipes': {},
            '_ctx_safe_log': MagicMock(),
            '_reuse_action_tool_names': lambda up, aid: [],
        }
        names = ['_attach_named_tools_for_action']
        try:
            lifted = _lift(*names, '_attach_current_action_tools')
        except AssertionError:
            lifted = _lift(*names)
        with patch('core.agent_tools.fit_schema_to_ctx', fit):
            for src in lifted.values():
                exec(src, ns)
            result = ns['_attach_named_tools_for_action']('sess')
        return fit, helper, assistant, result

    def test_an_unarmed_assistant_is_still_fitted(self):
        fit, helper, assistant, _ = self._door(armed=False, action=3)
        targets = [c.args[0] for c in fit.call_args_list]
        self.assertIn(helper, targets, 'the helper seat was never fitted')
        self.assertIn(assistant, targets)

    def test_an_unset_current_action_is_still_fitted(self):
        fit, helper, assistant, _ = self._door(armed=True, action=0)
        targets = [c.args[0] for c in fit.call_args_list]
        self.assertIn(helper, targets)
        self.assertIn(assistant, targets)

    def test_the_normal_armed_turn_is_still_fitted_once_per_seat(self):
        fit, helper, assistant, _ = self._door(armed=True, action=3)
        targets = [c.args[0] for c in fit.call_args_list]
        self.assertEqual(targets.count(helper), 1)
        self.assertEqual(targets.count(assistant), 1)

    def test_a_session_with_no_agents_fits_nothing_and_returns_false(self):
        fit, _, _, result = self._door(armed=True, action=3, agents=False)
        self.assertEqual(fit.call_count, 0)
        self.assertIs(result, False)

    def test_the_protected_set_always_names_send_message_to_user(self):
        fit, _, _, _ = self._door(armed=False, action=0)
        for call in fit.call_args_list:
            self.assertIn('send_message_to_user', call.kwargs['protect'])

    def test_a_failed_attach_still_protects_the_actions_own_tools(self):
        """The attach binds the recipe's tools, then something after it
        raises: the fallback fit becomes the turn's protected record, so it
        must carry those names or the budget evicts the tools just bound."""
        fit = MagicMock(return_value=set())
        helper, assistant = _agent('helper'), _agent('assistant')
        assistant._hart_attached_tools = set()

        def _log(level, msg):
            if 'Tier-1 named attach' in msg:
                raise RuntimeError('log sink down')

        ns = {
            'user_agents': {'sess': [assistant, None, None, None, helper]},
            'user_tasks': {'sess': SimpleNamespace(current_action=2)},
            'recipes': {},
            '_ctx_safe_log': _log,
            '_reuse_action_tool_names': lambda up, aid: ['crawl4ai_crawl'],
        }
        src = _lift('_attach_named_tools_for_action')
        import sys as _sys
        _svc = SimpleNamespace(service_tool_registry=MagicMock())
        with patch('core.agent_tools.fit_schema_to_ctx', fit), \
                patch('core.agent_tools.attach_for_names',
                      MagicMock(return_value=1)), \
                patch.dict(_sys.modules,
                           {'integrations.service_tools': _svc}):
            exec(src['_attach_named_tools_for_action'], ns)
            result = ns['_attach_named_tools_for_action']('sess')

        self.assertIs(result, False)
        self.assertTrue(fit.call_args_list, 'the failed attach was not fitted')
        for call in fit.call_args_list:
            self.assertIn('crawl4ai_crawl', call.kwargs['protect'])
            self.assertTrue(call.kwargs['turn_protect'])


if __name__ == '__main__':
    unittest.main()
