"""Browser Research — single canonical audit log.

Every tool invocation appends one JSON-line record.  No parallel logging path.

Path: <log_dir>/web_research_audit.log (via core.platform_paths.get_log_dir).
Append-only.  Size cap with rotation handled by the existing log rotator
(core.log_rotation) — this module just writes lines.
"""
import json
import logging
import os
import threading
import time
from typing import Any, Optional

logger = logging.getLogger('browser_research.audit')

_LOG_NAME = 'web_research_audit.log'
_LOG_LOCK = threading.Lock()


def _log_path() -> str:
    """Resolve audit log path lazily through canonical platform_paths.

    Lazy because get_log_dir() may not be importable at module-import time
    (cx_Freeze ordering).  Caller falls back to cwd if resolution fails.
    """
    try:
        from core.platform_paths import get_log_dir
        return os.path.join(get_log_dir(), _LOG_NAME)
    except Exception as exc:
        logger.debug('platform_paths unavailable, falling back to cwd: %s', exc)
        return os.path.join(os.getcwd(), _LOG_NAME)


def append(
    user_id: str,
    tool: str,
    platform: str,
    connection_mechanism: str,
    success: bool,
    dry_run: bool = False,
    details: Optional[dict] = None,
    error: Optional[str] = None,
) -> None:
    """Append one audit record.

    Connection mechanism values:
      "obscura_b2_cdp_user_chrome"   — attached to user's running Chrome
      "obscura_b1_headless_profile"  — our managed profile
      "public_http"                  — T3 plain fetch, no auth, no browser
      "ytdlp"                        — T3 yt-dlp
      "channel_websocket"            — T1 adapter (rare — this module mostly logs T2/T3)
    """
    record: dict[str, Any] = {
        'ts': time.time(),
        'user_id': user_id,
        'tool': tool,
        'platform': platform,
        'connection_mechanism': connection_mechanism,
        'success': bool(success),
        'dry_run': bool(dry_run),
    }
    if details:
        record['details'] = details
    if error:
        record['error'] = str(error)[:512]

    line = json.dumps(record, ensure_ascii=False, default=str)
    path = _log_path()
    try:
        with _LOG_LOCK:
            with open(path, 'a', encoding='utf-8') as f:
                f.write(line + '\n')
    except OSError as exc:
        logger.warning('audit append failed: %s', exc)


def read_recent(limit: int = 100) -> list[dict]:
    """Return up to `limit` most recent audit records (newest last).

    Read-only.  Returns [] on any I/O error — never raises.
    """
    path = _log_path()
    if not os.path.isfile(path):
        return []
    try:
        with open(path, encoding='utf-8') as f:
            lines = f.readlines()
    except OSError:
        return []
    records: list[dict] = []
    for line in lines[-limit:]:
        line = line.strip()
        if not line:
            continue
        try:
            records.append(json.loads(line))
        except ValueError:
            continue
    return records
