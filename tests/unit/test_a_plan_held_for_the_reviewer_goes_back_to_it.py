"""A plan held only because the reviewer gave no verdict goes back to the
reviewer when its card lapses unanswered; it is not put to the owner again.

Measured on central 2026-11-19 (build b1a4c64c5): goals fb84cdce (textile
supplier sourcing) and 5ba50f98 (rice order desk) each had a plan staged and
held on 18 Nov 05:34Z with the stored reason "the reviewer could not be
reached" (the free model's quota was spent).  b1a4c64c5 put the reviewer on
the turn's model, but every daemon tick read the lapsed card in
_held_plan_turn and asked the owner again with that stored reason, so the
working reviewer never saw either plan and both goals waited on a card all
day.  Owner, 11-02: an agent not built goes through the autonomous create's
review stage, with questions to the owner only when it cannot progress on its
own.  A plan whose only obstacle was the reviewer can.

    python -m pytest tests/unit/test_a_plan_held_for_the_reviewer_goes_back_to_it.py -q
"""
import pytest

from tests.unit.test_the_approved_plan_is_the_saved_plan import (  # noqa: F401
    APPROVE, CREATOR, OWNER, PID, REFINEMENT, USER, _actions, _held, _oscillate,
    _plan, hie, run)

REASONS = ['the reviewer could not be reached', 'the reviewer answered no verdict']
LAPSED = [{'status': 'expired', 'answer': None}, {'status': 'not_found', 'answer': None}]
DAEMON = dict(user='daemon-persona', background=True, request_id='daemon_g1')


def _hold_for_the_reviewer(run, why, **turn):
    """The plan held on its creator's card because the reviewer gave no verdict."""
    run.answers = [_plan('proposed_plan', CREATOR)]
    run.verdicts = [('unreviewed', why)]
    run.call(**turn)
    staged, hold = _held(run)
    assert hold['why'] == why and _actions(staged) == CREATOR
    assert len(run.asks.sent) == 1 and len(run.reviewed) == 1


def _saved(run):
    import json
    with open(run.saved_path, encoding='utf-8') as f:
        return json.load(f)


@pytest.mark.parametrize('lapsed', LAPSED)
@pytest.mark.parametrize('why', REASONS)
def test_a_lapsed_card_sends_the_plan_back_to_a_working_reviewer(run, why, lapsed):
    from core.prompt_files import held_plan
    _hold_for_the_reviewer(run, why)
    sent_before = list(run.sent)
    run.asks.state = lapsed
    run.verdicts = [('approved', None)]
    saved = run.go()
    assert run.reviewed[-1] == CREATOR and len(run.reviewed) == 2, (
        'the held plan never went back to the reviewer')
    assert len(run.asks.sent) == 1, 'the owner was asked again'
    assert _actions(saved) == CREATOR
    assert saved['status'] == 'completed' and 'review_required' not in saved
    assert 'hold' not in saved
    assert (saved['prompt_id'], saved['creator_user_id']) == (PID, USER)
    assert run.sent == sent_before, 'the approved plan was re-asked of the planner'
    assert _held(run)[1] is None and held_plan(USER) is None


def test_a_daemon_goals_lapsed_hold_is_reviewed_and_saved_as_on_central(run):
    from core.agent_tools import is_waiting_for_person
    _hold_for_the_reviewer(run, REASONS[0], **DAEMON)
    assert _held(run)[1]['asked'] == OWNER
    run.asks.state = LAPSED[0]
    run.verdicts = [('approved', None)]
    reply = run.call(**DAEMON)
    assert not is_waiting_for_person(reply), reply
    saved = _saved(run)
    assert _actions(saved) == CREATOR and saved['creator_user_id'] == 'daemon-persona'
    assert len(run.asks.sent) == 1 and _held(run)[1] is None


@pytest.mark.parametrize('why', REASONS)
def test_a_reviewer_still_unreachable_puts_it_to_the_owner_again(run, why):
    _hold_for_the_reviewer(run, why)
    run.asks.state = LAPSED[0]
    run.verdicts = [('unreviewed', REASONS[0])]
    reply = run.call()
    assert len(run.reviewed) == 2, 'the held plan never went back to the reviewer'
    assert 'waits for your approval' in reply
    assert len(run.asks.sent) == 2
    staged, hold = _held(run)
    assert hold['ask_id'] == 'ask-2' and hold['why'] == REASONS[0]
    assert _actions(staged) == CREATOR
    assert not run.saved_path.exists()


def test_a_reviewer_asking_for_a_fix_sends_the_plan_to_the_planner(run):
    """Refinement is the review loop's own path.  After a restart the planner
    holds no conversation for the plan, so the feedback carries the plan."""
    _hold_for_the_reviewer(run, REASONS[0])
    run.asks.state = LAPSED[0]
    run.verdicts = [('needs_refinement', 'tighten the feedback step'), ('approved', None)]
    run.answers = [_plan('proposed_plan', REFINEMENT)]
    saved = run.go()
    assert run.reviewed[1:] == [CREATOR, REFINEMENT]
    refine = run.sent[-1]
    assert refine.startswith('tighten the feedback step')
    assert all(step in refine for step in CREATOR), 'the planner was not shown the plan'
    assert _actions(saved) == REFINEMENT
    assert len(run.asks.sent) == 1


@pytest.mark.parametrize('answer', ['approve', 'deny', 'open'])
def test_the_owners_answer_wins_over_the_reviewer(run, answer):
    _hold_for_the_reviewer(run, REASONS[0])
    run.asks.state = {
        'approve': APPROVE,
        'deny': {'status': 'answered', 'answer': {'decision': 'deny'}},
        'open': {'status': 'open', 'answer': None}}[answer]
    run.verdicts = [('needs_refinement', 'the reviewer must not be asked')]
    reply = run.call()
    assert len(run.reviewed) == 1, 'the reviewer was asked over the owner'
    assert len(run.asks.sent) == 1
    if answer == 'approve':
        assert _actions(_saved(run)) == CREATOR
    elif answer == 'deny':
        assert reply.startswith('Dropped the plan') and not run.saved_path.exists()
    else:
        assert 'waiting on the card' in reply and not run.saved_path.exists()


def test_a_hold_for_another_reason_is_put_to_the_owner_again(run):
    """The yo-yo hold is the plan's own trouble, not the reviewer's."""
    _oscillate(run)
    run.call()
    reviewed = len(run.reviewed)
    assert _held(run)[1]['why'] != REASONS[0]
    run.asks.state = LAPSED[0]
    run.verdicts = [('approved', None)]
    reply = run.call()
    assert len(run.reviewed) == reviewed, 'a yo-yo hold was sent to the reviewer'
    assert 'waits for your approval' in reply and len(run.asks.sent) == 2
    assert not run.saved_path.exists()


def test_a_card_that_could_not_be_read_is_put_to_the_owner_again(run, monkeypatch):
    """Unread is not lapsed: the owner may have answered, so nothing is
    built on the reviewer's word alone."""
    from integrations.agent_engine import person_ask
    _hold_for_the_reviewer(run, REASONS[0])

    def unreadable(ask_id, user_id):
        raise RuntimeError('database is locked')
    monkeypatch.setattr(person_ask, 'ask_state', unreadable)
    run.verdicts = [('approved', None)]
    reply = run.call()
    assert len(run.reviewed) == 1
    assert 'waits for your approval' in reply and len(run.asks.sent) == 2
    assert not run.saved_path.exists()
