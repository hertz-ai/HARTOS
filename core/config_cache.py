"""
Cached configuration loader.

Replaces repeated `open("config.json")` calls across helper.py, create_recipe.py,
reuse_recipe.py, and hart_intelligence with a single cached load.

Before: config.json read 3+ times at module import (once per file).
After:  config.json read exactly once, cached in memory.

Configuration Loading Priority (highest to lowest):
1. Environment variables — always checked first by get_secret()
2. Encrypted vault (SecretsManager) — if migrated to encrypted storage
3. config.json (standalone) — developer mode, repo root
4. langchain_config.json (bundled) — Nunba/cx_Freeze, next to executable
5. Empty dict fallback — env-vars-only mode

Deployment mode detection:
- Bundled (Nunba): sys.frozen == True → looks for langchain_config.json next to .exe
- Standalone: looks for config.json in repo root (parent of core/)
- HART OS: /etc/hart/hart.env loaded by systemd, no config.json needed

Note: Nunba's AIKeyVault loads encrypted keys into env vars BEFORE
config_cache runs. get_secret() checks env vars first, so vault keys
always take precedence.

See deploy/deployment-manifest.json for the full deployment mode matrix
including tier definitions, service port assignments, and variant configs.
"""

import json
import os
import logging
import threading

logger = logging.getLogger('hevolve_core')

_config = None
_config_lock = threading.Lock()


def get_config() -> dict:
    """
    Load config.json once and cache it.
    Thread-safe singleton pattern.
    """
    global _config
    if _config is not None:
        return _config

    with _config_lock:
        # Double-check after acquiring lock
        if _config is not None:
            return _config

        # Try encrypted vault first (security module)
        try:
            from security.secrets_manager import SecretsManager
            mgr = SecretsManager()
            # If secrets manager has been migrated, use it
            if mgr._secrets:
                _config = dict(mgr._secrets)
                logger.info("Config loaded from encrypted vault")
                return _config
        except Exception:
            pass

        # Fall back to config.json (standalone) or langchain_config.json (bundled)
        _base_dir = os.path.dirname(os.path.dirname(__file__))
        # In frozen/bundled mode, config lives next to the exe as langchain_config.json
        if getattr(__import__('sys'), 'frozen', False):
            _base_dir = os.path.dirname(__import__('sys').executable)
        _config_candidates = [
            os.path.join(_base_dir, 'config.json'),
            os.path.join(_base_dir, 'langchain_config.json'),
        ]
        for _cp in _config_candidates:
            try:
                with open(_cp, 'r') as f:
                    _config = json.load(f)
                logger.info(f"Config loaded from {os.path.basename(_cp)}")
                return _config
            except FileNotFoundError:
                continue
        logger.warning("config.json not found, using environment variables only")
        _config = {}

        return _config


def get_secret(name: str, default: str = '') -> str:
    """
    Get a configuration value by name.
    Checks environment variable first, then cached config.
    """
    # Env vars take precedence
    env_val = os.environ.get(name)
    if env_val:
        return env_val

    config = get_config()
    return config.get(name, default)


def reload_config():
    """Force reload of configuration (for testing or after migration)."""
    global _config
    with _config_lock:
        _config = None
    return get_config()


# ── Endpoint Resolution ──
# Single source of truth for API URLs.
# In bundled Nunba mode (NUNBA_BUNDLED=1), all DB/action/prompt/vision
# endpoints resolve to the local Flask server (localhost:5000).
# In standalone/cloud mode, they resolve to cloud URLs from config.json.

def _local_base() -> str:
    """Local Nunba server base URL."""
    return f"http://localhost:{os.environ.get('NUNBA_PORT', '5000')}"


def is_bundled() -> bool:
    """True when running inside Nunba (pip-installed, bundled, or frozen)."""
    return bool(os.environ.get('NUNBA_BUNDLED') or getattr(__import__('sys'), 'frozen', False))


