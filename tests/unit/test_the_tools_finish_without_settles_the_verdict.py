"""The step's own tool said "this step may finish without my result": a
'pending' or 'error' verdict cannot keep the CREATE step open.

WHAT WENT WRONG (central, build 43e0d6a, 2026-11-20)
  McGroce goal 09bbc67a, action 13 "create_ad: create native ad ...".  Every
  create_ad call answered its own finish-without refusal (not_enough_spark:
  'system_daemon' has 0 Spark, the smallest ad needs 50; retry
  finish_without on a goal turn), 20 times.  The StatusVerifier, told the
  finish-without rule in its own system message, still answered
  {"status": "pending", "retry_allowed": false, "resume_when":
  "system_daemon has at least 50 Spark from the machine owner"}, and the
  step cycled "[TARGET] Action 13: in_progress -> pending (verifier
  pending)"; the goal never moved on.  The txt2img image steps and
  get_user_uploaded_file on goal turns paused goals the same way.

THE RULE (hartos.lifecycle_hooks.verdict_the_tool_settles, read by
state_transition and by lifecycle_hook_process_verifier_response)
  (a) the step's own tool returned its RETRY_FINISH_WITHOUT refusal in the
      step's window and the judge said pending -> the step is COMPLETED,
      the refusal its receipt;
  (b) the same with error -> COMPLETED;
  (c) ANOTHER tool's finish-without refusal in the window -> not completed;
  (d) a RETRY_AFTER_FIX refusal (create_ad's budget under the minimum) ->
      pending stays pending, error stays error;
  (e) a normal pending (a step whose ask card is out) -> stays pending.

REVIEW OF a0c9ee91
  - a step naming two tools settles only once every tool it names has
    returned its work (the gate's _work_results rule): txt2img's refusal
    with create_social_post never called stays pending; a saved copy of a
    refusal does not skip post_to_channel;
  - a step settled by a refusal reaches the world model as outcome
    'failure' (and Agent Lightning as not succeeded), never 'success';
  - a question the agent put out after the refusal keeps the step open;
  - the whole state_transition: a flat error verdict reaches the completion
    owner, not the Helper; a genuine error still does not.

    python -m pytest tests/unit/test_the_tools_finish_without_settles_the_verdict.py -q
"""
import pytest

from core.tool_refusal import image_not_made
from tests.unit.test_a_step_its_tool_lets_finish_completes_in_create import (
    _CompletionCase)

AD_STEP = ('create_ad: create native ad for the McGroce lash catalog '
           'targeting makeup artists in Chennai')
# The live verdict's own fields.
LIVE_PENDING = {'retry_allowed': False,
                'resume_when': 'system_daemon has at least 50 Spark from '
                               'the machine owner',
                'message': 'create_ad was refused for lack of Spark'}


@pytest.fixture()
def goal_turn(monkeypatch):
    """create_ad runs on a goal's turn, not a person's."""
    import core.agent_tools as agent_tools
    monkeypatch.setattr(agent_tools, 'camera_turn_id',
                        lambda: 'daemon_09bbc67a-goal')


def _spark_refusal():
    """create_ad's live answer for 'system_daemon' with 0 Spark."""
    from integrations.agent_engine.marketing_tools import _ad_refused
    return _ad_refused('system_daemon', 50,
                       {'error': 'Insufficient Spark', 'spark_balance': 0})


def _budget_refusal():
    """create_ad's answer for a budget under the minimum: fix and retry."""
    from integrations.agent_engine.marketing_tools import _ad_refused
    return _ad_refused('system_daemon', 5,
                       {'error': 'Minimum budget is 50 Spark'})


@pytest.fixture()
def completion():
    made = []

    def _make(action, body, tool, also_serves=()):
        case = _CompletionCase(action, body, tool)
        for name in also_serves:
            case.h.gc.agents[0]._function_map[name] = (lambda **_kw: None)
        made.append(case)
        return case
    yield _make
    for case in made:
        case.close()


def _receipt_index(case):
    records = case.h.ledger.tasks['action_1'].context.get(
        'verification_evidence') or []
    return [r['evidence'].get('message_index') for r in records]


# ── (a) pending on the step's own finish-without refusal ──────────────

