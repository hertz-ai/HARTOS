"""
HevolveSocial - Encounter Icebreaker Blueprint (PR-A — DB-backed).

Physical-world P2P encounter flow: two nearby Nunba users both set
'discoverable' on Hevolve Android, their phones do autonomous BLE
sighting correlation via close-range RSSI + dwell + compass alignment,
each user swipes like/dislike on the other's diffusion-styled avatar
(NO real photo, NO camera, NO upload), and a mutual-like fires the
encounter_icebreaker_agent to draft a warm opener for user approval.

Full design: Claude-memory/project_encounter_icebreaker.md
Seeded agent goal: integrations.agent_engine.goal_seeding
 .SEED_BOOTSTRAP_GOALS[slug='encounter_icebreaker_agent']

Persistence (v39 — see migrations.py):
  * discoverable_prefs        per-user toggle state + rotating pubkey
  * encounter_sightings       ephemeral pre-match swipe state (24h TTL)
  * encounters (extended)     durable post-match record with
                              context_type='ble', lat/lng map pin,
                              payload JSON for per-side icebreaker state.
  Mutual matches upsert into the canonical `encounters` table so they
  participate in the same bond_level / is_mutual_aware progression as
  community / post / region encounters — single graph, single SwarmCanvas
  surface, no parallel match concept.

Endpoints all mounted at /api/social/encounter/*  (JWT-auth required):

  POST /discoverable     enable/disable broadcast + TTL + age gate
  GET  /discoverable     current state + remaining TTL + toggle count
  GET  /persona          the user's persona card (bio, recognize_me, tags)
  PUT  /persona          edit the card; not a discoverable toggle
  POST /sighting         phone reports a BLE sighting; returns swipe card
  POST /swipe            like/dislike decision (signed event)
  GET  /matches          list of MUTUAL matches (one-sided never leaks)
  GET  /map-pins         post-match encounter pins the user kept visible
  POST /icebreaker/approve   send approved draft (agent integration: PR-C)
  POST /icebreaker/decline   reject draft; agent learns from reason
  POST /register-pubkey  phone registers current rotating pubkey
  GET  /topics           WAMP topic constants

Invariants enforced server-side (the blocking privacy gates):

  1. One-sided likes are write-only from the liker's perspective — no
     endpoint returns them.  The likee never learns they were liked
     unless THEY also swiped like within the match window.
  2. A match row is created only when BOTH sightings are within
     ENCOUNTER_MATCH_WINDOW_SEC of each other AND both swiped 'like'.
  3. Location is captured at match time from the sightings, never
     from the user's current reported location (prevents forged pins).
  4. Discoverable default OFF, auto-expires after ENCOUNTER_DISCOVERABLE
     _TTL_SEC, max ENCOUNTER_DISCOVERABLE_MAX_TOGGLES_24H per day.
  5. 18+ age claim required at toggle time.
  6. All pubkeys are rotating (scheme rotates every
     ENCOUNTER_PUBKEY_ROTATION_SEC on the phone); server stores only
     the rotating value, never the user's master identity.
  7. A user's vibe_tags reach anyone else only while their
     interests_discoverable is true (icebreaker_service.
     vibe_tags_others_may_see); consent flags are parsed strictly
     (_flags), so the string 'false' is never a yes.
"""
from __future__ import annotations

import hashlib
import logging
import math
import secrets
from datetime import datetime, timedelta
from typing import Any, Optional

from flask import Blueprint, g, jsonify, request
from sqlalchemy.orm.attributes import flag_modified

from core.constants import (
    ENCOUNTER_DISCOVERABLE_MAX_TOGGLES_24H,
    ENCOUNTER_DISCOVERABLE_TTL_SEC,
    ENCOUNTER_DRAFT_MAX_CHARS,
    ENCOUNTER_MATCH_WINDOW_SEC,
    ENCOUNTER_PERSONA_BIO_MAX_CHARS,
    ENCOUNTER_PERSONA_MAX_TAGS,
    ENCOUNTER_PERSONA_RECOGNIZE_MAX_CHARS,
    ENCOUNTER_PERSONA_TAG_MAX_CHARS,
    ENCOUNTER_SIGHTING_EXPIRES_SEC,
    ENCOUNTER_TOPIC_ICEBREAKER,
    ENCOUNTER_TOPIC_MATCH,
    ENCOUNTER_TOPIC_SIGHTING,
    ENCOUNTER_TOPIC_SWIPE,
)

from .auth import require_auth
from .icebreaker_service import vibe_tags_others_may_see
from .models import (
    DiscoverablePref,
    Encounter,
    EncounterSighting,
)

logger = logging.getLogger('hevolve_social')

encounter_bp = Blueprint('encounter', __name__, url_prefix='/api/social')


# ──────────────────────────────────────────────────────────────────────
# Tiny helpers — response shape mirrors the rest of /api/social/*.
# ──────────────────────────────────────────────────────────────────────

from .api_common import _ok, _err  # single-sourced envelope helpers (#97)


class _BodyNotAnObject(Exception):
    """The request body is JSON but not an object (a list, a string, a
    number).  Every handler reads fields with body.get, so it answers 400
    here instead of crashing with a 500."""


def _json() -> dict[str, Any]:
    """The request's JSON object; {} for no body or a body that is not
    JSON.  Raises _BodyNotAnObject (answered 400 by the blueprint) for
    JSON that is not an object.  Handlers call it after @require_auth, so
    an unauthenticated caller still gets 401 first."""
    body = request.get_json(force=True, silent=True)
    if body is None:
        return {}
    if not isinstance(body, dict):
        raise _BodyNotAnObject()
    return body


