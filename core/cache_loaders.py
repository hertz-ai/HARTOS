"""
Cache loaders for restorable TTLCache.

Each loader takes a cache key and returns the value from persistent storage,
or None if not found. These are used as TTLCache loader callbacks to
auto-restore evicted or expired entries from disk/Redis.
"""

import os
import sys
import json
import logging
import threading

logger = logging.getLogger('hevolve_core')

def _resolve_agent_data_dir():
    db_path = os.environ.get('HEVOLVE_DB_PATH', '')
    if db_path and db_path != ':memory:' and os.path.isabs(db_path):
        return os.path.join(os.path.dirname(db_path), 'agent_data')
    # Bundled/frozen mode: use cross-platform data dir (Program Files is read-only)
    if os.environ.get('NUNBA_BUNDLED') or getattr(sys, 'frozen', False):
        try:
            from core.platform_paths import get_agent_data_dir
            return get_agent_data_dir()
        except ImportError:
            return os.path.join(
                os.path.expanduser('~'), 'Documents', 'Nunba', 'data', 'agent_data')
    return os.path.join(os.path.dirname(os.path.dirname(__file__)), 'agent_data')

AGENT_DATA_DIR = _resolve_agent_data_dir()

def _resolve_prompts_dir():
    """Single source — delegates to the canonical deployment-aware resolver so the
    recipe REUSE read dir (reuse_recipe.load_recipe → here) always matches the
    SAVE dir (helper.PROMPTS_DIR) and the daemon reuse-CHECK, in every mode.  See
    core.platform_paths.get_recipe_prompts_dir (bundled → user data dir; Docker &
    dev → code-relative /app/prompts or <repo>/prompts; no extra env)."""
    try:
        from core.platform_paths import get_recipe_prompts_dir
        return get_recipe_prompts_dir()
    except Exception:
        # Conservative fallback mirroring get_recipe_prompts_dir's rule.
        if os.environ.get('NUNBA_BUNDLED') or getattr(sys, 'frozen', False):
            return os.path.join(
                os.path.expanduser('~'), 'Documents', 'Nunba', 'data', 'prompts')
        return os.path.join(os.path.dirname(os.path.dirname(__file__)), 'prompts')

PROMPTS_DIR = _resolve_prompts_dir()


def load_agent_data(prompt_id):
    """Load agent_data from disk. Key is prompt_id (int or str)."""
    # Sanitize to prevent path traversal
    safe_id = str(prompt_id)
    if not safe_id.replace('_', '').replace('-', '').isalnum():
        return None
    file_path = os.path.join(AGENT_DATA_DIR, f"{safe_id}_agent_data.json")
    if not os.path.exists(file_path):
        return None

    try:
        # Try encrypted load first
        try:
            from security.crypto import decrypt_json_file
            loaded_data = decrypt_json_file(file_path)
            if loaded_data is None:
                return None
        except (ImportError, Exception):
            with open(file_path, 'r', encoding='utf-8') as f:
                loaded_data = json.load(f)

        # Extract actual data (skip metadata wrapper)
        if isinstance(loaded_data, dict) and 'data' in loaded_data:
            logger.info(f"Restored agent_data for prompt_id={prompt_id} from disk")
            return loaded_data['data']
        else:
            logger.info(f"Restored agent_data (old format) for prompt_id={prompt_id} from disk")
            return loaded_data
    except Exception as e:
        logger.debug(f"Failed to load agent_data for {prompt_id}: {e}")
        return None


