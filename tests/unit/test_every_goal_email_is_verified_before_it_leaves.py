"""Every goal email from central is verified before it leaves.

Owner, 16 Nov 2026: "You verify all the outgoing emails on my behalf from
central."  Coordinator, 17 Nov (the shape):
  * a goal's templates: its follow-up sequences are created 'held' unless
    every step's exact copy is verified (config verified_copy: the content
    hash of each verified step, written through GoalManager.update_goal); the
    follow-up checker sends only 'active' sequences; verifying a goal adds
    the hashes of what is held for it and releases the sequences whose steps
    are then all verified; text written later, or changed by one character,
    is held again;
  * an email whose words did not come from a verified sequence step (the
    agent's send_outreach_email, a reply, the journey's first email) is held
    as a draft and not sent, without using up a send attempt; the tool says
    it goes out once approved, so the agent does not send it again;
  * one approver (a function and an owner-gated route) sends the held draft
    exactly as written through _send_email, so verify_outgoing_email still
    runs; a rejection records its reason; held drafts are listed per goal
    with subject and body, never with the recipient's address in a log.

The real tools, follow-up checker, journey engine, channel registry and
EmailAdapter on a running loop, the social DB, and the real admin blueprint
and its gate; mocked: the mail servers, the mailer, the admin config file,
UI cards, and the token lookup (as the admin gate's own tests do).
"""
import json
import logging
from unittest.mock import MagicMock, patch

import pytest

from tests.unit.test_each_goal_sends_from_its_own_mailbox import (  # noqa: F401
    HEVOLVE, MCGROCE, OWNER, _connect, _in_turn, _journey_tools, _legacy_mailbox,
    _mailbox, _prospect, _tools, channels, mail, mailer, node, web)

ANA = 'ana@clinic.example'
BODY = '<p>Hello Ana,</p><p>would a short call next week help?</p>'


def _speech_box(node):
    _connect(HEVOLVE)
    return _mailbox(node.channels, HEVOLVE)


def _write(tools, pid, goal='g-speech', subject='a question', body=BODY):
    """The goal's agent writes an email in its own turn."""
    return json.loads(_in_turn(goal, tools['send_outreach_email'], pid, subject, body))


def _held(store, goal='g-speech'):
    return store.list_held_drafts(goal_id=goal)


def _sequence(tools, pid, subject='checking in', body='<p>a quick follow-up</p>',
              goal='g-speech', steps=None):
    """The goal's agent creates a follow-up sequence in its own turn (one step
    of this copy, or ``steps``); the sequence as the tool stored it."""
    steps = steps or [{'delay_days': 0, 'subject': subject, 'body_html': body}]
    out = json.loads(_in_turn(goal, tools['create_followup_sequence'], pid,
                              'bump', json.dumps(steps)))
    assert out['success'] is True, out
    return out['sequence']


def _step(store, tools, pid, goal='g-speech'):
    steps = json.dumps([{'delay_days': 0, 'subject': 'checking in',
                         'body_html': '<p>a quick follow-up</p>'}])
    _in_turn(goal, tools['create_followup_sequence'], pid, 'bump', steps)
    seqs = store._load_prospects()['sequences']
    return next(s for s in seqs.values() if s['prospect_id'] == pid)


# ── a held draft is not sent ─────────────────────────────────────────

def test_an_email_the_agent_wrote_is_held_not_sent(node, monkeypatch):
    _speech_box(node)
    tools = _tools(monkeypatch, node.store)
    pid = _prospect(tools, 'Clinic', ANA, 'g-speech')

    first = _write(tools, pid)
    again = _write(tools, pid)                   # the same email asked twice

    assert first['held'] is True and first['sent'] is False
    assert 'held for verification' in first['note'].lower()
    assert 'do not send it again' in first['note'].lower()
    assert again['draft_id'] == first['draft_id']
    assert node.mail.sent == [] and node.mailer == []
    p = node.store._load_prospects()['prospects'][pid]
    assert (p['stage'], p['emails_sent']) == ('new', 0)
    (draft,) = _held(node.store)
    assert (draft['subject'], draft['body_html']) == ('a question', BODY)
    assert ANA not in json.dumps(draft)          # listed without the address


# ── approval sends the exact text ────────────────────────────────────

def test_approval_sends_the_held_draft_exactly_as_written(node, monkeypatch):
    _speech_box(node)
    tools = _tools(monkeypatch, node.store)
    pid = _prospect(tools, 'Clinic', ANA, 'g-speech')
    draft_id = _write(tools, pid)['draft_id']

    out = node.store.approve_held_draft(draft_id, approved_by=OWNER)

    assert out['sent'] is True, out
    (mailed,) = node.mail.sent_from(HEVOLVE)
    assert (mailed['to'], mailed['subject'], mailed['html']) == (ANA, 'a question', BODY)
    p = node.store._load_prospects()['prospects'][pid]
    assert (p['stage'], p['emails_sent']) == ('contacted', 1)
    assert _held(node.store) == []
    again = node.store.approve_held_draft(draft_id, approved_by=OWNER)
    assert again['sent'] is False and len(node.mail.sent) == 1


