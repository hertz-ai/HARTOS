"""The shell stands down only for chrome the compositor has actually claimed.

WHY THIS BRIDGE EXISTS
  hart-comp composites the aura backdrop (M1) and the breathing orb (M2)
  itself. Both are painted UNDER the shell's layer surface, so while the shell
  ALSO draws them the native ones are invisible and the browser keeps paying for
  animation it does not need to run. Measured on the box 2026-08-28 with
  `strace -c` on WebKitWebProcess while it burned a full core: 0.64s of syscall
  time out of ~6s and ZERO ioctls, i.e. ~5.4s of pure userspace pixel work.

  So the compositor publishes what it owns and the shell stands down for exactly
  those pieces. One verdict file, many consumers — the same contract
  /run/hart/gpu-render already uses, whose own comment says "REUSE the probe's
  verdict; do NOT invent a second probe".

THE DIRECTION THAT MATTERS
  Guessing wrong here does not hang anything, it produces a desktop with no
  background or no orb — and the paint watchdog catches hangs, not wrong-looking
  desktops. So every failure path must land on "the shell draws everything",
  which is byte-for-byte today's behaviour. This file pins that direction.

Run:
  pytest tests/unit/test_native_chrome_bridge.py -v --noconftest
"""

import os
import sys

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, REPO)

from integrations.agent_engine import liquid_ui_service as L  # noqa: E402

SHELL_SRC = os.path.join(REPO, "integrations", "agent_engine", "liquid_ui_service.py")


@pytest.fixture
def verdict(tmp_path, monkeypatch):
    """Point the reader at a temp verdict file."""
    f = tmp_path / "native-chrome"
    monkeypatch.setattr(L, "_NATIVE_CHROME_FILE", str(f))
    return f


def test_missing_file_means_the_shell_draws_everything(verdict):
    """THE fail-safe. An absent compositor verdict must never be read as a
    claim: that is how you get a desktop with no wallpaper."""
    assert L.read_native_chrome() == frozenset()


def test_unreadable_file_fails_the_same_way(verdict, monkeypatch):
    def boom(*a, **k):
        raise PermissionError("nope")
    monkeypatch.setattr("builtins.open", boom)
    assert L.read_native_chrome() == frozenset()


def test_a_claim_is_honoured(verdict):
    verdict.write_text("bloom,orb")
    assert L.read_native_chrome() == frozenset({"bloom", "orb"})


def test_partial_claims_are_honoured_independently(verdict):
    """The compositor may own the backdrop but not the orb (M1 shipped before
    M2). Each piece stands down on its own."""
    verdict.write_text("bloom")
    got = L.read_native_chrome()
    assert "bloom" in got and "orb" not in got


def test_unknown_names_are_ignored_not_trusted(verdict):
    """A NEWER compositor claiming `taskbar` must not make an OLDER shell hide a
    taskbar it still owns. Forward compatibility has to fail closed."""
    verdict.write_text("bloom,taskbar,topbar,nonsense")
    assert L.read_native_chrome() == frozenset({"bloom"})


def test_whitespace_and_newlines_are_tolerated(verdict):
    verdict.write_text("  bloom \n orb \n")
    assert L.read_native_chrome() == frozenset({"bloom", "orb"})


def test_empty_file_claims_nothing(verdict):
    verdict.write_text("   \n  ")
    assert L.read_native_chrome() == frozenset()


# ── the consumers ────────────────────────────────────────────────────────────

def _src():
    with open(SHELL_SRC, encoding="utf-8") as fh:
        return fh.read()


def test_the_wallpaper_stands_down_for_a_claimed_bloom():
    """The default wallpaper bottoms out in an OPAQUE linear-gradient. That is
    exactly what has hidden the native bloom since M1, so the claim must make it
    transparent or the bridge does nothing."""
    src = _src()
    i = src.index("native_chrome = read_native_chrome()")
    window = src[i: i + 1200]
    assert "'bloom' in native_chrome" in window
    assert "wp_css = 'transparent'" in window, (
        "a claimed bloom must make the shell's wallpaper transparent")


