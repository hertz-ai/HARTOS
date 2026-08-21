"""Redirect stdout/stderr to devnull in frozen builds.

cx_Freeze frozen builds may have stdout/stderr closed or pointing to
invalid file descriptors. This causes crashes when any library tries to
print (LangChain, autogen, etc.). Redirecting to devnull prevents these
crashes while preserving logging (which uses its own handlers).

Single source of truth — imported by create_recipe.py, reuse_recipe.py,
and hart_intelligence_entry.py instead of copy-pasting the guard.
"""
import os
import sys


def silence_stdio():
    """Redirect stdout/stderr to devnull if they're broken (frozen builds)."""
    try:
        if sys.stdout is None or sys.stdout.closed:
            sys.stdout = open(os.devnull, 'w')
    except Exception:
        sys.stdout = open(os.devnull, 'w')

    try:
        if sys.stderr is None or sys.stderr.closed:
            sys.stderr = open(os.devnull, 'w')
    except Exception:
        sys.stderr = open(os.devnull, 'w')


class _SafeIOStream:
    """autogen IOStream that cannot be killed by a closed stdout.

    ``silence_stdio`` above only runs at IMPORT time.  If stdout is closed
    LATER, every autogen ``print`` raises and takes the whole agent turn
    with it — autogen calls ``_print_received_message`` on EVERY message,
    inside ``initiate_chat``, before any reply is produced.

    Live 2026-08-21 in the frozen Nunba build, that is exactly what
    happened: every single chat message came back as "Error getting
    response: I/O operation on closed file", with this traceback::

        create_recipe.py:4141  initiate_chat(..., silent=False)
          autogen/agentchat/conversable_agent.py:809 _print_received_message
          autogen/io/console.py:21                   print(...)
        ValueError: I/O operation on closed file.

    A GUI build has no console for the transcript to go to, so losing a
    printed line costs nothing; losing the user's answer costs everything.
    Fixing it here rather than by threading ``silent=True`` through 25+
    ``initiate_chat`` call sites keeps one behaviour in one place.
    """

    def print(self, *objects, sep: str = ' ', end: str = '\n',
              flush: bool = False) -> None:
        try:
            print(*objects, sep=sep, end=end, flush=flush)
        except (ValueError, OSError):
            # stdout was closed under us. Re-arm it so the NEXT line has
            # somewhere to go, then drop this one.
            try:
                sys.stdout = open(os.devnull, 'w')
            except Exception:
                pass

    def input(self, prompt: str = '', *, password: bool = False) -> str:
        # No console to read from in a windowed build. Returning '' matches
        # what autogen does for an EOF'd stdin and keeps the turn moving
        # instead of raising inside the agent loop.
        return ''


def install_autogen_iostream() -> None:
    """Point autogen's global IOStream at :class:`_SafeIOStream`.

    Safe to call repeatedly, and a no-op when autogen is not importable.
    MUST NOT be called at module import: importing autogen drags
    google.api_core + flaml + torch (~12s), which is the whole reason
    ``autogen`` is behind ``lazy_module``.  It is wired as that proxy's
    ``on_import`` hook so it runs exactly when autogen really loads.
    """
    try:
        from autogen.io.base import IOStream
    except Exception:
        return
    try:
        IOStream.set_global_default(_SafeIOStream())
    except Exception:
        pass