@encounter_bp.errorhandler(_BodyNotAnObject)
def _body_not_an_object(_exc):
    return _err('request body must be a JSON object')


def _text(body: dict, key: str, cap: int):
    """A free-text field from the body, capped: (value, None), or
    (None, error) when the value is not a string.  Never str() of
    whatever arrived (that stored "None" and "{'k': 'v'}")."""
    raw = body[key]
    if not isinstance(raw, str):
        return None, f'{key} must be a string'
    return raw[:cap], None


def _now_dt() -> datetime:
    return datetime.utcnow()


def _user_id() -> Optional[str]:
    """Resolve current user_id as string (matches schema's String(64)).

    Auth populates `g.user` with an id that may be int (legacy local
    test fixtures) or str (real DB rows).  Coerce to str so DB queries
    match the FK column type.  Returns None if not authenticated.
    """
    user = getattr(g, 'user', None)
    if user is None:
        return None
    uid = getattr(user, 'id', None)
    if uid is None and isinstance(user, dict):
        uid = user.get('id')
    return str(uid) if uid is not None else None


def _clean_tags(raw) -> Optional[list[str]]:
    """The user's interest tags, capped the one way every writer caps them.
    None when the value is not a list of strings (the caller answers 400);
    a non-string tag is refused, never stored as its str() form."""
    if not isinstance(raw, list) or not all(isinstance(t, str) for t in raw):
        return None
    return [str(t)[:ENCOUNTER_PERSONA_TAG_MAX_CHARS]
            for t in raw[:ENCOUNTER_PERSONA_MAX_TAGS]]


_FLAG_STRINGS = {'true': True, 'false': False}


def _flags(body: dict, defaults: dict[str, Optional[bool]]):
    """Read the named yes/no flags from a request body, strictly.

    A flag is a JSON boolean or exactly the string 'true' / 'false'; a key
    the body does not name takes its default.  Anything else ('False',
    'yes', 1, null, ...) is refused, never guessed: these are consent
    flags, and bool('false') is True.  Returns (values, None) or
    (None, error message).  The codebase's other parsers
    (api_compute_earnings._parse_bool, compute_config._parse_bool,
    video_orchestrator._to_bool, core.platform.config._convert_bool) map
    unknown input to a value instead of refusing it, so they do not fit.
    """
    out = {}
    for key, default in defaults.items():
        if key not in body:
            out[key] = default
            continue
        raw = body[key]
        if isinstance(raw, bool):
            out[key] = raw
        elif isinstance(raw, str) and raw in _FLAG_STRINGS:
            out[key] = _FLAG_STRINGS[raw]
        else:
            return None, f'{key} must be true or false'
    return out, None


def _number(body: dict, key: str, default, *, whole: bool = True,
            nullable: bool = False):
    """A numeric field from the body: (value, None) or (None, error).

    A key the body does not name takes `default`; null is allowed only
    when `nullable`.  A JSON bool, a string ('abc' or '3600'), a list or a
    non-finite number is refused (int('abc') used to answer 500).  With
    `whole`, only a JSON integer is accepted."""
    if key not in body:
        return default, None
    raw = body[key]
    if raw is None and nullable:
        return None, None
    kinds = (int,) if whole else (int, float)
    error = (f'{key} must be a whole number' if whole
             else f'{key} must be a number')
    if isinstance(raw, bool) or not isinstance(raw, kinds):
        return None, error
    # Whole integers (including TTLs subsequently clamped by the caller)
    # are finite without conversion. Coordinate fields must fit a float.
    if not whole:
        try:
            finite = math.isfinite(raw)
        except OverflowError:
            finite = False
        if not finite:
            return None, error
    return raw, None


def _new_id(prefix: str) -> str:
    """16-byte urlsafe id.  Not a secret — used as a handle only."""
    return f"{prefix}_{secrets.token_urlsafe(12)}"


def _ble_pair_context_id(sighting_a_id: str, sighting_b_id: str) -> str:
    """Deterministic context_id for the BLE match's encounters row.

    Hashes the two sighting ids sorted so (A,B) and (B,A) collapse to
    the same row.  sha1 is fine — this is just a discriminator, not a
    secret.  Truncate to 32 chars to fit in encounters.context_id
    (VARCHAR(64)).
    """
    combined = ":".join(sorted([sighting_a_id, sighting_b_id]))
    return hashlib.sha1(combined.encode('utf-8')).hexdigest()[:32]


def _icebreaker_payload_init() -> dict[str, Any]:
    """Initial JSON payload for a fresh BLE encounters row."""
    return {
        'icebreaker_a': {
            'status': 'pending', 'text': None,
            'sent_at': None, 'decline_reason': None,
        },
        'icebreaker_b': {
            'status': 'pending', 'text': None,
            'sent_at': None, 'decline_reason': None,
        },
        'map_pin_visible': True,
    }


def _icebreaker_side_for(match, uid: str) -> Optional[str]:
    """Return 'a' or 'b' for which side of the match `uid` represents."""
    if match.user_a_id == uid:
        return 'a'
    if match.user_b_id == uid:
        return 'b'
    return None


