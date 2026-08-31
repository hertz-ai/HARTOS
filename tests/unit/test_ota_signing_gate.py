"""A node VERIFIES a release signature. It must never try to SIGN one.

THE BUG, live on the .69 box 2026-08-30 -> 2026-08-31. Every OTA reached the
`signing` stage and died there; pending_update.json sat at
`"status": "available"` for a day with revision ca645a43 never applied:

    [HART OTA] Pipeline stage: signing
    [HART OTA] Pipeline in progress (signing), advancing...
    Upgrade failed at signing: sign_release.py failed:
      python3.10: can't open file '//scripts/sign_release.py': [Errno 2]

`_stage_sign` shelled out to `scripts/sign_release.py`, and that was wrong
three independent ways -- the relative path was merely the one that fired
first:

  1. RELATIVE PATH. hart-ota-check.service sets no WorkingDirectory, so
     systemd ran it from `/`, giving `//scripts/sign_release.py`.
  2. IT IS A CI SCRIPT. Its docstring: "Release signing script for
     HevolveSocial CI/CD ... Requires MASTER_PRIVATE_KEY_HEX environment
     variable (GitHub Actions secret)." The box has no master private key
     (verified: no /var/lib/hart/keys, no master.key) and must not have one.
  3. NO ARGV. The script requires --version/--git-sha/--code-hash/
     --manifest-hash; none were passed.

A consumer node minting its own release signature would defeat the gate it is
standing at. The node's job is the inverse: prove CI signed what it is about
to adopt. These tests pin the DECISION at that gate, and pin the negative
property that made the bug possible -- no subprocess, no private key.

Run:
  pytest tests/unit/test_ota_signing_gate.py -v
"""

import os
import time
import sys

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if REPO not in sys.path:
    sys.path.insert(0, REPO)

from integrations.agent_engine.upgrade_orchestrator import (  # noqa: E402
    UpgradeOrchestrator)


def gate():
    """The stage under test, with no on-disk pipeline state."""
    return UpgradeOrchestrator.__new__(UpgradeOrchestrator)


def patch_verification(monkeypatch, result):
    """Make security.master_key.full_boot_verification return `result`."""
    import security.master_key as mk
    monkeypatch.setattr(mk, 'full_boot_verification',
                        lambda *a, **k: result, raising=False)


# ── the gate opens only on proof ────────────────────────────────────────────

def test_a_verified_release_advances(monkeypatch):
    patch_verification(monkeypatch, {
        'passed': True, 'enforcement': 'hard',
        'details': 'Code hash and guardrail hash match signed manifest'})
    ok, reason = gate()._stage_sign()
    assert ok
    assert 'verified' in reason.lower()


@pytest.mark.parametrize('why,details', [
    ('bad_signature', 'Invalid master signature on release manifest'),
    ('code_mismatch', 'Code hash mismatch: local=aaaa... manifest=bbbb...'),
    ('origin_failed', 'Origin attestation failed: not genuine HART OS'),
])
def test_evidence_of_tampering_blocks_the_upgrade(monkeypatch, why, details):
    """THE POINT OF THE STAGE. Each of these is a node being handed code that
    is not what the master key signed. The pipeline must stop."""
    patch_verification(monkeypatch, {
        'passed': False, 'enforcement': 'hard',
        'reason': why, 'details': details})
    ok, reason = gate()._stage_sign()
    assert not ok, 'a %s must NOT advance the pipeline' % why
    assert 'FAILED' in reason


def test_no_manifest_advances_because_absence_is_not_tampering(monkeypatch):
    """No bundled desktop build ships a release_manifest.json -- see the
    `no_manifest` note in full_boot_verification. Failing closed here would
    disable OTA on 100% of desktops while catching zero tampering. It must
    advance, and it must say the signature was not verified rather than
    claiming success."""
    patch_verification(monkeypatch, {
        'passed': False, 'enforcement': 'hard',
        'reason': 'no_manifest', 'details': 'No release_manifest.json found'})
    ok, reason = gate()._stage_sign()
    assert ok
    assert 'NOT verified' in reason


def test_a_missing_security_package_does_not_claim_verification(monkeypatch):
    """Permissive (not every deployment ships security/), but it must never
    report a verification it did not perform."""
    import builtins
    real_import = builtins.__import__

    def blocked(name, *a, **k):
        if name == 'security.master_key':
            raise ImportError('no module named security.master_key')
        return real_import(name, *a, **k)

    monkeypatch.setattr(builtins, '__import__', blocked)
    ok, reason = gate()._stage_sign()
    assert ok
    assert 'NOT verified' in reason


# ── the negative properties that made the bug possible ──────────────────────

