"""An engine is installed when the ENGINE is on this node, not when the
HARTOS adapter for it imports.

`integrations/channels/media/tts_router.py` shipped a per-engine if/elif
whose branch for several engines was `from integrations.service_tools.
<x>_tool import <x>_synthesize` -- a HARTOS source file, so it always
imports and the engine always read installed.  Measured on the owner's
desktop 2026-09-21, with neither the `pocket_tts` package nor an
`espeak-ng` binary present:

    _is_engine_installed('pocket_tts')  -> True      (package absent)
    _is_engine_installed('cosyvoice3')  -> True      (package absent)
    _is_engine_installed('chatterbox_ml') -> True    (package absent)
    TTSRouter().synthesize(...)         -> 'All TTS engines failed' in 0.1 s

Two things follow from that lie, and both are user-visible:

  * `synthesize` picks engines it has already been told cannot run, tries
    them, and reports a generic failure -- the reply is silent.
  * `clone_engines_not_installed` skips every engine that reads installed,
    so `capability_setup.offer_voice_clone_setup` never names pocket_tts,
    and the on-demand "set this up?" card for the one CPU engine that
    would fix this machine is never filed.

The check is now answered from the spec each engine already carries
(`required_package`, `install_target`), so a new engine declares itself
once and no branch has to be written for it.  These tests mock the import
boundary and assert the observable answer.
"""
import pytest


@pytest.fixture(autouse=True)
def _fresh_cache(monkeypatch):
    from integrations.channels.media import tts_router
    monkeypatch.setattr(tts_router, '_engine_available_cache', {})


@pytest.fixture
def absent_packages(monkeypatch):
    """Nothing third-party is importable anywhere on this node."""
    from integrations.channels.media import tts_router
    monkeypatch.setattr(tts_router, '_package_importable',
                        lambda package, search_path=None: False)
    return tts_router


@pytest.fixture
def present_packages(monkeypatch):
    """Every declared package is importable, wherever it is looked for."""
    from integrations.channels.media import tts_router
    seen = []

    def _importable(package, search_path=None):
        seen.append((package, search_path))
        return True

    monkeypatch.setattr(tts_router, '_package_importable', _importable)
    return seen


class TestTheAdapterImportIsNotTheEngine:

    @pytest.mark.parametrize('engine_id', ['pocket_tts', 'cosyvoice3',
                                           'chatterbox_ml'])
    def test_engine_absent_from_this_interpreter_is_not_installed(
            self, absent_packages, engine_id):
        tts_router = absent_packages
        spec = tts_router.ENGINE_REGISTRY[engine_id]
        assert spec.tool_module, "this engine dispatches through a HARTOS tool"
        assert spec.required_package, (
            "an engine the node installs must declare what has to import")
        assert tts_router._is_engine_installed(engine_id) is False

    def test_every_seed_engine_declares_how_it_is_found(self):
        """No engine may be left to a branch: each one is a package, a
        bundled binary, or a cloud endpoint, and says which."""
        from integrations.channels.media import tts_router
        for engine_id, spec in tts_router._SEED_SPECS.items():
            target = spec.install_target
            if target in ('bundled', 'cloud'):
                continue
            assert spec.required_package, (
                f"{engine_id} installs into {target!r} but names no package, "
                f"so nothing can tell whether it is there")

    def test_present_package_makes_a_main_target_engine_installed(
            self, present_packages):
        from integrations.channels.media import tts_router
        assert tts_router._is_engine_installed('chatterbox_ml') is True
        assert ('chatterbox', None) in present_packages, (
            "a main-interpreter engine is looked for in this interpreter")


class TestVenvEngineNeedsThePackageInTheVenv:
    """A venv engine's worker runs from its own venv.  The venv existing
    only proves a directory was created -- the install may have failed, and
    then the worker dies at import after the turn has already waited."""

    def test_venv_without_the_package_is_not_installed(self, monkeypatch,
                                                       tmp_path):
        from integrations.channels.media import tts_router
        monkeypatch.setattr('core.venv_paths.venv_python_if_exists',
                            lambda backend: str(tmp_path / backend / 'python'))
        monkeypatch.setattr('core.venv_paths.venv_site_packages',
                            lambda backend: str(tmp_path / backend / 'site'))
        monkeypatch.setattr(tts_router, '_package_importable',
                            lambda package, search_path=None: False)
        assert tts_router.ENGINE_REGISTRY['f5_tts'].install_target == 'venv'
        assert tts_router._is_engine_installed('f5_tts') is False

    def test_venv_with_the_package_is_installed_and_looked_for_there(
            self, monkeypatch, tmp_path):
        from integrations.channels.media import tts_router
        site = str(tmp_path / 'f5_tts' / 'site')
        monkeypatch.setattr('core.venv_paths.venv_python_if_exists',
                            lambda backend: str(tmp_path / backend / 'python'))
        monkeypatch.setattr('core.venv_paths.venv_site_packages',
                            lambda backend: site)
        asked = []

        def _importable(package, search_path=None):
            asked.append((package, search_path))
            return True

        monkeypatch.setattr(tts_router, '_package_importable', _importable)
        assert tts_router._is_engine_installed('f5_tts') is True
        assert asked == [('f5_tts', [site])], (
            "the engine is looked for in ITS venv, not in this interpreter")

    def test_no_venv_at_all_is_not_installed(self, monkeypatch,
                                             present_packages):
        from integrations.channels.media import tts_router
        monkeypatch.setattr('core.venv_paths.venv_python_if_exists',
                            lambda backend: None)
        assert tts_router._is_engine_installed('f5_tts') is False
        assert tts_router._is_engine_installed('xtts_v2') is False