# ──────────────────────────────────────────────────────────────────────
# WAMP publishers — routed via core.peer_link.message_bus, same path
# chat_messages.publish_new uses (chat_messages.py:282).  Lazy import
# + best-effort: a missing message_bus must never break the request.
#
# Topic format: f"{ENCOUNTER_TOPIC_*}.{user_id}" — per-user suffix
# matches the chat-sync convention (chat_messages.py:300) and the
# wamp_router subscription-authorization pattern (wamp_router.py — every
# topic ending with `.<viewer_uid>` is delivered only to that viewer).
# ──────────────────────────────────────────────────────────────────────


def _publish_to_topic(topic: str, payload: dict) -> None:
    """Publish to a single WAMP topic via message_bus.  Silent on failure
    (missing peer_link is not a request failure)."""
    try:
        from core.peer_link.message_bus import get_message_bus
    except ImportError:
        logger.debug('encounter_api: message_bus unavailable for %s', topic)
        return
    try:
        bus = get_message_bus()
        bus.publish(topic, payload)
    except Exception as exc:  # noqa: BLE001
        logger.debug('encounter_api: publish %s failed: %s', topic, exc)


def _publish_match(match: Encounter) -> None:
    """Fire encounter.match WAMP for both participants.  One per
    user_id — the wamp_router authorization layer confines each
    topic to its named viewer."""
    payload = _match_to_dict(match)
    for uid in (match.user_a_id, match.user_b_id):
        if uid:
            _publish_to_topic(f"{ENCOUNTER_TOPIC_MATCH}.{uid}", payload)


def _publish_icebreaker(match: Encounter, side: str, status: str) -> None:
    """Fire encounter.icebreaker WAMP for both participants when a side
    flips to sent / declined.  The likee learns about a sent draft;
    the agent on either device learns about a decline (so it can
    update its memory_graph learn-from-decline signal)."""
    p = match.payload or {}
    payload = {
        'match_id': match.id,
        'side': side,
        'status': status,
        'icebreaker_a': p.get('icebreaker_a') or {},
        'icebreaker_b': p.get('icebreaker_b') or {},
    }
    for uid in (match.user_a_id, match.user_b_id):
        if uid:
            _publish_to_topic(
                f"{ENCOUNTER_TOPIC_ICEBREAKER}.{uid}", payload,
            )


# ──────────────────────────────────────────────────────────────────────
# Test hook — kept for fixture compatibility; each test creates a fresh
# in-memory SQLite engine so this is now a no-op.  Preserved so the
# existing `app` fixture's `encounter_api.ENCOUNTER_STORE.clear()` call
# does not need to change shape during the v39 swap.
# ──────────────────────────────────────────────────────────────────────

class _NoopStore:
    """Vestige of the pre-v39 in-memory store.  All persistence now
    lives in the DB; tests get a fresh engine per fixture."""

    def clear(self) -> None:  # noqa: D401 — kept for fixture parity
        return None


ENCOUNTER_STORE = _NoopStore()


# ──────────────────────────────────────────────────────────────────────
# /discoverable — toggle the BLE broadcast + age + TTL state.
# ──────────────────────────────────────────────────────────────────────

@encounter_bp.route('/encounter/discoverable', methods=['GET'])
@require_auth
def get_discoverable():
    uid = _user_id()
    if uid is None:
        return _err('unauthenticated', 401)
    pref = g.db.query(DiscoverablePref).filter_by(user_id=uid).first()
    now = _now_dt()
    if not pref:
        return _ok({
            'enabled': False,
            'expires_at': None,
            'remaining_sec': 0,
            'toggle_count_24h': 0,
            'age_claim_18': False,
            'face_visible': False,
            'avatar_style': 'studio_ghibli',
            'vibe_tags': [],
        })
    remaining = (
        max(0, int((pref.expires_at - now).total_seconds()))
        if pref.expires_at else 0
    )
    still_on = bool(pref.enabled) and remaining > 0
    return _ok({
        'enabled': still_on,
        'expires_at': pref.expires_at.isoformat() if pref.expires_at else None,
        'remaining_sec': remaining,
        'toggle_count_24h': pref.toggle_count_24h or 0,
        'age_claim_18': bool(pref.age_claim_18),
        'face_visible': bool(pref.face_visible),
        'avatar_style': pref.avatar_style or 'studio_ghibli',
        'vibe_tags': pref.vibe_tags or [],
    })


