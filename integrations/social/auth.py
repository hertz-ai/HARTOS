"""
HevolveSocial - Authentication
JWT token generation, password hashing, and @require_auth decorator.
Uses security.jwt_manager for hardened token management.
"""
import hashlib
import hmac
import os
import sys
import stat
import secrets
import logging
import time
from functools import wraps

from flask import request, g, jsonify

try:
    import jwt as pyjwt
    HAS_JWT = True
except ImportError:
    HAS_JWT = False

logger = logging.getLogger('hevolve_social')

# Use hardened JWT manager when available
_jwt_manager = None

def _get_jwt_manager():
    global _jwt_manager
    if _jwt_manager is not None:
        return _jwt_manager
    if _jwt_manager is False:
        return None  # already tried and failed — don't log again
    try:
        from security.jwt_manager import JWTManager
        _jwt_manager = JWTManager()
        logger.info("Using hardened JWTManager")
        return _jwt_manager
    except Exception as e:
        logger.warning(f"JWTManager unavailable ({e}), using legacy JWT")
        _jwt_manager = False  # sentinel: don't retry
        return None

# Legacy fallback values - fail closed if not configured
SECRET_KEY = os.environ.get('SOCIAL_SECRET_KEY', '')
if not SECRET_KEY:
    # Auto-generate and persist so tokens survive restarts.
    # Stored next to the database file (writable user dir).
    def _load_or_create_secret_key():
        # Single-sourced with the JWTManager reader (core.platform_paths) so the
        # writer and reader never diverge on WHERE the key lives (#98e).
        try:
            from core.platform_paths import social_secret_key_write_target
            key_file = social_secret_key_write_target()
        except ImportError:
            # Fail-safe: same priority, inlined, if platform_paths is unavailable.
            db_path = os.environ.get('HEVOLVE_DB_PATH', '')
            if db_path and db_path != ':memory:' and os.path.isabs(db_path):
                key_file = os.path.join(os.path.dirname(db_path), '.social_secret_key')
            elif os.environ.get('NUNBA_BUNDLED') or getattr(sys, 'frozen', False):
                key_file = os.path.join(os.path.expanduser('~'), 'Documents', 'Nunba', 'data', '.social_secret_key')
            else:
                key_file = os.path.join('agent_data', '.social_secret_key')
        try:
            if os.path.exists(key_file):
                with open(key_file, 'r') as f:
                    key = f.read().strip()
                if len(key) >= 32:
                    return key
            # Generate new key and persist
            key = secrets.token_hex(32)
            os.makedirs(os.path.dirname(key_file), exist_ok=True)
            with open(key_file, 'w') as f:
                f.write(key)
            # Restrict file permissions to owner read/write only (600)
            try:
                os.chmod(key_file, stat.S_IRUSR | stat.S_IWUSR)
            except (OSError, NotImplementedError):
                pass  # Windows doesn't support POSIX chmod the same way
            logger.info(f"Generated persistent secret key at {key_file}")
            return key
        except (PermissionError, OSError) as e:
            logger.warning(f"Cannot persist secret key ({e}), using ephemeral")
            return secrets.token_hex(32)
    SECRET_KEY = _load_or_create_secret_key()
    if not os.environ.get('SOCIAL_SECRET_KEY'):
        logger.info("SOCIAL_SECRET_KEY not set — using auto-generated persistent key")

TOKEN_EXPIRY = 30 * 24 * 3600  # 30 days — desktop app needs long-lived sessions


PBKDF2_ITERATIONS = 600_000  # OWASP 2023 minimum for PBKDF2-SHA256


def hash_password(password: str) -> str:
    salt = secrets.token_hex(16)
    hashed = hashlib.pbkdf2_hmac('sha256', password.encode(), salt.encode(), PBKDF2_ITERATIONS)
    return f"{salt}:{hashed.hex()}"


def verify_password(password: str, stored: str) -> bool:
    if not stored or ':' not in stored:
        return False
    salt, hashed = stored.split(':', 1)
    # Support both old (100K) and new (600K) iteration counts
    for iterations in (PBKDF2_ITERATIONS, 100_000):
        check = hashlib.pbkdf2_hmac('sha256', password.encode(), salt.encode(), iterations)
        if hmac.compare_digest(check.hex(), hashed):
            return True
    return False


