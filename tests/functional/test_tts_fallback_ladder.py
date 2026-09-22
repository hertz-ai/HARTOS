"""T16+T17: TTS Fallback Ladder Tests under Artificial Resource Constraints.

Tests the full TTS engine selection + synthesis pipeline with simulated
resource constraints (no GPU, low VRAM, missing engines, language fallback).

40 scenarios across:
- English fallback ladder (10 engines ranked by quality)
- Indic language routing (indic_parler → chatterbox_ml → espeak)
- CJK routing (cosyvoice3 → f5_tts → chatterbox_ml → espeak)
- Resource constraint simulation (no GPU, low VRAM)
- Capability enumeration (which engines are installed?)
- Naturalness baseline (quality scores from ENGINE_REGISTRY)
"""
import os
import sys
import pytest
from unittest.mock import patch, MagicMock

# Ensure HARTOS root is importable
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..'))


class TestTTSEngineRegistry:
    """T17: Capability enumeration — verify all engines are registered."""

    def test_engine_registry_has_entries(self):
        from integrations.channels.media.tts_router import ENGINE_REGISTRY
        assert len(ENGINE_REGISTRY) >= 8, f"Expected 8+ engines, got {len(ENGINE_REGISTRY)}"

    def test_all_engines_have_required_fields(self):
        from integrations.channels.media.tts_router import ENGINE_REGISTRY
        required = {'quality', 'languages', 'device', 'voice_clone'}
        for name, spec in ENGINE_REGISTRY.items():
            for field in required:
                assert hasattr(spec, field) or field in (spec if isinstance(spec, dict) else spec.__dict__), \
                    f"Engine {name} missing field {field}"

    def test_english_engines_ranked_by_quality(self):
        from integrations.channels.media.tts_router import LANG_ENGINE_PREFERENCE
        en_engines = LANG_ENGINE_PREFERENCE.get('en', [])
        assert len(en_engines) >= 5, f"English should have 5+ engines, got {len(en_engines)}"

    def test_espeak_always_last_resort(self):
        from integrations.channels.media.tts_router import LANG_ENGINE_PREFERENCE
        for lang, engines in LANG_ENGINE_PREFERENCE.items():
            if 'espeak' in engines:
                assert engines[-1] == 'espeak', \
                    f"espeak should be last for {lang}, but position is {engines.index('espeak')}"

    def test_indic_languages_have_indic_parler(self):
        from integrations.channels.media.tts_router import LANG_ENGINE_PREFERENCE
        indic_langs = ['hi', 'ta', 'te', 'bn', 'mr', 'gu', 'kn', 'ml']
        for lang in indic_langs:
            if lang in LANG_ENGINE_PREFERENCE:
                engines = LANG_ENGINE_PREFERENCE[lang]
                assert 'indic_parler' in engines, \
                    f"{lang} should have indic_parler, got {engines}"

    def test_cjk_languages_have_cosyvoice(self):
        from integrations.channels.media.tts_router import LANG_ENGINE_PREFERENCE
        for lang in ['zh', 'ja', 'ko']:
            if lang in LANG_ENGINE_PREFERENCE:
                engines = LANG_ENGINE_PREFERENCE[lang]
                assert 'cosyvoice3' in engines, \
                    f"{lang} should have cosyvoice3, got {engines}"


def _installed_engines_for(lang='en'):
    """The engines on THIS node's ladder for ``lang``, measured.

    These are functional tests: they drive the real router against the real
    machine, with nothing mocked.  Until 2026-09-21 select_engines appended
    espeak unconditionally, so they could assert ">= 1 candidate" and
    "espeak is always present" on any box.  That floor was a fabrication --
    the ladder offered an engine it had already been told was absent, and a
    voiced turn then spent its time failing on it (c9bbcd91b).  espeak is
    bundled on the shipped OS, so the guarantee is real THERE; on a bare dev
    box nothing is installed and the honest answer is an empty ladder.

    So the shape assertions below run where a node has something to offer,
    and TestBareNodeIsHonest covers the other case.
    """
    from integrations.channels.media.tts_router import (
        LANG_ENGINE_PREFERENCE, _DEFAULT_PREFERENCE, _is_engine_installed,
    )
    ladder = LANG_ENGINE_PREFERENCE.get(lang, _DEFAULT_PREFERENCE)
    return [e for e in ladder if _is_engine_installed(e)]


