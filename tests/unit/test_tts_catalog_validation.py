"""Contract tests for #58 Scope-1: catalog-canonical TTS + ingest validation.

Pins what the ingest path must do:
  * `_validate_engine_caps` accepts the two valid shapes (tool_module
    escape hatch; full 5-field reflection contract) and rejects every
    malformed combination.
  * `populate_tts_catalog` removes invalid entries from the catalog at
    boot with a logged WARNING — fail-fast at ingest, not at synth time.
  * Reflection-only entries are explicitly rejected in Scope-1 because
    the dispatcher (#58 Scope-2) hasn't landed yet; admin sees the
    error immediately, not silence at first synth.
  * `_refresh_engine_registry_from_catalog` rebuilds ENGINE_REGISTRY in
    place after populate runs, snapshotting post-upsert catalog state.
  * Existing tool_module-shaped entries continue to round-trip through
    `_catalog_entry_to_spec` exactly as before.
"""
import logging
import sys
from pathlib import Path

import pytest

HARTOS_ROOT = Path(__file__).resolve().parents[2]
if str(HARTOS_ROOT) not in sys.path:
    sys.path.insert(0, str(HARTOS_ROOT))

from integrations.channels.media.tts_router import (  # noqa: E402
    ENGINE_REGISTRY,
    TTSEngineSpec,
    TTSDevice,
    _OUTPUT_FORMATS,
    _REFLECTION_FIELDS,
    _catalog_entry_to_spec,
    _refresh_engine_registry_from_catalog,
    _validate_engine_caps,
    populate_tts_catalog,
)
from integrations.service_tools.model_catalog import (  # noqa: E402
    ModelCatalog,
    ModelEntry,
    ModelType,
)


# ──────────────────────────────────────────────────────────────────────
# _validate_engine_caps
# ──────────────────────────────────────────────────────────────────────

class TestValidateEngineCaps:
    """Direct contract: which capability shapes are accepted vs rejected."""

    def test_tool_module_alone_is_accepted(self):
        # Every code-shipped engine in ENGINE_REGISTRY today fits this.
        # If this test breaks, the existing TTS pipeline is broken too.
        assert _validate_engine_caps({'tool_module': 'foo.bar'}) is None

    def test_empty_caps_is_rejected(self):
        err = _validate_engine_caps({})
        assert err is not None
        assert 'tool_module' in err and 'reflection' in err.lower()

    def test_non_dict_is_rejected(self):
        err = _validate_engine_caps([])  # type: ignore[arg-type]
        assert err is not None
        assert 'dict' in err

    def test_full_reflection_contract_is_accepted(self):
        caps = {
            'import_path': 'kokoro:Kokoro',
            'init_args': {'device': 'cuda'},
            'synth_method': 'create',
            'params_map': {'text': 'text', 'voice': 'voice'},
            'output_format': 'wav_bytes',
        }
        assert _validate_engine_caps(caps) is None

    @pytest.mark.parametrize('drop', list(_REFLECTION_FIELDS))
    def test_partial_reflection_contract_is_rejected(self, drop):
        caps = {
            'import_path': 'kokoro:Kokoro',
            'init_args': {},
            'synth_method': 'create',
            'params_map': {'text': 'text'},
            'output_format': 'wav_bytes',
        }
        del caps[drop]
        err = _validate_engine_caps(caps)
        assert err is not None
        assert drop in err, (
            f'error message must name the missing field {drop!r}; got: {err!r}'
        )

    def test_bad_import_path_shape_rejected(self):
        caps = {
            'import_path': 'no-colon',  # missing ':ClassName'
            'init_args': {},
            'synth_method': 'create',
            'params_map': {'text': 'text'},
            'output_format': 'wav_bytes',
        }
        err = _validate_engine_caps(caps)
        assert err is not None
        assert 'import_path' in err

    def test_unknown_output_format_rejected(self):
        caps = {
            'import_path': 'kokoro:Kokoro',
            'init_args': {},
            'synth_method': 'create',
            'params_map': {'text': 'text'},
            'output_format': 'made_up_format',
        }
        err = _validate_engine_caps(caps)
        assert err is not None
        assert 'output_format' in err

    def test_init_args_must_be_dict(self):
        caps = {
            'import_path': 'kokoro:Kokoro',
            'init_args': 'cuda',  # str, not dict
            'synth_method': 'create',
            'params_map': {'text': 'text'},
            'output_format': 'wav_bytes',
        }
        err = _validate_engine_caps(caps)
        assert err is not None
        assert 'init_args' in err

    def test_canonical_output_formats_all_accepted(self):
        # Every advertised output_format must round-trip through validation.
        for fmt in _OUTPUT_FORMATS:
            caps = {
                'import_path': 'kokoro:Kokoro',
                'init_args': {},
                'synth_method': 'create',
                'params_map': {'text': 'text'},
                'output_format': fmt,
            }
            assert _validate_engine_caps(caps) is None, (
                f'output_format={fmt!r} must be accepted'
            )


