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
  POST /api/social/consent/revoke   revoke — set revoked_at on every
                                    active row (granted_at preserved)
  POST /api/social/consent/decline  decline — say no to a pending ask,
                                    for the ask's agent only
                                    (ConsentService.revoke_consent)
  POST /api/social/consent/reopen   take a no back: the combination is
                                    undecided again, nothing is granted
                                    (ConsentService.reopen)
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
import os
from datetime import datetime
from typing import Any, Optional

from flask import Blueprint, g, jsonify, request

from .auth import require_auth
from .models import UserConsent
from .consent_service import (
    ConsentService, agent_display_name, device_fingerprint)

logger = logging.getLogger('hevolve_social')

consent_bp = Blueprint('consent', __name__, url_prefix='/api/social')


# ──────────────────────────────────────────────────────────────────────
# Tiny helpers — response shape mirrors the rest of /api/social/*.
# ──────────────────────────────────────────────────────────────────────

from .api_common import _ok, _err  # single-sourced envelope helpers (#97)


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


def _row_to_dict(row: UserConsent, agent_name=None) -> dict[str, Any]:
    """Subset projection that mirrors the F3 UI's expected shape.

    Avoids leaking columns the UI doesn't need (created_at,
    updated_at) while staying compatible with
    UserConsent.to_dict() callers elsewhere in the codebase.

    ``agent_id`` says which agent the row is for (None: every agent) and
    ``agent_name`` is the name its person knows it by (the caller looks it
    up, consent_service.agent_display_name), so a page can show and switch
    one agent's consent (owner 2026-11-21: a kept consent is turned off "in
    nunba admin for that agent").

    A device_access row is the trusted-phones listing (#111): its scope is
    the phone's key, unreadable, so it carries the ``label`` the phone
    signed into its ask (self-asserted) and the key's ``fingerprint``, the
    thing to match against the phone.
    """
    out = {
        'id': row.id,
        'consent_type': row.consent_type,
        'scope': row.scope,
        'granted': bool(row.granted),
        'granted_at': row.granted_at.isoformat() if row.granted_at else None,
        'revoked_at': row.revoked_at.isoformat() if row.revoked_at else None,
        # A no taken back ("Allow asking again") keeps its revoked_at; the
        # row is declined only while revoked_at is newer than this.
        'reopened_at': (row.reopened_at.isoformat()
                        if getattr(row, 'reopened_at', None) else None),
        'agent_id': row.agent_id,
        'agent_name': agent_name if row.agent_id is not None else None,
    }
    if row.consent_type == 'device_access':
        out['label'] = row.label
        out['fingerprint'] = device_fingerprint(row.scope)
    return out


# The longest credential value a card may send.  Generous for any password
# or API key; it only stops a body from filling the vault.
CREDENTIAL_VALUE_MAX = 4096


def _open_ask(uid: str, request_id: str) -> Optional[UserConsent]:
    """The ask ``request_id`` names, when it is uid's and still undecided:
    never granted, and no "no" standing on it (a reopened no counts as
    undecided again).  request_id is the row id the ask's push carries
    (consent_service.consent_prompt_push), so a phone that only got the push
    answers exactly the row it was asked about.  None for anything else, so
    a 404 says nothing about other users' rows."""
    row = g.db.query(UserConsent).filter(
        UserConsent.id == request_id,
        UserConsent.user_id == uid,
    ).first()
    if row is None or row.granted or row.granted_at is not None:
        return None
    reopened = getattr(row, 'reopened_at', None)
    if row.revoked_at is not None and not (reopened and reopened >= row.revoked_at):
        return None
    return row


def _store_owner_credential(uid: str, ask: UserConsent, value) -> Optional[tuple]:
    """Put the value the owner typed on a card into this node's vault, for
    the credential ask ``ask``.  Returns None when stored, else the
    (message, status) to refuse with.

    The phone's overlay has no local vault to put it in (the desktop card
    stores through Nunba's localhost /api/vault/store and only then grants),
    so the value comes with the answer, to the node that asked: only for
    this node's owner (HEVOLVE_OWNER_USER_ID, whose vault request_credential
    asks for), only on a credential ask still open, never a node secret.
    The value is never logged or returned."""
    from .consent_service import CREDENTIAL_SCOPE_PREFIX
    owner = os.environ.get('HEVOLVE_OWNER_USER_ID')
    if not owner or str(owner) != uid:
        return ('only the owner of this computer can enter its credentials', 403)
    scope = ask.scope or ''
    name = scope[len(CREDENTIAL_SCOPE_PREFIX):]
    if ask.consent_type != 'credential' or not scope.startswith(
            CREDENTIAL_SCOPE_PREFIX) or not name:
        return ('a value answers a credential ask only', 400)
    from hartos.ai_key_vault import get_ai_key_vault, is_node_secret
    if is_node_secret(name):
        return ('a value answers a credential ask only', 400)
    value = str(value).strip()
    if not value:
        return ('value required', 400)
    if len(value) > CREDENTIAL_VALUE_MAX:
        return (f'value exceeds {CREDENTIAL_VALUE_MAX} chars', 400)
    try:
        get_ai_key_vault().store_credential(name, value)
    except Exception as e:
        # The type only: a message could carry the value.
        logger.warning('consent.credential %s not stored: %s',
                       name, type(e).__name__)
        return ('the credential could not be stored', 500)
    return None


