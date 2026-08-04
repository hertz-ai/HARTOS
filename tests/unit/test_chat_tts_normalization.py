"""The chat-reply TTS path must go through the ONE text normalizer (#10 / 3.5).

THE DEFECT THIS GUARDS
──────────────────────
`tts_text_normalizer.normalize_for_tts` turns "Rs.200", "12.5%" and "2:30 PM"
into words, because diffusion-token TTS engines cannot pronounce those tokens
— they skip them or emit garbage. Its own docstring states the design:

    Single converging path:
      Called ONCE from tts_router.synthesize().  Every TTS engine in the
      registry benefits — we do not duplicate this logic per-engine.

That was true of the ENGINES and false of the CALLERS. The chat-reply path
(`_tts_synthesize_and_publish` -> `tts.tts_engine.synthesize_text`) never
touched the router and so never normalized anything: the identical sentence
was pronounced correctly through /api/voice/speak and spoken as garbage in
chat. Two callers reach this function — the /chat route and
speculative_dispatcher via safe_hartos_attr — so both were affected.

WHY THE MOCKS ARE WHERE THEY ARE
────────────────────────────────
`tts.tts_engine` is a NUNBA-BUNDLED module and genuinely does not exist in
this repo, so it is injected at the boundary rather than patched. Everything
inside the boundary — the artifact stripping, the urgency lookup, the real
rule-pass normalizer — runs for real. The LLM fallback is disabled so the
assertions stay deterministic and offline; its behaviour is already covered
by tests/unit/test_tts_text_normalizer.py, which is also where the expected
strings below come from rather than being re-derived here.
"""
import importlib.util
import os
import sys
import tempfile
import types
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))))

from integrations.channels.media import tts_text_normalizer as tn  # noqa: E402


class _InlineExecutor:
    """Runs the submitted callable inline so the assertions are not racing it.

    `_tts_synthesize_and_publish` hands `_bg` to a ThreadPoolExecutor; a test
    that returned before the thread ran would pass whether or not the fix is
    present.
    """

    def __init__(self):
        self.submitted = 0

    def submit(self, fn, *args, **kwargs):
        self.submitted += 1
        fn(*args, **kwargs)
        return None


class ChatTTSPathUsesTheCanonicalNormalizer(unittest.TestCase):

    def setUp(self):
        self._tmpdir = tempfile.mkdtemp()
        self._prev = os.environ.get('HEVOLVE_CACHE_DIR')
        os.environ['HEVOLVE_CACHE_DIR'] = self._tmpdir

    def tearDown(self):
        if self._prev is None:
            os.environ.pop('HEVOLVE_CACHE_DIR', None)
        else:
            os.environ['HEVOLVE_CACHE_DIR'] = self._prev

    def _speak(self, text, language='en'):
        """Drive the REAL _tts_synthesize_and_publish; return what TTS got."""
        import hart_intelligence_entry as hie

        captured = {}

        def _synthesize_text(t, language='en', **kw):
            captured['text'] = t
            captured['language'] = language
            return ''          # empty path -> the publish half no-ops

        engine_mod = types.ModuleType('tts.tts_engine')
        engine_mod.get_tts_engine = lambda: types.SimpleNamespace(
            backend_name='fake-engine')
        engine_mod.synthesize_text = _synthesize_text
        pkg = types.ModuleType('tts')
        pkg.tts_engine = engine_mod

        executor = _InlineExecutor()
        with patch.dict(sys.modules,
                        {'tts': pkg, 'tts.tts_engine': engine_mod}), \
                patch.object(hie, '_tts_executor', executor), \
                patch.object(tn, '_llm_normalize', lambda *a, **k: None):
            hie._tts_synthesize_and_publish(
                text, 'user-1', 'req-1', language=language)

        self.assertEqual(executor.submitted, 1,
                         "the TTS job was never submitted at all")
        self.assertIn('text', captured,
                      "synthesize_text was never reached — the assertions "
                      "below would have passed vacuously")
        return captured

    def test_currency_symbol_is_spoken_as_a_word(self):
        """The exact defect: 'Rs.200' reached the engine verbatim.

        Asserted WITHOUT the digit expansion, which needs an optional
        dependency — see the skipUnless test below. Splitting them keeps this
        one meaningful on a box that lacks num2words, instead of red for a
        reason that has nothing to do with the path under test.
        """
        got = self._speak('The fare is Rs.200')['text']
        self.assertIn('rupees', got)
        self.assertNotIn('Rs.200', got)

    @unittest.skipUnless(
        importlib.util.find_spec('num2words') is not None,
        "num2words absent — digit expansion cannot happen. NOTE: it is pinned "
        "in requirements.txt but MISSING from nixos/packages/hart-app.nix, so "
        "the shipped OS is in this same state")
    def test_number_is_expanded_to_words(self):
        got = self._speak('The fare is Rs.200')['text']
        self.assertIn('two hundred', got)

    def test_percent_is_expanded(self):
        got = self._speak('Growth of 12%')['text']
        self.assertIn('percent', got)
        self.assertNotIn('12%', got)

    def test_language_is_forwarded_to_the_normalizer(self):
        """A wrong language silently normalizes into the wrong tongue.

        Asserted through an observable difference rather than a call arg: in
        Hindi the canonical normalizer emits the Devanagari word for rupees.
        """
        got = self._speak('कीमत Rs.200 है', language='hi')
        self.assertEqual(got['language'], 'hi',
                         "language must still reach the engine unchanged")
        self.assertIn('रुपये', got['text'])

    def test_stripping_still_happens_before_normalization(self):
        """The pre-existing artifact stripping must not have been displaced.

        Normalization was ADDED to that pipeline, not swapped in for it — a
        regression here means code fences and emoji are being read aloud.
        """
        got = self._speak(
            'Result ```print(1)``` and **bold** see https://x.com 🙂 done'
        )['text']
        for artifact in ('```', 'print(1)', '**', 'https://x.com', '🙂'):
            self.assertNotIn(artifact, got, f"{artifact!r} survived stripping")

    def test_normalizer_failure_never_blocks_speech(self):
        """Degraded mode: if normalization raises, the reply is still spoken.

        The router's own call site swallows normalizer errors for this reason;
        this path must degrade identically rather than go silent.
        """
        import hart_intelligence_entry as hie

        captured = {}

        def _synthesize_text(t, language='en', **kw):
            captured['text'] = t
            return ''

        engine_mod = types.ModuleType('tts.tts_engine')
        engine_mod.get_tts_engine = lambda: types.SimpleNamespace(
            backend_name='fake-engine')
        engine_mod.synthesize_text = _synthesize_text
        pkg = types.ModuleType('tts')
        pkg.tts_engine = engine_mod

        def _boom(*a, **k):
            raise RuntimeError("normalizer exploded")

        with patch.dict(sys.modules,
                        {'tts': pkg, 'tts.tts_engine': engine_mod}), \
                patch.object(hie, '_tts_executor', _InlineExecutor()), \
                patch.object(tn, 'normalize_for_tts', _boom):
            hie._tts_synthesize_and_publish(
                'The fare is Rs.200', 'user-1', 'req-1', language='en')

        self.assertIn('text', captured,
                      "a normalizer failure silenced the assistant entirely")
        # Un-normalized, but SPOKEN — the stripping still applied.
        self.assertIn('Rs.200', captured['text'])


if __name__ == '__main__':
    unittest.main()
