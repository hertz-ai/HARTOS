# ═══════════════════════════════════════════════════════════════
# HART OS — Phase-4 GTK4 layer-shell glass-shell host nixosTest
# ═══════════════════════════════════════════════════════════════
#
# Proves the budgeted GTK3 → GTK4 WebKitGTK host-window port (ROADMAP Phase 4 /
# HART_OS_NATIVE_ARCHITECTURE §L2 + §7.4): a GTK4 + WebKitGTK-6.0 +
# gtk4-layer-shell host re-hosts the SAME served shell as a wlr-layer-shell
# BACKGROUND surface (exclusive zone 0, JS unchanged) — with its OWN broken-GPU
# software-GL validation on llvmpipe, NOT an inherited assumption.
#
# What it asserts (the Phase-4 deliverables + never-break gates):
#   1. The GTK4 layer-shell session is registered (greeter-selectable), and the
#      module does NOT flip defaultSession — cage GTK3 stays the Tier-3 floor.
#   2. The GTK4 host launcher forces software GL (WLR/LIBGL) AND the GTK4 host
#      script pins the WebKit NEVER-acceleration policy + WEBKIT_DISABLE_* — the
#      GTK4 path's OWN broken-GPU paint floor, bit-for-bit present.
#   3. The GTK4 toolkit GI typelibs are in the closure — Gtk-4.0, WebKit-6.0 (NOT
#      WebKit2-4.1), Gtk4LayerShell-1.0 — so the GTK4 host can actually launch on
#      llvmpipe. (The cage GTK3 floor's Gtk-3.0 + WebKit2-4.1 are ALSO still
#      present: the floor is untouched.)
#   4. **DEAD-HUSK-AWARE HEALTH CHECK** — a REAL HTTP fetch (curl, NOT inline
#      render) of `/shell/static/hartHero.js` over the LiquidUI server (the SAME
#      :6800 the GTK4 host points at) returns 200 + a non-empty body. The GTK4
#      host re-hosts THIS served shell; if `/shell/static/*` 404s the GTK4 desktop
#      is a dead husk too (the f294f52 lesson carried into the GTK4 path).
#   5. NEVER-BREAK: the GTK3 cage Tier-3 floor is intact — its session launcher is
#      realized in the closure and still forces software GL — so a GTK4-host crash
#      ALWAYS drops to a tier that paints (the Phase-1 supervisor's floor; this
#      node proves the floor is present, the supervisor test proves the drop).
#   6. Z-ORDER MODEL (1) is the one in code: the GTK4 host anchors a single
#      BACKGROUND layer-shell surface with exclusive zone 0 (asserted in the host
#      script content), keeping the shell JS unchanged.
#
# It runs the desktop variant on an llvmpipe / software-GL VM (no GPU passthrough
# in the test driver) so the GTK4 broken-GPU floor is exercised every run. Per the
# honest-hardware rule this is `[VM]` — it CANNOT run on the Windows dev box; it
# gates in CI (`nix flake check`) / local QEMU. The dev box only authors +
# source-guards (tests/unit/test_phase4_layer_shell_host.py).
#
# HONEST SCOPE (same shape as floor-lock.nix): the #70-safe minimal node has no
# display manager, so it cannot perform a full greeter LOGIN of the GTK4 session
# (that materializes sessionPackages -> sessionData via GDM's pathsToLink). What
# it CAN prove — and does — is that the GTK4 host + its layer-shell toolkit + the
# served shell it hosts + the GTK3 floor underneath are ALL realized in the system
# closure and correctly hardened. The full GTK4 layer-shell PAINT under a live
# sway session (the surface actually anchoring + first frame) is the GDM-based
# desktop-boot test's job and remains VM-pending; this node is the structural +
# served-asset + floor-intact gate, exactly mirroring floor-lock.nix's honesty.
#
# #70 discipline preserved: built from `hartModules` alone via the shared
# `mkNode` (./lib.nix), NO ../configurations/X.nix installer-CD overlay.

{ pkgs, hartModules, specialArgs }:

let
  inherit (import ./lib.nix { inherit hartModules; }) mkNode;
