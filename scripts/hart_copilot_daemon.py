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
  4. NO MERGE       verified work is carried into the build as a PR against main,
                    because a branch on one node is a dead end: it is not in main,
                    so no nightly contains it, so the next flashed image cannot be
                    tested against it. Merge, OTA publish and master-key signing
                    stay human. The worst case of an unattended run is a PR nobody
                    merges.
  5. BOUNDED        one task per tick, a hard wall-clock timeout, and a cap on runs
                    per hour, so a wedged or looping agent cannot burn the node.

VERIFYING ON THE NODE
  The agent tests OS-level changes with `nixos-rebuild test --flake`, which
  activates on the running machine in minutes instead of a ~90 minute ISO build
  plus a reflash, and does NOT change what the machine boots into: a power cycle
  reverts it. `switch` and `boot` are deliberately never used, because changing
  what the machine comes up as is the human's call. This needs the INSTALLED
  writable-root image; on the live ISO the store is a RAM-backed overlay and a
  rebuild will usually fail there.

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

# Constants, not configuration. Every one of these has exactly one correct value
# for this daemon, so it is written down rather than made a knob: a flag is a
# decision deferred, and each one doubles the states nobody tests. Tests patch the
# module attribute directly; they do not need an env var to exist.
INTERVAL_S = 300               # tick cadence
TASK_TIMEOUT_S = 1800          # hard wall-clock on one Claude run
NIXOS_REBUILD_TIMEOUT_S = 2700 # hard wall-clock on one activation
MAX_RUNS_PER_HOUR = 4          # it shares an 8 GB node with the OS it fixes
REPO = '/var/lib/hart/copilot/HARTOS'   # the daemon's own writable clone
ORIGIN = 'https://github.com/hertz-ai/HARTOS.git'
CLAUDE_BIN = 'claude'
STOP_FILE = '/run/hart/copilot-stop'    # drop this file to halt it, no systemd needed
FLAKE_ATTR = 'hart-desktop'    # the system this node IS
PR_BASE = 'main'               # CLAUDE.md: main branch only. Never a variable.
# The root oneshot that activates a config. Defined in nixos/modules/hart-copilot.nix,
# where REPO and FLAKE_ATTR above are written into its ExecStart. This daemon runs
# with NoNewPrivileges and cannot escalate, so triggering that unit is the only way
# it can activate anything, and it cannot choose the verb.
VERIFY_UNIT = 'hart-copilot-verify.service'


def backend():
    """The node's own backend, from the ONE canonical port source rather than a
    hardcoded port or an env override that can drift from what is listening."""
    try:
        from core.port_registry import get_port
        return f'http://127.0.0.1:{get_port("backend")}'
    except Exception:
        return 'http://127.0.0.1:6777'


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
        r = requests.get(f'{backend()}/api/hive/session/tasks', timeout=10)
        if r.status_code != 200:
            return None
        # The endpoint is ClaudeHiveSession.get_tasks(), which returns
        # {'pending': [...], 'completed': [...]} -- there is no 'tasks' key and
        # never was. Reading one made this return None on EVERY tick, so the
        # daemon logged "no task assigned by the hive" forever, indistinguishable
        # from a genuinely idle hive even with work queued and dispatched.
        # Verified on the real node 2026-08-20: the live endpoint answers
        # {"completed":[],"pending":[]}.
        tasks = (r.json() or {}).get('pending') or []
        for t in tasks:
            # get_tasks() projects pending entries down to task_id/description/
            # received_at, so 'status' is absent here -- None is accepted below,
            # and the richer shapes stay accepted for any other caller.
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
        f"- This node's live backend is {backend()}; use it to observe real behaviour "
        "rather than guessing.\n"
        "- To test an OS-level change on THIS RUNNING MACHINE, use:\n"
        f"      systemctl start --wait {VERIFY_UNIT}\n"
        "  That runs `nixos-rebuild test` on this clone as root. It activates the\n"
        "  change now but does NOT change what the machine boots into, so a power\n"
        "  cycle undoes it. `sudo nixos-rebuild` will not work for you and is not\n"
        "  the path: you have no sudo, and the verb is fixed in that unit, so\n"
        "  `switch` and `boot` are not available to you at all. Changing the boot\n"
        "  default is a human decision.\n"
        "- After activating, check the journal for what you changed and confirm the\n"
        "  behaviour actually differs. An unverified fix is not a fix.\n"
        "- If the task is unclear or unsafe, stop and say so instead of improvising.\n"
        "- When the fix is verified, commit to this branch, push it, and open a PR\n"
        f"  against `{PR_BASE}`. A local branch never becomes a build, so it never\n"
        "  reaches the flashed image; the PR is how the work gets somewhere.\n"
        "  Say in the PR body what you changed, how you verified it on this node,\n"
        "  and what you did NOT verify. A human merges.\n"
    )


