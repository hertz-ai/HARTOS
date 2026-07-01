"""HART OS curated app catalog — Python source-of-truth (offline-first).

ONE canonical catalog (nixos/modules/hart-app-catalog.json) feeds BOTH the
NixOS module (hart-apps.nix bakes the preinstall set into systemPackages) AND
this Python module (serves the App Store + Appearance dedup with the network
OFF). JSON is the only format both Nix (builtins.fromJSON) and Python
(json.load) parse natively, so there is exactly one list, no parallel copy to
drift. The shell's static marketplace JS keeps its own hard-coded fallback, so
this module is an ENHANCEMENT layered under it, never a hard dependency.

Offline-first contract:
  * load_catalog() / get_catalog_view() make NO network call and never raise.
  * installed-vs-catalog dedup is decided LOCALLY via shutil.which(exec), so the
    store honestly shows Open for a preinstalled app and Install for a catalog
    one with the internet OFF.
  * a missing/unreadable catalog file degrades to an empty list (the JS fallback
    still paints), never an exception.

Decentralisation / privacy: the curated catalog is local data and the preinstall
set is on the box, so the store works with central + the internet OFF. The
Flathub id (entry 'id') is only an OPTIONAL poster/source accelerant
(app_poster.py), never a gatekeeper, and no personal data leaves the device.
"""

import json
import logging
import os
import shutil
import threading

logger = logging.getLogger('hevolve.app_catalog')

# Required keys every catalog entry must carry to be served.
_REQUIRED_KEYS = ('id', 'name', 'category', 'icon', 'description')

_lock = threading.RLock()
_cache = None            # parsed {'apps': [...], 'categories': [...]} or None
_cache_path_used = None  # the path the cache was loaded from (for diagnostics)


def _candidate_paths():
    """Ordered candidate locations for the canonical catalog JSON.

    1. HART_APP_CATALOG env var (set by hart-apps.nix to the nix-store copy, or
       by a test) — the explicit, deployment-pinned override.
    2. Repo-relative nixos/modules/hart-app-catalog.json (this file lives at
       integrations/agent_engine/app_catalog.py, so repo root is two dirs up) —
       the path that resolves on a HART OS node running from the bundled source.
    """
    env = os.environ.get('HART_APP_CATALOG', '').strip()
    if env:
        yield env
    here = os.path.dirname(os.path.abspath(__file__))
    repo_root = os.path.dirname(os.path.dirname(here))
    yield os.path.join(repo_root, 'nixos', 'modules', 'hart-app-catalog.json')


def _read_catalog_file():
    """Read + validate the catalog from the first readable candidate path.

    Returns (apps_list, categories_list, path_or_None). Degrades to ([], [], None)
    on any error — never raises (the JS fallback still paints offline)."""
    for path in _candidate_paths():
        try:
            if not path or not os.path.isfile(path):
                continue
            with open(path, 'r', encoding='utf-8') as f:
                raw = json.load(f)
        except (OSError, ValueError) as e:
            logger.debug('app catalog read failed at %s: %s', path, e)
            continue
        if not isinstance(raw, dict):
            continue
        apps = raw.get('apps')
        if not isinstance(apps, list):
            continue
        # Keep only well-formed entries (a malformed entry is dropped, never
        # crashes the store). preinstall defaults False; package/exec optional.
        clean = []
        for a in apps:
            if not isinstance(a, dict):
                continue
            if any(not a.get(k) for k in _REQUIRED_KEYS):
                continue
            clean.append({
                'id': str(a['id']),
                'name': str(a['name']),
                'category': str(a['category']),
                'icon': str(a['icon']),
                'description': str(a['description']),
                'preinstall': bool(a.get('preinstall', False)),
                'package': str(a.get('package', '')),
                'exec': str(a.get('exec', '')),
            })
        cats = raw.get('categories')
        if not isinstance(cats, list) or not cats:
            # Derive category order from the entries when not declared.
            cats = []
            for a in clean:
                if a['category'] not in cats:
                    cats.append(a['category'])
        else:
            cats = [str(c) for c in cats]
        return clean, cats, path
    return [], [], None


def load_catalog(force=False):
    """Return the cached list of catalog entries (offline; never raises)."""
    global _cache, _cache_path_used
    with _lock:
        if _cache is not None and not force:
            return _cache['apps']
        apps, cats, path = _read_catalog_file()
        _cache = {'apps': apps, 'categories': cats}
        _cache_path_used = path
        return _cache['apps']


def categories():
    """Canonical category order (declared in the JSON, else derived)."""
    load_catalog()
    with _lock:
        return list(_cache['categories']) if _cache else []


def find(app_id):
    """Return the catalog entry for a Flathub id, or None."""
    if not app_id:
        return None
    for a in load_catalog():
        if a['id'] == app_id:
            return a
    return None


def preinstall_ids():
    """Ids of apps flagged as shipped in the base image."""
    return [a['id'] for a in load_catalog() if a['preinstall']]


def _is_installed(entry, which=shutil.which):
    """LOCAL installed check for one entry (no network): the app is installed if
    its declared exec resolves on PATH. Falls back to the preinstall flag only
    when no exec is declared (so a preinstalled GNOME util still reads Open even
    if its binary name is unusual). ``which`` is injectable for tests."""
    exe = entry.get('exec', '')
    if exe:
        try:
            return which(exe) is not None
        except Exception:
            return False
    return bool(entry.get('preinstall'))


def annotate_installed(apps, which=shutil.which):
    """Return a NEW list of entries each annotated with a fresh ``installed``
    bool decided locally via shutil.which (no network)."""
    out = []
    for a in apps:
        e = dict(a)
        e['installed'] = _is_installed(a, which=which)
        out.append(e)
    return out


def get_catalog_view(query=''):
    """The offline App Store payload: the curated catalog annotated with a
    LOCAL installed flag, optionally filtered by a free-text query, plus the
    canonical category order. No network, no subprocess beyond PATH lookup,
    never raises.

    Shape: {'apps': [...], 'categories': [...], 'count': int, 'query': str,
            'source': path|''}.
    """
    try:
        apps = annotate_installed(load_catalog())
        q = (query or '').strip().lower()
        if q:
            apps = [
                a for a in apps
                if q in a['name'].lower()
                or q in a['description'].lower()
                or q in a['category'].lower()
                or q in a['id'].lower()
            ]
        return {
            'apps': apps,
            'categories': categories(),
            'count': len(apps),
            'query': query or '',
            'source': _cache_path_used or '',
        }
    except Exception as e:  # the store must never 500 on a local feature
        logger.debug('get_catalog_view failed: %s', e)
        return {'apps': [], 'categories': [], 'count': 0,
                'query': query or '', 'source': ''}
