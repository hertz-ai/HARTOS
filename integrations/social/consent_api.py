"""
HevolveSocial - User Consent Blueprint (W0c F3 prereq).

JWT-authed, append-only consent surface for the UserConsent UI.

Single HTTP surface for consent writes (orchestrator review acd11f55,
2026-04-25): the legacy ``/api/consent/<user_id>/*`` route family in
``consent_service.py`` was deprecated in this commit and this module
is now the only HTTP write path for the ``user_consents`` table.
``ConsentService`` static methods on ``consent_service.py`` are still
the in-process write API for internal services and now share this
module's APPEND-ONLY semantics.

This is the WRITE path the cloud_capability scope (encounter_icebreaker
agent + future cloud capabilities) reads at runtime via
encounter_api._has_cloud_drafting_consent (encounter_api.py:616).

Endpoints (all mounted at /api/social/consent*, JWT auth required):

  POST /api/social/consent          grant — APPEND a NEW row
  POST /api/social/consent/revoke   revoke — set revoked_at on most-recent
                                    active row (granted_at preserved)
  GET  /api/social/consent          list — newest-first; supports
                                    consent_type + active_only filters

Append-only auditability:
  * Each grant creates a NEW UserConsent row.  Revoked rows are
    PRESERVED with their original granted_at intact and revoked_at
    set; subsequent grants do NOT reuse revoked rows.  Two rows with
    the same (user_id, agent_id=NULL, consent_type, scope) coexist
    because SQL treats NULL as distinct in the unique constraint.
  * Revoke flips revoked_at on the most-recent row where
    granted=True AND revoked_at IS NULL.  granted_at is never
    rewritten — the audit trail of WHEN the consent was granted is
    immutable.

Privacy invariants:
  * All routes scope by g.user_id — user A cannot see, grant, or
    revoke user B's consents.
  * 404 responses on revoke-with-no-active-row are neutral (no leak
    about whether the user has any consent rows at all).

Relationship to integrations/social/consent_service.py:
  * ``consent_service.py`` exposes the in-process ``ConsentService``
    static methods (``grant_consent``, ``revoke_consent``,
    ``check_consent``, ``list_consents``, ``set_payment_id``) used by
    internal services (``revenue_tracker``, ``ai_governance``,
    ``federated_aggregator``, ``lifecycle_hooks``).
  * Both surfaces share APPEND-ONLY write semantics as of the
    consolidation: every grant inserts a NEW row; ``granted_at`` is
    never rewritten on an existing row.
  * The legacy HTTP route family ``/api/consent/<user_id>/*`` from
    ``consent_service.py`` was REMOVED in the consolidation; this
    module is the single JWT-authed HTTP surface for consent writes.
"""
from __future__ import annotations

import logging
import uuid
from datetime import datetime
from typing import Any, Optional

from flask import Blueprint, g, jsonify, request

from .auth import require_auth
from .models import UserConsent

logger = logging.getLogger('hevolve_social')

consent_bp = Blueprint('consent', __name__, url_prefix='/api/social')


# ──────────────────────────────────────────────────────────────────────
# Tiny helpers — response shape mirrors the rest of /api/social/*.
# ──────────────────────────────────────────────────────────────────────

def _ok(data: Any = None, meta: Any = None, status: int = 200):
    r: dict[str, Any] = {'success': True}
    if data is not None:
        r['data'] = data
    if meta is not None:
        r['meta'] = meta
    return jsonify(r), status


def _err(msg: str, status: int = 400):
    return jsonify({'success': False, 'error': msg}), status


def _json() -> dict[str, Any]:
    return request.get_json(force=True, silent=True) or {}


def _user_id() -> Optional[str]:
    """Resolve current user_id as string (matches schema's String(64))."""
    user = getattr(g, 'user', None)
    if user is None:
        return None
    uid = getattr(user, 'id', None)
    if uid is None and isinstance(user, dict):
        uid = user.get('id')
    return str(uid) if uid is not None else None


def _row_to_dict(row: UserConsent) -> dict[str, Any]:
    """Subset projection that mirrors the F3 UI's expected shape.

    Avoids leaking columns the UI doesn't need (created_at,
    updated_at, agent_id) while staying compatible with
    UserConsent.to_dict() callers elsewhere in the codebase.
    """
    return {
        'id': row.id,
        'consent_type': row.consent_type,
        'scope': row.scope,
        'granted': bool(row.granted),
        'granted_at': row.granted_at.isoformat() if row.granted_at else None,
        'revoked_at': row.revoked_at.isoformat() if row.revoked_at else None,
    }


# ──────────────────────────────────────────────────────────────────────
# POST /api/social/consent — grant (APPEND a NEW row)
# ──────────────────────────────────────────────────────────────────────