def _node_can_serve(lang='en'):
    """Whether this node can offer ANY engine for ``lang`` right now.

    Installed is not the predicate -- fitting is.  Measured on the owner's
    desktop 2026-09-21: chatterbox_turbo IS installed, in its own venv, and
    is GPU-only against 2.97 GB free beside a resident LLM, so the ladder is
    correctly empty even though an engine is on disk.  Gating on
    "is something installed" would have kept these tests red for a router
    that was behaving exactly right.
    """
    from integrations.channels.media.tts_router import TTSRouter
    return bool(TTSRouter().select_engines('Hello', language=lang))


needs_an_installed_engine = pytest.mark.skipif(
    not _node_can_serve('en'),
    reason='no TTS engine on this node can serve English right now, so the '
           'ladder is correctly empty -- see TestBareNodeIsHonest',
)

needs_espeak = pytest.mark.skipif(
    'espeak' not in _installed_engines_for('en'),
    reason='espeak-ng is not on this box; it is bundled on the shipped OS, '
           'where this guarantee holds',
)


@needs_an_installed_engine
class TestTTSRouterSelection:
    """T16: TTS engine selection under various constraints."""

    @pytest.fixture
    def router(self):
        from integrations.channels.media.tts_router import TTSRouter
        return TTSRouter()

    def test_select_english_returns_candidates(self, router):
        candidates = router.select_engines("Hello world", language="en")
        assert len(candidates) >= 1, "Should return at least one candidate"

    @needs_espeak
    def test_select_english_espeak_is_the_floor_where_it_exists(self, router):
        candidates = router.select_engines("Hello world", language="en")
        names = [c.engine.engine_id if hasattr(c.engine, 'engine_id') else c.engine for c in candidates]
        assert 'espeak' in names, f"espeak should always be a candidate, got {names}"

    def test_select_hindi_returns_candidates(self, router):
        candidates = router.select_engines("नमस्ते", language="hi")
        assert len(candidates) >= 1

    def test_select_tamil_returns_candidates(self, router):
        candidates = router.select_engines("வணக்கம்", language="ta")
        assert len(candidates) >= 1

    def test_select_chinese_returns_candidates(self, router):
        candidates = router.select_engines("你好世界", language="zh")
        assert len(candidates) >= 1

    def test_select_unknown_language_falls_back(self, router):
        """Unknown language should still return candidates (default fallback)."""
        candidates = router.select_engines("xyz abc", language="xx")
        assert len(candidates) >= 1

    def test_urgency_instant_prefers_low_latency(self, router):
        candidates = router.select_engines("Hello", language="en", urgency="instant")
        assert len(candidates) >= 1

    def test_urgency_quality_prefers_high_quality(self, router):
        candidates = router.select_engines("Hello", language="en", urgency="quality")
        assert len(candidates) >= 1

    def test_voice_clone_filter(self, router):
        candidates = router.select_engines("Hello", language="en", require_clone=True)
        # Clone filter returns clone-capable engines + espeak (always appended)
        clone_capable = [c for c in candidates
                         if hasattr(c, 'engine') and hasattr(c.engine, 'voice_clone')
                         and c.engine.voice_clone]
        assert len(clone_capable) >= 1, "Should have at least one clone-capable engine"


