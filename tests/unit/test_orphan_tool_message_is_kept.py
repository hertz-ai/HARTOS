"""STEP 1's repair-then-exclude is DELIBERATE. Do not "fix" the slice.

This file exists because I got it wrong on 2026-09-06 and shipped a fix
(1253a8715, reverted) for a defect that was not there.  It now pins the
actual contract so the same mistake is harder to repeat.

``ToolMessageHandler.apply_transform`` STEP 1 handles a leading
``role=tool`` message — an orphan, invalid in the OpenAI schema because
nothing precedes it to answer.  It becomes one when the wire left-trim
eats the conversation from the front and splits a tool_call/tool-response
pair (measured live: the response migrates 4 -> 1 -> 0 -> 0 -> 0 across
snapshots as the trim advances).

    processed_messages = messages.copy()          # SHALLOW
    if processed_messages[0]['role'] == 'tool':
        processed_messages[0]['role'] = 'user'    # mutates the CALLER's dict
        processed_messages[0]['name'] = 'Helper'
        del processed_messages[0]['tool_call_id']
        processed_messages = processed_messages[1:]   # excludes it HERE only

**WHY THE SLICE IS NOT A BUG.**  ``list.copy()`` is shallow, so the three
repair lines mutate the dict objects the CALLER still holds.  The orphan
is permanently repaired in the shared history; the slice only withholds
it from THIS call's output, because on this pass it sits illegally at
index 0.  On the next pass it is already ``role=user`` and flows through
normally.  The repair is not dead code — its effect travels by aliasing,
not by return value.

**Live proof it works** (2026-09-06): 90 ``role=user, name=Helper``
messages across the day's wire bodies, carrying tool output such as
``"Error: Function send_message_to_user not found."`` — tool results that
STEP 1 demoted and that then reached the model as ordinary context.

**What my wrong fix did.**  I deleted the slice believing the repair was
dead code and the tool output destroyed.  Both premises were false.  The
change made the message appear one call earlier than designed, on a path
whose whole point is to withhold a schema-invalid message for exactly one
pass.  Caught by the owner's instruction to understand why code exists
before changing it.

    python -m pytest tests/unit/test_orphan_tool_message_is_kept.py --noconftest -q
"""
import os
import re

import pytest


_SRC = os.path.join(os.path.dirname(__file__), '..', '..', 'hartos', 'helper.py')


def _step1_code():
    """STEP 1's executable lines (comments stripped — they quote the code)."""
    with open(_SRC, encoding='utf-8') as fh:
        src = fh.read()
    m = re.search(r"# STEP 1: Handle first message if it's a tool message.*?\n(?=\s*# STEP 2)",
                  src, re.DOTALL)
    assert m, 'STEP 1 block not found'
    return '\n'.join(ln for ln in m.group(0).splitlines()
                     if not ln.lstrip().startswith('#'))


class TestTheAliasingContract:
    """The mechanism that makes repair-then-exclude correct."""

    def test_shallow_copy_so_the_repair_reaches_the_caller(self):
        """If this ever becomes a deep copy, STEP 1 silently loses the repair.

        The copy is established just ABOVE the `# STEP 1` comment, so this
        reads the enclosing method rather than the STEP 1 block.
        """
        with open(_SRC, encoding='utf-8') as fh:
            src = fh.read()
        m = re.search(r"def apply_transform\(self.*?\n(?=\s{4}def |\nclass )",
                      src, re.DOTALL)
        assert m, 'apply_transform not found'
        body = m.group(0)
        assert 'processed_messages = messages.copy()' in body, (
            'STEP 1 relies on a SHALLOW copy: the repair travels to the caller '
            'by mutating shared dicts. Change this and the three repair lines '
            'become genuinely dead and every orphaned tool result is lost.')
        assert 'deepcopy' not in body, (
            'a deep copy would break the aliasing the repair depends on')

    def test_aliasing_actually_behaves_that_way(self):
        """Pin the Python semantics the design rests on."""
        caller = [{'role': 'tool', 'name': 'A', 'tool_call_id': 'X',
                   'content': 'SEARCH RESULT'}]
        work = caller.copy()
        work[0]['role'] = 'user'
        work[0]['name'] = 'Helper'
        del work[0]['tool_call_id']
        work = work[1:]
        assert caller[0]['role'] == 'user', 'repair must persist to the caller'
        assert caller[0]['content'] == 'SEARCH RESULT', 'content must survive'
        assert 'tool_call_id' not in caller[0], 'dangling id must be dropped'
        assert work == [], 'and be withheld from THIS call only'


