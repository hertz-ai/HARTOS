"""
Unit tests for the 4 subsystem catalog populators.

Each test uses a fresh ModelCatalog instance backed by a temp file so
there is zero interaction with the real on-disk catalog or any singleton.

Populators under test:
  - populate_tts_catalog    (integrations.channels.media.tts_router)
  - populate_stt_catalog    (integrations.service_tools.whisper_tool)
  - populate_vlm_catalog    (integrations.vision.lightweight_backend)
  - populate_videogen_catalog (integrations.service_tools.media_agent)
"""

import os
import tempfile
import pytest

from integrations.service_tools.model_catalog import ModelCatalog, ModelType


# ─── Helpers ────────────────────────────────────────────────────────────────

def fresh_catalog() -> ModelCatalog:
    """Return a ModelCatalog that reads/writes to a throw-away temp file.

    The file does not exist yet, so _load() is a no-op and the catalog
    starts completely empty.
    """
    tmp = tempfile.NamedTemporaryFile(suffix='.json', delete=False)
    tmp.close()
    os.unlink(tmp.name)          # remove so the catalog sees "no file"
    return ModelCatalog(catalog_path=tmp.name)


# ═══════════════════════════════════════════════════════════════════════════
# TTS — populate_tts_catalog
# ═══════════════════════════════════════════════════════════════════════════

