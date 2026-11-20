"""A result bundle whose call the trim cut never strands the newest tool calls.

Central 2026-11-19 21:01:14 (task #293): the context trim cut an Assistant
tool call and kept its bundled results ({"role": "tool", "tool_responses":
[...]}, autogen's consolidated reply).  ToolMessageHandler.apply_transform
found no call for that bundle, turned it into a user message and APPENDED it
at the end of the history, behind the Assistant's newest tool calls, which
were still waiting to run.  The executor's generate_tool_calls_reply reads
only the last message (autogen conversable_agent.py), so it saw a user
message, ran nothing, and the request went to the model again: the agent's
actions never ran, on every provider (Claude refused the shape with a 400).

The bundle now becomes a user message where it stood.  Invariant pinned:
after the transform every assistant message with tool calls is followed by
its results, or by nothing (it is still pending); never by a user message.

    python -m pytest tests/unit/test_a_cut_calls_results_never_strand_the_new_calls.py --noconftest -q
"""
import os
import sys
from unittest.mock import MagicMock

import pytest

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)


@pytest.fixture
def handler(monkeypatch):
    monkeypatch.setattr('hartos.helper.current_app', MagicMock())
    from hartos.helper import ToolMessageHandler
    return ToolMessageHandler()


def _call(call_id):
    return {'id': call_id, 'type': 'function',
            'function': {'name': 'post_to_channel',
                         'arguments': '{"channel": "email", "text": "%s"}' % call_id}}


def _bundle(results):
    """autogen's consolidated reply: no top-level tool_call_id."""
    return {'role': 'tool', 'name': 'Assistant',
            'content': '\n\n'.join(results.values()),
            'tool_responses': [{'tool_call_id': c, 'role': 'tool', 'content': r}
                               for c, r in results.items()]}


def _centrals_history():
    """The Assistant's window at 21:01:14 (as test_claude_calls_cache_their_prompt
    replays it): the task turn the limiter put back, the bundle whose call it
    cut, four answered calls, three new calls still pending."""
    answered = ['toolu_a%d' % i for i in range(1, 5)]
    pending = ['toolu_b%d' % i for i in range(1, 4)]
    return pending, [
        {'role': 'user', 'name': 'ChatInstructor', 'content': 'Execute Action 2: post the offer'},
        _bundle({'toolu_cut1': 'r1', 'toolu_cut2': 'r2'}),
        {'role': 'assistant', 'name': 'Assistant', 'content': 'Checking the channels first.'},
        {'role': 'user', 'name': 'StatusVerifier', 'content': '{"status": "pending"}'},
        {'role': 'assistant', 'content': None, 'tool_calls': [_call(c) for c in answered]},
        _bundle({c: 'posted ' + c for c in answered}),
        {'role': 'user', 'name': 'StatusVerifier', 'content': '{"status": "pending"}'},
        {'role': 'assistant', 'content': None, 'tool_calls': [_call(c) for c in pending]},
    ]


def _parallel_history():
    """Parallel calls throughout: a cut three-call bundle, an older call that
    never got a result, two parallel calls answered one tool message each,
    and two parallel calls pending."""
    pending = ['call_q1', 'call_q2']
    return pending, [
        {'role': 'user', 'name': 'ChatInstructor', 'content': 'Execute Action 3: send the follow-ups'},
        _bundle({'call_cut1': 'sent 1', 'call_cut2': 'sent 2', 'call_cut3': 'sent 3'}),
        {'role': 'assistant', 'content': None, 'tool_calls': [_call('call_h1')]},
        {'role': 'user', 'name': 'StatusVerifier', 'content': '{"status": "pending"}'},
        {'role': 'assistant', 'content': None, 'tool_calls': [_call('call_p1'), _call('call_p2')]},
        {'role': 'tool', 'tool_call_id': 'call_p1', 'content': 'posted p1'},
        {'role': 'tool', 'tool_call_id': 'call_p2', 'content': 'posted p2'},
        {'role': 'user', 'name': 'StatusVerifier', 'content': '{"status": "pending"}'},
        {'role': 'assistant', 'content': None, 'tool_calls': [_call(c) for c in pending]},
    ]


def _assert_calls_answered_or_pending(out):
    """Each assistant tool-call message is followed by exactly its results,
    or is the last message (still pending execution); never by a user turn."""
    for i, m in enumerate(out):
        calls = [tc['id'] for tc in (m.get('tool_calls') or [])]
        if m.get('role') != 'assistant' or not calls:
            continue
        if i == len(out) - 1:
            continue
        after = out[i + 1:i + 1 + len(calls)]
        assert [x.get('role') for x in after] == ['tool'] * len(calls), (
            'calls %s at %d are followed by %s' % (calls, i, [x.get('role') for x in after]))
        assert sorted(x.get('tool_call_id') for x in after) == sorted(calls)


@pytest.mark.parametrize('history', [_centrals_history, _parallel_history],
                         ids=['central-21:01', 'parallel'])
def test_the_history_ends_on_the_pending_calls(handler, history):
    pending, messages = history()
    out = handler.apply_transform(messages)
    _assert_calls_answered_or_pending(out)
    assert out[-1]['role'] == 'assistant'
    assert [tc['id'] for tc in out[-1]['tool_calls']] == pending


@pytest.mark.parametrize('history, results', [
    (_centrals_history, 'r1\n\nr2'),
    (_parallel_history, 'sent 1\n\nsent 2\n\nsent 3'),
], ids=['central-21:01', 'parallel'])
def test_the_cut_calls_results_stay_where_they_stood(handler, history, results):
    """Still read by the model, as text, before everything said after them."""
    out = handler.apply_transform(history()[1])
    holder = next(i for i, m in enumerate(out)
                  if m.get('role') == 'user' and results in (m.get('content') or ''))
    first_assistant = next(i for i, m in enumerate(out) if m.get('role') == 'assistant')
    assert holder < first_assistant


def test_an_older_unanswered_call_still_gets_its_slot_right_after_it(handler):
    """The converted bundle now sits early in the list; the placeholder for
    a call that never got a result still lands right behind that call."""
    out = handler.apply_transform(_parallel_history()[1])
    at = next(i for i, m in enumerate(out)
              if [tc['id'] for tc in (m.get('tool_calls') or [])] == ['call_h1'])
    assert out[at + 1]['role'] == 'tool' and out[at + 1]['tool_call_id'] == 'call_h1'


@pytest.mark.parametrize('history', [_centrals_history, _parallel_history],
                         ids=['central-21:01', 'parallel'])
def test_the_executor_runs_the_pending_calls(monkeypatch, history):
    """End to end on autogen's own executor: the handler attached the way
    every seat attaches it, the history handed to generate_reply."""
    monkeypatch.setattr('hartos.helper.current_app', MagicMock())
    import autogen
    from autogen.agentchat.contrib.capabilities.transform_messages import TransformMessages
    from hartos.helper import ToolMessageHandler

    ran = []

    def post_to_channel(channel: str, text: str) -> str:
        ran.append(text)
        return 'posted ' + text

    assistant = autogen.ConversableAgent('Assistant', llm_config=False, human_input_mode='NEVER')
    executor = autogen.ConversableAgent(
        'Executor', llm_config=False, human_input_mode='NEVER',
        code_execution_config=False, function_map={'post_to_channel': post_to_channel})
    TransformMessages(transforms=[ToolMessageHandler()], verbose=False).add_to_agent(executor)

    pending, messages = history()
    reply = executor.generate_reply(messages=messages, sender=assistant)

    assert ran == pending
    assert isinstance(reply, dict), reply
    assert [r['tool_call_id'] for r in reply['tool_responses']] == pending
