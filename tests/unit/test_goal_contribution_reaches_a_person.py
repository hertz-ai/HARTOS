"""A goal-contribution notification is written for a PERSON, never an agent row.

Measured 2026-09-25 in ~/Documents/Nunba/data/hevolve_database.db (read-only):
3003 'goal_contribution' notifications addressed to
c23d388c-... = username hevolve_system_agent, user_type 'agent' -- the account
integrations/agent_engine/__init__.py bootstraps for daemon goal execution --
and 69 more to an ownerless agent row.  None was ever read: no person signs in
as an agent, so every row sat in an inbox nobody opens and every push went to
a WAMP/SSE topic nobody subscribes to.

_notify_goal_contribution skipped only MACHINE_GOAL_AUTHORS, which are daemon
LABELS ('system_daemon', ...), not the system agent's user id.  The recipient
is now decided from the users row: a person is notified, an agent is reported
to the person who owns it, and an agent or system account with no human owner
is not notified at all.

Real SQLite (in-memory) and the real NotificationService; only the realtime
push is patched.
"""
import os
import sys
from unittest.mock import patch

import pytest

_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
for _p in (_ROOT, os.path.join(_ROOT, 'agent-ledger-opensource')):
    if _p not in sys.path:
        sys.path.insert(0, _p)

os.environ.setdefault('HEVOLVE_DB_PATH', ':memory:')

from tests.unit.test_coordinator_dedup import _coordinator, _ledger  # noqa: E402


@pytest.fixture
def Session():
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from integrations.social.models import Base
    eng = create_engine('sqlite://', echo=False)
    Base.metadata.create_all(eng)
    return sessionmaker(bind=eng)


def _add_user(Session, uid, user_type, owner_id=None):
    from integrations.social.models import User
    s = Session()
    s.add(User(id=uid, username=f'u_{uid}', display_name=uid,
               user_type=user_type, owner_id=owner_id))
    s.commit()
    s.close()


def _notify_for_requester(Session, requester):
    led = _ledger()
    coord = _coordinator(led)
    coord.submit_goal('objective', [{'task_id': 'g1_task_0',
                                      'description': 'd'}],
                      {'user_id': requester}, goal_id='g1')
    with patch('integrations.social.models.get_db', side_effect=Session), \
         patch('integrations.social.realtime.on_notification'):
        coord._notify_goal_contribution('g1_task_0', agent_id='node-abc',
                                        task_description='d')
    from integrations.social.models import Notification
    s = Session()
    rows = [(n.user_id, n.type) for n in s.query(Notification).all()]
    s.close()
    return rows


def test_the_system_agent_account_gets_no_notification(Session):
    _add_user(Session, 'sys-agent', 'agent')
    assert _notify_for_requester(Session, 'sys-agent') == []


def test_a_system_account_gets_no_notification(Session):
    _add_user(Session, 'sys-user', 'system')
    assert _notify_for_requester(Session, 'sys-user') == []


def test_an_owned_agent_reports_to_its_human_owner(Session):
    _add_user(Session, 'human-1', 'human')
    _add_user(Session, 'agent-1', 'agent', owner_id='human-1')
    assert _notify_for_requester(Session, 'agent-1') == [
        ('human-1', 'goal_contribution')]


def test_an_agent_owned_by_the_system_agent_notifies_nobody(Session):
    # Measured live: analysis.local.sage (agent) is owned by
    # hevolve_system_agent, itself an agent.
    _add_user(Session, 'sys-agent', 'agent')
    _add_user(Session, 'agent-2', 'agent', owner_id='sys-agent')
    assert _notify_for_requester(Session, 'agent-2') == []


def test_a_human_requester_is_notified(Session):
    _add_user(Session, 'human-2', 'human')
    assert _notify_for_requester(Session, 'human-2') == [
        ('human-2', 'goal_contribution')]


def test_a_guest_is_a_person_and_is_notified(Session):
    _add_user(Session, 'guest-1', 'guest')
    assert _notify_for_requester(Session, 'guest-1') == [
        ('guest-1', 'goal_contribution')]


def test_a_requester_with_no_local_row_is_notified_as_before(Session):
    # A goal submitted on another node names a requester this node has no
    # users row for; that case is unchanged by this fix.
    assert _notify_for_requester(Session, 'remote-9') == [
        ('remote-9', 'goal_contribution')]


def test_an_agent_whose_owner_is_not_on_this_node_reports_to_that_owner(Session):
    _add_user(Session, 'agent-3', 'agent', owner_id='remote-owner')
    assert _notify_for_requester(Session, 'agent-3') == [
        ('remote-owner', 'goal_contribution')]
