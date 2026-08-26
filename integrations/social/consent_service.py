"""
User Consent Manager — explicit opt-in for data access, revenue sharing, public exposure.

Follows the NotificationService pattern (static methods taking db session).

HTTP surface deprecation (orchestrator review acd11f55, 2026-04-25):
  The legacy ``/api/consent/<user_id>/*`` route family was deleted as
  part of the consent-surface consolidation.  All HTTP writes to the
  ``user_consents`` table now flow through
  ``integrations.social.consent_api`` (JWT-authed, append-only,
  mounted at ``/api/social/consent``).  ``register_consent_routes``
  was removed from this module.

Static methods on ``ConsentService`` REMAIN — they are still the
direct (in-process) read + write API for internal services
(``revenue_tracker``, ``ai_governance``, ``federated_aggregator``,
``lifecycle_hooks``, etc).  As of the consolidation, ``grant_consent``
is APPEND-ONLY: every grant inserts a NEW row and never rewrites a
prior row's ``granted_at``.  This aligns the in-process semantics
with the JWT HTTP surface so the audit trail of grant events is
immutable regardless of which entry point a caller chose.
"""
import uuid
from datetime import datetime

from .models import UserConsent

CONSENT_TYPES = frozenset({
    'data_access',       # Agent needs to access user data
    'revenue_share',     # User opts into compute-for-revenue (earnings credited)
    'public_exposure',   # Content made public
    'payment_setup',     # User provides UPI/payment ID for revenue payouts
    'compute_contribute', # User allows their device to process hive tasks
    'announcement_subscription',  # A channel destination asked to receive
                         # broadcasts. Keyed by '<channel>:<chat_id>' rather
                         # than a HARTOS user, because the subscriber here is
                         # a Telegram group or Discord channel. Exists so the
                         # broadcaster can check a RECORD instead of an
                         # operator's assertion that people opted in.
    'cloud_egress',      # User allows fallback to cloud (vision, social
                         # sync, third-party APIs) when local resources
                         # can't satisfy the request.  Scope-aware: e.g.
                         # scope='vision' grants cloud-vision specifically;
                         # scope='social_sync' grants cloud social fan-out.
                         # First-time fallback emits a notification +
                         # request_consent record; user grants once,
                         # subsequent requests proceed without prompting.
    'cloud_capability',  # Agent uses a cloud-backed capability (T2 browser
                         # research read/post, encounter icebreaker, room
                         # presence).  The de-facto type written by
                         # consent_api.grant_consent and read by tools.py /
                         # encounter_api / icebreaker_service /
                         # room_presence_service — MUST be registered here or
                         # check_consent's _validate_consent_type rejects it and
                         # the whole T2 read/post subsystem is denied (#review).
    'screen_capture',    # The desktop's own screen is captured and described
                         # for the visual agent (VisionService screen channel,
                         # #701).  The capture loop's first denied tick files
                         # the pending ask; granting in the UserConsent UI
                         # starts capture on the next tick.
})


def _audit(event_type: str, actor_id: str, action: str, detail: dict):
    """Best-effort immutable audit log entry.

    Uses the audit log's in-memory fallback when the DB session would conflict
    (e.g. StaticPool / in-memory SQLite during tests).
    """
    try:
        from security.immutable_audit_log import get_audit_log
        audit = get_audit_log()
        # Force in-memory mode to avoid opening a second DB session
        # that would conflict with the caller's active session on StaticPool.
        saved = audit._use_db
        audit._use_db = False
        try:
            audit.log_event(event_type, actor_id=actor_id,
                            action=action, detail=detail)
        finally:
            audit._use_db = saved
    except Exception as e:
        import logging as _log
        _log.getLogger('hevolve.consent').warning(
            "Consent audit log failed (event=%s, actor=%s): %s",
            event_type, actor_id, e)


def _emit(topic: str, data: dict):
    """Best-effort EventBus broadcast + push to frontend via WAMP/SSE."""
    try:
        from core.platform.events import emit_event
        emit_event(topic, data)
    except Exception:
        pass
    # Also push as a notification to the user's frontend (Nunba, Hevolve, Android)
    # so consent dialogs can appear on any platform
    user_id = data.get('user_id', '')
    if user_id:
        try:
            from integrations.social.realtime import on_notification
            on_notification(user_id, {
                'type': topic,  # consent.granted, consent.revoked, consent.request
                'consent_type': data.get('consent_type', ''),
                'agent_id': data.get('agent_id'),
                'scope': data.get('scope', '*'),
                'reason': data.get('reason', ''),
            })
        except Exception:
            pass


