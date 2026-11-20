"""A step that names a service tool as the other choice to a tool it names is
held to that tool, as before b06b29405.

WHAT b06b29405 FIXED (live, research agent 1795014914479, 18 Nov 2026):
"crawl4ai: fetch and read the top result pages ..." named no tool for the
completion check, so the step GAVE_UP although the crawl it was given had
run.  A one-endpoint service tool's name now names its function
(crawl4ai -> crawl4ai_crawl), and the step needs that function's result.
The owner keeps that: a step whose named tool never ran is not completed
(the 2026-09-14 ruling, never record a completion that did not happen).

WHAT IT COST (rv_b06b S5): a step naming two tools as choices, "search with
google_search or crawl4ai for the latest prices", now needed BOTH results,
because the fabrication gate requires every tool the step names.  The parent
completed it on the search.  2 of the 49 saved actions on the owner's desktop
that name crawl4ai name google_search beside it.

THE RULE
  * a service tool named only by its tool name, and joined by "or" to a tool
    the step already names, adds nothing the step must run: the step is held
    to the other tool, as on the parent;
  * a step naming the service tool alone, or beside another tool but not as
    its other choice, still needs its function's result (b06b29405's fix).

    python -m pytest tests/unit/test_a_step_naming_a_service_as_the_other_choice_is_held_to_the_first.py -q --noconftest
"""
import pytest

import hartos.lifecycle_hooks as lh
import integrations.service_tools.registry as reg_mod
from integrations.service_tools.crawl4ai_tool import Crawl4AITool
from integrations.service_tools.registry import ServiceToolRegistry

SESSION = 'choice_user_1795014914479'
CHOICE_STEP = 'search with google_search or crawl4ai for the latest prices'
LIVE_STEP = ("crawl4ai: fetch and read the top result pages from the search "
             "to gather information")


class _Agent:
    """A seat as action_named_tools and the REUSE gate read it."""

    def __init__(self, *served):
        self.name = 'Helper'
        self._function_map = {n: (lambda **kw: 'ran') for n in served}
        self.llm_config = {'tools': [{'function': {'name': n}} for n in served]}
        self._oai_messages = {}


@pytest.fixture
def registry(monkeypatch):
    reg = ServiceToolRegistry.__new__(ServiceToolRegistry)
    reg._tools = {'crawl4ai': Crawl4AITool.create_tool_info()}
    reg.create_endpoint_function = lambda t, e: (lambda **kw: f'{t}.{e} ran')
    monkeypatch.setattr(reg_mod, 'service_tool_registry', reg)
    return reg


def _referenced(text):
    return lh.action_named_tools(
        [_Agent('google_search', 'crawl4ai_crawl', 'send_message_to_user')],
        text.lower())[1]


def test_the_other_choice_adds_nothing_to_run(registry):
    assert _referenced(CHOICE_STEP) == ['google_search']


def test_either_order_and_the_spelled_out_name_are_the_same_choice(registry):
    assert _referenced('fetch the prices with crawl4ai, or google search them') \
        == ['google_search']


def test_the_service_tool_named_alone_still_needs_its_function(registry):
    assert 'crawl4ai_crawl' in _referenced(LIVE_STEP)


def test_two_tools_named_one_after_the_other_both_need_their_results(registry):
    assert sorted(_referenced(
        'google_search the topic, then crawl4ai: read the top result pages')) \
        == ['crawl4ai_crawl', 'google_search']


# -- the REUSE fabrication gate itself (reuse_recipe._reuse_fabricated_tools),
#    the one that gave the choice step up in rv_b06b's probe ------------------

DISPATCH = 'Perform this action -> Action #2: {}'
G_CALL = {'role': 'assistant', 'name': 'Helper', 'content': None,
          'tool_calls': [{'id': 'g1', 'type': 'function', 'function': {
              'name': 'google_search', 'arguments': '{"query": "prices"}'}}]}
G_RES = {'role': 'tool', 'name': 'Assistant', 'content': 'r1 r2 r3',
         'tool_responses': [{'tool_call_id': 'g1', 'role': 'tool',
                             'content': '1. https://a.example 2. https://b.example'}]}


class _Chat:
    def __init__(self, messages, agents):
        self.messages = list(messages)
        self.agents = agents


class _Task:
    def __init__(self, actions):
        self.actions = list(actions)
        self.current_action = 2
        self.evidence_seen_call_ids = set()

    def get_action(self, idx):
        return self.actions[idx]


@pytest.fixture
def gate(registry, monkeypatch):
    flask = pytest.importorskip('flask')
    import hartos.reuse_recipe as rr
    app = flask.Flask('choice_gate_test')

    def _unrun(step):
        actions = [{'action': 'google_search: search the web for the topic'},
                   {'action': step}]
        monkeypatch.setitem(rr.user_tasks, SESSION, _Task(actions))
        agents = [_Agent('google_search', 'crawl4ai_crawl', 'send_message_to_user')]
        chat = _Chat([{'role': 'user', 'name': 'ChatInstructor',
                       'content': DISPATCH.format(step)}, G_CALL, G_RES], agents)
        with app.app_context():
            return rr._reuse_fabricated_tools(SESSION, 2, chat, agents)
    return _unrun


def test_the_gate_completes_the_choice_step_on_the_search(gate):
    assert gate(CHOICE_STEP) == []


def test_the_gate_still_holds_the_live_step_to_its_crawl(gate):
    assert gate(LIVE_STEP) == ['crawl4ai_crawl']
