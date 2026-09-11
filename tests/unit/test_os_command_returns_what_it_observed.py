"""The OS-command tool must hand back WHAT IT SAW, not just that it ran.

THE DEFECT, root-caused 2026-09-11 by reading every return in
``execute_windows_or_android_command`` (reuse_recipe.py:1771-2175).  All three
success paths return an ANNOUNCEMENT and discard the observation:

    :2134  'Successfully ran the command in user's computer and created the
            VLM agent data at {vlm_agent_path}.'
    :2145  'Successfully ran the command in user's computer.'      <- 48 chars
    :2138  'Command executed but could not create VLM agent data due to
            missing response structure'

The data is IN HAND at :2134.  The branch directly above it walks
``response['extracted_responses']``, cleans each ``analysis`` message through
``clean_text``, and builds ``recipe_steps`` -- then writes them to a VLM file
and returns a sentence with the content stripped out.  The model that asked
the question receives no observation and must invent one.

LIVE EVIDENCE, agent 89091774807 "disk.space.reporter", ground truth measured
BEFORE each run with Get-PSDrive C -> 9.17 GB free:

    07:29 turn  "The free space on your C: drive is currently 38.4 GB.  While
                 a previous stored value of 45.2 GB was expected..."
    07:50 turn  "...the current free space on your drive C: is 80 GB, which is
                 exactly the same as the previously saved value of 80 GB."

Both prefaced "Based on the data retrieved" / "Based on the tool results".
Neither number, and neither "previously saved value", exists anywhere.  The
same shape produced "145.6 GB" on agent 89088690384 (#835/D69).

THAT THE VLM PATH IS THE ONE THAT FIRED IS MEASURED, not assumed: in the
07:29-07:56 window "Generated recipe data saved to" appears 1x and
"No extracted_responses found" 0x, and the file read back carries
action='Identify the main system drive on the local computer and report its
label'.  So the tool did the work and the work reached a FILE but not the
model.

WHY THE PLACEHOLDER STORY IS NOT THIS STORY (#837/D71): the instrumented
build showed 181 REAL fills vs 34 placeholders, every line peers=5.  Many of
those "REAL" results are 48 chars -- exactly the announcement above.  A real
result that carries no data is indistinguishable, at the model, from no
result at all.  Fixing the placeholder path would not have put a single
number in front of the model.

WHAT THIS GUARD PINS: the success return carries the observed text, bounded,
built from the SAME cleaned steps the recipe file already stores -- no second
parse, no new source of truth.

WHAT IT DOES NOT CLAIM: that the VLM always observes the right thing, or that
the model then reports it faithfully.  It claims the observation is no longer
discarded between the tool and the model.

DELIBERATELY UNTOUCHED: TOOL_FAILURE_RESULTS.  The fabrication gate keys on
those exact strings to tell "the tool ran and refused" from "the tool did the
work" (reuse_recipe.py:2148-2155 comment).  Changing them would let a failed
action count as completed again.
"""

import io
import os
import re
import unittest

_HARTOS = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_SRC = os.environ.get('HARTOS_REUSE_SRC') or os.path.join(
    _HARTOS, 'hartos', 'reuse_recipe.py')

_HELPER = '_tool_observation_summary'


def _src():
    return io.open(_SRC, encoding='utf-8', errors='replace').read()


def _func(name, src):
    m = re.search(r'^def %s\(.*\n(?:(?:[ \t].*)?\n)*' % re.escape(name), src, re.M)
    return m.group(0) if m else ''


def _load_helper():
    """exec the REAL helper -- a local copy would prove nothing."""
    body = _func(_HELPER, _src())
    if not body:
        return None
    ns = {}
    try:
        from core.constants import TOOL_OBSERVATION_MAX_CHARS
        ns['TOOL_OBSERVATION_MAX_CHARS'] = TOOL_OBSERVATION_MAX_CHARS
    except Exception:
        ns['TOOL_OBSERVATION_MAX_CHARS'] = 2000
    exec(compile(body, '<helper>', 'exec'), ns)
    return ns[_HELPER]


