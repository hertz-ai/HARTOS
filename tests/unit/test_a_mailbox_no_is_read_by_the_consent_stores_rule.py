"""The send gate reads a mailbox "no" by the consent store's own rule (audit H14).

outreach_crm_tools._mailbox_permission_withdrawn decided whether a "no" to
sending from a mailbox still stands with a rule of its own (revoked, and not
reopened since), beside the consent store's (consent_service
.standing_revocation, which reopen and declined read).  Now it asks the
store's rule.  Every answer is the one the gate's own rule gave: the cases
below were written against that rule and pass on it.

Real ConsentService rows in the social DB; who connected the mailbox is the
stub of test_a_connected_mailbox_starts_its_outreach.py.
"""
import os
import sys
import uuid
from datetime import datetime, timedelta

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..'))
sys.path.insert(0, os.path.dirname(__file__))

import test_a_connected_mailbox_starts_its_outreach as rig  # noqa: E402
from integrations.agent_engine import outreach_crm_tools as oct_  # noqa: E402
from integrations.social import consent_service as cs_mod  # noqa: E402
from integrations.social.consent_service import ConsentService  # noqa: E402

outreach_type, connectors, user = rig.outreach_type, rig.connectors, rig.user
MAILBOX = rig.MAILBOX
NOW = datetime(2026, 11, 21, 12, 0, 0)


def _row(user_id, *, granted=False, revoked=None, reopened=None, agent_id=None,
         scope=MAILBOX, consent_type='mailbox_outreach'):
    """One consent row as the store keeps it; ``revoked`` and ``reopened``
    are minutes after NOW."""
    from integrations.social.models import UserConsent, db_session
    at = lambda minutes: None if minutes is None else NOW + timedelta(minutes=minutes)
    with db_session() as db:
        db.add(UserConsent(
            id=str(uuid.uuid4()), user_id=user_id, consent_type=consent_type,
            scope=scope, agent_id=agent_id, granted=granted,
            granted_at=NOW if granted else None, revoked_at=at(revoked),
            reopened_at=at(reopened)))


def test_nobody_asked_is_not_withdrawn(user):
    assert oct_._mailbox_permission_withdrawn(user, MAILBOX) is False


def test_an_ask_still_waiting_is_not_withdrawn(user):
    _row(user)
    assert oct_._mailbox_permission_withdrawn(user, MAILBOX) is False


def test_a_no_that_stands_is_withdrawn(user):
    _row(user, revoked=1)
    assert oct_._mailbox_permission_withdrawn(user, MAILBOX) is True


def test_a_no_to_one_agent_stands_for_the_mailbox(user):
    """The gate reads a no for ANY agent: a no given on one agent's card
    stops the mailbox, as before."""
    _row(user, revoked=1, agent_id='agent-7')
    assert oct_._mailbox_permission_withdrawn(user, MAILBOX) is True


def test_a_no_taken_back_is_not_withdrawn(user):
    _row(user, revoked=1, reopened=2)
    assert oct_._mailbox_permission_withdrawn(user, MAILBOX) is False


def test_a_no_given_again_after_the_reopen_is_withdrawn(user):
    _row(user, revoked=3, reopened=2)
    assert oct_._mailbox_permission_withdrawn(user, MAILBOX) is True


def test_one_standing_no_among_reopened_ones_is_withdrawn(user):
    _row(user, revoked=1, reopened=2)
    _row(user, revoked=5, agent_id='agent-7')
    assert oct_._mailbox_permission_withdrawn(user, MAILBOX) is True


def test_a_grant_in_force_wins_over_an_older_no(user):
    _row(user, revoked=1)
    _row(user, granted=True)
    assert oct_._mailbox_permission_withdrawn(user, MAILBOX) is False


def test_a_no_for_another_mailbox_or_another_consent_is_not_this_one(user):
    _row(user, revoked=1, scope='other@example.org')
    _row(user, revoked=1, consent_type='camera_capture', scope=MAILBOX)
    assert oct_._mailbox_permission_withdrawn(user, MAILBOX) is False


def test_a_connectors_no_stops_the_mailbox_for_another_accounts_goal(user, connectors):
    creator = f'creator-{uuid.uuid4().hex[:8]}'
    _row(user, revoked=1)
    assert oct_._mailbox_permission_withdrawn(creator, MAILBOX) is True


def test_the_gate_asks_the_consent_stores_rule(monkeypatch, user):
    asked = []
    real = cs_mod.standing_revocation

    def recording():
        asked.append(1)
        return real()
    monkeypatch.setattr(cs_mod, 'standing_revocation', recording)
    _row(user, revoked=1)

    assert oct_._mailbox_permission_withdrawn(user, MAILBOX) is True
    assert asked, 'the send gate decided a standing no by a rule of its own'
