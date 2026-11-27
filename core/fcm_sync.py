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
import time

logger = logging.getLogger('hevolve.fcm_sync')

# Central FCM-token registry (Hevolve_Database / mailer).  Env-overridable so a
# regional/self-hosted registry can be pointed at without a code change.
_FCM_REGISTRY = os.environ.get('HART_FCM_REGISTRY', 'https://mailer.hertzai.com').rstrip('/')


def parse_fcm_tokens_response(payload):
    """Every token in a registry response body, once each, in order (pure).

    Hevolve_Database answers ``GET /get_fcm_token/<id>`` with the bare token
    string (crud.get_fcm_token returns ``User.FCMtoken``), and with
    ``?devices=1`` a JSON list: that slot and every device row of the person
    (node_fcm_tokens).  ``{"fcm_token": ...}`` / ``{"token": ...}`` are read
    too; ``{"detail": "user not Found"}``, None and anything else are no
    token.  Returns a list, empty for a miss."""
    if isinstance(payload, dict):
        payload = payload.get('fcm_token') or payload.get('token')
    if isinstance(payload, str):
        payload = [payload]
    if not isinstance(payload, list):
        return []
    tokens = []
    for t in payload:
        t = t.strip() if isinstance(t, str) else ''
        if t and t not in tokens:
            tokens.append(t)
    return tokens


def parse_fcm_token_response(payload):
    """The one token in a registry response body (pure — unit-testable): the
    bare string crud.get_fcm_token returns, or ``{"fcm_token": ...}`` /
    ``{"token": ...}``.  None for a miss (``{"detail": "user not Found"}``,
    empty, None) or a malformed shape (parse_fcm_tokens_response)."""
    tokens = parse_fcm_tokens_response(payload)
    return tokens[0] if tokens else None


def fetch_central_fcm_tokens(user_id, timeout=8):
    """GET every device token of ``user_id`` from the central registry: the
    per-user slot and each device row (``?devices=1``).  A registry that
    predates the device rows ignores the parameter and answers its one bare
    token, which reads as a list of one.  [] when unregistered or
    unreachable; never raises."""
    if not user_id:
        return []
    try:
        import requests
        resp = requests.get(f"{_FCM_REGISTRY}/get_fcm_token/{user_id}?devices=1",
                            timeout=timeout)
        if resp.status_code != 200:
            return []
        return parse_fcm_tokens_response(resp.json())
    except Exception as e:
        logger.debug("fetch_central_fcm_tokens(%s) failed: %s", user_id, e)
        return []


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


def _cached_tokens(value):
    """The tokens a cached ``token`` column holds: a JSON list (one per
    device of the person), or the one bare token an older build cached."""
    if not value:
        return []
    if str(value).startswith('['):
        import json
        try:
            return parse_fcm_tokens_response(json.loads(value))
        except ValueError:
            return []
    return parse_fcm_tokens_response(str(value))


def store_local_fcm_tokens(user_id, tokens, synced_at=None):
    """Cache every device token of ``user_id`` (the registry's list, which
    replaces what was cached) in the local SQLite ``fcm_tokens`` table, as a
    JSON list in its ``token`` column.

    Idempotent upsert keyed by the notification user_id (decoupled from
    ``User.id``).  Returns True on write, False on any failure or no token.
    ``synced_at`` is stamped by the caller (datetime.now()-free here so the
    module stays side-effect-pure for tests; pass it in).
    """
    tokens = parse_fcm_tokens_response(list(tokens or []))
    if not user_id or not tokens:
        return False
    try:
        import json
        from sqlalchemy import text
        from integrations.social.models import db_session
        with db_session() as db:
            db.execute(text(_CREATE_TABLE))
            db.execute(text(_UPSERT), {'u': str(user_id), 't': json.dumps(tokens),
                                       's': synced_at or ''})
        return True
    except Exception as e:
        logger.debug("store_local_fcm_tokens(%s) failed: %s", user_id, e)
        return False


