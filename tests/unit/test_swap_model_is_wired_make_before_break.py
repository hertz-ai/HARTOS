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
    # Nor the endpoint: record what the repoint announces instead.
    import core.port_registry as pr
    m.endpoints = []
    monkeypatch.setattr(pr, 'set_local_llm_url',
                        lambda url: m.endpoints.append(url))
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
    monkeypatch.setattr(lcm, '_SWAP_LOAD_TIMEOUT', 1)
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


class TestTheSwapReachesTheUser:
    """Review findings on 4d6d147e7 (probe-reproduced, 2026-09-22).  The
    ordering was right; the wiring stranded the node."""

    def test_success_moves_the_canonical_endpoint(self, mgr, monkeypatch):
        """The newcomer sits on an ephemeral port no resolver candidate
        lists.  If the canonical URL is not moved, retiring :8080 leaves
        every /chat dialling a refused port."""
        arm(mgr, monkeypatch)
        assert mgr.swap_model(NEW_MODEL) is True
        assert mgr.endpoints == [f'http://127.0.0.1:{mgr._port}'], (
            'the swap succeeded but the endpoint still names the old port')

    def test_a_failed_swap_does_not_move_the_endpoint(self, mgr, monkeypatch):
        arm(mgr, monkeypatch, healthy=False)
        assert mgr.swap_model(NEW_MODEL) is False
        assert mgr.endpoints == []

    def test_the_newcomer_is_given_a_load_not_a_launch(self, mgr,
                                                       monkeypatch):
        """llama-server answers /health 503 until the weights are loaded;
        the motivating model took ~7 minutes.  A 30 s window killed every
        such swap mid-load.  The incumbent keeps serving meanwhile, so a
        long wait costs nothing."""
        arm(mgr, monkeypatch)
        seen = {}
        real = mgr._wait_for_health

        def spy(proc=None, port=None, timeout=None):
            seen['timeout'] = timeout
            return real(proc, port, timeout=timeout)
        monkeypatch.setattr(mgr, '_wait_for_health', spy)
        monkeypatch.setattr(lcm, '_SWAP_LOAD_TIMEOUT', 900)
        assert mgr.swap_model(NEW_MODEL) is True
        assert seen['timeout'] == 900


class TestStopForgetsEvenWhenTheKillHangs:
    def test_state_is_cleared_when_terminate_raises(self, mgr, monkeypatch):
        import subprocess as sp

        def boom(proc):
            raise sp.TimeoutExpired('llama-server', 5)
        monkeypatch.setattr(lcm.LlamaCppManager, '_terminate',
                            staticmethod(boom))
        with pytest.raises(sp.TimeoutExpired):
            mgr.stop()
        assert mgr._process is None and mgr._current_model is None


class TestTheOnlyCallerHonoursTheOutcome:
    """model_onboarding.switch_model (the switch_model MCP tool, POST
    /api/models/switch, `hart model switch`) dropped swap_model's result
    and reported 'ready' on the OLD port either way."""

    def _run(self, monkeypatch, swapped, port=57855):
        import integrations.service_tools.model_onboarding as mo

        class _Resolver:
            def resolve(self, name, quant):
                from pathlib import Path
                return Path('F:/models/new-model-Q4_K_M.gguf')

        class _Lcpp:
            def __init__(self):
                self.port = port

            def swap_model(self, path):
                return swapped

        calls = []
        monkeypatch.setattr(mo, '_get_resolver', lambda: _Resolver())
        monkeypatch.setattr(mo, '_get_llamacpp_manager', lambda: _Lcpp())
        monkeypatch.setattr(mo, '_get_catalog', lambda: None)
        monkeypatch.setattr(mo, '_register_in_catalog',
                            lambda *a: calls.append(('catalog', a)))
        monkeypatch.setattr(mo, '_register_in_registry',
                            lambda *a: calls.append(('registry', a)))
        monkeypatch.setattr(mo, '_active_model', {'catalog_id': 'old',
                                                  'port': 8080})
        return mo.switch_model('org/new-model', 'Q4_K_M'), calls, mo

    def test_a_refused_swap_is_reported_as_an_error(self, monkeypatch):
        out, calls, mo = self._run(monkeypatch, swapped=False)
        assert out['status'] == 'error'
        assert calls == [], 'registered a model that never loaded'
        assert mo._active_model['catalog_id'] == 'old'

    def test_a_swap_registers_the_port_the_newcomer_is_on(self, monkeypatch):
        out, calls, _ = self._run(monkeypatch, swapped=True, port=57855)
        assert out['status'] == 'ready'
        assert out['endpoint'] == 'http://127.0.0.1:57855'
        registry = [c for c in calls if c[0] == 'registry']
        assert registry and registry[-1][1][-1] == 57855, calls
