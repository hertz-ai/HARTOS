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
finder share, for a SAVE: a save's result is nothing the person reads, so
its step can have no text to write.  Any other tool named in the words alone
leaves the step under the text rule, as on the parent (rv_2da7 F2: a step
"get_data_by_key ..., then tell the parent ..." still owes the sentence).
hartos-52's review: that moves a save step from the written-answer rule to
the tool gate alone, so a drill ("send 'say ball', wait for the child, save
the attempt") would complete on a save made before the child tried.  So such
a step, once it asked the person this run, is held to
lifecycle_hooks.tools_owed_after_reply, the rule an ask step's service-named
tools already follow: on a person's turn (by the turn's OWN request id) it
waits with its question out, and on the reply turn only a tool that ran after
the reply completes it.  On a turn nobody can answer it stays under the text
rule, as on the parent (rv_2da7 F1).

WHAT THAT RULE DOES NOT STOP (rv_a146's review of a146c71e8, finding 1): the
text rule refuses such a step only while the model has written nothing.  Once
any line is written on a turn nobody can answer, the receipt finder takes the
save made before an answer and the step completes.  The parent did the same,
and so did the tree before 2da7dbd88.  It is pinned below as a known gap, not
fixed: holding it needs a new rule, which is the owner's to give.

WHOSE TURN IT IS (rv_a146 finding 3): the gate reads it once, from the turn's
own request id (reuse_recipe._reuse_turn_request_id), for the ask-step hold,
this hold, the text-rule exemption and the id the ask is recorded under.

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
TOOLS = ['get_data_by_key', 'save_data_in_memory', 'send_message_to_user',
         'crawl4ai_crawl']

# Live flow 0, action 4, verbatim (prompts/1794540972213_0_recipe.json).
SAVE_STEP = ("save_data_in_memory: key 'name_response', the value is what the "
             "child did when called (their own sound or word, 'turned', or 'no "
             "response'), never the agent's words")
# hartos-52's drill: the save belongs AFTER the child's try.
DRILL_STEP = ("send_message_to_user 'say ball' to the child and wait for the "
              "child to try it, then save_data_in_memory the attempt under the "
              "key 'ball_attempt'")
TEXT_STEP = 'Tell the parent in one sentence how the practice went'
# Ask steps (their words name no tool but the send, or one more through its
# service name): held by the ask-step rule, not the save rule.
ASK_STEP = 'send_message_to_user: ask the child which word they want to practise'
ASK_CRAWL_STEP = ("send_message_to_user 'which link?' to the person, then "
                  "crawl4ai: fetch the page they pick")

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


def _written(text):
    return {'role': 'assistant', 'name': 'Assistant', 'content': text}


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
              calls_before=None, rounds=4, recipe=(), session_request_id=None,
              bound=True):
        actions = [{'action': "send_message_to_user: get the child's name",
                    'recipe': [], 'can_perform_without_user_input': 'yes'},
                   {'action': step, 'recipe': list(recipe),
                    'can_perform_without_user_input': 'yes'},
                   {'action': TEXT_STEP, 'recipe': [],
                    'can_perform_without_user_input': 'yes'}]
        chat = _Chat(messages)
        asks = {'open': open_ask, 'recorded_under': []}
        monkeypatch.setattr(lh, 'get_registered_groupchat', lambda _s: chat)
        monkeypatch.setattr(rr, 'ledger_open_ask', lambda *a, **k: asks['open'])
        monkeypatch.setattr(lh, 'ledger_open_ask', lambda *a, **k: asks['open'])

        def _record_ask(up, a, rid, idx, flow, via=None, question=None, **k):
            asks['recorded_under'].append(rid)
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
        # A person's turn carries an id that is not a daemon's; an empty id is
        # nobody's (core.agent_tools.is_a_persons_request, the same rule for
        # these ids, without its config reads).
        monkeypatch.setattr(rr, '_reuse_request_is_a_person', lambda r: (
            bool(r) and not str(r).startswith('daemon_')))
        # request_id_list is the session's latest id (another arrival can
        # rewrite it); the turn's own id is what chat_agent bound.
        monkeypatch.setitem(rr.request_id_list, SESSION,
                            request_id if session_request_id is None else session_request_id)
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
        import contextlib
        import core.llm_outbound_logger as lol
        # ``bound=False``: no turn is bound at all (code run outside
        # chat_agent), which is not the same as a turn bound with an empty id.
        with app.app_context(), (lol.request_id_context(request_id) if bound
                                 else contextlib.nullcontext()):
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


