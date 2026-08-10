# ═══════════════════════════════════════════════════════════════
# HART OS — Phase-1 Out-of-Process Session Tier-Drop Supervisor nixosTest
# ═══════════════════════════════════════════════════════════════
#
# Proves the never-blank-screen guarantee (compositor/ROADMAP.md Phase 1):
# loop-kill fault injection on a deliberately-crashing higher tier MUST land
# on the cage floor with the latch written, and the operator reset path MUST
# clear the latch so the next boot retries Tier-1.
#
# WHY [VM]-gated: greetd + a Wayland session cannot run on the Windows dev box.
# The dev box authors + unit-tests the selector/hartctl shell logic
# (tests/unit-style POSIX-sh harness in the task report); THIS test exercises
# the wired module on a real (software-GL) VM. Per the honest-hardware rule it
# gates in CI (`nix flake check` / local QEMU), never inline-render or grep.
#
# What it asserts:
#   1. greetd is the active display manager (out-of-process supervisor), NOT GDM
#      — session selection is structurally out-of-process.
#   2. hartctl is on PATH and reports the floor by default.
#   3. CRASH-LOOP FAULT INJECTION: with a fake Tier-1 (hart-comp) + Tier-2 (sway)
#      that exit instantly, running the installed selector wrapper the crash-loop
#      count times drops + LATCHES the tier down to cage — and never below it.
#   4. The latch PERSISTS (it is /var/lib/hart/session-tier, on the data
#      partition) — latched across boot by construction.
#   5. `hartctl session reset-tier` clears the latch so the next attempt is
#      Tier-1 again (the operator recovery path; openRisk #8).
#   6. The supervisor can NEVER drop below cage: a crash-loop while already on
#      cage leaves the latch on cage (the floor still paints).
#
# #70 discipline preserved: built from `hartModules` alone via the shared
# `mkNode` (./lib.nix); the supervisor is opt-in so the node enables it +
# overrides the fake tier commands for fault injection.

{ pkgs, hartModules, specialArgs }:

let
  inherit (import ./lib.nix { inherit hartModules; }) mkNode;

  # A fake session command that exits immediately (rc=1) — the crash injection.
  # Used for Tier-1 (hart-comp) and Tier-2 (sway) so the supervisor must drop to
  # the cage floor. cage stays the REAL hart-shell-session (the audited floor).
  crashImmediately = "${pkgs.coreutils}/bin/false";