def _validate_consent_type(consent_type: str):
    if consent_type not in CONSENT_TYPES:
        raise ValueError(
            f"Invalid consent_type '{consent_type}'. "
            f"Must be one of: {', '.join(sorted(CONSENT_TYPES))}")


class ConsentService:
    """Static-method service for managing user consent records."""

    @staticmethod
    def request_consent(db, user_id: str, consent_type: str,
                        scope: str = '*', agent_id=None):
        """Create a pending (not yet granted) consent record.

        Returns existing record if one already exists for this combination.
        """
        _validate_consent_type(consent_type)

        existing = db.query(UserConsent).filter(
            UserConsent.user_id == user_id,
            UserConsent.consent_type == consent_type,
            UserConsent.scope == scope,
            UserConsent.agent_id == agent_id,
        ).first()
        if existing:
            return existing

        consent = UserConsent(
            id=str(uuid.uuid4()),
            user_id=user_id,
            agent_id=agent_id,
            consent_type=consent_type,
            scope=scope,
            granted=False,
        )
        db.add(consent)
        db.flush()
        # Delivery, sibling-parity: grant_consent / auto_grant_with_notice
        # / announce_revocation all _emit, and _emit's own comment always
        # listed consent.request as a topic -- but this filing site never
        # called it, so pending asks were invisible until the user opened
        # the UserConsent page (the hie L6891 bypass rationale).  NEW rows
        # only: the dedup return above stays silent, so a denied gate
        # polling every tick pushes exactly ONE ask per combination.
        _emit('consent.request', {
            'user_id': user_id,
            'consent_type': consent_type,
            'scope': scope,
            'agent_id': agent_id,
        })
        return consent

    @staticmethod
    def grant_consent(db, user_id: str, consent_type: str,
                      scope: str = '*', agent_id=None):
        """Grant consent — APPEND-ONLY.

        Always inserts a NEW ``UserConsent`` row.  The audit trail of
        WHEN a consent was granted is immutable history — never
        rewrite a prior row's ``granted_at``.

        Mirror of ``integrations.social.consent_api.grant_consent``
        (JWT HTTP surface) so all writers — internal services and the
        UI — share the same audit-trail invariant.

        Notes on the unique constraint:
          ``UserConsent`` has
          ``UniqueConstraint(user_id, agent_id, consent_type, scope)``
          (see ``_models_local.py``).  SQL treats ``NULL`` as distinct
          in unique constraints, so ``agent_id=None`` rows can stack
          freely.  For non-NULL ``agent_id``, callers that re-grant
          while a prior granted-and-not-revoked row exists for the
          same triple will hit ``IntegrityError``; the supported
          pattern in that case is to ``revoke_consent`` first or use
          ``check_consent`` and skip.  No production caller in the
          tree today re-grants under a non-NULL agent_id with the
          same scope, so this is a no-op semantic change for them
          (orchestrator review acd11f55).

        Deprecated path: prior versions UPSERTed and rewrote
        ``granted_at`` on re-grant.  That semantic is gone as of the
        consolidation commit.
        """
        _validate_consent_type(consent_type)

        now = datetime.utcnow()
        consent = UserConsent(
            id=str(uuid.uuid4()),
            user_id=user_id,
            agent_id=agent_id,
            consent_type=consent_type,
            scope=scope,
            granted=True,
            granted_at=now,
        )
        db.add(consent)
        db.flush()

        _audit('consent', actor_id=user_id,
               action=f'consent.granted:{consent_type}',
               detail={'scope': scope, 'agent_id': agent_id})
        _emit('consent.granted', {
            'user_id': user_id,
            'consent_type': consent_type,
            'scope': scope,
            'agent_id': agent_id,
        })

        # Up-sync the now-public agents (gap #4): agents are almost always
        # created BEFORE the owner grants public_exposure, so the
        # producer-on-create hook no-ops for them.  When public_exposure is
        # granted, re-queue up-sync for this owner's agents so an agent created
        # earlier RISES once its owner consents.  The receiver is idempotent
        # (upsert-by-id) so a re-queue is harmless; best-effort — a sync hiccup
        # never fails the consent grant.  No new state: consent stays the single
        # public signal.
        if consent_type == 'public_exposure':
            try:
                from .services import UserService
                from .federation import federation
                for _agent in UserService.get_owned_agents(db, user_id):
                    federation.sync_agent_to_parent(db, _agent)
            except Exception:
                pass

        return consent

    @staticmethod
    def auto_grant_with_notice(db, user_id: str, consent_type: str,
                               scope: str = '*', agent_id=None,
                               reason: str = '') -> bool:
        """Privacy-aware auto-grant: NEVER blocks the request, but informs
        the user the FIRST time the cloud (or any consent-gated path) is
        used and gives them a one-tap revoke action.

        Pattern: "transparency + easy revoke" — privacy-first should not
        cause functional failures.  If the user has actively REVOKED
        this consent, this method returns False and the caller refuses.
        Otherwise (no record OR previously granted) it ensures a granted
        record exists, emits a one-time `consent.auto_granted` notice,
        and returns True so the caller proceeds immediately.

        Use this for cloud egress that's safe-by-default (vision
        fallback, social cross-device sync, fleet API tool calls, etc.)
        where blocking the request would degrade UX more than the
        privacy gain justifies.  Use ``check_consent`` + ``request_consent``
        directly when the gated action is high-stakes (payment_setup,
        public_exposure of private content) and you genuinely want to
        block until the user explicitly grants.

        Returns:
            True  — caller may proceed (consent is now granted, possibly
                    silently auto-granted with notice).
            False — caller MUST refuse: user has explicitly revoked this
                    consent and re-granting requires a fresh user action.
        """
        _validate_consent_type(consent_type)

        # 1. Active grant exists? Proceed silently.
        if ConsentService.check_consent(db, user_id, consent_type,
                                        scope=scope, agent_id=agent_id):
            return True

        # 2. Was there a PRIOR explicit revoke?  Honor it — refuse.
        prior = db.query(UserConsent).filter(
            UserConsent.user_id == user_id,
            UserConsent.consent_type == consent_type,
            UserConsent.scope == scope,
            UserConsent.agent_id == agent_id,
            UserConsent.revoked_at.isnot(None),
        ).order_by(UserConsent.revoked_at.desc()).first()
        if prior is not None and prior.revoked_at is not None:
            _emit('consent.refused_after_revoke', {
                'user_id': user_id,
                'consent_type': consent_type,
                'scope': scope,
                'agent_id': agent_id,
                'reason': reason or (
                    f"Previously revoked {consent_type}/{scope}; "
                    f"re-grant requires a fresh user action."
                ),
            })
            return False

        # 3. No record at all → auto-grant + emit one-time notice.
        ConsentService.grant_consent(db, user_id, consent_type,
                                     scope=scope, agent_id=agent_id)
        _emit('consent.auto_granted', {
            'user_id': user_id,
            'consent_type': consent_type,
            'scope': scope,
            'agent_id': agent_id,
            'reason': reason or (
                f"Auto-granted {consent_type}/{scope} so your request "
                f"could be served.  Tap to review or revoke in settings."
            ),
            'revoke_action': 'consent.revoke',
        })
        return True

    @staticmethod
    def revoke_consent(db, user_id: str, consent_type: str,
                       scope: str = '*', agent_id=None):
        """Revoke previously granted consent. Returns None if not found."""
        _validate_consent_type(consent_type)

        consent = db.query(UserConsent).filter(
            UserConsent.user_id == user_id,
            UserConsent.consent_type == consent_type,
            UserConsent.scope == scope,
            UserConsent.agent_id == agent_id,
        ).first()

        if not consent:
            return None

        consent.granted = False
        consent.revoked_at = datetime.utcnow()
        db.flush()

        ConsentService.announce_revocation(user_id, consent_type, scope, agent_id)
        return consent

    @staticmethod
    def announce_revocation(user_id: str, consent_type: str,
                            scope: str = '*', agent_id=None):
        """Fire the immutable-audit entry + ``consent.revoked`` broadcast for a
        revocation. PUBLIC on purpose: a surface that revokes by its own row
        model (the append-only ``consent_api`` UI path cannot delegate the WRITE
        without corrupting its audit trail) still gets IDENTICAL observability by
        calling this, instead of reaching into this module's underscore-private
        ``_audit``/``_emit``. That keeps the grant/revoke side-effect parity
        STRUCTURAL — a caller cannot get the row and silently skip the audit +
        broadcast. Best-effort (never raises)."""
        _audit('consent', actor_id=user_id,
               action=f'consent.revoked:{consent_type}',
               detail={'scope': scope, 'agent_id': agent_id})
        _emit('consent.revoked', {
            'user_id': user_id,
            'consent_type': consent_type,
            'scope': scope,
            'agent_id': agent_id,
        })

    @staticmethod
    def check_consent(db, user_id: str, consent_type: str,
                      scope: str = '*', agent_id=None) -> bool:
        """Check if user has active consent.

        Lookup order:
          1. Exact match (user_id + agent_id + consent_type + scope)
          2. Wildcard scope (scope='*') for same agent
          3. Blanket consent (agent_id=None, scope='*')
        """
        _validate_consent_type(consent_type)

        # 1. Exact match
        exact = db.query(UserConsent).filter(
            UserConsent.user_id == user_id,
            UserConsent.consent_type == consent_type,
            UserConsent.scope == scope,
            UserConsent.agent_id == agent_id,
            UserConsent.granted == True,
        ).first()
        if exact:
            return True

        # 2. Wildcard scope for specific agent
        if scope != '*' and agent_id is not None:
            wildcard = db.query(UserConsent).filter(
                UserConsent.user_id == user_id,
                UserConsent.consent_type == consent_type,
                UserConsent.scope == '*',
                UserConsent.agent_id == agent_id,
                UserConsent.granted == True,
            ).first()
            if wildcard:
                return True

        # 3. Blanket consent (agent_id=None, scope='*')
        if agent_id is not None:
            blanket = db.query(UserConsent).filter(
                UserConsent.user_id == user_id,
                UserConsent.consent_type == consent_type,
                UserConsent.scope == '*',
                UserConsent.agent_id == None,
                UserConsent.granted == True,
            ).first()
            if blanket:
                return True

        import logging as _log
        _log.getLogger('hevolve.consent').warning(
            "Consent check denied: user=%s type=%s scope=%s agent=%s",
            user_id, consent_type, scope, agent_id)
        return False

    # Alias for readability
    has_consent = check_consent

    @staticmethod
    def set_payment_id(db, user_id: str, payment_id: str):
        """Store user's UPI/payment ID for revenue payouts.

        Uses consent_type='payment_setup', scope=payment_id.
        Triggers consent.granted event so frontend shows confirmation.
        """
        return ConsentService.grant_consent(
            db, user_id, 'payment_setup', scope=payment_id)

    @staticmethod
    def get_payment_id(db, user_id: str) -> str:
        """Get user's most-recently granted UPI/payment ID, or empty string.

        APPEND-ONLY ``grant_consent`` means a user who saves a NEW
        payment_id leaves the prior row in place (the prior scope
        becomes audit history).  Order by ``granted_at desc`` so the
        latest UPI wins; ``revoked_at IS NULL`` so a revoked entry
        does not shadow a later valid one.
        """
        record = db.query(UserConsent).filter(
            UserConsent.user_id == user_id,
            UserConsent.consent_type == 'payment_setup',
            UserConsent.granted == True,
            UserConsent.revoked_at.is_(None),
        ).order_by(UserConsent.granted_at.desc()).first()
        return record.scope if record else ''

    @staticmethod
    def list_consents(db, user_id: str, consent_type: str = None,
                      agent_id=None):
        """List consent records for a user, optionally filtered."""
        q = db.query(UserConsent).filter(UserConsent.user_id == user_id)
        if consent_type is not None:
            _validate_consent_type(consent_type)
            q = q.filter(UserConsent.consent_type == consent_type)
        if agent_id is not None:
            q = q.filter(UserConsent.agent_id == agent_id)
        return q.order_by(UserConsent.created_at.desc()).all()


# ──────────────────────────────────────────────────────────────────────
# Legacy ``register_consent_routes`` was REMOVED in the consent-surface
# consolidation (orchestrator review acd11f55, 2026-04-25).  The HTTP
# write surface for consent now lives at ``integrations.social.consent_api``
# (JWT-authed, append-only, mounted at ``/api/social/consent``).
# Internal services that need direct DB access continue to use the
# ``ConsentService`` static methods above — those are unchanged in
# signature, with grant_consent flipped to APPEND-ONLY.
# ──────────────────────────────────────────────────────────────────────