def test_a_pending_verdict_on_create_ads_own_refusal_completes_the_step(
        goal_turn, completion):
    case = completion(AD_STEP, _spark_refusal(), 'create_ad')
    out = case.verdict('pending', **LIVE_PENDING)
    assert out['action'] == 'force_fallback', out
    assert case.state() == case.S.COMPLETED
    assert _receipt_index(case) == [2]


def test_the_judges_no_does_not_keep_it_open_either(goal_turn, completion):
    """The judge's resume_when reads as waiting on the owner; the tool said
    no call can fix it and the step may finish."""
    case = completion(AD_STEP, _spark_refusal(), 'create_ad')
    case.h._state(case.S.STATUS_VERIFICATION_REQUESTED)
    out = case.verdict('pending', can_perform_without_user_input='no',
                       **LIVE_PENDING)
    assert out['action'] == 'force_fallback', out
    assert case.state() == case.S.COMPLETED


def test_the_txt2img_alias_settles_an_image_step(completion):
    """The step names text_2_image and ran as txt2img (both names serve the
    one closure, as live)."""
    case = completion('text_2_image: make a picture of a bag of rice',
                      image_not_made('txt2img', 'ReadTimeout'), 'txt2img',
                      also_serves=('text_2_image',))
    assert case.verdict('pending')['action'] == 'force_fallback'
    assert case.state() == case.S.COMPLETED


def test_state_transition_reads_the_settled_verdict(goal_turn, completion):
    """What state_transition branches on: 'completed', citing the refusal,
    without the judge's can_perform_without_user_input."""
    case = completion(AD_STEP, _spark_refusal(), 'create_ad')
    verdict = {'status': 'pending', 'action_id': 1,
               'can_perform_without_user_input': 'no', **LIVE_PENDING}
    settled = case.lh.verdict_the_tool_settles(case.h.UP, 1, verdict)
    assert settled['status'] == 'completed', settled
    assert settled['evidence'] == {'message_index': 2, 'kind': 'tool_receipt'}
    assert 'can_perform_without_user_input' not in settled
    assert verdict['status'] == 'pending'      # the judge's own is untouched


# ── (b) error on the same refusal ─────────────────────────────────────

def test_an_error_verdict_on_create_ads_own_refusal_completes_the_step(
        goal_turn, completion):
    case = completion(AD_STEP, _spark_refusal(), 'create_ad')
    case.h._state(case.S.STATUS_VERIFICATION_REQUESTED)
    out = case.verdict('error', message='create_ad failed 20 times')
    assert out['action'] == 'force_fallback', out
    assert case.state() == case.S.COMPLETED
    assert _receipt_index(case) == [2]


# ── (c) another tool's refusal is not this step's ─────────────────────

def test_another_tools_finish_without_refusal_does_not_settle_it(completion):
    case = completion(AD_STEP, image_not_made('txt2img', 'ReadTimeout'),
                      'txt2img', also_serves=('create_ad',))
    verdict = {'status': 'pending', 'action_id': 1}
    assert case.lh.verdict_the_tool_settles(case.h.UP, 1, verdict) is verdict
    case.h._state(case.S.STATUS_VERIFICATION_REQUESTED)
    assert case.verdict('pending')['action'] == 'force_completion'
    assert case.state() == case.S.PENDING


# ── (d) a refusal a fixed call can clear ──────────────────────────────

def test_a_retry_after_fix_refusal_keeps_pending_and_error(goal_turn,
                                                           completion):
    case = completion(AD_STEP, _budget_refusal(), 'create_ad')
    for status in ('pending', 'error'):
        verdict = {'status': status, 'action_id': 1}
        assert case.lh.verdict_the_tool_settles(case.h.UP, 1, verdict) is verdict
    case.h._state(case.S.STATUS_VERIFICATION_REQUESTED)
    assert case.verdict('pending')['action'] == 'force_completion'
    assert case.state() == case.S.PENDING
    case.h._state(case.S.IN_PROGRESS, case.S.STATUS_VERIFICATION_REQUESTED)
    assert case.verdict('error', message='budget')['action'] == 'force_fallback'
    assert case.state() == case.S.ERROR


# ── (e) a step that waits on the person ───────────────────────────────

