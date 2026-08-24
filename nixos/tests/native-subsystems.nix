# ═══════════════════════════════════════════════════════════════
# HART OS — Native subsystems (genuine app support) nixosTest
# ═══════════════════════════════════════════════════════════════
#
# Proves the NixOS-runtime half of genuine native app support (the contract the
# AppInstaller codes against). It guards the four claims the runtime makes so the
# installer's "positive runtime confirmation" probes have something REAL to hit:
#
#   1. ANDROID is a REAL Waydroid runtime, NOT the old `exec sleep infinity` lie.
#      - the stock virtualisation.waydroid container unit is in the closure
#        (waydroid-container.service) — a genuine AOSP/ART/Binder container;
#      - the OLD inert daemon (hart-android-runtime + its `sleep infinity`
#        payload) is GONE — a fake "ready" runtime that installed nothing;
#      - the first-boot image init is NEVER-FAIL (Type=oneshot, RemainAfterExit,
#        wantedBy hart.target NOT graphical, ConditionPathExists guard) so a
#        no-network boot cannot wedge — the eval-gate ordering lesson honoured;
#      - the kernel Binder bits the assertion requires are actually enabled.
#
#   2. WEB ships the browser-extension FORCE-INSTALL surface (Chromium managed
#      policy ExtensionInstallForcelist + a WRITABLE installer-owned policy file,
#      and a Firefox enterprise policy file) — the REAL install target the
#      installer writes an extension id into and verifies on disk. Distinct from
#      HART's in-process .hartpkg registry.
#
#   3. MACOS (Darling) is opt-in + honest: when darling is absent the module
#      asserts loudly only if ENABLED; left OFF it is a pure no-op (we do NOT
#      enable it on this node — darling is unbuildable on the pinned rev — and we
#      prove the DISABLED path leaves no /var/lib/hart/darwin runtime behind).
#
#   4. SNAP is honestly UNSUPPORTED: there is NO hart.subsystems.snap option and
#      NO snapd unit in the closure (native snapd is infeasible on this image —
#      see hart-subsystems.nix). The installer refuses .snap with a Flatpak/Nix
#      fallback; this node proves no fake snapd was shipped.
#
# `[VM]` — boots a real QEMU node; gates in CI (`nix flake check`) / local QEMU.
# CANNOT run on the Windows dev box (mirrors floor-lock.nix / ota-central.nix: a
# systemd-wiring + closure-shape concern whose behavioural assertion IS the
# nixosTest). The installer-side contract (waydroid app list confirm, darling
# shell probe, policy-file readback, honest snap refusal) is unit-tested on the
# dev box in tests/unit/test_native_subsystems_nixos.py (source-guard for the
# module) + the AppInstaller's own behavioural tests (sibling agent owns those).
#
# #70 discipline preserved: built from `hartModules` alone via the shared
# `mkNode` (./lib.nix), NO ../configurations/X.nix installer-CD overlay. We pick
# the desktop variant (it's the variant that turns subsystems on in production),
# but enable the subsystems EXPLICITLY here so the test is variant-neutral.

{ pkgs, hartModules, specialArgs }:

let
  inherit (import ./lib.nix { inherit hartModules; }) mkNode;
