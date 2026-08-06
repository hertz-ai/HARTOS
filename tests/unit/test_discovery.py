"""
test_discovery.py - Tests for integrations/social/discovery.py

Tests the platform discovery + gossip protocol — how nodes find each other.
Each test verifies a specific network protocol contract or safety boundary:

FT: .well-known endpoint (platform metadata), peer announce (gossip),
    rate limiter (flood prevention), agent/community discovery.
NFT: Rate limiting enforcement, gossip flood rejection, well-known JSON
     schema stability, peer list safety (no internal IPs leaked).
"""
import os
import sys
import time
from unittest.mock import patch, MagicMock

import pytest
from flask import Flask

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from integrations.social.discovery import discovery_bp, _check_announce_rate, _ANNOUNCE_RATE


@pytest.fixture
def app():
    app = Flask(__name__)
    app.config['TESTING'] = True
    app.register_blueprint(discovery_bp)
    return app


@pytest.fixture
def client(app):
    return app.test_client()


@pytest.fixture(autouse=True)
def reset_rate_limiter():
    """Clear rate limiter between tests — shared module state."""
    _ANNOUNCE_RATE.clear()
    yield
    _ANNOUNCE_RATE.clear()


# ============================================================
# Rate limiter — prevents gossip flooding
# ============================================================

class TestRateLimiter:
    """_check_announce_rate prevents a single node from flooding the gossip protocol."""

    def test_allows_first_request(self):
        assert _check_announce_rate('192.168.1.1') is True

    def test_allows_within_limit(self):
        """10 requests within window = all allowed."""
        for i in range(10):
            assert _check_announce_rate('10.0.0.1') is True

    def test_rejects_over_limit(self):
        """11th request in same window = rejected (flood protection)."""
        for _ in range(10):
            _check_announce_rate('flood_ip')
        assert _check_announce_rate('flood_ip') is False

    def test_different_ips_independent(self):
        """Rate limiting is per-IP — one node flooding doesn't block others."""
        for _ in range(10):
            _check_announce_rate('bad_node')
        # bad_node is limited, but good_node should still work
        assert _check_announce_rate('good_node') is True

    def test_expires_after_window(self):
        """Old entries are pruned — after 60s, requests are allowed again."""
        # Fill the rate limit
        for _ in range(10):
            _check_announce_rate('temp_ip')
        # Manually expire the timestamps
        _ANNOUNCE_RATE['temp_ip'] = [time.time() - 120]  # 120s ago
        assert _check_announce_rate('temp_ip') is True


# ============================================================
# .well-known discovery endpoint
# ============================================================

class TestWellKnown:
    """/.well-known/hevolve-social.json — how external bots discover the platform."""

    def test_returns_200(self, client):
        resp = client.get('/.well-known/hevolve-social.json')
        assert resp.status_code == 200

    def test_returns_json(self, client):
        resp = client.get('/.well-known/hevolve-social.json')
        assert resp.content_type.startswith('application/json')

    def test_has_name(self, client):
        """Platform name — used by bots to identify the service."""
        data = client.get('/.well-known/hevolve-social.json').get_json()
        assert 'name' in data
        assert data['name'] == 'HevolveSocial'

    def test_has_version(self, client):
        data = client.get('/.well-known/hevolve-social.json').get_json()
        assert 'version' in data

    def test_has_description(self, client):
        data = client.get('/.well-known/hevolve-social.json').get_json()
        assert 'description' in data
        assert len(data['description']) > 10


# ============================================================
# Peer health endpoint
# ============================================================

class TestPeerHealth:
    """GET /api/social/peers/health — lightweight liveness check."""

    def test_returns_200(self, client):
        resp = client.get('/api/social/peers/health')
        assert resp.status_code == 200

    def test_returns_json(self, client):
        resp = client.get('/api/social/peers/health')
        data = resp.get_json()
        assert isinstance(data, dict)


# ============================================================
# Agent discovery
# ============================================================

class TestEndpointRegistration:
    """Verify all discovery endpoints are registered on the blueprint."""

    def test_agent_discovery_route_exists(self, app):
        rules = [r.rule for r in app.url_map.iter_rules()]
        assert '/api/social/discovery/agents' in rules

    def test_community_discovery_route_exists(self, app):
        rules = [r.rule for r in app.url_map.iter_rules()]
        assert '/api/social/discovery/communities' in rules

    def test_peer_announce_route_exists(self, app):
        rules = [r.rule for r in app.url_map.iter_rules()]
        assert '/api/social/peers/announce' in rules

    def test_peer_list_route_exists(self, app):
        rules = [r.rule for r in app.url_map.iter_rules()]
        assert '/api/social/peers' in rules

    def test_federation_inbox_exists(self, app):
        rules = [r.rule for r in app.url_map.iter_rules()]
        assert '/api/social/federation/inbox' in rules

    def test_integrity_code_hash_exists(self, app):
        rules = [r.rule for r in app.url_map.iter_rules()]
        assert '/api/social/integrity/code-hash' in rules


