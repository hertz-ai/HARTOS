"""Every goal email from central is verified before it leaves.

Owner, 16 Nov 2026: "You verify all the outgoing emails on my behalf from
central."  Coordinator, 17 Nov (the shape):
  * a goal's templates: its follow-up sequences are created 'held' while
    the goal's copy is not verified (config copy_verified, written through
    GoalManager.update_goal); the follow-up checker sends only 'active'
    sequences; marking the goal verified releases its held sequences;
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
    HEVOLVE, MCGROCE, OWNER, _connect, _in_turn, _journey_tools, _mailbox,
    _prospect, _tools, channels, mail, mailer, node)

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
            'copy_verified'] is True
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