def store_local_fcm_token(user_id, token, synced_at=None):
    """Cache ``token`` (the per-user slot a profile pull brings) for
    ``user_id``: first, ahead of the other devices already cached, which stay
    until Firebase says they are gone (drop_fcm_token).  Returns True on
    write, False on any failure."""
    if not user_id or not token:
        return False
    return store_local_fcm_tokens(
        user_id, [str(token)] + get_local_fcm_tokens(user_id), synced_at=synced_at)


def get_local_fcm_tokens(user_id):
    """Every cached FCM token of ``user_id`` from local SQLite, [] for none."""
    if not user_id:
        return []
    try:
        from sqlalchemy import text
        from integrations.social.models import db_session
        with db_session(commit=False) as db:
            db.execute(text(_CREATE_TABLE))
            row = db.execute(
                text("SELECT token FROM fcm_tokens WHERE user_id = :u"),
                {'u': str(user_id)}).fetchone()
            return _cached_tokens(row[0]) if row else []
    except Exception as e:
        logger.debug("get_local_fcm_tokens(%s) failed: %s", user_id, e)
        return []


def get_local_fcm_token(user_id):
    """The first cached FCM token for ``user_id`` (the per-user slot when a
    profile pull brought one), or None."""
    tokens = get_local_fcm_tokens(user_id)
    return tokens[0] if tokens else None


def _forget_local_token(token):
    """Take ``token`` out of every cached list (Firebase said it is gone).
    Best-effort, never raises."""
    if not token:
        return
    try:
        import json
        from sqlalchemy import text
        from integrations.social.models import db_session
        with db_session() as db:
            db.execute(text(_CREATE_TABLE))
            rows = db.execute(text("SELECT user_id, token FROM fcm_tokens")).fetchall()
            for user_id, value in rows:
                cached = _cached_tokens(value)
                if token not in cached:
                    continue
                kept = [t for t in cached if t != token]
                if kept:
                    db.execute(text("UPDATE fcm_tokens SET token = :t WHERE user_id = :u"),
                               {'t': json.dumps(kept), 'u': user_id})
                else:
                    db.execute(text("DELETE FROM fcm_tokens WHERE user_id = :u"),
                               {'u': user_id})
    except Exception as e:
        logger.debug("_forget_local_token failed: %s", e)


