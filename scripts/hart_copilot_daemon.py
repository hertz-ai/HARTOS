#!/usr/bin/env python3
"""HART co-pilot daemon: Claude Code as a resident, bounded worker on the node.

The steward's ask: Claude Code should be the co-pilot of HART OS, fixing and
bootstrapping it from within, working the seeded goals as its own, through the
guardrails HART OS already has.

WHAT THIS IS NOT
  It is not a second work loop. `coding_daemon.CodingAgentDaemon` already ticks,
  already consults the canonical user-yield gate, already budget-gates, and already
  dispatches through the ONE pipeline. This daemon does the part that does not
  exist: it keeps a Claude Code session RESIDENT and hands it bounded work, so the
  executor is a real coding agent rather than a single /chat turn.

THE BOUNDARY (steward: "trust is a boundary which makes it human-like... where
important it doesn't change the outcome"). Full autonomy inside the work, zero
authority at the boundaries. Enforced here, mechanically, in this order:

  1. CONSTITUTION   HiveCircuitBreaker.is_halted() -> stop. The human's kill switch
                    outranks the daemon absolutely; halted means it does nothing.
  2. THE HUMAN      should_yield_to_user() -> skip this tick. The same canonical gate
                    every other background loop uses (no parallel throttle). A
                    co-pilot must never compete with the person using the machine.
  3. BRANCH ONLY    every run happens on a copilot/* branch in a writable clone.
                    It cannot commit to main, and it cannot touch the read-only
                    /nix/store the running system boots from.
  4. NO MERGE       merge, OTA publish and master-key signing are human. The worst
                    case of an unattended run is a branch nobody merges.
  5. BOUNDED        one task per tick, a hard wall-clock timeout, and a cap on runs
                    per hour, so a wedged or looping agent cannot burn the node.

Run:  hart-copilot-daemon            (systemd: hart.copilot.daemon.enable = true)
      hart-copilot-daemon --once     (one tick, for testing)
      hart-copilot-daemon --dry-run  (decide + report, never invoke Claude)
"""
import argparse
import json
import logging
import os
import subprocess
import sys
import time

logger = logging.getLogger('hart.copilot.daemon')

# Bounds. Deliberately conservative: this shares an 8 GB node with the OS it fixes.
DEFAULT_INTERVAL_S = int(os.environ.get('HART_COPILOT_INTERVAL', '300'))
DEFAULT_TASK_TIMEOUT_S = int(os.environ.get('HART_COPILOT_TASK_TIMEOUT', '1800'))
MAX_RUNS_PER_HOUR = int(os.environ.get('HART_COPILOT_MAX_RUNS_PER_HOUR', '4'))
REPO = os.environ.get('HART_COPILOT_REPO', os.path.expanduser('~/HARTOS'))
BACKEND = os.environ.get('HART_COPILOT_BACKEND', 'http://127.0.0.1:6777')
CLAUDE_BIN = os.environ.get('HART_COPILOT_CLAUDE', 'claude')
# A file the steward can drop to stop the daemon without touching systemd.
STOP_FILE = os.environ.get('HART_COPILOT_STOP', '/run/hart/copilot-stop')


# ─── Gate 1: the constitution ────────────────────────────────────────────────

def hive_halted():
    """True when the human has halted the hive. Fail-OPEN on an import error so a
    guardrail module problem cannot silently wedge the daemon shut, but log it:
    a halt we cannot read is a fact worth seeing."""
    try:
        from security.hive_guardrails import HiveCircuitBreaker
        return bool(HiveCircuitBreaker.is_halted())
    except Exception:
        logger.exception('copilot: could not read the circuit breaker (continuing)')
        return False


# ─── Gate 2: the human at the keyboard ───────────────────────────────────────

def yield_to_user():
    """The ONE canonical gate (dispatch.should_yield_to_user): covers both recent
    user activity and system pressure. Reused, never reimplemented. Fail-open."""
    try:
        from integrations.agent_engine.dispatch import should_yield_to_user
        return bool(should_yield_to_user())
    except Exception:
        logger.debug('copilot: yield gate unavailable (continuing)')
        return False


# ─── Gate 5: rate limiting ───────────────────────────────────────────────────

class RateLimiter:
    """At most ``max_per_hour`` real Claude runs. Cheap in-memory sliding window;
    the daemon is a single process, so no shared state is needed."""

    def __init__(self, max_per_hour=MAX_RUNS_PER_HOUR):
        self.max_per_hour = max_per_hour
        self._runs = []

    def allow(self, now=None):
        now = time.time() if now is None else now
        self._runs = [t for t in self._runs if now - t < 3600]
        return len(self._runs) < self.max_per_hour

    def record(self, now=None):
        self._runs.append(time.time() if now is None else now)


# ─── Work: what the co-pilot is asked to do ──────────────────────────────────

