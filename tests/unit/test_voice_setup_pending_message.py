"""A fresh offline box must not blame the user for its own missing model (#7).

`_process_voice_command` sends the transcript to the Model Bus. On a box that
has never been online there is no model, the call fails, and the user was told:

    "Could not process"

which reads as "I did not understand you" — their microphone, their accent,
their fault. The words arrived perfectly; there was simply nothing to answer
them with, and that is fixed by connecting to the internet once, not by
speaking more clearly. Two opposite problems wearing the same sentence is how
a working machine feels broken.

These tests pin BOTH directions, because the fix is only worth having if it
still says "could not process" when that is actually what happened:
  * no models on the bus       -> setup-pending message + setup_pending flag
  * models present, call fails -> the original message, unchanged
"""
import os
import sys
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))))

from integrations.agent_engine import liquid_ui_service as lus  # noqa: E402


class _Svc:
    """Minimal stand-in carrying only what the method under test touches.

    The real service builds a Flask app and a renderer; binding the unbound
    functions onto a bare object exercises the SAME code without dragging that
    in. The methods are taken from the real class, not reimplemented.
    """
    model_bus_port = 6790
    _SETUP_PENDING_MSG = lus.LiquidUIService._SETUP_PENDING_MSG
    _process_voice_command = lus.LiquidUIService._process_voice_command
    # A REAL ContextEngine, because that is where the ONE Model-Bus probe
    # lives and it is what the code under test calls. Constructing the
    # real class (it only needs two ports) keeps the probe under test
    # instead of substituting a stand-in for the thing being reused.
    context_engine = lus.ContextEngine(6777, 6790)


def _resp(status, payload):
    r = MagicMock()
    r.status_code = status
    r.json.return_value = payload
    return r


class VoiceFailureNamesTheRealCause(unittest.TestCase):

    def test_no_model_says_setup_pending_not_could_not_process(self):
        """The fresh-offline-box case — the whole point of the change."""
        with patch('core.http_pool.pooled_post',
                   side_effect=OSError("connection refused")), \
                patch('core.http_pool.pooled_get',
                      return_value=_resp(200, {'models': []})):
            out = _Svc()._process_voice_command("what is the weather")

        self.assertEqual(out['text'], "what is the weather",
                         "the transcript must survive — the words WERE heard")
        self.assertTrue(out.get('setup_pending'),
                        "no machine-readable flag, so the shell cannot render "
                        "this differently from a real failure")
        self.assertIn("internet", out['response'].lower())
        self.assertNotIn("could not process", out['response'].lower())

    def test_models_present_keeps_the_honest_failure_message(self):
        """Do not over-claim: a stocked bus that fails IS a processing failure.

        Without this, the fix would relabel every voice error as a setup
        problem and simply move the dishonesty.
        """
        with patch('core.http_pool.pooled_post',
                   side_effect=OSError("boom")), \
                patch('core.http_pool.pooled_get',
                      return_value=_resp(200, {'models': [{'id': 'qwen'}]})):
            out = _Svc()._process_voice_command("hello")

        self.assertEqual(out['response'], 'Could not process')
        self.assertNotIn('setup_pending', out)

    def test_unreachable_model_bus_counts_as_no_model(self):
        """Cannot-tell must resolve to the accurate message.

        A box that cannot reach its own model bus is not a box that failed to
        understand you.
        """
        with patch('core.http_pool.pooled_post', side_effect=OSError("x")), \
                patch('core.http_pool.pooled_get',
                      side_effect=OSError("no route to host")):
            out = _Svc()._process_voice_command("hi")
        self.assertTrue(out.get('setup_pending'))

    def test_the_happy_path_is_untouched(self):
        """Guard the guard: if the success path broke, the rest is noise."""
        with patch('core.http_pool.pooled_post',
                   return_value=_resp(200, {'response': 'It is sunny.'})):
            out = _Svc()._process_voice_command("weather?")
        self.assertEqual(out['response'], 'It is sunny.')
        self.assertEqual(out['source'], 'voice')
        self.assertNotIn('setup_pending', out)

    def test_probe_is_bounded(self):
        """It runs on an already-failing path; it must not stall the failure."""
        seen = {}

        def _get(url, timeout=None, **kw):
            seen['timeout'] = timeout
            return _resp(200, {'models': []})

        with patch('core.http_pool.pooled_post', side_effect=OSError("x")), \
                patch('core.http_pool.pooled_get', _get):
            _Svc()._process_voice_command("hi")

        self.assertIsNotNone(seen.get('timeout'),
                             "model-bus probe has no timeout — one failure "
                             "could become a hang")
        self.assertLessEqual(seen['timeout'], 5)


if __name__ == '__main__':
    unittest.main()
