"""Unit tests for OmniVoice TTS engine registration + tool module.

These tests verify the registry wiring without loading the model — the
actual subprocess + inference path is exercised by integration tests
once the omnivoice pip package is installed on a GPU host.
"""

import os
import unittest

from integrations.channels.media.tts_router import (
    ENGINE_REGISTRY,
    LANG_ENGINE_PREFERENCE,
    _DEFAULT_PREFERENCE,
    TTSDevice,
)
from integrations.service_tools import omnivoice_tool


class RegistryTest(unittest.TestCase):
    """OmniVoice is discoverable through the same interfaces as every
    other TTS engine."""

    def test_engine_spec_present(self):
        self.assertIn('omnivoice', ENGINE_REGISTRY)

    def test_engine_is_gpu_only(self):
        spec = ENGINE_REGISTRY['omnivoice']
        self.assertEqual(spec.device, TTSDevice.GPU_ONLY)

    def test_engine_supports_voice_cloning(self):
        self.assertTrue(ENGINE_REGISTRY['omnivoice'].voice_clone)

    def test_engine_wildcard_languages(self):
        # 646 langs encoded as the '*' wildcard — same convention as espeak
        self.assertEqual(ENGINE_REGISTRY['omnivoice'].languages, ('*',))

    def test_sample_rate_24khz(self):
        self.assertEqual(ENGINE_REGISTRY['omnivoice'].sample_rate, 24000)

    def test_required_package_is_omnivoice(self):
        self.assertEqual(
            ENGINE_REGISTRY['omnivoice'].required_package, 'omnivoice'
        )

    def test_tool_module_path(self):
        self.assertEqual(
            ENGINE_REGISTRY['omnivoice'].tool_module,
            'integrations.service_tools.omnivoice_tool',
        )


class LanguagePriorityTest(unittest.TestCase):
    """OmniVoice takes precedence over indic_parler / chatterbox_ml /
    cosyvoice3 in every non-English priority list."""

    def test_omnivoice_first_for_all_indic(self):
        indic_langs = (
            'hi', 'ta', 'te', 'bn', 'gu', 'kn', 'ml', 'mr', 'or', 'pa',
            'ur', 'as', 'ne', 'sa',
        )
        for lang in indic_langs:
            with self.subTest(lang=lang):
                self.assertEqual(
                    LANG_ENGINE_PREFERENCE[lang][0],
                    'omnivoice',
                    f'omnivoice should be primary for {lang}',
                )

    def test_indic_parler_still_fallback_for_indic(self):
        # One release cycle of overlap — parler must still appear so the
        # router can demote to it when omnivoice isn't installed.
        self.assertIn('indic_parler', LANG_ENGINE_PREFERENCE['hi'])
        self.assertIn('indic_parler', LANG_ENGINE_PREFERENCE['ta'])

    def test_omnivoice_first_for_cjk(self):
        for lang in ('zh', 'ja', 'ko'):
            with self.subTest(lang=lang):
                self.assertEqual(LANG_ENGINE_PREFERENCE[lang][0], 'omnivoice')

    def test_omnivoice_in_default_preference(self):
        self.assertEqual(_DEFAULT_PREFERENCE[0], 'omnivoice')

    def test_sinhala_newly_supported(self):
        # Sinhala (si) is a newly-supported language via OmniVoice —
        # wasn't in the table before.
        self.assertIn('si', LANG_ENGINE_PREFERENCE)
        self.assertEqual(LANG_ENGINE_PREFERENCE['si'][0], 'omnivoice')

    def test_english_prefers_chatterbox_turbo(self):
        # English has a dedicated higher-quality engine; omnivoice sits
        # below it.
        en = LANG_ENGINE_PREFERENCE['en']
        self.assertEqual(en[0], 'chatterbox_turbo')
        self.assertIn('omnivoice', en)
        self.assertLess(en.index('chatterbox_turbo'), en.index('omnivoice'))