def get_db_url() -> str:
    """Database API base URL (replaces hardcoded mailer.hertzai.com).

    Resolves to the LOCAL Nunba backend in bundled mode — every
    db_routes.py endpoint (/create_action, /createpromptlist,
    /getprompt, /getprompt_onlyuserid, /getprompt_all, …) is mounted
    on the same Flask process, so the same DB_URL covers
    write+read+update from inside the bundled app.

    For cross-device CLOUD reads (e.g. /prompts wants to merge in
    agents the user owns on hevolve.ai but never synced down to this
    machine), use get_central_db_url() instead — that one always
    points at the central cloud regardless of bundle mode.
    """
    if is_bundled():
        return _local_base()
    return get_secret('DB_URL', get_config().get('IP_ADDRESS', {}).get('database_url', ''))


# Central cloud DB base URL.  Pinned to the kong-routed central
# instance; does NOT collapse to localhost in bundled mode the way
# get_db_url() does.  Override with HEVOLVE_CENTRAL_DB_URL when
# running against a private Hevolve installation.  Resolves to '' if
# the env override is set to empty, which callers must treat as
# "no central available — skip cross-device merge".
_DEFAULT_CENTRAL_DB_URL = 'https://azurekong.hertzai.com:8443/db'


def get_central_db_url() -> str:
    """Central cloud DB base URL — always the central instance.

    Used by readers that want cross-device data (e.g. /prompts merging
    in agents created on another device, /prompts/public showing
    cloud-only catalogue entries).  Distinct from get_db_url() because
    that one collapses to localhost in bundled mode and would silently
    no-op the merge.

    Default: kong-routed central instance.  Override:
        HEVOLVE_CENTRAL_DB_URL=https://my-private-cloud:8443/db
    Empty override is honored ('' = disable cross-device reads).
    """
    override = os.environ.get('HEVOLVE_CENTRAL_DB_URL')
    if override is not None:
        return override
    return _DEFAULT_CENTRAL_DB_URL


def get_stop_api_url() -> str:
    """VLM computer-use stop endpoint URL.

    Resolves to HARTOS's local /api/vlm/stop in bundled mode (the
    handler ported from OmniParser/omnitool/gradio/agentic_rpc.py
    lives at hart_intelligence_entry.py:vlm_stop).  When the user
    clicks Stop in Nunba's indicator window, this URL is what gets
    POST'd; the handler flips the stop flag in
    integrations.vlm.local_loop._vlm_stop_flags and the next
    iteration of run_local_agentic_loop exits cleanly with
    exit_reason='stopped' before another action runs on the screen.

    Standalone mode falls back to the env-var override (if set) or
    empty string (callers skip the POST when falsy).

    Override:
        HEVOLVE_STOP_API_URL=http://my-cluster:5001/api/vlm/stop
    Empty override is honored ('' = disable).

    Used to live as a hardcoded `http://gcp_training2.hertzai.com:5001/stop`
    literal in Nunba's main.py and app.py — pointed at a cloud
    OmniParser instance that's not part of any current local
    install.  The /stop endpoint ITSELF was never ported when the
    VLM execution loop moved into HARTOS, so the call timed out on
    every shutdown of every install for months.  Now the endpoint
    is in HARTOS and this resolver wires Nunba to it correctly.
    """
    override = os.environ.get('HEVOLVE_STOP_API_URL')
    if override is not None:
        return override
    if is_bundled():
        return f"{_local_base()}/api/vlm/stop"
    return ''


def get_action_api() -> str:
    """Action API URL for create/query actions."""
    if is_bundled():
        return f'{_local_base()}/create_action'
    return get_secret('ACTION_API', get_config().get('IP_ADDRESS', {}).get('database_url', ''))


def get_student_api() -> str:
    """Student profile API URL."""
    if is_bundled():
        return f'{_local_base()}/getstudent_by_user_id'
    return get_secret('STUDENT_API', '')


def get_vision_api() -> str:
    """Vision/image inference API URL."""
    if is_bundled():
        return f'{_local_base()}/upload/vision'
    return get_secret('LLAVA_API', '')


def get_book_parsing_api() -> str:
    """PDF/book parsing API URL."""
    if is_bundled():
        return f'{_local_base()}/upload/parse_pdf'
    return get_secret('BOOKPARSING_API', '')


def get_visual_context_api(user_id, mins=5) -> str:
    """Visual context query URL (recent actions by time window)."""
    base = _local_base() if is_bundled() else os.environ.get('HEVOLVE_MAILER_URL', _local_base())
    return f'{base}/get_visual_bymins?user_id={user_id}&mins={mins}'
