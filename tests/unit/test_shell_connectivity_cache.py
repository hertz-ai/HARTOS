"""CAUSE 1 — the click-wifi / drag freeze: connectivity probing must NOT run on
the request thread.

shell_connectivity_summary() + shell_wifi() used to run up to SIX synchronous
4s subprocess.run() calls (nmcli x2, bluetoothctl x2, wpctl/pactl) in series ON
the waitress request thread. hartConnectivity.js polls /connectivity/summary
every ~8s + /network/wifi on popover-open/Rescan, so on a software-rendered lite
box (threads=1-2) the pool saturated and EVERY shell fetch froze behind it.

The fix moves all probing onto ONE background daemon (_ConnectivityCache) that
refreshes a cached snapshot; the request handlers return the cache INSTANTLY.

Behavioural (no grep): the prober is driven with mocked subprocess output and its
parsed cache asserted; the routes are exercised through the REAL Flask app with
the prober mocked and a forbidding subprocess.run, proving the request path never
spawns a process.
"""
from types import SimpleNamespace
from unittest import mock

import pytest

from integrations.agent_engine import liquid_ui_service as lus


def _cp(returncode, stdout=''):
    """A stand-in for subprocess.CompletedProcess (only .returncode/.stdout read)."""
    return SimpleNamespace(returncode=returncode, stdout=stdout)


def _fake_run(cmd, *a, **k):
    """Canned outputs for the prober's nmcli/bluetoothctl/hostname probes;
    everything else (wpctl/pactl) reports failure so volume reads unavailable."""
    if cmd[:3] == ['nmcli', 'radio', 'wifi']:
        return _cp(0, 'enabled\n')
    if cmd[:2] == ['nmcli', '-t'] and 'ACTIVE,SSID,SIGNAL' in cmd:
        return _cp(0, 'no:Cafe:30\nyes:HomeNet:71\n')
    if cmd[:2] == ['bluetoothctl', 'show']:
        return _cp(0, 'Controller AA:BB:CC\n\tPowered: yes\n\tDiscoverable: no\n')
    if cmd[:2] == ['bluetoothctl', 'devices']:
        return _cp(0, 'Device 11:22:33 Phone\nDevice 44:55:66 Buds\n')
    if 'SSID,SIGNAL,SECURITY,ACTIVE' in cmd:
        return _cp(0, 'HomeNet:71:WPA2:yes\nCafe:30:--:no\n')
    if cmd[:2] == ['hostname', '-I']:
        return _cp(0, '192.168.1.50 fe80::abcd\n')
    return _cp(1, '')  # wpctl / pactl / anything else -> unavailable


# ── The prober parses each domain into the cache ──────────────────────────────

def test_refresh_populates_summary_from_probes(monkeypatch):
    monkeypatch.setattr(lus.subprocess, 'run', _fake_run)
    cache = lus._ConnectivityCache()
    cache.refresh()
    snap = cache.summary()

    assert snap['wifi'] == {'available': True, 'enabled': True,
                            'connected': True, 'ssid': 'HomeNet', 'signal': 71}
    assert snap['bluetooth'] == {'available': True, 'powered': True,
                                 'connected_count': 2}
    # Volume tools absent in the fixture -> degrades cleanly, never crashes.
    assert snap['volume']['available'] is False
    # Battery shape is always present (psutil/sysfs best-effort).
    assert 'available' in snap['battery']


def test_refresh_populates_wifi_list(monkeypatch):
    monkeypatch.setattr(lus.subprocess, 'run', _fake_run)
    cache = lus._ConnectivityCache()
    cache.refresh()
    wifi = cache.wifi_networks()

    ssids = [n['ssid'] for n in wifi['networks']]
    assert ssids == ['HomeNet', 'Cafe']
    assert wifi['connected']['ssid'] == 'HomeNet'
    assert wifi['connected']['ip'] == '192.168.1.50'


def test_unprimed_cache_is_safe_defaults():
    """Before the daemon's first refresh the routes must still return a valid,
    everything-unavailable snapshot — never a crash, never a missing key."""
    cache = lus._ConnectivityCache()
    snap = cache.summary()
    assert snap['wifi']['available'] is False
    assert snap['bluetooth']['available'] is False
    assert cache.wifi_networks() == {'networks': [], 'connected': {}}


