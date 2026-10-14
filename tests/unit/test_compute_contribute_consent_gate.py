"""Regression test: the compute_contribute consent gate on the hive-serve routes.

Contributing THIS device's compute to the hive is EXPLICIT OPT-IN (privacy-first,
humans-always-in-control). The subsystem audit (2026-07-09) found compute_contribute
was DEFINED as a consent type but enforced NOWHERE — a peer could have this device run
inference/shard work with no consent. ComputeMeshService now gates _route_infer and
_route_shard fail-closed (reusing the canonical UserConsent table). A regression that
serves peer compute without consent fails here.

CI is the oracle (the mesh + social stack imports there); skips cleanly in a minimal env.
"""
from unittest import mock
import pytest

try:
    from integrations.agent_engine.compute_mesh_service import ComputeMeshService
except Exception:
    ComputeMeshService = None

pytestmark = pytest.mark.skipif(ComputeMeshService is None, reason="mesh stack not importable")


def test_route_infer_fails_closed_without_consent():
    """No compute_contribute consent (cold table / no db) => 403, never serve."""
    mesh = ComputeMeshService()
    status, _ctype, body = mesh._route_infer(b'{"prompt":"hi"}')
    assert status == 403, f"peer inference must be refused without consent, got {status}"
    assert b'consent_required' in body


def test_route_shard_fails_closed_without_consent():
    mesh = ComputeMeshService()
    status, _ctype, body = mesh._route_shard(b'\x00')
    assert status == 403 and b'consent_required' in body, \
        "sharded-model serving must also be gated by compute_contribute"


def test_route_infer_serves_when_consent_granted():
    """With consent granted, the peer's inference is served (gate opens)."""
    mesh = ComputeMeshService()
    with mock.patch.object(mesh, '_compute_contribute_consented', return_value=True), \
         mock.patch('core.http_pool.pooled_post') as pp:
        pp.return_value = mock.Mock(status_code=200, json=lambda: {'text': 'ok'})
        status, _ctype, _body = mesh._route_infer(b'{"prompt":"hi"}')
    assert status == 200, f"consented peer inference should serve, got {status}"


def test_gate_helper_fails_closed_on_any_error():
    """The helper must return False (do NOT contribute) on any consent-system error."""
    mesh = ComputeMeshService()
    # No real consent DB in the unit env → the helper's except-branch returns False.
    assert mesh._compute_contribute_consented() is False


def test_active_consent_query_excludes_revoked_rows():
    """A REVOKED compute_contribute consent must not keep authorising compute.

    revoke_consent() sets revoked_at and LEAVES granted=True, so a query that
    filters on granted alone treats a withdrawn grant as live and the device
    keeps serving peer compute after the human took the permission back. Pin
    that revoked_at is part of the active-consent predicate (the same one
    ConsentService.active_grant / check_consent use).
    """
    import contextlib
    mesh = ComputeMeshService()
    db = mock.MagicMock()
    db.query.return_value.filter.return_value.first.return_value = None

    @contextlib.contextmanager
    def _sess(commit=False):
        yield db

    # the helper imports db_session at call time, so patch the module attr
    with mock.patch('integrations.social.models.db_session', _sess):
        assert mesh._compute_contribute_consented() is False

    criteria = ' '.join(
        str(c) for c in db.query.return_value.filter.call_args[0])
    assert 'revoked_at' in criteria, (
        "active-consent query must filter revoked_at IS NULL, or a revoked "
        f"grant keeps authorising peer compute; got: {criteria}")


# ── The same gate over PeerLink (a node behind a NAT is served this way) ──

def _serve_over_peerlink(consented):
    """A requester link asks a serving link; the serving side runs the mesh's
    PeerLink handler.  Real PeerLink objects on an in-memory socket pair."""
    from tests.unit.test_peer_link import _linked_pair, _stop
    mesh = ComputeMeshService()
    asker, server = _linked_pair()
    server.on_message('compute', mesh.handle_peerlink_compute)
    try:
        with mock.patch.object(mesh, '_compute_contribute_consented',
                               return_value=consented), \
             mock.patch('core.http_pool.pooled_post') as pp:
            pp.return_value = mock.Mock(status_code=200,
                                        json=lambda: {'response': 'ok', 'model': 'm'})
            return asker.send('compute', {'prompt': 'hi', 'model_type': 'llm'},
                              wait_response=True, timeout=5)
    finally:
        _stop(asker, server)


def test_peerlink_compute_is_refused_without_consent():
    reply = _serve_over_peerlink(consented=False)
    assert reply is not None and reply.get('code') == 'consent_required'


def test_peerlink_compute_is_served_when_consent_granted():
    reply = _serve_over_peerlink(consented=True)
    assert reply is not None and reply.get('response') == 'ok'
    assert 'error' not in reply and 'served_by' in reply


def test_peerlink_and_http_serve_through_one_function():
    mesh = ComputeMeshService()
    with mock.patch.object(mesh, 'serve_infer', return_value=(200, {'response': 'x'})) as s:
        assert mesh.handle_peerlink_compute('compute', {'prompt': 'hi'}, 'p') == {'response': 'x'}
        mesh._route_infer(b'{"prompt":"hi"}')
    assert s.call_count == 2


def test_peerlink_compute_refuses_a_payload_with_no_prompt():
    mesh = ComputeMeshService()
    with mock.patch.object(mesh, 'serve_infer') as s:
        assert mesh.handle_peerlink_compute('compute', {}, 'p') == {'error': 'Invalid payload'}
        assert mesh.handle_peerlink_compute('compute', {'prompt': '  '}, 'p') == {'error': 'Invalid payload'}
        assert mesh.handle_peerlink_compute('compute', 'x', 'p') == {'error': 'Invalid payload'}
    s.assert_not_called()


def test_peerlink_compute_is_capped_across_links():
    import threading
    from integrations.agent_engine import compute_mesh_service as cms
    mesh = ComputeMeshService()
    gate, started = threading.Event(), []

    def slow(data):
        started.append(1)
        gate.wait(5)
        return 200, {'response': 'ok'}

    results = []
    with mock.patch.object(mesh, 'serve_infer', side_effect=slow):
        threads = [threading.Thread(
            target=lambda: results.append(mesh.handle_peerlink_compute('compute', {'prompt': 'hi'}, 'p')))
            for _ in range(cms._MAX_PEERLINK_INFERENCES + 3)]
        for t in threads:
            t.start()
        import time
        time.sleep(0.6)
        busy = [r for r in results if r.get('code') == 'busy']
        gate.set()
        for t in threads:
            t.join(5)
    assert len(started) == cms._MAX_PEERLINK_INFERENCES        # no more ran at once
    assert len(busy) == 3                                       # the rest were told so
