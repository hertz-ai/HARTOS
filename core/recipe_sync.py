"""
core.recipe_sync - cloud push/pull of recipe-file bundles.

Closes the cross-device gap user hit on 2026-05-04 (Speech Therapy
clicked from Recents -> recipe file missing locally -> silent
fallback to local_assistant).  Existing /createpromptlist syncs
only metadata (name, prompt, image_url); recipe BLOBS ({id}.json
flows + actions, {id}_*_recipe.json distilled steps,
{id}_personality.json) were local-only - so an agent created on
device A never reached device B.

This module pushes the full recipe bundle (ALL {id}*.json files in
PROMPTS_DIR) to a central server when an agent is created or its
recipe is updated, and pulls them back when the user opens an
agent whose recipe is missing locally.

Wire format (single source of truth - both push and pull use the
same envelope):

    POST {CENTRAL_DB_URL}/prompts/sync
    {
      "schema_version": 1,
      "prompt_id":  "<int or str>",
      "user_id":    "<owner>",
      "files":      {"<filename>": "<file contents as string>", ...},
      "checksum":   "<sha256 of canonical files-dict>",
      "uploaded_at": <unix epoch>
    }
    -> 200 OK { "stored": true, "checksum": "..." }

    GET  {CENTRAL_DB_URL}/prompts/sync/{prompt_id}?user_id=...
    -> 200 OK { same envelope as above }
    -> 404    { "error": "not_found" }

The cloud endpoint MAY be served by HARTOS itself (for centrally-
deployed instances) or by an external sync service.  The HARTOS
side ships endpoints in hart_intelligence_entry.py:/prompts/sync.

Best-effort: every push/pull is wrapped in try/except so a sync
failure (offline, central down, schema mismatch) never blocks
the user-facing flow.  All failures log at debug; only the rare
schema-version mismatch warns.
"""

import hashlib
import json
import logging
import os
import re
import time
from typing import Dict, List, Optional

logger = logging.getLogger('hevolve.recipe_sync')

#: Bump when the wire envelope changes shape.  Cloud endpoints
#: REJECT requests with schema_version > known_max, so older clients
#: see a graceful 4xx instead of silently corrupting cloud state.
SCHEMA_VERSION: int = 1

#: Per-prompt last-pushed checksum cache file - skips redundant cloud
#: writes during multi-flow CREATE (each flow triggers a push; if the
#: bundle didn't change we can skip the network roundtrip).  See M7
#: in the post-shipment idempotency review.
_PUSH_CACHE_FILE: str = os.path.join(
    os.path.expanduser('~'), '.hevolve', 'recipe_sync_state.json')

#: Reserved Windows device names that os.open() silently aliases to
#: actual hardware - any incoming filename matching these (with or
#: without extension) must be rejected.  Mirror Windows' own
#: reserved-name list per MS-DOS Device Naming Convention docs.
_WINDOWS_RESERVED_NAMES = frozenset({
    'CON', 'PRN', 'AUX', 'NUL',
    'COM1', 'COM2', 'COM3', 'COM4', 'COM5', 'COM6', 'COM7', 'COM8', 'COM9',
    'LPT1', 'LPT2', 'LPT3', 'LPT4', 'LPT5', 'LPT6', 'LPT7', 'LPT8', 'LPT9',
})


def _safe_filename(fname: str) -> bool:
    """Validate a filename from an untrusted cloud payload.

    Rejects:
      - empty / dotfile / current-or-parent dir
      - any path separator (forward or back) - keeps writes inside the
        target dir even when os.path.join would otherwise strip it
      - drive-letter prefix (Windows: ``C:foo.json``) - os.path.join
        SILENTLY ignores the dir prefix when joined with a drive-relative
        path, which lets attackers anchor writes anywhere on the drive
      - NUL byte - Python open() raises ValueError but we want a clean
        WARNING instead of a stack trace
      - Windows reserved device names (CON, NUL, COM1, ...) - opens
        succeed and alias to actual hardware
      - basename mismatch - os.path.basename(fname) != fname catches
        any composite path that snuck past the simple separator check

    Returns True when safe to write under prompts_dir, False otherwise.
    Caller logs WARNING on False.
    """
    if not fname or fname in {'.', '..'}:
        return False
    if fname.startswith('.'):
        return False
    if '/' in fname or '\\' in fname:
        return False
    if '\x00' in fname:
        return False
    # Drive letter detection: a single letter followed by ':' anywhere
    # in the name is suspicious on Windows.  os.path.basename only
    # strips trailing path components, NOT drive-relative anchors.
    if re.match(r'^[A-Za-z]:', fname):
        return False
    # Windows reserved names.  Apply to the stem (everything before the
    # first dot) since CON.json / CON.txt both alias to CON.
    stem = fname.split('.', 1)[0].upper()
    if stem in _WINDOWS_RESERVED_NAMES:
        return False
    # Belt+suspenders: basename must equal fname.  Catches anything
    # the explicit checks above missed.
    if os.path.basename(fname) != fname:
        return False
    return True


