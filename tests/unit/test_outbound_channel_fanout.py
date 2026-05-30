"""Behavioural tests for the outbound omni-channel fan-out (task #62,
2026-05-30).

A new post in a broadcast-designated community must fan OUT to external
channels via the canonical announcement_broadcaster — "Nunba content made
public on other channels to bring in new users." Guards:
  - only event == 'post.new' broadcasts (edits/deletes don't),
  - only broadcast-enabled communities (personal communities stay internal),
  - the external text carries NO author PII (built from the sanitized payload,
    title/content/link only),
  - '*' opts in every community.

These drive the real _publish_post_event with the internal WAMP fan-out
(publish_event) and the external broadcaster both mocked at the boundary.
"""
from __future__ import annotations

import os
import sys
from unittest.mock import MagicMock, patch

import pytest

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import integrations.social.realtime as rt  # noqa: E402

_BCAST = 'integrations.channels.announcement_broadcaster.broadcast_announcement'


@pytest.fixture(autouse=True)
def _isolate(monkeypatch):
    # Don't touch the real WAMP/SSE publisher; reset the community cache so
    # each test's env takes effect.
    monkeypatch.setattr(rt, 'publish_event', MagicMock())
    rt._broadcast_communities_cache = None
    yield
    rt._broadcast_communities_cache = None


def test_new_post_in_broadcast_community_fans_out(monkeypatch):
    monkeypatch.setenv('HEVOLVE_BROADCAST_COMMUNITIES', 'announcements,platform')
    post = {
        'id': 'p1', 'title': 'Local-first agents', 'content': 'The body.',
        'link_url': 'https://hevolve.ai/x',
        'author': {'username': 'u', 'email': 'leak@example.com'},
    }
    with patch(_BCAST, return_value=2) as mk:
        rt._publish_post_event('post.new', post, community_name='announcements')
    assert mk.called, "post.new in a broadcast community must fan out externally"
    text = mk.call_args[0][0]
    assert 'Local-first agents' in text and 'The body.' in text
    assert 'https://hevolve.ai/x' in text
    assert 'leak@example.com' not in text, "author PII must never reach channels"


def test_new_post_in_personal_community_stays_internal(monkeypatch):
    monkeypatch.setenv('HEVOLVE_BROADCAST_COMMUNITIES', 'announcements')
    post = {'id': 'p2', 'title': 'private note', 'content': 'x'}
    with patch(_BCAST) as mk:
        rt._publish_post_event('post.new', post, community_name='user_private_42')
    assert not mk.called, "non-broadcast community must NOT fan out"


def test_no_community_does_not_fan_out(monkeypatch):
    monkeypatch.setenv('HEVOLVE_BROADCAST_COMMUNITIES', '*')
    post = {'id': 'p3', 'title': 'orphan', 'content': 'x'}
    with patch(_BCAST) as mk:
        rt._publish_post_event('post.new', post, community_name=None)
    assert not mk.called


def test_post_update_does_not_rebroadcast(monkeypatch):
    monkeypatch.setenv('HEVOLVE_BROADCAST_COMMUNITIES', '*')
    post = {'id': 'p4', 'title': 'edited', 'content': 'x'}
    with patch(_BCAST) as mk:
        rt._publish_post_event('post.update', post, community_name='announcements')
    assert not mk.called, "edits must not re-broadcast to channels"


def test_wildcard_broadcasts_any_community(monkeypatch):
    monkeypatch.setenv('HEVOLVE_BROADCAST_COMMUNITIES', '*')
    post = {'id': 'p5', 'title': 'anything', 'content': 'x'}
    with patch(_BCAST, return_value=1) as mk:
        rt._publish_post_event('post.new', post, community_name='some_random_community')
    assert mk.called


def test_internal_fanout_still_happens_regardless(monkeypatch):
    """The external leg must be ADDITIVE — internal community.feed fan-out
    fires whether or not the community broadcasts externally."""
    monkeypatch.setenv('HEVOLVE_BROADCAST_COMMUNITIES', 'announcements')
    post = {'id': 'p6', 'title': 't', 'content': 'x'}
    with patch(_BCAST):
        rt._publish_post_event('post.new', post, community_name='user_private_42')
    assert rt.publish_event.called, "internal community.feed fan-out must still fire"
