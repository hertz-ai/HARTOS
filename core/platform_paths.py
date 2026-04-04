"""
Cross-platform data directory resolution for Nunba / HARTOS.

Returns the correct data root for each platform:
    Windows:  ~/Documents/Nunba
    macOS:    ~/Library/Application Support/Nunba
    Linux:    ~/.config/nunba
    HARTOS OS (embedded): /var/lib/hartos  (or HARTOS_DATA_DIR env var)

Override with NUNBA_DATA_DIR env var for any custom deployment.

Usage:
    from core.platform_paths import get_data_dir, get_db_path, get_agent_data_dir
    data_root = get_data_dir()          # e.g. ~/Documents/Nunba on Windows
    db_path   = get_db_path()           # .../data/hevolve_database.db
    agent_dir = get_agent_data_dir()    # .../data/agent_data
"""

import os
import sys
import time

_IS_WINDOWS = sys.platform == 'win32'
_IS_MACOS = sys.platform == 'darwin'
_IS_LINUX = sys.platform.startswith('linux')

_cached_data_dir = None


def get_data_dir() -> str:
    """Return the platform-appropriate Nunba data root directory.

    Priority:
        1. NUNBA_DATA_DIR env var (explicit override)
        2. HARTOS_DATA_DIR env var (embedded OS / custom deployment)
        3. Platform default
    """
    global _cached_data_dir
    if _cached_data_dir is not None:
        return _cached_data_dir

    # 1. Explicit override
    override = os.environ.get('NUNBA_DATA_DIR', '').strip()
    if override:
        _cached_data_dir = override
        return _cached_data_dir

    # 2. HARTOS OS deployment override
    hartos_dir = os.environ.get('HARTOS_DATA_DIR', '').strip()
    if hartos_dir:
        _cached_data_dir = hartos_dir
        return _cached_data_dir

    # 3. Detect embedded HARTOS OS (systemd service, no home dir)
    if _IS_LINUX and os.path.isfile('/etc/hartos-release'):
        _cached_data_dir = '/var/lib/hartos'
        return _cached_data_dir

    # 4. Platform defaults
    home = os.path.expanduser('~')
    if _IS_WINDOWS:
        _cached_data_dir = os.path.join(home, 'Documents', 'Nunba')
    elif _IS_MACOS:
        _cached_data_dir = os.path.join(home, 'Library', 'Application Support', 'Nunba')
    else:
        # Linux / other Unix
        xdg = os.environ.get('XDG_DATA_HOME', '').strip()
        if xdg:
            _cached_data_dir = os.path.join(xdg, 'nunba')
        else:
            _cached_data_dir = os.path.join(home, '.config', 'nunba')

    return _cached_data_dir


def get_db_dir() -> str:
    """Return the data/ subdirectory (databases, caches)."""
    return os.path.join(get_data_dir(), 'data')


def get_db_path(filename: str = 'hevolve_database.db') -> str:
    """Return full path to a database file inside data/."""
    return os.path.join(get_db_dir(), filename)


def get_agent_data_dir() -> str:
    """Return the agent_data/ subdirectory."""
    return os.path.join(get_db_dir(), 'agent_data')


def get_prompts_dir() -> str:
    """Return the prompts/ subdirectory."""
    return os.path.join(get_db_dir(), 'prompts')


def get_log_dir() -> str:
    """Return the platform-appropriate log directory."""
    if _IS_WINDOWS:
        return os.path.join(get_data_dir(), 'logs')
    elif _IS_MACOS:
        return os.path.expanduser('~/Library/Logs/Nunba')
    else:
        return os.path.join(get_data_dir(), 'logs')


def get_memory_graph_dir(session_key: str = '') -> str:
    """Return the memory_graph/ subdirectory, optionally with session key."""
    base = os.path.join(get_db_dir(), 'memory_graph')
    if session_key:
        return os.path.join(base, session_key)
    return base


def get_simplemem_dir(session_key: str = '') -> str:
    """Return the simplemem_db/ subdirectory, optionally with session key."""
    base = os.path.join(get_db_dir(), 'simplemem_db')
    if session_key:
        return os.path.join(base, session_key)
    return base


def cleanup_old_logs(max_age_days: int = 7, max_total_mb: int = 50):
    """Delete log files older than max_age_days or when total exceeds max_total_mb.

    Called at startup to prevent unbounded log accumulation.
    Safe: only deletes *.log and *.log.* files in the log directory.
    """
    import glob as _glob
    log_dir = get_log_dir()
    if not os.path.isdir(log_dir):
        return
    now = time.time()
    cutoff = now - (max_age_days * 86400)
    log_patterns = [
        os.path.join(log_dir, '*.log'),
        os.path.join(log_dir, '*.log.*'),
    ]
    all_logs = []
    for pat in log_patterns:
        all_logs.extend(_glob.glob(pat))
    # Sort oldest first
    all_logs.sort(key=lambda f: os.path.getmtime(f) if os.path.exists(f) else 0)

    deleted = 0
    # Phase 1: delete files older than max_age_days
    for f in all_logs:
        try:
            if os.path.getmtime(f) < cutoff:
                os.remove(f)
                deleted += 1
        except OSError:
            pass

    # Phase 2: if still over budget, delete oldest until under limit
    remaining = [f for f in all_logs if os.path.exists(f)]
    total_bytes = sum(os.path.getsize(f) for f in remaining if os.path.exists(f))
    max_bytes = max_total_mb * 1024 * 1024
    for f in remaining:
        if total_bytes <= max_bytes:
            break
        try:
            sz = os.path.getsize(f)
            os.remove(f)
            total_bytes -= sz
            deleted += 1
        except OSError:
            pass

    if deleted:
        import logging
        logging.getLogger('hevolve.platform').info(
            f"Log cleanup: deleted {deleted} old log files from {log_dir}")


def ensure_data_dirs():
    """Create all standard data directories if they don't exist.

    Also runs log cleanup on startup to prevent unbounded log accumulation.
    """
    for d in [get_db_dir(), get_agent_data_dir(), get_prompts_dir(),
              get_log_dir(), get_memory_graph_dir(), get_simplemem_dir()]:
        os.makedirs(d, exist_ok=True)
    # Clean old logs on every startup (safe — worst case is a no-op)
    try:
        cleanup_old_logs(max_age_days=7, max_total_mb=50)
    except Exception:
        pass


def reset_cache():
    """Reset the cached data dir (useful for testing)."""
    global _cached_data_dir
    _cached_data_dir = None
