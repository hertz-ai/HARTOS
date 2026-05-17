"""Shim so journey tests can `from _harness_shim import harness, ...`
without fighting pytest's module discovery.  Re-exports from the
parent agentic_harness module.
"""
import os
import sys as _sys
from pathlib import Path as _Path

_HERE = _Path(__file__).resolve().parent
_PARENT = _HERE.parent   # tests/e2e/
_ROOT = _PARENT.parent.parent  # HARTOS repo root
for _p in (_ROOT, _PARENT):
    sp = str(_p)
    if sp not in _sys.path:
        _sys.path.insert(0, sp)

from agentic_harness import (  # noqa: E402,F401
    AgenticHarness,
    EventRecorder,
    LedgerProbe,
    LLMJudge,
    NFTTimer,
    harness,
    skip_if_missing,
)

__all__ = [
    'AgenticHarness', 'EventRecorder', 'LedgerProbe', 'LLMJudge',
    'NFTTimer', 'harness', 'skip_if_missing',
]
