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
    base = os.path.join(os.path.dirname(os.path.dirname(__file__)), 'prompts')
    if os.path.isdir(base):
        return base
    # Bundled mode fallback: cross-platform prompts dir
    if os.environ.get('NUNBA_BUNDLED') or getattr(sys, 'frozen', False):
        try:
            from core.platform_paths import get_prompts_dir
            return get_prompts_dir()
        except ImportError:
            return os.path.join(
                os.path.expanduser('~'), 'Documents', 'Nunba', 'data', 'prompts')
    return base

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
