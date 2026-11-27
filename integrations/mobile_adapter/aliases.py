"""The phone's cloud calls this desktop answers, each mapped to the desktop's
own route.

A row is one call the phone already makes to the cloud -- method, cloud host,
cloud path (a prefix when it ends in '/') -- and the desktop route that does
the same job.  No row means the desktop does not answer that call, and the
phone sends it to the cloud unchanged.  scripts/gen_mobile_contract.py lists
every call the phone makes (integrations/mobile_adapter/contract.json); a row
is added only where the desktop has the canonical equivalent, with a test
that pins the reply the phone already reads.
"""
from typing import NamedTuple, Optional
from urllib.parse import unquote, urlsplit


#: How long a phone waits for this desktop's answer to a call before it sends
#: the call to the cloud.  A short call is answered in well under this; a chat
#: turn runs an agent on the desktop's model (a local 4B's first teaching turn
#: measured ~45 s, a multi-step turn longer), and the phone's chat screens
#: already waited 150 s for one.
SHORT_WAIT_S = 15
CHAT_WAIT_S = 150

#: The cloud host the phone's own calls go to (Android BuildConfig base_url /
#: chatbot_url, PeerLinkDiscovery.CLOUD_BACKEND_URL).
CLOUD = 'azurekong.hertzai.com'


class Alias(NamedTuple):
    method: str      # GET, POST, ...
    host: str        # the cloud host the phone calls, e.g. azurekong.hertzai.com
    path: str        # the cloud path; a prefix when it ends with '/'
    desktop: str     # the desktop route; for a prefix, the rest of the path follows it
    wait_s: int = SHORT_WAIT_S   # how long the phone waits for this desktop


#: The rows (plan #185 phase 6 adds them one call at a time).
ALIASES: tuple = (
    # The Teach Yourself and custom-bot turns (Android TeachYourselfChatApi
    # and CustomChatBotAPI on the user Retrofit).  The desktop answers
    # central's contract for both with the turn /chat runs (Nunba
    # routes/chatbot_routes.py teachme2 / custom_gpt); a turn it does not
    # serve (kids transport scopes: 501) goes to the cloud.
    Alias('POST', CLOUD, '/chat/teachme2', '/chat/teachme2', CHAT_WAIT_S),
    Alias('POST', CLOUD, '/chat/custom_gpt', '/chat/custom_gpt', CHAT_WAIT_S),
    # The answer to an agent's ask (Android PersonAsk.send, desktop-only;
    # integrations.agent_engine.person_ask): the desktop holds its own asks,
    # and its one door takes the phone as the owner who allowed it
    # (social.auth.allowed_device_owner).  A phone's token names nobody on
    # the door's payment, draft and game-sound branches
    # (commerce.approvals.approver_from_request).
    Alias('POST', CLOUD, '/api/agent/approval', '/api/agent/approval'),
    # The read of one ask (GET ?ask_id=) the phone makes when the ask's
    # Firebase banner opened the app (Android PersonAsk.read, desktop-only,
    # as its answer goes): the card for the ask's own person, else 404 gone
    # (person_ask.read_from_request).
    Alias('GET', CLOUD, '/api/agent/approval', '/api/agent/approval'),
    # The phone's camera frames (Android DesktopCameraFrames, desktop-only,
    # one frame in 2 s): the desktop keeps each in its own frame store for
    # captions (Nunba routes/chatbot_routes.py vision_frame_ingest: JPEG
    # body, user_id and channel in the query, 204).  The cloud has no such
    # route, so a frame this desktop does not take goes to the phone's vision
    # WebSocket instead.  A frame is stale in seconds: the phone waits 5.
    Alias('POST', CLOUD, '/api/vision/frame', '/api/vision/frame', 5),
    # The answer to a consent ask this desktop filed (Android
    # ConsentOverlayService / ConsentAnswer: POST /api/social/consent with
    # the ask's request_id, /consent/decline for a no), and the read of that
    # one ask the phone makes before it shows a credential card (GET
    # /api/social/consent?request_id=).  The desktop holds its own asks, so
    # a password typed on the phone goes to the desktop that asked over the
    # device link and never to the cloud (owner 2026-11-22: only to the node
    # that asked).  The phone marks these calls desktop-only when the ask
    # names this desktop (the ask's node_id is the key the handshake gave
    # it); the route itself refuses an answer that names another node or
    # another scope (consent_api._answered_ask).
    Alias('POST', CLOUD, '/api/social/consent', '/api/social/consent'),
    Alias('POST', CLOUD, '/api/social/consent/decline', '/api/social/consent/decline'),
    Alias('GET', CLOUD, '/api/social/consent', '/api/social/consent'),
)


def _safe(path: str) -> bool:
    """A path whose segments are plain: no '..' or empty segment to walk out
    of the prefix it matched.  Judged as the desktop routes it, decoded
    ('%2e%2e' is '..' to the route), with a backslash read as a separator."""
    path = unquote(path).replace('\\', '/')
    return all(seg not in ('..', '.') for seg in path.split('/')[1:]) and '//' not in path


def resolve(method: str, url: str, aliases: Optional[tuple] = None) -> Optional[str]:
    """The desktop route (with the call's query) that answers this cloud
    call, or None when no row covers it."""
    parts = urlsplit(url or '')
    if not parts.hostname or not _safe(parts.path or '/'):
        return None
    method = (method or '').upper()
    for row in (ALIASES if aliases is None else aliases):
        if row.method != method or row.host != parts.hostname:
            continue
        if row.path.endswith('/') and parts.path.startswith(row.path):
            target = row.desktop + parts.path[len(row.path):]
        elif parts.path == row.path:
            target = row.desktop
        else:
            continue
        return target + ('?' + parts.query if parts.query else '')
    return None


def on_host(app, aliases: Optional[tuple] = None) -> tuple:
    """The rows whose desktop route ``app`` serves for the row's method: a
    route this desktop lacks is never claimed (a phone would only meet a 404
    there before going to the cloud).  A prefix row needs a route under its
    prefix."""
    rows = ALIASES if aliases is None else aliases
    urls = app.url_map.bind('localhost')
    kept = []
    for row in rows:
        if row.desktop.endswith('/'):
            ok = any(rule.rule.startswith(row.desktop) and row.method in (rule.methods or ())
                     for rule in app.url_map.iter_rules())
        else:
            try:
                urls.match(row.desktop, method=row.method)
                ok = True
            except Exception:
                ok = False
        if ok:
            kept.append(row)
    return tuple(kept)


def served(aliases: Optional[tuple] = None) -> list:
    """What the handshake tells a phone this desktop answers: one
    "METHOD host path wait_s" per row (a path ending in '/' is a prefix;
    wait_s is how long the phone waits for this desktop before the cloud)."""
    return [f'{a.method} {a.host} {a.path} {a.wait_s}'
            for a in (ALIASES if aliases is None else aliases)]
