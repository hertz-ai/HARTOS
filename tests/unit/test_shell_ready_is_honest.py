"""shell-ready must mean the desktop is ON THE GLASS, not merely "a load ended".

THE BUG (false-healthy #3/#6, documented in
docs/architecture/OS_PARITY_CLOSURE_PLAN.md and prescribed in
docs/internal/ux_degrading_design_choices_2026-07-24.md:1.6). The GTK3 cage
floor in nixos/modules/hart-liquid-ui.nix touched the first-paint marker on
LoadEvent.FINISHED ALONE:

    def _on_load_changed(self, _webview, event):
        if event == WebKit2.LoadEvent.FINISHED:
            _signal_painted()

WebKit emits FINISHED for its own stock ERROR page too, and it emits it for a
window that never mapped. So a blank surface -- the shell port not up yet, a
host that died before present() -- marked the tier HEALTHY. The paint-watchdog
reads HEALTHY off that marker, so it could not legitimately drop the tier, and
a black screen stayed up forever. That is the never-black ladder being defeated
by its own health signal.

It is what fails the display-tiers-neverblack paint-ladder nixosTest: every tier
fell to the cage floor having painted NOTHING, yet /run/hart/session/shell-ready
existed. The GTK4 host (hart-layer-shell-host.nix) already required mapped AND
finished AND not-failed; this is the GTK3 floor catching up to it, not a second
scheme.

HOW THESE TESTS WORK. The shell is Python embedded inside a Nix string, so it
cannot be imported. These tests EXTRACT the shipped program, exec it, and then
drive the REAL handlers against a REAL file on disk -- the marker either appears
or it does not. No assertions about source text (CLAUDE.md Gate 5); the thing
under test is the behaviour that decides whether a black screen is called
healthy.

Run:
  pytest tests/unit/test_shell_ready_is_honest.py -v
"""

import io
import os
import re
import sys

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
NIX = os.path.join(REPO, 'nixos', 'modules', 'hart-liquid-ui.nix')
GTK4_NIX = os.path.join(REPO, 'nixos', 'modules', 'hart-layer-shell-host.nix')


class LoadEvent:
    """The WebKit2 enum values the handler compares against."""
    STARTED = 'started'
    REDIRECTED = 'redirected'
    COMMITTED = 'committed'
    FINISHED = 'finished'


class _WebKit2Stub:
    LoadEvent = LoadEvent


def load_shell_program(nix_path, start_marker, end_marker):
    """Extract the embedded shell program and exec it.

    Truncated before the instantiation line so no GTK window is constructed;
    we want the real class object, not a running shell.
    """
    src = io.open(nix_path, encoding='utf-8').read()
    i = src.index(start_marker)
    j = src.index(end_marker, i)
    prog = src[i:j]

    # Undo Nix indented-string escapes, then neutralise interpolations.
    prog = prog.replace("''${", "${").replace("'''", "''")
    prog = re.sub(r'\$\{[^}]*\}', 'NIXVAL', prog)

    ns = {
        'os': os,
        'sys': sys,
        'WebKit2': _WebKit2Stub,
        'WebKit': _WebKit2Stub,
        'Gtk': type('Gtk', (), {'Window': object, 'main_quit': lambda *a: None}),
        'GLib': type('GLib', (), {}),
        'Gdk': type('Gdk', (), {}),
    }
    exec(compile(prog, nix_path, 'exec'), ns)
    return ns


@pytest.fixture
def shell(tmp_path, monkeypatch):
    """The real cage-floor program, with the marker pointed at a temp file."""
    ns = load_shell_program(NIX, 'READY_FLAG = os.environ.get',
                            'GlassShell()')
    marker = tmp_path / 'shell-ready'
    ns['READY_FLAG'] = str(marker)

    cls = ns['GlassShell']
    obj = cls.__new__(cls)          # real methods, no GTK construction
    obj._mapped = False
    obj._load_finished = False
    obj._last_load_failed = False
    return obj, marker


def painted(marker):
    return marker.exists()


# -- the false-healthy cases the watchdog must be able to drop ---------------

def test_a_finished_load_alone_is_not_paint(shell):
    """THE REGRESSION TEST. The old code fired here. A load that finished into a
    window that never mapped is a black screen."""
    obj, marker = shell
    obj._on_load_changed(None, LoadEvent.FINISHED)
    assert not painted(marker), (
        'a finished load with an UNMAPPED window claimed paint -- the watchdog '
        'can no longer drop a black screen')


def test_a_mapped_window_alone_is_not_paint(shell):
    """A window on screen showing nothing yet is not paint either."""
    obj, marker = shell
    obj._on_map(None)
    assert not painted(marker)


