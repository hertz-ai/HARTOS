"""
Cached file I/O for recipe and prompt JSON files.

Replaces repeated json.load() calls (64+ in create_recipe.py, 39 in reuse_recipe.py)
with an LRU cache that reduces disk I/O by 90%+ for frequently accessed files.

Cache invalidation: Files are re-read if modified (mtime check).
"""

import json
import os
import threading
import logging
from functools import lru_cache

logger = logging.getLogger('hevolve_core')

_file_cache = {}
_mtime_cache = {}
_cache_lock = threading.Lock()


def cached_json_load(filepath: str) -> dict:
    """
    Load a JSON file with mtime-based cache invalidation.
    Returns a new dict copy each time to prevent mutation of cached data.
    """
    filepath = os.path.abspath(filepath)

    try:
        current_mtime = os.path.getmtime(filepath)
    except OSError:
        # File doesn't exist, clear cache entry if present
        with _cache_lock:
            _file_cache.pop(filepath, None)
            _mtime_cache.pop(filepath, None)
        raise FileNotFoundError(f"File not found: {filepath}")

    with _cache_lock:
        cached_mtime = _mtime_cache.get(filepath)
        if cached_mtime == current_mtime and filepath in _file_cache:
            # Return a copy to prevent mutation of cached data
            return json.loads(json.dumps(_file_cache[filepath]))

    # Cache miss or stale - read from disk
    with open(filepath, 'r') as f:
        data = json.load(f)

    with _cache_lock:
        _file_cache[filepath] = data
        _mtime_cache[filepath] = current_mtime

    # Return a copy
    return json.loads(json.dumps(data))


def invalidate_file_cache(filepath: str = None):
    """
    Invalidate cache for a specific file, or all files if filepath is None.
    Call this after writing to a cached file.
    """
    with _cache_lock:
        if filepath is None:
            _file_cache.clear()
            _mtime_cache.clear()
            logger.debug("File cache fully cleared")
        else:
            filepath = os.path.abspath(filepath)
            _file_cache.pop(filepath, None)
            _mtime_cache.pop(filepath, None)
            logger.debug(f"File cache invalidated for {filepath}")


def atomic_json_write(filepath: str, data, indent: int = 2):
    """Write JSON atomically: write to temp file → os.replace() (atomic on POSIX and Windows).

    Prevents corrupt JSON from crash/power loss during write.

    The temp file has a unique name, so concurrent writers of the same path
    never share one, and it is created with ONE exclusive open.  Not
    tempfile.mkstemp: on Windows mkstemp retries a PermissionError up to
    2**31 times while os.access calls the directory writable; measured live
    2026-09-25 (bff95ab44), a consent Allow spun in it for minutes.  A
    refused directory now fails the write at once.  Only the final move is
    tried again, briefly, while another handle holds the file open
    (_replace_held_file); every JSON save gets that, as the vault does.
    """
    _atomic_replace(filepath,
                    lambda f: json.dump(data, f, indent=indent, default=str),
                    binary=False)


def atomic_bytes_write(filepath: str, data: bytes) -> None:
    """Write ``data`` atomically, as atomic_json_write writes JSON: a reader
    sees the old file or the new one, never a part.  A failure (a full disk)
    leaves the old file as it was and no temp file behind, and raises.  For
    a file whose bytes are already made: the encrypted vault
    (security.secrets_manager)."""
    _atomic_replace(filepath, lambda f: f.write(data), binary=True)


#: How often the final os.replace is tried while another handle holds the
#: target open, and the pause between tries: 0.3 s in all, past a 0.25 s
#: holder (a save itself takes about 12 ms, so a 0.1 s holder outlasts
#: several; measured on the demo machine, review rv4a1).  On Windows
#: the move is refused (PermissionError, WinError 5 or 32) for as long as
#: such a handle is open, because CPython and most scanners open files
#: without FILE_SHARE_DELETE; an in-place write needed no delete access and
#: passed, at the cost of a torn file on a crash.  The holders seen are
#: brief: another process reading the file once at its start
#: (SecretsManager._load_vault), an antivirus or indexer scan.  A holder
#: that stays longer, or a real denial, still fails the write after these
#: tries.
_REPLACE_TRIES = 6
_REPLACE_PAUSE_S = 0.06


def _replace_held_file(src: str, dst: str) -> None:
    """os.replace(src, dst), tried again on PermissionError up to
    _REPLACE_TRIES times with _REPLACE_PAUSE_S between tries; the last
    PermissionError is raised.  One WARNING when a try after the first
    succeeded.  Nothing else is tried again: the temp file was made with
    one exclusive open, so a refused directory has already failed."""
    import time
    for attempt in range(1, _REPLACE_TRIES + 1):
        try:
            os.replace(src, dst)
        except PermissionError:
            if attempt == _REPLACE_TRIES:
                raise
            time.sleep(_REPLACE_PAUSE_S)
            continue
        if attempt > 1:
            logger.warning("%s was held open by another handle; replaced on "
                           "try %d of %d", dst, attempt, _REPLACE_TRIES)
        return


