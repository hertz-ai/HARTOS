# ═══════════════════════════════════════════════════════════════
# HART OS — DISPLAY tier-ladder NEVER-BLACK nixosTest (the degrade-not-die proof)
# ═══════════════════════════════════════════════════════════════
#
# This is the DISPLAY dimension of the degrade-not-die contract: every
# hardware-dependent display path must DEGRADE gracefully — never brick, black,
# or hang. It complements (does NOT duplicate) tests/session-supervisor.nix by
# closing two specific gaps that file leaves open:
#
#   GAP 1 — the FULL ladder via PAINT HANGS, all three tiers present.
#     session-supervisor.nix's paint-watchdog test sets compCommand = null
#     (Tier-1 hart-comp UNAVAILABLE), so it only ever exercises a single hung
#     Tier-2 (sway) dropping to cage. The real-HW failure is WORSE: hart-comp
#     (Tier-1) comes UP on DRM but the GTK4/GSK glass host never first-paints
#     (the "pointer-only black screen"), THEN sway (Tier-2) ALSO fails to paint.
#     This test makes BOTH higher tiers alive-but-never-painting and proves the
#     paint-watchdog walks the whole ladder hart-comp -> sway -> cage, one rung
#     at a time, never skipping a rung and never dropping below the cage floor —
#     within a deterministic budget (a hang drops on the FIRST timeout). This is
#     the "a tier fails to first-paint -> drop to the next + ultimately the cage
#     software floor" guarantee, end to end on a real (software-GL) VM.
#
#   GAP 2 — the GPU verdict FAILS SAFE to `software` in a REAL boot.
#     tests/unit/test_nixos_gpu_probe.py proves the renderer-classification shell
#     logic + the unit shape (oneshot, before greetd, bounded timeout) on the dev
#     box, but NOTHING boots a VM and asserts the probe's END-TO-END verdict. A VM
#     has NO hardware GL render path, so hart-gpu-probe cannot report a hardware
#     renderer: the verdict MUST be `software` (the floor). This node proves the
#     probe RAN, SUCCEEDED (never blocked/failed the boot), wrote `software`, and
#     that greetd (the never-black supervisor entry it is ordered BEFORE) still
#     came up — i.e. a GPU smoke-test can never wedge the display path. The probe
#     is bounded (TimeoutStartSec + an inner `timeout 12` on eglinfo), so a GPU
#     that HANGS on context creation cannot wedge the boot transaction either.
#
# READ-ONLY on the GPU-arming surface: this test READS hart-gpu-probe.nix /
# hart-comp.nix / hart-layer-shell-host.nix (via the booted closure) to PROVE the
# never-black contract; it EDITS none of them (another workstream owns those).
#
# WHY [VM]-gated: greetd + a Wayland session + a real GL probe cannot run on the
# Windows dev box. Per the honest-hardware rule this gates in CI (`nix flake
# check` / local QEMU-KVM), never inline-render or grep. #70 discipline preserved:
# built from `hartModules` alone via the shared `mkNode` (./lib.nix); the
# supervisor + gpu probe are opt-in/always-on hart options, so the node enables
# the supervisor + injects the fault tiers, and the gpu probe ships by cfg.enable.

{ pkgs, hartModules, specialArgs }:

