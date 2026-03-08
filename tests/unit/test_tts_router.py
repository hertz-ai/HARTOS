"""Tests for the Smart TTS Router — engine selection, synthesis, fallback."""
import json
import os
import sys
import time
from unittest.mock import MagicMock, patch, PropertyMock

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))


# ═══════════════════════════════════════════════════════════════
# Language Detection
# ═══════════════════════════════════════════════════════════════

class TestLanguageDetection:
    """Test detect_language() heuristics."""

    def test_english_default(self):
        from integrations.channels.media.tts_router import detect_language
        assert detect_language("Hello world") == 'en'

    def test_empty_returns_english(self):
        from integrations.channels.media.tts_router import detect_language
        assert detect_language("") == 'en'
        assert detect_language(None) == 'en'

    def test_hindi_devanagari(self):
        from integrations.channels.media.tts_router import detect_language
        assert detect_language("नमस्ते दुनिया यह एक परीक्षण है") == 'hi'

    def test_chinese_cjk(self):
        from integrations.channels.media.tts_router import detect_language
        assert detect_language("你好世界这是一个测试用的文字") == 'zh'

    def test_korean_hangul(self):
        from integrations.channels.media.tts_router import detect_language
        assert detect_language("안녕하세요 세계 이것은 테스트입니다") == 'ko'

    def test_japanese_hiragana(self):
        from integrations.channels.media.tts_router import detect_language
        assert detect_language("こんにちは世界これはてすとです") == 'ja'

    def test_arabic(self):
        from integrations.channels.media.tts_router import detect_language
        assert detect_language("مرحبا بالعالم هذا اختبار نصي") == 'ar'

    def test_russian_cyrillic(self):
        from integrations.channels.media.tts_router import detect_language
        assert detect_language("Привет мир это тестовый текст") == 'ru'

    def test_tamil(self):
        from integrations.channels.media.tts_router import detect_language
        assert detect_language("வணக்கம் உலகம் இது ஒரு சோதனை") == 'ta'

    def test_telugu(self):
        from integrations.channels.media.tts_router import detect_language
        assert detect_language("హలో ప్రపంచం ఇది ఒక పరీక్ష వచనం") == 'te'

    def test_bengali(self):
        from integrations.channels.media.tts_router import detect_language
        assert detect_language("হ্যালো বিশ্ব এটি একটি পরীক্ষামূলক") == 'bn'


# ═══════════════════════════════════════════════════════════════
# Engine Selection
# ═══════════════════════════════════════════════════════════════

class TestEngineSelection:
    """Test TTSRouter.select_engines() — correct candidates per language."""

    @pytest.fixture
    def router(self):
        from integrations.channels.media.tts_router import TTSRouter
        return TTSRouter()

    @patch('integrations.channels.media.tts_router._get_gpu_info',
           return_value={'cuda_available': False})
    @patch('integrations.channels.media.tts_router._get_compute_policy',
           return_value={'compute_policy': 'local_only'})
    @patch('integrations.channels.media.tts_router._is_engine_installed',
           return_value=True)
    def test_english_no_gpu_local_only(self, mock_inst, mock_pol, mock_gpu, router):
        candidates = router.select_engines("Hello", language='en')
        engine_ids = [c.engine.engine_id for c in candidates]
        # GPU-only engines excluded, CPU engines present
        assert 'pocket_tts' in engine_ids
        assert 'espeak' in engine_ids
        # No GPU-only engines when no GPU
        assert 'chatterbox_turbo' not in engine_ids

    @patch('integrations.channels.media.tts_router._get_gpu_info',
           return_value={'cuda_available': True, 'free_gb': 8.0})
    @patch('integrations.channels.media.tts_router._can_fit_on_gpu', return_value=True)
    @patch('integrations.channels.media.tts_router._get_compute_policy',
           return_value={'compute_policy': 'local_preferred'})
    @patch('integrations.channels.media.tts_router._is_engine_installed',
           return_value=True)
    def test_english_with_gpu(self, mock_inst, mock_pol, mock_fit, mock_gpu, router):
        candidates = router.select_engines("Hello", language='en')
        engine_ids = [c.engine.engine_id for c in candidates]
        assert 'chatterbox_turbo' in engine_ids
        assert 'luxtts' in engine_ids

    @patch('integrations.channels.media.tts_router._get_gpu_info',
           return_value={'cuda_available': False})
    @patch('integrations.channels.media.tts_router._get_compute_policy',
           return_value={'compute_policy': 'local_only'})
    @patch('integrations.channels.media.tts_router._is_engine_installed',
           return_value=True)
    def test_hindi_no_gpu(self, mock_inst, mock_pol, mock_gpu, router):
        candidates = router.select_engines("नमस्ते", language='hi')
        engine_ids = [c.engine.engine_id for c in candidates]
        # Only espeak can serve Hindi on CPU
        assert 'espeak' in engine_ids
        # GPU-only Hindi engines excluded
        assert 'indic_parler' not in engine_ids

    def test_always_has_espeak_fallback(self, router):
        with patch('integrations.channels.media.tts_router._get_gpu_info',
                   return_value={'cuda_available': False}), \
             patch('integrations.channels.media.tts_router._get_compute_policy',
                   return_value={'compute_policy': 'local_only'}), \
             patch('integrations.channels.media.tts_router._is_engine_installed',
                   return_value=False):
            candidates = router.select_engines("Hello", language='en')
            engine_ids = [c.engine.engine_id for c in candidates]
            assert 'espeak' in engine_ids