def generate_api_token() -> str:
    return secrets.token_urlsafe(64)


def generate_jwt(user_id: str, username: str, role: str = 'flat',
                 tenant_id: str = None) -> str:
    """Generate a LOCAL-scoped JWT (Layer 1). Works offline, survives kill switch.

    Optional `tenant_id` adds a 'tid' claim used by central-cloud
    deploys to scope all subsequent queries to that tenant.
    Flat / regional pass tenant_id=None — the claim is omitted and
    every downstream query treats the request as untenanted (NULL
    pass-through).  See plan Part E.1.

    SECURITY: When the hardened JWTManager is available, ALL JWTs
    are issued through it — including tenant-scoped tokens, which
    are the most security-sensitive scope. Earlier revisions of
    this function bypassed JWTManager when tenant_id was set; that
    bypass is removed because it inverted the threat model
    (cloud tokens getting weaker hardening than flat tokens).
    JWTManager.generate_access_token now accepts tenant_id directly
    (security/jwt_manager.py).
    """
    mgr = _get_jwt_manager()
    if mgr:
        return mgr.generate_access_token(str(user_id), username,
                                         tenant_id=tenant_id)
    if HAS_JWT:
        import uuid
        payload = {
            'user_id': str(user_id),
            'username': username,
            'role': role or 'flat',
            'jti': str(uuid.uuid4()),
            'iat': int(time.time()),
            'exp': int(time.time()) + TOKEN_EXPIRY,
            'type': 'access',
            'scope': 'local',
        }
        if tenant_id:
            payload['tid'] = str(tenant_id)
        return pyjwt.encode(payload, SECRET_KEY, algorithm='HS256')
    return generate_api_token()


def generate_jwt_with_tenant(user_id: str, username: str, role: str,
                             tenant_id: str) -> str:
    """Convenience wrapper for cloud signups — signs JWT with tid claim."""
    return generate_jwt(user_id, username, role, tenant_id=tenant_id)


def generate_hive_jwt(user_id: str, username: str, role: str = 'flat') -> str:
    """Generate a HIVE-scoped JWT (Layer 2). Ed25519-signed for cross-node verification.

    Dies with master key revocation (kill switch). Requires certificate chain.
    """
    mgr = _get_jwt_manager()
    if mgr:
        return mgr.generate_hive_token(str(user_id), username)
    # Fallback: if JWTManager unavailable, produce a local token
    # (hive features degrade gracefully)
    logger.warning("JWTManager unavailable, falling back to local token for hive request")
    return generate_jwt(user_id, username, role)


def verify_hive_jwt(token: str, issuer_public_key_hex: str) -> dict:
    """Verify a HIVE-scoped JWT from another node using its Ed25519 public key.

    Returns payload dict on success, empty dict on failure.

    TRUSTED KEY ONLY — NEVER a key from the request (#59).  ``issuer_public_key_hex``
    MUST be resolved from a trusted source (a peer's registered
    PeerNode.public_key by the claimed node_id, or a key that traces to the
    node's master trust anchor via the delegation chain), the way
    discovery._sender_signature_valid resolves it.
    Passing a caller-supplied key is self-certification: it always verifies and
    proves nothing.  That was the /api/social/auth/sync-user admin-takeover
    (a caller signed with its own key, sent that key as ``node_public_key``,
    and created a role-'central' user); that route now uses
    _sender_signature_valid and no longer calls this.  This function has NO
    production caller (grep: only tests); a new route MUST NOT reuse the
    key-as-parameter shape — resolve the key by identity first.
    """
    mgr = _get_jwt_manager()
    if mgr:
        result = mgr.verify_hive_token(token, issuer_public_key_hex)
        return result or {}
    return {}