def test_summary_returns_a_copy_not_the_live_cache(monkeypatch):
    """A caller mutating the returned dict must not corrupt the cache."""
    monkeypatch.setattr(lus.subprocess, 'run', _fake_run)
    cache = lus._ConnectivityCache()
    cache.refresh()
    snap = cache.summary()
    snap['wifi']['ssid'] = 'TAMPERED'
    assert cache.summary()['wifi']['ssid'] == 'HomeNet'


def test_run_records_absent_tool_and_skips_it(monkeypatch):
    """A tool that raises FileNotFoundError is recorded once and never spawned
    again — the known-absent skip that keeps a toolless live USB cheap."""
    calls = []

    def fnf(cmd, *a, **k):
        calls.append(cmd[0])
        raise FileNotFoundError(cmd[0])

    monkeypatch.setattr(lus.subprocess, 'run', fnf)
    cache = lus._ConnectivityCache()
    assert cache._run(['nmcli', 'radio', 'wifi']) is None
    assert cache._run(['nmcli', 'radio', 'wifi']) is None
    assert calls == ['nmcli']  # second call short-circuited, no spawn


def test_run_timeout_returns_none_without_raising(monkeypatch):
    def slow(cmd, *a, **k):
        raise lus.subprocess.TimeoutExpired(cmd, 1.2)

    monkeypatch.setattr(lus.subprocess, 'run', slow)
    cache = lus._ConnectivityCache()
    assert cache._run(['nmcli', 'radio', 'wifi']) is None
    # A timeout is transient, NOT absence — the tool is retried next cadence.
    assert 'nmcli' not in cache._absent


# ── The routes read the cache with ZERO subprocess on the request path ────────

class _StubCache:
    """Stands in for the module prober: start() is a no-op (no daemon spawned),
    the reads return sentinels so the route's return value is unambiguous."""

    SUMMARY = {'wifi': {'available': True, 'enabled': True, 'connected': False,
                        'ssid': None, 'signal': None},
               'bluetooth': {'available': False, 'powered': False,
                             'connected_count': 0},
               'battery': {'available': False, 'percent': None,
                           'plugged_in': False, 'state': 'unknown'},
               'volume': {'available': False, 'volume': None, 'muted': None}}
    WIFI = {'networks': [{'ssid': 'HomeNet', 'signal': 71,
                          'security': 'WPA2', 'active': True}],
            'connected': {'ssid': 'HomeNet', 'ip': '10.0.0.2'}}

    def __init__(self):
        self.started = 0

    def start(self):
        self.started += 1

    def summary(self):
        return self.SUMMARY

    def wifi_networks(self):
        return self.WIFI


@pytest.fixture
def served_with_stub(monkeypatch):
    """Real Flask app, but the prober singleton replaced by a stub and the media
    idle-indexer neutralised, so the ONLY thing that could spawn a subprocess is
    a route handler."""
    monkeypatch.setattr(lus, '_connectivity_cache', _StubCache())
    monkeypatch.setattr(
        'integrations.agent_engine.media_semantic_index.register_idle_indexer',
        lambda *a, **k: False)
    svc = lus.LiquidUIService()
    app = svc._create_flask_app()
    app.testing = True
    return app.test_client()


def test_summary_route_returns_cache_without_subprocess(served_with_stub, monkeypatch):
    forbidden = mock.Mock(side_effect=AssertionError(
        'subprocess spawned on the connectivity request path'))
    monkeypatch.setattr(lus.subprocess, 'run', forbidden)

    r = served_with_stub.get('/api/shell/connectivity/summary')
    assert r.status_code == 200
    assert r.get_json() == _StubCache.SUMMARY
    forbidden.assert_not_called()
    # The route lazy-starts the prober (idempotent) — spawning a daemon thread,
    # NOT probing on the request path.
    assert lus._connectivity_cache.started >= 1


def test_wifi_route_returns_cache_without_subprocess(served_with_stub, monkeypatch):
    forbidden = mock.Mock(side_effect=AssertionError(
        'subprocess spawned on the wifi request path'))
    monkeypatch.setattr(lus.subprocess, 'run', forbidden)

    r = served_with_stub.get('/api/shell/network/wifi')
    assert r.status_code == 200
    assert r.get_json() == _StubCache.WIFI
    forbidden.assert_not_called()
    assert lus._connectivity_cache.started >= 1
