"""The main helper/assistant leg registers a BOUNDED core tool set.

Measured live 2026-09-05 on the installed build, CREATE of agent
88601674818 ("Relay") action 6 — the pipeline's own guard named it:

    llm_outbound ERROR  wire-trim: the TOOL SCHEMA alone is 10544 tokens
    against an n_ctx of 12288 (72 tool(s)) — no amount of message trimming
    can make this fit.  Prune the tool list for this agent.
    -> 400 request (13378 tokens) exceeds the available context size (12288)

The turn that 400'd was asking the Helper to author a JSON recipe for one
step while carrying payments, video, channel, camera, receipt, Instagram
and coding tools it cannot use.  CREATE could not finish for ANY agent
whose flow reached that size, so this blocked every agent, not one.

ROOT CAUSE — an asymmetry between two legs with the SAME agent roles:

    reuse_recipe.py  register_core_tools(<filtered>, helper, assistant)  18
    create_recipe.py register_core_tools(core_tools,  helper, assistant)  ALL

filter_service_tools() already gates the SERVICE registry and is called by
both pipelines, but it says of this set: "the always-on core closures and
Tier-2 families are unaffected".  Nothing bounded them, and the set grew
past the context budget (36 tools / 5,328 tok when #730 measured it;
72 / 10,544 here).

The name set now lives at core.agent_tools.MAIN_LEG_CORE_TOOLS, beside the
factory that builds the closures, and BOTH legs go through
main_leg_core_tools().  These tests fail if either drifts back.

    python -m pytest tests/unit/test_main_leg_core_tool_filter.py --noconftest -q
"""
import ast
import os
import unittest

from core.agent_tools import MAIN_LEG_CORE_TOOLS, main_leg_core_tools

_HARTOS = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _main_leg_calls(path):
    """Every register_core_tools(...) call whose executor pair is the MAIN leg.

    The main leg is identified by its agent variable names (helper,
    assistant) — the same pair in both pipelines.  The time/visual legs use
    helper1/time_agent and are deliberately NOT filtered.
    """
    tree = ast.parse(open(path, encoding='utf-8').read(), path)
    out = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        fn = node.func
        name = fn.id if isinstance(fn, ast.Name) else getattr(fn, 'attr', None)
        if name != 'register_core_tools' or len(node.args) < 3:
            continue
        pair = [a.id for a in node.args[1:3] if isinstance(a, ast.Name)]
        if pair == ['helper', 'assistant']:
            out.append(node)
    return out


class MainLegCoreToolFilter(unittest.TestCase):

    # ---- THE REGRESSION -------------------------------------------------
    def test_both_pipelines_filter_the_main_leg(self):
        """create AND reuse must pass core tools through main_leg_core_tools."""
        for rel in ('hartos/create_recipe.py', 'hartos/reuse_recipe.py'):
            path = os.path.join(_HARTOS, rel)
            calls = _main_leg_calls(path)
            self.assertTrue(
                calls, f'{rel}: no register_core_tools(..., helper, assistant) '
                       'found — the AST guard has gone stale, re-point it')
            for call in calls:
                first = call.args[0]
                self.assertTrue(
                    isinstance(first, ast.Call)
                    and getattr(first.func, 'id', getattr(first.func, 'attr', None))
                    == 'main_leg_core_tools',
                    f'{rel}:{call.lineno} registers UNFILTERED core tools on the '
                    'main leg. That is the 72-tool / 10,544-token schema that '
                    '400s every turn once a flow grows (agent 88601674818, '
                    '2026-09-05). Wrap it in main_leg_core_tools().')

    def test_no_second_copy_of_the_name_set(self):
        """One source of truth: no local re-declaration of the name set."""
        for rel in ('hartos/create_recipe.py', 'hartos/reuse_recipe.py'):
            src = open(os.path.join(_HARTOS, rel), encoding='utf-8').read()
            self.assertNotIn(
                '_MAIN_LEG_CORE = ', src,
                f'{rel} re-declares the main-leg name set locally. It lives in '
                'core.agent_tools.MAIN_LEG_CORE_TOOLS; two copies drift.')

    # ---- the filter itself ---------------------------------------------
    def test_filter_keeps_only_named_tools(self):
        tools = [('google_search', 'd', lambda: None),
                 ('request_payment', 'd', lambda: None),
                 ('get_chat_history', 'd', lambda: None),
                 ('execute_coding_task', 'd', lambda: None)]
        kept = {t[0] for t in main_leg_core_tools(tools)}
        self.assertEqual({'google_search', 'get_chat_history'}, kept)

    def test_filter_preserves_tuple_shape(self):
        """register_core_tools unpacks (name, desc, func) — keep it intact."""
        f = lambda: None            # noqa: E731 - identity check below
        out = main_leg_core_tools([('google_search', 'desc', f)])
        self.assertEqual([('google_search', 'desc', f)], out)

    def test_memory_tools_survive_the_filter(self):
        """f5f0f7f2e put these on the main leg; the filter must not undo it."""
        for name in ('search_long_term_memory', 'save_to_long_term_memory'):
            self.assertIn(name, MAIN_LEG_CORE_TOOLS)

    def test_set_is_small_enough_to_fit_a_slot(self):
        """A bound, not a preference: 72 tools cost 10,544 of 12,288 tokens.

        ~146 tok/tool measured on the live body, so 18 names is ~2.6k — it
        leaves room for the system prompt, history and 2048 generation.
        """
        self.assertLessEqual(
            len(MAIN_LEG_CORE_TOOLS) * 146, 4000,
            'main-leg core schema no longer leaves room in a 12,288 slot')


if __name__ == '__main__':
    unittest.main()