# ═══════════════════════════════════════════════════════════════
# GPU / VRAM Constraints
# ═══════════════════════════════════════════════════════════════

class TestGPUConstraints:
    """Test GPU availability filtering."""

    @pytest.fixture
    def router(self):
        from integrations.channels.media.tts_router import TTSRouter
        return TTSRouter()

    @patch('integrations.channels.media.tts_router._get_gpu_info',
           return_value={'cuda_available': True, 'free_gb': 2.0})
    @patch('integrations.channels.media.tts_router._can_fit_on_gpu')
    @patch('integrations.channels.media.tts_router._get_compute_policy',
           return_value={'compute_policy': 'local_only'})
    @patch('integrations.channels.media.tts_router._is_engine_installed',
           return_value=True)
    def test_large_model_excluded_when_vram_low(self, mock_inst, mock_pol, mock_fit, mock_gpu, router):
        # Only small models fit
        def fit_check(eid):
            return eid in ('tts_f5', 'tts_indic_parler')
        mock_fit.side_effect = fit_check

        candidates = router.select_engines("Hello", language='en')
        engine_ids = [c.engine.engine_id for c in candidates]
        # chatterbox_turbo needs 3.8GB, shouldn't fit in 2GB
        assert 'chatterbox_turbo' not in [c.engine.engine_id for c in candidates
                                           if c.location.value == 'local' and c.device == 'gpu']


# ═══════════════════════════════════════════════════════════════
# Compute Policy
# ═══════════════════════════════════════════════════════════════

class TestComputePolicy:
    """Test compute_policy filtering (local_only blocks hive/cloud)."""

    @pytest.fixture
    def router(self):
        from integrations.channels.media.tts_router import TTSRouter
        return TTSRouter()

    @patch('integrations.channels.media.tts_router._get_gpu_info',
           return_value={'cuda_available': False})
    @patch('integrations.channels.media.tts_router._get_compute_policy',
           return_value={'compute_policy': 'local_only'})
    @patch('integrations.channels.media.tts_router._is_engine_installed',
           return_value=True)
    def test_local_only_no_cloud(self, mock_inst, mock_pol, mock_gpu, router):
        candidates = router.select_engines("Hello", language='en')
        locations = [c.location.value for c in candidates]
        assert 'cloud' not in locations
        assert 'hive_peer' not in locations


# ═══════════════════════════════════════════════════════════════
# Hive Peer Offload
# ═══════════════════════════════════════════════════════════════

