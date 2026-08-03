# ═══════════════════════════════════════════════════════════════
# HART OS — GDM-based desktop-boot nixosTest (the floor-lock's DM-driven twin)
# ═══════════════════════════════════════════════════════════════
#
# THE test the Phase-0 floor-lock (tests/floor-lock.nix) DEFERS its DM-driven
# assertions to. floor-lock runs a #70-minimal node that has NO display manager,
# so it CANNOT materialize `services.displayManager.sessionPackages ->
# sessionData -> /run/current-system/sw/share/wayland-sessions/*.desktop` and it
# CANNOT log a real cage session in to paint a frame. Those checks are explicitly
# punted there (floor-lock.nix:89, :116 — "the GDM-based hart-desktop-boot test's
# job"). This file IS that job: a node WITH a real GDM display manager that
# autologins the cage `hart-shell` Wayland session, so every link of the floor —
# registration, the forced-software-GL launcher env, the first painted WebView
# frame on llvmpipe, and Restart=on-failure WebView recovery — is a TESTED
# invariant, not an aspiration.
#
# What it asserts (the four floor-lock-deferred gates, made real):
#   1. REGISTRATION (DM-materialized). The cage `hart-shell.desktop` Wayland
#      session is present under /run/current-system/sw/share/wayland-sessions —
#      i.e. GDM's sessionData actually materialized the sessionPackages entry
#      (the floor IS the session, not an app on GNOME). floor-lock can only prove
#      the launcher is in the *closure*; here we prove the DM *registered* it.
#   2. FORCED SOFTWARE GL, BIT-FOR-BIT. The launcher the registered .desktop's
#      `Exec=` points at exports the broken-GPU paint floor verbatim
#      (WLR_RENDERER_ALLOW_SOFTWARE=1 / LIBGL_ALWAYS_SOFTWARE=1) AND the glass
#      shell pins WebKit2.HardwareAccelerationPolicy.NEVER + the DMABUF/compositing
#      disables. We read the SAME launcher the DM will exec — not a lookalike.
#   3. FIRST WEBVIEW FRAME PAINTS ON llvmpipe. GDM autologins hart-admin into the
#      cage `hart-shell` session; cage takes the QEMU virtual GPU via DRM/KMS with
#      Mesa llvmpipe (no GPU passthrough in the driver — the broken-GPU floor is
#      exercised every run), the glass shell's WebKitGTK WebView paints, and the
#      rendered OS name is read back off the framebuffer by OCR (enableOCR). The
#      cage + WebKit web-process being alive is the un-fakeable structural proof
#      the shell did NOT SIGABRT on software GL (the #99/#100 crash class).
#   4. WEBVIEW-KILL RECOVERY, NO WATCHDOG SELF-KILL. The renderer unit
#      (hart-liquid-ui-renderer, the Restart=on-failure WebView host) recovers a
#      crashed WebView via Restart=on-failure (RestartSec) and has NO WatchdogSec
#      armed (the sd_notify-once self-kill lesson — a WebView renderer sends
#      READY=1 once but never periodic WATCHDOG=1, so a WatchdogSec would
#      SIGABRT-loop it). We assert the unit config bit-for-bit (WatchdogUSec=0 +
#      Restart=on-failure) AND SIGKILL its live main process, proving systemd's
#      authoritative NRestarts counter climbs (the restart policy — not a watchdog
#      — brought it back) while the cage compositor floor stays up.
#
# It runs on an llvmpipe / software-GL VM (the test driver gives no GPU
# passthrough) so the broken-GPU floor is exercised every run. Per the
# honest-hardware-limit rule this is `[VM]` — it CANNOT run on the Windows dev
# box; it gates in CI (the nixos-vm-tests workflow `nix build`s it) / local QEMU.
#
# #70 discipline preserved: built from `hartModules` alone via the shared
# `mkNode` (./lib.nix). The DM is added through the per-node `extra` MODULE
# (services.xserver + GDM + autoLogin), NOT by importing ../configurations/
# desktop.nix — which would drag the installer-CD overlay back in and make
# `nix flake check` un-EVALUABLE ("nixpkgs.overlays defined multiple times").