def _gate_accepts(run, step, monkeypatch, aid=2):
    """The real commit gate (lifecycle_hooks
    ._verifier_completion_has_conversation_evidence) on the evidence REUSE
    committed, the step's own words read from its ledger task."""
    monkeypatch.setattr(lh, 'get_registered_ledger', lambda _u: SimpleNamespace(
        tasks={f'action_{aid}': SimpleNamespace(description=step)}))
    return lh._verifier_completion_has_conversation_evidence(
        SESSION, aid, {'evidence': run.commits[0]})


def test_the_commit_gate_takes_the_save_steps_receipts(walk, monkeypatch):
    """rv_2da7 F3: the commit gate itself, not the stub, accepts what this
    class commits: the live save step's save, and the drill's save after the
    reply."""
    run = walk([_dispatch(2, SAVE_STEP), *_save('v1', 'turned'), _verdict(2)],
               SAVE_STEP, request_id='turn2_req')
    assert _gate_accepts(run, SAVE_STEP, monkeypatch) is True
    run = walk([_dispatch(2, DRILL_STEP), *_ask('s1'), _seed(2, DRILL_STEP),
                *_save('v2', REPLY), _verdict(2)], DRILL_STEP,
               request_id='reply_req', open_ask=dict(ASK), calls_before=['s1'])
    assert _gate_accepts(run, DRILL_STEP, monkeypatch) is True


def test_on_a_turn_nobody_can_answer_a_save_before_any_answer_does_not_complete_it(walk):
    """rv_2da7 F1: the drill on an empty-id turn (standalone, WhatsApp, a
    scheduled run that lost its id).  Its question is out and nobody can
    answer it on this turn: it stays under the text rule, as on the parent.
    With nothing written, that rule refuses it and the save made before any
    answer does not complete it.  (With a line written it does: the known gap
    pinned in the next test.)"""
    run = walk([_dispatch(2, DRILL_STEP), *_ask('s1'), *_save('v1', 'ball'),
                _verdict(2)], DRILL_STEP, request_id='')
    assert not run.commits and not run.waits, vars(run)
    assert run.pending[0] == [run.rr._REUSE_NO_OUTPUT_SENTINEL], run.pending


def test_known_gap_one_written_line_lets_the_save_before_any_answer_complete_it(walk):
    """KNOWN GAP, pinned as it is today and NOT wanted (rv_a146 finding 1,
    probe p1b).  It was the same on the parent and before 2da7dbd88, so no
    commit here made it.  The drill on an empty-id turn, and the model wrote
    one line ("Say ball! Your turn.").  The text rule is met by that line,
    the receipt finder takes the save made before any answer, and the step
    completes on an attempt nobody made.

    Closing it means holding a step that asked on a turn nobody can answer
    (the window_asked rule of _reuse_completion_evidence, extended to a step
    that names a tool).  That is a new rule and waits for the owner's word.
    When it is given, this test is the one to turn round."""
    run = walk([_dispatch(2, DRILL_STEP), *_ask('s1'), *_save('v1', 'ball'),
                _written('Say ball! Your turn.'), _verdict(2)],
               DRILL_STEP, request_id='')
    assert len(run.commits) == 1 and not run.waits and not run.states, vars(run)
    receipt = run.commits[0]
    assert receipt['kind'] == 'tool_receipt', receipt
    assert run.chat.messages[receipt['message_index']]['tool_responses'][0][
        'tool_call_id'] == 'v1', receipt