class TestHiveOffload:

    @pytest.fixture
    def router(self):
        from integrations.channels.media.tts_router import TTSRouter
        return TTSRouter()

    @patch('integrations.channels.media.tts_router._get_gpu_info',
           return_value={'cuda_available': False})
    @patch('integrations.channels.media.tts_router._get_compute_policy',
           return_value={'compute_policy': 'any'})
    @patch('integrations.channels.media.tts_router._is_engine_installed',
           return_value=True)
    @patch('integrations.channels.media.tts_router._find_hive_peer_for_tts')
    def test_hive_peer_for_gpu_engine(self, mock_peer, mock_inst, mock_pol, mock_gpu, router):
        mock_peer.return_value = {
            'peer_id': 'node-abc',
            'address': '192.168.1.100',
            'latency_ms': 100,
            'gpu': 'RTX 4090',
        }
        candidates = router.select_engines("Hello", language='en')
        hive_candidates = [c for c in candidates if c.location.value == 'hive_peer']
        assert len(hive_candidates) > 0


# ═══════════════════════════════════════════════════════════════
# Urgency / Source Mapping
# ═══════════════════════════════════════════════════════════════

class TestUrgency:
    """Test urgency sorting and SOURCE_URGENCY mapping."""

    @pytest.fixture
    def router(self):
        from integrations.channels.media.tts_router import TTSRouter
        return TTSRouter()

    def test_source_urgency_mapping(self):
        from integrations.channels.media.tts_router import SOURCE_URGENCY
        assert SOURCE_URGENCY['greeting'] == 'instant'
        assert SOURCE_URGENCY['read_aloud'] == 'quality'
        assert SOURCE_URGENCY['chat_response'] == 'normal'

    @patch('integrations.channels.media.tts_router._get_gpu_info',
           return_value={'cuda_available': False})
    @patch('integrations.channels.media.tts_router._get_compute_policy',
           return_value={'compute_policy': 'local_only'})
    @patch('integrations.channels.media.tts_router._is_engine_installed',
           return_value=True)
    def test_instant_sorts_by_latency(self, mock_inst, mock_pol, mock_gpu, router):
        candidates = router.select_engines("Hello", language='en', urgency='instant')
        if len(candidates) >= 2:
            assert candidates[0].estimated_latency_ms <= candidates[1].estimated_latency_ms

    @patch('integrations.channels.media.tts_router._get_gpu_info',
           return_value={'cuda_available': False})
    @patch('integrations.channels.media.tts_router._get_compute_policy',
           return_value={'compute_policy': 'local_only'})
    @patch('integrations.channels.media.tts_router._is_engine_installed',
           return_value=True)
    def test_quality_sorts_by_quality(self, mock_inst, mock_pol, mock_gpu, router):
        candidates = router.select_engines("Hello", language='en', urgency='quality')
        if len(candidates) >= 2:
            assert candidates[0].quality_score >= candidates[1].quality_score


# ═══════════════════════════════════════════════════════════════
# Voice Clone Filter
# ═══════════════════════════════════════════════════════════════

class TestVoiceClone:

    @pytest.fixture
    def router(self):
        from integrations.channels.media.tts_router import TTSRouter
        return TTSRouter()

    @patch('integrations.channels.media.tts_router._get_gpu_info',
           return_value={'cuda_available': True, 'free_gb': 16.0})
    @patch('integrations.channels.media.tts_router._can_fit_on_gpu', return_value=True)
    @patch('integrations.channels.media.tts_router._get_compute_policy',
           return_value={'compute_policy': 'local_preferred'})
    @patch('integrations.channels.media.tts_router._is_engine_installed',
           return_value=True)
    def test_require_clone_filters_non_clone(self, mock_inst, mock_pol, mock_fit, mock_gpu, router):
        candidates = router.select_engines(
            "Hello", language='en', require_clone=True,
        )
        for c in candidates:
            if c.engine.engine_id != 'espeak':  # espeak is always added as fallback
                assert c.engine.voice_clone is True


# ═══════════════════════════════════════════════════════════════
# Fallback Chain
# ═══════════════════════════════════════════════════════════════

class TestFallbackChain:

    @pytest.fixture
    def router(self):
        from integrations.channels.media.tts_router import TTSRouter
        return TTSRouter()

    @patch('integrations.channels.media.tts_router._get_gpu_info',
           return_value={'cuda_available': False})
    @patch('integrations.channels.media.tts_router._get_compute_policy',
           return_value={'compute_policy': 'local_only'})
    @patch('integrations.channels.media.tts_router._is_engine_installed',
           return_value=False)
    def test_all_engines_unavailable_still_has_espeak(self, mock_inst, mock_pol, mock_gpu, router):
        candidates = router.select_engines("Hello", language='en')
        assert len(candidates) >= 1
        assert candidates[-1].engine.engine_id == 'espeak'


