"""Guard: the logging helper must not mutate the message it is logging.

Live root cause, measured 2026-09-06 17:08-17:55 on the installed build
(agent 89555447799, 46-minute reuse drive).  The user-visible ending was the
agent apologising:

    "The previous attempts to open LinkedIn failed because I didn't have the
     correct parameters. Let me try again with proper arguments."

and the executor errors behind it were

    send_message_to_user() missing 1 required positional argument: 'text'
    send_message_to_user() got an unexpected keyword argument 'remains'

The model did not generate either shape.  With the response-side capture
(2fb910215) recording what the model actually returned: 195 generated tool
calls, ZERO wrongly empty — the only '{}' generations are get_user_id (7/7),
get_user_uploaded_file (3/3), get_prompt_id (1/1) and get_saved_metadata
(1/1), all of which take no arguments.  Every argument-taking tool got real
arguments, `send_message_to_user` 30/30 among them.

`remains` names the mechanism.  Exactly ONE generated call in the whole run
contained that word: a long `{"text": "...an article draft... remains ..."}`
at 17:15:31 — well-formed, single-key, the word sitting inside the *value*.
For it to arrive at the executor as a keyword, the arguments string had to be
cut mid-value and then re-parsed leniently, so a fragment of the prose became
a key.

`create_log_safe_message` is where the cut happens, and it is a LOGGING
function.  `log_msg = msg.copy()` is shallow, so `log_msg['tool_calls']` IS
`msg['tool_calls']` — the same list object.  The subsequent
`log_msg['tool_calls'][i] = tool_call.copy()` is `list.__setitem__` on that
shared list, so the truncated copy is written straight back into the live
message.  The author did copy the tool_call and the function dict; the list
holding them was missed.  Same shape in the `tool_responses` branch.

The gate is `len(arguments) > 200` and the cut is `[:1000] + "... [truncated]"`,
which matches the live per-tool split exactly — short arguments survive, long
ones do not:

    execute_windows_or_android_command  14/15 generated arg-sets survive
    send_message_to_roles                3/5
    save_data_in_memory                  1/12
    send_message_to_user                 0/17
    create_campaign                      0/1

Downstream, `ensure_tool_call_arguments_json` (helper.py:921) sees arguments
that no longer parse and does exactly what it promises: `repair_json` them,
or fall back to `'{}'`.  That guard is not at fault — it is the last line of
defence against a llama 500, and it is why the request-side replays show
'{}'.  It is being handed already-corrupted input.

So this is a logging side effect corrupting agent execution state.  The test
below drives the real method and asserts the input is untouched.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..'))

from hartos.helper import ToolMessageHandler  # noqa: E402

# Longer than the 1000-char cut, so truncation lands mid-value — the live shape.
_LONG_TEXT = ('I have opened the LinkedIn web interface. ' * 40) + 'remains open.'
_LONG_ARGS = '{"text": "%s"}' % _LONG_TEXT


def _msg_with_long_tool_call():
    return {
        'role': 'assistant',
        'name': 'Assistant',
        'content': None,
        'tool_calls': [{
            'id': 'call_1',
            'type': 'function',
            'function': {'name': 'send_message_to_user',
                         'arguments': _LONG_ARGS},
        }],
    }


def test_logging_a_message_does_not_truncate_its_tool_call_arguments():
    """The load-bearing assertion: the LIVE message survives being logged."""
    handler = ToolMessageHandler()
    msg = _msg_with_long_tool_call()

    handler.create_log_safe_message(msg, max_words=70)

    got = msg['tool_calls'][0]['function']['arguments']
    assert got == _LONG_ARGS, (
        "create_log_safe_message truncated the arguments of the message it "
        "was asked to LOG.  `log_msg = msg.copy()` is shallow, so "
        "`log_msg['tool_calls'][i] = ...` writes through to the live message; "
        "the executor then receives '%s...' which is not valid JSON, and the "
        "call reaches the tool with repaired-but-wrong or empty arguments."
        % got[:60])


def test_the_returned_log_copy_is_still_truncated():
    """The truncation itself is wanted — only its reach is not.

    Keeping this pinned means a fix cannot 'pass' by simply deleting the
    truncation and blowing the log size back up (PERF-2).
    """
    handler = ToolMessageHandler()
    msg = _msg_with_long_tool_call()

    log_msg = handler.create_log_safe_message(msg, max_words=70)

    logged = log_msg['tool_calls'][0]['function']['arguments']
    assert logged.endswith('... [truncated]'), (
        'the log copy must still be truncated — the point is a smaller log '
        'line, not an unbounded one')
    assert len(logged) < len(_LONG_ARGS)


def test_short_arguments_are_left_alone_on_both_sides():
    """Under the 200-char gate nothing is copied or cut, either way.

    This is why `execute_windows_or_android_command` (short instructions)
    survived 14/15 live while `send_message_to_user` (article drafts) survived
    0/17 — the defect is length-gated, and that must stay true after the fix.
    """
    handler = ToolMessageHandler()
    short = '{"instructions":"Open LinkedIn","os_to_control":"windows"}'
    msg = {
        'role': 'assistant',
        'tool_calls': [{'id': 'c', 'type': 'function',
                        'function': {'name': 'execute_windows_or_android_command',
                                     'arguments': short}}],
    }

    log_msg = handler.create_log_safe_message(msg, max_words=70)

    assert msg['tool_calls'][0]['function']['arguments'] == short
    assert log_msg['tool_calls'][0]['function']['arguments'] == short


def test_logging_does_not_truncate_live_tool_response_content():
    """The same aliasing bug, second instance, same function.

    `log_msg['tool_responses'][i] = response.copy()` writes into the shared
    list exactly as the tool_calls branch does, so a tool's real result is
    replaced in the live message by a 10-word stub.
    """
    handler = ToolMessageHandler()
    full = ' '.join('word%d' % i for i in range(200))
    msg = {
        'role': 'tool',
        'tool_responses': [{'tool_call_id': 'c', 'role': 'tool',
                            'content': full}],
        'content': full,
    }

    handler.create_log_safe_message(msg, max_words=10)

    assert msg['tool_responses'][0]['content'] == full, (
        'a tool response carrying a REAL result must not be replaced by its '
        'own log stub — that discards the tool output the agent is about to '
        'reason over')


def test_original_message_object_identity_is_preserved():
    """The caller's list must still be the caller's list.

    A fix that swaps in a new list on the ORIGINAL message would technically
    keep the text intact while still mutating the caller's object; this
    pins that the original container is untouched.
    """
    handler = ToolMessageHandler()
    msg = _msg_with_long_tool_call()
    tool_calls_before = msg['tool_calls']
    call_before = msg['tool_calls'][0]

    handler.create_log_safe_message(msg, max_words=70)

    assert msg['tool_calls'] is tool_calls_before
    assert msg['tool_calls'][0] is call_before
