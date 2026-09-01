"""The co-pilot daemon's boundary holds, in the right order.

This daemon runs Claude Code unattended on the node, so its gates ARE the safety
argument. The steward's framing: full autonomy inside the work, zero authority at
the boundaries, and "where important it doesn't change the outcome". The gates
below are what make that mechanical rather than advisory:

  1. stop file        the steward can halt it without touching systemd
  2. circuit breaker  the human's kill switch outranks the daemon absolutely
  3. user yield       it must never compete with the person using the machine
  4. rate limit       a looping agent cannot burn an 8GB node
  5. no work          no assigned task means idle, never invented work

Behavioural: drives the REAL tick() with the boundaries stubbed at their seams and
asserts the observable decision, including that Claude is NEVER invoked when any
gate is closed (the property that actually matters).

Run:
    python -m pytest tests/unit/test_copilot_daemon_boundary.py -v \
        --noconftest -p no:cacheprovider
"""
import os
import pathlib
import sys

import pytest

sys.path.insert(0, os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), 'scripts'))

import hart_copilot_daemon as d  # noqa: E402


@pytest.fixture
def limiter():
    return d.RateLimiter(max_per_hour=4)


@pytest.fixture(autouse=True)
def _never_really_run_claude(monkeypatch):
    """Hard safety net for the suite itself: if any test would shell out to the real
    Claude binary, fail loudly instead."""
    def _boom(*a, **k):
        raise AssertionError('run_claude was invoked when it must not be')
    monkeypatch.setattr(d, 'run_claude', _boom)
    monkeypatch.setattr(d, 'hive_halted', lambda: False)
    monkeypatch.setattr(d, 'yield_to_user', lambda: False)
    monkeypatch.setattr(d, 'next_task', lambda: None)
    monkeypatch.setattr(d, 'ensure_hive_session', lambda: 'idle')
    monkeypatch.setattr(d.os.path, 'exists', lambda p: False)
    # The workspace is real git on a real disk; stub it at its seam so these tests
    # exercise the DECISION logic, not git.
    monkeypatch.setattr(d, 'ensure_workspace', lambda: (True, 'stub'))
    monkeypatch.setattr(d, 'start_branch', lambda task: 'copilot/stub-1')
    monkeypatch.setattr(d, 'has_commits_ahead', lambda: False)


def test_stop_file_halts_before_anything_else(monkeypatch, limiter):
    monkeypatch.setattr(d.os.path, 'exists', lambda p: p == d.STOP_FILE)
    monkeypatch.setattr(d, 'next_task', lambda: {'id': 't1'})
    out = d.tick(limiter)
    assert out['action'] == 'stopped'


def test_halted_hive_outranks_available_work(monkeypatch, limiter):
    """The constitutional kill switch wins even when a task is waiting."""
    monkeypatch.setattr(d, 'hive_halted', lambda: True)
    monkeypatch.setattr(d, 'next_task', lambda: {'id': 't1'})
    out = d.tick(limiter)
    assert out['action'] == 'halted'


def test_user_activity_outranks_available_work(monkeypatch, limiter):
    """A co-pilot must never compete with the human at the keyboard."""
    monkeypatch.setattr(d, 'yield_to_user', lambda: True)
    monkeypatch.setattr(d, 'next_task', lambda: {'id': 't1'})
    out = d.tick(limiter)
    assert out['action'] == 'yield'


def test_no_assigned_task_means_idle_not_invented_work(limiter):
    """The daemon does not make up work. No task assigned is an honest idle."""
    out = d.tick(limiter)
    assert out['action'] == 'idle'
    assert 'no task' in out['reason']


def test_rate_limit_caps_runs_per_hour(monkeypatch):
    lim = d.RateLimiter(max_per_hour=2)
    monkeypatch.setattr(d, 'next_task', lambda: {'id': 't1'})
    monkeypatch.setattr(d, 'run_claude', lambda *a, **k: {'ok': True})
    assert d.tick(lim)['action'] == 'ran'
    assert d.tick(lim)['action'] == 'ran'
    out = d.tick(lim)
    assert out['action'] == 'rate-limited', out


def test_rate_limit_window_slides(monkeypatch):
    lim = d.RateLimiter(max_per_hour=1)
    lim.record(now=1000.0)
    assert lim.allow(now=1000.0 + 3599) is False   # still inside the hour
    assert lim.allow(now=1000.0 + 3601) is True    # window slid