# ═══════════════════════════════════════════════════════════════
# Synthesize
# ═══════════════════════════════════════════════════════════════

class TestSynthesize:

    @pytest.fixture
    def router(self):
        from integrations.channels.media.tts_router import TTSRouter
        return TTSRouter()

    def test_empty_text_returns_error(self, router):
        result = router.synthesize("")
        assert result.error == 'Text is required'

    def test_whitespace_returns_error(self, router):
        result = router.synthesize("   ")
        assert result.error == 'Text is required'

    @patch('integrations.channels.media.tts_router._get_gpu_info',
           return_value={'cuda_available': False})
    @patch('integrations.channels.media.tts_router._get_compute_policy',
           return_value={'compute_policy': 'local_only'})
    @patch('integrations.channels.media.tts_router._is_engine_installed',
           return_value=True)
    def test_synthesize_source_maps_urgency(self, mock_inst, mock_pol, mock_gpu, router):
        """source='greeting' should be treated as urgency='instant'."""
        with patch.object(router, '_execute') as mock_exec:
            mock_exec.return_value = {
                'path': '/tmp/test.wav', 'duration': 1.0,
                'sample_rate': 24000, 'voice': 'default',
            }
            result = router.synthesize("Hello", source='greeting')
            assert result.error is None

    @patch('integrations.channels.media.tts_router._get_gpu_info',
           return_value={'cuda_available': False})
    @patch('integrations.channels.media.tts_router._get_compute_policy',
           return_value={'compute_policy': 'local_only'})
    @patch('integrations.channels.media.tts_router._is_engine_installed',
           return_value=True)
    def test_engine_override(self, mock_inst, mock_pol, mock_gpu, router):
        """engine_override bypasses selection."""
        with patch.object(router, '_execute') as mock_exec:
            mock_exec.return_value = {
                'path': '/tmp/test.wav', 'duration': 1.0,
                'sample_rate': 24000, 'voice': 'alba',
            }
            result = router.synthesize("Hello", engine_override='pocket_tts')
            assert result.engine_id == 'pocket_tts'

    @patch('integrations.channels.media.tts_router._get_gpu_info',
           return_value={'cuda_available': False})
    @patch('integrations.channels.media.tts_router._get_compute_policy',
           return_value={'compute_policy': 'local_only'})
    @patch('integrations.channels.media.tts_router._is_engine_installed',
           return_value=True)
    def test_all_engines_fail_returns_error(self, mock_inst, mock_pol, mock_gpu, router):
        with patch.object(router, '_execute',
                          return_value={'error': 'mock failure'}):
            result = router.synthesize("Hello")
            assert result.error == 'All TTS engines failed'
            assert len(result.warnings) > 0


# ═══════════════════════════════════════════════════════════════
# Engine Status
# ═══════════════════════════════════════════════════════════════

class TestEngineStatus:

    @pytest.fixture
    def router(self):
        from integrations.channels.media.tts_router import TTSRouter
        return TTSRouter()

    @patch('integrations.channels.media.tts_router._get_gpu_info',
           return_value={'cuda_available': False})
    @patch('integrations.channels.media.tts_router._is_engine_installed',
           return_value=False)
    def test_reports_all_engines(self, mock_inst, mock_gpu, router):
        statuses = router.get_engine_status()
        engine_ids = [s['engine'] for s in statuses]
        assert 'luxtts' in engine_ids
        assert 'pocket_tts' in engine_ids
        assert 'espeak' in engine_ids
        assert 'chatterbox_turbo' in engine_ids
        assert 'cosyvoice3' in engine_ids

    @patch('integrations.channels.media.tts_router._get_gpu_info',
           return_value={'cuda_available': False})
    @patch('integrations.channels.media.tts_router._is_engine_installed',
           return_value=False)
    def test_status_has_required_fields(self, mock_inst, mock_gpu, router):
        statuses = router.get_engine_status()
        for s in statuses:
            assert 'engine' in s
            assert 'installed' in s
            assert 'can_run' in s
            assert 'device' in s
            assert 'languages' in s
            assert 'quality' in s


# ═══════════════════════════════════════════════════════════════
# Get All Voices
# ═══════════════════════════════════════════════════════════════

