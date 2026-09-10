"""The synthesis steer must never be handed to the user as the answer.

MEASURED LIVE 2026-09-10 14:30:55, agent 92583386981 ("English Learning
Session"), driven as its owner through the real /chat route.  HTTP 200 in
103.1s, and the WHOLE reply the user read was:

    "The actions are finished and their tools have already run - do NOT run
     any tool again and do NOT emit another status object. Write the ANSWER
     for the user now, in your own words, ... Address your reply to @user,
     and send one JSON object whose only key is message2userfinal ..."

That is ``_REUSE_SYNTHESIS_STEER`` verbatim -- this module's own instruction,
delivered as the agent's lesson.

WHY IT REACHED THE USER, measured three ways:

  1. The steer never reached a model in that run.  ``llm_outbound.jsonl``
     holds exactly 12 calls for request_id walk-92583386981-143055, the last
     at 14:32:38,150 -- BEFORE the steer was posted at 14:32:38,907 -- and
     none of the 672 records in that file carries the steer text for this
     session.  ``[SYNTHESIS] steer failed`` never appears either, so
     initiate_chat returned NORMALLY having produced no model turn
     (20 -> 21 messages in 109 ms: only the steer landed).

  2. So ``group_chat.messages[-1]`` WAS the steer, and get_agent_response
     takes that tail with no test of who wrote it.

  3. The gate agreed with it.  ``_reuse_message_is_user_answer`` tests
     ``'message2userfinal' in low`` BEFORE it asks whether this module wrote
     the text -- and the steer NAMES that key, because
     ``_REUSE_SYNTHESIS_ANSWER_SHAPE`` instructs the model to use it.
     retrieve_json finds no dict in prose, so the branch fell through to
     ``return True`` ("the answer is already there").  That is why the run
     logged the reassuring ``still control JSON: False``.

THE HOLE IS THE ONE ITS OWN DOCSTRING NAMES.  ``_reuse_is_pipeline_text``
says it is "ONE implementation, two callers ... sharing only the constant is
what let one be fixed while the other kept the hole".  There is a THIRD
reader -- get_agent_response's tail pick -- that never consults it, and the
predicate did not know the two synthesis steers at all.

    python -m pytest tests/unit/test_reuse_never_delivers_its_own_steer.py \
        --noconftest -q
"""
import ast
import io
import json
import os

MODULE = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    'hartos', 'reuse_recipe.py')

# Verbatim from the 14:30:55 reply body.
LIVE_REPLY = (
    'The actions are finished and their tools have already run — do NOT '
    'run any tool again and do NOT emit another status object. Write the '
    'ANSWER for the user now, in your own words, using the real tool results '
    'from this conversation. Address your reply to @user, and send one JSON '
    'object whose only key is message2userfinal and whose value is the '
    'answer itself, written out as sentences the user will read. Substitute '
    'the real text — a placeholder, an empty value, or anything in '
    'angle brackets is not an answer.')

_WANTED_FUNCS = ('_reuse_is_pipeline_text', '_reuse_is_written_answer',
                 '_reuse_message_is_user_answer')


def _src():
    with io.open(MODULE, encoding='utf-8', errors='replace') as fh:
        return fh.read()


def _ns():
    """Compile the REAL predicates out of the shipped file.

    Importing hartos.reuse_recipe pulls autogen -> llmlingua -> torch, an
    OSError at collection time on this workstation, so the functions under
    test are exec'd straight from the source tree.  These are the bytes
    production runs, not a restatement of them.

    ``retrieve_json`` is stubbed with the only behaviour these paths depend
    on: a dict when the text really parses as a JSON object, None otherwise.
    The live steer carries no braces at all (its constant's own comment
    records that, so ``.format(unrun=...)`` stays safe), so the real parser
    and this stub agree on it.
    """
    tree = ast.parse(_src())
    body = []
    for node in tree.body:
        if isinstance(node, ast.Assign):
            for t in node.targets:
                if isinstance(t, ast.Name) and t.id.startswith('_REUSE_'):
                    body.append(node)
                    break
        elif isinstance(node, ast.FunctionDef) and node.name in _WANTED_FUNCS:
            body.append(node)

    def retrieve_json(content):
        try:
            v = json.loads(str(content or ''))
        except Exception:
            return None
        return v if isinstance(v, dict) else None

    ns = {'retrieve_json': retrieve_json}
    mod = ast.Module(body=body, type_ignores=[])
    exec(compile(ast.fix_missing_locations(mod), MODULE, 'exec'), ns)
    for name in _WANTED_FUNCS:
        assert name in ns, '%s not found in reuse_recipe.py' % name
    return ns


def _fn(tree, name):
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    raise AssertionError('%s not found' % name)


