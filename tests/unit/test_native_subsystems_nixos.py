"""
NixOS native-subsystems runtime source-guards — genuine app support (Android
Waydroid, browser-extension force-install, honest macOS/Snap).

WHY THESE ARE SOURCE-GUARDS (and that is the correct kind here):
  The actual behaviour — the Waydroid container running real ART/Binder, the
  Chromium/Firefox managed-policy force-install, the never-fail boot ordering —
  is a NixOS eval + VM concern. It CANNOT be evaluated or booted on this Windows
  dev box (no nix, no QEMU). That is the CI/VM gate:
  nixos/tests/native-subsystems.nix (wired into `nix flake check` via flake.nix),
  which BOOTS a node and asserts the runtime is real (waydroid-container.service
  in the closure, the `sleep infinity` stub GONE, the policy files on disk, no
  fake snapd). This file guards the dev-box-verifiable STRUCTURAL invariants of
  the module SOURCE so a regression that deletes the Waydroid wiring or
  resurrects the fake-success stub is caught WITHOUT nix. This is the acceptable
  cross-file source-guard class named in memory/feedback_no_grep_tests.md, and it
  is clearly labelled (test_source_guard_*). The VM test proves the behaviour;
  this file guards the shape.

Run:
  pytest tests/unit/test_native_subsystems_nixos.py -v --noconftest -p no:capture
"""

import os

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
MODULES_DIR = os.path.join(REPO_ROOT, "nixos", "modules")
TESTS_DIR = os.path.join(REPO_ROOT, "nixos", "tests")


def _read(path):
    with open(path, "r", encoding="utf-8") as f:
        return f.read()


def _strip_nix_comments(src):
    """Drop `#` comment lines + trailing `#` comments so guards assert on the
    ACTUAL Nix bindings, not on the (intentionally explanatory) prose that
    legitimately names the very things we want absent from the config (e.g. a
    comment explaining the deleted `sleep infinity` stub, or why snapd is
    infeasible). A `#` inside a string literal is rare in these modules; the
    guards that need string content read the raw source instead."""
    out = []
    for line in src.splitlines():
        stripped = line.lstrip()
        if stripped.startswith("#"):
            continue
        # Trailing inline comment (best-effort; modules here don't put `#` in
        # string literals on config-binding lines).
        if "#" in line:
            line = line.split("#", 1)[0]
        out.append(line)
    return "\n".join(out)


def _subsystems():
    return _read(os.path.join(MODULES_DIR, "hart-subsystems.nix"))


def _subsystems_code():
    """The module with comments stripped — for absence guards."""
    return _strip_nix_comments(_subsystems())


def _flake():
    return _read(os.path.join(REPO_ROOT, "nixos", "flake.nix"))


# ═══════════════════════════════════════════════════════════════
# 1. ANDROID — REAL Waydroid runtime, the sleep-infinity lie is GONE
# ═══════════════════════════════════════════════════════════════

class TestSourceGuardAndroidWaydroid:
    def test_source_guard_binds_stock_waydroid_module(self):
        src = _subsystems()
        assert "virtualisation.waydroid.enable = true" in src, (
            "Android must bind the stock virtualisation.waydroid module — a real "
            "AOSP/ART/Binder container, not a parallel inert daemon"
        )

    def test_source_guard_sleep_infinity_stub_deleted(self):
        # Comment-stripped: the module's prose legitimately NAMES the deleted stub
        # ("NOT the old `exec sleep infinity` stub"); the guard is that no live
        # config carries it.
        code = _subsystems_code()
        assert "exec sleep infinity" not in code, (
            "the fake-success Android runtime (exec sleep infinity payload that "
            "reported 'ready' while running no ART) must be deleted"
        )
        assert "hart-android-runtime" not in code, (
            "the old inert hart-android-runtime service must be gone"
        )

    def test_source_guard_android_asserts_kernel_binder(self):
        src = _subsystems()
        # The eval-loud prerequisite: Android needs the kernel Binder bits.
        assert "assertion = cfg.kernel.enable && cfg.kernel.androidNative.enable" in src, (
            "Android must assert hart.kernel.enable && androidNative (Binder) so a "
            "misconfig fails eval LOUDLY instead of booting an inert runtime"
        )

    def test_source_guard_waydroid_init_cannot_block_activation(self):
        src = _subsystems()
        # The first-boot image init must not be able to wedge boot OR an OTA.
        assert "hart-waydroid-init" in src, "missing the Waydroid first-boot image init"
        # Type=simple, NOT oneshot. This guard used to demand oneshot and call it
        # non-blocking, which is backwards: a oneshot start job only completes when
        # ExecStart exits, and switch-to-configuration waits on the units it starts.
        # Measured on the box 2026-08-24: a ~10-minute sourceforge image download
        # held the OTA activation open until systemd killed the unit at its start
        # timeout, and that single failure made nixos-rebuild report the whole
        # switch as failed and roll back a healthy generation.
        assert 'Type = "simple"' in src, (
            "waydroid init must be Type=simple so its start job completes on fork; "
            "oneshot blocks activation for the whole image download and fails OTAs")
        assert 'Type = "oneshot"' not in src.split("hart-waydroid-init")[1][:1200], (
            "the waydroid init unit must not be oneshot")
        assert "RemainAfterExit = true" in src
        # A start timeout is what actually broke the OTA (SIGTERM at the deadline
        # never reaches the script's own `exit 0`), so bound the process instead.
        assert "RuntimeMaxSec" in src, (
            "waydroid init must bound a hung mirror with RuntimeMaxSec, not "
            "TimeoutStartSec — the latter kills the unit and fails the transaction")
        # Pulled in by hart.target (a plain HART child), NOT graphical (the
        # ordering-cycle lesson the old Android runtime learned).
        assert 'wantedBy = [ "hart.target" ]' in src
        # Idempotency guard on the system image.
        assert "ConditionPathExists = \"!/var/lib/waydroid/images/system.img\"" in src

    def test_source_guard_waydroid_init_tolerates_no_network(self):
        src = _subsystems()
        # Must not fail the boot transaction when the image download is impossible
        # (mirrors hart-flathub-init's `|| true` tolerance).
        assert "exit 0" in src, "waydroid init must exit 0 even when the download is skipped"

    def test_source_guard_does_not_redeclare_inotify_sysctl(self):
        # eval-gate lesson #1: hart-kernel already forces
        # fs.inotify.max_user_watches; a second declaration collides at eval.
        # (Comment-stripped: the module's prose explains WHY it must not re-declare.)
        code = _subsystems_code()
        assert "max_user_watches" not in code, (
            "hart-subsystems must NOT re-declare fs.inotify.max_user_watches — "
            "hart-kernel already forces it; a second declaration reds the eval gate"
        )