# ============================================================
# Announce signature must cover the whole payload
# ============================================================

class TestAnnounceSignatureCoversWholePayload:
    """The announce signature has to survive the receiver's reconstruction.

    _merge_peer verifies with ``{k: v for k, v in peer_data.items()
    if k != 'signature'}``. If the sender signs anything narrower, every
    announce fails verification, and since get_enforcement_mode() defaults to
    hard the peer is rejected outright. That happened live: _self_info signed
    mid-construction and then appended x25519_public, guardrail_hash,
    capability_tier, enabled_features, hardware_summary, idle_compute and
    current_version, so the network carried 69 registered nodes and every one
    reported remote_count 0.

    It stayed invisible for two reasons worth keeping in mind here. First,
    peer_announce returns HTTP 200 success:true on all five _merge_peer
    rejection paths, so a rejection is indistinguishable from a duplicate.
    Second, the tests that touched this either patched verify_json_signature
    out (tests/e2e/test_e2e_pipelines.py) or verified a hand-built dict rather
    than real _self_info output (test_security_modules_functional.py). So this
    test deliberately does neither: real payload, real crypto, receiver's exact
    reconstruction.
    """

    def _self_info(self):
        from integrations.social.peer_discovery import gossip
        return gossip._self_info()

    def test_signature_verifies_over_receivers_reconstruction(self):
        info = self._self_info()
        if not info.get('signature') or not info.get('public_key'):
            pytest.skip('node crypto identity unavailable in this environment')

        from security.node_integrity import verify_json_signature

        # Byte for byte what _merge_peer builds.
        receiver_view = {k: v for k, v in info.items() if k != 'signature'}
        assert verify_json_signature(
            info['public_key'], receiver_view, info['signature']), (
            'announce signature does not cover the payload actually sent; '
            'a field is being added to _self_info after sign_json_payload')

    def test_fields_added_after_signing_are_caught(self):
        """Appending a field post-signature must break verification.

        Guards the fix itself: if someone later adds an attribute below the
        signing call, the test above starts failing rather than the network
        going quiet.
        """
        info = self._self_info()
        if not info.get('signature') or not info.get('public_key'):
            pytest.skip('node crypto identity unavailable in this environment')

        from security.node_integrity import verify_json_signature

        tampered = {k: v for k, v in info.items() if k != 'signature'}
        tampered['some_field_added_after_signing'] = 'x'
        assert not verify_json_signature(
            info['public_key'], tampered, info['signature'])


# ============================================================
# A refused announce must say so
# ============================================================

class TestAnnounceReportsRejection:
    """HTTP 200 success:true used to cover every refusal.

    _merge_peer has eight rejection paths and all of them returned the same
    False that an already-known peer returns, so a node could be turned away
    by all of them while reading back success:true. `accepted` and `reason`
    make the outcome legible. Both paths exercised here reject before any DB
    access, so these tests need no database.
    """

    def test_merge_peer_records_why_it_refused(self):
        from integrations.social.peer_discovery import gossip
        reasons = []
        # No url. Rejected on the first check, before the session is touched,
        # so passing None for db is safe.
        assert gossip._merge_peer(None, {'node_id': 'abc'},
                                  reasons=reasons) is False
        assert reasons, 'rejection recorded no reason'
        assert 'url' in reasons[0]

    def test_merge_peer_stays_silent_when_no_list_passed(self):
        """The reasons list is opt-in; existing callers keep the old shape."""
        from integrations.social.peer_discovery import gossip
        assert gossip._merge_peer(None, {'node_id': 'abc'}) is False

    def test_endpoint_reports_accepted_false_with_reason(self, client):
        from integrations.social.peer_discovery import gossip
        # A node announcing itself is refused before any DB query.
        resp = client.post('/api/social/peers/announce', json={
            'node_id': gossip.node_id,
            'url': 'http://192.0.2.10:5000',
        })
        assert resp.status_code == 200
        body = resp.get_json()
        assert body['success'] is True      # request was well formed
        assert body['accepted'] is False    # but the peer was refused
        assert 'reason' in body and body['reason']


# ============================================================
# Compact gossip payloads must still verify
# ============================================================

