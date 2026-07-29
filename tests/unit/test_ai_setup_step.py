"""Native AI setup step -- the reusable decision logic.

Steward 2026-07-22: the AI setup wizard is a native post-install step, built by
"leverage existing no rebuild" -- it wires the EXISTING model_onboarding backend
into the EXISTING native_onboarding.py ceremony. The GTK4 UI ships/tests on a
node; what is verifiable on any box is the shared decision logic both callers
(the native step + the API + Nunba's wizard) depend on:
  * recommend_for_hardware() sizes the model to the box (reusing vram_manager),
    and NEVER recommends a model a potato cannot run.
  * needs_setup() reuses get_active_model (no parallel state) and fails toward
    OFFERING setup rather than silently skipping it.

Also guards the wiring invariant: native_onboarding routes "Begin" through the
existing backend callables, and the step can never block the ceremony.
"""
import os

import pytest

from integrations.service_tools import model_onboarding as mo


def test_recommend_falls_to_smallest_tier_on_a_potato(monkeypatch):
    """0 / low VRAM -> the smallest tier. A potato must never be handed a 7B."""
    class _VM:
        def detect_gpu(self):
            return {"total_gb": 0.0, "cuda_available": False}
    monkeypatch.setattr(mo, "_get_vram_manager", lambda: _VM())
    rec = mo.recommend_for_hardware()
    assert rec["model_name"] == mo.MODEL_TIERS[-1][1]
    assert rec["total_vram_gb"] == 0.0


def test_recommend_scales_up_with_vram(monkeypatch):
    class _VM:
        def __init__(self, gb):
            self._gb = gb
        def detect_gpu(self):
            return {"total_gb": self._gb}
    monkeypatch.setattr(mo, "_get_vram_manager", lambda: _VM(24.0))
    assert "7B" in mo.recommend_for_hardware()["label"]
    monkeypatch.setattr(mo, "_get_vram_manager", lambda: _VM(8.0))
    assert "3B" in mo.recommend_for_hardware()["label"]


def test_recommend_is_failsafe_when_probe_raises(monkeypatch):
    """A VRAM-probe exception must not crash the setup step -- it falls to the
    smallest tier (safe on any hardware)."""
    class _VM:
        def detect_gpu(self):
            raise RuntimeError("no gpu tools")
    monkeypatch.setattr(mo, "_get_vram_manager", lambda: _VM())
    rec = mo.recommend_for_hardware()
    assert rec["model_name"] == mo.MODEL_TIERS[-1][1]


def test_tiers_are_monotonic_and_smallest_is_the_catch_all():
    thresholds = [t[0] for t in mo.MODEL_TIERS]
    assert thresholds == sorted(thresholds, reverse=True), "tiers must descend by VRAM"
    assert mo.MODEL_TIERS[-1][0] == 0.0, "the smallest tier must accept any hardware"


def test_needs_setup_true_when_no_model_active(monkeypatch):
    monkeypatch.setattr(mo, "get_active_model", lambda: None)
    assert mo.needs_setup() is True


def test_needs_setup_false_when_a_model_is_active(monkeypatch):
    monkeypatch.setattr(mo, "get_active_model", lambda: {"model": "Qwen", "port": 8080})
    assert mo.needs_setup() is False


def test_needs_setup_failsafe_offers_setup_on_error(monkeypatch):
    def _boom():
        raise RuntimeError("state unavailable")
    monkeypatch.setattr(mo, "get_active_model", _boom)
    assert mo.needs_setup() is True  # fail toward offering, not skipping


def test_native_onboarding_wires_the_existing_backend_not_a_new_path():
    """Source guard (the GTK4 ceremony cannot be run on this box): the AI step in
    native_onboarding.py must call the EXISTING model_onboarding callables and
    must be non-blocking + fail-safe (a threaded worker + a skip button), so it
    can never wedge first boot."""
    # tests/unit/<file> -> repo root is THREE dirnames up.
    root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    src = open(os.path.join(root, "integrations", "agent_engine",
                            "native_onboarding.py"), encoding="utf-8").read()
    assert "from integrations.service_tools.model_onboarding import" in src
    assert "needs_setup" in src and "recommend_for_hardware" in src and "onboard(" in src
    assert "_build_ai_model_page" in src
    assert "Skip for now" in src, "the AI step must be skippable (never blocks boot)"
    assert "threading.Thread" in src, "onboard() must run off the UI thread"
