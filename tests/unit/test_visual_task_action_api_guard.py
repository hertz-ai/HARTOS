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
