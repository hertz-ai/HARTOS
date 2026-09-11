"""The OS-command tool must hand back WHAT IT SAW, not just that it ran.

THE DEFECT, root-caused 2026-09-11 by reading every return in
``execute_windows_or_android_command``.  Both success paths returned an
ANNOUNCEMENT and discarded the observation:

    the VLM/learn path   'Successfully ran the command in user's computer and
                          created the VLM agent data at {vlm_agent_path}.'
    the plain path       'Successfully ran the command in user's computer.'
                                                               <- 48 chars

The data is in hand at both: ``response['extracted_responses']`` is the VLM's
own account of what it did, and the learn path already cleans it into
``recipe_steps`` before writing it to a file -- then returns a sentence with
the content stripped out.  The model that asked the question receives no
observation and must invent one.

LIVE EVIDENCE, agent 89091774807 "disk.space.reporter", ground truth measured
BEFORE each run with Get-PSDrive C -> 9.17 GB free:

    07:29 turn  "The free space on your C: drive is currently 38.4 GB.  While
                 a previous stored value of 45.2 GB was expected..."
    07:50 turn  "...the current free space on your drive C: is 80 GB, which is
                 exactly the same as the previously saved value of 80 GB."

Both prefaced "Based on the data retrieved" / "Based on the tool results".
Neither number, and neither "previously saved value", exists anywhere.  The
same shape produced "145.6 GB" on agent 89088690384 (#835/D69).

WHY THIS FILE WAS REWRITTEN, AND IT IS THE POINT OF THE WHOLE GUARD.
The first fix (c7627df5a) changed ONLY the VLM/learn return, because the
07:29 window measured that branch firing.  The deployed pyc was unmarshalled
and byte-verified.  It still put no figure in front of the model, because a
REUSE walk never reaches that branch:

    if response and response['status'] == 'success':
        if not matching_recipe and _relearning_claims_this_action:
            ...                       # learn path -- fixed first
            return <observation>
        # falls through whenever a banked recipe MATCHED
    if response and response['status'] == 'success':
        return <announcement>         # the path reuse actually takes

``matching_recipe`` is truthy exactly when the instruction resembles a banked
action -- which is what reusing a recipe MEANS.  Measured live 2026-09-11
08:25-08:38 on the same agent: 'Processing RPC response to create recipe
format' 0x (the learn branch was never entered) against 'REUSING command -
matched with' 1x and RELEARN-REFUSED 2x -- three skips for the window's three
'INSIDE execute_windows_or_android_command' calls.

So the lesson this file encodes: a fix verified in the BYTES is not verified
on the PATH.  Both returns are pinned below, by the branch each one serves.

WHAT THIS GUARD PINS: both success returns carry the observed text, bounded,
built from ONE extraction shared with the recipe writer -- no second parse, no
second source of truth for "what did the tool see".

WHAT IT DOES NOT CLAIM: that the VLM always observes the right thing, or that
the model then reports it faithfully.  It claims the observation is no longer
discarded between the tool and the model.

DELIBERATELY UNTOUCHED: TOOL_FAILURE_RESULTS.  The fabrication gate keys on
those exact strings to tell "the tool ran and refused" from "the tool did the
work".  Changing them would let a failed action count as completed again.
"""

import io
import os
import re
import unittest

_HARTOS = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_SRC = os.environ.get('HARTOS_REUSE_SRC') or os.path.join(
    _HARTOS, 'hartos', 'reuse_recipe.py')

_HELPER = '_tool_observation_summary'
_STEPS = '_vlm_recipe_steps'


def _src():
    return io.open(_SRC, encoding='utf-8', errors='replace').read()


def _func(name, src):
    m = re.search(r'^def %s\(.*\n(?:(?:[ \t].*)?\n)*' % re.escape(name), src, re.M)
    return m.group(0) if m else ''


class _HelperFunShim(object):
    """Stands in for hartos.helper, which cannot be imported without an app.

    Only ``format_action_text`` is reachable from the code under test, and
    only for ``next_action`` messages.  Identity is the honest stand-in: the
    assertions below are about whether the text SURVIVES to the return, not
    about how helper formats it.
    """

    @staticmethod
    def format_action_text(text):
        return text


def _load_helper():
    """exec the REAL helpers -- a local copy would prove nothing."""
    src = _src()
    body = _func(_STEPS, src) + '\n' + _func(_HELPER, src)
    if _STEPS not in body or _HELPER not in body:
        return None
    ns = {'helper_fun': _HelperFunShim}
    try:
        from core.constants import TOOL_OBSERVATION_MAX_CHARS
        ns['TOOL_OBSERVATION_MAX_CHARS'] = TOOL_OBSERVATION_MAX_CHARS
    except Exception:
        ns['TOOL_OBSERVATION_MAX_CHARS'] = 2000
    exec(compile(body, '<helpers>', 'exec'), ns)
    return ns[_HELPER]


def _response(*chunks):
    """The RPC response shape both success branches hold."""
    return {'status': 'success',
            'extracted_responses': [
                {'type': t, 'content': c} for t, c in chunks]}


# Verbatim shape of a real VLM account of the disk-space run.
_LIVE = _response(
    ('analysis', 'The system drive is C:. Running Get-PSDrive C to read free '
                 'space.'),
    ('analysis', 'Free space on C: reported as 9.17 GB.'),
)


