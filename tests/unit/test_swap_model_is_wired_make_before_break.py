"""llamacpp_manager.swap_model actually drives the never-dark sequence.

A coordinator with a green test suite is worth nothing if the product
calls something else -- so this exercises the REAL ``swap_model``, with
only two things stubbed, both of them genuine boundaries:

  * ``_spawn_locked``   -- launches a real llama-server subprocess
  * ``_check_health``   -- an HTTP GET to a real port

Everything between them is the shipped code: the ownership precondition,
``llm_swap.swap_main_llm``'s ordering, the real ``_wait_for_health`` loop,
the repoint, and ``_terminate``.

``_admission_facts`` is stubbed too, and deliberately: the real one calls
``get_catalog()``, which reads and can write the OWNER'S model catalog.
A test fixture that leaked into the real user catalog is already an open
finding (#109); this suite does not add a second one. What admission
DECIDES is proven against the real rule in
test_llm_swap_is_never_dark.py; what is proven here is that swap_model
asks it, and what it does with the answer.

    python -m pytest tests/unit/test_swap_model_is_wired_make_before_break.py -q
"""
import os
import sys

import pytest

_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from integrations.service_tools import llamacpp_manager as lcm

INCUMBENT_PORT = 8080
NEW_MODEL = 'F:/models/Tiel-Coder-35B-A3B-MTP-UD-Q4_K_XL.gguf'


class FakeProc:
    """Stands in for a llama-server subprocess."""

    def __init__(self, pid, alive=True):
        self.pid = pid
        self._alive = alive
        self.terminated = False

    def poll(self):
        return None if self._alive else 0

    def terminate(self):
        self.terminated = True
        self._alive = False

    def wait(self, timeout=None):
        return 0

    def kill(self):
        self._alive = False


@pytest.fixture
def mgr(monkeypatch):
    """A manager that OWNS a running incumbent on :8080."""
    m = lcm.LlamaCppManager()
    m._process = FakeProc(pid=111)
    m._port = INCUMBENT_PORT
    m._current_model = 'old.gguf'

    # Admission says yes; the rule itself is tested elsewhere (see module
    # docstring -- the real one touches the owner's catalog).
    monkeypatch.setattr(m, '_admission_facts', lambda path: {
        'free_vram_gb': 8.0, 'free_ram_gb': 32.0, 'gpu_available': True,
        'moe': False, 'vram_need_gb': 3.0, 'ram_need_gb': None,
        'whole_need_gb': None,
    })
    # Don't let a unit test publish geometry into the process environment.
    import core.llama_geometry as geo
    m.published = []
    monkeypatch.setattr(geo, 'publish_geometry',
                        lambda ctx, slots: m.published.append((ctx, slots)))
    return m


def arm(m, monkeypatch, *, healthy=True, spawn_ok=True):
    """Stub the two boundaries and record what the spawn was asked for."""
    m.spawned_on = []
    newcomer = FakeProc(pid=222)

    def _spawn(model_path, port, **kw):
        m.spawned_on.append((model_path, port))
        return (newcomer, 8192, 1) if spawn_ok else None

    monkeypatch.setattr(m, '_spawn_locked', _spawn)
    monkeypatch.setattr(m, '_check_health', lambda port=None: healthy)
    monkeypatch.setattr(lcm, '_HEALTH_START_TIMEOUT', 1)
    return newcomer


class TestOwnershipIsThePrecondition:
    """We swap only a server we started.  Someone else's server can be
    neither stopped (our handle is None, so the stop silently no-ops) nor
    doubled up on (the 2026-09-13 duplicate-:8080 incident)."""

    def test_a_server_we_did_not_start_is_not_swapped(self, mgr, monkeypatch):
        arm(mgr, monkeypatch)
        mgr._process = None
        assert mgr.swap_model(NEW_MODEL) is False
        assert mgr.spawned_on == [], 'launched a second main server'

    def test_a_dead_handle_is_not_treated_as_ownership(self, mgr, monkeypatch):
        arm(mgr, monkeypatch)
        mgr._process = FakeProc(pid=111, alive=False)
        assert mgr.swap_model(NEW_MODEL) is False
        assert mgr.spawned_on == []


class TestTheNewcomerComesUpBeside:
    def test_the_newcomer_does_not_take_the_incumbents_port(self, mgr,
                                                            monkeypatch):
        """Sharing the port IS break-before-make."""
        arm(mgr, monkeypatch)
        assert mgr.swap_model(NEW_MODEL) is True
        (_path, port), = mgr.spawned_on
        assert port != INCUMBENT_PORT, (
            'the newcomer was spawned on the port the incumbent is serving')

    def test_the_incumbent_is_retired_only_after_a_successful_swap(
            self, mgr, monkeypatch):
        incumbent = mgr._process
        arm(mgr, monkeypatch)
        assert mgr.swap_model(NEW_MODEL) is True
        assert incumbent.terminated is True

    def test_success_repoints_this_manager_at_the_newcomer(self, mgr,
                                                           monkeypatch):
        newcomer = arm(mgr, monkeypatch)
        assert mgr.swap_model(NEW_MODEL) is True
        assert mgr._process is newcomer
        assert mgr._port != INCUMBENT_PORT
        assert mgr._current_model == NEW_MODEL

    def test_geometry_is_announced_only_once_the_newcomer_is_the_endpoint(
            self, mgr, monkeypatch):
        """Announce what exists, not what was attempted -- the 2026-09-11
        shape, where the trimmer budgeted against a window nothing served."""
        arm(mgr, monkeypatch)
        assert mgr.swap_model(NEW_MODEL) is True
        assert mgr.published == [(8192, 1)]


class TestAFailedSwapLeavesTheNodeServing:
    """The whole point.  Every one of these was an outage before."""

    def test_a_newcomer_that_never_serves_costs_nothing(self, mgr,
                                                        monkeypatch):
        incumbent = mgr._process
        newcomer = arm(mgr, monkeypatch, healthy=False)

        assert mgr.swap_model(NEW_MODEL) is False
        assert incumbent.terminated is False, 'the node went dark'
        assert mgr._process is incumbent
        assert mgr._port == INCUMBENT_PORT
        assert mgr._current_model == 'old.gguf'
        assert newcomer.terminated is True, 'the dud was left running'
        assert mgr.published == [], 'announced a geometry nothing serves'

    def test_a_spawn_that_never_launches_costs_nothing(self, mgr,
                                                       monkeypatch):
        incumbent = mgr._process
        arm(mgr, monkeypatch, spawn_ok=False)

        assert mgr.swap_model(NEW_MODEL) is False
        assert incumbent.terminated is False
        assert mgr._process is incumbent
        assert mgr._port == INCUMBENT_PORT

    def test_a_model_that_does_not_fit_is_refused_before_anything_starts(
            self, mgr, monkeypatch):
        incumbent = mgr._process
        arm(mgr, monkeypatch)
        monkeypatch.setattr(mgr, '_admission_facts', lambda path: {
            'free_vram_gb': 0.5, 'free_ram_gb': 2.0, 'gpu_available': True,
            'moe': False, 'vram_need_gb': 20.0, 'ram_need_gb': None,
            'whole_need_gb': None,
        })
        assert mgr.swap_model(NEW_MODEL) is False
        assert mgr.spawned_on == []
        assert incumbent.terminated is False
