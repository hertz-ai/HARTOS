"""Unit tests for integrations/social/icebreaker_service.draft_icebreaker
(closes #399 service contract).

No mocks on production code.  The service is a pure function; tests
seed Encounter + DiscoverablePref rows directly via the same engine
the endpoint tests use, then call the function with a real session.
The optional `llm_callback` is a real callable test parameter — the
function's contract is "use the callback when supplied; fall back to
templates when the callback raises or returns empty."  The callback
in tests is just an in-test lambda that exercises the contract.
"""
from __future__ import annotations

import os
import sys
from datetime import datetime, timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

PROJECT_ROOT = os.path.abspath(
    os.path.join(os.path.dirname(__file__), '..', '..'),
)
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from core import constants as C  # noqa: E402
from integrations.social.icebreaker_service import (  # noqa: E402
    draft_icebreaker,
)


@pytest.fixture
def session():
    """Fresh in-memory SQLite + the encounter tables created."""
    from integrations.social.models import (  # noqa: F401
        DiscoverablePref, Encounter, EncounterSighting,
    )
    engine = create_engine('sqlite:///:memory:')
    EncounterSighting.__table__.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, expire_on_commit=False)
    s = Session()
    yield s
    s.close()
    engine.dispose()


def _seed_match(session, *, user_a, user_b, payload=None):
    from integrations.social.models import Encounter
    m = Encounter(
        id=f'match_{user_a}_{user_b}',
        user_a_id=str(user_a),
        user_b_id=str(user_b),
        context_type='ble',
        context_id='ctx_test',
        encounter_count=1,
        is_mutual_aware=True,
        bond_level=1,
        first_at=datetime.utcnow(),
        latest_at=datetime.utcnow(),
        payload=payload or {},
    )
    session.add(m)
    session.commit()
    return m.id


def _seed_pref(session, *, user_id, vibe_tags=None, avatar_style='studio_ghibli'):
    from integrations.social.models import DiscoverablePref
    s = DiscoverablePref(
        user_id=str(user_id),
        enabled=True,
        enabled_at=datetime.utcnow(),
        expires_at=datetime.utcnow() + timedelta(hours=1),
        age_claim_18=True,
        avatar_style=avatar_style,
        vibe_tags=vibe_tags or [],
        toggle_window_start=datetime.utcnow(),
    )
    session.add(s)
    session.commit()


# ══════════════════════════════════════════════════════════════════════
# Happy path + tag anchoring
# ══════════════════════════════════════════════════════════════════════


def test_returns_draft_with_shared_tag(session):
    mid = _seed_match(session, user_a=1, user_b=2)
    _seed_pref(session, user_id=1, vibe_tags=['hiking', 'chai'])
    _seed_pref(session, user_id=2, vibe_tags=['indie_film', 'hiking'])

    out = draft_icebreaker(mid, '1', session)
    assert out['source'] == 'template'
    assert out['shared_tag'] == 'hiking'
    assert 'hiking' in out['draft']
    assert out['length'] == len(out['draft'])
    assert out['length'] <= C.ENCOUNTER_DRAFT_MAX_CHARS
    assert len(out['alt_drafts']) == 2
    for alt in out['alt_drafts']:
        assert alt and len(alt) <= C.ENCOUNTER_DRAFT_MAX_CHARS


def test_falls_back_to_peer_tag_when_no_overlap(session):
    mid = _seed_match(session, user_a=1, user_b=2)
    _seed_pref(session, user_id=1, vibe_tags=['chai'])
    _seed_pref(session, user_id=2, vibe_tags=['indie_film'])

    out = draft_icebreaker(mid, '1', session)
    # Peer's first tag is what the draft anchors on.
    assert out['shared_tag'] == 'indie_film'
    assert 'indie_film' in out['draft']


