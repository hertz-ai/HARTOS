"""A step that names two tools as choices is done by either one's real result.

"search with google_search or crawl4ai for the latest prices" names two tools
the step may use.  b06b29405 made it need BOTH results (rv_b06b S5: the
fabrication gate requires every tool a step names); 2569beaf0 held it to the
search alone, as the parent did.  The owner, 20 Nov 2026: "or" means either,
and it must be one rule in the gate, the receipt pick and the completion
check, never two copies.

THE RULE (hartos.lifecycle_hooks.named_tool_choices, read through
unrun_named_tools and choice_member_came_back_empty)
  * tools the step's words join by "or" are one choice: any one of their real
    results does, in REUSE's fabrication gate (_reuse_fabricated_tools), its
    receipt pick (_reuse_completion_evidence) and the completion gate
    (_work_results, _every_named_tool_worked);
  * a failed result is no result (as for every tool), and an EMPTY result of
    a choice is no result of the choice: the other tool is still to be tried;
  * a tool named alone, or two tools named one after the other, each still
    need their own result (b06b29405's fix: "crawl4ai: fetch ..." needs the
    crawl).

Real code: action_named_tools over the crawl4ai registry entry, REUSE's
_advance_reuse_action with its real gate and receipt pick (the state writers
and the commit recorded, not run), lifecycle_hooks' completion-gate readers.

    python -m pytest tests/unit/test_a_step_naming_two_tools_as_choices_is_done_by_either.py -q --noconftest
"""
import json

import pytest

import hartos.lifecycle_hooks as lh
import integrations.service_tools.registry as reg_mod
from core.constants import EMPTY_SEARCH_RESULT
from integrations.service_tools.crawl4ai_tool import Crawl4AITool
from integrations.service_tools.registry import ServiceToolRegistry

SESSION = 'choices_user_1795014914479'
CHOICE = 'search with google_search or crawl4ai for the latest prices'
ALONE = "crawl4ai: fetch and read the top result pages from the search"
ONE_THEN_THE_OTHER = 'google_search the topic, then crawl4ai: read the top pages'
FAILED = 'Not able to perform this action now please try later'


class _Agent:
    def __init__(self, name, tools):
        self.name = name
        self._function_map = {t: (lambda **kw: None) for t in tools}
        self.llm_config = {'tools': [{'function': {'name': t}} for t in tools]}
        self._oai_messages = {}


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


def _call(cid, tool, args):
    return {'role': 'assistant', 'name': 'Helper', 'content': None,
            'tool_calls': [{'id': cid, 'type': 'function', 'function': {
                'name': tool, 'arguments': json.dumps(args)}}]}


def _result(cid, body):
    return {'role': 'tool', 'name': 'Assistant', 'content': body,
            'tool_responses': [{'tool_call_id': cid, 'role': 'tool', 'content': body}]}


SEARCH = [_call('g1', 'google_search', {'query': 'prices'}),
          _result('g1', '1. https://a.example 2. https://b.example')]
SEARCH_EMPTY = [_call('g1', 'google_search', {'query': 'prices'}),
                _result('g1', EMPTY_SEARCH_RESULT)]
SEARCH_FAILED = [_call('g1', 'google_search', {'query': 'prices'}),
                 _result('g1', FAILED)]
CRAWL = [_call('c1', 'crawl4ai_crawl', {'url': 'https://a.example'}),
         _result('c1', '# Prices\n' + 'word ' * 300)]
ANSWER = {'role': 'user', 'name': 'Assistant',
          'content': 'The latest prices rose 4% this month; two shops agree.'}
VERDICT = {'role': 'user', 'name': 'StatusVerifier',
           'content': '{"status": "completed", "action_id": 2}'}
TOOLS = ['google_search', 'crawl4ai_crawl', 'send_message_to_user']


@pytest.fixture
def registry(monkeypatch):
    reg = ServiceToolRegistry.__new__(ServiceToolRegistry)
    reg._tools = {'crawl4ai': Crawl4AITool.create_tool_info()}
    reg.create_endpoint_function = lambda t, e: (lambda **kw: f'{t}.{e} ran')
    monkeypatch.setattr(reg_mod, 'service_tool_registry', reg)
    return reg


def _referenced(text):
    return sorted(lh.action_named_tools([_Agent('Helper', TOOLS)], text.lower())[1])


def test_both_choices_are_named(registry):
    assert _referenced(CHOICE) == ['crawl4ai_crawl', 'google_search']
    assert lh.named_tool_choices(CHOICE, ['google_search', 'crawl4ai_crawl']) \
        == [['google_search', 'crawl4ai_crawl']]


def test_either_order_and_the_spelled_out_name_are_one_choice(registry):
    text = 'fetch the prices with crawl4ai, or google search them'
    assert len(lh.named_tool_choices(text, ['google_search', 'crawl4ai_crawl'])) == 1


def test_tools_named_one_after_the_other_are_two_choices(registry):
    assert len(lh.named_tool_choices(ONE_THEN_THE_OTHER,
                                     ['google_search', 'crawl4ai_crawl'])) == 2


# -- the REUSE walk: gate + receipt pick, the commit recorded ----------------

