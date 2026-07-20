"""P2 of the unified-sync refactor: SyncEngine.queue_entity is the SINGLE
producer. It resolves the entity from the registry (SyncEntity.match), runs the
canonical gate (privacy/consent), serializes (no assets) and queues — no
isinstance ladder, no second producer. The old sync_to_parent/
sync_agent_to_parent are now thin shims over it (their behaviour is covered by
test_sync_agent_producer.py / federation tests — kept green = zero regression).

Behavioural: real queue_entity, SyncEngine.queue + the entity helpers mocked at
the boundary.

    python -m pytest tests/unit/test_sync_queue_entity.py --noconftest -q
"""
import types
from unittest.mock import patch

import integrations.social.sync_engine as se


def test_public_post_routes_to_sync_post_queue():
    # REAL public-post shape: Post.to_dict OMITS 'privacy' entirely when NULL
    # (= public), so the match must NOT require a 'privacy' key. Regression guard
    # for the self-review bug where default-public posts silently never synced.
    with patch.object(se.SyncEngine, 'queue', return_value='q1') as q, \
            patch('integrations.social.federation.federation._outbox_message',
                  side_effect=lambda o: {'type': 'new_post', 'post': o}):
        out = se.SyncEngine.queue_entity(
            None, {'id': 'p1', 'author_id': 'u1', 'content_type': 'text'})
    assert out == 'q1'
    _, tier, op, msg = q.call_args.args
    assert tier == 'central' and op == 'sync_post' and msg['type'] == 'new_post'


def test_private_post_is_not_queued():
    # a real post dict (which MATCHES) whose privacy gates it OUT — exercises the
    # GATE, not a non-match.
    with patch.object(se.SyncEngine, 'queue') as q:
        out = se.SyncEngine.queue_entity(
            None, {'id': 'p2', 'author_id': 'u1', 'content_type': 'text',
                   'privacy': 'private'})
    assert out is None
    q.assert_not_called()


def test_public_agent_routes_to_register_agent_queue():
    user = types.SimpleNamespace(
        id='a1', user_type='agent', owner_id='o1',
        to_dict=lambda: {'id': 'a1', 'username': 'x', 'user_type': 'agent'})
    with patch.object(se.SyncEngine, 'queue', return_value='q2') as q, \
            patch('integrations.social.consent_service.ConsentService.check_consent',
                  return_value=True) as chk, \
            patch('integrations.social.federation.federation._agent_skill_summary',
                  return_value=[]), \
            patch('integrations.social.peer_discovery.gossip',
                  types.SimpleNamespace(node_id='n', base_url='u', node_name='N')):
        out = se.SyncEngine.queue_entity(None, user)
    assert out == 'q2'
    _, tier, op, msg = q.call_args.args
    assert tier == 'central' and op == 'register_agent'
    assert msg['agent']['owner_id'] == 'o1'          # owner_id round-trips
    cargs = chk.call_args.args
    assert cargs[1] == 'o1' and cargs[2] == 'public_exposure'  # the right gate


def test_unconsented_agent_not_queued():
    user = types.SimpleNamespace(id='a2', user_type='agent', owner_id='o2',
                                 to_dict=lambda: {})
    with patch.object(se.SyncEngine, 'queue') as q, \
            patch('integrations.social.consent_service.ConsentService.check_consent',
                  return_value=False):
        out = se.SyncEngine.queue_entity(None, user)
    assert out is None
    q.assert_not_called()


def test_unknown_obj_no_match_no_queue():
    human = types.SimpleNamespace(id='h', user_type='human')
    with patch.object(se.SyncEngine, 'queue') as q:
        assert se.SyncEngine.queue_entity(None, human) is None
        assert se.SyncEngine.queue_entity(None, {'random': 'non-entity dict'}) is None
    q.assert_not_called()


def test_queue_failure_is_swallowed_best_effort():
    with patch.object(se.SyncEngine, 'queue', side_effect=RuntimeError('down')), \
            patch('integrations.social.federation.federation._outbox_message',
                  side_effect=lambda o: {'type': 'new_post', 'post': o}):
        out = se.SyncEngine.queue_entity(
            None, {'id': 'p3', 'author_id': 'u1', 'content_type': 'text'})
    assert out is None          # never blocks the caller's write
