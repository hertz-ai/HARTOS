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