def test_the_gate_never_shells_out(monkeypatch):
    """THE REGRESSION TEST. The old stage ran a subprocess; that is how a
    relative path and a missing CI secret could break OTA at all. Verifying a
    signature is in-process work -- if anything here spawns a process again,
    fail loudly."""
    import subprocess as sp
    calls = []
    monkeypatch.setattr(sp, 'run',
                        lambda *a, **k: calls.append(a) or pytest.fail(
                            'signing gate spawned a subprocess: %r' % (a,)))
    patch_verification(monkeypatch, {'passed': True, 'details': 'ok'})
    gate()._stage_sign()
    assert calls == []


def test_the_gate_never_touches_a_private_key(monkeypatch):
    """A node holds no master private key and must not reach for one. Any
    access is a design regression toward signing-on-node."""
    import security.master_key as mk
    monkeypatch.setattr(mk, 'get_master_private_key',
                        lambda *a, **k: pytest.fail(
                            'signing gate reached for the master PRIVATE key'),
                        raising=False)
    monkeypatch.setattr(mk, 'sign_child_certificate',
                        lambda *a, **k: pytest.fail(
                            'signing gate tried to sign'), raising=False)
    patch_verification(monkeypatch, {'passed': True, 'details': 'ok'})
    ok, _ = gate()._stage_sign()
    assert ok


def test_a_verification_crash_blocks_rather_than_passes(monkeypatch):
    """An exception inside verification is not a pass."""
    import security.master_key as mk

    def boom(*a, **k):
        raise RuntimeError('canonicalisation drift')

    monkeypatch.setattr(mk, 'full_boot_verification', boom, raising=False)
    ok, reason = gate()._stage_sign()
    assert not ok
    assert 'error' in reason.lower()


# ── the rung after signing: a canary must be waitable ───────────────────────
#
# Unblocking SIGNING only moved the death one rung along. advance_pipeline
# mapped EVERY falsy handler result to _fail() -> stage='failed' (terminal),
# and _stage_canary's own first return is (False, 'canary started, check
# again later'). So starting a canary marked the upgrade FAILED, and the
# "check again later" it asks for could never come. Recorded as a narrated
# contradiction in test_flow_04_apps_and_upgrades.py long before it was fixed.

def pipeline_at(stage, **attrs):
    """A real orchestrator parked on `stage`, with no disk state."""
    orch = UpgradeOrchestrator.__new__(UpgradeOrchestrator)
    import threading
    orch._lock = threading.RLock()
    orch._state = {'stage': stage, 'stage_history': [], 'version': 'vtest'}
    orch._save_state = lambda: None
    orch._canary_start = 0
    orch._canary_duration = 1800
    for k, v in attrs.items():
        setattr(orch, k, v)
    return orch


def test_starting_a_canary_does_not_fail_the_upgrade(monkeypatch):
    """THE REGRESSION TEST. First canary advance must hold the stage."""
    orch = pipeline_at('canary')
    orch._start_canary_deployment = lambda: None

    result = orch.advance_pipeline()

    assert result['stage'] == 'canary', 'the canary stage was abandoned'
    assert result.get('in_progress') is True
    assert orch._state['stage'] == 'canary', (
        'starting a canary marked the upgrade %r' % orch._state['stage'])


def test_a_canary_still_inside_its_window_keeps_waiting(monkeypatch):
    """Healthy, mid-window. Not a pass, not a failure."""
    orch = pipeline_at('canary')
    orch._start_canary_deployment = lambda: None
    orch._check_canary_health = lambda: (True, 'ok')
    orch._canary_start = time.time() - 60      # 60s into a 1800s window

    result = orch.advance_pipeline()

    assert result.get('in_progress') is True
    assert orch._state['stage'] == 'canary'
    assert 'in progress' in result['detail']


def test_an_unhealthy_canary_still_fails(monkeypatch):
    """The point of a canary. IN_PROGRESS must not swallow real failures."""
    orch = pipeline_at('canary')
    orch._check_canary_health = lambda: (False, 'node 3 unhealthy')
    orch._canary_start = time.time() - 60

    result = orch.advance_pipeline()

    assert result['success'] is False
    assert orch._state['stage'] == 'failed'
    assert 'canary failed' in result['detail']


def test_a_completed_healthy_canary_advances(monkeypatch):
    """And it must still be able to finish."""
    orch = pipeline_at('canary')
    orch._check_canary_health = lambda: (True, 'ok')
    orch._canary_start = time.time() - 3600    # past the 1800s window

    result = orch.advance_pipeline()

    assert result['success'] is True
    assert orch._state['stage'] == 'deploying', (
        'a passed canary did not advance; got %r' % orch._state['stage'])


def test_in_progress_is_falsy_so_old_style_checks_cannot_advance():
    """Belt and braces. Any caller still doing a plain `if passed:` must read
    IN_PROGRESS as 'did not pass', never as success."""
    from integrations.agent_engine.upgrade_orchestrator import IN_PROGRESS
    assert not IN_PROGRESS
    assert IN_PROGRESS is not False and IN_PROGRESS is not True
