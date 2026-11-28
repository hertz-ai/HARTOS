"""A model whose setup failed is remembered as failed on its catalog row.

MEASURED 2026-11-17 on the owner's desktop: NeuTTS's pip install failed at
107 boots in a row.  The failure was written with mark_error, which keeps it
in memory only (to_dict drops `error`), so every restart read the row as
never tried, select_best chose it again, and the same install ran again.

The row now keeps a failed SETUP across restarts, stamped with the build,
platform and time it failed:
  * select_best skips it, so the next engine is chosen;
  * a newer build or another platform reads it as never failed, so a fix in
    that build is tried;
  * it lapses after a day, as a failed CUDA ctranslate2 install does in
    Nunba, so a temporary cause (offline, a full disk) is tried again;
  * a successful load or download clears it;
  * a "needs <KEY>: ..." failure (gated weights) stops counting once the key
    is in the environment or the key vault;
  * a runtime error (mark_error) is still memory only.
"""
import json
import sys
import types

import pytest

from integrations.service_tools import model_catalog as mc
from integrations.service_tools.model_catalog import ModelCatalog, ModelEntry


@pytest.fixture
def build_a(monkeypatch):
    monkeypatch.setattr(mc, '_BUILD', ['build-A'])


@pytest.fixture
def no_vault_key(monkeypatch):
    vault = types.SimpleNamespace(get_tool_key=lambda key: None)
    import hartos.ai_key_vault as akv
    monkeypatch.setattr(akv, 'get_ai_key_vault', lambda: vault)
    return vault


def _row(mid, quality):
    return ModelEntry(id=mid, name=mid, model_type='tts', backend='torch',
                      supports_gpu=False, supports_cpu=True, vram_gb=0.0,
                      ram_gb=1.0, quality_score=quality, priority=50)


def _catalog(path):
    cat = ModelCatalog(catalog_path=str(path))
    if not cat.get('tts-better'):
        cat.register(_row('tts-better', 0.9))
        cat.register(_row('tts-other', 0.5))
    return cat


def _pick(cat):
    best = cat.select_best('tts', budget_vram_gb=0, budget_ram_gb=4,
                           gpu_available=False)
    return best.id if best else None


def test_the_better_engine_is_chosen_before_anything_failed(tmp_path, build_a, no_vault_key):
    assert _pick(_catalog(tmp_path / 'c.json')) == 'tts-better'


def test_a_failed_setup_survives_a_restart_and_the_next_engine_is_chosen(tmp_path, build_a, no_vault_key):
    path = tmp_path / 'c.json'
    _catalog(path).mark_setup_failed('tts-better', 'pip install neutts_air failed')

    after_restart = _catalog(path)
    row = after_restart.get('tts-better')
    assert row.error == 'pip install neutts_air failed'
    assert row.setup_failure_current() is True
    assert _pick(after_restart) == 'tts-other'


def test_a_newer_build_tries_the_failed_setup_again(tmp_path, build_a, no_vault_key, monkeypatch):
    path = tmp_path / 'c.json'
    _catalog(path).mark_setup_failed('tts-better', 'pip install neutts_air failed')

    monkeypatch.setattr(mc, '_BUILD', ['build-B'])
    newer = _catalog(path)
    assert newer.get('tts-better').error is None
    assert _pick(newer) == 'tts-better'


def test_another_platform_tries_the_failed_setup_again(tmp_path, build_a, no_vault_key):
    path = tmp_path / 'c.json'
    _catalog(path).mark_setup_failed('tts-better', 'pip install neutts_air failed')

    data = json.loads(path.read_text(encoding='utf-8'))
    for m in data['models']:
        if m['id'] == 'tts-better':
            assert m['error_platform'] == sys.platform
            m['error_platform'] = 'another-platform'
    path.write_text(json.dumps(data), encoding='utf-8')

    assert _pick(_catalog(path)) == 'tts-better'


def test_a_remembered_failure_lapses_after_a_day(tmp_path, build_a, no_vault_key, monkeypatch):
    path = tmp_path / 'c.json'
    _catalog(path).mark_setup_failed('tts-better', 'pip: could not reach the package index')

    cat = _catalog(path)
    assert _pick(cat) == 'tts-other'
    later = mc.time.time() + mc.SETUP_FAILURE_TTL_S + 1
    monkeypatch.setattr(mc.time, 'time', lambda: later)
    assert cat.get('tts-better').setup_failure_current() is False
    assert _pick(cat) == 'tts-better'


def test_a_successful_load_clears_the_failure_for_the_next_restart(tmp_path, build_a, no_vault_key):
    path = tmp_path / 'c.json'
    cat = _catalog(path)
    cat.mark_setup_failed('tts-better', 'pip install neutts_air failed')
    cat.mark_loaded('tts-better', device='cpu')

    after_restart = _catalog(path)
    assert after_restart.get('tts-better').error is None
    assert _pick(after_restart) == 'tts-better'


