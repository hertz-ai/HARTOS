"""A consumer of the VLM loop must parse the types the loop actually emits.

THE DEFECT THIS PINS, measured 2026-09-11.

``run_local_agentic_loop`` appends exactly three message types and nothing
else — ``action`` (:707), ``completion`` (:671), ``error`` (:733) — and
returns them with an ``exit_reason`` on EVERY exit.  Three of the four
consumers parsed ``analysis`` and ``next_action``, which no producer in either
repo has ever emitted.  The only occurrence of ``"type": "analysis"`` anywhere
in HARTOS was a test fixture I wrote myself.

A filter that matches nothing fails SILENTLY: empty list, fall through to a
default, plausible string returned.  Nothing errors, so nothing is noticed.

WHAT IT COST — the recipes on disk, counted:

    vlm_agent files banked                                   : 106
    recipe is EXACTLY the instruction restated (the fallback) : 106
    recipe carrying a real observed step                      :   0

100% of banked VLM recipe steps were the instruction echoed back.  CREATE
never once stored what the VLM did.  On the reuse side the same fiction handed
the model its own request under the heading "What was observed on the
machine", which is worse than saying nothing — it reads as data.

WHY THE EARLIER GUARD DID NOT CATCH IT: I built its fixture from the code
under test instead of from the producer, so the test agreed with the fiction.
Every fixture below is copied from local_loop.py's own append sites.

THE DRIFT GUARD is the part that generalises.  It derives the producer's
vocabulary from local_loop.py at test time and fails if any consumer compares
a message type against a literal the producer cannot emit.  It does not care
what the types are called, so it keeps working when they change.
"""

import ast
import io
import os
import unittest

_HARTOS = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_LOOP = os.path.join(_HARTOS, 'integrations', 'vlm', 'local_loop.py')

# Every module that reads a VLM response.  Adding a consumer without adding it
# here is itself the drift this file exists to stop.
_CONSUMERS = [
    os.path.join(_HARTOS, 'integrations', 'vlm', 'response_view.py'),
    os.path.join(_HARTOS, 'hartos', 'reuse_recipe.py'),
    os.path.join(_HARTOS, 'hartos', 'create_recipe.py'),
    os.path.join(_HARTOS, 'hart_intelligence_entry.py'),
]


def _src(path):
    return io.open(path, encoding='utf-8', errors='replace').read()


def producer_types(loop_src=None):
    """The types local_loop APPENDS, read from local_loop itself.

    Derived, not declared: a new producer type must not need this test edited
    to be honoured by consumers.
    """
    tree = ast.parse(loop_src if loop_src is not None else _src(_LOOP))
    found = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        fn = node.func
        if not (isinstance(fn, ast.Attribute) and fn.attr == 'append'
                and isinstance(fn.value, ast.Name)
                and fn.value.id == 'extracted_responses'):
            continue
        for arg in node.args:
            if not isinstance(arg, ast.Dict):
                continue
            for k, v in zip(arg.keys, arg.values):
                if (isinstance(k, ast.Constant) and k.value == 'type'
                        and isinstance(v, ast.Constant)
                        and isinstance(v.value, str)):
                    found.add(v.value)
    return found


def _mentions(node, word):
    parts = {n.id for n in ast.walk(node) if isinstance(n, ast.Name)} | {
        n.attr for n in ast.walk(node) if isinstance(n, ast.Attribute)} | {
        c.value for c in ast.walk(node)
        if isinstance(c, ast.Constant) and isinstance(c.value, str)}
    return any(word in str(p).lower() for p in parts)


def _type_literals_in(subtree):
    """String literals compared against something named ``*type*``."""
    found = set()
    for node in ast.walk(subtree):
        if not isinstance(node, ast.Compare) or not _mentions(node.left, 'type'):
            continue
        for op, comp in zip(node.ops, node.comparators):
            if not isinstance(op, (ast.Eq, ast.In)):
                continue
            if isinstance(comp, ast.Constant) and isinstance(comp.value, str):
                found.add(comp.value)
            elif isinstance(comp, (ast.Tuple, ast.List, ast.Set)):
                for e in comp.elts:
                    if isinstance(e, ast.Constant) and isinstance(e.value, str):
                        found.add(e.value)
    return found


