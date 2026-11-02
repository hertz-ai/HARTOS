"""A task left pending on an asyncio loop, for tests of how a loop is ended.

The suite's venv (a 3.12.3 binary under a newer 3.12 Lib, the mismatch task
#100 records) cannot cancel the C-accelerated Task at all: measured
2026-10-28, in pure stdlib and with site disabled (-S), ``task.cancel()`` on
``asyncio.sleep(3600)`` ends the task with
``AttributeError("'coroutine' object has no attribute 'timeout'")`` instead
of cancelled, while the bundled python-embed 3.12.6 and miniconda 3.11
cancel it cleanly.  The pure-Python Task takes the Lib's own cancel path and
is still found by ``asyncio.all_tasks``, so a test built on it measures the
code under test, not that venv's defect.
"""
import asyncio
from asyncio.tasks import _PyTask


def leftover_task(loop):
    """A task that stays pending on ``loop`` until it is cancelled."""
    return _PyTask(asyncio.sleep(3600), loop=loop)