@encounter_bp.route('/encounter/discoverable', methods=['POST'])
@require_auth
def set_discoverable():
    """Turn discoverable on/off.  Server-side invariants:
      * 18+ age claim required to enable
      * TTL capped at ENCOUNTER_DISCOVERABLE_TTL_SEC
      * Max ENCOUNTER_DISCOVERABLE_MAX_TOGGLES_24H toggles per 24h
    """
    uid = _user_id()
    if uid is None:
        return _err('unauthenticated', 401)
    body = _json()
    # enabled / age_claim_18 left out are a no (consent is never implied);
    # face_visible and avatar_style left out keep what is stored, like
    # vibe_tags below (a toggle used to reset them).
    flags, bad = _flags(body, {'enabled': False, 'age_claim_18': False,
                               'face_visible': None})
    if bad:
        return _err(bad)
    enable = flags['enabled']
    ttl, bad = _number(body, 'ttl_sec', ENCOUNTER_DISCOVERABLE_TTL_SEC)
    if bad:
        return _err(bad)
    if ttl <= 0 or ttl > ENCOUNTER_DISCOVERABLE_TTL_SEC:
        ttl = ENCOUNTER_DISCOVERABLE_TTL_SEC
    age_claim = flags['age_claim_18']
    face_visible = flags['face_visible']
    avatar_style = None
    if 'avatar_style' in body:
        avatar_style, bad = _text(body, 'avatar_style', 64)
        if bad:
            return _err(bad)
    # vibe_tags is also written by PUT /encounter/persona, so a toggle that
    # does not name them leaves the user's tags alone (it used to reset
    # them to []).
    vibe_tags = None
    if 'vibe_tags' in body:
        vibe_tags = _clean_tags(body['vibe_tags'])
        if vibe_tags is None:
            return _err('vibe_tags must be a list of strings')

    now = _now_dt()
    pref = g.db.query(DiscoverablePref).filter_by(user_id=uid).first()
    if pref is None:
        pref = DiscoverablePref(
            user_id=uid,
            toggle_window_start=now,
            toggle_count_24h=0,
        )
        g.db.add(pref)

    # 24h sliding toggle-count window.
    if pref.toggle_window_start and \
            (now - pref.toggle_window_start).total_seconds() > 24 * 3600:
        pref.toggle_window_start = now
        pref.toggle_count_24h = 0
    if (pref.toggle_count_24h or 0) >= ENCOUNTER_DISCOVERABLE_MAX_TOGGLES_24H:
        return _err(
            f'toggle limit reached '
            f'({ENCOUNTER_DISCOVERABLE_MAX_TOGGLES_24H} per 24h)',
            429,
        )
    if enable and not age_claim:
        return _err('age_claim_18 must be true to enable discoverable', 403)

    pref.enabled = enable
    pref.enabled_at = now if enable else pref.enabled_at
    pref.expires_at = (now + timedelta(seconds=ttl)) if enable else None
    pref.age_claim_18 = age_claim
    if face_visible is not None:
        pref.face_visible = face_visible
    if avatar_style is not None:
        pref.avatar_style = avatar_style
    if vibe_tags is not None:
        pref.vibe_tags = vibe_tags
    pref.toggle_count_24h = (pref.toggle_count_24h or 0) + 1
    pref.last_toggle_at = now
    g.db.commit()

    return _ok({
        'enabled': enable,
        'expires_at': pref.expires_at.isoformat() if pref.expires_at else None,
        'remaining_sec': ttl if enable else 0,
    })


# ──────────────────────────────────────────────────────────────────────
# /persona — the user's persona card: what their agent may tell a
# matched person's agent (bio, "how to recognise me", interest tags) and
# whether they may be matched on interests at all.  Editing the card is
# not a discoverable toggle: it spends no toggle and never turns the BLE
# broadcast on.
# ──────────────────────────────────────────────────────────────────────

def _persona_dict(pref: Optional[DiscoverablePref]) -> dict[str, Any]:
    if pref is None:
        return {'bio': '', 'recognize_me': '', 'vibe_tags': [],
                'interests_discoverable': False}
    return {
        'bio': pref.bio or '',
        'recognize_me': pref.recognize_me or '',
        'vibe_tags': pref.vibe_tags or [],
        'interests_discoverable': bool(pref.interests_discoverable),
    }


@encounter_bp.route('/encounter/persona', methods=['GET'])
@require_auth
def get_persona():
    uid = _user_id()
    if uid is None:
        return _err('unauthenticated', 401)
    pref = g.db.query(DiscoverablePref).filter_by(user_id=uid).first()
    return _ok(_persona_dict(pref))


@encounter_bp.route('/encounter/persona', methods=['PUT'])
@require_auth
def set_persona():
    """Update any of bio, recognize_me, vibe_tags, interests_discoverable.
    Fields the body does not name are left as they are."""
    uid = _user_id()
    if uid is None:
        return _err('unauthenticated', 401)
    body = _json()
    tags = None
    if 'vibe_tags' in body:
        tags = _clean_tags(body['vibe_tags'])
        if tags is None:
            return _err('vibe_tags must be a list of strings')
    flags, bad = _flags(body, {'interests_discoverable': None})
    if bad:
        return _err(bad)
    texts = {}
    for key, cap in (('bio', ENCOUNTER_PERSONA_BIO_MAX_CHARS),
                     ('recognize_me', ENCOUNTER_PERSONA_RECOGNIZE_MAX_CHARS)):
        if key in body:
            texts[key], bad = _text(body, key, cap)
            if bad:
                return _err(bad)

    pref = g.db.query(DiscoverablePref).filter_by(user_id=uid).first()
    if pref is None:
        pref = DiscoverablePref(user_id=uid, toggle_window_start=_now_dt(),
                                toggle_count_24h=0)
        g.db.add(pref)
    if 'bio' in texts:
        pref.bio = texts['bio']
    if 'recognize_me' in texts:
        pref.recognize_me = texts['recognize_me']
    if tags is not None:
        pref.vibe_tags = tags
    if flags['interests_discoverable'] is not None:
        pref.interests_discoverable = flags['interests_discoverable']
    g.db.commit()
    return _ok(_persona_dict(pref))


# ──────────────────────────────────────────────────────────────────────
# /sighting — phone reports a BLE sighting.  Returns swipe-card payload
# if the peer is still discoverable and opted in; returns 404 otherwise
# (the likee's phone would simply never produce a card for a non-
# discoverable peer — no leak surface).
# ──────────────────────────────────────────────────────────────────────

