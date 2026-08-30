"""#127: ToolMessageHandler.apply_transform sanitizes orphaned tool messages.

The "Tool message without tool_call_id and no preceding assistant" text (17x/
window) is the handler's OWN warning as it CONVERTS such an orphan to a user
message (helper.py:1486-1494) — it is the handler doing its job, not an
unhandled LLM-API rejection. This locks that in: after apply_transform, NO
'tool' message survives without a preceding assistant carrying its tool_call_id,
so the request the LLM receives is always valid.

Behavioral: real apply_transform, current_app mocked (it only needs .logger),
assert the observable output structure.
"""
import os
import sys
from unittest.mock import MagicMock, patch

_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from hartos import helper  # noqa: E402


def _no_orphaned_tool(messages):
    """True iff every 'tool' message is preceded by an assistant carrying its
    tool_call_id (the OpenAI/llama validity rule #127 is about)."""
    seen = set()
    for m in messages:
        if not isinstance(m, dict):
            continue
        if m.get('role') == 'assistant':
            for tc in (m.get('tool_calls') or []):
                if tc.get('id'):
                    seen.add(tc['id'])
        if m.get('role') == 'tool':
            tcid = m.get('tool_call_id')
            if not tcid or tcid not in seen:
                return False
    return True


def test_orphaned_tool_no_preceding_assistant_is_sanitized():
    messages = [
        {'role': 'user', 'content': 'hi'},
        {'role': 'tool', 'content': 'orphan result', 'tool_call_id': 'zzz'},
    ]
    with patch.object(helper, 'current_app', MagicMock()):
        out = helper.ToolMessageHandler().apply_transform(messages)
    assert _no_orphaned_tool(out), f"orphaned tool message survived: {out}"


def test_valid_assistant_tool_pair_is_preserved():
    messages = [
        {'role': 'user', 'content': 'do it'},
        {'role': 'assistant', 'content': '', 'tool_calls': [
            {'id': 'call_1', 'type': 'function',
             'function': {'name': 'f', 'arguments': '{}'}}]},
        {'role': 'tool', 'content': 'ok', 'tool_call_id': 'call_1'},
    ]
    with patch.object(helper, 'current_app', MagicMock()):
        out = helper.ToolMessageHandler().apply_transform(messages)
    # The valid pairing must survive (the tool response still maps to its call).
    assert _no_orphaned_tool(out)
    assert any(m.get('role') == 'tool' and m.get('tool_call_id') == 'call_1'
               for m in out if isinstance(m, dict))
