"""An autonomous creation saves the plan that was approved, as it was approved.

Measured 2026-11-02 (hartos-c9), Syllable Coach 1793359802508 on the owner's
desktop, all written 30 Oct 17:00: the three staged review rounds
(prompts/1793359802508.proposed.r0/r1/r2.json) keep the creator's order, "greet
Arjun, say ball slowly, ask him to say ball" and THEN "get_chat_history to read
Arjun's latest attempt"; the saved config (1793359802508.json, 17:00:33) has the
read first and both steps reworded.  _autonomous_gather_info finalized an
approved plan by sending 'approved' to gather_info and saving whatever the model
re-emitted, at three sites: the reviewer's approval, the oscillation
force-approve and the escalation after three rounds.  The re-emission is a new
LLM answer, so the saved agent was not the plan anyone reviewed, and its reuse
read the child's attempt before asking for it.

Now each site finalizes the plan in code (status 'completed', review_required
dropped).  The escalation saves the refinement that answered the last feedback,
staged as its own round; only when nothing parses as a plan does it still ask
gather_info, as before.

    python -m pytest tests/unit/test_the_approved_plan_is_the_saved_plan.py -q
"""
import json
import os
import sys
import tempfile

os.environ.setdefault('HEVOLVE_DB_PATH', ':memory:')
os.environ.setdefault('HEVOLVE_CACHE_DIR', tempfile.mkdtemp())

from types import SimpleNamespace  # noqa: E402
from unittest.mock import MagicMock  # noqa: E402

import pytest  # noqa: E402

from core.prompt_files import proposed_plan_filename  # noqa: E402

USER = 'u-plan'
PID = 1793359802599

ASK = 'send_message_to_user: greet Arjun, say ball slowly, ask him to say ball'
READ = "get_chat_history to read Arjun's latest attempt"
CREATOR = ['save_data_in_memory key target_word value ball', ASK, READ,
           'send_message_to_user: warm feedback on the attempt']
# What the model wrote when asked to re-emit the approved plan on 30 Oct: the
# read moved ahead of the ask and both reworded.
REWRITTEN = ['save_data_in_memory key target_word value ball',
             "get_chat_history to retrieve Arjun's latest attempt text",
             'send_message_to_user: greet Arjun, say ball slowly, and ask him to say ball',
             'send_message_to_user: warm feedback on the attempt']


def _plan(status, actions, **extra):
    plan = {'status': status, 'name': 'Syllable Coach', 'agent_name': 'syllable.coach',
            'goal': 'Arjun says ball', 'broadcast_agent': False,
            'personas': [{'name': 'Speech Companion', 'description': 'speaks to Arjun'}],
            'flows': [{'flow_name': 'Syllable exercise ball', 'persona': 'Speech Companion',
                       'actions': list(actions), 'sub_goal': 'Arjun says ball'}]}
    if status == 'proposed_plan':
        plan['review_required'] = True
    plan.update(extra)
    return json.dumps(plan)


@pytest.fixture(scope='module')
def hie():
    import hart_intelligence_entry
    return hart_intelligence_entry


@pytest.fixture
def run(hie, tmp_path, monkeypatch):
    """Drive the real _autonomous_gather_info.  ``answers`` are gather_info's
    replies in order; any message that starts with 'approved' gets the
    model's re-emission (REWRITTEN, status completed), as on 30 Oct.
    ``verdicts`` are the reviewer's, in order."""
    monkeypatch.setattr(hie, 'PROMPTS_DIR', str(tmp_path))
    monkeypatch.setattr(hie, 'pooled_post', MagicMock(name='pooled_post'))
    state = SimpleNamespace(answers=[], verdicts=[], sent=[])

    def gather_info(user_id, message, prompt_id, autonomous=False):
        state.sent.append(message)
        if str(message).startswith('approved'):
            return _plan('completed', REWRITTEN)
        return state.answers.pop(0)

    monkeypatch.setitem(sys.modules, 'hartos.gather_agentdetails',
                        SimpleNamespace(gather_info=gather_info))
    monkeypatch.setattr(hie, '_review_proposed_plan',
                        lambda plan, remaining: state.verdicts.pop(0))

    def go():
        hie._autonomous_gather_info(USER, 'Create an agent named "Syllable Coach"', PID)
        with open(tmp_path / f'{PID}.json', encoding='utf-8') as f:
            return json.load(f)

    state.go = go
    state.staged = lambda n: json.loads(
        (tmp_path / proposed_plan_filename(PID, n)).read_text(encoding='utf-8'))
    return state


def _actions(config):
    return config['flows'][0]['actions']