def open_pr(branch, title, body):
    """Push the working branch and open a PR against the branch the nightlies build.

    This is the point of the whole loop. A commit on a local branch on one node
    changes nothing: it is not in `main`, so no nightly contains it, so the next
    flashed image does not have it, so it can never be tested on the installed
    device. The PR is what carries the work into the build.

    The boundary is unchanged and is in fact sharpened by this: the daemon can
    PROPOSE into the pipeline, and only a human merge puts it into what the machine
    becomes. Opening a PR is reversible with one click; merging is the decision.
    """
    try:
        push = subprocess.run(['git', 'push', '-u', 'origin', branch],
                              cwd=REPO, capture_output=True, text=True, timeout=180)
        if push.returncode != 0:
            return {'ok': False, 'error': 'push failed: ' + (push.stderr or '')[-500:]}
        pr = subprocess.run(
            ['gh', 'pr', 'create', '--base', PR_BASE, '--head', branch,
             '--title', title, '--body', body],
            cwd=REPO, capture_output=True, text=True, timeout=180)
        if pr.returncode != 0:
            return {'ok': False, 'error': 'gh pr create failed: ' + (pr.stderr or '')[-500:]}
        return {'ok': True, 'url': (pr.stdout or '').strip()}
    except FileNotFoundError as e:
        return {'ok': False, 'error': f'missing tool: {e}'}
    except subprocess.TimeoutExpired:
        return {'ok': False, 'error': 'push/PR timed out'}
    except Exception as e:
        return {'ok': False, 'error': str(e)}


def _git(*args, timeout=180, cwd=REPO):
    """One place that shells git, so every call is bounded and its failure is
    inspectable rather than swallowed."""
    return subprocess.run(['git', *args], cwd=cwd, capture_output=True,
                          text=True, timeout=timeout)


def ensure_workspace():
    """Make the daemon's workspace real, because nothing else does.

    The interactive `hart-copilot` launcher clones into the user's home; the daemon
    runs headless as `hart` with its own StateDirectory and never touches that. On a
    fresh node /var/lib/hart/copilot exists but is EMPTY, so without this the first
    run fails on a working directory that is not there.

    Returns (ok, reason). Failure is reported, never silently idled through.
    """
    try:
        if not os.path.isdir(os.path.join(REPO, '.git')):
            os.makedirs(os.path.dirname(REPO), exist_ok=True)
            r = _git('clone', '--depth', '50', ORIGIN, REPO, cwd=None, timeout=900)
            if r.returncode != 0:
                return False, 'clone failed: ' + (r.stderr or '')[-300:]
            return True, 'cloned'
        r = _git('fetch', '--depth', '50', 'origin', PR_BASE)
        if r.returncode != 0:
            return False, 'fetch failed: ' + (r.stderr or '')[-300:]
        return True, 'fetched'
    except subprocess.TimeoutExpired:
        return False, 'git timed out preparing the workspace'
    except Exception as e:
        return False, str(e)


def start_branch(task):
    """Put the clone on a FRESH branch cut from origin/main before any work.

    This is what makes "never commit to main" true by construction instead of
    something checked afterwards: the daemon creates the branch it is going to work
    on, so the working branch is known rather than discovered, and a stale clone
    left on some previous branch cannot leak work into the wrong place.

    Returns the branch name, or None if git refused (reported by the caller).
    """
    ident = str(task.get('id') or task.get('title') or 'task')
    slug = ''.join(c if c.isalnum() else '-' for c in ident).strip('-').lower()[:40]
    branch = f'copilot/{slug or "task"}-{int(time.time())}'
    r = _git('checkout', '-B', branch, f'origin/{PR_BASE}')
    if r.returncode != 0:
        logger.error('copilot: could not start branch %s: %s',
                     branch, (r.stderr or '').strip()[-300:])
        return None
    return branch


def has_commits_ahead():
    """True when the branch actually has work on it. Prevents an empty PR, which is
    noise a human then has to close."""
    try:
        r = subprocess.run(['git', 'log', '--oneline', f'origin/{PR_BASE}..HEAD'],
                           cwd=REPO, capture_output=True, text=True, timeout=30)
        return bool((r.stdout or '').strip())
    except Exception:
        return False


