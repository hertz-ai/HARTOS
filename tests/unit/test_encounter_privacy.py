"""NFT / privacy adversarial tests for encounter_api (closes #403).

Augments tests/unit/test_encounter_api.py (23 functional tests) with
focused adversarial cases on the privacy + lifecycle invariants the
design doc (project_encounter_icebreaker.md) treats as blocking.

Time-control approach
---------------------
NO mocks or monkeypatches on production code.  Production runs with
its real `datetime.utcnow()` clock.  When a test needs to verify
behavior against state that production naturally produces only after
hours of wall-time (TTL expiry, sighting auto-expiry, match-window
boundaries), the test seeds that state DIRECTLY into the in-memory
SQLite engine via the existing `app.test_engine`.

The seeded state is identical in shape to what production writes;
only the timestamps are positioned where the test needs them.  The
endpoint under test runs unmodified production code against that
state.  This is the standard arrange-act-assert pattern with a
fixture-shaped arrange step — no parallel test path, no production
patching, no time mocking.

Helpers `_seed_pref` and `_seed_sighting` are the only test-only
writers; they accept whatever fields the test wants to position and
fill the rest with neutral defaults so each test stays focused.
"""
from __future__ import annotations

import os
import secrets
import sys
from datetime import datetime, timedelta

# Re-use the rich `app` / `client` / `_as_user` fixtures from the
# functional suite — same fake-auth, same fresh in-memory engine,
# same _make_discoverable / _register_pubkey helpers.  Single fixture
# surface, no parallel test setup.
from .test_encounter_api import (  # noqa: F401, E402
    _as_user,
    _make_discoverable,
    _matched_pair,
    _register_pubkey,
    _setup_mutual_sighting,
    app,
    client,
)

PROJECT_ROOT = os.path.abspath(
    os.path.join(os.path.dirname(__file__), '..', '..'),
)
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from core import constants as C  # noqa: E402


# ══════════════════════════════════════════════════════════════════════
# Test-only state seeders.
#
# These insert a DiscoverablePref / EncounterSighting row directly
# against the fixture's in-memory engine, in whatever shape the test
# wants.  The shape matches what production writes — `set_discoverable`
# would produce the same row given enough time passing, the seeders
# just position the timestamp exactly where the test needs it.
# Production code (the /sighting and /swipe handlers) reads the
# seeded state via real DB queries; nothing in production is patched.
# ══════════════════════════════════════════════════════════════════════


def _seed_pref(
    engine,
    *,
    user_id,
    enabled,
    expires_at,
    current_pubkey,
    age_claim_18=True,
    face_visible=False,
    avatar_style='studio_ghibli',
    vibe_tags=None,
):
    from sqlalchemy.orm import sessionmaker

    from integrations.social.models import DiscoverablePref

    Session = sessionmaker(bind=engine, expire_on_commit=False)
    s = Session()
    try:
        s.add(DiscoverablePref(
            user_id=str(user_id),
            enabled=enabled,
            expires_at=expires_at,
            current_pubkey=current_pubkey,
            pubkey_registered_at=datetime.utcnow(),
            age_claim_18=age_claim_18,
            face_visible=face_visible,
            avatar_style=avatar_style,
            vibe_tags=vibe_tags or [],
            toggle_window_start=datetime.utcnow(),
        ))
        s.commit()
    finally:
        s.close()


def _seed_sighting(
    engine,
    *,
    owner_user_id,
    peer_user_id,
    peer_pubkey,
    sighted_at,
    expires_at,
    swipe_decision='pending',
    rssi_peak=-40,
    dwell_sec=4,
    lat=12.97,
    lng=77.59,
):
    from sqlalchemy.orm import sessionmaker

    from integrations.social.models import EncounterSighting

    Session = sessionmaker(bind=engine, expire_on_commit=False)
    s = Session()
    sid = f"sight_{secrets.token_urlsafe(12)}"
    try:
        s.add(EncounterSighting(
            id=sid,
            owner_user_id=str(owner_user_id),
            peer_user_id=str(peer_user_id),
            peer_pubkey=peer_pubkey,
            rssi_peak=rssi_peak,
            dwell_sec=dwell_sec,
            lat=lat,
            lng=lng,
            sighted_at=sighted_at,
            swipe_decision=swipe_decision,
            expires_at=expires_at,
        ))
        s.commit()
    finally:
        s.close()
    return sid


# ══════════════════════════════════════════════════════════════════════
# Discoverable TTL auto-off
# ══════════════════════════════════════════════════════════════════════