def test_a_step_whose_ask_card_is_out_stays_pending(completion):
    asked = ('{"status": "asked", "ask_id": "a1", "note": "The card is out; '
             'the answer comes on the person\'s turn."}')
    case = completion('ask_person: ask the owner which ad budget to spend',
                      asked, 'ask_person')
    verdict = {'status': 'pending', 'action_id': 1}
    assert case.lh.verdict_the_tool_settles(case.h.UP, 1, verdict) is verdict
    case.h._state(case.S.STATUS_VERIFICATION_REQUESTED)
    assert case.verdict('pending')['action'] == 'force_completion'
    assert case.state() == case.S.PENDING


# ── review of a0c9ee91 ────────────────────────────────────────────────
# MUST FIX 1: a step that names two tools settles only once every tool it
# names has returned its work (the gate's _work_results rule).

TWO_TOOL_STEP = ('text_2_image: make a picture of rice, then '
                 'create_social_post with that picture')
_POSTED = '{"success": true, "post_id": "p1"}'


def _two_tool_case(completion):
    return completion(TWO_TOOL_STEP, image_not_made('txt2img', 'ReadTimeout'),
                      'txt2img', also_serves=('text_2_image',
                                              'create_social_post'))


def test_a_two_tool_step_stays_pending_until_its_second_tool_ran(completion):
    case = _two_tool_case(completion)
    verdict = {'status': 'pending', 'action_id': 1}
    assert case.lh.verdict_the_tool_settles(case.h.UP, 1, verdict) is verdict
    case.h._state(case.S.STATUS_VERIFICATION_REQUESTED)
    assert case.verdict('pending')['action'] == 'force_completion'
    assert case.state() == case.S.PENDING


def test_a_two_tool_step_settles_once_its_second_tool_ran(completion):
    from tests.unit.test_completion_needs_real_work import _call, _result
    case = _two_tool_case(completion)
    case.h.gc.messages += [_call('c2', 'create_social_post'),
                           _result('c2', _POSTED)]
    settled = case.lh.verdict_the_tool_settles(
        case.h.UP, 1, {'status': 'pending', 'action_id': 1})
    assert settled['status'] == 'completed', settled
    assert settled['evidence']['message_index'] == 2
    assert case.verdict('pending')['action'] == 'force_fallback'
    assert case.state() == case.S.COMPLETED


def test_a_saved_copy_of_a_refusal_does_not_skip_the_post(goal_turn,
                                                          completion):
    """get_data_by_key hands back create_ad's saved refusal; the step also
    names post_to_channel, which never ran."""
    case = completion('get_data_by_key: read the saved ad result, then '
                      'post_to_channel to announce it', _spark_refusal(),
                      'get_data_by_key', also_serves=('post_to_channel',))
    verdict = {'status': 'pending', 'action_id': 1}
    assert case.lh.verdict_the_tool_settles(case.h.UP, 1, verdict) is verdict
    case.h._state(case.S.STATUS_VERIFICATION_REQUESTED)
    assert case.verdict('pending')['action'] == 'force_completion'
    assert case.state() == case.S.PENDING


# FIX 2: the world model never learns a refusal as a success.

def _learning_sinks(case, monkeypatch):
    """The real _promote_verified_outcome (the harness stubs it) with the
    bridge and Agent Lightning faked at their import seams."""
    from unittest import mock
    import integrations.agent_engine.world_model_bridge as wmb
    import integrations.agent_lightning as al
    bridge = mock.Mock()
    monkeypatch.setattr(wmb, 'get_world_model_bridge', lambda: bridge)
    credit = mock.Mock(return_value=True)
    monkeypatch.setattr(al, 'record_verified_outcome_for_agents', credit)
    case.h.patch(case.lh, '_promote_verified_outcome', _REAL_PROMOTE)
    return bridge, credit


def test_a_step_settled_by_a_refusal_is_not_learned_as_a_success(
        goal_turn, completion, monkeypatch):
    case = completion(AD_STEP, _spark_refusal(), 'create_ad')
    bridge, credit = _learning_sinks(case, monkeypatch)
    assert case.verdict('pending', **LIVE_PENDING)['action'] == 'force_fallback'
    assert case.state() == case.S.COMPLETED
    sent = bridge.record_interaction.call_args.kwargs
    assert sent['verification']['outcome'] == 'failure', sent['verification']
    assert credit.call_args.args[1] is False


