"""Guard: the fabrication gate must recognise a tool that really ran.

Live root cause, measured 2026-09-06 19:22-19:43 on the installed build
(agent 89555447799, drive 5, real user id).  The gate's own log line:

    [FAB-GUARD] action 1 names tool(s) ['google_search'];
        executed=['Assistant']; unrun=['google_search']
    [FABRICATED-COMPLETE] action 1 still claims completion with tool(s)
        ['google_search'] never executed after 3 re-steers — advancing to
        avoid a permanent stall; this action's output is NOT tool-backed

against what the canonical tool-execution log (`agent_system.log`, written
by ``core.tool_logging.log_tool_execution``) recorded in the SAME window:

    tool                                START  SUCCESS  ERROR
    execute_windows_or_android_command     28       28      0
    google_search                          10       10      0
    send_message_to_user                   10       10      0
    text_2_image                            2        2      0

50 executions, 50 successes, zero errors — and the gate still called every
one of them unrun.  Its ``executed`` set never contains a TOOL name, only
``'Assistant'``: the name of the agent that executed them.

The mechanism.  ``_reuse_fabricated_tools`` reads ``m['name']`` off raw
``role=='tool'`` messages in ``group_chat.messages`` / each agent's
``_oai_messages``.  In this pipeline a tool result arrives as an AGGREGATE
message — ``{'role': 'tool', 'tool_responses': [ ... ], 'content': ...}`` —
whose top-level ``name`` is stamped with the SENDING AGENT's name when it is
delivered.  The per-call entries live in ``tool_responses``, keyed by
``tool_call_id``, and the function name they belong to is carried by the
PRECEDING assistant message's ``tool_calls[].function.name``.

`helper.py` already resolves it exactly that way when it builds the outgoing
body (:1898-1907, mapping tool_call_id -> function name, falling back to
``assistant_msg.get('name', 'Assistant')``).  That is why the live wire
bodies show 12 properly-named ``google_search`` tool messages in the very
window where the gate saw none: the function-name attribution existed only
DOWNSTREAM of the buffers the gate reads.  Same evidence proves the sibling
theories dead — 0 HISTORICAL_TOOL_PLACEHOLDER messages in that window, and
80/80 tool messages carried real content.

Consequence, and why this is the load-bearing blocker: every tool-naming
action is judged fabricated no matter how well its tool ran, so the gate
burns its 3 re-steers and then force-advances while logging "NOT
tool-backed".  `GOT COMPLETED FOR ACTION` stayed 0 across the whole run.

These tests pin BOTH directions, because a gate that cannot fail is worth
nothing (feedback_vacuous_guards): a real execution must be seen, and a real
fabrication must still be caught.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..'))

from core.constants import HISTORICAL_TOOL_PLACEHOLDER  # noqa: E402
from hartos import reuse_recipe  # noqa: E402


class _FakeTask:
    """Stands in for the ledger's action tracker."""

    def __init__(self, action_text):
        self._text = action_text

    def get_action(self, idx):
        return self._text


class _FakeAgent:
    """An agent that carries a tool in its function map, like the executor."""

    def __init__(self, tools, oai_messages=None):
        self._function_map = {t: (lambda: None) for t in tools}
        self.llm_config = {'tools': [
            {'function': {'name': t}} for t in tools]}
        self._oai_messages = oai_messages or {}


class _FakeGroupChat:
    def __init__(self, messages, agents):
        self.messages = messages
        self.agents = agents


_TOOLS = ['google_search', 'execute_windows_or_android_command']
_ACTION = 'Action #1: Use google_search to research the topic.'
_UP = 'testuser_123'


def _assistant_proposing(call_id, fn_name):
    """The model's tool_call — a PROPOSAL, never proof of execution."""
    return {
        'role': 'assistant',
        'name': 'Assistant',
        'content': None,
        'tool_calls': [{
            'id': call_id,
            'type': 'function',
            'function': {'name': fn_name, 'arguments': '{"query": "x"}'},
        }],
    }


