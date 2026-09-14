"""A reply spoken as an avatar goes through the canonical synth entry.

TTSRouter.synthesize is the canonical synth entry and clones from a voice
reference; the chat path's tts_engine.synthesize_text is the divergent copy
(docs/architecture/HARTOS_PARALLEL_PATH_AUDIT.md, "TTS orchestration split";
docs/internal/ux_degrading_design_choices 3.5).  So _tts_synthesize_and_publish
hands a voiced utterance to the router and uses its audio only when a cloning
engine answered with a file.  Otherwise it speaks the default voice through
synthesize_text WITHOUT the reference: that engine's primary backend (Piper on
a desktop) raises on a file path, and its fallback then makes another engine
the active one for good.

`tts.tts_engine` is Nunba-bundled and absent from this repo, so it is injected
at the boundary, as in test_chat_tts_normalization.py; the router is replaced
at its accessor.  The engine registry is the real one.
"""
import os
import shutil
import sys
import tempfile
import types
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))))

from integrations.channels.media import tts_text_normalizer as tn  # noqa: E402
from integrations.channels.media.tts_router import TTSResult  # noqa: E402


class _InlineExecutor:
    """Runs the TTS job inline, so the assertions are not racing its thread."""

    def submit(self, fn, *args, **kwargs):
        fn(*args, **kwargs)


def _result(engine_id, path, error=None):
    return TTSResult(path=path, duration=1.0, engine_id=engine_id,
                     device='gpu', location='local', latency_ms=1.0,
                     sample_rate=24000, voice='ref', quality_score=0.9,
                     error=error)


class VoicedReplyGoesThroughTheRouter(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.recording = self._file('avatar_voice.wav')
        self.cloned = self._file('cloned_reply.wav')
        self.default = self._file('default_reply.wav')

    def _file(self, name):
        path = os.path.join(self.tmp, name)
        with open(path, 'wb') as fh:
            fh.write(b'RIFF')
        return path

    def _speak(self, voice, result=None, raises=None, avatar_id=7):
        """Drive the REAL _tts_synthesize_and_publish; return what it did."""
        import hart_intelligence_entry as hie

        seen = {'synthesize_text': [], 'published': []}

        def _synthesize_text(text, language='en', **kwargs):
            seen['synthesize_text'].append(kwargs)
            return self.default

        engine_mod = types.ModuleType('tts.tts_engine')
        engine_mod.get_tts_engine = lambda: types.SimpleNamespace(
            backend_name='fake-engine')
        engine_mod.synthesize_text = _synthesize_text
        pkg = types.ModuleType('tts')
        pkg.tts_engine = engine_mod

        router = MagicMock()
        if raises is not None:
            router.synthesize.side_effect = raises
        else:
            router.synthesize.return_value = result
        normalize = MagicMock(side_effect=lambda text, *a, **k: text)

        with patch.dict(sys.modules, {'tts': pkg, 'tts.tts_engine': engine_mod}), \
                patch.object(hie, '_tts_executor', _InlineExecutor()), \
                patch.object(tn, 'normalize_for_tts', normalize), \
                patch('core.teacher_avatar.voice_reference', return_value=voice), \
                patch('integrations.channels.media.tts_router.get_tts_router',
                      return_value=router), \
                patch.object(hie, 'publish_async',
                             lambda topic, payload: seen['published'].append(payload)):
            hie._tts_synthesize_and_publish(
                'Hello there, friend.', 'user-1', 'req-1',
                language='en', avatar_id=avatar_id)
        seen['router'] = router
        seen['normalize'] = normalize
        return seen

    def _published_file(self, seen):
        self.assertEqual(len(seen['published']), 1, 'the reply must be spoken once')
        return seen['published'][0].rsplit('/tts/audio/', 1)[1].split('"', 1)[0]

    def _spoke_the_default_voice(self, seen):
        self.assertEqual(len(seen['synthesize_text']), 1)
        self.assertNotIn('voice', seen['synthesize_text'][0],
                         'synthesize_text must never receive a voice reference')
        self.assertEqual(self._published_file(seen), 'default_reply.wav')

    # ── the voice is used ──────────────────────────────────────────────

    def test_a_recorded_voice_is_spoken_by_a_cloning_engine(self):
        seen = self._speak(self.recording,
                           _result('chatterbox_turbo', self.cloned))
        seen['router'].synthesize.assert_called_once()
        call = seen['router'].synthesize.call_args
        self.assertEqual(call.kwargs['voice'], self.recording)
        self.assertEqual(call.kwargs['source'], 'chat_response')
        self.assertEqual(seen['synthesize_text'], [])
        self.assertEqual(self._published_file(seen), 'cloned_reply.wav')
        # the router normalizes the text itself; it is not normalized twice
        seen['normalize'].assert_not_called()

    # ── the voice is not used: the default voice, with no reference ────

    def test_an_engine_that_cannot_clone_is_not_taken_as_the_voice(self):
        """The router appends espeak even when it needs a clone."""
        self._spoke_the_default_voice(
            self._speak(self.recording, _result('espeak', self.cloned)))

    def test_a_router_error_falls_back(self):
        self._spoke_the_default_voice(self._speak(
            self.recording, _result('none', '', error='All TTS engines failed')))

    def test_a_router_crash_falls_back(self):
        self._spoke_the_default_voice(
            self._speak(self.recording, raises=RuntimeError('worker died')))

    def test_an_engine_the_registry_does_not_know_falls_back(self):
        self._spoke_the_default_voice(
            self._speak(self.recording, _result('mystery_engine', self.cloned)))

    def test_an_answer_without_a_file_falls_back(self):
        missing = os.path.join(self.tmp, 'never_written.wav')
        self._spoke_the_default_voice(
            self._speak(self.recording, _result('chatterbox_turbo', missing)))

    def test_no_reference_never_reaches_the_router(self):
        """The router's own test: voice not in ('default', '', None)."""
        for voice in (None, '', 'default'):
            with self.subTest(voice=voice):
                seen = self._speak(voice, _result('chatterbox_turbo', self.cloned))
                seen['router'].synthesize.assert_not_called()
                self._spoke_the_default_voice(seen)
                seen['normalize'].assert_called_once()

    def test_a_reply_without_an_avatar_never_reaches_the_router(self):
        seen = self._speak(self.recording, _result('chatterbox_turbo', self.cloned),
                           avatar_id=None)
        seen['router'].synthesize.assert_not_called()
        self._spoke_the_default_voice(seen)


if __name__ == '__main__':
    unittest.main()
