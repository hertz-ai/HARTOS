"""A connected mailbox starts its outreach at once, for the business it speaks for.

Owner, 19 Nov 2026: "When user provides their email creds shd setup the
marketing loop via marketing agent oif the email agent will have to work with
marketing agent or vise versa", and, asked whether to wait for a yes: "as
soon as the mailbox connect. Each agent should understand to which bussines
sit will be relevant and talk to the appropriate bussiness".  Design D8,
reviewed by hartos-43 and hartos-ee
(D:/claude_scratch/66e67cad/designs/D8_email_creds_start_marketing_loop.md):

  * once a mailbox's login is proven (register_channel's start watcher,
    never boot restore) the connect is the person's yes: it is recorded as
    the mailbox_outreach consent (privacy page, revocable);
  * the goals that already name it in config sender_email send from it now,
    and the owner is told which;
  * with none, ONE outreach goal starts on the mailbox; its agent works out
    which of the owner's businesses the mailbox speaks for and writes only
    to businesses that offer is relevant to;
  * goals that name no mailbox are never bound to it (one business never
    mails from another business's address);
  * a revoke pauses every goal sending from it and the goal-mailbox send
    gate (follow-ups and held drafts go through it) refuses while the "no"
    stands; a later connect does not overrule it;
  * no other path auto-grants mailbox_outreach.

The DB is the real social DB (a temp file under pytest).  Two tests run the
real connect tool on hartos-ee's rig (tests/unit/test_each_goal_sends_from_
its_own_mailbox.py: real register_channel, channel registry and EmailAdapter;
mail servers faked).

    python -m pytest tests/unit/test_a_connected_mailbox_starts_its_outreach.py -q --noconftest
"""
import os
import sys
import threading
import time
import uuid
from datetime import datetime, timedelta

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..'))
sys.path.insert(0, os.path.dirname(__file__))

import test_each_goal_sends_from_its_own_mailbox as rig  # noqa: E402
from integrations.agent_engine import outreach_crm_tools as oct_  # noqa: E402
from integrations.channels import agent_tools  # noqa: E402
from integrations.social import consent_service as cs_mod  # noqa: E402
from integrations.social.consent_service import ConsentService  # noqa: E402

# hartos-ee's rig, used as is.
node, channels, mail, mailer = rig.node, rig.channels, rig.mail, rig.mailer

MAILBOX = 'shop@example.org'


@pytest.fixture(autouse=True)
def outreach_type():
    """The outreach goal type, as boot registers it."""
    from integrations.social.models import Base, get_engine
    Base.metadata.create_all(get_engine())
    oct_.register_outreach_goal_type()


@pytest.fixture
def connectors(monkeypatch):
    """The accounts that connected MAILBOX (FlaskChannelIntegration.
    accounts_connecting, the one lookup _may_use_mailbox and the send gate
    read).  A unit test's connect is the proven login: its user is one of
    them; the e2e tests below use real binding rows instead.  Only MAILBOX is
    connected: any other address (and '*') has no binding row."""
    from integrations.channels.flask_integration import FlaskChannelIntegration
    accounts = set()
    monkeypatch.setattr(
        FlaskChannelIntegration, 'accounts_connecting',
        classmethod(lambda cls, db, channel_type, account:
                    set(accounts) if account == MAILBOX else set()))
    return accounts


@pytest.fixture
def user(connectors):
    uid = f'mailbox-user-{uuid.uuid4().hex[:8]}'
    connectors.add(uid)
    return uid


def _goal(user_id, title, goal_type='outreach', cfg=None, status='active'):
    from integrations.social.models import AgentGoal, db_session
    gid = f'g-{uuid.uuid4().hex[:10]}'
    with db_session() as db:
        db.add(AgentGoal(id=gid, goal_type=goal_type, title=title,
                         config_json=dict(cfg or {}), created_by=user_id,
                         status=status,
                         created_at=datetime.utcnow() - timedelta(minutes=5)))
    return gid


def _goal_row(gid):
    from integrations.social.models import AgentGoal, db_session
    with db_session(commit=False) as db:
        g = db.query(AgentGoal).filter_by(id=gid).first()
        return (g.status, dict(g.config_json or {})) if g else (None, None)


def _users_goals(user_id):
    from integrations.social.models import AgentGoal, db_session
    with db_session(commit=False) as db:
        return [(g.id, g.goal_type, g.status, dict(g.config_json or {}),
                 g.description or '')
                for g in db.query(AgentGoal).filter_by(created_by=user_id).all()]


def _mailbox_goals(user_id, address=MAILBOX):
    return [g for g in _users_goals(user_id)
            if (g[3].get('sender_email') or '').lower() == address]


