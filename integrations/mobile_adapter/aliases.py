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
from urllib.parse import urlsplit


class Alias(NamedTuple):
    method: str      # GET, POST, ...
    host: str        # the cloud host the phone calls, e.g. azurekong.hertzai.com
    path: str        # the cloud path; a prefix when it ends with '/'
    desktop: str     # the desktop route; for a prefix, the rest of the path follows it


#: The rows (plan #185 phase 6 adds them one call at a time).
ALIASES: tuple = ()


def _safe(path: str) -> bool:
    """A path whose segments are plain: no '..' or empty segment to walk out
    of the prefix it matched."""
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
    "METHOD host path" per row (a path ending in '/' is a prefix)."""
    return [f'{a.method} {a.host} {a.path}' for a in (ALIASES if aliases is None else aliases)]