class TestBundledAndCloudEnginesAnswerForThemselves:

    def test_espeak_follows_the_binary(self, monkeypatch, absent_packages):
        tts_router = absent_packages
        monkeypatch.setattr(tts_router.shutil, 'which',
                            lambda name: None)
        assert tts_router._is_engine_installed('espeak') is False

        tts_router._engine_available_cache.clear()
        monkeypatch.setattr(
            tts_router.shutil, 'which',
            lambda name: '/usr/bin/espeak-ng' if name == 'espeak-ng' else None)
        assert tts_router._is_engine_installed('espeak') is True

    def test_makeittalk_follows_its_endpoint(self, monkeypatch,
                                             absent_packages):
        """A cloud engine is configured, not installed.  Its branch sat
        below an early `if not spec.tool_module: return False` and could
        never run, so a configured endpoint was never offered."""
        tts_router = absent_packages
        assert tts_router.ENGINE_REGISTRY['makeittalk'].tool_module is None
        monkeypatch.delenv('MAKEITTALK_API_URL', raising=False)
        assert tts_router._is_engine_installed('makeittalk') is False

        tts_router._engine_available_cache.clear()
        monkeypatch.setenv('MAKEITTALK_API_URL', 'https://example.invalid')
        assert tts_router._is_engine_installed('makeittalk') is True


class TestSelectionOffersNothingItCannotRun:

    @pytest.fixture
    def router(self):
        from integrations.channels.media.tts_router import TTSRouter
        return TTSRouter()

    def test_absent_espeak_is_not_appended_as_a_fallback(self, monkeypatch,
                                                         router):
        """The ladder ended in an unconditional espeak rung, which is right
        on the shipped OS (espeak is bundled) and a fabrication on a desktop
        without it: selection returned a candidate it had just been told was
        missing, and the turn spent its time failing on it."""
        from integrations.channels.media import tts_router
        monkeypatch.setattr(tts_router, '_get_gpu_info',
                            lambda: {'cuda_available': False})
        monkeypatch.setattr(tts_router, '_get_compute_policy',
                            lambda: {'compute_policy': 'local_only'})
        monkeypatch.setattr(tts_router, '_is_engine_installed',
                            lambda engine_id: False)
        candidates = router.select_engines('Hello', language='en')
        assert [c.engine.engine_id for c in candidates] == []

    def test_present_espeak_is_still_the_last_rung(self, monkeypatch, router):
        from integrations.channels.media import tts_router
        monkeypatch.setattr(tts_router, '_get_gpu_info',
                            lambda: {'cuda_available': False})
        monkeypatch.setattr(tts_router, '_get_compute_policy',
                            lambda: {'compute_policy': 'local_only'})
        monkeypatch.setattr(tts_router, '_is_engine_installed',
                            lambda engine_id: engine_id == 'espeak')
        candidates = router.select_engines('Hello', language='en')
        assert candidates[-1].engine.engine_id == 'espeak'


class TestTheSetupOfferNamesTheEngineThatWouldFixThisMachine:

    def test_absent_cpu_cloner_is_offered(self, monkeypatch, absent_packages):
        """pocket_tts clones voices, runs on CPU, and is the rung a machine
        with no free VRAM actually lands on.  While it read installed it was
        skipped here, so the owner was never asked about the one engine that
        would have made the voice work."""
        tts_router = absent_packages
        monkeypatch.setattr(tts_router, '_get_gpu_info',
                            lambda: {'cuda_available': False})
        missing = tts_router.clone_engines_not_installed('en')
        assert 'pocket_tts' in missing

    def test_an_installed_cloner_is_not_offered(self, monkeypatch,
                                                present_packages):
        from integrations.channels.media import tts_router
        monkeypatch.setattr(tts_router, '_get_gpu_info',
                            lambda: {'cuda_available': False})
        monkeypatch.setattr('core.venv_paths.venv_python_if_exists',
                            lambda backend: '/venv/' + backend + '/python')
        monkeypatch.setattr('core.venv_paths.venv_site_packages',
                            lambda backend: '/venv/' + backend + '/site')
        assert tts_router.clone_engines_not_installed('en') == []