def test_a_real_receipt_is_still_learned_as_a_success(completion, monkeypatch):
    case = completion(AD_STEP, '{"success": true, "ad_id": "ad1"}', 'create_ad')
    bridge, credit = _learning_sinks(case, monkeypatch)
    out = case.verdict('completed',
                       evidence={'message_index': 2, 'kind': 'tool_receipt'})
    assert out['action'] == 'force_fallback', out
    sent = bridge.record_interaction.call_args.kwargs
    assert sent['verification']['outcome'] == 'success'
    assert credit.call_args.args[1] is True


# MINOR: the owner's open question keeps the step open.

def test_a_question_asked_after_the_refusal_keeps_the_step_open(
        goal_turn, completion):
    from tests.unit.test_completion_needs_real_work import _call, _result
    case = completion(AD_STEP, _spark_refusal(), 'create_ad')
    case.h.gc.messages += [
        _call('s1', 'send_message_to_user',
              '{"text": "Shall I give system_daemon 50 Spark?"}'),
        _result('s1', 'Message sent successfully to user with request_id: r1')]
    verdict = {'status': 'pending', 'action_id': 1,
               'can_perform_without_user_input': 'no'}
    assert case.lh.verdict_the_tool_settles(case.h.UP, 1, verdict) is verdict


# MINOR: the whole state_transition, flat error verdict included.

@pytest.fixture
def selector(test_user_id, test_prompt_id, mock_flask_app, sample_config_json,
             monkeypatch):
    """The REAL state_transition of create_agents (the seam of
    test_create_gate_stops_the_chat), its receipt registries faked."""
    from types import SimpleNamespace
    from tests.unit.test_completion_needs_real_work import _Ledger, _call, _result
    from tests.unit.test_create_gate_stops_the_chat import _built_selector
    import hartos.lifecycle_hooks as lh
    import core.agent_tools as agent_tools
    monkeypatch.setattr(agent_tools, 'camera_turn_id',
                        lambda: 'daemon_09bbc67a-goal')
    with _built_selector(test_user_id, test_prompt_id) as built:
        log = [{'role': 'user', 'name': 'ChatInstructor',
                'content': f'Execute Action 1: {AD_STEP}'},
               _call('c1', 'create_ad'), _result('c1', _spark_refusal())]
        group = SimpleNamespace(messages=log, agents=[SimpleNamespace(
            name='Helper', llm_config=None,
            _function_map={'create_ad': (lambda **_kw: None)})])
        monkeypatch.setattr(lh, 'get_registered_groupchat', lambda _up: group)
        monkeypatch.setattr(lh, 'get_registered_ledger',
                            lambda _up: _Ledger(AD_STEP))
        lh.force_state_through_valid_path(built.up, 1, lh.ActionState.IN_PROGRESS,
                                          'test')
        built.group, built.lh = group, lh
        yield built


def _verdict_message(**verdict):
    import json
    return {'role': 'user', 'name': 'StatusVerifier',
            'content': json.dumps({'action_id': 1, **verdict})}


@pytest.mark.parametrize('verdict', [
    {'status': 'error', 'message': 'create_ad failed 20 times'},
    {'status': 'pending', 'retry_allowed': False,
     'resume_when': 'system_daemon has at least 50 Spark',
     'can_perform_without_user_input': 'no'},
])
def test_state_transition_hands_the_settled_step_to_the_completion_owner(
        selector, verdict):
    s = selector
    s.group.messages.append(_verdict_message(**verdict))
    chosen = s.select(s.verify, s.group)
    assert chosen is s.chat_instructor, chosen
    assert s.lh.get_action_state(s.up, 1) not in (
        s.lh.ActionState.ERROR, s.lh.ActionState.PENDING)


def test_state_transition_still_records_a_genuine_error(selector):
    from tests.unit.test_completion_needs_real_work import _result
    s = selector
    s.group.messages[2] = _result('c1', _budget_refusal())
    s.group.messages.append(_verdict_message(status='error',
                                             message='budget under 50'))
    assert s.select(s.verify, s.group) is not s.chat_instructor


import hartos.lifecycle_hooks as _lh  # noqa: E402
_REAL_PROMOTE = _lh._promote_verified_outcome