# ── Local UUID ↔ central account-id mapping (#90) ────────────────────────────
#
# The central FCM registry is keyed by the central Hevolve account id (the
# phone / account-number, e.g. 9003054371), but the local notification path
# carries the local social ``User.id`` (a UUID).  When HARTOS knows the central
# id for a user — from the person's own profile pull (core.profile_sync), never
# from a sync batch (#188) — we stash it on the canonical local
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
    committed (core.profile_sync.sync_profile calls it after its user write
    commits)."""
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


def device_tokens(user_id, synced_at=None):
    """Every device token of ``user_id``: fetched from the central registry
    (the per-user slot and each device row, fetch_central_fcm_tokens) and
    cached locally; a list fetched less than DEVICE_LIST_FRESH_S ago is
    served from the cache, so a burst of pushes makes one round-trip.  If
    the central fetch misses (unregistered / offline), the cached list, so
    an offline node keeps using the last-known tokens.  [] if neither
    exists.

    One slot per person meant the last device to register took every push
    from the others (the owner's tablet and phone, 23 Nov 2026); the device
    rows give each device its own token.

    Identity (#90): the registry is keyed by the central account id, so we query
    it by ``resolve_central_id(user_id)`` when that mapping is known, and cache
    the result under the LOCAL ``user_id`` (the key the push path looks up).
    With no mapping we query by ``user_id`` itself — unchanged behaviour.
    """
    cached, age = _cached_device_list(user_id)
    if cached and age is not None and 0 <= age < DEVICE_LIST_FRESH_S:
        return cached          # fetched moments ago: no round-trip per push
    registry_id = resolve_central_id(user_id) or user_id
    tokens = fetch_central_fcm_tokens(registry_id)
    if tokens:
        store_local_fcm_tokens(user_id, tokens,
                               synced_at=synced_at or repr(time.time()))
        return tokens
    return cached or get_local_fcm_tokens(user_id)


# How long a device list device_tokens fetched serves the pushes after it
# before the registry is asked again (a device registered since is reached
# within this).
DEVICE_LIST_FRESH_S = 600


def _cached_device_list(user_id):
    """``(tokens, seconds since device_tokens fetched them)`` from the local
    cache; the age is None when the row was not stamped by device_tokens
    (a profile pull's token, an older build's row).  ``([], None)`` for
    none.  Never raises."""
    if not user_id:
        return [], None
    try:
        from sqlalchemy import text
        from integrations.social.models import db_session
        with db_session(commit=False) as db:
            db.execute(text(_CREATE_TABLE))
            row = db.execute(
                text("SELECT token, synced_at FROM fcm_tokens WHERE user_id = :u"),
                {'u': str(user_id)}).fetchone()
        if not row:
            return [], None
        try:
            age = time.time() - float(row[1])
        except (TypeError, ValueError):
            age = None
        return _cached_tokens(row[0]), age
    except Exception as e:
        logger.debug("_cached_device_list(%s) failed: %s", user_id, e)
        return [], None


def sync_fcm_token(user_id, synced_at=None):
    """The first of ``device_tokens(user_id)`` (fetched and cached), or
    None."""
    tokens = device_tokens(user_id, synced_at=synced_at)
    return tokens[0] if tokens else None


# ── Decentralized push: local node → Google FCM, using the synced token ─────

_FCM_V1_ENDPOINT = 'https://fcm.googleapis.com/v1/projects/{project}/messages:send'

# Stamped into every push: sending through Google's FCM leaves the user's
# local-private tier, so we never do it silently — the client surfaces this to
# the user (per the privacy-transparency rule).
_PRIVACY_TIER_NOTICE = (
    'Delivered via the central FCM relay to reach your device — this left your '
    'local-private tier.'
)


# The push kinds the phone acts on (contract with the Android app, cointinue,
# 23 Nov 2026): every such push carries ``kind`` in its data, and a banner of
# an ask names the screen its tap opens (android.notification.click_action);
# the tap delivers the push's data as that intent's extras under the same
# names (kind, ask_id, request_id, prompt_id, node_id).  The phone then reads
# the ask from the node that asked (a person ask: GET /api/agent/approval
# ?ask_id=; a consent ask: GET /api/social/consent?request_id=).
ASK_KIND = 'ui_ask'
CONSENT_KIND = 'consent_prompt'
ANSWERED_KIND = 'ask.answered'
PUSH_KINDS = (ASK_KIND, CONSENT_KIND, ANSWERED_KIND)
CLICK_ACTIONS = {
    ASK_KIND: 'com.hertzai.hevolve.OPEN_PERSON_ASK',
    CONSENT_KIND: 'com.hertzai.hevolve.OPEN_CONSENT_ASK',
}


def push_kind(data) -> str:
    """The contract's ``kind`` of a push's data ('' for any other push): its
    own ``kind`` or, as every builder spells it today, its ``type``."""
    data = data or {}
    kind = str(data.get('kind') or data.get('type') or '')
    return kind if kind in PUSH_KINDS else ''


def build_fcm_v1_message(token, title, body, data=None, privacy_tier_skipped=True):
    """Build an FCM HTTP v1 ``message`` body (pure — unit-testable).

    All v1 ``data`` values must be strings.  When the push leaves the local
    tier (the default for a personal push routed off-device), stamps the
    privacy-tier notice so the user is told their message used the central relay.

    Shaped as central's relay shapes it (chatbot_pipeline/confirmation.sendPush),
    so a phone gets the same message whichever node sends it: high priority,
    kept for 24 h.  A push with a title or a body has a notification block,
    a consent ask's too: Xiaomi's HyperOS delivers no data-only push to a
    stopped app (owner's Redmi, 23 Nov 2026), so the system has to show the
    banner itself.  A banner of an ask carries the ask's id as its tag (so a
    later push for that ask replaces it) and the screen its tap opens
    (CLICK_ACTIONS).  A push with neither title nor body stays data-only: the
    phone acts on it without showing anything; the ask.answered drop names
    the ask by the same id the banner was tagged with, which the phone takes
    down (MyFirebaseMessagingService.questionAnswered).
    """
    _data = {str(k): str(v) for k, v in (data or {}).items()}
    if privacy_tier_skipped:
        _data['privacy_tier_skipped'] = 'true'
        _data['privacy_notice'] = _PRIVACY_TIER_NOTICE
    kind = push_kind(_data)
    if kind:
        _data['kind'] = kind
    message = {'token': token, 'data': _data,
               'android': {'priority': 'high', 'ttl': '86400s'}}
    if title or body:
        message['notification'] = {'title': title or '', 'body': body or ''}
        shown = {}
        tag = _data.get('ask_id') or _data.get('request_id')
        if tag:
            shown['tag'] = tag
        if kind in CLICK_ACTIONS:
            shown['click_action'] = CLICK_ACTIONS[kind]
        if shown:
            message['android']['notification'] = shown
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
    as the governor hang)."""
    access = _fcm_access_token()
    project = os.environ.get('HART_FCM_PROJECT', '')
    if not access or not project:
        return None, None
    return access, project


def fcm_token_is_dead(status_code, body) -> bool:
    """Whether Firebase's answer to a send says the TOKEN is gone (pure): 404
    UNREGISTERED (the app was uninstalled or the token rotated), or 400
    INVALID_ARGUMENT about the registration token itself.  A 400 about the
    payload says nothing about the token, so it never counts."""
    err = body.get('error') if isinstance(body, dict) else None
    if not isinstance(err, dict):
        return False
    codes = {str(d.get('errorCode', '')) for d in (err.get('details') or [])
             if isinstance(d, dict)}
    codes.add(str(err.get('status', '')))
    if status_code == 404:
        # only by name: a wrong project is a 404 NOT_FOUND too, and must
        # never cost every device its token
        return 'UNREGISTERED' in codes
    return (status_code == 400 and 'INVALID_ARGUMENT' in codes
            and 'registration token' in str(err.get('message', '')).lower())


def drop_fcm_token(token, timeout=8):
    """Forget a token Firebase says is gone: at the central registry
    (Hevolve_Database ``POST /delete_fcm_token``, which deletes the device
    row holding it and clears a per-user slot holding it) and in this node's
    cache.  Best-effort, never raises."""
    if not token:
        return
    try:
        import requests
        requests.post(f"{_FCM_REGISTRY}/delete_fcm_token", json={'token': str(token)},
                      timeout=timeout)
    except Exception as e:
        logger.debug("drop_fcm_token at the registry failed: %s", e)
    _forget_local_token(str(token))


def _post_fcm_message(access, project, token, title, body, data, timeout):
    """POST one built message to FCM v1.  Returns True on a 200, else False.
    A token Firebase says is gone is dropped (fcm_token_is_dead,
    drop_fcm_token).  Never raises.  The single FCM-send implementation
    (send_fcm_push's direct leg)."""
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
        try:
            answer = resp.json()
        except Exception:
            answer = None
        if fcm_token_is_dead(resp.status_code, answer):
            logger.info("FCM: a device token is no longer registered; dropped")
            drop_fcm_token(token)
        return False
    except Exception as e:
        logger.debug("_post_fcm_message failed: %s", e)
        return False


# How long hand_push_to_central waits for its Crossbar post to finish: the
# HTTP publisher's own timeout (hart_intelligence_entry, 2 s) plus a worker
# that may be busy with one post before it.
RELAY_POST_WAIT_S = 5.0

# Keys central builds for itself (confirmation.py) or drops from a relayed push.
_CENTRAL_OWNED_PUSH_KEYS = ('type', 'topic_reply', 'user_id', 'request_id', 'action',
                            'title', 'body', 'bot_type', 'topic_name', 'response')


def relay_carries(title, body, data=None) -> bool:
    """Whether central's relay can carry this push: every push with a title
    or a body, a consent prompt, and the ask.answered drop, which central
    sends data-only (chatbot_pipeline relay_push.DATA_ONLY_PUSH_TYPES) with
    the request_id the phone takes the ask's banner down by.  Not a
    data-only push of any other kind: central would send it with a
    notification block titled 'Missed Notification From AI Buddy', ringing
    the phone for something meant to be silent.

    The one statement of the rule: hand_push_to_central refuses on it, and
    a caller that logs a push that did not go out (realtime._send_push)
    says why from it."""
    return bool(title or body) or push_kind(data) in (CONSENT_KIND, ANSWERED_KIND)


def hand_push_to_central(user_id, title, body, data=None):
    """Give a push to central's relay when this node has no FCM credential.

    A consumer install has no Firebase service account, and must not: central
    already holds one (chatbot_pipeline/confirmation.py).  The node publishes
    the message as a PENDING confirmation on the bus (topic 'task.confirmation',
    which the default relay bridge carries to central); central looks up the
    user's token and pushes it after its own delay, carrying ``push_title`` and
    ``push_data`` through so the phone receives what was meant to be sent.

    Central keys the token by the numeric account id (it does int() on the
    topic suffix), so a local id with no known central mapping is not handed
    off -- a non-numeric suffix would raise inside central's loop.

    Reach depends on the node's mode.  The message leaves on the bus's
    crossbar leg; Nunba points that at central only in Hybrid/Hive mode
    (main.py), and at the local Flask bridge in local-only mode, where it
    never reaches central.  Central also pushes only a pending message that is
    still unacknowledged after 30 s and sweeps every 10 s, so a relayed push
    lands 30-40 s late.  A consent prompt uses this path too (it is the only
    way a credential-less node can reach a phone that is not looking at the
    app); the consent itself stays pending, and is also a notification record
    and a fleet command, so a prompt that arrives late is still honoured -- the
    overlay's own 60 s dismiss only hides the overlay.  Its message is shaped
    the way central's existing consent path expects: bot_type='consent_prompt'
    and the consent's own request_id, so the phone's answer maps back.

    Returns True only when the message's Crossbar post SUCCEEDED (the bus's
    crossbar_delivered, waited on for up to RELAY_POST_WAIT_S): the router's
    HTTP bridge took it.  Before, True meant a worker had queued the post,
    so a post that failed, or a stub publisher that answered {"ok": True}
    and sent nothing, read as handed.  True is still not a delivery receipt
    from the phone, and in local-only mode the bridge is this machine's
    own.  False when there is nothing to hand off, no transport, or the post
    failed or did not finish in time.  Best-effort, never raises.
    """
    if not relay_carries(title, body, data):
        # A data-only push of a kind central sends with a notification
        # block (relay_carries): it would ring as a banner.
        logger.debug("hand_push_to_central(%s): a data-only push is not relayed "
                     "(central's relay would show it as a banner)", user_id)
        return False
    try:
        central_id = resolve_central_id(user_id) or str(user_id)
        if not str(central_id).isdigit():
            logger.debug("hand_push_to_central(%s): no numeric central id", user_id)
            return False
        import uuid
        from core.peer_link.message_bus import get_message_bus
        data = data or {}
        consent = str(data.get('type', '')) == 'consent_prompt'
        # central keys a consent's answer by this id, so keep the consent's own
        request_id = (str(data['request_id']) if consent and data.get('request_id')
                      else 'push-' + uuid.uuid4().hex)
        # Central derives these itself or drops them, and the shared-URI egress
        # scrub would rewrite a 10-digit id inside them (a reply topic became
        # '...pupit.[PHONE_REDACTED]'), so none of them belong in push_data.
        push_data = {str(k): str(v) for k, v in data.items()
                     if k not in _CENTRAL_OWNED_PUSH_KEYS}
        # type is central's to decide for a consent; a game-sound review names its own
        if not consent and data.get('type'):
            push_data['type'] = str(data['type'])
        push_data['privacy_tier_skipped'] = 'true'
        push_data['privacy_notice'] = _PRIVACY_TIER_NOTICE
        message = {
            'request_id': request_id,
            'topic_name': f'com.hertzai.pupit.{central_id}',
            'bot_type': 'consent_prompt' if consent else 'Hevolve',
            'confirmation': False,
            'user_id': str(central_id),
            'text': [body or ''],
            'push_title': title or '',
            'push_data': push_data,
        }
        if consent:
            message['action'] = 'consent_prompt'
        bus = get_message_bus()
        msg_id = bus.publish('task.confirmation', message,
                             user_id=str(user_id), skip_sse=True, skip_peerlink=True)
        return bus.crossbar_delivered(msg_id, timeout=RELAY_POST_WAIT_S) is True
    except Exception as e:
        logger.debug("hand_push_to_central failed: %s", e)
        return False


def _relay_push(user_id, title, body, data):
    """The relay leg of send_fcm_push: central sends it.  A consent that could
    not be queued must not vanish quietly."""
    handed = hand_push_to_central(user_id, title, body, data)
    if not handed and str((data or {}).get('type', '')) == 'consent_prompt':
        logger.warning("consent prompt for %s was not queued for the phone "
                       "(the direct send did not go and no relay transport "
                       "accepted it); it remains pending as a notification "
                       "and in the app", user_id)
    return handed


def send_fcm_push(user_id, title, body, data=None, timeout=8, relay=True):
    """Push an FCM notification to every device of ``user_id``.

    One push takes exactly one path.  The direct send comes first: this node's
    own FCM credential, sent to each of the person's device tokens
    (device_tokens: the registry's list, the local cache when it is
    unreachable), so a tablet and a phone both get it; a token Firebase says
    is gone is dropped (_post_fcm_message).  When no device took it (no
    credential, no token, FCM refused or timed out) the push goes to
    central's relay instead (see ``hand_push_to_central`` for its limits,
    notably the 30-40 s it takes; central sends to every device too).  The
    relay is only tried after the direct send reported failure, so a push
    goes down one path per reported result.  (A POST that timed out after FCM
    accepted it reads as a failure and can be duplicated by the relay; that
    is the cost of a best-effort send.)

    The relay is on by default: a consumer install has no Firebase credential,
    and nobody opts in to being reachable.  ``relay=False`` is for a caller
    that must never leave this node.

    Best-effort, never raises.  Returns True when FCM answered 200 for at
    least one device (direct) or the message's post to central's relay
    succeeded (relay; that is not a delivery receipt), else False.  The edge
    credential (HART_FCM_SA_FILE / HART_FCM_ACCESS_TOKEN) + HART_FCM_PROJECT
    gate the direct send.
    """
    if not user_id:
        return False
    access, project = _fcm_credential()
    if access:
        sent = [_post_fcm_message(access, project, token, title, body, data, timeout)
                for token in device_tokens(user_id)]
        if any(sent):
            return True
    else:
        logger.debug("send_fcm_push(%s): no FCM credential/project, direct send off",
                     user_id)
    if not relay:
        return False
    return _relay_push(user_id, title, body, data)


# Read from the environment as this node's own configuration or key
# material: a vault or consent-card value must never set these.
# tests/unit/test_env_secrets_declared.py fails on a secret read not
# declared here or in ENV_SECRETS.
ENV_NOT_FROM_VAULT = (
    'HART_FCM_ACCESS_TOKEN',
)