def _allowed(user_id, address=MAILBOX):
    from integrations.social.models import db_session
    with db_session(commit=False) as db:
        return ConsentService.check_consent(db, user_id, 'mailbox_outreach',
                                            scope=address)


def _revoke(user_id, address=MAILBOX):
    from integrations.social.models import db_session
    with db_session() as db:
        ConsentService.revoke_consent(db, user_id, 'mailbox_outreach', scope=address)


# ---- the trigger: once, only when the login is proven ---------------------

@pytest.fixture
def watcher(monkeypatch):
    """The start watcher with the credential vault out of the way."""
    monkeypatch.setattr(agent_tools, '_owner_value_fingerprint', lambda *a, **k: None)


@pytest.mark.parametrize('connected', [True, False])
def test_the_watcher_takes_the_next_step_only_when_the_mailbox_connected(
        monkeypatch, watcher, connected):
    monkeypatch.setattr(agent_tools, '_report_when_live',
                        lambda *a, **k: connected)
    ran, done = [], threading.Event()
    agent_tools._watch_start('email', {}, 'u1', account=MAILBOX,
                             on_connected=lambda: ran.append(1) or done.set())
    done.wait(3)
    time.sleep(0.2)
    assert ran == ([1] if connected else []), ran


def test_a_failing_next_step_leaves_the_start_connected(monkeypatch, watcher):
    monkeypatch.setattr(agent_tools, '_report_when_live', lambda *a, **k: True)
    tried = threading.Event()

    def _boom():
        tried.set()
        raise RuntimeError('db locked')

    start = agent_tools._watch_start('email', {}, 'u1', account=MAILBOX,
                                     on_connected=_boom)
    assert tried.wait(3)
    time.sleep(0.2)
    assert agent_tools._STARTS['email']['start'] == start
    assert agent_tools._STARTS['email']['state'] == agent_tools.START_CONNECTED


# ---- the connect starts it -------------------------------------------------

def test_a_mailbox_no_goal_names_starts_one_outreach_goal_at_once(monkeypatch, user):
    said = []
    monkeypatch.setattr(cs_mod, '_emit', lambda topic, data, **k: said.append(topic))
    out = oct_.mailbox_connected(user, MAILBOX)
    assert out['started'] and not out['joined'], out
    goals = _mailbox_goals(user)
    assert len(goals) == 1 and goals[0][1] == 'outreach', goals
    assert "which of the owner's businesses" in goals[0][4], goals[0][4]
    assert 'consent.request' not in said, said
    assert _allowed(user)


def test_goals_that_already_name_the_mailbox_send_from_it_and_no_goal_is_added(user):
    _goal(user, 'Lash studios', cfg={'sender_email': MAILBOX.upper()})
    out = oct_.mailbox_connected(user, MAILBOX)
    assert out['joined'] == ['Lash studios'] and not out['started'], out
    assert len(_users_goals(user)) == 1
    assert _allowed(user)


def test_a_second_connect_starts_no_second_goal(user):
    oct_.mailbox_connected(user, MAILBOX)
    out = oct_.mailbox_connected(user, MAILBOX)
    assert out['joined'] and not out['started'], out
    assert len(_mailbox_goals(user)) == 1


def test_goals_that_name_no_mailbox_are_never_bound_to_it(user):
    unbound = _goal(user, 'Find salon buyers')
    stranger = _goal(f'other-{user}', "Someone else's goal")
    oct_.mailbox_connected(user, MAILBOX)
    assert 'sender_email' not in _goal_row(unbound)[1]
    assert 'sender_email' not in _goal_row(stranger)[1]
    assert len(_mailbox_goals(user)) == 1


def test_a_mailbox_whose_sending_was_stopped_stays_stopped_on_a_new_connect(user):
    oct_.mailbox_connected(user, MAILBOX)
    gid = _mailbox_goals(user)[0][0]
    _revoke(user)
    out = oct_.mailbox_connected(user, MAILBOX)
    assert out['withdrawn'] and not out['started'], out
    assert not _allowed(user)
    assert _goal_row(gid)[0] == 'paused'
    assert len(_mailbox_goals(user)) == 1


def test_mailbox_outreach_is_never_auto_granted(user):
    from integrations.social.models import db_session
    with db_session() as db:
        assert ConsentService.auto_grant_with_notice(
            db, user, 'mailbox_outreach', scope=MAILBOX) is False
    assert not _allowed(user)


def test_another_consents_yes_starts_nothing(user):
    from integrations.social.models import db_session
    with db_session() as db:
        ConsentService.grant_consent(db, user, 'data_access', scope=MAILBOX)
    assert _users_goals(user) == []


# ---- the revoke ----------------------------------------------------------