def verify_device_jwt(db, token: str, owner_id: str) -> dict:
    """Verify a hive-shaped token from a person's phone against the key the
    desktop owner allowed for it (#111).

    The phone signs the same token nodes exchange (scope 'hive', node_sig
    over the canonical payload, verify_hive_jwt) with its own PeerLink
    Ed25519 key, and carries that key's hex in a ``node_public_key`` claim.
    The claim is read ONLY to find the row: the key on file is the owner's
    GRANTED ``device_access`` consent whose scope is that key
    (consent_service.device_scope), the row the owner wrote with "Always
    allow" on the ask.  The signature is verified against the key read back
    from that row (ConsentService.active_grant, an exact-scope lookup), so
    the claim can select a row and nothing more, and a blanket '*' grant
    admits no device.

    Before any ask is filed the token must verify against the key it
    CLAIMS: that proves the caller holds that key, so nobody can file asks
    in the name of another phone's key, and an ask always names a key its
    sender can answer for.  (Holding a key is not identity; only the
    owner's grant is.)

    Returns ``{'status': 'ok', 'payload': ..., 'public_key': ...}``,
    ``{'status': 'pending', 'public_key': ..., 'claims': ...}`` (no grant
    yet: the caller files the ask), ``{'status': 'denied', 'public_key':
    ...}`` (the owner said no), or ``{'status': 'invalid'}`` (not a device
    token, or the signature does not verify).
    """
    from .consent_service import ConsentService, device_scope
    if not HAS_JWT or not token or not owner_id:
        return {'status': 'invalid'}
    try:
        claims = pyjwt.decode(token, options={'verify_signature': False,
                                              'verify_exp': False},
                              algorithms=['HS256'])
    except Exception:
        return {'status': 'invalid'}
    scope = device_scope(claims.get('node_public_key'))
    if scope is None or claims.get('scope') != 'hive':
        return {'status': 'invalid'}
    row = ConsentService.active_grant(db, owner_id, 'device_access',
                                      scope=scope, agent_id=None)
    if row is None:
        claimed_key = scope[len('device:'):]
        if not verify_hive_jwt(token, claimed_key):
            return {'status': 'invalid'}
        declined = ConsentService.declined(db, owner_id, 'device_access',
                                           scope=scope, agent_id=None)
        return {'status': 'denied' if declined else 'pending',
                'public_key': claimed_key, 'claims': claims}
    granted_key = row.scope[len('device:'):]
    payload = verify_hive_jwt(token, granted_key)
    if not payload:
        logger.warning("device token for granted key %s... did not verify",
                       granted_key[:16])
        return {'status': 'invalid'}
    return {'status': 'ok', 'payload': payload, 'public_key': granted_key}


def file_device_access_ask(db, owner_id: str, public_key: str, claims: dict) -> None:
    """File the one canonical owner-consent ask for a proven phone key.

    Both the HTTP gate and PeerLink admission call this only after
    ``verify_device_jwt`` has verified proof of possession.  Keeping the
    wording, scope, record, and realtime fanout here prevents the two
    transports from drifting into separate device-trust flows.
    """
    from .consent_service import ConsentService, device_scope

    name = ' '.join(str(claims.get('username') or '').split())[:100]
    who = f'A phone calling itself "{name}"' if name else 'An unnamed phone'
    ConsentService.request_consent(
        db, owner_id, 'device_access', scope=device_scope(public_key),
        reason=f"{who} asks to use this computer's agents from the network.",
        requester_name=name)

def generate_token_pair(user_id: str, username: str, role: str = 'flat') -> dict:
    """Generate access + refresh token pair."""
    mgr = _get_jwt_manager()
    if mgr:
        return mgr.generate_token_pair(str(user_id), username)
    return {
        'access_token': generate_jwt(user_id, username, role),
        'refresh_token': generate_api_token(),
        'token_type': 'bearer',
        'expires_in': TOKEN_EXPIRY,
    }


def decode_jwt(token: str) -> dict:
    """Decode a JWT token (any scope). Backward compatible.

    Returns payload dict with 'scope' key ('local' for pre-upgrade tokens
    without scope, or the actual scope value). Empty dict on failure.
    """
    mgr = _get_jwt_manager()
    if mgr:
        result = mgr.decode_token(token, expected_type='access')
        if result:
            # Ensure scope is always present for callers
            result.setdefault('scope', 'local')
        return result or {}
    if HAS_JWT:
        try:
            payload = pyjwt.decode(token, SECRET_KEY, algorithms=['HS256'])
            if payload.get('type') not in ('access', None):
                return {}  # Reject non-access tokens (e.g. refresh tokens)
            payload.setdefault('scope', 'local')
            return payload
        except (pyjwt.ExpiredSignatureError, pyjwt.InvalidTokenError):
            return {}
    return {}