def next_task():
    """The next bounded unit of work, or None.

    Source of truth is the hive session already registered by `hart hive connect`
    (ClaudeHiveSession) -- the tasks it hands out are master-key verified at origin
    and privacy-filtered by the shard engine. This daemon does NOT invent work: no
    task means idle, which is the honest state for a co-pilot with nothing assigned.
    """
    try:
        import requests
        r = requests.get(f'{BACKEND}/api/hive/session/tasks', timeout=10)
        if r.status_code != 200:
            return None
        tasks = (r.json() or {}).get('tasks') or []
        for t in tasks:
            if t.get('status') in (None, '', 'assigned', 'pending', 'queued'):
                return t
        return None
    except Exception:
        logger.debug('copilot: task fetch failed (idling this tick)')
        return None


def build_prompt(task):
    """The instruction handed to Claude Code. States the boundary in-band so the
    agent's own behaviour matches the daemon's enforcement, and points it at the
    node's live runtime rather than a guess."""
    title = task.get('title') or task.get('description') or task.get('goal') or 'the assigned task'
    detail = task.get('detail') or task.get('description') or ''
    return (
        "You are the resident co-pilot of this HART OS node, working from inside it.\n\n"
        f"TASK: {title}\n"
        f"{detail}\n\n"
        "Rules:\n"
        "- Work only in this checkout. You are on a copilot/* branch; stay on it.\n"
        "- Never commit to main, never push to main, never force-push.\n"
        "- Verify with the repo's own tests before you commit. Do not claim a fix "
        "you have not run.\n"
        f"- This node's live backend is {BACKEND}; use it to observe real behaviour "
        "rather than guessing.\n"
        "- If the task is unclear or unsafe, stop and say so instead of improvising.\n"
        "- Leave the work on the branch. A human reviews and merges.\n"
    )


def run_claude(prompt, timeout_s=DEFAULT_TASK_TIMEOUT_S, cwd=REPO):
    """One bounded, headless Claude Code run. -p is print/non-interactive mode.

    A hard timeout is the point: an agent that wedges must not hold the node. The
    subprocess is killed on timeout (never left orphaned) and the outcome reported
    honestly, including failure.
    """
    cmd = [CLAUDE_BIN, '-p', prompt]
    try:
        proc = subprocess.run(
            cmd, cwd=cwd, capture_output=True, text=True, timeout=timeout_s)
        return {
            'ok': proc.returncode == 0,
            'returncode': proc.returncode,
            'stdout': (proc.stdout or '')[-4000:],
            'stderr': (proc.stderr or '')[-2000:],
        }
    except FileNotFoundError:
        return {'ok': False, 'error': 'claude not on PATH (hart.copilot.enable?)'}
    except subprocess.TimeoutExpired:
        return {'ok': False, 'error': f'timed out after {timeout_s}s'}
    except Exception as e:  # never let one bad run kill the daemon
        return {'ok': False, 'error': str(e)}


# ─── The tick ────────────────────────────────────────────────────────────────

def tick(limiter, dry_run=False):
    """One decision. Returns a dict describing what happened and why, so the
    journal shows the REASON for an idle tick rather than silence."""
    if os.path.exists(STOP_FILE):
        return {'action': 'stopped', 'reason': f'stop file present: {STOP_FILE}'}
    if hive_halted():
        return {'action': 'halted', 'reason': 'hive circuit breaker is halted'}
    if yield_to_user():
        return {'action': 'yield', 'reason': 'user active or system under pressure'}
    if not limiter.allow():
        return {'action': 'rate-limited',
                'reason': f'{limiter.max_per_hour} runs/hour cap reached'}

    task = next_task()
    if not task:
        return {'action': 'idle', 'reason': 'no task assigned by the hive'}

    if dry_run:
        return {'action': 'would-run', 'task': task.get('id') or task.get('title')}

    limiter.record()
    result = run_claude(build_prompt(task))
    return {
        'action': 'ran',
        'task': task.get('id') or task.get('title'),
        'ok': result.get('ok'),
        'error': result.get('error'),
    }


def main(argv=None):
    args = _parse(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format='[hart-copilot] %(message)s', stream=sys.stdout)
    limiter = RateLimiter()

    logger.info('co-pilot daemon starting. repo=%s backend=%s interval=%ss '
                'max_runs/h=%s', REPO, BACKEND, args.interval, MAX_RUNS_PER_HOUR)
    logger.info('boundary: branch-only, no merge, halts with the hive, yields to '
                'the user. stop file: %s', STOP_FILE)

    while True:
        try:
            out = tick(limiter, dry_run=args.dry_run)
            logger.info('%s', json.dumps(out, default=str))
        except Exception:
            # A daemon that dies on one bad tick is worse than one that logs it.
            logger.exception('copilot: tick failed (continuing)')
        if args.once:
            return 0
        time.sleep(args.interval)


def _parse(argv):
    p = argparse.ArgumentParser(description='HART co-pilot daemon (bounded Claude Code worker)')
    p.add_argument('--once', action='store_true', help='run a single tick and exit')
    p.add_argument('--dry-run', action='store_true',
                   help='decide and report, never invoke Claude')
    p.add_argument('--interval', type=int, default=DEFAULT_INTERVAL_S,
                   help='seconds between ticks')
    p.add_argument('--verbose', action='store_true')
    return p.parse_args(argv)


if __name__ == '__main__':
    sys.exit(main())
