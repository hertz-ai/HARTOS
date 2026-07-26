"""steer_goal MCP tool — the external-Claude-Code co-pilot action.

This is how a connected Claude Code session drives the HARTOS flywheel WITHOUT an
Anthropic key: watch goals (list_goals/agent_status), and steer a stalled one by
injecting an instruction into its live GroupChat. It MUST route through the live
app's canonical inject route over loopback (the GroupChat registry is process-
local; the MCP server is a separate process), NOT call inject_instruction direct.

Behavioural: mock requests.post + the loopback bases, call the real impl, assert
the POST target/body + the returned payload.

    python -m pytest tests/unit/test_mcp_steer_goal.py --noconftest -q
"""
import json
from unittest.mock import patch, MagicMock

import integrations.mcp._tool_impls as impls


def _resp(status, payload=None):
    r = MagicMock()
    r.status_code = status
    r.content = b'x' if payload is not None else b''
    r.json.return_value = payload or {}
    r.text = json.dumps(payload or {})
    return r


def test_steer_goal_posts_to_inject_route_as_copilot_actor():
    posted = {}

    def fake_post(url, json=None, timeout=None):
        posted['url'] = url
        posted['body'] = json
        return _resp(200, {'ok': True, 'message_index': 3})

    with patch.object(impls, '_loopback_bases', return_value=['http://127.0.0.1:5000']), \
            patch('requests.post', side_effect=fake_post):
        out = impls.steer_goal('goal-42', 'emit the recipe JSON now')

    assert posted['url'].endswith('/api/social/dashboard/agents/goal-42/inject')
    assert posted['body']['instruction'] == 'emit the recipe JSON now'
    assert posted['body']['actor_id'] == 'claude-code-copilot'  # audit attribution
    assert json.loads(out)['ok'] is True


def test_steer_goal_empty_instruction_rejected_before_any_post():
    with patch('requests.post') as mock_post:
        out = impls.steer_goal('goal-42', '   ')
    assert mock_post.call_count == 0
    assert 'error' in json.loads(out)


def test_steer_goal_falls_over_to_next_base_on_5xx():
    calls = []

    def fake_post(url, json=None, timeout=None):
        calls.append(url)
        if '5000' in url:
            return _resp(502)            # first base down → try next
        return _resp(400, {'ok': False, 'error': 'no live GroupChat'})

    with patch.object(impls, '_loopback_bases',
                      return_value=['http://127.0.0.1:5000', 'http://127.0.0.1:6777']), \
            patch('requests.post', side_effect=fake_post):
        out = impls.steer_goal('g1', 'hi')

    assert len(calls) == 2                # tried both bases
    # 400 (goal not live / no GroupChat) is an ANSWER, surfaced to the copilot
    assert json.loads(out)['ok'] is False


def test_loopback_bases_is_single_source_for_agent_status_and_steer():
    # DRY guard: the candidate-base list has ONE definition, used by both.
    bases = impls._loopback_bases()
    assert any('127.0.0.1:5000' in b for b in bases)
    assert any('127.0.0.1:6777' in b for b in bases)