class TestPopulateTtsCatalog:
    """Tests for populate_tts_catalog from integrations.channels.media.tts_router."""

    # The ENGINE_REGISTRY currently has exactly 9 engines:
    #   chatterbox_turbo, luxtts, cosyvoice3, f5_tts, indic_parler,
    #   chatterbox_ml, pocket_tts, espeak, makeittalk
    EXPECTED_COUNT = 9

    # Every catalog ID produced by populate_tts_catalog
    # (engine_id.replace("_", "-") prefixed with "tts-")
    EXPECTED_IDS = {
        'tts-chatterbox-turbo',
        'tts-luxtts',
        'tts-cosyvoice3',
        'tts-f5-tts',
        'tts-indic-parler',
        'tts-chatterbox-ml',
        'tts-pocket-tts',
        'tts-espeak',
        'tts-makeittalk',
    }

    # These IDs must also appear as keys in ModelOrchestrator._CATALOG_TO_VRAM_KEY
    # (the subset that have GPU VRAM > 0)
    GPU_IDS_IN_VRAM_KEY = {
        'tts-chatterbox-turbo',
        'tts-f5-tts',
        'tts-indic-parler',
        'tts-cosyvoice3',
        'tts-chatterbox-ml',
    }

    @pytest.fixture
    def catalog(self):
        return fresh_catalog()

    @pytest.fixture
    def populated(self, catalog):
        from integrations.channels.media.tts_router import populate_tts_catalog
        populate_tts_catalog(catalog)
        return catalog

    # ── entry count ─────────────────────────────────────────────────────────

    def test_correct_number_of_entries(self, catalog):
        from integrations.channels.media.tts_router import populate_tts_catalog
        added = populate_tts_catalog(catalog)
        assert added == self.EXPECTED_COUNT

    def test_all_expected_ids_registered(self, populated):
        registered = {e.id for e in populated.list_all()}
        assert self.EXPECTED_IDS.issubset(registered), (
            f"Missing IDs: {self.EXPECTED_IDS - registered}"
        )

    # ── IDs use hyphens not underscores ─────────────────────────────────────

    def test_ids_use_hyphens_not_underscores(self, populated):
        tts_entries = populated.list_by_type('tts')
        bad = [e.id for e in tts_entries if '_' in e.id.lstrip('tts-')]
        assert bad == [], (
            f"TTS catalog IDs must not contain underscores: {bad}"
        )

    def test_known_hyphen_ids_exist(self, populated):
        """Spot-check the most important IDs exist with hyphens."""
        for expected_id in ('tts-chatterbox-turbo', 'tts-f5-tts',
                            'tts-indic-parler', 'tts-chatterbox-ml'):
            assert populated.get(expected_id) is not None, (
                f"Expected catalog entry '{expected_id}' not found"
            )

    # ── IDs match _CATALOG_TO_VRAM_KEY ──────────────────────────────────────

    def test_gpu_ids_match_catalog_to_vram_key(self, populated):
        from integrations.service_tools.model_orchestrator import ModelOrchestrator
        vram_key_map = ModelOrchestrator._CATALOG_TO_VRAM_KEY
        for mid in self.GPU_IDS_IN_VRAM_KEY:
            assert mid in vram_key_map, (
                f"GPU TTS entry '{mid}' missing from _CATALOG_TO_VRAM_KEY"
            )

    # ── idempotency ──────────────────────────────────────────────────────────

    def test_idempotent_second_call_adds_zero(self, catalog):
        from integrations.channels.media.tts_router import populate_tts_catalog
        first = populate_tts_catalog(catalog)
        second = populate_tts_catalog(catalog)
        assert first == self.EXPECTED_COUNT
        assert second == 0, (
            "Second populate_tts_catalog call should add 0 (already registered)"
        )

    def test_idempotent_total_count_unchanged(self, catalog):
        from integrations.channels.media.tts_router import populate_tts_catalog
        populate_tts_catalog(catalog)
        count_after_first = len(catalog.list_all())
        populate_tts_catalog(catalog)
        count_after_second = len(catalog.list_all())
        assert count_after_first == count_after_second

    # ── ModelType.TTS ────────────────────────────────────────────────────────

    def test_all_entries_have_tts_type(self, populated):
        tts_entries = populated.list_by_type('tts')
        assert len(tts_entries) == self.EXPECTED_COUNT
        for entry in tts_entries:
            assert entry.model_type == ModelType.TTS, (
                f"Entry {entry.id} has model_type={entry.model_type!r}, expected TTS"
            )

    # ── language_priority populated ──────────────────────────────────────────

    def test_english_preference_in_language_priority(self, populated):
        """chatterbox_turbo, luxtts, pocket_tts should all have 'en' in language_priority."""
        english_engines = ['tts-chatterbox-turbo', 'tts-luxtts', 'tts-pocket-tts']
        for mid in english_engines:
            entry = populated.get(mid)
            assert entry is not None
            assert 'en' in entry.language_priority, (
                f"{mid} should have 'en' in language_priority"
            )

    def test_hindi_preference_in_language_priority(self, populated):
        """indic_parler and chatterbox_ml should have 'hi' in language_priority."""
        hindi_engines = ['tts-indic-parler', 'tts-chatterbox-ml']
        for mid in hindi_engines:
            entry = populated.get(mid)
            assert entry is not None
            assert 'hi' in entry.language_priority, (
                f"{mid} should have 'hi' in language_priority"
            )

    def test_chatterbox_turbo_en_is_top_priority(self, populated):
        """chatterbox_turbo is ranked 0 (first) in the English preference list."""
        entry = populated.get('tts-chatterbox-turbo')
        assert entry is not None
        # rank 0 in LANG_ENGINE_PREFERENCE['en'] → language_priority['en'] == 0
        assert entry.language_priority.get('en') == 0


# ═══════════════════════════════════════════════════════════════════════════
# STT — populate_stt_catalog
# ═══════════════════════════════════════════════════════════════════════════

