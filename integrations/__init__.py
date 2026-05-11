"""
Integrations Package

This package contains all external protocol integrations:
- MCP (Model Context Protocol) - Anthropic's protocol for connecting to external data sources
- Internal Agent Communication - In-process skill-based agent delegation
- Google A2A - Google's official Agent2Agent cross-platform communication protocol
"""

# Install transformers `_LazyModule.__getattr__` recursion guard FIRST.
# Nunba's bg_import path enters HARTOS through `integrations.service_tools.
# model_catalog` (via Nunba's `models.catalog` re-export) BEFORE HIE has
# a chance to install its own guard at HIE:80-209.  That path then loads
# `core.labeled_tool` → `from langchain_classic.agents import Tool` →
# transformers lazy `__getattr__` → infinite recursion, splash hangs.
# Importing the guard here gets it in place before any submodule of this
# package can pull in langchain / transformers.  Idempotent — re-import
# is a no-op (sentinel on `_LazyModule._hartos_reentry_guarded`).
from core import _transformers_lazy_guard  # noqa: F401
