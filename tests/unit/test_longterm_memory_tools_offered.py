"""Guard: long-term memory tools must not vanish when there is no cloud API key.

Measured live 2026-09-05, Auto Research 18088688973, installed build.

`_MAIN_LEG_CORE` (reuse_recipe.py:2057) lists 18 tools.  On the wire that
turn offered 59 tools across 6 bodies, and EXACTLY TWO of the 18 were
missing:

    MISSING: ['save_to_long_term_memory', 'search_long_term_memory']

Those two, and only those two, are gated in build_core_tool_closures on
`if simplemem_store is not None:`.  Every unconditional sibling
(google_search included) was present.  So this was never per-turn tool
selection — the closures were never built, so the name-filter had
nothing to match.

Why simplemem_store was None (reuse_recipe.py:963, create_recipe.py:886,
byte-identical twins):

    if sm_config.enabled and sm_config.api_key:

A local desktop install has no cloud API key, so the branch is skipped.
There is no `else`, so nothing is logged: the live log shows
`SimpleMem initialized` 0 times AND `SimpleMem init failed` 0 times.
A capability disappeared in complete silence.

The damage is that the prompts do NOT treat it as optional — nine sites
instruct the model to call `save_to_long_term_memory` by name
(reuse_recipe.py:1213,1219,1297,1325,1908,1931,1954 and
create_recipe.py:3065,3282).  The model is told to save, is handed no
tool that can, and skips the step silently: no structured tool_call for
it appears anywhere in the 179s run.  Six actions later the agent reads
`get_memory_context` 24x, gets "No relevant memories found for current
context." every time, has nothing to report, and asserts completion.

MemoryGraph is the fix's material, not new code: it needs NO api key
(reuse_recipe.py:976-990), initialized 27 times in the same log with 0
failures — including for this very agent — and `save_to_long_term_memory`
ALREADY dual-writes to it (agent_tools.py:1072-1081), while
`get_data_by_key` already reads it back (agent_tools.py:449-455).

So the tools stay available whenever EITHER store exists.

    python -m pytest tests/unit/test_longterm_memory_tools_offered.py -q
"""
import unittest

from core.agent_tools import build_core_tool_closures


class _StubGraph:
    """Stands in for MemoryGraph — only the two methods the tools call."""

    def __init__(self):
        self.registered = []

    def register(self, content, metadata=None, parent_ids=None, **kw):
        self.registered.append((content, metadata))
        return 'mem-1'

    def recall(self, query, mode='hybrid', top_k=5):
        return []


class _StubHelperFun:
    def __getattr__(self, _name):
        return lambda *a, **k: ''


def _ctx(simplemem_store=None, memory_graph=None):
    """Minimal ctx — the factory only DEFINES closures, it never calls them."""
    return {
        'user_id': 1,
        'prompt_id': 'p1',
        'agent_data': {},
        'helper_fun': _StubHelperFun(),
        'user_prompt': 'u1_p1',
        'request_id_list': {'u1_p1': 'r1'},
        'recent_file_id': {1: None},
        'scheduler': None,
        'simplemem_store': simplemem_store,
        'memory_graph': memory_graph,
        'send_message_to_user1': lambda *a, **k: None,
        'retrieve_json': lambda s: s,
        'strip_json_values': lambda d: d,
        'save_conversation_db': lambda *a, **k: 1,
    }


def _names(ctx):
    return {name for name, _desc, _fn in build_core_tool_closures(ctx)}


class LongTermMemoryToolsOffered(unittest.TestCase):

    def test_tools_survive_a_keyless_install(self):
        """THE REGRESSION. No SimpleMem (no API key) but MemoryGraph is up.

        Pre-fix this fails: both names are absent, which is exactly what the
        live wire showed.
        """
        names = _names(_ctx(simplemem_store=None, memory_graph=_StubGraph()))
        self.assertIn(
            'save_to_long_term_memory', names,
            'a keyless local install still has MemoryGraph — the agent must '
            'keep the save tool its own prompts instruct it to call')
        self.assertIn(
            'search_long_term_memory', names,
            'the read half must come back with the write half; a store that '
            'can be written and not read is no better than none')

    def test_still_offered_when_simplemem_is_present(self):
        """No regression for installs that DO have a key."""
        names = _names(_ctx(simplemem_store=object(), memory_graph=None))
        self.assertIn('save_to_long_term_memory', names)
        self.assertIn('search_long_term_memory', names)

    def test_absent_only_when_no_store_at_all(self):
        """Honest degradation: with neither store the tools are correctly gone.

        Pins that the fix widens the gate rather than removing it — offering a
        save tool with nothing behind it would be the fabrication this whole
        investigation was about.
        """
        names = _names(_ctx(simplemem_store=None, memory_graph=None))
        self.assertNotIn('save_to_long_term_memory', names)
        self.assertNotIn('search_long_term_memory', names)

    def test_save_actually_reaches_the_graph_when_simplemem_is_absent(self):
        """Behavioural, not just presence: calling it must WRITE something.

        The failure being fixed is a tool that exists but persists nothing, so
        presence in the list is not sufficient evidence.
        """
        graph = _StubGraph()
        tools = build_core_tool_closures(_ctx(None, graph))
        save = next(fn for name, _d, fn in tools
                    if name == 'save_to_long_term_memory')
        save('The capital of France is Paris.', 'User')
        self.assertTrue(
            graph.registered,
            'save_to_long_term_memory must persist to MemoryGraph when '
            'SimpleMem is unavailable — otherwise it is a fabricated success')
        content, _meta = graph.registered[0]
        self.assertIn('Paris', content, 'the actual content must be stored')

    def test_main_leg_core_membership_is_unchanged(self):
        """The two names must still be spelled exactly as _MAIN_LEG_CORE has them.

        register_core_tools filters by `t[0] in _MAIN_LEG_CORE`, so a rename
        here silently un-registers them again — the same failure with a
        different cause.
        """
        names = _names(_ctx(None, _StubGraph()))
        for expected in ('save_to_long_term_memory', 'search_long_term_memory'):
            self.assertIn(expected, names,
                          f'{expected} is the name _MAIN_LEG_CORE filters on')


if __name__ == '__main__':
    unittest.main()
