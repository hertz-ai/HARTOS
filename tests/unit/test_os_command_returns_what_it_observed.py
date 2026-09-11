"""Every return of the OS-command tool hands back what it SAW.

Behaviour of the reader lives in test_vlm_response_is_read_as_produced.py.
This file pins the WIRING: that each of the tool's four exits calls it.

WHY FOUR, AND WHY THAT IS THE WHOLE STORY.
``execute_windows_or_android_command`` ends in four places:

    learn success  — first time an instruction is seen (no matching recipe)
    reuse success  — a banked recipe MATCHED, i.e. every REUSE walk
    screenshot failure                }  TOOL_FAILURE_RESULTS, the strings
    generic failure                   }  the fabrication gate keys on

The history of this defect is a history of fixing one at a time:

    c7627df5a  fixed the learn return.  Byte-verified in the deployed pyc.
               Changed nothing a user could see, because `matching_recipe` is
               truthy exactly when a recipe is being REUSED, so a reuse walk
               skips that branch by construction.  MEASURED 08:25-08:38:
               'Processing RPC response to create recipe format' 0x against
               'REUSING command - matched with' 1x + RELEARN-REFUSED 2x.
    32a4c866a  fixed the reuse return too — but both still read the response
               with a filter for message types local_loop never emits, so the
               "observation" was the INSTRUCTION echoed back.
    this one   both successes AND both failures read the producer's real
               output through integrations/vlm/response_view.py.

The failure exits matter most: that is where the model decides whether to
retry, adapt, or tell the user it could not — and it was handed a bare
constant with no record of what had been attempted.

DELIBERATELY UNCHANGED: the TOOL_FAILURE_RESULTS strings themselves.  The
fabrication gate matches them by SUBSTRING, so the observation is APPENDED
after the constant; a refusal still reads as a refusal and a failed action
still cannot count as completed.
"""

import io
import os
import re
import unittest

_HARTOS = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_SRC = os.environ.get('HARTOS_REUSE_SRC') or os.path.join(
    _HARTOS, 'hartos', 'reuse_recipe.py')

# The canonical reader's call shape.  Wiring is proven by the call being
# present between the branch's guard and its return — not by a helper merely
# existing, which is what the previous version of this file settled for.
_READER = '_rv.observation_text'


def _src():
    return io.open(_SRC, encoding='utf-8', errors='replace').read()


class TestEveryExitCarriesWhatItSaw(unittest.TestCase):

    def setUp(self):
        self.src = _src()

    def _window_before(self, marker_regex, guard):
        """Source between the enclosing `guard` and the matched return.

        Anchored to the guard rather than a character count.  An earlier
        draft used a 2600-char lookback and PASSED on the pre-fix source
        because it reached a DIFFERENT branch's call 11 lines above — a
        guard that could not fail, on the exact defect it existed for.
        """
        m = re.search(marker_regex, self.src)
        self.assertIsNotNone(
            m, 'return changed shape (%s) — re-point this guard' % marker_regex)
        start = self.src.rfind(guard, 0, m.start())
        self.assertNotEqual(start, -1, 'lost the enclosing guard %r' % guard)
        return self.src[start:m.start()]

    def test_the_learn_success_return_carries_it(self):
        w = self._window_before(
            r"return f'Successfully ran the command in user\\?'s computer and "
            r"created", "if 'extracted_responses' in response:")
        self.assertIn(_READER, w,
                      'the learn-path success return discards the observation')

    def test_the_reuse_success_return_carries_it(self):
        """THE branch every reuse walk takes — 0 learn-branch entries measured."""
        w = self._window_before(
            r"return 'Successfully ran the command in user\\?'s computer\.'",
            "if response and response['status'] == 'success':")
        self.assertIn(
            _READER, w,
            'the reuse-path success return discards the observation. A '
            'matching banked recipe skips the learn branch entirely, so '
            'fixing only that one leaves every reuse walk blind.')

    def test_the_failure_returns_carry_it_too(self):
        """Where knowing what was ATTEMPTED matters most."""
        i = self.src.find('TOOL_FAILURE_RESULTS[1]')
        self.assertNotEqual(i, -1, 'the failure branch changed shape')
        window = self.src[i:i + 1600]
        self.assertIn(
            _READER, window,
            'the failure branch returns a bare constant. The loop records '
            'every command it fired and its reasoning on EVERY exit and '
            'publishes exit_reason precisely so the caller can be honest '
            '(local_loop.py:756) — this branch threw all of it away.')
        self.assertIn(
            '_rv.outcome_summary', window,
            'the failure branch does not say WHY the loop stopped, though '
            'exit_reason is published for exactly that')

    def test_the_failure_contract_is_untouched(self):
        """TOOL_FAILURE_RESULTS is the fabrication gate's key — append, never
        substitute."""
        for k in ('TOOL_FAILURE_RESULTS[0]', 'TOOL_FAILURE_RESULTS[1]'):
            self.assertIn(k, self.src,
                          '%s was removed; the fabrication gate keys on these '
                          'exact strings to tell a refusal from real work' % k)

    def test_this_module_no_longer_parses_the_response_itself(self):
        """One shape, one reader."""
        self.assertNotIn(
            'def _vlm_recipe_steps(', self.src,
            'a local parse came back; response_view.py owns this shape')
        self.assertNotIn(
            'def _tool_observation_summary(', self.src,
            'a local summariser came back; response_view.py owns this shape')


if __name__ == '__main__':
    unittest.main()