def revoke_token(token: str):
    """Revoke a JWT token (add to blocklist)."""
    mgr = _get_jwt_manager()
    if mgr:
        mgr.revoke_token(token)


def _get_user_from_token(token: str):
    """Look up user by API token or JWT (local or hive scope).

    For hive tokens from other nodes: the HS256 decode will fail (different
    secret), so we fall through to the API token lookup. Cross-node hive
    verification should use verify_hive_jwt() explicitly in the endpoint.

    Sets Flask g.token_scope to 'local', 'hive', or 'api_token' for callers.
    Also sets g.token_tenant_id to the JWT 'tid' claim (None for legacy
    tokens or API-token auth — flat/regional pass-through).
    """
    from .models import get_db, User

    # Try JWT first (works for local tokens and hive tokens issued by THIS node)
    payload = decode_jwt(token)
    if payload and 'user_id' in payload:
        db = get_db()
        try:
            user = db.query(User).filter(User.id == payload['user_id']).first()
            if user and not user.is_banned:
                try:
                    g.token_scope = payload.get('scope', 'local')
                    g.token_node_id = payload.get('node_id', '')
                    g.token_tenant_id = payload.get('tid')
                except RuntimeError:
                    pass  # Outside request context (testing)
                return user, db
        finally:
            pass  # keep session open for request lifecycle
        return None, db

    # Fall back to raw API token lookup
    db = get_db()
    # 2026-06-20: skip the api_token query when token is empty so a
    # User row with NULL/empty api_token doesn't falsely match the
    # Kong-stripped-header case (where we route through here with
    # token='').  Empty-token requests should reach the Kong fallback
    # below instead of accidentally authenticating as someone else.
    user = None
    if token:
        user = db.query(User).filter(User.api_token == token).first()
    if user and not user.is_banned:
        try:
            g.token_scope = 'api_token'
            g.token_node_id = ''
            g.token_tenant_id = None  # API tokens are pre-tenancy
        except RuntimeError:
            pass
        return user, db

    # ── Kong gateway fallback (2026-06-20) ────────────────────────────
    # If both JWT decode + api_token lookup failed AND the request was
    # pre-authenticated by Kong, trust Kong's identity headers as a
    # last resort.  This unblocks the cloud central tier where Kong is
    # the single source of truth for auth and HARTOS would otherwise
    # reject every request because the JWT was signed by Kong's key
    # (not HARTOS's SECRET_KEY).
    #
    # Standard Kong key-auth / jwt plugin sets:
    #   X-Consumer-Custom-ID  : external user identifier (email/uuid)
    #   X-Consumer-Username   : human-readable username
    #   X-Anonymous-Consumer  : "true" if no credentials were provided
    #
    # Only honored when HEVOLVE_TRUST_KONG=true (off by default — flat
    # deployments don't want the Kong header path enabled).  In cloud
    # mode where Kong fronts every request, set HEVOLVE_TRUST_KONG=true
    # in the HARTOS env.
    if os.environ.get('HEVOLVE_TRUST_KONG', '').lower() == 'true':
        try:
            is_anon = (request.headers.get('X-Anonymous-Consumer', '')
                       .lower() == 'true')
            if not is_anon:
                kong_custom_id = request.headers.get('X-Consumer-Custom-ID', '')
                kong_username = request.headers.get('X-Consumer-Username', '')
                kong_user = None
                # Prefer custom_id (typically the user's stable external id).
                if kong_custom_id:
                    # custom_id may be the email, uuid, or username depending
                    # on how the Kong consumer was provisioned — try all three.
                    kong_user = (
                        db.query(User).filter(User.email == kong_custom_id).first()
                        or db.query(User).filter(User.id == kong_custom_id).first()
                        or db.query(User).filter(User.username == kong_custom_id).first()
                    )
                if kong_user is None and kong_username:
                    kong_user = (
                        db.query(User).filter(User.username == kong_username).first()
                        or db.query(User).filter(User.email == kong_username).first()
                    )
                if kong_user and not kong_user.is_banned:
                    try:
                        g.token_scope = 'kong'
                        g.token_node_id = ''
                        # Kong does not currently propagate tenant — flat for now.
                        g.token_tenant_id = None
                    except RuntimeError:
                        pass
                    logger.info(
                        f"Kong fallback auth: user={kong_user.username} "
                        f"custom_id={kong_custom_id} username={kong_username}"
                    )
                    return kong_user, db
        except Exception as e:
            # Kong fallback must never raise — auth still 401s the same way.
            logger.warning(f"Kong fallback auth failed: {e}")
    return None, db


