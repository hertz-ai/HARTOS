"""A step whose words name its tool is a tool step, even when its banked
recipe field is empty; and when it asked the person, what its words name is
owed after their reply.

WHAT WENT WRONG (live 2026-11-21 12:43-12:47 IST, speech agent 1794540972213,
build f5d2f7b04, frozen_debug.log.old): every step of the agent's flow 0 is
banked ``recipe: []``.  The fabrication gate reads a step's tools from its
words, but the no-output rule asked _reuse_action_declares_tool, which read
only the recipe field, so these steps read as text steps:

    12:46:50  save_data_in_memory(key='name_response', ...) ran
    12:46:53  [FABRICATED-COMPLETE] refusing to advance action 4: it names no
              tool, so its deliverable is the text itself
    steer     "This action calls no tool -- its result IS the text you write"

Action 2 ("save_data_in_memory: ...; then send_message_to_user '<child_name>!'
... and wait for the child to respond") was refused the same way at 12:43:24
and 12:43:32 with both its tools run, sent the child a filler message to get
past it, and its spent re-steers gave it up on the next turns.

THE RULE: _reuse_action_declares_tool also reads the step's words, through
_reuse_step_referenced_tools, the reader the fabrication gate and the receipt
finder share.  hartos-52's review: that moves such a step from the
written-answer rule to the tool gate alone, so a drill ("send 'say ball',
wait for the child, save the attempt") would complete on a save made before
the child tried.  So a step named in its words only, once it asked the person
this run, is held to lifecycle_hooks.tools_owed_after_reply, the rule an ask
step's service-named tools already follow: with its question out it waits,
and on the reply turn only a tool that ran after the reply completes it.

Real code: REUSE's _advance_reuse_action with its real gate, readers and
receipt pick; the ledger reads, the state writers, the wait and the commit
are recorded, not run.

    python -m pytest tests/unit/test_a_step_naming_its_tool_in_its_words_is_a_tool_step.py -q --noconftest
"""
import json
from types import SimpleNamespace

import pytest

import hartos.lifecycle_hooks as lh
import integrations.service_tools.registry as reg_mod
from integrations.service_tools.registry import ServiceToolRegistry

SESSION = 'ead7c07d_words_1794540972213'
TOOLS = ['get_data_by_key', 'save_data_in_memory', 'send_message_to_user']

# Live flow 0, action 4, verbatim (prompts/1794540972213_0_recipe.json).
SAVE_STEP = ("save_data_in_memory: key 'name_response', the value is what the "
             "child did when called (their own sound or word, 'turned', or 'no "
             "response'), never the agent's words")
# hartos-52's drill: the save belongs AFTER the child's try.
DRILL_STEP = ("send_message_to_user 'say ball' to the child and wait for the "
              "child to try it, then save_data_in_memory the attempt under the "
              "key 'ball_attempt'")
TEXT_STEP = 'Tell the parent in one sentence how the practice went'

ASK = {'request_id': 'ask_req', 'via': 'send', 'question': 'say ball',
       'asked': 'say ball'}
REPLY = 'baa'


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
    def __init__(self, actions, current):
        self.actions = list(actions)
        self.current_action = current
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


def _dispatch(aid, step):
    return {'role': 'user', 'name': 'ChatInstructor',
            'content': f'Perform this action -> Action #{aid}:{step}\n follow these steps: []'}


def _seed(aid, step):
    """The reply turn's seed, as _reuse_seed_message posts it: a dispatch
    that carries the person's words (live 12:46:06, action 3)."""
    return {'role': 'user', 'name': 'User',
            'content': (f'Perform this action -> Action #{aid}:{step}\n\n'
                        "This step's question was already delivered to the "
                        'person and is waiting for their answer.\n'
                        f"The question: say ball\nTheir words:\n{REPLY}\n"
                        ' follow these steps: []')}


def _save(cid, value):
    return [_call(cid, 'save_data_in_memory', {'key': 'ball_attempt', 'value': value}),
            _result(cid, f'Saved at ball_attempt: "{value}"')]