def test_discoverable_auto_off_after_ttl_blocks_sighting(client, app):
    """Peer's discoverable_prefs.expires_at is in the past — same
    state production produces after wall-time elapses past TTL.  A
    sighting against this peer must 404 'peer not discoverable'
    (neutral message — no leak about why)."""
    pk = 'aabbccdd' * 4
    _seed_pref(
        app.test_engine,
        user_id=20,
        enabled=True,
        expires_at=datetime.utcnow() - timedelta(minutes=1),
        current_pubkey=pk,
    )
    r = client.post(
        '/api/social/encounter/sighting',
        json={'peer_pubkey': pk, 'rssi_peak': -40, 'dwell_sec': 4},
        headers=_as_user(10),
    )
    assert r.status_code == 404
    assert r.get_json()['error'] == 'peer not discoverable'


def test_discoverable_disabled_pref_blocks_sighting(client, app):
    """Even if expires_at is in the future, an explicitly-disabled
    pref must 404.  Defends against the "user toggled off but the
    expiry hasn't arrived yet" case."""
    pk = 'eeff0011' * 4
    _seed_pref(
        app.test_engine,
        user_id=20,
        enabled=False,
        expires_at=datetime.utcnow() + timedelta(hours=1),
        current_pubkey=pk,
    )
    r = client.post(
        '/api/social/encounter/sighting',
        json={'peer_pubkey': pk, 'rssi_peak': -40, 'dwell_sec': 4},
        headers=_as_user(10),
    )
    assert r.status_code == 404
    assert r.get_json()['error'] == 'peer not discoverable'


# ══════════════════════════════════════════════════════════════════════
# Match-window enforcement
# ══════════════════════════════════════════════════════════════════════


def test_sightings_outside_match_window_do_not_match(client, app):
    """Two reciprocal 'like' sightings far apart in time must NOT
    create a match — even when both swipe like.  The window exists
    to refuse forced-pairing-via-replay."""
    pk_a = 'aaaa1111' * 4
    pk_b = 'bbbb2222' * 4
    now = datetime.utcnow()

    # Both peers discoverable.
    _seed_pref(
        app.test_engine,
        user_id=1, enabled=True,
        expires_at=now + timedelta(hours=1),
        current_pubkey=pk_a,
    )
    _seed_pref(
        app.test_engine,
        user_id=2, enabled=True,
        expires_at=now + timedelta(hours=1),
        current_pubkey=pk_b,
    )

    # Sighting #1 was seen long ago; sighting #2 is recent.
    far_in_past = now - timedelta(
        seconds=C.ENCOUNTER_MATCH_WINDOW_SEC + 600,
    )
    s1 = _seed_sighting(
        app.test_engine,
        owner_user_id=1, peer_user_id=2,
        peer_pubkey=pk_b,
        sighted_at=far_in_past,
        expires_at=far_in_past + timedelta(seconds=C.ENCOUNTER_SIGHTING_EXPIRES_SEC),
    )
    s2 = _seed_sighting(
        app.test_engine,
        owner_user_id=2, peer_user_id=1,
        peer_pubkey=pk_a,
        sighted_at=now,
        expires_at=now + timedelta(seconds=C.ENCOUNTER_SIGHTING_EXPIRES_SEC),
    )

    # Both swipe like — reciprocal-check is window-bounded.
    client.post(
        '/api/social/encounter/swipe',
        json={'sighting_id': s1, 'decision': 'like'},
        headers=_as_user(1),
    )
    r = client.post(
        '/api/social/encounter/swipe',
        json={'sighting_id': s2, 'decision': 'like'},
        headers=_as_user(2),
    )
    assert r.status_code == 200
    # No mutual match — sightings out-of-window.
    assert r.get_json()['data']['match_id'] is None

    for uid in (1, 2):
        m = client.get(
            '/api/social/encounter/matches', headers=_as_user(uid),
        )
        assert m.status_code == 200
        assert m.get_json()['data']['matches'] == []


# ══════════════════════════════════════════════════════════════════════
# Sighting auto-expiry
# ══════════════════════════════════════════════════════════════════════


def test_swipe_on_expired_sighting_410(client, app):
    """A sighting whose expires_at has already passed must reject
    swipes with 410 Gone — ephemeral state is past TTL."""
    pk = 'cafe' * 8
    now = datetime.utcnow()
    _seed_pref(
        app.test_engine,
        user_id=20, enabled=True,
        expires_at=now + timedelta(hours=1),
        current_pubkey=pk,
    )
    sid = _seed_sighting(
        app.test_engine,
        owner_user_id=10,
        peer_user_id=20,
        peer_pubkey=pk,
        sighted_at=now - timedelta(hours=25),
        expires_at=now - timedelta(minutes=1),  # already expired
    )
    r = client.post(
        '/api/social/encounter/swipe',
        json={'sighting_id': sid, 'decision': 'like'},
        headers=_as_user(10),
    )
    assert r.status_code == 410
    assert 'expired' in r.get_json()['error'].lower()