def load_user_ledger(user_prompt):
    """Load SmartLedger from Redis/JSON. Key is user_prompt (e.g. '123_456')."""
    parts = str(user_prompt).split('_', 1)
    if len(parts) != 2:
        return None

    user_id, prompt_id = parts
    try:
        user_id_int = int(user_id)
        prompt_id_int = int(prompt_id)
    except (ValueError, TypeError):
        return None

    try:
        from helper_ledger import create_ledger_with_auto_backend
        ledger = create_ledger_with_auto_backend(user_id_int, prompt_id_int)
        if ledger and hasattr(ledger, 'tasks') and len(ledger.tasks) > 0:
            # Also restore action_states from this ledger
            try:
                from lifecycle_hooks import restore_action_states_from_ledger
                restored = restore_action_states_from_ledger(user_prompt, ledger)
                if restored > 0:
                    logger.info(f"Restored {restored} action states for {user_prompt}")
            except Exception as e:
                logger.debug(f"Could not restore action_states: {e}")
            logger.info(f"Restored ledger for {user_prompt} with {len(ledger.tasks)} tasks")
            return ledger
    except Exception as e:
        logger.debug(f"Failed to load ledger for {user_prompt}: {e}")

    return None


def load_recipe(user_prompt):
    """Load recipe from disk. Key is user_prompt (e.g. '123_456')."""
    parts = str(user_prompt).split('_', 1)
    if len(parts) != 2:
        return None

    _user_id, prompt_id = parts

    # Try flow 0 first (most common), then scan for any flow
    for flow_id in range(10):
        file_path = os.path.join(PROMPTS_DIR, f"{prompt_id}_{flow_id}_recipe.json")
        if os.path.exists(file_path):
            try:
                with open(file_path, 'r', encoding='utf-8') as f:
                    content = f.read()
                from helper import retrieve_json
                recipe = retrieve_json(content)
                if recipe:
                    logger.info(f"Restored recipe for {user_prompt} from {file_path}")
                    return recipe
            except Exception as e:
                logger.debug(f"Failed to load recipe from {file_path}: {e}")

    return None


# ── Agent config (prompts/{id}.json) — mtime-cached ("cache on set") ─────────
# The agent's DEFINITION (name, personality, goal, creator_user_id), used to
# colour the casual/fast-path identity prompt (agent_identity.build_identity_
# prompt).  Cached for the chat hot path (1.5s budget) but KEYED ON THE FILE'S
# mtime, so the cache refreshes the instant the config is written/edited — every
# "set" (the CREATE save, or an edit via the social / channels-admin APIs) bumps
# the mtime, so the next read reloads fresh.  No writer has to call us, and a
# stale persona can never be served after a creator edits their agent.  A cache
# HIT is one os.stat (microseconds).  prompts/{id}.json is plain JSON, never
# encrypted (cf. create_recipe.py:833, hart_intelligence_entry.py:9174).
_agent_config_cache = {}  # safe_id(str) -> (mtime, config_dict)
_agent_config_lock = threading.Lock()


def load_agent_config(prompt_id):
    """Return the agent's config dict from prompts/{prompt_id}.json, or None.

    mtime-cached + self-refreshing on write (see module note above).  Path-safe
    (same rule as load_agent_data — no traversal).  Returns None for autonomous
    agents that have no prompts/{id}.json, dropping any stale cache entry."""
    if prompt_id is None:
        return None
    safe_id = str(prompt_id)
    if not safe_id.replace('_', '').replace('-', '').isalnum():
        return None
    file_path = os.path.join(PROMPTS_DIR, f"{safe_id}.json")
    try:
        mtime = os.path.getmtime(file_path)
    except OSError:
        with _agent_config_lock:
            _agent_config_cache.pop(safe_id, None)
        return None
    with _agent_config_lock:
        cached = _agent_config_cache.get(safe_id)
        if cached is not None and cached[0] == mtime:
            return cached[1]
    try:
        with open(file_path, 'r', encoding='utf-8') as f:
            config = json.load(f)
    except Exception as e:
        logger.debug(f"Failed to load agent config for {safe_id}: {e}")
        return None
    if not isinstance(config, dict):
        return None
    with _agent_config_lock:
        _agent_config_cache[safe_id] = (mtime, config)
    return config


# Terminal task statuses — duplicated from agent_ledger.core's
# _TERMINAL_TASK_STATUSES because importing core triggers a heavy
# dependency chain at cache-loader-import time (this module is imported
# during TTLCache construction in create_recipe.py).  The set is small
# and stable; sync any addition with agent_ledger.core's copy.
_TERMINAL_LEDGER_STATUSES = frozenset({
    "completed", "failed", "terminated", "user_stopped", "rolled_back",
})


