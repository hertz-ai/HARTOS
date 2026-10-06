"""The mobile adapter: a phone's cloud API call, answered by this desktop.

Owner 10-06: every phone API reaches the person's own desktop over the
PeerLink device link (the LAN, else the relay), and one adapter fronts them
all.  It maps the cloud path the phone already calls to the desktop's own
canonical route (aliases.py) and runs that route, so nothing is implemented a
second time.  The phone sends the call as an ``api_request`` on 'dispatch';
a call this desktop does not answer gets ``not_here`` at once, and the phone
sends it to the cloud unchanged.

The route runs in-process on the host app, through the same API gate a
phone's HTTP call passes (security.middleware: the device bearer, the
owner's device_access grant, a JSON body's user_id equal to the token's), as
a remote caller -- never as this machine -- so the gate applies exactly the
rules it applies to that phone over HTTP.
"""
import base64
import binascii
import json
import logging
import uuid
from typing import Any, Optional

from . import aliases

logger = logging.getLogger('hevolve.mobile_adapter')

#: The frame a phone's answer comes back in.
API_REPLY = 'api_reply'

#: The largest request or reply body one call carries over the link.  Past it
#: the phone keeps the call on the cloud, so a large upload never holds the
#: link a chat turn needs (the relay's own frame cap is 8 MB).
MAX_BODY_BYTES = 4 * 1024 * 1024

#: The request headers carried into the desktop route, besides the bearer.
_CARRIED_HEADERS = ('Content-Type', 'Accept', 'Accept-Language')

#: The caller address the gate sees for an adapted call: not loopback, so the
#: gate never takes a phone for this machine, and not an address a LAN host
#: could also have.
REMOTE_ADDR = 'peerlink-device'

_NOT_HERE = {'type': API_REPLY, 'not_here': True}

_HOST_APP = None
#: The rows this desktop claims: aliases.ALIASES on the host app's routes.
_ROWS: tuple = ()


def set_host_app(app) -> None:
    """The Flask app whose routes answer adapted calls: the app bootstrap
    serves (Nunba's on a desktop, hart_intelligence_entry's standalone), and
    the rows of it this desktop claims."""
    global _HOST_APP, _ROWS
    _HOST_APP = app
    _ROWS = aliases.on_host(app) if app is not None else ()


def _token_user(token: str) -> str:
    """The user a device token names (read, not verified: the gate verifies)."""
    try:
        import jwt as pyjwt
        claims = pyjwt.decode(token, options={'verify_signature': False,
                                              'verify_exp': False},
                              algorithms=['HS256'])
    except Exception:
        return ''
    return str(claims.get('user_id') or '')


def _request_id(body: bytes) -> str:
    """The id the call names in its JSON body, else a fresh one.  A request
    with no id is background to this desktop (core.foreground.mark_view via
    dispatch.is_genuine_user_request), and a phone's call is the person
    acting now: the phone's teach and custom-bot bodies carry none (their
    routes mint one inside the view, after the foreground rule ran)."""
    try:
        rid = json.loads(body).get('request_id') if body else None
    except (ValueError, AttributeError, UnicodeDecodeError):
        rid = None
    return str(rid) if rid else f'device-{uuid.uuid4().hex[:12]}'


def _reply(status: int, body: bytes, content_type: str = 'application/json') -> dict:
    return {'type': API_REPLY, 'status': status, 'content_type': content_type,
            'body_b64': base64.b64encode(body).decode('ascii')}


def answer(link, frame: dict) -> dict:
    """Run one adapted call for a device link and return its reply frame."""
    method = str(frame.get('method') or 'GET').upper()
    url = str(frame.get('url') or '')
    route = aliases.resolve(method, url, _ROWS)
    if route is None or _HOST_APP is None:
        return _NOT_HERE
    token = frame.get('device_token')
    token = token if isinstance(token, str) else ''
    if token and _token_user(token) != link.user_id:
        logger.warning("Device %s sent a call under another user's token; refused",
                       link.peer_id)
        return _reply(403, b'{"error":"the token is not this link\'s user"}')
    try:
        body = base64.b64decode(frame.get('body_b64') or '', validate=True)
    except (binascii.Error, ValueError):
        return _reply(400, b'{"error":"body is not base64"}')
    if len(body) > MAX_BODY_BYTES:
        return _NOT_HERE
    sent = frame.get('headers') if isinstance(frame.get('headers'), dict) else {}
    headers = {h: str(sent[h]) for h in _CARRIED_HEADERS if h in sent}
    if token:
        headers['Authorization'] = f'Bearer {token}'
    headers['X-HARTOS-Request-ID'] = _request_id(body)
    with _HOST_APP.test_client() as client:
        resp = client.open(route, method=method, data=body or None, headers=headers,
                           environ_base={'REMOTE_ADDR': REMOTE_ADDR})
    data = resp.get_data()
    if len(data) > MAX_BODY_BYTES:
        return _NOT_HERE
    # Shape only, never the body.
    logger.info("Device %s %s %s -> %s %d (%d bytes)", link.peer_id, method,
                url.split('?', 1)[0], route.split('?', 1)[0], resp.status_code, len(data))
    return _reply(resp.status_code, data, resp.headers.get('Content-Type', ''))


def handle_api_request(channel: str, data: Any, peer_id: str) -> Optional[dict]:
    """The 'dispatch' handler for a device's ``api_request``: anything else
    is not this handler's (None, other handlers decide)."""
    from core.peer_link.channels import API_REQUEST
    if not isinstance(data, dict) or data.get('type') != API_REQUEST:
        return None
    try:
        from core.peer_link.link_manager import get_link_manager
        link = get_link_manager().get_link(peer_id)
    except Exception as e:
        logger.warning("Device API call from %s unanswered: link lookup failed: %s",
                       peer_id, e)
        return None
    if link is None or link.kind != 'device' or not link.user_id:
        return None
    try:
        return answer(link, data)
    except Exception as e:
        logger.warning("Device %s API call failed on the desktop: %s", peer_id, e)
        return _reply(502, b'{"error":"the desktop could not run this call"}')


def install(app) -> bool:
    """Answer a person's own devices' API calls on the device link: the host
    app, the one 'dispatch' handler (bound once however often this runs), and
    the paths the handshake names -- the rows whose route ``app`` serves, so
    call it after the routes are registered.  False when PeerLink is not
    available."""
    try:
        from core.peer_link.channels import API_REQUEST
        from core.peer_link.link_manager import get_link_manager
    except Exception as e:
        logger.warning("Mobile adapter not installed: %s", e)
        return False
    set_host_app(app)
    manager = get_link_manager()
    manager.register_channel_handler('dispatch', handle_api_request,
                                     answers=(API_REQUEST,))
    manager.advertise('mobile_paths', aliases.served(_ROWS))
    return True