@consent_bp.route('/consent', methods=['POST'])
@require_auth
def grant_consent():
    """Grant a consent.  ALWAYS appends a new row — never updates an
    existing one — so the audit log of grant events is immutable.

    Body: {consent_type: str, scope: str (default '*')}
    Returns: {id, consent_type, scope, granted_at}
    """
    uid = _user_id()
    if uid is None:
        return _err('unauthenticated', 401)

    body = _json()
    consent_type = str(body.get('consent_type', '')).strip()
    scope = str(body.get('scope', '*')).strip() or '*'

    if not consent_type:
        return _err('consent_type required')
    # Defensive caps that match the schema column widths so writes
    # never silently truncate at the DB layer.
    if len(consent_type) > 30:
        return _err('consent_type exceeds 30 chars')
    if len(scope) > 100:
        return _err('scope exceeds 100 chars')

    now = datetime.utcnow()
    row = UserConsent(
        id=str(uuid.uuid4()),
        user_id=uid,
        agent_id=None,
        consent_type=consent_type,
        scope=scope,
        granted=True,
        granted_at=now,
        revoked_at=None,
    )
    g.db.add(row)
    g.db.flush()  # assign defaults + surface IntegrityError before response

    logger.info(
        'consent.grant user=%s type=%s scope=%s id=%s',
        uid, consent_type, scope, row.id,
    )
    return _ok({
        'id': row.id,
        'consent_type': row.consent_type,
        'scope': row.scope,
        'granted_at': row.granted_at.isoformat(),
    }, status=201)


# ──────────────────────────────────────────────────────────────────────
# POST /api/social/consent/revoke — set revoked_at on the active row
# ──────────────────────────────────────────────────────────────────────

@consent_bp.route('/consent/revoke', methods=['POST'])
@require_auth
def revoke_consent():
    """Revoke the most-recent active consent for (user, type, scope).

    Active = granted=True AND revoked_at IS NULL.

    Body: {consent_type: str, scope: str (default '*')}
    Returns: {id, revoked_at}
    Errors:
      400 — missing consent_type
      404 — no active consent for this triple (neutral message; no
            leak about whether the user has any rows at all)
    """
    uid = _user_id()
    if uid is None:
        return _err('unauthenticated', 401)

    body = _json()
    consent_type = str(body.get('consent_type', '')).strip()
    scope = str(body.get('scope', '*')).strip() or '*'

    if not consent_type:
        return _err('consent_type required')

    # Most-recent active row.  Sort by granted_at desc so a re-grant
    # made after a previous revoke is the row we touch.
    row = g.db.query(UserConsent).filter(
        UserConsent.user_id == uid,
        UserConsent.consent_type == consent_type,
        UserConsent.scope == scope,
        UserConsent.granted == True,  # noqa: E712 — SQLAlchemy idiom
        UserConsent.revoked_at.is_(None),
    ).order_by(UserConsent.granted_at.desc()).first()

    if row is None:
        return _err('no active consent', 404)

    # Audit-evidence-discipline: NEVER overwrite granted_at.  The
    # event of "this consent was granted at T" is immutable history.
    now = datetime.utcnow()
    row.revoked_at = now
    g.db.flush()

    logger.info(
        'consent.revoke user=%s type=%s scope=%s id=%s',
        uid, consent_type, scope, row.id,
    )
    return _ok({
        'id': row.id,
        'revoked_at': row.revoked_at.isoformat(),
    })


# ──────────────────────────────────────────────────────────────────────
# GET /api/social/consent — list this user's consents
# ──────────────────────────────────────────────────────────────────────

@consent_bp.route('/consent', methods=['GET'])
@require_auth
def list_consents():
    """List the caller's consent rows (newest-first by granted_at).

    Query params:
      consent_type — filter by exact match (optional)
      active_only  — if 'true', return only granted=True AND
                     revoked_at IS NULL (default: return all rows)

    Returns: {consents: [{id, consent_type, scope, granted,
                          granted_at, revoked_at}, ...]}
    """
    uid = _user_id()
    if uid is None:
        return _err('unauthenticated', 401)

    consent_type = request.args.get('consent_type')
    active_only = request.args.get('active_only', '').lower() in {
        'true', '1', 'yes',
    }

    q = g.db.query(UserConsent).filter(UserConsent.user_id == uid)
    if consent_type:
        q = q.filter(UserConsent.consent_type == consent_type)
    if active_only:
        q = q.filter(
            UserConsent.granted == True,  # noqa: E712
            UserConsent.revoked_at.is_(None),
        )
    # Newest-first by granted_at.  Rows that were created via
    # request_consent (granted=False, granted_at=NULL) sort last
    # naturally because NULLs are LAST in ASC and FIRST in DESC on
    # most engines — push them to the bottom by also sorting on
    # created_at DESC as a stable secondary key.
    rows = q.order_by(
        UserConsent.granted_at.desc().nullslast()
        if hasattr(UserConsent.granted_at.desc(), 'nullslast')
        else UserConsent.granted_at.desc(),
        UserConsent.created_at.desc(),
    ).all()

    return _ok({
        'consents': [_row_to_dict(r) for r in rows],
        'count': len(rows),
    })