class TestGetAllVoices:

    @pytest.fixture
    def router(self):
        from integrations.channels.media.tts_router import TTSRouter
        return TTSRouter()

    def test_aggregates_from_engines(self, router):
        with patch('integrations.service_tools.pocket_tts_tool._BUILTIN_VOICES',
                   ['alba', 'jean']), \
             patch('integrations.service_tools.luxtts_tool.luxtts_list_voices',
                   return_value='{"voices": [{"id": "alice"}], "count": 1}'):
            voices = router.get_all_voices()
            engines = {v['engine'] for v in voices}
            assert 'pocket_tts' in engines
            assert 'luxtts' in engines

    def test_empty_when_no_engines(self, router):
        with patch.dict('sys.modules', {
            'integrations.service_tools.pocket_tts_tool': None,
            'integrations.service_tools.luxtts_tool': None,
        }):
            # get_all_voices handles ImportError gracefully
            voices = router.get_all_voices()
            assert isinstance(voices, list)


# ═══════════════════════════════════════════════════════════════
# Singleton
# ═══════════════════════════════════════════════════════════════

class TestSingleton:

    def test_singleton_returns_same_instance(self):
        from integrations.channels.media import tts_router as mod
        old = mod._router_instance
        mod._router_instance = None
        try:
            r1 = mod.get_tts_router()
            r2 = mod.get_tts_router()
            assert r1 is r2
        finally:
            mod._router_instance = old


# ═══════════════════════════════════════════════════════════════
# TTSResult
# ═══════════════════════════════════════════════════════════════

class TestTTSResult:

    def test_to_dict_minimal(self):
        from integrations.channels.media.tts_router import TTSResult
        r = TTSResult(
            path='/tmp/test.wav', duration=1.5, engine_id='pocket_tts',
            device='cpu', location='local', latency_ms=200.0,
            sample_rate=24000, voice='alba', quality_score=0.85,
        )
        d = r.to_dict()
        assert d['engine'] == 'pocket_tts'
        assert d['path'] == '/tmp/test.wav'
        assert 'warnings' not in d

    def test_to_dict_with_warnings(self):
        from integrations.channels.media.tts_router import TTSResult
        r = TTSResult(
            path='/tmp/test.wav', duration=1.5, engine_id='luxtts',
            device='cpu', location='local', latency_ms=800.0,
            sample_rate=24000, voice='default', quality_score=0.93,
            warnings=['Running on CPU'],
        )
        d = r.to_dict()
        assert 'warnings' in d
        assert 'Running on CPU' in d['warnings']

    def test_to_dict_with_error(self):
        from integrations.channels.media.tts_router import TTSResult
        r = TTSResult(
            path='', duration=0, engine_id='none', device='none',
            location='none', latency_ms=0, sample_rate=0, voice='',
            quality_score=0, error='All TTS engines failed',
        )
        d = r.to_dict()
        assert d['error'] == 'All TTS engines failed'


# ═══════════════════════════════════════════════════════════════
# ENGINE_REGISTRY Integrity
# ═══════════════════════════════════════════════════════════════

class TestEngineRegistry:

    def test_all_engines_present(self):
        from integrations.channels.media.tts_router import ENGINE_REGISTRY
        expected = {
            'chatterbox_turbo', 'luxtts', 'cosyvoice3', 'f5_tts',
            'indic_parler', 'chatterbox_ml', 'pocket_tts', 'espeak', 'makeittalk',
        }
        assert set(ENGINE_REGISTRY.keys()) == expected

    def test_all_specs_have_required_fields(self):
        from integrations.channels.media.tts_router import ENGINE_REGISTRY
        for eid, spec in ENGINE_REGISTRY.items():
            assert spec.engine_id == eid
            assert isinstance(spec.languages, tuple)
            assert 0 <= spec.quality <= 1.0
            assert isinstance(spec.voice_clone, bool)

    def test_lang_preference_covers_common_languages(self):
        from integrations.channels.media.tts_router import LANG_ENGINE_PREFERENCE
        for lang in ['en', 'hi', 'zh', 'ja', 'ko', 'de', 'es', 'fr', 'ar', 'ru']:
            assert lang in LANG_ENGINE_PREFERENCE
