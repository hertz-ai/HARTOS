"""Local PUBLIC agent up-sync (gap #4): a local agent + its skill summary rise
to central, gated by owner public_exposure consent — the agent twin of the post
up-sync.

federation.sync_agent_to_parent mirrors sync_to_parent one axis over (agents
instead of posts): the SAME gate→build→queue control flow, reusing the existing
ConsentService.check_consent gate (agents have no privacy column), the declared
'register_agent' op, and SyncEngine.queue.  The receiver _handle_sync_agent
un-stubs the log-only branch and upserts a User-by-id mirroring
_handle_sync_user.

Behavioural (mock the boundary, call the real fn, assert observable effects —
no grep tests, per memory/feedback_no_grep_tests.md), mirroring
tests/unit/test_sync_post_producer.py (patches SyncEngine.queue) and the
in-memory User stand-in style of test_profile_sync.py.
"""
import os
import sys
import types
from unittest.mock import MagicMock, patch

_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from integrations.social.federation import federation  # noqa: E402
from integrations.social.sync_engine import SyncEngine  # noqa: E402


def _agent_user(owner_id='owner-1'):
    """A stand-in agent User: user_type='agent', to_dict() returns the envelope
    payload.  owner_id None models an ownerless system/hive agent."""
    return types.SimpleNamespace(
        id='agent-9',
        username='swift.falcon.sathi',
        user_type='agent',
        owner_id=owner_id,
        to_dict=lambda: {
            'id': 'agent-9', 'username': 'swift.falcon.sathi',
            'display_name': 'swift falcon', 'user_type': 'agent',
            'agent_id': '65_0', 'owner_id': owner_id,
        },
    )


def _patch_gossip_and_skills(monkeypatch):
    """Patch the gossip origin fields + the skill summary so _agent_message
    builds without a real DB."""
    import integrations.social.peer_discovery as pd
    g = types.SimpleNamespace(node_id='nodeA', base_url='http://a', node_name='A')
    monkeypatch.setattr(pd, 'gossip', g, raising=False)
    monkeypatch.setattr(SyncEngine, '_handle_sync_agent', staticmethod(lambda *a, **k: None))
    # skill summary is a static query helper — stub it to a fixed list
    monkeypatch.setattr(federation, '_agent_skill_summary',
                        lambda db, user: [{'name': 'web_search', 'proficiency': 1.0}])


# ── PRODUCER: gate→build→queue ───────────────────────────────────────────────
def test_public_agent_queued_to_central_as_register_agent(monkeypatch):
    """Agent owner WITH public_exposure consent → queued once, target central,
    op register_agent, envelope type 'agent', the agent id rides along."""
    _patch_gossip_and_skills(monkeypatch)
    with patch('integrations.social.consent_service.ConsentService.check_consent',
               return_value=True) as chk, \
            patch('integrations.social.sync_engine.SyncEngine.queue',
                  return_value='qid-9') as q:
        out = federation.sync_agent_to_parent('DB', _agent_user('owner-1'))

    assert out == 'qid-9'
    # gated on the OWNER's public_exposure consent
    chk.assert_called_once()
    cargs = chk.call_args.args
    assert cargs[1] == 'owner-1'          # owner_id
    assert cargs[2] == 'public_exposure'  # the canonical public signal
    # queued as the declared register_agent op
    q.assert_called_once()
    db_arg, tier, op, msg = q.call_args.args
    assert db_arg == 'DB'
    assert tier == 'central'
    assert op == 'register_agent'
    assert msg['type'] == 'agent'
    assert msg['agent']['id'] == 'agent-9'
    assert msg['origin_node_id'] == 'nodeA'   # built via the shared gossip fields
    assert msg['skills'] == [{'name': 'web_search', 'proficiency': 1.0}]


def test_agent_without_consent_does_not_rise(monkeypatch):
    """No public_exposure consent → no queue (private agents never leak up)."""
    _patch_gossip_and_skills(monkeypatch)
    with patch('integrations.social.consent_service.ConsentService.check_consent',
               return_value=False), \
            patch('integrations.social.sync_engine.SyncEngine.queue') as q:
        out = federation.sync_agent_to_parent('DB', _agent_user('owner-1'))
    assert out is None
    q.assert_not_called()