@encounter_bp.route('/encounter/sighting', methods=['POST'])
@require_auth
def report_sighting():
    uid = _user_id()
    if uid is None:
        return _err('unauthenticated', 401)
    body = _json()
    peer_pubkey = str(body.get('peer_pubkey', '')).strip().lower()
    nums = {}
    for key, default, whole in (('rssi_peak', 0, True), ('dwell_sec', 0, True),
                                ('lat', None, False), ('lng', None, False)):
        nums[key], bad = _number(body, key, default, whole=whole,
                                 nullable=not whole)
        if bad:
            return _err(bad)
    rssi_peak, dwell_sec = nums['rssi_peak'], nums['dwell_sec']
    lat, lng = nums['lat'], nums['lng']
    if not peer_pubkey or len(peer_pubkey) < 16:
        return _err('peer_pubkey required (hex, >=16 chars)')

    now = _now_dt()
    # Resolve peer via the rotating-pubkey reverse index on
    # discoverable_prefs.current_pubkey.  If the peer hasn't registered
    # this pubkey OR isn't discoverable OR has expired → 404 with a
    # neutral message.  No information is leaked about which case it is.
    peer_pref = g.db.query(DiscoverablePref).filter_by(
        current_pubkey=peer_pubkey,
    ).first()
    if not peer_pref:
        return _err('peer not discoverable', 404)
    peer_uid = peer_pref.user_id
    if peer_uid == uid:
        return _err('self-sighting rejected')
    if (
        not peer_pref.enabled
        or not peer_pref.expires_at
        or peer_pref.expires_at < now
    ):
        return _err('peer not discoverable', 404)

    sighting = EncounterSighting(
        id=_new_id('sight'),
        owner_user_id=uid,
        peer_user_id=peer_uid,
        peer_pubkey=peer_pubkey,
        rssi_peak=rssi_peak,
        dwell_sec=dwell_sec,
        lat=float(lat) if lat is not None else None,
        lng=float(lng) if lng is not None else None,
        sighted_at=now,
        swipe_decision='pending',
        expires_at=now + timedelta(seconds=ENCOUNTER_SIGHTING_EXPIRES_SEC),
    )
    g.db.add(sighting)
    g.db.commit()

    return _ok({
        'sighting_id': sighting.id,
        'peer_anon_id': peer_pubkey[:12],
        'avatar_style': peer_pref.avatar_style or 'studio_ghibli',
        # A stranger's card: tags only if the peer said yes to sharing
        # interests.  Being discoverable is not that yes.
        'vibe_tags': vibe_tags_others_may_see(peer_pref),
        'face_visible': bool(peer_pref.face_visible),
        'expires_at': sighting.expires_at.isoformat(),
    })


# ──────────────────────────────────────────────────────────────────────
# /swipe — like/dislike decision.  Server checks for a mutual like
# within ENCOUNTER_MATCH_WINDOW_SEC and, if found, upserts an encounters
# row with context_type='ble'.  ONE-SIDED LIKES NEVER LEAK — they live
# only on the liker's sighting row as swipe_decision='like' with no
# peer-side visibility.  The response shape is symmetric: even when no
# match, the client gets the same fields aside from the match_id.
# ──────────────────────────────────────────────────────────────────────

@encounter_bp.route('/encounter/swipe', methods=['POST'])
@require_auth
def swipe():
    uid = _user_id()
    if uid is None:
        return _err('unauthenticated', 401)
    body = _json()
    sighting_id = str(body.get('sighting_id', ''))
    decision = str(body.get('decision', '')).lower()
    if decision not in {'like', 'dislike'}:
        return _err("decision must be 'like' or 'dislike'")
    if not sighting_id:
        return _err('sighting_id required')

    now = _now_dt()
    sighting = g.db.query(EncounterSighting).filter_by(id=sighting_id).first()
    if not sighting or sighting.owner_user_id != uid:
        return _err('sighting not found', 404)
    if sighting.expires_at and sighting.expires_at < now:
        return _err('sighting expired', 410)
    if sighting.swipe_decision != 'pending':
        return _err('already swiped', 409)

    sighting.swipe_decision = decision
    matched_id: Optional[str] = None

    if decision == 'like':
        peer_uid = sighting.peer_user_id
        # Reciprocal-like check within match window.
        window_start = sighting.sighted_at - timedelta(
            seconds=ENCOUNTER_MATCH_WINDOW_SEC,
        )
        window_end = sighting.sighted_at + timedelta(
            seconds=ENCOUNTER_MATCH_WINDOW_SEC,
        )
        reciprocal = g.db.query(EncounterSighting).filter(
            EncounterSighting.id != sighting_id,
            EncounterSighting.owner_user_id == peer_uid,
            EncounterSighting.peer_user_id == uid,
            EncounterSighting.swipe_decision == 'like',
            EncounterSighting.sighted_at >= window_start,
            EncounterSighting.sighted_at <= window_end,
        ).first()
        if reciprocal is not None:
            # Mutual match — write canonical encounters row.  Pin at
            # the midpoint of the two sighting locations (if both
            # reported lat/lng).  Canonical (user_a, user_b) ordering
            # so we never double-create rows for (A,B) and (B,A).
            a_uid, b_uid = sorted((uid, peer_uid))
            la, lb = sighting.lat, reciprocal.lat
            lat = ((la + lb) / 2) if (la is not None and lb is not None) else None
            lga, lgb = sighting.lng, reciprocal.lng
            lng = ((lga + lgb) / 2) if (lga is not None and lgb is not None) else None
            ctx_id = _ble_pair_context_id(sighting.id, reciprocal.id)
            existing = g.db.query(Encounter).filter_by(
                user_a_id=a_uid,
                user_b_id=b_uid,
                context_type='ble',
                context_id=ctx_id,
            ).first()
            if existing is not None:
                # Re-swipe inside the same match-window: bind the
                # already-persisted Encounter so the publisher below
                # has a row to broadcast.  Without this assignment
                # `match` is unbound and `_publish_match(match)` raises
                # UnboundLocalError on the duplicate-mutual-like path.
                match = existing
            else:
                match = Encounter(
                    id=_new_id('match'),
                    user_a_id=a_uid,
                    user_b_id=b_uid,
                    context_type='ble',
                    context_id=ctx_id,
                    encounter_count=1,
                    is_mutual_aware=True,
                    bond_level=1,
                    lat=lat,
                    lng=lng,
                    payload=_icebreaker_payload_init(),
                    first_at=now,
                    latest_at=now,
                )
                g.db.add(match)
            matched_id = match.id

    g.db.commit()
    if matched_id is not None:
        # Live notification to BOTH matched users — the SPA / RN
        # subscribers open the icebreaker draft modal on receipt.
        # Best-effort: a missing message_bus must not fail the swipe.
        _publish_match(match)
    return _ok({
        'sighting_id': sighting_id,
        'decision': decision,
        # Matched flag tells the liker's CLIENT that *something*
        # happened, but the response is SYMMETRIC: even when there's
        # no mutual, this shape is identical aside from 'match_id'.
        # We do NOT include any signal about whether the peer swiped
        # dislike — only the positive-match signal is surfaced, and
        # only to the two matched parties.
        'match_id': matched_id,
    })