in
{
  hart-layer-shell-host = pkgs.testers.runNixOSTest {
    name = "hart-layer-shell-host";
    # runNixOSTest's mypy pre-check does NOT resolve the per-node Machine global
    # (`host`) the driver injects at RUNTIME — it flags every `host.succeed(...)`
    # as "Name not defined" though the node IS named `host` and works at runtime
    # (floor-lock.nix / session-supervisor.nix are structured identically). Skip
    # the static pre-check; the VM still boots and the assertions still run.
    skipTypeCheck = true;
    # The pyflakes lint (config.skipLint) ALSO flags the runtime-injected `host`
    # node global as "undefined name" — separate static pass, same false positive.
    skipLint = true;
    node.specialArgs = specialArgs;

    nodes.host = mkNode "desktop" {
      virtualisation = {
        memorySize = 4096;
        cores = 2;
      };
      # Opt the Phase-4 GTK4 layer-shell host ON (default off). The host re-hosts
      # the SAME served glass shell, so hart.layerShellHost asserts
      # hart.liquidUI.enable=true with renderer="webkit". liquidUI defaults OFF and
      # NO variant turns it on (desktop-boot.nix:94) -> enable it explicitly.
      # defaultSession is NOT touched here — the GTK4 path is additive, cage stays
      # the floor (the never-break gate).
      hart.liquidUI = { enable = true; renderer = "webkit"; voiceEnabled = pkgs.lib.mkForce false; };
      hart.layerShellHost.enable = true;
      # gst-inspect-1.0 on PATH so the #150 mic subtest can resolve a real capture
      # element against the host's exported GST_PLUGIN_SYSTEM_PATH_1_0. Test-only —
      # production discovers the plugins via the host's exported search path, not a
      # system tool. (gst_all_1.gstreamer ships gst-inspect-1.0.)
      environment.systemPackages = [ pkgs.gst_all_1.gstreamer ];
    };

    testScript = ''
      # The driver keys the single machine global by its HOSTNAME — mkNode forces
      # it to the variant ("desktop"), NOT the nodes.host key — so the `host` name
      # is absent at runtime (NameError). Bind it from the machines list
      # (single-node test -> element 0). The real fix; skip* above only silence the
      # static passes that flagged the same absence.
      host = machines[0]
      host.start()
      host.wait_for_unit("multi-user.target")

      with subtest("Backend service starts"):
          host.wait_for_unit("hart-backend.service", timeout=120)

      # ── 1. The GTK4 layer-shell session is registered; defaultSession NOT flipped ──
      with subtest("GTK4 layer-shell session launcher is built into the system closure"):
          # The minimal node has no DM to put the launcher on PATH or materialize
          # the .desktop (that needs GDM's pathsToLink). What it CAN assert: the
          # GTK4 session's exec (hart-glass-shell-gtk4-session) is realized in the
          # closure — the same store-find floor-lock.nix uses for the cage session.
          _gtk4_launcher = host.succeed(
              "find /nix/store -maxdepth 4 -name 'hart-glass-shell-gtk4-session' "
              "-type f -print -quit; true").strip()
          assert _gtk4_launcher, \
              "GTK4 layer-shell session launcher not realized in the closure"
          host.log("GTK4 layer-shell launcher in closure: " + _gtk4_launcher)

      # ── 2. The GTK4 host's OWN broken-GPU floor: software GL + NEVER accel ──
      with subtest("GTK4 host forces software GL (WLR/LIBGL) — its OWN broken-GPU floor"):
          launcher = host.succeed("cat " + _gtk4_launcher)
          assert "WLR_RENDERER_ALLOW_SOFTWARE=1" in launcher, \
              "GTK4 session launcher missing WLR_RENDERER_ALLOW_SOFTWARE — software floor lost"
          assert "LIBGL_ALWAYS_SOFTWARE=1" in launcher, \
              "GTK4 session launcher missing LIBGL_ALWAYS_SOFTWARE — software floor lost"

      with subtest("GTK4 host script pins WebKit NEVER-accel + WEBKIT_DISABLE_* (GTK4 paint floor)"):
          # The host binary the launcher runs must disable the DMABUF/compositing
          # GL paths AND pin HardwareAccelerationPolicy.NEVER so WebKitGTK-6.0
          # paints on llvmpipe — the GTK4 path's fresh broken-GPU proof, not an
          # inherited GTK3 assumption.
          host_bin = host.succeed(
              "find /nix/store -maxdepth 4 -name 'hart-glass-shell-gtk4' -type f "
              "-print -quit").strip()
          assert host_bin, "GTK4 host binary not realized in the closure"
          host_src = host.succeed("cat " + host_bin)
          assert "WEBKIT_DISABLE_DMABUF_RENDERER=1" in host_src, \
              "GTK4 host missing WEBKIT_DISABLE_DMABUF_RENDERER — would crash on llvmpipe"
          assert "WEBKIT_DISABLE_COMPOSITING_MODE=1" in host_src, \
              "GTK4 host missing WEBKIT_DISABLE_COMPOSITING_MODE — would crash on llvmpipe"
          assert "HardwareAccelerationPolicy.NEVER" in host_src, \
              "GTK4 host must pin HardwareAccelerationPolicy.NEVER (broken-GPU floor)"
          # GTK4 draws via GSK, whose DEFAULT renderer is GL — a SEPARATE GL context
          # from WebKit's, NOT covered by WEBKIT_DISABLE_* above. On a real GPU that
          # GSK GL/EGL/GBM context hangs on the layer-shell surface (pointer-only
          # black screen); llvmpipe resolves it to software GL so it paints (why this
          # very test passes). Pin GSK to the cairo software renderer + disable GDK
          # GL so the GTK4 host paints on ANY GPU — the GTK3 cage floor is immune
          # only because it has no GSK (cairo-direct). This is the real-HW paint-hang
          # fix; without it the host works on llvmpipe but black-screens on real HW.
          assert "GSK_RENDERER=cairo" in host_src, \
              "GTK4 host missing GSK_RENDERER=cairo — GSK's GL renderer hangs on a real GPU"
          assert "GDK_GL=disable" in host_src, \
              "GTK4 host missing GDK_GL=disable — GDK would still create a GL context"

      with subtest("GTK4 host wires the first-paint marker (load-changed -> shell-ready)"):
          # The session-supervisor's paint-watchdog drops a tier to the cage floor
          # if /run/hart/session/shell-ready is not touched within its budget. The
          # host connects 'load-changed' to _on_load_changed; that handler MUST exist
          # and call _signal_painted() on LoadEvent.FINISHED, or the marker never
          # fires and a HEALTHY GTK4 tier is wrongly dropped as HUNG (the other half
          # of the pointer-only regression).
          assert "def _on_load_changed" in host_src, \
              "GTK4 host connects load-changed but never DEFINES _on_load_changed — marker never fires"
          assert "_signal_painted()" in host_src, \
              "GTK4 host never CALLS _signal_painted() — shell-ready marker never fires, watchdog drops the tier"
          assert "WebKit.LoadEvent.FINISHED" in host_src, \
              "GTK4 host must signal paint on WebKit.LoadEvent.FINISHED (first-frame marker)"

      # ── 3. The GTK4 toolkit GI typelibs are present so the host can launch ──
      with subtest("GTK4 host GI typelibs present (Gtk-4.0 + WebKit-6.0 + Gtk4LayerShell-1.0)"):
          host.succeed("find /nix/store -name 'Gtk-4.0.typelib' -print -quit | grep -q .")
          # WebKitGTK 6.0 ships 'WebKit-6.0.typelib' (the GTK4 binding) — NOT the
          # GTK3 'WebKit2-4.1.typelib'. This is the toolkit port made concrete.
          host.succeed("find /nix/store -name 'WebKit-6.0.typelib' -print -quit | grep -q .")
          host.succeed("find /nix/store -name 'Gtk4LayerShell-1.0.typelib' -print -quit | grep -q .")

      # ── 3b. #150 MIC: the host wires a GStreamer capture path with a real source ──
      # WebKitGTK 6.0 does getUserMedia capture via GStreamer. The host's minimal
      # env never set GST_PLUGIN_SYSTEM_PATH_1_0, so WebKit found NO audio source
      # element and the mic was DENIED on real HW despite the permission handler
      # allowing it + PipeWire being up. Prove BEHAVIOURALLY that (1) the host now
      # exports the GStreamer plugin search path and (2) a real capture element
      # actually RESOLVES on it — i.e. WebKit has a mic source to bind. This is the
      # un-fakeable half: a path that points at nothing would pass a grep but fail
      # gst-inspect.
      with subtest("GTK4 host exports GST_PLUGIN_SYSTEM_PATH_1_0 (mic capture plugin path)"):
          assert "GST_PLUGIN_SYSTEM_PATH_1_0" in host_src, \
              "GTK4 host missing GST_PLUGIN_SYSTEM_PATH_1_0 — WebKit getUserMedia has no capture source"

      with subtest("a real GStreamer audio capture element resolves on the host's plugin path (#150)"):
          # Parse the exported value straight out of the host script we already
          # cat'd (host_src) — no shell-quoting games. The export line is
          # GST_PLUGIN_SYSTEM_PATH_1_0="<path>".
          assert 'GST_PLUGIN_SYSTEM_PATH_1_0="' in host_src
          gst_path = host_src.split('GST_PLUGIN_SYSTEM_PATH_1_0="', 1)[1].split('"', 1)[0]
          assert gst_path, "could not extract the host's GST_PLUGIN_SYSTEM_PATH_1_0 value"
          # gst-inspect-1.0 LOADS the element off that path — proves the capture
          # source is really there (pulsesrc via PipeWire-pulse, else native
          # pipewiresrc), not just a path string. Either capture element satisfies
          # WebKit's getUserMedia audio source requirement.
          host.succeed(
              "GST_PLUGIN_SYSTEM_PATH_1_0='" + gst_path + "' gst-inspect-1.0 pulsesrc >/dev/null 2>&1 "
              "|| GST_PLUGIN_SYSTEM_PATH_1_0='" + gst_path + "' gst-inspect-1.0 pipewiresrc >/dev/null")

      # ── 4. LiquidUI server is active and serves /shell/static (DEAD-HUSK CHECK) ──
      # The GTK4 host re-hosts THIS served shell (:6800). The same dead-husk guard
      # as the floor: render produces HTML, but if /shell/static 404s the GTK4
      # desktop is a husk too. Inline-render is BLIND to this — only a real fetch
      # catches it.
      with subtest("LiquidUI server is active (the served shell the GTK4 host points at)"):
          host.wait_for_unit("hart-liquid-ui.service", timeout=180)
          host.wait_for_open_port(6800, timeout=60)

      with subtest("DEAD-HUSK-AWARE: a REAL /shell/static fetch returns 200 + non-empty body"):
          body = host.succeed(
              "curl -fs http://localhost:6800/shell/static/hartHero.js")
          assert body.strip(), \
              "/shell/static/hartHero.js served EMPTY — GTK4 desktop would be a dead husk"
          host.succeed("curl -fs http://localhost:6800/ -o /dev/null")

      with subtest("Served shell page references /shell/static assets (GTK4 host renders them)"):
          page = host.succeed("curl -fs http://localhost:6800/")
          assert "/shell/static/" in page, \
              "rendered shell references no /shell/static assets — render changed?"

      # ── 5. NEVER-BREAK: the GTK3 cage Tier-3 floor is intact (crash drops to it) ──
      with subtest("GTK3 cage floor UNCHANGED: cage launcher realized + still software-GL"):
          # A GTK4-host crash must ALWAYS land on a tier that paints. The Phase-1
          # supervisor owns the DROP; THIS node proves the FLOOR it drops to is
          # present and hardened. The cage GTK3 session launcher + its software-GL
          # env must still be in the closure (byte-for-byte the audited floor).
          cage_launcher = host.succeed(
              "find /nix/store -maxdepth 4 -name 'hart-shell-session' -type f "
              "-print -quit; true").strip()
          assert cage_launcher, "cage GTK3 floor launcher missing from the closure — floor lost"
          cage_src = host.succeed("cat " + cage_launcher)
          assert "WLR_RENDERER_ALLOW_SOFTWARE=1" in cage_src, \
              "cage GTK3 floor lost its software-GL env — never-break gate violated"
          # The GTK3 floor's own toolkit typelibs (the cage path) are STILL present
          # — the GTK4 port is additive, it did not replace the floor's toolkit.
          host.succeed("find /nix/store -name 'Gtk-3.0.typelib' -print -quit | grep -q .")
          host.succeed("find /nix/store -name 'WebKit2-4.1.typelib' -print -quit | grep -q .")

      # ── 6. Z-order model (1) is the one in code: single BACKGROUND layer, zone 0 ──
      with subtest("GTK4 host anchors a single BACKGROUND layer-shell surface, exclusive zone 0"):
          # ROADMAP Phase 4 demands ONE z-order model chosen in code. Model (1):
          # one layer-shell surface anchored BACKGROUND with exclusive zone 0,
          # overlays/orb co-planar (JS unchanged). Assert the host script encodes
          # exactly that (the model decision is load-bearing, not prose).
          assert "Layer.BACKGROUND" in host_src, \
              "GTK4 host must anchor the BACKGROUND layer (Model 1 — the desktop plane)"
          # Match the EXACT zero-zone call, not `"0" in host_src` (a tautology — any
          # source contains a '0'). The exclusive zone must be set to 0 on the host
          # window: `set_exclusive_zone(self._win, 0)`.
          assert "set_exclusive_zone(self._win, 0)" in host_src, \
              "GTK4 host must call set_exclusive_zone(self._win, 0) (backdrop, not a panel) — Model 1"
          # And it must NOT have silently forked into a second top-layer WebView
          # (that would be Model 2, which breaks 'JS unchanged' — explicitly not
          # chosen here).
          assert host_src.count("WebKit.WebView()") == 1, \
              "GTK4 host created >1 WebView — Model 2 was not chosen (JS-unchanged broken)"

      # ── 7. DRY: Tier-1 (hart-comp) + Tier-2 (sway/layer-shell) run the SAME host ──
      with subtest("Tier-1 (hart-comp) and Tier-2 (layer-shell) launch the SAME hart-glass-shell-gtk4 binary"):
          # The whole point of the layer-shell host: ONE glass host window every
          # higher tier re-hosts the served shell through — not a per-tier copy. The
          # Tier-2 layer-shell session execs the GTK4 host via its sway config; the
          # Tier-1 hart-comp session launcher runs the SAME `hart-glass-shell-gtk4`
          # binary (found on PATH — the layer-shell host adds it to systemPackages).
          # Assert BOTH session commands reference the identical binary basename, so
          # a rename in one tier can't silently fork the host (a parity regression).
          comp_launcher = host.succeed(
              "find /nix/store -maxdepth 4 -name 'hart-comp-session' -type f "
              "-print -quit; true").strip()
          # hart-comp is opt-in and OFF on this node, but its session launcher is
          # only realized in the closure when armed; treat absence as informational
          # (the binary-NAME parity is the load-bearing invariant either way).
          if comp_launcher:
              comp_src = host.succeed("cat " + comp_launcher)
              assert "hart-glass-shell-gtk4" in comp_src, \
                  "Tier-1 hart-comp session does not launch the SAME hart-glass-shell-gtk4 host as Tier-2"
              host.log("Tier-1 hart-comp session references hart-glass-shell-gtk4 (same host as Tier-2)")
          else:
              host.log("hart-comp session launcher not in closure (Tier-1 disabled here) — name-parity asserted in the unit guard")
          # The Tier-2 sway host config (the layer-shell session's single client)
          # MUST exec the same binary basename — read it back off the closure.
          # -maxdepth 6, not 3. A config installed into a package lands at
          #     /nix/store/<hash>-name/etc/sway/hart-gtk4-layer-host.conf
          # which is FOUR levels below /nix/store, so `-maxdepth 3` could never
          # see it and the assertion reported "not realized" for a file that
          # was present (run 30774512407). The sibling launcher check three
          # lines below already used -maxdepth 4 — the two disagreed about how
          # deep the same closure is.
          #
          # `|| true` on find: a permission error inside /nix/store must not
          # abort the search and masquerade as absence.
          sway_conf = host.succeed(
              "find /nix/store -maxdepth 6 -name 'hart-gtk4-layer-host.conf' "
              "-print -quit 2>/dev/null || true").strip()
          if not sway_conf:
              # Self-describing: say what IS there. "not realized" alone cannot
              # distinguish "never built" from "built somewhere I did not look",
              # and those have opposite fixes.
              probe = host.succeed(
                  "echo '--- any hart-gtk4* in the store ---'; "
                  "find /nix/store -maxdepth 6 -name 'hart-gtk4*' -print 2>/dev/null | head -10; "
                  "echo '--- any *layer-host* ---'; "
                  "find /nix/store -maxdepth 6 -name '*layer-host*' -print 2>/dev/null | head -10; "
                  "echo '--- sway configs ---'; "
                  "find /nix/store -maxdepth 6 -name '*.conf' -path '*sway*' -print 2>/dev/null | head -10 || true"
              )
              raise AssertionError(
                  "Tier-2 sway host config (hart-gtk4-layer-host.conf) not "
                  "found in the closure.\n" + probe)
          conf_src = host.succeed("cat " + sway_conf)
          assert "hart-glass-shell-gtk4" in conf_src, \
              "Tier-2 sway host config does not exec the hart-glass-shell-gtk4 host binary"
    '';
  };

  # ═══════════════════════════════════════════════════════════════
  # GTK4 layer-shell host — the FRESH broken-GPU PAINT proof (GDM-driven)
  # ═══════════════════════════════════════════════════════════════
  #
  # The structural node above (no display manager, #70-minimal mkNode) proves the
  # closure + the served /shell/static + the GTK3 floor — but it CANNOT log the
  # GTK4 session in, so it cannot prove the GTK4 host actually PAINTS a frame on
  # llvmpipe. ROADMAP Phase 4 demands a FRESH broken-GPU paint proof under the
  # GTK4 NEVER-acceleration equivalents — NOT an inherited GTK3 assumption. This is
  # that proof: a node WITH a real GDM that autologins the `hart-glass-gtk4`
  # wayland-session (sway hosting the GTK4 + WebKitGTK-6.0 + gtk4-layer-shell host
  # as its single layer-shell client), so the GTK4 BACKGROUND surface anchors and
  # the served shell's brand text is read back off the QEMU framebuffer by OCR.
  #
  # It mirrors the cage GTK3 desktop-boot.nix paint proof (subtest 3/4) EXACTLY,
  # but on the GTK4 layer-shell session — the toolkit port's OWN paint floor, not
  # the floor's. The same un-fakeable signal: if the GTK4 host SIGABRTed on
  # software GL (the #99/#100 crash class re-litigated on an unproven GTK4 stack),
  # OCR finds nothing and this fails. A screenshot is saved either way.
  #
  # Separate test (distinct attr) so the structural node stays fast + DM-free and
  # this heavier GDM+sway+WebKitGTK-6.0 paint node is isolated. #70 discipline is
  # identical: built from hartModules via the shared mkNode; the DM is added via
  # the per-node `extra` MODULE (services.xserver + GDM + autoLogin), NOT by
  # importing ../configurations/desktop.nix (which drags the installer-CD overlay).
  #
  # [VM] — CANNOT run on the Windows dev box; gates in CI (the nixos-vm-tests
  # workflow `nix build`s it) / local QEMU on an llvmpipe software-GL VM.
  hart-layer-shell-host-paint = pkgs.testers.runNixOSTest {
    name = "hart-layer-shell-host-paint";
    # OCR the painted framebuffer (the GTK4 surface renders the "HART" brand span).
    # enableOCR pulls tesseract + frame-grab tooling into the driver so
    # wait_for_text / get_screen_text work; without it they raise.
    enableOCR = true;
    # Same runtime-injected-Machine-global false positives the structural node
    # documents (mypy + pyflakes flag `paint.succeed(...)` as undefined though the
    # node IS bound at runtime). Skip the static passes; the VM still boots+asserts.
    skipTypeCheck = true;
    skipLint = true;
    node.specialArgs = specialArgs;

    nodes.paint = mkNode "desktop" {
      virtualisation = {
        # GDM + sway + a GTK4 WebKitGTK-6.0 layer-shell host on llvmpipe is heavier
        # than the structural node; give it room so software-GL paint isn't starved.
        memorySize = 4096;
        cores = 2;
        # A virtual GPU so sway has a DRM/KMS node to scan out to (software GL via
        # llvmpipe — no host GPU passthrough). Without virtio-gpu the wlroots DRM
        # backend has no /dev/dri card and the GTK4 surface can't paint for OCR.
        qemu.options = [ "-vga" "virtio" ];
      };

      # Opt the Phase-4 GTK4 layer-shell host ON (default off). voiceEnabled is
      # left at the desktop default; it is irrelevant to the paint/crash gates.
      # hart.layerShellHost asserts hart.liquidUI.enable=true + renderer="webkit"
      # (it re-hosts the served glass shell); liquidUI defaults OFF, so enable it.
      hart.liquidUI = { enable = true; renderer = "webkit"; voiceEnabled = pkgs.lib.mkForce false; };
      hart.layerShellHost.enable = true;

      # ── A real display manager that autologins the GTK4 layer-shell session ──
      # GDM materializes services.displayManager.sessionPackages -> sessionData ->
      # /run/current-system/sw/share/wayland-sessions/*.desktop AND logs the
      # hart-glass-gtk4 session in to paint. Plain NixOS options — NO installer-CD
      # overlay, so the #70 eval-gate stays green (unlike importing desktop.nix).
      # We do NOT enable GNOME: the GTK4 layer-shell session is standalone (sway IS
      # the compositor hosting the layer-shell surface, no GNOME beneath it).
      services.xserver.enable = true;
      services.xserver.displayManager.gdm = {
        enable = true;
        # The GTK4 layer-shell session is a wayland-session; force Wayland (default).
        wayland = true;
      };
      # Autologin hart-admin straight into the GTK4 layer-shell session — the
      # never-fail invariant is intact (defaultSession is NOT flipped in the module;
      # we point it here, in the TEST node only, to exercise the GTK4 PAINT path).
      # cage stays the production default (desktop.nix); this is a test-local pin.
      services.displayManager.autoLogin = {
        enable = true;
        user = "hart-admin";
      };
      services.displayManager.defaultSession = "hart-glass-gtk4";

      # NO fs.inotify.max_user_watches override here — see the matching note in
      # desktop-boot.nix. Briefly: this mkForce 524288 was written to break a
      # two-mkDefault collision, but hart-kernel.nix now mkForces the same
      # option to 1048576 and the profile enables hart.kernel, so this line
      # became a SECOND equal-priority mkForce with a different value — the
      # very "defined multiple times" error it was meant to prevent.
      # hart-kernel's mkForce already beats both mkDefaults.
    };

    testScript = ''
      # Bind the runtime Machine global from the machines list (mkNode forces the
      # hostname to the variant "desktop", not the nodes.paint key — single-node
      # test -> element 0). skip* above only silence the static passes.
      paint = machines[0]
      paint.start()
      paint.wait_for_unit("multi-user.target")

      with subtest("Backend service starts"):
          paint.wait_for_unit("hart-backend.service", timeout=120)

      with subtest("Display manager (GDM) starts"):
          paint.wait_for_unit("display-manager.service", timeout=180)

      # The GTK4 host re-hosts THIS served shell (:6800). Prove it serves before we
      # judge the painted frame — a dead-husk server would render blank and OCR
      # would (correctly) fail, but we want the failure attributed to the right
      # layer. The SAME dead-husk gate (real fetch, not inline render) as the floor.
      with subtest("LiquidUI server is active and serves its shell (not a dead husk)"):
          paint.wait_for_unit("hart-liquid-ui.service", timeout=180)
          paint.wait_for_open_port(6800, timeout=60)
          body = paint.succeed("curl -fs http://localhost:6800/shell/static/hartHero.js")
          assert body.strip(), "/shell/static/hartHero.js served EMPTY — dead-husk"

      # ════════════════════════════════════════════════════════════════
      # 1. REGISTRATION — GDM materialized the GTK4 hart-glass-gtk4 session
      # ════════════════════════════════════════════════════════════════
      # RESOLVED, not guessed — identical treatment to desktop-boot.nix
      # (0725adca). This hard-coded the environment.systemPackages path while
      # the session is registered through
      # `services.displayManager.sessionPackages`, which feeds displayManager
      # sessionData: a different store path. The subtest name already said
      # "sessionData materialized"; only the assertion disagreed, and its
      # failure named just the guessed path (run 30774512407).
      session_desktop = paint.succeed(
          "for d in /run/current-system/sw/share/wayland-sessions "
          "         /etc/X11/sessions "
          "         /run/current-system/sw/share/xsessions; do "
          "  [ -f \"$d/hart-glass-gtk4.desktop\" ] && echo \"$d/hart-glass-gtk4.desktop\" && exit 0; "
          "done; "
          "ls -d /nix/store/*-desktops/share/wayland-sessions/hart-glass-gtk4.desktop "
          "  2>/dev/null | head -1"
      ).strip()
      with subtest("GDM registered the GTK4 'hart-glass-gtk4' wayland-session (sessionData materialized)"):
          if not session_desktop:
              dirs = paint.succeed(
                  "echo '--- sw/share/wayland-sessions ---'; "
                  "ls -la /run/current-system/sw/share/wayland-sessions 2>&1 | head -20; "
                  "echo '--- any *-desktops store paths ---'; "
                  "ls -d /nix/store/*-desktops 2>/dev/null | head -5"
              )
              raise AssertionError(
                  "hart-glass-gtk4.desktop is registered NOWHERE a wayland "
                  "session can be found — showing what IS present rather than "
                  "only the path this test guessed.\n" + dirs
              )
          paint.log(f"hart-glass-gtk4 session registered at: {session_desktop}")
          entry = paint.succeed(f"cat {session_desktop}")
          # The registered session must exec the GTK4 layer-shell launcher (sway +
          # the GTK4 host), not some other compositor.
          assert "hart-glass-shell-gtk4-session" in entry, \
              f"registered hart-glass-gtk4 session does not exec the GTK4 launcher:\n{entry}"

      # ════════════════════════════════════════════════════════════════
      # 2. FORCED SOFTWARE GL, BIT-FOR-BIT — read the EXACT launcher GDM execs
      # ════════════════════════════════════════════════════════════════
      with subtest("The registered GTK4 launcher forces software GL (WLR/LIBGL) bit-for-bit"):
          exec_path = paint.succeed(
              f"awk -F= '/^Exec=/{{print $2; exit}}' {session_desktop}"
          ).strip().split()[0]
          launcher = paint.succeed(f"cat {exec_path}")
          assert "WLR_RENDERER_ALLOW_SOFTWARE=1" in launcher, \
              "GTK4 launcher missing WLR_RENDERER_ALLOW_SOFTWARE — software floor lost"
          assert "LIBGL_ALWAYS_SOFTWARE=1" in launcher, \
              "GTK4 launcher missing LIBGL_ALWAYS_SOFTWARE — software floor lost"
          # The launcher runs sway onto the GTK4 host; the host script holds the
          # WebKit-side software-render contract. Follow it and assert the GTK4 host
          # pins HardwareAccelerationPolicy.NEVER + the DMABUF/compositing disables.
          host_path = paint.succeed(
              "grep -oE '/nix/store/[^ ]*/bin/hart-glass-shell-gtk4' "
              f"$(grep -oE '/nix/store/[^ ]*hart-gtk4-layer-host.conf' {exec_path} | head -1) "
              "| head -1").strip()
          host_src = paint.succeed(f"cat {host_path}")
          assert "HardwareAccelerationPolicy.NEVER" in host_src, \
              "GTK4 host missing HardwareAccelerationPolicy.NEVER — GPU accel would crash on llvmpipe"
          assert "WEBKIT_DISABLE_DMABUF_RENDERER=1" in host_src, \
              "GTK4 host missing WEBKIT_DISABLE_DMABUF_RENDERER — DMABUF path crashes GL-less"
          assert "WEBKIT_DISABLE_COMPOSITING_MODE=1" in host_src, \
              "GTK4 host missing WEBKIT_DISABLE_COMPOSITING_MODE — compositing crashes GL-less"
          # And the in-code Z-ORDER MODEL (1) anchoring is the one being painted —
          # the EXACT zone-0 call, not a bare `set_exclusive_zone` substring.
          assert "Layer.BACKGROUND" in host_src and "set_exclusive_zone(self._win, 0)" in host_src, \
              "GTK4 host being painted is not the Model-1 BACKGROUND/zone-0 surface"

      # ════════════════════════════════════════════════════════════════
      # 3. FIRST GTK4 LAYER-SHELL FRAME PAINTS ON llvmpipe (the fresh proof)
      # ════════════════════════════════════════════════════════════════
      with subtest("GTK4 layer-shell session logs in: sway + the GTK4 host are alive (no SIGABRT on software GL)"):
          # GDM autologin -> hart-glass-shell-gtk4-session -> sway -> the GTK4 host.
          # The hard structural proof the GTK4 host CAME UP (rather than crashing on
          # software GL like the #99/#100 class, now on an unproven GTK4 stack) is
          # that sway AND its GTK4 layer-shell client are both alive.
          paint.wait_until_succeeds("pgrep -x sway >/dev/null", timeout=180)
          paint.wait_until_succeeds(
              "pgrep -f 'hart-glass-shell-gtk4' >/dev/null "
              "|| pgrep -f 'GlassShellLayer' >/dev/null "
              "|| pgrep -f 'gi.require_version' >/dev/null", timeout=180)
          # WebKitWebProcess is the WebView content child — present once the GTK4
          # WebView realizes. INFORMATIONAL (the sandbox can rename it across
          # WebKitGTK builds); the authoritative paint proof is the OCR below.
          web = paint.succeed("pgrep -f 'WebKitWebProcess' >/dev/null && echo yes || echo no").strip()
          paint.log(f"WebKit web process present: {web}")

      with subtest("First GTK4 layer-shell frame PAINTS on llvmpipe — the rendered brand is read off the framebuffer (OCR)"):
          # The served shell renders a high-contrast brand span ("HART") on the
          # painted surface. If the GTK4 layer-shell BACKGROUND surface actually
          # presented on llvmpipe under NEVER-accel, that text is readable on the
          # QEMU framebuffer via OCR; if the GTK4 host produced only blank/black
          # (the regression this fresh proof guards), OCR finds nothing and this
          # fails. THE authoritative "pixels presented" proof for the GTK4 path —
          # un-fakeable by a half-started host. A screenshot is saved either way.
          paint.screenshot("hart_gtk4_layer_shell_first_frame")
          paint.wait_for_text("HART", timeout=120)

      with subtest("PAINT+MARKER E2E: the GTK4 host TOUCHES /run/hart/session/shell-ready on first paint"):
          # The full paint+marker contract, end-to-end on a live session. OCR above
          # proves PIXELS presented; this proves the GTK4 host's _on_load_changed
          # actually fired _signal_painted() on LoadEvent.FINISHED and the
          # session-supervisor's HUNG-tier guard sees a HEALTHY Tier-2 (so a
          # painting GTK4 surface is NOT wrongly dropped to cage). Without the marker
          # handler the surface paints but the watchdog still escalates — the OTHER
          # half of the pointer-only regression. The marker lives under the GTK4
          # host's autologin user runtime, so look in BOTH the pinned /run/hart
          # contract path and any per-user XDG_RUNTIME_DIR the supervisor may pass.
          paint.wait_until_succeeds(
              "test -e /run/hart/session/shell-ready "
              "|| find /run/user -name 'shell-ready' -path '*hart*' 2>/dev/null | grep -q .",
              timeout=120)
          paint.log("GTK4 host touched the shell-ready first-paint marker")

      # ════════════════════════════════════════════════════════════════
      # 4. NEVER-BREAK: a GTK4-host crash drops to a tier that PAINTS (cage floor)
      # ════════════════════════════════════════════════════════════════
      # The Phase-1 supervisor owns the actual tier DROP; this asserts the FLOOR it
      # drops to is present + can paint. We kill the live GTK4 host and prove (a)
      # the cage GTK3 floor launcher is realized + still software-GL (the tier a
      # GTK4 crash lands on), and (b) the served shell — which the cage floor also
      # hosts — still serves, so the post-drop screen is NOT blank.
      with subtest("Kill the GTK4 host -> the cage GTK3 floor is present + still software-GL (crash lands on a painting tier)"):
          # SIGKILL the GTK4 layer-shell host (the crash the floor must survive).
          paint.succeed(
              "pkill -KILL -f 'hart-glass-shell-gtk4' "
              "|| pkill -KILL -f 'GlassShellLayer' || true")
          cage_launcher = paint.succeed(
              "find /nix/store -maxdepth 4 -name 'hart-shell-session' -type f "
              "-print -quit; true").strip()
          assert cage_launcher, "cage GTK3 floor launcher missing — a GTK4 crash has no tier to land on"
          cage_src = paint.succeed(f"cat {cage_launcher}")
          assert "WLR_RENDERER_ALLOW_SOFTWARE=1" in cage_src, \
              "cage GTK3 floor lost its software-GL env — never-break gate violated"
          # The cage floor's GTK3 toolkit typelibs are present (the tier it drops to
          # can launch its own WebView); the GTK4 port is additive, not a swap.
          paint.succeed("find /nix/store -name 'WebKit2-4.1.typelib' -print -quit | grep -q .")
          # The served shell the cage floor re-hosts still serves (post-drop screen
          # is not a dead husk) — the dead-husk gate, re-checked after the crash.
          body2 = paint.succeed("curl -fs http://localhost:6800/shell/static/hartHero.js")
          assert body2.strip(), "/shell/static served EMPTY after GTK4 crash — drop tier would be a dead husk"
    '';
  };
}