def load_current_flow(user_prompt):
    """Restore the active flow index for ``user_prompt`` from the latest
    non-terminal ledger session — the canonical TTLCache loader pattern
    for ``recipe_for_persona`` (``create_recipe.py:4863``).

    Before this loader existed, ``recipe_for_persona`` was a bare
    ``TTLCache`` with no restoration callback; on Nunba restart (or
    2-hour idle eviction) the cache would miss and
    ``get_current_flow(user_prompt)`` would call
    ``initialise_current_flow_to_zero``, dropping a mid-execution agent
    back to flow 0 and re-running already-completed flows.  See
    ``docs/architecture/TASK_LEDGER_PERSISTENCE_PLAN.md`` §3 Phase 3
    for the rationale; mirrors ``load_user_ledger``'s pattern so the
    cache restoration story is uniform across all per-session caches.

    Resolution:
      1. Parse ``user_prompt = "{user_id}_{prompt_id}"``.
      2. Reuse ``SmartLedger.list_grouped_by_recipe_hierarchy`` (the
         Phase 1 canonical reader) — no parallel directory walk.
      3. Pick the newest session_id for this user (lexicographic
         descending — works for both legacy ``f"{user}_{prompt}"`` and
         new ``f"{user}_{prompt}_{ts_ms}"`` formats because the ms
         suffix collates after the bare form, and even within new
         sessions higher ts_ms sorts later).
      4. Within that session, return the highest flow_id with any
         non-terminal task (active flow); if every flow in that
         session is fully terminal, return the highest flow_id seen
         (the session's final state — keeps follow-up logic monotonic).
      5. If no ledger or no recognisable session exists, return 0 (a
         fresh user_prompt, same as ``initialise_current_flow_to_zero``).

    Returns:
        int flow_id, or None on parse failure (TTLCache treats None as
        a cache miss and falls through to the default-zero path).
    """
    parts = str(user_prompt).split('_', 1)
    if len(parts) != 2:
        return None
    user_id, prompt_id = parts

    try:
        from agent_ledger.core import SmartLedger
        groups = SmartLedger.list_grouped_by_recipe_hierarchy(AGENT_DATA_DIR)
    except Exception as e:
        logger.debug(f"load_current_flow: helper unavailable ({e})")
        return None

    sessions = groups.get(str(prompt_id), {})
    if not sessions:
        return 0

    user_prefix = f"{user_id}_"
    # Newest-first by session_id (works for both legacy and timestamped
    # forms because ts_ms suffix collates lexicographically after the
    # bare form for the same prefix).
    for session_id in sorted(sessions.keys(), reverse=True):
        if not session_id.startswith(user_prefix):
            continue
        flows_in_session = sessions[session_id]
        if not flows_in_session:
            continue
        flow_ids_sorted_desc = sorted(flows_in_session.keys(), reverse=True)
        for fid in flow_ids_sorted_desc:
            tasks = flows_in_session[fid]
            for _aid, task_dict in tasks:
                status = str(task_dict.get('status') or '').lower()
                if status and status not in _TERMINAL_LEDGER_STATUSES:
                    return fid
        # Every flow terminal — return the highest flow_id seen.
        return flow_ids_sorted_desc[0]

    return 0


def load_user_simplemem(user_prompt):
    """Load SimpleMem store. Key is user_prompt (e.g. '123_456')."""
    try:
        from core.platform_paths import get_simplemem_dir
        simplemem_dir = get_simplemem_dir(str(user_prompt))
    except ImportError:
        simplemem_dir = os.path.join('.', 'simplemem_db', str(user_prompt))
    if not os.path.exists(simplemem_dir):
        return None

    try:
        from integrations.channels.memory.simplemem_store import SimpleMemStore, SimpleMemConfig
        config = SimpleMemConfig()
        store = SimpleMemStore(config)
        logger.info(f"Restored SimpleMem store for {user_prompt}")
        return store
    except (ImportError, Exception) as e:
        logger.debug(f"Failed to load SimpleMem for {user_prompt}: {e}")
        return None
