"""The hevolveai brain spawn is gated on the CHILD interpreter importing torch.

WHY (macOS incident 2026-06-16): the brain's api_server imports
weight_tracker -> ``import torch`` at module load.  ``_hevolveai_available``
only checked that *hevolveai* was importable in the parent, not torch.  On the
frozen macOS build ``_resolve_python_exe`` falls back to ``sys.executable`` --
the Nunba binary run as ``<exe> -c`` -- whose minimal frozen sys.path has no
torch, so the brain crash-looped ~20x and failed the post-build ``--validate``
DMG gate.

The fix adds ``_child_can_import_torch``: a POSITIVE capability gate that probes
the actual child interpreter once (cached) and is required by
``supervisor_should_run``.  Windows (python-embed child carries torch) is
unaffected -- the probe passes and the brain spawns normally.

Behavioral: mock the ``run_bounded`` boundary + ``_hevolveai_available``; assert
the gate.  Covers: probe pass/fail/timeout/spawn-error, caching, and the
supervisor_should_run composition.
"""
import os
import sys
import types
from unittest.mock import patch

_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import integrations.agent_engine.hevolveai_supervisor as sup  # noqa: E402


def _bounded(returncode=0, timed_out=False):
    """Stand-in for core.subprocess_safe.BoundedResult."""
    return types.SimpleNamespace(
        returncode=returncode, stdout='', stderr='', timed_out=timed_out)


def _reset_cache():
    sup._CHILD_TORCH_OK = None


def _patch_probe(**bounded_kw):
    """Patch run_bounded (imported lazily inside the probe) to return a
    BoundedResult-like object, and reset the module cache first."""
    _reset_cache()
    return patch('core.subprocess_safe.run_bounded',
                 return_value=_bounded(**bounded_kw))


# ── _child_can_import_torch ──────────────────────────────────────────
def test_torch_probe_true_when_child_resolves_torch():
    with _patch_probe(returncode=0):
        assert sup._child_can_import_torch() is True


def test_torch_probe_false_when_child_missing_torch():
    with _patch_probe(returncode=3):
        assert sup._child_can_import_torch() is False


def test_torch_probe_false_on_timeout():
    # run_bounded returns returncode=-1, timed_out=True on timeout.
    with _patch_probe(returncode=-1, timed_out=True):
        assert sup._child_can_import_torch() is False


def test_torch_probe_false_when_spawn_raises():
    _reset_cache()
    with patch('core.subprocess_safe.run_bounded',
               side_effect=FileNotFoundError('no interpreter')):
        assert sup._child_can_import_torch() is False


def test_torch_probe_is_cached_one_spawn_per_process():
    _reset_cache()
    with patch('core.subprocess_safe.run_bounded',
               return_value=_bounded(returncode=0)) as m:
        assert sup._child_can_import_torch() is True
        assert sup._child_can_import_torch() is True
        assert m.call_count == 1  # second call hit the cache, no respawn


# ── supervisor_should_run composition ────────────────────────────────
def test_should_run_false_when_torch_absent_even_if_hevolveai_present():
    with patch.dict(os.environ, {}, clear=False):
        os.environ.pop('HEVOLVE_SKIP_HEVOLVEAI_SPAWN', None)
        os.environ.pop('HEVOLVEAI_API_URL', None)
        with patch.object(sup, '_hevolveai_available', return_value=True), \
                patch.object(sup, '_child_can_import_torch',
                             return_value=False):
            assert sup.supervisor_should_run() is False


def test_should_run_true_when_hevolveai_and_torch_present():
    with patch.dict(os.environ, {}, clear=False):
        os.environ.pop('HEVOLVE_SKIP_HEVOLVEAI_SPAWN', None)
        os.environ.pop('HEVOLVEAI_API_URL', None)
        with patch.object(sup, '_hevolveai_available', return_value=True), \
                patch.object(sup, '_child_can_import_torch',
                             return_value=True):
            assert sup.supervisor_should_run() is True


def test_should_run_skips_torch_probe_when_hevolveai_absent():
    # Short-circuit order: never probe torch if hevolveai itself is missing.
    with patch.dict(os.environ, {}, clear=False):
        os.environ.pop('HEVOLVE_SKIP_HEVOLVEAI_SPAWN', None)
        os.environ.pop('HEVOLVEAI_API_URL', None)
        with patch.object(sup, '_hevolveai_available', return_value=False), \
                patch.object(sup, '_child_can_import_torch') as probe:
            assert sup.supervisor_should_run() is False
            probe.assert_not_called()