def test_approval_still_passes_the_outgoing_check(node, monkeypatch):
    """An approved draft goes through _send_email, so verify_outgoing_email
    runs at approval too: a draft whose recipient has since become one of
    the node's own mailboxes never leaves."""
    _speech_box(node)
    tools = _tools(monkeypatch, node.store)
    draft_id = _write(tools, _prospect(tools, 'Us', MCGROCE, 'g-speech'))['draft_id']
    _connect(MCGROCE)
    _mailbox(node.channels, MCGROCE)

    out = node.store.approve_held_draft(draft_id, approved_by=OWNER)

    assert out['sent'] is False and "node's own" in out['error']
    assert node.mail.sent == []
    assert len(_held(node.store)) == 1                 # still waiting


def test_a_rejection_records_its_reason_and_sends_nothing(node, monkeypatch):
    _speech_box(node)
    tools = _tools(monkeypatch, node.store)
    pid = _prospect(tools, 'Clinic', ANA, 'g-speech')
    draft_id = _write(tools, pid)['draft_id']

    node.store.reject_held_draft(draft_id, reason='promises an outcome', rejected_by=OWNER)

    (draft,) = node.store.list_held_drafts(goal_id='g-speech', status='rejected')
    assert draft['reason'] == 'promises an outcome'
    assert node.store.approve_held_draft(draft_id, approved_by=OWNER)['sent'] is False
    assert node.mail.sent == []


# ── an unverified goal's sequence never sends ────────────────────────

def test_an_unverified_goals_sequence_never_sends(node, monkeypatch):
    _speech_box(node)
    tools = _tools(monkeypatch, node.store)
    pid = _prospect(tools, 'Clinic', ANA, 'g-speech')
    seq = _step(node.store, tools, pid)

    for _ in range(3):
        node.store.check_pending_followups_daemon()

    assert seq['status'] == 'held'
    data = node.store._load_prospects()
    step = data['sequences'][seq['id']]['steps'][0]
    assert step['status'] == 'pending' and 'attempts' not in step
    assert node.mail.sent == []


def test_an_active_sequence_of_an_unverified_goal_is_held_by_the_checker(node, monkeypatch):
    """A sequence made active before this rule (or by hand) still waits."""
    _speech_box(node)
    tools = _tools(monkeypatch, node.store)
    pid = _prospect(tools, 'Clinic', ANA, 'g-speech')
    seq = _step(node.store, tools, pid)
    data = node.store._load_prospects()
    data['sequences'][seq['id']]['status'] = 'active'
    node.store._save_prospects(data)

    node.store.check_pending_followups_daemon()

    data = node.store._load_prospects()
    assert data['sequences'][seq['id']]['status'] == 'held'
    assert 'attempts' not in data['sequences'][seq['id']]['steps'][0]
    assert node.mail.sent == []


# ── verifying a goal releases its sequences ──────────────────────────

def test_verifying_a_goal_releases_its_sequences_and_only_its_own(node, monkeypatch):
    from integrations.social.models import AgentGoal, db_session
    for address in (HEVOLVE, MCGROCE):
        _connect(address)
        _mailbox(node.channels, address)
    tools = _tools(monkeypatch, node.store)
    speech = _step(node.store, tools, _prospect(tools, 'Clinic', ANA, 'g-speech'))
    lashes = _step(node.store, tools, _prospect(
        tools, 'Salon', 'ravi@salon.example', 'g-lashes'), goal='g-lashes')

    out = node.store.mark_goal_copy_verified('g-speech', verified_by=OWNER)
    tick = node.store.check_pending_followups_daemon()

    assert out['success'] is True and out['sequences_released'] == 1
    with db_session(commit=False) as db:
        assert db.query(AgentGoal).filter_by(id='g-speech').one().config_json[
            'verified_copy'] == [node.store.step_content_hash(
                'checking in', '<p>a quick follow-up</p>')]
    seqs = node.store._load_prospects()['sequences']
    assert seqs[lashes['id']]['status'] == 'held'
    assert tick['sent'] == 1
    assert [m['to'] for m in node.mail.sent_from(HEVOLVE)] == [ANA]
    assert node.mail.sent_from(MCGROCE) == []
    later = _step(node.store, tools, _prospect(tools, 'Clinic 2', 'bo@clinic.example', 'g-speech'))
    assert later['status'] == 'active'                 # verified: created active
    assert seqs[speech['id']]['status'] in ('active', 'completed')