@pytest.fixture
def walk(registry, monkeypatch):
    flask = pytest.importorskip('flask')
    import hartos.reuse_recipe as rr
    app = flask.Flask('choices_walk')

    def _walk(step, window, rounds=4):
        actions = [{'action': 'google_search: search the web for the topic',
                    'can_perform_without_user_input': 'yes'},
                   {'action': step, 'can_perform_without_user_input': 'yes'}]
        agents = [_Agent('Helper', TOOLS), _Agent('Assistant', TOOLS)]
        chat = _Chat([{'role': 'user', 'name': 'ChatInstructor',
                       'content': f'Perform this action -> Action #2: {step}'}]
                     + window + [VERDICT], agents)
        monkeypatch.setattr(lh, 'get_registered_groupchat', lambda _s: chat)
        monkeypatch.setitem(rr.recipes, SESSION, {'actions': actions})
        monkeypatch.setitem(rr.user_tasks, SESSION, _Task(actions))
        monkeypatch.setattr(rr, '_reuse_request_is_a_person', lambda _r: True)
        monkeypatch.setitem(rr.request_id_list, SESSION, 'person_req_1')
        states, commits = [], []
        monkeypatch.setattr(rr, 'force_state_through_valid_path',
                            lambda up, aid, st, why='', **k: states.append(st.name) or True)
        monkeypatch.setattr(rr, 'safe_set_state', lambda *a, **k: True)
        monkeypatch.setattr(rr, 'commit_verified_action_completion',
                            lambda up, aid, ev, *a, **k: commits.append(ev) or True)
        for name in ('_stamp_action_evidence_watermark', '_stamp_action_result_vacuity',
                     '_start_reuse_action'):
            monkeypatch.setattr(rr, name, lambda *a, **k: None)
        rr._reuse_resteer_counts.pop((SESSION, 2), None)
        rr._reuse_fab_pending.pop((SESSION, 2), None)
        steers = []
        with app.app_context():
            for _ in range(rounds):
                got = rr._advance_reuse_action(SESSION, 2, 'choices', '1795014914479')
                steers.append(rr._reuse_fab_steer_message(SESSION, 2))
                if got != (None, False) or states or commits:
                    break
        return commits, states, [s for s in steers if s], chat
    return _walk


def _cited(chat, commits):
    """The tool the committed receipt is a result of."""
    names = lh.tool_call_names([chat.messages])
    msg = chat.messages[commits[0]['message_index']]
    return [names.get(r.get('tool_call_id')) for r in msg.get('tool_responses') or []]


def test_the_search_alone_completes_the_choice(walk):
    commits, states, _s, chat = walk(CHOICE, SEARCH + [ANSWER])
    assert commits and not states and _cited(chat, commits) == ['google_search']


def test_the_crawl_alone_completes_the_choice(walk):
    commits, states, _s, chat = walk(CHOICE, CRAWL + [ANSWER])
    assert commits and not states and _cited(chat, commits) == ['crawl4ai_crawl']


def test_a_failed_search_and_no_crawl_completes_nothing(walk):
    commits, states, steers, _c = walk(CHOICE, SEARCH_FAILED + [ANSWER])
    assert not commits and states == ['GAVE_UP'], (commits, states)
    assert 'google_search' in steers[0] and 'crawl4ai_crawl' in steers[0]


def test_an_empty_search_and_no_crawl_completes_nothing(walk):
    commits, states, steers, _c = walk(CHOICE, SEARCH_EMPTY + [ANSWER])
    assert not commits and states == ['GAVE_UP'], (commits, states)
    assert 'crawl4ai_crawl' in steers[0]


def test_an_empty_search_then_the_crawl_completes_on_the_crawl(walk):
    commits, states, _s, chat = walk(CHOICE, SEARCH_EMPTY + CRAWL + [ANSWER])
    assert commits and not states and _cited(chat, commits) == ['crawl4ai_crawl']


def test_a_crawl_then_an_empty_search_completes_on_the_crawl(walk):
    """The receipt pick reads the rule too: the newest result, an empty
    search, is no receipt of the choice; the crawl before it is."""
    commits, states, _s, chat = walk(CHOICE, CRAWL + SEARCH_EMPTY + [ANSWER])
    assert commits and not states and _cited(chat, commits) == ['crawl4ai_crawl']


def test_a_tool_named_alone_still_needs_its_own_result(walk):
    commits, states, steers, _c = walk(ALONE, SEARCH + [ANSWER])
    assert not commits and states == ['GAVE_UP'] and 'crawl4ai_crawl' in steers[0]


def test_tools_named_one_after_the_other_need_both(walk):
    commits, states, _s, _c = walk(ONE_THEN_THE_OTHER, SEARCH + [ANSWER])
    assert not commits and states == ['GAVE_UP']


# -- the completion gate's own readers (lifecycle_hooks) ---------------------

def _window(step, results):
    return ([{'role': 'user', 'name': 'ChatInstructor',
              'content': f'Perform this action -> Action #2: {step}'}] + results)


def test_the_completion_gate_counts_the_crawl_as_the_choice_done(registry):
    msgs = _window(CHOICE, CRAWL)
    assert lh._every_named_tool_worked(msgs, len(msgs) - 1, 2, CHOICE,
                                       ['google_search', 'crawl4ai_crawl'])


def test_the_completion_gate_takes_no_empty_search_as_the_choice(registry):
    msgs = _window(CHOICE, SEARCH_EMPTY)
    names = lh.tool_call_names([msgs])
    assert list(lh._work_results(msgs[2], names, CHOICE.lower(),
                                 ['google_search', 'crawl4ai_crawl'], msgs)) == []
    assert not lh._every_named_tool_worked(msgs, len(msgs) - 1, 2, CHOICE,
                                           ['google_search', 'crawl4ai_crawl'])


def test_an_empty_search_named_alone_is_still_its_steps_work(registry):
    step = 'google_search: find the latest prices'
    msgs = _window(step, SEARCH_EMPTY)
    names = lh.tool_call_names([msgs])
    assert list(lh._work_results(msgs[2], names, step, ['google_search'], msgs)) \
        == ['google_search']
