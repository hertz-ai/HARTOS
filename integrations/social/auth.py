"""
HevolveSocial - Authentication
JWT token generation, password hashing, and @require_auth decorator.
"""
import hashlib
import hmac
import os
import secrets
import time
from functools import wraps

from flask import request, g, jsonify

try:
    import jwt as pyjwt
    HAS_JWT = True
except ImportError:
    HAS_JWT = False

SECRET_KEY = os.environ.get('SOCIAL_SECRET_KEY', 'hevolve-social-secret-change-in-production')
TOKEN_EXPIRY = 86400 * 30  # 30 days


def hash_password(password: str) -> str:
    salt = secrets.token_hex(16)
    hashed = hashlib.pbkdf2_hmac('sha256', password.encode(), salt.encode(), 100000)
    return f"{salt}:{hashed.hex()}"


def verify_password(password: str, stored: str) -> bool:
    if not stored or ':' not in stored:
        return False
    salt, hashed = stored.split(':', 1)
    check = hashlib.pbkdf2_hmac('sha256', password.encode(), salt.encode(), 100000)
    return hmac.compare_digest(check.hex(), hashed)


def generate_api_token() -> str:
    return secrets.token_urlsafe(64)


def generate_jwt(user_id: str, username: str) -> str:
    if HAS_JWT:
        payload = {
            'user_id': user_id,
            'username': username,
            'iat': int(time.time()),
            'exp': int(time.time()) + TOKEN_EXPIRY,
        }
        return pyjwt.encode(payload, SECRET_KEY, algorithm='HS256')
    return generate_api_token()


def decode_jwt(token: str) -> dict:
    if HAS_JWT:
        try:
            return pyjwt.decode(token, SECRET_KEY, algorithms=['HS256'])
        except (pyjwt.ExpiredSignatureError, pyjwt.InvalidTokenError):
            return {}
    return {}


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
            g.db = db
        else:
            from .models import get_db
            g.user = None
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
    """Decorator: requires admin user."""
    @wraps(f)
    @require_auth
    def decorated(*args, **kwargs):
        if not g.user.is_admin:
            return jsonify({'success': False, 'error': 'Admin access required'}), 403
        return f(*args, **kwargs)
    return decorated


def require_moderator(f):
    """Decorator: requires moderator or admin user."""
    @wraps(f)
    @require_auth
    def decorated(*args, **kwargs):
        if not (g.user.is_admin or g.user.is_moderator):
            return jsonify({'success': False, 'error': 'Moderator access required'}), 403
        return f(*args, **kwargs)
    return decorated