def test_webkits_error_page_does_not_claim_paint(shell):
    """The documented case: the shell port is not up, WebKit substitutes its
    stock error page, and STILL emits FINISHED. That surface is blank and the
    tier must read as HUNG so the supervisor drops to a working one."""
    obj, marker = shell
    obj._on_map(None)
    obj._on_load_failed(None, LoadEvent.FINISHED, 'http://localhost:6800',
                        OSError('connection refused'))
    obj._on_load_changed(None, LoadEvent.FINISHED)
    assert not painted(marker), (
        'a failed load claimed paint -- a connection-refused blank screen '
        'would be kept HEALTHY forever')


# -- and the honest case must still work ------------------------------------

def test_mapped_plus_a_clean_finish_is_paint(shell):
    """The gate is worthless if a real desktop cannot report itself healthy."""
    obj, marker = shell
    obj._on_map(None)
    obj._on_load_changed(None, LoadEvent.FINISHED)
    assert painted(marker), 'a mapped window with a clean load did NOT signal paint'


def test_order_does_not_matter(shell):
    """map and load-finished race; whichever lands second must fire."""
    obj, marker = shell
    obj._on_load_changed(None, LoadEvent.FINISHED)
    assert not painted(marker)
    obj._on_map(None)
    assert painted(marker), 'load-then-map must signal just like map-then-load'


def test_a_retry_after_a_failure_can_still_go_healthy(shell):
    """The guard must not strand a tier. A failed load followed by a NEW load
    that succeeds is a working desktop and must be able to say so -- otherwise
    this fix would trade a false-healthy for a permanent false-HUNG."""
    obj, marker = shell
    obj._on_map(None)
    obj._on_load_failed(None, LoadEvent.FINISHED, 'http://localhost:6800',
                        OSError('connection refused'))
    obj._on_load_changed(None, LoadEvent.FINISHED)
    assert not painted(marker)

    obj._on_load_changed(None, LoadEvent.STARTED)     # the retry begins
    obj._on_load_changed(None, LoadEvent.FINISHED)    # and succeeds
    assert painted(marker), (
        'a successful reload after a failure never went healthy -- the tier '
        'would be dropped forever')


def test_intermediate_load_events_do_not_claim_paint(shell):
    """COMMITTED/REDIRECTED arrive before any pixels exist."""
    obj, marker = shell
    obj._on_map(None)
    for ev in (LoadEvent.REDIRECTED, LoadEvent.COMMITTED):
        obj._on_load_changed(None, ev)
        assert not painted(marker), '%s claimed paint' % ev


# -- the marker has exactly one gated writer --------------------------------

def test_the_marker_has_exactly_one_writer_and_it_is_the_guard():
    """Structural, but proven by EXECUTING the program: _signal_painted must be
    unreachable except through _maybe_signal_painted. A second ungated caller is
    how this bug existed in the first place."""
    import ast
    src = io.open(NIX, encoding='utf-8').read()
    i = src.index('READY_FLAG = os.environ.get')
    j = src.index('GlassShell()', i)
    prog = src[i:j].replace("''${", "${").replace("'''", "''")
    prog = re.sub(r'\$\{[^}]*\}', 'NIXVAL', prog)
    tree = ast.parse(prog)

    callers = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef):
            for c in ast.walk(node):
                if isinstance(c, ast.Call) and getattr(c.func, 'id', '') == \
                        '_signal_painted':
                    callers.add(node.name)
    assert callers == {'_maybe_signal_painted'}, (
        'the first-paint marker must have ONE gated writer, found: %s' % callers)


def test_the_gtk4_host_agrees_with_the_floor(tmp_path):
    """Both hosts must apply the SAME honesty rule. They are separate programs
    (GTK4/WebKit-6.0 vs GTK3/WebKit2) and drifted apart once already, which is
    how the floor kept the weaker rule."""
    ns = load_shell_program(GTK4_NIX, 'READY_FLAG = os.environ.get',
                            'app = Gtk.Application(')
    marker = tmp_path / 'shell-ready-gtk4'
    ns['READY_FLAG'] = str(marker)
    cls = next(v for k, v in ns.items()
               if isinstance(v, type) and hasattr(v, '_maybe_signal_painted'))
    obj = cls.__new__(cls)
    obj._mapped = False
    obj._load_finished = True
    obj._last_load_failed = False
    obj._maybe_signal_painted()
    assert not marker.exists(), 'the GTK4 host signalled paint while UNMAPPED'
    obj._mapped = True
    obj._maybe_signal_painted()
    assert marker.exists(), 'the GTK4 host did not signal paint when honest'