def test_the_shell_hides_its_own_orb_when_the_compositor_owns_it():
    """Without this there would be TWO orbs, the native one breathing under an
    HTML one breathing on top — and the browser would still pay the per-frame
    cost M2 exists to remove."""
    src = _src()
    i = src.index("native_chrome = read_native_chrome()")
    window = src[i: i + 2000]
    assert "'orb' in native_chrome" in window
    assert "hart-voice-orb" in window, "the HTML orb must be suppressed"
    assert "animation:none" in window, (
        "suppressing the orb must also stop its animation, or the browser keeps "
        "rasterising an invisible element every frame")


def test_the_orb_keeps_its_hit_target():
    """visibility, not display:none. The wrapper must keep its layout box so
    click-to-talk and drag keep working against the same geometry while the
    compositor draws the pixels."""
    src = _src()
    i = src.index("native_orb_css = ")
    window = src[i: i + 600]
    assert "visibility:hidden" in window
    assert "display:none" not in window, (
        "display:none would remove the orb's hit target and break input, which "
        "is a far bigger change than swapping who paints it")


# ── the GTK4 host half (bridge part 2) ───────────────────────────────────────

HOST_NIX = os.path.join(REPO, "nixos", "modules", "hart-layer-shell-host.nix")


def _host_python():
    """The program passed to `python -c` in the GTK4 host wrapper."""
    with open(HOST_NIX, encoding="utf-8") as fh:
        src = fh.read()
    lines = src.split("\n")
    start = next(i for i, l in enumerate(lines) if l.startswith("import gi, os"))
    end = next(i for i, l in enumerate(lines[start:], start)
               if l.strip() == "app.run(None)")
    return "\n".join(lines[start:end + 1])


def test_the_embedded_python_has_no_double_quote_or_backslash():
    """THE build-breaker, stated as its own rule.

    That program is passed to `python -c "<body>"` — a Nix DOUBLE-quoted string.
    A single `"` anywhere in it, INCLUDING inside a Python comment, ends the Nix
    string early and no image builds at all. A backslash is consumed as a Nix
    escape before Python ever sees it, so `\n` silently becomes a real newline.

    test_gtk4_host_python_py_compiles already fails when this happens, but it
    reports "extraction failed — body did not contain the host class", which
    reads like a broken test rather than broken source. This says what the rule
    actually is.
    """
    body = _host_python()
    bad_q = [l for l in body.split("\n") if '"' in l]
    assert not bad_q, (
        "double quote inside the `python -c` program (ends the Nix string "
        "early; use single quotes, even in comments): %r" % bad_q[:3])
    bad_b = [l for l in body.split("\n") if "\\" in l]
    assert not bad_b, (
        "backslash inside the `python -c` program (Nix eats it as an escape "
        "before Python sees it; use chr(10) etc.): %r" % bad_b[:3])


def test_the_host_reads_the_same_verdict_as_the_shell():
    """One publisher, several consumers. A second path here would let the host
    and the served shell disagree about who is painting the backdrop, which
    shows up as either a double orb or no background at all."""
    body = _host_python()
    assert "/run/hart/session/native-chrome" in body, (
        "the GTK4 host must read the SAME verdict file as the served shell")
    assert "_native_chrome_claimed" in body


def test_the_host_makes_both_layers_transparent():
    """The WebView paints an opaque page background AND the GTK4 window paints
    its own themed background behind it. Clearing only one leaves the other
    covering the compositor, which is the whole point of the bridge."""
    body = _host_python()
    assert "set_background_color" in body, "the WebView background must be cleared"
    assert "background: transparent" in body, (
        "the GTK4 window background must be cleared too")


def test_the_host_stays_opaque_when_nothing_is_claimed():
    body = _host_python()
    i = body.index("_claimed = _native_chrome_claimed()")
    window = body[i: i + 400]
    assert "if _claimed:" in window, (
        "transparency must be gated on the compositor's claim; unconditional "
        "transparency with no native backdrop is a black desktop")


def test_transparency_failure_never_kills_the_session():
    body = _host_python()
    i = body.index("set_background_color")
    window = body[max(0, i - 400): i + 900]
    assert "except Exception" in window, (
        "a cosmetic handoff must not be able to take the session down; an "
        "opaque shell that paints beats a transparent one that crashed")


