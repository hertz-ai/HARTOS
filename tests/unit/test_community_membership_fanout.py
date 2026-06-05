"""#55 — community membership changes fan out in realtime.

CommunityService.join/leave now publish on_community_membership so members see
joins/leaves live (per-community topic community.message) instead of only on the
next /communities/{id} fetch.  Mocks the publish boundary and asserts the payload
+ the service→realtime shim path the service methods use.
"""
from __future__ import annotations

import os
import sys

import pytest

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)


def test_on_community_membership_payload(monkeypatch):
    try:
        import integrations.social.realtime as R
    except Exception as e:
        pytest.skip(f"realtime unavailable: {e}")
    calls = []
    monkeypatch.setattr(R, 'publish_event', lambda topic, data, **k: calls.append((topic, data)))

    R.on_community_membership('c1', 'u1', 'join', role='member')
    assert len(calls) == 1
    topic, data = calls[0]
    assert topic == 'community.message'
    assert data['community_id'] == 'c1' and data['user_id'] == 'u1'
    assert data['action'] == 'join' and data['role'] == 'member'
    assert data['event'] == 'community.membership'  # clients filter on this
    assert data.get('msg_id'), "stamped with an idempotency key like other events"

    # No community → no fan-out (don't emit an unroutable event).
    calls.clear()
    R.on_community_membership('', 'u1', 'leave')
    assert calls == []


def test_service_shim_routes_membership_fanout(monkeypatch):
    """The exact path join/leave use: _publish_realtime('on_community_membership',
    …) → realtime.on_community_membership → publish_event."""
    try:
        import integrations.social.realtime as R
        import integrations.social.services as S
    except Exception as e:
        pytest.skip(f"social services unavailable: {e}")
    calls = []
    monkeypatch.setattr(R, 'publish_event', lambda topic, data, **k: calls.append((topic, data)))

    S._publish_realtime('on_community_membership', 'c9', 'u9', 'leave')
    assert calls and calls[0][0] == 'community.message'
    assert calls[0][1]['action'] == 'leave' and calls[0][1]['community_id'] == 'c9'
