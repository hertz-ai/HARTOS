"""Per-type card image SOURCING for app + agent cards (#143 / steward d8).

Two responsibilities, both producing a URL STRING the producer stamps on
``card['image_url']`` so the bytes are fetched + cached EXACTLY ONCE by the W10
ImageCache (``/api/media/image``).  This module never opens a second
image-fetch path and never stores image bytes itself; on any miss it returns
None so the card falls back to the deterministic brand-art the client already
paints.  Never raises.

  1. resolve_app_poster(app_id)  -> the most appropriate APP poster, sourced
     from the OFFICIAL marketplace listing (Flathub appstream screenshot or the
     Flathub icon CDN) or the app's official website (og:image, used only when
     the listing carries no screenshot).  This is real app artwork, per the
     steward's "from the official website OR the marketplace/app-store listing
     image".

  2. agent_art_url(name, model_bus_port) -> a synthetically GENERATED per-agent
     art URL, when a LOCAL image generator is reachable through the Model Bus.
     Today NO on-device image-gen backend exists (Model Bus _route_image_gen is
     a stub; the channels ImageGenerator needs cloud keys, which the
     decentralization + privacy lenses forbid by default), so this honestly
     returns None and the agent card keeps the deterministic HartBrandArt
     gradient + the dark-to-light scrim + the agent name the client already
     composites.  We do NOT stub a fake generator; this is the SINGLE seam that
     lights up the moment a real local generator registers as an 'image_gen'
     backend - then it flows through the SAME ImageCache + scrim path, zero
     client change.

Decentralization / privacy: Flathub is the canonical app-store listing source
the steward named.  It is an OPTIONAL accelerant, never a gatekeeper - every
miss degrades to brand-art, the resolver works with the network OFF (returns
None), and nothing personal leaves the device: only the PUBLIC app id is sent,
and only to resolve PUBLIC artwork.
"""

import json
import logging
import os
import re
import threading
import time
from typing import Optional

logger = logging.getLogger('hevolve.app_poster')

# Flathub is the canonical marketplace listing source (the steward's "app-store
# listing image").  These are the ONLY hosts this resolver itself contacts for
# the LISTING path; the website og:image path contacts the homepage URL Flathub
# publishes in the same listing.
_FLATHUB_APPSTREAM = 'https://flathub.org/api/v2/appstream/%s'
_FLATHUB_ICON = ('https://dl.flathub.org/repo/appstream/x86_64/icons/'
                 '128x128/%s.png')

# A Flathub application id is reverse-DNS (org.mozilla.firefox).  Validate before
# any network call so we never send junk to Flathub and only ever build URLs
# from a known-safe id.
_APP_ID_RE = re.compile(r'^[A-Za-z0-9][A-Za-z0-9_-]*(\.[A-Za-z0-9][A-Za-z0-9_-]*)+$')

# Resolved posters are cached (positive AND negative) so the CONTINUOUS home
# compose never re-hits Flathub on every idle cycle.  A hit is long-lived; a
# miss is re-checked sooner (an app may gain a screenshot, or come back online).
_HIT_TTL = 7 * 24 * 3600.0
_MISS_TTL = 6 * 3600.0
_HTML_CAP = 200 * 1024            # bytes of homepage HTML scanned for og:image
_FETCH_TIMEOUT = 6.0

_lock = threading.RLock()
_cache: Optional[dict] = None     # app_id -> {'url': str|None, 'ts': float}
_cache_path_cached: Optional[str] = None

# Model-Bus image-gen availability is probed at most once per this window so a
# continuous compose costs a dict lookup, not a network call, while a generator
# installed mid-session is still discovered within the window.
_GEN_PROBE_TTL = 5 * 60.0
_gen_lock = threading.RLock()
_gen_state = {'ready': False, 'ts': 0.0}


# ─── poster cache (disk-backed, best-effort) ─────────────────────────────────

def _cache_path() -> str:
    global _cache_path_cached
    if _cache_path_cached is not None:
        return _cache_path_cached
    try:
        from core.platform_paths import get_data_dir
        base = get_data_dir()
    except Exception:
        base = os.path.join(os.path.expanduser('~'), '.hartos')
    _cache_path_cached = os.path.join(base, 'app_posters.json')
    return _cache_path_cached


def _load_cache() -> dict:
    global _cache
    if _cache is not None:
        return _cache
    with _lock:
        if _cache is not None:
            return _cache
        data = {}
        try:
            with open(_cache_path(), 'r', encoding='utf-8') as f:
                raw = json.load(f)
            if isinstance(raw, dict):
                data = {k: v for k, v in raw.items() if isinstance(v, dict)}
        except (OSError, ValueError):
            data = {}
        _cache = data
        return _cache