def test_a_persons_turn_holds_though_the_sessions_latest_id_was_rewritten(walk):
    """rv_a146 finding 2, probe p3, the direction no test held: this turn is a
    person's by its own id, and another arrival has rewritten the session's
    latest id to ''.  The drill still waits for the person, and the save made
    before the answer does not complete it."""
    run = walk([_dispatch(2, DRILL_STEP), *_ask('s1'), *_save('v1', 'ball'),
                _verdict(2)], DRILL_STEP, request_id='person_req',
               session_request_id='')
    assert run.waits and not run.commits and not run.states, vars(run)
    assert not run.steers, run.steers


def test_the_ask_is_recorded_under_the_turns_own_id(walk):
    """rv_a146 finding 3: the same turn.  The ask it waits on is recorded
    under the id of the turn that asked, not under whatever the session's
    latest id was rewritten to (it was recorded under '')."""
    run = walk([_dispatch(2, DRILL_STEP), *_ask('s1'), *_save('v1', 'ball'),
                _verdict(2)], DRILL_STEP, request_id='person_req',
               session_request_id='')
    assert run.asks['recorded_under'] == ['person_req'], run.asks
    assert run.asks['open']['request_id'] == 'person_req', run.asks


def _serve_crawl4ai(monkeypatch):
    """The chat's agents serve crawl4ai_crawl under its service name, so a
    step can name it as "crawl4ai: ..." (lifecycle_hooks
    .service_named_functions)."""
    from integrations.service_tools.crawl4ai_tool import Crawl4AITool
    monkeypatch.setitem(reg_mod.service_tool_registry._tools, 'crawl4ai',
                        Crawl4AITool.create_tool_info())
    reg_mod.service_tool_registry.create_endpoint_function = (
        lambda t, e: (lambda **kw: f'{t}.{e} ran'))


def test_the_gate_holds_an_ask_step_by_the_turns_own_id(walk, monkeypatch):
    """rv_a146 finding 3, the ask-step hold and the text-rule exemption: a
    person's turn whose session id was rewritten to ''.  The ask step's
    question is out, so the gate neither sends it to crawl before the answer
    nor asks it for a text.  Both read the session's latest id before, and
    the gate steered this turn to crawl.

    Only the gate is held here.  The receipt finder and the hold after it
    still read the session's latest id, as the rest of the REUSE turn does
    (their move is a separate piece of work)."""
    _serve_crawl4ai(monkeypatch)
    run = walk([_dispatch(2, ASK_CRAWL_STEP), *_ask('s1'), _verdict(2)],
               ASK_CRAWL_STEP, request_id='person_req', session_request_id='')
    assert not run.commits, run.commits
    assert 'crawl4ai_crawl' not in run.pending[0], run.pending
    assert _no_text_steer(run), run.pending


def test_an_ask_step_on_a_turn_nobody_can_answer_is_not_held_as_a_persons(walk):
    """The other direction, a8c1e1b8f's rule: this turn's own id is empty and
    a person's request has landed on the session since.  The turn is judged
    by its own id, exactly as when the session's id is empty too: no hold,
    and the text rule asks for the step's output."""
    run = walk([_dispatch(2, ASK_STEP), *_ask('s1'), _verdict(2)],
               ASK_STEP, request_id='', session_request_id='person_req')
    assert not run.commits and not run.waits, vars(run)
    assert run.pending[0] == [run.rr._REUSE_NO_OUTPUT_SENTINEL], run.pending
    same = walk([_dispatch(2, ASK_STEP), *_ask('s1'), _verdict(2)],
                ASK_STEP, request_id='')
    assert same.pending[0] == run.pending[0] and not same.waits, vars(same)