def test_a_verified_goals_agent_written_email_is_still_held(node, monkeypatch):
    """Verifying a goal's copy covers its sequence steps; an email the agent
    writes outside one is still held for the owner."""
    _speech_box(node)
    node.store.mark_goal_copy_verified('g-speech', verified_by=OWNER)
    tools = _tools(monkeypatch, node.store)

    out = _write(tools, _prospect(tools, 'Clinic', ANA, 'g-speech'))

    assert out['held'] is True and node.mail.sent == []


# ── replies and the journey's first email are held too ───────────────

def test_the_journeys_first_email_and_its_messages_are_held_without_a_try(node, monkeypatch):
    from integrations.agent_engine.journey_engine import JourneyEngine
    _speech_box(node)
    tools = _tools(monkeypatch, node.store)
    journey = _journey_tools(monkeypatch)
    pid = _prospect(tools, 'Clinic', ANA, 'g-speech')
    prospect = dict(node.store._load_prospects()['prospects'][pid],
                    email_draft={'subject': 'hello', 'body': '<p>hello</p>'})

    first = JourneyEngine()._action_send_email(prospect, {})
    second = JourneyEngine()._action_send_email(prospect, {})
    msg = json.loads(_in_turn('g-speech', journey['send_prospect_message'],
                              pid, 'following up on our call', 'email'))

    assert first.get('held') is True and first.get('sent') is False
    assert second.get('held') is True
    assert 'attempts' not in prospect['email_draft']
    assert msg.get('held') is True
    assert {d['subject'] for d in _held(node.store)} == {'hello', 'Update from HevolveAI'}
    assert node.mail.sent == []


def test_a_prospects_reply_is_drafted_in_its_own_goals_turn(node, monkeypatch):
    """The reply to a prospect is written in its goal's own turn, where
    send_outreach_email holds it for the owner."""
    from integrations.agent_engine import dispatch
    dispatched = MagicMock()
    monkeypatch.setattr(dispatch, 'dispatch_goal', dispatched)
    tools = _tools(monkeypatch, node.store)
    pid = _prospect(tools, 'Clinic', ANA, 'g-speech')
    prospect = node.store._load_prospects()['prospects'][pid]

    node.store._dispatch_response_draft(prospect, {
        'their_reply': {'subject': 'Re: a question', 'body': 'yes'}, 'our_emails': []})

    assert dispatched.call_args.kwargs['goal_id'] == 'g-speech'
    assert 'held' in dispatched.call_args.kwargs['prompt']


# ── what the hold does not do ────────────────────────────────────────

def test_a_rejected_email_written_again_is_refused_with_the_reason(node, monkeypatch):
    _speech_box(node)
    tools = _tools(monkeypatch, node.store)
    pid = _prospect(tools, 'Clinic', ANA, 'g-speech')
    node.store.reject_held_draft(_write(tools, pid)['draft_id'],
                                 reason='promises an outcome', rejected_by=OWNER)

    again = _write(tools, pid)

    assert again['success'] is False and 'held' not in again
    assert 'promises an outcome' in again['error']
    assert node.store.list_held_drafts(goal_id='g-speech') == []
    assert node.mail.sent == []


def test_an_email_that_could_never_leave_is_refused_not_held(node, monkeypatch):
    """The owner is not asked to approve what cannot go: a goal whose mailbox
    is not connected, or that its creator may not use, is refused at once,
    saying what to fix, and leaves no draft."""
    _speech_box(node)
    tools = _tools(monkeypatch, node.store)
    lashes = _prospect(tools, 'Salon', 'ravi@salon.example', 'g-lashes')   # no mailbox

    out = _write(tools, lashes, goal='g-lashes')

    assert out['success'] is False and 'held' not in out
    assert MCGROCE in out['error'] and 'channel setup form' in out['error']
    assert node.store.list_held_drafts() == []


def test_a_bulk_import_holds_its_sequences_until_the_goal_is_verified(node, monkeypatch):
    _speech_box(node)
    tools = _tools(monkeypatch, node.store)
    steps = json.dumps([{'delay_days': 0, 'subject': 'bump', 'body_html': '<p>hi</p>'}])
    rows = json.dumps([{'company': 'Clinic', 'contact_name': 'Ana', 'email': ANA}])
    first = json.loads(_in_turn('g-speech', tools['bulk_import_prospects'], rows, True, steps))

    (seq,) = node.store._load_prospects()['sequences'].values()
    assert first['sequences_created'] == 1 and seq['status'] == 'held'   # made held
    node.store.check_pending_followups_daemon()
    assert node.mail.sent == []

    released = node.store.mark_goal_copy_verified('g-speech', verified_by=OWNER)
    rows2 = json.dumps([{'company': 'Shop', 'contact_name': 'Mo', 'email': 'mo@shop.example'}])
    json.loads(_in_turn('g-speech', tools['bulk_import_prospects'], rows2, True, steps))

    statuses = [s['status'] for s in node.store._load_prospects()['sequences'].values()]
    assert released['sequences_released'] == 1 and statuses == ['active', 'active']


