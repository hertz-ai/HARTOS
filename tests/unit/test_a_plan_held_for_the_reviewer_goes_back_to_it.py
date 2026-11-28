"""A daemon goal's plan held only because the reviewer gave no verdict goes
back to the reviewer when the record proves its card was never answered; it
is not put to the owner again.

Measured on central 2026-11-19 (build b1a4c64c5): goals fb84cdce (textile
supplier sourcing) and 5ba50f98 (rice order desk) each had a plan staged and
held on 18 Nov 05:34Z with the stored reason "the reviewer could not be
reached" (the free model's quota was spent).  b1a4c64c5 put the reviewer on
the turn's model, but every daemon tick read the lapsed card in
_held_plan_turn and asked the owner again with that stored reason, so the
working reviewer never saw either plan.  Owner, 11-02: an agent not built
goes through the autonomous create's review stage, with questions to the
owner only when it cannot progress on its own.

Review of 25d078b8b (rejected): ask_state reads a card the owner DENIED as
'expired' after a restart, and 'not_found' when its row cannot be read, and
25d078b8b took both for "never answered" and built the denied plan.  Only the
durable record (person_ask.unanswered_on_record: the row never marked read,
which every answer and dismissal does) sends a plan back to the reviewer;
anything else asks the owner, as before.  And a person's own turn keeps the
parent's behaviour: their new request is never answered with their old plan.

Real person_ask (ask, ask_state, answer_ask, dismiss_ask, its rows in the
in-memory social DB); the card push, WAMP and Firebase are the boundaries.

    python -m pytest tests/unit/test_a_plan_held_for_the_reviewer_goes_back_to_it.py -q
"""
import json

import pytest

from integrations.agent_engine import person_ask as _person_ask
from tests.unit.test_the_approved_plan_is_the_saved_plan import (  # noqa: F401
    APPROVE, CREATOR, OWNER, PID, REFINEMENT, USER, _actions, _held, _oscillate,
    _plan, hie, run)

#: The real ask and its reader, before the ``run`` fixture fakes them.
REAL_ASK, REAL_ASK_STATE = _person_ask.ask, _person_ask.ask_state
REASONS = ['the reviewer could not be reached', 'the reviewer answered no verdict']
DAEMON = dict(user='daemon-persona', background=True, request_id='daemon_g1')


@pytest.fixture
def real(run, monkeypatch):
    """``run`` with the real person_ask: cards filed as rows in the social DB,
    answered and dismissed through answer_ask / dismiss_ask, a restart being
    the in-process asks dropped."""
    from integrations.agent_engine import liquid_ui_service as lui
    from integrations.social.models import Base, User, db_session, get_engine
    import core.fcm_sync as fcm
    import integrations.social.realtime as rt
    Base.metadata.create_all(get_engine())
    for uid in (OWNER, USER):
        with db_session() as db:
            if not db.query(User).filter_by(id=uid).first():
                db.add(User(id=uid, username=f'p{uid}', display_name=f'P {uid}',
                            user_type='human', api_token=f'tok-{uid}'))
    monkeypatch.setattr(fcm, 'send_fcm_push', lambda *a, **k: True)
    monkeypatch.setattr(rt, 'publish_event', lambda *a, **k: None)
    monkeypatch.setattr(lui, 'push_agent_ui', lambda agent_id, card, user_id=None: True)
    monkeypatch.setattr(lui, 'withdraw_agent_ui', lambda msg_id: 0, raising=False)
    filed = []

    def ask(*a, **k):
        out = REAL_ASK(*a, **k)
        filed.append(out)
        return out
    monkeypatch.setattr(_person_ask, 'ask', ask)
    monkeypatch.setattr(_person_ask, 'ask_state', REAL_ASK_STATE)
    with _person_ask._lock:
        _person_ask._asks.clear()
    run.filed = filed
    run.restart = lambda: _person_ask._asks.clear()
    yield run
    with _person_ask._lock:
        _person_ask._asks.clear()


def _hold_for_the_reviewer(run, why, **turn):
    """The plan held on its card because the reviewer gave no verdict."""
    run.answers = [_plan('proposed_plan', CREATOR)]
    run.verdicts = [('unreviewed', why)]
    run.call(**turn)
    staged, hold = _held(run)
    assert hold['why'] == why and _actions(staged) == CREATOR
    assert len(run.filed) == 1 and run.filed[0]['ok'] and len(run.reviewed) == 1
    return hold


def _saved(run):
    with open(run.saved_path, encoding='utf-8') as f:
        return json.load(f)


def _asked_again(run, reply, reviewed):
    """Today's behaviour: the owner is asked again, nothing is built, and the
    reviewer is not asked."""
    assert len(run.reviewed) == reviewed, 'the plan went to the reviewer'
    assert not run.saved_path.exists(), 'a plan nobody approved was built'
    assert len(run.filed) == 2, 'the owner was not asked again'
    assert 'waiting' in reply.lower() or 'approval' in reply