def test_a_real_run_reports_failure_honestly(monkeypatch, limiter):
    """A failed Claude run is reported as failed, never smoothed into success."""
    monkeypatch.setattr(d, 'next_task', lambda: {'id': 't9'})
    monkeypatch.setattr(d, 'run_claude',
                        lambda *a, **k: {'ok': False, 'error': 'timed out after 1800s'})
    out = d.tick(limiter)
    assert out['action'] == 'ran' and out['ok'] is False
    assert 'timed out' in out['error']


def test_dry_run_never_invokes_claude(monkeypatch, limiter):
    monkeypatch.setattr(d, 'next_task', lambda: {'id': 't1'})
    out = d.tick(limiter, dry_run=True)   # run_claude is the exploding stub
    assert out['action'] == 'would-run'


def test_gates_fail_open_but_are_not_silent(monkeypatch, caplog):
    """A guardrail module that cannot be imported must not wedge the daemon shut,
    but the failure must be logged rather than swallowed (the no-silent-gulping
    rule). hive_halted fails OPEN and records why."""
    monkeypatch.delattr(d, 'hive_halted', raising=False)
    import importlib
    importlib.reload(d)
    monkeypatch.setitem(sys.modules, 'security.hive_guardrails', None)
    assert d.hive_halted() is False


def test_prompt_states_the_boundary_to_the_agent():
    """The instruction handed to Claude must repeat the boundary in-band, so the
    agent's own behaviour matches what the daemon enforces."""
    p = d.build_prompt({'title': 'fix the paint watchdog', 'detail': 'it never drops'})
    low = p.lower()
    assert 'fix the paint watchdog' in p
    assert 'never commit to main' in low
    assert 'a human merges' in low
    # It must be told how to verify on THIS machine, and told not to change what
    # the machine boots into: `test` activates now, `switch`/`boot` do not.
    # The prompt now points at the root oneshot rather than at sudo, because sudo
    # never worked from a NoNewPrivileges unit. Telling it the truth about its own
    # privileges matters more than telling it a rule: an agent that believes it has
    # sudo burns a run discovering it does not.
    assert 'nixos-rebuild test' in low
    assert d.VERIFY_UNIT in p
    assert '`switch` and `boot` are not available to you' in low
    # And it must know a local branch is a dead end.
    assert 'open a pr' in low


def test_no_pr_when_the_branch_has_no_commits(monkeypatch, limiter):
    """An empty PR is noise a human has to close. A run that changed nothing must
    not open one."""
    monkeypatch.setattr(d, 'next_task', lambda: {'id': 't1', 'title': 'x'})
    monkeypatch.setattr(d, 'run_claude', lambda *a, **k: {'ok': True})
    monkeypatch.setattr(d, 'has_commits_ahead', lambda: False)
    monkeypatch.setattr(d, 'open_pr', lambda *a, **k: pytest.fail('opened an empty PR'))
    out = d.tick(limiter)
    assert out['action'] == 'ran' and 'pr' not in out


def test_no_pr_when_the_run_failed(monkeypatch, limiter):
    """A failed run must not be proposed for merge."""
    monkeypatch.setattr(d, 'next_task', lambda: {'id': 't1', 'title': 'x'})
    monkeypatch.setattr(d, 'run_claude', lambda *a, **k: {'ok': False, 'error': 'boom'})
    monkeypatch.setattr(d, 'has_commits_ahead', lambda: True)
    monkeypatch.setattr(d, 'open_pr', lambda *a, **k: pytest.fail('proposed a failed run'))
    out = d.tick(limiter)
    assert out['ok'] is False and 'pr' not in out


def test_successful_verified_work_becomes_a_pr(monkeypatch, limiter):
    """The point of the loop: work that succeeded and has commits is carried into
    the build as a PR against main. A branch on one node reaches no image."""
    seen = {}

    def fake_pr(branch, title, body):
        seen.update(branch=branch, title=title, body=body)
        return {'ok': True, 'url': 'https://github.com/x/y/pull/1'}

    monkeypatch.setattr(d, 'next_task', lambda: {'id': 't1', 'title': 'fix the meter'})
    monkeypatch.setattr(d, 'run_claude', lambda *a, **k: {'ok': True})
    monkeypatch.setattr(d, 'has_commits_ahead', lambda: True)
    monkeypatch.setattr(d, 'start_branch', lambda t: 'copilot/20260727-010203')
    monkeypatch.setattr(d, 'open_pr', fake_pr)
    out = d.tick(limiter)
    assert out['pr'] == 'https://github.com/x/y/pull/1'
    assert seen['branch'].startswith('copilot/')
    assert 'fix the meter' in seen['title']