class TestTheObservationSurvivesTheReturn(unittest.TestCase):
    """RED until the tool returns what it saw."""

    def setUp(self):
        self.fn = _load_helper()
        if self.fn is None:
            self.skipTest('%s/%s absent -- TestItIsWiredIntoBothSuccessReturns '
                          'is the assertion that fails for that'
                          % (_STEPS, _HELPER))

    def test_the_observed_text_is_returned(self):
        out = self.fn(_LIVE, 'check free disk space')
        self.assertIn(
            '9.17', out,
            "the figure the tool observed is missing from its own return. "
            "Live 2026-09-11 that is exactly why the agent answered 38.4 GB "
            "and 80 GB against a real 9.17 GB: the model was handed "
            "'Successfully ran the command in user's computer.' and nothing "
            "else, so it invented the number.")

    def test_technical_noise_is_stripped_the_same_way_the_recipe_strips_it(self):
        """One extraction means the file and the return agree by construction."""
        out = self.fn(_response(
            ('analysis', 'Free space on C: is 9.17 GB.\n'
                         'Box ID: 42\n'
                         'box_centroid_coordinate: (10, 20)\n'
                         'value: 7')), 'check free disk space')
        self.assertIn('9.17', out)
        for noise in ('Box ID:', 'box_centroid_coordinate:', 'value: 7'):
            self.assertNotIn(
                noise, out,
                'grounding coordinates reached the model; the recipe writer '
                'strips them and the return must not diverge from it')

    def test_it_is_bounded(self):
        """An unbounded tool result would blow the wire budget (#730/#732)."""
        from core.constants import TOOL_OBSERVATION_MAX_CHARS
        out = self.fn(_response(('analysis', 'x' * 50000)), 'anything')
        self.assertLessEqual(
            len(out), TOOL_OBSERVATION_MAX_CHARS + 200,
            'the observation is unbounded; a large screen dump would push the '
            'body past the slot budget the wire-trim work sized (n_ctx 12288, '
            '~6144/slot) and cost the whole turn')

    def test_nothing_observed_stays_empty(self):
        """Empty must stay empty so the caller keeps its plain sentence."""
        for junk in ({'status': 'success'},
                     {'status': 'success', 'extracted_responses': []},
                     None):
            self.assertEqual(
                self.fn(junk, ''), '',
                'an empty observation must not append a blank section')

    def test_it_never_raises(self):
        """It runs on the tool return path; it must not cost the result."""
        for junk in (None, 'nonsense', 42, {'extracted_responses': None},
                     {'extracted_responses': [None]},
                     {'extracted_responses': [{'type': 'analysis'}]},
                     {'extracted_responses': 'not-a-list'}):
            try:
                self.fn(junk, 'x')
            except Exception as err:
                self.fail('raised on %r: %r' % (junk, err))


class TestItIsWiredIntoBothSuccessReturns(unittest.TestCase):
    """The helper existing is worth nothing if a return does not use it.

    Two returns, two guards.  The first fix pinned only the VLM one and the
    branch that actually runs on a reuse walk went on discarding the
    observation for another whole drive.
    """

    def setUp(self):
        self.src = _src()

    def test_each_helper_is_defined_once(self):
        for name in (_HELPER, _STEPS):
            self.assertEqual(
                len(re.findall(r'^def %s\(' % name, self.src, re.M)), 1,
                '%s must have ONE definition; two copies of "what did the '
                'tool see" drift into two different answers' % name)

    def test_the_extraction_has_no_inline_twin(self):
        """The recipe writer and the summariser share ONE parse."""
        self.assertEqual(
            len(re.findall(r'def clean_text\(', self.src)), 1,
            'clean_text is defined more than once -- the extraction was '
            'copied back inline instead of calling %s' % _STEPS)
        self.assertGreaterEqual(
            len(re.findall(r'%s\(' % _STEPS, self.src)), 3,
            'expected the shared extraction to be CALLED by the recipe writer '
            'and by %s, not merely defined' % _HELPER)

    def test_the_vlm_success_return_carries_the_observation(self):
        m = re.search(r"Successfully ran the command in user\\?'s computer and "
                      r"created", self.src)
        self.assertIsNotNone(
            m, 'the VLM success return changed shape -- re-point this guard')
        window = self.src[max(0, m.start() - 1500):m.start() + 600]
        self.assertIn(
            _HELPER, window,
            'the VLM-path success return does not call %s, so the observation '
            'is still discarded between the tool and the model.' % _HELPER)

    def test_the_reuse_success_return_carries_the_observation(self):
        """THE branch a reuse walk takes -- measured 0 learn-branch entries.

        This is the assertion c7627df5a did not have, which is why a
        byte-verified deploy changed nothing the user could see.
        """
        m = re.search(
            r"return 'Successfully ran the command in user\\?'s computer\.'",
            self.src)
        self.assertIsNotNone(
            m, 'the plain success return changed shape -- re-point this guard')
        # Anchor to the ENCLOSING `if`, not to a fixed character count.  A
        # 2600-char lookback passed on the pre-fix source because it reached
        # the VLM branch's own call 11 lines above -- i.e. it would have gone
        # green with this branch still discarding the observation, which is
        # the exact failure this file exists to catch.  Between this `if` and
        # its return there is nowhere for the call to hide.
        guard = self.src.rfind(
            "if response and response['status'] == 'success':", 0, m.start())
        self.assertNotEqual(guard, -1, 'lost the enclosing success check')
        window = self.src[guard:m.start()]
        self.assertIn(
            _HELPER, window,
            'the plain-path success return does not call %s.  That is the '
            'branch every REUSE walk takes: a matching banked recipe (or a '
            'refused re-learning) skips the VLM branch entirely, so fixing '
            'only the VLM return leaves the reuse agent inventing its '
            'numbers.' % _HELPER)

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