def test_with_no_turn_bound_the_sessions_latest_id_is_read_as_before(walk):
    """No regression: code that reaches the gate outside chat_agent has no
    turn bound, which is not an empty id.  There the session's latest id is
    still what says whose turn it is, so an ask step with its question out
    holds for the person as it did."""
    run = walk([_dispatch(2, ASK_STEP), *_ask('s1'), _verdict(2)],
               ASK_STEP, request_id='person_req', bound=False)
    assert run.waits and not run.commits and not run.states, vars(run)


def test_the_hold_reads_the_turns_own_id_not_the_sessions_latest(walk):
    """rv_2da7 F1, a8c1e1b8f's rule: request_id_list says a person's request
    landed, but this turn's own id is empty.  The turn is judged by its own
    id: no hold, and no completion on the save before any answer."""
    run = walk([_dispatch(2, DRILL_STEP), *_ask('s1'), *_save('v1', 'ball'),
                _verdict(2)], DRILL_STEP, request_id='',
               session_request_id='person_req')
    assert not run.commits and not run.waits, vars(run)


def _parked(monkeypatch):
    """The goals the REAL hand-off (_reuse_hand_background_ask) parks for
    their person, recorded instead of written (lifecycle_hooks
    .hand_goal_to_its_person is the one writer)."""
    import hartos.reuse_recipe as rr
    parked = []
    monkeypatch.setattr(rr, 'hand_goal_to_its_person',
                        lambda goal_id, *a, **k: parked.append(goal_id) or 'owner')
    return parked


def test_a_daemon_turns_question_is_handed_off_though_a_persons_request_landed(
        walk, monkeypatch):
    """rv_a146's review of 63d681956: a daemon turn asked its question, and a
    person's request landed on the session before the gate ran.  The hand-off
    read the session's latest id, took the turn for a person's, skipped the
    hand-off, and the step ended GAVE_UP for retry: the outcome the owner
    rejected on 2026-10-31 (park, notify, resume).  The turn is a daemon's by
    its own id, so its question goes to the goal's person: the step holds,
    the ask is recorded under the daemon turn, and the goal that turn runs
    for is the one parked (the session's latest id names no goal)."""
    parked = _parked(monkeypatch)
    run = walk([_dispatch(2, ASK_STEP), *_ask('s1'), _verdict(2)],
               ASK_STEP, request_id='daemon_goal7', session_request_id='person_req')
    assert not run.states and not run.commits, vars(run)
    assert run.waits and not run.steers, vars(run)
    assert parked == ['goal7'], parked
    assert run.asks['recorded_under'] == ['daemon_goal7'], run.asks


def test_a_daemon_turn_with_no_landing_is_handed_off_as_before(walk, monkeypatch):
    """No regression: the session's latest id is the daemon turn's own."""
    parked = _parked(monkeypatch)
    run = walk([_dispatch(2, ASK_STEP), *_ask('s1'), _verdict(2)],
               ASK_STEP, request_id='daemon_goal7')
    assert not run.states and not run.commits and run.waits, vars(run)
    assert parked == ['goal7'], parked
    assert run.asks['recorded_under'] == ['daemon_goal7'], run.asks


def test_a_persons_turn_parks_no_goal_though_a_daemon_id_landed(walk, monkeypatch):
    """The other direction: this turn is a person's by its own id and a
    daemon tick has rewritten the session's latest id.  The person is here to
    answer, so the gate does not hand their question to that goal's person
    and parks nothing, and the step holds for their reply with the ask
    recorded under their turn.  rv_reuse2's review of 063325b58 (shape C):
    the receipt finder and the hold after the gate read the session's latest
    id, took the turn for the daemon's, and the step ended GAVE_UP."""
    parked = _parked(monkeypatch)
    run = walk([_dispatch(2, ASK_STEP), *_ask('s1'), _verdict(2)],
               ASK_STEP, request_id='person_req', session_request_id='daemon_goal7')
    assert parked == [], parked
    assert run.asks['recorded_under'] == ['person_req'], run.asks
    assert run.waits and not run.states and not run.commits, vars(run)
    assert not run.steers, run.steers


