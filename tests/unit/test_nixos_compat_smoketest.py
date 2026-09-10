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
    assert re.search(r"TimeoutStartSec\s*=\s*toString startTimeout", svc), (
        "TimeoutStartSec must be DERIVED from the per-probe budgets (toString "
        "startTimeout), never hand-written: a literal 360 against inner bounds "
        "summing to 390 meant a run where every probe merely reached its own "
        "limit could not finish inside the unit's limit, so systemd SIGKILLed "
        "the unit and the status file stayed empty. The invariant that replaces "
        "the number is test_outer_bound_exceeds_the_sum_of_the_probe_bounds.")


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
    assert re.search(r"probe \$\{toString budget\.wine\} wine", src), (
        "the wine probe must run through the bounded `probe` helper so a "
        "cold-prefix init cannot hang, and so its bound is counted into the "
        "unit's derived TimeoutStartSec")
    assert re.search(r"wineserver -k", src), (
        "the wine probe must tear down wineserver afterwards: Wine's daemons "
        "outlive the `wine` process by design, and a lingering wineserver keeps "
        "the unit's cgroup populated so systemd has to SIGKILL it at exit "
        "(observed on real HW 2026-09-10, seven processes at a time)")


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
    assert re.search(r"probe \$\{toString budget\.darling\} darling", src), (
        "the darling probe must run through the bounded `probe` helper "
        "(experimental + heavy)")


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


# ─────────────────────────────────────────────────────────────────────────────
# 6. The two defects that left the status file EMPTY on real hardware.
#    Both are pinned as invariants rather than numbers, so neither can come
#    back by drift.
# ─────────────────────────────────────────────────────────────────────────────

def _budget() -> dict:
    """Parse the module's per-probe time budgets out of its `let` block."""
    src = _read(_SMOKE)
    m = re.search(r"budget\s*=\s*\{(.*?)\};", src, re.S)
    assert m, "the module must declare a `budget` attrset of per-probe bounds"
    return {k: int(v) for k, v in re.findall(r"(\w+)\s*=\s*(\d+);", m.group(1))}


def test_outer_bound_exceeds_the_sum_of_the_probe_bounds():
    """The unit must be able to survive its own worst case.

    THE BUG, measured on real HW 2026-09-10: TimeoutStartSec was a hand-written
    360 while the inner bounds summed to 390 (120 wine + 30 waydroid-status +
    60 waydroid-shell + 120 darling + 30 flatpak + 30 fsprobe). A run in which
    every probe merely reached its OWN limit therefore could not finish inside
    the unit's limit. systemd SIGKILLed the unit twice in a row with
    `Failed with result timeout`, and /run/hart/compat-status was left EMPTY:
    not one honest verdict, which is the single outcome this module exists to
    prevent. A module that measures nothing is worse than one that measures
    badly, because it reads as though the runtimes were never asked.

    So the outer bound is derived now, and this is the invariant behind it.
    """
    src = _read(_SMOKE)
    budget = _budget()
    assert budget, "the budget attrset must not be empty"

    # The sum must be computed by the module, not restated by hand.
    assert re.search(r"probeBudget\s*=\s*lib\.foldl'.*lib\.attrValues budget", src), (
        "probeBudget must be summed from the budget attrset itself, so that "
        "adding a probe automatically widens the unit's ceiling")

    m = re.search(r"startTimeout\s*=\s*probeBudget\s*\+\s*(\d+);", src)
    assert m, "startTimeout must be probeBudget plus a headroom constant"
    headroom = int(m.group(1))
    assert headroom > 0, (
        "the outer bound needs headroom over the probe sum for the script's own "
        "plumbing: the prefix mkdir, the wineserver teardown, and the "
        "SIGTERM-then-SIGKILL grace that each bound allows")

    # And the derived ceiling must be what the unit actually uses.
    assert re.search(r"TimeoutStartSec\s*=\s*toString startTimeout", src), (
        "the unit must use the derived ceiling, not a literal")


def test_no_probe_captures_through_a_command_substitution():
    """A probe's time bound must actually bound it.

    THE BUG, measured on real HW 2026-09-10: the probes captured output with
    `OUT="$(timeout N ...)"`. A command substitution reads the pipe until EOF,
    and EOF only arrives once EVERY holder of the write end has closed it. Wine
    forks wineserver and winedevice.exe, which inherit that write end and
    deliberately outlive the `wine` process, so `timeout` killed wine exactly on
    schedule while the substitution went on blocking on the daemons. The bound
    silently stopped bounding. Same prefix, same command, back to back:

        timeout 20 wine cmd /c "echo HARTOK"   via $(...)   returned after 41s
        timeout 20 wine cmd /c "echo HARTOK"   via a file   returned after 20s

    Capturing to a file needs no such handshake, so the bound written is the
    bound enforced. This guard is what keeps the pipe from coming back.
    """
    src = _read(_SMOKE)
    assert '"$(timeout' not in src, (
        "no probe may capture through a command substitution: a detached daemon "
        "inheriting the pipe's write end holds it open past the kill, which "
        "makes the timeout a lie. Use the `probe` helper, which captures to a "
        "file.")
    assert re.search(r'timeout -k 5 "\$secs" "\$@" >"\$PROBE_TMP" 2>&1 </dev/null',
                     src), (
        "the probe helper must redirect the probe's output to a FILE and its "
        "stdin from /dev/null, and use -k so an ignored SIGTERM becomes a KILL")


