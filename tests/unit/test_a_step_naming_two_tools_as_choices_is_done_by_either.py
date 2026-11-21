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
    a choice is no result of the choice while the choice has better to give:
    another tool of it not tried yet, or one that returned real work.  Once
    every other tool of the choice came back empty or failed, an empty
    result counts as a lone tool's does, so an honest "nothing found"
    finishes the step (rv_bc53 F1: both empty gave up after three re-steers,
    where the parent finished in one round);
  * a tool of the choice never called is steered to first; once the step's
    re-steer budget is spent on the pipeline's own counter (REUSE's
    _reuse_resteer_counts, CREATE's retry_tracker) and it still was not
    called, the empty result counts and the step completes, never GAVE_UP:
    the parent's outcome, at 3 extra rounds (hartos-52's review of
    e6808a611, F2; owner: no compromise);
  * what came back is read by ONE builder (step_results_came_back) over
    every list a receipt can live in, each held to the step's dispatch
    window, by the gate, the receipt pick, the commit's completion gate and
    CREATE's reads alike (hartos-52 F1: REUSE read every list, the gate one,
    so a crawl only in a buffer split them);
  * the re-steer for a choice says either tool will do, says which came
    back empty or failed, and names what is left (rv_bc53 F2); a lone tool's
    re-steer is unchanged;
  * a tool named alone, or two tools named one after the other, each still
    need their own result (b06b29405's fix: "crawl4ai: fetch ..." needs the
    crawl).

Real code: action_named_tools over the crawl4ai registry entry, REUSE's
_advance_reuse_action with its real gate and receipt pick (the state writers
and the commit recorded, not run), lifecycle_hooks' completion-gate readers.

    python -m pytest tests/unit/test_a_step_naming_two_tools_as_choices_is_done_by_either.py -q --noconftest
