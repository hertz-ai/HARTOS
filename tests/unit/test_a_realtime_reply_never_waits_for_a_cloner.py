"""A realtime reply never waits for a voice-cloning engine to start (owner, 2026-11-26).

"While setting up a cloning voice TTS we can continue to serve with whichever TTS engine
currently serves, also for realtime conversations and languages where a signature cannot
be given to speech engines; an agent should use realtime TTS or not by whether the action
can work in a blocking or non-blocking way."

A chat reply is the blocking kind: someone is waiting.  The rule lives in the canonical synth
entry, TTSRouter.synthesize(running_only=True) (tests/unit/test_tts_router_realtime.py pins
it: only a running cloner speaks, none running brings one up off the path, a remembered
reply is heard at once).  Pinned here, the chat reply's half: it asks the router that way,
publishes the voice when the router answers with one, and otherwise speaks now with the
engine that serves -- with no voice-clone setup offer while a cloner is being brought up.  An
asynchronous caller (realtime=False) can wait: the router brings the voice up on demand.
"""
import os
import shutil
import sys
import tempfile
import types
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..'))

from tests.unit.module_swap import swap_modules  # noqa: E402

from integrations.channels.media import tts_text_normalizer as tn  # noqa: E402
from integrations.channels.media.tts_router import ENGINE_REGISTRY, TTSResult  # noqa: E402


class _InlineExecutor:
    def submit(self, fn, *args, **kwargs):
        fn(*args, **kwargs)


def _a_cloner():
    return next(eid for eid, spec in ENGINE_REGISTRY.items() if spec.voice_clone)


def _warming(engine_id):
    return TTSResult(path='', duration=0, engine_id='none', device='none', location='none',
                     latency_ms=0, sample_rate=0, voice='', quality_score=0,
                     error=f'No engine is running for this request; {engine_id} is being brought up',
                     warming=engine_id)


class ARealtimeReplyNeverWaitsForACloner(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.default = os.path.join(self.tmp, 'default_reply.wav')
        self.cloned = os.path.join(self.tmp, 'cloned_reply.wav')
        for p in (self.default, self.cloned):
            with open(p, 'wb') as fh:
                fh.write(b'RIFF')

    def _speak(self, router_answer, *, realtime=True):
        import hart_intelligence_entry as hie
        seen = {'synthesize_text': 0, 'published': [], 'offers': []}

        def _synthesize_text(text, language='en', **kwargs):
            seen['synthesize_text'] += 1
            return self.default

        engine_mod = types.ModuleType('tts.tts_engine')
        engine_mod.get_tts_engine = lambda: types.SimpleNamespace(backend_name='fake-engine')
        engine_mod.synthesize_text = _synthesize_text
        pkg = types.ModuleType('tts')
        pkg.tts_engine = engine_mod
        router = MagicMock()
        router.synthesize.return_value = router_answer
        with swap_modules({'tts': pkg, 'tts.tts_engine': engine_mod}), \
                patch.object(hie, '_tts_executor', _InlineExecutor()), \
                patch.object(tn, 'normalize_for_tts', MagicMock(side_effect=lambda t, *a, **k: t)), \
                patch('core.teacher_avatar.voice_reference', return_value=os.path.join(self.tmp, 'voice.wav')), \
                patch('integrations.channels.media.tts_router.get_tts_router', return_value=router), \
                patch.object(hie, 'publish_async', lambda topic, payload: seen['published'].append(payload)), \
                patch.object(hie, '_offer_voice_clone_setup', lambda lang: seen['offers'].append(lang)):
            hie._tts_synthesize_and_publish('Hello there, friend.', 'user-1', 'req-1',
                                            language='en', avatar_id=7, realtime=realtime)
        seen['router'] = router
        return seen

    def _cloned(self):
        return TTSResult(path=self.cloned, duration=1.0, engine_id=_a_cloner(), device='gpu',
                         location='local', latency_ms=10, sample_rate=24000, voice='v',
                         quality_score=0.9)

    def test_a_realtime_reply_asks_only_for_a_running_cloner(self):
        seen = self._speak(self._cloned())
        self.assertIs(seen['router'].synthesize.call_args.kwargs['running_only'], True)
        self.assertIn('cloned_reply.wav', seen['published'][0])
        self.assertEqual(seen['synthesize_text'], 0)

    def test_none_running_the_engine_that_serves_speaks_now(self):
        seen = self._speak(_warming('chatterbox_turbo'))
        self.assertEqual(seen['synthesize_text'], 1)
        self.assertIn('default_reply.wav', seen['published'][0])
        self.assertEqual(len(seen['published']), 1, 'spoken once')

    def test_no_setup_offer_while_a_cloner_is_brought_up(self):
        """A cloner is installed and coming up: offering to set one up would be wrong."""
        seen = self._speak(_warming('chatterbox_turbo'))
        self.assertEqual(seen['offers'], [])

    def test_no_cloner_installed_is_still_offered(self):
        seen = self._speak(TTSResult(path='', duration=0, engine_id='none', device='none',
                                     location='none', latency_ms=0, sample_rate=0, voice='',
                                     quality_score=0,
                                     error='No TTS engine is installed for this language'))
        self.assertEqual(seen['offers'], ['en'])
        self.assertIn('default_reply.wav', seen['published'][0])

    def test_an_asynchronous_caller_can_wait_for_the_voice(self):
        seen = self._speak(self._cloned(), realtime=False)
        self.assertIs(seen['router'].synthesize.call_args.kwargs['running_only'], False)
        self.assertIn('cloned_reply.wav', seen['published'][0])

    def test_the_reply_has_no_gate_of_its_own(self):
        """One rule, in the router: the chat path keeps no second cloner check or warm-up."""
        import hart_intelligence_entry as hie
        for name in ('_cloner_serving', '_serving_cloner', '_serving_cloners', '_warm_cloner',
                     '_any_cloner_speaks', '_voiced_memo'):
            self.assertFalse(hasattr(hie, name), f'{name} is a second copy of the router rule')


if __name__ == '__main__':
    unittest.main()