class TestPopulateSttCatalog:
    """Tests for populate_stt_catalog from integrations.service_tools.whisper_tool."""

    # 5 faster-whisper + 6 sherpa-onnx = 11 total
    EXPECTED_COUNT = 11

    EXPECTED_IDS = {
        # faster-whisper (primary engine, CTranslate2)
        'stt-faster-whisper-tiny',
        'stt-faster-whisper-base',
        'stt-faster-whisper-small',
        'stt-faster-whisper-medium',
        'stt-faster-whisper-large',
        # sherpa-onnx (lightweight ONNX)
        'stt-sherpa-moonshine-tiny',
        'stt-sherpa-moonshine-base',
        'stt-sherpa-whisper-tiny',
        'stt-sherpa-whisper-base',
        'stt-sherpa-whisper-small',
        'stt-sherpa-whisper-medium',
    }

    @pytest.fixture
    def catalog(self):
        return fresh_catalog()

    @pytest.fixture
    def populated(self, catalog):
        from integrations.service_tools.whisper_tool import populate_stt_catalog
        populate_stt_catalog(catalog)
        return catalog

    # ── entry count ─────────────────────────────────────────────────────────

    def test_correct_number_of_entries(self, catalog):
        from integrations.service_tools.whisper_tool import populate_stt_catalog
        added = populate_stt_catalog(catalog)
        assert added == self.EXPECTED_COUNT

    def test_all_expected_ids_registered(self, populated):
        registered = {e.id for e in populated.list_all()}
        assert self.EXPECTED_IDS.issubset(registered), (
            f"Missing IDs: {self.EXPECTED_IDS - registered}"
        )

    # ── specific IDs ────────────────────────────────────────────────────────

    def test_faster_whisper_base_exists(self, populated):
        assert populated.get('stt-faster-whisper-base') is not None

    def test_sherpa_moonshine_tiny_exists(self, populated):
        assert populated.get('stt-sherpa-moonshine-tiny') is not None

    def test_sherpa_whisper_medium_exists(self, populated):
        assert populated.get('stt-sherpa-whisper-medium') is not None

    # ── idempotency ──────────────────────────────────────────────────────────

    def test_idempotent_second_call_adds_zero(self, catalog):
        from integrations.service_tools.whisper_tool import populate_stt_catalog
        first = populate_stt_catalog(catalog)
        second = populate_stt_catalog(catalog)
        assert first == self.EXPECTED_COUNT
        assert second == 0

    def test_idempotent_total_count_unchanged(self, catalog):
        from integrations.service_tools.whisper_tool import populate_stt_catalog
        populate_stt_catalog(catalog)
        before = len(catalog.list_all())
        populate_stt_catalog(catalog)
        after = len(catalog.list_all())
        assert before == after

    # ── ModelType.STT ────────────────────────────────────────────────────────

    def test_all_entries_have_stt_type(self, populated):
        stt_entries = populated.list_by_type('stt')
        assert len(stt_entries) == self.EXPECTED_COUNT
        for entry in stt_entries:
            assert entry.model_type == ModelType.STT, (
                f"Entry {entry.id} has model_type={entry.model_type!r}, expected STT"
            )

    # ── sherpa-onnx entries are CPU-only (vram_gb == 0) ─────────────────────

    def test_sherpa_entries_have_zero_vram(self, populated):
        sherpa_ids = [mid for mid in self.EXPECTED_IDS if 'sherpa' in mid]
        for mid in sherpa_ids:
            entry = populated.get(mid)
            assert entry is not None
            assert entry.vram_gb == 0.0, (
                f"{mid} should have vram_gb=0.0 (CPU-only ONNX)"
            )

    def test_faster_whisper_large_has_highest_vram(self, populated):
        """faster-whisper-large has 3.0 GB — the most VRAM of all STT models."""
        entry = populated.get('stt-faster-whisper-large')
        assert entry is not None
        assert entry.vram_gb == 3.0

    def test_faster_whisper_tiny_has_low_vram(self, populated):
        """faster-whisper-tiny has 0.0 GB (runs on CPU)."""
        entry = populated.get('stt-faster-whisper-tiny')
        assert entry is not None
        assert entry.vram_gb == 0.0


# ═══════════════════════════════════════════════════════════════════════════
# VLM — populate_vlm_catalog
# ═══════════════════════════════════════════════════════════════════════════

