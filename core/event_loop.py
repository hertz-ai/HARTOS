"""
Singleton event loop management.

Replaces repeated `asyncio.new_event_loop()` calls (7+ across codebase)
with a single reusable event loop.

Before: Each async call creates a new event loop, causing resource leaks.
After:  One event loop per thread, properly managed.
"""

import asyncio
import threading
import logging

logger = logging.getLogger('hevolve_core')

_thread_loops = threading.local()


def get_or_create_event_loop() -> asyncio.AbstractEventLoop:
    """
    Get or create an event loop for the current thread.
    Reuses existing loops instead of creating new ones each time.
    """
    # Check for thread-local loop first
    loop = getattr(_thread_loops, 'loop', None)
    if loop is not None and not loop.is_closed():
        return loop

    # Try to get existing loop
    try:
        loop = asyncio.get_event_loop()
        if not loop.is_closed():
            _thread_loops.loop = loop
            return loop
    except RuntimeError:
        pass

    # Create new loop for this thread
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    _thread_loops.loop = loop
    logger.debug(f"Created new event loop for thread {threading.current_thread().name}")
    return loop


def run_async(coro):
    """
    Run an async coroutine from synchronous code.
    Uses the singleton event loop for the current thread.
    """
    loop = get_or_create_event_loop()
    return loop.run_until_complete(coro)
