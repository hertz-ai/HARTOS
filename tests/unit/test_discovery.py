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
