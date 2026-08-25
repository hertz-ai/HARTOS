"""Bundled mode must have ONE learning home, not two (RFC-B).

Measured incident 2026-08-25: hevolveai_supervisor always spawns (or
adopts) the :8000 hevolveai server in bundled Nunba and exports
HEVOLVEAI_API_URL before hart_intelligence_entry's delayed learning init
runs (export 19:15:49 vs init 19:19:13; export ~18:23 vs init 18:27:15).
The delayed init then built a SECOND full learning stack in-process —
BootstrappedIntelligence init lines appear in both processes' logs — and
the two stacks together grew commit at ~23GB/h (learning child +6.1GB/h,
Flask parent +17GB/h, snapshots 21:40→22:17) until the app OOM-died at
~22:2x, taking the census offline for 25 minutes.

_init_learning_pipeline's own docstring says "Instead of starting a
separate server on port 8000" — written before the supervisor existed.
The fix restores that 'instead of': when bundled AND a hevolveai server
is designated (HEVOLVEAI_API_URL), skip the in-process stack entirely.
The WorldModelBridge already serves stats, experiences, and skills over
HTTP (the 5bdc1ca7 heal plus the Direction-B poller, both live-proven
2026-08-25 21:00:59).  Proven RED before the fix.
"""
import pytest

import hart_intelligence_entry as hie


@pytest.fixture
def _reached(monkeypatch):
    """Record whether the heavy init body was entered.

    The sentinel is try_import_hevolveai — the FIRST call inside the body,
    before any `import hevolveai.*`. Later sentinels are unreachable in
    this test process: pytest's earlier imports bind txaio to asyncio, so
    the hevolveai import chain raises RuntimeError("Explicitly using
    'asyncio' already") and the body bails before e.g.
    _wait_for_llm_server ever runs.
    """
    calls = []
    monkeypatch.setattr('security.native_hive_loader.try_import_hevolveai',
                        lambda name: calls.append(name))
    return calls


def test_bundled_with_supervisor_child_skips_in_process(monkeypatch, _reached):
    monkeypatch.setattr(hie, '_is_bundled', lambda: True)
    monkeypatch.setenv('HEVOLVEAI_API_URL', 'http://localhost:8000')

    hie._init_learning_pipeline()

    assert _reached == [], (
        'bundled + supervisor-designated server must skip the in-process '
        'learning stack (one learning home)')


def test_standalone_still_initializes(monkeypatch, _reached):
    monkeypatch.setattr(hie, '_is_bundled', lambda: False)
    monkeypatch.setenv('HEVOLVEAI_API_URL', 'http://localhost:8000')

    hie._init_learning_pipeline()

    assert _reached, 'standalone (dev) keeps the in-process pipeline'


def test_bundled_without_server_still_initializes(monkeypatch, _reached):
    monkeypatch.setattr(hie, '_is_bundled', lambda: True)
    monkeypatch.delenv('HEVOLVEAI_API_URL', raising=False)

    hie._init_learning_pipeline()

    assert _reached, (
        'bundled with NO designated server keeps the in-process pipeline '
        '(learning must not silently vanish)')
