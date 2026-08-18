"""Structural + behavioural guards for the cross-OS runtime smoke-test
(hart-compat-smoketest).

HART OS advertises native Windows / Android / macOS / Linux app support
(hart-subsystems.nix). Historically the AppInstaller reported some of those
runtimes "available" UNCONDITIONALLY — a CLAIM, not a measurement. This module
turns the claim into a per-runtime FACT: after boot it actually EXECUTES a tiny
test command (echo HARTOK) inside each ENABLED runtime and writes an honest
per-runtime status to /run/hart/compat-status. So the thing under test is the
HONESTY + NEVER-BLOCK-THE-DESKTOP contract:

  * Each runtime is probed by a REAL exec (wine cmd echo HARTOK; waydroid
    shell echo HARTOK / image check; darling shell echo HARTOK), classified by
    whether HARTOK came back, and the verdict written to /run/hart/compat-status.
  * It runs IN PARALLEL with the desktop — wantedBy multi-user.target, ordered
    AFTER hart-waydroid-init + network-online, and NEVER `before greetd` (it must
    not delay first paint).
  * Fail-safe: `set -uo pipefail` (NOT -e), every probe `command -v`-gated so an
    absent (disabled) subsystem records `skip` not a false `failed`, and the unit
    ALWAYS exits 0 (oneshot + RemainAfterExit) so it can never block/fail boot.

The HARTOK-classification half is a BEHAVIOURAL test, not a string-survival grep:
it extracts the actual `grep -q 'HARTOK'` decision the module uses and runs it
through a real shell against fixtures (output-with-HARTOK => ok; without =>
failed), exactly like test_nixos_gpu_probe.py exercises the eglinfo classifier.
The wiring half is a source-shape guard (acceptable here — a Nix module cannot be
imported/executed on the Windows dev box; the boot-wiring proof is the
native-subsystems nixosTest in CI, which can't run on Windows).
"""

import pathlib
import re
import shutil
import subprocess

import pytest

_NIXOS = pathlib.Path(__file__).resolve().parents[2] / "nixos"
_MODULES = _NIXOS / "modules"
_SMOKE = _MODULES / "hart-compat-smoketest.nix"
_SUBSYS = _MODULES / "hart-subsystems.nix"
_FLAKE = _NIXOS / "flake.nix"

_STATUS_PATH = "/run/hart/compat-status"


def _read(p: pathlib.Path) -> str:
    return p.read_text(encoding="utf-8")


# ─────────────────────────────────────────────────────────────────────────────
# 1. The module exists, is imported, and defines the option + the oneshot.
# ─────────────────────────────────────────────────────────────────────────────

def test_module_exists():
    assert _SMOKE.is_file(), "nixos/modules/hart-compat-smoketest.nix is missing"


def test_module_is_imported_in_flake():
    flake = _read(_FLAKE)
    assert "./modules/hart-compat-smoketest.nix" in flake, (
        "hart-compat-smoketest.nix must be added to the nixos/flake.nix "
        "hartModules[] import list (next to hart-subsystems.nix / hart-gpu-probe.nix) "
        "— otherwise the hart.subsystems.smoketest option never exists and the "
        "smoke-test unit never ships."
    )


def test_imported_next_to_subsystems():
    """Sibling-comment style: the import should sit beside hart-subsystems.nix
    (the module that wires the runtimes it probes), per the task's placement."""
    flake = _read(_FLAKE)
    sub_idx = flake.index("./modules/hart-subsystems.nix")
    smoke_idx = flake.index("./modules/hart-compat-smoketest.nix")
    # Right after hart-subsystems (within a few lines / the comment block).
    assert 0 < (smoke_idx - sub_idx) < 1200, (
        "hart-compat-smoketest.nix should be imported adjacent to "
        "hart-subsystems.nix (it smoke-tests the runtimes that module wires)")


