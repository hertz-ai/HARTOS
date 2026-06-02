"""#47 — federation must never leak a non-public post to followers.

push_to_followers gained a fail-closed privacy gate: only None (default/public,
since the privacy feature is opt-in), 'public', or 'federation' may leave the
node; private/community/unlisted/followers/unknown are blocked BEFORE the
follower fan-out.  This pins that (the bug is latent today — federation is dead
code — but activates the moment it's re-enabled).
"""
from __future__ import annotations

import os
import sys
import types

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)


def test_push_to_followers_blocks_non_public(monkeypatch):
    import pytest
    try:
        from integrations.social.federation import FederationManager
    except Exception as e:
        pytest.skip(f"federation not importable: {e}")

    fed = FederationManager.__new__(FederationManager)  # no __init__ side effects
    reached = []
    # get_followers is the first thing AFTER the gate; record reaching it.
    monkeypatch.setattr(fed, 'get_followers', lambda db, nid: reached.append(nid) or [])
    # Stub peer_discovery so the public path doesn't import the heavy real module.
    pd = types.ModuleType('integrations.social.peer_discovery')
    pd.gossip = types.SimpleNamespace(node_id='n1', base_url='http://x', node_name='X')
    monkeypatch.setitem(sys.modules, 'integrations.social.peer_discovery', pd)

    # Non-public privacy values must be blocked BEFORE the fan-out.
    for pv in ('private', 'community', 'unlisted', 'followers', 'mystery'):
        fed.push_to_followers(None, {'id': 'x', 'privacy': pv})
    assert reached == [], f"a non-public post reached fan-out: {reached}"

    # public / federation / None (default-public) must proceed past the gate.
    fed.push_to_followers(None, {'id': 'p2', 'privacy': 'public'})
    fed.push_to_followers(None, {'id': 'p3', 'privacy': 'federation'})
    fed.push_to_followers(None, {'id': 'p4'})  # no privacy field → default public
    assert len(reached) == 3, f"public/federation/None must federate, reached={reached}"