# ── the compositor half (bridge part 4): the claim must be EARNED ────────────

COMP_SRC = os.path.join(REPO, "compositor", "src")


def _rust(name):
    with open(os.path.join(COMP_SRC, name), encoding="utf-8") as fh:
        src = fh.read()
    import re as _re
    src = _re.sub(r"/\*.*?\*/", "", src, flags=_re.S)
    return "\n".join(l for l in src.splitlines()
                     if not l.lstrip().startswith("//"))


# ── MOVED TO RUST ───────────────────────────────────────────────────────────
#
# Three tests that used to live here regexed compositor/src/udev.rs as TEXT:
#
#   test_the_claim_is_published_from_the_flip_path_not_config
#   test_the_claim_only_grows_within_a_session
#   test_the_write_is_atomic
#
# They asserted source strings, not behaviour, which is the pattern CLAUDE.md
# Gate 5 forbids. The first one proved the cost: it asserted the literal
# `last_flip_at = Some(now)` sat within 500 characters before the publish call,
# so it went RED when that call legitimately moved into the vblank reaper -- a
# change that made the claim STRONGER (a real DrmEvent::VBlank proves scanout;
# queue_frame's Ok does not, since Ok is also returned for a merely PARKED
# frame). A test that fails when the code gets better, while never once proving
# a claim actually grows or that a torn read is impossible, is worse than none.
#
# The invariants now live in compositor/src/udev.rs `mod tests`, against
# extracted pure helpers (next_claim / claim_names / write_claim) plus a real
# filesystem for the atomic write:
#
#   a_claim_only_ever_grows
#   an_unchanged_claim_is_not_rewritten
#   claiming_nothing_publishes_nothing
#   claim_names_are_stable_and_ordered
#   the_claim_is_written_atomically
#   a_failed_publish_leaves_no_temp_file
#
# They run in CI under `cargo test --features smithay` (the Compositor nix
# build job), which is the only place udev.rs compiles at all.
#
# STILL SOURCE-GREPPING BELOW, tracked in task #69: the comp_core.rs Err-arm
# test and the hart-comp.nix session-start test. The Nix one wants an eval-time
# flake check (precedent: otaTargetBootsRaw in nixos/flake.nix).


def test_a_failed_element_import_does_not_claim_it():
    """A buffer that failed to import was never drawn. Claiming it anyway would
    make the shell hide its own copy of something nobody is painting."""
    core = _rust("comp_core.rs")
    for elem, mask in (("orb", "NATIVE_CHROME_ORB"),
                       ("bloom", "NATIVE_CHROME_BLOOM")):
        i = core.index("%s: failed to import" % elem)
        # The mask must be set in the Ok arm ABOVE, never in/after the Err arm.
        after_err = core[i: i + 300]
        assert mask not in after_err, (
            "%s sets its claim mask on the failure path" % elem)


def test_the_claim_is_cleared_at_session_start():
    """The dangerous case: hart-comp claims, then the ladder drops to sway/cage
    where NOTHING draws the native backdrop. A stale claim would leave the shell
    transparent over nothing."""
    with open(os.path.join(REPO, "nixos", "modules", "hart-comp.nix"),
              encoding="utf-8") as fh:
        nix = fh.read()
    i = nix.index('writeShellScriptBin "hart-comp-session"')
    head = nix[i: i + 1600]
    assert "rm -f /run/hart/session/native-chrome" in head, (
        "every session must start claiming nothing; hart-comp re-earns the "
        "claim by presenting a frame")