def user_id_for_token(token):
    """The user id a bearer token belongs to, or None.

    The public form of _get_user_from_token, for callers that need only the
    id and are not decorated routes (an SSE stream, a sync endpoint in the
    desktop app).  Accepts everything require_auth accepts: a local JWT, a
    hive JWT from this node, and a stored api_token.  The last is how a cloud
    login works on a desktop: the login sync stores the Kong-issued token as
    the user's api_token.  Decoding the token as a JWT instead rejects every
    cloud login (measured 2026-09-25: /agents/sync 401 while /api/social/
    auth/me answered 200 for the same token).  The DB session is closed here.
    """
    if not token:
        return None
    user, db = _get_user_from_token(token)
    try:
        if user is None or getattr(user, 'is_banned', False):
            return None
        return str(user.id)
    finally:
        if db is not None:
            db.close()


def require_auth(f):
    """Decorator: requires valid Bearer token. Sets g.user and g.db.

    Phase 7a: also sets g.tenant_id (from JWT 'tid' claim) and
    g.feature_flags. Cloud deployments enforce that 'tid' is present
    when HEVOLVE_CLOUD_MODE=true; flat/regional pass through with
    g.tenant_id=None which downstream code treats as untenanted
    (NULL match in tenant_id-scoped queries — see plan Part E.1).
    """
    @wraps(f)
    def decorated(*args, **kwargs):
        auth_header = request.headers.get('Authorization', '')
        # 2026-06-20: when Kong strips the upstream Authorization header
        # after its own pre-auth, HARTOS sees no Bearer but still gets
        # Kong's identity headers.  Route through _get_user_from_token
        # with an empty token so the Kong fallback path inside it runs.
        # The Kong fallback itself is gated on HEVOLVE_TRUST_KONG=true,
        # so flat deployments without Kong continue to reject the
        # missing-header case as before.
        token = ''
        if auth_header.startswith('Bearer '):
            token = auth_header[7:]
        elif os.environ.get('HEVOLVE_TRUST_KONG', '').lower() != 'true':
            return jsonify({'success': False, 'error': 'Missing or invalid Authorization header'}), 401

        user, db = _get_user_from_token(token)
        if user is None:
            if db:
                db.close()
            return jsonify({'success': False, 'error': 'Invalid or expired token'}), 401

        g.user = user
        g.user_id = str(user.id)
        g.db = db
        # Tenant + feature flags. _get_user_from_token sets
        # g.token_tenant_id; we promote it to g.tenant_id for
        # downstream code. In cloud mode, missing tid is fatal.
        tenant_id = getattr(g, 'token_tenant_id', None)
        if os.environ.get('HEVOLVE_CLOUD_MODE', '').lower() == 'true' and not tenant_id:
            db.close()
            return jsonify({
                'success': False,
                'error': 'Tenant required (cloud mode)',
            }), 403
        g.tenant_id = tenant_id
        try:
            from .feature_flags import get_flags_for_tenant
            g.feature_flags = get_flags_for_tenant(db, tenant_id)
        except ImportError:
            g.feature_flags = {}
        try:
            result = f(*args, **kwargs)
            db.commit()
            return result
        except Exception as e:
            if db.is_active:
                db.rollback()
            raise
        finally:
            db.close()

    return decorated