def test_ownerless_agent_denied_by_real_consent_check(monkeypatch):
    """Ownerless system/hive agent (owner_id is None): the REAL
    ConsentService.check_consent denies (no consent row for a None owner) →
    not replicated as public user content.  Uses the real gate against a mock DB
    that returns no rows."""
    _patch_gossip_and_skills(monkeypatch)
    mock_db = MagicMock()
    mock_db.query.return_value.filter.return_value.first.return_value = None  # no consent row
    with patch('integrations.social.sync_engine.SyncEngine.queue') as q:
        out = federation.sync_agent_to_parent(mock_db, _agent_user(owner_id=None))
    assert out is None
    q.assert_not_called()


def test_non_agent_user_is_not_synced(monkeypatch):
    """A human User must never be queued by the agent producer."""
    human = types.SimpleNamespace(id='u1', user_type='human', owner_id=None)
    with patch('integrations.social.consent_service.ConsentService.check_consent') as chk, \
            patch('integrations.social.sync_engine.SyncEngine.queue') as q:
        out = federation.sync_agent_to_parent('DB', human)
    assert out is None
    chk.assert_not_called()   # short-circuits before the consent lookup
    q.assert_not_called()


def test_queue_failure_is_best_effort(monkeypatch):
    """A queue hiccup is swallowed — never blocks agent creation."""
    _patch_gossip_and_skills(monkeypatch)
    with patch('integrations.social.consent_service.ConsentService.check_consent',
               return_value=True), \
            patch('integrations.social.sync_engine.SyncEngine.queue',
                  side_effect=RuntimeError('queue down')):
        out = federation.sync_agent_to_parent('DB', _agent_user('owner-1'))
    assert out is None


# ── PRODUCER hook: services.register_agent fires the up-sync ──────────────────
def test_register_agent_fires_up_sync(monkeypatch):
    """The single canonical producer: UserService.register_agent calls
    sync_agent_to_parent after creating the agent (covers all register_agent
    call sites).  Patch the DB + sync hook, assert it fired with the new user."""
    from integrations.social.services import UserService

    created = {}

    class _DB:
        def query(self, _m):
            return self

        def filter(self, *a, **k):
            return self

        def first(self):
            return None          # username free

        def add(self, obj):
            created['user'] = obj

        def flush(self):
            pass

    fired = {}
    monkeypatch.setattr(federation, 'sync_agent_to_parent',
                        lambda db, user: fired.setdefault('user', user))

    user = UserService.register_agent(_DB(), 'swift falcon sathi', 'desc',
                                      agent_id='65_0', owner_id='owner-1',
                                      skip_name_validation=True)
    assert fired.get('user') is user          # the up-sync hook fired
    assert user.user_type == 'agent'


# ── PRODUCER re-sync: grant public_exposure rises the owner's existing agents ─
def test_grant_public_exposure_resyncs_owned_agents(monkeypatch):
    """create-before-consent ordering: granting public_exposure re-queues
    up-sync for the owner's agents so an agent created earlier rises now."""
    from integrations.social.consent_service import ConsentService

    a1, a2 = _agent_user('owner-1'), _agent_user('owner-1')
    a2.id = 'agent-10'

    # neutralise the audit/emit side-channels + the append-only insert
    monkeypatch.setattr('integrations.social.consent_service._audit', lambda *a, **k: None)
    monkeypatch.setattr('integrations.social.consent_service._emit', lambda *a, **k: None)

    class _DB:
        def add(self, obj):
            pass

        def flush(self):
            pass

    synced = []
    monkeypatch.setattr('integrations.social.services.UserService.get_owned_agents',
                        staticmethod(lambda db, uid: [a1, a2]))
    monkeypatch.setattr(federation, 'sync_agent_to_parent',
                        lambda db, agent: synced.append(agent.id))

    ConsentService.grant_consent(_DB(), 'owner-1', 'public_exposure')
    assert synced == ['agent-9', 'agent-10']   # both owned agents re-synced


def test_grant_other_consent_does_not_resync(monkeypatch):
    """A non-public consent grant must NOT trigger the agent re-sync."""
    from integrations.social.consent_service import ConsentService
    monkeypatch.setattr('integrations.social.consent_service._audit', lambda *a, **k: None)
    monkeypatch.setattr('integrations.social.consent_service._emit', lambda *a, **k: None)

    class _DB:
        def add(self, obj):
            pass

        def flush(self):
            pass

    def _must_not_run(db, uid):
        raise AssertionError("get_owned_agents must not be called for non-public consent")
    monkeypatch.setattr('integrations.social.services.UserService.get_owned_agents',
                        staticmethod(_must_not_run))
    # a 'data_access' grant must not touch the agent up-sync path
    ConsentService.grant_consent(_DB(), 'owner-1', 'data_access')
