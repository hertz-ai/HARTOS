"""When a failure that keeps repeating is worth a log line.

A check that runs every few seconds and keeps failing wrote one line each
time: VisionService's camera ask, every 2 s per socket, about 1,800 lines an
hour (review of 2f0c28a14).  The rule here: the first failure is logged in
full, then one summary line every ``every_s`` seconds saying how many more
there were, until the check works again.

    n = repeated_failure(('camera_wanted', user_id), 300)
    if n == 0:      # the first failure: log it in full
    elif n:         # n failures since the last line: log one summary
    # None: say nothing
    ...
    failure_cleared(('camera_wanted', user_id))   # it worked

The state is one small entry per key that is failing now; a key that works
again is removed.
"""
import threading
import time
from typing import Hashable, Optional

_lock = threading.Lock()
_failing = {}  # key -> [monotonic time of the last line, failures since it]


def repeated_failure(key: Hashable, every_s: float) -> Optional[int]:
    """Count one failure of ``key``.  Returns 0 for the first failure since
    the key last worked (log it in full), the number of failures since the
    last line once ``every_s`` seconds have passed (log one summary), and
    None otherwise (log nothing)."""
    now = time.monotonic()
    with _lock:
        state = _failing.get(key)
        if state is None:
            _failing[key] = [now, 0]
            return 0
        state[1] += 1
        if now - state[0] < every_s:
            return None
        count, state[0], state[1] = state[1], now, 0
        return count


def failure_cleared(key: Hashable) -> None:
    """``key`` worked: its next failure is a first one again."""
    with _lock:
        _failing.pop(key, None)
