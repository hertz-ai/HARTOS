"""Regression guard for the real-HW Tier-ladder BOOT LOOP (journal 2026-06-23).

The GTK4 glass-shell host (``hart-layer-shell-host.nix``) and the cage floor host
(``hart-liquid-ui.nix``) each embed a Python program as ``python -c "<program>"``
which itself lives inside a Nix ``''...''`` indented string. That double layer is
a quoting trap:

  * the Python CANNOT use ``\"\"\"`` docstrings — they'd close the shell's ``"``;
  * Python ``'''...'''`` docstrings are COLLAPSED by Nix's ``'''`` -> ``''``
    escaping into ``''text`` — i.e. an empty string followed by bare text, a hard
    ``SyntaxError`` the moment the compositor launches the host at boot.

On real hardware that crashed EVERY shell host (Tier-1 hart-comp, Tier-2 sway,
and the Tier-3 cage floor), so the first-paint marker was never written, the
session-supervisor's paint-watchdog declared every tier HUNG, and the boot
LOOPED. The fix is to use ``#`` comments (no quotes) instead of triple-quoted
docstrings in the embedded programs.

This test extracts the embedded Python EXACTLY as the Nix build materialises it
(applying the same ``''`` un-escaping, then stubbing the ``${...}`` build-time
interpolations) and ``py_compile``s it, so a re-introduced ``'''`` — or any other
embedded-Python syntax error — fails in CI instead of on a flashed USB stick.
"""

import os
import pathlib
import re
import tempfile

import py_compile
import pytest

_NIXOS_MODULES = pathlib.Path(__file__).resolve().parents[2] / "nixos" / "modules"
_HOSTS = ["hart-layer-shell-host.nix", "hart-liquid-ui.nix"]
_SQ = "'"
_DOL = "$"


def _extract_first_embedded_python(path):
    """Return the RAW ``python -c "<...>"`` program text from a Nix module (the
    bytes between the opening shell quote and its matching close), or None."""
    src = path.read_text(encoding="utf-8")
    m = re.search(r'python -c "', src)
    if not m:
        return None
    i = m.end()
    j = i
    while j < len(src):
        # A Nix ${...} interpolation may legitimately contain a " (e.g. a string
        # literal inside an `if ... then "A" else "B"`); skip it so we don't mistake
        # that for the shell-closing quote.
        if src[j:j + 2] == _DOL + "{":
            k = src.find("}", j)
            j = (k + 1) if k != -1 else (j + 2)
            continue
        if src[j] == '"':
            break  # the shell-closing quote ends the `python -c` argument
        j += 1
    return src[i:j]


def _materialise_like_nix(raw):
    """Apply the Nix ``''`` string un-escaping the build performs, then stub the
    build-time ``${...}`` interpolations with a valid Python token, yielding the
    program text the interpreter actually receives."""
    out = raw.replace(_SQ * 2 + _DOL + "{", _DOL + "{").replace(_SQ * 3, _SQ * 2)
    out = out.replace(_SQ * 2 + "\\n", "\n").replace(_SQ * 2 + "\\t", "\t")
    # ${liquidPort}, ${if ui.preferHardwareGL then "ON_DEMAND" else "NEVER"}, ... ->
    # a bare identifier that is valid wherever the interpolation appears (an enum
    # member access or inside a string literal).
    out = re.sub(r"\$\{[^}]*\}", "PLACEHOLDER", out)
    return out


@pytest.mark.parametrize("host", _HOSTS)
def test_embedded_shell_host_python_compiles(host):
    path = _NIXOS_MODULES / host
    assert path.exists(), f"{host} missing under nixos/modules"
    raw = _extract_first_embedded_python(path)
    assert raw, f"no `python -c` glass-shell host found in {host}"

    # Clear, specific message for the exact regression: no Python triple-single-quote
    # may appear in the embedded program — Nix collapses ''' -> '' and the host dies
    # with SyntaxError at the compositor's first boot. Use `#` comments instead.
    assert _SQ * 3 not in raw, (
        f"{host}: the embedded `python -c` program contains a Python '''...''' — "
        f"inside a Nix '' string Nix collapses it to '' and the glass-shell host "
        f"crashes with SyntaxError at boot (the real-HW Tier-ladder loop). "
        f"Use # comments (no quotes) for the docstring instead."
    )

    program = _materialise_like_nix(raw)
    with tempfile.NamedTemporaryFile(
        "w", suffix=".py", delete=False, encoding="utf-8"
    ) as fh:
        fh.write(program)
        tmp = fh.name
    try:
        # MUST parse exactly as the Nix build hands it to the interpreter.
        py_compile.compile(tmp, doraise=True)
    finally:
        os.unlink(tmp)


def test_session_latch_dir_is_group_writable():
    """The selector wrapper runs as ``hart-admin`` (in the ``hart`` GROUP, not the
    ``hart`` OWNER) and must WRITE the session-tier latch + crash-window file in
    ``/var/lib/hart``. At 0750 the group has only r-x, so every latch/window write
    failed "Permission denied", a tier-drop could never persist, and the selector
    re-attempted hart-comp forever — the real-HW boot loop. Guard the 0770."""
    sup = (_NIXOS_MODULES / "hart-session-supervisor.nix").read_text(encoding="utf-8")
    assert '"d /var/lib/hart 0770 hart hart -"' in sup, (
        "/var/lib/hart must be 0770 (group-writable) so the hart-admin selector can "
        "persist the session-tier latch; 0750 caused the real-HW boot loop "
        "(journal: 'session-tier.window: Permission denied')."
    )
    # And it must NOT have regressed back to the broken 0750.
    assert '"d /var/lib/hart 0750 hart hart -"' not in sup, (
        "/var/lib/hart is back to 0750 — the selector (hart-admin) can't write the "
        "latch and the boot will loop again."
    )