def _ask(cid='s1'):
    return [_call(cid, 'send_message_to_user', {'text': 'say ball', 'expects_reply': True}),
            _result(cid, 'Message sent successfully to user with request_id: ask_req')]


def _verdict(aid):
    return {'role': 'user', 'name': 'StatusVerifier',
            'content': json.dumps({'status': 'completed', 'action_id': aid})}


@pytest.fixture
def walk(monkeypatch):
    flask = pytest.importorskip('flask')
    import hartos.reuse_recipe as rr
    reg = ServiceToolRegistry.__new__(ServiceToolRegistry)
    reg._tools = {}
    monkeypatch.setattr(reg_mod, 'service_tool_registry', reg)
    app = flask.Flask('words_walk')

    def _walk(messages, step, *, aid=2, request_id, open_ask=None,
              calls_before=None, rounds=4, recipe=()):
        actions = [{'action': "send_message_to_user: get the child's name",
                    'recipe': [], 'can_perform_without_user_input': 'yes'},
                   {'action': step, 'recipe': list(recipe),
                    'can_perform_without_user_input': 'yes'},
                   {'action': TEXT_STEP, 'recipe': [],
                    'can_perform_without_user_input': 'yes'}]
        chat = _Chat(messages)
        asks = {'open': open_ask}
        monkeypatch.setattr(lh, 'get_registered_groupchat', lambda _s: chat)
        monkeypatch.setattr(rr, 'ledger_open_ask', lambda *a, **k: asks['open'])
        monkeypatch.setattr(lh, 'ledger_open_ask', lambda *a, **k: asks['open'])

        def _record_ask(up, a, rid, idx, flow, via=None, question=None, **k):
            if asks['open'] is None:
                asks['open'] = {'request_id': rid, 'via': via, 'question': question}
            return True
        monkeypatch.setattr(rr, 'record_ask_delivered', _record_ask)
        monkeypatch.setattr(rr, 'recorded_answers', lambda *a, **k: [])
        waits = []
        monkeypatch.setattr(rr, '_reuse_wait_for_the_person',
                            lambda up, a, why: waits.append(why) or True)
        monkeypatch.setitem(rr.recipes, SESSION, {'actions': actions})
        monkeypatch.setitem(rr.user_tasks, SESSION, _Task(actions, aid))
        monkeypatch.setattr(rr, '_reuse_request_is_a_person', lambda _r: True)
        monkeypatch.setitem(rr.request_id_list, SESSION, request_id)
        if calls_before is not None:
            monkeypatch.setitem(rr._reuse_pending_reply, SESSION, {
                'action_id': aid, 'request_id': request_id, 'reply': REPLY,
                'seed_text': _seed(aid, step)['content'], 'flow': 0,
                'calls_before': calls_before})
        else:
            rr._reuse_pending_reply.pop(SESSION, None)
        states, commits = [], []
        monkeypatch.setattr(rr, 'force_state_through_valid_path',
                            lambda up, a, st, why='', **k: states.append(st.name) or True)
        monkeypatch.setattr(rr, 'safe_set_state', lambda *a, **k: True)
        monkeypatch.setattr(rr, 'commit_verified_action_completion',
                            lambda up, a, ev, *x, **k: commits.append(ev) or True)
        for name in ('_stamp_action_evidence_watermark', '_stamp_action_result_vacuity',
                     '_start_reuse_action'):
            monkeypatch.setattr(rr, name, lambda *a, **k: None)
        rr._reuse_resteer_counts.pop((SESSION, aid), None)
        rr._reuse_fab_pending.pop((SESSION, aid), None)
        steers, pending = [], []
        with app.app_context():
            for _ in range(rounds):
                got = rr._advance_reuse_action(SESSION, aid, 'words', '1794540972213')
                pending.append(list(rr._reuse_fab_pending.get((SESSION, aid)) or []))
                steers.append(rr._reuse_fab_steer_message(SESSION, aid))
                if got != (None, False) or states or commits or waits:
                    break
        return SimpleNamespace(commits=commits, states=states, waits=waits,
                               steers=[s for s in steers if s], pending=pending,
                               chat=chat, asks=asks, rr=rr)
    return _walk