let
  inherit (import ./lib.nix { inherit hartModules; }) mkNode;

  # A compositor fake that comes UP and stays alive forever but NEVER paints
  # (never touches the shell-ready marker) — the HUNG-tier failure the bare
  # crash-on-exit detection is blind to. Used for BOTH Tier-1 (hart-comp) and
  # Tier-2 (sway) so the paint-watchdog must walk the FULL ladder
  # hart-comp -> sway -> cage. Dies immediately on the SIGTERM the watchdog sends.
  hangNeverPaint = "${pkgs.coreutils}/bin/sleep infinity";

  # A HUNG fake that comes up, NEVER paints, AND IGNORES SIGTERM — the worst-case
  # wedged compositor (so wedged it will not even honour a polite drmDropMaster
  # request on TERM). The watchdog must STILL stop it: SIGTERM (ignored) -> wait the
  # bounded tierTermGrace -> SIGKILL. Proves the EBUSY-prevention handoff is bounded
  # — degrade-not-die even against a compositor that refuses to die on TERM (if the
  # SIGKILL escalation were broken the selector would `wait` on it forever and the
  # whole ladder would wedge black). `trap '' TERM INT` makes the process ignore
  # both; only SIGKILL can reap it. NO `exec` (exec would replace the trapping shell
  # with a TERM-default `sleep`).
  hangIgnoreTerm = pkgs.writeShellScript "fake-hang-ignore-term" ''
    trap "" TERM INT
    while true; do ${pkgs.coreutils}/bin/sleep 1; done
  '';

  # A Tier-1 fake that simulates the COLD-then-WARM cold-boot transient: on a COLD
  # boot (/var/lib/hart/test-warm absent) it NEVER paints (the transient cold-boot
  # paint-HANG that demotes the machine); on a WARM boot (the sentinel present) it
  # PAINTS its first frame then stays alive until the test RELEASES it via a precise
  # exit-sentinel (/var/lib/hart/test-exit) — a clean logout, NOT a broad `pkill`, so
  # it never disturbs greetd's own parked selector. The sentinels live on the
  # PERSISTENT data dir so the test toggles cold/warm + tears down deterministically.
  # Proves the fresh-boot re-promotion self-heal: a one-off cold hang must not
  # permanently demote the machine.
  coldHangWarmPaint = pkgs.writeShellScript "fake-cold-hang-warm-paint" ''
    if [ -e /var/lib/hart/test-warm ]; then
      ${pkgs.coreutils}/bin/touch "$HART_SHELL_READY_FLAG"   # WARM: paint its first frame
      # Stay alive (a healthy long-lived session) until the test releases it — a
      # clean exit, so the watchdog KEEPS it and the run is a normal logout.
      while [ ! -e /var/lib/hart/test-exit ]; do ${pkgs.coreutils}/bin/sleep 1; done
      exit 0
    else
      exec ${pkgs.coreutils}/bin/sleep infinity              # COLD: never paint (hang)
    fi
  '';