class ToolModuleTest(unittest.TestCase):
    """The omnivoice_tool module exposes the standard parent-side
    interface — same shape as indic_parler_tool."""

    def test_public_synthesize_callable(self):
        self.assertTrue(callable(omnivoice_tool.omnivoice_synthesize))

    def test_public_unload_callable(self):
        self.assertTrue(callable(omnivoice_tool.unload_omnivoice))

    def test_worker_bound_to_vram_budget_key(self):
        self.assertEqual(omnivoice_tool._tool.vram_budget, 'tts_omnivoice')

    def test_worker_name(self):
        self.assertEqual(omnivoice_tool._tool.tool_name, 'omnivoice')

    def test_sample_rate_constant(self):
        self.assertEqual(omnivoice_tool.SAMPLE_RATE, 24000)


class VoiceClassifierTest(unittest.TestCase):
    """`_is_audio_path` classifies voice argument correctly so we pick
    ref_audio (cloning) vs instruct (voice design)."""

    def test_wav_path_detected(self):
        self.assertTrue(omnivoice_tool._is_audio_path('voice/ref.wav'))
        self.assertTrue(omnivoice_tool._is_audio_path('ref.mp3'))
        self.assertTrue(omnivoice_tool._is_audio_path('audio.flac'))

    def test_free_form_descriptor_not_audio_path(self):
        self.assertFalse(omnivoice_tool._is_audio_path(
            'female, low pitch, british accent'
        ))

    def test_none_not_audio_path(self):
        self.assertFalse(omnivoice_tool._is_audio_path(None))
        self.assertFalse(omnivoice_tool._is_audio_path(''))

    def test_absolute_nonexistent_path_not_audio_path(self):
        # Gate: absolute + existing on disk OR recognised suffix.  A
        # lone "/not/real" should NOT be treated as audio (we don't
        # want to accidentally pass a random path to ref_audio).
        self.assertFalse(omnivoice_tool._is_audio_path('/not/a/real/file'))


class SentenceSplitTest(unittest.TestCase):
    """Verify _split_sentences handles Latin, Devanagari, and Bengali
    sentence boundaries + ellipses."""

    def test_split_by_period(self):
        # Each sentence must exceed _MIN_CHUNK_CHARS (20) + _TAIL_MERGE_CHARS
        # (15) individually; otherwise the split post-processor merges
        # short fragments into neighbours.  This is intentional — short
        # chunks cause prosody discontinuities through diffusion TTS.
        out = omnivoice_tool._split_sentences(
            'The first full sentence goes here. '
            'The second full sentence follows. '
            'The third full sentence concludes the paragraph.'
        )
        self.assertGreaterEqual(len(out), 2)

    def test_split_preserves_ellipsis(self):
        # '...' must not be treated as three sentence boundaries
        text = 'Wait... Who is that? Interesting case indeed.'
        out = omnivoice_tool._split_sentences(text)
        self.assertTrue(any('...' in s for s in out))

    def test_short_text_not_split(self):
        out = omnivoice_tool._split_sentences('Short.')
        self.assertEqual(out, ['Short.'])

    def test_hindi_danda_split(self):
        # Devanagari danda '।' is a sentence boundary
        text = 'पहला वाक्य है। दूसरा वाक्य है। तीसरा वाक्य है।'
        out = omnivoice_tool._split_sentences(text)
        self.assertGreaterEqual(len(out), 2)


class VRAMBudgetTest(unittest.TestCase):
    """tts_omnivoice is registered in vram_manager.VRAM_BUDGETS."""

    def test_budget_entry_exists(self):
        import sys
        import integrations.service_tools.vram_manager  # noqa: F401
        vm = sys.modules['integrations.service_tools.vram_manager']
        self.assertIn('tts_omnivoice', vm.VRAM_BUDGETS)

    def test_budget_is_stub_value(self):
        import sys
        vm = sys.modules['integrations.service_tools.vram_manager']
        min_vram, model_size = vm.VRAM_BUDGETS['tts_omnivoice']
        # Stub: somewhere between 2 and 6 GB — telemetry tightens later.
        self.assertGreater(min_vram, 0)
        self.assertGreaterEqual(min_vram, model_size)
        self.assertLess(model_size, 6.0)


if __name__ == '__main__':
    unittest.main()