class TestPopulateVlmCatalog:
    """Tests for populate_vlm_catalog from integrations.vision.lightweight_backend."""

    # qwen3vl, qwen08b (caption), minicpm-v2, mobilevlm, clip = 5 entries
    EXPECTED_COUNT = 5

    EXPECTED_IDS = {
        'vlm-qwen3vl',
        'vlm-qwen08b',
        'vlm-minicpm-v2',
        'vlm-mobilevlm',
        'vlm-clip',
    }

    @pytest.fixture
    def catalog(self):
        return fresh_catalog()

    @pytest.fixture
    def populated(self, catalog):
        from integrations.vision.lightweight_backend import populate_vlm_catalog
        populate_vlm_catalog(catalog)
        return catalog

    # ── entry count ─────────────────────────────────────────────────────────

    def test_correct_number_of_entries(self, catalog):
        from integrations.vision.lightweight_backend import populate_vlm_catalog
        added = populate_vlm_catalog(catalog)
        assert added == self.EXPECTED_COUNT

    def test_all_expected_ids_registered(self, populated):
        registered = {e.id for e in populated.list_all()}
        assert self.EXPECTED_IDS.issubset(registered), (
            f"Missing IDs: {self.EXPECTED_IDS - registered}"
        )

    # ── ModelType.VLM ────────────────────────────────────────────────────────

    def test_all_entries_have_vlm_type(self, populated):
        vlm_entries = populated.list_by_type('vlm')
        assert len(vlm_entries) == self.EXPECTED_COUNT
        for entry in vlm_entries:
            assert entry.model_type == ModelType.VLM, (
                f"Entry {entry.id} has model_type={entry.model_type!r}, expected VLM"
            )

    # ── VRAM requirements ────────────────────────────────────────────────────

    def test_minicpm_has_nonzero_vram(self, populated):
        """MiniCPM-V-2 is a GPU model and requires 4 GB VRAM."""
        entry = populated.get('vlm-minicpm-v2')
        assert entry is not None
        assert entry.vram_gb > 0, (
            "vlm-minicpm-v2 should require GPU VRAM (vram_gb > 0)"
        )
        assert entry.vram_gb == 4.0

    def test_mobilevlm_has_zero_vram(self, populated):
        """MobileVLM runs on CPU via ONNX Runtime — no GPU VRAM needed."""
        entry = populated.get('vlm-mobilevlm')
        assert entry is not None
        assert entry.vram_gb == 0.0, (
            "vlm-mobilevlm is CPU-only and should have vram_gb=0.0"
        )

    def test_clip_has_zero_vram(self, populated):
        """CLIP ViT-B/16 runs on CPU — no GPU VRAM needed."""
        entry = populated.get('vlm-clip')
        assert entry is not None
        assert entry.vram_gb == 0.0

    def test_qwen3vl_has_nonzero_vram(self, populated):
        """Qwen3-VL requires 4 GB VRAM."""
        entry = populated.get('vlm-qwen3vl')
        assert entry is not None
        assert entry.vram_gb == 4.0

    # ── GPU vs CPU support flags ──────────────────────────────────────────────

    def test_minicpm_is_gpu_only(self, populated):
        entry = populated.get('vlm-minicpm-v2')
        assert entry.supports_gpu is True
        assert entry.supports_cpu is False

    def test_mobilevlm_is_cpu_only(self, populated):
        entry = populated.get('vlm-mobilevlm')
        assert entry.supports_gpu is False
        assert entry.supports_cpu is True

    def test_clip_is_cpu_only(self, populated):
        entry = populated.get('vlm-clip')
        assert entry.supports_gpu is False
        assert entry.supports_cpu is True

    # ── idempotency ──────────────────────────────────────────────────────────

    def test_idempotent_second_call_adds_zero(self, catalog):
        from integrations.vision.lightweight_backend import populate_vlm_catalog
        first = populate_vlm_catalog(catalog)
        second = populate_vlm_catalog(catalog)
        assert first == self.EXPECTED_COUNT
        assert second == 0

    def test_idempotent_total_count_unchanged(self, catalog):
        from integrations.vision.lightweight_backend import populate_vlm_catalog
        populate_vlm_catalog(catalog)
        before = len(catalog.list_all())
        populate_vlm_catalog(catalog)
        after = len(catalog.list_all())
        assert before == after


