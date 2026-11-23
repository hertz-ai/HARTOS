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
same envelope).  The base is the node's parent HARTOS and every request
carries ``Authorization: Bearer <the creator's login>``; the caller names
both (integrations.social.sync_engine.recipe_sync_route).  It was
get_central_db_url(), Hevolve_Database behind Kong, which has no
/prompts/sync: measured 2026-11-20, every push from a desktop got 404.

    POST {parent}/prompts/sync
    {
      "schema_version": 1,
      "prompt_id":  "<int or str>",
      "user_id":    "<owner>",
      "files":      {"<filename>": "<file contents as string>", ...},
      "checksum":   "<sha256 of canonical files-dict>",
      "uploaded_at": <unix epoch>
    }
    -> 200 OK { "stored": true, "checksum": "..." }

    GET  {parent}/prompts/sync/{prompt_id}?user_id=...
    -> 200 OK { same envelope as above }
    -> 404    { "error": "not_found" }

The parent serves these from hart_intelligence_entry.py:/prompts/sync,
for the account its gate verified: a pushed bundle is that account's, kept
where the parent's /chat reads agents, and read back only by it.  A bundle
the parent kept before bundles had owners (no owner) is a public agent:
any signed-in account reads it, unless a push of it would be refused today
(belongs_to_prompt, claims_system_agent), and then it answers 404.

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


def belongs_to_prompt(fname: str, prompt_id) -> bool:
    """True when ``fname`` is one of ``prompt_id``'s own files.

    The canonical naming convention:
      {prompt_id}.json                  config (always present)
      {prompt_id}_personality.json      personality (optional)
      {prompt_id}_{flow}_recipe.json    flow recipe (per flow)
      {prompt_id}_{flow}_{action}.json  per-action recipes

    The leading prefix ``{prompt_id}_`` AND the bare ``{prompt_id}.json``
    are both matched so callers don't have to enumerate suffix
    variations themselves.  The one rule for what a bundle may carry: it
    gathers a bundle here, and the parent refuses a pushed bundle holding
    any other file (one agent's bundle must not write another agent).
    """
    pid_str = str(prompt_id)
    if not fname.endswith('.json'):
        return False
    return fname == f'{pid_str}.json' or fname.startswith(f'{pid_str}_')


def _files_for_prompt(prompts_dir: str, prompt_id) -> List[str]:
    """List all on-disk filenames for a given prompt_id (belongs_to_prompt)."""
    if not os.path.isdir(prompts_dir):
        return []
    return sorted(f for f in os.listdir(prompts_dir)
                  if belongs_to_prompt(f, prompt_id))


def _checksum(files: Dict[str, str]) -> str:
    """Stable sha256 over the canonicalized files dict.  Lets the
    cloud endpoint dedupe identical pushes + lets the client skip
    a re-push when nothing changed since the last sync."""
    canonical = json.dumps(files, sort_keys=True, separators=(',', ':'))
    return hashlib.sha256(canonical.encode('utf-8')).hexdigest()


def envelope_checksum(files: Dict[str, str]) -> str:
    """Public checksum helper (same sha256 the envelope carries).

    A2A peer pulls (integrations.google_a2a.peer_reuse) verify wire
    integrity with this before banking a peer's recipe bundle.
    """
    return _checksum(files)


def write_envelope_files(prompts_dir: str, envelope: dict) -> int:
    """Write an envelope's ``files`` dict to *prompts_dir*.

    SINGLE writer for envelope-to-disk: used by the central pull path
    (``pull_recipe`` below) AND by the A2A peer pull
    (``integrations.google_a2a.peer_reuse``), so both banks share ONE
    hardening + atomicity implementation instead of drifting copies.

    Per file: refuse unsafe filenames (``_safe_filename``, WARNING),
    then atomic write (tmp + ``os.replace``) so concurrent readers
    never observe partial content.  Returns files written (0 on total
    failure); never raises.
    """
    files = envelope.get('files') or {}
    if not files:
        return 0
    prompt_id = envelope.get('prompt_id', '?')
    try:
        os.makedirs(prompts_dir, exist_ok=True)
    except OSError as e:
        logger.warning(
            f'recipe_sync: cannot create {prompts_dir} for '
            f'prompt_id={prompt_id}: {e}')
        return 0

    written = 0
    for fname, content in files.items():
        # Defensive: refuse paths that escape the prompts dir, drive
        # letters, NUL bytes, Windows reserved names.  See _safe_filename
        # docstring + reviewer M2 for the full hardening rationale.
        if not _safe_filename(fname):
            logger.warning(
                f'recipe_sync: refusing unsafe filename {fname!r} '
                f'in envelope for prompt_id={prompt_id}')
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
            except OSError as rm_err:
                logger.debug(
                    f'recipe_sync: temp cleanup for {fname} failed: {rm_err}')
    return written


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