def test_defines_smoketest_enable_option_default_true():
    src = _read(_SMOKE)
    # hart.subsystems.smoketest.enable must exist, be a bool, and default true.
    assert re.search(r"options\.hart\.subsystems\.smoketest", src), (
        "the module must define options.hart.subsystems.smoketest")
    m = re.search(r"enable\s*=\s*lib\.mkOption\s*\{(.*?)\};", src, re.S)
    assert m, "hart.subsystems.smoketest.enable must be a lib.mkOption"
    body = m.group(1)
    assert re.search(r"type\s*=\s*lib\.types\.bool", body), (
        "hart.subsystems.smoketest.enable must be lib.types.bool")
    assert re.search(r"default\s*=\s*true", body), (
        "hart.subsystems.smoketest.enable must default to true")


def test_config_gated_on_three_toggles():
    """Config must be gated on the hart master toggle AND the subsystems master
    toggle AND this smoke-test toggle (cfg.enable && sub.enable &&
    sub.smoketest.enable) so it's a pure no-op when any is off."""
    src = _read(_SMOKE)
    m = re.search(r"config\s*=\s*lib\.mkIf\s*\((.*?)\)\s*\{", src, re.S)
    assert m, "the module must gate config on a lib.mkIf"
    guard = m.group(1)
    assert "cfg.enable" in guard, "config must be gated on cfg.enable (hart master)"
    assert "sub.enable" in guard, (
        "config must be gated on sub.enable (the subsystems master toggle)")
    assert "smoketest.enable" in guard, (
        "config must be gated on sub.smoketest.enable")


# ─────────────────────────────────────────────────────────────────────────────
# 2. UNIT SHAPE: parallel with the desktop (NOT before greetd), never-fail.
# ─────────────────────────────────────────────────────────────────────────────

def test_unit_runs_in_parallel_not_before_greetd():
    # INTENT (unchanged since this test was written): the smoke-test runs on
    # every boot cycle AND can never delay first paint. The MECHANISM changed
    # 2026-08-14: as a multi-user.target member it cold-started a Wine prefix
    # DURING bring-up (real-HW trial: start timed out at 360s, winedevice
    # SIGKILLed while the desktop settled), so activation moved to a timer
    # 10 minutes after boot. The timer is strictly stronger for this test's
    # intent: off the boot transaction entirely, it CANNOT delay paint.
    src = _read(_SMOKE)
    m = re.search(
        r"systemd\.services\.hart-compat-smoketest\s*=\s*\{(.*?)\n    \};",
        src, re.S)
    assert m, "the module must define systemd.services.hart-compat-smoketest"
    svc = m.group(1)

    # The SERVICE must NOT be in any boot target: activation is the timer's job.
    assert "wantedBy" not in svc, (
        "the smoke-test service must have NO wantedBy — a boot-target membership "
        "puts the Wine cold-start back inside bring-up, which is exactly what the "
        "2026-08-14 real-HW trial showed timing out mid-boot")

    # A timer must exist and fire a bounded time after boot, so the probe still
    # runs every boot cycle (the 'runs on a normal boot' half of the intent).
    tm = re.search(
        r"systemd\.timers\.hart-compat-smoketest\s*=\s*\{(.*?)\n    \};",
        src, re.S)
    assert tm, "the smoke-test must define its activation timer"
    assert 'wantedBy = [ "timers.target" ]' in tm.group(1), (
        "the timer must be wantedBy timers.target so it arms on every boot")
    assert re.search(r'OnBootSec\s*=\s*"\d+min"', tm.group(1)), (
        "the timer must fire a bounded number of minutes after boot")

    # …and MUST NOT delay the desktop: never `before greetd`.
    assert "greetd" not in svc, (
        "the smoke-test must NOT reference greetd at all — it must NEVER be "
        "`before greetd` (Wine/Waydroid/Darling cold-start would delay first paint).")
    assert not re.search(r"before\s*=", svc), (
        "the smoke-test must declare NO `before =` ordering — it must never gate "
        "anything (especially not the greeter / desktop).")

    # Ordered AFTER the runtimes' host target, the Waydroid image init, and
    # network-online (best-effort).
    assert re.search(r'after\s*=\s*\[[^\]]*"hart\.target"', svc), (
        "the smoke-test must run after hart.target (the runtimes' host services)")
    assert re.search(r'after\s*=\s*\[[^\]]*"hart-waydroid-init\.service"', svc), (
        "the smoke-test must run after hart-waydroid-init.service so the AOSP "
        "image had a chance to download before the android probe")
    assert re.search(r'after\s*=\s*\[[^\]]*"network-online\.target"', svc), (
        "the smoke-test must be ordered after network-online.target")
    assert re.search(r'wants\s*=\s*\[[^\]]*"network-online\.target"', svc), (
        "network-online must be WANTED (best-effort) so a no-network boot still "
        "runs the smoke-test")