def test_branch_is_created_from_main_not_discovered(monkeypatch, limiter):
    """The daemon CUTS its working branch from origin/main before any work, so
    "never commit to main" is true by construction rather than checked afterwards.
    A clone left on some previous branch cannot leak work into the wrong place."""
    seen = {}
    monkeypatch.setattr(d, 'next_task', lambda: {'id': 't1', 'title': 'x'})
    monkeypatch.setattr(d, 'run_claude', lambda *a, **k: {'ok': True})
    def _start(task):
        seen['called'] = True
        return 'copilot/x-1'
    monkeypatch.setattr(d, 'start_branch', _start)
    out = d.tick(limiter)
    assert seen.get('called') is True
    assert out['branch'] == 'copilot/x-1'


def test_workspace_failure_is_reported_not_idled_through(monkeypatch, limiter):
    """No clone and nothing to do are different states. A workspace failure says so
    instead of looking like an idle tick."""
    monkeypatch.setattr(d, 'next_task', lambda: {'id': 't1', 'title': 'x'})
    monkeypatch.setattr(d, 'ensure_workspace', lambda: (False, 'clone failed: no network'))
    out = d.tick(limiter)
    assert out['action'] == 'workspace-error'
    assert 'clone failed' in out['reason']


# ── The privilege boundary is in the system config, not in the prompt ────────
#
# The daemon unit runs as `hart` with NoNewPrivileges=true, so it cannot escalate
# at all. Activation therefore goes through a root oneshot whose ExecStart names
# the flake ref and the verb. These assert the daemon cannot express `switch` or
# `boot`, and that the two files still agree about what gets activated.

NIX_MODULE = pathlib.Path(__file__).resolve().parents[2] / 'nixos' / 'modules' / 'hart-copilot.nix'


def test_daemon_never_builds_a_privileged_argv():
    """It triggers a unit. It does not shell out to sudo, which NoNewPrivileges
    blocks anyway, and which silently reported 'not a NixOS host?' on NixOS."""
    src = pathlib.Path(d.__file__).read_text(encoding='utf-8')
    assert "'sudo'" not in src and '"sudo"' not in src
    assert d.VERIFY_UNIT == 'hart-copilot-verify.service'


def test_verify_unit_activates_and_cannot_switch():
    """`test` leaves the boot generation alone. `switch` and `boot` do not appear,
    so no agent can reach them through this path however it is prompted."""
    nix = NIX_MODULE.read_text(encoding='utf-8')
    # Anchor on the UNIT DEFINITION, not on the first mention of the name: comments
    # reference the unit before it is defined, so splitting on the bare name
    # inspected the gap between two comments and passed or failed by accident.
    marker = 'systemd.services.hart-copilot-verify'
    assert marker in nix, 'the verify unit is not defined'
    start = nix.split(marker, 1)[1]
    assert 'nixos-rebuild test' in start
    # The whole point: the verb is fixed in the unit, so no prompt can reach the
    # two that would change what the machine boots into.
    assert 'nixos-rebuild switch' not in start
    assert 'nixos-rebuild boot' not in start


def test_nix_and_daemon_agree_on_what_is_activated():
    """Two files hold these constants because the daemon argues for constants over
    knobs. That is fine only while something fails when they drift."""
    nix = NIX_MODULE.read_text(encoding='utf-8')
    assert f'copilotRepo = "{d.REPO}"' in nix
    assert f'copilotFlakeAttr = "{d.FLAKE_ATTR}"' in nix


def test_daemon_can_reach_the_unit_it_needs():
    """A polkit rule is what makes the separation reachable rather than a unit the
    daemon is not allowed to start."""
    nix = NIX_MODULE.read_text(encoding='utf-8')
    assert 'polkit.addRule' in nix
    assert 'hart-copilot-verify.service' in nix
    assert 'pkgs.systemd' in nix, 'systemctl must be on the daemon unit path'


# ── the copilot must be able to REACH the hive ───────────────────────────────

