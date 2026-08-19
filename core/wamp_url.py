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
# no-env behaviour is byte-identical to what shipped -- OFF a container.
# Inside one, see _container_publish_url(): localhost is provably wrong there.
DEFAULT_PUBLISH_URL = 'http://localhost:8088/publish'

# Fallback only for when core.port_registry cannot be imported. The registry is
# the source of truth for this port ('crossbar', 8088 in both app and OS mode).
_CROSSBAR_PORT_FALLBACK = 8088

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


def _in_container(environ=None):
    """True when this process is running inside a container.

    Same three signals integrations/social/models.py:_IS_DOCKER already uses.
    Kept here rather than imported from there because core must not import from
    integrations (layering); if a canonical core-level predicate is ever added,
    both should move onto it.
    """
    env = os.environ if environ is None else environ
    return (
        os.path.exists('/.dockerenv')
        or env.get('DOCKER_CONTAINER') == 'true'
        or env.get('HEVOLVE_CLOUD_MODE') == 'true'
    )


def _default_route_gateway():
    """The container's default-route gateway (i.e. the host), or None.

    Read from /proc/net/route instead of hardcoding 172.17.0.1. That literal is
    only correct for Docker's default bridge -- it is wrong on a user-defined
    network, and wrong again if the daemon runs with a custom --bip. Asking the
    kernel costs one file read and is right on any of them.

    /proc/net/route stores the gateway as little-endian hex, so 172.17.0.1
    appears as '010011AC' and the octets come back out low byte first.
    """
    try:
        with open('/proc/net/route', encoding='ascii') as fh:
            fh.readline()  # header row
            for line in fh:
                fields = line.split()
                # fields[1] is Destination; 00000000 marks the default route.
                if len(fields) > 2 and fields[1] == '00000000':
                    packed = int(fields[2], 16)
                    return '.'.join(
                        str((packed >> shift) & 0xFF) for shift in (0, 8, 16, 24))
    except (OSError, ValueError):
        return None
    return None


def _container_publish_url():
    """Publish URL aimed at the host's crossbar, or None if undeterminable.

    Only consulted when WAMP_URL is unset AND we are in a container. Both parts
    are derived, not declared: the host from the default route, the port from
    core.port_registry (which already owns 'crossbar').
    """
    gateway = _default_route_gateway()
    if not gateway:
        return None
    try:
        from core.port_registry import get_port
        port = get_port('crossbar') or _CROSSBAR_PORT_FALLBACK
    except Exception:
        port = _CROSSBAR_PORT_FALLBACK
    return f'http://{gateway}:{port}{_PUBLISH_PATH}'


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
        # No explicit setting. Off a container, localhost is right and this is
        # unchanged. Inside one, localhost is the CONTAINER's loopback, not the
        # host -- crossbar runs as a sibling container (nothing in this repo
        # starts one in the app image), so the publish is refused and every
        # telemetry/federation message is dropped with only "Couldn't publish
        # message" in the log. Measured on central 2026-08-19: from inside the
        # langchain container 127.0.0.1:8088 refused while the default-route
        # gateway answered HTTP 200.
        if _in_container(env):
            derived = _container_publish_url()
            if derived:
                return derived
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