# Verbatim shape of what the :2134 branch builds before it returns.
_LIVE_STEPS = [
    {'steps': 'The system drive is C:. Running Get-PSDrive C to read free space.',
     'tool_name': 'execute_windows_or_android_command',
     'agent_to_perform_this_action': 'Helper'},
    {'steps': 'Free space on C: reported as 9.17 GB.',
     'tool_name': 'execute_windows_or_android_command',
     'agent_to_perform_this_action': 'Helper'},
]


class TestTheObservationSurvivesTheReturn(unittest.TestCase):
    """RED until the tool returns what it saw."""

    def setUp(self):
        self.fn = _load_helper()
        if self.fn is None:
            self.skipTest('%s absent -- TestItIsWiredIntoTheSuccessReturn is '
                          'the assertion that fails for that' % _HELPER)

    def test_the_observed_text_is_returned(self):
        out = self.fn(_LIVE_STEPS)
        self.assertIn(
            '9.17', out,
            "the figure the tool observed is missing from its own return. "
            "Live 2026-09-11 that is exactly why the agent answered 38.4 GB "
            "and 80 GB against a real 9.17 GB: the model was handed "
            "'Successfully ran the command in user's computer.' and nothing "
            "else, so it invented the number.")

    def test_it_is_bounded(self):
        """An unbounded tool result would blow the wire budget (#730/#732)."""
        from core.constants import TOOL_OBSERVATION_MAX_CHARS
        huge = [{'steps': 'x' * 50000}]
        out = self.fn(huge)
        self.assertLessEqual(
            len(out), TOOL_OBSERVATION_MAX_CHARS + 200,
            'the observation is unbounded; a large screen dump would push the '
            'body past the slot budget the wire-trim work sized (n_ctx 12288, '
            '~6144/slot) and cost the whole turn')

    def test_empty_in_empty_out(self):
        for junk in (None, [], [{}], [{'steps': ''}], [{'steps': '   '}]):
            self.assertEqual(
                self.fn(junk), '',
                'an empty observation must stay empty so the caller keeps its '
                'plain success sentence rather than appending a blank section')

    def test_it_never_raises(self):
        """It runs on the tool return path; it must not cost the result."""
        for junk in (None, 'nonsense', [None], [{'steps': 5}], 42, [{'steps': None}]):
            try:
                self.fn(junk)
            except Exception as err:
                self.fail('raised on %r: %r' % (junk, err))


class TestItIsWiredIntoTheSuccessReturn(unittest.TestCase):
    """The helper existing is worth nothing if the return does not use it."""

    def setUp(self):
        self.src = _src()

    def test_the_helper_is_defined_once(self):
        self.assertEqual(
            len(re.findall(r'^def %s\(' % _HELPER, self.src, re.M)), 1,
            'the summariser must have ONE definition; two copies drift')

    def test_the_vlm_success_return_carries_the_observation(self):
        """:2134 is the branch that fired live (measured, see module docstring)."""
        m = re.search(r"Successfully ran the command in user\\?'s computer and "
                      r"created", self.src)
        self.assertIsNotNone(
            m, 'the VLM success return changed shape -- re-point this guard')
        window = self.src[max(0, m.start() - 1500):m.start() + 600]
        self.assertIn(
            _HELPER, window,
            'the VLM-path success return does not call %s, so the observation '
            'the branch just cleaned into recipe_steps is still discarded '
            'between the tool and the model.' % _HELPER)

    def test_the_failure_contract_is_untouched(self):
        """TOOL_FAILURE_RESULTS is the fabrication gate's key -- do not edit."""
        self.assertIn(
            'TOOL_FAILURE_RESULTS[1]', self.src,
            'the screenshot-failure return was changed; the fabrication gate '
            'keys on these EXACT strings to tell a refusal from real work')
        self.assertIn(
            'TOOL_FAILURE_RESULTS[0]', self.src,
            'the generic-failure return was changed; same contract')


if __name__ == '__main__':
    unittest.main()