# ──────────────────────────────────────────────────────────────────────
# /matches — list mutual matches the user is part of.
# Reads the canonical `encounters` table filtered to context_type='ble'.
# ──────────────────────────────────────────────────────────────────────

def _match_to_dict(match: Encounter) -> dict[str, Any]:
    p = match.payload or {}
    matched_at = match.first_at
    return {
        'id': match.id,
        'user_a': match.user_a_id,
        'user_b': match.user_b_id,
        'lat': match.lat,
        'lng': match.lng,
        'matched_at': matched_at.timestamp() if matched_at else None,
        'icebreaker_a_status': (p.get('icebreaker_a') or {}).get(
            'status', 'pending',
        ),
        'icebreaker_b_status': (p.get('icebreaker_b') or {}).get(
            'status', 'pending',
        ),
        'map_pin_visible': bool(p.get('map_pin_visible', True)),
    }


@encounter_bp.route('/encounter/matches', methods=['GET'])
@require_auth
def list_matches():
    uid = _user_id()
    if uid is None:
        return _err('unauthenticated', 401)
    rows = g.db.query(Encounter).filter(
        Encounter.context_type == 'ble',
        ((Encounter.user_a_id == uid) | (Encounter.user_b_id == uid)),
    ).order_by(Encounter.latest_at.desc()).all()
    matches = [_match_to_dict(r) for r in rows]
    return _ok({'matches': matches, 'count': len(matches)})


@encounter_bp.route('/encounter/map-pins', methods=['GET'])
@require_auth
def map_pins():
    uid = _user_id()
    if uid is None:
        return _err('unauthenticated', 401)
    rows = g.db.query(Encounter).filter(
        Encounter.context_type == 'ble',
        ((Encounter.user_a_id == uid) | (Encounter.user_b_id == uid)),
        Encounter.lat.isnot(None),
        Encounter.lng.isnot(None),
    ).all()
    pins = []
    for r in rows:
        p = r.payload or {}
        if not bool(p.get('map_pin_visible', True)):
            continue
        pins.append({
            'match_id': r.id,
            'lat': r.lat,
            'lng': r.lng,
            'matched_at': r.first_at.timestamp() if r.first_at else None,
        })
    return _ok({'pins': pins, 'count': len(pins)})


# ──────────────────────────────────────────────────────────────────────
# /icebreaker — approve or decline a draft.  The actual agent that
# PRODUCES the draft lives in integrations/agent_engine/ and lands in
# PR-C; until then, these endpoints accept user-supplied draft text so
# the RN/React UI can be integration-tested end-to-end.
# Per-side state lives in the encounter row's `payload` JSON column.
# ──────────────────────────────────────────────────────────────────────

# /icebreaker/draft — generate a candidate opener for the match without
# sending it.  Wraps icebreaker_service.draft_icebreaker, which is
# also the entry point the seeded encounter_icebreaker_agent uses
# server-side when the match WAMP topic fires.  This endpoint exists
# so the SPA can request a fresh draft on demand (e.g., user taps
# "regenerate" before approving).  Returns the same shape the agent
# publishes on com.hevolve.encounter.icebreaker.

def _icebreaker_kind(body: dict) -> Optional[str]:
    """'ble' (the default, so older clients keep working) or 'proximity'
    for a GPS match.  None for anything else."""
    from .icebreaker_service import ICEBREAKER_KINDS
    kind = str(body.get('kind') or 'ble')
    return kind if kind in ICEBREAKER_KINDS else None


