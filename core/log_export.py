"""Non-destructive export of HART OS event logs to a mounted removable disk.

Unlike the USB FLASHER (``scripts/hart_usb_flasher.py``), which writes a raw ISO
to the block device and WIPES it, this COPIES HART's event logs INTO a folder on
an already-mounted filesystem (e.g. ``/run/media/<user>/<label>`` on HART OS), so
every OTHER file on the disk is preserved. Reuses ``core.platform_paths`` for the
canonical log + db dirs — no second log-location source.
"""
import os
import shutil
from typing import Dict, List, Optional

from core.platform_paths import get_log_dir, get_db_dir

EXPORT_DIRNAME = 'HARTOS-logs'

# Immutable-audit-chain candidates under the db dir (whichever the build writes).
_AUDIT_CANDIDATES = (
    'immutable_audit_log.jsonl', 'audit_log.jsonl', 'audit_chain.jsonl',
    'hart_audit.jsonl',
)


def _default_sources() -> List[str]:
    """Every file in the platform log dir + any immutable-audit file present."""
    out: List[str] = []
    log_dir = get_log_dir()
    if os.path.isdir(log_dir):
        for name in sorted(os.listdir(log_dir)):
            p = os.path.join(log_dir, name)
            if os.path.isfile(p):
                out.append(p)
    db_dir = get_db_dir()
    for name in _AUDIT_CANDIDATES:
        p = os.path.join(db_dir, name)
        if os.path.isfile(p):
            out.append(p)
    return out


def export_logs_to_disk(dest_mount: str,
                        sources: Optional[List[str]] = None) -> Dict:
    """Copy HART event logs into ``<dest_mount>/HARTOS-logs/`` NON-destructively.

    NEVER deletes or overwrites anything outside the ``HARTOS-logs`` subfolder, so
    the user's existing files on the removable disk are preserved (the inverse of
    the destructive ISO flasher). One unreadable log never aborts the rest.
    Returns a manifest ``{ok, dest, files, bytes, error}``.
    """
    if not dest_mount or not os.path.isdir(dest_mount):
        return {'ok': False, 'dest': dest_mount, 'files': [], 'bytes': 0,
                'error': 'destination is not a mounted directory'}

    if sources is None:
        sources = _default_sources()

    dest_dir = os.path.join(dest_mount, EXPORT_DIRNAME)
    try:
        os.makedirs(dest_dir, exist_ok=True)
    except OSError as e:
        return {'ok': False, 'dest': dest_mount, 'files': [], 'bytes': 0,
                'error': f'cannot create {EXPORT_DIRNAME}: {e}'}

    copied: List[str] = []
    total = 0
    for src in sources:
        if not os.path.isfile(src):
            continue
        try:
            dst = os.path.join(dest_dir, os.path.basename(src))
            shutil.copy2(src, dst)
            copied.append(os.path.basename(src))
            total += os.path.getsize(dst)
        except OSError:
            continue  # one bad log must not abort the export

    return {'ok': bool(copied), 'dest': dest_dir, 'files': copied, 'bytes': total,
            'error': '' if copied else 'no log files found to export'}