def _asked_to_approve(state):
    return [m for m in state.sent if str(m).startswith('approved')]


def test_the_reviewers_approval_saves_the_plan_it_approved(run):
    run.answers = [_plan('proposed_plan', CREATOR)]
    run.verdicts = [('approved', '')]
    saved = run.go()
    assert _actions(saved) == CREATOR, (
        'the saved agent is the model re-emitting the approved plan, not the '
        'plan the reviewer approved: the read moved ahead of the ask')
    assert saved['status'] == 'completed'
    assert 'review_required' not in saved
    assert (saved['prompt_id'], saved['creator_user_id']) == (PID, USER)
    assert _asked_to_approve(run) == []


def test_the_oscillation_force_approve_saves_the_newest_plan_as_it_stands(run):
    short = CREATOR[:2]
    longer = CREATOR + ['save_data_in_memory key last_attempt value the attempt',
                        'save_data_in_memory key exercise_result value practised',
                        'send_message_to_user: say goodbye to Arjun']
    run.answers = [_plan('proposed_plan', short), _plan('proposed_plan', longer)]
    run.verdicts = [('needs_refinement', 'add the remaining steps')]
    saved = run.go()
    assert _actions(saved) == longer
    assert saved['status'] == 'completed' and 'review_required' not in saved
    assert _asked_to_approve(run) == []
    # The force-approved plan was never reviewed; it is staged as its own
    # round, like the escalation's (review of cdeca7500, F4).
    assert _actions(run.staged(1)) == longer


def test_the_escalation_saves_the_refinement_that_answered_the_last_feedback(run):
    rounds = [CREATOR,
              [a.replace('warm feedback', 'kind feedback') for a in CREATOR],
              [a.replace('warm feedback', 'gentle feedback') for a in CREATOR]]
    refinement = [a.replace('warm feedback', 'short warm feedback') for a in CREATOR]
    assert refinement != rounds[2]
    run.answers = [_plan('proposed_plan', r) for r in rounds] + [
        _plan('proposed_plan', refinement)]
    run.verdicts = [('needs_refinement', 'tighten the feedback step')] * 3
    saved = run.go()
    assert _actions(saved) == refinement, (
        'the escalation saved something other than the newest plan')
    assert saved['status'] == 'completed' and 'review_required' not in saved
    assert _asked_to_approve(run) == []
    # The saved plan was never reviewed; it is staged as its own round,
    # stamped like every other round (review of cdeca7500, mutant M6).
    staged = run.staged(3)
    assert _actions(staged) == refinement
    assert (staged['prompt_id'], staged['creator_user_id']) == (PID, USER)
    assert _actions(run.staged(2)) == rounds[2]


def test_a_refinement_the_model_marked_completed_is_saved_as_it_stands(run):
    """Review of cdeca7500, F1 (ran by its reviewer): when the round-3
    refinement comes back with status 'completed', the escalation still
    sent 'approved (escalated...)' to gather_info and saved the
    re-emission.  The newest plan is final whichever status the model gave
    it."""
    rounds = [CREATOR,
              [a.replace('warm feedback', 'kind feedback') for a in CREATOR],
              [a.replace('warm feedback', 'gentle feedback') for a in CREATOR]]
    refinement = [a.replace('warm feedback', 'short warm feedback') for a in CREATOR]
    run.answers = [_plan('proposed_plan', r) for r in rounds] + [
        _plan('completed', refinement)]
    run.verdicts = [('needs_refinement', 'tighten the feedback step')] * 3
    saved = run.go()
    assert _actions(saved) == refinement
    assert _asked_to_approve(run) == []
    assert _actions(run.staged(3)) == refinement


def test_the_approved_plan_keeps_text_outside_ascii(run):
    """The reviewer's probe: non-ASCII words in the steps reach the saved
    file unchanged (a coach that says the word in the child's language)."""
    steps = ['send_message_to_user: say "பந்து" (ball) slowly and ask Arjun to say it',
             "get_chat_history to read Arjun's attempt at பந்து",
             'send_message_to_user: warm feedback, très bien']
    run.answers = [_plan('proposed_plan', steps)]
    run.verdicts = [('approved', '')]
    assert _actions(run.go()) == steps


def test_an_escalation_with_no_plan_to_save_still_asks_gather_as_before(run):
    run.answers = [_plan('proposed_plan', CREATOR)] * 3 + ['I could not refine it.']
    run.verdicts = [('needs_refinement', 'tighten the feedback step')] * 3
    saved = run.go()
    assert len(_asked_to_approve(run)) == 1
    assert _actions(saved) == REWRITTEN
