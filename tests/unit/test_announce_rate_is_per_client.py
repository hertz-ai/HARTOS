"""The gossip rate limit must be per CLIENT, not per socket peer.

Behind Kong every request reaches central from the gateway address, so keying
the limiter on request.remote_addr put every node in the network into ONE
budget of 10 calls a minute. With each node announcing every gossip interval,
nodes were refused (429) and aged out of the peer table. The limiter keys on
core.auth_local.client_address(): the last X-Forwarded-For hop, believed only
when the socket peer is a forwarder this node runs (loopback, or the
configured TRUSTED_PROXY; central sets it to its gateway).  Review of
d35926896 (REJECTED, Critical): believing it from any PRIVATE socket peer
let every LAN host rotate a fake header past both limiters (199/199
announces, 100/100 device asks).

Runs the real routes through a Flask test client; the gossip handler behind
the limiter is stubbed so only the limiter's decision is under test.
"""
import os
import sys
from unittest.mock import patch

import pytest
from flask import Flask

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..'))

from integrations.social import discovery  # noqa: E402

GATEWAY = '172.21.0.1'


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv('TRUSTED_PROXY', GATEWAY)
    discovery._ANNOUNCE_RATE.clear()
    app = Flask(__name__)
    app.register_blueprint(discovery.discovery_bp)
    yield app.test_client()
    discovery._ANNOUNCE_RATE.clear()


def _announce(client, xff):
    # A body with no node_id: the route answers 400 AFTER the limiter, so a
    # 400 means "allowed through", a 429 means "rate limited".
    return client.post('/api/social/peers/announce', json={},
                       headers={'X-Forwarded-For': xff},
                       environ_base={'REMOTE_ADDR': GATEWAY})


def test_distinct_clients_behind_one_gateway_are_not_limited_together(client):
    codes = [_announce(client, '203.0.113.%d' % i).status_code for i in range(1, 12)]
    assert 429 not in codes, codes


def test_one_client_is_still_limited(client):
    codes = [_announce(client, '203.0.113.7').status_code for _ in range(12)]
    assert codes[:10] == [400] * 10 and codes[10:] == [429, 429], codes


def test_broadcast_and_embedding_delta_routes_are_per_client_too(client):
    for path in ('/api/social/peers/broadcast', '/api/social/peers/embedding-delta',
                 '/api/social/peers/exchange'):
        with patch.object(discovery, '_check_announce_rate', return_value=False) as rate:
            r = client.post(path, json={}, headers={'X-Forwarded-For': '198.51.100.4'},
                            environ_base={'REMOTE_ADDR': GATEWAY})
        assert r.status_code == 429, path
        assert rate.call_args[0][0] == '198.51.100.4', (path, rate.call_args)


# A PUBLIC socket peer is a direct client, not our gateway: its
# X-Forwarded-For is whatever it chose to write (review of 55d8b9152 measured
# 500 of 500 requests through, 500 keys in the table, with a fresh header each).
# Routable on purpose: Python's ipaddress counts the RFC 5737 documentation
# ranges (198.51.100.0/24 etc.) as private, which would make it a forwarder.
DIRECT = '93.184.216.34'


def test_a_direct_client_cannot_escape_the_limit_by_forging_the_header(client):
    codes = [client.post('/api/social/peers/announce', json={},
                         headers={'X-Forwarded-For': '203.0.113.%d' % i},
                         environ_base={'REMOTE_ADDR': DIRECT}).status_code
             for i in range(1, 30)]
    assert codes[:10] == [400] * 10, codes
    assert set(codes[10:]) == {429}, codes
    assert list(discovery._ANNOUNCE_RATE) == [DIRECT]


def test_the_configured_trusted_proxy_is_believed_even_when_public(client, monkeypatch):
    monkeypatch.setenv('TRUSTED_PROXY', DIRECT)
    codes = [client.post('/api/social/peers/announce', json={},
                         headers={'X-Forwarded-For': '203.0.113.%d' % i},
                         environ_base={'REMOTE_ADDR': DIRECT}).status_code
             for i in range(1, 15)]
    assert 429 not in codes, codes