def _atomic_replace(filepath: str, fill, binary: bool) -> None:
    """The one atomic write behind atomic_json_write and atomic_bytes_write:
    ``fill(f)`` into a temp file beside ``filepath`` (unique name, ONE
    exclusive open, 0600), flush, fsync, then os.replace, tried again for a
    moment while another handle holds ``filepath`` open
    (_replace_held_file).  On any failure the temp file is removed and
    ``filepath`` is as it was."""
    import uuid
    filepath = os.path.abspath(filepath)
    dir_path = os.path.dirname(filepath)
    os.makedirs(dir_path, exist_ok=True)

    tmp_path = os.path.join(
        dir_path, f'.{os.path.basename(filepath)}.{uuid.uuid4().hex}.tmp')
    tmp_fd = os.open(tmp_path,
                     os.O_WRONLY | os.O_CREAT | os.O_EXCL
                     | getattr(os, 'O_BINARY', 0)
                     | getattr(os, 'O_NOINHERIT', 0), 0o600)
    try:
        with (os.fdopen(tmp_fd, 'wb') if binary
              else os.fdopen(tmp_fd, 'w', encoding='utf-8')) as f:
            fill(f)
            f.flush()
            os.fsync(f.fileno())
        _replace_held_file(tmp_path, filepath)  # Atomic
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def migration_marker_path(new_path: str) -> str:
    """Where adopt_legacy_json_once records that ``new_path`` took over:
    ``<dir>/<stem>.migrated.json`` beside it."""
    stem = os.path.splitext(os.path.basename(new_path))[0]
    return os.path.join(os.path.dirname(os.path.abspath(new_path)),
                        f'{stem}.migrated.json')


def adopt_legacy_json_once(new_path: str, legacy_path: str, *, what: str,
                           accept=None, indent=2) -> str:
    """Carry a JSON file from where it used to live to ``new_path``, once.

    Non-destructive: the old file is never changed or deleted (a rollback or
    another install may still read it), a file already at ``new_path`` is
    never overwritten, and an old file that does not parse, or that
    ``accept(data)`` refuses, is left behind, not copied.  A failure is
    logged, never raised.

    Once means once: when ``new_path`` is in use (just copied, or found
    already there) a marker (migration_marker_path) records it, and nothing
    is copied again while the marker exists.  Deleting ``new_path`` is how an
    owner resets it; without the marker the next start copied the old file
    back (admin channels with their bot tokens, 4b1796862's review).

    Returns what happened: 'same' (one path), 'done' (marked before),
    'none' (no old file), 'marked' (new file already there), 'copied',
    'refused' (old file unusable; not marked, so a fixed one still moves),
    or 'failed'.
    """
    new = os.path.abspath(new_path)
    legacy = os.path.abspath(legacy_path)
    marker = migration_marker_path(new)
    try:
        if os.path.normcase(new) == os.path.normcase(legacy):
            return 'same'
        if os.path.exists(marker):
            return 'done'
        if not os.path.isfile(legacy):
            return 'none'
        if os.path.exists(new):
            _mark_migrated(marker, legacy, copied=False, what=what)
            return 'marked'
        try:
            with open(legacy, encoding='utf-8') as f:
                data = json.load(f)
        except ValueError as e:
            logger.warning("%s at %s is not JSON; not carried over to %s: %s",
                           what, legacy, new, e)
            return 'refused'
        if accept is not None and not accept(data):
            logger.warning("%s at %s is not usable; not carried over to %s",
                           what, legacy, new)
            return 'refused'
        atomic_json_write(new, data, indent=indent)
        _mark_migrated(marker, legacy, copied=True, what=what)
        logger.info("Copied %s from %s to %s", what, legacy, new)
        return 'copied'
    except Exception as e:
        logger.warning("%s at %s not carried over to %s: %s",
                       what, legacy, new, e)
        return 'failed'


def _mark_migrated(marker: str, legacy: str, *, copied: bool, what: str):
    import time
    try:
        atomic_json_write(marker, {
            'from': legacy, 'copied': copied,
            'at': time.strftime('%Y-%m-%d %H:%M:%S'),
        }, indent=2)
    except Exception as e:
        logger.warning("%s move not marked done at %s (%s); a deleted file "
                       "could be copied again", what, marker, e)


def cached_json_save(filepath: str, data: dict, indent: int = 4):
    """
    Save JSON data atomically and update the cache.
    Uses write-to-temp + os.replace() to prevent corruption.
    """
    filepath = os.path.abspath(filepath)

    atomic_json_write(filepath, data, indent=indent)

    # Update cache with the data we just wrote
    with _cache_lock:
        _file_cache[filepath] = json.loads(json.dumps(data))
        try:
            _mtime_cache[filepath] = os.path.getmtime(filepath)
        except OSError:
            pass


def cache_stats() -> dict:
    """Return cache statistics for monitoring."""
    with _cache_lock:
        return {
            'cached_files': len(_file_cache),
            'total_size_bytes': sum(
                len(json.dumps(v).encode()) for v in _file_cache.values()
            ),
        }