in
{
  # ─────────────────────────────────────────────────────────────
  # GAP 1 — FULL PAINT-HANG LADDER: hart-comp (Tier-1) and sway (Tier-2) both
  # come up but never first-paint; the paint-watchdog must drop each to the next
  # rung and ultimately reach the cage software floor, never below it.
  # ─────────────────────────────────────────────────────────────
  display-tiers-neverblack-paint-ladder = pkgs.testers.runNixOSTest {
    name = "display-tiers-neverblack-paint-ladder";
    # runNixOSTest's mypy/pyflakes pre-checks do NOT resolve the per-node Machine
    # global the driver injects at RUNTIME (the node IS bound at runtime). Skip the
    # static passes; the VM still boots and the assertions still run — identical to
    # every sibling supervisor test.
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
        # BOTH higher tiers are AVAILABLE and ALIVE-but-NEVER-PAINTING (the
        # pointer-only black-screen failure). cageCommand defaults to the real
        # "hart-shell-session" floor.
        compCommand = hangNeverPaint;   # Tier-1 hart-comp: up, never paints
        swayCommand = hangNeverPaint;    # Tier-2 sway:      up, never paints
        crashLoopCount = 3;
        crashLoopWindowSeconds = 300;
        # Short paint budget so the VM test is fast (real default is 20s). A hang
        # drops on the FIRST timeout (deterministic), so 3s is the whole per-rung
        # cost.
        shellPaintTimeoutSeconds = pkgs.lib.mkForce 3;
        # Exercise the SIGTERM grace (sleep dies on TERM immediately, so 2s is the
        # ceiling) but zero the post-kill DRM settle (no real DRM master in the VM)
        # so the walk stays fast.
        tierTermGraceSeconds = 2;
        drmMasterSettleSeconds = 0;
        # inputAliveTimeoutSeconds left at its default 0 — this test exercises ONLY
        # the paint-hang ladder, not the input-alive twin (covered elsewhere).
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
      assert selector, "hart-session-selector wrapper not found in the store"

      with subtest("each higher tier that fails to PAINT drops to the next, reaching the cage floor"):
          sup.succeed("hartctl session reset-tier")  # arm Tier-1 (hart-comp)
          # Tier-1 (hart-comp) and Tier-2 (sway) are both alive-but-never-paint. Each
          # selector run launches the latched tier; after shellPaintTimeoutSeconds the
          # paint-watchdog KILLS the hung tier and drops one rung on the FIRST timeout
          # (a hang is deterministic). Walk hart-comp -> sway -> cage. The marker must
          # be ABSENT every run (neither fake ever paints) so every drop is a paint
          # hang, not a crash.
          for _ in range(8):
              sup.succeed(f"runuser -u hart -- {selector} || true")
              sup.fail(f"test -e {READY}")
          tier = sup.succeed(f"cat {LATCH}").strip()
          assert tier == "cage", \
              f"each non-painting higher tier must drop to the cage floor, got {tier!r}"

      with subtest("the paint-watchdog NEVER drops below the cage floor"):
          # Already on cage. cage's REAL launcher may not fully paint headless (no DM
          # /no liquidUI on this minimal node), but the floor is EXEMPT from the
          # watchdog drop — the latch must STAY cage, never a phantom 4th tier.
          for _ in range(4):
              sup.succeed(f"runuser -u hart -- {selector} || true")
          tier = sup.succeed(f"cat {LATCH}").strip()
          assert tier == "cage", \
              f"the paint-watchdog dropped below the cage floor to {tier!r} — NEVER allowed"

      with subtest("the walk is ONE RUNG AT A TIME — Tier-2 (sway) is genuinely reached, never skipped"):
          # Reset and step the ladder one drop at a time, asserting the intermediate
          # sway rung. A single Tier-1 paint hang must land on sway (NOT jump to
          # cage), and only the NEXT hang drops sway -> cage. This proves the drop is
          # strictly one rung (lower_tier), so the never-black ladder degrades through
          # every middle tier rather than collapsing straight to the floor.
          sup.succeed("hartctl session reset-tier")
          assert sup.succeed(f"cat {LATCH}").strip() == "hart-comp", \
              "reset-tier must re-arm Tier-1 (hart-comp)"
          sup.succeed(f"runuser -u hart -- {selector} || true")
          assert sup.succeed(f"cat {LATCH}").strip() == "sway", \
              "a single Tier-1 (hart-comp) paint hang must drop EXACTLY one rung to Tier-2 (sway)"
          sup.succeed(f"runuser -u hart -- {selector} || true")
          assert sup.succeed(f"cat {LATCH}").strip() == "cage", \
              "a single Tier-2 (sway) paint hang must drop EXACTLY one rung to the cage floor"

      with subtest("greetd (the never-black supervisor) is the active DM throughout — GDM is OFF"):
          # The whole degrade ladder runs out-of-process under greetd; GDM must never
          # be the one driving the seat (two DMs would fight). This is the structural
          # never-black guarantee: session selection is out-of-process by construction.
          assert sup.succeed("systemctl is-active greetd.service").strip() == "active"
          sup.fail("systemctl is-active gdm.service")
    '';
  };

  # ─────────────────────────────────────────────────────────────
  # GAP 2 — GPU PROBE FAILS SAFE TO `software` IN A REAL BOOT, and can never
  # wedge the display path. A VM has no hardware GL, so the boot-time smoke test
  # (hart-gpu-probe) must land on the software floor; the probe is ordered BEFORE
  # greetd with bounded timeouts, so it can never block the never-black supervisor.
  # ─────────────────────────────────────────────────────────────
  display-tiers-neverblack-gpu-failsafe = pkgs.testers.runNixOSTest {
    name = "display-tiers-neverblack-gpu-failsafe";
    skipTypeCheck = true;
    skipLint = true;
    node.specialArgs = specialArgs;

    nodes.gpu = mkNode "desktop" {
      virtualisation = {
        memorySize = 3072;
        cores = 2;
        # DELIBERATELY NO `qemu.options = [ "-vga" "virtio" ]`: the VM has NO
        # hardware GL render path, so hart-gpu-probe's `eglinfo` cannot create a
        # hardware GL context / report a hardware renderer. That is EXACTLY the
        # "GPU init fails / no usable GPU" case the verdict must FAIL-SAFE to
        # `software`. hart.gpu.accelerate defaults TRUE, so the probe still RUNS
        # eglinfo (it does not short-circuit) — the fail-safe is proven by the probe
        # running and STILL landing on software because no hardware renderer exists.
      };
      # Enable the supervisor too, so greetd (the never-black ladder's entry point)
      # is present. hart-gpu-probe is ordered `before = greetd.service`, so a green
      # greetd here also proves a GPU smoke-test that runs first can never wedge the
      # display path. crash-only (paint watchdog off) keeps this node about the
      # PROBE, not the paint ladder (covered above).
      hart.sessionSupervisor = {
        enable = true;
        shellPaintTimeoutSeconds = pkgs.lib.mkForce 0;
        drmMasterSettleSeconds = 0;
        tierTermGraceSeconds = 0;
      };
    };

    testScript = ''
      gpu = machines[0]
      gpu.start()
      # The boot REACHING multi-user.target at all is the first half of "a GPU init
      # hang must not wedge the boot": the probe is a oneshot ordered BEFORE greetd
      # with a bounded TimeoutStartSec + an inner `timeout 12` on eglinfo + an
      # always-exit-0 script, so even a hung/erroring GPU can never block the boot
      # transaction.
      gpu.wait_for_unit("multi-user.target")

      VERDICT = "/run/hart/gpu-render"

      with subtest("the GPU smoke-test oneshot RAN and SUCCEEDED (never blocked/failed the boot)"):
          gpu.wait_for_unit("hart-gpu-probe.service", timeout=60)
          # oneshot + RemainAfterExit => 'active (exited)' after a successful run; the
          # unit must never be 'failed' — it always exits 0 by contract.
          state = gpu.succeed("systemctl is-active hart-gpu-probe.service").strip()
          assert state == "active", \
              f"hart-gpu-probe must be active(exited) — it must always succeed, got {state!r}"

      with subtest("the probe is BOUNDED so a wedged GPU can never wedge the boot"):
          show = gpu.succeed(
              "systemctl show hart-gpu-probe.service "
              "-p Type -p RemainAfterExit -p TimeoutStartUSec")
          assert "Type=oneshot" in show, f"probe must be a oneshot: {show!r}"
          assert "RemainAfterExit=yes" in show, f"probe must RemainAfterExit: {show!r}"
          # A FINITE TimeoutStartSec is the outer belt (the inner `timeout 12` on
          # eglinfo is the inner one). 'infinity' would mean a wedged probe could hang
          # boot forever — the exact failure the never-black contract forbids.
          assert "TimeoutStartUSec=infinity" not in show, \
              "probe must have a FINITE TimeoutStartSec so a wedged GPU can't wedge boot"

      with subtest("the verdict FAILS SAFE to `software` when no hardware GL is present"):
          gpu.succeed(f"test -f {VERDICT}")
          verdict = gpu.succeed(f"cat {VERDICT}").strip()
          assert verdict == "software", \
              f"a VM with no hardware GL must yield the `software` floor verdict, got {verdict!r}"

      with subtest("the verdict lives on tmpfs (/run) so it is re-derived every boot"):
          # A probe verdict must never outlive the hardware/driver state it measured —
          # the file is under /run (tmpfs), re-derived each boot. (Mirrors the latch
          # test's persistent-vs-volatile mount assertion, inverted: the verdict is
          # the one that MUST be volatile.)
          fstype = gpu.succeed(f"df --output=fstype {VERDICT} | tail -1").strip()
          assert fstype.lower() in ("tmpfs", "ramfs"), \
              f"the gpu-render verdict must be on tmpfs (re-derived per boot), got {fstype!r}"

      with subtest("a pre-greetd GPU probe never wedges the never-black supervisor — greetd still comes up"):
          # hart-gpu-probe runs BEFORE greetd. Proving greetd reaches active confirms
          # the probe (which already ran) did NOT block the display path: the
          # never-black ladder's entry point is up regardless of the GPU verdict.
          gpu.wait_for_unit("greetd.service", timeout=120)
          assert gpu.succeed("systemctl is-active greetd.service").strip() == "active"

      # ── The post-boot DISPLAY-HEALTH snapshot (real-HW observability) ──────────
      # hart-display-health ships wherever the supervisor is enabled (this node), is
      # ordered AFTER greetd, and records the honest never-black verdict. Proving it
      # RAN, succeeded, and reports fail-safe values closes the "real-HW probe" half
      # of the contract: on real hardware THIS file is where #131 (scanout) + #134
      # (input) surface once the compositor writes those markers — here they read
      # `unknown` HONESTLY (the markers are unbuilt), never a faked positive.
      DH = "/run/hart/display-health"
      with subtest("the display-health snapshot RAN and SUCCEEDED (never blocked/failed the boot)"):
          gpu.wait_for_unit("hart-display-health.service", timeout=90)
          state = gpu.succeed("systemctl is-active hart-display-health.service").strip()
          assert state == "active", \
              f"hart-display-health must be active(exited) — it must always succeed, got {state!r}"

      with subtest("the snapshot is BOUNDED + ordered AFTER greetd so it can NEVER delay first paint"):
          show = gpu.succeed(
              "systemctl show hart-display-health.service "
              "-p Type -p RemainAfterExit -p TimeoutStartUSec -p After")
          assert "Type=oneshot" in show, f"snapshot must be a oneshot: {show!r}"
          assert "RemainAfterExit=yes" in show, f"snapshot must RemainAfterExit: {show!r}"
          assert "TimeoutStartUSec=infinity" not in show, \
              "snapshot must have a FINITE TimeoutStartSec so a wedged read can't wedge boot"
          # It must run AFTER greetd (never before it) — the never-block-first-paint rule.
          assert "greetd.service" in show, \
              "the display-health snapshot must be ordered AFTER greetd (never before it)"

      with subtest("the verdict file is written, on tmpfs, with the honest never-black keys"):
          gpu.succeed(f"test -f {DH}")
          fstype = gpu.succeed(f"df --output=fstype {DH} | tail -1").strip()
          assert fstype.lower() in ("tmpfs", "ramfs"), \
              f"the display-health verdict must be on tmpfs (re-derived per boot), got {fstype!r}"
          verdict = gpu.succeed(f"cat {DH}")
          # Every dimension must be present (one key=value line each).
          for key in ("tier=", "gpu=", "painted=", "input=", "scanout=", "screen="):
              assert key in verdict, f"display-health verdict missing {key!r}: {verdict!r}"

      with subtest("the snapshot reports HONEST fail-safe values (never a faked #131/#134 positive)"):
          def dh(key):
              return gpu.succeed(f"grep '^{key}=' {DH} | head -1 | cut -d= -f2").strip()
          # tier is one of the three ladder rungs (the latched rung that won).
          assert dh("tier") in ("hart-comp", "sway", "cage"), f"unexpected tier {dh('tier')!r}"
          # gpu mirrors the probe verdict — this node has no hardware GL, so software.
          assert dh("gpu") == "software", \
              f"display-health gpu must mirror the gpu-probe software floor, got {dh('gpu')!r}"
          # #134: the input-alive marker is unbuilt -> the snapshot must say `unknown`,
          # NOT `dead`/`no` (absence is ambiguous; only the in-session watchdog drops).
          assert dh("input") == "unknown", \
              f"input must be `unknown` until the compositor writes the marker (#134), got {dh('input')!r}"
          # #131: the first-scanout marker is unbuilt -> `unknown`, NEVER `black`.
          assert dh("scanout") == "unknown", \
              f"scanout must be `unknown` until the compositor writes the marker (#131), got {dh('scanout')!r}"
          # screen is `alive` only on a confirmed paint, else `unknown` (never `black`).
          assert dh("screen") in ("alive", "unknown"), \
              f"screen must be alive|unknown (never a black claim), got {dh('screen')!r}"
    '';
  };

  # ─────────────────────────────────────────────────────────────
  # GAP 3 — the DRM-master EBUSY HANDOFF GRACE is EXERCISED (non-zero), and a
  # compositor WEDGED so hard it IGNORES SIGTERM is STILL bounded-killed + dropped.
  # The sibling paint tests zero tierTermGrace/drmMasterSettle (no real DRM master
  # in a VM); this node runs them NON-ZERO and uses a TERM-IGNORING hung Tier-2 to
  # prove the SIGTERM -> grace -> SIGKILL escalation actually fires. If it did not,
  # the selector would `wait` on the un-killable fake forever and the ladder would
  # wedge black — so a bounded, completed drop IS the degrade-not-die proof. (The
  # grace's PURPOSE — letting the compositor drop DRM master before SIGKILL so the
  # next tier does not hit EBUSY — is real-HW only; this proves the MECHANISM that
  # delivers it is honoured + bounded, which is all a VM can assert.)
  # ─────────────────────────────────────────────────────────────
  display-tiers-neverblack-drm-handoff = pkgs.testers.runNixOSTest {
    name = "display-tiers-neverblack-drm-handoff";
    skipTypeCheck = true;
    skipLint = true;
    node.specialArgs = specialArgs;

    nodes.sup = mkNode "desktop" {
      virtualisation = { memorySize = 3072; cores = 2; };
      hart.sessionSupervisor = {
        enable = true;
        compCommand = null;                  # Tier-1 unavailable -> ladder starts at sway
        swayCommand = "${hangIgnoreTerm}";   # Tier-2: HUNG + IGNORES SIGTERM (worst case)
        # Pin the FLOOR to a long-lived sleep so greetd's OWN boot selector PARKS on
        # cage after its sway hang-drop (instead of relaunch-looping on a fast-exiting
        # headless floor and racing subtest 1's TIMED manual drop). The floor's paint
        # invariant is proven by the paint-ladder node; THIS node isolates the bounded
        # SIGTERM->grace->SIGKILL handoff against a TERM-ignoring tier.
        cageCommand = "${pkgs.coreutils}/bin/sleep infinity";
        crashLoopCount = 3;
        crashLoopWindowSeconds = 300;
        shellPaintTimeoutSeconds = pkgs.lib.mkForce 2;        # short paint budget -> fast hang detection
        # NON-ZERO grace + settle (the real-HW EBUSY-handoff window): SIGTERM, wait
        # this grace, then SIGKILL. With a TERM-ignoring fake the FULL grace elapses
        # before the SIGKILL — proving the bounded escalation, not a fast SIGKILL.
        tierTermGraceSeconds = 3;
        drmMasterSettleSeconds = 2;
      };
    };

    testScript = ''
      import time
      sup = machines[0]
      sup.start()
      sup.wait_for_unit("multi-user.target")
      sup.wait_for_unit("greetd.service", timeout=120)

      LATCH = "/var/lib/hart/session-tier"
      READY = "/run/hart/session/shell-ready"

      selector = sup.succeed(
          "find /nix/store -maxdepth 3 -name '*-hart-session-selector' -type f -print -quit"
      ).strip()
      assert selector, "hart-session-selector wrapper not found in the store"

      # Let greetd's OWN boot selector SETTLE first: the boot launches sway (the
      # TERM-ignoring hang), the watchdog drops it to the long-lived cage floor, and
      # greetd PARKS there (no relaunch loop). Waiting for the latch to reach cage
      # means greetd is quiescent, so it can never race subtest 1's timed manual drop.
      sup.wait_until_succeeds(f"cat {LATCH} 2>/dev/null | grep -qx cage", timeout=90)

      with subtest("a TERM-IGNORING HUNG tier is STILL bounded-killed + dropped (degrade-not-die)"):
          sup.succeed("hartctl session reset-tier")  # arm Tier-1; comp null -> sway
          # The fake never paints AND ignores SIGTERM. The paint-watchdog must drop it:
          # SIGTERM (ignored) -> wait tierTermGrace -> SIGKILL -> reap -> settle -> drop.
          # If the SIGKILL escalation were broken, this `succeed` would TIME OUT (the
          # selector would `wait` on the un-killable fake forever) — so completion is
          # the proof the wedged-compositor handoff is bounded.
          t0 = time.monotonic()
          sup.succeed(f"runuser -u hart -- {selector} || true")
          elapsed = time.monotonic() - t0
          sup.fail(f"test -e {READY}")  # it never painted -> the drop was a paint hang
          tier = sup.succeed(f"cat {LATCH}").strip()
          assert tier == "cage", \
              f"a TERM-ignoring HUNG Tier-2 must be dropped to the cage floor, got {tier!r}"
          # The drop must have ACTUALLY waited the grace window (paint budget 2s +
          # term grace 3s + settle 2s = 7s of mandatory sleeps), proving the grace
          # was honoured (not skipped to an immediate SIGKILL that could orphan DRM
          # master). A generous lower bound (5s) tolerates VM scheduling jitter.
          assert elapsed >= 5, \
              f"the drop completed in {elapsed:.1f}s — too fast; the SIGTERM->grace->SIGKILL " \
              "EBUSY-handoff window was skipped (a hard SIGKILL mid-scanout can orphan DRM master)"
          # And it must NOT have wedged: a bounded escalation finishes well under a minute.
          assert elapsed < 40, \
              f"the drop took {elapsed:.1f}s — the SIGKILL escalation did not bound the wedged tier"

      with subtest("the wedged-handoff drop never goes below the cage floor"):
          # Already latched to cage. Running the selector again launches the (long-
          # lived) cage floor, which PARKS — so run in the BACKGROUND; the latch must
          # STAY cage (the floor is exempt from the watchdog drop, never a phantom 4th
          # tier). No teardown — this is the last subtest; the VM is torn down next.
          for _ in range(3):
              sup.succeed(f"runuser -u hart -- {selector} >/dev/null 2>&1 & echo started")
          tier = sup.succeed(f"cat {LATCH}").strip()
          assert tier == "cage", \
              f"the wedged-handoff drop went below the cage floor to {tier!r} — NEVER allowed"
    '';
  };

  # ─────────────────────────────────────────────────────────────
  # GAP 4 — the TRANSIENT COLD-BOOT self-heal: a one-off cold-boot paint-HANG must
  # NOT permanently demote the machine with no recovery (the listed never-degrading
  # failure mode). A hang-drop ARMS a fresh-boot re-promotion; the next (warm) boot
  # re-promotes the transiently-hung tier to startTier and KEEPS it once it paints;
  # a CONFIRMED re-hang SETTLES (no infinite re-walk); and a CRASH-class drop is
  # STICKY (never re-promoted). Also proves the PRODUCTION selector USER (hart-admin,
  # a `hart` GROUP member — NOT the file owner) can PERSIST a drop at the 0770 latch
  # dir (the 0750-vs-0770 / missing-group boot-loop root cause the sibling tests only
  # exercise as the `hart` OWNER via `runuser -u hart`).
  # ─────────────────────────────────────────────────────────────
  display-tiers-neverblack-repromote = pkgs.testers.runNixOSTest {
    name = "display-tiers-neverblack-repromote";
    skipTypeCheck = true;
    skipLint = true;
    node.specialArgs = specialArgs;

    nodes.sup = mkNode "desktop" {
      virtualisation = { memorySize = 3072; cores = 2; };
      hart.sessionSupervisor = {
        enable = true;
        startTier = "hart-comp";
        # Tier-1 = the cold-hang/warm-paint fake; Tier-2 unavailable so a Tier-1 drop
        # lands straight on the cage floor (a single intermediate-less ladder keeps
        # the re-promotion target unambiguous: cage -> back up to hart-comp).
        compCommand = "${coldHangWarmPaint}";
        swayCommand = null;
        # Pin the FLOOR to a long-lived sleep so greetd's OWN selector PARKS on it
        # after the boot drop (instead of relaunch-looping on a fast-exiting headless
        # cage and racing this test's maybe_repromote assertions by re-touching the
        # boot-sentinel/latch). The floor's PAINT + never-drop-below-cage invariants
        # are proven by the paint-ladder + drm-handoff nodes; THIS node isolates the
        # re-promotion mechanics, so a non-painting parked floor is the right fixture.
        cageCommand = "${pkgs.coreutils}/bin/sleep infinity";
        crashLoopCount = 3;
        crashLoopWindowSeconds = 300;
        shellPaintTimeoutSeconds = pkgs.lib.mkForce 3;
        tierTermGraceSeconds = 2;
        drmMasterSettleSeconds = 0;
      };
    };

    testScript = ''
      sup = machines[0]
      sup.start()
      sup.wait_for_unit("multi-user.target")
      sup.wait_for_unit("greetd.service", timeout=120)

      LATCH = "/var/lib/hart/session-tier"
      HANGMARK = "/var/lib/hart/session-tier.hang"
      READY = "/run/hart/session/shell-ready"
      WARM = "/var/lib/hart/test-warm"
      EXIT = "/var/lib/hart/test-exit"   # release the warm comp (clean logout, no broad pkill)
      SENTINEL = "/run/hart/session/boot-repromote-checked"
      REPROMOTED = "/run/hart/session/repromoted-this-boot"

      selector = sup.succeed(
          "find /nix/store -maxdepth 3 -name '*-hart-session-selector' -type f -print -quit"
      ).strip()
      assert selector, "hart-session-selector wrapper not found in the store"

      # Let greetd's OWN boot selector SETTLE before driving the manual subtests:
      # the boot launches Tier-1 (hart-comp) which COLD-hangs, the paint-watchdog
      # drops it to the long-lived cage floor, and greetd PARKS there (it never
      # relaunch-loops, because the cage sleep is long-lived). Waiting for the latch
      # to reach cage means greetd is quiescent, so it can never race the manual
      # maybe_repromote assertions below.
      sup.wait_until_succeeds(f"cat {LATCH} 2>/dev/null | grep -qx cage", timeout=90)

      # The PRODUCTION selector user is hart-admin (greetd's user) — a `hart` GROUP
      # member, NOT the owner. Run every drop as hart-admin so this test ALSO proves
      # the 0770 group-writable latch dir lets the real selector user PERSIST a drop
      # (the 0750-vs-0770 / missing-group boot-loop root cause).
      def sel_admin(bg=False):
          cmd = f"runuser -u hart-admin -- {selector}"
          return sup.succeed(f"{cmd} >/tmp/sel.log 2>&1 & echo started") if bg \
              else sup.succeed(f"{cmd} || true")

      with subtest("the PRODUCTION selector user (hart-admin) can write the 0770 latch dir"):
          groups = sup.succeed("id -nG hart-admin").split()
          assert "hart" in groups, \
              "hart-admin must be in the 'hart' group to persist a tier drop (the 0750/group-write boot loop)"
          mode = sup.succeed("stat -c '%a' /var/lib/hart").strip()
          assert mode == "770", \
              f"/var/lib/hart must be 0770 (group-writable) so the selector user can latch a drop, got {mode}"

      with subtest("a COLD Tier-1 paint-HANG drops to cage AND arms the fresh-boot re-promotion"):
          sup.succeed(f"rm -f {WARM}")                       # cold boot (never paints)
          sup.succeed("hartctl session reset-tier")          # arm Tier-1 (hart-comp)
          sup.succeed(f"rm -f {SENTINEL} {REPROMOTED} {READY}")  # a fresh boot's clean /run slate
          sel_admin()                                        # comp cold-hangs -> watchdog drops
          assert sup.succeed(f"cat {LATCH}").strip() == "cage", \
              "a cold Tier-1 paint hang must drop to the cage floor (sway unavailable)"
          sup.succeed(f"test -e {HANGMARK}")                 # a HANG drop arms re-promotion
          sup.fail(f"test -e {READY}")                       # it never painted

      with subtest("a FRESH (warm) boot SELF-HEALS: the transiently-hung Tier-1 is re-promoted AND kept"):
          sup.succeed(f"rm -f {EXIT}")                       # arm the warm comp to stay alive
          sup.succeed(f"touch {WARM}")                       # warm boot: comp now paints + stays
          sup.succeed(f"rm -f {SENTINEL} {REPROMOTED} {READY}")  # simulate the next fresh boot
          sel_admin(bg=True)                                 # re-promote -> launch warm comp (paints, stays)
          # The re-promotion re-armed hart-comp and the warm comp painted; the
          # paint-watchdog KEEPS it. Wait for the paint, then assert the latch climbed
          # back to the start tier — the cold-boot demotion HEALED, not permanent.
          sup.wait_until_succeeds(f"test -e {READY}", timeout=30)
          assert sup.succeed(f"cat {LATCH}").strip() == "hart-comp", \
              "a warm fresh boot must RE-PROMOTE the transiently-hung tier back to startTier (self-heal)"
          sup.fail(f"test -e {HANGMARK}")                    # the re-promotion consumed the hang arm
          sup.succeed(f"test -e {REPROMOTED}")               # this boot spent its one retry
          # Release the warm comp via its OWN exit-sentinel (a clean logout) — precise,
          # so greetd's parked floor selector is never disturbed (no broad pkill).
          sup.succeed(f"touch {EXIT}")

      with subtest("a CONFIRMED re-hang in the SAME boot SETTLES (never re-walked every boot)"):
          # Same boot (SENTINEL + REPROMOTED still set): make it cold again and re-arm
          # Tier-1. The comp re-hangs; because this boot already spent its retry
          # (REPROMOTED set) the hang path must NOT re-arm HANGMARK — the tier settles
          # on the floor and later boots never re-walk a confirmed-broken tier.
          sup.succeed(f"rm -f {WARM} {READY}")               # cold again
          sup.succeed("hartctl session reset-tier")          # re-arm hart-comp (window cleared)
          sel_admin()                                        # comp cold-hangs -> drop, but REPROMOTED set
          assert sup.succeed(f"cat {LATCH}").strip() == "cage", \
              "the confirmed re-hang must drop to the floor"
          sup.fail(f"test -e {HANGMARK}")                    # SETTLED: not re-armed (no infinite re-walk)

      with subtest("a CRASH-class drop is STICKY — never re-promoted (only a transient HANG self-heals)"):
          # Simulate a crash-dropped state: latch lowered to cage with NO hang arm
          # (a crash drop clears HANGMARK). A fresh boot must NOT re-promote it — a
          # genuinely crash-looping tier stays demoted until an operator reset-tier.
          # Write the latch the SAME atomic way the selector does (mv needs only the
          # 0770 dir-write the hart group has, regardless of the existing file owner).
          sup.succeed(
              f"runuser -u hart-admin -- sh -c 'echo cage > {LATCH}.tmp && mv -f {LATCH}.tmp {LATCH}'")
          sup.succeed(f"rm -f {HANGMARK}")                   # crash drops are sticky (no arm)
          sup.succeed(f"rm -f {SENTINEL} {REPROMOTED} {READY} /tmp/sel.log")  # a fresh boot, fresh log
          sel_admin(bg=True)
          # maybe_repromote runs BEFORE any launch; with HANGMARK absent it must
          # return early and NOT re-arm the start tier. Wait until the selector has
          # passed re-promotion + reached its launch, then assert no re-promotion fired.
          sup.wait_until_succeeds("grep -q 'launching tier' /tmp/sel.log", timeout=30)
          log = sup.succeed("cat /tmp/sel.log")
          assert "fresh-boot re-promotion" not in log, \
              "a crash-class drop (no hang arm) must be STICKY — never re-promoted"
          # No teardown — this is the last subtest; the VM is torn down next. (A broad
          # pkill here would kill greetd's parked floor selector, not just this run.)
    '';
  };
}