def test_unit_is_nonfatal_oneshot_as_hart_user():
    src = _read(_SMOKE)
    m = re.search(
        r"systemd\.services\.hart-compat-smoketest\s*=\s*\{(.*?)\n    \};",
        src, re.S)
    assert m
    svc = m.group(1)
    # oneshot + RemainAfterExit + User=hart + bounded timeout = never blocks/fails boot.
    assert re.search(r'Type\s*=\s*"oneshot"', svc), "must be a oneshot"
    assert re.search(r"RemainAfterExit\s*=\s*true", svc), (
        "must RemainAfterExit=true so it never re-runs / blocks")
    assert re.search(r'User\s*=\s*"hart"', svc), "must run as User=hart"
    assert re.search(r'TimeoutStartSec\s*=\s*"360"', svc), (
        "must set a bounded TimeoutStartSec (360) so a wedged probe can't wedge boot")


def test_tmpfiles_run_hart_dir():
    src = _read(_SMOKE)
    assert re.search(
        r'"d /run/hart 0750 hart hart -"', src), (
        "the module must declare the /run/hart tmpfiles rule (de-dups with siblings)")


# ─────────────────────────────────────────────────────────────────────────────
# 3. FAIL-SAFE script shape: set -uo pipefail (NOT -e), always exit 0,
#    command-v gating per subsystem, truncates + writes the status file.
# ─────────────────────────────────────────────────────────────────────────────

def test_script_is_failsafe_set_u_not_e():
    src = _read(_SMOKE)
    # set -uo pipefail (NOT -e): a probe failing must record its status, not abort.
    assert re.search(r"^\s*set -uo pipefail\s*$", src, re.M), (
        "the script must use `set -uo pipefail` (NOT `set -e`) so a failing probe "
        "records `failed` instead of aborting the whole run")
    assert not re.search(r"^\s*set -e", src, re.M), (
        "the script must NOT use `set -e` — a probe failing must never abort")
    # Always exits 0 — measurement, never a gate.
    assert re.search(r"^\s*exit 0\s*$", src, re.M), (
        "the script must always `exit 0` so the unit can never fail the boot")


def test_script_writes_and_truncates_status_file():
    src = _read(_SMOKE)
    assert _STATUS_PATH in src, (
        f"the script must write its verdicts to {_STATUS_PATH}")
    # Truncate first (fresh measurement, never appended to a stale file).
    assert re.search(r':\s*>\s*"\$STATUS"', src), (
        "the script must truncate the status file first (`: > \"$STATUS\"`) — a "
        "fresh measurement every boot, never appended to a stale file")
    # Each runtime line is key=value AND echoed to the journal.
    assert re.search(r"printf '%s=%s\\n'", src), (
        "each runtime must be recorded as a key=value line in the status file")
    assert re.search(r"\[hart-compat-smoketest\]", src), (
        "each runtime verdict must be echoed to the journal "
        "([hart-compat-smoketest] <runtime> = <status>)")