# ──────────────────────────────────────────────────────────────────────
# POST /api/social/consent — grant (APPEND a NEW row)
# ──────────────────────────────────────────────────────────────────────

@consent_bp.route('/consent', methods=['POST'])
@require_auth
def grant_consent():
    """Grant a consent.  ALWAYS appends a new row — never updates an
    existing one — so the audit log of grant events is immutable.

    Body: {consent_type: str, scope: str (default '*')}
       or {request_id: str[, value: str]}: the answer to the ask whose push
          carried request_id (the phone's overlay); type and scope are the
          ask's.  ``value`` is what the owner typed on a credential ask: it
          is stored in this node's vault before the grant (only for this
          node's owner, only while the ask is open), and never logged or
          returned.
    Returns: {id, consent_type, scope, granted_at}
    Errors: 404 — no open ask for request_id (neutral message)
    """
    uid = _user_id()
    if uid is None:
        return _err('unauthenticated', 401)

    body = _json()
    request_id = str(body.get('request_id') or '').strip()
    value = body.get('value')
    ask = None
    if request_id:
        ask = _open_ask(uid, request_id)
        if ask is None:
            return _err('no such ask', 404)
        consent_type, scope = ask.consent_type, ask.scope
        # A yes with nothing typed would name a credential that holds no
        # value; the card's Accept always carries one.
        if consent_type == 'credential' and value is None:
            return _err('a credential ask is answered with its value')
    else:
        if value is not None:
            return _err('a value answers an ask: request_id required')
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
    # A phone is allowed by its key, never by a blanket: the gate reads the
    # key back from the granted row (auth.verify_device_jwt), so a '*' row
    # admits no phone and would only look like "all phones allowed".
    if consent_type == 'device_access' and device_fingerprint(scope) is None:
        return _err('device_access is granted per phone: scope must be device:<key>')

    # Delegate the WRITE to the canonical ConsentService so this UI surface gets
    # the SAME immutable-audit entry, `consent.granted` event, consent-type
    # validation, and public_exposure up-sync that internal callers get. This
    # route used to INSERT the UserConsent row inline, so UI-driven grants left
    # NO audit trail, emitted NO event, skipped up-sync, and accepted arbitrary
    # consent_type strings — a drifted parallel path (audit gap #4). grant is
    # append-only in both, so the delegation is behaviour-preserving for the row.
    if value is not None:
        refused = _store_owner_credential(uid, ask, value)
        if refused:
            return _err(*refused)
    try:
        row = ConsentService.grant_consent(g.db, uid, consent_type, scope)
    except ValueError as e:
        return _err(str(e))  # unknown consent_type -> 400, not a masked 500

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
    """Revoke every active consent for (user, type, scope).

    Active = granted=True AND revoked_at IS NULL.  The response names the
    most recent of them.

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

    # Every active row.  A grant appends a row, so two Allow clicks are two
    # rows; revoking only the newest left the older one passing
    # check_consent (tests/unit/test_consent_revoke_is_honoured.py).
    # Newest first, so the response still names the most recent grant.
    rows = g.db.query(UserConsent).filter(
        UserConsent.user_id == uid,
        UserConsent.consent_type == consent_type,
        UserConsent.scope == scope,
        UserConsent.granted == True,  # noqa: E712 — SQLAlchemy idiom
        UserConsent.revoked_at.is_(None),
    ).order_by(UserConsent.granted_at.desc()).all()

    if not rows:
        return _err('no active consent', 404)

    # Audit-evidence-discipline: NEVER overwrite granted_at.  The
    # event of "this consent was granted at T" is immutable history.
    now = datetime.utcnow()
    for r in rows:
        r.revoked_at = now
    g.db.flush()
    row = rows[0]

    # Parallel-path parity (audit #4): this UI surface keeps its OWN append-only
    # row model on purpose — granted stays True and revoked_at is the tombstone
    # (see the module docstring), which is deliberately NOT the same as
    # ConsentService.revoke_consent's granted=False + agent_id-keyed row model,
    # so we do not delegate the WRITE (that would corrupt the append-only audit
    # trail). But the observable SIDE EFFECTS must not drift between the two
    # writers, so fire them through the service's PUBLIC announce_revocation —
    # the same entry point ConsentService.revoke_consent uses — rather than
    # reaching into its underscore-private _audit/_emit. Previously a UI revoke
    # left no audit entry and never reached WAMP/SSE listeners or the user's
    # other devices. announce_revocation is best-effort (never raises), so this
    # cannot fail the revoke.
    from .consent_service import ConsentService
    ConsentService.announce_revocation(
        uid, consent_type, scope, getattr(row, 'agent_id', None), db=g.db)

    logger.info(
        'consent.revoke user=%s type=%s scope=%s id=%s',
        uid, consent_type, scope, row.id,
    )
    return _ok({
        'id': row.id,
        'revoked_at': row.revoked_at.isoformat(),
    })


# ──────────────────────────────────────────────────────────────────────
# POST /api/social/consent/decline — say no to a pending ask
# ──────────────────────────────────────────────────────────────────────

@consent_bp.route('/consent/decline', methods=['POST'])
@require_auth
def decline_consent():
    """Say no to a pending ask: the "Don't allow" answer on a consent card.

    The write is ConsentService.revoke_consent.  With no active grant it
    marks the ask declined, which stops request_consent asking again and
    makes ConsentService.declined true, so a gate that is waiting refuses at
    once.  agent_id is the ask's agent: a no to one agent's ask leaves every
    other agent's ask open.  While "Allow ALL agents" stands, a no that names
    an agent turns the consent off for that agent only, asked or not: the
    grant stays for the others.  The way back is a grant (POST /consent),
    which covers every agent, or POST /consent/reopen.

    Body: {consent_type: str, scope: str (default '*'), agent_id: str|null}
       or {request_id: str}: the no to the ask whose push carried
          request_id (the phone's overlay); type, scope and agent are the
          ask's.
    Returns: {declined: true, id}
    Errors:
      400 — missing or unknown consent_type
      404 — no ask for this combination (neutral message)
    """
    uid = _user_id()
    if uid is None:
        return _err('unauthenticated', 401)

    body = _json()
    request_id = str(body.get('request_id') or '').strip()
    if request_id:
        ask = _open_ask(uid, request_id)
        if ask is None:
            return _err('no such ask', 404)
        consent_type, scope, agent_id = ask.consent_type, ask.scope, ask.agent_id
    else:
        consent_type = str(body.get('consent_type', '')).strip()
        scope = str(body.get('scope', '*')).strip() or '*'
        agent_id = body.get('agent_id')
        if agent_id is not None:
            agent_id = str(agent_id).strip() or None

    if not consent_type:
        return _err('consent_type required')
    # The same caps as the grant route: a no can write a row (an agent's own
    # no under "Allow ALL agents"), and a row never truncates at the store.
    if len(consent_type) > 30:
        return _err('consent_type exceeds 30 chars')
    if len(scope) > 100:
        return _err('scope exceeds 100 chars')
    if agent_id is not None and len(str(agent_id)) > 64:
        return _err('agent_id exceeds 64 chars')

    try:
        row = ConsentService.decline(g.db, uid, consent_type, scope, agent_id)
    except ValueError as e:
        return _err(str(e))  # unknown consent_type -> 400
    if row is None:
        return _err('no such ask', 404)

    logger.info(
        'consent.decline user=%s type=%s scope=%s agent=%s id=%s',
        uid, consent_type, scope, agent_id, row.id,
    )
    return _ok({'declined': True, 'id': row.id})


# ──────────────────────────────────────────────────────────────────────
# POST /api/social/consent/reopen — take a "no" back
# ──────────────────────────────────────────────────────────────────────

@consent_bp.route('/consent/reopen', methods=['POST'])
@require_auth
def reopen_consent():
    """Take back a "no" (ConsentService.reopen): the privacy page's "Allow
    asking again" for an ask with no on/off card to grant from, such as a
    credential.  The combination is undecided again, for every agent, and
    the next ask shows the card.  Nothing is granted.

    Body: {consent_type: str, scope: str (default '*')}
    Returns: {reopened: n}
    Errors:
      400 — missing or unknown consent_type
      404 — nothing declined for this combination (neutral message)
    """
    uid = _user_id()
    if uid is None:
        return _err('unauthenticated', 401)

    body = _json()
    consent_type = str(body.get('consent_type', '')).strip()
    scope = str(body.get('scope', '*')).strip() or '*'
    if not consent_type:
        return _err('consent_type required')

    try:
        n = ConsentService.reopen(g.db, uid, consent_type, scope)
    except ValueError as e:
        return _err(str(e))  # unknown consent_type -> 400
    if not n:
        return _err('nothing declined', 404)

    logger.info('consent.reopen user=%s type=%s scope=%s rows=%d',
                uid, consent_type, scope, n)
    return _ok({'reopened': n})


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

    Each row also carries agent_id (null: every agent) and agent_name.

    Returns: {consents: [{id, consent_type, scope, granted,
                          granted_at, revoked_at}, ...]}; a device_access
             row also carries label + fingerprint (#111).
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

    # One name lookup per agent, not per row.
    names = {}
    for r in rows:
        if r.agent_id is not None and r.agent_id not in names:
            names[r.agent_id] = agent_display_name(g.db, r.agent_id)

    return _ok({
        'consents': [_row_to_dict(r, names.get(r.agent_id)) for r in rows],
        'count': len(rows),
    })
