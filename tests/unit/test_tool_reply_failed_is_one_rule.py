"""One rule for "this tool reply means the call failed".

Review of dd46b4da0: the CREATE trace banker
(hartos/create_recipe.py::_bank_action_recipe_from_trace) judged a tool
reply by its own prefix list, ('Error:', 'Tool execution failed:'), and never
looked at core.constants.TOOL_FAILURE_RESULTS -- the strings CREATE's own
execute_windows_or_android_command returns when it refuses (operator gate,
computer-control consent, a VLM loop that could not do the work).  The
reviewer's probe: a call answered with

    TOOL_FAILURE_RESULTS[0] + "\\nComputer control consent refused"

was banked as a successful step, so a desktop action the user never consented
to became part of the recipe and REUSE would replay it.

The REUSE side had the mirror gap: its three readers (_reuse_fabricated_tools,
_reuse_completion_evidence, _reuse_own_tool_progress) each restated
`any(f in body for f in TOOL_FAILURE_RESULTS)` and none knew the
core.tool_logging error envelope or the executor's "Error: ..." reply, so a
tool that RAISED counted as having done the action's work.

Now both sides call core.constants.tool_reply_failed.  These tests import
and call the real banker and the real REUSE gate; the source guard at the end
keeps the rule at one.
"""
import ast
import os
import sys

import pytest

_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from core.constants import (  # noqa: E402
    TOOL_FAILURE_RESULTS, tool_reply_failed)
from core.tool_logging import _error_envelope  # noqa: E402
from tests.unit.test_trace_action_banking import banked, _code_run  # noqa: E402,F401

_TOOL = 'execute_windows_or_android_command'
_CONSENT_REFUSED = TOOL_FAILURE_RESULTS[0] + '\nComputer control consent refused'


def _one_call(reply, call_id='v', name=_TOOL):
    return [
        {'content': '', 'tool_calls': [{'id': call_id, 'function': {
            'name': name, 'arguments': '{"instructions": "open dashboard"}'}}]},
        {'content': reply, 'role': 'tool', 'tool_responses': [
            {'tool_call_id': call_id, 'role': 'tool', 'content': reply}]},
    ]


# ---------------------------------------------------------------------------
# The predicate, fed each producer's REAL output.
# ---------------------------------------------------------------------------
class TestPredicate:
    def test_the_tool_logging_envelope_is_a_failure(self):
        assert tool_reply_failed(_error_envelope('google_search',
                                                 RuntimeError('quota')))

    def test_every_canonical_refusal_is_a_failure_with_a_reason_appended(self):
        # create_recipe and reuse_recipe both APPEND the reason on a newline.
        for s in TOOL_FAILURE_RESULTS:
            assert tool_reply_failed(s)
            assert tool_reply_failed(f'{s}\nComputer control consent refused')

    def test_the_executors_argument_refusal_is_a_failure(self):
        from hartos.helper import tool_argument_error

        def google_search(query):
            return query
        text = tool_argument_error(google_search, 'google_search',
                                   {'q': 'gpu'}, repaired=False)
        assert text and tool_reply_failed(text)

    def test_the_executors_unknown_function_reply_is_a_failure(self):
        # hartos/helper.py enhanced_execute_function, verbatim shape.
        assert tool_reply_failed('Error: Function save_data_in_memory not found.')

    @pytest.mark.parametrize('reply', [
        None, '', 'three results',
        "Successfully ran the command in user's computer.",
        # A real result that merely MENTIONS an error is still a result.
        'Top result: "Error: out of memory" explained in 5 steps',
        '{"status": "ok"}',
    ])
    def test_a_real_result_is_not_a_failure(self, reply):
        assert not tool_reply_failed(reply)

    def test_leading_whitespace_does_not_hide_a_failure(self):
        assert tool_reply_failed('\n  Error: Function x not found.')


