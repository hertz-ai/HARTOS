"""Operator one-shot — revive the agent_daemon when it's gone silent.

Symptom this script addresses (seen 2026-05-27):

    $ mcp__hartos__agent_status
    {
      "daemon_enabled": false,
      "daemon_thread_alive": false,
      "daemon_tick_count": 0,
      "goals": {"total": 142, "by_status": {"active": 84, ...}}
    }

84 active goals, ZERO ticks — the daemon worker thread isn't running so
queued marketing / content_gen / revenue goals never dispatch.  The
`integrations.agent_engine.__init__._start_daemon_with_self_heal`
supervisor thread is supposed to keep this alive, but it can be killed
by an unhandled exception in the loop or never started if Phase-2 init
silently failed.

What this script does:

  1. Imports the live agent_daemon singleton in-process.
  2. Clears the `_running` zombie flag if the previous thread died
     without resetting it.
  3. Calls `agent_daemon.start()` to spawn a fresh worker thread.
  4. Polls `_tick_count` for ~60 seconds and prints each tick so the
     operator sees real motion instead of staring at agent_status.

Run AGAINST the live HARTOS process via the same Python environment:

    cd C:\\Users\\sathi\\PycharmProjects\\HARTOS
    python scripts/kick_agent_daemon.py

If running from the embedded Python inside Nunba.exe, use:

    & "C:\\Users\\sathi\\AppData\\Local\\Programs\\Nunba\\python-embed\\python.exe" `
      "C:\\Users\\sathi\\PycharmProjects\\HARTOS\\scripts\\kick_agent_daemon.py"

This script does NOT restart Nunba.exe — it just wakes the in-process
agent_daemon thread.  If even that fails (Phase-2 init never ran),
restart Nunba and check the logs for "Agent daemon supervisor thread
spawned" near startup.
"""
from __future__ import annotations

import sys
import time


def kick(poll_seconds: int = 60) -> int:
    """Return 0 on success, non-zero on failure."""
    try:
        from integrations.agent_engine.agent_daemon import agent_daemon
    except Exception as exc:  # pragma: no cover — extreme degraded boot
        print(f"FATAL: cannot import agent_daemon: {exc}", file=sys.stderr)
        return 2

    pre_tick = getattr(agent_daemon, '_tick_count', 0)
    pre_alive = bool(getattr(agent_daemon, '_thread', None) and
                     agent_daemon._thread.is_alive())
    print(f"BEFORE  _running={agent_daemon._running} "
          f"_thread_alive={pre_alive} _tick_count={pre_tick}")

    # Zombie clear: if _running=True but the thread died, the start()
    # guard at agent_daemon.py:138 would return early without spawning.
    # The supervisor self-heals this on its 60s heartbeat — we do the
    # same manual step here so a one-shot kick doesn't have to wait.
    if agent_daemon._running and not pre_alive:
        print("ZOMBIE detected: _running=True but thread dead — clearing")
        agent_daemon._running = False

    if agent_daemon._running:
        print("Daemon already running.  Polling tick count for motion ...")
    else:
        try:
            agent_daemon.start()
            print("Daemon start() called.")
        except Exception as exc:
            print(f"FATAL: agent_daemon.start() raised: {exc}", file=sys.stderr)
            return 3

    # Poll for visible motion: tick_count should advance within
    # poll_interval seconds (default 30s per HEVOLVE_AGENT_POLL_INTERVAL).
    start = time.time()
    while time.time() - start < poll_seconds:
        time.sleep(2)
        tick_now = getattr(agent_daemon, '_tick_count', 0)
        alive_now = bool(getattr(agent_daemon, '_thread', None) and
                         agent_daemon._thread.is_alive())
        elapsed = int(time.time() - start)
        print(f"  t+{elapsed:>3}s  _thread_alive={alive_now} "
              f"_tick_count={tick_now}")
        if tick_now > pre_tick:
            print(f"OK — tick advanced ({pre_tick} → {tick_now}).  "
                  f"Daemon is dispatching goals on its 30s loop now.")
            return 0

    print("WARN: poll window elapsed with no tick advance.")
    print("      Check ~/Documents/Nunba/logs/frozen_debug.log for")
    print("      'Agent daemon' / 'Daemon tick' lines around now.")
    return 1


if __name__ == '__main__':
    sys.exit(kick(poll_seconds=int(sys.argv[1]) if len(sys.argv) > 1 else 60))