def test_copilot_unit_carries_the_backend_port():
    """The daemon resolves its backend through core.port_registry's
    get_port('backend') deliberately, as "the ONE canonical port source rather
    than an env override that can drift". That registry reads
    HARTOS_BACKEND_PORT, and hart-copilot.nix was the ONLY HART unit that did
    not set it (hart-backend, hart-liquid-ui, hart-model-bus, hart-app-bridge
    and hart-conky all do), so it fell through to the OS-mode default of 677 --
    a port nothing binds.

    Measured on the box 2026-08-24: the daemon logged
    `backend=http://127.0.0.1:677`; curl :677 returned 000 while :6777 returned
    200; every tick for a day said {"action": "idle", "reason": "no task
    assigned by the hive"}; and `claude` was invoked exactly zero times. An
    unreachable hive is reported as an idle hive, so the daemon looked healthy
    while being structurally unable to do anything.
    """
    import pathlib
    src = (pathlib.Path(__file__).resolve().parents[2]
           / "nixos" / "modules" / "hart-copilot.nix").read_text(encoding="utf-8")
    code = "\n".join(line.split("#")[0] for line in src.splitlines())
    assert "HARTOS_BACKEND_PORT" in code, (
        "hart-copilot.nix must set HARTOS_BACKEND_PORT in the daemon's unit "
        "environment; without it get_port('backend') resolves to a dead port and "
        "the copilot can never receive a task.")
    assert "127.0.0.1:6777" not in code, (
        "the backend port must come from config.hart.ports.backend, not a "
        "hardcoded 6777 that can drift from what the backend actually binds.")


# ── gate 5½: the hive session self-heals, without violating gates 1-4 ───────
#
# ClaudeHiveSession is a process-global singleton in the backend with NO
# persistence: every backend restart silently dropped the session, and the
# daemon then polled an empty queue forever while honestly reporting an "idle
# hive" — 30 days of zero task accepts on the real node traced to exactly
# this. ensure_hive_session() reconnects ONLY from the disconnected state
# (reconnecting a live session re-registers it and zeroes its stats), rides
# the same public HTTP API the CLI uses, and runs INSIDE the gated tick so a
# stopped/halted/yielding daemon never touches the network.

# Captured at import, BEFORE the autouse fixture stubs the attr — the two
# direct tests below must drive the REAL function, not their own stub.
_REAL_ENSURE = d.ensure_hive_session


class _Resp:
    def __init__(self, code=200, body=None):
        self.status_code = code
        self._body = body or {}

    def json(self):
        return self._body


def test_a_disconnected_session_is_reconnected(monkeypatch):
    calls = []

    class FakeRequests:
        @staticmethod
        def get(url, timeout=None):
            calls.append(('get', url))
            return _Resp(200, {'status': 'disconnected', 'session_id': ''})

        @staticmethod
        def post(url, json=None, timeout=None):
            calls.append(('post', url, json))
            return _Resp(200, {'success': True})

    monkeypatch.setitem(sys.modules, 'requests', FakeRequests)
    monkeypatch.setattr(d, 'backend', lambda: 'http://127.0.0.1:6777')
    assert _REAL_ENSURE() == 'idle'
    posts = [c for c in calls if c[0] == 'post']
    assert len(posts) == 1 and posts[0][1].endswith('/api/hive/session/connect')
    assert posts[0][2]['task_scope'] == 'own_repos'


def test_a_live_session_is_left_alone(monkeypatch):
    """Reconnecting a live session re-registers it (new id, zeroed stats) —
    so idle and working sessions must not be touched."""
    calls = []

    class FakeRequests:
        @staticmethod
        def get(url, timeout=None):
            return _Resp(200, {'status': 'idle', 'session_id': 'chs_x'})

        @staticmethod
        def post(url, json=None, timeout=None):
            calls.append('post')
            return _Resp(200, {})

    monkeypatch.setitem(sys.modules, 'requests', FakeRequests)
    monkeypatch.setattr(d, 'backend', lambda: 'http://127.0.0.1:6777')
    assert _REAL_ENSURE() == 'idle'
    assert calls == [], 'a live session was re-registered'


def test_a_dead_backend_degrades_to_honest_unknown(monkeypatch):
    """Mid-restart the backend answers nothing; the tick must not crash."""
    class FakeRequests:
        @staticmethod
        def get(url, timeout=None):
            raise OSError('connection refused')

    monkeypatch.setitem(sys.modules, 'requests', FakeRequests)
    monkeypatch.setattr(d, 'backend', lambda: 'http://127.0.0.1:6777')
    assert _REAL_ENSURE() == 'unknown'


def test_closed_gates_never_reach_the_session_ensure(monkeypatch, limiter):
    """A stopped daemon must not so much as touch the network: the stop file
    (gate 1) has to short-circuit BEFORE ensure_hive_session runs. Uses the
    file's own stop-file idiom (the autouse fixture pins os.path.exists to
    False, so a real tmp file is invisible here by design)."""
    touched = []
    monkeypatch.setattr(d, 'ensure_hive_session',
                        lambda: touched.append(True))
    monkeypatch.setattr(d.os.path, 'exists', lambda p: p == d.STOP_FILE)
    out = d.tick(limiter)
    assert out['action'] == 'stopped'
    assert touched == [], 'a stopped daemon touched the hive session API'