class TestCompactPayloadStaysVerifiable:
    """_COMPACT_FIELDS drops fields the signature covers.

    It keeps signature and public_key but strips name, version, agent_count,
    post_count, x25519_public and current_version. Since the receiver verifies
    over every field except 'signature', a compacted record carrying the full
    record's signature cannot verify, and enforcement=hard then refuses the
    peer. That silently made every constrained and minimal profile node
    unverifiable. _gossip_self_info re-signs after compacting.
    """

    def test_compact_self_info_verifies(self):
        from unittest.mock import patch as _patch
        from integrations.social.peer_discovery import GossipProtocol
        from security.node_integrity import verify_json_signature

        with _patch.dict(os.environ,
                         {'HEVOLVE_GOSSIP_BANDWIDTH': 'constrained'}):
            gp = GossipProtocol()
            info = gp._gossip_self_info()

        if not info.get('signature') or not info.get('public_key'):
            pytest.skip('node crypto identity unavailable in this environment')

        assert len(info) < 19, 'expected a compacted payload'
        receiver_view = {k: v for k, v in info.items() if k != 'signature'}
        assert verify_json_signature(
            info['public_key'], receiver_view, info['signature']), (
            'compacted gossip payload must carry a signature over itself, '
            'not over the uncompacted record')


# ============================================================
# Relayed peer records are hints, not identity claims
# ============================================================

class TestRelayedPeersAreHints:
    """A record from another node's peer list cannot prove anything.

    PeerNode has no signature column, so a relayed record can only republish
    node_id, url and public_key. Judged by the direct-announce rules it is
    refused for having no signature, which meant no node could ever learn a
    third party and the topology could only be a star around whoever you
    contacted first. Measured against live central: of 72 records returned by
    /api/social/peers/exchange, exactly one carried a signature.

    So relayed records are admitted as unverified address hints, and the
    direct announce that follows is what actually verifies the peer.
    """

    def _db(self):
        """A stand-in session.

        _merge_peer queries for a banned row before it reaches the signature
        gate, so None is not usable here. first() returning None means "peer
        unknown", which is the path a newly relayed record takes.
        """
        db = MagicMock()
        db.query.return_value.filter.return_value.first.return_value = None
        db.query.return_value.filter.return_value.count.return_value = 0
        return db

    def test_unsigned_relayed_record_is_not_refused_for_being_unsigned(self):
        from integrations.social.peer_discovery import gossip
        reasons = []
        gossip._merge_peer(self._db(), {
            'node_id': 'relayed-hint-1',
            'url': 'http://192.0.2.77:5000',
        }, reasons=reasons, relayed=True)
        joined = ' '.join(reasons)
        assert 'no usable signature' not in joined, (
            f'a hint was refused for lacking a signature it cannot carry: '
            f'{joined}')

    def test_unsigned_direct_announce_is_still_refused(self):
        """The relayed path must not weaken the direct path."""
        from integrations.social.peer_discovery import gossip
        reasons = []
        assert gossip._merge_peer(self._db(), {
            'node_id': 'direct-unsigned-1',
            'url': 'http://192.0.2.78:5000',
        }, reasons=reasons) is False
        assert any('no usable signature' in r for r in reasons), reasons

    def test_relayed_central_tier_without_certificate_is_not_refused(self):
        """Central advertises tier=central and sends no certificate.

        Live: the only signed record central returns is its own, and it was
        refused with "tier central requires a certificate, none sent", so
        central could not be learned by anyone. As a hint the tier claim is
        unproven either way and gets checked when it announces directly.
        """
        from integrations.social.peer_discovery import gossip
        reasons = []
        gossip._merge_peer(self._db(), {
            'node_id': 'relayed-central-1',
            'url': 'http://192.0.2.79:6777',
            'tier': 'central',
        }, reasons=reasons, relayed=True)
        joined = ' '.join(reasons)
        assert 'requires a certificate' not in joined, joined

    def test_direct_central_tier_without_certificate_is_still_refused(self):
        """The certificate gate still guards a DIRECT central-tier claim.

        Must be signed to get there: the signature gate fires first, and an
        unsigned record never reaches the certificate check at all.
        """
        from integrations.social.peer_discovery import gossip
        try:
            from security.node_integrity import (sign_json_payload,
                                                 get_public_key_hex)
        except ImportError:
            pytest.skip('node crypto unavailable in this environment')

        payload = {
            'node_id': 'direct-central-1',
            'url': 'http://192.0.2.80:6777',
            'tier': 'central',
            'public_key': get_public_key_hex(),
        }
        if not payload['public_key']:
            pytest.skip('no keypair in this environment')
        payload['signature'] = sign_json_payload(
            {k: v for k, v in payload.items()})

        reasons = []
        assert gossip._merge_peer(self._db(), payload,
                                  reasons=reasons) is False
        assert any('certificate' in r for r in reasons), reasons