def optional_auth(f):
    """Decorator: attaches user if token present, but doesn't require it.

    Phase 7a: sets g.tenant_id and g.feature_flags identically to
    require_auth, with the addition that anonymous (no-token)
    requests get tenant_id=None (untenanted) and flags from the
    public default set — used by listing/discovery routes that
    don't need a logged-in user but should still respect tenancy
    when a JWT happens to be presented.
    """
    @wraps(f)
    def decorated(*args, **kwargs):
        auth_header = request.headers.get('Authorization', '')
        if auth_header.startswith('Bearer '):
            token = auth_header[7:]
            user, db = _get_user_from_token(token)
            g.user = user
            g.user_id = str(user.id) if user else None
            g.db = db
        else:
            from .models import get_db
            g.user = None
            g.user_id = None
            g.db = get_db()

        g.tenant_id = getattr(g, 'token_tenant_id', None)
        try:
            from .feature_flags import get_flags_for_tenant
            g.feature_flags = get_flags_for_tenant(g.db, g.tenant_id)
        except ImportError:
            g.feature_flags = {}

        try:
            result = f(*args, **kwargs)
            g.db.commit()
            return result
        except Exception as e:
            g.db.rollback()
            raise
        finally:
            g.db.close()

    return decorated


def require_local_or_auth(f):
    """Decorator: loopback callers pass as they are; anyone else needs a user token.

    For routes a local client calls without a session that every HARTOS node
    must ALSO be able to serve to the network. The bundled desktop's own SPA has
    always called the book routes from 127.0.0.1 with no Authorization header;
    once any node serves them, a remote caller must be a known user, scoped to
    their own data. So:

      * local  -> g.user = g.user_id = None; the route decides what a local
                  request may name (a desktop is single-user).
      * remote -> require_auth: g.user / g.user_id identify the caller.

    Composes the two canonical gates -- core.auth_local._is_local_request (the
    same loopback test, TRUSTED_PROXY included, that require_local_or_token
    uses) and require_auth -- rather than re-implementing either.
    """
    remote = require_auth(f)

    @wraps(f)
    def decorated(*args, **kwargs):
        from core.auth_local import _is_local_request
        if _is_local_request():
            g.user = None
            g.user_id = None
            return f(*args, **kwargs)
        return remote(*args, **kwargs)

    return decorated


# The User.role strings that confer elevated authority.  require_admin honors
# 'central' (below), require_moderator honors 'regional'/'central'; the extra
# 'admin'/'moderator' are kept as a conservative superset so a role STRING of
# that name is never treated as an ordinary profile field.  ONE definition,
# imported by sync_engine's role-strip (#59/#65) so the set a sync may set or
# preserve can never drift from the set the authority checks honor.  A new
# privileged role added here is stripped from syncs automatically.
PRIVILEGED_ROLES = frozenset({'central', 'regional', 'admin', 'moderator'})


def require_admin(f):
    """Decorator: requires central (cloud admin) role or is_admin flag."""
    @wraps(f)
    @require_auth
    def decorated(*args, **kwargs):
        user_role = getattr(g.user, 'role', None) or 'flat'
        if not (g.user.is_admin or user_role in ('central',)):
            return jsonify({'success': False, 'error': 'Admin access required'}), 403
        return f(*args, **kwargs)
    return decorated


def require_moderator(f):
    """Decorator: requires regional/central role, or is_admin/is_moderator flag."""
    @wraps(f)
    @require_auth
    def decorated(*args, **kwargs):
        user_role = getattr(g.user, 'role', None) or 'flat'
        if not (g.user.is_admin or g.user.is_moderator or user_role in ('regional', 'central')):
            return jsonify({'success': False, 'error': 'Moderator access required'}), 403
        return f(*args, **kwargs)
    return decorated


def require_central(f):
    """Decorator: requires central (cloud admin) role."""
    @wraps(f)
    @require_auth
    def decorated(*args, **kwargs):
        user_role = getattr(g.user, 'role', None) or 'flat'
        if user_role != 'central' and not g.user.is_admin:
            return jsonify({'success': False, 'error': 'Central access required'}), 403
        return f(*args, **kwargs)
    return decorated


def require_regional(f):
    """Decorator: requires regional or central role."""
    @wraps(f)
    @require_auth
    def decorated(*args, **kwargs):
        user_role = getattr(g.user, 'role', None) or 'flat'
        if user_role not in ('central', 'regional') and not (g.user.is_admin or g.user.is_moderator):
            return jsonify({'success': False, 'error': 'Regional access required'}), 403
        return f(*args, **kwargs)
    return decorated