def _save_cache() -> None:
    path = _cache_path()
    tmp = path + '.tmp'
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(_cache or {}, f)
        os.replace(tmp, path)
    except OSError as e:
        logger.debug('app_poster cache save failed: %s', e)


def _cache_get(app_id: str):
    """Return (hit, url). hit=False -> not cached / expired (must resolve)."""
    cache = _load_cache()
    with _lock:
        rec = cache.get(app_id)
    if not isinstance(rec, dict):
        return False, None
    url = rec.get('url')
    ts = rec.get('ts') or 0.0
    ttl = _HIT_TTL if url else _MISS_TTL
    if (time.time() - ts) > ttl:
        return False, None
    return True, url


def _cache_put(app_id: str, url: Optional[str]) -> None:
    cache = _load_cache()
    with _lock:
        cache[app_id] = {'url': url, 'ts': time.time()}
        _save_cache()


# ─── url helpers ─────────────────────────────────────────────────────────────

def _is_http_url(u) -> bool:
    return isinstance(u, str) and (u.startswith('http://')
                                   or u.startswith('https://'))


def flathub_icon_url(app_id: str) -> Optional[str]:
    """The deterministic Flathub icon-CDN URL for ``app_id`` (no network).

    The W10 ImageCache 404-degrades to None when the app is not on Flathub, so
    an id that is not a real listing simply falls back to brand-art."""
    if not app_id or not _APP_ID_RE.match(app_id):
        return None
    return _FLATHUB_ICON % app_id


_OG_RE = re.compile(
    r'<meta[^>]+(?:property|name)\s*=\s*["\']og:image["\'][^>]*>',
    re.IGNORECASE)
_CONTENT_RE = re.compile(r'content\s*=\s*["\']([^"\']+)["\']', re.IGNORECASE)


def _parse_og_image(html: str) -> Optional[str]:
    """Extract the og:image URL from a page <head> (tolerant of attribute
    order; no bs4 dependency).  Returns an http(s) URL or None."""
    if not html:
        return None
    m = _OG_RE.search(html)
    if not m:
        return None
    cm = _CONTENT_RE.search(m.group(0))
    if not cm:
        return None
    url = cm.group(1).strip()
    return url if _is_http_url(url) else None


def _appstream_listing(app_id: str):
    """Query the Flathub appstream listing for ``app_id``.

    Returns (screenshot_url_or_None, homepage_url_or_None).  Offline / unknown
    app / bad JSON -> (None, None)."""
    try:
        from core.http_pool import pooled_get
        resp = pooled_get(_FLATHUB_APPSTREAM % app_id, timeout=_FETCH_TIMEOUT)
    except Exception as e:
        logger.debug('appstream fetch failed (offline?) %s: %s', app_id, e)
        return None, None
    if getattr(resp, 'status_code', 0) != 200:
        return None, None
    try:
        data = resp.json()
    except Exception:
        return None, None
    if not isinstance(data, dict):
        return None, None

    shot = None
    shots = data.get('screenshots')
    if isinstance(shots, list):
        for s in shots:
            if not isinstance(s, dict):
                continue
            # appstream screenshot shape: {'sizes': [{'src': url, ...}, ...]}
            # or a flat {'src'/'url': ...}.  Take the LARGEST size for a poster.
            cand = None
            sizes = s.get('sizes')
            if isinstance(sizes, list) and sizes:
                best, best_w = None, -1
                for sz in sizes:
                    if not isinstance(sz, dict):
                        continue
                    src = sz.get('src') or sz.get('url')
                    try:
                        w = int(sz.get('width') or 0)
                    except (TypeError, ValueError):
                        w = 0
                    if _is_http_url(src) and w > best_w:
                        best, best_w = src, w
                cand = best
            if not cand:
                src = s.get('src') or s.get('url') or s.get('imgDesktopUrl')
                cand = src if _is_http_url(src) else None
            if cand:
                shot = cand
                break

    homepage = None
    urls = data.get('urls')
    if isinstance(urls, dict):
        hp = urls.get('homepage')
        if _is_http_url(hp):
            homepage = hp
    return shot, homepage


def _og_image_for(homepage: str) -> Optional[str]:
    """GET the official website and return its og:image URL (bounded read).

    This is the ONLY genuinely new fetch the resolver makes beyond the Flathub
    listing, so it is gated behind a listing miss and rarely runs."""
    try:
        from core.http_pool import pooled_get
        resp = pooled_get(homepage, timeout=_FETCH_TIMEOUT)
    except Exception as e:
        logger.debug('og:image fetch failed %s: %s', homepage, e)
        return None
    if getattr(resp, 'status_code', 0) != 200:
        return None
    try:
        html = resp.text or ''
    except Exception:
        return None
    return _parse_og_image(html[:_HTML_CAP])