def test_neutral_template_when_no_tags(session):
    mid = _seed_match(session, user_a=1, user_b=2)
    _seed_pref(session, user_id=1, vibe_tags=[])
    _seed_pref(session, user_id=2, vibe_tags=[])

    out = draft_icebreaker(mid, '1', session)
    assert out['shared_tag'] is None
    assert out['draft']  # non-empty
    assert 'neutral opener' in out['rationale']


def test_neutral_template_when_no_pref_rows(session):
    """No DiscoverablePref rows on either side — function shouldn't
    raise, just produce a neutral template."""
    mid = _seed_match(session, user_a=1, user_b=2)
    out = draft_icebreaker(mid, '1', session)
    assert out['shared_tag'] is None
    assert out['draft']


# ══════════════════════════════════════════════════════════════════════
# LLM callback contract
# ══════════════════════════════════════════════════════════════════════


def test_llm_callback_used_when_supplied(session):
    mid = _seed_match(session, user_a=1, user_b=2)
    _seed_pref(session, user_id=1, vibe_tags=['hiking'])
    _seed_pref(session, user_id=2, vibe_tags=['hiking'])

    seen = {}

    def cb(ctx):
        seen.update(ctx)
        return 'CUSTOM-LLM-DRAFT'

    out = draft_icebreaker(mid, '1', session, llm_callback=cb)
    assert out['source'] == 'llm'
    assert out['draft'] == 'CUSTOM-LLM-DRAFT'
    assert seen['shared_tag'] == 'hiking'
    assert seen['peer_user_id'] == '2'


def test_llm_callback_failure_falls_back_to_template(session):
    mid = _seed_match(session, user_a=1, user_b=2)
    _seed_pref(session, user_id=1, vibe_tags=['chai'])
    _seed_pref(session, user_id=2, vibe_tags=['chai'])

    def cb(ctx):
        raise RuntimeError('llm exploded')

    out = draft_icebreaker(mid, '1', session, llm_callback=cb)
    assert out['source'] == 'template'
    assert 'chai' in out['draft']  # template anchored on shared tag


def test_llm_callback_empty_string_falls_back(session):
    mid = _seed_match(session, user_a=1, user_b=2)
    out = draft_icebreaker(
        mid, '1', session,
        llm_callback=lambda ctx: '   ',  # whitespace only
    )
    assert out['source'] == 'template'
    assert out['draft']


def test_llm_callback_non_string_falls_back(session):
    mid = _seed_match(session, user_a=1, user_b=2)
    out = draft_icebreaker(
        mid, '1', session,
        llm_callback=lambda ctx: 42,  # not a string
    )
    assert out['source'] == 'template'


# ══════════════════════════════════════════════════════════════════════
# Length cap (sentence-aware trim)
# ══════════════════════════════════════════════════════════════════════


def test_oversize_llm_output_is_trimmed_at_sentence_boundary(session):
    mid = _seed_match(session, user_a=1, user_b=2)
    long_text = ('Hi! Saw the show. ' * 30).strip()
    out = draft_icebreaker(
        mid, '1', session,
        llm_callback=lambda ctx: long_text,
    )
    assert out['source'] == 'llm'
    assert len(out['draft']) <= C.ENCOUNTER_DRAFT_MAX_CHARS
    # Sentence-aware: the draft should still end on a sentence boundary
    # (period, ?, !, or newline).
    assert out['draft'][-1] in {'.', '!', '?'}


def test_oversize_llm_output_no_boundary_hard_cut(session):
    mid = _seed_match(session, user_a=1, user_b=2)
    # No sentence boundary at all → forced ellipsis cut.
    blob = 'A' * (C.ENCOUNTER_DRAFT_MAX_CHARS + 200)
    out = draft_icebreaker(
        mid, '1', session,
        llm_callback=lambda ctx: blob,
    )
    assert out['source'] == 'llm'
    assert len(out['draft']) <= C.ENCOUNTER_DRAFT_MAX_CHARS
    assert out['draft'].endswith('…')


# ══════════════════════════════════════════════════════════════════════
# Validation errors
# ══════════════════════════════════════════════════════════════════════


