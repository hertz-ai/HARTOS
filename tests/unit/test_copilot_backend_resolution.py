"""The co-pilot must dial where HARTOS is SERVING, on both deployments.

THE BUG (#71, diagnosed on the desktop by agent-4, fixed in Nunba a34b6244;
this file covers the HARTOS-side daemon which still carried it).

`hart_copilot_daemon.backend()` used `get_port("backend")`, which answers
"which port is the backend ASSIGNED" (6777). Two very different deployments
read that differently:

  standalone appliance (Samsung .69)  6777 IS bound -> correct
  bundled desktop                     HARTOS runs IN-PROCESS on the Flask port
                                      (5000); NOTHING binds 6777 -> dead dial

On the desktop every tick hit a closed socket, and because `next_task()` maps
any failure to "no work", the daemon logged

    {"action": "idle", "reason": "no task assigned by the hive"}

forever. That reads as an idle hive, not a broken dial -- the co-pilot looked
healthy while being structurally unable to receive a task. Its own docstring
claimed it used "the ONE canonical port source"; it used the wrong one.

The fix is the existing resolver `core.port_registry.get_local_backend_url()`,
which probes 'backend' then 'flask' and returns the first ACTUALLY LISTENING.
Neither deployment hardcodes the other's port.

These tests drive the REAL backend() against simulated listeners rather than
asserting on source text (CLAUDE.md Gate 5).

Run:
  pytest tests/unit/test_copilot_backend_resolution.py -v
"""

import importlib.util
import os
import sys

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if REPO not in sys.path:
    sys.path.insert(0, REPO)

DAEMON = os.path.join(REPO, 'scripts', 'hart_copilot_daemon.py')


def load_daemon():
    """Import the daemon module by path (scripts/ is not a package)."""
    spec = importlib.util.spec_from_file_location('hart_copilot_daemon', DAEMON)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def daemon():
    return load_daemon()


@pytest.fixture
def listening(monkeypatch):
    """Control which ports appear to be listening."""
    import core.port_registry as pr

    state = {'open': set()}

    def fake(port):
        return port in state['open']

    monkeypatch.setattr(pr, '_is_port_listening', fake)
    monkeypatch.delenv('HEVOLVE_BASE_URL', raising=False)
    return state


def port_of(url):
    return int(url.rsplit(':', 1)[1].rstrip('/'))


# -- the two real deployments -----------------------------------------------

def test_appliance_resolves_the_backend_port(daemon, listening):
    """Samsung .69: 6777 is bound. Verified live 2026-09-01 --
    /api/hive/session/tasks answers there. This must not change."""
    import core.port_registry as pr
    listening['open'] = {pr.get_port('backend')}

    assert port_of(daemon.backend()) == pr.get_port('backend')


def test_bundled_desktop_resolves_the_flask_port(daemon, listening):
    """THE REGRESSION TEST. Desktop: nothing binds 6777, HARTOS serves
    in-process on Flask. The old resolver dialled 6777 and the co-pilot idled
    forever."""
    import core.port_registry as pr
    listening['open'] = {pr.get_port('flask')}

    resolved = port_of(daemon.backend())
    assert resolved == pr.get_port('flask'), (
        'the co-pilot dialled %d while HARTOS was serving on %d -- every task '
        'poll hits a dead socket and reports an idle hive'
        % (resolved, pr.get_port('flask')))


def test_the_appliance_port_wins_when_both_listen(daemon, listening):
    """Probe order matters: a node serving both must be addressed as the
    appliance, not the bundle."""
    import core.port_registry as pr
    listening['open'] = {pr.get_port('backend'), pr.get_port('flask')}

    assert port_of(daemon.backend()) == pr.get_port('backend')


def test_cold_boot_falls_back_rather_than_crashing(daemon, listening):
    """Nothing listening yet (boot race). Must return a usable URL, not raise
    -- the daemon polls forever and an exception here would kill the loop."""
    listening['open'] = set()

    url = daemon.backend()
    assert url.startswith('http://')
    assert port_of(url) > 0


def test_an_explicit_base_url_wins(daemon, listening, monkeypatch):
    """Remote/cloud deploys set HEVOLVE_BASE_URL; probing must not override a
    deliberate operator choice."""
    monkeypatch.setenv('HEVOLVE_BASE_URL', 'http://10.0.0.5:9999/')
    import core.port_registry as pr
    listening['open'] = {pr.get_port('backend')}

    assert daemon.backend() == 'http://10.0.0.5:9999'


def test_backend_never_raises(daemon, monkeypatch):
    """The resolver is called on every poll tick. If it can raise, one bad
    import wedges the co-pilot shut -- the failure mode this daemon exists to
    avoid."""
    import core.port_registry as pr

    def boom(*a, **k):
        raise RuntimeError('registry unavailable')

    monkeypatch.setattr(pr, 'get_local_backend_url', boom)
    url = daemon.backend()
    assert url.startswith('http://'), 'backend() must always yield a URL'
