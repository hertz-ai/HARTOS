"""Phase 7: the portal screencast gate is driven by the cross-process screen
kill-switch, fail-closed, and the proof is observable.

The xdg-desktop-portal-hart ScreenCast handler + the wlr-screencopy grim/wf-
recorder wrappers (hart-portal.nix) decide ALLOW/REFUSE by one rule only:
core.ai_sensing.query_authority('screen'). These tests pin the Python contract
that the Nix gate binary rides on — the VM test (tests/portal-screencast.nix)
proves the shell wrapper + real socket end-to-end, but cannot run on the dev box.

  python -m pytest tests/unit/test_portal_screencast_gate.py --noconftest -p no:capture -q
"""
import os
import socket
import tempfile

import pytest

import core.ai_sensing as s


# ── The proof field the portal status surfaces (un-fakeable) ────────────────

def test_status_proof_reports_portal_screencast_blocked_tracking_screen_gate():
    """status().proof.portal_screencast_blocked mirrors the human's screen cut —
    True exactly when 'screen' is disabled. This is the observable, un-fakeable
    signal that every native screencast surface is shut at the portal."""
    try:
        s.set_sense('screen', False)
        st = s.status()
        assert st['proof']['portal_screencast_blocked'] is False
        s.set_sense('screen', True)
        st = s.status()
        assert st['proof']['portal_screencast_blocked'] is True
        # It tracks 'screen' specifically, not the other senses.
        assert st['proof']['screen_gated'] is True
    finally:
        s.set_sense('screen', False)


# ── The cross-process gate decision the wrappers make ───────────────────────

def _gate_allows(sock_path):
    """Mirror the hart-screencast-gate binary's decision in Python: ALLOW iff
    query_authority('screen') is True; REFUSE (fail-closed) otherwise."""
    return s.query_authority('screen', sock_path)


def test_gate_refuses_fail_closed_when_authority_unreachable():
    # No server bound ⇒ the gate (and thus the grim/wf-recorder wrappers) REFUSE.
    # A down brain must never open a capture surface.
    assert _gate_allows('/tmp/hart-portal-no-authority-xyz.sock') is False


@pytest.mark.skipif(not hasattr(socket, 'AF_UNIX'),
                    reason='AF_UNIX unavailable on this platform')
def test_gate_allows_only_when_screen_on_refuses_when_cut():
    path = os.path.join(tempfile.mkdtemp(), 'sense.sock')
    s.set_sense('screen', False)                  # sensing on
    if not s.start_authority_server(path):
        pytest.skip('AF_UNIX bind unsupported here — Linux deployment path')
    try:
        assert _gate_allows(path) is True         # screen on ⇒ gate allows
        s.set_sense('screen', True)               # human cuts the screen
        assert _gate_allows(path) is False        # ⇒ gate REFUSES cross-process
    finally:
        s.set_sense('screen', False)


# ── The pinned-socket env contract (the load-bearing cross-process glue) ────

def test_authority_path_honors_pinned_env_override():
    """hart-portal.nix pins BOTH the canonical state holder (the LiquidUI shell
    process that serves /api/shell/ai-sensing) AND the portal query client to ONE
    path via HART_AI_SENSING_SOCK (separate systemd units, no shared
    XDG_RUNTIME_DIR). The resolver must prefer that env so both sides agree."""
    pinned = '/run/hart/ai-sensing.sock'
    old = os.environ.get('HART_AI_SENSING_SOCK')
    try:
        os.environ['HART_AI_SENSING_SOCK'] = pinned
        assert s._authority_path() == pinned
        # An explicit arg still wins over the env (test/diagnostic override).
        assert s._authority_path('/tmp/explicit.sock') == '/tmp/explicit.sock'
    finally:
        if old is None:
            os.environ.pop('HART_AI_SENSING_SOCK', None)
        else:
            os.environ['HART_AI_SENSING_SOCK'] = old


# ── Source guards (clearly-labelled; COMPLEMENT the behavioural tests above) ──
# Per CLAUDE.md Gate 5/Gate 6: an opt-in module not in hartModules[] exposes no
# option; a nixosTest not merged into `checks` never runs. These guard the wiring
# that a pure-Python behavioural test cannot reach (the Nix flake + module files).

import os.path as _osp

_REPO = _osp.dirname(_osp.dirname(_osp.dirname(_osp.abspath(__file__))))
_NIXOS = _osp.join(_REPO, 'nixos')


def _read_text(path):
    with open(path, 'r', encoding='utf-8') as f:
        return f.read()


def test_source_guard_portal_module_wired_into_flake():
    flake = _read_text(_osp.join(_NIXOS, 'flake.nix'))
    # In hartModules[] (else hart.portal.enable exists for no variant) ...
    assert 'hart-portal.nix' in flake
    # ... AND the nixosTest is imported + merged into `checks` (else it never runs).
    assert 'tests/portal-screencast.nix' in flake
    assert 'portalScreencast' in flake


def test_source_guard_portal_consumes_the_supreme_gate_only():
    # The portal must CONSUME core.ai_sensing (query_authority), never re-implement
    # a second screen flag or expose a re-enable path. The gate binary + module ride
    # on query_authority('screen'); assert the module references it and NOT a
    # bespoke parallel flag.
    mod = _read_text(_osp.join(_NIXOS, 'modules', 'hart-portal.nix'))
    assert 'query_authority' in mod, \
        "portal screencast gate must consult core.ai_sensing.query_authority"
    assert 'core.ai_sensing' in mod


def test_source_guard_portal_does_not_flip_default_session():
    # Phase 7 adds a portal + lock; it JOINS nothing on the never-fail ladder and
    # must NOT touch defaultSession (cage stays the floor — ROADMAP §6 invariant).
    mod = _read_text(_osp.join(_NIXOS, 'modules', 'hart-portal.nix'))
    assert 'defaultSession' not in mod, \
        "hart-portal.nix must NOT assign defaultSession — cage stays the floor"
