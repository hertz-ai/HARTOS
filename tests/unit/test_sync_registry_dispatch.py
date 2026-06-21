"""P1 of the unified-sync refactor: the receiver dispatch is now a single
registry lookup (SYNC_ENTITIES + OP_DISPATCH), not an if/elif ladder. These
guard behaviour PARITY — every legacy op still routes to the same handler, and
adding an entity becomes a registration, not a branch.

Behavioural: real SyncEngine.receive_sync_batch, the handler mocked at the
dispatch boundary; db=None so the idempotency query is skipped.

    python -m pytest tests/unit/test_sync_registry_dispatch.py --noconftest -q
"""
import integrations.social.sync_engine as se


# The complete op set the legacy if/elif handled — parity guard (nothing dropped).
_LEGACY_OPS = {
    'sync_post', 'register_agent', 'sync_user',          # entities
    'revoke_token', 'sync_blocklist',                    # token/security mutations
    'update_stats', 'register_node',                     # log-only acks
    'coding_task_assign', 'coding_submission',
}


def test_registry_holds_the_three_entities():
    assert {'sync_post', 'register_agent', 'sync_user'}.issubset(se.SYNC_ENTITIES)
    for op, ent in se.SYNC_ENTITIES.items():
        assert ent.op == op
        assert callable(ent.apply)


def test_dispatch_covers_every_legacy_op():
    # No LEGACY op dropped — behaviour parity with the old if/elif ladder. New
    # entity ops (sync_community in P3, and onward) are additive registrations,
    # exactly as the unified registry intends.
    assert _LEGACY_OPS.issubset(set(se.OP_DISPATCH))


def test_entity_op_routes_through_registry(monkeypatch):
    seen = {}
    monkeypatch.setitem(se.OP_DISPATCH, 'sync_post',
                        lambda db, p: seen.setdefault('payload', p))
    res = se.SyncEngine.receive_sync_batch(
        None, [{'id': 'i1', 'operation_type': 'sync_post', 'payload': {'x': 1}}])
    assert seen['payload'] == {'x': 1}
    assert 'i1' in res['processed']
    assert res['errors'] == []


def test_non_entity_op_still_dispatches(monkeypatch):
    got = {}
    monkeypatch.setattr(se.SyncEngine, '_handle_revoke_token',
                        staticmethod(lambda p: got.setdefault('p', p)))
    res = se.SyncEngine.receive_sync_batch(
        None, [{'id': 'r1', 'operation_type': 'revoke_token', 'payload': {'jti': 'z'}}])
    assert got['p'] == {'jti': 'z'}          # lambda resolves the method at call time
    assert 'r1' in res['processed']


def test_unknown_op_is_noop_not_error():
    res = se.SyncEngine.receive_sync_batch(
        None, [{'id': 'i9', 'operation_type': 'bogus', 'payload': {}}])
    assert res['errors'] == []
    assert 'i9' in res['processed']          # acked (logged), never raised


def test_every_non_entity_op_executes_its_handler(monkeypatch):
    """Exercise every non-entity dispatch lambda BODY (the log-only acks +
    the blocklist mutation) so the registry wiring is 100% line-covered."""
    seen = []
    monkeypatch.setattr(se.SyncEngine, '_handle_sync_blocklist',
                        staticmethod(lambda p: seen.append(('blocklist', p))))
    ops = ['update_stats', 'register_node', 'coding_task_assign',
           'coding_submission', 'sync_blocklist']
    items = [{'id': f'n{i}', 'operation_type': op, 'payload': {'k': i}}
             for i, op in enumerate(ops)]
    res = se.SyncEngine.receive_sync_batch(None, items)
    assert res['errors'] == []
    assert len(res['processed']) == len(ops)        # all five acked
    assert ('blocklist', {'k': 4}) in seen          # the blocklist lambda ran
