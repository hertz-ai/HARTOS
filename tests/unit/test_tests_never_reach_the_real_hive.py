"""A test or CI process never reaches the real hive's centrals.

MEASURED 2026-09-26 (task #98): the owner's desktop held 1,967 peer rows in
10.1.x and 81 in 192.168.64.x, first seen inside CI windows (89% of 676 10.x
rows during a HARTOS Release run, vs 47% of random instants).  Release run
36152836720's pytest shard logs its own node announcing to the genesis seeds
and "auto-federated with 17343ed4 at http://10.1.0.62:6777": importing
hart_intelligence_entry runs init_social, which started gossip (and its
superadmin report-in) in the test process, and every shard registered a
throwaway node that central then relayed to every desktop.

The contract:
- under pytest the node's background services default OFF (config_cache.
  should_start_background_services; an explicit flag still wins), so
  importing the entry point starts no gossip;
- under pytest or in CI (GITHUB_ACTIONS / CI / NUNBA_CI) the real centrals
  (core.superadmins.ALL_CENTRAL_URLS) are never dialled: not as gossip
  seeds, not by the superadmin report-in, not by resolve_reachable_central,
  unless HEVOLVE_ALLOW_REAL_HIVE is set on purpose.

Behavioural: the real GossipProtocol, its real announce / gossip rounds, the
real report-in and resolver run with the socket boundary (getaddrinfo and
create_connection) recording every host they try.  The guard's one test
predicate is core.platform_paths.under_test, shared with the data-root guard.
"""
import json
import os
import socket
import subprocess
import sys
from urllib.parse import urlparse

import pytest

from core import superadmins

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
CENTRAL_HOSTS = {urlparse(u).hostname for u in superadmins.ALL_CENTRAL_URLS}
CI_VARS = ('GITHUB_ACTIONS', 'CI', 'NUNBA_CI', 'HEVOLVE_ALLOW_REAL_HIVE',
           'HEVOLVE_SEED_PEERS')


@pytest.fixture
def dialled(monkeypatch):
    """Every host a socket is asked for; each attempt is refused."""
    hosts = []

    def getaddrinfo(host, *a, **k):
        hosts.append(host)
        raise socket.gaierror('refused by test')

    def create_connection(address, *a, **k):
        hosts.append(address[0])
        raise OSError('refused by test')
    monkeypatch.setattr(socket, 'getaddrinfo', getaddrinfo)
    monkeypatch.setattr(socket, 'create_connection', create_connection)
    for v in CI_VARS:
        monkeypatch.delenv(v, raising=False)
    superadmins._resolve_cache.update(url='', expires=0.0)
    return hosts


def _gossip():
    from integrations.social.peer_discovery import GossipProtocol
    g = GossipProtocol()
    g._running = True
    return g


def _exercise(g):
    """The network half of GossipProtocol.start(), run synchronously."""
    g._announce_to_all()
    g._gossip_round()
    from core import superadmin_report
    superadmin_report.report_join({'node_id': 'livetest-node', 'url': 'x'})
    superadmin_report.drain_outbox()
    return superadmins.resolve_reachable_central(force=True)


def test_under_pytest_no_central_is_ever_dialled(dialled):
    g = _gossip()
    assert not (set(urlparse(u).hostname for u in g.seed_peers)
                & CENTRAL_HOSTS), g.seed_peers
    assert _exercise(g) == ''
    assert not (set(dialled) & CENTRAL_HOSTS), dialled


def test_a_central_named_in_the_seed_env_is_dropped_too(dialled, monkeypatch):
    monkeypatch.setenv('HEVOLVE_SEED_PEERS',
                       'https://central.hevolve.ai,http://10.9.9.9:6777')
    g = _gossip()
    assert g.seed_peers == ['http://10.9.9.9:6777'], g.seed_peers


def test_the_guard_is_the_reason(dialled, monkeypatch):
    """Anti-vacuity: with the deliberate override the same code DOES reach
    the centrals, so the test above is measuring the guard."""
    monkeypatch.setenv('HEVOLVE_ALLOW_REAL_HIVE', '1')
    g = _gossip()
    assert CENTRAL_HOSTS <= {urlparse(u).hostname for u in g.seed_peers}
    _exercise(g)
    assert set(dialled) & CENTRAL_HOSTS, dialled


@pytest.mark.parametrize('var, val', [('GITHUB_ACTIONS', 'true'),
                                      ('CI', 'true'), ('NUNBA_CI', '1')])
def test_a_ci_process_outside_pytest_does_not_dial_them(
        dialled, monkeypatch, var, val):
    """Nunba's e2e jobs run the real bundle, not pytest: CI alone must hold."""
    from core import platform_paths
    monkeypatch.setattr(platform_paths, 'under_test', lambda: False)
    monkeypatch.setenv(var, val)
    g = _gossip()
    assert _exercise(g) == ''
    assert not (set(dialled) & CENTRAL_HOSTS), dialled


def test_a_real_node_keeps_its_genesis_seeds(dialled, monkeypatch):
    from core import platform_paths
    monkeypatch.setattr(platform_paths, 'under_test', lambda: False)
    g = _gossip()
    assert CENTRAL_HOSTS <= {urlparse(u).hostname for u in g.seed_peers}


def test_background_services_default_off_under_pytest(monkeypatch):
    from core.config_cache import should_start_background_services
    monkeypatch.delenv('HEVOLVE_START_BACKGROUND_SERVICES', raising=False)
    assert should_start_background_services() is False
    monkeypatch.setenv('HEVOLVE_START_BACKGROUND_SERVICES', '1')
    assert should_start_background_services() is True


def test_importing_discovery_in_a_test_process_opens_no_socket():
    """A fresh interpreter that imports pytest (as every test process does)
    and then the discovery module and init_social's decision: no socket to
    anyone, and background services off."""
    probe = (
        'import json, socket, sys, time\n'
        'import pytest\n'
        'hosts = []\n'
        'def _gai(h, *a, **k):\n'
        '    hosts.append(h); raise socket.gaierror("x")\n'
        'def _cc(addr, *a, **k):\n'
        '    hosts.append(addr[0]); raise OSError("x")\n'
        'socket.getaddrinfo = _gai\n'
        'socket.create_connection = _cc\n'
        'import integrations.social.peer_discovery as pd\n'
        'from core.config_cache import should_start_background_services\n'
        'time.sleep(1.0)\n'
        'print(json.dumps({"hosts": hosts,\n'
        '  "services": should_start_background_services(),\n'
        '  "running": pd.gossip._running}))\n')
    env = {k: v for k, v in os.environ.items()
           if k not in CI_VARS + ('HEVOLVE_START_BACKGROUND_SERVICES',)}
    env['PYTHONPATH'] = os.pathsep.join(
        p for p in (ROOT, env.get('PYTHONPATH', '')) if p)
    out = subprocess.run([sys.executable, '-c', probe], cwd=ROOT, env=env,
                         capture_output=True, text=True, timeout=300)
    assert out.returncode == 0, out.stderr[-2000:]
    res = json.loads(out.stdout.strip().splitlines()[-1])
    assert res['hosts'] == [], res
    assert res['services'] is False and res['running'] is False, res
