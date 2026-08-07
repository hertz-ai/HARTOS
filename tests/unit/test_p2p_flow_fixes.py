"""Task #629 — internet-wide P2P unblock, four fixes in one change-set.

Live findings these tests guard (all measured 2026-08-07):
- central's registry held 147 peers, 146 fresh, and 0 routable URLs
  (67 localhost + 80 private-IP) — nodes can only CLAIM addresses, and
  behind NAT every claim is wrong.
- flat desktops resolved no sync parent (env-only parent_tier_url), so
  queue_entity queued public posts fleet-wide that never drained, while
  azurekong's /api/social/hierarchy/sync ingress answered 200 the whole
  time.
- federation.push_to_followers — docstring "Called when a post is
  created locally" — had ZERO production callers, so auto-federated
  LAN peers still held fully disjoint feeds.
- nat.py rung 5 returned a WAMP router URL that link_manager's
  '/peer_link' strip mangled into a garbage dial address.
"""
import os
from unittest import mock

import pytest


# ─── core.superadmins.resolve_reachable_central ──────────────────────────────

@pytest.fixture
def fresh_resolver_cache():
    from core import superadmins
    saved = dict(superadmins._resolve_cache)
    superadmins._resolve_cache.update({'url': '', 'expires': 0.0})
    yield superadmins
    superadmins._resolve_cache.update(saved)


def _resp(code):
    r = mock.Mock()
    r.status_code = code
    return r


def test_resolver_picks_first_alive_central(fresh_resolver_cache):
    sa = fresh_resolver_cache
    with mock.patch('core.http_pool.pooled_get',
                    side_effect=[Exception('dns dead'), _resp(200)]) as pg:
        assert sa.resolve_reachable_central(force=True) == \
            'https://azurekong.hertzai.com'
    # Probed in priority order: primary first, fallback second.
    assert pg.call_count == 2
    assert 'central.hevolve.ai' in pg.call_args_list[0].args[0]
    assert 'azurekong' in pg.call_args_list[1].args[0]


def test_resolver_returns_empty_when_all_down(fresh_resolver_cache):
    sa = fresh_resolver_cache
    with mock.patch('core.http_pool.pooled_get',
                    side_effect=Exception('offline')):
        assert sa.resolve_reachable_central(force=True) == ''


def test_resolver_caches_positive_answer(fresh_resolver_cache):
    sa = fresh_resolver_cache
    with mock.patch('core.http_pool.pooled_get',
                    return_value=_resp(200)) as pg:
        first = sa.resolve_reachable_central(force=True)
        second = sa.resolve_reachable_central()
    assert first == second != ''
    # Second call served from cache — no extra probe.
    assert pg.call_count == 1


def test_resolver_rejects_non_200(fresh_resolver_cache):
    sa = fresh_resolver_cache
    with mock.patch('core.http_pool.pooled_get', return_value=_resp(503)):
        assert sa.resolve_reachable_central(force=True) == ''


# ─── SyncEngine.parent_tier_url fallback ─────────────────────────────────────

def test_parent_tier_url_env_always_wins(monkeypatch):
    from integrations.social.sync_engine import SyncEngine
    monkeypatch.setenv('HEVOLVE_CENTRAL_URL', 'https://my-central.example')
    with mock.patch('core.superadmins.resolve_reachable_central') as res:
        assert SyncEngine.parent_tier_url() == 'https://my-central.example'
    res.assert_not_called()


def test_parent_tier_url_falls_back_to_reachable_central(monkeypatch):
    from integrations.social.sync_engine import SyncEngine
    monkeypatch.delenv('HEVOLVE_CENTRAL_URL', raising=False)
    monkeypatch.delenv('HEVOLVE_REGIONAL_URL', raising=False)
    with mock.patch('core.superadmins.resolve_reachable_central',
                    return_value='https://azurekong.hertzai.com'):
        assert SyncEngine.parent_tier_url() == 'https://azurekong.hertzai.com'


def test_parent_tier_url_offline_keeps_prior_no_parent_behaviour(monkeypatch):
    from integrations.social.sync_engine import SyncEngine
    monkeypatch.delenv('HEVOLVE_CENTRAL_URL', raising=False)
    monkeypatch.delenv('HEVOLVE_REGIONAL_URL', raising=False)
    with mock.patch('core.superadmins.resolve_reachable_central',
                    return_value=''):
        assert SyncEngine.parent_tier_url() == ''


# ─── PostService.create wires BOTH federation legs (AST drift guard) ─────────

def test_post_create_calls_both_federation_legs():
    """The horizontal leg (push_to_followers) went unwired for its whole
    life because nothing asserted the call site existed.  Walk the source
    so removing either leg fails a named test, not a user's feed."""
    import ast
    import inspect
    import integrations.social.services as services
    tree = ast.parse(inspect.getsource(services))
    calls = {
        node.func.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
    }
    assert 'sync_to_parent' in calls, 'vertical federation leg unwired'
    assert 'push_to_followers' in calls, 'horizontal federation leg unwired'


# ─── observed-URL construction (pure helper) ─────────────────────────────────

def test_observed_url_pairs_observed_ip_with_claimed_port():
    from integrations.social.peer_discovery import GossipProtocol
    assert GossipProtocol._observed_url_for(
        'http://10.1.1.5:6777', '203.0.113.7') == 'http://203.0.113.7:6777'


