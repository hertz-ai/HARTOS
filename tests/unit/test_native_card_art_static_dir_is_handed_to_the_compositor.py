"""The compositor is HANDED the shell's static dir; it must never guess it.

S4 (2026-09-24) made hart-comp rasterise the bundled SVG card art at compose time. The
art lives in the ONE app package, in the directory liquid-ui serves as /shell/static, and
where that is on a node is a deployment fact: the OS keeps it in the nix store, the Nunba
bundle keeps it elsewhere. comp_core.rs therefore reads `HART_SHELL_STATIC_DIR` and lowers
no photo when it is absent (cards keep their gradient, which is correct but photo-less).

These pins tie the two halves together so neither can drift alone: the Rust side must
read exactly the name the session wrapper exports, and the wrapper must export it from the
app package's static path, the same path liquid-ui serves. A guard satisfied by a comment
is not a guard, so both sides are read from code, not from prose.
"""
import os
import re

REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
COMP_NIX = os.path.join(REPO, "nixos", "modules", "hart-comp.nix")
COMP_CORE = os.path.join(REPO, "compositor", "src", "comp_core.rs")

ENV_NAME = "HART_SHELL_STATIC_DIR"


def _read(path):
    with open(path, encoding="utf-8") as f:
        return f.read()


def _code_only(nix_text):
    return "\n".join(l for l in nix_text.splitlines() if not l.lstrip().startswith("#"))


def test_the_compositor_reads_the_static_dir_from_the_environment_by_this_name():
    src = _read(COMP_CORE)
    reads = re.findall(r'dir\("([A-Z_]+)"\)', src)
    assert ENV_NAME in reads, (
        "comp_core.rs no longer resolves the card art root from %s; the wrapper "
        "export below would then hand it to nobody" % ENV_NAME)


def test_the_session_wrapper_exports_the_app_packages_static_dir_under_that_name():
    nix = _code_only(_read(COMP_NIX))
    m = re.search(r'export %s="([^"]*)"' % ENV_NAME, nix)
    assert m, "hart-comp.nix's session wrapper does not export %s" % ENV_NAME
    value = m.group(1)
    assert "${cfg.package}/integrations/agent_engine/static" in value, (
        "the export must point at the app package's static dir (the directory "
        "liquid-ui serves as /shell/static), got %r" % value)
    assert ("''${%s:-" % ENV_NAME) in value, (
        "a supervisor or an operator must be able to hand a different dir in; "
        "the export has to be a default, not an override")


def test_the_export_lands_beside_the_other_session_markers_the_wrapper_re_exports():
    nix = _code_only(_read(COMP_NIX))
    ready = nix.index('export HART_SHELL_READY_FLAG=')
    static = nix.index('export %s=' % ENV_NAME)
    assert static > ready, (
        "the static dir export belongs in the session wrapper's export block, after "
        "the ready flag it sits beside, not in an unrelated scope of the module")