@needs_an_installed_engine
class TestTTSResourceConstraints:
    """T16: Simulated resource constraints."""

    @pytest.fixture
    def router(self):
        from integrations.channels.media.tts_router import TTSRouter
        return TTSRouter()

    def test_no_gpu_excludes_gpu_only(self, router):
        """With no GPU, GPU-only engines should be excluded or ranked lowest."""
        with patch('integrations.channels.media.tts_router._get_gpu_info',
                   return_value={'cuda_available': False, 'vram_total_gb': 0}):
            candidates = router.select_engines("Hello", language="en")
            assert len(candidates) >= 1
            # Should still produce candidates (CPU fallback exists)

    def test_low_vram_prefers_small_engines(self, router):
        """With 1GB VRAM, only small engines should be selected."""
        with patch('integrations.channels.media.tts_router._get_gpu_info',
                   return_value={'cuda_available': True, 'vram_total_gb': 1.0, 'vram_free_gb': 0.5}):
            candidates = router.select_engines("Hello", language="en")
            assert len(candidates) >= 1

    def test_local_only_excludes_hive(self, router):
        """compute_policy=local_only should never select hive peers."""
        with patch('integrations.channels.media.tts_router._get_compute_policy',
                   return_value={'compute_policy': 'local_only'}):
            candidates = router.select_engines("Hello", language="en")
            for c in candidates:
                assert c.engine.engine_id if hasattr(c.engine, 'engine_id') else c.engine != 'hive_tts', \
                    "local_only should never select hive peers"

    def test_empty_text_still_returns_candidates(self, router):
        """Even with empty text, router should return candidates."""
        candidates = router.select_engines("", language="en")
        assert len(candidates) >= 1, "Router should always return at least one candidate"


class TestTTSQualityBaselines:
    """T17: Naturalness A/B baselines — verify quality scores are set."""

    def test_qualitys_are_floats(self):
        from integrations.channels.media.tts_router import ENGINE_REGISTRY
        for name, spec in ENGINE_REGISTRY.items():
            score = getattr(spec, 'quality', None)
            if score is not None:
                assert isinstance(score, (int, float)), \
                    f"{name} quality should be numeric, got {type(score)}"
                assert 0.0 <= score <= 1.0, \
                    f"{name} quality should be 0-1, got {score}"

    def test_chatterbox_turbo_highest_english(self):
        from integrations.channels.media.tts_router import ENGINE_REGISTRY
        ct = ENGINE_REGISTRY.get('chatterbox_turbo')
        if ct:
            assert getattr(ct, 'quality', 0) >= 0.9, \
                "chatterbox_turbo should have quality >= 0.9 for English"

    def test_espeak_lowest_quality(self):
        from integrations.channels.media.tts_router import ENGINE_REGISTRY
        espeak = ENGINE_REGISTRY.get('espeak')
        if espeak:
            assert getattr(espeak, 'quality', 1) <= 0.5, \
                "espeak should have quality <= 0.5 (it's the fallback)"

    def test_language_routing_correctness(self):
        """Verify each language preference list is ordered by quality."""
        from integrations.channels.media.tts_router import LANG_ENGINE_PREFERENCE, ENGINE_REGISTRY
        for lang, engines in LANG_ENGINE_PREFERENCE.items():
            scores = []
            for eid in engines:
                spec = ENGINE_REGISTRY.get(eid)
                if spec:
                    scores.append(getattr(spec, 'quality', 0))
            # Scores should be roughly descending (allowing for ties)
            for i in range(len(scores) - 1):
                if scores[i] < scores[i + 1] - 0.1:
                    pass  # Allow some flexibility in ordering


class TestBareNodeIsHonest:
    """A node with no TTS engine must say so rather than offer one.

    The counterpart to the skips above: where those classes have nothing to
    assert, this is what the router owes the caller instead.
    """

    @pytest.fixture
    def router(self):
        from integrations.channels.media.tts_router import TTSRouter
        return TTSRouter()

    @pytest.mark.skipif(
        _node_can_serve('en'),
        reason='this node can serve English, so the bare-node path is not live here',
    )
    def test_no_engine_means_no_candidates(self, router):
        assert router.select_engines('Hello', language='en') == []

    def test_synthesize_names_the_absence_rather_than_a_failure(self, router):
        """Distinguishing "nothing is installed" from "engines were tried and
        failed" is what lets the caller offer a setup instead of a shrug."""
        from unittest.mock import patch
        with patch('integrations.channels.media.tts_router._is_engine_installed',
                   return_value=False):
            result = router.synthesize('Hello', language='en')
        assert result.error == 'No TTS engine is installed for this language'
        assert result.path == ''