class TestStep1StaysIntact:
    """Guard every piece. Removing any one of them is a regression."""

    def test_repair_lines_present(self):
        code = _step1_code()
        assert "'role'] = 'user'" in code, 'orphan must be demoted to user'
        assert "'name'] = 'Helper'" in code, 'orphan must be renamed'
        assert "del processed_messages[0]['tool_call_id']" in code, (
            'the dangling tool_call_id references a trimmed-away assistant '
            'tool_call and must be dropped')

    def test_slice_present(self):
        """The line I wrongly deleted. It is load-bearing."""
        code = _step1_code()
        assert 'processed_messages[1:]' in code, (
            'STEP 1 must still withhold the repaired orphan from THIS call. '
            'A leading role=tool is schema-invalid, and the repair already '
            'reached the caller by aliasing, so the message is not lost — it '
            'flows through on the next pass as role=user. Deleting this slice '
            'was commit 1253a8715, reverted.')

    def test_only_fires_for_a_leading_tool_message(self):
        code = _step1_code()
        assert "processed_messages[0].get('role') == 'tool'" in code, (
            'the special case is ONLY the orphan-at-index-0 case; a paired '
            'tool response deeper in the list must reach the normal handling')


class TestBehaviour:

    def _transform(self, messages):
        h = pytest.importorskip('hartos.helper')
        flask = pytest.importorskip('flask')
        with flask.Flask(__name__).app_context():
            return h.ToolMessageHandler().apply_transform(messages)

    def test_orphan_is_repaired_in_the_callers_list(self):
        """The contract: content is preserved where it matters."""
        caller = [
            {'role': 'tool', 'name': 'Assistant', 'tool_call_id': 'Tqon4dDj',
             'content': 'March 2025, UtmoLight ... 18.1% efficiency'},
            {'role': 'user', 'name': 'ChatInstructor', 'content': 'continue'},
        ]
        self._transform(caller)
        assert caller[0]['role'] == 'user', 'orphan demoted in shared history'
        assert 'UtmoLight' in caller[0]['content'], 'tool result NOT destroyed'
        assert 'tool_call_id' not in caller[0]

    def test_output_does_not_lead_with_a_tool_message(self):
        out = self._transform([
            {'role': 'tool', 'name': 'Assistant', 'tool_call_id': 'X1',
             'content': 'result text'},
            {'role': 'user', 'name': 'ChatInstructor', 'content': 'continue'},
        ])
        assert not out or out[0].get('role') != 'tool', (
            'a leading role=tool is invalid — nothing precedes it to answer')

    def test_paired_tool_message_is_untouched(self):
        out = self._transform([
            {'role': 'assistant', 'name': 'Helper',
             'tool_calls': [{'id': 'A1', 'type': 'function',
                             'function': {'name': 'google_search',
                                          'arguments': '{"text":"x"}'}}],
             'content': ''},
            {'role': 'tool', 'name': 'Assistant', 'tool_call_id': 'A1',
             'content': 'paired result'},
        ])
        blob = ' '.join(str(m.get('content', '')) for m in out)
        assert 'paired result' in blob, 'a correctly paired response was lost'

    def test_no_leading_tool_is_a_noop(self):
        out = self._transform([
            {'role': 'user', 'name': 'User', 'content': 'hello'},
            {'role': 'assistant', 'name': 'Assistant', 'content': 'hi'},
        ])
        assert [m.get('content') for m in out] == ['hello', 'hi']

    def test_empty_input_is_safe(self):
        assert self._transform([]) == []