def test_the_embedded_python_uses_no_undefined_module_name():
    """A NameError in that program kills the GTK4 host, and py_compile CANNOT see it.

    The host program's only top-level imports are `import gi, os` and the
    gi.repository line; everything else (sys, subprocess, ...) is imported
    LOCALLY inside the function that needs it. So a bare `sys.stderr` written at
    a scope with no local import compiles fine and raises NameError at runtime.

    That is not hypothetical. The native-chrome transparency block was written
    with `file=sys.stderr` in BOTH its success print and its except handler.
    The success path would have raised NameError, been caught, and then the
    handler would have raised NameError AGAIN where nothing catches it —
    killing the host and leaving no shell. And it would have fired only once the
    compositor first CLAIMED chrome, i.e. exactly when the bridge began working.

    test_gtk4_host_python_py_compiles passes on that code, because it is a
    runtime error. This checks name resolution instead.
    """
    import ast
    body = _host_python()
    tree = ast.parse(body)
    builtins_ns = set(dir(__builtins__)) | set(dir(__import__("builtins")))

    def bound_by(node, nodes=None):
        """Names bound in this scope: imports, assignments, defs, args, except.

        `nodes` lets the caller pass ONLY the statements of a scope. That matters:
        ast.walk descends INTO nested functions, so walking the module would
        absorb every local `import sys` from every method and make this check
        vacuous — which is precisely how the first version of this test passed
        against the very bug it was written to catch.
        """
        out = set()
        for n in (nodes if nodes is not None else ast.walk(node)):
            if isinstance(n, (ast.Import, ast.ImportFrom)):
                for a in n.names:
                    out.add((a.asname or a.name).split(".")[0])
            elif isinstance(n, ast.Name) and isinstance(n.ctx, ast.Store):
                out.add(n.id)
            elif isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                out.add(n.name)
            elif isinstance(n, ast.arg):
                out.add(n.arg)
            elif isinstance(n, ast.ExceptHandler) and n.name:
                out.add(n.name)
            elif isinstance(n, ast.alias):
                out.add((n.asname or n.name).split(".")[0])
        return out

    # MODULE scope: fully walk each top-level statement (so `FOO = ...` and
    # `import x` register their targets), but for a def/class bind only its NAME
    # and never descend into a function body — that descent is what made the
    # first version of this check vacuous.
    def module_bindings(mod):
        out = set()
        def take(stmt):
            if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef)):
                out.add(stmt.name)
            elif isinstance(stmt, ast.ClassDef):
                out.add(stmt.name)
                for s in stmt.body:          # class-level names methods can see
                    take(s)
            else:
                out.update(bound_by(stmt))   # safe: no nested function scope here
        for stmt in mod.body:
            take(stmt)
        return out

    module_ns = module_bindings(tree) | builtins_ns

    bad = []
    for fn in [n for n in ast.walk(tree)
               if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]:
        # The function's OWN bindings (walk is fine here: an inner def's imports
        # are genuinely unavailable to the outer body, but treating them as
        # available only makes this check more permissive, never falsely red).
        local = bound_by(fn) | module_ns
        for n in ast.walk(fn):
            if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load):
                if n.id not in local:
                    bad.append("%s() line %d: %r" % (fn.name, n.lineno, n.id))
    assert not bad, (
        "undefined name(s) in the `python -c` host program — these raise "
        "NameError at runtime and py_compile cannot see them. If it is a stdlib "
        "module, import it LOCALLY the way the rest of this program does "
        "(`import sys as _sys`): %s" % bad[:6])

def test_the_embedded_python_has_no_doubled_single_quote():
    """A doubled single quote in this program is read by NIX, not Python.

    The program is passed to python -c, and that whole thing sits inside an
    OUTER Nix INDENTED string where a doubled single quote is the escape
    sequence. So an ordinary Python empty-string literal in

        raw = (fh.read() or <empty literal>).strip().lower()

    was consumed by Nix, ended the string early, and failed the flake
    evaluation gate with a syntax error. That gate SKIPS every build target,
    so nothing shipped at all until it was found.

    This is the third build break of this family (the quad-quote incident,
    then double quotes, then this). The sibling guards check for a double
    quote and for backslash and did NOT catch it: a doubled single quote is
    legal in Python AND in a Nix double-quoted string, and is fatal only
    because of the enclosing indented string. Hence its own check.
    """
    body = _host_python()
    bad = [(i + 1, l) for i, l in enumerate(body.split(chr(10))) if "''" in l]
    assert not bad, (
        'doubled single quote inside the python -c program: Nix reads it as an '
        'escape in the enclosing indented string and the evaluation gate dies, '
        'skipping every build: %r' % bad[:3])