# ---------------------------------------------------------------------------
# CREATE: the real trace banker.
# ---------------------------------------------------------------------------
class TestBankerUsesTheCanonicalRule:
    def test_a_consent_refused_desktop_call_is_not_banked(self, banked):
        """The reviewer's probe, as an assertion."""
        ok, data, _ = banked([{'content': 'Execute Action 2: open the dashboard'}]
                             + _one_call(_CONSENT_REFUSED))
        assert ok is True
        assert [s['tool_name'] for s in data['recipe']] == [''], data['recipe']
        assert 'no-op' in data['recipe'][0]['steps']

    def test_the_companion_app_refusal_is_not_banked(self, banked):
        ok, data, _ = banked([{'content': 'Execute Action 2: open the dashboard'}]
                             + _one_call(TOOL_FAILURE_RESULTS[1]))
        assert ok is True
        assert _TOOL not in [s['tool_name'] for s in data['recipe']]

    def test_one_block_run_twice_is_banked_once(self, banked):
        """Measured with the real autogen 0.2.37 executor configured as CREATE
        configures it (last_n_messages=2, no docker), a side-effect counter in
        the block: the Executor selected twice in a row ran the ONE authored
        block twice (runs=2), and the Assistant, which also executes code in
        CREATE, ran it a third time.  That is the executor re-scanning the
        same message, not new work, so the recipe carries the block once;
        REUSE replaying it twice would repeat its side effects."""
        again = [{'content': 'exitcode: 0 (execution succeeded)\nCode output: \nx\n',
                  'role': 'user', 'name': 'Executor'}]
        ok, data, _ = banked(
            [{'content': 'Execute Action 2: compute'}] + _code_run() + again + again)
        assert ok is True
        assert len(data['recipe']) == 1, data['recipe']
        assert 'hashlib' in data['recipe'][0]['generalized_functions']

    def test_two_blocks_each_run_are_both_banked(self, banked):
        """Control for the dedupe: it keys on the authored message, so two
        different blocks that both ran stay two steps, in order."""
        second = "```python\nprint('second')\n```"
        ok, data, _ = banked(
            [{'content': 'Execute Action 2: compute'}]
            + _code_run() + _code_run(code=second))
        assert ok is True
        code = [s['generalized_functions'] for s in data['recipe']]
        assert len(code) == 2 and 'hashlib' in code[0] and 'second' in code[1], code

    def test_a_desktop_call_that_worked_is_still_banked(self, banked):
        """Control: the fix must not drop real work."""
        ok, data, _ = banked(
            [{'content': 'Execute Action 2: open the dashboard'}]
            + _one_call("Successfully ran the command in user's computer."))
        assert ok is True
        assert [s['tool_name'] for s in data['recipe']] == [_TOOL]


# ---------------------------------------------------------------------------
# REUSE: the real fabrication gate and its two sibling readers.
# ---------------------------------------------------------------------------
class _Task:
    def __init__(self, text):
        self._text = text
        self.evidence_seen_call_ids = set()

    def get_action(self, _idx):
        return self._text


class _Agent:
    def __init__(self, name, tools):
        self.name = name
        self._function_map = {t: (lambda: None) for t in tools}
        self.llm_config = {'tools': [{'function': {'name': t}} for t in tools]}
        self._oai_messages = {}


class _GC:
    def __init__(self, messages, agents):
        self.messages = messages
        self.agents = agents


_ACTION = f'Action #1: Use {_TOOL} to open the settings window.'
_DISPATCH = {'role': 'user', 'name': 'ChatInstructor',
             'content': f'Perform this action -> Action #1: {_ACTION}'}


def _reuse_chat(reply):
    proposal = {'role': 'assistant', 'name': 'Helper', 'content': None,
                'tool_calls': [{'id': 'c1', 'type': 'function',
                                'function': {'name': _TOOL, 'arguments': '{}'}}]}
    result = {'role': 'tool', 'name': 'Assistant', 'content': reply,
              'tool_responses': [{'tool_call_id': 'c1', 'role': 'tool',
                                  'content': reply}]}
    agents = [_Agent('Helper', [_TOOL]), _Agent('Assistant', [])]
    return _GC([_DISPATCH, proposal, result], agents)


_RAISED = _error_envelope(_TOOL, RuntimeError('display unavailable'))


@pytest.fixture()
def rr():
    from hartos import reuse_recipe
    key = 'tool_reply_failed_u1'
    saved = reuse_recipe.user_tasks.get(key)
    reuse_recipe.user_tasks[key] = _Task(_ACTION)
    yield reuse_recipe, key
    if saved is None:
        reuse_recipe.user_tasks.pop(key, None)
    else:
        reuse_recipe.user_tasks[key] = saved