def resolve_app_poster(app_id: str, prefer: str = 'poster') -> Optional[str]:
    """Resolve ``app_id`` to the most appropriate app-card image URL.

    prefer='poster' (default, for wide home cards): the marketplace listing
      screenshot, else the official-website og:image, else the icon CDN.
    prefer='icon' (for small app-icon tiles): the deterministic Flathub icon CDN
      URL (no network).

    Returns an http(s) URL the producer stamps on card['image_url'] (the W10
    ImageCache then fetches + caches it once), or None to fall back to brand-art.
    Cached (positive + negative) so the continuous compose never re-hits the
    network for the same app.  Never raises."""
    try:
        app_id = (app_id or '').strip()
        if not app_id or not _APP_ID_RE.match(app_id):
            return None
        if prefer == 'icon':
            return flathub_icon_url(app_id)

        hit, url = _cache_get(app_id)
        if hit:
            return url

        shot, homepage = _appstream_listing(app_id)
        resolved = shot
        if not resolved and homepage:
            resolved = _og_image_for(homepage)
        if not resolved:
            # Last resort for a real listing with no screenshot/og:image: the
            # icon CDN (still real marketplace artwork, just smaller).
            resolved = flathub_icon_url(app_id)
        resolved = resolved if _is_http_url(resolved) else None
        _cache_put(app_id, resolved)
        return resolved
    except Exception as e:                       # never break a compose
        logger.debug('resolve_app_poster failed %s: %s', app_id, e)
        return None


# ─── agent-card generated art (honest seam) ──────────────────────────────────

def _image_gen_ready(model_bus_port: Optional[int]) -> bool:
    """Probe (cached) whether a LOCAL image generator is reachable through the
    Model Bus.  Today the Model Bus exposes no image_gen backend (its router is
    a stub), so this returns False and agent cards keep the brand-art fallback.

    The probe is the honest realisation of the steward rule "find if an image
    generator is reachable and use it; if none exists, fall back": when a real
    local generator registers as an 'image_gen' backend (status 'ready'), this
    flips True and agent_art_url begins requesting generated art."""
    now = time.time()
    with _gen_lock:
        if (now - _gen_state['ts']) < _GEN_PROBE_TTL:
            return _gen_state['ready']
    ready = False
    try:
        port = int(model_bus_port or int(os.environ.get('HART_MODEL_BUS_PORT', 6790)))
        from core.http_pool import pooled_get
        # /v1/status -> {'backends': {name: status_string, ...}, ...}.  An
        # image_gen backend reporting 'ready' is the signal to generate.  Use
        # 127.0.0.1 (not 'localhost') so a closed port refuses instantly instead
        # of stalling on the IPv6->IPv4 fallback - this runs on the idle compose,
        # so the negative case must be cheap, not a multi-second connect wait.
        resp = pooled_get('http://127.0.0.1:%d/v1/status' % port, timeout=2)
        if getattr(resp, 'status_code', 0) == 200:
            data = resp.json()
            backends = data.get('backends') if isinstance(data, dict) else None
            if isinstance(backends, dict):
                ready = backends.get('image_gen') == 'ready'
    except Exception:
        ready = False
    with _gen_lock:
        _gen_state['ready'] = ready
        _gen_state['ts'] = now
    return ready


def agent_art_url(name: str, model_bus_port: Optional[int] = None) -> Optional[str]:
    """Per-agent GENERATED art URL when a local image generator is reachable.

    Returns None today (no on-device image-gen backend exists) so the agent card
    keeps the deterministic HartBrandArt gradient + dark-to-light scrim + name
    composite the client already paints - the honest fallback, not a fake gen.
    When a real local generator lands, _image_gen_ready flips True and the art
    URL it returns flows through the SAME card['image_url'] -> ImageCache + scrim
    path with zero client change.  Never raises."""
    try:
        if not name:
            return None
        if not _image_gen_ready(model_bus_port):
            return None
        # A generator is reachable: request art for this agent and accept a URL
        # if the backend returns one.  The current Model Bus image_gen route is a
        # stub that returns text (no URL), so this still degrades to brand-art
        # until a real backend is wired - we never fabricate a URL.
        port = int(model_bus_port or int(os.environ.get('HART_MODEL_BUS_PORT', 6790)))
        from core.http_pool import pooled_post
        prompt = ('Abstract brand artwork for an AI agent named ' + str(name)[:80]
                  + ', dark cinematic gradient, no text')
        resp = pooled_post('http://127.0.0.1:%d/v1/generate' % port,
                           json={'model_type': 'image_gen', 'prompt': prompt},
                           timeout=8)
        if getattr(resp, 'status_code', 0) != 200:
            return None
        data = resp.json()
        if not isinstance(data, dict):
            return None
        for key in ('image_url', 'url', 'image'):
            url = data.get(key)
            if _is_http_url(url):
                return url
        return None
    except Exception as e:
        logger.debug('agent_art_url failed %s: %s', name, e)
        return None
