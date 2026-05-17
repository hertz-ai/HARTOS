"""
core.prompts_backup - periodic local snapshot of the prompts/ directory.

Closes the "user accidentally wipes data dir / corruption / bad
shutdown" recovery gap.  Cloud sync (core.recipe_sync) handles
cross-device propagation, but it only helps when the central is
reachable AND the user previously pushed.  Local snapshots are the
zero-network fallback: the user's last N agent states sit in a
sibling directory and can be restored in one cp -r.

When called:
  Once per HARTOS boot (best-effort, non-blocking) - cheap because
  the prompts/ dir is typically <10MB even with hundreds of agents.
  No file watcher / continuous backup - the boot snapshot bounds
  data loss to "since last reboot" which matches user mental
  model for most desktop applications.

Where stored:
  ``<data>/prompts_snapshots/YYYYMMDD_HHMMSS/`` - one dir per snapshot,
  each containing a flat copy of all .json files from prompts/ at
  that moment.  See core.platform_paths.get_data_dir for the data
  root resolution.

Retention:
  Keep the latest ``MAX_DAILY_SNAPSHOTS`` (default 7) - one per
  day if HARTOS reboots daily; old snapshots beyond that count
  get pruned automatically on each new snapshot.

Restore:
  Manual for now - user copies files back.  A future feature could
  expose a /prompts/restore endpoint.

Tests in tests/unit/test_prompts_backup.py.
"""

import logging
import os
import shutil
import time
from typing import List, Optional

logger = logging.getLogger('hevolve.prompts_backup')

#: How many recent snapshots to keep.  Override via env.
MAX_DAILY_SNAPSHOTS: int = int(os.environ.get(
    'HEVOLVE_PROMPTS_SNAPSHOT_KEEP', '7'))

#: Subdirectory under data/ where snapshot dirs live.  Sibling of
#: prompts/ so the user can find it without a hunt.
SNAPSHOTS_SUBDIR: str = 'prompts_snapshots'


def _snapshots_root(prompts_dir: str) -> str:
    """Sibling directory of prompts_dir for snapshot storage."""
    return os.path.join(os.path.dirname(prompts_dir), SNAPSHOTS_SUBDIR)


def list_snapshots(prompts_dir: str) -> List[str]:
    """Return existing snapshot directory names, oldest-first.

    Snapshot names are YYYYMMDD_HHMMSS so lexical sort = chronological.
    Returns ``[]`` when the snapshots root doesn't exist.
    """
    root = _snapshots_root(prompts_dir)
    if not os.path.isdir(root):
        return []
    names = []
    for name in os.listdir(root):
        full = os.path.join(root, name)
        if os.path.isdir(full) and name[:1].isdigit():
            names.append(name)
    return sorted(names)


def snapshot_prompts(prompts_dir: str,
                     max_keep: Optional[int] = None) -> Optional[str]:
    """Take one snapshot of prompts_dir.  Returns the snapshot dir
    name on success, None on failure or no-op.

    Best-effort: any IOError / permission issue is logged at debug
    and returns None so the boot path never crashes on a backup
    failure.

    Idempotent within the same second: if a snapshot for the current
    YYYYMMDD_HHMMSS already exists, returns that name without copying
    again.  (Multiple HARTOS boots within one second is rare but the
    guard avoids a partial-overwrite race.)
    """
    if not os.path.isdir(prompts_dir):
        logger.debug(f'snapshot_prompts: source missing: {prompts_dir}')
        return None
    # Don't snapshot empty prompts/.  Avoids creating misleading
    # "everything was wiped" snapshots when the user starts a fresh
    # install before any agent exists.
    try:
        entries = [f for f in os.listdir(prompts_dir) if f.endswith('.json')]
    except OSError as e:
        logger.debug(f'snapshot_prompts: cannot list {prompts_dir}: {e}')
        return None
    if not entries:
        logger.debug('snapshot_prompts: prompts dir empty, skipping')
        return None

    snap_name = time.strftime('%Y%m%d_%H%M%S')
    root = _snapshots_root(prompts_dir)
    snap_path = os.path.join(root, snap_name)
    if os.path.isdir(snap_path):
        logger.debug(f'snapshot_prompts: {snap_name} already exists')
        return snap_name

    try:
        os.makedirs(snap_path, exist_ok=True)
    except OSError as e:
        logger.debug(f'snapshot_prompts: cannot create {snap_path}: {e}')
        return None

    # Belt + suspenders for M3 (post-shipment review): even though
    # the canonical recipe writers were converted to atomic temp+rename,
    # third-party writers / older HARTOS versions / hand-edits could
    # still leave torn JSON.  Validate each file's JSON before copying;
    # skip + log on parse failure so the snapshot only contains valid
    # restorable state.
    import json as _json
    copied = 0
    skipped_corrupt = 0
    for fname in entries:
        src = os.path.join(prompts_dir, fname)
        dst = os.path.join(snap_path, fname)
        try:
            with open(src, 'r', encoding='utf-8') as _src_f:
                _content = _src_f.read()
            try:
                _json.loads(_content)
            except _json.JSONDecodeError as je:
                logger.warning(
                    f'snapshot_prompts: skipping torn/corrupt {fname} '
                    f'({je}) - snapshot stays restore-safe')
                skipped_corrupt += 1
                continue
            shutil.copy2(src, dst)
            copied += 1
        except (IOError, OSError) as e:
            logger.debug(f'snapshot_prompts: copy {fname} failed: {e}')
    if skipped_corrupt:
        logger.warning(
            f'snapshot_prompts: {skipped_corrupt} torn/corrupt files '
            f'skipped in snapshot {snap_name}')
    if copied == 0:
        # Empty snapshot is worse than no snapshot.
        try:
            os.rmdir(snap_path)
        except OSError:
            pass
        return None

    logger.info(
        f'prompts_backup: snapshot {snap_name} saved '
        f'({copied}/{len(entries)} files)')

    # Retention: prune old snapshots beyond max_keep.
    keep = max_keep if max_keep is not None else MAX_DAILY_SNAPSHOTS
    _prune_old_snapshots(prompts_dir, keep=keep)
    return snap_name


def _prune_old_snapshots(prompts_dir: str, keep: int) -> int:
    """Delete snapshots beyond the most-recent *keep*.

    Returns count of pruned snapshots.
    """
    if keep < 1:
        return 0
    snapshots = list_snapshots(prompts_dir)
    excess = len(snapshots) - keep
    if excess <= 0:
        return 0
    pruned = 0
    root = _snapshots_root(prompts_dir)
    for name in snapshots[:excess]:  # oldest first
        full = os.path.join(root, name)
        try:
            shutil.rmtree(full)
            pruned += 1
            logger.info(f'prompts_backup: pruned old snapshot {name}')
        except (IOError, OSError) as e:
            logger.debug(f'prompts_backup: prune {name} failed: {e}')
    return pruned


def snapshot_at_boot() -> Optional[str]:
    """Convenience entry-point for HARTOS boot.  Resolves the
    prompts directory via core.platform_paths and snapshots it.

    Returns the snapshot name on success, None on no-op or failure.
    """
    try:
        from core.platform_paths import get_prompts_dir
        prompts_dir = get_prompts_dir()
    except Exception as e:
        logger.debug(f'snapshot_at_boot: cannot resolve prompts_dir: {e}')
        return None
    return snapshot_prompts(prompts_dir)