def test_path_exports_system_path_for_runtime_tools():
    src = _read(_SMOKE)
    # The per-subsystem runtime tools land in the SYSTEM path only when enabled;
    # PATH must include /run/current-system/sw/bin so `command -v wine` etc. find them.
    assert "/run/current-system/sw/bin" in src, (
        "the script must export /run/current-system/sw/bin on PATH so the "
        "per-subsystem runtime tools (wine/waydroid/darling/flatpak/appimage-run) "
        "are found when their subsystem is enabled")


def test_each_runtime_is_command_v_gated_to_skip_when_absent():
    """`command -v <tool>` gates each probe so a DISABLED subsystem (tool absent)
    records `skip`, never a false `failed`."""
    src = _read(_SMOKE)
    for tool in ("wine", "waydroid", "darling", "flatpak", "appimage-run"):
        assert re.search(rf"command -v {re.escape(tool)}\b", src), (
            f"the {tool} probe must be gated on `command -v {tool}` so an absent "
            f"(disabled-subsystem) tool records `skip`, not a false `failed`")
    # The skip status must be a real branch (absent tool => skip).
    assert re.search(r"record \w+ skip", src), (
        "an absent runtime tool must `record <runtime> skip`")


# ─────────────────────────────────────────────────────────────────────────────
# 4. REAL EXEC per runtime: each runtime is probed by actually running echo HARTOK
#    (wine cmd / waydroid shell / darling shell) — NOT a claim.
# ─────────────────────────────────────────────────────────────────────────────

def test_windows_probe_is_real_wine_exec():
    src = _read(_SMOKE)
    # `wine cmd /c "echo HARTOK"` — a REAL Win32 exec, network-free via the DLL
    # overrides, under a timeout, in a dedicated prefix.
    assert re.search(r'wine cmd /c "echo HARTOK"', src), (
        "windows must be probed by a REAL `wine cmd /c \"echo HARTOK\"` exec")
    assert "WINEDLLOVERRIDES=" in src, (
        "the wine probe must set WINEDLLOVERRIDES to skip the mono/gecko download "
        "prompts (network-free)")
    assert re.search(r"WINEPREFIX=/var/lib/hart/wine/smoke", src), (
        "the wine probe must use a dedicated WINEPREFIX under hart-subsystems' "
        "/var/lib/hart/wine (mkdir -p the /smoke subdir)")
    assert re.search(r"timeout 120 wine", src), (
        "the wine probe must be wrapped in a timeout so a cold-prefix init can't hang")


def test_android_probe_checks_image_and_real_shell_exec():
    src = _read(_SMOKE)
    # Image-existence gate: present => probe; absent => no-image.
    assert "/var/lib/waydroid/images/system.img" in src, (
        "the android probe must check the AOSP image at "
        "/var/lib/waydroid/images/system.img")
    assert re.search(r"record android no-image", src), (
        "an absent AOSP image must `record android no-image` (init hasn't "
        "downloaded it / no network yet)")
    # Running session => REAL exec; image present but no session => ready (do NOT
    # force-boot the heavy AOSP container).
    assert re.search(r"waydroid status", src), (
        "the android probe must check `waydroid status` for a RUNNING session")
    assert re.search(r"waydroid shell echo HARTOK", src), (
        "a running Waydroid session must be probed by a REAL "
        "`waydroid shell echo HARTOK` exec")
    assert re.search(r"record android ready", src), (
        "image-present-but-no-session must `record android ready` — a real launch "
        "would start it; the smoke-test must NOT force-boot AOSP")


def test_macos_probe_is_real_darling_exec():
    src = _read(_SMOKE)
    assert re.search(r"darling shell echo HARTOK", src), (
        "macos must be probed by a REAL `darling shell echo HARTOK` exec")
    assert re.search(r"timeout 120 darling", src), (
        "the darling probe must be wrapped in a timeout (experimental + heavy)")


