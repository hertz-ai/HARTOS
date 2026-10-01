"""A background goal turn is never spoken.

MEASURED 2026-09-30 .. 2026-10-01 on the owner's Windows install
(gui_app.log + rotations, 5 files): 37 TTS syntheses came from daemon turns
(request_id 'daemon_<goal>') and every one was published with
`broadcast_sse_event: type=chat.pupit ... targeted=0` -- nobody was
listening.  Each one still held the Piper engine for ~30 s of CPU
(17:19:45 -> 17:20:15 for one 999,168-frame wav), the same engine a real
user's reply needs.  Only 2 syntheses in the same window came from the
user, both delivered.

`_chat_reply` is the single TTS policy home.  It suppressed speech only for an
explicit media_mode='text'; a daemon turn arrives in-process with no request
body, so it fell through to "keep speaking".  The one discriminator for "is
this a person" is dispatch.is_genuine_user_request; an EMPTY request id is
deliberately left speaking, because suppressing on it would silence real
callers that predate request ids.

    python -m pytest tests/unit/test_chat_reply_daemon_turn_is_silent.py --noconftest -q
"""
import os
import tempfile
import unittest
from unittest.mock import patch


class DaemonTurnIsNotSpoken(unittest.TestCase):

    def setUp(self):
        self._tmpdir = tempfile.mkdtemp()
        self._prev = os.environ.get('HEVOLVE_CACHE_DIR')
        os.environ['HEVOLVE_CACHE_DIR'] = self._tmpdir

    def tearDown(self):
        if self._prev is None:
            os.environ.pop('HEVOLVE_CACHE_DIR', None)
        else:
            os.environ['HEVOLVE_CACHE_DIR'] = self._prev

    def _spoken(self, request_id, body=None):
        import hart_intelligence_entry as hie

        calls = []
        with patch.object(hie, '_tts_synthesize_and_publish',
                          lambda *a, **k: calls.append(a)):
            if body is None:
                # The daemon path: in-process, an app context (jsonify needs
                # one) but no request body to read media_mode from.
                with hie.app.app_context():
                    hie._chat_reply('t-dm-user', request_id, 'hello there')
            else:
                with hie.app.test_request_context('/chat', json=body):
                    hie._chat_reply('t-dm-user', request_id, 'hello there')
        return calls

    def test_daemon_turn_without_request_context_is_silent(self):
        self.assertEqual(
            len(self._spoken('daemon_goal-123')), 0,
            "a 'daemon_<goal>' turn was handed to the TTS engine")

    def test_daemon_turn_is_silent_even_in_audio_mode(self):
        calls = self._spoken('daemon_goal-123',
                             {'media_mode': 'audio', 'prompt': 'x'})
        self.assertEqual(len(calls), 0,
                         "audio mode must not make a background turn speak")

    def test_a_user_turn_still_speaks(self):
        calls = self._spoken('c0bee2d3-706e-4d53-98bf-ec7f1bd7df0f',
                             {'media_mode': 'audio', 'prompt': 'hey'})
        self.assertEqual(len(calls), 1, 'a real user reply must be spoken')

    def test_a_user_turn_with_no_media_mode_still_speaks(self):
        calls = self._spoken('c0bee2d3-706e-4d53-98bf-ec7f1bd7df0f',
                             {'prompt': 'hey'})
        self.assertEqual(len(calls), 1)

    def test_an_empty_request_id_keeps_todays_behaviour(self):
        # Not asserted as 'a user' -- only that this change does not silence
        # callers that carry no id.
        calls = self._spoken('', {'prompt': 'hey'})
        self.assertEqual(len(calls), 1,
                         'an empty request id must keep speaking, as before')

    def test_the_gate_uses_the_single_discriminator(self):
        # No second rule: the gate must consult is_genuine_user_request.
        import inspect
        import hart_intelligence_entry as hie
        src = inspect.getsource(hie._chat_reply)
        self.assertIn('is_genuine_user_request', src)
        self.assertNotIn("startswith('daemon_')", src)


if __name__ == '__main__':
    unittest.main()
