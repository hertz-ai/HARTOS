"""
User Consent Manager — explicit opt-in for data access, revenue sharing, public exposure.

Follows the NotificationService pattern (static methods taking db session).
"""
import uuid
from datetime import datetime

from flask import Flask, jsonify, request

from .models import UserConsent, db_session

CONSENT_TYPES = frozenset({'data_access', 'revenue_share', 'public_exposure'})


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
    except Exception:
        pass


def _emit(topic: str, data: dict):
    """Best-effort EventBus broadcast."""
    try:
        from core.platform.events import emit_event
        emit_event(topic, data)
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
        return consent

    @staticmethod
    def grant_consent(db, user_id: str, consent_type: str,
                      scope: str = '*', agent_id=None):
        """Grant consent — creates record if it doesn't exist, updates if it does."""
        _validate_consent_type(consent_type)

        consent = db.query(UserConsent).filter(
            UserConsent.user_id == user_id,
            UserConsent.consent_type == consent_type,
            UserConsent.scope == scope,
            UserConsent.agent_id == agent_id,
        ).first()

        now = datetime.utcnow()

        if consent:
            consent.granted = True
            consent.granted_at = now
            consent.revoked_at = None
        else:
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
        return consent

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

        _audit('consent', actor_id=user_id,
               action=f'consent.revoked:{consent_type}',
               detail={'scope': scope, 'agent_id': agent_id})
        _emit('consent.revoked', {
            'user_id': user_id,
            'consent_type': consent_type,
            'scope': scope,
            'agent_id': agent_id,
        })
        return consent

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

        return False

    # Alias for readability
    has_consent = check_consent

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
# Flask route registration
# ──────────────────────────────────────────────────────────────────────

def register_consent_routes(app: Flask):
    """Register /api/consent/* endpoints on the Flask app."""

    @app.route('/api/consent/<user_id>', methods=['GET'])
    def _consent_list(user_id):
        consent_type = request.args.get('type')
        agent_id = request.args.get('agent_id')
        try:
            with db_session() as db:
                records = ConsentService.list_consents(
                    db, user_id,
                    consent_type=consent_type,
                    agent_id=agent_id)
                return jsonify([r.to_dict() for r in records])
        except ValueError as e:
            return jsonify({'error': str(e)}), 400

    @app.route('/api/consent/<user_id>', methods=['POST'])
    def _consent_grant(user_id):
        body = request.get_json(silent=True) or {}
        consent_type = body.get('consent_type')
        scope = body.get('scope', '*')
        agent_id = body.get('agent_id')
        if not consent_type:
            return jsonify({'error': 'consent_type is required'}), 400
        try:
            with db_session() as db:
                record = ConsentService.grant_consent(
                    db, user_id, consent_type,
                    scope=scope, agent_id=agent_id)
                return jsonify(record.to_dict()), 201
        except ValueError as e:
            return jsonify({'error': str(e)}), 400

    @app.route('/api/consent/<user_id>/revoke', methods=['POST'])
    def _consent_revoke(user_id):
        body = request.get_json(silent=True) or {}
        consent_type = body.get('consent_type')
        scope = body.get('scope', '*')
        agent_id = body.get('agent_id')
        if not consent_type:
            return jsonify({'error': 'consent_type is required'}), 400
        try:
            with db_session() as db:
                record = ConsentService.revoke_consent(
                    db, user_id, consent_type,
                    scope=scope, agent_id=agent_id)
                if record is None:
                    return jsonify({'error': 'Consent record not found'}), 404
                return jsonify(record.to_dict())
        except ValueError as e:
            return jsonify({'error': str(e)}), 400

    @app.route('/api/consent/<user_id>/check', methods=['GET'])
    def _consent_check(user_id):
        consent_type = request.args.get('type')
        scope = request.args.get('scope', '*')
        agent_id = request.args.get('agent_id')
        if not consent_type:
            return jsonify({'error': 'type query parameter is required'}), 400
        try:
            with db_session() as db:
                granted = ConsentService.check_consent(
                    db, user_id, consent_type,
                    scope=scope, agent_id=agent_id)
                return jsonify({'granted': granted})
        except ValueError as e:
            return jsonify({'error': str(e)}), 400

    app.logger.info("Consent routes registered at /api/consent/")
