"""A step flagged 'no' that names a tool holds for the person's reply, and
completes only once that tool has really run after the reply.

WHAT WENT WRONG (rv_b06b S6): b06b29405 made "crawl4ai: fetch ..." name
crawl4ai_crawl for the completion check.  A step flagged 'no' (it waits on
the person) whose words name crawl4ai that way stopped being an ask step, so
it lost its hold for the reply: with its question out, the gate re-steered
it to crawl and then gave it up.  The parent held it, and then let the
person's reply complete it with no crawl at all.

THE RULE (owner 20 Nov 2026, "Everything should work the way it should, no
compromise"; option (b)):
  * such a step holds for the person's reply exactly as the parent did
    (reuse_recipe._reuse_ask_only_step: a tool named only through its service
    name, lifecycle_hooks.service_named_functions, leaves it an ask step);
  * it completes only once the named tool has really run AFTER the reply
    (the 09-14 ruling: never record a completion that did not happen).  ONE
    rule, lifecycle_hooks.tools_owed_after_reply, read the same way by
    REUSE's fabrication gate on the reply turn, its receipt pick
    (_reuse_completion_evidence) and the completion gate
    (_user_reply_is_the_evidence).  "After" is by tool call: the calls made
    before the reply arrived are recorded with it.

Live shape: agents 1794818852965 and 1795013039459, step 2 "crawl4ai: fetch
the first relevant result URL to extract clean markdown content", flagged
'no - the person answers this step'.

Real code: REUSE's _advance_reuse_action with its real gate, ask-step rule and
receipt pick; the ledger reads, the state writers, the wait and the commit
are recorded, not run.  The completion gate's own reply rule is run on the
evidence REUSE would commit.

    python -m pytest tests/unit/test_a_no_step_naming_a_tool_holds_then_needs_the_tool_after_the_reply.py -q --noconftest
"""
import json
from types import SimpleNamespace

import pytest

import hartos.lifecycle_hooks as lh
import integrations.service_tools.registry as reg_mod
from integrations.service_tools.crawl4ai_tool import Crawl4AITool
from integrations.service_tools.registry import ServiceToolRegistry

SESSION = 'nostep_user_1794818852965'
STEP = ('crawl4ai: fetch the first relevant result URL to extract clean '
        'markdown content')
NO_TOOL_STEP = 'Ask the person which page they want summarised'
SEED = "The person's reply to your question is below.\n\nthe second link please"
REPLY = 'the second link please'
TOOLS = ['google_search', 'crawl4ai_crawl', 'send_message_to_user']
ASK = {'request_id': 'ask_req', 'via': 'send', 'question': 'Which link?',
       'asked': 'Which link?'}


class _Agent:
    def __init__(self, name):
        self.name = name
        self._function_map = {t: (lambda **kw: None) for t in TOOLS}
        self.llm_config = {'tools': [{'function': {'name': t}} for t in TOOLS]}
        self._oai_messages = {}


class _Chat:
    def __init__(self, messages):
        self.messages = list(messages)
        self.agents = [_Agent('Helper'), _Agent('Assistant')]


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


def _dispatch(step):
    return {'role': 'user', 'name': 'ChatInstructor',
            'content': f'Perform this action -> Action #2: {step}'}


def _crawl(cid):
    return [_call(cid, 'crawl4ai_crawl', {'url': 'https://b.example'}),
            _result(cid, '# Page two\n' + 'word ' * 300)]


def _ask(cid='s1'):
    return [_call(cid, 'send_message_to_user',
                  {'text': 'Which link should I read, the first or the second?',
                   'expects_reply': True}),
            _result(cid, 'Message sent successfully to user with request_id: ask_req')]


SEED_MSG = {'role': 'user', 'name': 'User', 'content': SEED}
VERDICT = {'role': 'user', 'name': 'StatusVerifier',
           'content': '{"status": "completed", "action_id": 2}'}


