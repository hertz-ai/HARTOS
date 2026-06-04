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


# ── Decentralized push: local node → Google FCM, using the synced token ─────

_FCM_V1_ENDPOINT = 'https://fcm.googleapis.com/v1/projects/{project}/messages:send'

# Stamped into every push: sending through Google's FCM leaves the user's
# local-private tier, so we never do it silently — the client surfaces this to
# the user (per the privacy-transparency rule).
_PRIVACY_TIER_NOTICE = (
    'Delivered via the central FCM relay to reach your device — this left your '
    'local-private tier.'
)


def build_fcm_v1_message(token, title, body, data=None, privacy_tier_skipped=True):
    """Build an FCM HTTP v1 ``message`` body (pure — unit-testable).

    All v1 ``data`` values must be strings.  When the push leaves the local
    tier (the default for a personal push routed off-device), stamps the
    privacy-tier notice so the user is told their message used the central relay.
    """
    _data = {str(k): str(v) for k, v in (data or {}).items()}
    if privacy_tier_skipped:
        _data['privacy_tier_skipped'] = 'true'
        _data['privacy_notice'] = _PRIVACY_TIER_NOTICE
    message = {'token': token, 'data': _data}
    if title or body:
        message['notification'] = {'title': title or '', 'body': body or ''}
    return {'message': message}


def _fcm_access_token():
    """OAuth bearer for FCM v1, or None when no credential is configured.

    ``HART_FCM_ACCESS_TOKEN`` short-circuits (tests / a pre-minted token).
    Otherwise mints one from a service-account file at ``HART_FCM_SA_FILE`` via
    google-auth.  Returns None (push disabled) when neither is set — the
    decentralized send is OPT-IN via that edge credential.
    """
    tok = os.environ.get('HART_FCM_ACCESS_TOKEN')
    if tok:
        return tok
    sa_file = os.environ.get('HART_FCM_SA_FILE')
    if not sa_file:
        return None
    try:
        from google.oauth2 import service_account
        from google.auth.transport.requests import Request
        creds = service_account.Credentials.from_service_account_file(
            sa_file, scopes=['https://www.googleapis.com/auth/firebase.messaging'])
        creds.refresh(Request())
        return creds.token
    except Exception as e:
        logger.debug("_fcm_access_token failed: %s", e)
        return None


def send_fcm_push(user_id, title, body, data=None, timeout=8):
    """Push an FCM notification to ``user_id``'s device using the LOCALLY-cached
    token (syncing it first if absent) — the decentralized, no-crossbar send.

    Best-effort, never raises.  Returns True on a 200 from FCM, else False (no
    token, no credential/project, network/HTTP error).  The edge credential
    (HART_FCM_SA_FILE / HART_FCM_ACCESS_TOKEN) + HART_FCM_PROJECT gate the real
    send, so a node with no push credential degrades cleanly to a no-op.
    """
    if not user_id:
        return False
    token = get_local_fcm_token(user_id) or sync_fcm_token(user_id)
    if not token:
        return False
    access = _fcm_access_token()
    project = os.environ.get('HART_FCM_PROJECT', '')
    if not access or not project:
        logger.debug("send_fcm_push(%s): no FCM credential/project — push disabled", user_id)
        return False
    try:
        import requests
        resp = requests.post(
            _FCM_V1_ENDPOINT.format(project=project),
            headers={'Authorization': f'Bearer {access}',
                     'Content-Type': 'application/json'},
            json=build_fcm_v1_message(token, title, body, data),
            timeout=timeout)
        if resp.status_code == 200:
            return True
        logger.debug("send_fcm_push(%s): FCM %s %s", user_id, resp.status_code,
                     str(getattr(resp, 'text', ''))[:200])
        return False
    except Exception as e:
        logger.debug("send_fcm_push(%s) failed: %s", user_id, e)
        return False
