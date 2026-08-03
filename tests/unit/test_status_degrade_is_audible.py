"""/status must SAY WHY it is unhealthy, not just that it is (task #3d).

THE DEFECT
──────────
The hevolve-bridge block in `/status` was:

    except Exception:
        result['hevolve_core_healthy'] = False
        result['learning_active'] = False

Two problems, both about a reader who is trying to find out what is wrong:

1. The exception was discarded. When health is False the ONE fact you need is
   WHY, and this threw it away — a direct silent-gulp violation on, of all
   things, the health surface. The failure is not hypothetical: VM run
   30758875130 shows the learning pipeline failing to import (hevolveai rl_ef
   -> "RuntimeError: Explicitly using 'asyncio' already"). From outside, that
   was indistinguishable from a healthy node that simply has not started
   learning.

2. The response SHAPE changed. The success path always sets `learning_mode`;
   the degraded path did not, so the key silently vanished exactly when it
   mattered. `hart_cli status` prints whatever keys it finds, so the field
   just stopped appearing.

These tests drive the REAL Flask route through the test client with the bridge
made to raise, and assert on what a caller and an operator actually receive.
"""
import os
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))))


class StatusDegradesAudibly(unittest.TestCase):

    def _get_status(self, bridge_error=None):
        """Call the REAL /status route; optionally make the bridge blow up."""
        import hart_intelligence_entry as hie

        hie.app.config['TESTING'] = True
        client = hie.app.test_client()

        if bridge_error is None:
            return client.get('/status')

        def _boom(*a, **k):
            raise bridge_error

        # Patch where the route LOOKS IT UP: the import is inside the handler,
        # so the module attribute is resolved at call time.
        import integrations.agent_engine.world_model_bridge as wmb
        with patch.object(wmb, 'get_world_model_bridge', _boom):
            return client.get('/status')

    def test_the_route_answers_at_all(self):
        """Guard the guard — if /status 500s, everything below is vacuous."""
        resp = self._get_status()
        self.assertEqual(resp.status_code, 200)
        self.assertIn('status', resp.get_json())

    def test_degraded_body_names_the_error(self):
        """The reason must reach the caller, not just the log."""
        resp = self._get_status(
            bridge_error=RuntimeError("Explicitly using 'asyncio' already"))
        body = resp.get_json()
        self.assertIs(body['hevolve_core_healthy'], False)
        self.assertIs(body['learning_active'], False)
        self.assertIn('hevolve_core_error', body,
                      "the degraded response does not say WHY — the caller "
                      "sees 'unhealthy' with no way to tell an import crash "
                      "from a node that simply is not learning yet")
        self.assertIn('asyncio', body['hevolve_core_error'])
        self.assertIn('RuntimeError', body['hevolve_core_error'])

    def test_degraded_response_keeps_the_same_shape(self):
        """learning_mode must not vanish on the degraded path.

        `hart_cli status` iterates and prints whatever keys it finds, so a key
        that disappears under failure is a field that goes missing precisely
        when someone is looking at it.
        """
        ok = self._get_status().get_json()
        bad = self._get_status(bridge_error=OSError("bridge socket gone")).get_json()
        self.assertIn('learning_mode', ok)
        self.assertIn('learning_mode', bad,
                      "learning_mode present when healthy but absent when "
                      "degraded — the response shape changes under failure")
        self.assertEqual(bad['learning_mode'], 'unknown')

    def test_the_failure_is_logged_with_its_traceback(self):
        """An operator reading the journal must find the cause.

        exc_info is required, not decorative: the rl_ef import error is raised
        several frames deep and the message alone does not name the importer.
        """
        import hart_intelligence_entry as hie

        with patch.object(hie.app.logger, 'warning') as warn:
            self._get_status(bridge_error=RuntimeError("boom-xyz"))

        self.assertTrue(warn.called,
                        "the bridge failure was swallowed without a log line")
        _args, kwargs = warn.call_args
        self.assertTrue(kwargs.get('exc_info'),
                        "logged without exc_info — the traceback that names "
                        "the failing import is lost")
        # The logger is called lazily (`"...(%s: %s)...", type, exc`), so the
        # message text lives in the ARGS, not in a pre-formatted string.
        self.assertIn("boom-xyz", " ".join(str(a) for a in _args),
                      "the log line does not carry the exception text")


if __name__ == '__main__':
    unittest.main()
