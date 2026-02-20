"""
HevolveSocial - Authentication
JWT token generation, password hashing, and @require_auth decorator.
Uses security.jwt_manager for hardened token management.
"""
import hashlib
import hmac
import os
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
    try:
        from security.jwt_manager import JWTManager
        _jwt_manager = JWTManager()
        logger.info("Using hardened JWTManager")
        return _jwt_manager
    except Exception as e:
        logger.warning(f"JWTManager unavailable ({e}), using legacy JWT")
        return None

# Legacy fallback values - fail closed if not configured
SECRET_KEY = os.environ.get('SOCIAL_SECRET_KEY', '')
if not SECRET_KEY:
    # Auto-generate and persist so tokens survive restarts.
    # Stored next to the database file (writable user dir).
    def _load_or_create_secret_key():
        db_path = os.environ.get('HEVOLVE_DB_PATH', '')
        if db_path and db_path != ':memory:' and os.path.isabs(db_path):
            key_file = os.path.join(os.path.dirname(db_path), '.social_secret_key')
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

TOKEN_EXPIRY = 3600  # 1 hour (was 30 days)


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


def generate_jwt(user_id: str, username: str, role: str = 'flat') -> str:
    mgr = _get_jwt_manager()
    if mgr:
        return mgr.generate_access_token(str(user_id), username)
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
        }
        return pyjwt.encode(payload, SECRET_KEY, algorithm='HS256')
    return generate_api_token()


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
    mgr = _get_jwt_manager()
    if mgr:
        result = mgr.decode_token(token, expected_type='access')
        return result or {}
    if HAS_JWT:
        try:
            payload = pyjwt.decode(token, SECRET_KEY, algorithms=['HS256'])
            if payload.get('type') not in ('access', None):
                return {}  # Reject non-access tokens (e.g. refresh tokens)
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
    """Look up user by API token or JWT."""
    from .models import get_db, User

    # Try JWT first
    payload = decode_jwt(token)
    if payload and 'user_id' in payload:
        db = get_db()
        try:
            user = db.query(User).filter(User.id == payload['user_id']).first()
            if user and not user.is_banned:
                return user, db
        finally:
            pass  # keep session open for request lifecycle
        return None, db

    # Fall back to raw API token lookup
    db = get_db()
    user = db.query(User).filter(User.api_token == token).first()
    if user and not user.is_banned:
        return user, db
    return None, db


def require_auth(f):
    """Decorator: requires valid Bearer token. Sets g.user and g.db."""
    @wraps(f)
    def decorated(*args, **kwargs):
        auth_header = request.headers.get('Authorization', '')
        if not auth_header.startswith('Bearer '):
            return jsonify({'success': False, 'error': 'Missing or invalid Authorization header'}), 401

        token = auth_header[7:]
        user, db = _get_user_from_token(token)
        if user is None:
            if db:
                db.close()
            return jsonify({'success': False, 'error': 'Invalid or expired token'}), 401

        g.user = user
        g.user_id = str(user.id)
        g.db = db
        try:
            result = f(*args, **kwargs)
            db.commit()
            return result
        except Exception as e:
            db.rollback()
            raise
        finally:
            db.close()

    return decorated


def optional_auth(f):
    """Decorator: attaches user if token present, but doesn't require it."""
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