def test_check_client_rate_charges_the_socket_peer_of_a_direct_client(
        monkeypatch):
    """check_client_rate is what security/middleware.py's device-ask limiter
    calls too, so both limiters charge the same client."""
    monkeypatch.setenv('TRUSTED_PROXY', GATEWAY)
    app = Flask(__name__)
    with patch.object(discovery, '_check_announce_rate', return_value=True) as rate:
        with app.test_request_context(headers={'X-Forwarded-For': '203.0.113.5'},
                                      environ_base={'REMOTE_ADDR': DIRECT}):
            discovery.check_client_rate()
        with app.test_request_context(headers={'X-Forwarded-For': '203.0.113.5'},
                                      environ_base={'REMOTE_ADDR': GATEWAY}):
            discovery.check_client_rate()
    assert [c[0][0] for c in rate.call_args_list] == [DIRECT, '203.0.113.5']


def test_idle_clients_are_swept_so_the_table_stays_bounded(client, monkeypatch):
    now = [1000.0]
    monkeypatch.setattr(discovery._time, 'time', lambda: now[0])
    discovery._rate_last_sweep[0] = 0.0
    for i in range(50):
        discovery._check_announce_rate('203.0.113.%d' % i)
    assert len(discovery._ANNOUNCE_RATE) == 50
    now[0] += discovery._RATE_WINDOW  # a window passes with no requests
    discovery._check_announce_rate('198.51.100.1')
    assert list(discovery._ANNOUNCE_RATE) == ['198.51.100.1']


LAN_HOST = '192.168.0.50'


def test_a_lan_host_cannot_escape_the_limit_by_forging_the_header(
        client, monkeypatch):
    """A private socket peer is a LAN machine, not a forwarder we run."""
    codes = [client.post('/api/social/peers/announce', json={},
                         headers={'X-Forwarded-For': '203.0.113.%d' % i},
                         environ_base={'REMOTE_ADDR': LAN_HOST}).status_code
             for i in range(1, 30)]
    assert codes[:10] == [400] * 10, codes
    assert set(codes[10:]) == {429}, codes
    assert list(discovery._ANNOUNCE_RATE) == [LAN_HOST]


def test_a_gateway_that_is_not_configured_is_not_believed(client, monkeypatch):
    monkeypatch.delenv('TRUSTED_PROXY')
    codes = [_announce(client, '203.0.113.%d' % i).status_code
             for i in range(1, 15)]
    assert codes[10:] == [429] * 4, codes


def test_a_proxy_on_this_host_is_believed(client, monkeypatch):
    monkeypatch.delenv('TRUSTED_PROXY')
    codes = [client.post('/api/social/peers/announce', json={},
                         headers={'X-Forwarded-For': '203.0.113.%d' % i},
                         environ_base={'REMOTE_ADDR': '127.0.0.1'}).status_code
             for i in range(1, 15)]
    assert 429 not in codes, codes


def test_the_announce_vantage_is_the_same_client_address(monkeypatch):
    """peer_announce hands _observed_ip() to address_evidence, which makes a
    row 'active' when it matches the url: a forged header must not."""
    monkeypatch.delenv('TRUSTED_PROXY', raising=False)
    app = Flask(__name__)
    with app.test_request_context(headers={'X-Forwarded-For': '10.1.0.5'},
                                  environ_base={'REMOTE_ADDR': LAN_HOST}):
        assert discovery._observed_ip() == LAN_HOST


def test_the_device_ask_limiter_charges_a_lan_host_once(monkeypatch):
    """security/middleware.py files a consent card per allowed ask: a LAN host
    rotating the header must not get a fresh budget per request."""
    monkeypatch.delenv('TRUSTED_PROXY', raising=False)
    discovery._ANNOUNCE_RATE.clear()
    app = Flask(__name__)
    allowed = 0
    for i in range(100):
        with app.test_request_context(
                headers={'X-Forwarded-For': '203.0.113.%d' % (i % 250)},
                environ_base={'REMOTE_ADDR': LAN_HOST}):
            allowed += discovery.check_client_rate()
    discovery._ANNOUNCE_RATE.clear()
    assert allowed == discovery._RATE_LIMIT, allowed


def test_a_proxy_that_names_no_client_leaves_the_announce_unconfirmed(
        monkeypatch):
    """F2 (review of 291e548df): a TRUSTED_PROXY that sends no header made
    the vantage '' and address_evidence(url, '') 'confirmed', so the row
    went active on no evidence.  The vantage falls back to the socket peer,
    which is the proxy, never the peer's own address."""
    from integrations.social.peer_discovery import address_evidence
    monkeypatch.setenv('TRUSTED_PROXY', GATEWAY)
    app = Flask(__name__)
    with app.test_request_context(environ_base={'REMOTE_ADDR': GATEWAY}):
        vantage = discovery._observed_ip()
    assert vantage == GATEWAY
    assert address_evidence('http://10.1.0.5:6777', vantage)[0] == 'unconfirmed'