@pytest.fixture
def walk(monkeypatch):
    flask = pytest.importorskip('flask')
    import hartos.reuse_recipe as rr
    reg = ServiceToolRegistry.__new__(ServiceToolRegistry)
    reg._tools = {'crawl4ai': Crawl4AITool.create_tool_info()}
    reg.create_endpoint_function = lambda t, e: (lambda **kw: f'{t}.{e} ran')
    monkeypatch.setattr(reg_mod, 'service_tool_registry', reg)
    app = flask.Flask('nostep_walk')

    def _walk(messages, *, step=STEP, request_id, open_ask=None, calls_before=None,
              rounds=4):
        actions = [{'action': 'google_search: find pages on the topic',
                    'can_perform_without_user_input': 'yes'},
                   {'action': step,
                    'can_perform_without_user_input': 'no - the person answers this step'}]
        chat = _Chat(messages)
        asks = {'open': open_ask}
        monkeypatch.setattr(lh, 'get_registered_groupchat', lambda _s: chat)
        monkeypatch.setattr(rr, 'ledger_open_ask', lambda *a, **k: asks['open'])
        monkeypatch.setattr(lh, 'ledger_open_ask', lambda *a, **k: asks['open'])

        def _record_ask(up, aid, rid, idx, flow, via=None, question=None, **k):
            asks['open'] = {'request_id': rid, 'via': via, 'question': question}
            return True
        monkeypatch.setattr(rr, 'record_ask_delivered', _record_ask)
        monkeypatch.setattr(rr, 'recorded_answers', lambda *a, **k: [])
        waits = []
        monkeypatch.setattr(rr, '_reuse_wait_for_the_person',
                            lambda up, aid, why: waits.append(why) or True)
        monkeypatch.setitem(rr.recipes, SESSION, {'actions': actions})
        monkeypatch.setitem(rr.user_tasks, SESSION, _Task(actions))
        monkeypatch.setattr(rr, '_reuse_request_is_a_person', lambda _r: True)
        monkeypatch.setitem(rr.request_id_list, SESSION, request_id)
        if calls_before is not None:
            monkeypatch.setitem(rr._reuse_pending_reply, SESSION, {
                'action_id': 2, 'request_id': request_id, 'reply': REPLY,
                'seed_text': SEED, 'flow': 0, 'calls_before': calls_before})
        else:
            rr._reuse_pending_reply.pop(SESSION, None)
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
                got = rr._advance_reuse_action(SESSION, 2, 'nostep', '1794818852965')
                steers.append(rr._reuse_fab_steer_message(SESSION, 2))
                if got != (None, False) or states or commits or waits:
                    break
        return SimpleNamespace(commits=commits, states=states, waits=waits,
                               steers=[s for s in steers if s], chat=chat, asks=asks,
                               rr=rr)
    return _walk


def _gate_accepts(run, evidence, step=STEP, monkeypatch=None):
    """The completion gate's own reply rule on the evidence REUSE commits."""
    import integrations.agent_engine.dispatch as dispatch
    monkeypatch.setattr(dispatch, 'is_genuine_user_request', lambda rid: True)
    monkeypatch.setattr(lh, 'get_registered_ledger', lambda _u: SimpleNamespace(
        tasks={'action_2': SimpleNamespace(description=step)}))
    message = run.chat.messages[evidence['message_index']]
    return lh._user_reply_is_the_evidence(SESSION, 2, evidence, message, None)


def test_with_its_question_out_it_holds_for_the_person(walk):
    """The verifier calls it done while its question is out and nothing ran:
    it holds for the reply, as on the parent, and is not sent to crawl."""
    run = walk([_dispatch(STEP), *_ask(), VERDICT], request_id='ask_req')
    assert run.waits and not run.commits and not run.states, vars(run)
    assert not run.steers, run.steers


def test_a_crawl_without_a_reply_holds_for_the_ask(walk):
    run = walk([_dispatch(STEP), *_crawl('c1'), *_ask(), VERDICT],
               request_id='ask_req')
    assert run.waits and not run.commits and not run.states, vars(run)


def test_the_reply_then_the_crawl_completes_it_on_the_reply(walk, monkeypatch):
    run = walk([_dispatch(STEP), *_ask(), SEED_MSG, *_crawl('c2'), VERDICT],
               request_id='reply_req', open_ask=dict(ASK), calls_before=['s1'])
    assert len(run.commits) == 1 and not run.states, vars(run)
    evidence = run.commits[0]
    assert evidence['kind'] == 'user_reply' and evidence['reply'] == REPLY
    assert _gate_accepts(run, evidence, monkeypatch=monkeypatch) is True


