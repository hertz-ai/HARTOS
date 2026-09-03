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


def fetch_central_fcm_token_by_node(node_id, timeout=8):
    """GET the FCM token registered for a peer_link ``node_id`` from the central
    registry (``GET {registry}/get_node_token/{node_id}``, written by the
    pre-login ``POST /register_node_token``).

    Sibling of ``fetch_central_fcm_token``, keyed by the node's ed25519 device
    identity rather than a user account — the reach for a device that has NOT yet
    logged in (the desktop already auto-discovers the same node_id on the LAN).
    Returns the token, or None when unregistered / unreachable.  Robust to both
    the bare-string body ``get_node_token`` returns and a ``{"token": ...}`` wrap.
    Timeout-bounded + exception-handled (never raises)."""
    if not node_id:
        return None
    try:
        import requests
        resp = requests.get(f"{_FCM_REGISTRY}/get_node_token/{node_id}", timeout=timeout)
        if resp.status_code != 200:
            return None
        body = resp.json()
        if isinstance(body, str):
            return body.strip() or None
        return parse_fcm_token_response(body)
    except Exception as e:
        logger.debug("fetch_central_fcm_token_by_node(%s) failed: %s", node_id, e)
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


# ── Local UUID ↔ central account-id mapping (#90) ────────────────────────────
#
# The central FCM registry is keyed by the central Hevolve account id (the
# phone / account-number, e.g. 9003054371), but the local notification path
# carries the local social ``User.id`` (a UUID).  When HARTOS knows the central
# id for a user — it learns it at login with the central account, or central
# pushes it down in the user-sync payload — we stash it on the canonical local
# record (``User.settings['central_user_id']``, a migration-free JSON field) so
# the pull can query the registry by the id it is actually keyed with, instead
# of the UUID that always missed (#90).  No mapping known → callers fall back to
# the UUID itself, i.e. byte-for-byte the previous behaviour.

CENTRAL_ID_SETTINGS_KEY = 'central_user_id'


def resolve_central_id(user_id):
    """Return the central account id the FCM registry is keyed by for
    ``user_id`` (from ``User.settings['central_user_id']``), or None when no
    mapping is known.  Best-effort, never raises."""
    if not user_id:
        return None
    try:
        from integrations.social.models import db_session, User
        with db_session(commit=False) as db:
            u = db.query(User).filter(User.id == str(user_id)).first()
            settings = getattr(u, 'settings', None) if u else None
            if isinstance(settings, dict):
                cid = settings.get(CENTRAL_ID_SETTINGS_KEY)
                if cid and str(cid).strip():
                    return str(cid).strip()
    except Exception as e:
        logger.debug("resolve_central_id(%s) failed: %s", user_id, e)
    return None


def set_central_id(user_id, central_id):
    """Persist the central account id for the local ``user_id`` (on
    ``User.settings``) so the FCM pull can query the central registry by the id
    it is keyed with.  The hook a central-account login / device-link calls once
    it knows both ids.  Idempotent, best-effort; returns True on a stored value.

    Note: writes through a fresh session, so the local User row must already be
    committed.  Sync-time capture (a User created in the same transaction) sets
    ``settings`` inline instead — see sync_engine._handle_sync_user."""
    if not user_id or not central_id:
        return False
    try:
        from sqlalchemy.orm.attributes import flag_modified
        from integrations.social.models import db_session, User
        with db_session() as db:
            u = db.query(User).filter(User.id == str(user_id)).first()
            if not u:
                return False
            settings = dict(u.settings or {})
            if settings.get(CENTRAL_ID_SETTINGS_KEY) == str(central_id):
                return True
            settings[CENTRAL_ID_SETTINGS_KEY] = str(central_id)
            u.settings = settings
            flag_modified(u, 'settings')
        return True
    except Exception as e:
        logger.debug("set_central_id(%s) failed: %s", user_id, e)
        return False


def sync_fcm_token(user_id, synced_at=None):
    """Fetch the central token for ``user_id`` and cache it locally.

    Returns the freshly-synced token; if the central fetch misses (unregistered
    / offline) falls back to any previously-cached local copy so an offline node
    keeps using the last-known token.  Returns None if neither exists.

    Identity (#90): the registry is keyed by the central account id, so we query
    it by ``resolve_central_id(user_id)`` when that mapping is known, and cache
    the result under the LOCAL ``user_id`` (the key the push path looks up).
    With no mapping we query by ``user_id`` itself — unchanged behaviour.
    """
    registry_id = resolve_central_id(user_id) or user_id
    token = fetch_central_fcm_token(registry_id)
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


def _fcm_credential():
    """``(access_token, project)`` when a real FCM send is possible, else
    ``(None, None)``.

    The cheap credential/project gate every push checks FIRST, before any token
    network-sync — a node with no push credential (the default today) must never
    trigger a blocking token fetch just to discover it cannot send. Before this
    ordering, send_fcm_push fired an up-to-8s blocking GET per expired message in
    DeliveryTracker's cleanup loop, then no-op'd (2026-06-05 sweep, same family
    as the governor hang). Shared by the user-keyed and node-keyed send paths."""
    access = _fcm_access_token()
    project = os.environ.get('HART_FCM_PROJECT', '')
    if not access or not project:
        return None, None
    return access, project


def _post_fcm_message(access, project, token, title, body, data, timeout):
    """POST one built message to FCM v1.  Returns True on a 200, else False.
    Never raises.  The SINGLE FCM-send implementation shared by both push paths
    (send_fcm_push by user_id, send_fcm_push_to_node by node_id) — no parallel
    send."""
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
        logger.debug("FCM send %s %s", resp.status_code,
                     str(getattr(resp, 'text', ''))[:200])
        return False
    except Exception as e:
        logger.debug("_post_fcm_message failed: %s", e)
        return False


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
    access, project = _fcm_credential()
    if not access:
        logger.debug("send_fcm_push(%s): no FCM credential/project — push disabled", user_id)
        return False
    token = get_local_fcm_token(user_id) or sync_fcm_token(user_id)
    if not token:
        return False
    return _post_fcm_message(access, project, token, title, body, data, timeout)


def send_fcm_push_to_node(node_id, title, body, data=None, timeout=8):
    """Push an FCM notification to a device by its peer_link ``node_id`` — the
    PRE-LOGIN reach.  The token was registered centrally against the node's
    ed25519 identity (POST /register_node_token); the desktop, having
    auto-discovered that node_id on the LAN, wakes the phone off-LAN through it.

    Same credential gate + FCM send as send_fcm_push (the shared _fcm_credential
    / _post_fcm_message helpers) — ONLY the token resolution differs, by node
    rather than user.  Best-effort, never raises; returns True on a 200, else
    False (no token, no credential/project, network/HTTP error)."""
    if not node_id:
        return False
    access, project = _fcm_credential()
    if not access:
        logger.debug("send_fcm_push_to_node(%s): no FCM credential/project — push disabled", node_id)
        return False
    token = fetch_central_fcm_token_by_node(node_id)
    if not token:
        return False
    return _post_fcm_message(access, project, token, title, body, data, timeout)
