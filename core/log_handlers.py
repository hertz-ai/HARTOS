"""The ONE rule for giving a process's root logger a console handler when nobody did.

Two launch shapes reach Python's lastResort handler, which prints WARNING and
above only and silently drops every INFO line: the HART OS backend started by
systemd as `python hart_intelligence_entry.py` (fixed in babefb0) and the agent
daemon started as `python -c "... AgentDaemon().run_forever()"` (fixed in
04eed1d). Both fixes were written separately and said "same rule" in their
comments; this module is that rule, written once (review 2026-10-11).

The rule: attach a tagged stdout handler at INFO only when the root logger has
NO handlers. A host that already configured root (Nunba's gui_app.log and
server.log, pytest's capture) is left exactly as found, the 2026-08-03
incident's contract (#489). The tag lets a re-import remove only its own
handler. Pass `handler` to attach an existing, already-formatted handler; or
`formatter`/`stream` to build one.
"""
import logging
import sys
from typing import Optional

HARTOS_HANDLER_TAG = '_hartos_root_handler'

DEFAULT_FORMAT = '%(asctime)s - %(name)s - [%(threadName)s] - %(levelname)s - %(message)s'


def install_console_handler_if_bare(root: Optional[logging.Logger] = None,
                                    handler: Optional[logging.Handler] = None,
                                    formatter: Optional[logging.Formatter] = None,
                                    stream=None,
                                    tag: str = HARTOS_HANDLER_TAG) -> Optional[logging.Handler]:
    """Attach a console handler to `root` iff it has none. Returns it, or None."""
    root = root if root is not None else logging.getLogger()
    if root.handlers:
        return None
    h = handler if handler is not None else logging.StreamHandler(stream or sys.stdout)
    if handler is None:
        h.setFormatter(formatter or logging.Formatter(DEFAULT_FORMAT))
    setattr(h, tag, True)
    root.addHandler(h)
    if root.level == logging.NOTSET or root.level > logging.INFO:
        root.setLevel(logging.INFO)
    return h
