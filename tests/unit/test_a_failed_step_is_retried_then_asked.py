"""A step the StatusVerifier judged FAILED is run again, a bounded number of
times, and then the person is asked in words.  The verdict JSON is never the
reply.

WHAT WENT WRONG (desktop build 75, 14 Nov 2026, langchain.log)
  The Research Assistant (93676680129) answered a person's turn with the raw
  verifier verdict:
      {"status": "error", "action": "search_long_term_memory for saved
       research notes", "action_id": 2, "message": "Action failed. ..."}
  (request msg_1794670427623_sim, 21:04:02 IST; the same reply on 13 Nov at
  13:45:32 and 13:46:40).  The verdict ended the round: state_transition hands
  an 'error' verdict to the UserProxy, whose reply ends the chat, and set
  action 2 to ERROR.  The create loop then had no branch for ERROR: the
  re-post path took ASSIGNED, PENDING, IN_PROGRESS and
  STATUS_VERIFICATION_REQUESTED only, so the loop logged "no matching
  condition found" 300 times in 1.6 s, left at "reaching max iterations
  (300)", and returned the group chat's last message, the verdict.

THE RULE
  A failed step is re-posted like a refused completion claim, under the same
  three-attempt bound; past it the loop asks for help (_ask_for_help): a person
  gets a question in words, a daemon turn parks the goal with the ask.  It
  never runs to its iteration cap, never records the step done, never banks
  it, and never moves past it.

    python -m pytest tests/unit/test_a_failed_step_is_retried_then_asked.py -q --noconftest
"""
import json
import logging

import pytest

from tests.unit.test_create_loop_end_to_end import (  # noqa: F401  (fixture)
    ACTIONS, UP, USER_ID, PROMPT_ID, _DONE_STATES, _MARKER, _action_file,
    _content, _run, create_env)


@pytest.fixture
def idle_desktop(monkeypatch):
    """tests/conftest.py's idle_desktop, here so the file also runs under
    --noconftest: nobody is at this computer, so no background gate says stop."""
    from core import resource_governor
    monkeypatch.setattr(resource_governor, 'get_physical_input_state',
                        lambda **_kw: (0, None))
    from integrations.agent_engine import dispatch
    monkeypatch.setattr(dispatch, 'person_using_the_machine', lambda: None)
    monkeypatch.setattr(dispatch, 'should_yield_to_user', lambda: False)


_FAILED = 2
_VERDICT = {
    'status': 'error', 'action': ACTIONS[_FAILED - 1], 'action_id': _FAILED,
    'message': ("Action failed. The tool 'google_search' returned the "
                "action's own description instead of data.")}


def _failed_verdict(script):
    """The live verdict's shape: pretty-printed, citing its receipt in a nested
    evidence object.  The nested braces matter: state_transition's first error
    check reads only up to the first '}', cannot parse that, and the verdict
    reaches the branch that sets ERROR and ends the round, as it did live."""
    receipt = max(i for i, m in enumerate(script._msgs())
                  if isinstance(m, dict) and m.get('role') == 'tool')
    return json.dumps(dict(
        _VERDICT, evidence={'message_index': receipt, 'kind': 'tool_receipt'},
        can_perform_without_user_input='yes',
        persona_name='Researcher', fallback_action='retry the search once'),
        indent=2)


def _fail_step(env):
    """The scripted verifier judges every run of step 2 failed, as the live
    verifier did for the Research Assistant's memory search."""
    script, passed = env.script, env.script.verifier

    def verifier(recipient, messages=None, sender=None, config=None):
        if ('please verify' in _content(script._last())
                and script.current() == _FAILED):
            return True, _failed_verdict(script)
        return passed(recipient, messages=messages, sender=sender, config=config)
    script.verifier = verifier


def _posts_of(env, n):
    return sum(1 for m in env.script.gc.messages
               if f'Execute Action {n}:' in _content(m))


def _assert_held_not_done(env):
    assert _action_file(env, 1) is not None, 'action 1 ran and should be banked'
    assert _action_file(env, _FAILED) is None, 'a failed step was banked'
    posted = {int(n) for m in env.script.gc.messages
              for n in _MARKER.findall(_content(m))}
    assert _FAILED + 1 not in posted, 'the flow moved past the failed step'
    done = [s for aid, s, _ in env.events if aid == _FAILED and s in _DONE_STATES]
    assert not done, f'the failed step was recorded as done: {done}'


def _assert_not_the_verdict(reply):
    assert '"status"' not in reply and '"action_id"' not in reply and (
        'tool_receipt' not in reply), (
        f"the person was handed the verifier's JSON verdict: {reply[:300]}")
    assert _VERDICT['message'] not in reply


