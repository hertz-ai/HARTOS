"""Guard: the wire layer must never send llama-server a body with no
role='user' turn.

Live root cause (2026-09-03 03:40:07, source autogen.reuse): a reuse
group-chat reply view reached the wire as ``[system, assistant, tool,
assistant]`` — no user turn.  llama-server's Qwen3 chat template raises a
hard 500 ``No user query found in messages.`` on exactly that shape.  The
body was small (4 short messages), so ``_trim_to_budget`` returned it via
the ``est_before <= budget`` early-return UNTOUCHED, and the per-agent
``ToolMessageHandler`` transform was bypassed on this reply path.  The wire
is the single chokepoint every outbound body crosses, so the seed lives
there now.

RED before the fix: ``_trim_to_budget`` on a user-less under-budget body
returned it with no user turn (the exact 500 precondition).  GREEN after:
it seeds one role='user' turn after the system message.
"""
import pytest

from core.llm_outbound_logger import _trim_to_budget


def _roles(body):
    return [m.get('role') for m in body.get('messages', [])]


def test_userless_under_budget_body_gets_a_user_turn_seeded():
    # The exact live-failure shape, small enough to be well under budget so
    # the trim path's early-return fires (this is what shipped user-less).
    body = {
        'model': 'local',
        'max_tokens': 2048,
        'messages': [
            {'role': 'system', 'content': 'You are a Helpful Assistant.'},
            {'role': 'assistant', 'name': 'Helper', 'content': None,
             'tool_calls': [{'id': 'a', 'type': 'function',
                             'function': {'name': 'fetch_news_feeds',
                                          'arguments': '{}'}}]},
            {'role': 'tool', 'tool_call_id': 'a', 'content': '{"trending": []}'},
            {'role': 'assistant', 'name': 'Assistant',
             'content': 'It looks like there are no trending items.'},
        ],
    }
    new_body, n_dropped, n_trunc, *_ = _trim_to_budget(body)
    assert 'user' in _roles(new_body), (
        "wire must guarantee a role='user' turn — else llama-server's Qwen3 "
        "template 500s with 'No user query found in messages' (the live bug)")
    # Seeded after the system message, ahead of the assistant/tool exchange.
    assert _roles(new_body)[0] == 'system'
    assert _roles(new_body)[1] == 'user'


def test_body_with_existing_user_is_untouched():
    body = {
        'model': 'local',
        'max_tokens': 2048,
        'messages': [
            {'role': 'system', 'content': 'sys'},
            {'role': 'user', 'name': 'User', 'content': 'do the thing'},
            {'role': 'assistant', 'content': 'on it'},
        ],
    }
    new_body, *_ = _trim_to_budget(body)
    # Exactly one user turn — strict no-op, no extra seed.
    assert sum(1 for r in _roles(new_body) if r == 'user') == 1
    assert _roles(new_body) == ['system', 'user', 'assistant']


def test_userless_no_system_seeds_at_front():
    body = {
        'model': 'local',
        'max_tokens': 2048,
        'messages': [
            {'role': 'assistant', 'content': 'hi'},
            {'role': 'tool', 'content': '{}'},
        ],
    }
    new_body, *_ = _trim_to_budget(body)
    assert _roles(new_body)[0] == 'user'


if __name__ == '__main__':
    raise SystemExit(pytest.main([__file__, '-v']))