def test_two_approvals_of_one_draft_send_it_once(node, monkeypatch):
    """While a draft is being sent it is not held: a second approval (the
    owner clicking twice, two operators) cannot send it again."""
    _speech_box(node)
    tools = _tools(monkeypatch, node.store)
    draft_id = _write(tools, _prospect(tools, 'Clinic', ANA, 'g-speech'))['draft_id']
    real_send, nested = node.store._send_email, []

    def _send_and_approve_again(*args, **kwargs):
        nested.append(node.store.approve_held_draft(draft_id, approved_by='op-2'))
        return real_send(*args, **kwargs)
    monkeypatch.setattr(node.store, '_send_email', _send_and_approve_again)

    first = node.store.approve_held_draft(draft_id, approved_by=OWNER)

    assert first['sent'] is True and len(node.mail.sent) == 1
    assert nested[0]['sent'] is False and 'sending' in nested[0]['error']


def test_a_draft_survives_a_stale_save_of_the_prospect_store(node, monkeypatch):
    """The journey tick loads the prospect store, works, and saves its copy;
    a draft the tick's own email held meanwhile is not in that copy.  Drafts
    live in their own file, so the older save cannot take one with it."""
    _speech_box(node)
    tools = _tools(monkeypatch, node.store)
    pid = _prospect(tools, 'Clinic', ANA, 'g-speech')
    stale = node.store._load_prospects()

    draft_id = _write(tools, pid)['draft_id']
    node.store._save_prospects(stale)

    assert [d['draft_id'] for d in _held(node.store)] == [draft_id]


# ── verification is of the exact copy, never of the goal's future ────

OFFER = ('a different offer', '<p>something else entirely</p>')


def _verified_goal(node, tools, store_goal='g-speech'):
    """A goal whose one sequence (the _sequence default copy) the owner has
    verified; returns that first sequence's id."""
    first = _sequence(tools, _prospect(tools, 'Clinic', ANA, store_goal), goal=store_goal)
    assert first['status'] == 'held'
    assert node.store.mark_goal_copy_verified(store_goal, verified_by=OWNER)['success']
    return first['id']


def test_after_a_goal_is_verified_a_new_sequence_with_different_text_is_held(node, monkeypatch):
    _speech_box(node)
    tools = _tools(monkeypatch, node.store)
    first_id = _verified_goal(node, tools)

    other = _sequence(tools, _prospect(tools, 'Shop', 'mo@shop.example', 'g-speech'),
                      subject=OFFER[0], body=OFFER[1])
    tick = node.store.check_pending_followups_daemon()

    stored = node.store._load_prospects()['sequences']
    assert other['status'] == 'held' and stored[other['id']]['status'] == 'held'
    assert stored[first_id]['status'] in ('active', 'completed')
    assert tick['sent'] == 1
    assert [m['subject'] for m in node.mail.sent_from(HEVOLVE)] == ['checking in']


def test_after_a_goal_is_verified_a_sequence_with_identical_text_is_active(node, monkeypatch):
    _speech_box(node)
    tools = _tools(monkeypatch, node.store)
    _verified_goal(node, tools)

    same = _sequence(tools, _prospect(tools, 'Shop', 'mo@shop.example', 'g-speech'))
    node.store.check_pending_followups_daemon()

    assert same['status'] == 'active'
    assert [m['to'] for m in node.mail.sent_from(HEVOLVE)] == [ANA, 'mo@shop.example']


@pytest.mark.parametrize('subject, body', [
    ('checking in!', '<p>a quick follow-up</p>'),         # one character of the subject
    ('checking in', '<p>a quick follow-up.</p>'),         # one character of the body
    ('checking in', '<p>a quick follow-up</p> '
                    '<a href="https://x.example/a">see</a>'),     # a link added
])
def test_any_change_to_verified_text_is_held(node, monkeypatch, subject, body):
    _speech_box(node)
    tools = _tools(monkeypatch, node.store)
    _verified_goal(node, tools)

    changed = _sequence(tools, _prospect(tools, 'Shop', 'mo@shop.example', 'g-speech'),
                        subject=subject, body=body)
    node.store.check_pending_followups_daemon()

    assert changed['status'] == 'held'
    assert [m['to'] for m in node.mail.sent_from(HEVOLVE)] == [ANA]