@pytest.mark.parametrize('why', REASONS)
def test_a_daemon_goals_card_never_answered_goes_back_to_the_reviewer(real, why):
    """Central's case: the deploy's restart left the card held by no process;
    its row was never read, so nobody answered it."""
    from core.agent_tools import is_waiting_for_person
    hold = _hold_for_the_reviewer(real, why, **DAEMON)
    assert hold['asked'] == OWNER
    real.restart()
    assert REAL_ASK_STATE(hold['ask_id'], OWNER)['status'] == 'expired'
    sent_before = list(real.sent)
    real.verdicts = [('approved', None)]
    reply = real.call(**DAEMON)
    assert real.reviewed[1:] == [CREATOR], 'the held plan never went back to the reviewer'
    assert not is_waiting_for_person(reply), reply
    saved = _saved(real)
    assert _actions(saved) == CREATOR and saved['creator_user_id'] == 'daemon-persona'
    assert saved['status'] == 'completed' and 'review_required' not in saved
    assert 'hold' not in saved
    assert len(real.filed) == 1, 'the owner was asked again'
    assert real.sent == sent_before, 'the approved plan was re-asked of the planner'
    assert _held(real)[1] is None


@pytest.mark.parametrize('closed', ['deny', 'dismiss', 'approve'])
def test_a_card_answered_before_the_restart_is_never_taken_for_no_answer(real, closed):
    """MUST-FIX of the review: denied, then a restart, ask_state reads
    'expired'; 25d078b8b had the reviewer approve it and built it."""
    hold = _hold_for_the_reviewer(real, REASONS[0], **DAEMON)
    if closed == 'dismiss':
        assert _person_ask.dismiss_ask(hold['ask_id'], OWNER)[1] == 200
    else:
        decision = {'deny': 'deny', 'approve': 'approve'}[closed]
        assert _person_ask.answer_ask(hold['ask_id'], {'decision': decision}, OWNER)[1] == 200
    real.restart()
    assert REAL_ASK_STATE(hold['ask_id'], OWNER)['status'] == 'expired'
    real.verdicts = [('approved', None)]
    reply = real.call(**DAEMON)
    _asked_again(real, reply, reviewed=1)


@pytest.mark.parametrize('closed', ['deny', 'dismiss'])
def test_an_answer_whose_record_cannot_be_written_is_not_taken(real, monkeypatch, closed):
    """Review of dbde21415: the owner denied and was told 200, but marking
    the row read failed ('database is locked'), so after a restart the
    unread row read as never answered and the denied plan was built.  The
    record is written first: when it fails the owner is told so, the card
    stays open, nothing is built, and a retry that is recorded holds after
    the restart."""
    from integrations.social.services import NotificationService
    hold = _hold_for_the_reviewer(real, REASONS[0], **DAEMON)
    real_mark_read = NotificationService.mark_read

    def locked(*a, **k):
        raise RuntimeError('database is locked')
    monkeypatch.setattr(NotificationService, 'mark_read', staticmethod(locked))

    def close():
        if closed == 'dismiss':
            return _person_ask.dismiss_ask(hold['ask_id'], OWNER)
        return _person_ask.answer_ask(hold['ask_id'], {'decision': 'deny'}, OWNER)
    body, code = close()
    assert code != 200 and body['reason'] == 'not_recorded', (code, body)
    assert REAL_ASK_STATE(hold['ask_id'], OWNER)['status'] == 'open'
    real.verdicts = [('approved', None)]
    reply = real.call(**DAEMON)
    assert 'waiting on the card' in reply
    assert len(real.reviewed) == 1 and not real.saved_path.exists()
    monkeypatch.setattr(NotificationService, 'mark_read', staticmethod(real_mark_read))
    assert close()[1] == 200
    real.restart()
    reply = real.call(**DAEMON)
    _asked_again(real, reply, reviewed=1)


def test_a_card_whose_row_is_gone_is_put_to_the_owner_again(real):
    from integrations.social.models import Notification, db_session
    hold = _hold_for_the_reviewer(real, REASONS[0], **DAEMON)
    real.restart()
    with db_session() as db:
        db.query(Notification).filter_by(id=hold['ask_id']).delete()
    assert REAL_ASK_STATE(hold['ask_id'], OWNER)['status'] == 'not_found'
    real.verdicts = [('approved', None)]
    reply = real.call(**DAEMON)
    _asked_again(real, reply, reviewed=1)


def test_a_card_whose_row_cannot_be_read_builds_nothing(real, monkeypatch):
    """The database down: ask_state reads 'not_found' and the record proves
    nothing, so nothing is built and the reviewer is not asked (the re-ask
    cannot be filed either, and the turn says so)."""
    import integrations.social.models as models
    hold = _hold_for_the_reviewer(real, REASONS[0], **DAEMON)
    real.restart()

    def down(*a, **k):
        raise RuntimeError('database is locked')
    monkeypatch.setattr(models, 'db_session', down)
    assert REAL_ASK_STATE(hold['ask_id'], OWNER)['status'] == 'not_found'
    assert _person_ask.unanswered_on_record(hold['ask_id'], OWNER) is False
    real.verdicts = [('approved', None)]
    real.call(**DAEMON)
    assert len(real.reviewed) == 1, 'the plan went to the reviewer'
    assert not real.saved_path.exists()