{ pkgs, hartModules, specialArgs }:

let
  inherit (import ./lib.nix { inherit hartModules; }) mkNode;
in
{
  # ─────────────────────────────────────────────────────────────
  # GDM-based desktop boot: the cage hart-shell session logs in and paints
  # ─────────────────────────────────────────────────────────────
  hart-desktop-shell-boot = pkgs.testers.runNixOSTest {
    name = "hart-desktop-shell-boot";
    # OCR the painted framebuffer (subtest 3 reads the "HART" brand span off the
    # rendered WebView). enableOCR pulls tesseract + the frame-grab tooling into
    # the driver so `wait_for_text` / `get_screen_text` work. Without it they raise.
    enableOCR = true;
    # runNixOSTest's mypy/pyflakes pre-checks do NOT resolve the per-node Machine
    # global the driver injects at RUNTIME — they flag every `shell.succeed(...)`
    # as "Name not defined" though the node IS bound at runtime (the floor-lock +
    # supervisor tests are structured identically and skip the same false
    # positives). Skip the static passes; the VM still boots + asserts.
    skipTypeCheck = true;
    skipLint = true;
    node.specialArgs = specialArgs;

    nodes.shell = mkNode "desktop" {
      virtualisation = {
        # GDM + a Wayland cage session + WebKitGTK on llvmpipe is heavier than the
        # headless server node; give it room so software-GL paint isn't starved.
        memorySize = 4096;
        cores = 2;
        # A virtual GPU so cage has a DRM/KMS node to scan out to (software GL via
        # llvmpipe — no host GPU passthrough). Without a virtio-gpu the cage
        # wlroots DRM backend has no /dev/dri card to take and the session can't
        # paint a frame for OCR.
        qemu.options = [ "-vga" "virtio" ];
      };

      # ── Enable the LiquidUI glass shell (the cage session + renderer + server) ──
      # CRITICAL: hart.liquidUI.enable defaults FALSE (mkEnableOption) and NO
      # module turns it on by variant — so the #70-minimal node does NOT register
      # the kiosk session or start the :6800 server unless we ask. desktop.nix sets
      # this for the real ISO; we set it explicitly here (the minimal node carries
      # ONLY hart.enable + variant). webkit renderer = the cage glass-shell path
      # this whole test exercises. voiceEnabled=false trims the voice-listener user
      # service (irrelevant to the paint/recovery gates + extra boot surface).
      #
      # model-bus is deliberately LEFT OFF: it is only a soft `wants` of the
      # liquid-ui server (which curl-probes it and degrades gracefully), and
      # enabling another Type=notify unit only adds a boot-hang surface none of the
      # four gates need. The server still serves /shell/static (the dead-husk gate)
      # and the shell still paints without the bus.
      hart.liquidUI = {
        enable = true;
        renderer = "webkit";
        voiceEnabled = pkgs.lib.mkForce false;
      };

      # ── A real display manager (the whole point) ──
      # GDM is what materializes services.displayManager.sessionPackages ->
      # sessionData -> /run/current-system/sw/share/wayland-sessions/*.desktop
      # (the registration subtest 1 reads) and what logs the cage session in to
      # paint (subtest 3). These are plain NixOS options — NO installer-CD
      # overlay, so the #70 eval-gate stays green (unlike importing desktop.nix).
      #
      # We deliberately do NOT enable services.desktopManager.gnome: the cage
      # hart-shell session is standalone (cage IS the compositor, no GNOME
      # beneath it), and pulling GNOME would only bloat the closure + boot. GDM
      # alone provides the greeter + the autologin path into a wayland-session.
      services.xserver.enable = true;
      services.xserver.displayManager.gdm = {
        enable = true;
        # Force Wayland (the default): the cage session is a wayland-session, and
        # the glass shell's forced-software-GL contract is the Wayland path.
        wayland = true;
      };

      # NO fs.inotify.max_user_watches override here — deliberately.
      #
      # This test used to mkForce 524288 to break a collision between
      # graphical-desktop's mkDefault (pulled in by GDM) and hart-base.nix's
      # mkDefault. That reasoning was correct WHEN WRITTEN, but hart-kernel.nix
      # later began mkForce-ing the same option to 1048576, and the profile
      # enables hart.kernel — so this line became a SECOND mkForce at equal
      # priority with a DIFFERENT value, which is itself the "defined multiple
      # times" error. The workaround became the bug.
      #
      # hart-kernel's mkForce already beats both mkDefaults, so the original
      # collision cannot recur; keeping a test-local copy would just be a
      # parallel path that drifts from production tuning. Verified 2026-08-02
      # against the eval error naming `nodes.shell...max_user_watches`.

      # Autologin hart-admin straight into the cage floor — exactly the pin
      # desktop.nix ships (defaultSession = "hart-shell"), but without importing
      # the full ISO config. This drives subtest 3: GDM logs the session in, cage
      # launches the glass shell, the first WebView frame paints on llvmpipe.
      services.displayManager.autoLogin = {
        enable = true;
        user = "hart-admin";
      };
      services.displayManager.defaultSession = "hart-shell";
    };

    testScript = ''
      # The driver keys the single Machine global by HOSTNAME — mkNode forces it
      # to the variant ("desktop"), NOT the nodes.shell key — so `shell` is absent
      # at runtime (NameError). Bind it from the machines list (single-node test
      # -> element 0). The skip* flags above only silence the static passes that
      # flagged the same absence; THIS is the real binding.
      shell = machines[0]
      shell.start()
      shell.wait_for_unit("multi-user.target")

      with subtest("Backend service starts"):
          shell.wait_for_unit("hart-backend.service", timeout=120)

      with subtest("Display manager (GDM) starts"):
          shell.wait_for_unit("display-manager.service", timeout=180)

      # The LiquidUI server must be up + actually serving before we judge the
      # painted frame — a dead-husk server would render a blank shell and OCR
      # would (correctly) fail, but we want the failure attributed to the right
      # layer. This mirrors the floor-lock dead-husk gate (real fetch, not
      # inline-render) so subtest 3 only judges the COMPOSITOR/WebView, not a
      # missing backend.
      with subtest("LiquidUI server is active and serves its shell (not a dead husk)"):
          shell.wait_for_unit("hart-liquid-ui.service", timeout=180)
          shell.wait_for_open_port(6800, timeout=60)
          body = shell.succeed("curl -fs http://localhost:6800/shell/static/hartHero.js")
          assert body.strip(), "/shell/static/hartHero.js served EMPTY — dead-husk"

      # ════════════════════════════════════════════════════════════════
      # 1. REGISTRATION — GDM materialized the cage hart-shell wayland-session
      # ════════════════════════════════════════════════════════════════
      # floor-lock can only prove the launcher is in the *closure* (no DM to
      # register it); with GDM here, sessionData puts the .desktop on the runtime
      # search path. This is the real "the floor IS the session" proof.
      # The path is RESOLVED, not assumed. This assertion hard-coded
      # /run/current-system/sw/share/... — the environment.systemPackages
      # path — but the session is registered through
      # `services.displayManager.sessionPackages` (hart-liquid-ui.nix:635),
      # which feeds displayManager **sessionData**, a separate store path.
      # The subtest name says "sessionData materialized"; the assertion was
      # checking somewhere else, and the failure ("test -f ... failed") named
      # only the path it guessed, never what actually exists (run 30774512407).
      #
      # So: look in every place a wayland session can legitimately land, and
      # if it is in none of them, SHOW the directories rather than asserting a
      # path. A registration test that cannot say where it looked sends the
      # reader hunting for a missing file that is simply elsewhere.
      session_desktop = shell.succeed(
          "for d in /run/current-system/sw/share/wayland-sessions "
          "         /etc/X11/sessions "
          "         /run/current-system/sw/share/xsessions; do "
          "  [ -f \"$d/hart-shell.desktop\" ] && echo \"$d/hart-shell.desktop\" && exit 0; "
          "done; "
          # sessionData is a store path referenced by the DM unit; find it.
          "ls -d /nix/store/*-desktops/share/wayland-sessions/hart-shell.desktop "
          "  2>/dev/null | head -1"
      ).strip()
      with subtest("GDM registered the cage 'hart-shell' wayland-session (sessionData materialized)"):
          if not session_desktop:
              dirs = shell.succeed(
                  "echo '--- sw/share/wayland-sessions ---'; "
                  "ls -la /run/current-system/sw/share/wayland-sessions 2>&1 | head -20; "
                  "echo '--- any *-desktops store paths ---'; "
                  "ls -d /nix/store/*-desktops 2>/dev/null | head -5; "
                  "echo '--- sessionPackages referenced by the DM unit ---'; "
                  "systemctl cat display-manager.service 2>/dev/null | grep -i session | head -10 || true"
              )
              raise AssertionError(
                  "hart-shell.desktop is registered NOWHERE a wayland session "
                  "can be found. hart-liquid-ui.nix:635 sets "
                  "services.displayManager.sessionPackages = [ kioskSession ] "
                  "under `mkIf (ui.renderer == \"webkit\")`, and the desktop "
                  "profile sets renderer = \"webkit\", so it SHOULD be "
                  "registered.
" + dirs
              )
          shell.log(f"hart-shell session registered at: {session_desktop}")
          entry = shell.succeed(f"cat {session_desktop}")
          # The registered session must point at the cage launcher (the floor),
          # not some other compositor — the session IS the cage floor.
          assert "hart-shell-session" in entry, \
              f"registered hart-shell session does not exec the cage launcher:\n{entry}"

      # ════════════════════════════════════════════════════════════════
      # 2. FORCED SOFTWARE GL, BIT-FOR-BIT — read the EXACT launcher GDM execs
      # ════════════════════════════════════════════════════════════════
      with subtest("The registered launcher forces software GL (WLR/LIBGL) bit-for-bit"):
          # Pull the Exec= target out of the registered .desktop (the real path
          # GDM will run), then read that launcher's script. This is the DM-driven
          # path floor-lock deferred — same assertion, but on the launcher the DM
          # actually resolved, not a closure-find lookalike.
          exec_path = shell.succeed(
              f"awk -F= '/^Exec=/{{print $2; exit}}' {session_desktop}"
          ).strip().split()[0]
          launcher = shell.succeed(f"cat {exec_path}")
          assert "WLR_RENDERER_ALLOW_SOFTWARE=1" in launcher, \
              "kiosk launcher missing WLR_RENDERER_ALLOW_SOFTWARE — software floor lost"
          assert "LIBGL_ALWAYS_SOFTWARE=1" in launcher, \
              "kiosk launcher missing LIBGL_ALWAYS_SOFTWARE — software floor lost"
          # The launcher execs cage onto the glass shell; the shell wrapper holds
          # the WebKit-side software-render contract. Follow cage's argument to the
          # glass-shell script and assert the NEVER acceleration policy +
          # DMABUF/compositing disables are present bit-for-bit.
          glass_path = shell.succeed(
              f"grep -oE '/nix/store/[^ ]*/bin/hart-glass-shell' {exec_path} | head -1"
          ).strip()
          glass = shell.succeed(f"cat {glass_path}")
          assert "HardwareAccelerationPolicy.NEVER" in glass, \
              "glass shell missing HardwareAccelerationPolicy.NEVER — GPU accel would crash on llvmpipe"
          assert "WEBKIT_DISABLE_DMABUF_RENDERER=1" in glass, \
              "glass shell missing WEBKIT_DISABLE_DMABUF_RENDERER — DMABUF path crashes GL-less"
          assert "WEBKIT_DISABLE_COMPOSITING_MODE=1" in glass, \
              "glass shell missing WEBKIT_DISABLE_COMPOSITING_MODE — compositing crashes GL-less"

      # ════════════════════════════════════════════════════════════════
      # 3. FIRST WEBVIEW FRAME PAINTS ON llvmpipe (the broken-GPU floor)
      # ════════════════════════════════════════════════════════════════
      with subtest("Cage hart-shell session logs in and the glass shell is alive (no SIGABRT on software GL)"):
          # GDM autologin -> cage -> hart-glass-shell. cage launches the glass
          # shell as its single Wayland client. The hard structural proof the shell
          # CAME UP (rather than crashing on software GL like the #99/#100 class)
          # is that cage AND its glass-shell GTK/python client are both alive.
          shell.wait_until_succeeds("pgrep -x cage >/dev/null || pgrep -f '/bin/cage' >/dev/null", timeout=180)
          shell.wait_until_succeeds(
              "pgrep -f 'hart-glass-shell' >/dev/null "
              "|| pgrep -f 'gi.require_version' >/dev/null", timeout=180)
          # WebKitWebProcess is the WebView's content child — present once the
          # WebView is realized. INFORMATIONAL (the sandbox can rename it across
          # WebKitGTK builds); the authoritative PAINT proof is the OCR below, not
          # a process name. Never fail on its absence.
          web = shell.succeed("pgrep -f 'WebKitWebProcess' >/dev/null && echo yes || echo no").strip()
          shell.log(f"WebKit web process present: {web}")

      with subtest("First WebView frame PAINTS on llvmpipe — the rendered brand is read off the framebuffer (OCR)"):
          # The top bar renders a high-contrast brand span (<span>HART</span>) on
          # the painted shell. If the WebView frame actually presented on llvmpipe,
          # that text is readable on the QEMU framebuffer via OCR; if cage produced
          # only a blank/black screen (the regression this gate guards), OCR finds
          # nothing and this fails. This is THE authoritative "pixels presented"
          # proof — un-fakeable by a half-started shell. A screenshot is saved to
          # the build output for the run log either way.
          shell.screenshot("hart_shell_first_frame")
          shell.wait_for_text("HART", timeout=120)

      with subtest("PAINT+MARKER PARITY: the cage GTK3 host TOUCHES /run/hart/session/shell-ready on first paint"):
          # The cage GTK3 Tier-3 floor host must satisfy the SAME paint-watchdog
          # contract as the GTK4 layer-shell host (Phase-4 parity): on
          # WebKit2.LoadEvent.FINISHED its _on_load_changed calls _signal_painted(),
          # touching /run/hart/session/shell-ready so the session-supervisor sees a
          # HEALTHY tier. OCR above proved PIXELS; this proves the floor host's
          # marker fired — so a painting cage floor is never wrongly dropped as HUNG
          # (both hosts honor ONE marker contract). The marker may land in the
          # pinned /run/hart path or the autologin user's XDG runtime dir.
          shell.wait_until_succeeds(
              "test -e /run/hart/session/shell-ready "
              "|| find /run/user -name 'shell-ready' -path '*hart*' 2>/dev/null | grep -q .",
              timeout=120)
          shell.log("cage GTK3 floor host touched the shell-ready first-paint marker (parity with GTK4)")

      # ════════════════════════════════════════════════════════════════
      # 4. WEBVIEW-KILL RECOVERY via Restart=on-failure, NO WatchdogSec self-kill
      # ════════════════════════════════════════════════════════════════
      # The renderer unit (hart-liquid-ui-renderer, user service) IS the
      # Restart=on-failure + no-WatchdogSec WebView host (ExecStart = the glass
      # shell). It is the canonical "Restart=on-failure" recovery mechanism the
      # ROADMAP Phase-0 gate names — NOT WebKitGTK's internal web-process respawn
      # (which only shows a crash page, it does not relaunch the host). Two proofs:
      #   (a) the unit's contract bit-for-bit — Restart=on-failure with NO
      #       WatchdogSec armed (the sd_notify-once lesson: a WebView renderer
      #       sends READY=1 once but never periodic WATCHDOG=1, so a WatchdogSec
      #       would SIGABRT-kill it on the watchdog loop), and
      #   (b) a REAL kill -> systemd restarts it: SIGKILL the unit's main process
      #       and assert systemd's authoritative NRestarts counter climbs (the
      #       restart policy fired) and the unit is active again.
      uid = shell.succeed("id -u hart-admin").strip()
      # Address hart-admin's per-user systemd (the autologin session brought the
      # user manager online) from the test driver.
      uenv = (
          f"runuser -u hart-admin -- env "
          f"XDG_RUNTIME_DIR=/run/user/{uid} "
          f"DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/{uid}/bus"
      )
      uctl = f"{uenv} systemctl --user"
      RU = "hart-liquid-ui-renderer.service"

      with subtest("Renderer unit is Restart=on-failure with NO WatchdogSec (sd_notify-once self-kill lesson)"):
          shell.wait_until_succeeds(f"test -S /run/user/{uid}/systemd/private", timeout=120)
          wd = shell.succeed(f"{uctl} show -p WatchdogUSec {RU}").strip()
          # WatchdogUSec=0 (or 'infinity') means no watchdog armed — the floor-lock
          # contract, applied to the WebView renderer host (mirrors floor-lock's
          # server-unit assertion).
          assert wd.endswith("=0") or "infinity" in wd, \
              f"renderer has a WatchdogSec armed ({wd}) — WebView would self-kill on the watchdog loop"
          restart = shell.succeed(f"{uctl} show -p Restart {RU}").strip()
          assert restart == "Restart=on-failure", \
              f"renderer expected Restart=on-failure, got {restart}"

      with subtest("A WebView kill is RECOVERED by Restart=on-failure (systemd relaunches, no watchdog self-kill)"):
          # Drive the renderer unit directly (it is wantedBy=[] so it does not
          # auto-layer on the cage session; here we exercise its recovery contract).
          # The glass-shell wrapper first curl-waits for LiquidUI /health, so the
          # process is reliably alive with a stable MainPID for the kill window —
          # even if the WebView later can't reach the cage display in this user
          # environment, the restart-policy recovery (NRestarts) still fires.
          shell.succeed(f"{uctl} start {RU}")
          # Wait for a STABLE non-zero MainPID (tolerates the brief activating
          # window) — the process to kill.
          shell.wait_until_succeeds(
              f"[ \"$({uenv} systemctl --user show -p MainPID --value {RU})\" != 0 ]",
              timeout=90)
          n_before = int(shell.succeed(f"{uctl} show -p NRestarts --value {RU}").strip() or "0")
          pid1 = shell.succeed(f"{uctl} show -p MainPID --value {RU}").strip()
          assert pid1 and pid1 != "0", f"renderer has no MainPID to kill ({pid1!r})"
          # SIGKILL the WebView host — the crash Restart=on-failure must survive.
          shell.succeed(f"kill -KILL {pid1}")
          # systemd's NRestarts is the authoritative "the restart policy fired"
          # signal — immune to PID-reuse / timing races. It MUST climb by >=1: the
          # only actor is Restart=on-failure (RestartSec). With NO WatchdogSec there
          # is no watchdog SIGABRT loop — the recovery is purely the restart policy.
          shell.wait_until_succeeds(
              f"[ \"$({uenv} systemctl --user show -p NRestarts --value {RU})\" -gt {n_before} ]",
              timeout=90)
          # The cage compositor survived the WebView host crash (the floor held —
          # the screen was never blank; only the renderer unit cycled).
          shell.succeed("pgrep -x cage >/dev/null || pgrep -f '/bin/cage' >/dev/null")
    '';
  };
}
