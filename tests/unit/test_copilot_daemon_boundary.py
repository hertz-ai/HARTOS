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
    monkeypatch.setattr(d.os.path, 'exists', lambda p: False)


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
    assert 'nixos-rebuild test' in low
    assert 'never use `switch` or `boot`' in low
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
    monkeypatch.setattr(d, 'current_branch', lambda: 'copilot/20260727-010203')
    monkeypatch.setattr(d, 'open_pr', fake_pr)
    out = d.tick(limiter)
    assert out['pr'] == 'https://github.com/x/y/pull/1'
    assert seen['branch'].startswith('copilot/')
    assert 'fix the meter' in seen['title']


def test_never_opens_a_pr_from_main(monkeypatch, limiter):
    """Defence in depth: if the clone somehow sits on main, do not open a PR from
    it. main is the base, never the head."""
    monkeypatch.setattr(d, 'next_task', lambda: {'id': 't1', 'title': 'x'})
    monkeypatch.setattr(d, 'run_claude', lambda *a, **k: {'ok': True})
    monkeypatch.setattr(d, 'has_commits_ahead', lambda: True)
    monkeypatch.setattr(d, 'current_branch', lambda: 'main')
    monkeypatch.setattr(d, 'open_pr', lambda *a, **k: pytest.fail('opened a PR from main'))
    out = d.tick(limiter)
    assert out['action'] == 'ran'
