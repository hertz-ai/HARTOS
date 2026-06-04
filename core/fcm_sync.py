"""FCM token sync — pull the centrally-registered FCM token into HARTOS's local
SQLite so a node can push to a user's device WITHOUT a crossbar/WAMP relay.

Background.  The FCM token is captured centrally (Hevolve_Database
``POST /update_fcm_token``) and retrievable at
``GET {registry}/get_fcm_token/{user_id}``.  The cloud ``confirmation.py`` uses
it to fire FCM.  The decentralized model is: the LOCAL node caches the token in
its own SQLite (keyed by the SAME user_id the notification path uses) so it can
look it up locally and push directly — no round-trip to the cloud per push, no
crossbar bridge.

Why a dedicated ``fcm_tokens`` table (not ``User.FCMtoken``): the local ``User``
PK is a UUID, but the central FCM registry keys by the account number/phone
(e.g. 9003054371).  Keying the cache by the notification user_id keeps the two
in lockstep and sidesteps the UUID↔number identity-mapping (which is a separate
gap, see ``sync_fcm_token`` notes).

This module is the SYNC + LOCAL-CACHE mechanism only.  Actually SENDING the push
needs an FCM credential at the edge (FCM HTTP v1 service account, or a legacy
server key) — tracked separately; without it the token is cached but no push is
sent.
"""
import logging
import os

logger = logging.getLogger('hevolve.fcm_sync')

# Central FCM-token registry (Hevolve_Database / mailer).  Env-overridable so a
# regional/self-hosted registry can be pointed at without a code change.
_FCM_REGISTRY = os.environ.get('HART_FCM_REGISTRY', 'https://mailer.hertzai.com').rstrip('/')


def parse_fcm_token_response(payload):
    """Extract the token from a registry response body (pure — unit-testable).

    The registry answers ``{"fcm_token": "..."}`` (or ``{"token": "..."}``) on a
    hit and ``{"detail": "user not Found"}`` (HTTP 404/200) when unregistered.
    Returns the token string, or None for any miss / malformed shape.
    """
    if not isinstance(payload, dict):
        return None
    token = payload.get('fcm_token') or payload.get('token') or ''
    return token.strip() or None if isinstance(token, str) else None


def fetch_central_fcm_token(user_id, timeout=8):
    """GET the FCM token for ``user_id`` from the central registry.

    Returns the token, or None when unregistered / unreachable.  Timeout-bounded
    + exception-handled (never raises) so callers can treat it as best-effort.
    """
    if not user_id:
        return None
    try:
        import requests
        resp = requests.get(f"{_FCM_REGISTRY}/get_fcm_token/{user_id}", timeout=timeout)
        if resp.status_code != 200:
            return None
        return parse_fcm_token_response(resp.json())
    except Exception as e:
        logger.debug("fetch_central_fcm_token(%s) failed: %s", user_id, e)
        return None


_CREATE_TABLE = (
    "CREATE TABLE IF NOT EXISTS fcm_tokens ("
    "user_id TEXT PRIMARY KEY, token TEXT NOT NULL, synced_at TEXT)"
)
_UPSERT = (
    "INSERT INTO fcm_tokens (user_id, token, synced_at) "
    "VALUES (:u, :t, :s) "
    "ON CONFLICT(user_id) DO UPDATE SET token=:t, synced_at=:s"
)


def store_local_fcm_token(user_id, token, synced_at=None):
    """Cache ``token`` for ``user_id`` in the local SQLite ``fcm_tokens`` table.

    Idempotent upsert keyed by the notification user_id (decoupled from
    ``User.id``).  Returns True on write, False on any failure.  ``synced_at`` is
    stamped by the caller (datetime.now()-free here so the module stays
    side-effect-pure for tests; pass it in).
    """
    if not user_id or not token:
        return False
    try:
        from sqlalchemy import text
        from integrations.social.models import db_session
        with db_session() as db:
            db.execute(text(_CREATE_TABLE))
            db.execute(text(_UPSERT),
                       {'u': str(user_id), 't': str(token), 's': synced_at or ''})
        return True
    except Exception as e:
        logger.debug("store_local_fcm_token(%s) failed: %s", user_id, e)
        return False


def get_local_fcm_token(user_id):
    """Read the cached FCM token for ``user_id`` from local SQLite, or None."""
    if not user_id:
        return None
    try:
        from sqlalchemy import text
        from integrations.social.models import db_session
        with db_session(commit=False) as db:
            db.execute(text(_CREATE_TABLE))
            row = db.execute(
                text("SELECT token FROM fcm_tokens WHERE user_id = :u"),
                {'u': str(user_id)}).fetchone()
            return row[0] if row else None
    except Exception as e:
        logger.debug("get_local_fcm_token(%s) failed: %s", user_id, e)
        return None


def sync_fcm_token(user_id, synced_at=None):
    """Fetch the central token for ``user_id`` and cache it locally.

    Returns the freshly-synced token; if the central fetch misses (unregistered
    / offline) falls back to any previously-cached local copy so an offline node
    keeps using the last-known token.  Returns None if neither exists.

    NOTE on identity: ``user_id`` must be the id the FCM registry was keyed with
    (the account number/phone), which is also what the notification path passes.
    If the Android app never registered (registry returns "user not Found") this
    correctly returns None — there is no token to sync yet.
    """
    token = fetch_central_fcm_token(user_id)
    if token:
        store_local_fcm_token(user_id, token, synced_at=synced_at)
        return token
    return get_local_fcm_token(user_id)