def _no_text_steer(run):
    return not any(run.rr._REUSE_NO_OUTPUT_SENTINEL in p for p in run.pending)


def test_the_live_save_step_completes_on_its_save(walk):
    """Live action 4: its save ran, so the save is its receipt.  On the parent
    it was refused as a text step that 'calls no tool'."""
    run = walk([_dispatch(2, SAVE_STEP), *_save('v1', 'turned'), _verdict(2)],
               SAVE_STEP, request_id='turn2_req')
    assert _no_text_steer(run), run.pending
    assert len(run.commits) == 1 and not run.states and not run.waits, vars(run)
    assert run.commits[0]['kind'] == 'tool_receipt', run.commits


def test_with_its_question_out_a_save_before_the_reply_does_not_complete_it(walk):
    """The drill asked and saved an attempt before the child tried: the step
    records its ask and waits; nothing completes it and nothing steers it."""
    run = walk([_dispatch(2, DRILL_STEP), *_ask('s1'), *_save('v1', 'ball'),
                _verdict(2)], DRILL_STEP, request_id='ask_req')
    assert run.waits and not run.commits and not run.states, vars(run)
    assert not run.steers, run.steers
    assert run.asks['open'] and run.asks['open']['via'] == 'send', run.asks


def test_the_reply_with_its_save_only_before_it_does_not_complete_it(walk):
    """On the reply turn the only save ran before the reply: the save is owed
    again, and the steer names it (not 'write the text')."""
    run = walk([_dispatch(2, DRILL_STEP), *_ask('s1'), *_save('v1', 'ball'),
                _seed(2, DRILL_STEP), _verdict(2)], DRILL_STEP,
               request_id='reply_req', open_ask=dict(ASK), calls_before=['s1', 'v1'])
    assert not run.commits, run.commits
    assert _no_text_steer(run), run.pending
    assert run.pending[0] == ['save_data_in_memory'], run.pending


def test_the_reply_then_its_save_completes_it(walk):
    """The child tried, then the attempt was saved: the save after the reply is
    the receipt."""
    msgs = [_dispatch(2, DRILL_STEP), *_ask('s1'), _seed(2, DRILL_STEP),
            *_save('v2', REPLY), _verdict(2)]
    run = walk(msgs, DRILL_STEP, request_id='reply_req', open_ask=dict(ASK),
               calls_before=['s1'])
    assert len(run.commits) == 1 and not run.states and not run.waits, vars(run)
    receipt = run.commits[0]
    assert receipt['kind'] == 'tool_receipt', receipt
    assert run.chat.messages[receipt['message_index']]['tool_responses'][0][
        'tool_call_id'] == 'v2', receipt


def test_a_step_whose_recipe_names_its_tool_is_judged_as_before(walk):
    """Scope: only a step named in its words alone changed.  A step whose
    banked recipe declares its tool was a tool step on the parent too, and
    it is judged exactly as there: with its question out, its save is its
    receipt.  (Whether such a step should also wait for the reply is the
    owner's ruling to make; this fix does not change it.)"""
    run = walk([_dispatch(2, DRILL_STEP), *_ask('s1'), *_save('v1', 'ball'),
                _verdict(2)], DRILL_STEP, request_id='ask_req',
               recipe=[{'steps': 'save_data_in_memory({"key": "ball_attempt"})',
                        'tool_name': 'save_data_in_memory'}])
    assert len(run.commits) == 1 and not run.waits, vars(run)


def test_a_step_whose_words_name_no_tool_keeps_the_no_output_rule(walk):
    """No regression: a text step that wrote nothing is still refused, even
    when the chat's agents serve tools."""
    run = walk([_dispatch(2, TEXT_STEP), _verdict(2)], TEXT_STEP,
               request_id='turn2_req')
    assert not run.commits, run.commits
    assert run.pending[0] == [run.rr._REUSE_NO_OUTPUT_SENTINEL], run.pending