def rekey_envelope(envelope: dict, new_prompt_id) -> dict:
    """The same bundle under another prompt id: a new envelope whose files
    carry ``new_prompt_id`` in place of the old one (belongs_to_prompt's
    prefix) and whose agent file names it as its prompt_id.  The input is
    not changed.

    Why: the id a desktop gives an agent (11-13 digits) fits neither
    central's prompt table (int(11)) nor the phone's Integer, so the agent
    is listed on central under the id central gives it, and its bundle
    goes there under that id (measured 2026-11-20: the desktop id appears
    only in the file names and the agent file's own prompt_id).

    The one re-keyer: an A2A pull banks a peer's bundle under the local
    goal's id with it too (integrations.google_a2a.peer_reuse.pull_recipe).
    A numeric id stays a number only when the new id is all digits."""
    old, new = str(envelope.get('prompt_id', '')), str(new_prompt_id)
    if old == new:
        return envelope
    files: Dict[str, str] = {}
    for fname, content in (envelope.get('files') or {}).items():
        renamed = new + fname[len(old):] if belongs_to_prompt(fname, old) else fname
        if fname == f'{old}.json':
            try:
                record = json.loads(content)
                if isinstance(record, dict):
                    record['prompt_id'] = (int(new) if isinstance(
                        record.get('prompt_id'), int) and new.isdigit()
                        else new)
                    content = json.dumps(record)
            except ValueError:
                logger.warning(f'recipe_sync: agent file of {old} is not JSON; '
                               f'sent under {new} unchanged')
        files[renamed] = content
    return dict(envelope, prompt_id=new, files=files, checksum=_checksum(files))


def prompt_owner_email(prompt_id) -> Optional[str]:
    """The email of the account central's prompt list names as the owner of
    agent ``prompt_id``, lower-cased, or None when it cannot say.

    Central's prompt list is the account store (Hevolve_Database, at
    core.config_cache.get_central_db_url()): GET /getprompt/?prompt_id= gives
    the row and its owner's account id, and the account's email comes from
    GET /get_user/<id> through core.profile_sync.fetch_central_profile, the
    one reader of that route.  A Kong login proves an email and nothing else,
    so the email is what a pushing account is matched on.  None for no row,
    no owner, no email, or a store that cannot be reached: a caller treats
    None as "not shown to be theirs"."""
    row = central_prompt_row(prompt_id)
    owner = row.get('user_id') if isinstance(row, dict) else None
    if owner in (None, ''):
        return None
    from core.profile_sync import fetch_central_profile
    profile = fetch_central_profile(owner) or {}
    email = str(profile.get('email_address') or '').strip().lower()
    return email or None


def central_prompt_row(prompt_id, bearer: str = '') -> Optional[dict]:
    """Central's prompt-list row for agent ``prompt_id`` (Hevolve_Database
    GET /getprompt/?prompt_id= at core.config_cache.get_central_db_url():
    its owner's ``user_id``, ``name``, ``prompt`` (the goal), ...), or None
    for no row, an id that is no number, or a store that cannot be reached.

    The one reader of that route: prompt_owner_email takes the owner from
    it; integrations.social.sync_engine.sync_agent_to_parent asks whether
    central already numbers an agent for its account; a CREATE's first turn
    (hart_intelligence_entry chat()) takes central's name and goal from it.
    ``bearer`` is a login central_login_for gave for that URL, else ''."""
    try:
        pid = int(str(prompt_id))
    except (TypeError, ValueError):
        return None
    from core.config_cache import get_central_db_url
    base = (get_central_db_url() or '').rstrip('/')
    if not base:
        return None
    try:
        from core import http_pool
        resp = http_pool.pooled_get(f'{base}/getprompt/',
                                    params={'prompt_id': pid}, timeout=10,
                                    headers=_login_headers(bearer))
        rows = resp.json() if resp.status_code == 200 else None
    except Exception as e:
        logger.warning(f'recipe_sync: central prompt {pid} could not be read '
                       f'({type(e).__name__})')
        return None
    row = rows[0] if isinstance(rows, list) and rows else None
    return row if isinstance(row, dict) else None


def claims_system_agent(files: Dict[str, str]) -> bool:
    """True when a bundle's file marks its agent a system agent.  /chat
    gives a system agent's prompt the system seat for every user of it
    (hart_intelligence_entry, is_system_agent), so a pushed agent may never
    claim it (review rv_e97b of 45c957c93)."""
    for content in (files or {}).values():
        try:
            data = json.loads(content) if isinstance(content, str) else content
        except ValueError:
            continue
        if isinstance(data, dict) and data.get('is_system_agent'):
            return True
    return False


def _login_headers(bearer: str) -> dict:
    """The creator's login on a sync request; none when the node holds none
    (the parent then refuses, which push/pull report like any failure)."""
    return {'Authorization': f'Bearer {bearer}'} if bearer else {}