def test_a_persons_turn_gets_a_question_not_the_verdict(create_env, caplog):
    env = create_env
    _fail_step(env)
    from hartos.threadlocal import thread_local_data
    thread_local_data.set_request_id('msg_e2e_person')
    caplog.set_level(logging.INFO)
    with env.app.app_context():
        reply = env.cr.recipe(USER_ID, 'Find what local AI costs, with sources',
                              PROMPT_ID, None, 'msg_e2e_person')
    reply = reply if isinstance(reply, str) else json.dumps(reply)
    _assert_not_the_verdict(reply)
    assert 'reaching max iterations' not in caplog.text, (
        'the loop spun to its iteration cap on a failed step')
    assert 2 <= _posts_of(env, _FAILED) <= 4, (
        f'step {_FAILED} posted {_posts_of(env, _FAILED)} times: '
        'a failed step is run again, at most three more times')
    assert '[NEEDS-INPUT]' in caplog.text, 'the person was not asked'
    _assert_held_not_done(env)


def test_a_daemon_turn_parks_the_goal_with_an_ask(create_env, monkeypatch, caplog):
    env = create_env
    _fail_step(env)
    asks = []
    import integrations.agent_engine.goal_manager as gm
    monkeypatch.setattr(gm.GoalManager, 'escalate_goal', staticmethod(
        lambda db, goal_id, escalation: asks.append((goal_id, escalation))
        or {'success': True}))
    import integrations.social.models as models
    from contextlib import contextmanager

    @contextmanager
    def _no_db(commit=False):
        yield None
    monkeypatch.setattr(models, 'db_session', _no_db)
    caplog.set_level(logging.INFO)
    replies = _run(env, turns=1)
    for reply in replies:
        _assert_not_the_verdict(reply if isinstance(reply, str) else json.dumps(reply))
    assert 'reaching max iterations' not in caplog.text
    assert [(goal, e['action_id']) for goal, e in asks] == [('e2e_create', _FAILED)], asks
    _assert_held_not_done(env)


def test_a_step_that_succeeds_on_retry_is_banked_and_the_flow_goes_on(create_env):
    """The retry is a real second run: when the step then succeeds it completes
    from its own receipt and the flow continues past it."""
    env = create_env
    script, passed = env.script, env.script.verifier
    failed_once = []

    def verifier(recipient, messages=None, sender=None, config=None):
        if ('please verify' in _content(script._last())
                and script.current() == _FAILED and not failed_once):
            failed_once.append(True)
            return True, _failed_verdict(script)
        return passed(recipient, messages=messages, sender=sender, config=config)
    script.verifier = verifier
    _run(env, turns=3)
    assert failed_once, 'the scripted failure never fired'
    assert _action_file(env, _FAILED) is not None, (
        'the step that succeeded on its retry was not banked')
    assert _action_file(env, 3) is not None, 'the flow did not go on past it'


# ── The loop's two give-up guards ask too (forward fix, 2026-11-16) ─────────
# Live on build 75, 2026-11-15 21:07 IST: the Research Assistant's step 3 sat
# open for 121 iterations, [STALL-GUARD] broke the loop, and the person was
# sent the group chat's last message, the StatusVerifier's verdict ("Action
# completed. The user has explicitly provided the research topic ...").  The
# stall and cycle guards now ask for help the way the input gate does.

_STUCK = 2


def _guard_gives_up_at(real, step):
    """The real guard, made to give up the first time it looks at ``step``."""
    def guard(prev_key, prev, action_id, state, *args, **kwargs):
        key, seen, gives_up = real(prev_key, prev, action_id, state, *args, **kwargs)
        return key, seen, gives_up or action_id == step
    return guard


def _stuck_step_held_not_done(env):
    assert _action_file(env, 1) is not None, 'action 1 ran and should be banked'
    assert _action_file(env, _STUCK) is None, 'a stuck step was banked'
    done = [s for aid, s, _ in env.events if aid == _STUCK and s in _DONE_STATES]
    assert not done, f'the stuck step was recorded as done: {done}'


@pytest.mark.parametrize('guard', ['stall_guard_step', 'cycle_guard_step'])
def test_a_guard_that_gives_up_asks_the_person(create_env, monkeypatch, caplog, guard):
    env = create_env
    monkeypatch.setattr(env.cr, guard,
                        _guard_gives_up_at(getattr(env.cr, guard), _STUCK))
    from hartos.threadlocal import thread_local_data
    thread_local_data.set_request_id('msg_e2e_person')
    caplog.set_level(logging.INFO)
    with env.app.app_context():
        reply = env.cr.recipe(USER_ID, 'Find what local AI costs, with sources',
                              PROMPT_ID, None, 'msg_e2e_person')
    reply = reply if isinstance(reply, str) else json.dumps(reply)
    assert '"status"' not in reply and '"action_id"' not in reply, (
        f"the person was handed a verifier verdict: {reply[:300]}")
    # The question _ask_for_help puts to a person (_needs_input_reply).
    assert ('need your input' in reply or 'waiting for your reply' in reply), (
        f'the guard gave up and the person was not asked; reply: {reply[:300]}')
    assert f'Step {_STUCK} ' in reply, reply[:300]
    _stuck_step_held_not_done(env)


