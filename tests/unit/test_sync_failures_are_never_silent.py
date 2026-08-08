"""Every failure on the sync path must say something in the log.

2026-08-08: a fleet-wide sync outage took most of a day to diagnose because
every handler on the path swallowed its exception with a bare ``pass``. The
log showed a healthy node talking to a healthy central, and a bare
``403 {"error":"unverified node identity"}`` with no way to tell WHICH of
five different faults produced it:

  * the batch was never signed (signing raised -> sent unsigned)
  * the envelope failed to decrypt (-> body has no node_id at all)
  * no peer row resolves the declared node_id (unknown sender)
  * a peer DOES resolve but its stored key is a different keypair
  * verification itself raised

The first and the fourth are completely different operational problems -
one is local, one is a registration mismatch - and they were indistinguishable.

These tests pin the log lines, not the return values, because the RETURN
VALUES WERE ALWAYS CORRECT. Silence was the defect.
"""
import ast
import inspect
import logging
from unittest import mock

import pytest


UUID_ID = '46329c87-cbb6-4ca1-bad5-816f6007b6a0'
PUBKEY = '25cedaa441302f25387a912257c7bb85' + '0' * 32


# ─── producer: a batch that cannot be signed must SAY it is unsigned ─────────

def test_signing_failure_is_logged_as_error(caplog):
    """The single highest-value line: an unsigned batch is a guaranteed 403."""
    from integrations.social.sync_engine import SyncEngine
    with mock.patch('security.node_integrity.sign_json_payload',
                    side_effect=RuntimeError('no key')), \
         caplog.at_level(logging.ERROR, logger='hevolve_social'):
        payload = SyncEngine._signed_send_payload(UUID_ID, [{'id': 'a'}])

    assert 'signature' not in payload, 'behaviour changed — must still degrade'
    blob = caplog.text
    assert 'FAILED TO SIGN' in blob, 'signing failure was silent'
    assert UUID_ID in blob, 'log must name the node whose batch is unsigned'
    assert '403' in blob, 'log must state the consequence, not just the fault'


def test_successful_signing_stays_quiet(caplog):
    """ZERO-NOISE PIN: the happy path must not warn, or the log stops being
    a signal (see the O(messages) log-bleed in task #623)."""
    from integrations.social.sync_engine import SyncEngine
    with caplog.at_level(logging.WARNING, logger='hevolve_social'):
        payload = SyncEngine._signed_send_payload(UUID_ID, [{'id': 'a'}])
    assert payload.get('signature'), 'precondition: signing should succeed'
    assert caplog.text.strip() == '', f'happy path logged noise: {caplog.text}'


def test_legacy_identity_fallback_is_logged(caplog):
    """Falling back to a key-prefix identity is a degraded mode: say so."""
    from integrations.social.sync_engine import SyncEngine
    broken = mock.Mock()
    broken.node_id = ''
    with mock.patch('integrations.social.peer_discovery.gossip', broken), \
         mock.patch('security.node_integrity.get_public_key_hex',
                    return_value=PUBKEY), \
         caplog.at_level(logging.WARNING, logger='hevolve_social'):
        nid = SyncEngine.canonical_node_id()

    assert nid == PUBKEY[:16], 'behaviour changed — fallback must still work'
    assert 'LEGACY' in caplog.text, 'silent downgrade to a legacy identity'


# ─── receiver: the 403 must name WHICH fault produced it ────────────────────

def _peer(node_id, public_key):
    p = mock.Mock()
    p.node_id = node_id
    p.public_key = public_key
    return p


def _db(by_node_id=None, by_prefix=None):
    db = mock.MagicMock()
    db.query.return_value.filter_by.return_value.first.return_value = by_node_id
    db.query.return_value.filter.return_value.first.return_value = by_prefix
    return db


def test_unknown_sender_is_distinguishable_in_the_log(caplog):
    from integrations.social.discovery import _verify_sync_sender
    with mock.patch('security.master_key.get_enforcement_mode',
                    return_value='hard'), \
         caplog.at_level(logging.WARNING):
        ok = _verify_sync_sender(
            _db(), {'items': [{'x': 1}], 'node_id': UUID_ID, 'signature': 's'})

    assert ok is False
    assert 'NO peer row resolves' in caplog.text
    assert UUID_ID in caplog.text, 'log must name the id that failed to resolve'


def test_key_mismatch_is_distinguishable_from_unknown_sender(caplog):
    """THE line that would have saved 2026-08-08.

    The peer IS known; its registered key just is not the one that signed.
    That is a registration/keypair problem, not an unknown-sender problem,
    and the operator must be able to tell them apart from the log alone.
    """
    from integrations.social.discovery import _verify_sync_sender
    with mock.patch('security.node_integrity.verify_json_signature',
                    return_value=False), \
         mock.patch('security.master_key.get_enforcement_mode',
                    return_value='hard'), \
         caplog.at_level(logging.WARNING):
        ok = _verify_sync_sender(
            _db(by_node_id=_peer(UUID_ID, PUBKEY)),
            {'items': [{'x': 1}], 'node_id': UUID_ID, 'signature': 'bad'})

    assert ok is False, 'a bad signature must still be rejected'
    blob = caplog.text
    assert 'DIFFERENT keypair' in blob, 'key mismatch reads as unknown sender'
    assert 'NO peer row resolves' not in blob, 'mislabelled as unknown sender'
    assert PUBKEY[:16] in blob, 'log must show the stored key it compared to'


def test_verification_exception_is_logged_not_swallowed(caplog):
    from integrations.social.discovery import _verify_sync_sender
    with mock.patch('security.node_integrity.verify_json_signature',
                    side_effect=RuntimeError('boom')), \
         mock.patch('security.master_key.get_enforcement_mode',
                    return_value='hard'), \
         caplog.at_level(logging.WARNING):
        ok = _verify_sync_sender(
            _db(by_node_id=_peer(UUID_ID, PUBKEY)),
            {'items': [{'x': 1}], 'node_id': UUID_ID, 'signature': 's'})

    assert ok is False, 'fail-closed behaviour must be unchanged'
    assert 'RAISED' in caplog.text, 'verification crash was swallowed silently'


# ─── mechanical guard: no bare `pass` handler may come back ─────────────────

GUARDED = [
    ('integrations.social.sync_engine', [
        'canonical_node_id', '_signed_send_payload', '_get_target_x25519',
        '_emit_sync_status', 'drain_queue']),
    ('integrations.social.discovery', [
        '_verify_sync_sender', 'hierarchy_sync']),
]


@pytest.mark.parametrize('modname,funcs', GUARDED)
def test_no_silent_exception_handlers_on_the_sync_path(modname, funcs):
    """A handler whose entire body is `pass` destroys the only evidence there
    was. Walk the AST of each function on the federation path and fail if one
    reappears — reviewers do not reliably catch a re-added `except: pass`.
    """
    import importlib
    mod = importlib.import_module(modname)
    tree = ast.parse(inspect.getsource(mod))

    silent = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef) or node.name not in funcs:
            continue
        for inner in ast.walk(node):
            if not isinstance(inner, ast.ExceptHandler):
                continue
            body = [s for s in inner.body
                    if not (isinstance(s, ast.Expr)
                            and isinstance(s.value, ast.Constant)
                            and isinstance(s.value.value, str))]  # drop docstr
            if len(body) == 1 and isinstance(body[0], ast.Pass):
                silent.append(f'{node.name}:{inner.lineno}')

    assert not silent, (
        f'{modname} has silent `except: pass` handlers on the sync path at '
        f'{silent} — every failure here must leave a log line')