"""
import json
from types import SimpleNamespace

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
CRAWL_EMPTY = [_call('c1', 'crawl4ai_crawl', {'url': 'https://a.example'}),
               _result('c1', '{"markdown": "", "links": []}')]
CRAWL_FAILED = [_call('c1', 'crawl4ai_crawl', {'url': 'https://a.example'}),
                _result('c1', FAILED)]
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

    def _walk(step, window, rounds=4, before=(), buffer=None, later=None, seen=(),
              buffer_got_the_order=True, buffers=()):
        """REUSE's rounds on action 2.  ``before``: messages ahead of its
        dispatch, their call ids in ``seen`` (the dispatch watermark);
        ``buffer``: results only in the Helper's buffer, behind its copy of
        the dispatch; ``later``: {round: messages the agents add after that
        round's re-steer}; ``buffer_got_the_order``: False for a buffer with
        no copy of the dispatch.  _walk.last: the rounds run and the keyword
        arguments REUSE committed with."""
        actions = [{'action': 'google_search: search the web for the topic',
                    'can_perform_without_user_input': 'yes'},
                   {'action': step, 'can_perform_without_user_input': 'yes'}]
        agents = [_Agent('Helper', TOOLS), _Agent('Assistant', TOOLS)]
        dispatch = _dispatch(2, step)
        chat = _Chat(list(before) + [dispatch] + window + [VERDICT], agents)
        if buffer:
            agents[0]._oai_messages['Assistant'] = (
                [dict(dispatch)] if buffer_got_the_order else []) + list(buffer)
        for ai, peer, msgs in buffers:      # buffers with no copy of the order
            agents[ai]._oai_messages[peer] = list(msgs)
        task = _Task(actions)
        task.evidence_seen_call_ids = set(seen)
        monkeypatch.setattr(lh, 'get_registered_groupchat', lambda _s: chat)
        monkeypatch.setitem(rr.recipes, SESSION, {'actions': actions})
        monkeypatch.setitem(rr.user_tasks, SESSION, task)
        monkeypatch.setattr(rr, '_reuse_request_is_a_person', lambda _r: True)
        monkeypatch.setitem(rr.request_id_list, SESSION, 'person_req_1')
        states, commits, kwargs = [], [], []
        monkeypatch.setattr(rr, 'force_state_through_valid_path',
                            lambda up, aid, st, why='', **k: states.append(st.name) or True)
        monkeypatch.setattr(rr, 'safe_set_state', lambda *a, **k: True)
        monkeypatch.setattr(rr, 'commit_verified_action_completion',
                            lambda up, aid, ev, *a, **k: (commits.append(ev),
                                                          kwargs.append(k)) and True)
        for name in ('_stamp_action_evidence_watermark', '_stamp_action_result_vacuity',
                     '_start_reuse_action'):
            monkeypatch.setattr(rr, name, lambda *a, **k: None)
        rr._reuse_resteer_counts.pop((SESSION, 2), None)
        rr._reuse_fab_pending.pop((SESSION, 2), None)
        steers, n = [], 0
        with app.app_context():
            for n in range(1, rounds + 1):
                got = rr._advance_reuse_action(SESSION, 2, 'choices', '1795014914479')
                steers.append(rr._reuse_fab_steer_message(SESSION, 2))
                if got != (None, False) or states or commits:
                    break
                for m in (later or {}).get(n, ()):
                    chat.messages.insert(len(chat.messages) - 1, m)
        _walk.last = SimpleNamespace(rounds=n, kwargs=kwargs, step=step)
        return commits, states, [s for s in steers if s], chat
    return _walk


def _dispatch(action_id, step):
    return {'role': 'user', 'name': 'ChatInstructor',
            'content': f'Perform this action -> Action #{action_id}: {step}'}


def _commit_accepts(monkeypatch, chat, walked, evidence):
    """The real commit (commit_verified_action_completion) on what REUSE
    committed, with the keyword arguments REUSE passed, its state writers
    stubbed: True when its completion gate accepts the receipt."""
    monkeypatch.setattr(lh, 'get_registered_groupchat', lambda _s: chat)
    monkeypatch.setattr(lh, 'get_registered_ledger', lambda _u: SimpleNamespace(
        tasks={'action_2': SimpleNamespace(description=walked.step)}))
    monkeypatch.setattr(lh, 'get_action_state',
                        lambda *a, **k: lh.ActionState.STATUS_VERIFICATION_REQUESTED)
    monkeypatch.setattr(lh, 'verdict_names_another_action', lambda *a, **k: None)
    for name in ('validate_state_transition', '_record_verifier_evidence', 'safe_set_state'):
        monkeypatch.setattr(lh, name, lambda *a, **k: True)
    monkeypatch.setattr(lh, '_promote_verified_outcome', lambda *a, **k: None)
    return lh.commit_verified_action_completion(SESSION, 2, evidence, 'walk',
                                                **walked.kwargs[0])


def _cited(chat, commits):
    """The tool the committed receipt is a result of, in the list it cites."""
    ev = commits[0]
    msgs = (chat.agents[0]._oai_messages[ev['peer']] if ev.get('source') == 'buffer'
            else chat.messages)
    names = lh.tool_call_names([msgs])
    msg = msgs[ev['message_index']]
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


def test_an_empty_search_and_no_crawl_completes_once_the_steers_are_spent(
        walk, monkeypatch):
    """hartos-52 F2: the crawl is steered to first, three times; it is never
    called, so the honest empty search counts and the step completes on it,
    never GAVE_UP.  The parent completed in round 1: this is round 4, three
    extra rounds, each one re-steer the agents answer (the walk counts
    rounds; it runs no model, so model calls are not counted here)."""
    commits, states, steers, chat = walk(CHOICE, SEARCH_EMPTY + [ANSWER])
    assert commits and not states, (commits, states)
    assert walk.last.rounds == 4, walk.last.rounds
    assert len(steers) == 3 and all('@Helper call crawl4ai_crawl now' in s
                                    for s in steers), steers
    assert _cited(chat, commits) == ['google_search']
    # The commit's completion gate accepts it, on the budget REUSE spent.
    assert walk.last.kwargs[0].get('steers_spent') is True, walk.last.kwargs
    assert _commit_accepts(monkeypatch, chat, walk.last, commits[0]) is True


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


# -- rv_bc53: both choices empty, the re-steer's words, raw step text --------

def test_both_choices_coming_back_empty_completes_the_step(walk):
    """F1: an empty search and an empty crawl are an honest "nothing found":
    the step completes in one round, as the parent did with its one tool."""
    commits, states, steers, chat = walk(CHOICE, SEARCH_EMPTY + CRAWL_EMPTY + [ANSWER])
    assert commits and not states and not steers, (commits, states, steers)


def test_an_empty_search_and_a_failed_crawl_complete_on_the_search(walk):
    commits, states, _s, chat = walk(CHOICE, SEARCH_EMPTY + CRAWL_FAILED + [ANSWER])
    assert commits and not states and _cited(chat, commits) == ['google_search']


def test_the_completion_gate_counts_both_empty_choices_as_done(registry):
    """F1 for CREATE: _every_named_tool_worked (create_recipe's routing and
    finish_without_receipt) and the receipt check (_work_results)."""
    msgs = _window(CHOICE, SEARCH_EMPTY + CRAWL_EMPTY)
    named = ['google_search', 'crawl4ai_crawl']
    assert lh._every_named_tool_worked(msgs, len(msgs) - 1, 2, CHOICE, named)
    names = lh.tool_call_names([msgs])
    assert list(lh._work_results(msgs[2], names, CHOICE.lower(), named, msgs)) \
        == ['google_search']


def test_the_resteer_for_a_choice_says_either_will_do_and_what_is_left(walk):
    """F2: the search came back empty and the crawl was never called."""
    commits, states, steers, _c = walk(CHOICE, SEARCH_EMPTY + [ANSWER])
    steer = steers[0]
    assert 'either one will do' in steer, steer
    assert 'google_search came back empty' in steer, steer
    assert '@Helper call crawl4ai_crawl now' in steer, steer
    assert 'returned a failure' not in steer, steer


def test_the_resteer_for_a_tool_named_alone_is_unchanged(walk):
    commits, states, steers, _c = walk(ALONE, SEARCH + [ANSWER])
    assert steers[0].startswith('Action 2'), steers[0]
    assert 'the tool(s) crawl4ai_crawl did not produce a real result' in steers[0]
    assert 'either they were never called, or they ran and returned a failure' in steers[0]
    assert 'either one will do' not in steers[0]


SPELLED = 'fetch the latest prices with crawl4ai, or google search them'


def test_a_choice_spelled_out_in_the_steps_own_words_is_one_choice(registry, walk):
    """F3: from the raw step text, "google search" names google_search and is
    the other choice to crawl4ai: either tool's result completes the step."""
    assert _referenced(SPELLED) == ['crawl4ai_crawl', 'google_search']
    commits, states, _s, chat = walk(SPELLED, CRAWL + [ANSWER])
    assert commits and not states and _cited(chat, commits) == ['crawl4ai_crawl']
    commits, states, _s, chat = walk(SPELLED, SEARCH + [ANSWER])
    assert commits and not states and _cited(chat, commits) == ['google_search']


# -- hartos-52's review of e6808a611: one builder (F1), the budget (F2) -------

def test_both_empty_with_the_crawl_only_in_a_buffer_completes_and_commits(
        walk, monkeypatch):
    """F1: the empty crawl lives only in the Helper's buffer.  The gate and
    the receipt pick take the empty search, and the commit's completion gate
    reads the same lists (one builder), so it accepts it: on e6808a611 it
    refused it and REUSE logged "Cannot advance action ... aborting"."""
    commits, states, steers, chat = walk(CHOICE, SEARCH_EMPTY + [ANSWER],
                                         buffer=CRAWL_EMPTY)
    assert commits and not states and not steers, (commits, states, steers)
    assert walk.last.rounds == 1 and _cited(chat, commits) == ['google_search']
    assert _commit_accepts(monkeypatch, chat, walk.last, commits[0]) is True


def test_a_real_crawl_only_in_a_buffer_completes_on_the_crawl(walk, monkeypatch):
    """Unchanged from e6808a611: the crawl's real result, in the buffer, is
    the choice's; the empty search in the log is not."""
    commits, states, _s, chat = walk(CHOICE, SEARCH_EMPTY + [ANSWER], buffer=CRAWL)
    assert commits and not states and _cited(chat, commits) == ['crawl4ai_crawl']
    assert commits[0].get('source') == 'buffer', commits[0]
    assert _commit_accepts(monkeypatch, chat, walk.last, commits[0]) is True


def test_a_crawl_from_the_step_before_is_not_this_steps(walk):
    """Each list is held to this step's dispatch window: an empty crawl the
    step before ran does not make this step's empty search count, so the
    crawl is steered to (and the step completes once the steers are spent)."""
    before = [_dispatch(1, 'google_search: search the web for the topic'),
              _call('c0', 'crawl4ai_crawl', {'url': 'https://old.example'}),
              _result('c0', '{"markdown": "", "links": []}')]
    commits, states, steers, _c = walk(CHOICE, SEARCH_EMPTY + [ANSWER],
                                       before=before, seen={'c0'})
    assert '@Helper call crawl4ai_crawl now' in steers[0], steers
    assert walk.last.rounds == 4 and commits and not states, (walk.last, states)


def test_an_empty_search_then_a_crawl_after_the_steer_finishes_on_the_crawl(walk):
    commits, states, steers, chat = walk(CHOICE, SEARCH_EMPTY + [ANSWER],
                                         later={1: CRAWL})
    assert commits and not states and len(steers) == 1, (commits, states, steers)
    assert walk.last.rounds == 2 and _cited(chat, commits) == ['crawl4ai_crawl']


def test_both_failed_still_gives_up(walk):
    commits, states, _s, _c = walk(CHOICE, SEARCH_FAILED + CRAWL_FAILED + [ANSWER])
    assert not commits and states == ['GAVE_UP'], (commits, states)


# -- CREATE: its per-list read and its verifier hook --------------------------

def test_create_counts_both_empty_with_the_crawl_only_in_a_buffer(registry):
    """F1 for CREATE: _every_named_tool_worked reads what came back in every
    list of the group chat, by the one builder."""
    dispatch = _dispatch(2, CHOICE)
    agents = [_Agent('Helper', TOOLS), _Agent('Assistant', TOOLS)]
    agents[0]._oai_messages['Assistant'] = [dict(dispatch)] + CRAWL_EMPTY
    chat = _Chat([dispatch] + SEARCH_EMPTY, agents)
    named = ['google_search', 'crawl4ai_crawl']
    assert lh._every_named_tool_worked(chat.messages, len(chat.messages) - 1, 2,
                                       CHOICE, named, chat)


def test_create_holds_each_list_to_this_steps_window(registry):
    msgs = ([_dispatch(1, 'google_search: search the web for the topic'),
             _call('c0', 'crawl4ai_crawl', {'url': 'https://old.example'}),
             _result('c0', '{"markdown": "", "links": []}')]
            + _window(CHOICE, SEARCH_EMPTY))
    assert not lh._every_named_tool_worked(msgs, len(msgs) - 1, 2, CHOICE,
                                           ['google_search', 'crawl4ai_crawl'])


@pytest.fixture
def create_verdicts(registry, monkeypatch):
    """CREATE's verifier hook (lifecycle_hook_process_verifier_response) on
    action 2, one 'completed' verdict a round, the state writers stubbed:
    what each round returned, and the receipt each commit cited."""
    def _run(step, window, rounds=6, later=None, buffer=None,
             buffer_got_the_order=True):
        agents = [_Agent('Helper', TOOLS), _Agent('Assistant', TOOLS)]
        dispatch = _dispatch(2, step)
        chat = _Chat([dispatch] + window, agents)
        if buffer:
            agents[0]._oai_messages['Assistant'] = (
                [dict(dispatch)] if buffer_got_the_order else []) + list(buffer)
        monkeypatch.setattr(lh, 'get_registered_groupchat', lambda _s: chat)
        monkeypatch.setattr(lh, 'get_registered_ledger', lambda _u: SimpleNamespace(
            tasks={'action_2': SimpleNamespace(description=step)}))
        monkeypatch.setattr(lh, 'get_action_state',
                            lambda *a, **k: lh.ActionState.STATUS_VERIFICATION_REQUESTED)
        monkeypatch.setattr(lh, 'verdict_names_another_action', lambda *a, **k: None)
        cited = []
        monkeypatch.setattr(lh, '_record_verifier_evidence',
                            lambda up, aid, verdict: cited.append(verdict['evidence']) or True)
        for name in ('validate_state_transition', 'safe_set_state',
                     'force_state_through_valid_path'):
            monkeypatch.setattr(lh, name, lambda *a, **k: True)
        monkeypatch.setattr(lh, '_promote_verified_outcome', lambda *a, **k: None)
        lh.retry_tracker.reset_count(SESSION, 2)
        tasks = SimpleNamespace(current_action=2)
        actions = []
        try:
            for n in range(1, rounds + 1):
                got = lh.lifecycle_hook_process_verifier_response(
                    SESSION, {'status': 'completed', 'action_id': 2}, tasks)
                actions.append(got['action'])
                if got['action'] != 'force_completion':
                    break
                chat.messages.extend((later or {}).get(n, ()))
        finally:
            lh.retry_tracker.reset_count(SESSION, 2)
        return actions, cited, chat
    return _run


def _cited_tool(chat, evidence):
    msgs = (chat.agents[0]._oai_messages[evidence['peer']]
            if evidence.get('source') == 'buffer' else chat.messages)
    names = lh.tool_call_names([msgs])
    return [names.get(r.get('tool_call_id'))
            for r in msgs[evidence['message_index']].get('tool_responses') or []]


def test_create_completes_an_empty_search_once_its_rounds_are_spent(create_verdicts):
    """F2 for CREATE, on retry_tracker: three refusals, then the empty
    search counts and the fourth verdict commits; never gave_up.  The parent
    committed on the first verdict: three extra rounds."""
    actions, cited, chat = create_verdicts(CHOICE, SEARCH_EMPTY)
    assert actions == ['force_completion'] * 3 + ['force_fallback'], actions
    assert _cited_tool(chat, cited[0]) == ['google_search']


def test_create_finishes_on_a_crawl_called_after_the_first_refusal(create_verdicts):
    actions, cited, chat = create_verdicts(CHOICE, SEARCH_EMPTY, later={1: CRAWL})
    assert actions == ['force_completion', 'force_fallback'], actions
    assert _cited_tool(chat, cited[0]) == ['crawl4ai_crawl']


def test_create_still_gives_up_on_a_failed_search(create_verdicts):
    actions, _c, _ch = create_verdicts(CHOICE, SEARCH_FAILED)
    assert actions == ['force_completion'] * 3 + ['gave_up'], actions
    actions, _c, _ch = create_verdicts(CHOICE, SEARCH_FAILED + CRAWL_FAILED)
    assert actions == ['force_completion'] * 3 + ['gave_up'], actions


def test_create_commits_both_empty_with_the_crawl_only_in_a_buffer(create_verdicts):
    actions, cited, chat = create_verdicts(CHOICE, SEARCH_EMPTY, buffer=CRAWL_EMPTY)
    assert actions == ['force_fallback'], actions
    assert _cited_tool(chat, cited[0]) == ['google_search']
    # CREATE's speaker selection reads the same lists.
    assert lh.tool_result_is_action_work(SESSION, 2, 2)


# -- hartos-52 F1 / hartos-73 on eac3fe98e: a buffer that never got the order -

def test_both_empty_with_the_crawl_in_a_buffer_without_the_order_completes(
        walk, monkeypatch):
    """The empty crawl lives only in a buffer with no copy of the dispatch.
    The gate counted it (its call is not before the step's dispatch) and
    passed, the receipt pick refused the empty search, and the step gave up
    in round 1; the parent completed.  What came back now reads such a
    buffer on the same span, so the search is the receipt, in round 1."""
    commits, states, steers, chat = walk(CHOICE, SEARCH_EMPTY + [ANSWER],
                                         buffer=CRAWL_EMPTY,
                                         buffer_got_the_order=False)
    assert commits and not states and not steers, (commits, states, steers)
    assert walk.last.rounds == 1 and _cited(chat, commits) == ['google_search']
    assert _commit_accepts(monkeypatch, chat, walk.last, commits[0]) is True


def test_a_real_crawl_in_a_buffer_without_the_order_completes_on_the_search(
        walk, monkeypatch):
    """No gate can cite the crawl there (it is outside every dispatch
    window), so it is no receipt of the choice and the empty search is, as
    on the parent."""
    commits, states, steers, chat = walk(CHOICE, SEARCH_EMPTY + [ANSWER],
                                         buffer=CRAWL, buffer_got_the_order=False)
    assert commits and not states and not steers, (commits, states, steers)
    assert walk.last.rounds == 1 and _cited(chat, commits) == ['google_search']
    assert _commit_accepts(monkeypatch, chat, walk.last, commits[0]) is True


def test_create_commits_with_the_crawl_in_a_buffer_without_the_order(create_verdicts):
    actions, cited, chat = create_verdicts(CHOICE, SEARCH_EMPTY, buffer=CRAWL_EMPTY,
                                           buffer_got_the_order=False)
    assert actions == ['force_fallback'], actions
    assert _cited_tool(chat, cited[0]) == ['google_search']


def test_a_crawl_the_step_before_ran_in_a_buffer_is_not_this_steps(registry):
    """A call proposed in the step before's window, in a list that got the
    orders, is that step's, wherever its result lands."""
    log = ([_dispatch(1, 'google_search: search the web for the topic'),
            _call('c0', 'crawl4ai_crawl', {'url': 'https://old.example'})]
           + [_dispatch(2, CHOICE)] + SEARCH_EMPTY)
    buffer = [_result('c0', '{"markdown": "", "links": []}')]
    came = lh.step_results_came_back([log, [log[1]] + buffer], 2)
    assert came == {'google_search': False}, came


# -- the cost (hartos-52 F2, hartos-73 1): one pass per list, built once ------

def _long_log(n_pairs):
    msgs = [_dispatch(1, 'google_search: search the web for the topic')]
    for k in range(n_pairs):
        msgs += [_call(f'p{k}', 'google_search', {'query': 'q'}),
                 _result(f'p{k}', '1. https://a.example')]
    msgs.append(_dispatch(2, CHOICE))
    for k in range(n_pairs):
        msgs += [_call(f'g{k}', 'google_search', {'query': 'q'}),
                 _result(f'g{k}', EMPTY_SEARCH_RESULT)]
    return msgs


def test_the_step_windows_are_the_latest_dispatch_of_every_message():
    msgs = [{'role': 'user', 'content': 'hello'}, _dispatch(1, ALONE), 'not a dict',
            {'role': 'user', 'content': 'Perform this action -> Action #3: x',
             '_from_shared': True},
            _call('a', 'google_search', {}), _dispatch(2, CHOICE), _result('a', 'r')]
    assert lh._step_windows(msgs) == [lh.latest_dispatch_before(msgs, i + 1)
                                      for i in range(len(msgs))]


def test_what_came_back_reads_each_message_once(monkeypatch):
    """8 lists of 3,001 messages took 6 s: latest_dispatch_before copied the
    list for every tool result.  One forward pass reads each message once."""
    lists = [_long_log(750) for _ in range(8)]
    seen = []
    real = lh.dispatch_action_id
    monkeypatch.setattr(lh, 'dispatch_action_id', lambda c: seen.append(1) or real(c))
    assert lh.step_results_came_back(lists, 2) == {'google_search': False}
    assert len(seen) <= sum(len(m) for m in lists), len(seen)


def _count_builds(monkeypatch):
    builds = []
    real = lh.step_results_came_back
    monkeypatch.setattr(lh, 'step_results_came_back',
                        lambda *a, **k: builds.append(1) or real(*a, **k))
    return builds


def test_a_round_builds_what_came_back_once_in_the_gate_and_once_in_the_pick(
        walk, monkeypatch):
    """A real crawl, then 20 empty searches: the receipt pick judged each
    empty search and rebuilt what came back for every one of them (2.8 s a
    round on a 199-message log)."""
    window = CRAWL + [m for k in range(20) for m in (
        _call(f'g{k}', 'google_search', {'query': 'q'}), _result(f'g{k}', EMPTY_SEARCH_RESULT))]
    builds = _count_builds(monkeypatch)
    commits, states, _s, chat = walk(CHOICE, window + [ANSWER])
    assert commits and _cited(chat, commits) == ['crawl4ai_crawl']
    assert len(builds) <= 2, len(builds)


def test_a_create_verdict_builds_what_came_back_once(create_verdicts, monkeypatch):
    """derive_completion_evidence judged every candidate receipt and built
    what came back for each (1.28 s against the parent's 0.13 s)."""
    window = [m for k in range(20) for m in (
        _call(f'g{k}', 'google_search', {'query': 'q'}), _result(f'g{k}', EMPTY_SEARCH_RESULT))]
    builds = _count_builds(monkeypatch)
    actions, _c, _ch = create_verdicts(CHOICE, window, rounds=1)
    assert actions == ['force_completion'], actions
    assert len(builds) <= 1, len(builds)


# -- a step that waits for the reply has the same budget (hartos-73, minor 3) -

def test_an_empty_search_owed_after_the_reply_counts_once_the_steers_are_spent(registry):
    step = 'ask which page, then search with google_search or crawl4ai for it'
    agents = [_Agent('Helper', TOOLS), _Agent('Assistant', TOOLS)]
    chat = _Chat([_dispatch(2, step), {'role': 'user', 'name': 'User', 'content': 'page two'}]
                 + SEARCH_EMPTY, agents)
    named = ['crawl4ai_crawl', 'google_search']
    assert lh.tools_owed_after_reply(chat, step, named, []) == named
    assert lh.tools_owed_after_reply(chat, step, named, [], steers_spent=True) == []


# -- hartos-52's rv_gate3 on 798b2bada: REUSE's watermark in such buffers -----

OLD_CRAWL = [_call('c_old', 'crawl4ai_crawl', {'url': 'https://old.example'}),
             _result('c_old', '{"markdown": "", "links": []}')]


@pytest.mark.parametrize('where', ['another_request', 'another_seat',
                                   'step_one_window', 'seeded'])
def test_an_older_crawl_in_a_buffer_without_the_order_is_not_this_steps(walk, where):
    """A crawl call already on the wire when the step was dispatched (in
    REUSE's watermark) is no call of this step, in a buffer with no copy of
    its order: the empty search is steered to the crawl, three times, as on
    4dd804f7d, and does not commit in round 1."""
    buffers = {
        'another_request': [(0, 'Assistant', OLD_CRAWL)],
        'another_seat': [(1, 'Helper', OLD_CRAWL)],
        'step_one_window': [(0, 'Assistant',
                             [_dispatch(1, 'google_search: search the web for the topic')]
                             + OLD_CRAWL)],
        'seeded': [(0, 'Assistant', [dict(m, _from_shared=True) for m in OLD_CRAWL])],
    }[where]
    commits, states, steers, _c = walk(CHOICE, SEARCH_EMPTY + [ANSWER],
                                       buffers=buffers, seen={'c_old'})
    assert len(steers) == 3 and '@Helper call crawl4ai_crawl now' in steers[0], steers
    assert walk.last.rounds == 4 and commits and not states, (walk.last, states)


def test_a_crawl_in_a_buffer_without_the_order_never_hides_a_real_one(registry):
    """What such a buffer adds never replaces what came back in the window:
    the real crawl in the window stays real work."""
    log = _window(CHOICE, SEARCH_EMPTY + CRAWL)
    buffer = [_call('c9', 'crawl4ai_crawl', {'url': 'https://b.example'}),
              _result('c9', '{"markdown": "", "links": []}')]
    came = lh.step_results_came_back([log, buffer], 2)
    assert came == {'google_search': False, 'crawl4ai_crawl': True}, came


def test_a_placeholder_in_a_buffer_without_the_order_is_no_result(registry):
    """The stand-in helper.py writes for a call that returned nothing is not
    a tool that came back."""
    from core.constants import HISTORICAL_TOOL_PLACEHOLDER
    log = _window(CHOICE, SEARCH_EMPTY)
    buffer = [_call('c9', 'crawl4ai_crawl', {'url': 'https://b.example'}),
              _result('c9', HISTORICAL_TOOL_PLACEHOLDER)]
    came = lh.step_results_came_back([log, buffer], 2)
    assert came == {'google_search': False}, came


def test_the_receipt_pick_reads_the_same_watermark(walk):
    """The receipt pick reads such a buffer as the gate does: with the
    older crawl in the watermark, the empty search is no receipt while the
    crawl is untried and the steers are not spent."""
    import hartos.reuse_recipe as rr
    commits, states, steers, chat = walk(CHOICE, SEARCH_EMPTY + [ANSWER], rounds=1,
                                         buffers=[(0, 'Assistant', OLD_CRAWL)],
                                         seen={'c_old'})
    assert not commits and len(steers) == 1, (commits, steers)
    assert rr._reuse_completion_evidence(SESSION, 2, chat) is None


def test_a_seeded_result_in_a_buffer_without_the_order_never_counts(registry):
    """A seeded (_from_shared) message is an earlier run's, for a reader
    with no watermark too (the commit's gate, CREATE)."""
    log = _window(CHOICE, SEARCH_EMPTY)
    shared = [dict(m, _from_shared=True) for m in OLD_CRAWL]
    assert lh.step_results_came_back([log, shared], 2) == {'google_search': False}


# -- hartos-52's rv_2569, finding 1: every mention must be inside the "or" ---

RETRY = 'run google_search and crawl4ai; if google_search or crawl4ai fails, retry it'


@pytest.mark.parametrize('text', [
    RETRY,
    'google_search the price, compare google_search or crawl4ai results, then crawl4ai the winner',
    'search with google_search or crawl4ai, then google_search the brand',
    'search with google_search or crawl4ai, then crawl4ai the top page',
])
def test_an_or_is_a_choice_only_when_every_mention_is_inside_it(registry, text):
    """A tool the step also names outside its "or" join is owed on its own:
    take the joins out, and neither tool may be named in what is left (the
    "every mention" rule, rv_942a S4)."""
    choices = lh.named_tool_choices(text, ['google_search', 'crawl4ai_crawl'])
    assert sorted(sorted(c) for c in choices) == [['crawl4ai_crawl'], ['google_search']], choices


def test_a_step_needing_both_is_not_completed_by_the_search_alone(walk):
    """RETRY needs both tools: on HEAD the "or" in its retry clause let the
    search alone complete it (unrun=[]), a completion that did not happen
    (the 09-14 ruling).  The crawl is owed, as on 2569beaf0's parent."""
    commits, states, steers, _c = walk(RETRY, SEARCH + [ANSWER])
    assert not commits and states == ['GAVE_UP'], (commits, states)
    assert 'crawl4ai_crawl' in steers[0] and 'google_search' not in steers[0], steers[0]


def test_a_step_needing_both_is_not_completed_by_the_crawl_alone(walk):
    commits, states, steers, _c = walk(RETRY, CRAWL + [ANSWER])
    assert not commits and states == ['GAVE_UP'], (commits, states)
    assert 'google_search' in steers[0], steers[0]


def test_create_owes_both_tools_of_a_step_that_needs_both(registry):
    msgs = _window(RETRY, SEARCH)
    assert not lh._every_named_tool_worked(msgs, len(msgs) - 1, 2, RETRY,
                                           ['google_search', 'crawl4ai_crawl'])
    msgs = _window(RETRY, SEARCH + CRAWL)
    assert lh._every_named_tool_worked(msgs, len(msgs) - 1, 2, RETRY,
                                       ['google_search', 'crawl4ai_crawl'])