def test_a_successful_download_clears_the_failure(tmp_path, build_a, no_vault_key):
    path = tmp_path / 'c.json'
    cat = _catalog(path)
    cat.mark_setup_failed('tts-better', 'weights download failed')
    cat.mark_downloaded('tts-better', True)

    after_restart = _catalog(path)
    assert after_restart.get('tts-better').error is None
    assert _pick(after_restart) == 'tts-better'


def test_a_gated_download_comes_back_once_its_key_is_in_the_environment(tmp_path, build_a, no_vault_key, monkeypatch):
    path = tmp_path / 'c.json'
    monkeypatch.delenv('HARTOS_TEST_GATED_KEY', raising=False)
    _catalog(path).mark_setup_failed('tts-better', 'needs HARTOS_TEST_GATED_KEY: gated repo')

    cat = _catalog(path)
    assert _pick(cat) == 'tts-other'
    monkeypatch.setenv('HARTOS_TEST_GATED_KEY', 'set')
    assert cat.get('tts-better').setup_failure_current() is False
    assert _pick(cat) == 'tts-better'


def test_a_gated_download_comes_back_once_its_key_is_in_the_vault(tmp_path, build_a, no_vault_key, monkeypatch):
    path = tmp_path / 'c.json'
    monkeypatch.delenv('HARTOS_TEST_GATED_KEY', raising=False)
    _catalog(path).mark_setup_failed('tts-better', 'needs HARTOS_TEST_GATED_KEY: gated repo')

    cat = _catalog(path)
    assert _pick(cat) == 'tts-other'
    no_vault_key.get_tool_key = lambda key: 'stored' if key == 'HARTOS_TEST_GATED_KEY' else None
    assert _pick(cat) == 'tts-better'


def test_a_runtime_error_is_still_not_kept_across_a_restart(tmp_path, build_a, no_vault_key):
    path = tmp_path / 'c.json'
    cat = _catalog(path)
    cat.mark_error('tts-better', 'not enough video memory right now')
    cat._save()

    after_restart = _catalog(path)
    assert after_restart.get('tts-better').error is None
    assert _pick(after_restart) == 'tts-better'


def test_a_runtime_error_after_a_setup_failure_is_not_kept_either(tmp_path, build_a, no_vault_key):
    path = tmp_path / 'c.json'
    cat = _catalog(path)
    cat.mark_setup_failed('tts-better', 'pip install neutts_air failed')
    cat.mark_error('tts-better', 'not enough video memory right now')
    cat._save()

    assert _catalog(path).get('tts-better').error is None


# ── a loader that said why keeps its answer through the load ──────────

class _Loader:
    """A TTS loader as Nunba registers one; ``says`` is what load() does."""

    def __init__(self, says):
        self._says = says

    def load(self, entry, run_mode):
        return self._says(entry)

    def is_loaded(self, entry):
        return False


def _orchestrator(cat, loader, monkeypatch):
    from integrations.service_tools.model_orchestrator import ModelOrchestrator
    orch = ModelOrchestrator(catalog=cat)
    orch.register_loader('tts', loader)
    monkeypatch.setattr(orch, '_get_compute_state', lambda: {
        'vram_free_gb': 0.0, 'ram_free_gb': 8.0, 'gpu_available': False})
    return orch


def test_a_loader_that_recorded_a_failed_setup_is_not_chosen_again_in_this_run(
        tmp_path, build_a, no_vault_key, monkeypatch):
    """Live 2026-11-28: Nunba's TTS loader found a voice's weights cut,
    recorded the failed setup and returned False.  load() then wrote
    'Loader returned failure' with mark_error, which wipes the record from
    memory, so select_best handed the same voice back for the rest of the
    run."""
    cat = _catalog(tmp_path / 'c.json')

    def weights_cut(entry):
        cat.mark_setup_failed(entry.id, 'weights download failed: reset')
        return False

    orch = _orchestrator(cat, _Loader(weights_cut), monkeypatch)
    assert orch.load('tts-better') is None
    row = cat.get('tts-better')
    assert row.setup_failure_current()
    assert row.error == 'weights download failed: reset'
    assert _pick(cat) == 'tts-other'


def test_a_loader_that_only_says_no_is_still_a_runtime_error(
        tmp_path, build_a, no_vault_key, monkeypatch):
    cat = _catalog(tmp_path / 'c.json')
    orch = _orchestrator(cat, _Loader(lambda entry: False), monkeypatch)
    assert orch.load('tts-better') is None
    row = cat.get('tts-better')
    assert row.error == 'Loader returned failure'
    assert not row.setup_failure_current()
    assert _pick(cat) == 'tts-better'