def _load_push_cache() -> dict:
    """Read the per-prompt last-pushed-checksum cache.  Returns {}
    on missing/corrupt - cache is purely an optimization."""
    try:
        with open(_PUSH_CACHE_FILE, 'r', encoding='utf-8') as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}


def _store_push_cache(cache: dict) -> None:
    """Write the cache atomically (temp + os.replace).  Best-effort -
    a write failure just means the next push won't be skipped."""
    try:
        os.makedirs(os.path.dirname(_PUSH_CACHE_FILE), exist_ok=True)
        tmp = _PUSH_CACHE_FILE + '.tmp'
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(cache, f)
        os.replace(tmp, _PUSH_CACHE_FILE)
    except (IOError, OSError) as e:
        logger.debug(f'recipe_sync: push-cache write failed: {e}')


def _files_for_prompt(prompts_dir: str, prompt_id) -> List[str]:
    """List all on-disk filenames for a given prompt_id.

    Matches the canonical naming convention:
      {prompt_id}.json                  config (always present)
      {prompt_id}_personality.json      personality (optional)
      {prompt_id}_{flow}_recipe.json    flow recipe (per flow)
      {prompt_id}_{flow}_{action}.json  per-action recipes

    The leading prefix ``{prompt_id}_`` AND the bare ``{prompt_id}.json``
    are both matched so callers don't have to enumerate suffix
    variations themselves.
    """
    pid_str = str(prompt_id)
    if not os.path.isdir(prompts_dir):
        return []
    out = []
    for fname in os.listdir(prompts_dir):
        if not fname.endswith('.json'):
            continue
        if fname == f'{pid_str}.json' or fname.startswith(f'{pid_str}_'):
            out.append(fname)
    return sorted(out)


def _checksum(files: Dict[str, str]) -> str:
    """Stable sha256 over the canonicalized files dict.  Lets the
    cloud endpoint dedupe identical pushes + lets the client skip
    a re-push when nothing changed since the last sync."""
    canonical = json.dumps(files, sort_keys=True, separators=(',', ':'))
    return hashlib.sha256(canonical.encode('utf-8')).hexdigest()


def build_envelope(prompts_dir: str, prompt_id, user_id: str = '') -> Optional[dict]:
    """Read all {prompt_id}*.json files from disk + wrap in the
    canonical envelope.  Returns None if no files exist (caller
    treats as a no-op push, not an error)."""
    filenames = _files_for_prompt(prompts_dir, prompt_id)
    if not filenames:
        logger.debug(f'recipe_sync: no files for prompt_id={prompt_id} on disk')
        return None
    files: Dict[str, str] = {}
    for fname in filenames:
        path = os.path.join(prompts_dir, fname)
        try:
            with open(path, 'r', encoding='utf-8') as f:
                files[fname] = f.read()
        except (IOError, OSError) as e:
            logger.debug(f'recipe_sync: skipped {fname} ({e})')
    if not files:
        return None
    return {
        'schema_version': SCHEMA_VERSION,
        'prompt_id': str(prompt_id),
        'user_id': user_id or '',
        'files': files,
        'checksum': _checksum(files),
        'uploaded_at': int(time.time()),
    }


def push_recipe(prompts_dir: str, prompt_id, user_id: str = '',
                central_url: str = '', force: bool = False) -> bool:
    """Push all on-disk files for *prompt_id* to the central cloud.

    Returns True on 2xx, False on any failure (offline, schema
    mismatch, file missing).  Best-effort: never raises.

    Surfaces failures at WARNING (not debug) so operators can see
    when cross-device sync is silently broken - this whole module
    exists because of a silent-fallback bug; debug-level here would
    repeat the anti-pattern at the other end of the wire.

    Skips the network roundtrip when the bundle's checksum matches
    the cache from the last successful push (M7 in the post-shipment
    review).  Pass ``force=True`` to override - useful when the
    cache itself is suspect or the cloud rejected the prior push
    for a transient reason.
    """
    if not central_url:
        try:
            from core.config_cache import get_central_db_url
            central_url = get_central_db_url()
        except ImportError:
            central_url = ''
    if not central_url:
        logger.debug('recipe_sync: no central_url available, skip push')
        return False

    envelope = build_envelope(prompts_dir, prompt_id, user_id)
    if envelope is None:
        return False

    # M7: skip when content unchanged since last successful push.
    cache = _load_push_cache()
    cache_key = str(prompt_id)
    if not force and cache.get(cache_key, {}).get('checksum') == envelope['checksum']:
        logger.debug(
            f'recipe_sync: prompt_id={prompt_id} unchanged since last '
            f'push (checksum={envelope["checksum"][:12]}) - skipping')
        return True

    try:
        from core.http_pool import pooled_post
        url = f"{central_url.rstrip('/')}/prompts/sync"
        resp = pooled_post(url, json=envelope, timeout=(3, 10))
        if 200 <= resp.status_code < 300:
            logger.info(
                f'recipe_sync: pushed prompt_id={prompt_id} '
                f'({len(envelope["files"])} files, '
                f'checksum={envelope["checksum"][:12]})')
            cache[cache_key] = {
                'checksum': envelope['checksum'],
                'pushed_at': envelope['uploaded_at'],
            }
            _store_push_cache(cache)
            return True
        # M1: surface as WARNING - cross-device sync silently broken
        # is exactly the anti-pattern this module exists to fix.
        logger.warning(
            f'recipe_sync: push prompt_id={prompt_id} returned '
            f'status={resp.status_code} - cross-device sync deferred '
            f'until next successful push (next CREATE event or manual)')
        return False
    except Exception as e:
        logger.warning(
            f'recipe_sync: push prompt_id={prompt_id} failed: {e} - '
            f'cross-device sync deferred')
        return False