def _aggregate_tool_result(call_id, content):
    """The live shape: agent-named envelope, per-call entries inside.

    Note the top-level ``name`` is the AGENT's — that is the whole defect.
    """
    return {
        'role': 'tool',
        'name': 'Assistant',
        'content': content,
        'tool_responses': [{
            'tool_call_id': call_id,
            'role': 'tool',
            'content': content,
        }],
    }


def _run(messages, tools=_TOOLS, action=_ACTION):
    agents = [_FakeAgent(tools)]
    gc = _FakeGroupChat(messages, agents)
    original = reuse_recipe.user_tasks
    reuse_recipe.user_tasks = {_UP: _FakeTask(action)}
    try:
        return reuse_recipe._reuse_fabricated_tools(_UP, 1, gc, agents)
    finally:
        reuse_recipe.user_tasks = original


def test_a_tool_that_really_ran_is_not_reported_unrun():
    """The load-bearing assertion — this is the live 2026-09-06 failure."""
    unrun = _run([
        _assistant_proposing('call_abc', 'google_search'),
        _aggregate_tool_result('call_abc',
                               'Search results: https://example.com/... '),
    ])
    assert 'google_search' not in unrun, (
        "google_search executed 10/10 successfully in the live window yet the "
        "gate reported it unrun.  The result arrives as an aggregate "
        "{'role':'tool','name':<AGENT>,'tool_responses':[...]} message, so "
        "reading the top-level 'name' yields 'Assistant' and never a tool "
        "name.  Resolve it from tool_call_id via the preceding assistant "
        "message's tool_calls, as helper.py:1898-1907 already does.  "
        "got unrun=%r" % (unrun,))


def test_the_executing_agents_name_is_never_counted_as_a_tool():
    """'Assistant' is not a tool; it must not satisfy any tool reference.

    Pins the observed `executed=['Assistant']` pollution: agent names in the
    executed set are meaningless and mask which tool is actually missing.
    """
    unrun = _run([
        _assistant_proposing('call_x', 'execute_windows_or_android_command'),
        _aggregate_tool_result('call_x', 'command output'),
    ])
    # A DIFFERENT tool is what the action names, and it never ran.
    assert 'google_search' in unrun, (
        'an unrelated tool result (or a bare agent name) must not mark '
        'google_search as executed — that is the revwarm5407 defeat')


def test_a_genuinely_fabricated_completion_is_still_caught():
    """Non-vacuity: the fix must not simply fail open on everything.

    A proposal with NO result is exactly the fabrication case the gate
    exists for (live 2026-09-03: revenue agent claimed 92% verified with
    zero get_api_revenue_stats execution).
    """
    unrun = _run([
        _assistant_proposing('call_never', 'google_search'),
        # no tool result at all
    ])
    assert 'google_search' in unrun, (
        'a tool_call with no result is a PROPOSAL; counting it as execution '
        'is what let nine fabricated completions advance')


def test_a_placeholder_result_still_does_not_count_as_execution():
    """The stand-in helper.py mints BECAUSE a call produced no result."""
    unrun = _run([
        _assistant_proposing('call_ph', 'google_search'),
        _aggregate_tool_result('call_ph', HISTORICAL_TOOL_PLACEHOLDER),
    ])
    assert 'google_search' in unrun, (
        'HISTORICAL_TOOL_PLACEHOLDER is proof of NON-execution and must '
        'never satisfy the gate, in either message shape')


def test_the_flat_tool_message_shape_also_resolves():
    """Some results arrive flat: {'role':'tool','tool_call_id':..,'content':..}

    Measured live in the same window: 22 of 80 tool messages in the wire
    bodies carried a tool_call_id and NO name at all.  Those were skipped
    outright by the `not m.get('name')` test, so this shape must resolve
    through the same tool_call_id mapping.
    """
    unrun = _run([
        _assistant_proposing('call_flat', 'google_search'),
        {'role': 'tool', 'tool_call_id': 'call_flat',
         'content': 'real search output'},
    ])
    assert 'google_search' not in unrun, (
        'a nameless flat tool result carrying a real tool_call_id is a REAL '
        'execution; the old `not m.get("name")` skip discarded it')