# ══════════════════════════════════════════════════════════════════════
# Pubkey rotation unlinkability — drives through the real
# /register-pubkey endpoint twice; no time control needed.
# ══════════════════════════════════════════════════════════════════════


def test_old_pubkey_no_longer_resolves_after_rotation(client):
    """When peer rotates their pubkey via /register-pubkey, the OLD
    pubkey must no longer resolve a sighting.  Unlinkability across
    observation windows is the WHOLE POINT of rotation.

    No mocks: drives /discoverable + /register-pubkey live against
    the test DB."""
    pk_old = 'old0' * 8
    pk_new = 'new1' * 8

    _make_discoverable(client, 20)
    _register_pubkey(client, 20, pk_old)

    # Sighting on pk_old works.
    r = client.post(
        '/api/social/encounter/sighting',
        json={'peer_pubkey': pk_old, 'rssi_peak': -40, 'dwell_sec': 4},
        headers=_as_user(10),
    )
    assert r.status_code == 200

    # Peer rotates.
    _register_pubkey(client, 20, pk_new)

    # Sighting on pk_old now 404 — no DiscoverablePref has it.
    r = client.post(
        '/api/social/encounter/sighting',
        json={'peer_pubkey': pk_old, 'rssi_peak': -40, 'dwell_sec': 4},
        headers=_as_user(10),
    )
    assert r.status_code == 404

    # Sighting on pk_new resolves.
    r = client.post(
        '/api/social/encounter/sighting',
        json={'peer_pubkey': pk_new, 'rssi_peak': -40, 'dwell_sec': 4},
        headers=_as_user(10),
    )
    assert r.status_code == 200


# ══════════════════════════════════════════════════════════════════════
# Cross-user payload isolation — drives real flow, no mocks.
# ══════════════════════════════════════════════════════════════════════


def test_unrelated_user_matches_endpoint_is_empty(client):
    """A user who never sighted anyone must see /matches return []
    even when other users have matches.  No cross-user enumeration."""
    s1, s2 = _setup_mutual_sighting(client)
    client.post(
        '/api/social/encounter/swipe',
        json={'sighting_id': s1, 'decision': 'like'},
        headers=_as_user(1),
    )
    client.post(
        '/api/social/encounter/swipe',
        json={'sighting_id': s2, 'decision': 'like'},
        headers=_as_user(2),
    )

    r = client.get(
        '/api/social/encounter/matches', headers=_as_user(99),
    )
    assert r.status_code == 200
    body = r.get_json()['data']
    assert body['matches'] == []
    assert body['count'] == 0


# ══════════════════════════════════════════════════════════════════════
# Map-pin visibility gate
# ══════════════════════════════════════════════════════════════════════


def test_map_pins_skip_unmatched_pairs(client):
    """A one-sided like must NOT produce a map pin.  Pins are
    post-match adornment only."""
    s1, _ = _setup_mutual_sighting(client)
    client.post(
        '/api/social/encounter/swipe',
        json={'sighting_id': s1, 'decision': 'like'},
        headers=_as_user(1),
    )

    r = client.get(
        '/api/social/encounter/map-pins', headers=_as_user(1),
    )
    assert r.status_code == 200
    body = r.get_json()['data']
    assert body['pins'] == []
    assert body['count'] == 0


# ══════════════════════════════════════════════════════════════════════
# Icebreaker decline-then-approve refusal
# ══════════════════════════════════════════════════════════════════════


def test_icebreaker_approve_after_decline_409(client):
    """Once a side declines an icebreaker, approving a new draft for
    the same side must 409 — declined state is terminal so the
    learn-from-decline signal isn't overwritten by a later flip."""
    m = _matched_pair(client)
    r = client.post(
        '/api/social/encounter/icebreaker/decline',
        json={'match_id': m, 'reason': 'not interested'},
        headers=_as_user(1),
    )
    assert r.status_code == 200

    r = client.post(
        '/api/social/encounter/icebreaker/approve',
        json={'match_id': m, 'text': 'changed my mind'},
        headers=_as_user(1),
    )
    assert r.status_code == 409
