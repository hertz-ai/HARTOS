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


class _Entry:
    """Minimal stand-in for catalog ModelEntry (only the fields selection reads)."""
    def __init__(self, mid, name, vram, ram, prio, quality=0.5, disk=1.0):
        self.id, self.name = mid, name
        self.repo_id = f"vendor/{mid}"
        self.vram_gb, self.ram_gb, self.disk_gb = vram, ram, disk
        self.priority, self.quality_score = prio, quality
        self.enabled, self.downloaded = True, False
        self.purposes, self.min_build = ['main'], None
        self.capabilities = {'quant': 'Q4_K_M'}


_TINY = _Entry('tiny', 'Tiny 0.8B', vram=1.0, ram=2.0, prio=30)
_MID = _Entry('mid', 'Mid 2B', vram=2.5, ram=4.0, prio=40)
_BIG = _Entry('big', 'Big 7B', vram=6.0, ram=12.0, prio=55)


def _catalog(monkeypatch, entries=(_TINY, _MID, _BIG)):
    """Point selection at a known catalog so these assert policy, not seed data."""
    class _Cat:
        def list_by_type(self, t):
            return list(entries)
    monkeypatch.setattr(mo, "_get_catalog", lambda: _Cat())


def _gpu(monkeypatch, **gpu):
    class _VM:
        def detect_gpu(self):
            return gpu
    monkeypatch.setattr(mo, "_get_vram_manager", lambda: _VM())


def test_recommend_never_exceeds_the_budget(monkeypatch):
    """The core safety invariant: whatever is chosen must FIT. A potato must
    never be handed a model it cannot run."""
    _catalog(monkeypatch)
    _gpu(monkeypatch, total_gb=0.0, cuda_available=False)
    monkeypatch.setattr(mo, "compute_budget", lambda: {
        'budget_gb': 2.0, 'source': 'ram', 'run_mode': 'cpu',
        'total_vram_gb': 0.0, 'free_vram_gb': 0.0, 'total_ram_gb': 3.4,
        'gpu_name': None})
    rec = mo.recommend_for_hardware()
    assert rec['model_id'] == 'tiny', "must pick the only entry that fits in 2GB RAM"


def test_recommend_scales_up_with_vram(monkeypatch):
    """A real GPU gets the biggest entry that fits, not merely a bigger one."""
    _catalog(monkeypatch)
    _gpu(monkeypatch, total_gb=24.0, free_gb=24.0, cuda_available=True, name='RTX')
    assert mo.recommend_for_hardware()['model_id'] == 'big'
    _gpu(monkeypatch, total_gb=3.0, free_gb=3.0, cuda_available=True, name='small')
    assert mo.recommend_for_hardware()['model_id'] == 'mid'


def test_cpu_only_box_uses_RAM_not_just_vram(monkeypatch):
    """REGRESSION GUARD. The VRAM-only ladder this replaced collapsed every
    CPU-only box to the smallest model however much RAM it had. A no-GPU box
    with plenty of RAM must get a real model."""
    _catalog(monkeypatch)
    _gpu(monkeypatch, total_gb=0.0, cuda_available=False)   # no usable GPU
    monkeypatch.setattr(mo, "psutil", None, raising=False)
    import psutil
    monkeypatch.setattr(psutil, "virtual_memory",
                        lambda: type('M', (), {'total': 32 * 1024 ** 3})())
    rec = mo.recommend_for_hardware()
    assert rec['source'] == 'ram' and rec['run_mode'] == 'cpu'
    assert rec['model_id'] == 'big', "32GB RAM must reach the big entry on CPU"


def test_present_but_driver_gated_gpu_falls_to_the_ram_arm(monkeypatch):
    """VRAM that no CUDA build can address must not be spent. cuda_available
    already folds in the driver-too-old gate, so the budget comes from RAM."""
    _catalog(monkeypatch)
    _gpu(monkeypatch, total_gb=8.0, free_gb=8.0, cuda_available=False,
         name='GeForce 940MX', driver_cuda_ok=False)
    import psutil
    monkeypatch.setattr(psutil, "virtual_memory",
                        lambda: type('M', (), {'total': 8 * 1024 ** 3})())
    b = mo.compute_budget()
    assert b['source'] == 'ram', "an unusable GPU must not fund a VRAM budget"
    assert b['total_vram_gb'] == 8.0, "but its VRAM is still reported"


def test_recommend_is_failsafe_when_probe_raises(monkeypatch):
    """A probe exception must not crash the setup step; it still returns a
    usable recommendation with the legacy keys intact."""
    _catalog(monkeypatch)
    class _VM:
        def detect_gpu(self):
            raise RuntimeError("no gpu tools")
    monkeypatch.setattr(mo, "_get_vram_manager", lambda: _VM())
    rec = mo.recommend_for_hardware()
    assert rec['model_name'] and rec['quant']
    assert all(k in rec for k in ('model_name', 'quant', 'label', 'total_vram_gb'))


def test_recommend_survives_an_unavailable_catalog(monkeypatch):
    """The catalog is the source of truth, but its absence must not leave the
    caller without a model."""
    def _boom():
        raise RuntimeError("catalog down")
    monkeypatch.setattr(mo, "_get_catalog", _boom)
    _gpu(monkeypatch, total_gb=0.0, cuda_available=False)
    rec = mo.recommend_for_hardware()
    assert rec['model_name'], "must still name a fallback model"
    assert all(k in rec for k in ('model_name', 'quant', 'label', 'total_vram_gb'))


def test_legacy_return_contract_is_preserved(monkeypatch):
    """native_onboarding._build_ai_model_page reads these four keys."""
    _catalog(monkeypatch)
    _gpu(monkeypatch, total_gb=8.0, free_gb=8.0, cuda_available=True, name='gpu')
    rec = mo.recommend_for_hardware()
    for k in ('model_name', 'quant', 'label', 'total_vram_gb'):
        assert k in rec, f"legacy caller depends on {k}"


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