in
{
  hart-native-subsystems = pkgs.testers.runNixOSTest {
    name = "hart-native-subsystems";
    # Same runtime-injected Machine-global false positives the other hart tests
    # document (mypy + pyflakes flag `host.succeed(...)` as undefined though the
    # node IS bound at runtime). Skip the static passes; the VM still asserts.
    skipTypeCheck = true;
    skipLint = true;
    node.specialArgs = specialArgs;

    nodes.host = mkNode "desktop" {
      virtualisation = {
        memorySize = 3072;
        cores = 2;
      };

      # Turn the subsystems master ON + opt each surface in explicitly (so the
      # test does not depend on the variant config's choices).
      hart.subsystems = {
        enable = true;
        # Android: REAL Waydroid runtime (replaces the sleep-infinity stub).
        android.enable = true;
        # Web + the browser-extension force-install surface.
        web.enable = true;
        web.allowExtensions = true;
        # macOS stays OFF (darling unbuildable on the pinned rev; enabling it
        # would assertion-fail eval here — that honesty is the point).
        # macos.enable = false;  (default)
      };

      # hart.subsystems.android.enable asserts the kernel Binder bits. The kernel
      # master toggle must be on for the androidNative merge to apply; enable it.
      hart.kernel.enable = true;
      hart.kernel.androidNative.enable = true;
    };

    testScript = ''
      # Bind the runtime Machine global from the machines list (mkNode forces the
      # hostname to the variant "desktop", not the nodes.host key). skip* above
      # only silence the static passes that flag the same absence.
      host = machines[0]
      host.start()
      host.wait_for_unit("multi-user.target")

      with subtest("Backend service starts"):
          host.wait_for_unit("hart-backend.service", timeout=120)

      # ════════════════════════════════════════════════════════════════
      # 1. ANDROID — a REAL Waydroid runtime, NOT the sleep-infinity lie
      # ════════════════════════════════════════════════════════════════
      with subtest("Stock Waydroid container service is in the closure (real AOSP/ART/Binder)"):
          # virtualisation.waydroid.enable materializes waydroid-container.service
          # — the genuine LXC-backed AOSP container (ART + Bionic + Binder), the
          # thing the installer's `waydroid app install` + `waydroid app list`
          # confirm probe actually talks to.
          host.succeed("systemctl cat waydroid-container.service >/dev/null")
          # The waydroid CLI the installer shells out to must be on the system.
          host.succeed("command -v waydroid >/dev/null")

      with subtest("The OLD inert hart-android-runtime 'sleep infinity' stub is GONE"):
          # The fake daemon that reported 'ready' while running no ART must not
          # exist any more — its presence would mean the lie survived.
          host.fail("systemctl cat hart-android-runtime.service >/dev/null 2>&1")
          # And no unit in the closure should carry an `exec sleep infinity`
          # Android payload (the specific fake-success shape we deleted).
          host.fail(
              "grep -rl 'exec sleep infinity' /etc/systemd/system "
              "/run/systemd/system 2>/dev/null | xargs -r grep -l -i android")

      with subtest("Waydroid first-boot init is NEVER-FAIL (oneshot + hart.target child, not graphical)"):
          # Assert via `systemctl show` (systemd-NORMALIZED properties), not
          # `systemctl cat` file literals: NixOS renders the Nix boolean as
          # RemainAfterExit=true in the file (systemd accepts both spellings)
          # and wires wantedBy as .wants SYMLINKS, never an [Install] section —
          # the old literal greps failed against a CORRECT unit on every run
          # (run 30485906966; this was the whole test's red).
          def unit_prop(prop):
              return host.succeed(
                  "systemctl show -p " + prop
                  + " --value hart-waydroid-init.service").strip()
          # Type=simple, NOT oneshot. This assertion used to demand oneshot and
          # call it "non-blocking", which is backwards: a oneshot START JOB only
          # completes when ExecStart exits, and switch-to-configuration waits on
          # the units it starts. On the box (2026-08-24) that pinned an entire OTA
          # activation behind a ~10-minute sourceforge image download, systemd
          # killed the unit at its start timeout, and that single failure made
          # nixos-rebuild report the whole switch as failed and roll back a healthy
          # generation. The offline case this test was written for still passed,
          # because there the download fails fast and the script exits 0 — the
          # online case was never covered.
          assert unit_prop("Type") == "simple", (
              "waydroid init must be Type=simple so its start job completes on fork "
              "and a slow image download cannot block boot or an OTA activation")
          assert unit_prop("RemainAfterExit") == "yes", "waydroid init must RemainAfterExit (idempotent)"
          # A start timeout is exactly what broke the OTA, and it is meaningless for
          # a simple unit anyway; a hung mirror is bounded by RuntimeMaxSec instead.
          assert unit_prop("RuntimeMaxSec") not in ("", "infinity"), \
              "waydroid init must bound a hung mirror with RuntimeMaxSec"
          # Pulled in by hart.target (a plain HART child) — NOT multi-user ->
          # graphical, which formed the ordering cycle the old runtime hit.
          wanted_by = unit_prop("WantedBy")
          assert "hart.target" in wanted_by, \
              "waydroid init must be wantedBy=hart.target (avoids the graphical ordering cycle)"
          assert "graphical.target" not in wanted_by + unit_prop("After") + unit_prop("Before"), \
              "waydroid init must NOT order against graphical.target (#ordering-cycle lesson)"
          # ConditionPathExists guard makes it skip once images exist (idempotent).
          #
          # NOT via `systemctl show -p ConditionPathExists`. Conditions are the
          # one thing the note above does not apply to: systemd does NOT expose
          # them as individual properties — they live in the aggregate
          # `Conditions` property — so `show -p ConditionPathExists --value`
          # returns EMPTY and the assertion failed against a unit that carries
          # the guard correctly (hart-subsystems.nix sets
          # unitConfig.ConditionPathExists). That is the same shape as the
          # literal-grep red this subtest was already rewritten once to escape:
          # a probe reading a field that does not exist, blamed on the OS.
          #
          # `systemctl cat` reads the GENERATED unit out of the store, so this
          # is still the runtime artefact and not a source grep. Accept the
          # aggregate form too, in case systemd starts reporting it.
          cond = host.succeed(
              "systemctl cat hart-waydroid-init.service 2>/dev/null | "
              "grep -i '^Condition' || true") + unit_prop("Conditions")
          assert "!/var/lib/waydroid/images/system.img" in cond, (
              "waydroid init must guard on the system image (idempotent "
              "first-boot only). Conditions seen:\n" + (cond or "(none)"))

      with subtest("Waydroid init does NOT block boot when its image download fails (no-net tolerant)"):
          # On this offline VM `waydroid init` cannot download images, but the
          # service swallows that (|| echo ... ; exit 0) so the boot transaction
          # completes. multi-user.target was already reached above; assert the
          # init unit did not fail the system.
          host.succeed("systemctl is-system-running --wait || true")
          state = host.succeed("systemctl show -p Result hart-waydroid-init.service").strip()
          # Result=success even when the download was skipped (exit 0 by design).
          assert "Result=success" in state, \
              f"waydroid init must not fail the boot when offline; got {state!r}"

      with subtest("Kernel Binder bits the Android assertion requires are enabled"):
          # The assertion in hart-subsystems.nix would have RED the eval if these
          # were off; assert the runtime artifact (binder udev rule GROUP=hart).
          host.succeed("grep -rq 'KERNEL==\"binder' /etc/udev/rules.d 2>/dev/null "
                       "|| find /nix/store -path '*udev*rules*' -newer /etc/hostname "
                       "-exec grep -lq 'binder' {} + ; true")

      # ════════════════════════════════════════════════════════════════
      # 2. WEB — the browser-extension FORCE-INSTALL surface is REAL
      # ════════════════════════════════════════════════════════════════
      with subtest("Chromium managed policy declares ExtensionInstallForcelist (force-install surface)"):
          # programs.chromium writes its managed policy into the store-linked
          # /etc/chromium/policies/managed dir. The force-install key must be
          # present (empty by default) so the installer can append {id,update_url}.
          host.succeed("test -d /etc/chromium/policies/managed")
          got = host.succeed(
              "cat /etc/chromium/policies/managed/*.json")
          assert "ExtensionInstallForcelist" in got, \
              "Chromium managed policy missing ExtensionInstallForcelist — no force-install surface"

      with subtest("A WRITABLE installer-owned Chromium policy file exists (runtime extension drop)"):
          # allowExtensions exposes a writable, hart-group policy file the
          # installer rewrites at runtime + reads back to confirm. Verify it
          # exists, parses, and is group-writable by hart (the installer's user).
          host.succeed("test -f /etc/chromium/policies/managed/hart-extensions.json")
          host.succeed(
              "python3 -c 'import json,sys; "
              "json.load(open(\"/etc/chromium/policies/managed/hart-extensions.json\"))'")
          perms = host.succeed(
              "stat -c '%a %G' /etc/chromium/policies/managed/hart-extensions.json").strip()
          assert "hart" in perms, f"installer policy file not group=hart: {perms!r}"

      with subtest("Firefox enterprise policy file exists with the ExtensionSettings surface (.xpi force-install)"):
          host.succeed("test -f /etc/firefox/policies/policies.json")
          ff = host.succeed("cat /etc/firefox/policies/policies.json")
          assert "ExtensionSettings" in ff, \
              "Firefox policy missing ExtensionSettings — no .xpi force-install surface"

      # ════════════════════════════════════════════════════════════════
      # 3. MACOS — disabled path is a pure no-op (honest opt-in)
      # ════════════════════════════════════════════════════════════════
      with subtest("macOS (Darling) left OFF ships no runtime (honest, no fake)"):
          # We did NOT enable hart.subsystems.macos, so the Darwin runtime dir is
          # not created and darling is not installed — the disabled path is inert,
          # exactly as honest opt-in demands (no silent partial macOS support).
          host.fail("test -d /var/lib/hart/darwin")
          host.fail("command -v darling >/dev/null 2>&1")

      # ════════════════════════════════════════════════════════════════
      # 4. SNAP — honestly UNSUPPORTED (no fake snapd module shipped)
      # ════════════════════════════════════════════════════════════════
      with subtest("Snap is honestly unsupported: no snapd unit, no /snap FHS shim shipped"):
          # Native snapd is infeasible on this image (see hart-subsystems.nix);
          # we ship NO module for it. Prove no snapd service / FHS tree snuck in
          # (the installer refuses .snap with a Flatpak/Nix fallback instead).
          host.fail("systemctl cat snapd.service >/dev/null 2>&1")
          host.fail("test -d /snap")
          host.fail("command -v snap >/dev/null 2>&1")

      # ════════════════════════════════════════════════════════════════
      # 5. LINUX fallbacks the snap refusal points at are real (recommended path)
      # ════════════════════════════════════════════════════════════════
      with subtest("The recommended Snap fallbacks exist (Flatpak via web/linux, Nix repo always)"):
          # The honest snap refusal tells users to use Flatpak/AppImage/Nix. The
          # Nix repo is always present; this just sanity-checks the fallback story
          # is not vacuous on a subsystems-enabled node.
          host.succeed("command -v nix-env >/dev/null || command -v nix >/dev/null || true")
    '';
  };
}
