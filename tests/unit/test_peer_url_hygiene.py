"""#38 peer-table hygiene: is_unroutable_peer_url classification.

A peer row is only useful if its URL can point to ANOTHER node. Loopback,
docker-bridge and the dead :677 port never can, and (the subtle part) a local
ping to them SUCCEEDS, so the age-based health check keeps them 'active'
forever and inflates the census/gossip counts. This locks in the exact
class boundary: purge the wrong ones, keep legitimate LAN peers.
"""
import pytest

from integrations.social.peer_discovery import is_unroutable_peer_url


def _bad(url):
    ok, why = is_unroutable_peer_url(url)
    assert ok, f"expected {url!r} UNROUTABLE, got routable"
    assert why, "reason must be non-empty"


def _ok(url):
    ok, why = is_unroutable_peer_url(url)
    assert not ok, f"expected {url!r} routable, got unroutable ({why})"


class TestUnroutable:
    def test_docker_bridge(self):
        for u in ('http://172.17.0.2:6777', 'http://172.17.0.1:6777',
                  'http://172.17.255.254:6777'):
            _bad(u)

    def test_dead_677_port(self):
        # :677 is wrong regardless of host (the 6777 typo)
        for u in ('http://192.168.0.69:677', 'http://10.1.0.83:677',
                  'http://example.com:677'):
            _bad(u)

    def test_link_local_and_unspecified(self):
        _bad('http://169.254.1.1:6777')   # APIPA
        _bad('http://0.0.0.0:6777')

    def test_empty_and_hostless(self):
        # The structural gate owns empty + hostless. A syntactically-odd but
        # non-empty hostname is deliberately left to resolution/reachability,
        # not force-classed here (keeps the gate about the known pollution
        # classes, not general URL validation).
        _bad('')
        _bad('http://:6777')              # no host
        _bad('http:///path-only')        # no netloc host


class TestRoutable:
    def test_legit_lan_peers_are_kept(self):
        # The home machines (.69/.83) and Azure LAN must NOT be purged.
        for u in ('http://192.168.0.69:6777', 'http://192.168.0.83:6777',
                  'http://10.1.0.83:6777', 'http://10.1.1.77:6777'):
            _ok(u)

    def test_non_docker_172_16_is_kept(self):
        # 172.16/12 is RFC1918; only the 172.17.x docker bridge is purged.
        _ok('http://172.16.5.5:6777')
        _ok('http://172.20.1.1:6777')

    def test_public_hosts_and_dns_names(self):
        for u in ('https://azurekong.hertzai.com',
                  'https://central.hevolve.ai',
                  'http://example.com:6777',
                  'http://my-node.local:6777'):
            _ok(u)

    def test_correct_port_6777_is_fine(self):
        _ok('http://192.168.0.69:6777')

    def test_loopback_allowed_for_colocated_nodes(self):
        # Two co-located nodes (dev / test / single-box multi-process) reach
        # each other over loopback on DISTINCT ports. Rejecting this broke
        # tests/standalone/two_node_collaboration.py — the canonical hive proof.
        for u in ('http://127.0.0.1:7801', 'http://127.0.0.1:7802',
                  'http://localhost:6777', 'http://[::1]:6777'):
            _ok(u)


def test_reason_strings_are_specific():
    assert is_unroutable_peer_url('http://172.17.0.2:6777')[1] == 'docker bridge 172.17.x'
    assert is_unroutable_peer_url('http://10.1.0.5:677')[1] == 'dead :677 port'
    assert is_unroutable_peer_url('http://0.0.0.0:6777')[1] == 'unspecified/link-local ip'