# ═══════════════════════════════════════════════════════════════════════════
# VideoGen — populate_videogen_catalog
# ═══════════════════════════════════════════════════════════════════════════

class TestPopulateVideoGenCatalog:
    """Tests for populate_videogen_catalog from integrations.service_tools.media_agent."""

    # wan2gp + ltx2 = 2 entries
    EXPECTED_COUNT = 2

    EXPECTED_IDS = {
        'video_gen-wan2gp',
        'video_gen-ltx2',
    }

    @pytest.fixture
    def catalog(self):
        return fresh_catalog()

    @pytest.fixture
    def populated(self, catalog):
        from integrations.service_tools.media_agent import populate_videogen_catalog
        populate_videogen_catalog(catalog)
        return catalog

    # ── entry count ─────────────────────────────────────────────────────────

    def test_correct_number_of_entries(self, catalog):
        from integrations.service_tools.media_agent import populate_videogen_catalog
        added = populate_videogen_catalog(catalog)
        assert added == self.EXPECTED_COUNT

    def test_all_expected_ids_registered(self, populated):
        registered = {e.id for e in populated.list_all()}
        assert self.EXPECTED_IDS.issubset(registered), (
            f"Missing IDs: {self.EXPECTED_IDS - registered}"
        )

    # ── ModelType.VIDEO_GEN ───────────────────────────────────────────────────

    def test_all_entries_have_video_gen_type(self, populated):
        vg_entries = populated.list_by_type('video_gen')
        assert len(vg_entries) == self.EXPECTED_COUNT
        for entry in vg_entries:
            assert entry.model_type == ModelType.VIDEO_GEN, (
                f"Entry {entry.id} has model_type={entry.model_type!r}, expected VIDEO_GEN"
            )

    # ── VRAM: wan2gp requires more than ltx2 ────────────────────────────────

    def test_wan2gp_requires_more_vram_than_ltx2(self, populated):
        wan2gp = populated.get('video_gen-wan2gp')
        ltx2 = populated.get('video_gen-ltx2')
        assert wan2gp is not None
        assert ltx2 is not None
        assert wan2gp.vram_gb > ltx2.vram_gb, (
            f"wan2gp ({wan2gp.vram_gb} GB) should require more VRAM than "
            f"ltx2 ({ltx2.vram_gb} GB)"
        )

    def test_wan2gp_vram_is_8gb(self, populated):
        entry = populated.get('video_gen-wan2gp')
        assert entry.vram_gb == 8.0

    def test_ltx2_vram_is_4gb(self, populated):
        entry = populated.get('video_gen-ltx2')
        assert entry.vram_gb == 4.0

    # ── CPU offload ───────────────────────────────────────────────────────────

    def test_wan2gp_does_not_support_cpu(self, populated):
        entry = populated.get('video_gen-wan2gp')
        assert entry.supports_cpu is False

    def test_ltx2_supports_cpu_offload(self, populated):
        entry = populated.get('video_gen-ltx2')
        assert entry.supports_cpu is True
        assert entry.supports_cpu_offload is True

    # ── idempotency ──────────────────────────────────────────────────────────

    def test_idempotent_second_call_adds_zero(self, catalog):
        from integrations.service_tools.media_agent import populate_videogen_catalog
        first = populate_videogen_catalog(catalog)
        second = populate_videogen_catalog(catalog)
        assert first == self.EXPECTED_COUNT
        assert second == 0

    def test_idempotent_total_count_unchanged(self, catalog):
        from integrations.service_tools.media_agent import populate_videogen_catalog
        populate_videogen_catalog(catalog)
        before = len(catalog.list_all())
        populate_videogen_catalog(catalog)
        after = len(catalog.list_all())
        assert before == after


