"""
Core infrastructure modules for performance optimization.

Provides:
- config_cache: Cached config loading (eliminates repeated file I/O)
- http_pool: Connection-pooled HTTP sessions
- event_loop: Singleton event loop management
- session_cache: TTL-based session caching for global dicts
- platform: OS platform layer (ServiceRegistry, EventBus, AppRegistry, etc.)
"""

from core.config_cache import get_config, get_secret
from core.http_pool import get_http_session, pooled_get, pooled_post
from core.event_loop import get_or_create_event_loop
from core.session_cache import TTLCache
from core.file_cache import cached_json_load, cached_json_save, invalidate_file_cache
from core.platform_paths import get_data_dir, get_db_path, get_db_dir, get_agent_data_dir
