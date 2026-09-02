"""Guard: ToolMessageHandler.validate_messages must never return a body with
no user/tool message.

Live root cause (2026-09-03): during reuse the Assistant emits Qwen
``<tool_call>...</tool_call>`` as TEXT (not an OpenAI ``tool_calls`` field), so
autogen never routes it to an executor and no ``role=tool`` reply is appended;
the speaker loop keeps re-selecting the Assistant, whose per-reply view becomes
``[assistant, assistant, assistant]``.  The ROLE-ORDER-GUARD coalesces that to a
lone ``[assistant]``; autogen prepends the 28KB system message and llama-server's
Qwen3 template raises a hard 500 ``No user query found in messages.``, killing
the reuse turn.  The guard now seeds one user turn (the real current action
text) whenever the coalesce would otherwise leave no user/tool message.

RED before the fix (validate_messages returned a lone assistant); GREEN after.
"""
import flask
import pytest

from hartos.helper import ToolMessageHandler


def _run(handler, messages):
    # validate_messages logs via current_app.logger, so run inside an app ctx.
    app = flask.Flask(__name__)
    with app.app_context():
        return handler.validate_messages(messages)


def test_assistant_only_run_gets_a_user_turn_seeded():
    h = ToolMessageHandler(user_tasks=None, user_prompt=None)  # neutral-seed path
    out = _run(h, [
        {"role": "assistant", "name": "Assistant",
         "content": "<tool_call>\n<function=get_saved_metadata>...</tool_call>"},
        {"role": "assistant", "name": "Assistant",
         "content": "<tool_call>\n<function=get_saved_metadata>...</tool_call>"},
        {"role": "assistant", "name": "Assistant",
         "content": "<tool_call>\n<function=get_saved_metadata>...</tool_call>"},
    ])
    # The precondition the Qwen3 template enforces: at least one user/tool turn.
    assert any((m.get("role") or "").lower() in ("user", "tool") for m in out), (
        "validate_messages left a user-less body -> Qwen3 'No user query "
        "found in messages' 500 (this is the pre-fix failure)")


def test_assistant_plus_tool_but_no_user_gets_seeded():
    """The exact live-failure shape: [assistant(tool_calls), tool, assistant].

    A role=tool result is NOT a user query — the Qwen3 template still raises
    'No user query found in messages'.  The guard must seed a role=user turn
    even though a tool message is present (this is why the condition is
    role=='user', not role in ('user','tool')).
    """
    h = ToolMessageHandler(user_tasks=None, user_prompt=None)
    out = _run(h, [
        {"role": "assistant", "name": "Helper", "content": None,
         "tool_calls": [{"id": "abc", "type": "function",
                         "function": {"name": "fetch_news_feeds", "arguments": "{}"}}]},
        {"role": "tool", "tool_call_id": "abc", "content": '{"status":"failed"}'},
        {"role": "assistant", "name": "Assistant", "content": "<tool_call>...</tool_call>"},
    ])
    assert any((m.get("role") or "").lower() == "user" for m in out), (
        "a role=tool result does not satisfy the Qwen3 'user query' requirement; "
        "guard must still seed a user turn (RED under the in('user','tool') condition)")


def test_healthy_turn_is_untouched():
    """A body that already has a user turn must be a strict no-op (no extra user)."""
    h = ToolMessageHandler(user_tasks=None, user_prompt=None)
    out = _run(h, [
        {"role": "user", "name": "User", "content": "run your recipe"},
        {"role": "assistant", "name": "Assistant", "content": "on it"},
    ])
    assert sum(1 for m in out if (m.get("role") or "").lower() == "user") == 1


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
