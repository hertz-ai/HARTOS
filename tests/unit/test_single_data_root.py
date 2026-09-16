"""Every hart process must resolve the SAME data root.

THE DEFECT, measured on real hardware 2026-09-10
------------------------------------------------
core/platform_paths.py:get_data_dir() resolves in priority order:

    1. NUNBA_DATA_DIR
    2. HARTOS_DATA_DIR
    3. an embedded-OS probe for /etc/hartos-release  -> /var/lib/hartos
    4. the PLATFORM DEFAULT, on Linux ~/.config/nunba

Only hart-backend.nix and hart-agent.nix set HARTOS_DATA_DIR. Every other hart
unit fell through to step 4, so on one machine, one user, one HOME, there were
TWO dispatcher queues:

    /var/lib/hart/agent_data/hive_tasks.json                11 tasks, assigned
    /var/lib/hart/.config/nunba/agent_data/hive_tasks.json   9 tasks, pending

The second was written once at boot by a unit that lacked the variable, then
orphaned. Its nine tasks can never dispatch, because the session registry lives
in the process that reads the other file. Nothing errored and nothing logged;
the work just went somewhere nobody looks. That is the failure mode this test
exists to prevent, and it is exactly the "no parallel paths" rule stated in
physical form: one queue, or the queue is a lie.

Step 3 cannot rescue it either, and must not be made to: that branch resolves
to /var/lib/hartos, a THIRD root, not hart.dataDir. Shipping
/etc/hartos-release would move every path on the node.

The fix is one declaration in hart-base.nix (which owns hart.dataDir) via
systemd.globalEnvironment, so it reaches units added later too. Copying the
variable per-unit is what produced the split in the first place.
"""

import pathlib
import re

_NIXOS = pathlib.Path(__file__).resolve().parents[2] / "nixos"
_BASE = _NIXOS / "modules" / "hart-base.nix"
_PLATFORM = pathlib.Path(__file__).resolve().parents[2] / "core" / "platform_paths.py"


def _read(p: pathlib.Path) -> str:
    return p.read_text(encoding="utf-8")


def test_hart_base_declares_one_global_data_root():
    """The variable must be set once, globally, from the module that owns the
    option -- not copied into individual units."""
    src = _read(_BASE)
    m = re.search(r"systemd\.globalEnvironment\s*=\s*\{(.*?)\};", src, re.S)
    assert m, (
        "hart-base.nix must declare systemd.globalEnvironment so EVERY hart "
        "unit resolves the same data root; per-unit copies are how the split "
        "happened")
    body = m.group(1)
    assert re.search(r"HARTOS_DATA_DIR\s*=\s*cfg\.dataDir\s*;", body), (
        "the global environment must set HARTOS_DATA_DIR to cfg.dataDir, the "
        "option this module owns -- not a hardcoded path that can drift from it")


def test_it_is_declared_in_the_module_that_owns_the_option():
    """Single source: the declaration and the option live together, so changing
    hart.dataDir moves every process at once."""
    src = _read(_BASE)
    assert "dataDir = lib.mkOption" in src, (
        "hart.dataDir is no longer declared in hart-base.nix; move the "
        "globalEnvironment declaration to wherever it went, keeping them "
        "together")


def test_platform_paths_priority_is_unchanged():
    """This fix relies on HARTOS_DATA_DIR outranking the platform default. If
    that ordering changes, the fix silently stops working."""
    src = _read(_PLATFORM)
    hartos_at = src.index("HARTOS_DATA_DIR")
    # The Linux platform default must be resolved AFTER the env override.
    default_at = src.index("XDG_DATA_HOME")
    assert hartos_at < default_at, (
        "HARTOS_DATA_DIR must be consulted BEFORE the Linux platform default, "
        "or units fall back to ~/.config/nunba and the data root splits again")


def test_the_embedded_probe_still_points_somewhere_else():
    """A guard, not an endorsement.

    platform_paths' /etc/hartos-release branch resolves to /var/lib/hartos,
    which is NOT hart.dataDir (/var/lib/hart). Creating that file on a node
    would relocate every data path at once. This test documents the trap and
    fails if someone 'fixes' the branch to point at the real dataDir without
    also removing the file-based detection, which would give the node two
    mechanisms for one decision.
    """
    src = _read(_PLATFORM)
    m = re.search(r"/etc/hartos-release.*?_cached_data_dir\s*=\s*'([^']+)'",
                  src, re.S)
    if not m:
        return  # branch removed entirely: fine, one less mechanism
    assert m.group(1) == "/var/lib/hartos", (
        "the embedded-OS branch changed target. If it now points at the real "
        "hart.dataDir, remove it: HARTOS_DATA_DIR already covers every unit, "
        "and two mechanisms deciding one path is how the split started")


def test_no_unit_hardcodes_a_competing_data_root():
    """A unit setting HARTOS_DATA_DIR to a literal would re-create the split."""
    for nix in (_NIXOS / "modules").glob("*.nix"):
        src = _read(nix)
        for m in re.finditer(r"HARTOS_DATA_DIR\s*=\s*([^;]+);", src):
            value = m.group(1).strip()
            assert "cfg.dataDir" in value or "config.hart.dataDir" in value, (
                "%s sets HARTOS_DATA_DIR to %s; it must derive from the "
                "hart.dataDir option so every unit moves together"
                % (nix.name, value))
