"""Per-node HMAC secret, and the campaign token that used a public default.

integrations/channels/email_campaign keyed its click-attribution HMAC on
`os.environ.get('HEVOLVE_TRACK_SECRET', 'hevolve-campaign')`. The token is an
HMAC over 'campaign|address', so with the key in the public source anyone
holding the repository and a candidate address could confirm whether a token
belonged to it.

The same class of bug was already fixed for federation deltas, whose comment
calls it "a hardcoded/default key vulnerability". These pin the fixed
behaviour for the caller that was missed.

Behavioural: no source greps. Call the real functions and assert on the values.
"""
from __future__ import annotations

import os
import sys

import pytest

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)


@pytest.fixture
def ns(tmp_path, monkeypatch):
    import core.node_secret as m
    # Isolate: never touch the developer's real secret.
    monkeypatch.setattr(m, '_HMAC_SECRET_PATH', str(tmp_path / '.hmac_secret'))
    monkeypatch.setattr(m, '_NODE_HMAC_SECRET', '')
    monkeypatch.delenv('HEVOLVE_TRACK_SECRET', raising=False)
    return m


def test_tracking_secret_is_not_the_public_default(ns):
    """The regression this exists for."""
    assert ns.get_tracking_secret() != 'hevolve-campaign'


def test_tracking_secret_has_real_entropy(ns):
    """32 random bytes hex-encoded."""
    assert len(ns.get_tracking_secret()) == 64


def test_env_override_still_wins(ns, monkeypatch):
    """An operator who already set one keeps their existing links working."""
    monkeypatch.setenv('HEVOLVE_TRACK_SECRET', 'operator-chosen')
    assert ns.get_tracking_secret() == 'operator-chosen'


def test_secret_is_stable_across_calls(ns):
    """Tokens must verify later, so the key cannot churn per call."""
    assert ns.get_tracking_secret() == ns.get_tracking_secret()


def test_secret_persists_across_process_restart(ns):
    """Simulate a restart: cache cleared, file kept. Same key, so links minted
    before the restart still verify."""
    first = ns.get_tracking_secret()
    ns._NODE_HMAC_SECRET = ''
    assert ns.get_tracking_secret() == first


def test_two_nodes_do_not_share_a_secret(ns, tmp_path, monkeypatch):
    """The point of the fix: a token forged on one install must not verify on
    another."""
    first = ns.get_tracking_secret()
    monkeypatch.setattr(ns, '_HMAC_SECRET_PATH', str(tmp_path / 'other' / '.hmac_secret'))
    monkeypatch.setattr(ns, '_NODE_HMAC_SECRET', '')
    assert ns.get_tracking_secret() != first


def test_campaign_token_differs_from_the_old_public_key(ns):
    """End to end through the real token builder.

    Recomputing the token with the old published key must NOT match, which is
    exactly what an attacker with the repo would have been doing.
    """
    import hashlib
    import hmac
    try:
        from integrations.channels.email_campaign import tracking_token
    except Exception as e:  # pragma: no cover - module unavailable
        pytest.skip(f'token builder not importable: {e}')

    campaign, address = 'nunba-gift', 'someone@example.com'
    real = tracking_token(address, campaign)
    forged = hmac.new(b'hevolve-campaign',
                      f'{campaign}|{address.lower()}'.encode(),
                      hashlib.sha256).hexdigest()[:12]
    assert real != forged