def consumed_types(src):
    """Message types a module checks WHILE WALKING ``extracted_responses``.

    Scoped to the loop deliberately.  An earlier draft scanned whole files and
    flagged `type == 'whatsapp'` and `type == 'pdf'` in
    hart_intelligence_entry — unrelated comparisons that would have made this
    guard cry wolf until someone silenced it.  A noisy guard is a disabled
    guard.  Inside a loop over extracted_responses, every type literal IS a
    verdict about a VLM message.
    """
    tree = ast.parse(src)
    found = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.For) and _mentions(node.iter, 'extracted_responses'):
            found |= _type_literals_in(node)
    return found


# ── fixtures copied from local_loop.py's own append sites ──────────────────
def _action(act='shell', reasoning='Query free space with Get-PSDrive.',
            result='Free (GB)\n---------\n    11.10', ok=True, it=1):
    """Verbatim shape of local_loop.py:707-718."""
    return {'type': 'action', 'iteration': it,
            'content': {'action': act, 'reasoning': reasoning,
                        'result': result, 'ok': ok, 'coordinate': None,
                        '_strategy': 'inline_prompt'}}


def _completion(text='C: has 11.10 GB free.', it=2):
    """Verbatim shape of local_loop.py:671-675."""
    return {'type': 'completion', 'content': text, 'iteration': it}


def _error(text='pyautogui failed', it=3):
    """Verbatim shape of local_loop.py:733-737."""
    return {'type': 'error', 'content': text, 'iteration': it}


def _response(*msgs, **kw):
    return {'status': kw.get('status', 'success'),
            'exit_reason': kw.get('exit_reason', 'done'),
            'extracted_responses': list(msgs),
            'execution_time_seconds': kw.get('secs', 12.0)}


class TestTheProducersOwnOutputIsRead(unittest.TestCase):
    """RED until a consumer parses `action` / `completion` / `error`."""

    def setUp(self):
        from integrations.vlm import response_view as rv
        self.rv = rv

    def test_the_command_and_the_reasoning_both_survive(self):
        out = self.rv.observation_text(_response(_action()))
        self.assertIn('shell', out, 'the command fired was dropped')
        self.assertIn('Get-PSDrive', out, 'the reasoning was dropped')
        self.assertIn('11.10', out, "the action's own output was dropped")

    def test_a_completion_is_read(self):
        self.assertIn('11.10', self.rv.observation_text(_response(_completion())))

    def test_an_error_is_read_and_marked_not_ok(self):
        recs = self.rv.observed_records(_response(_error(), exit_reason='action_error'))
        self.assertTrue(recs, 'the error record was dropped entirely')
        self.assertFalse(recs[-1]['ok'])
        self.assertIn('pyautogui failed', recs[-1]['text'])

    def test_the_record_is_kept_on_every_exit_reason_not_just_success(self):
        """The producer collects on every exit; the reader must too."""
        for reason in ('max_iterations', 'timeout', 'stopped', 'action_error'):
            out = self.rv.observation_text(
                _response(_action(), status='incomplete', exit_reason=reason))
            self.assertIn(
                'shell', out,
                'exit_reason=%s lost the commands that DID run — the failure '
                'case is where knowing what was attempted matters most'
                % reason)

    def test_the_outcome_is_described_honestly_per_exit_reason(self):
        self.assertIn('Completed', self.rv.outcome_summary(_response(_action())))
        for reason, word in (('timeout', 'time'),
                             ('max_iterations', 'without reaching'),
                             ('action_error', 'stopped'),
                             ('stopped', 'your request')):
            msg = self.rv.outcome_summary(
                _response(_action(), status='incomplete', exit_reason=reason))
            self.assertIn(word, msg,
                          'exit_reason=%s is not described honestly' % reason)

    def test_recipe_steps_bank_what_happened_not_what_was_asked(self):
        """The 106/106 defect, as an assertion."""
        instruction = 'Check how much free disk space this computer has'
        steps = self.rv.recipe_steps(_response(_action(), _completion()),
                                     instruction)
        texts = [s['steps'] for s in steps]
        self.assertNotEqual(
            texts, [instruction],
            'the banked recipe is the instruction restated — this is exactly '
            'the fallback that fired on 106 of 106 recipes on disk')
        self.assertTrue(any('shell' in t for t in texts),
                        'the command fired never reached the recipe')

    def test_the_fallback_still_covers_a_genuinely_empty_run(self):
        steps = self.rv.recipe_steps(_response(), 'do the thing')
        self.assertEqual([s['steps'] for s in steps], ['do the thing'])

    def test_it_is_bounded(self):
        from core.constants import TOOL_OBSERVATION_MAX_CHARS
        huge = _response(_action(result='x' * 50000))
        self.assertLessEqual(
            len(self.rv.observation_text(huge)),
            TOOL_OBSERVATION_MAX_CHARS + 200,
            'an unbounded screen dump spends the slot it is informing')

    def test_it_never_raises(self):
        for junk in (None, {}, 'nonsense', 42,
                     {'extracted_responses': None},
                     {'extracted_responses': 'not-a-list'},
                     {'extracted_responses': [None, 7, 'x']},
                     {'extracted_responses': [{'type': 'action'}]},
                     {'extracted_responses': [{'type': 'action',
                                               'content': 'not-a-dict'}]}):
            for fn in (self.rv.observed_records, self.rv.observation_text,
                       self.rv.outcome_summary):
                try:
                    fn(junk)
                except Exception as err:
                    self.fail('%s raised on %r: %r' % (fn.__name__, junk, err))
            try:
                self.rv.recipe_steps(junk, 'fallback')
            except Exception as err:
                self.fail('recipe_steps raised on %r: %r' % (junk, err))