def _guard_gives_up_once_at(real, step, spent):
    """The real guard, made to give up the first time it looks at ``step``,
    handing back the count it would hold at that moment (``spent``: the
    count past the cap)."""
    fired = []

    def guard(prev_key, prev, action_id, state, *args, **kwargs):
        key, seen, gives_up = real(prev_key, prev, action_id, state, *args, **kwargs)
        if action_id == step and not fired:
            fired.append(state)
            return (action_id, state), spent(state), True
        return key, seen, gives_up
    guard.fired = fired
    return guard


@pytest.mark.parametrize('guard,spent', [
    ('stall_guard_step', lambda state: 10 ** 6),
    ('cycle_guard_step', lambda state: {state: 10 ** 6}),
])
def test_a_guard_that_asked_counts_again_from_zero(create_env, monkeypatch, guard, spent):
    """Review of b972e755b: the guards' counts live on user_tasks, which
    outlives the turn; kept, they asked the person the same question one
    round after her answer.  The ask clears both."""
    env = create_env
    gated = _guard_gives_up_once_at(getattr(env.cr, guard), _STUCK, spent)
    monkeypatch.setattr(env.cr, guard, gated)
    from hartos.threadlocal import thread_local_data
    thread_local_data.set_request_id('msg_e2e_person')
    with env.app.app_context():
        env.cr.recipe(USER_ID, 'Find what local AI costs, with sources',
                      PROMPT_ID, None, 'msg_e2e_person')
    assert gated.fired, 'the guard never gave up'
    task = env.cr.user_tasks[UP]
    assert (getattr(task, '_stall_key', None), getattr(task, '_stall_iters', 0)) == (None, 0)
    assert (getattr(task, '_cycle_key', None), getattr(task, '_cycle_entries', None)) == (None, None)


def test_the_answer_to_a_cycle_guard_ask_gets_a_fresh_round(create_env, monkeypatch, caplog):
    """Two turns: the cycle guard gives up on step 2 and asks; the person
    answers; her turn runs step 2 again without the guard asking again at once
    (review of b972e755b: 10 of 10 answers were met by the same question)."""
    env = create_env
    gated = _guard_gives_up_once_at(env.cr.cycle_guard_step, _STUCK,
                                    lambda state: {state: 10 ** 6})
    monkeypatch.setattr(env.cr, 'cycle_guard_step', gated)
    from hartos.threadlocal import thread_local_data
    caplog.set_level(logging.WARNING)
    with env.app.app_context():
        thread_local_data.set_request_id('msg_e2e_person')
        first = env.cr.recipe(USER_ID, 'Find what local AI costs, with sources',
                              PROMPT_ID, None, 'msg_e2e_person')
        assert f'Step {_STUCK} ' in str(first), str(first)[:300]
        thread_local_data.set_request_id('msg_e2e_answer')
        env.cr.recipe(USER_ID, 'Use the three sources you found in step 1.',
                      PROMPT_ID, None, 'msg_e2e_answer')
    gave_up = [r for r in caplog.records if '[CYCLE-GUARD]' in r.getMessage()]
    assert len(gave_up) == 1, (
        f'the cycle guard gave up {len(gave_up)} times; the answer turn should '
        'start counting from zero')


def test_a_stalled_step_on_a_daemon_turn_parks_the_goal(create_env, monkeypatch, caplog):
    env = create_env
    monkeypatch.setattr(env.cr, 'stall_guard_step',
                        _guard_gives_up_at(env.cr.stall_guard_step, _STUCK))
    asks = []
    import integrations.agent_engine.goal_manager as gm
    monkeypatch.setattr(gm.GoalManager, 'escalate_goal', staticmethod(
        lambda db, goal_id, escalation: asks.append((goal_id, escalation))
        or {'success': True}))
    import integrations.social.models as models
    from contextlib import contextmanager

    @contextmanager
    def _no_db(commit=False):
        yield None
    monkeypatch.setattr(models, 'db_session', _no_db)
    caplog.set_level(logging.INFO)
    replies = _run(env, turns=1)
    for reply in replies:
        text = reply if isinstance(reply, str) else json.dumps(reply)
        assert '"status"' not in text and '"action_id"' not in text, text[:300]
    assert [(goal, e['action_id']) for goal, e in asks] == [('e2e_create', _STUCK)], asks
    _stuck_step_held_not_done(env)
