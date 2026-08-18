"""Canonical reader for the ``WAMP_URL`` environment variable.

``WAMP_URL`` is ONE name carrying TWO protocols, and the two halves of this
codebase disagree about which one it is:

  PRODUCERS set a crossbar **WebSocket router** URL --
    scripts/run.sh:49, scripts/run.bat:53,
    scripts/run_with_tracing.sh:60, scripts/run_with_tracing.bat:59
    all export ``ws://azurekong.hertzai.com:8088/ws``
    (docs/scripts/README.md documents it that way too).

  CONSUMERS want an **HTTP publish-bridge** URL --
    hart_intelligence_entry.py and integrations/social/realtime.py both
    fall back to ``http://localhost:8088/publish``.

So whenever a run script is used, both consumers POST to a ``ws://.../ws``
socket as though it were an HTTP route.  The unset-env default hides this:
with no ``WAMP_URL`` the fallback is already a correct publish URL, so it
only breaks in the deployments that actually set the variable -- which
reads as "works on my machine, silently dead in regional/central."

This module is the single place that knows both vocabularies.  Renaming the
variable across the four repos would be the tidier fix, but it is a
coordinated breaking change; normalising at the consumer removes the silent
failure today without stranding any existing deployment.

See memory/feedback_one_name_two_vocabularies.md -- ``WAMP_URL`` is the
worked example there.  Bucket producers vs consumers before "just passing
it through."
"""
import os
from urllib.parse import urlsplit, urlunsplit

# Same literal both consumers used before this module existed; kept so the
# no-env behaviour is byte-identical to what shipped.
DEFAULT_PUBLISH_URL = 'http://localhost:8088/publish'

# crossbar's HTTP bridge always terminates at /publish; the router socket
# path (/ws) is what we translate away from.
_PUBLISH_PATH = '/publish'

_WS_TO_HTTP = {'ws': 'http', 'wss': 'https'}
_HTTP_TO_WS = {'http': 'ws', 'https': 'wss'}

# crossbar's ROUTER socket path — the other half of the vocabulary this module
# exists to reconcile. `/publish` is the HTTP bridge; `/ws` is the router itself.
_ROUTER_PATH = '/ws'

# The canonical router URL, BUILT from the single sources of truth rather than
# spelled out here:
#   host -> core.constants.CENTRAL_HOST        (one literal for the whole repo)
#   port -> core.port_registry.get_port(...)   (registered as 'crossbar': 8088;
#           it was MISSING once and get_port returned 0, making the bridge URL
#           ws://localhost:0/ws — so resolving it beats re-typing it)
#
# This module previously carried its own copy of both, which made it the FOURTH
# place the hostname was written out. Composing keeps the literal in one place
# while leaving WAMP_URL as the override knob.
def _default_router_url() -> str:
    from core.constants import CENTRAL_HOST
    try:
        from core.port_registry import get_port
        port = get_port('crossbar') or 8088
    except Exception:
        # port_registry unavailable (trimmed install / early boot): fall back to
        # the registered value rather than failing to produce a URL at all.
        port = 8088
    return 'ws://%s:%d%s' % (CENTRAL_HOST, port, _ROUTER_PATH)


DEFAULT_ROUTER_URL = _default_router_url()

#: Legacy DNS alias for the SAME box (see core.constants.CENTRAL_HOST_LEGACY_ALIAS).
#: Kept so a reader who greps the old literal lands on the explanation.
LEGACY_ROUTER_ALIAS = 'ws://aws_rasa.hertzai.com:8088/ws'


def resolve_publish_url(environ=None):
    """Return an HTTP publish-bridge URL regardless of which dialect is set.

    ``ws://host:8088/ws``  -> ``http://host:8088/publish``
    ``wss://host:8088/ws`` -> ``https://host:8088/publish``
    ``http://host/publish`` -> unchanged
    unset / blank          -> ``DEFAULT_PUBLISH_URL``

    Anything unparseable is returned untouched rather than replaced: a
    caller that fails loudly on a bad URL is better than one silently
    redirected somewhere it was never configured to reach.
    """
    env = os.environ if environ is None else environ
    raw = (env.get('WAMP_URL') or '').strip()
    if not raw:
        return DEFAULT_PUBLISH_URL

    try:
        parts = urlsplit(raw)
    except ValueError:
        return raw

    http_scheme = _WS_TO_HTTP.get(parts.scheme.lower())
    if not http_scheme or not parts.netloc:
        # Already http(s), or something we don't recognise -- leave it alone.
        return raw

    return urlunsplit((http_scheme, parts.netloc, _PUBLISH_PATH, '', ''))


def resolve_router_url(environ=None):
    """Return a crossbar **WebSocket router** URL regardless of which dialect is set.

    The twin of :func:`resolve_publish_url`. Consumers that open a WAMP session
    (``Component(transports=...)``) need the router socket, not the HTTP bridge:

    ``ws://host:8088/ws``    -> unchanged
    ``http://host/publish``  -> ``ws://host/ws``
    ``https://host/publish`` -> ``wss://host/ws``
    unset / blank            -> :data:`DEFAULT_ROUTER_URL`

    WHY THIS EXISTS: 15 call sites hardcoded the router literal because this module
    only offered the HTTP half, so a node could not be pointed at a REGIONAL host, a
    LAN peer, or the router Nunba already ships locally
    (``wamp_router.start_wamp_router(port=8088)``). WAMP carries central relay AND
    federation here, so a fixed literal means the mesh cannot relay peer-to-peer or
    via a regional host — central becomes the only reachable option in a system whose
    thesis is that it must not be. ``WAMP_URL`` is the one knob that redirects it.

    Anything unparseable is returned untouched, for the same reason
    :func:`resolve_publish_url` does: a caller that fails loudly on a bad URL beats
    one silently redirected somewhere it was never configured to reach.
    """
    env = os.environ if environ is None else environ
    raw = (env.get('WAMP_URL') or '').strip()
    if not raw:
        return DEFAULT_ROUTER_URL

    try:
        parts = urlsplit(raw)
    except ValueError:
        return raw

    scheme = parts.scheme.lower()
    if scheme in _WS_TO_HTTP:
        return raw                      # already a ws(s) router URL
    ws_scheme = _HTTP_TO_WS.get(scheme)
    if not ws_scheme or not parts.netloc:
        return raw                      # unrecognised — leave it alone

    return urlunsplit((ws_scheme, parts.netloc, _ROUTER_PATH, '', ''))