def test_every_runtime_probe_goes_through_the_bounded_helper():
    """One bounding mechanism, applied everywhere. A probe that hand-rolls its
    own escapes the derived ceiling and the file-capture fix at the same time."""
    body = _read(_SMOKE)
    body = body[body.index("smokeScript ="):]
    for tool in ("wine", "waydroid status", "waydroid shell", "darling", "flatpak"):
        assert re.search(r"probe \$\{toString budget\.\w+\} " + re.escape(tool),
                         body), (
            "the " + tool + " probe must run through the `probe` helper so its "
            "bound is both enforced and counted into TimeoutStartSec")


# ─────────────────────────────────────────────────────────────────────────────
# 7. The embedded shell script must actually PARSE.
# ─────────────────────────────────────────────────────────────────────────────

def _render_smoke_script() -> str:
    """Render the Nix-embedded shell script the way Nix would.

    The script lives inside a Nix `''` string, so nothing on the Python side
    ever sees it as shell. Substitute the interpolations with representative
    values and unescape `''${` back to `${`, and what comes out is the text the
    unit will actually execute.
    """
    src = _read(_SMOKE)
    open_marker = 'pkgs.writeShellScript "hart-compat-smoketest" ' + "''"
    start = src.index(open_marker) + len(open_marker)
    end = src.index("''" + ";", start)
    body = src[start:end]

    # `''${` is the Nix escape for a literal `${` -- shell, not interpolation.
    sentinel = "\x00SHELLBRACE\x00"
    body = body.replace("''" + "${", sentinel)

    # Everything still spelled ${...} IS a Nix interpolation. Substitute each
    # with a value of the right shape so the result is runnable shell.
    subs = {
        "${binPath}": "/nix/store/stub-coreutils/bin",
        "${statusFile}": "/run/hart/compat-status",
        '${lib.concatStringsSep " " config.hart.storage.filesystems}':
            "ntfs exfat vfat ext4 btrfs",
    }
    for k, v in subs.items():
        body = body.replace(k, v)
    for name, value in _budget().items():
        body = body.replace("${toString budget." + name + "}", str(value))

    leftover = re.findall(r"\$\{[^}]*\}", body)
    assert not leftover, (
        "unhandled Nix interpolation in the smoke script, so this guard would "
        "be checking the wrong text: " + repr(leftover))

    return body.replace(sentinel, "${")


def test_rendered_script_is_valid_shell():
    """`bash -n` the script Nix will actually write.

    Worth its own guard: the script is a string inside a Nix expression, so a
    syntax error in it survives `nix-instantiate --parse` (the NIX parses fine),
    survives every source-shape assertion above, and only surfaces when the unit
    runs on a real machine -- where its whole job is to write a status file, and
    a shell that will not parse writes nothing at all. That is the same silent
    empty-status outcome the timeout bug produced, arriving by a different road.
    """
    shell = _shell()
    if not shell:
        pytest.skip("no POSIX shell available to syntax-check the script")
    rendered = _render_smoke_script()
    # BYTES on stdin, not a path and not text: the module's comments are full of
    # box-drawing characters a cp1252 dev host cannot encode, and Windows
    # newline translation would turn `}` into `}\r`, which no longer terminates
    # a block. Both produce alarming syntax errors for a perfectly good script.
    r = subprocess.run([shell, "-n"], input=rendered.encode("utf-8"),
                       capture_output=True, timeout=60)
    assert r.returncode == 0, (
        "the rendered smoke script is not valid shell:\n"
        + r.stderr.decode("utf-8", "replace")[:600])


def test_rendered_script_bounds_every_probe_it_runs():
    """Behavioural, not textual: walk the rendered script and confirm no line
    invokes a foreign-OS runtime as its own command word. Anything that does has
    escaped both the enforced time bound and the derived TimeoutStartSec, which
    is exactly how the unit came to be SIGKILLed with an empty status file."""
    rendered = _render_smoke_script()
    runtimes = {"wine", "waydroid", "darling", "flatpak", "appimage-run"}
    for line in rendered.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        # Strip shell keywords that can precede a command word on one line.
        tokens = stripped.split()
        while tokens and tokens[0] in ("if", "elif", "while", "until", "then",
                                       "else", "do", "!"):
            tokens = tokens[1:]
        if not tokens:
            continue
        assert tokens[0] not in runtimes, (
            "this line invokes a foreign-OS runtime directly, outside the "
            "bounded `probe` helper, so its time bound is neither enforced nor "
            "counted into TimeoutStartSec: " + stripped)