def test_the_same_words_wrapped_differently_are_the_same_copy(node, monkeypatch):
    _speech_box(node)
    tools = _tools(monkeypatch, node.store)
    _verified_goal(node, tools)

    rewrapped = _sequence(tools, _prospect(tools, 'Shop', 'mo@shop.example', 'g-speech'),
                          subject='  checking   in ', body='<p>a quick\n   follow-up</p>\n')

    assert rewrapped['status'] == 'active'


def test_a_sequence_is_active_only_when_every_step_is_verified(node, monkeypatch):
    _speech_box(node)
    tools = _tools(monkeypatch, node.store)
    _verified_goal(node, tools)
    known = {'delay_days': 0, 'subject': 'checking in', 'body_html': '<p>a quick follow-up</p>'}
    new = {'delay_days': 3, 'subject': OFFER[0], 'body_html': OFFER[1]}

    mixed = _sequence(tools, _prospect(tools, 'Shop', 'mo@shop.example', 'g-speech'),
                      steps=[known, new])
    node.store.check_pending_followups_daemon()

    assert mixed['status'] == 'held'
    assert [m['to'] for m in node.mail.sent_from(HEVOLVE)] == [ANA]


def test_verifying_with_nothing_held_passes_nothing_for_the_future(node, monkeypatch):
    from integrations.social.models import AgentGoal, db_session
    _speech_box(node)
    tools = _tools(monkeypatch, node.store)

    out = node.store.mark_goal_copy_verified('g-speech', verified_by=OWNER)
    later = _sequence(tools, _prospect(tools, 'Clinic', ANA, 'g-speech'))
    node.store.check_pending_followups_daemon()

    assert out['success'] is True and out['steps_verified'] == 0
    assert later['status'] == 'held' and node.mail.sent == []
    with db_session(commit=False) as db:
        assert db.query(AgentGoal).filter_by(id='g-speech').one().config_json[
            'verified_copy'] == []


def test_the_owner_verifies_only_the_copy_he_was_shown(node, monkeypatch):
    """A sequence made after the owner read the held list is not verified by
    the click that verifies what he read."""
    _speech_box(node)
    tools = _tools(monkeypatch, node.store)
    seen = _sequence(tools, _prospect(tools, 'Clinic', ANA, 'g-speech'))
    unseen = _sequence(tools, _prospect(tools, 'Shop', 'mo@shop.example', 'g-speech'),
                       subject=OFFER[0], body=OFFER[1])
    shown = node.store.step_content_hash('checking in', '<p>a quick follow-up</p>')

    out = node.store.mark_goal_copy_verified('g-speech', verified_by=OWNER,
                                             content_hashes=[shown])

    stored = node.store._load_prospects()['sequences']
    assert out['steps_verified'] == 1 and out['sequences_released'] == 1
    assert stored[seen['id']]['status'] == 'active'
    assert stored[unseen['id']]['status'] == 'held'
    again = _sequence(tools, _prospect(tools, 'Cafe', 'jo@cafe.example', 'g-speech'),
                      subject=OFFER[0], body=OFFER[1])
    assert again['status'] == 'held'


def test_a_verified_sequence_whose_text_is_changed_afterwards_is_held_again(node, monkeypatch):
    """The checker asks the goal's verified copy at every send: an 'active'
    sequence whose step text changed since (by hand, or a writer that skipped
    this rule) is held, not sent."""
    _speech_box(node)
    tools = _tools(monkeypatch, node.store)
    _verified_goal(node, tools)
    fresh = _sequence(tools, _prospect(tools, 'Shop', 'mo@shop.example', 'g-speech'))
    data = node.store._load_prospects()
    data['sequences'][fresh['id']]['steps'][0]['body_html'] = '<p>buy my watches</p>'
    node.store._save_prospects(data)

    node.store.check_pending_followups_daemon()

    assert node.store._load_prospects()['sequences'][fresh['id']]['status'] == 'held'
    assert [m['to'] for m in node.mail.sent_from(HEVOLVE)] == [ANA]


