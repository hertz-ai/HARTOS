"""#731 — `_chat_reply` must honor the request's media_mode.

Product contract (Nunba routes/chatbot_routes.py, the ONE normalizer):
`media_mode` is the unified cross-platform flag — 'audio' | 'video' |
'text'.  Desktop defaults to 'audio', web to 'text'; only an EXPLICIT
'text' means "do not speak this reply".  The Nunba adapter
(routes/hartos_backend_adapter.py) has forwarded the flag in the /chat
body all along, with a TODO naming the missing half verbatim:

    "media_mode": media_mode,  # TODO: HARTOS /chat needs
                               # data.get('media_mode') to use this

hart_intelligence_entry never read it — zero occurrences of media_mode
in the file — so every reply synthesized speech regardless of mode.
Measured live 2026-08-30 17:48 (val-tool1 sent media_mode='text'):
"TTS: engine OK (Piper TTS (CPU)), submitting ..." then "TTS async:
publishing audio ... to pupit.val-tool1".  Inaudible that time only
because no client was subscribed to the probe user; a real text-mode
user gets spoken at, and error envelopes get spoken too (#716 family).

The gate lives in `_chat_reply` because its own docstring makes it the
single TTS policy home ("the TTS synthesis trigger lives in ONE
place").  Background callers with no request context — the speculative
expert publish in speculative_dispatcher.py:2011 calls
`_tts_synthesize_and_publish` directly, below this gate — keep today's
behavior.

    python -m pytest tests/unit/test_chat_reply_media_mode.py --noconftest -q
"""
import os
import tempfile
import unittest
from unittest.mock import patch


class ChatReplyHonorsMediaMode(unittest.TestCase):

    def setUp(self):
        self._tmpdir = tempfile.mkdtemp()
        self._prev = os.environ.get('HEVOLVE_CACHE_DIR')
        os.environ['HEVOLVE_CACHE_DIR'] = self._tmpdir

    def tearDown(self):
        if self._prev is None:
            os.environ.pop('HEVOLVE_CACHE_DIR', None)
        else:
            os.environ['HEVOLVE_CACHE_DIR'] = self._prev

    def _reply(self, body):
        """Drive the REAL _chat_reply inside a request carrying `body`."""
        import hart_intelligence_entry as hie

        calls = []
        with patch.object(hie, '_tts_synthesize_and_publish',
                          lambda *a, **k: calls.append(a)):
            with hie.app.test_request_context('/chat', json=body):
                hie._chat_reply('t-mm-user', 't-mm-req', 'hello there')
        return calls

    def test_explicit_text_mode_suppresses_tts(self):
        calls = self._reply({'media_mode': 'text', 'prompt': 'hi'})
        self.assertEqual(
            len(calls), 0,
            'media_mode="text" in the request body must suppress TTS '
            'synthesis — the reply was still handed to '
            '_tts_synthesize_and_publish')

    def test_audio_mode_still_speaks(self):
        calls = self._reply({'media_mode': 'audio', 'prompt': 'hi'})
        self.assertEqual(len(calls), 1,
                         'media_mode="audio" must keep synthesizing')

    def test_absent_mode_keeps_todays_behavior(self):
        """Legacy callers (gpt_lang path) send no media_mode — the
        adapter default is None/absent and the reply must still speak,
        exactly as before this gate existed."""
        calls = self._reply({'prompt': 'hi'})
        self.assertEqual(len(calls), 1,
                         'absent media_mode must not change behavior')


if __name__ == '__main__':
    unittest.main()
