"""A step that asks the person again after their reply waits for that answer,
and the walk never asks twice in one turn (#295 F7).

MEASURED on the owner's desktop, 2026-11-26, build #5 (HARTOS 9a3a4a98b) on
the local 4B, speech agent 95568353503, child flow.  Turn
msg_1795705626706_5tjhm1 (server.log.1): the child answered step 6's
question with "ஜோயி!" at 20:37:06, and in one turn she got three questions:

    20:37:29  step 6  "அழகான முயற்சி! ... Can you try saying it a few more times?"
    20:37:42  verdict 'pending' -> "routing to helper to continue (bounded)"   M1
    20:37:53  step 6  "Great! Now let's try saying your name in English: ..."
    20:38:03  verdict 'completed' -> "evidence-backed completion", step 7  M2
    20:39:20  step 8  "Zoey, can you say it back to me?"

The hold after a question already existed; two branches skipped it.

  M1  state_transition sent a 'pending' verdict back to the Helper by the
      raw banked flag ('yes'), while the step's own question was out.  It
      now reads _reuse_runs_alone (the one reader of "runs without the person
      this run"), and a 'pending' verdict with the step's question out ends
      the round (_reuse_window_holds_an_ask), and an 'error' one too; the
      turn's exit then holds the step on its open ask, with no answer
      recorded (a reply the verifier did not accept is not the answer).
  M2  _reuse_completion_evidence took the reply to the step's EARLIER
      question as its receipt although the step had asked again after it.
      The same S2b rule now holds the step on its new question instead.

The same M2 shape in all four live bursts of that walk: h3iuea (19:39, steps
6 then 8), 1wg72e (19:50, 10 then 11), 5tjhm1 (above) and d3ceeb (20:46, 10
then 11).  The clean turn jmj8st (20:36:50 send, 20:36:54 hold) is the
guard.

The M1 tests run REUSE's real state_transition on the real group
create_agents_for_user builds (the fixture pattern of tests/unit/
test_a_reuse_turn_never_tells_the_agent_it_is_the_person.py; needs the repo
conftest for idle_desktop).  The walk tests run the real chat_agent, state
machine, ledger and group chat of tests/unit/test_reuse_reply_is_evidence.py
with only the model seats scripted.

    python -m pytest tests/unit/test_a_step_that_asks_again_after_the_reply_waits.py -q
"""
from __future__ import annotations

import json
import os
import sys
from types import SimpleNamespace

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import pytest  # noqa: E402

from tests.unit.test_create_loop_end_to_end import (  # noqa: E402,F401  (fixture)
    PROMPT_ID, USER_ID, create_env)
from tests.unit.test_reuse_reply_is_evidence import speech  # noqa: E402,F401

#: The live step 6 of flow 0 (95568353503_0_recipe.json): banked 'yes'.
STEP6 = ("send_message_to_user: Send a chat message in home_language calling the "
         "child's name and wait for a sound, word, turn, or look (captions).")
FLOW = [
    {'action': STEP6, 'action_id': 1, 'persona': 'Child',
     'can_perform_without_user_input': 'yes', 'recipe': []},
    {'action': 'praise Zoey for the attempt in the latest user message',
     'action_id': 2, 'persona': 'Child', 'can_perform_without_user_input': 'yes',
     'recipe': []},
    {'action': ("send_message_to_user: ask Zoey to say her name back to you and wait "
                "for her answer"),
     'action_id': 3, 'persona': 'Child', 'can_perform_without_user_input': 'yes',
     'recipe': []},
]
#: The questions of the walk, as the 4B wrote them (server.log.1).
Q0 = ("வணக்கம் ஜொயி! (Hello Zoey!) Now let's practice saying your name in Tamil: "
      "ஜொயி! Can you say it back to me?")                                # 20:36:50
Q1 = ('அழகான முயற்சி! (Beautiful attempt!) You said your name "ஜொயி" (Zoey) in '
      'Tamil. Can you try saying it a few more times? Let\'s practice together: '
      'ஜொயி! ஜொயி! ஜொயி!')                                                  # 20:37:29
Q3 = 'Zoey, can you say it back to me?'                                 # 20:39:20
REPLY = 'ஜோயி!'                                                         # 20:37:06
#: What each recorded ask must name, whichever part of the send it keeps.
MARK = {Q0: 'Can you say it back to me?', Q1: 'a few more times',
        Q3: 'Zoey, can you say it back'}
TURN = 'msg_1795705626706_5tjhm1'


# ── M1: the verdict route, on REUSE's real state_transition ──────────────