# ──────────────────────────────────────────────────────────────────────
# _catalog_entry_to_spec
# ──────────────────────────────────────────────────────────────────────

def _make_entry(eid: str, caps: dict, languages=('en',)) -> ModelEntry:
    """Minimum-viable ModelEntry for testing the converter."""
    return ModelEntry(
        id=eid,
        name=eid,
        model_type=ModelType.TTS,
        version='1.0',
        source='local',
        capabilities=caps,
        languages=list(languages),
        supports_gpu=True,
        supports_cpu=False,
        quality_score=0.9,
        enabled=True,
    )


class TestCatalogEntryToSpec:
    def test_tool_module_entry_round_trips(self):
        entry = _make_entry('tts-foo', {
            'tool_module': 'integrations.service_tools.foo_tool',
            'tool_function': 'foo_synth',
            'sample_rate': 24000,
            'voice_clone': True,
        })
        spec = _catalog_entry_to_spec(entry)
        assert isinstance(spec, TTSEngineSpec)
        assert spec.tool_module == 'integrations.service_tools.foo_tool'
        assert spec.engine_id == 'foo'  # 'tts-' prefix stripped
        assert spec.voice_clone is True

    def test_invalid_entry_returns_none(self):
        # Empty caps fails validation → None.  Caller must handle.
        entry = _make_entry('tts-bad', {})
        assert _catalog_entry_to_spec(entry) is None

    def test_reflection_only_entry_returns_none(self):
        # Validation accepts the 5-field contract, but TTSEngineSpec needs
        # tool_module — so the converter still returns None (the catalog
        # entry stays in ModelCatalog and dispatches via #58 Scope-2's
        # --catalog-id path once that lands).
        entry = _make_entry('tts-flexible', {
            'import_path': 'flex:Flex',
            'init_args': {},
            'synth_method': 'speak',
            'params_map': {'text': 'text'},
            'output_format': 'wav_bytes',
        })
        assert _catalog_entry_to_spec(entry) is None


# ──────────────────────────────────────────────────────────────────────
# populate_tts_catalog ingest validation + ENGINE_REGISTRY refresh
# ──────────────────────────────────────────────────────────────────────

@pytest.fixture
def fresh_catalog():
    """Hermetic ModelCatalog backed by a throwaway temp file path.

    Mirrors `tests/unit/test_catalog_populators.py:fresh_catalog`'s
    pattern verbatim — does NOT use tmp_path because pytest's tmp_path
    teardown trips a Python 3.12 stdlib bug (`os._walk_symlinks_as_files`
    only exists in 3.13+) on this machine.  The catalog never persists
    so cleanup is a noop.
    """
    import os as _os, tempfile as _tempfile
    tmp = _tempfile.NamedTemporaryFile(suffix='.json', delete=False)
    tmp.close()
    _os.unlink(tmp.name)  # remove so the catalog sees "no file" → empty
    return ModelCatalog(catalog_path=tmp.name)


