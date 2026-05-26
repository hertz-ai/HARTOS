"""Regression for task #80 — stale-cache reconciliation in
ModelOrchestrator.load().

Background: On 2026-05-04 the user observed Nunba spinning with
"Draft boot decision: ... → single (main-only)" every 30s for
14 minutes after llama-server died externally (CUDA crash).  Root
cause: orchestrator.load() short-circuited at "if entry.loaded:
return entry" without probing the actual subprocess.  ensure_loaded_async
returned "Model already loaded" forever, the supervisor never
respawned.

Fix: load() now calls loader.is_loaded(entry) — the live probe
override — when the cache says loaded.  If the probe says dead,
release VRAM, mark unloaded in catalog, fall through to a fresh
load.  Loaders without an override fall back to entry.loaded
(base class default), preserving prior behavior.

This test pins both halves of the contract.
"""
from __future__ import annotations

import os
import sys
import logging
from unittest.mock import MagicMock, patch

import pytest

_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)


def _make_orchestrator_with_loader(probe_returns: bool):
    """Build an orchestrator with a fake catalog + loader where
    is_loaded() returns the configured value.  Returns
    (orch, fake_loader, fake_entry, fake_catalog) so each test can
    assert on whichever side it cares about.
    """
    from integrations.service_tools.model_orchestrator import (
        ModelOrchestrator, ModelLoader,
    )

    fake_entry = MagicMock()
    fake_entry.id = 'llm-test-4b'
    fake_entry.model_type = 'llm'
    fake_entry.loaded = True              # cache says loaded
    fake_entry.device = 'gpu'
    fake_entry.vram_gb = 3.0
    fake_entry.ram_gb = 0.0

    fake_catalog = MagicMock()
    fake_catalog.get.return_value = fake_entry
    fake_catalog.mark_unloaded = MagicMock()
    fake_catalog.mark_loaded = MagicMock()
    fake_catalog.mark_error = MagicMock()
    fake_catalog.list_types.return_value = []
    fake_catalog.list_by_type.return_value = []

    class _FakeLoader(ModelLoader):
        def __init__(self):
            self.is_loaded_calls = 0
            self.load_calls = 0

        def is_loaded(self, entry):
            self.is_loaded_calls += 1
            return probe_returns

        def load(self, entry, run_mode):
            self.load_calls += 1
            return True

        def is_downloaded(self, entry):
            return True

        def download(self, entry):
            return True

    orch = ModelOrchestrator(catalog=fake_catalog)
    fake_loader = _FakeLoader()
    orch.register_loader('llm', fake_loader)
    return orch, fake_loader, fake_entry, fake_catalog


def test_load_returns_early_when_probe_confirms_loaded():
    """Cache says loaded AND probe says alive → return entry without
    re-running compute checks or _dispatch_load."""
    orch, loader, entry, catalog = _make_orchestrator_with_loader(
        probe_returns=True
    )
    result = orch.load('llm-test-4b')
    assert result is entry, "Expected to return the cached entry"
    assert loader.is_loaded_calls == 1, "is_loaded probe must be invoked"
    assert loader.load_calls == 0, "Should NOT re-dispatch load"
    assert catalog.mark_unloaded.call_count == 0, (
        "Catalog must NOT be marked unloaded — process is alive"
    )