def test_observed_url_defaults_port_by_scheme():
    from integrations.social.peer_discovery import GossipProtocol
    assert GossipProtocol._observed_url_for(
        'https://node.example', '203.0.113.7') == 'http://203.0.113.7:443'


def test_observed_url_empty_when_hint_adds_nothing():
    from integrations.social.peer_discovery import GossipProtocol
    f = GossipProtocol._observed_url_for
    assert f('http://192.168.0.15:5000', '') == ''
    assert f('http://192.168.0.15:5000', '127.0.0.1') == ''
    assert f('http://192.168.0.15:5000', '192.168.0.15') == ''


def test_observed_url_brackets_ipv6():
    from integrations.social.peer_discovery import GossipProtocol
    assert GossipProtocol._observed_url_for(
        'http://10.0.0.2:5000', '2001:db8::7') == 'http://[2001:db8::7]:5000'


# ─── announce echo consumption ───────────────────────────────────────────────

def _gossip_instance():
    from integrations.social.peer_discovery import GossipProtocol
    g = GossipProtocol.__new__(GossipProtocol)  # skip env-heavy __init__
    g._observed_public_ip = ''
    return g


def _echo_resp(payload):
    r = mock.Mock()
    r.json.return_value = payload
    return r


def test_echo_learns_public_ip():
    g = _gossip_instance()
    g._consume_observed_ip_echo(_echo_resp({'observed_ip': '203.0.113.9'}))
    assert g._observed_public_ip == '203.0.113.9'


def test_echo_ignores_private_and_missing():
    g = _gossip_instance()
    g._consume_observed_ip_echo(_echo_resp({'observed_ip': '192.168.0.15'}))
    assert g._observed_public_ip == ''
    g._consume_observed_ip_echo(_echo_resp({}))
    assert g._observed_public_ip == ''
    g._consume_observed_ip_echo(_echo_resp(None))
    assert g._observed_public_ip == ''


def test_self_info_advertises_observed_url_before_signing():
    """observed_url must be inside the signed field set — a field added
    after signing is the exact make-every-announce-unverifiable defect
    documented at the signing block."""
    import inspect
    from integrations.social.peer_discovery import GossipProtocol
    src = inspect.getsource(GossipProtocol._self_info)
    obs_at = src.index("observed_url")
    sign_at = src.index("sign_json_payload")
    assert obs_at < sign_at, 'observed_url added after signing'


# ─── nat.py strategy 2b — observed WAN candidate ─────────────────────────────

def test_nat_tries_observed_url_when_claimed_host_is_private():
    from core.peer_link.nat import NATTraversal
    nat = NATTraversal()
    peer_info = {
        'url': 'http://10.0.0.5:5000',
        'metadata': {'observed_url': 'http://203.0.113.4:5000'},
    }
    with mock.patch.object(nat, '_try_lan_direct', return_value=None), \
         mock.patch.object(
             nat, '_try_direct_wan',
             side_effect=lambda host, port=0:
             f'ws://{host}:{port}/peer_link'
             if host == '203.0.113.4' else None) as wan:
        assert nat.resolve_peer_address(peer_info) == \
            'ws://203.0.113.4:5000/peer_link'
    tried_hosts = [c.args[0] for c in wan.call_args_list]
    assert tried_hosts == ['10.0.0.5', '203.0.113.4']


def test_nat_skips_observed_when_same_as_claimed():
    from core.peer_link.nat import NATTraversal
    nat = NATTraversal()
    peer_info = {
        'url': 'http://203.0.113.4:5000',
        'observed_url': 'http://203.0.113.4:5000',
    }
    with mock.patch.object(nat, '_try_lan_direct', return_value=None), \
         mock.patch.object(nat, '_try_direct_wan',
                           return_value=None) as wan, \
         mock.patch.object(nat, '_try_crossbar_relay', return_value=None):
        nat.resolve_peer_address(peer_info)
    assert [c.args[0] for c in wan.call_args_list] == ['203.0.113.4']


# ─── link_manager rung-5 guard — relay URLs are not dial addresses ───────────

def test_auto_upgrade_never_mangles_relay_url():
    from core.peer_link.link_manager import PeerLinkManager
    mgr = PeerLinkManager.__new__(PeerLinkManager)
    captured = {}
    mgr.upgrade_peer = lambda **kw: captured.update(kw)

    fake_gossip = mock.Mock()
    fake_gossip.get_peer_list.return_value = [
        {'node_id': 'peer-x', 'url': 'http://10.0.0.5:5000'}]
    fake_nat = mock.Mock()
    fake_nat.resolve_peer_address.return_value = 'ws://relay.example:8088/ws'

    with mock.patch.dict('sys.modules'), \
         mock.patch('integrations.social.peer_discovery.gossip', fake_gossip), \
         mock.patch('core.peer_link.nat.get_nat_traversal',
                    return_value=fake_nat):
        mgr._try_auto_upgrade('peer-x')

    # Relay URL skipped; fell back to the claimed URL's host:port —
    # never 'relay.example:8088/ws'.
    assert captured.get('address') == '10.0.0.5:5000'