class TestReuseGateUsesTheCanonicalRule:
    @pytest.mark.parametrize('reply', [
        _RAISED, 'Error: Function execute_windows_or_android_command not found.',
        _CONSENT_REFUSED])
    def test_a_failed_reply_leaves_the_tool_unrun(self, rr, reply):
        mod, key = rr
        gc = _reuse_chat(reply)
        assert mod._reuse_fabricated_tools(key, 1, gc, gc.agents) == [_TOOL]

    @pytest.mark.parametrize('reply', [_RAISED, _CONSENT_REFUSED])
    def test_a_failed_reply_is_no_completion_receipt(self, rr, reply):
        mod, key = rr
        assert mod._reuse_completion_evidence(key, 1, _reuse_chat(reply)) is None

    @pytest.mark.parametrize('reply', [_RAISED, _CONSENT_REFUSED])
    def test_a_failed_reply_is_no_own_tool_progress(self, rr, reply):
        mod, key = rr
        gc = _reuse_chat(reply)
        assert mod._reuse_own_tool_progress(key, 1, gc, gc.agents) == 0

    def test_a_real_result_still_clears_all_three(self, rr):
        """Control: fail-open on a genuine result must survive."""
        mod, key = rr
        gc = _reuse_chat('Settings opened.')
        assert mod._reuse_fabricated_tools(key, 1, gc, gc.agents) == []
        assert mod._reuse_completion_evidence(key, 1, gc) is not None
        assert mod._reuse_own_tool_progress(key, 1, gc, gc.agents) == 1


# ---------------------------------------------------------------------------
# The guard that keeps it at one.
# ---------------------------------------------------------------------------
_CANONICAL = os.path.join('core', 'constants.py')
_SCANNED = ('core', 'hartos', 'integrations', 'security')
_ENVELOPE_WORDS = 'Tool execution failed:'


def _py_files():
    for top in _SCANNED:
        for d, dirs, files in os.walk(os.path.join(_ROOT, top)):
            dirs[:] = [x for x in dirs if x not in ('__pycache__',)]
            for f in files:
                if f.endswith('.py'):
                    yield os.path.join(d, f)


def _is_error_prefix(node):
    return (isinstance(node, ast.Constant) and isinstance(node.value, str)
            and node.value.strip() == 'Error:')


def _second_rules(path):
    """Each inline restatement of the failure rule in one file."""
    rel = os.path.relpath(path, _ROOT)
    if rel == _CANONICAL:
        return []
    try:
        tree = ast.parse(open(path, encoding='utf-8').read())
    except (SyntaxError, UnicodeDecodeError):
        return []
    found = []
    for node in ast.walk(tree):
        # (a) the tool_logging envelope's words, spelled anywhere but home.
        if (isinstance(node, ast.Constant) and isinstance(node.value, str)
                and node.value.lstrip().startswith(_ENVELOPE_WORDS)):
            found.append(f'{rel}:{node.lineno} envelope literal')
        # (b) a prefix list holding 'Error:' (the shape dd46b4da0 added).
        if (isinstance(node, (ast.Tuple, ast.List, ast.Set))
                and any(_is_error_prefix(e) for e in node.elts)):
            found.append(f'{rel}:{node.lineno} failure-prefix list')
        # (c) .startswith('Error:') on a reply.
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and node.func.attr == 'startswith'
                and any(_is_error_prefix(a) for a in node.args)):
            found.append(f'{rel}:{node.lineno} startswith Error:')
        # (d) iterating TOOL_FAILURE_RESULTS to test a reply.
        if isinstance(node, ast.comprehension):
            it = node.iter
            nm = getattr(it, 'id', None) or getattr(it, 'attr', None)
            if nm == 'TOOL_FAILURE_RESULTS':
                found.append(f'{rel}:{it.lineno} loops TOOL_FAILURE_RESULTS')
    return found


def test_source_guard_one_tool_failure_rule():
    """DRY guard, not the behaviour test (that is everything above).

    A second inline "is this reply a failure" rule is how CREATE and REUSE
    drifted apart in dd46b4da0.  Any new one fails here; route it through
    core.constants.tool_reply_failed instead.
    """
    hits = [h for p in _py_files() for h in _second_rules(p)]
    assert hits == [], hits


def test_source_guard_catches_what_it_guards(tmp_path):
    """The guard must be able to fail: each forbidden shape is detected."""
    probe = tmp_path / 'probe.py'
    probe.write_text(
        "P = ('Error:', 'x')\n"
        "a = s.startswith('Error:')\n"
        "b = any(f in s for f in TOOL_FAILURE_RESULTS)\n"
        "c = 'Tool execution failed: {}'\n", encoding='utf-8')
    kinds = [h.split(' ', 1)[1] for h in _second_rules(str(probe))]
    assert sorted(kinds) == sorted([
        'failure-prefix list', 'startswith Error:',
        'loops TOOL_FAILURE_RESULTS', 'envelope literal']), kinds