def test_load_reconciles_stale_cache_when_probe_says_dead(caplog):
    """Cache says loaded BUT probe says dead → release VRAM,
    mark_unloaded in catalog, fall through to a fresh _dispatch_load.
    This is the exact failure mode that produced the 14-minute "Draft
    boot decision" loop on 2026-05-04 (task #80)."""
    orch, loader, entry, catalog = _make_orchestrator_with_loader(
        probe_returns=False
    )

    # Stub out the compute-state lookup so _attempt_swap doesn't blow
    # up looking for VRAMManager.  Returning gpu_available=True with
    # adequate free VRAM means matches_compute returns 'gpu' → load
    # path proceeds to _dispatch_load.
    orch._get_compute_state = MagicMock(return_value={
        'vram_free_gb': 8.0,
        'ram_free_gb': 16.0,
        'gpu_available': True,
    })
    entry.matches_compute = MagicMock(return_value='gpu')

    # Stub _register_vram so the load path doesn't need a real
    # VRAMManager.  Stub _release_vram so we can assert it was called
    # during reconciliation.
    orch._register_vram = MagicMock(return_value=True)
    orch._release_vram = MagicMock()
    orch._register_lifecycle = MagicMock()
    orch._register_service_tool = MagicMock()

    with caplog.at_level(logging.WARNING, logger='ModelOrchestrator'):
        result = orch.load('llm-test-4b')

    # Probe was invoked
    assert loader.is_loaded_calls == 1, "is_loaded probe must be invoked"
    # VRAM release happened during reconciliation (NOT just on
    # eventual unload — the point is to free VRAM so the fresh load
    # has room).
    assert orch._release_vram.call_args_list, (
        "VRAM must be released during stale-cache reconciliation"
    )
    # Catalog was marked unloaded so subsequent ensure_loaded_async
    # callers don't see the stale flag.
    assert catalog.mark_unloaded.call_args_list, (
        "Catalog must be marked unloaded during reconciliation"
    )
    # The reconciliation warning surfaced (operator visibility).
    stale_warnings = [
        r for r in caplog.records
        if r.levelname == 'WARNING' and 'Stale cache' in r.message
    ]
    assert stale_warnings, (
        f"Expected a 'Stale cache' WARNING. Got: "
        f"{[(r.levelname, r.message) for r in caplog.records]}"
    )
    # _dispatch_load was actually invoked — the fresh respawn happened.
    assert loader.load_calls == 1, (
        "Loader.load() must be invoked after stale-cache reconciliation "
        "(fresh respawn) — this is the bug fix"
    )
    # The fresh load succeeded so we got an entry back.
    assert result is entry


def test_load_falls_back_to_cache_flag_when_probe_raises(caplog):
    """If the loader's is_loaded() override itself raises, we trust
    the cache flag (conservative — better to over-report alive than
    spuriously evict a working model).  The probe failure must surface
    at WARNING for operator visibility."""
    from integrations.service_tools.model_orchestrator import (
        ModelOrchestrator, ModelLoader,
    )

    fake_entry = MagicMock()
    fake_entry.id = 'llm-test-4b'
    fake_entry.model_type = 'llm'
    fake_entry.loaded = True
    fake_entry.device = 'gpu'

    fake_catalog = MagicMock()
    fake_catalog.get.return_value = fake_entry

    class _RaisingLoader(ModelLoader):
        def is_loaded(self, entry):
            raise RuntimeError("simulated probe failure")

    orch = ModelOrchestrator(catalog=fake_catalog)
    orch.register_loader('llm', _RaisingLoader())

    with caplog.at_level(logging.WARNING, logger='ModelOrchestrator'):
        result = orch.load('llm-test-4b')

    assert result is fake_entry, (
        "Probe failure must NOT block the early-return — fall back to "
        "trusting the cache flag"
    )
    # Operator should see WHY the probe failed
    probe_warnings = [
        r for r in caplog.records
        if r.levelname == 'WARNING' and 'is_loaded probe failed' in r.message
    ]
    assert probe_warnings, (
        "Probe failure must log at WARNING (not be silent) so an open "
        "loader-bug isn't masked"
    )


def test_load_uses_default_is_loaded_when_loader_has_no_override():
    """Loaders that don't override is_loaded() inherit the base class
    default (return entry.loaded).  This preserves prior behavior for
    every loader except LlamaLoader (which is the bug-fix case).
    """
    from integrations.service_tools.model_orchestrator import (
        ModelOrchestrator, ModelLoader,
    )

    fake_entry = MagicMock()
    fake_entry.id = 'tts-piper-cpu'
    fake_entry.model_type = 'tts'
    fake_entry.loaded = True
    fake_entry.device = 'cpu'

    fake_catalog = MagicMock()
    fake_catalog.get.return_value = fake_entry

    class _MinimalLoader(ModelLoader):
        # No is_loaded override — uses base default
        pass

    orch = ModelOrchestrator(catalog=fake_catalog)
    orch.register_loader('tts', _MinimalLoader())
    result = orch.load('tts-piper-cpu')
    # Base default returns entry.loaded → True → early-return
    assert result is fake_entry