def _conversations_on() -> bool:
    """Same gate api_conversations uses: DMs exist only with the
    `conversations` feature flag on."""
    flags = getattr(g, 'feature_flags', {}) or {}
    return bool(flags.get('conversations', False))


def _dm_already_opened_by(viewer_uid: str, peer_uid: str) -> Optional[str]:
    """The DM between the two people, when the viewer has already
    written in it, else None.  Breaking the ice twice would only repeat
    an opener the other person already has."""
    if not _conversations_on():
        return None
    from sqlalchemy import text
    from .conversation_service import _member_hash
    try:
        row = g.db.execute(text(
            "SELECT id FROM conversations "
            "WHERE kind = 'dm' AND member_hash = :h"),
            {'h': _member_hash([viewer_uid, peer_uid])},
        ).fetchone()
        if row is None:
            return None
        wrote = g.db.execute(text(
            "SELECT 1 FROM messages WHERE parent_kind = 'conversation' "
            "AND parent_id = :c AND author_id = :u AND is_deleted = 0 "
            "LIMIT 1"),
            {'c': row[0], 'u': viewer_uid},
        ).fetchone()
        return row[0] if wrote else None
    except Exception as exc:  # noqa: BLE001
        logger.debug('encounter.icebreaker dm lookup failed: %s', exc)
        return None


def _send_icebreaker_as_dm(viewer_uid: str, peer_uid: str,
                           text_val: str) -> str:
    """Open (or reuse) the DM with the peer and post the approved
    icebreaker as the viewer's message.  Returns the conversation id.
    Raises ConversationError when the DM can't be had (e.g. a block)."""
    from .conversation_service import ConversationService
    tenant_id = getattr(g, 'tenant_id', None)
    conv = ConversationService.create(
        g.db, kind='dm', member_ids=[viewer_uid, peer_uid],
        created_by=viewer_uid, tenant_id=tenant_id,
    )
    ConversationService.send_message(
        g.db, conv['id'], viewer_uid, text_val,
        metadata={'source': 'encounter_icebreaker'},
        tenant_id=tenant_id,
    )
    return conv['id']


def _has_cloud_drafting_consent(user_id: str) -> bool:
    """Lookup helper for icebreaker_service.draft_icebreaker.

    True iff the user has an active UserConsent row for cloud-
    capability scoped to the encounter_icebreaker feature (or '*').
    Used only when the HARTOS process is in central topology — the
    service ignores the check on flat / regional (user-trusted edge).
    """
    from .models import UserConsent
    try:
        row = g.db.query(UserConsent).filter_by(
            user_id=user_id,
            consent_type='cloud_capability',
            granted=True,
        ).filter(
            UserConsent.scope.in_(['*', 'encounter_icebreaker']),
        ).filter(UserConsent.revoked_at.is_(None)).first()
        return row is not None
    except Exception:  # noqa: BLE001
        return False


@encounter_bp.route('/encounter/icebreaker/draft', methods=['POST'])
@require_auth
def icebreaker_draft():
    uid = _user_id()
    if uid is None:
        return _err('unauthenticated', 401)
    body = _json()
    match_id = str(body.get('match_id', ''))
    if not match_id:
        return _err('match_id required')
    kind = _icebreaker_kind(body)
    if kind is None:
        return _err("kind must be 'ble' or 'proximity'")
    from .icebreaker_service import draft_icebreaker
    try:
        out = draft_icebreaker(
            match_id, uid, g.db,
            cloud_consent_check=_has_cloud_drafting_consent,
            kind=kind,
        )
    except PermissionError as pe:
        return _err(str(pe), 403)
    except ValueError as ve:
        return _err(str(ve), 404)
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            'encounter.icebreaker draft failed match=%s uid=%s: %s',
            match_id, uid, exc,
        )
        return _err('draft_failed', 500)
    # Already broke the ice with this person: say where the chat is so
    # the app opens it instead of drafting a second opener.
    from .icebreaker_service import find_icebreaker_match
    match = find_icebreaker_match(g.db, match_id, kind)
    side = _icebreaker_side_for(match, uid) if match else None
    peer_uid = None
    if side is not None:
        peer_uid = match.user_b_id if side == 'a' else match.user_a_id
    out['conversation_id'] = (
        _dm_already_opened_by(uid, peer_uid) if peer_uid else None)
    return _ok(out)