def test_the_held_list_shows_the_copy_waiting_for_verification(node, monkeypatch):
    monkeypatch.setenv('HEVOLVE_NODE_TIER', 'central')
    _speech_box(node)
    tools = _tools(monkeypatch, node.store)
    for company, address in (('Clinic', ANA), ('Shop', 'mo@shop.example')):
        _sequence(tools, _prospect(tools, company, address, 'g-speech'))
    _sequence(tools, _prospect(tools, 'Cafe', 'jo@cafe.example', 'g-speech'),
              subject=OFFER[0], body=OFFER[1])

    rows = {r['subject']: r for r in node.store.list_held_sequence_steps('g-speech')}
    shown = _as(_person(role='central'), 'get', '/api/admin/outreach/held?goal_id=g-speech')

    assert sorted(rows) == ['a different offer', 'checking in']
    assert rows['checking in']['sequences'] == 2
    assert rows['checking in']['companies'] == ['Clinic', 'Shop']
    assert rows['checking in']['content_hash'] == node.store.step_content_hash(
        'checking in', '<p>a quick follow-up</p>')
    assert rows['a different offer']['body_html'] == OFFER[1]
    assert node.store.list_held_sequence_steps('g-lashes') == []
    body = shown.get_data(as_text=True)
    assert shown.status_code == 200 and len(shown.get_json()['data']['sequence_steps']) == 2
    assert ANA not in body and 'mo@shop.example' not in body and 'jo@cafe.example' not in body


# ── the verified copy has one writer: the owner's verification ───────

FORGED_KEYS = ('verified_copy', 'copy_verified', 'copy_verified_by', 'copy_verified_at')


def _goal_config(goal_id):
    from integrations.social.models import AgentGoal, db_session
    with db_session(commit=False) as db:
        return dict(db.query(AgentGoal).filter_by(id=goal_id).one().config_json or {})


def _owner_verifies_one_step(node, tools):
    """The owner verifies the default copy for g-speech through the writer;
    returns its content hash."""
    _sequence(tools, _prospect(tools, 'Clinic', ANA, 'g-speech'))
    assert node.store.mark_goal_copy_verified('g-speech', verified_by=OWNER)['steps_verified'] == 1
    return node.store.step_content_hash('checking in', '<p>a quick follow-up</p>')


def test_a_goal_created_through_the_api_holding_verified_copy_is_created_without_it(node, web, monkeypatch):
    """POST /api/goals stores the body's config, and goal_sender reads
    verified_copy from it: an account made a goal whose config already held
    the hashes of its own steps, and its sequences started active with no
    owner verification (review of c6343fb17).  Here the account legitimately
    connected the mailbox, so nothing else stands in the way."""
    seller = web.sign_up('seller')
    assert web.connect(seller, MCGROCE).get_json()['success'] is True
    _mailbox(node.channels, MCGROCE)
    forged = [node.store.step_content_hash('checking in', '<p>a quick follow-up</p>')]

    goal = web.goal(seller, 'My own lash outreach', MCGROCE, verified_copy=forged,
                    copy_verified=True, copy_verified_by='the-owner',
                    copy_verified_at='2026-11-17T00:00:00')
    tools = _tools(monkeypatch, node.store)
    sequence = _sequence(tools, _prospect(tools, 'Salon', 'ravi@salon.example', goal), goal=goal)
    node.store.check_pending_followups_daemon()

    config = _goal_config(goal)
    assert config['sender_email'] == MCGROCE
    assert not set(FORGED_KEYS) & set(config)
    assert sequence['status'] == 'held' and node.mail.sent == []


def test_no_update_path_writes_the_verified_copy_and_none_drops_it(node, web, monkeypatch):
    """update_goal, whichever caller: a config that carries the keys changes
    nothing about them, and one that leaves them out does not drop them.  The
    status route takes no config; one sent along is ignored."""
    from integrations.agent_engine.goal_manager import GoalManager
    from integrations.social.models import db_session
    _speech_box(node)
    tools = _tools(monkeypatch, node.store)
    verified = _owner_verifies_one_step(node, tools)
    mine = web.sign_up('mine')
    own = web.goal(mine, 'A goal of my own', HEVOLVE)          # its creator is not the owner

    with db_session() as db:
        GoalManager.update_goal(db, 'g-speech', config_json={
            'sender_email': HEVOLVE, 'verified_copy': ['forged-hash'],
            'copy_verified': True, 'copy_verified_by': 'someone'})
    after_forgery = _goal_config('g-speech')
    with db_session() as db:
        GoalManager.update_goal(db, 'g-speech', config_json={'sender_email': HEVOLVE})
    after_rewrite = _goal_config('g-speech')
    with db_session() as db:
        GoalManager.update_goal(db, own, config_json={
            'sender_email': HEVOLVE, 'verified_copy': ['forged-hash']})
    web.client.patch(f'/api/goals/{own}/status', headers=mine['auth'], json={
        'status': 'paused', 'config': {'verified_copy': ['forged-hash']}})
    with db_session() as db:        # the writer's parameter carries those keys only
        GoalManager.update_goal(db, 'g-speech', _verified_copy={
            'sender_email': 'evil@else.example', 'verified_copy': after_rewrite['verified_copy']})

    assert after_forgery['verified_copy'] == [verified]
    assert after_forgery['copy_verified_by'] == OWNER and 'copy_verified' not in after_forgery
    assert after_rewrite['verified_copy'] == [verified]
    assert 'verified_copy' not in _goal_config(own)
    assert _goal_config('g-speech')['sender_email'] == HEVOLVE