def nixos_rebuild_test(timeout_s=NIXOS_REBUILD_TIMEOUT_S):
    """Activate the working clone's config on the RUNNING node, without touching
    the boot default.

    `nixos-rebuild test` is the whole reason a co-pilot on a NixOS box can verify
    its own work: it applies to the live system in minutes instead of a ~90 minute
    ISO build plus a reflash, and it is self-limiting, because the boot generation
    is unchanged and a reboot reverts it. `switch` and `boot` are deliberately NOT
    offered here: changing what the machine comes up as is a boundary crossing, and
    those stay with the human (and with OTA, which is master-key signed).

    Requires the INSTALLED writable-root image. On the live ISO the store is a
    RAM-backed overlay on an 8GB box, so a rebuild will usually fail there; that is
    a real limitation and it is reported, not hidden.

    It does NOT run `sudo nixos-rebuild`, which never worked. The unit runs as
    `hart` with NoNewPrivileges=true, `sudo` is not on its path, and no sudoers rule
    grants it anything, so escalation was blocked at the kernel and every call on a
    real node returned "not a NixOS host?" while sitting on a NixOS host.

    Loosening the daemon to fix that would trade the hardening for the feature. So
    activation moved into a root oneshot (hart-copilot-verify.service) that accepts
    NO arguments from here: the flake ref and the verb `test` are written into its
    ExecStart. An agent that ignores every line of its prompt still cannot ask for
    `switch` or `boot`, because this function has no argument in which to say it.
    What the machine boots into stays a human decision by construction rather than
    by instruction.
    """
    cmd = ['systemctl', 'start', '--wait', VERIFY_UNIT]
    try:
        p = subprocess.run(cmd, cwd=REPO, capture_output=True, text=True,
                           timeout=timeout_s)
        return {'ok': p.returncode == 0, 'returncode': p.returncode,
                'stderr': ((p.stderr or '').strip()[-2000:] or
                           f'see: journalctl -u {VERIFY_UNIT}')}
    except FileNotFoundError:
        return {'ok': False, 'error': 'systemctl not on PATH (not a systemd host?)'}
    except subprocess.TimeoutExpired:
        return {'ok': False, 'error': f'nixos-rebuild test timed out after {timeout_s}s'}
    except Exception as e:
        return {'ok': False, 'error': str(e)}


def run_claude(prompt, timeout_s=TASK_TIMEOUT_S, cwd=REPO):
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

    # The workspace is the daemon's own responsibility. A failure here is reported,
    # not idled through: "no clone" and "nothing to do" are different states.
    ok, reason = ensure_workspace()
    if not ok:
        return {'action': 'workspace-error', 'reason': reason}

    # Cut the working branch BEFORE any work. This is what makes "never commit to
    # main" structural: the branch is created, so it is known, so nothing has to be
    # checked afterwards.
    branch = start_branch(task)
    if not branch:
        return {'action': 'branch-error', 'reason': 'could not create the working branch'}

    limiter.record()
    result = run_claude(build_prompt(task))
    out = {
        'action': 'ran',
        'task': task.get('id') or task.get('title'),
        'branch': branch,
        'ok': result.get('ok'),
        'error': result.get('error'),
    }

    # Carry the work into the build. Without this the run is a no-op in practice:
    # a commit sitting on a branch on one node is not in main, so no nightly has
    # it, so the next flashed image cannot be tested against it. Only opened when
    # the run succeeded AND there is actually something to review.
    if result.get('ok') and has_commits_ahead():
        title = f"copilot: {task.get('title') or task.get('id') or 'node fix'}"
        body = (
            "Opened by the resident co-pilot daemon on a HART OS node.\n\n"
            f"Task: {task.get('id') or ''} {task.get('title') or ''}\n\n"
            "Verified on the node it was written on, to the extent stated in the "
            "commits. Merge is a human decision; nothing here has changed what "
            "any machine boots into.\n"
        )
        pr = open_pr(branch, title, body)
        out['pr'] = pr.get('url') if pr.get('ok') else None
        out['pr_error'] = pr.get('error')
    return out


def main(argv=None):
    args = _parse(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format='[hart-copilot] %(message)s', stream=sys.stdout)
    limiter = RateLimiter()

    logger.info('co-pilot daemon starting. repo=%s backend=%s interval=%ss '
                'max_runs/h=%s', REPO, backend(), args.interval, MAX_RUNS_PER_HOUR)
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
    p.add_argument('--interval', type=int, default=INTERVAL_S,
                   help='seconds between ticks')
    p.add_argument('--verbose', action='store_true')
    return p.parse_args(argv)


if __name__ == '__main__':
    sys.exit(main())