class TestNoConsumerParsesATypeTheLoopCannotEmit(unittest.TestCase):
    """The drift guard — the part that generalises beyond this bug."""

    def test_the_producer_vocabulary_is_discoverable(self):
        types = producer_types()
        self.assertTrue(
            types,
            'could not derive the producer vocabulary from local_loop.py — '
            're-point this guard before trusting any result below')
        for t in ('action', 'completion', 'error'):
            self.assertIn(t, types)

    def test_the_guard_itself_catches_a_known_bad_consumer(self):
        """A guard that cannot fail is not a guard.

        The fixture is the real defect verbatim: the filter INSIDE the loop
        over extracted_responses, which is the only place a type literal is a
        verdict about a VLM message.
        """
        bad = ("for msg in response['extracted_responses']:\n"
               "    msg_type = msg.get('type', '')\n"
               "    if msg_type == 'analysis':\n"
               "        pass\n")
        self.assertIn('analysis', consumed_types(bad),
                      'the detector cannot see the very defect it exists for')

    def test_no_consumer_compares_against_a_phantom_type(self):
        emitted = producer_types()
        offenders = []
        for path in _CONSUMERS:
            if not os.path.exists(path):
                continue
            for t in sorted(consumed_types(_src(path))):
                # Only judge literals that look like a message-type verdict;
                # a module comparing type=='str' is doing something else.
                if t in emitted or t in ('', 'str', 'dict', 'list', 'unknown'):
                    continue
                offenders.append('%s parses %r' % (os.path.basename(path), t))
        self.assertEqual(
            offenders, [],
            'these consumers parse a VLM message type the loop never emits, '
            'so they match nothing and fall through to a default SILENTLY. '
            'Producer emits %s. Offenders: %s' % (sorted(emitted), offenders))


class TestTheParseHasOneHome(unittest.TestCase):

    def test_extracted_responses_is_parsed_in_exactly_one_module(self):
        """Two parses of 'what did the tool see' drift into two answers."""
        parsers = []
        for path in _CONSUMERS:
            if not os.path.exists(path):
                continue
            src = _src(path)
            # a parse = iterating the list, not merely naming it
            if ("for msg in" in src or "for resp in" in src) and \
                    "extracted_responses" in src:
                tree = ast.parse(src)
                for node in ast.walk(tree):
                    # ONE extractor, shared with consumed_types.  A second
                    # one here silently missed response_view's own
                    # `.get('extracted_responses')` because it ignored string
                    # constants — two implementations of "does this loop walk
                    # the responses", disagreeing, inside the test that
                    # exists to stop exactly that.
                    if isinstance(node, ast.For) and _mentions(
                            node.iter, 'extracted_responses'):
                        parsers.append(os.path.basename(path))
                        break
        self.assertEqual(
            sorted(set(parsers)), ['response_view.py'],
            'extracted_responses is iterated outside response_view.py. One '
            'shape, one reader — a second parse is how analysis/next_action '
            'survived for months. Found: %s' % sorted(set(parsers)))


if __name__ == '__main__':
    unittest.main()
