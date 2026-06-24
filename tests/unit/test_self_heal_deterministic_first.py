"""Self-heal is deterministic-FIRST, agentic-FALLBACK.

Guards the 2026-06-24 fix for the pyloudnorm self-heal loop.  A missing
dependency (``ModuleNotFoundError``) is remediated by a deterministic
``pip install``; the agentic code-agent goal is dispatched ONLY if that
install fails (rc != 0) — because editing source can never summon a
package and the code agent just loops, churning the GIL.  On success the
worker is reaped so the next call respawns with the dep present.

Regression targets:
  * gpu_worker.GPUWorker._maybe_self_heal_from_line   (P1 gate, P2 respawn)
  * goal_manager._build_self_heal_prompt              (P4 prompt routing)
"""
import sys
import types

from unittest.mock import MagicMock

import pytest

from integrations.service_tools import gpu_worker
from integrations.agent_engine.goal_manager import _build_self_heal_prompt


_MODNOTFOUND_LINE = "ModuleNotFoundError: No module named 'pyloudnorm'"


class _SyncThread:
    """Run the target inline so the daemon-threaded ``_install_async`` is
    deterministic in tests (no join/sleep race)."""

    def __init__(self, target=None, daemon=None, name=None, args=(), kwargs=None):
        self._target = target
        self._args = args
        self._kwargs = kwargs or {}

    def start(self):
        if self._target:
            self._target(*self._args, **self._kwargs)


def _make_worker():
    return gpu_worker.GPUWorker(
        name='chatterbox_turbo',
        module='integrations.service_tools.gpu_worker',
    )


def _fake_run(returncode):
    def _run(*a, **k):
        m = MagicMock()
        m.returncode = returncode
        return m
    return _run


@pytest.fixture
def he_mock(monkeypatch):
    """Install a mockable ``core.error_advice.handle_exception`` and run
    the self-heal install thread inline.  Returns the handle_exception
    mock so tests can assert dispatch / non-dispatch."""
    monkeypatch.setattr(gpu_worker.threading, 'Thread', _SyncThread)

    mock = MagicMock()
    if 'core' not in sys.modules:
        monkeypatch.setitem(sys.modules, 'core', types.ModuleType('core'))
    ea = types.ModuleType('core.error_advice')
    ea.handle_exception = mock
    monkeypatch.setitem(sys.modules, 'core.error_advice', ea)
    return mock


# ── P1 + P2 : gpu_worker gating + respawn ──────────────────────────────

def test_install_success_reaps_worker_and_skips_agentic(monkeypatch, he_mock):
    """rc == 0  →  worker reaped (P2), NO agentic goal (P1, loop killed)."""
    monkeypatch.setattr(gpu_worker.subprocess, 'run', _fake_run(0))
    w = _make_worker()
    stop_mock = MagicMock()
    monkeypatch.setattr(w, 'stop', stop_mock)

    w._maybe_self_heal_from_line(_MODNOTFOUND_LINE)

    stop_mock.assert_called_once()
    he_mock.assert_not_called()


def test_install_failure_dispatches_agentic_fallback(monkeypatch, he_mock):
    """rc != 0  →  agentic fallback fired (P1), worker NOT reaped."""
    monkeypatch.setattr(gpu_worker.subprocess, 'run', _fake_run(1))
    w = _make_worker()
    stop_mock = MagicMock()
    monkeypatch.setattr(w, 'stop', stop_mock)

    w._maybe_self_heal_from_line(_MODNOTFOUND_LINE)

    stop_mock.assert_not_called()
    he_mock.assert_called_once()
    _, kwargs = he_mock.call_args
    assert kwargs.get('category') == 'subprocess.tool_load'
    assert kwargs.get('context', {}).get('missing_package') == 'pyloudnorm'


def test_self_heal_idempotent_per_package(monkeypatch, he_mock):
    """A flood of identical tracebacks triggers exactly one install."""
    run_mock = MagicMock(side_effect=_fake_run(0))
    monkeypatch.setattr(gpu_worker.subprocess, 'run', run_mock)
    w = _make_worker()
    monkeypatch.setattr(w, 'stop', MagicMock())

    w._maybe_self_heal_from_line(_MODNOTFOUND_LINE)
    w._maybe_self_heal_from_line(_MODNOTFOUND_LINE)

    assert run_mock.call_count == 1


def test_non_modulenotfound_line_is_ignored(monkeypatch, he_mock):
    """Unrelated stderr must not trigger any remediation."""
    run_mock = MagicMock(side_effect=_fake_run(0))
    monkeypatch.setattr(gpu_worker.subprocess, 'run', run_mock)
    w = _make_worker()
    monkeypatch.setattr(w, 'stop', MagicMock())

    w._maybe_self_heal_from_line("INFO: loading model weights...")

    run_mock.assert_not_called()
    he_mock.assert_not_called()


# ── P4 : goal_manager prompt routing ───────────────────────────────────

def _goal(category, ctx):
    return {
        'title': 'Self-heal test',
        'description': 'desc',
        'config': {'category': category, 'context': ctx},
    }


def test_prompt_missing_package_routes_to_dependency_remediation():
    """subprocess.tool_load + missing_package (no backend) must NOT fall
    through to the generic 'read source, write fix' loop."""
    p = _build_self_heal_prompt(
        _goal('subprocess.tool_load', {'missing_package': 'pyloudnorm'})
    )
    assert 'pyloudnorm' in p
    assert 'NOT a source-code bug' in p
    assert 'Read the source file' not in p   # generic path must be skipped


def test_prompt_backend_case_still_repairs_venv():
    """Regression guard: the existing backend-repair branch is untouched."""
    p = _build_self_heal_prompt(
        _goal('subprocess.tool_load', {'backend': 'chatterbox'})
    )
    assert 'repair_backend_venv' in p


def test_prompt_generic_exception_still_edits_source():
    """A real code bug (non-dep category) still routes to source editing."""
    p = _build_self_heal_prompt(_goal('runtime.assertion', {}))
    assert 'Read the source file' in p