@pytest.fixture
def real_group(create_env, tmp_path, monkeypatch):
    """The real REUSE group for FLOW.  route(verdict, asked, request_id): who
    speaks after the StatusVerifier's verdict on step 1, with the step's
    question in its window or not."""
    env = create_env
    from security import key_delegation
    monkeypatch.setattr(key_delegation, 'get_node_tier', lambda: 'flat')
    monkeypatch.setenv('NUNBA_DATA_DIR', str(tmp_path / 'data'))
    from core import platform_paths
    platform_paths.reset_cache()
    from integrations.reel_studio import jobs
    monkeypatch.setattr(jobs, '_SWEPT', True)
    import hartos.create_recipe as cr
    import hartos.reuse_recipe as rr
    monkeypatch.setattr(rr, 'PROMPTS_DIR', str(env.prompts))
    monkeypatch.setattr(rr, 'HAS_SIMPLEMEM', False)
    # Two boundaries the route does not decide: a person using the computer
    # (the yield between rounds) and the thought stream to the page.
    monkeypatch.setattr(rr.helper_fun, 'yield_between_rounds', lambda *a, **k: False)
    monkeypatch.setattr(cr, 'publish_agent_thought', lambda *a, **k: None)
    caches = ('user_tasks', 'recipes', 'final_recipe', 'request_id_list',
              'user_agents', 'time_actions', 'user_ledgers', 'agents_session')
    for name in caches:
        cache = getattr(rr, name, None)
        if cache is not None and hasattr(cache, '_loader'):
            monkeypatch.setattr(cache, '_loader', None)
        if cache is not None and hasattr(cache, 'clear'):
            cache.clear()
    if getattr(rr, 'get_production_backend', None) is not None:
        from agent_ledger.backends import InMemoryBackend
        monkeypatch.setattr(rr, 'get_production_backend', lambda *a, **k: InMemoryBackend())
    (env.prompts / f'{PROMPT_ID}.json').write_text(json.dumps({
        'personas': [{'name': 'Child', 'description': 'Zoey, three'}],
        'goal': 'Help a child respond to her name', 'agent_name': 'speech.local.coach',
        'flows': [{'flow_name': 'Name calling', 'persona': 'Child',
                   'sub_goal': 'turns when called',
                   'actions': [a['action'] for a in FLOW]}],
    }), encoding='utf-8')
    (env.prompts / f'{PROMPT_ID}_0_recipe.json').write_text(
        json.dumps({'status': 'done', 'actions': FLOW}), encoding='utf-8')
    # Each step's own saved recipe (<prompt>_<flow>_<step>.json), which the
    # group's individual_recipe is read from, as on the desktop.
    for action in FLOW:
        (env.prompts / f"{PROMPT_ID}_0_{action['action_id']}.json").write_text(
            json.dumps(dict(action, status='done')), encoding='utf-8')
    from core.flow_progress import set_persona
    assert set_persona(USER_ID, PROMPT_ID, 'Child', ['Child']) == 'Child'
    with env.app.app_context():
        built = rr.create_agents_for_user(USER_ID, PROMPT_ID)
    group, helper = built[2], built[4]
    verify = next(a for a in group.agents if a.name == 'StatusVerifier')
    up = f'{USER_ID}_{PROMPT_ID}'

    def route(verdict, asked=True, request_id=TURN):
        rr.user_tasks[up].current_action = 1
        rr.request_id_list[up] = request_id
        window = [{'role': 'user', 'name': 'ChatInstructor',
                   'content': f'{rr._REUSE_ACTION_MESSAGE_PREFIX}1:{STEP6}'}]
        if asked:
            sent = f'Message sent successfully to user with request_id: {request_id}'
            window += [
                {'role': 'assistant', 'name': 'Assistant', 'content': '',
                 'tool_calls': [{'id': 'call_q1', 'type': 'function', 'function': {
                     'name': 'send_message_to_user',
                     'arguments': json.dumps({'text': Q1, 'expects_reply': True})}}]},
                {'role': 'tool', 'name': 'Executor', 'content': sent,
                 'tool_responses': [{'tool_call_id': 'call_q1', 'role': 'tool',
                                     'content': sent}]}]
        window.append({'role': 'user', 'name': 'StatusVerifier', 'content': json.dumps(
            {'status': verdict, 'action_id': 1, 'message': 'Waiting for her attempt'})})
        group.messages[:] = window
        with env.app.app_context():
            return group.speaker_selection_method(verify, group)

    yield SimpleNamespace(route=route, helper=helper)
    for name in caches:
        cache = getattr(rr, name, None)
        if cache is not None and hasattr(cache, 'clear'):
            cache.clear()
    platform_paths.reset_cache()