class TestRvG2Forward:
    """rv_g2's REJECT of ae350c0f1: M1, S1, S2."""

    def test_a_revoke_stops_every_goal_sending_from_the_mailbox(self, monkeypatch, user):
        """M1: the node's owner (or a system lane) may run a goal on a mailbox
        another account connected; that account's "no" stops it too, its
        follow-ups included, and the revoker is told which goals paused."""
        told = []
        from integrations.social import services
        monkeypatch.setattr(services.NotificationService, 'create',
                            staticmethod(lambda db, uid, kind, **kw: told.append((uid, kind, kw))))
        owner = f'node-owner-{uuid.uuid4().hex[:6]}'
        theirs = _goal(owner, 'Owner lane: lash studios', cfg={'sender_email': MAILBOX})
        oct_.mailbox_connected(user, MAILBOX)
        assert not oct_._mailbox_permission_withdrawn(owner, MAILBOX)
        _revoke(user)
        status, cfg = _goal_row(theirs)
        assert status == 'paused' and cfg.get('pause_cause') == 'person', (status, cfg)
        assert oct_._mailbox_permission_withdrawn(owner, MAILBOX), (
            "a goal made by another account still sends after the connector said no")
        assert told and told[-1][0] == user and told[-1][1] == 'mailbox_outreach_revoked'
        assert 'Owner lane: lash studios' in told[-1][2]['message'], told

    def test_another_accounts_no_is_lifted_when_they_allow_it_again(self, user):
        owner = f'node-owner-{uuid.uuid4().hex[:6]}'
        _goal(owner, 'Owner lane', cfg={'sender_email': MAILBOX})
        oct_.mailbox_connected(user, MAILBOX)
        _revoke(user)
        from integrations.social.models import db_session
        with db_session() as db:
            ConsentService.grant_consent(db, user, 'mailbox_outreach', scope=MAILBOX)
        assert not oct_._mailbox_permission_withdrawn(owner, MAILBOX)

    def test_a_reconnect_after_the_goal_ended_starts_a_new_one(self, user):
        """S1 + the owner's "as soon as the mailbox connect"."""
        oct_.mailbox_connected(user, MAILBOX)
        from integrations.social.models import AgentGoal, db_session
        with db_session() as db:
            for g in db.query(AgentGoal).filter_by(created_by=user).all():
                g.status = 'completed'
        out = oct_.mailbox_connected(user, MAILBOX)
        assert out['started'] and not out['joined'], out
        assert len([g for g in _mailbox_goals(user) if g[2] == 'active']) == 1

    def test_started_is_never_said_when_no_goal_was_made(self, monkeypatch, user):
        """S1: the toast reads 'started' only from a goal that exists."""
        from integrations.agent_engine import goal_manager
        monkeypatch.setattr(goal_manager.GoalManager, 'create_goal',
                            staticmethod(lambda *a, **k: {'success': False, 'error': 'db down'}))
        out = oct_.mailbox_connected(user, MAILBOX)
        assert out['started'] is False, out
        assert _mailbox_goals(user) == []

    def test_a_grant_for_every_mailbox_starts_nothing(self, user):
        """S2: a privacy-page yes with the default scope '*'."""
        from integrations.social.models import db_session
        with db_session() as db:
            ConsentService.grant_consent(db, user, 'mailbox_outreach', scope='*')
        assert [g for g in _users_goals(user) if g[1] == 'outreach'] == []

    def test_the_node_owners_grant_for_every_mailbox_starts_nothing(self, monkeypatch, user):
        """S2 for the owner: _may_use_mailbox says yes to the node's owner for
        any address, so '*' must be refused as no connected mailbox."""
        from integrations.social.thought_experiment_service import (
            ThoughtExperimentService)
        monkeypatch.setattr(ThoughtExperimentService, 'node_held_ids',
                            staticmethod(lambda db, ids: list(ids)))
        owner = f'node-owner-{uuid.uuid4().hex[:6]}'
        from integrations.social.models import db_session
        with db_session() as db:
            ConsentService.grant_consent(db, owner, 'mailbox_outreach', scope='*')
            ConsentService.grant_consent(db, owner, 'mailbox_outreach',
                                         scope='never-connected@example.org')
        assert [g for g in _users_goals(owner) if g[1] == 'outreach'] == []
        # and the owner's yes for the connected mailbox does start one
        with db_session() as db:
            ConsentService.grant_consent(db, owner, 'mailbox_outreach', scope=MAILBOX)
        assert len(_mailbox_goals(owner)) == 1

    def test_a_grant_for_a_mailbox_they_did_not_connect_starts_nothing(self, connectors, user):
        """S2: only an address the granting account may use."""
        connectors.discard(user)
        from integrations.social.models import db_session
        with db_session() as db:
            ConsentService.grant_consent(db, user, 'mailbox_outreach', scope=MAILBOX)
        assert _mailbox_goals(user) == []


