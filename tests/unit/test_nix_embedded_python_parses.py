r"""Every Python embedded in a Nix module must PARSE after Nix emits it.

WHY THIS EXISTS (two real-HW/CI outages, same root family):

  1. 2026-07-19/20 -- ``hart-smart-index.service`` died on EVERY boot with
     ``SyntaxError: '(' was never closed``. The embedded indexer wrote ``''''``
     (four quotes) meaning the Python empty string, but in a Nix indented string
     ``'''`` is the escape that EMITS ``''`` -- so four quotes emitted THREE: an
     unterminated Python triple-quote that swallowed the rest of the function.

  2. 2026-07-20 -- the fix's own explanatory comment contained a BARE ``''``
     pair, which TERMINATES a Nix indented string. The script ended mid-comment,
     the remaining Python was parsed as Nix, and iso-desktop failed to build
     (``error: syntax error, unexpected '='``). A build-breaking typo inside a
     comment.

Neither is catchable by reading the .nix (the bug only exists AFTER Nix emits
the string) and neither is catchable by ``nix flake check`` for case 1 (it is
valid Nix that emits invalid Python). This guard closes both: it walks the Nix
indented strings exactly as Nix's lexer does (honoring ``'''``, ``''$``, ``''\``
and treating a bare ``''`` as the terminator), extracts each embedded
``python -c`` body, replays the Nix escapes + neutralizes antiquotations, and
``ast.parse``s the result.

Labeled a source-guard by placement, but it is genuinely behavioural about the
artifact that ships: it parses the exact program the node will execute.
"""
import ast
import os
import re
import textwrap

import pytest

BS = chr(92)
Q2 = "''"
NIXOS_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "nixos",
)


def _nix_indented_string_end(src, start):
    """Index of the terminating ``''`` for an indented string whose body begins
    at ``start`` -- Nix's own rule: ``'''``, ``''$`` and ``''\\`` are escapes;
    any other bare ``''`` ENDS the string."""
    i, n = start, len(src)
    while i < n - 1:
        if src[i] == "'" and src[i + 1] == "'":
            nxt = src[i + 2] if i + 2 < n else ""
            if nxt in ("'", "$", BS):
                i += 3
                continue
            return i
        i += 1
    return n


def _emit(body):
    """Apply the Nix escapes the way Nix does, then neutralize antiquotations so
    the result is parseable Python (``${pkgs.x}`` -> a bareword)."""
    sentinel = chr(0)
    body = body.replace("'''", sentinel)      # ''' -> literal ''
    body = body.replace(Q2 + "${", "${")      # ''${ -> literal ${
    body = body.replace(sentinel, Q2)
    body = re.sub(r"\$\{[^{}]*\}", "NIX", body)   # antiquote -> bareword
    return textwrap.dedent(body)


def _embedded_pythons():
    """Yield (nix_path, script_name, python_source) for every ``python -c`` body
    embedded in a Nix indented string under nixos/."""
    out = []
    for root, dirs, files in os.walk(NIXOS_DIR):
        dirs[:] = [d for d in dirs if d not in (".git", "result")]
        for fn in sorted(files):
            if not fn.endswith(".nix"):
                continue
            path = os.path.join(root, fn)
            with open(path, "r", encoding="utf-8", errors="replace") as f:
                src = f.read()
            for m in re.finditer(
                r'(?:writeShellScriptBin|writeShellScript|writeText)\s+"([^"]+)"\s+' + Q2,
                src,
            ):
                name = m.group(1)
                end = _nix_indented_string_end(src, m.end())
                body = src[m.end():end]
                for pm in re.finditer(r'python3?\s+-c\s+"\n(.*?)\n\s*"', body, re.S):
                    out.append((path, name, pm.group(1)))
    return out


EMBEDDED = _embedded_pythons()


def test_there_are_embedded_pythons_to_check():
    """Guard the guard: if the extraction silently stops matching, this test
    would vacuously pass and both outage classes would be unprotected again."""
    assert EMBEDDED, (
        "no embedded `python -c` blocks found under nixos/ -- the extractor "
        "drifted (or the Nix string scan is terminating early); this guard "
        "would be silently vacuous."
    )


@pytest.mark.parametrize(
    "path,name,py",
    EMBEDDED,
    ids=[f"{os.path.basename(p)}:{n}" for p, n, _ in EMBEDDED],
)
def test_embedded_python_parses_after_nix_emission(path, name, py):
    emitted = _emit(py)
    try:
        ast.parse(emitted)
    except (SyntaxError, IndentationError) as e:
        rel = os.path.relpath(path, os.path.dirname(NIXOS_DIR))
        lines = emitted.splitlines()
        lo = max(0, (e.lineno or 1) - 3)
        ctx = "\n".join(
            "%4d| %s" % (i + 1, lines[i]) for i in range(lo, min(len(lines), (e.lineno or 1) + 2))
        )
        pytest.fail(
            "embedded python in %s (%s) does NOT parse after Nix emission: %s\n"
            "Common causes: a 4-quote empty string (emits 3 -> unterminated "
            "triple-quote), or a BARE 2-quote pair anywhere in the block "
            "(including prose in a comment) which TERMINATES the Nix string.\n%s"
            % (rel, name, e, ctx)
        )
