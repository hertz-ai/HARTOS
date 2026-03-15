"""
HART Intelligence — the brain of HARTOS.

Routes messages through LLM agents, tools, and memory to produce responses.
Handles conversation, agent creation, agentic routing, and multi-modal AI.

This is the canonical name. The legacy name 'langchain_gpt_api' is preserved
as a re-export alias for backward compatibility across all deployments.

Usage:
    from hart_intelligence import get_ans, app, get_llm, publish_async
    # OR (legacy, still works):
    from langchain_gpt_api import get_ans, app, get_llm, publish_async
"""

# Re-export everything from the implementation module.
# The actual code stays in langchain_gpt_api.py for now — renaming a 5000+ line
# file that's referenced in Docker, frozen builds, pyproject.toml, and 25+ import
# sites requires a coordinated migration. This alias lets new code use the
# canonical name while old code keeps working.
from langchain_gpt_api import *  # noqa: F401,F403
import langchain_gpt_api as _impl
import sys

# Make `import hart_intelligence` and `from hart_intelligence import X` work
# by ensuring this module mirrors the implementation module's namespace.
for _attr in dir(_impl):
    if not _attr.startswith('__'):
        if not hasattr(sys.modules[__name__], _attr):
            setattr(sys.modules[__name__], _attr, getattr(_impl, _attr))

# Expose the Flask app explicitly
app = _impl.app