def test_a_revoke_pauses_the_goals_sending_from_it(user):
    named = _goal(user, 'Lash studios', cfg={'sender_email': MAILBOX})
    oct_.mailbox_connected(user, MAILBOX)
    assert not oct_._mailbox_permission_withdrawn(user, MAILBOX)
    _revoke(user)
    status, cfg = _goal_row(named)
    assert status == 'paused' and cfg.get('pause_cause') == 'person', (status, cfg)
    assert oct_._mailbox_permission_withdrawn(user, MAILBOX)


def test_a_no_taken_back_is_undecided_not_withdrawn(user):
    from integrations.social.models import db_session
    with db_session() as db:
        ConsentService.request_consent(db, user, 'mailbox_outreach', scope=MAILBOX)
    with db_session() as db:
        ConsentService.decline(db, user, 'mailbox_outreach', scope=MAILBOX)
    assert oct_._mailbox_permission_withdrawn(user, MAILBOX)
    with db_session() as db:
        ConsentService.reopen(db, user, 'mailbox_outreach', scope=MAILBOX)
    assert not oct_._mailbox_permission_withdrawn(user, MAILBOX)


def test_a_mailbox_nobody_was_asked_about_is_not_withdrawn(user):
    assert not oct_._mailbox_permission_withdrawn(user, MAILBOX)


def test_a_permission_that_cannot_be_read_sends_nothing(monkeypatch, user):
    def _broken(*a, **k):
        raise RuntimeError('consent table locked')

    monkeypatch.setattr(ConsentService, 'check_consent', staticmethod(_broken))
    assert oct_._mailbox_permission_withdrawn(user, MAILBOX)


def test_the_privacy_pages_revoke_pauses_the_goals_too(user):
    """The revoke route keeps its own row model and announces through
    announce_revocation; the goals are paused after its commit."""
    from flask import Flask, g
    from integrations.social import consent_api
    from integrations.social.models import db_session
    oct_.mailbox_connected(user, MAILBOX)
    gid = _mailbox_goals(user)[0][0]
    app = Flask('revoke_route_test')
    original = consent_api._user_id
    consent_api._user_id = lambda: user
    try:
        with db_session() as db:
            with app.test_request_context(json={'consent_type': 'mailbox_outreach',
                                                'scope': MAILBOX}):
                g.db = db
                consent_api.revoke_consent.__wrapped__()
    finally:
        consent_api._user_id = original
    assert _goal_row(gid)[0] == 'paused'


# ---- through the real connect (hartos-ee's rig) ---------------------------

def _wait(cond, timeout=10.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if cond():
            return True
        time.sleep(0.05)
    return False


def test_connecting_a_mailbox_goals_name_tells_the_owner_which_goals(node, monkeypatch):
    told = []
    monkeypatch.setattr(agent_tools, '_emit_to_user', lambda owner, p: told.append((owner, p)))
    rig._connect(rig.HEVOLVE)
    rig._mailbox(node.channels, rig.HEVOLVE)
    assert _wait(lambda: told), 'the owner was never told'
    owner, card = told[-1]
    assert owner == rig.OWNER and 'Speech therapy centres' in card['message'], told
    assert len(_mailbox_goals(rig.OWNER, rig.HEVOLVE)) == 1


def test_connect_starts_outreach_sends_then_a_revoke_stops_every_send(node, monkeypatch):
    told = []
    monkeypatch.setattr(agent_tools, '_emit_to_user', lambda owner, p: told.append(p))
    desktop = 'owner@desktop.example'
    rig._connect(desktop)
    rig._mailbox(node.channels, desktop)
    assert _wait(lambda: _mailbox_goals(rig.OWNER, desktop)), 'no outreach started'
    assert _wait(lambda: told) and 'outreach agent' in told[-1]['message'], told
    gid = _mailbox_goals(rig.OWNER, desktop)[0][0]
    # Goals that name no mailbox or another one are left as they were.
    assert 'sender_email' not in _goal_row('g-plain')[1]
    assert _goal_row('g-speech')[1].get('sender_email') == rig.HEVOLVE
    adapter, _loop, refused = oct_._goal_mailbox(oct_.goal_sender(gid))
    assert adapter is not None and refused is None, refused
    _revoke(rig.OWNER, desktop)
    assert _goal_row(gid)[0] == 'paused'
    adapter, _loop, refused = oct_._goal_mailbox(oct_.goal_sender(gid))
    assert adapter is None and 'took back' in str(refused), refused
    # The goal never existed in the fixture's list: tidy it here.
    from integrations.social.models import AgentGoal, db_session
    with db_session() as db:
        db.query(AgentGoal).filter_by(id=gid).delete()