@pytest.mark.parametrize('verdict', ['pending', 'error'])
def test_the_20_37_verdict_with_her_question_out_ends_the_round(real_group, verdict):
    """20:37:42: the verdict after the 20:37:29 question ('pending' live).  The
    round ends, so no seat asks her again before she answers (the Helper sent
    the 20:37:53 question).  An 'error' too (hartos-17): sent to the default
    seat, the Assistant could ask again, the same burst by another door; the
    error is looked at when the step resumes on her reply."""
    assert real_group.route(verdict) is None


def test_a_daemon_turns_pending_verdict_with_the_question_out_ends_the_round(real_group):
    """The one reader holds on every turn, as the escape and the nudge read
    it; the turn's exit then hands the question to the goal's person."""
    assert real_group.route('pending', request_id='daemon_goal-1') is None


def test_a_yes_step_with_no_question_out_still_goes_to_the_helper_on_pending(real_group):
    assert real_group.route('pending', asked=False) is real_group.helper


def test_an_error_verdict_with_no_question_out_keeps_its_route(real_group):
    assert real_group.route('error', asked=False) is real_group.helper


# ── M2 and the turn's exit: the real walk, the model seats scripted ──────

def _send(state, text):
    return {'content': '', 'tool_calls': [{
        'id': 'ask_%d' % len(state['sends']), 'type': 'function',
        'function': {'name': 'send_message_to_user',
                     'arguments': json.dumps({'text': text, 'expects_reply': True})}}]}


def _the_4b(asks_again=True):
    """The 4B of the walk: step 1 calls her name on the ask turn; on the turn
    that carries her reply it asks a NEW question (asks_again) or only judges
    the reply; step 3 asks her to say it back.  One send per step and turn."""
    def script(state, aid, content):
        done = state.setdefault('asked', set())
        key = (state['turn'], aid)
        if aid == 1:
            if key not in done and state['turn'] == 'ask':
                done.add(key)
                return _send(state, Q0)
            if key not in done and state['turn'] == 'reply' and asks_again:
                done.add(key)
                return _send(state, Q1)
            return 'She said her name: a lovely try.'
        if aid == 3:
            if key not in done:
                done.add(key)
                return _send(state, Q3)
            return 'Waiting for her answer.'
        return None                     # step 2: the harness's praise line
    return script


def _verdicts(on_the_reply=None):
    """'completed' for every step (the live 4B's), or ``on_the_reply``
    ('pending' at 20:37:42 after the verifier was asked again, or 'error')
    for step 1 on the reply turn."""
    def script(state, aid, content):
        if aid == 1 and state['turn'] == 'reply' and on_the_reply:
            return json.dumps({'status': on_the_reply, 'action_id': 1,
                               'message': 'Waiting for her new attempt'})
        return json.dumps({'status': 'completed', 'action_id': aid})
    return script


def _walk(speech, asks_again=True, on_the_reply=None):
    run = speech(actions=FLOW, assistant_script=_the_4b(asks_again),
                 verdict_script=_verdicts(on_the_reply))
    run.turn('hi, I am ready', 'request-1')                  # jmj8st: Q0, held
    assert run.state['sends'] == [Q0], run.state['sends']
    return run


def _task(run, aid):
    return run.ledger.tasks[f'action_{aid}']


def _answers(run, aid):
    ctx = getattr(_task(run, aid), 'context', None) or {}
    return [e.get('answer') for e in (ctx.get(run.L.USER_INPUT_EVIDENCE_KEY) or [])]


def _held_on(run, aid, question, request_id):
    from agent_ledger.core import TaskStatus
    ask = run.L.ledger_open_ask(run.key, aid, 0) or {}
    asked = ' '.join(str(ask.get(k) or '') for k in ('question', 'asked'))
    assert ask.get('request_id') == request_id, ask
    assert MARK[question] in asked, ask
    assert _task(run, aid).status == TaskStatus.BLOCKED
    assert _task(run, aid).blocked_reason == 'input_required'


def test_the_clean_ask_turn_holds_as_today(speech):
    """jmj8st: one question, a 'completed' verdict, and the step holds on it."""
    run = _walk(speech)
    assert run.rr.user_tasks[run.key].current_action == 1
    _held_on(run, 1, Q0, 'request-1')
    assert _answers(run, 1) == []


def test_a_completed_verdict_after_the_new_question_holds_the_step(speech):
    """M2, 20:38:03: her reply answered Q0, the step asked Q1 after it, and
    the verdict said 'completed'.  The step holds on Q1; the walk does not
    move on, so step 3 never asks her a third question."""
    run = _walk(speech)
    run.turn(REPLY, 'request-2', kind='reply')
    assert run.state['sends'] == [Q0, Q1], run.state['sends']
    assert run.rr.user_tasks[run.key].current_action == 1
    assert 2 not in run.state['dispatched'], run.state['dispatched']
    assert _answers(run, 1) == [REPLY]
    _held_on(run, 1, Q1, 'request-2')


