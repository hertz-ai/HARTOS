"""The sync_meetings agent tool wires the Zoom/Meet ingest fetchers (#64).

fetch_and_ingest_zoom / fetch_and_ingest_gmeet (integrations/social/events.py)
were implemented and unit-tested, but NOTHING called them — a write-only orphan:
list_upcoming_events (the read side) had nothing to read for those sources.
sync_meetings is the caller. These behavioural tests build the REAL channel
tool closures, pull out sync_meetings, and assert it:
  * routes 'zoom' / 'meet' to the right fetcher with the resolved token;
  * scopes ingest to the calling user (created_by);
  * resolves the token param > env var (the adapter-wide convention);
  * degrades with a clear "connect your account" message when no token exists
    (never a silent no-op).
The fetchers are mocked at the module boundary — no live Zoom/Google. No grep tests.
"""
from __future__ import annotations

import os
import sys
from unittest.mock import patch

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from integrations.channels import agent_tools


def _get_tool(name, user_id='u-1'):
    tools = agent_tools.build_channel_tool_closures({'user_id': user_id, 'prompt_id': None})
    fn = next((t[2] for t in tools if t[0] == name), None)
    assert fn is not None, f"{name} tool not registered"
    return fn


def test_zoom_routes_to_fetcher_with_token_and_user_scope():
    captured = {}

    def _fake_zoom(token, created_by=None, **k):
        captured['token'] = token
        captured['created_by'] = created_by
        return [{'title': 'Standup'}, {'title': 'Review'}]

    with patch('integrations.social.events.fetch_and_ingest_zoom', _fake_zoom):
        fn = _get_tool('sync_meetings', user_id='u-42')
        out = fn(provider='zoom', access_token='tok-abc')

    assert captured['token'] == 'tok-abc'
    assert captured['created_by'] == 'u-42'      # ingest scoped to the caller
    assert 'Synced 2' in out


def test_meet_routes_to_gmeet_fetcher():
    captured = {}

    def _fake_gmeet(token, created_by=None, **k):
        captured['token'] = token
        return [{'title': 'Sync'}]

    with patch('integrations.social.events.fetch_and_ingest_gmeet', _fake_gmeet):
        fn = _get_tool('sync_meetings')
        out = fn(provider='meet', access_token='g-tok')

    assert captured['token'] == 'g-tok'
    assert 'Synced 1' in out


def test_token_falls_back_to_env(monkeypatch):
    captured = {}
    monkeypatch.setenv('ZOOM_ACCESS_TOKEN', 'env-zoom-tok')

    def _fake_zoom(token, created_by=None, **k):
        captured['token'] = token
        return []

    with patch('integrations.social.events.fetch_and_ingest_zoom', _fake_zoom):
        fn = _get_tool('sync_meetings')
        out = fn(provider='zoom')          # no token arg -> env

    assert captured['token'] == 'env-zoom-tok'
    assert 'No upcoming' in out            # 0-events path, distinct from no-token


def test_no_token_degrades_clearly(monkeypatch):
    monkeypatch.delenv('ZOOM_ACCESS_TOKEN', raising=False)
    fn = _get_tool('sync_meetings')
    out = fn(provider='zoom').lower()
    assert 'no zoom token' in out and 'connect' in out


def test_unknown_provider_is_rejected():
    fn = _get_tool('sync_meetings')
    assert 'Unknown provider' in fn(provider='webex')