def test_a_goal_made_by_the_goal_manager_never_holds_verified_copy(node):
    from integrations.agent_engine import goal_manager
    from integrations.agent_engine.goal_manager import GoalManager
    from integrations.social.models import AgentGoal, db_session
    if 'outreach' not in goal_manager.get_registered_types():
        node.store.register_outreach_goal_type()
    with db_session() as db:
        made = GoalManager.create_goal(
            db, 'outreach', 'Made in code', config={
                'sender_email': HEVOLVE, 'verified_copy': ['forged-hash'],
                'copy_verified': True})
        gid = made['goal']['id']
    try:
        assert made['success'] is True
        assert _goal_config(gid) == {'sender_email': HEVOLVE}
    finally:
        with db_session() as db:
            db.query(AgentGoal).filter_by(id=gid).delete()


def test_only_the_gated_writer_sets_the_verified_copy(node, monkeypatch):
    """The writer is reached through the admin route: a person without the
    operator role is refused on central and nothing is written."""
    monkeypatch.setenv('HEVOLVE_NODE_TIER', 'central')
    _speech_box(node)
    tools = _tools(monkeypatch, node.store)
    _sequence(tools, _prospect(tools, 'Clinic', ANA, 'g-speech'))
    path = '/api/admin/outreach/goals/g-speech/copy-verified'

    refused = _as(_person(), 'post', path)
    assert refused.status_code == 403
    assert 'verified_copy' not in _goal_config('g-speech')

    allowed = _as(_person(role='central'), 'post', path)
    assert allowed.status_code == 200 and allowed.get_json()['data']['steps_verified'] == 1
    assert len(_goal_config('g-speech')['verified_copy']) == 1


# ── a prospect of no goal: held on a node that is not flat ───────────

@pytest.mark.parametrize('tier', ['central', 'regional'])
def test_an_email_for_no_goal_is_held_on_a_node_that_is_not_flat(node, monkeypatch, tier):
    monkeypatch.setenv('HEVOLVE_NODE_TIER', tier)
    _legacy_mailbox(node, monkeypatch)
    tools = _tools(monkeypatch, node.store)
    pid = _prospect(tools, 'Shop', 'mo@shop.example')                     # no goal

    out = json.loads(tools['send_outreach_email'](pid, 'hello', '<p>hi there</p>'))

    assert out['held'] is True and out['sent'] is False
    assert node.mail.sent == [] and node.mailer == []
    (draft,) = node.store.list_held_drafts()
    assert draft['goal_id'] is None and 'mo@shop.example' not in json.dumps(draft)
    approved = node.store.approve_held_draft(draft['draft_id'], approved_by=OWNER)
    assert approved['sent'] is True and approved['send_result']['via'] == 'email_channel'
    assert [m['to'] for m in node.mail.sent_from('owner@desktop.example')] == ['mo@shop.example']


def test_an_email_for_no_goal_on_a_flat_node_is_sent_as_before(node, monkeypatch):
    _legacy_mailbox(node, monkeypatch)
    tools = _tools(monkeypatch, node.store)
    pid = _prospect(tools, 'Shop', 'mo@shop.example')

    out = json.loads(tools['send_outreach_email'](pid, 'hello', '<p>hi there</p>'))

    assert out['success'] is True and 'held' not in out
    assert [m['to'] for m in node.mail.sent_from('owner@desktop.example')] == ['mo@shop.example']
    assert node.store.list_held_drafts() == []


@pytest.mark.parametrize('tier', ['central', 'regional'])
def test_a_sequence_of_no_goal_is_held_on_a_node_that_is_not_flat(node, monkeypatch, tier):
    """A prospect of no goal has no goal to verify its copy for: its sequence
    is held (and says why), never sent, on a node the owner verifies."""
    monkeypatch.setenv('HEVOLVE_NODE_TIER', tier)
    _legacy_mailbox(node, monkeypatch)
    tools = _tools(monkeypatch, node.store)
    pid = _prospect(tools, 'Shop', 'mo@shop.example')
    steps = json.dumps([{'delay_days': 0, 'subject': 'bump', 'body_html': '<p>hi</p>'}])

    made = json.loads(tools['create_followup_sequence'](pid, 'bump', steps))
    node.store.check_pending_followups_daemon()

    assert made['sequence']['status'] == 'held' and 'no goal' in made['note']
    assert node.mail.sent == []