def test_a_persons_new_request_is_never_answered_with_their_old_plan(real):
    """Review of 25d078b8b, MEDIUM: after a lapsed hold the person asked for
    another agent and was answered with the old plan built.  A person's own
    turn keeps the parent's behaviour: the old plan waits on its card."""
    from core.prompt_files import held_plan
    hold = _hold_for_the_reviewer(real, REASONS[0])
    assert hold['asked'] == USER
    real.restart()
    sent_before = list(real.sent)
    real.verdicts = [('approved', None)]
    real.answers = [_plan('proposed_plan', ['send_message_to_user: a recipe'])]
    reply = real.call(description='Create an agent named "Recipe Helper"')
    assert reply.startswith('The plan for Syllable Coach') and 'waits for your approval' in reply
    assert 'Agent details gathered' not in reply
    _asked_again(real, reply, reviewed=1)
    assert real.sent == sent_before
    assert str(held_plan(USER)) == str(PID)


@pytest.mark.parametrize('why', REASONS)
def test_a_reviewer_still_unreachable_puts_it_to_the_owner_again(real, why):
    _hold_for_the_reviewer(real, why, **DAEMON)
    real.restart()
    real.verdicts = [('unreviewed', REASONS[0])]
    real.call(**DAEMON)
    assert len(real.reviewed) == 2, 'the held plan never went back to the reviewer'
    assert len(real.filed) == 2 and real.filed[1]['ok']
    staged, hold = _held(real)
    assert hold['ask_id'] == real.filed[1]['ask_id'] and hold['why'] == REASONS[0]
    assert _actions(staged) == CREATOR
    assert not real.saved_path.exists()


def test_a_reviewer_asking_for_a_fix_sends_the_plan_to_the_planner(real):
    """Refinement is the review loop's own path.  After a restart the planner
    holds no conversation for the plan, so the feedback carries the plan."""
    _hold_for_the_reviewer(real, REASONS[0], **DAEMON)
    real.restart()
    real.verdicts = [('needs_refinement', 'tighten the feedback step'), ('approved', None)]
    real.answers = [_plan('proposed_plan', REFINEMENT)]
    real.call(**DAEMON)
    assert real.reviewed[1:] == [CREATOR, REFINEMENT]
    refine = real.sent[-1]
    assert refine.startswith('tighten the feedback step')
    assert all(step in refine for step in CREATOR), 'the planner was not shown the plan'
    assert _actions(_saved(real)) == REFINEMENT
    assert len(real.filed) == 1


@pytest.mark.parametrize('answer', ['approve', 'deny', 'open'])
def test_the_owners_answer_wins_over_the_reviewer(real, answer):
    hold = _hold_for_the_reviewer(real, REASONS[0], **DAEMON)
    if answer != 'open':
        assert _person_ask.answer_ask(hold['ask_id'], {'decision': answer}, OWNER)[1] == 200
    real.verdicts = [('needs_refinement', 'the reviewer must not be asked')]
    reply = real.call(**DAEMON)
    assert len(real.reviewed) == 1, 'the reviewer was asked over the owner'
    assert len(real.filed) == 1
    if answer == 'approve':
        assert _actions(_saved(real)) == CREATOR
    elif answer == 'deny':
        assert 'Dropped the plan' in reply and not real.saved_path.exists()
    else:
        assert 'waiting on the card' in reply and not real.saved_path.exists()


def test_a_hold_for_another_reason_is_put_to_the_owner_again(real):
    """The yo-yo hold is the plan's own trouble, not the reviewer's."""
    _oscillate(real)
    real.call(**DAEMON)
    assert _held(real)[1]['why'] not in REASONS
    reviewed = len(real.reviewed)
    real.restart()
    real.verdicts = [('approved', None)]
    reply = real.call(**DAEMON)
    _asked_again(real, reply, reviewed=reviewed)


@pytest.mark.parametrize('typed', ['yes, build it', 'no', 'what does step 2 do?'])
@pytest.mark.parametrize('turn', [DAEMON, {}], ids=['daemon', 'person'])
def test_typed_words_on_the_plan_card_are_neither_a_yes_nor_a_no(real, typed, turn):
    """Review of Nunba 12b62b19 (rv_ci, 2026-11-28): the desktop card's
    "Or type your answer" box sends {text} with no decision.  On a join ask
    that was recorded as a refusal (fixed in cbc0ca8ca).  On a plan card
    nothing reads typed words either: the plan is not built and not
    dropped, and the owner is asked again with the buttons."""
    hold = _hold_for_the_reviewer(real, REASONS[0], **turn)
    body, code = _person_ask.answer_ask(hold['ask_id'], {'text': typed}, hold['asked'])
    assert code == 200, (code, body)
    real.verdicts = [('approved', None)]
    reply = real.call(**turn)
    assert 'Dropped the plan' not in reply, reply
    assert not real.saved_path.exists(), 'typed words built the plan'
    assert _held(real)[1] is not None, 'the held plan was dropped'
    assert len(real.filed) == 2, 'the owner was not asked again'