# ═══════════════════════════════════════════════════════════════
# 2. WEB — browser-extension force-install surface (Chromium + Firefox)
# ═══════════════════════════════════════════════════════════════

class TestSourceGuardBrowserExtension:
    def test_source_guard_chromium_force_install_list_declared(self):
        src = _subsystems()
        assert "ExtensionInstallForcelist" in src, (
            "web must declare the Chromium ExtensionInstallForcelist managed "
            "policy — the real .crx force-install surface"
        )

    def test_source_guard_writable_chromium_policy_file_for_installer(self):
        src = _subsystems()
        assert "chromium/policies/managed/hart-extensions.json" in src, (
            "allowExtensions must expose a writable installer-owned Chromium "
            "managed-policy file the installer drops {id,update_url} into"
        )
        # group=hart so the installer (running as hart) can rewrite + read it back.
        assert 'group = "hart"' in src

    def test_source_guard_firefox_extension_settings_policy(self):
        src = _subsystems()
        assert "firefox/policies/policies.json" in src, (
            "web must expose the Firefox enterprise policy file for .xpi "
            "force-install (ExtensionSettings)"
        )
        assert "ExtensionSettings" in src

    def test_source_guard_allow_extensions_option_exists(self):
        src = _subsystems()
        assert "allowExtensions" in src, (
            "hart.subsystems.web.allowExtensions option must exist (gates the "
            "writable managed-policy surface)"
        )


# ═══════════════════════════════════════════════════════════════
# 3. MACOS — honest opt-in, eval-loud only when enabled
# ═══════════════════════════════════════════════════════════════

class TestSourceGuardMacos:
    def test_source_guard_macos_asserts_darling_present_when_enabled(self):
        src = _subsystems()
        assert "assertion = pkgs ? darling" in src, (
            "enabling macOS on a rev without darling must fail eval loudly"
        )

    def test_source_guard_macos_disabled_path_stays_eval_safe(self):
        src = _subsystems()
        # The optional keeps the DISABLED path safe everywhere (no darling => no pkg).
        assert "lib.optional (pkgs ? darling) pkgs.darling" in src


# ═══════════════════════════════════════════════════════════════
# 4. SNAP — honestly unsupported, NO fake module
# ═══════════════════════════════════════════════════════════════

class TestSourceGuardSnap:
    def test_source_guard_no_snap_enable_option(self):
        # Comment-stripped: the honest NOTE block discusses snap at length; the
        # guard is that no live OPTION/CONFIG binding exists.
        code = _subsystems_code()
        assert "snap.enable" not in code and "snap = {" not in code, (
            "there must be NO hart.subsystems.snap option — native snapd is "
            "infeasible; the installer refuses .snap honestly instead"
        )

    def test_source_guard_no_services_snap_module(self):
        code = _subsystems_code()
        # No third-party nix-snapd / services.snap was silently added (the note
        # block that NAMES them is a comment, stripped above).
        assert "services.snap" not in code and "snapd" not in code, (
            "no fake snapd module may be shipped (would be an unpinned out-of-tree "
            "flake input — a steward decision, not a silent module)"
        )

    def test_source_guard_snap_infeasibility_documented(self):
        src = _subsystems()
        # The honest deliverable: a comment explaining why + the fallback.
        assert "INFEASIBLE" in src.upper() or "infeasible" in src
        assert "Flatpak" in src, "the honest snap note must point at the Flatpak/Nix fallback"


# ═══════════════════════════════════════════════════════════════
# 5. The VM test is WIRED into the CI gate (a test that never runs guards nothing)
# ═══════════════════════════════════════════════════════════════

class TestSourceGuardVmTestWired:
    def test_source_guard_native_subsystems_vm_test_exists(self):
        assert os.path.exists(os.path.join(TESTS_DIR, "native-subsystems.nix")), (
            "the behavioural VM test must exist (it gates in CI, not on Windows)"
        )

    def test_source_guard_vm_test_wired_into_flake_checks(self):
        flake = _flake()
        assert "native-subsystems.nix" in flake, (
            "the VM test must be imported in flake.nix checks (CLAUDE.md Gate 5: a "
            "test that never runs guards nothing)"
        )
        assert "nativeSubsystems" in flake, (
            "the VM test must be merged into the checks // chain so nix flake "
            "check actually runs it"
        )