def test_the_reply_with_no_crawl_since_does_not_complete_it(walk, monkeypatch):
    """The crawl ran before the reply: the reply alone does not complete the
    step; the model is sent to crawl, and the completion gate refuses the
    reply too."""
    run = walk([_dispatch(STEP), *_crawl('c1'), *_ask(), SEED_MSG, VERDICT],
               request_id='reply_req', open_ask=dict(ASK), calls_before=['c1', 's1'])
    assert not run.commits, run.commits
    assert run.steers and 'crawl4ai_crawl' in run.steers[0], run.steers
    seed_index = run.chat.messages.index(SEED_MSG)
    evidence = {'message_index': seed_index, 'kind': 'user_reply',
                'request_id': 'reply_req', 'reply': REPLY, 'flow': 0,
                'calls_before': ['c1', 's1']}
    assert _gate_accepts(run, evidence, monkeypatch=monkeypatch) is False


def test_the_receipt_pick_takes_no_reply_before_the_tool_ran_after_it(walk):
    """The receipt pick reads the same rule as the gate: on the reply turn,
    with no crawl since the reply, it offers no receipt at all."""
    import flask
    run = walk([_dispatch(STEP), *_crawl('c1'), *_ask(), SEED_MSG, VERDICT],
               request_id='reply_req', open_ask=dict(ASK), calls_before=['c1', 's1'])
    with flask.Flask('nostep_pick').app_context():
        assert run.rr._reuse_completion_evidence(SESSION, 2, run.chat) is None


def test_a_reply_recorded_without_its_calls_owes_every_named_tool(walk, monkeypatch):
    run = walk([_dispatch(STEP), *_ask(), SEED_MSG, *_crawl('c2'), VERDICT],
               request_id='reply_req', open_ask=dict(ASK), calls_before=['s1'])
    seed_index = run.chat.messages.index(SEED_MSG)
    evidence = {'message_index': seed_index, 'kind': 'user_reply',
                'request_id': 'reply_req', 'reply': REPLY, 'flow': 0}
    assert _gate_accepts(run, evidence, monkeypatch=monkeypatch) is False


def test_a_no_step_naming_no_tool_is_still_completed_by_the_reply(walk, monkeypatch):
    run = walk([_dispatch(NO_TOOL_STEP), *_ask(), SEED_MSG, VERDICT],
               step=NO_TOOL_STEP, request_id='reply_req', open_ask=dict(ASK),
               calls_before=['s1'])
    assert len(run.commits) == 1 and run.commits[0]['kind'] == 'user_reply', vars(run)
    assert _gate_accepts(run, run.commits[0], step=NO_TOOL_STEP,
                         monkeypatch=monkeypatch) is True


def test_the_reply_records_the_calls_made_before_it(walk, monkeypatch):
    """The reply turn's seed (_reuse_seed_message) records the calls already
    made when the reply arrived, and _reuse_reply_evidence hands them on."""
    import flask
    run = walk([_dispatch(STEP), *_crawl('c1'), *_ask()], request_id='ask_req')
    rr = run.rr
    monkeypatch.setattr(rr, 'get_registered_groupchat', lambda _s: run.chat)
    monkeypatch.setattr(rr, 'ledger_open_ask', lambda *a, **k: dict(ASK))
    monkeypatch.setitem(rr.request_id_list, SESSION, 'reply_req')
    for name in ('_reuse_camera_window', '_reuse_voice_window'):
        monkeypatch.setattr(rr, name, lambda *a, **k: '')
    monkeypatch.setattr(rr, '_build_reuse_action_message',
                        lambda up, aid, user_words='': f'seed: {user_words}')
    rr._reuse_pending_reply.pop(SESSION, None)
    with flask.Flask('nostep_seed').app_context():
        seeded = rr._reuse_seed_message(SESSION, REPLY)
    pending = rr._reuse_pending_reply.get(SESSION)
    assert pending and pending['calls_before'] == ['c1', 's1'], pending
    run.chat.messages.append({'role': 'user', 'name': 'User', 'content': seeded})
    ev = rr._reuse_reply_evidence(SESSION, 2, run.chat.messages)
    assert ev and ev['calls_before'] == ['c1', 's1'], ev
