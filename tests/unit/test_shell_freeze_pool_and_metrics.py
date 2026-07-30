"""GOAL 3 — mid-session-freeze fixes (beyond the wifi-click + drag fix 35821aca).

Two behavioural contracts, exercised through the REAL LiquidUIService:

1. POOL FLOOR (`_resolve_shell_pool_threads`): waitress is a thread-PER-connection
   server and the shell holds an always-on `/api/notifications/stream` SSE that
   never returns (it permanently owns one worker thread), on top of inherently
   blocking request handlers (the 30s `/api/agent/ask` chat proxy; nmcli/pactl/
   journalctl panel routes). A 1-2 thread pool is starved by a single persistent
   SSE + one blocking request -> the desktop freezes mid session. The floor must
   leave real headroom on EVERY tier.

2. NON-BLOCKING METRICS: `/api/shell/system/metrics` is polled every 4s by
   hartSessionUI. It must not sleep on the request thread (it used to call
   psutil.cpu_percent(interval=0.5), pinning a worker 0.5s out of every 4s). The
   route must return fast and still carry cpu_percent.
"""
from core.constants import latency_budget
import time

import pytest

from integrations.agent_engine.liquid_ui_service import (
    LiquidUIService, _resolve_shell_pool_threads)


# ── 1. pool floor leaves room for the persistent SSE + a blocking request ─────

# The shell opens at least one always-on SSE (notifications) that holds a worker
# for the whole session; the log-viewer SSE can take a second. Plus one blocking
# request (the chat proxy) can hold a worker for up to 30s. So every tier must
# serve enough workers that those long holders cannot starve the steady polls.
_MIN_HEADROOM = 4


@pytest.mark.parametrize('tier', ['embedded', 'observer', 'lite',
                                  'standard', 'pro', '', None, 'unknown-future'])
def test_pool_floor_never_starves_persistent_sse(tier):
    n = _resolve_shell_pool_threads(tier)
    # 2 persistent SSE (notifications + logs) + a 30s chat + at least one free
    # worker for the next poll/click => a hard floor well above the old 1-2.
    assert n >= _MIN_HEADROOM, (
        f'tier {tier!r} -> {n} workers; a persistent SSE + a blocking chat '
        f'would starve the UI')


def test_pool_floor_scales_with_tier():
    # Weaker tiers get fewer (memory) but still above the starvation floor;
    # capable tiers get more headroom. Monotonic, never below the floor.
    embedded = _resolve_shell_pool_threads('embedded')
    lite = _resolve_shell_pool_threads('lite')
    standard = _resolve_shell_pool_threads('standard')
    assert embedded <= lite <= standard
    assert embedded >= _MIN_HEADROOM


def test_pool_floor_is_above_the_frozen_1_2():
    # Regression guard: the old inline mapping handed 1 (embedded/observer) and
    # 2 (lite) — exactly the values the freeze RCA proved a single persistent
    # SSE + one blocking request could exhaust. Never return those again.
    assert _resolve_shell_pool_threads('embedded') > 2
    assert _resolve_shell_pool_threads('observer') > 2
    assert _resolve_shell_pool_threads('lite') > 2


# ── 2. the polled metrics route does not block the request thread ─────────────

@pytest.fixture
def client():
    svc = LiquidUIService()
    app = svc._create_flask_app()
    app.testing = True
    return app.test_client()


def test_metrics_route_is_non_blocking_and_carries_cpu(client):
    pytest.importorskip('psutil')
    # Prime once (the first non-blocking sample returns 0.0 by design), then the
    # polled call must come back fast — proving no 0.5s interval sleep remains.
    client.get('/api/shell/system/metrics')
    t0 = time.monotonic()
    r = client.get('/api/shell/system/metrics')
    elapsed = time.monotonic() - t0
    assert r.status_code == 200
    body = r.get_json()
    assert 'cpu_percent' in body
    # A 0.5s blocking sample would put this at >=0.5s; a non-blocking read is
    # microseconds. Allow generous slack for slow CI without re-admitting 0.5s.
    _budget = latency_budget('shell_metrics_poll_s')
    assert elapsed < _budget, f'metrics poll took {elapsed:.3f}s (budget {_budget}s) — still blocking?'
