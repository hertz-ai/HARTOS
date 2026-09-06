"""An orphaned leading tool message must be KEPT as user text, not discarded.

Measured live 2026-09-06, agent 89555447799, action 1 of 24 — the reason
that agent never advanced.

``google_search`` really executes: `INSIDE google search` at 10:58:28,044
followed by five real HTTP 200s via primp (grokipedia, wikipedia, mojeek,
startpage, duckduckgo).  Its ``role=tool`` response really reaches the
group chat, correctly paired — the 10:58:33 snapshot shows

    [3] assistant Helper  tool_calls=[Tqon4dDj...]
    [4] role=tool  name=Assistant

Then the wire left-trim eats the conversation from the front and the pair
is split.  The response migrates 4 -> 1 -> 0 -> 0 -> 0 across snapshots;
once it sits at index 0 its assistant ``tool_call`` has been trimmed away
and it is an orphan (a leading ``role=tool`` is invalid in the OpenAI
schema — nothing precedes it to answer).

``ToolMessageHandler.apply_transform`` STEP 1 handles exactly that.  It
does the right repair — demote to ``role=user``, rename to Helper, drop
the now-danging ``tool_call_id`` — and then throws the repaired message
away::

    processed_messages[0]['role'] = 'user'
    processed_messages[0]['name'] = 'Helper'
    if 'tool_call_id' in processed_messages[0]:
        del processed_messages[0]['tool_call_id']
    processed_messages = processed_messages[1:]   # <- discards it

The three repair lines are dead code.  The search results are destroyed,
so the model never sees what its own tool returned, apologises ("I
apologize for the error in my previous response..."), re-emits the same
call, and the unanswered ``tool_call`` accumulates — 1 -> 7 copies of one
byte-identical assistant message, `Detected 7 active tool calls`, and the
action can never complete.

Counts from that window: 3 STEP-1 firings, matching exactly the 3
snapshots with ``role=tool`` at index 0.

The fix keeps what the repair produced.  It does NOT try to re-pair the
orphan with a trimmed-away call (that content is gone) and it does NOT
touch the trimmer — those are separate concerns; this only stops the
handler destroying a tool result it has already made schema-valid.

    python -m pytest tests/unit/test_orphan_tool_message_is_kept.py --noconftest -q
"""
import os
import re

import pytest


_SRC = os.path.join(os.path.dirname(__file__), '..', '..', 'hartos', 'helper.py')


def _source():
    with open(_SRC, encoding='utf-8') as fh:
        return fh.read()


def _step1(code_only=True):
    """The STEP 1 block.

    ``code_only`` strips comment lines.  The block's comment QUOTES the
    removed ``processed_messages[1:]`` line while explaining the defect, so a
    raw-text search would match the explanation and report the bug as still
    present — which is exactly what happened on the first run of this file.
    Assertions here are about executable code, not prose.
    """
    src = _source()
    m = re.search(r"# STEP 1: Handle first message if it's a tool message.*?\n(?=\s*# STEP 2)",
                  src, re.DOTALL)
    assert m, 'STEP 1 block not found'
    block = m.group(0)
    if not code_only:
        return block
    return '\n'.join(ln for ln in block.splitlines()
                     if not ln.lstrip().startswith('#'))


class TestRepairIsNotDeadCode:
    """The three repair lines must actually affect the output."""

    def test_repaired_message_is_not_sliced_away(self):
        block = _step1()
        assert 'processed_messages[1:]' not in block, (
            'STEP 1 demotes the orphaned tool message to role=user and then '
            'discards it with processed_messages[1:], making the repair dead '
            'code and destroying the tool output the model needs to see. '
            'Keep the repaired message.')

    def test_repair_still_happens(self):
        """Don't "fix" this by deleting the repair instead of the slice."""
        block = _step1()
        assert "'role'] = 'user'" in block, 'orphan must still be demoted to user'
        assert "'name'] = 'Helper'" in block, 'orphan must still be renamed'
        assert "del processed_messages[0]['tool_call_id']" in block, (
            'the dangling tool_call_id must still be dropped — it references '
            'an assistant tool_call the trimmer removed')


class TestBehaviour:
    """Exercise the transform for real."""

    def _transform(self, messages):
        h = pytest.importorskip('hartos.helper')
        flask = pytest.importorskip('flask')
        app = flask.Flask(__name__)
        with app.app_context():
            return h.ToolMessageHandler().apply_transform(messages)

    def test_orphan_tool_content_survives(self):
        """The measured case: search results must not vanish."""
        out = self._transform([
            {'role': 'tool', 'name': 'Assistant',
             'tool_call_id': 'Tqon4dDj',
             'content': 'March 2025, UtmoLight ... 18.1% efficiency'},
            {'role': 'user', 'name': 'ChatInstructor', 'content': 'continue'},
        ])
        blob = ' '.join(str(m.get('content', '')) for m in out)
        assert 'UtmoLight' in blob, (
            'the tool result was destroyed; the model cannot see what its own '
            'search returned and will re-issue the call forever')

    def test_orphan_is_schema_valid_after_transform(self):
        out = self._transform([
            {'role': 'tool', 'name': 'Assistant', 'tool_call_id': 'X1',
             'content': 'result text'},
            {'role': 'user', 'name': 'ChatInstructor', 'content': 'continue'},
        ])
        assert out, 'transform returned nothing'
        assert out[0].get('role') != 'tool', (
            'a leading role=tool is invalid — nothing precedes it to answer')
        assert 'tool_call_id' not in out[0], (
            'the dangling tool_call_id must be dropped with the demotion')

    def test_paired_tool_message_is_untouched(self):
        """Only the ORPHAN case is special. A paired response must survive."""
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
        assert 'paired result' in blob, 'a correctly paired tool response was lost'

    def test_no_leading_tool_is_a_noop(self):
        msgs = [
            {'role': 'user', 'name': 'User', 'content': 'hello'},
            {'role': 'assistant', 'name': 'Assistant', 'content': 'hi'},
        ]
        out = self._transform([dict(m) for m in msgs])
        assert [m.get('content') for m in out] == ['hello', 'hi']

    def test_empty_input_is_safe(self):
        assert self._transform([]) == []