def test_a_sequence_of_no_goal_goes_out_on_a_flat_node(node, monkeypatch):
    _legacy_mailbox(node, monkeypatch)
    tools = _tools(monkeypatch, node.store)
    pid = _prospect(tools, 'Shop', 'mo@shop.example')
    steps = json.dumps([{'delay_days': 0, 'subject': 'bump', 'body_html': '<p>hi</p>'}])

    made = json.loads(tools['create_followup_sequence'](pid, 'bump', steps))
    node.store.check_pending_followups_daemon()

    assert made['sequence']['status'] == 'active'
    assert [m['to'] for m in node.mail.sent_from('owner@desktop.example')] == ['mo@shop.example']


# ── the owner's routes ───────────────────────────────────────────────

def _admin_client():
    from flask import Flask
    from integrations.channels.admin.api import admin_bp
    app = Flask(__name__)
    app.register_blueprint(admin_bp)
    return app.test_client()


def _person(**attrs):
    u = MagicMock()
    u.id = attrs.pop('id', 7)
    u.role = attrs.pop('role', 'flat')
    u.is_admin = attrs.pop('is_admin', False)
    u.is_moderator = attrs.pop('is_moderator', False)
    return u


def _as(user, method, path, **kw):
    with patch('integrations.social.auth._get_user_from_token',
               return_value=(user, MagicMock())):
        return getattr(_admin_client(), method)(
            path, headers={'Authorization': 'Bearer t'}, **kw)


def test_the_approve_route_refuses_a_non_admin_and_sends_for_the_owner(node, monkeypatch):
    monkeypatch.setenv('HEVOLVE_NODE_TIER', 'central')
    _speech_box(node)
    tools = _tools(monkeypatch, node.store)
    draft_id = _write(tools, _prospect(tools, 'Clinic', ANA, 'g-speech'))['draft_id']
    path = f'/api/admin/outreach/held/{draft_id}/approve'

    refused = _as(_person(), 'post', path)
    listing = _as(_person(), 'get', '/api/admin/outreach/held?goal_id=g-speech')

    assert refused.status_code == 403 and listing.status_code == 403
    assert node.mail.sent == [] and len(_held(node.store)) == 1

    owner = _person(role='central')
    shown = _as(owner, 'get', '/api/admin/outreach/held?goal_id=g-speech')
    (row,) = shown.get_json()['data']['drafts']
    assert (row['subject'], row['body_html']) == ('a question', BODY)
    assert ANA not in shown.get_data(as_text=True)
    approved = _as(owner, 'post', path)
    assert approved.status_code == 200, approved.get_json()
    assert approved.get_json()['data']['sent'] is True
    assert [m['html'] for m in node.mail.sent_from(HEVOLVE)] == [BODY]


def test_the_owner_routes_reject_a_draft_and_verify_a_goal(node, monkeypatch):
    monkeypatch.setenv('HEVOLVE_NODE_TIER', 'central')
    _speech_box(node)
    tools = _tools(monkeypatch, node.store)
    pid = _prospect(tools, 'Clinic', ANA, 'g-speech')
    draft_id = _write(tools, pid)['draft_id']
    seq = _step(node.store, tools, pid)
    owner = _person(is_admin=True)

    assert _as(_person(), 'post', '/api/admin/outreach/goals/g-speech/copy-verified'
               ).status_code == 403
    rejected = _as(owner, 'post', f'/api/admin/outreach/held/{draft_id}/reject',
                   json={'reason': 'too long'})
    verified = _as(owner, 'post', '/api/admin/outreach/goals/g-speech/copy-verified')

    assert rejected.status_code == 200 and verified.status_code == 200
    (draft,) = node.store.list_held_drafts(goal_id='g-speech', status='rejected')
    assert draft['reason'] == 'too long'
    assert node.store._load_prospects()['sequences'][seq['id']]['status'] == 'active'


# ── no recipient address in a log line ───────────────────────────────

def test_no_recipient_address_is_logged(node, monkeypatch, caplog):
    caplog.set_level(logging.DEBUG)
    _speech_box(node)
    tools = _tools(monkeypatch, node.store)
    pid = _prospect(tools, 'Clinic', ANA, 'g-speech')
    draft_id = _write(tools, pid)['draft_id']
    node.store.list_held_drafts(goal_id='g-speech')
    node.store.approve_held_draft(draft_id, approved_by=OWNER)
    other = _write(tools, _prospect(tools, 'Shop', 'mo@shop.example', 'g-speech'))
    node.store.reject_held_draft(other['draft_id'], reason='no', rejected_by=OWNER)

    assert ANA not in caplog.text and 'mo@shop.example' not in caplog.text
