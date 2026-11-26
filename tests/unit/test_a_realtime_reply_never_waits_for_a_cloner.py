"""A realtime reply never waits for a voice-cloning engine to start (owner, 2026-11-26).

"While setting up a cloning voice TTS we can continue to serve with whichever TTS engine
currently serves, also for realtime conversations and languages where a signature cannot
be given to speech engines; an agent should use realtime TTS or not by whether the action
can work in a blocking or non-blocking way."

A chat reply is the blocking kind: someone is waiting.  So _tts_synthesize_and_publish
speaks in the avatar's recorded voice only when a cloning engine for the language is
already serving; otherwise the engine that serves speaks now and a cloner is brought up
off the reply's path for the turns after it.  An asynchronous caller (realtime=False)
keeps the old behaviour: the cloner is brought up on demand and the voice is used.
"""
import os
import shutil
import sys
import tempfile
import types
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..'))

from integrations.channels.media import tts_text_normalizer as tn  # noqa: E402
from integrations.channels.media.tts_router import ENGINE_REGISTRY, TTSResult  # noqa: E402


class _InlineExecutor:
    def submit(self, fn, *args, **kwargs):
        fn(*args, **kwargs)


def _a_cloner():
    return next(eid for eid, spec in ENGINE_REGISTRY.items() if spec.voice_clone)


