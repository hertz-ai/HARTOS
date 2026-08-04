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