def push_recipe(prompts_dir: str, prompt_id, user_id: str = '',
                central_url: str = '', force: bool = False,
                bearer: str = '', as_prompt_id=None) -> bool:
    """Push all on-disk files for *prompt_id* to the parent HARTOS
    ``central_url`` as the person whose login ``bearer`` is, under
    ``as_prompt_id`` when the parent knows the agent by another id
    (rekey_envelope).

    Returns True on 2xx, False on any failure (offline, no parent named,
    schema mismatch, file missing, login refused).  Best-effort: never
    raises.

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
        logger.debug('recipe_sync: no parent named, skip push')
        return False

    envelope = build_envelope(prompts_dir, prompt_id, user_id)
    if envelope is None:
        return False
    # The files as they are on this node, before any rekey: what
    # settled_since_last_push compares the next send against.
    source_checksum = envelope['checksum']
    if as_prompt_id not in (None, ''):
        envelope = rekey_envelope(envelope, as_prompt_id)

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
        resp = pooled_post(url, json=envelope, timeout=(3, 10),
                           headers=_login_headers(bearer))
        if 200 <= resp.status_code < 300:
            logger.info(
                f'recipe_sync: pushed prompt_id={prompt_id} '
                f'({len(envelope["files"])} files, '
                f'checksum={envelope["checksum"][:12]})')
            cache[cache_key] = {
                'checksum': envelope['checksum'],
                'source_checksum': source_checksum,
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


def settled_since_last_push(prompts_dir: str, prompt_id) -> bool:
    """True when *prompt_id*'s files on this node are the bundle its last
    stored push sent, and that send was settled on the parent (note_settled):
    there is nothing to send again.  False when the cache cannot say."""
    entry = _load_push_cache().get(str(prompt_id)) or {}
    if not (entry.get('source_checksum') and entry.get('settled')):
        return False
    envelope = build_envelope(prompts_dir, prompt_id)
    return bool(envelope) and envelope['checksum'] == entry['source_checksum']


def note_settled(prompt_id) -> None:
    """Record that *prompt_id*'s last stored push was also settled on the
    parent (integrations.social.sync_engine.sync_agent_to_parent: listed
    and out of Creation Mode).  The next stored push clears it."""
    cache = _load_push_cache()
    entry = cache.get(str(prompt_id))
    if isinstance(entry, dict):
        entry['settled'] = True
        _store_push_cache(cache)


#: A pull the parent refused DEFINITIVELY (it answered 404, 403 or 410 for
#: that account and agent), remembered in memory for _REFUSED_FOR seconds,
#: keyed (user_id, prompt_id): a turn that would pull it again goes straight
#: on (owner rule 11-23, latency aware for real-time conversations: a refused
#: pull must not cost every turn a round trip).  Network errors, timeouts and
#: any other answer are not remembered; a pull that lands clears the entry.
_REFUSED_FOR = 600
_REFUSED_STATUSES = frozenset({403, 404, 410})
_refused_pulls: Dict[tuple, float] = {}
_monotonic = time.monotonic


def pull_recipe(prompts_dir: str, prompt_id, user_id: str = '',
                central_url: str = '', bearer: str = '') -> bool:
    """Pull recipe bundle for *prompt_id* from the parent HARTOS
    ``central_url``, as the person whose login ``bearer`` is (the parent
    returns a bundle with an owner only to that owner, and one with no owner,
    a public agent, to any signed-in account), and write it to disk.

    Returns True if at least one file was written, False otherwise
    (no parent named, not on the parent, offline, write failed, schema
    mismatch, or the parent refused it in the last _REFUSED_FOR seconds).

    Files are written under their original names in *prompts_dir*.
    Existing files are NOT overwritten unless the cloud envelope's
    checksum differs from the local checksum (last-write-wins by
    upload_at would be racier; checksum-equality keeps idempotent).
    """
    if not central_url:
        logger.debug('recipe_sync: no parent named, skip pull')
        return False
    refusal = (str(user_id or ''), str(prompt_id))
    refused_at = _refused_pulls.get(refusal)
    if refused_at is not None and _monotonic() - refused_at < _REFUSED_FOR:
        logger.debug(f'recipe_sync: prompt_id={prompt_id} was refused '
                     f'{int(_monotonic() - refused_at)}s ago; not asked again')
        return False

    try:
        from core.http_pool import get_http_session
        params = {'user_id': user_id} if user_id else {}
        url = f"{central_url.rstrip('/')}/prompts/sync/{prompt_id}"
        resp = get_http_session().get(url, params=params, timeout=(3, 10),
                                      headers=_login_headers(bearer))
        if resp.status_code in _REFUSED_STATUSES:
            _refused_pulls[refusal] = _monotonic()
            logger.debug(f'recipe_sync: prompt_id={prompt_id} refused by the '
                         f'parent (status {resp.status_code}); not asked '
                         f'again for {_REFUSED_FOR}s')
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
        _refused_pulls.pop(refusal, None)
        return True

    written = write_envelope_files(prompts_dir, envelope)
    if written:
        logger.info(
            f'recipe_sync: pulled prompt_id={prompt_id} '
            f'({written}/{len(files)} files written)')
        _refused_pulls.pop(refusal, None)
        return True
    return False