def test_linux_flatpak_appimage_statuses():
    src = _read(_SMOKE)
    # Linux native => always ok.
    assert re.search(r"record linux ok", src), (
        "linux (native) must always `record linux ok`")
    # flatpak --version => ok else skip.
    assert re.search(r"flatpak --version", src), (
        "flatpak must be probed by `flatpak --version` => ok, else skip")
    # appimage-run presence => ok else skip.
    assert re.search(r"record appimage ok", src), (
        "appimage-run present must `record appimage ok`")


# ─────────────────────────────────────────────────────────────────────────────
# 5. BEHAVIOURAL: the HARTOK classification itself.
#    Extract the real `grep -q 'HARTOK'` decision from the module and run it
#    through a real shell against fixtures (with HARTOK => ok; without => failed).
# ─────────────────────────────────────────────────────────────────────────────

# Foreign-OS exec outputs: the first set CONTAINS HARTOK (the runtime executed our
# command), the second set does NOT (failed / hung / tool error).
_HARTOK_FIXTURES = [
    "HARTOK",
    "HARTOK\r\n",                      # Wine cmd CRLF
    "Z:\\>echo HARTOK\nHARTOK\n",      # Wine cmd echoes the command + the output
    "some noise\nHARTOK\nmore noise",  # buried in runtime chatter
]
_NO_HARTOK_FIXTURES = [
    "",                                                  # empty (tool missing / timed out)
    "wine: could not load kernel32.dll",                 # Wine init failure
    "Segmentation fault",                                # crashed
    "darling: failed to set up the prefix",              # Darling failure
    "/bin/sh: line 1: echo: command not found",          # nonsense
]


def _extract_classifier() -> str:
    """Pull the exact `printf ... | grep -q 'HARTOK'` decision out of the module so
    the test exercises the REAL classification, not a copy."""
    src = _read(_SMOKE)
    m = re.search(r"(printf '%s' \"\$\w+\" \| grep -q 'HARTOK')", src)
    assert m, "could not locate the HARTOK classification pipeline"
    return m.group(1)


def _classify(shell: str, exec_output: str) -> str:
    """Run the extracted classifier under a real shell against the runtime output;
    return ok|failed (matching the module's `record <runtime> ok|failed`)."""
    cond = _extract_classifier()
    # Substitute the captured-output var the classifier reads ($WIN_OUT / $MAC_OUT /
    # $WD_OUT) with our fixture var so the SAME pipeline runs against the fixture.
    cond_var = re.search(r'"\$(\w+)"', cond).group(1)
    script = (
        f'{cond_var}="$EXECOUT"\n'
        f"if {cond}; then RESULT=ok; else RESULT=failed; fi\n"
        'printf "%s" "$RESULT"\n'
    )
    out = subprocess.run(
        [shell, "-c", script],
        env={"EXECOUT": exec_output, "PATH": _os_path()},
        capture_output=True, text=True, timeout=30,
    )
    return out.stdout.strip()


def _os_path() -> str:
    import os
    return os.environ.get("PATH", "")


def _shell():
    for name in ("bash", "sh", "dash"):
        p = shutil.which(name)
        if p:
            return p
    return None


@pytest.mark.parametrize("out", _HARTOK_FIXTURES)
def test_classifier_marks_hartok_output_ok(out):
    shell = _shell()
    if not shell:
        pytest.skip("no POSIX shell available to exercise the classifier")
    assert _classify(shell, out) == "ok", (
        f"runtime output containing HARTOK must classify as ok "
        f"(the runtime executed our command): {out!r}")


@pytest.mark.parametrize("out", _NO_HARTOK_FIXTURES)
def test_classifier_marks_missing_hartok_failed(out):
    shell = _shell()
    if not shell:
        pytest.skip("no POSIX shell available to exercise the classifier")
    assert _classify(shell, out) == "failed", (
        f"runtime output WITHOUT HARTOK must classify as failed "
        f"(the runtime did not execute our command): {out!r}")