@pytest.mark.parametrize('verdict', ['pending', 'error'])
def test_the_turn_exit_holds_the_step_on_its_open_ask(speech, verdict):
    """M1's round end, 20:37:42: a 'pending' (or 'error') verdict after Q1.
    The turn's exit holds the step on its one open ask, Q0's from turn 1,
    and records no answer: a reply the verifier did not accept is not the
    step's answer (hartos-f9's pinned contract, test_reuse_reply_is_evidence
    TestAReplyJudgedInsufficientKeepsTheOneAsk).  The accepted trade: the
    person last saw Q1, so her next reply is recorded against Q0's question."""
    run = _walk(speech, on_the_reply=verdict)
    run.turn(REPLY, 'request-2', kind='reply')
    assert run.state['sends'] == [Q0, Q1], run.state['sends']
    assert run.rr.user_tasks[run.key].current_action == 1
    assert _answers(run, 1) == []
    asks = _task(run, 1).context.get(run.L.ASK_DELIVERED_KEY) or []
    assert len(asks) == 1 and asks[0]['request_id'] == 'request-1', asks
    _held_on(run, 1, Q0, 'request-1')


def test_the_evidence_read_twice_in_a_turn_records_one_answer(speech):
    """Its callers read _reuse_completion_evidence as a reader; it now writes
    through the S2b rule (hartos-17).  Read once more than the walk reads it,
    in the reply turn after Q1, it still records one answer and leaves one
    open ask, Q1's: the second read finds that ask was asked in this same
    turn, so the reply is no evidence for it."""
    box = {}

    def verdicts(state, aid, content):
        if aid == 1 and state['turn'] == 'reply' and not state.get('read_once'):
            state['read_once'] = True
            run = box['run']
            run.rr._reuse_completion_evidence(run.key, 1, run.gc)
        return json.dumps({'status': 'completed', 'action_id': aid})

    run = speech(actions=FLOW, assistant_script=_the_4b(), verdict_script=verdicts)
    box['run'] = run
    run.turn('hi, I am ready', 'request-1')
    run.turn(REPLY, 'request-2', kind='reply')
    assert run.state['sends'] == [Q0, Q1], run.state['sends']
    assert _answers(run, 1) == [REPLY]
    _held_on(run, 1, Q1, 'request-2')


def test_the_next_turn_takes_the_reply_to_the_last_question(speech):
    """Her next message answers Q1: step 1 completes on it, the walk goes
    on, and step 3 asks once."""
    from agent_ledger.core import TaskStatus
    run = _walk(speech)
    run.turn(REPLY, 'request-2', kind='reply')
    run.turn('Zoey', 'request-3', kind='next')
    assert run.state['sends'] == [Q0, Q1, Q3], run.state['sends']
    assert _answers(run, 1) == [REPLY, 'Zoey']
    assert _task(run, 1).status == TaskStatus.COMPLETED
    assert run.rr.user_tasks[run.key].current_action == 3
    _held_on(run, 3, Q3, 'request-3')


def test_a_reply_turn_that_asks_nothing_again_completes_on_the_reply(speech):
    """zaf02y: the step only judges her reply, so the reply is its receipt
    and the walk goes on as before."""
    from agent_ledger.core import TaskStatus
    run = _walk(speech, asks_again=False)
    run.turn(REPLY, 'request-2', kind='reply')
    assert _task(run, 1).status == TaskStatus.COMPLETED
    assert _answers(run, 1) == [REPLY]
    assert run.state['sends'] == [Q0, Q3], run.state['sends']
    assert run.rr.user_tasks[run.key].current_action == 3


def test_a_daemon_turns_question_is_still_handed_to_the_goals_person(speech, monkeypatch):
    """The turn's exit tries the S2b rule first: a daemon turn carries no
    reply, so its question still takes the background hand-off."""
    import hartos.reuse_recipe as rr
    handed = []
    monkeypatch.setattr(rr, '_reuse_hand_background_ask',
                        lambda up, aid, asked, *a, **k: handed.append((aid, asked)) or True)
    run = speech(actions=FLOW, assistant_script=_the_4b(),
                 verdict_script=lambda state, aid, content: json.dumps(
                     {'status': 'pending', 'action_id': aid, 'message': 'waiting'}))
    run.turn('tick', 'daemon_goal_turn', kind='ask')
    assert run.state['sends'] == [Q0], run.state['sends']
    assert handed and {aid for aid, _asked in handed} == {1}, handed
    assert MARK[Q0] in str(handed[0][1]), handed