def test_a_persons_drill_holds_though_a_daemon_id_landed(walk, monkeypatch):
    """Shape C on a drill: the save made before the person's answer does not
    complete it, and the step waits for the answer under the person's turn."""
    parked = _parked(monkeypatch)
    run = walk([_dispatch(2, DRILL_STEP), *_ask('s1'), *_save('v1', 'ball'),
                _verdict(2)], DRILL_STEP, request_id='person_req',
               session_request_id='daemon_goal7')
    assert parked == [], parked
    assert run.asks['recorded_under'] == ['person_req'], run.asks
    assert run.waits and not run.states and not run.commits, vars(run)


def test_the_hand_off_without_a_turn_id_reads_the_sessions_latest_as_before(monkeypatch):
    """No regression for the hand-off's other caller (the receipt finder's
    hold), which passes no turn id: the session's latest id names the goal."""
    import flask
    import hartos.reuse_recipe as rr
    parked = _parked(monkeypatch)
    recorded = []
    monkeypatch.setattr(rr, 'record_ask_delivered',
                        lambda up, a, rid, *x, **k: recorded.append(rid) or True)
    monkeypatch.setattr(rr, '_reuse_wait_for_the_person', lambda *a, **k: True)
    monkeypatch.setattr(rr, '_reuse_action_flow', lambda *a, **k: 0)
    monkeypatch.setitem(rr.request_id_list, SESSION, 'daemon_goal9')
    with flask.Flask('hand_off').app_context():
        rr._reuse_hand_background_ask(SESSION, 2, (1, 'which word?'), '1794540972213', 'r')
        rr._reuse_hand_background_ask(SESSION, 2, (1, 'which word?'), '1794540972213', 'r',
                                      request_id='daemon_goal7')
    assert parked == ['goal9', 'goal7'] and recorded == ['daemon_goal9', 'daemon_goal7']


def test_the_gate_reads_the_turns_id_through_the_one_public_reader(monkeypatch):
    """rv_a146 (minor): _reuse_turn_request_id read llm_outbound_logger's
    private contextvar.  It asks the module's public reader, which says what
    an unbound turn reads as: '' for every caller that had it, and None for
    the one that must tell "no turn bound" from "a turn bound with no id"."""
    import core.llm_outbound_logger as lol
    import hartos.reuse_recipe as rr
    assert lol.turn_request_id() == '' and lol.turn_request_id(unbound=None) is None
    with lol.request_id_context(''):
        assert lol.turn_request_id() == '' and lol.turn_request_id(unbound=None) == ''
    with lol.request_id_context('person_req'):
        assert lol.turn_request_id() == lol.turn_request_id(unbound=None) == 'person_req'
    monkeypatch.setitem(rr.request_id_list, SESSION, 'session_latest')
    asked = []
    monkeypatch.setattr(lol, 'turn_request_id',
                        lambda unbound='': asked.append(unbound) or 'from_the_reader')
    assert rr._reuse_turn_request_id(SESSION) == 'from_the_reader'
    assert asked == [None], asked
    monkeypatch.setattr(lol, 'turn_request_id', lambda unbound='': unbound)
    assert rr._reuse_turn_request_id(SESSION) == 'session_latest'


def test_a_read_then_a_sentence_still_owes_the_sentence(walk):
    """rv_2da7 F2: a step whose words name a READ and ask for text.  The read
    ran and nothing was written: refused under the text rule, as on the
    parent (a read's result is what the step answers from)."""
    step = ("get_data_by_key 'child_name', then tell the parent the child's "
            "name in one sentence")
    run = walk([_dispatch(2, step),
                _call('g1', 'get_data_by_key', {'key': 'child_name'}),
                _result('g1', 'Arun'), _verdict(2)], step, request_id='turn2_req')
    assert not run.commits, run.commits
    assert run.pending[0] == [run.rr._REUSE_NO_OUTPUT_SENTINEL], run.pending


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