class TestPopulateRejectsInvalidEntries:
    def test_invalid_entry_seeded_pre_boot_is_rejected_with_log(
        self, fresh_catalog, caplog,
    ):
        # Simulate an admin or hive-federated entry that landed in the
        # catalog BEFORE populate_tts_catalog runs (e.g. malformed JSON
        # written to model_catalog.json by a careless edit).
        bad = _make_entry('tts-malformed', {})  # empty caps fails validation
        fresh_catalog.register(bad, persist=False)
        assert fresh_catalog.get('tts-malformed') is not None

        with caplog.at_level(logging.WARNING,
                             logger='integrations.channels.media.tts_router'):
            populate_tts_catalog(fresh_catalog)

        # Entry must be GONE from the catalog (fail-fast at ingest).
        assert fresh_catalog.get('tts-malformed') is None, (
            'invalid entry must be unregistered at populate time, not '
            'left to fail at synth time'
        )
        # And the WARNING must be logged so the admin can find it.
        assert any('tts-malformed' in rec.message and 'reject' in rec.message
                   for rec in caplog.records), (
            f'expected WARNING about tts-malformed; got '
            f'{[r.message for r in caplog.records]!r}'
        )

    def test_reflection_only_entry_is_rejected_in_scope_1(
        self, fresh_catalog, caplog,
    ):
        # Pure-config entry passes _validate_engine_caps but the dispatcher
        # for it (--catalog-id path) hasn't landed yet (#58 Scope-2).  In
        # the meantime, we reject at ingest so the admin sees the error
        # immediately rather than silence at synth.
        ref_only = _make_entry('tts-reflection', {
            'import_path': 'flex:Flex',
            'init_args': {},
            'synth_method': 'speak',
            'params_map': {'text': 'text'},
            'output_format': 'wav_bytes',
        })
        fresh_catalog.register(ref_only, persist=False)

        with caplog.at_level(logging.WARNING,
                             logger='integrations.channels.media.tts_router'):
            populate_tts_catalog(fresh_catalog)

        assert fresh_catalog.get('tts-reflection') is None
        assert any('tts-reflection' in rec.message and
                   'reflection-only' in rec.message
                   for rec in caplog.records), (
            'reflection-only entry must be rejected with a clear log line '
            f'pointing at #58 Scope-2; got {[r.message for r in caplog.records]!r}'
        )

    def test_valid_tool_module_entry_survives_ingest(self, fresh_catalog):
        # An admin-customised entry with a valid tool_module must NOT
        # be touched by the validation pre-pass.
        good = _make_entry('tts-custom-engine', {
            'tool_module': 'integrations.service_tools.foo_tool',
            'tool_function': 'foo_synth',
            'sample_rate': 24000,
            'voice_clone': False,
        })
        fresh_catalog.register(good, persist=False)
        populate_tts_catalog(fresh_catalog)
        assert fresh_catalog.get('tts-custom-engine') is not None, (
            'valid admin entry must be preserved across populate; '
            'pre-pass should only drop invalid ones'
        )


class TestEngineRegistrySnapshot:
    def test_engine_registry_is_post_upsert_snapshot(self, fresh_catalog):
        """After populate_tts_catalog, ENGINE_REGISTRY contains exactly
        the spec-shaped catalog entries (post-upsert)."""
        populate_tts_catalog(fresh_catalog)
        # Every spec in ENGINE_REGISTRY corresponds to a 'tts-<id>' entry
        # in the catalog (the post-upsert state).
        for engine_id, spec in ENGINE_REGISTRY.items():
            cat_id = f'tts-{engine_id.replace("_", "-")}'
            assert fresh_catalog.get(cat_id) is not None, (
                f'engine {engine_id!r} in ENGINE_REGISTRY but not in catalog'
            )
            assert spec.tool_module is not None, (
                f'ENGINE_REGISTRY only stores spec-shaped entries; '
                f'{engine_id!r} has tool_module=None which means a '
                f'reflection-only entry slipped through'
            )

    def test_refresh_excludes_reflection_only_entries(self, fresh_catalog):
        """Reflection-only entries are valid in the catalog but excluded
        from the ENGINE_REGISTRY snapshot (they need #58 Scope-2's
        --catalog-id dispatch path)."""
        # Register a tool_module entry directly + a reflection-only entry.
        # Bypass populate_tts_catalog's pre-pass (which currently rejects
        # reflection-only) by calling the refresh helper directly.
        good = _make_entry('tts-real', {
            'tool_module': 'integrations.service_tools.foo_tool',
            'tool_function': 'foo_synth',
            'sample_rate': 24000,
        })
        ref_only = _make_entry('tts-flex', {
            'import_path': 'flex:Flex',
            'init_args': {},
            'synth_method': 'speak',
            'params_map': {'text': 'text'},
            'output_format': 'wav_bytes',
        })
        fresh_catalog.register(good, persist=False)
        fresh_catalog.register(ref_only, persist=False)
        n = _refresh_engine_registry_from_catalog(fresh_catalog)
        assert 'real' in ENGINE_REGISTRY, (
            'tool_module-shaped entry should be in ENGINE_REGISTRY snapshot'
        )
        assert 'flex' not in ENGINE_REGISTRY, (
            'reflection-only entry must NOT leak into ENGINE_REGISTRY '
            '(no tool_module to feed the existing dispatcher)'
        )
        assert n == 1