def pull_recipe(prompts_dir: str, prompt_id, user_id: str = '',
                central_url: str = '') -> bool:
    """Pull recipe bundle for *prompt_id* from cloud + write to disk.

    Returns True if at least one file was written, False otherwise
    (not on cloud, offline, write failed, schema mismatch).

    Files are written under their original names in *prompts_dir*.
    Existing files are NOT overwritten unless the cloud envelope's
    checksum differs from the local checksum (last-write-wins by
    upload_at would be racier; checksum-equality keeps idempotent).
    """
    if not central_url:
        try:
            from core.config_cache import get_central_db_url
            central_url = get_central_db_url()
        except ImportError:
            central_url = ''
    if not central_url:
        logger.debug('recipe_sync: no central_url available, skip pull')
        return False

    try:
        from core.http_pool import get_http_session
        params = {'user_id': user_id} if user_id else {}
        url = f"{central_url.rstrip('/')}/prompts/sync/{prompt_id}"
        resp = get_http_session().get(url, params=params, timeout=(3, 10))
        if resp.status_code == 404:
            logger.debug(f'recipe_sync: prompt_id={prompt_id} not on cloud')
            return False
        if not (200 <= resp.status_code < 300):
            logger.debug(
                f'recipe_sync: pull prompt_id={prompt_id} status='
                f'{resp.status_code}')
            return False
        envelope = resp.json()
    except Exception as e:
        logger.debug(f'recipe_sync: pull prompt_id={prompt_id} failed: {e}')
        return False

    if envelope.get('schema_version') != SCHEMA_VERSION:
        logger.warning(
            f'recipe_sync: schema mismatch for prompt_id={prompt_id} '
            f'(cloud={envelope.get("schema_version")}, '
            f'local={SCHEMA_VERSION}) - skipping pull')
        return False

    files = envelope.get('files') or {}
    if not files:
        return False

    # Skip pull when local + cloud checksums match - common case
    # after a push; saves a writable-disk roundtrip.
    local_envelope = build_envelope(prompts_dir, prompt_id, user_id)
    if local_envelope and local_envelope['checksum'] == envelope.get('checksum'):
        logger.debug(
            f'recipe_sync: prompt_id={prompt_id} checksum matches local, '
            f'skipping write')
        return True

    try:
        os.makedirs(prompts_dir, exist_ok=True)
    except OSError as e:
        logger.debug(f'recipe_sync: cannot create {prompts_dir}: {e}')
        return False

    written = 0
    for fname, content in files.items():
        # Defensive: refuse paths that escape the prompts dir, drive
        # letters, NUL bytes, Windows reserved names.  See _safe_filename
        # docstring + reviewer M2 for the full hardening rationale.
        if not _safe_filename(fname):
            logger.warning(
                f'recipe_sync: refusing unsafe filename {fname!r} '
                f'in pull payload for prompt_id={prompt_id}')
            continue
        path = os.path.join(prompts_dir, fname)
        # Atomic write (M3): write to a temp file in the same dir,
        # then os.replace to the final name.  Prevents partial-content
        # snapshots from concurrent prompts_backup.snapshot_prompts
        # picking up a half-written {pid}.json.
        tmp = path + '.tmp'
        try:
            with open(tmp, 'w', encoding='utf-8') as f:
                f.write(content)
            os.replace(tmp, path)
            written += 1
        except (IOError, OSError) as e:
            logger.debug(f'recipe_sync: write {fname} failed: {e}')
            # Clean up orphan temp on failure.
            try:
                os.remove(tmp)
            except OSError:
                pass
    if written:
        logger.info(
            f'recipe_sync: pulled prompt_id={prompt_id} '
            f'({written}/{len(files)} files written)')
        return True
    return False
