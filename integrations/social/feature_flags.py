"""
HevolveSocial — Feature flag substrate.

Phase 7+ rollout uses per-flag gating so every new behavior can land
dark, soak in dev, and flip on per-tenant before going global. The
plan (sunny-gliding-eich.md, Part A.2 + Part N) specifies the rollout
order: dev → one tenant → 10% → global.

Resolution order (highest priority first):
  1. Per-tenant override row in `tenant_feature_flags` (cloud only).
  2. Process env var `HEVOLVE_FLAG_<NAME>` (any deploy mode).
  3. Default value declared in `_DEFAULTS` below.

Flat / regional deploys never reach (1) — `tenant_id` is None so the
DB lookup is skipped and env vars + defaults rule.

This module has zero dependencies beyond logging + os; it is safe to
import from auth.py at request time without circular imports.

Transport:  N/A — this is read-only and synchronous.  Reads happen
once per authenticated request and the result is cached on Flask `g`.
"""
import logging
import os
from typing import Dict, Optional

logger = logging.getLogger('hevolve_social')


# Default flag values. Every new behavior added by Phase 7+ adds a
# row here. Default is False so dark-launching is the norm; flip on
# per-deploy via env var or per-tenant DB row.
_DEFAULTS: Dict[str, bool] = {
    # Phase 7a foundation
    'tenancy_v2':         False,  # JWT 'tid' claim + query filter
    'members_v2':         False,  # polymorphic Membership table reads
    'mentions_autocomplete': False,  # GET /users/autocomplete

    # Phase 7b
    'mentions':           False,  # parse @-mentions on post/comment create
    'agent_members':      False,  # agents addable as community members

    # Phase 7c
    'friends_v2':         False,  # symmetric Friendship state machine
    'invites_v2':         False,  # first-class community/conversation invites
    'conversations':      False,  # internal DM/group chat (separate from external channels)
    'reactions':          False,  # emoji reactions on posts/comments/messages
    'post_privacy':       False,  # public/friends/community/private privacy levels
    'sync_v1':            False,  # /sync delta endpoint for multi-device backfill

    # Phase 7d
    'calls_v1':           False,  # voice/video/screen via LiveKit + WebRTC mesh
    'agent_voice_bridge': False,  # agents in calls via node-side bridge
    'wear_calls':         False,  # Wear OS call controls

    # Phase 7e
    'moderation_v2':      False,  # ContentClassifier post-DLP layer
    'nunba_desktop_v2':   False,  # web parity edits applied

    # Phase 8
    'multi_tenant_cloud': False,  # full tenant signup + per-tenant LiveKit creds
    'tenant_strict_mode': False,  # drop NULL pass-through in tenant_filter
                                  # (Pass-2 H-NEW-2 + Pass-4 P4-3 hardening)

    # Phase 9
    'e2e_dms':            False,  # libsignal-style DM ratchet (optional)
    'electron_build':     False,  # Electron desktop build alongside cx_Freeze
}


def _env_override(name: str) -> Optional[bool]:
    """Read HEVOLVE_FLAG_<NAME> env var. Returns None if unset.

    Accepts: '1', 'true', 'yes', 'on' → True
             '0', 'false', 'no', 'off' → False
    Anything else logs a warning and returns None.
    """
    raw = os.environ.get(f'HEVOLVE_FLAG_{name.upper()}')
    if raw is None:
        return None
    val = raw.strip().lower()
    if val in ('1', 'true', 'yes', 'on'):
        return True
    if val in ('0', 'false', 'no', 'off'):
        return False
    logger.warning(
        "feature_flags: HEVOLVE_FLAG_%s has unrecognized value %r — ignoring",
        name.upper(), raw)
    return None


def _tenant_override(db, tenant_id: Optional[str], name: str) -> Optional[bool]:
    """Read per-tenant override row. Returns None if no row OR table missing.

    The `tenant_feature_flags` table is created by the multi-tenant
    cloud migration (Phase 8). Until that ships, this function always
    returns None. Wrapped in try/except so flat/regional deploys
    without the table never hit a hard error.
    """
    if not tenant_id or db is None:
        return None
    try:
        from sqlalchemy import text
        result = db.execute(text(
            "SELECT enabled FROM tenant_feature_flags "
            "WHERE tenant_id = :tid AND flag_name = :name"),
            {'tid': tenant_id, 'name': name}
        ).fetchone()
        if result is None:
            return None
        return bool(result[0])
    except Exception:
        # Table missing (pre-Phase-8) or transient DB error — fall through.
        return None


def get_flag(name: str, db=None, tenant_id: Optional[str] = None,
             default: Optional[bool] = None) -> bool:
    """Resolve a single flag's value.

    Priority: tenant override > env var > _DEFAULTS > caller-provided default > False.
    """
    tenant_val = _tenant_override(db, tenant_id, name)
    if tenant_val is not None:
        return tenant_val
    env_val = _env_override(name)
    if env_val is not None:
        return env_val
    if name in _DEFAULTS:
        return _DEFAULTS[name]
    if default is not None:
        return default
    return False


def get_flags_for_tenant(db, tenant_id: Optional[str]) -> Dict[str, bool]:
    """Resolve every known flag for the given tenant. Returns a dict
    keyed by flag name. Used by auth.py to populate g.feature_flags
    once per authenticated request.
    """
    return {name: get_flag(name, db=db, tenant_id=tenant_id)
            for name in _DEFAULTS}


# Convenience for tests / CLI introspection.
def list_flags() -> Dict[str, bool]:
    """Return the static defaults (no env / tenant resolution)."""
    return dict(_DEFAULTS)
