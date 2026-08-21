"""
Core infrastructure modules for performance optimization.

Provides:
- config_cache: Cached config loading (eliminates repeated file I/O)
- http_pool: Connection-pooled HTTP sessions
- event_loop: Singleton event loop management
- session_cache: TTL-based session caching for global dicts
- platform: OS platform layer (ServiceRegistry, EventBus, AppRegistry, etc.)
"""

# Make the vendored `agent_ledger` importable in EVERY execution mode.
#
# agent_ledger is first-party HARTOS code living in-tree at
# agent-ledger-opensource/agent_ledger, and it is an essential agent building
# block (task memory, delegation, the Smart Ledger) rather than an optional
# extra.  pyproject.toml already declares that directory as a package root
# TWICE — `[tool.setuptools.packages.find] where = [".", "agent-ledger-opensource"]`
# for the installed wheel, and `pythonpath = [".", "agent-ledger-opensource"]`
# for pytest.  Neither covers the third mode: plain `python <script>.py` from a
# source checkout, which is how ad-hoc runs and the desktop app's in-process
# imports happen.  There, the 13+ `from agent_ledger import ...` call sites
# raise ImportError.
#
# This makes the runtime honour the same single declaration, so the package
# resolves from ONE canonical in-tree location in all three modes instead of
# two of three.  An installed wheel or frozen bundle already has it on the
# path and wins — find_spec short-circuits before we touch sys.path.
def _ensure_vendored_agent_ledger() -> None:
    import importlib.util
    import os
    import sys
    try:
        if importlib.util.find_spec('agent_ledger') is not None:
            return          # installed / frozen — leave resolution alone
    except (ImportError, ValueError):
        pass                # treat an unresolvable spec as "not importable"
    _repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    _vendored = os.path.join(_repo_root, 'agent-ledger-opensource')
    if os.path.isdir(_vendored) and _vendored not in sys.path:
        sys.path.insert(0, _vendored)


_ensure_vendored_agent_ledger()

# Install transformers `_LazyModule.__getattr__` recursion guard FIRST.
# Any `from core.X import Y` path triggers this package init, so the guard
# is in place before any submodule pulls in langchain/transformers.  Mirrors
# the install at hart_intelligence_entry.py:80-209 (commit a6d2dca) so
# entry paths that don't go through HIE are still protected.  Idempotent.
from core import _transformers_lazy_guard  # noqa: F401

from core.config_cache import get_config, get_secret
from core.http_pool import get_http_session, pooled_get, pooled_post
from core.event_loop import get_or_create_event_loop
from core.session_cache import TTLCache
from core.file_cache import cached_json_load, cached_json_save, invalidate_file_cache
from core.platform_paths import get_data_dir, get_db_path, get_db_dir, get_agent_data_dir