class TestThePredicateKnowsTheSynthesisSteers:
    """Both steers carry ``_REUSE_SYNTHESIS_ANSWER_SHAPE`` -- one marker."""

    def test_the_live_reply_is_recognised_as_pipeline_text(self):
        ns = _ns()
        assert ns['_reuse_is_pipeline_text'](LIVE_REPLY), (
            'the text the user actually read on 2026-09-10 14:30:55 is this '
            'module\'s own _REUSE_SYNTHESIS_STEER, but the predicate whose '
            'whole job is "did this module write this" does not recognise it')

    def test_complete_steer_is_pipeline_text(self):
        ns = _ns()
        assert ns['_reuse_is_pipeline_text'](ns['_REUSE_SYNTHESIS_STEER'])

    def test_incomplete_steer_is_pipeline_text_after_format(self):
        """The sibling steer is `.format(unrun=...)`ed before it is posted."""
        ns = _ns()
        posted = ns['_REUSE_SYNTHESIS_STEER_INCOMPLETE'].format(
            unrun='get_chat_history, send_message_to_user')
        assert ns['_reuse_is_pipeline_text'](posted)


class TestTheGateRefusesIt:
    """The message-shape test must not call a steer an answer."""

    def test_the_live_reply_is_not_a_user_answer(self):
        ns = _ns()
        msg = {'content': LIVE_REPLY, 'name': 'ChatInstructor', 'role': 'user'}
        assert not ns['_reuse_message_is_user_answer'](msg), (
            'the steer was classified as an answer because it NAMES '
            'message2userfinal and retrieve_json finds no dict in prose, so '
            'the branch fell through to `return True` -- which is why the '
            'run logged "still control JSON: False" and the tail was '
            'delivered')

    def test_provenance_is_checked_before_the_answer_key(self):
        """Ordering, not just membership: the key-mention branch returns
        True on unparseable text, so a later check can never be reached."""
        fn = _fn(ast.parse(_src()), '_reuse_message_is_user_answer')
        pipeline_line = key_line = None
        for node in ast.walk(fn):
            if (isinstance(node, ast.Call)
                    and getattr(node.func, 'id', '') == '_reuse_is_pipeline_text'
                    and pipeline_line is None):
                pipeline_line = node.lineno
            if (isinstance(node, ast.Constant)
                    and node.value == 'message2userfinal'
                    and key_line is None):
                key_line = node.lineno
        assert pipeline_line and key_line
        assert pipeline_line < key_line, (
            'the "did this module write it" check sits at line %d, BELOW the '
            'message2userfinal branch at line %d. That branch returns True '
            'for any unparseable text mentioning the key -- and every '
            'synthesis steer mentions it by construction -- so the check '
            'below can never run.' % (pipeline_line, key_line))


class TestTheExtractorAsks:
    """get_agent_response must not deliver a tail this module wrote."""

    def test_tail_pick_consults_the_answer_predicate(self):
        fn = _fn(ast.parse(_src()), 'get_agent_response')
        called = {getattr(n.func, 'id', '') for n in ast.walk(fn)
                  if isinstance(n, ast.Call)}
        assert '_reuse_synthesis_turn' in called, (
            'anchor lost: this guard is pinned to the post-loop extractor')
        assert '_reuse_message_is_user_answer' in called, (
            'get_agent_response takes group_chat.messages[-1] straight after '
            '_reuse_synthesis_turn with no test of who wrote it. When the '
            'synthesis round produces no model turn the tail IS the steer, '
            'and it goes to the user verbatim (measured 2026-09-10 14:30:55).')


class TestNoRegression:
    """Refuse only the module's own text -- never the agent's."""

    def test_a_real_answer_still_passes(self):
        ns = _ns()
        msg = {'content': json.dumps(
            {'message2userfinal': 'You are at B1. Today we work on the '
                                  'present perfect.'}),
               'name': 'Assistant', 'role': 'assistant'}
        assert ns['_reuse_message_is_user_answer'](msg)

    def test_an_honest_failure_report_still_reaches_the_user(self):
        ns = _ns()
        honest = ('Since no specific text was provided in your input, I could '
                  'not summarize anything.')
        assert not ns['_reuse_is_pipeline_text'](honest)
        assert ns['_reuse_message_is_user_answer'](
            {'content': honest, 'name': 'Assistant', 'role': 'assistant'})

    def test_a_lesson_that_mentions_the_word_answer_is_not_refused(self):
        ns = _ns()
        lesson = ('Here is your answer: the present perfect joins "have" to '
                  'a past participle.')
        assert not ns['_reuse_is_pipeline_text'](lesson)

    def test_the_four_older_steers_are_still_refused(self):
        """No marker was lost while adding the synthesis one."""
        ns = _ns()
        for name in ('_REUSE_ACTION_MESSAGE_PREFIX', '_REUSE_AUTONOMY_NUDGE',
                     '_REUSE_UNDER_REPORT_STEER', '_REUSE_SUBTASK_STEER_PREFIX'):
            assert ns['_reuse_is_pipeline_text'](ns[name]), name
