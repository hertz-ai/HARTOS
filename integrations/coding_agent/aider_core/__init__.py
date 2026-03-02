"""
Aider Core — Vendored modules from Aider (https://github.com/Aider-AI/aider).

License: Apache 2.0 (see LICENSE file in this directory)

This package contains cherry-picked modules from the Aider AI pair programming
tool, adapted for deep integration with HARTOS. The following modifications were
made to the original source:

1. All `from aider.X` imports replaced with relative `.X` imports
2. LiteLLM dependency removed — replaced with HARTOS model adapter
3. Aider's InputOutput class replaced with a minimal IO adapter
4. Self-contained: no dependency on the full aider-chat package

Vendored modules:
- repomap.py — Tree-sitter based repository understanding (PageRank on code graph)
- coders/search_replace.py — Flexible text search/replace with multiple strategies
- linter.py — Auto-lint after edits
- prompts.py — System prompt templates
- queries/ — Tree-sitter query files for 30+ languages

Supporting utilities:
- dump.py, special.py, waiting.py, utils.py — Aider utilities

HARTOS-specific:
- io_adapter.py — Minimal IO adapter replacing Aider's InputOutput
- hart_model_adapter.py — HARTOS LLM bridge replacing LiteLLM
"""

__version__ = "0.1.0"
__aider_upstream__ = "https://github.com/Aider-AI/aider"
__aider_license__ = "Apache-2.0"