class ARealtimeReplyNeverWaitsForACloner(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.default = os.path.join(self.tmp, 'default_reply.wav')
        self.cloned = os.path.join(self.tmp, 'cloned_reply.wav')
        for p in (self.default, self.cloned):
            with open(p, 'wb') as fh:
                fh.write(b'RIFF')

    def _speak(self, *, serving, realtime=True, language='en'):
        import hart_intelligence_entry as hie
        seen = {'synthesize_text': 0, 'published': [], 'warm': []}

        def _synthesize_text(text, language='en', **kwargs):
            seen['synthesize_text'] += 1
            return self.default

        engine_mod = types.ModuleType('tts.tts_engine')
        engine_mod.get_tts_engine = lambda: types.SimpleNamespace(backend_name='fake-engine')
        engine_mod.synthesize_text = _synthesize_text
        pkg = types.ModuleType('tts')
        pkg.tts_engine = engine_mod
        router = MagicMock()
        router.synthesize.return_value = TTSResult(
            path=self.cloned, duration=1.0, engine_id=_a_cloner(), device='gpu', location='local',
            latency_ms=10, sample_rate=24000, voice='v', quality_score=0.9)
        with patch.dict(sys.modules, {'tts': pkg, 'tts.tts_engine': engine_mod}), \
                patch.object(hie, '_tts_executor', _InlineExecutor()), \
                patch.object(tn, 'normalize_for_tts', MagicMock(side_effect=lambda t, *a, **k: t)), \
                patch('core.teacher_avatar.voice_reference', return_value=os.path.join(self.tmp, 'voice.wav')), \
                patch('integrations.channels.media.tts_router.get_tts_router', return_value=router), \
                patch.object(hie, 'publish_async', lambda topic, payload: seen['published'].append(payload)), \
                patch.object(hie, '_cloner_serving', return_value=serving), \
                patch.object(hie, '_warm_cloner', lambda *a: seen['warm'].append(a)):
            hie._tts_synthesize_and_publish('Hello there, friend.', 'user-1', 'req-1',
                                            language=language, avatar_id=7, realtime=realtime)
        seen['router'] = router
        return seen

    def test_no_cloner_serving_the_engine_that_serves_speaks_now(self):
        seen = self._speak(serving=False)
        router = seen['router']
        self.assertFalse(router.synthesize.called, 'the reply must not wait for a cloner to start')
        self.assertEqual(seen['synthesize_text'], 1)
        self.assertIn('default_reply.wav', seen['published'][0])
        self.assertEqual(len(seen['warm']), 1, 'a cloner is brought up for the turns after it')

    def test_a_cloner_serving_speaks_in_the_recorded_voice(self):
        seen = self._speak(serving=True)
        self.assertTrue(seen['router'].synthesize.called)
        self.assertIn('cloned_reply.wav', seen['published'][0])
        self.assertEqual(seen['warm'], [])

    def test_an_asynchronous_caller_can_wait_for_the_voice(self):
        seen = self._speak(serving=False, realtime=False)
        self.assertTrue(seen['router'].synthesize.called)
        self.assertIn('cloned_reply.wav', seen['published'][0])
        self.assertEqual(seen['warm'], [])


class TheClonerChecksAskOnly(unittest.TestCase):

    def _catalog(self, rows):
        return types.SimpleNamespace(list_by_type=lambda t: rows if t == 'tts' else [])

    def test_serving_means_a_loaded_cloner_that_speaks_the_language(self):
        import hart_intelligence_entry as hie
        en_only = next(eid for eid, s in ENGINE_REGISTRY.items() if s.voice_clone and s.languages == ('en',))
        row = types.SimpleNamespace(id='tts-' + en_only.replace('_', '-'), loaded=True)
        with patch('integrations.service_tools.model_catalog.get_catalog', return_value=self._catalog([row])):
            self.assertTrue(hie._cloner_serving('en'))
            self.assertTrue(hie._cloner_serving('en-IN'))
            self.assertFalse(hie._cloner_serving('ta'))  # a language it cannot take a voice in
        row.loaded = False
        with patch('integrations.service_tools.model_catalog.get_catalog', return_value=self._catalog([row])):
            self.assertFalse(hie._cloner_serving('en'))  # installed but not serving

    def test_a_non_cloning_engine_serving_is_not_a_cloner(self):
        import hart_intelligence_entry as hie
        plain = next(eid for eid, s in ENGINE_REGISTRY.items() if not s.voice_clone)
        row = types.SimpleNamespace(id='tts-' + plain.replace('_', '-'), loaded=True)
        with patch('integrations.service_tools.model_catalog.get_catalog', return_value=self._catalog([row])):
            self.assertFalse(hie._cloner_serving('en'))

    def test_the_warm_up_runs_off_the_reply_path_and_one_at_a_time(self):
        import threading
        import hart_intelligence_entry as hie
        started, release = threading.Event(), threading.Event()
        calls = []

        def slow_voice(text, language, voice):
            calls.append(voice)
            started.set()
            release.wait(5)
        with patch.object(hie, '_speak_in_voice', slow_voice), \
                patch.object(hie, '_cloner_warmed_at', {}), \
                patch.object(hie, '_any_cloner_speaks', return_value=True):
            try:
                hie._warm_cloner('hi', 'en', 'v1')  # returns at once
                self.assertTrue(started.wait(5))
                hie._warm_cloner('hi', 'en', 'v2')  # one already warming: nothing more
                self.assertEqual(calls, ['v1'])
            finally:
                release.set()

    def test_a_language_is_warmed_at_most_every_ten_minutes(self):
        import threading
        import hart_intelligence_entry as hie
        calls = []
        with patch.object(hie, '_speak_in_voice', lambda t, lang, v: calls.append(lang)), \
                patch.object(hie, '_cloner_warmed_at', {}), \
                patch.object(hie, '_any_cloner_speaks', return_value=True):
            for _ in range(3):
                hie._warm_cloner('hi', 'en', 'v')
                for t in threading.enumerate():
                    if t.name == 'tts-cloner-warmup':
                        t.join(5)
        self.assertEqual(calls, ['en'])  # a lagging 'loaded' flag costs one warm-up, not one per turn

    def test_no_warm_up_for_a_language_no_cloner_speaks(self):
        import hart_intelligence_entry as hie
        with patch.object(hie, '_any_cloner_speaks', return_value=False), \
                patch.object(hie, '_speak_in_voice', side_effect=AssertionError('warmed for nothing')):
            hie._warm_cloner('hi', 'xx', 'v')


if __name__ == '__main__':
    unittest.main()
