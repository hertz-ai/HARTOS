"""Behavioural test for the call_visual_task ACTION_API guard (2026-05-31).

LIVE EVIDENCE (frozen_debug.log): a `call_visual_task` job on a 2-second
IntervalTrigger logged
  reuse_recipe ERROR - Error getting user action details: Invalid URL '?user_id=...'
~30×/min, forever.  Root cause: ACTION_API = config.get('ACTION_API', '') is ''
when unset, so the action-details GET built f"{ACTION_API}?user_id=..." ==
"?user_id=..." (no scheme/host) → pooled_request raises "Invalid URL" every 2s,
burning CPU + spamming the log (which feeds the box-busy → governor-throttle
that starves the flywheel).

FIX: call_visual_task returns early (no HTTP) when ACTION_API is empty.  This
test pins that the malformed request is NEVER made in that case, and that a
configured ACTION_API still proceeds to the request path.
"""
from __future__ import annotations

import os
import sys
from unittest.mock import patch

import pytest

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

# reuse_recipe type-annotates module-level caches with autogen.AssistantAgent
# (evaluated at import time), so importing it crashes when autogen is absent
# (CI). Skip cleanly, matching the suite-wide pattern.
pytest.importorskip('autogen', reason='autogen not installed')

from hartos import reuse_recipe  # noqa: E402


def test_empty_action_api_skips_http_entirely():
    """With ACTION_API='', call_visual_task must return None WITHOUT making any
    HTTP request (no malformed 'Invalid URL' call)."""
    with patch.object(reuse_recipe, 'ACTION_API', ''), \
         patch.object(reuse_recipe, 'pooled_request') as mk_get, \
         patch.object(reuse_recipe, 'pooled_post') as mk_post:
        result = reuse_recipe.call_visual_task('get visual info', 'user-1', 'pid-1')
    assert result is None
    assert not mk_get.called, "must NOT issue the action-details GET when ACTION_API is empty"
    assert not mk_post.called, "must NOT call the visual agent when ACTION_API is empty"


def test_configured_action_api_proceeds_to_request():
    """With ACTION_API set, the function proceeds to issue the action-details
    GET (i.e. the guard does not block the real path)."""
    class _Resp:
        status_code = 200
        def json(self):
            return []  # no entries → returns None after the GET, but GET WAS made

    with patch.object(reuse_recipe, 'ACTION_API', 'http://localhost:8088/get_user_actions'), \
         patch.object(reuse_recipe, 'pooled_request', return_value=_Resp()) as mk_get, \
         patch.object(reuse_recipe, 'pooled_post') as mk_post:
        reuse_recipe.call_visual_task('get visual info', 'user-1', 'pid-1')
    assert mk_get.called, "configured ACTION_API must reach the action-details GET"
    # URL must be well-formed (base + query), not a bare '?user_id='
    called_url = mk_get.call_args[0][1]
    assert called_url.startswith('http'), f"URL must have a scheme/host, got {called_url!r}"
    assert '?user_id=user-1' in called_url


# ---------------------------------------------------------------------------
# 2026-10-04: a poll that FAILS pauses that URL instead of failing again 2s later.
# Live: central's bridge-network container had config.json pointing at
# localhost:6006; the job logged "Error getting user action details" 2,544 times
# in 29 min (one per run).  A reachable-but-refusing endpoint (422 for the
# daemon's non-numeric user id) would have done the same through the non-200
# branch.  Both branches now pause; a good poll never does.
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _no_pause_carried_between_tests():
    reuse_recipe._visual_poll_paused_until.clear()
    yield
    reuse_recipe._visual_poll_paused_until.clear()


def _run(action_api, get):
    with patch.object(reuse_recipe, 'ACTION_API', action_api), \
         patch.object(reuse_recipe, 'pooled_request', side_effect=get) as mk_get, \
         patch.object(reuse_recipe, 'pooled_post'):
        first = reuse_recipe.call_visual_task('get visual info', 'system_daemon', 'pid-1')
        second = reuse_recipe.call_visual_task('get visual info', 'system_daemon', 'pid-1')
    return first, second, mk_get


def test_unreachable_action_api_is_polled_once_then_paused():
    """Connection refused: one 'error' return, one GET, then silence."""
    def refused(*_a, **_k):
        raise ConnectionError("[Errno 111] Connection refused")

    first, second, mk_get = _run('http://localhost:6006/action_by_user_id', refused)
    assert first == 'error'
    assert second is None, "the paused poll must return quietly, not retry"
    assert mk_get.call_count == 1, "no second GET while the pause holds"
    paused_until = reuse_recipe._visual_poll_paused_until[
        'http://localhost:6006/action_by_user_id?user_id=system_daemon']
    assert paused_until > reuse_recipe.time.monotonic()


def test_refusing_action_api_pauses_through_the_non_200_branch():
    """A 422 for the daemon's user id is a failed poll too."""
    class _Resp:
        status_code = 422
        def json(self):
            return {}

    first, second, mk_get = _run('http://172.17.0.1:6006/action_by_user_id', lambda *a, **k: _Resp())
    assert first == 'error'
    assert second is None
    assert mk_get.call_count == 1


def test_pause_expires_and_a_good_poll_never_pauses():
    """Once the pause is over the GET resumes; a 200 leaves no pause behind."""
    class _Ok:
        status_code = 200
        def json(self):
            return []

    def refused(*_a, **_k):
        raise ConnectionError("refused")

    url = 'http://172.17.0.1:6006/action_by_user_id'
    key = url + '?user_id=system_daemon'
    _run(url, refused)
    # Expire the pause by hand (no clock patching) and poll again: the GET runs.
    reuse_recipe._visual_poll_paused_until[key] = reuse_recipe.time.monotonic() - 1
    first, second, mk_get = _run(url, lambda *a, **k: _Ok())
    assert first is None and second is None, "200 with no entries returns None, as before"
    assert mk_get.call_count == 2, "a succeeding poll runs every time; nothing paused it"
    assert key not in reuse_recipe._visual_poll_paused_until or \
        reuse_recipe._visual_poll_paused_until[key] <= reuse_recipe.time.monotonic()
