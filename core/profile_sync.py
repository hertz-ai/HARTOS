"""Profile down-sync — PULL a centrally-created/edited user profile (+ FCM
token + central-id mapping) DOWN into HARTOS's local social store so a node
finally learns about a user who signed up / edited their profile on central
(gap #2).

Background.  The sync fabric is up-only (a leaf POSTs queued items to its
parent via ``SyncEngine.drain_queue``); central (Hevolve_Database, a FastAPI
app) has no push fabric and a leaf is NAT'd, so central can't push to a node.
The local social ``User`` receiver ``SyncEngine._handle_sync_user`` already
exists and is idempotent (create-or-update by id + FCM-token cache +
central-id map, all in one transaction) — but NOTHING ever fed it from
central.  This module is the missing FETCH that produces the ``sync_user``
payload and hands it straight to that EXISTING receiver — no parallel writer.

Mirrors ``core.fcm_sync`` exactly: the same PULL pattern (a node fetches its
own data by central id), the same purity split (a pure payload builder + a
timeout-bounded, exception-swallowing fetch), and the same never-raise
best-effort contract.  The only structural difference is WHICH central service
holds the data: the FCM token lives at ``HART_FCM_REGISTRY`` (mailer), but the
full profile (name / email / phone / preferred_language) lives in the central
account store (Hevolve_Database), reached via the SAME env keys the up-sync
drain resolves its parent with — ``SyncEngine.parent_tier_url()`` — so "where
is central" has ONE resolver, not a second invented here.

GATE (only the user's OWN profile): ``sync_profile`` takes a SINGLE
``central_user_id`` and feeds a SINGLE ``sync_user`` payload to the receiver —
no list, no batch, no fan-out.  The central ``GET /get_user/{user_id}``
endpoint likewise returns exactly one user (single PK lookup, 400 on miss) and
has no bulk variant.  The only caller (``link_device``) passes the central id
bound to the authenticated session (``g.user.id`` is the local UUID), so no
code path can pull a third party's profile.
"""
import logging
import os

logger = logging.getLogger('hevolve.profile_sync')


def _central_url():
    """Base URL of the central account store (Hevolve_Database) this node pulls
    a profile from.  REUSES the same env resolution the up-sync drain uses
    (``SyncEngine.parent_tier_url`` → HEVOLVE_CENTRAL_URL, else
    HEVOLVE_REGIONAL_URL) so there is ONE "where is my parent" source, not a
    second.  Empty on a flat/standalone node (no central → no down-sync)."""
    try:
        from integrations.social.sync_engine import SyncEngine
        return (SyncEngine.parent_tier_url() or '').rstrip('/')
    except Exception:
        # Fall back to the raw env keys if the sync engine import is unavailable
        # (keeps the resolver identical, just without the indirection).
        return (os.environ.get('HEVOLVE_CENTRAL_URL', '')
                or os.environ.get('HEVOLVE_REGIONAL_URL', '')).rstrip('/')


def build_user_sync_payload(local_user_id, central_user_id, central_resp):
    """Translate a central ``GET /get_user`` response into the ``sync_user``
    payload shape ``SyncEngine._handle_sync_user`` consumes (pure —
    unit-testable, no I/O).

    Keys the row on ``local_user_id`` — the LOCAL social UUID the push path
    looks up; the node knows its own id, central does not.  Carries the central
    id through so the receiver populates ``User.settings['central_user_id']``
    (#90 FCM resolution), the central FCMtoken so it lands already mapped to the
    local UUID, plus the profile extras the receiver already tolerates.

    ``username`` is REQUIRED non-empty by the receiver (it early-returns
    otherwise) and central has no username column — so derive it
    deterministically: the central name, else ``str(central_user_id)`` (stable
    so an empty name never mints duplicate-looking local users).
    """
    resp = central_resp if isinstance(central_resp, dict) else {}
    name = (resp.get('name') or '').strip() if isinstance(resp.get('name'), str) else resp.get('name')
    cid = str(central_user_id) if central_user_id is not None else ''
    username = name or cid
    payload = {
        'user_id': str(local_user_id),
        'username': username,
        'display_name': name or username,
        'central_user_id': cid,
    }
    fcm_token = resp.get('FCMtoken') or resp.get('fcm_token')
    if fcm_token:
        payload['fcm_token'] = fcm_token
    # Profile extras the receiver tolerates (it ignores unknown keys); kept
    # minimal — only what the down-sync genuinely needs locally.
    for src, dst in (('preferred_language', 'preferred_language'),
                     ('email_address', 'email'),
                     ('phone_number', 'phone'),
                     ('role', 'role')):
        val = resp.get(src)
        if val:
            payload[dst] = val
    return payload


def fetch_central_profile(central_user_id, timeout=8):
    """GET the profile for ``central_user_id`` from the central account store.

    Returns the JSON dict, or None when unreachable / not found / no central
    configured.  Timeout-bounded + exception-handled (never raises) so callers
    treat it as best-effort.  Clone of ``fcm_sync.fetch_central_fcm_token``.
    """
    if not central_user_id:
        return None
    base = _central_url()
    if not base:
        return None
    try:
        import requests
        resp = requests.get(f"{base}/get_user/{central_user_id}", timeout=timeout)
        if resp.status_code != 200:
            return None
        data = resp.json()
        return data if isinstance(data, dict) else None
    except Exception as e:
        logger.debug("fetch_central_profile(%s) failed: %s", central_user_id, e)
        return None


def sync_profile(local_user_id, central_user_id):
    """Pull ``central_user_id``'s profile DOWN and feed it to the EXISTING
    local social receiver (``SyncEngine._handle_sync_user``) — create-or-update
    the local ``User``, cache the central FCM token, and map the local UUID →
    central id, all in the receiver's one idempotent transaction.

    GATE: a SINGLE central id in → exactly that one user persisted.  No bulk,
    no fan-out — the receiver is handed one payload.

    Best-effort, never raises: a missing/unreachable central, a not-found user,
    or a receiver hiccup all return False without disturbing the caller (the
    same defensive posture as ``fcm_sync.sync_fcm_token``).  Returns True only
    when a profile was fetched and handed to the receiver.
    """
    if not local_user_id or not central_user_id:
        return False
    central_resp = fetch_central_profile(central_user_id)
    if not central_resp:
        return False
    payload = build_user_sync_payload(local_user_id, central_user_id, central_resp)
    try:
        from integrations.social.models import db_session
        from integrations.social.sync_engine import SyncEngine
        with db_session() as db:
            SyncEngine._handle_sync_user(db, payload)
        logger.info("profile_sync: synced central %s → local %s",
                    central_user_id, local_user_id)
        return True
    except Exception as e:
        logger.debug("sync_profile(%s→%s) failed: %s",
                     central_user_id, local_user_id, e)
        return False