in
{
  hart-session-supervisor-tier-drop = pkgs.testers.runNixOSTest {
    name = "hart-session-supervisor-tier-drop";
    # runNixOSTest's mypy pre-check does NOT resolve the per-node Machine global
    # (`sup`) the driver injects at RUNTIME — same false "Name not defined" as the
    # floor-lock test (node IS named `sup`, works at runtime). Skip the static
    # pre-check; the VM still boots and the tier-drop assertions still run.
    skipTypeCheck = true;
    # The pyflakes lint (config.skipLint) ALSO flags the runtime-injected `sup`
    # node global as "undefined name" — separate static pass from mypy, same false
    # positive. Skip it too; `sup` exists at runtime when the VM boots.
    skipLint = true;
    node.specialArgs = specialArgs;

    nodes.sup = mkNode "desktop" {
      virtualisation = {
        memorySize = 3072;
        cores = 2;
      };
      # Opt the supervisor on (default off) and inject crashing higher tiers so
      # the drop ladder is exercised. cageCommand stays the real floor launcher.
      hart.sessionSupervisor = {
        enable = true;
        compCommand = crashImmediately;   # Tier-1 fake: crashes instantly
        swayCommand = crashImmediately;    # Tier-2 fake: crashes instantly
        # cageCommand defaults to "hart-shell-session" — the real floor.
        crashLoopCount = 3;
        crashLoopWindowSeconds = 300;
        # No real DRM master to reclaim in the VM — zero the settle so the 12×
        # selector loop stays fast + deterministic (the settle is a real-HW EBUSY
        # guard, exercised by the boot path, not this crash-accounting test).
        drmMasterSettleSeconds = 0;
        tierTermGraceSeconds = 0;
      };
    };

    testScript = ''
      # The driver keys the single machine global by its HOSTNAME — mkNode forces
      # it to the variant ("desktop"), NOT the nodes.sup key — so the `sup` name
      # is absent at runtime (NameError). Bind it from the machines list
      # (single-node test → element 0). The real fix; skip* above only silence the
      # static passes.
      sup = machines[0]
      sup.start()
      sup.wait_for_unit("multi-user.target")

      LATCH = "/var/lib/hart/session-tier"
      WINDOW = "/var/lib/hart/session-tier.window"

      # ── 1. greetd is the supervisor (out-of-process), not GDM ──
      with subtest("greetd is the active display manager (out-of-process supervisor)"):
          # The supervisor REPLACES gdm with greetd when enabled. greetd's unit is
          # greetd.service; gdm must be off.
          sup.wait_for_unit("greetd.service", timeout=120)
          sup.fail("systemctl is-active gdm.service")

      # ── 1b. SEAT / DRM preconditions for a compositor on real HW ──
      # The bare-metal "permission denied / device busy across all tiers" boot loop
      # was a SEAT-MANAGER conflict: an earlier fix forced seatd on top of systemd-
      # logind (two seat managers fighting). The corrected preconditions:
      #   - systemd-logind (THE seat manager on a systemd box) is up; the compositor
      #     rides greetd's active logind session to acquire the seat's DRM + input,
      #   - the session user is in the device groups the compositor needs to open
      #     /dev/dri (video/render = KMS + GPU) and /dev/input (input = libinput),
      #   - greetd FORCES the logind backend (LIBSEAT_BACKEND=logind): the compositor
      #     uses systemd-logind, the single seat manager, NOT seatd layered on top of
      #     logind (two seat managers fighting = the real-HW boot loop / frozen input).
      with subtest("logind is the seat manager + greetd forces the logind backend"):
          # systemd-logind is THE seat manager (always up on systemd); libseat-logind
          # rides greetd's active logind session to TakeDevice the seat's DRM + input.
          sup.wait_for_unit("systemd-logind.service", timeout=60)
          # seatd stays enabled as an idle fallback (keeps the seat group valid) but is
          # NOT the forced backend.
          sup.wait_for_unit("seatd.service", timeout=60)
          groups = sup.succeed("id -nG hart-admin").split()
          for g in ("video", "render", "input", "seat"):
              assert g in groups, \
                  f"hart-admin missing the '{g}' group ({groups}) — compositor can't open the seat's DRM/input"
          # greetd's session command must FORCE LIBSEAT_BACKEND=logind so every tier
          # uses the logind seat manager (the canonical greetd-on-systemd path), and
          # must NOT force seatd-over-logind (the dual-seat-manager regression that
          # froze input + EBUSY-looped the boot on real hardware).
          # The forced backend lives in greetd's SESSION COMMAND (the module
          # wraps the selector: `env LIBSEAT_BACKEND=logind ...`), which greetd
          # reads from its config.toml — it is NOT in the systemd unit file, so
          # the old `systemctl cat greetd.service` grep failed against a
          # CORRECT config on every run (run 30485906966). Assert the config
          # greetd actually consumes.
          # RESOLVE the config path off the unit — do not guess /etc/greetd.
          #
          # On NixOS `services.greetd.settings` renders the config into the
          # STORE and passes it to greetd as `--config /nix/store/...`. There
          # is no /etc/greetd/config.toml and no /etc/greetd/greetd.toml, so
          # both cats failed and the subtest reported a config problem when
          # the config was correct (run 30774512407).
          #
          # The comment above this block records the previous swing: the check
          # used to grep `systemctl cat greetd.service` and was moved to
          # /etc/greetd because the COMMAND is not in the unit. Both are half
          # right — the command is not IN the unit, but the path to the file
          # containing it IS. So: read the path from the unit, then read the
          # file. That is the only place NixOS actually puts it.
          _unit = sup.succeed("systemctl cat greetd.service 2>/dev/null || true")
          _cfg = sup.succeed(
              "systemctl cat greetd.service 2>/dev/null "
              "| grep -oE '\-\-config[= ][^ ]+' | head -1 "
              "| sed -E 's/^--config[= ]//' || true"
          ).strip()
          if not _cfg:
              # Fall back to the /etc locations before failing, so a distro
              # layout that DOES use them still works.
              _cfg = sup.succeed(
                  "for f in /etc/greetd/config.toml /etc/greetd/greetd.toml; do "
                  "  [ -f \"$f\" ] && echo \"$f\" && break; done || true"
              ).strip()
          assert _cfg, (
              "could not locate greetd's config: it is not referenced by "
              "--config on greetd.service and not at either /etc/greetd path.\n"
              "--- greetd.service ---\n" + _unit)
          sup.log(f"greetd config resolved to: {_cfg}")
          greetd_cmd = sup.succeed(f"cat {_cfg}")
          assert "LIBSEAT_BACKEND=logind" in greetd_cmd, \
              "greetd session must force the logind libseat backend (canonical greetd-on-systemd)"
          assert "LIBSEAT_BACKEND=seatd" not in greetd_cmd, \
              "greetd must NOT force seatd over logind — two seat managers fight on real HW (the boot loop)"

      # ── 2. hartctl on PATH; fail-safe to cage when unlatched (contract §3.1) ──
      with subtest("hartctl reports the cage floor when the latch is absent (fail-safe)"):
          sup.succeed("command -v hartctl")
          out = sup.succeed("hartctl session get-tier").strip()
          # A missing/garbage latch fails SAFE to cage — never a higher unproven
          # tier. (An earlier in-VM run may already hold a valid tier; accept any
          # valid token, but the contract default for absent is cage.)
          assert out in ("hart-comp", "sway", "cage"), f"unexpected tier {out!r}"
          # Arm Tier-1 explicitly so the fault-injection starts from the top.
          sup.succeed("hartctl session reset-tier")
          assert sup.succeed("hartctl session get-tier").strip() == "hart-comp", \
              "reset-tier must WRITE hart-comp (re-arm Tier-1), per contract §4"

      # Locate the installed selector wrapper (greetd's session command).
      # It is referenced from greetd's config; pull it from the store.
      selector = sup.succeed(
          "find /nix/store -maxdepth 3 -name '*-hart-session-selector' -type f -print -quit"
      ).strip()
      assert selector, "hart-session-selector wrapper not found in the store"

      # ── 3. CRASH-LOOP FAULT INJECTION: drop Tier-1 -> Tier-2 -> cage + latch ──
      with subtest("crash-loop on the fake higher tiers drops + latches down to cage"):
          # Each selector run launches the latched tier's command. Tier-1
          # (hart-comp=false) and Tier-2 (sway=false) exit instantly (< the crash
          # window) so each run records a crash; after crashLoopCount runs at a
          # tier the selector drops one tier and rewrites the latch. Run it enough
          # times to walk hart-comp -> sway -> cage. We run as the hart user so
          # the latch is writable (tmpfiles makes /var/lib/hart hart-owned).
          for _ in range(12):
              # The selector returns rc=0 (it returns control to greetd); the
              # injected tier command inside it is what "crashes".
              sup.succeed(f"runuser -u hart -- {selector} || true")

          tier = sup.succeed(f"cat {LATCH}").strip()
          assert tier == "cage", \
              f"after crash-loop the latch must be the cage floor, got {tier!r}"

      # ── 3b. A downward drop ARMS the HARTLOG boot-log capture (regression) ──
      # write_tier touches /run/hart/session/tier-degraded on every fall-back so
      # hart-boot-log captures "why it fell to cage" at the moment it happens
      # (hart-session-supervisor.nix write_tier + hart-boot-log.nix path unit). The
      # crash-loop above dropped hart-comp -> sway -> cage, so the trigger must exist
      # and record the final drop to the floor.
      with subtest("a tier degrade arms the HARTLOG boot-log capture trigger"):
          sup.succeed("test -f /run/hart/session/tier-degraded")
          rec = sup.succeed("cat /run/hart/session/tier-degraded").strip()
          assert rec.startswith("from=") and "to=cage" in rec, \
              f"degrade trigger should record the drop to the cage floor, got {rec!r}"

      # ── 4. The latch persists on the data partition (latched across boot) ──
      with subtest("the latch file persists under /var/lib/hart (survives reboot by construction)"):
          sup.succeed(f"test -f {LATCH}")
          # Confirm it is on the persistent hart state dir, not tmpfs.
          mnt = sup.succeed(f"df --output=target {LATCH} | tail -1").strip()
          assert "/run" not in mnt and "tmpfs" not in mnt.lower(), \
              f"latch is on a volatile mount ({mnt}) — would NOT latch across boot"

      # ── 6. Cannot drop below the floor ──
      with subtest("a crash-loop while already on cage cannot drop below the floor"):
          # Already latched to cage. Run the selector more times; the REAL cage
          # floor may itself fail to fully start headless, but the latch must
          # STAY cage (never a 4th tier, never blank).
          for _ in range(5):
              sup.succeed(f"runuser -u hart -- {selector} || true")
          tier = sup.succeed(f"cat {LATCH}").strip()
          assert tier == "cage", f"latch dropped below the floor to {tier!r} — NEVER allowed"

      # ── 5. Operator reset path: reset-tier re-arms Tier-1 (writes hart-comp) ──
      with subtest("hartctl session reset-tier re-arms Tier-1 so the next boot attempts hart-comp"):
          sup.succeed("hartctl session reset-tier")
          # Contract §4: reset WRITES hart-comp (not merely deletes the file) so
          # a transient Tier-1 bug can never permanently mask as a downgrade.
          assert sup.succeed(f"cat {LATCH}").strip() == "hart-comp", \
              "after reset-tier the latch must be hart-comp (Tier-1 re-armed)"
          sup.fail(f"test -f {WINDOW}")  # crash window cleared so T1 gets a clean budget

      # ── status telemetry is observable ──
      with subtest("hartctl session status surfaces the current latched tier"):
          status = sup.succeed("hartctl session status")
          assert "session tier" in status, f"status missing tier line: {status!r}"
    '';
  };

  # ─────────────────────────────────────────────────────────────
  # PAINT-WATCHDOG: a HUNG tier (compositor up, shell never paints, process
  # never exits) is dropped just like a crash. This is the regression the bare
  # crash-on-exit detection was blind to — the "boots to only a mouse pointer"
  # failure where sway/the GTK4 host stays alive but never presents a frame.
  # ─────────────────────────────────────────────────────────────
  hart-session-supervisor-paint-watchdog = pkgs.testers.runNixOSTest {
    name = "hart-session-supervisor-paint-watchdog";
    skipTypeCheck = true;
    skipLint = true;
    node.specialArgs = specialArgs;

    nodes.sup = mkNode "desktop" {
      virtualisation = {
        memorySize = 3072;
        cores = 2;
      };
      hart.sessionSupervisor = {
        enable = true;
        # Tier-1 (hart-comp) unavailable so the ladder starts at Tier-2 (sway).
        compCommand = null;
        # Tier-2 fake: stays ALIVE forever but NEVER touches the paint marker —
        # the HUNG case. The watchdog must kill it + count it as a crash.
        swayCommand = "${pkgs.coreutils}/bin/sleep infinity";
        # cageCommand defaults to the real "hart-shell-session" floor.
        crashLoopCount = 3;
        crashLoopWindowSeconds = 300;
        # Short paint budget so the VM test is fast (real default is 20s).
        shellPaintTimeoutSeconds = pkgs.lib.mkForce 3;
        # Exercise the SIGTERM grace (sleep dies on TERM immediately, so 2s is the
        # ceiling, not a real wait) but zero the post-kill settle (no real DRM
        # master in the VM) so the 8× hung-kill loop stays fast.
        tierTermGraceSeconds = 2;
        drmMasterSettleSeconds = 0;
      };
    };

    testScript = ''
      sup = machines[0]
      sup.start()
      sup.wait_for_unit("multi-user.target")

      LATCH = "/var/lib/hart/session-tier"
      READY = "/run/hart/session/shell-ready"

      sup.wait_for_unit("greetd.service", timeout=120)
      sup.succeed("command -v hartctl")

      selector = sup.succeed(
          "find /nix/store -maxdepth 3 -name '*-hart-session-selector' -type f -print -quit"
      ).strip()
      assert selector, "hart-session-selector wrapper not found in the store"

      # Arm Tier-2 (sway) as the start tier (Tier-1 is unavailable -> sway).
      with subtest("the group-writable paint marker dir exists (shell host can write it)"):
          sup.succeed("test -d /run/hart/session")
          # 0770 hart hart — group-writable so the hart-admin shell host can touch
          # the marker. (drwxrwx---)
          mode = sup.succeed("stat -c '%a' /run/hart/session").strip()
          assert mode == "770", f"/run/hart/session must be 0770 (group-writable), got {mode}"

      with subtest("a HUNG Tier-2 (alive but never paints) is killed + dropped to cage by the watchdog"):
          sup.succeed("hartctl session reset-tier")  # writes hart-comp; comp null -> falls to sway
          # Each selector run launches the latched tier. sway = `sleep infinity`
          # stays alive and never writes READY, so after shellPaintTimeoutSeconds
          # the watchdog KILLS it and records a crash; crashLoopCount such hangs
          # drop the tier. Walk hart-comp(unavail)->sway->cage. The marker must be
          # absent each run (the fake never paints) so every run is a hang.
          for _ in range(8):
              sup.succeed(f"runuser -u hart -- {selector} || true")
              # The watchdog clears + the fake never writes it: marker stays absent.
              sup.fail(f"test -e {READY}")

          tier = sup.succeed(f"cat {LATCH}").strip()
          assert tier == "cage", \
              f"a HUNG higher tier must be dropped to the cage floor by the paint watchdog, got {tier!r}"

      with subtest("the watchdog never drops below the cage floor on a hang"):
          # Already on cage. cage's REAL launcher may not fully paint headless, but
          # the floor is exempt from the watchdog drop — the latch must STAY cage.
          for _ in range(4):
              sup.succeed(f"runuser -u hart -- {selector} || true")
          tier = sup.succeed(f"cat {LATCH}").strip()
          assert tier == "cage", f"watchdog dropped below the floor to {tier!r} — NEVER allowed"
    '';
  };

  # ─────────────────────────────────────────────────────────────
  # PAINT-WATCHDOG POSITIVE CASE: a tier whose compositor DOES touch the paint
  # marker within the budget is KEPT, never dropped. The hang test proves the
  # watchdog FIRES; this proves it does NOT over-fire — a healthy painting tier
  # must survive. The fake Tier-2 here honours the real contract: it touches the
  # marker the selector exports via $HART_SHELL_READY_FLAG (proving the host and
  # the watchdog share ONE path, no hardcoded divergence), then stays alive past
  # the budget. The watchdog must observe `painted=1`, stop watching, and leave
  # the latch on sway.
  # ─────────────────────────────────────────────────────────────
  hart-session-supervisor-paint-watchdog-keep =
    let
      # A painting fake: touch the marker the selector tells us about, then stay
      # alive (a healthy long-lived compositor). Because it painted within the
      # budget the watchdog keeps it; we then SIGTERM it out of band so the
      # selector returns. The long-lived run (no quick exit) is a normal session,
      # not a crash — the latch must stay sway.
      paintThenStay = pkgs.writeShellScript "fake-painting-comp" ''
        touch "$HART_SHELL_READY_FLAG"
        exec ${pkgs.coreutils}/bin/sleep infinity
      '';
    in
    pkgs.testers.runNixOSTest {
      name = "hart-session-supervisor-paint-watchdog-keep";
      skipTypeCheck = true;
      skipLint = true;
      node.specialArgs = specialArgs;

      nodes.sup = mkNode "desktop" {
        virtualisation = { memorySize = 3072; cores = 2; };
        hart.sessionSupervisor = {
          enable = true;
          compCommand = null;                       # Tier-1 unavailable -> sway
          swayCommand = "${paintThenStay}";         # Tier-2 PAINTS then stays alive
          crashLoopCount = 3;
          crashLoopWindowSeconds = 300;
          shellPaintTimeoutSeconds = pkgs.lib.mkForce 5;             # ample for the immediate touch
          drmMasterSettleSeconds = 0;               # no real DRM master in the VM
        };
      };

      testScript = ''
        sup = machines[0]
        sup.start()
        sup.wait_for_unit("multi-user.target")
        sup.wait_for_unit("greetd.service", timeout=120)

        LATCH = "/var/lib/hart/session-tier"
        READY = "/run/hart/session/shell-ready"

        selector = sup.succeed(
            "find /nix/store -maxdepth 3 -name '*-hart-session-selector' -type f -print -quit"
        ).strip()
        assert selector, "selector wrapper not found in the store"

        with subtest("a Tier-2 that PAINTS within the budget is KEPT (watchdog does not over-fire)"):
            sup.succeed("hartctl session reset-tier")   # arm Tier-1; comp null -> sway
            # Run the selector in the background — the painting fake stays alive, so
            # the selector blocks in `wait` after observing the paint. We give it a
            # moment to launch + touch the marker, assert the marker appeared (the
            # host wrote the path the selector exported), then SIGTERM the fake so
            # the selector returns. A painted, long-lived run is NOT a crash, so the
            # latch must remain sway — the tier was KEPT.
            sup.succeed(f"runuser -u hart -- {selector} >/tmp/sel.log 2>&1 & echo started")
            # The fake touches the marker immediately on launch; wait for it.
            sup.wait_until_succeeds(f"test -e {READY}", timeout=30)
            # The compositor stayed up AND painted -> the watchdog kept it. Tear it
            # down so the selector exits cleanly (a long-lived run is a normal
            # logout, not a crash).
            sup.succeed("pkill -TERM -x sleep || true")  # -x (exact comm), NOT -f: `-f sleep` matches the word "sleep" in pkill's OWN command line → SIGTERMs its shell → exit 143 before `|| true`
            # The latch was NEVER lowered: a painting tier is kept on sway.
            tier = sup.succeed(f"cat {LATCH} 2>/dev/null || echo sway").strip()
            assert tier in ("sway", "hart-comp"), \
                f"a painting Tier-2 must be KEPT (latch sway / un-dropped), got {tier!r}"
            assert tier != "cage", \
                "a tier that PAINTED within the budget was wrongly dropped to cage — watchdog over-fired"
      '';
    };

  # ─────────────────────────────────────────────────────────────
  # FRESH BOOT honours startTier — for ALL THREE valid values.
  # A clean (un-latched) boot must start at the configured startTier, not the
  # floor: the ladder tries the BEST configured tier first and only DEGRADES on a
  # real failure. Three nodes, one per enum value (cage|sway|hart-comp), so the
  # option is proven honored end-to-end (the selector's read_tier fallback to
  # $START on an absent latch).
  # ─────────────────────────────────────────────────────────────
  hart-session-supervisor-start-tier =
    let
      # A node parameterised by its configured startTier + the tier the un-latched
      # boot must resolve to (after skipping unavailable higher tiers). All three
      # tiers are AVAILABLE here (comp/sway = a harmless real command, cage = the
      # floor) so the resolved tier equals the configured startTier exactly.
      startTierNode = startTier: mkNode "desktop" {
        virtualisation = { memorySize = 2048; cores = 2; };
        hart.sessionSupervisor = {
          enable = true;
          # mkForce, NOT `inherit startTier`: since mkNode composes the real
          # variant profile, the desktop profile ALSO sets startTier (its
          # shipped value "hart-comp"). Two plain definitions of one enum
          # conflict unless equal — so the `sway` and `cage` nodes failed to
          # EVALUATE (run 30574137255, ❌ hart-session-supervisor-start-tier).
          # This test's whole point is overriding the shipped start tier, so
          # saying so explicitly is also the honest expression of intent.
          startTier = pkgs.lib.mkForce startTier;
          # Make ALL tiers available so startTier is honored verbatim (an
          # unavailable tier would legitimately skip down — tested elsewhere).
          compCommand = "${pkgs.coreutils}/bin/true";
          swayCommand = "${pkgs.coreutils}/bin/true";
          # cageCommand defaults to the real floor launcher (always available).
          crashLoopCount = 3;
          crashLoopWindowSeconds = 300;
          drmMasterSettleSeconds = 0;  # no real DRM master in the VM — stay fast
          # Disable the paint watchdog: this test only exercises the un-latched
          # START resolution, not the hang path.
          shellPaintTimeoutSeconds = pkgs.lib.mkForce 0;
        };
      };
    in
    pkgs.testers.runNixOSTest {
      name = "hart-session-supervisor-start-tier";
      skipTypeCheck = true;
      skipLint = true;
      node.specialArgs = specialArgs;

      nodes = {
        startcomp = startTierNode "hart-comp";
        startsway = startTierNode "sway";
        startcage = startTierNode "cage";
      };

      testScript = ''
        start_all()
        LATCH = "/var/lib/hart/session-tier"

        # The driver keys each machine global by hostname (mkNode forces it to the
        # variant "desktop"), so all three share the name — bind by index instead.
        comp, sway, cage = machines[0], machines[1], machines[2]
        for m in (comp, sway, cage):
            m.wait_for_unit("multi-user.target")
            m.wait_for_unit("greetd.service", timeout=120)

        # Each node's hostname collides on "desktop"; address them by the index we
        # already bound. Pair each with its EXPECTED un-latched start tier.
        cases = [
            (machines[0], "hart-comp"),
            (machines[1], "sway"),
            (machines[2], "cage"),
        ]

        for m, expected in cases:
            with subtest(f"a fresh (un-latched) boot starts at startTier={expected}"):
                selector = m.succeed(
                    "find /nix/store -maxdepth 3 -name '*-hart-session-selector' "
                    "-type f -print -quit"
                ).strip()
                assert selector, "selector wrapper not found in the store"
                # Guarantee the latch is ABSENT (a truly fresh boot) immediately
                # before the run so read_tier MUST fall back to $START. The selector
                # only WRITES the latch on a DROP, so an un-latched clean start never
                # records the chosen tier in the latch — the START decision is
                # observable only in the selector's `launching tier '<TIER>'` log.
                # Capture that line from a SINGLE deterministic run (stderr→stdout):
                # the tier it launches for an absent latch IS the configured
                # startTier. (All tiers are available here, so no skip-down.)
                m.succeed(f"rm -f {LATCH} {LATCH}.tmp /var/lib/hart/session-tier.window")
                log = m.succeed(
                    f"runuser -u hart -- {selector} 2>&1 | "
                    "grep -o \"launching tier '[a-z-]*'\" | tail -1 || true"
                ).strip()
                assert f"launching tier '{expected}'" in log, \
                    f"un-latched boot did not start at startTier={expected!r}; selector said: {log!r}"
                # The clean start must NOT have written a latch (only a drop does);
                # the next fresh boot would again seed startTier.
                assert m.succeed(f"test -f {LATCH}; echo $?").strip() == "1", \
                    "a clean un-latched start must not write the latch (only a drop latches)"
      '';
    };

  # ─────────────────────────────────────────────────────────────
  # LATCH PERSISTS ACROSS A REAL REBOOT — a dropped tier stays dropped, and the
  # latch never goes below cage. The tier-drop test proves the latch is on a
  # persistent mount; THIS proves the live invariant: drop the tier, REBOOT the
  # VM, and assert the post-reboot boot reads the LOWERED latch (does not silently
  # re-arm Tier-1), and that it is still the cage floor (never below it).
  # ─────────────────────────────────────────────────────────────
  hart-session-supervisor-reboot-latch = pkgs.testers.runNixOSTest {
    name = "hart-session-supervisor-reboot-latch";
    skipTypeCheck = true;
    skipLint = true;
    node.specialArgs = specialArgs;

    nodes.sup = mkNode "desktop" {
      virtualisation = { memorySize = 3072; cores = 2; };
      hart.sessionSupervisor = {
        enable = true;
        compCommand = "${pkgs.coreutils}/bin/false";   # Tier-1 crashes instantly
        swayCommand = "${pkgs.coreutils}/bin/false";    # Tier-2 crashes instantly
        crashLoopCount = 3;
        crashLoopWindowSeconds = 300;
        shellPaintTimeoutSeconds = pkgs.lib.mkForce 0;   # crash-only path for this test
        drmMasterSettleSeconds = 0;     # no real DRM master in the VM — stay fast
      };
    };

    testScript = ''
      sup = machines[0]
      sup.start()
      sup.wait_for_unit("multi-user.target")
      sup.wait_for_unit("greetd.service", timeout=120)

      LATCH = "/var/lib/hart/session-tier"

      selector = sup.succeed(
          "find /nix/store -maxdepth 3 -name '*-hart-session-selector' -type f -print -quit"
      ).strip()
      assert selector, "selector wrapper not found in the store"

      with subtest("crash-loop drops + latches the tier down to cage"):
          sup.succeed("hartctl session reset-tier")  # arm Tier-1
          for _ in range(12):
              sup.succeed(f"runuser -u hart -- {selector} || true")
          assert sup.succeed(f"cat {LATCH}").strip() == "cage", \
              "pre-reboot: crash-loop must drop the latch to the cage floor"

      with subtest("the LOWERED latch SURVIVES a real reboot (dropped stays dropped)"):
          # A real power-cycle of the VM — /var/lib/hart is on the persistent disk,
          # so the latch the supervisor wrote must still be cage after the reboot.
          # This is the live proof of "latches across boot" the tier-drop test only
          # inferred from the mount point.
          sup.shutdown()
          sup.start()
          sup.wait_for_unit("multi-user.target")
          sup.wait_for_unit("greetd.service", timeout=120)
          tier = sup.succeed(f"cat {LATCH}").strip()
          assert tier == "cage", \
              f"after reboot the dropped latch must STILL be cage (latched), got {tier!r}"
          # And hartctl agrees the live latch is the floor.
          assert sup.succeed("hartctl session get-tier").strip() == "cage", \
              "hartctl must read the persisted cage latch after reboot"

      with subtest("the latch never goes below cage even after more crash-loops post-reboot"):
          for _ in range(6):
              sup.succeed(f"runuser -u hart -- {selector} || true")
          assert sup.succeed(f"cat {LATCH}").strip() == "cage", \
              "post-reboot crash-loop dropped below the floor — NEVER allowed"

      with subtest("reset-tier re-arms Tier-1, and a reboot READS + HONORS the durable re-arm"):
          # This proves TWO things and, deliberately, NOT a third that would be
          # WRONG (the old freeze-assert; run 31354663901: the post-reboot latch
          # was NOT hart-comp — and that is correct behaviour, not the bug it was
          # reported as):
          #
          #  (1) reset-tier's WRITE is durable — asserted pre-reboot below, with
          #      greetd quiesced so nothing races the write.
          #  (2) A reboot READS that durable re-arm and HONORS it: the next boot's
          #      supervisor re-attempts Tier-1. We prove the re-attempt with the
          #      supervisor's OWN append-only journal ("tier degrade hart-comp ->
          #      sway" on THIS boot) — a line that can ONLY appear if the durably
          #      stored hart-comp survived the power cycle and was read at boot.
          #
          # NOT asserted: that the hart-comp VALUE is frozen across the reboot. It
          # is not, and that is CORRECT — this node's fake Tier-1 is `/bin/false`
          # (permanently broken), so the supervisor rightly re-attempts-then-drops
          # it; a real node would KEEP a now-working Tier-1. The old assert was
          # non-deterministic because it required greetd to stay masked across the
          # reboot — impossible: greetd.service is a NixOS-MANAGED unit, so the
          # declarative /etc regeneration on boot restores it over any runtime
          # `systemctl mask`. The DISK reboot-durability of the latch file is
          # already power-cycled + proved by the cage-drop subtest above (same
          # file, different value) — not re-litigated here.
          #
          # greetd is quiesced ONLY for the pre-reboot write assert (a runtime mask
          # suffices — no reboot yet). `sup.succeed` DISCARDS stderr, so when the
          # mask failed (run 30774512407) the report was a bare "command failed";
          # capture WHY and fall back to stop + runtime-mask, asserting the
          # REQUIREMENT (greetd not active), not the mechanism.
          _rc, _out = sup.execute("systemctl mask --now greetd.service 2>&1")
          if _rc != 0:
              sup.log(f"persistent mask refused (rc={_rc}): {_out.strip()}")
              _rc2, _out2 = sup.execute(
                  "systemctl stop greetd.service 2>&1; "
                  "systemctl mask --runtime --now greetd.service 2>&1")
              sup.log(f"runtime-mask fallback rc={_rc2}: {_out2.strip()}")
              # Whatever the mechanism, the REQUIREMENT is that greetd is not
              # running — assert that, not the mechanism.
              _active = sup.succeed(
                  "systemctl is-active greetd.service || true").strip()
              assert _active != "active", (
                  f"greetd is still ACTIVE after mask+stop ({_active!r}); this "
                  f"subtest needs it quiet so its crash-loop cannot eat the "
                  f"freshly reset latch before the pre-reboot read.\n"
                  f"mask: {_out.strip()}\nfallback: {_out2.strip()}")
              sup.log("greetd quiesced via the runtime fallback")

          # (1) reset-tier durably re-arms Tier-1 (greetd quiet -> the write is stable).
          sup.succeed("hartctl session reset-tier")
          assert sup.succeed(f"cat {LATCH}").strip() == "hart-comp", \
              "reset-tier must durably write hart-comp (Tier-1 re-armed) to the latch"

          # (2) Power-cycle. greetd is NixOS-managed -> it returns on boot (the
          # runtime mask does NOT survive) and re-runs the selector, which reads the
          # durable hart-comp re-arm and, Tier-1 being /bin/false, degrades from it.
          sup.shutdown()
          sup.start()
          sup.wait_for_unit("multi-user.target")
          sup.wait_for_unit("greetd.service", timeout=120)
          # The append-only supervisor journal on THIS boot must show the re-attempt
          # degrading FROM hart-comp -> proof the durable re-arm survived the reboot
          # and was read at boot. Bounded wait: the crash-loop reaches the drop in a
          # few relaunches; never a single-instant catch.
          sup.wait_until_succeeds(
              "journalctl -t hart-session-supervisor -b --no-pager | "
              "grep -q 'tier degrade hart-comp -> sway'", timeout=90)
          # The terminal outcome is the audited floor — never a silent stay above it.
          tier = sup.succeed(f"cat {LATCH}").strip()
          assert tier == "cage", \
              f"a re-attempted broken Tier-1 must settle on the cage floor, got {tier!r}"
    '';
  };

  # ─────────────────────────────────────────────────────────────
  # RECOVERY: Ctrl+Alt+F-key ALWAYS reaches a getty login console, EVEN while a
  # graphical session (greetd → the supervisor selector) holds VT1. This is the
  # never-trap-the-machine guarantee the only-a-pointer hang exposed: the user
  # must be able to switch to a TTY and log in to recover, independent of the
  # compositor's health. A real VM can't press Ctrl+Alt+F2, but it CAN assert the
  # mechanism is live: getty on tty2..tty6 is reachable (the autovt@ttyN template
  # is enabled), autovt@tty2 is PRE-SPAWNED (active from boot, not lazy), and the
  # console framework is on — so a VT switch lands on a real login, not a void.
  # ─────────────────────────────────────────────────────────────
  hart-session-supervisor-recovery-tty = pkgs.testers.runNixOSTest {
    name = "hart-session-supervisor-recovery-tty";
    skipTypeCheck = true;
    skipLint = true;
    node.specialArgs = specialArgs;

    nodes.sup = mkNode "desktop" {
      virtualisation = { memorySize = 3072; cores = 2; };
      # The supervisor owns VT1 via greetd — exactly the "graphical session holds
      # VT1" condition. The recovery TTYs must still be reachable underneath it.
      hart.sessionSupervisor = {
        enable = true;
        # A harmless Tier that stays up so VT1 is genuinely occupied while we probe
        # the recovery consoles (a never-painting sleep, watchdog disabled so it is
        # NOT killed — we want VT1 held for the duration of the probes).
        compCommand = null;
        swayCommand = "${pkgs.coreutils}/bin/sleep infinity";
        shellPaintTimeoutSeconds = pkgs.lib.mkForce 0;   # don't let the watchdog tear down VT1
      };
      # The desktop config pre-spawns autovt@tty2; the minimal mkNode node does not
      # import desktop.nix, so wire the SAME recovery contract here so the test
      # proves the mechanism (the structural test guards desktop.nix carries it).
      systemd.services."autovt@tty2".wantedBy = [ "multi-user.target" ];
      services.getty.autologinUser = pkgs.lib.mkForce null;
    };

    testScript = ''
      sup = machines[0]
      sup.start()
      sup.wait_for_unit("multi-user.target")

      with subtest("greetd (the supervisor) holds the graphical seat on VT1"):
          sup.wait_for_unit("greetd.service", timeout=120)

      with subtest("a getty login console is reachable on tty2..tty6 (the recovery range)"):
          # autovt@ttyN is the template logind activates on a Ctrl+Alt+Fn switch.
          # We can't press the chord in a headless VM, but we CAN start the exact
          # units logind would and confirm each comes up as a real getty login —
          # i.e. a VT switch would land on a console, not a void. tty2 is already
          # pre-spawned (below); start tty3..tty6 to prove the whole range works.
          # NOTE the instance NAME: starting autovt@ttyN instantiates the unit
          # AS autovt@ttyN — getty@ttyN is a SEPARATE, never-started instance.
          # The old wait on getty@ttyN therefore failed against a WORKING
          # recovery console on every run ("unit getty@tty2.service is
          # inactive", run 30485906966). Assert the instance we actually start.
          #
          # WHY THE PROCESS AND NOT ExecStart (run 30774512407): the previous
          # version asserted `"agetty" in ExecStart` and failed on tty3 with
          #     path=/nix/store/…-getty ; argv[]=/nix/store/…-getty
          # while tty2 logged a plain `(agetty)`. That is not two behaviours,
          # it is nixpkgs' two SHAPES — getty@ and autovt@ are distinct unit
          # definitions in nixos/modules/services/ttys/getty.nix:
          #     getty@   ExecStart = writers.writeDash "getty" autologinScript
          #     autovt@  ExecStart = gettyCmd "--noclear %I $TERM"
          # The first is a dash wrapper whose last line EXECs agetty, so both
          # shapes end up as a real login — one just isn't spelled "agetty" in
          # its ExecStart. (The comment this replaces claimed autovt@ is a
          # symlink to the getty@ template; at this nixpkgs pin it is not, and
          # believing that is what made the string check look sound.)
          #
          # So assert the REQUIREMENT — a login program is RUNNING on that VT —
          # rather than which of the two shapes systemd resolved. Reading the
          # main PID's comm is also STRICTLY STRONGER: it proves agetty actually
          # started, where the string only ever proved it was configured to.
          for n in range(2, 7):
              sup.succeed(f"systemctl start autovt@tty{n}.service")
              sup.wait_for_unit(f"autovt@tty{n}.service", timeout=30)
              # Retry: the unit reports active at fork, and the wrapper shape
              # needs one more exec before comm settles to "agetty".
              try:
                  sup.wait_until_succeeds(
                      f"pid=$(systemctl show -p MainPID --value "
                      f"autovt@tty{n}.service); "
                      f'[ -n "$pid" ] && [ "$pid" != 0 ] && '
                      f'grep -qx agetty "/proc/$pid/comm"',
                      timeout=30)
              except Exception:
                  pid = sup.succeed(
                      f"systemctl show -p MainPID --value "
                      f"autovt@tty{n}.service").strip()
                  comm = sup.succeed(
                      f'cat "/proc/{pid}/comm" 2>/dev/null || echo "<no pid>"'
                  ).strip()
                  execstart = sup.succeed(
                      f"systemctl show -p ExecStart --value "
                      f"autovt@tty{n}.service").strip()
                  raise AssertionError(
                      f"autovt@tty{n} is active but its main process is "
                      f"{comm!r} (pid {pid}), not agetty — a Ctrl+Alt+F{n} "
                      f"switch would land on a void.\nExecStart: {execstart!r}")

      with subtest("autovt@tty2 is PRE-SPAWNED from boot (recovery console already alive)"):
          # The belt-and-suspenders pin: tty2's getty must be active WITHOUT us
          # starting it, because desktop.nix (and this node) put it wantedBy
          # multi-user.target — so the instant the user switches to VT2 the console
          # is already there, never depending on logind's lazy autovt spawn while
          # the graphical session is wedged.
          # (It is wantedBy multi-user.target, so it came up at boot.)
          assert "multi-user.target" in sup.succeed(
              "systemctl show -p WantedBy autovt@tty2.service"
          ), "autovt@tty2 is not wantedBy multi-user.target — not pre-spawned"
          assert sup.succeed("systemctl is-active autovt@tty2.service").strip() == "active", \
              "tty2 getty is not active from boot — recovery console not pre-spawned"

      with subtest("the TTY autologin is nulled so a recovery F-key never lands on a hidden user"):
          # getty.autologinUser is null → the F-key reaches a real LOGIN PROMPT,
          # not an auto-session on the hidden `nixos`/kiosk user.
          al = sup.succeed("systemctl cat autovt@tty2.service || true")
          assert "--autologin" not in al, \
              "getty has --autologin wired — a recovery F-key would skip the login prompt"

      with subtest("the graphical session on VT1 cannot veto the kernel VT switch"):
          # logind owns the seat; the compositor cannot refuse a VT switch. Assert
          # logind is up and the seat has multiple VTs (NAutoVTs default 6) so the
          # switch target exists.
          sup.wait_for_unit("systemd-logind.service")
          # The recovery consoles we brought up above are the switch targets; their
          # being active proves the seat can host them alongside greetd's VT1.
          assert sup.succeed("systemctl is-active getty@tty3.service").strip() == "active"
    '';
  };

  # ─────────────────────────────────────────────────────────────
  # NODE_WATCHDOG UNHEALTHY SIGNAL: when node_watchdog touches the one-way
  # /run/hart/compositor-unhealthy flag, ONE selector invocation must record
  # EXACTLY ONE crash and then return to greetd WITHOUT launching a tier in the
  # same run. This proves the fix for the double-record bug: previously the
  # unhealthy block recorded+dropped and then FELL THROUGH to launch the OLD-latch
  # tier, whose crash/hang path recorded a SECOND crash for the same boot (firing
  # the crash-loop threshold a cycle early + non-deterministically). The block now
  # `exit 0`s immediately after handling, so a single invocation == at most one
  # crash. We use crashLoopCount=3 so ONE unhealthy run records 1 (< threshold) and
  # does NOT drop — letting us count the window file and assert it is exactly 1
  # line and the latch is unchanged (a fall-through would have added a 2nd line and
  # possibly dropped). Then a SECOND + THIRD unhealthy run reach the threshold and
  # drop EXACTLY ONE tier (hart-comp → sway), proving per-run single-crash
  # accounting end to end.
  # ─────────────────────────────────────────────────────────────
  hart-session-supervisor-unhealthy-flag = pkgs.testers.runNixOSTest {
    name = "hart-session-supervisor-unhealthy-flag";
    skipTypeCheck = true;
    skipLint = true;
    node.specialArgs = specialArgs;

    nodes.sup = mkNode "desktop" {
      virtualisation = { memorySize = 3072; cores = 2; };
      hart.sessionSupervisor = {
        enable = true;
        # Both higher tiers are AVAILABLE and would PAINT (true exits 0 immediately,
        # which the watchdog-disabled crash path treats as a fast exit). They are
        # never reached in an unhealthy run — the unhealthy block exits BEFORE any
        # launch — so their exact command is irrelevant; we only need them
        # available so a DROP from hart-comp lands on sway (not skipped).
        compCommand = "${pkgs.coreutils}/bin/true";
        swayCommand = "${pkgs.coreutils}/bin/true";
        crashLoopCount = 3;
        crashLoopWindowSeconds = 300;
        drmMasterSettleSeconds = 0;  # no real DRM master in the VM — stay fast
        # Irrelevant here (no launch happens in an unhealthy run) but pin it off so
        # nothing about the paint path can interfere with the assertion.
        shellPaintTimeoutSeconds = pkgs.lib.mkForce 0;
      };
    };

    testScript = ''
      sup = machines[0]
      sup.start()
      sup.wait_for_unit("multi-user.target")
      sup.wait_for_unit("greetd.service", timeout=120)

      LATCH = "/var/lib/hart/session-tier"
      WINDOW = "/var/lib/hart/session-tier.window"
      UNHEALTHY = "/run/hart/compositor-unhealthy"

      selector = sup.succeed(
          "find /nix/store -maxdepth 3 -name '*-hart-session-selector' -type f -print -quit"
      ).strip()
      assert selector, "hart-session-selector wrapper not found in the store"

      def window_lines():
          # Count crash timestamps recorded in the window file (0 if absent).
          return int(sup.succeed(
              f"if [ -f {WINDOW} ]; then wc -l < {WINDOW} | tr -d ' '; else echo 0; fi"
          ).strip())

      with subtest("a clean start state: arm Tier-1, clear the window + the flag"):
          # ISOLATE from greetd first: in this GPU-less VM every tier crashes
          # instantly, so greetd re-runs the crashing selector continuously in
          # the background — polluting the crash window and dropping the latch
          # UNDERNEATH the exact-count assertions below (the family-wide red,
          # run 30485906966). greetd's supervisor role was already asserted in
          # subtest 1; from here the test IS the session runner.
          sup.succeed("systemctl stop greetd.service")
          sup.succeed("hartctl session reset-tier")  # latch = hart-comp, window cleared
          sup.succeed(f"rm -f {WINDOW} {UNHEALTHY}")
          assert sup.succeed(f"cat {LATCH}").strip() == "hart-comp"
          assert window_lines() == 0, "window must start empty"

      with subtest("ONE unhealthy signal records EXACTLY ONE crash and does NOT fall through to launch a tier"):
          # node_watchdog's one-way signal: touch the flag, run the selector ONCE.
          sup.succeed(f"touch {UNHEALTHY}")
          out = sup.succeed(f"runuser -u hart -- {selector} 2>&1; echo RC=$?")
          assert "RC=0" in out, f"selector must return 0 to greetd, got: {out!r}"
          # The flag was consumed (the selector removes it).
          sup.fail(f"test -e {UNHEALTHY}")
          # THE fix: exactly ONE crash recorded for this single invocation. A
          # fall-through to launch the (true→exits-fast) tier would have recorded a
          # SECOND crash on the exit-accounting path → 2 lines.
          n = window_lines()
          assert n == 1, f"one unhealthy run must record EXACTLY one crash, got {n} (double-record regression)"
          # Below threshold (3) so NO drop yet — the latch is untouched. A
          # fall-through that also launched + crashed could have tipped accounting;
          # the latch staying hart-comp confirms the single, non-dropping record.
          assert sup.succeed(f"cat {LATCH}").strip() == "hart-comp", \
              "one sub-threshold unhealthy crash must not drop the tier"
          # And the selector did NOT log a tier launch in an unhealthy run (it
          # exits before the 'launching tier' line).
          assert "launching tier" not in out, \
              "the unhealthy block must NOT launch a tier in the same run (it exits first)"

      with subtest("the unhealthy signal reaching the threshold drops EXACTLY ONE tier (hart-comp -> sway)"):
          # Two more unhealthy runs reach crashLoopCount=3. Each records exactly one
          # crash; the third tips the threshold and drops ONE rung + clears the
          # window. A per-run double-record would have over-counted and dropped
          # early / by more than one rung.
          sup.succeed(f"touch {UNHEALTHY}")
          sup.succeed(f"runuser -u hart -- {selector} || true")   # 2nd crash (window=2)
          assert window_lines() == 2, "second unhealthy run must record exactly one more crash"
          assert sup.succeed(f"cat {LATCH}").strip() == "hart-comp", "still below threshold — no drop"

          sup.succeed(f"touch {UNHEALTHY}")
          sup.succeed(f"runuser -u hart -- {selector} || true")   # 3rd crash -> drop + clear
          assert sup.succeed(f"cat {LATCH}").strip() == "sway", \
              "the 3rd unhealthy crash must drop EXACTLY one rung hart-comp -> sway"
          # The drop cleared the window (fresh budget at the new tier).
          assert window_lines() == 0, "a drop must clear the crash window"

      with subtest("the unhealthy drop never goes below the cage floor"):
          # Walk sway -> cage, then prove a further unhealthy crash-loop on cage
          # cannot drop below the floor.
          for _ in range(3):
              sup.succeed(f"touch {UNHEALTHY}")
              sup.succeed(f"runuser -u hart -- {selector} || true")
          assert sup.succeed(f"cat {LATCH}").strip() == "cage", \
              "sway must drop to the cage floor under the unhealthy crash-loop"
          for _ in range(4):
              sup.succeed(f"touch {UNHEALTHY}")
              sup.succeed(f"runuser -u hart -- {selector} || true")
          assert sup.succeed(f"cat {LATCH}").strip() == "cage", \
              "the unhealthy crash-loop must NEVER drop below the cage floor"
    '';
  };

  # ─────────────────────────────────────────────────────────────
  # INPUT-ALIVE WATCHDOG (the INPUT twin of the paint watchdog): a tier that
  # PAINTS (touches shell-ready) but is INPUT-STARVED (never touches input-alive
  # while staying alive) is the real-HW "pointer frozen at 0,0, nothing types"
  # failure (#134) the paint watchdog is blind to. With inputAliveTimeoutSeconds
  # > 0 (the operator opting in), the watchdog must kill + drop it exactly like a
  # paint hang, and never below the cage floor.
  # ─────────────────────────────────────────────────────────────
  hart-session-supervisor-input-watchdog =
    let
      # A painting-but-input-dead fake: touch the PAINT marker the selector
      # exported (so it passes the paint watchdog) but NEVER the INPUT marker, then
      # stay alive forever. The input watchdog must catch it.
      paintNoInput = pkgs.writeShellScript "fake-paint-no-input" ''
        touch "$HART_SHELL_READY_FLAG"
        exec ${pkgs.coreutils}/bin/sleep infinity
      '';
    in
    pkgs.testers.runNixOSTest {
      name = "hart-session-supervisor-input-watchdog";
      skipTypeCheck = true;
      skipLint = true;
      node.specialArgs = specialArgs;

      nodes.sup = mkNode "desktop" {
        virtualisation = { memorySize = 3072; cores = 2; };
        hart.sessionSupervisor = {
          enable = true;
          compCommand = null;                       # Tier-1 unavailable -> sway
          swayCommand = "${paintNoInput}";          # Tier-2 PAINTS but never input
          crashLoopCount = 3;
          crashLoopWindowSeconds = 300;
          shellPaintTimeoutSeconds = pkgs.lib.mkForce 3;             # ample for the immediate paint touch
          inputAliveTimeoutSeconds = 3;             # OPT IN: short input budget for the VM
          tierTermGraceSeconds = 2;
          drmMasterSettleSeconds = 0;               # no real DRM master in the VM
        };
      };

      testScript = ''
        sup = machines[0]
        sup.start()
        sup.wait_for_unit("multi-user.target")
        sup.wait_for_unit("greetd.service", timeout=120)

        LATCH = "/var/lib/hart/session-tier"
        READY = "/run/hart/session/shell-ready"
        INPUT_ALIVE = "/run/hart/session/input-alive"

        selector = sup.succeed(
            "find /nix/store -maxdepth 3 -name '*-hart-session-selector' -type f -print -quit"
        ).strip()
        assert selector, "selector wrapper not found in the store"

        with subtest("a PAINTED-but-input-starved Tier-2 is killed + dropped to cage by the input watchdog"):
            sup.succeed("hartctl session reset-tier")  # arm Tier-1; comp null -> sway
            # Each selector run launches sway = the painting-but-input-dead fake. It
            # touches READY (passes the paint watchdog) but NEVER INPUT_ALIVE, so
            # after inputAliveTimeoutSeconds the input watchdog KILLS it and records a
            # hang; the deterministic-hang drop walks hart-comp(unavail)->sway->cage.
            for _ in range(8):
                sup.succeed(f"runuser -u hart -- {selector} || true")
                # The fake DID paint but NEVER signalled input — prove the drop was the
                # INPUT dimension (paint marker present, input marker absent), not the
                # paint watchdog.
                sup.fail(f"test -e {INPUT_ALIVE}")

            tier = sup.succeed(f"cat {LATCH}").strip()
            assert tier == "cage", \
                f"a painted-but-input-dead higher tier must be dropped to the cage floor, got {tier!r}"

        with subtest("the input watchdog never drops below the cage floor"):
            # Already on cage. cage's REAL launcher may not signal input headless, but
            # the floor is exempt from the watchdog drop — the latch must STAY cage.
            for _ in range(4):
                sup.succeed(f"runuser -u hart -- {selector} || true")
            tier = sup.succeed(f"cat {LATCH}").strip()
            assert tier == "cage", f"input watchdog dropped below the floor to {tier!r} — NEVER allowed"
      '';
    };

  # ─────────────────────────────────────────────────────────────
  # INPUT-ALIVE WATCHDOG POSITIVE CASE: a tier that PAINTS *and* signals
  # input-alive within the budget is KEPT, never dropped. The negative test proves
  # the input watchdog FIRES; this proves it does NOT over-fire — a healthy tier
  # that delivers input must survive. The fake honours BOTH contracts (touches the
  # paths the selector exports via $HART_SHELL_READY_FLAG + $HART_INPUT_ALIVE_FLAG,
  # proving writer and watchdog share ONE path each), then stays alive. The
  # watchdog must observe input_alive=1, stop watching, and leave the latch on sway.
  # ─────────────────────────────────────────────────────────────
  hart-session-supervisor-input-watchdog-keep =
    let
      paintAndInput = pkgs.writeShellScript "fake-paint-and-input" ''
        touch "$HART_SHELL_READY_FLAG"
        touch "$HART_INPUT_ALIVE_FLAG"
        exec ${pkgs.coreutils}/bin/sleep infinity
      '';
    in
    pkgs.testers.runNixOSTest {
      name = "hart-session-supervisor-input-watchdog-keep";
      skipTypeCheck = true;
      skipLint = true;
      node.specialArgs = specialArgs;

      nodes.sup = mkNode "desktop" {
        virtualisation = { memorySize = 3072; cores = 2; };
        hart.sessionSupervisor = {
          enable = true;
          compCommand = null;                       # Tier-1 unavailable -> sway
          swayCommand = "${paintAndInput}";         # Tier-2 PAINTS + signals input
          crashLoopCount = 3;
          crashLoopWindowSeconds = 300;
          shellPaintTimeoutSeconds = pkgs.lib.mkForce 5;
          inputAliveTimeoutSeconds = 5;             # OPT IN: input watchdog active
          drmMasterSettleSeconds = 0;
        };
      };

      testScript = ''
        sup = machines[0]
        sup.start()
        sup.wait_for_unit("multi-user.target")
        sup.wait_for_unit("greetd.service", timeout=120)

        LATCH = "/var/lib/hart/session-tier"
        READY = "/run/hart/session/shell-ready"
        INPUT_ALIVE = "/run/hart/session/input-alive"

        selector = sup.succeed(
            "find /nix/store -maxdepth 3 -name '*-hart-session-selector' -type f -print -quit"
        ).strip()
        assert selector, "selector wrapper not found in the store"

        with subtest("a Tier-2 that PAINTS *and* signals input within the budget is KEPT (no over-fire)"):
            sup.succeed("hartctl session reset-tier")   # arm Tier-1; comp null -> sway
            # Run the selector in the background — the fake stays alive after touching
            # BOTH markers, so the selector blocks in `wait` once it observes input.
            sup.succeed(f"runuser -u hart -- {selector} >/tmp/sel.log 2>&1 & echo started")
            # Both markers appear immediately on launch; wait for the input one (the
            # one this watchdog gates on).
            sup.wait_until_succeeds(f"test -e {READY}", timeout=30)
            sup.wait_until_succeeds(f"test -e {INPUT_ALIVE}", timeout=30)
            # The tier painted AND signalled input -> the watchdog kept it. Tear it
            # down so the selector exits (a long-lived run is a normal logout).
            sup.succeed("pkill -TERM -x sleep || true")  # -x (exact comm), NOT -f: `-f sleep` matches the word "sleep" in pkill's OWN command line → SIGTERMs its shell → exit 143 before `|| true`
            tier = sup.succeed(f"cat {LATCH} 2>/dev/null || echo sway").strip()
            assert tier in ("sway", "hart-comp"), \
                f"an input-alive Tier-2 must be KEPT (latch sway / un-dropped), got {tier!r}"
            assert tier != "cage", \
                "a tier that signalled input within the budget was wrongly dropped — input watchdog over-fired"
      '';
    };

  # ─────────────────────────────────────────────────────────────
  # INPUT-ALIVE WATCHDOG FAIL-SAFE DEFAULT (the never-flap guarantee): with
  # inputAliveTimeoutSeconds = 0 (the DEFAULT), the input watchdog is a pure no-op
  # — a tier that PAINTS but never signals input is KEPT, NOT dropped. This proves
  # the critical invariant: a build whose compositors do not yet write the
  # input-alive marker can never flap a healthy painting tier down to the floor.
  # ─────────────────────────────────────────────────────────────
  hart-session-supervisor-input-watchdog-disabled =
    let
      paintNoInput = pkgs.writeShellScript "fake-paint-no-input-disabled" ''
        touch "$HART_SHELL_READY_FLAG"
        exec ${pkgs.coreutils}/bin/sleep infinity
      '';
    in
    pkgs.testers.runNixOSTest {
      name = "hart-session-supervisor-input-watchdog-disabled";
      skipTypeCheck = true;
      skipLint = true;
      node.specialArgs = specialArgs;

      nodes.sup = mkNode "desktop" {
        virtualisation = { memorySize = 3072; cores = 2; };
        hart.sessionSupervisor = {
          enable = true;
          compCommand = null;                       # Tier-1 unavailable -> sway
          swayCommand = "${paintNoInput}";          # PAINTS, never signals input
          crashLoopCount = 3;
          crashLoopWindowSeconds = 300;
          shellPaintTimeoutSeconds = pkgs.lib.mkForce 5;
          # inputAliveTimeoutSeconds omitted -> DEFAULT 0 (input watchdog disabled).
          drmMasterSettleSeconds = 0;
        };
      };

      testScript = ''
        sup = machines[0]
        sup.start()
        sup.wait_for_unit("multi-user.target")
        sup.wait_for_unit("greetd.service", timeout=120)

        LATCH = "/var/lib/hart/session-tier"
        READY = "/run/hart/session/shell-ready"

        selector = sup.succeed(
            "find /nix/store -maxdepth 3 -name '*-hart-session-selector' -type f -print -quit"
        ).strip()
        assert selector, "selector wrapper not found in the store"

        with subtest("with the input watchdog DISABLED (default 0), a painted-but-input-dead tier is KEPT (never flaps)"):
            sup.succeed("hartctl session reset-tier")   # arm Tier-1; comp null -> sway
            sup.succeed(f"runuser -u hart -- {selector} >/tmp/sel.log 2>&1 & echo started")
            # The tier paints; with inputAliveTimeoutSeconds=0 the selector does NOT
            # wait on / drop for the missing input marker — it just `wait`s the
            # healthy long-lived session.
            sup.wait_until_succeeds(f"test -e {READY}", timeout=30)
            sup.succeed("pkill -TERM -x sleep || true")  # -x (exact comm), NOT -f: `-f sleep` matches the word "sleep" in pkill's OWN command line → SIGTERMs its shell → exit 143 before `|| true`
            tier = sup.succeed(f"cat {LATCH} 2>/dev/null || echo sway").strip()
            assert tier != "cage", \
                "with the input watchdog disabled, a painting tier was flapped to cage — the fail-safe default is broken"
            assert tier in ("sway", "hart-comp"), \
                f"the painting tier must be KEPT (latch sway / un-dropped) when input watchdog is off, got {tier!r}"
      '';
    };

  # ─────────────────────────────────────────────────────────────
  # INPUT-ALIVE WATCHDOG TOUCH-ONLY / DEVICE-LESS GUARD (FM3b / FM5): even with the
  # input watchdog ARMED (inputAliveTimeoutSeconds > 0), a painted tier on a
  # touchSCREEN-only seat (or a seat with no input device at all) must NOT be
  # dropped. The compositor emits the input-alive beacon on a KEYBOARD or POINTER
  # event only — NOT on touch — so on a touch-only box the marker legitimately
  # never appears, and dropping would flap a healthy painting surface to the floor.
  # The selector's seat_has_beacon_input_device guard consults the seat device
  # enumerator (inputDeviceProbeCommand) and SUPPRESSES the drop when the seat
  # exposes only touch. We inject a fake probe reporting a touch-only seat (the VM
  # really has a keyboard, so injection is the only deterministic way to simulate a
  # touch-only box) and prove the armed watchdog keeps the tier. This is the twin of
  # the -input-watchdog FIRE test: that proves a keyboard/pointer seat IS dropped on
  # input death; this proves a touch-only seat is NOT.
  # ─────────────────────────────────────────────────────────────
  hart-session-supervisor-input-watchdog-touch-only =
    let
      # Paints (passes the paint watchdog) but NEVER signals input, then stays alive.
      paintNoInput = pkgs.writeShellScript "fake-paint-no-input-touch" ''
        touch "$HART_SHELL_READY_FLAG"
        exec ${pkgs.coreutils}/bin/sleep infinity
      '';
      # A fake seat-device enumerator that reports a TOUCHSCREEN-ONLY seat (a `touch`
      # capability, NO keyboard / pointer) — the FM3b case the compositor cannot
      # beacon on. Mimics the libinput `list-devices` Capabilities line the guard
      # scans for, so the guard takes its authoritative-classification path.
      touchOnlyProbe = pkgs.writeShellScript "fake-touch-only-probe" ''
        ${pkgs.coreutils}/bin/printf '%s\n' "Device:           HART Test Touchscreen"
        ${pkgs.coreutils}/bin/printf '%s\n' "Capabilities:     touch"
      '';
    in
    pkgs.testers.runNixOSTest {
      name = "hart-session-supervisor-input-watchdog-touch-only";
      skipTypeCheck = true;
      skipLint = true;
      node.specialArgs = specialArgs;

      nodes.sup = mkNode "desktop" {
        virtualisation = { memorySize = 3072; cores = 2; };
        hart.sessionSupervisor = {
          enable = true;
          compCommand = null;                       # Tier-1 unavailable -> sway
          swayCommand = "${paintNoInput}";          # PAINTS, never signals input
          crashLoopCount = 3;
          crashLoopWindowSeconds = 300;
          shellPaintTimeoutSeconds = pkgs.lib.mkForce 5;
          inputAliveTimeoutSeconds = 3;             # ARMED — but the seat is touch-only
          # Inject the touch-only seat enumeration (the VM really has a keyboard, so
          # this is the only deterministic way to exercise the touch-only branch).
          inputDeviceProbeCommand = "${touchOnlyProbe}";
          drmMasterSettleSeconds = 0;
        };
      };

      testScript = ''
        sup = machines[0]
        sup.start()
        sup.wait_for_unit("multi-user.target")
        sup.wait_for_unit("greetd.service", timeout=120)

        LATCH = "/var/lib/hart/session-tier"
        READY = "/run/hart/session/shell-ready"
        INPUT_ALIVE = "/run/hart/session/input-alive"

        selector = sup.succeed(
            "find /nix/store -maxdepth 3 -name '*-hart-session-selector' -type f -print -quit"
        ).strip()
        assert selector, "selector wrapper not found in the store"

        with subtest("an ARMED input watchdog does NOT drop a painting tier on a touch-only seat (FM3b guard)"):
            sup.succeed("hartctl session reset-tier")   # arm Tier-1; comp null -> sway
            # The fake paints but never signals input. The watchdog IS armed, but the
            # injected probe reports a touch-ONLY seat, so the guard must SUPPRESS the
            # drop. Run the selector in the background (the painting fake stays alive,
            # so the selector blocks in `wait` after the watchdog poll).
            sup.succeed(f"runuser -u hart -- {selector} >/tmp/sel.log 2>&1 & echo started")
            sup.wait_until_succeeds(f"test -e {READY}", timeout=30)
            # The input marker NEVER appears (the fake never writes it) — proving the
            # KEEP is the touch-only guard, not an input signal.
            sup.fail(f"test -e {INPUT_ALIVE}")
            # The guard logs the suppression once the armed input budget elapses.
            sup.wait_until_succeeds(
                "grep -q 'touch-only / device-less' /tmp/sel.log", timeout=30)
            sup.succeed("pkill -TERM -x sleep || true")  # -x (exact comm), NOT -f: `-f sleep` matches the word "sleep" in pkill's OWN command line → SIGTERMs its shell → exit 143 before `|| true`
            tier = sup.succeed(f"cat {LATCH} 2>/dev/null || echo sway").strip()
            assert tier != "cage", \
                "the input watchdog dropped a painting tier on a TOUCH-ONLY seat — the FM3b guard failed (flap)"
            assert tier in ("sway", "hart-comp"), \
                f"the painting touch-only tier must be KEPT (latch un-dropped), got {tier!r}"
      '';
    };

  # ─────────────────────────────────────────────────────────────
  # JOURNALD CAPTURE (real-HW diagnosability): the selector must route BOTH its own
  # decisions AND each launched tier's stdout/stderr into the system journal, so a
  # bare-metal tier crash is finally diagnosable via `journalctl` (captured by the
  # HARTJRNL journal-export). BEFORE this, the compositor + supervisor ran inside the
  # greetd session whose stderr never reached journald — a full real-HW export had
  # ZERO supervisor/compositor lines, so every tier-drop was a guess (2026-07-10).
  # A fake Tier-1 that PRINTS unique markers (stdout + stderr) then crashes must
  # appear under `journalctl -t hart-tier-hart-comp`, and the supervisor's own
  # "launching tier" decision under `journalctl -t hart-session-supervisor`. This is
  # the behavioural proof of the systemd-cat wrap (real fn, real journal, asserted
  # side-effect) — NOT a grep-of-source guard.
  # ─────────────────────────────────────────────────────────────
  hart-session-supervisor-journald-capture =
    let
      # Prints a marker to BOTH streams, then crashes (rc=1) — exercises the tier's
      # stdout AND stderr flowing through the systemd-cat wrap into the journal.
      echoingCrash = pkgs.writeShellScript "fake-echoing-comp" ''
        echo "HART_TIER_STDOUT_MARKER launched-and-about-to-crash"
        echo "HART_TIER_STDERR_MARKER on stderr" >&2
        exit 1
      '';
    in
    pkgs.testers.runNixOSTest {
      name = "hart-session-supervisor-journald-capture";
      skipTypeCheck = true;
      skipLint = true;
      node.specialArgs = specialArgs;

      nodes.sup = mkNode "desktop" {
        virtualisation = { memorySize = 3072; cores = 2; };
        hart.sessionSupervisor = {
          enable = true;
          compCommand = "${echoingCrash}";               # Tier-1 prints then crashes
          swayCommand = "${pkgs.coreutils}/bin/false";   # Tier-2 fake crash
          crashLoopCount = 3;              # ONE run records 1 crash (< 3) → no drop
          crashLoopWindowSeconds = 300;
          shellPaintTimeoutSeconds = pkgs.lib.mkForce 0;    # crash-only path — no paint wait
          drmMasterSettleSeconds = 0;      # no real DRM master in the VM — stay fast
          tierTermGraceSeconds = 0;
        };
      };

      testScript = ''
        sup = machines[0]
        sup.start()
        sup.wait_for_unit("multi-user.target")
        sup.wait_for_unit("greetd.service", timeout=120)

        # ISOLATE from greetd's own loop before driving the selector manually:
        # in this GPU-less VM every tier crashes instantly, so greetd re-runs
        # the crashing selector continuously in the background, racking the
        # crash window past crashLoopCount and dropping the latch underneath
        # the single-run assertions (run 30485906966: the "one sub-threshold
        # crash" check read a latch the BACKGROUND loop had already dropped —
        # the wrap's accounting was never at fault). greetd's supervisor role
        # is already asserted above by wait_for_unit; from here the test IS
        # the session runner.
        sup.succeed("systemctl stop greetd.service")

        selector = sup.succeed(
            "find /nix/store -maxdepth 3 -name '*-hart-session-selector' -type f -print -quit"
        ).strip()
        assert selector, "hart-session-selector wrapper not found in the store"

        with subtest("the launched tier's stdout+stderr are captured under journalctl -t hart-tier-hart-comp"):
            sup.succeed("hartctl session reset-tier")  # arm Tier-1 (hart-comp)
            sup.succeed("rm -f /var/lib/hart/session-tier.window")  # clean crash budget
            sup.succeed(f"runuser -u hart -- {selector} || true")
            # The tier's OWN output (BOTH streams) reached the journal under the
            # per-tier identifier — the exact diagnosability the real-HW boot lacked.
            sup.wait_until_succeeds(
                "journalctl -t hart-tier-hart-comp --no-pager | grep -q HART_TIER_STDOUT_MARKER",
                timeout=30)
            sup.wait_until_succeeds(
                "journalctl -t hart-tier-hart-comp --no-pager | grep -q HART_TIER_STDERR_MARKER",
                timeout=30)

        with subtest("the supervisor's own tier decision is captured under journalctl -t hart-session-supervisor"):
            # The `launching tier '<TIER>'` line (log()) must ALSO reach the journal,
            # not just greetd's tty — so the export shows WHICH tier ran (and, on a
            # drop, why). This is the supervisor half of the self-logging chain.
            sup.wait_until_succeeds(
                "journalctl -t hart-session-supervisor --no-pager | "
                "grep -q \"launching tier 'hart-comp'\"",
                timeout=30)

        with subtest("the crash was recorded but stayed below the drop threshold (behaviour unchanged by the wrap)"):
            # The systemd-cat wrap must NOT alter crash accounting: one crashing run
            # records exactly one crash (< crashLoopCount) so the latch stays hart-comp
            # — proving the exec-preserved rc still reaches the crash path.
            assert sup.succeed("cat /var/lib/hart/session-tier").strip() == "hart-comp", \
                "the wrap changed crash accounting — a single sub-threshold crash must not drop the tier"
      '';
    };
}
