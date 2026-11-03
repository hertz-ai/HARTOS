"""core.event_attribution.person_for: the one resolver for "who is asked".

An ask addressed to an agent row, a system row or the daemon's identity
reaches nobody: no person reads those inboxes.  Measured: on the owner's
desktop 2026-11-02, 38 of 38 active goals name no owner and a goal's REUSE
session runs as an agent row (task #175); on central 2026-10-31 10:29:25Z an
ask_person push to 'system_daemon' "did not go out" (hartos-95, task #231).

The rule: a human is their own person; an agent or system row answers to its
owner_id, else to the node's owner (HEVOLVE_OWNER_USER_ID); no id, or a
system identity (core.constants.SYSTEM_IDENTITIES), is the node's owner; any
other id with no users row here comes back as it is; a failed read answers
the node's owner and never raises.

Real in-memory social DB (the harness of test_central_asks_reach_the_person).

    python -m pytest tests/unit/test_the_person_who_answers.py -q
"""
from __future__ import annotations

import os
import sys
import uuid
from types import SimpleNamespace

os.environ['HEVOLVE_DB_PATH'] = ':memory:'

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from integrations.social.models import User, db_session  # noqa: E402
from tests.unit.test_central_asks_reach_the_person import (  # noqa: E402,F401
    _person, pushes)

_NODE_OWNER = '10202'


def _machine(user_type, owner_id=None):
    """An agent or system user row, the kinds a background turn runs as."""
    uid = str(uuid.uuid4())
    with db_session() as db:
        db.add(User(id=uid, username=f'{user_type}_{uid[:8]}', display_name=f'a {user_type}',
                    user_type=user_type, owner_id=owner_id, api_token=f'tok-{uid}'))
    return uid


class _OneRow:
    """A caller's own session: person_for reads the row through it."""

    def __init__(self, row):
        self.row, self.asked = row, None

    def query(self, _model):
        return self

    def filter_by(self, **kw):
        self.asked = kw
        return self

    def first(self):
        return self.row


class TestPersonFor:

    def test_a_human_is_their_own_person(self, pushes, monkeypatch):
        from core.event_attribution import person_for
        monkeypatch.setenv('HEVOLVE_OWNER_USER_ID', _NODE_OWNER)
        _person('pf-human')
        assert person_for('pf-human') == 'pf-human'

    def test_an_agent_answers_to_its_owner_else_the_node_owner(self, pushes,
                                                               monkeypatch):
        from core.event_attribution import person_for
        monkeypatch.setenv('HEVOLVE_OWNER_USER_ID', _NODE_OWNER)
        _person('pf-parent')
        assert person_for(_machine('agent', owner_id='pf-parent')) == 'pf-parent'
        assert person_for(_machine('agent')) == _NODE_OWNER

    def test_a_system_row_answers_to_its_owner_else_the_node_owner(self, pushes,
                                                                   monkeypatch):
        from core.event_attribution import person_for
        monkeypatch.setenv('HEVOLVE_OWNER_USER_ID', _NODE_OWNER)
        assert person_for(_machine('system', owner_id='pf-sys-parent')) == 'pf-sys-parent'
        assert person_for(_machine('system')) == _NODE_OWNER

    def test_no_owner_at_all_is_the_node_owner(self, pushes, monkeypatch):
        from core.event_attribution import person_for
        monkeypatch.setenv('HEVOLVE_OWNER_USER_ID', _NODE_OWNER)
        assert person_for(None) == _NODE_OWNER
        assert person_for('') == _NODE_OWNER

    def test_the_daemon_identity_is_the_node_owner(self, pushes, monkeypatch):
        """'system_daemon' is a string identity, never a users row id; a
        background turn for an unowned goal runs as it."""
        from core.constants import SYSTEM_DAEMON_IDENTITY
        from core.event_attribution import person_for
        monkeypatch.setenv('HEVOLVE_OWNER_USER_ID', _NODE_OWNER)
        assert person_for(SYSTEM_DAEMON_IDENTITY) == _NODE_OWNER

    def test_an_id_with_no_row_here_is_returned_as_it_is(self, pushes, monkeypatch):
        """The caller that must reach someone on this node checks the row."""
        from core.event_attribution import person_for
        monkeypatch.setenv('HEVOLVE_OWNER_USER_ID', _NODE_OWNER)
        assert person_for('nobody-here') == 'nobody-here'

    def test_the_callers_session_is_the_one_read(self, pushes, monkeypatch):
        from core.event_attribution import person_for
        monkeypatch.setenv('HEVOLVE_OWNER_USER_ID', _NODE_OWNER)
        db = _OneRow(SimpleNamespace(user_type='agent', owner_id='pf-in-session'))
        assert person_for('pf-only-in-that-session', db) == 'pf-in-session'
        assert db.asked == {'id': 'pf-only-in-that-session'}

    def test_a_failed_read_asks_the_node_owner(self, pushes, monkeypatch, caplog):
        import integrations.social.models as models
        from core.event_attribution import person_for
        monkeypatch.setenv('HEVOLVE_OWNER_USER_ID', _NODE_OWNER)

        def _down():
            raise RuntimeError('database is locked')

        monkeypatch.setattr(models, 'db_session', _down)
        assert person_for('pf-unreadable') == _NODE_OWNER
        assert any('pf-unreadable' in r.getMessage() for r in caplog.records)