# ═══════════════════════════════════════════════════════════════════════════
# Integration — ModelCatalog.populate_from_subsystems()
# ═══════════════════════════════════════════════════════════════════════════

class TestPopulateFromSubsystems:
    """Integration test: populate_from_subsystems() calls all 4 populators.

    Expected totals from the built-in _populate_* methods (no extra
    application-registered populators):
        TTS      :  9  (ENGINE_REGISTRY)
        STT      : 11  (5 faster-whisper + 6 sherpa-onnx)
        VLM      :  4  (qwen3vl, minicpm-v2, mobilevlm, clip)
        VideoGen :  2  (wan2gp, ltx2)
        Total    : 26
    """

    EXPECTED_TTS_COUNT = 9
    EXPECTED_STT_COUNT = 11
    EXPECTED_VLM_COUNT = 5  # +1 for qwen08b caption model
    EXPECTED_VIDEOGEN_COUNT = 2
    EXPECTED_TOTAL = EXPECTED_TTS_COUNT + EXPECTED_STT_COUNT + EXPECTED_VLM_COUNT + EXPECTED_VIDEOGEN_COUNT

    @pytest.fixture
    def populated_catalog(self):
        cat = fresh_catalog()
        cat.populate_from_subsystems()
        return cat

    def test_tts_entries_present(self, populated_catalog):
        tts = populated_catalog.list_by_type('tts')
        assert len(tts) == self.EXPECTED_TTS_COUNT, (
            f"Expected {self.EXPECTED_TTS_COUNT} TTS entries, got {len(tts)}"
        )

    def test_stt_entries_present(self, populated_catalog):
        stt = populated_catalog.list_by_type('stt')
        assert len(stt) == self.EXPECTED_STT_COUNT, (
            f"Expected {self.EXPECTED_STT_COUNT} STT entries, got {len(stt)}"
        )

    def test_vlm_entries_present(self, populated_catalog):
        vlm = populated_catalog.list_by_type('vlm')
        assert len(vlm) == self.EXPECTED_VLM_COUNT, (
            f"Expected {self.EXPECTED_VLM_COUNT} VLM entries, got {len(vlm)}"
        )

    def test_videogen_entries_present(self, populated_catalog):
        vg = populated_catalog.list_by_type('video_gen')
        assert len(vg) == self.EXPECTED_VIDEOGEN_COUNT, (
            f"Expected {self.EXPECTED_VIDEOGEN_COUNT} VideoGen entries, got {len(vg)}"
        )

    def test_total_entry_count(self, populated_catalog):
        total = len(populated_catalog.list_all())
        assert total == self.EXPECTED_TOTAL, (
            f"Expected {self.EXPECTED_TOTAL} total entries after populate_from_subsystems, "
            f"got {total}"
        )

    def test_populate_from_subsystems_idempotent(self):
        cat = fresh_catalog()
        first = cat.populate_from_subsystems()
        second = cat.populate_from_subsystems()
        assert first == self.EXPECTED_TOTAL
        assert second == 0, (
            "Second populate_from_subsystems() call should add 0 entries"
        )

    def test_no_duplicate_ids(self, populated_catalog):
        all_entries = populated_catalog.list_all()
        ids = [e.id for e in all_entries]
        assert len(ids) == len(set(ids)), (
            f"Duplicate IDs found: {[x for x in ids if ids.count(x) > 1]}"
        )

    def test_all_entries_have_valid_model_type(self, populated_catalog):
        valid_types = {mt.value for mt in ModelType}
        for entry in populated_catalog.list_all():
            assert str(entry.model_type) in valid_types, (
                f"Entry {entry.id} has unrecognised model_type: {entry.model_type!r}"
            )