def test_unknown_match_raises(session):
    with pytest.raises(ValueError) as exc:
        draft_icebreaker('match_does_not_exist', '1', session)
    assert 'not found' in str(exc.value)


def test_non_participant_viewer_raises(session):
    mid = _seed_match(session, user_a=1, user_b=2)
    _seed_pref(session, user_id=1, vibe_tags=['x'])
    _seed_pref(session, user_id=2, vibe_tags=['y'])
    with pytest.raises(ValueError) as exc:
        draft_icebreaker(mid, '99', session)
    assert 'not a party' in str(exc.value)


def test_non_ble_match_raises(session):
    """A community-context encounter row must NOT be drafted-against
    by the BLE icebreaker service.  Different feature, different
    semantics, separate code path.  Refusing is the right behavior."""
    from integrations.social.models import Encounter
    e = Encounter(
        id='community_match',
        user_a_id='1',
        user_b_id='2',
        context_type='community',  # NOT 'ble'
        context_id='community_xyz',
        encounter_count=1,
        first_at=datetime.utcnow(),
        latest_at=datetime.utcnow(),
    )
    session.add(e)
    session.commit()

    with pytest.raises(ValueError):
        draft_icebreaker('community_match', '1', session)


# ══════════════════════════════════════════════════════════════════════
# Topology gate (consent-gated cloud, allow-by-default edge)
#
# Drives the same env var production reads (HEVOLVE_NODE_TIER) — no
# monkeypatch on production code.  get_node_tier() reads env on every
# call (uncached), so setenv before each draft_icebreaker call gives
# us live tier control.
# ══════════════════════════════════════════════════════════════════════


def test_flat_topology_allows_drafting_no_consent_needed(session):
    """Flat = single user, single machine.  Drafting is unconditional —
    the user IS the edge."""
    mid = _seed_match(session, user_a=1, user_b=2)
    out = draft_icebreaker(mid, '1', session, topology='flat')
    assert out['draft']


def test_regional_topology_allows_drafting_no_consent_needed(session):
    """Regional = LAN cluster of user-trusted devices.  Still edge."""
    mid = _seed_match(session, user_a=1, user_b=2)
    out = draft_icebreaker(mid, '1', session, topology='regional')
    assert out['draft']


def test_central_topology_without_consent_raises_permission_error(session):
    """Central = cloud.  Drafting needs explicit cloud-capability
    consent; refusing without it surfaces a PermissionError the
    endpoint maps to 403."""
    mid = _seed_match(session, user_a=1, user_b=2)
    with pytest.raises(PermissionError) as exc:
        draft_icebreaker(mid, '1', session, topology='central')
    assert 'cloud_capability' in str(exc.value)


def test_central_topology_with_explicit_consent_allows(session):
    mid = _seed_match(session, user_a=1, user_b=2)
    out = draft_icebreaker(
        mid, '1', session,
        topology='central',
        cloud_consent_check=lambda uid: uid == '1',
    )
    assert out['draft']


def test_central_topology_consent_check_returning_false_raises(session):
    mid = _seed_match(session, user_a=1, user_b=2)
    with pytest.raises(PermissionError):
        draft_icebreaker(
            mid, '1', session,
            topology='central',
            cloud_consent_check=lambda uid: False,
        )


def test_central_topology_consent_check_per_user(session):
    """Consent is per-user; user 1 may have opted in while user 2 has
    not.  The check is invoked with viewer_user_id."""
    mid = _seed_match(session, user_a=1, user_b=2)
    consent = {'1': True, '2': False}

    out = draft_icebreaker(
        mid, '1', session,
        topology='central',
        cloud_consent_check=lambda uid: consent.get(uid, False),
    )
    assert out['draft']

    with pytest.raises(PermissionError):
        draft_icebreaker(
            mid, '2', session,
            topology='central',
            cloud_consent_check=lambda uid: consent.get(uid, False),
        )