@encounter_bp.route('/encounter/icebreaker/approve', methods=['POST'])
@require_auth
def icebreaker_approve():
    uid = _user_id()
    if uid is None:
        return _err('unauthenticated', 401)
    body = _json()
    match_id = str(body.get('match_id', ''))
    text_val = str(body.get('text', '')).strip()
    if not match_id:
        return _err('match_id required')
    if not text_val:
        return _err('text required')
    if len(text_val) > ENCOUNTER_DRAFT_MAX_CHARS:
        return _err(
            f'text exceeds {ENCOUNTER_DRAFT_MAX_CHARS} chars', 413,
        )

    kind = _icebreaker_kind(body)
    if kind is None:
        return _err("kind must be 'ble' or 'proximity'")

    from .icebreaker_service import find_icebreaker_match
    from .conversation_service import ConversationError
    match = find_icebreaker_match(g.db, match_id, kind)
    side = _icebreaker_side_for(match, uid) if match else None
    if match is None or side is None:
        return _err('match not found', 404)
    peer_uid = match.user_b_id if side == 'a' else match.user_a_id

    if kind == 'proximity':
        # A GPS match keeps no per-side state; the opener lives in the
        # DM, so without DMs there is nowhere for it to go.
        if not _conversations_on():
            return _err('conversations feature flag is off', 503)
        if _dm_already_opened_by(uid, peer_uid):
            return _err('icebreaker already sent', 409)
        try:
            conv_id = _send_icebreaker_as_dm(uid, peer_uid, text_val)
        except ConversationError as ce:
            return _err(str(ce), 400)
        logger.info(
            'encounter.icebreaker sent kind=proximity match=%s len=%d',
            match_id, len(text_val),
        )
        return _ok({'match_id': match_id, 'status': 'sent',
                    'conversation_id': conv_id})

    payload = dict(match.payload or _icebreaker_payload_init())
    side_state = dict(payload.get(f'icebreaker_{side}') or {})
    if side_state.get('status') in {'sent', 'declined'}:
        return _err(
            f"icebreaker already {side_state.get('status')}", 409,
        )
    # The approved text is the first message of the DM (when DMs are
    # on), so the other person reads it where they can answer it.
    conv_id = None
    if _conversations_on():
        try:
            conv_id = _send_icebreaker_as_dm(uid, peer_uid, text_val)
        except ConversationError as ce:
            return _err(str(ce), 400)
    side_state['status'] = 'sent'
    side_state['text'] = text_val
    side_state['sent_at'] = _now_dt().timestamp()
    payload[f'icebreaker_{side}'] = side_state
    match.payload = payload
    match.latest_at = _now_dt()
    # SQLAlchemy doesn't auto-detect mutation of mutable JSON dict
    # values; flag the column dirty so the change persists.
    flag_modified(match, 'payload')
    g.db.commit()
    _publish_icebreaker(match, side, 'sent')

    logger.info(
        'encounter.icebreaker sent side=%s match=%s len=%d',
        side, match_id, len(text_val),
    )
    return _ok({'match_id': match_id, 'status': 'sent',
                'conversation_id': conv_id})


@encounter_bp.route('/encounter/icebreaker/decline', methods=['POST'])
@require_auth
def icebreaker_decline():
    uid = _user_id()
    if uid is None:
        return _err('unauthenticated', 401)
    body = _json()
    match_id = str(body.get('match_id', ''))
    reason = str(body.get('reason', ''))[:400]
    if not match_id:
        return _err('match_id required')
    kind = _icebreaker_kind(body)
    if kind is None:
        return _err("kind must be 'ble' or 'proximity'")

    from .icebreaker_service import find_icebreaker_match
    match = find_icebreaker_match(g.db, match_id, kind)
    side = _icebreaker_side_for(match, uid) if match else None
    if match is None or side is None:
        return _err('match not found', 404)
    if kind == 'proximity':
        # Nothing to record on a GPS match; declining just sends nothing.
        logger.info('encounter.icebreaker declined kind=proximity '
                    'match=%s', match_id)
        return _ok({'match_id': match_id, 'status': 'declined'})

    payload = dict(match.payload or _icebreaker_payload_init())
    side_state = dict(payload.get(f'icebreaker_{side}') or {})
    side_state['status'] = 'declined'
    side_state['decline_reason'] = reason
    payload[f'icebreaker_{side}'] = side_state
    match.payload = payload
    match.latest_at = _now_dt()
    flag_modified(match, 'payload')
    g.db.commit()
    _publish_icebreaker(match, side, 'declined')

    return _ok({'match_id': match_id, 'status': 'declined'})


# ──────────────────────────────────────────────────────────────────────
# /register-pubkey — phone registers its current rotating pubkey after
# it rotates (every ENCOUNTER_PUBKEY_ROTATION_SEC).  This is the
# reverse-index sightings query against.  Self-scoped to the
# authenticated user — peers cannot look up the user's pubkey via this
# endpoint.
# ──────────────────────────────────────────────────────────────────────

@encounter_bp.route('/encounter/register-pubkey', methods=['POST'])
@require_auth
def register_pubkey():
    uid = _user_id()
    if uid is None:
        return _err('unauthenticated', 401)
    body = _json()
    pk = str(body.get('pubkey', '')).strip().lower()
    if not pk or len(pk) < 16 or len(pk) > 128:
        return _err('pubkey hex 16..128 chars')
    now = _now_dt()
    pref = g.db.query(DiscoverablePref).filter_by(user_id=uid).first()
    if pref is None:
        pref = DiscoverablePref(
            user_id=uid,
            current_pubkey=pk,
            pubkey_registered_at=now,
            toggle_window_start=now,
        )
        g.db.add(pref)
    else:
        pref.current_pubkey = pk
        pref.pubkey_registered_at = now
    g.db.commit()
    return _ok({'registered_at': now.timestamp()})


# ──────────────────────────────────────────────────────────────────────
# Topic constants re-exported for clients that need them (Nunba's
# crossbarWorker + RN subscription manager).  Single-source import.
# ──────────────────────────────────────────────────────────────────────

WAMP_TOPICS = {
    'sighting': ENCOUNTER_TOPIC_SIGHTING,
    'swipe': ENCOUNTER_TOPIC_SWIPE,
    'match': ENCOUNTER_TOPIC_MATCH,
    'icebreaker': ENCOUNTER_TOPIC_ICEBREAKER,
}


@encounter_bp.route('/encounter/topics', methods=['GET'])
@require_auth
def list_topics():
    return _ok({'topics': WAMP_TOPICS})
