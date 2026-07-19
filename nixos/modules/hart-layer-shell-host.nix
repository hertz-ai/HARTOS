{ config, lib, pkgs, hartSrc ? /etc/hart, ... }:

# ════════════════════════════════════════════════════════════════════════════
# HART OS — Phase 4: glass shell as a real wlr-layer-shell BACKGROUND surface
#           (the GTK3 → GTK4 WebKitGTK host-window port)
# ════════════════════════════════════════════════════════════════════════════
#
# WHY (ROADMAP.md Phase 4 + HART_OS_NATIVE_ARCHITECTURE §L2 + §7.4):
#
#   The cage Tier-3 floor (hart-liquid-ui.nix) runs the glass shell as a GTK3
#   single FULLSCREEN `Gtk.Window` (WebKit2-4.1). That is the audited never-fail
#   floor and it is kept VERBATIM. It is NOT a true wlr-layer-shell surface — it
#   cannot anchor the desktop BELOW agent-placed native toplevels.
#
#   This module is the budgeted GTK3 → GTK4 host-window port the architecture
#   names: a GTK4 + WebKitGTK-6.0 + `gtk4-layer-shell` host window pointed at the
#   SAME served shell (`LiquidUIService :6800` `render_desktop_shell` +
#   `/shell/static`, JS served UNCHANGED), anchored as a BACKGROUND layer with
#   exclusive zone 0 — so native windows (Phase 5) sit ABOVE the desktop while
#   the shell still IS the desktop. It re-litigates the never-fail software-GL
#   paint floor on an UNPROVEN GTK4 stack with its OWN broken-GPU paint proof —
#   this is NOT a config flag and NOT "same WebView", it is a real toolkit port.
#
# ── Z-ORDER MODEL — PICKED IN CODE: MODEL (1) ─────────────────────────────────
#   ROADMAP Phase 4 demands ONE z-order model be chosen in code. We choose
#   MODEL (1): a SINGLE layer-shell surface (one WebView) anchored BACKGROUND,
#   with the voice orb / A2UI overlays / hero CO-PLANAR inside that same WebView.
#
#   Consequence, stated HONESTLY (architecture §7.4): native app windows sit
#   ABOVE the orb/overlays — the orb does NOT float over a focused native window.
#   We accept that limitation DELIBERATELY because it is the ONLY model that keeps
#   the shell's JS UNCHANGED. Model (2) (two WebViews: a background desktop + a
#   top-layer overlay surface) would SEVER the implicit single-window sharing of
#   the `window.*` globals every shell module reaches across (`openPanel`,
#   `acSend`, `HartSession`, `toggleVoice`, `renderAgentOverlay`) and force a real
#   cross-WebView message bus — at which point "JS untouched" is false and the bus
#   must be budgeted. The task contract here is "JS unchanged", so Model (1) is the
#   correct (and only consistent) choice. Promoting the orb to its own top layer is
#   a future Model-(2) upgrade with its own budget, NOT this port.
#
# NEVER-FAIL POSITION (ROADMAP §6 tiering — INVARIANT):
#   Tier-1 = HART-comp (Smithay) → Tier-2 = sway (hart-sway-tier1.nix) →
#   Tier-3/FLOOR = cage + forced-software-GL (hart-liquid-ui.nix, GTK3 path,
#   audited bit-for-bit). This GTK4 layer-shell host is an L2 host-window the
#   higher tiers can adopt; it becomes a greeter-selectable session ONLY, and
#   ONLY after its broken-GPU paint proof passes in CI. **defaultSession STAYS
#   cage.** Nothing here flips it. A GTK4-host crash drops to the GTK3 cage Tier-3
#   under the Phase-1 supervisor (the Phase-4 nixosTest proves this).
#
# STATUS: AUTHORED ON A WINDOWS DEV BOX — NOT BOOTED HERE.
#   No Wayland/wlroots/gtk4-layer-shell/WebKitGTK paint can run on Windows. This
#   Nix expression + the embedded GTK4 host script are authored + structurally
#   validated (test_phase4_layer_shell_host.py + this module's source-guards); the
#   real boot + layer-shell anchoring + the GTK4 broken-GPU paint proof are ALL
#   VM/CI-pending (Linux nixosTest on llvmpipe software-GL, or local QEMU-KVM).
#   Opt-in, default OFF.
#
# DRY / no-parallel-path:
#   - REUSES the SAME served shell (`render_desktop_shell` + `/shell/static`,
#     :6800) the cage floor serves — there is ONE renderer of the HTML/JS. The
#     SECOND host WINDOW (GTK4 vs GTK3) is the explicitly-budgeted toolkit port,
#     NOT a second copy of the shell. The GTK3 cage path is untouched/verbatim.
#   - REUSES the SAME software-GL hardening contract as cage Tier-3 + sway Tier-2
#     + hart-comp (WLR_RENDERER_ALLOW_SOFTWARE / LIBGL_ALWAYS_SOFTWARE /
#     WEBKIT_DISABLE_DMABUF_RENDERER / WEBKIT_DISABLE_COMPOSITING_MODE /
#     HardwareAccelerationPolicy.NEVER) — a kiosk MUST paint on any GPU.
#   - REUSES the SAME `:6800`→Nunba-SPA fallback URL probe the cage wrapper uses,
#     so the GTK4 host is NEVER a blank surface (the dead-husk lesson).

let
  cfg = config.hart;
  ui = config.hart.liquidUI;
  host = config.hart.layerShellHost;

  liquidPort = toString (ui.port or 6800);
  nunbaPort = toString (config.hart.nunba.port or 5000);

  # GObject-introspection typelibs the GTK4 host's gi.require_version needs. The
  # GTK4 stack is a DIFFERENT typelib set than the cage GTK3 floor: GTK4 (Gtk-4.0),
  # WebKitGTK 6.0 (WebKit-6.0, NOT WebKit2-4.1), and gtk4-layer-shell
  # (Gtk4LayerShell-1.0). Same makeSearchPathOutput "out" lesson as the cage floor:
  # several of these (glib, gtk4, ...) have a non-`out` DEFAULT output, so plain
  # makeSearchPath points at the wrong store path and `gi` fails with
  # "Typelib file for namespace 'GObject' not found" (the glass-shell SIGABRT class
  # #99-103). gtk4-layer-shell ships its typelib in `out`.
  giTypelibPath = lib.makeSearchPathOutput "out" "lib/girepository-1.0" (with pkgs; [
    glib gobject-introspection gtk4 webkitgtk_6_0 gtk4-layer-shell
    pango gdk-pixbuf graphene harfbuzz libsoup_3 cairo
  ]);

  # ── GStreamer capture plugins for WebKit getUserMedia (mic/camera) — #150 ──────
  # WebKitGTK 6.0 routes ALL getUserMedia capture through GStreamer: the orb's
  # click-to-talk mic request resolves to a GStreamer audio SOURCE element. With NO
  # GStreamer plugins DISCOVERABLE, WebKit has no capture element and getUserMedia
  # fails "microphone access denied" — even though the permission-request handler
  # ALLOWed it (hart-layer-shell-host's _on_permission_request) and PipeWire is up
  # (desktop.nix services.pipewire). The host launches from a minimal Nix-store env
  # that sets GI_TYPELIB_PATH/LD_PRELOAD/WEBKIT_DISABLE_*/GDK_GL/GSK_RENDERER but
  # NEVER GST_PLUGIN_SYSTEM_PATH_1_0, so the plugins are invisible. Point at them
  # explicitly (the search-path reference also pins them into the host closure).
  #   - gst-plugins-good: pulsesrc + the pulse device provider — the proven capture
  #     path against PipeWire's pulse compat (desktop.nix pipewire.pulse.enable).
  #   - gst-plugins-base: audioconvert/audioresample the capture pipeline needs.
  #   - gst-plugins-bad: opus + webrtc DSP for the live/peer voice the WebRTC path
  #     negotiates (enable-webrtc is set on the WebView).
  #   - pipewire: pipewiresrc — the NATIVE PipeWire capture element (best path on
  #     this PipeWire desktop; harmless if a nixpkgs rev ships it in another output).
  # Privacy-first (memory): the mic is a LOCAL capability wired ON for the trusted
  # first-party shell, but real capture stays user-initiated via the click-to-talk
  # orb + defence-in-depth gated on the AI-sensing kill-switch (_sense_cut).
  gstCapturePlugins = (with pkgs.gst_all_1; [
    gstreamer gst-plugins-base gst-plugins-good gst-plugins-bad
  ]) ++ [ pkgs.pipewire ];
  gstPluginPath = lib.makeSearchPath "lib/gstreamer-1.0" gstCapturePlugins;

  # ── Portal-less private D-Bus session — the DEGRADE FALLBACK (2026-06-29) ────────
  #
  # HISTORY: this bus was the PRIMARY first-paint fix (2026-06-28). It made the portal
  # name NON-ACTIVATABLE so GTK4's startup GtkSettings.Read fast-failed instead of the
  # ~25s freeze. But "portal absent" also breaks the things that NEED the portal:
  # WebKit getUserMedia (mic/camera) needs xdg-desktop-portal + PipeWire, so on the
  # real-HW boot the mic request wedged the GTK main loop ("Microphone access denied
  # and then HANGS") and a stuck portal call on the main loop stalled seat servicing
  # so even VT-switch looked dead. So "portal absent" is the wrong cut.
  #
  # PRIMARY (now, 2026-06-29, see the wrapper preamble + the NameHasOwner wait below):
  # PROVIDE a portal that is AVAILABLE + RESPONSIVE instead of absent — start
  # xdg-desktop-portal + the gtk backend, push the Wayland/desktop env into the D-Bus
  # activation environment, then WAIT (bounded ~8s) until org.freedesktop.portal.
  # Desktop OWNS its name before exec'ing python. With the name owned, GtkSettings.Read
  # is a MILLISECOND method call (not a 25s activation), so first-paint stays FAST
  # while mic/camera/screenshare/file-picker all work.
  #
  # ROOT CAUSE the wait fixes (kept for the record): GTK4's GtkSettings issues a
  # SYNCHRONOUS org.freedesktop.portal.Settings.Read('org.freedesktop.appearance',
  # 'color-scheme') on the GTK MAIN LOOP during window setup. If NOTHING owns
  # org.freedesktop.portal.Desktop the call tries to ACTIVATE the portal and BLOCKS
  # for the full D-Bus activation TIMEOUT (~25s) -> the main loop FREEZES -> the
  # WebView never reaches LoadEvent.FINISHED -> /run/hart/session/shell-ready is never
  # touched -> the 45s paint-watchdog declares the tier HUNG and drops to cage. The
  # cure is to make the name OWNED before GTK asks, not to make it un-askable.
  #
  # THIS PRIVATE BUS is now the DEGRADE-NOT-DIE FALLBACK only: if the bounded portal
  # wait times out (no session bus / portal failed to come up), the wrapper launches
  # python under this portal-LESS bus so first-paint stays fast and the 25s freeze can
  # NEVER recur (mic is unavailable on that fallback, but a painting mic-less desktop
  # beats a black hung one). It is NOT deleted — it is the proven floor the primary
  # path degrades to. The host talks to the served shell over HTTP :6800, not D-Bus,
  # so the bus choice never affects the UI itself.
  noPortalBusConfig = pkgs.writeText "hart-glass-shell-noportal-bus.conf" ''
    <!DOCTYPE busconfig PUBLIC "-//freedesktop//DTD D-Bus Bus Configuration 1.0//EN" "http://www.freedesktop.org/standards/dbus/1.0/busconfig.dtd">
    <busconfig>
      <type>session</type>
      <listen>unix:tmpdir=/tmp</listen>
      <!-- NO servicedir / standard_session_servicedirs: NOTHING is activatable on
           this bus, so GTK4's startup org.freedesktop.portal.Settings.Read fails FAST
           with ServiceUnknown instead of blocking on the ~25s portal activation
           timeout. This mirrors the cage GTK3 floor (which never reads the portal). -->
      <policy context="default">
        <allow send_destination="*"/>
        <allow own="*"/>
        <allow receive_sender="*"/>
      </policy>
      <limit name="max_match_rules_per_connection">4096</limit>
    </busconfig>
  '';

  # ── The GTK4 layer-shell glass-shell host (Phase-4 L2 host window) ──
  # Mirrors the cage wrapper's URL probe + software-GL hardening (one contract)
  # but hosts the SAME served shell as a GTK4 wlr-layer-shell BACKGROUND surface
  # instead of a GTK3 fullscreen window. The python below is the GTK4/WebKit-6.0
  # equivalent of hart-liquid-ui.nix's GlassShell — the budgeted port, not a copy
  # of the shell.
  layerShellHost = pkgs.writeShellScriptBin "hart-glass-shell-gtk4" ''
    set -euo pipefail
    URL="http://localhost:${liquidPort}"
    # ── Shell render RUNG (the auto-fallback ladder, 2026-07-19) ───────────────────
    # HART_SHELL_RENDER is set by THIS tier's session launcher:
    #   vulkan       (Tier-1 hart-comp)  GSK vulkan + WebKit accel  -- best, riskiest
    #   webkit-cairo (Tier-2 sway)       GSK cairo  + WebKit accel  -- animations, safe
    #   software     (fallback/default)  GSK cairo  + WebKit OFF    -- the calm floor
    # Default from the build flag hart.liquidUI.preferHardwareGL so it still works when
    # no tier set the env. This decides GSK_RENDERER, WebKit compositing, and the
    # WEBKIT_DISABLE_* gates below (one source, no scattered flags). Publish it to
    # /run/hart/shell-render so the backend body-class (liquid_ui_service.
    # read_shell_render_mode) tracks the ACTUAL painted rung: when the paint-watchdog
    # drops a hung vulkan Tier-1 to webkit-cairo Tier-2, the relaunched shell load
    # re-renders as gpu-hardware (animations + glass), not stuck flat.
    HART_SHELL_RENDER="''${HART_SHELL_RENDER:-${if ui.preferHardwareGL then "vulkan" else "software"}}"
    case "$HART_SHELL_RENDER" in
      vulkan|webkit-cairo) : ;;
      *) HART_SHELL_RENDER=software ;;
    esac
    export HART_SHELL_RENDER
    mkdir -p /run/hart 2>/dev/null || true
    printf '%s' "$HART_SHELL_RENDER" > /run/hart/shell-render 2>/dev/null || true
    echo "[hart-glass-shell-gtk4] render rung = $HART_SHELL_RENDER" >&2
    # ── Bring up xdg-desktop-portal EARLY, in the BACKGROUND (the portal-AVAILABLE
    #    first-paint fix, 2026-06-29) ───────────────────────────────────────────────
    # PRIMARY path: make org.freedesktop.portal.Desktop ACTUALLY available + responsive
    # before python launches, instead of making it non-activatable. Both ends of the
    # real-HW symptom trace to a portal that was deliberately unreachable: (1) GTK4
    # GtkSettings issues a synchronous appearance Settings.Read on the main loop at
    # startup -- on an UNOWNED name that BLOCKS ~25s and never first-paints; and (2)
    # WebKit getUserMedia (mic/camera) needs the SAME portal + PipeWire, so a non-
    # activatable portal wedged the capture path and the main loop with it ("Microphone
    # access denied and then HANGS"; a stuck portal call on the main loop also stalls
    # seat servicing so VT-switch looks dead). The fix is a portal that RESPONDS: start
    # it here, then WAIT (bounded, below) until it OWNS its name so Settings.Read is a
    # millisecond call (NOT a 25s activation) and mic/camera/file-picker work. Kicked
    # off at the TOP so it warms up IN PARALLEL with the :6800 health wait -- near-zero
    # added latency in the common case.
    export XDG_CURRENT_DESKTOP="''${XDG_CURRENT_DESKTOP:-sway}"
    export XDG_SESSION_DESKTOP="''${XDG_SESSION_DESKTOP:-sway}"
    export XDG_SESSION_TYPE="''${XDG_SESSION_TYPE:-wayland}"
    # Push the Wayland/desktop identity into the D-Bus + systemd-user activation
    # environment so a D-Bus-ACTIVATED portal backend inherits WAYLAND_DISPLAY and can
    # talk to the compositor (Camera/ScreenCast). Best-effort: harmless where there is
    # no session bus / systemd-user (we then fall back to the private bus below).
    ${pkgs.dbus}/bin/dbus-update-activation-environment --systemd \
      WAYLAND_DISPLAY XDG_CURRENT_DESKTOP XDG_SESSION_DESKTOP XDG_SESSION_TYPE XDG_RUNTIME_DIR \
      >/dev/null 2>&1 || true
    # Start the gtk backend + the portal frontend best-effort in the BACKGROUND. The
    # frontend owns org.freedesktop.portal.Desktop; the gtk backend serves Settings/
    # Access/FileChooser/Camera-frontend. XDG_DATA_DIRS is widened so the frontend finds
    # the backend's .portal file in this bare-compositor (non-graphical-session) launch.
    # Idempotent: if a portal is already up, these just fail to own the name and exit.
    # This is the SINGLE place the portal is started, for BOTH Tier-1 hart-comp (no
    # portal otherwise) and Tier-2 sway -- one starter, no parallel path.
    _HART_PORTAL_DATA_DIRS="${pkgs.xdg-desktop-portal-gtk}/share:${pkgs.xdg-desktop-portal}/share"
    XDG_DATA_DIRS="$_HART_PORTAL_DATA_DIRS''${XDG_DATA_DIRS:+:$XDG_DATA_DIRS}" \
      ${pkgs.xdg-desktop-portal-gtk}/libexec/xdg-desktop-portal-gtk >/dev/null 2>&1 &
    XDG_DATA_DIRS="$_HART_PORTAL_DATA_DIRS''${XDG_DATA_DIRS:+:$XDG_DATA_DIRS}" \
      ${pkgs.xdg-desktop-portal}/libexec/xdg-desktop-portal >/dev/null 2>&1 &
    for i in $(seq 1 30); do
      if ${pkgs.curl}/bin/curl -sf "$URL/health" >/dev/null 2>&1; then break; fi
      sleep 1
    done
    if ! ${pkgs.curl}/bin/curl -sf "$URL/health" >/dev/null 2>&1; then
      # LiquidUI down — fall back to the Nunba SPA so the surface is never blank
      # (the SAME dead-husk-avoidance the cage floor uses).
      if ${pkgs.curl}/bin/curl -sf "http://localhost:${nunbaPort}/" >/dev/null 2>&1; then
        URL="http://localhost:${nunbaPort}"
      fi
    fi
    # GI typelibs for the GTK4 / WebKit-6.0 / gtk4-layer-shell python below.
    export GI_TYPELIB_PATH="${giTypelibPath}"
    # ── GStreamer capture plugins so WebKit getUserMedia (mic) has a source (#150) ──
    # WebKitGTK 6.0 captures via GStreamer; without this path it finds NO audio
    # source element and getUserMedia fails "microphone access denied" (the real-HW
    # bug) despite the permission handler allowing it. Point at the capture/codec
    # plugins (pulsesrc/pipewiresrc + opus/webrtc) so the click-to-talk orb can
    # actually capture the mic. Inherited by the WebKitWebProcess capture child.
    export GST_PLUGIN_SYSTEM_PATH_1_0="${gstPluginPath}"
    # ── gtk4-layer-shell LOAD ORDER (the real-HW "not a layer surface" fix) ──
    # gtk4-layer-shell works by INTERPOSING libwayland-client's wl_proxy_* symbols, so
    # it MUST be loaded BEFORE libwayland-client. Pulled in lazily via the GI typelib
    # (`from gi.repository import Gtk4LayerShell`) it loads AFTER GTK has already linked
    # libwayland -> the interposition silently fails and LayerShell.init_for_window()
    # leaves the window a plain xdg-toplevel ("GtkWindow is not a layer surface"), so the
    # shell never anchors as the BACKGROUND desktop, never first-paints, and the
    # supervisor declares the tier HUNG and drops to cage (the real-HW 2026-06-24 boot:
    # "Failed to initialize layer surface. GTK4 Layer Shell may have been linked after
    # libwayland"). LD_PRELOAD the runtime .so so it loads first -- gtk4-layer-shell's own
    # documented fix; Nix has no link-order knob for a GI-dlopened library.
    for _glsl in ${lib.getLib pkgs.gtk4-layer-shell}/lib/libgtk4-layer-shell.so*; do
      if [ -e "$_glsl" ]; then export LD_PRELOAD="$_glsl"; break; fi
    done
    # WebKitGTK robustness on fresh-ISO boots (VM / software GL / no GPU): the
    # DMABUF renderer + GL compositing crash on a GL-less display — exactly the
    # first-boot / live-USB / llvmpipe case. Disable both so a GTK4 host that
    # cannot paint never takes the session down. SAME contract as the cage floor;
    # this is the GTK4 path's OWN broken-GPU proof, not an inherited assumption.
    # WebKit accelerated compositing is what paints backdrop-filter glass + promotes
    # the shell's CSS animations to the GPU (the whole point of the GPU rungs). Keep
    # it ON for vulkan + webkit-cairo; disable it ONLY on the software floor rung
    # (where the WebView paints on cairo and a live blur would peg a core).
    if [ "$HART_SHELL_RENDER" = "software" ]; then
      export WEBKIT_DISABLE_DMABUF_RENDERER=1
      export WEBKIT_DISABLE_COMPOSITING_MODE=1
    fi
    # ── GTK4/GSK SOFTWARE RENDERER (the real-HW paint-hang fix) ──────────────────
    # THE difference between this GTK4 host and the GTK3 cage floor: GTK4 draws via
    # GSK, whose DEFAULT renderer is GL (gl/ngl) — it spins up its OWN GL context on
    # the layer-shell surface, SEPARATE from WebKit's (the WEBKIT_DISABLE_* above
    # only governs WebKitGTK's compositor, NOT GTK4's own GSK renderer). On llvmpipe
    # that GL context resolves to software GL and PAINTS (why the CI nixosTest +
    # this host both work there); on a REAL GPU driver GSK's GL/EGL/GBM context
    # creation HANGS on the layer-shell surface, so the compositor cursor shows on a
    # black screen and the first frame never presents (the "pointer-only" boot).
    # The GTK3 cage floor is IMMUNE because GTK3 has no GSK — it paints via cairo
    # directly. THE FIX (Part 2 candidate for the GSK-GL layer-shell hang): GL is NEVER
    # used here — export GDK_GL=disable ALWAYS — so the GL/EGL/GBM context-creation hang
    # above CANNOT recur. GTK4's VULKAN GSK renderer is a SEPARATE path (no GL context)
    # on the SAME GPU backend the LLM already proved, so when the boot probe
    # (/run/hart/gpu-render) says the GPU is good we ask GSK for `vulkan`; with GL
    # disabled, if Vulkan can't init GSK falls back to CAIRO (software), never to the
    # hanging GL. Else (software/unproven) -> cairo, the floor. SAFE-BY-FLOOR: if
    # GSK-vulkan ITSELF hangs on the layer-shell surface the tier never first-paints and
    # the supervisor drops to the cage GTK3 floor (no GSK at all). CANDIDATE — real-HW
    # unverified; the next boot's journal proves vulkan vs the hang.
    export GDK_GL=disable
    # ── GSK renderer PER RUNG (the auto-fallback ladder) ──────────────────────────
    # vulkan rung -> GSK vulkan (a SEPARATE path from GL; GDK_GL stays disabled so the
    #   GL/EGL/GBM context-creation hang can never recur). This is the 2026-07-08
    #   real-HW-DISPROVEN path -- it HUNG first-paint on the layer-shell surface and
    #   the 45s watchdog dropped the tier. It is attempted ONLY as Tier-1 hart-comp on
    #   purpose: if it hangs again, the watchdog drops to Tier-2 sway (webkit-cairo,
    #   below), which paints. So the known hang is now a self-healing rung, not a dead
    #   end -- the whole reason the ladder exists.
    # webkit-cairo / software rung -> GSK cairo, the proven software floor GTK4 host
    #   paint path (the d8c1567 stability guard: the WebView shell's GTK4 window paints
    #   via cairo; WebKit's OWN compositing -- governed by the WEBKIT_DISABLE_* gate
    #   above, ON for webkit-cairo -- is what accelerates the shell CONTENT). hart-comp's
    #   GLES compositing + prime-run app GPU are separate paths, untouched.
    if [ "$HART_SHELL_RENDER" = "vulkan" ]; then
      export GSK_RENDERER=vulkan
      echo "[hart-glass-shell-gtk4] GSK = VULKAN (rung=vulkan; GL disabled; watchdog drops to webkit-cairo if it hangs)" >&2
    else
      export GSK_RENDERER=cairo
      echo "[hart-glass-shell-gtk4] GSK = CAIRO (rung=$HART_SHELL_RENDER; proven GTK4 host floor; WebKit compositing carries the shell content)" >&2
    fi
    export HART_SHELL_URL="$URL"
    # Shell-paint readiness marker (the session-supervisor's HUNG-tier guard): the
    # GTK4 host touches this once the WebView finishes its first load, telling the
    # paint-watchdog this Tier-2 surface is HEALTHY so it is NOT dropped as a hang.
    # THIS is the host the "pointer-only" regression hung in — without the marker
    # the watchdog would time out and escalate to cage; with it a working tier
    # stays up. The supervisor passes HART_SHELL_READY_FLAG; default to the pinned
    # /run/hart contract path so a bare (supervisor-less) launch is harmless.
    export HART_SHELL_READY_FLAG="''${HART_SHELL_READY_FLAG:-/run/hart/session/shell-ready}"
    # ── Wait (bounded) for the portal to OWN its name, then launch on the REAL bus ──
    # Poll org.freedesktop.portal.Desktop NameHasOwner for up to ~8s. THIS is what
    # makes the portal AVAILABLE + RESPONSIVE: once OWNED, the GTK4 GtkSettings
    # Settings.Read at startup is a millisecond round-trip (NOT a 25s activation), so
    # first-paint stays FAST, AND mic/camera/screenshare/file-picker work (they need
    # the same portal). The wait OVERLAPS the :6800 health loop above (the portal was
    # kicked off at the top of this wrapper), so in the common case it is already owned
    # and this adds ~0 latency; worst case <=8s, well inside the 45s paint watchdog.
    #
    # DEGRADE-NOT-DIE: if the name is NOT owned in time (no session bus / the portal
    # failed to come up), fall through to the portal-LESS PRIVATE bus (noPortalBusConfig
    # above) for THIS launch -- the proven fast-but-mic-less paint path -- so first-paint
    # stays fast and the 25s GtkSettings freeze can NEVER recur. --dbus-daemon is pinned
    # to the closure binary so the fallback never depends on PATH. BOTH branches exec the
    # SAME single `python -c` host below; only the bus prefix differs, so the first-paint
    # marker path (LoadEvent.FINISHED -> shell-ready) is byte-identical either way.
    LAUNCH_PREFIX=""
    _PORTAL_OWNED=0
    for _ in $(seq 1 8); do
      # Capture the NameHasOwner reply to a VARIABLE (no pipe): piping into `grep -q`
      # under `set -o pipefail` can SIGPIPE dbus-send when grep closes the pipe early,
      # turning a TRUE match into a non-zero pipeline -- a false negative. `case` glob
      # on the captured text avoids that and drops the extra grep dependency. `|| true`
      # so a non-zero dbus-send (no session bus) never trips `set -e`.
      _OWN="$(${pkgs.dbus}/bin/dbus-send --session --print-reply --dest=org.freedesktop.DBus \
                /org/freedesktop/DBus org.freedesktop.DBus.NameHasOwner \
                string:org.freedesktop.portal.Desktop 2>/dev/null || true)"
      case "$_OWN" in
        *"boolean true"*) _PORTAL_OWNED=1; break ;;
        "") break ;;   # empty reply => no reachable session bus; stop waiting, fall back
      esac
      sleep 1
    done
    if [ "$_PORTAL_OWNED" = "1" ]; then
      echo "[hart-glass-shell-gtk4] xdg-desktop-portal OWNED -> real session bus (Settings.Read is ms; mic/portal available)" >&2
    else
      # DEGRADE-NOT-DIE: portal never owned (timed out) OR no session bus at all ->
      # launch under the portal-less PRIVATE bus so first-paint stays fast and the 25s
      # GtkSettings freeze can never recur (mic unavailable on this fallback).
      echo "[hart-glass-shell-gtk4] portal not owned -> FALLBACK to portal-less private bus (fast paint, mic unavailable)" >&2
      LAUNCH_PREFIX="${pkgs.dbus}/bin/dbus-run-session --dbus-daemon=${pkgs.dbus}/bin/dbus-daemon --config-file=${noPortalBusConfig} --"
    fi
    # Single host launch. $LAUNCH_PREFIX is empty (real session bus, portal available)
    # or the private-bus dbus-run-session prefix (degrade fallback). Word-splitting on
    # the prefix is intentional (Nix store paths contain no spaces/globs). The embedded
    # host program below is IDENTICAL on both branches -- one host, one first-paint marker.
    exec $LAUNCH_PREFIX ${cfg.package.python}/bin/python -c "
import gi, os
gi.require_version('Gtk', '4.0')
# WebKitGTK 6.0 is the GTK4 binding; the namespace is 'WebKit' (NOT 'WebKit2',
# which is the GTK3 4.1 binding the cage floor uses). This is the toolkit port.
gi.require_version('WebKit', '6.0')
gi.require_version('Gtk4LayerShell', '1.0')
from gi.repository import Gtk, WebKit, Gtk4LayerShell as LayerShell

SHELL_URL = os.environ.get('HART_SHELL_URL', 'http://localhost:${liquidPort}')
READY_FLAG = os.environ.get('HART_SHELL_READY_FLAG', '/run/hart/session/shell-ready')


def _signal_painted():
    # Touch the first-paint marker the session-supervisor watches. Best-effort:
    # a missing dir / permission error must NEVER crash the shell (the supervisor
    # degrades safely: a missing marker escalates DOWN to the cage floor).
    try:
        os.makedirs(os.path.dirname(READY_FLAG), exist_ok=True)
        with open(READY_FLAG, 'w'):
            pass
    except OSError:
        pass


def _sense_cut(sensor):
    # Best-effort cross-process read of the human's AI-sensing kill-switch over the
    # SAME Unix-socket authority core/ai_sensing.py:start_authority_server exposes
    # (send the sensor name -> read b'1'=allowed / b'0'=cut). Returns True ONLY on a
    # definitive b'0' from a REACHABLE authority; an unreachable authority or ANY
    # error returns False (fail-OPEN), so a missing kill-switch never wrongly denies
    # the first-party shell's own capture (the 'mic denied' bug we are fixing). The
    # substantive enforcement is server-side at ingestion (/api/voice refuses a cut
    # mic); this is defence-in-depth, not the primary gate. Socket path mirrors
    # ai_sensing._authority_path: HART_AI_SENSING_SOCK env, else $XDG_RUNTIME_DIR.
    import socket as _socket
    sock_path = (os.environ.get('HART_AI_SENSING_SOCK')
                 or os.path.join(os.environ.get('XDG_RUNTIME_DIR', '/run'),
                                 'hart-ai-sensing.sock'))
    try:
        c = _socket.socket(_socket.AF_UNIX, _socket.SOCK_STREAM)
        c.settimeout(0.5)
        c.connect(sock_path)
        c.sendall(sensor.encode('ascii'))
        verdict = c.recv(8).strip()
        c.close()
        return verdict == b'0'
    except Exception:
        return False


class GlassShellLayer:
    # The glass shell as a GTK4 wlr-layer-shell BACKGROUND surface.
    #
    # Z-ORDER MODEL (1) - picked in code: ONE layer-shell surface (one WebView),
    # overlays/orb co-planar inside it, anchored to all 4 edges as the BACKGROUND
    # layer with exclusive zone 0. Native toplevels (Phase 5) sit ABOVE this; the
    # orb does NOT float over a focused native window (the honest Model-1 limit).
    # The served shell HTML/JS is UNCHANGED - that is the whole reason for Model 1.

    def __init__(self, app):
        self._win = Gtk.ApplicationWindow(application=app)

        # ── Make this a wlr-layer-shell surface BEFORE the window is realized ──
        # init_for_window must run before present(); it converts the toplevel
        # into a zwlr_layer_surface_v1 the compositor stacks by layer.
        LayerShell.init_for_window(self._win)
        # BACKGROUND layer == the desktop wallpaper plane: below every native
        # toplevel. This is what makes the shell the DESKTOP rather than an app.
        LayerShell.set_layer(self._win, LayerShell.Layer.BACKGROUND)
        # Anchor to all four edges => the surface spans the whole output (the
        # desktop fills the screen) without us hardcoding a pixel size.
        for edge in (LayerShell.Edge.TOP, LayerShell.Edge.BOTTOM,
                     LayerShell.Edge.LEFT, LayerShell.Edge.RIGHT):
            LayerShell.set_anchor(self._win, edge, True)
        # Exclusive zone 0: the desktop does NOT reserve space away from other
        # surfaces (it is the backdrop, not a panel/bar). Per the Phase-4 spec.
        LayerShell.set_exclusive_zone(self._win, 0)
        # The background desktop should not steal keyboard focus from native
        # windows on top of it; ON_DEMAND lets the shell take focus only when the
        # user actually interacts with it (clicks the orb / a panel).
        # NOTE: ON_DEMAND on a BACKGROUND layer means the surface is NOT given
        # keyboard focus automatically by the compositor — typing only works once
        # the surface holds focus. So ON_DEMAND MUST be paired with an explicit
        # self._webview.grab_focus() after present() (below); without that grab,
        # the layer-shell surface never accepts keystrokes and the caret/typing
        # are dead even though the WebView renders.
        LayerShell.set_keyboard_mode(
            self._win, LayerShell.KeyboardMode.ON_DEMAND)

        # ── The WebView: same served shell, GTK4/WebKit-6.0 host ──
        webview = WebKit.WebView()
        # Signal first paint to the session-supervisor when the page finishes
        # loading — the GTK4/WebKit-6.0 load-changed signal mirrors the GTK3 cage
        # floor's. This marks Tier-2 HEALTHY so the paint-watchdog does NOT drop it.
        webview.connect('load-changed', self._on_load_changed)
        # Sets the _last_load_failed guard (and journals the reason) so the FINISHED
        # that WebKit STILL emits after a failed load (it substitutes a stock error
        # page) does NOT touch the shell-ready marker in _on_load_changed. A blank /
        # error surface (e.g. :6800 connection-refused) must read as HUNG so the
        # supervisor drops to a working tier, NOT count as HEALTHY (the false-healthy
        # WebKit FMEA). A GOOD load leaves the guard False, so it still fires the
        # marker on FINISHED, byte-identical to before.
        webview.connect('load-failed', self._on_load_failed)
        # Allow first-party getUserMedia (mic/camera) so click-to-talk voice + the
        # vision sense work. WITHOUT a handler WebKit DEFAULT-DENIES (the 'Microphone
        # access denied' half of the real-HW regression) and, on the portal-less bus,
        # an un-handled request also wedged the GTK main loop (the HANG). The handler
        # is gated best-effort on the human's AI-sensing kill-switch. Connected before
        # load_uri so it is wired before any page script can call getUserMedia.
        webview.connect('permission-request', self._on_permission_request)
        # False-healthy guard, cleared on every fresh load_uri (this initial
        # navigation), set in _on_load_failed. Initialised before load_uri runs so it
        # is always defined before the first load-changed / load-failed fires.
        self._last_load_failed = False
        webview.load_uri(SHELL_URL)
        s = webview.get_settings()
        s.set_enable_javascript(True)
        s.set_enable_developer_extras(True)
        # getUserMedia is OFF by default in WebKitGTK: navigator.mediaDevices is
        # undefined unless enable-media-stream is set, so the permission-request above
        # would never even FIRE without this (the mic path is dead before it starts).
        # Enable media-stream (+ webrtc for live/peer voice); guarded per-setter so an
        # older WebKit lacking one degrades instead of crashing the shell.
        for _setter in ('set_enable_media_stream', 'set_enable_webrtc'):
            try:
                getattr(s, _setter)(True)
            except Exception:
                pass
        # HW-accel policy PER RUNG (the auto-fallback ladder): ON_DEMAND for the GPU
        # rungs (vulkan + webkit-cairo) so WebKit composites the shell content on the
        # GPU (backdrop-filter glass + GPU-promoted CSS animations); NEVER on the
        # software floor rung, where forcing GPU accel on a llvmpipe / GL-less display
        # crashes WebKitGTK and takes the session down (the cage GTK3 floor lesson,
        # re-applied on GTK4). Read the rung from the env the wrapper exported (the
        # WEBKIT_DISABLE_* env above is the belt; this is the suspenders).
        _rung = os.environ.get('HART_SHELL_RENDER', 'software')
        s.set_hardware_acceleration_policy(
            WebKit.HardwareAccelerationPolicy.ON_DEMAND
            if _rung in ('vulkan', 'webkit-cairo')
            else WebKit.HardwareAccelerationPolicy.NEVER)
        self._webview = webview

        # GTK4: set_child (the GTK3 container .add() is gone); key events via an
        # EventControllerKey emitting 'key-pressed' (the GTK3 window key-press
        # SIGNAL is gone in GTK4, so we do NOT connect it on the window).
        self._win.set_child(webview)
        keyctl = Gtk.EventControllerKey.new()
        keyctl.connect('key-pressed', self._on_key)
        self._win.add_controller(keyctl)

        # Reliable keyboard focus (the LIVE-OS #2 'typing is dead' fix).
        # A single post-present() grab_focus() is NOT reliable: on a BACKGROUND
        # layer-shell surface with KeyboardMode.ON_DEMAND the compositor does not
        # auto-route keys to us, and grab_focus() is a no-op on a widget that is
        # not yet realized/mapped, so the very first present() grab can fire
        # before the surface exists and the caret/typing stay dead. Wire the grab
        # to the realize AND map signals (fired when the surface actually comes
        # up) so focus is taken the moment it CAN stick. Connect BEFORE present()
        # so the map handler runs during it.
        self._win.connect('realize', self._on_realize)
        self._win.connect('map', self._on_map)
        # Re-grab on every pointer press so clicking the orb / command bar always
        # makes typing live again, even after focus drifted to a native toplevel
        # stacked above this BACKGROUND desktop. CAPTURE phase + no claim means
        # the press still reaches the web content (we steal focus, not the event).
        click = Gtk.GestureClick.new()
        click.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
        click.connect('pressed', self._on_pointer_press)
        webview.add_controller(click)

        # GTK4: present() (no .show_all()); layer-shell sizes it to the anchors.
        self._win.present()
        # Explicitly grab keyboard focus into the WebView after present(). With
        # KeyboardMode.ON_DEMAND on a background layer-shell surface the
        # compositor does NOT auto-focus us, so without this grab left-clicks
        # land on a focus-less surface and typing/caret never work.
        self._webview.grab_focus()

    def _on_load_changed(self, _webview, event):
        # Touch the first-paint marker once the WebView finishes its first load --
        # but ONLY when that load actually SUCCEEDED. WebKit emits LoadEvent.FINISHED
        # even after a failed load (it substitutes a stock error page), so gating on
        # _last_load_failed is what stops a blank :6800-refused surface from touching
        # shell-ready and counting as HEALTHY (the false-healthy WebKit FMEA). A GOOD
        # load leaves _last_load_failed False, so this path stays byte-identical.
        #
        # This is the GTK4/WebKit-6.0 mirror of the GTK3 cage floor's
        # _on_load_changed. WITHOUT it the connected load-changed handler does not
        # exist, _signal_painted() is NEVER called, /run/hart/session/shell-ready
        # never fires, and the session-supervisor's paint-watchdog times this Tier-2
        # surface out as HUNG and drops to the cage floor - the EXACT shell-ready-
        # never-fires half of the pointer-only regression. LoadEvent.FINISHED is the
        # WebKitGTK-6.0 enum (same name as the GTK3 WebKit2 binding). Re-grab focus on
        # every FINISHED (good load or error page) so typing works once the page JS
        # has run (mirrors the cage floor + the m2 WSL reference host); only the
        # shell-ready marker is gated on the load actually succeeding.
        if event == WebKit.LoadEvent.FINISHED:
            if not self._last_load_failed:
                _signal_painted()
            self._webview.grab_focus()

    def _on_load_failed(self, _webview, _event, failing_uri, error):
        # Mark this load as FAILED so the LoadEvent.FINISHED that WebKit STILL emits
        # afterwards (it substitutes a stock error page) does NOT touch the shell-ready
        # marker in _on_load_changed. A blank / error surface (e.g. :6800 connection-
        # refused) must read as HUNG so the supervisor drops to a working tier, NOT
        # count as HEALTHY (the false-healthy WebKit FMEA). The flag is cleared on the
        # next fresh load_uri, so a later successful load still signals first-paint.
        # Set FIRST, before the (best-effort) journal print, so the guard holds even
        # if logging raises.
        self._last_load_failed = True
        # Journal-only diagnostic so a real-HW boot shows the load-failure reason
        # (connection refused while :6800 is still coming up, TLS, etc.) instead of a
        # silent blank surface. Returning False lets WebKit show its default error
        # page (never raises out of the handler).
        import sys
        try:
            detail = getattr(error, 'message', None) or str(error)
            print('[hart-glass-shell-gtk4] load-failed', failing_uri, detail,
                  file=sys.stderr)
        except Exception:
            pass
        return False

    def _on_permission_request(self, _webview, request):
        # The glass shell is the trusted first-party desktop. WebKitGTK DEFAULT-DENIES
        # getUserMedia (mic/camera) unless a permission-request handler ALLOWs it --
        # that default-deny is the 'Microphone access denied' half of the real-HW
        # regression, and on the portal-less bus the un-handled request also wedged the
        # GTK main loop (the HANG). Allow mic/camera so click-to-talk voice + the vision
        # sense work, gated best-effort on the human's AI-sensing kill-switch: DENY only
        # when the cross-process authority is REACHABLE and reports the sense CUT; else
        # ALLOW (fail-OPEN). Returning True means WE handled it, so WebKit does NOT fall
        # back to the portal Access dialog (one fewer main-loop portal round-trip).
        # NEVER raises -- on ANY error we allow rather than crash/wedge the shell.
        try:
            if isinstance(request, WebKit.UserMediaPermissionRequest):
                cut = False
                try:
                    if request.is_for_audio_device():
                        cut = cut or _sense_cut('mic')
                except Exception:
                    pass
                try:
                    if request.is_for_video_device():
                        cut = cut or _sense_cut('camera')
                except Exception:
                    pass
                if cut:
                    request.deny()
                else:
                    request.allow()
                return True
        except Exception:
            # Degrade-not-die: on ANY error allow the first-party shell's request
            # rather than crash or wedge the main loop (the exact hang we are fixing).
            try:
                request.allow()
            except Exception:
                pass
            return True
        # Non-media permission types: let WebKit apply its default (deny).
        return False

    def _on_realize(self, _widget):
        # The surface now has a backing GdkSurface, the earliest point a
        # grab_focus() can stick. Pairs with KeyboardMode.ON_DEMAND so the shell
        # is ready to type into as soon as it is realized.
        self._webview.grab_focus()

    def _on_map(self, _widget):
        # The surface is now mapped (visible). grab_focus() on an unmapped widget
        # is a no-op, so this map-time re-grab is what actually makes the first
        # keystrokes land: THE reliable focus point for the layer surface.
        self._webview.grab_focus()

    def _on_pointer_press(self, _gesture, _n_press, _x, _y):
        # Any pointer press re-asserts keyboard focus into the WebView so a click
        # on the orb / command bar always makes typing live, even after focus
        # drifted to a native window above this BACKGROUND desktop layer.
        self._webview.grab_focus()

    def _on_key(self, _ctrl, keyval, _keycode, _state):
        from gi.repository import Gdk
        if keyval == Gdk.KEY_F12:
            self._webview.get_inspector().show()
            return True
        return False


def _on_activate(app):
    app.__hart_shell = GlassShellLayer(app)


# GTK4 uses GtkApplication.run as the loop — the GTK3 bare main loop is gone.
app = Gtk.Application(application_id='ai.hart.GlassShellLayer')
app.connect('activate', _on_activate)
app.run(None)
"
  '';

  # ── GTK4-layer-shell session launcher ──
  # Forces software rendering (paint on any GPU — SAME contract as the cage floor
  # + sway + hart-comp) and runs the GTK4 layer-shell host as the compositor's
  # single client. This is the L2 host-window session a higher tier (sway/hart-
  # comp) or the operator can select; it is NOT the default. Like the cage
  # launcher it wraps the renderer so wlroots/Mesa never touch a broken GPU GL
  # path. The GTK4 host needs a layer-shell-capable compositor (sway/hart-comp);
  # running it directly under cage is unsupported (cage implements no
  # zwlr_layer_shell_v1) — under cage the GTK3 floor is the renderer.
  sessionLauncher = pkgs.writeShellScriptBin "hart-glass-shell-gtk4-session" ''
    export WLR_RENDERER_ALLOW_SOFTWARE=1
    export WLR_NO_HARDWARE_CURSORS=1
    # Desktop identity for native/Flatpak apps + xdg-desktop-portal backend selection
    # under Tier-2 sway. NOTE (2026-06-28): the GTK4 host's OWN first-paint no longer
    # depends on this, or on any portal at all -- it runs on a portal-free private
    # D-Bus session (noPortalBusConfig in hart-glass-shell-gtk4), so GtkSettings can no
    # longer block on the portal activation timeout that was killing the tier. These
    # exports remain for the benefit of apps that map ABOVE the desktop on the real
    # session bus, not for the host's paint path.
    export XDG_CURRENT_DESKTOP=sway
    export XDG_SESSION_DESKTOP=sway
    # ── Shell render RUNG (the auto-fallback ladder, 2026-07-19) ──────────────────
    # Tier-2 is the SAFE decoupled rung: webkit-cairo (GSK cairo + WebKit accel). It
    # lights up the SAME shell micro-animations + live glass as vulkan but keeps
    # GTK4's own renderer on the proven cairo floor, so it never touches the
    # GSK-vulkan layer-shell path that hung 2026-07-08. This is where Tier-1
    # hart-comp's vulkan attempt lands if it cannot first-paint; if webkit-cairo also
    # cannot paint (WebKit's own GL on the layer surface), the watchdog drops to the
    # cage software floor. sway passes this env to the host it exec's.
    export HART_SHELL_RENDER="''${HART_SHELL_RENDER:-webkit-cairo}"
    ${if ui.preferHardwareGL then ''
    # Operator opted into hardware GL (hart.liquidUI.preferHardwareGL = true) — do
    # NOT force software; honour the explicit opt-in unconditionally.
    echo "[hart-gtk4-session] Tier-2 GL = HARDWARE (operator preferHardwareGL=true)" >&2
    '' else ''
    # GPU smoke-test gate: force software GL UNLESS the boot probe proved the GPU good
    # (/run/hart/gpu-render == hardware). wlroots keeps its own pixman fallback via
    # WLR_RENDERER_ALLOW_SOFTWARE, and a GPU that lies still drops to the cage floor.
    # The chosen mode is logged to the journal so a real-HW boot shows what engaged.
    HART_GPU_VERDICT="$(cat /run/hart/gpu-render 2>/dev/null || echo unknown)"
    if [ "$HART_GPU_VERDICT" != "hardware" ]; then
      export LIBGL_ALWAYS_SOFTWARE=1
      echo "[hart-gtk4-session] Tier-2 GL = SOFTWARE (gpu-render verdict: $HART_GPU_VERDICT)" >&2
    else
      echo "[hart-gtk4-session] Tier-2 GL = HARDWARE (gpu-render verdict: hardware)" >&2
    fi
    ''}
    # sway hosts the layer-shell surface; the GTK4 host is sway's startup client.
    # Software-GL forced above so it paints on any GPU. (A bare hart-comp variant
    # selects the same host binary once its software-render path is VM-proven.)
    exec ${pkgs.sway}/bin/sway -c ${swayHostConfig}
  '';

  # sway single-output config whose ONLY client is the GTK4 layer-shell host. No
  # bars / launchers — sway exists to give the layer-shell surface a compositor
  # that implements zwlr_layer_shell_v1 (cage does not). Mirrors the sway-Tier-1
  # kiosk config shape; software-GL is forced in the launcher env above.
  swayHostConfig = pkgs.writeText "hart-gtk4-layer-host.conf" ''
    # HART OS GTK4 layer-shell host — single client (the GTK4 glass shell).
    # Generated; do not edit on the live ISO. See hart-layer-shell-host.nix.
    default_border none
    default_floating_border none
    gaps inner 0
    gaps outer 0
    # Touchpad tap-to-click. libinput defaults tap-to-click OFF, so a light tap on a
    # laptop touchpad registers NOTHING (only a physical button press clicks) -- the
    # real-HW 2026-06-24 "touchpad taps not registering". sway applies this libinput
    # config to every touchpad when Tier-2 hosts the shell. (hart-comp Tier-1 enables
    # the same in udev.rs; cage Tier-3 cannot configure libinput.)
    input "type:touchpad" {
      tap enabled
      tap_button_map lrm
    }
    # Wildcard output config: enable every detected output at its preferred mode.
    # Without ANY output config sway logs "Could not find config for output <name>
    # (BUG 0x6675)" while bringing up eDP-1 on real HW. That is BENIGN -- sway still
    # enables the output with its detected mode and the layer-shell surface still gets
    # a configure (LoadEvent.FINISHED is independent of surface size) -- but it has been
    # misread as a paint failure. The wildcard gives sway a config for every output so
    # the message does not fire and the output is guaranteed enabled for the BACKGROUND
    # layer-shell surface to anchor to; the host's all-edge anchor sizes the shell to
    # whatever mode sway detects, so a missing per-output config never aborts the anchor.
    output * {
      enable
    }
    # xdg-desktop-portal is now started + WAITED-ON by the hart-glass-shell-gtk4 host
    # wrapper itself (2026-06-29) -- the SINGLE, cross-tier portal starter that serves
    # BOTH Tier-1 hart-comp and Tier-2 sway, and BLOCKS on org.freedesktop.portal.
    # Desktop name-ownership so GtkSettings.Read is a millisecond call (NOT a 25s
    # activation) AND mic/camera/file-picker work. Starting the portal HERE too would
    # be a parallel starter racing the wrapper's, so it is intentionally NOT exec'd
    # from this sway config (DRY: one portal starter, in the host wrapper).
    # Launch the GTK4 layer-shell host as sway's startup client. It anchors itself
    # as the BACKGROUND layer (exclusive zone 0) via gtk4-layer-shell so it is the
    # desktop, not a fullscreen app. Native toplevels (Phase 5) map above it.
    exec ${layerShellHost}/bin/hart-glass-shell-gtk4
  '';

  sessionDesktop = pkgs.writeText "hart-glass-gtk4.desktop" ''
    [Desktop Entry]
    Name=HART OS (GTK4 layer-shell)
    Comment=AI-native glass shell as a wlr-layer-shell desktop surface (L2 host port)
    Exec=${sessionLauncher}/bin/hart-glass-shell-gtk4-session
    Type=Application
    DesktopNames=HART-OS-gtk4
  '';
  # passthru.providedSessions REQUIRED by services.displayManager.sessionPackages
  # (session id must match the wayland-sessions/*.desktop basename) — the SAME
  # lesson hart-liquid-ui.nix's kioskSession + hart-sway-tier1.nix's swaySession +
  # hart-comp.nix's compSession encode. Without it, nix flake-check fails with
  # "did not specify any session names".
  hostSession = pkgs.runCommand "hart-glass-gtk4-wayland-session"
    { passthru.providedSessions = [ "hart-glass-gtk4" ]; } ''
      install -Dm644 ${sessionDesktop} $out/share/wayland-sessions/hart-glass-gtk4.desktop
    '';
in
{
  # ═══════════════════════════════════════════════════════════
  # Options
  # ═══════════════════════════════════════════════════════════
  options.hart.layerShellHost = {
    enable = lib.mkEnableOption ''
      HART OS Phase-4 GTK4 layer-shell glass-shell host: the budgeted GTK3 → GTK4
      WebKitGTK host-window port that re-hosts the SAME served shell as a real
      wlr-layer-shell BACKGROUND surface (exclusive zone 0, JS unchanged). Opt-in,
      default OFF; does NOT flip defaultSession (cage GTK3 stays the Tier-3 floor
      until this GTK4 path's broken-GPU paint proof passes in CI). Registers a
      greeter-selectable session + the L2 host-window the higher tiers adopt.
    '';
  };

  # ═══════════════════════════════════════════════════════════
  # Config
  # ═══════════════════════════════════════════════════════════
  config = lib.mkIf host.enable {
    # The GTK4 layer-shell host REUSES the canonical served shell (single
    # renderer of the HTML/JS, no parallel path). It is therefore only coherent
    # when hart.liquidUI is enabled with the webkit renderer (the SAME assertion
    # sway-Tier-1 + hart-comp make). Fail EVAL loudly otherwise rather than ship a
    # blank session or a second shell renderer.
    assertions = [
      {
        assertion = ui.enable && (ui.renderer == "webkit");
        message =
          "hart.layerShellHost.enable requires hart.liquidUI.enable = true with "
          + "renderer = \"webkit\" — the GTK4 layer-shell host re-hosts the SAME "
          + "served glass shell (:6800 render_desktop_shell + /shell/static); it is "
          + "a host-window port, not a second shell renderer. Enable LiquidUI or "
          + "disable layerShellHost.";
      }
    ];

    # The GTK4 host binary + its software-GL session launcher + sway (the layer-
    # shell-capable compositor that hosts it). gtk4-layer-shell + webkitgtk_6_0 +
    # gtk4 land in the closure via giTypelibPath above; sway pulls wlroots.
    environment.systemPackages = [
      layerShellHost
      sessionLauncher
      pkgs.gtk4-layer-shell   # the wlr-layer-shell binding for GTK4 (Gtk4LayerShell-1.0)
      pkgs.webkitgtk_6_0      # WebKitGTK 6.0 — the GTK4 WebView (WebKit-6.0)
      pkgs.gtk4
      pkgs.sway               # layer-shell-capable compositor host (cage is not)
    ];

    # Register the opt-in GTK4 layer-shell session. desktop.nix keeps the default
    # session on cage ("hart-shell"); this is ADDITIVE — a selectable session +
    # the L2 host-window rung. cage (GTK3) + sway + GNOME all stay selectable (the
    # full never-fail ladder). This module NEVER assigns defaultSession.
    services.displayManager.sessionPackages = [ hostSession ];

    # ── Integration with the Phase-1 tier-drop supervisor: BE Tier-2 ──
    # Repoint the supervisor's Tier-2 `swayCommand` at THIS module's
    # `hart-glass-shell-gtk4-session` (sway hosting the GTK4 + WebKitGTK-6.0 +
    # gtk4-layer-shell host) so Tier-2 is a TRUE layer-shell desktop running the
    # SAME glass host as Tier-1 hart-comp — not bare sway and not the GTK3 cage
    # fullscreen window. The cage GTK3 host stays Tier-3 underneath either way; a
    # GTK4-host crash OR hang (the shell-paint watchdog) drops to it (the Phase-4 +
    # paint-watchdog nixosTests prove the drop lands on cage and the shell paints).
    #
    # mkOverride 900 (stronger than mkDefault=1000) so this wins over BOTH the
    # supervisor's bare-sway option default AND hart-sway-tier1.nix's mkDefault
    # `hart-sway-session` — when the GTK4 layer-shell host is enabled it IS the
    # Tier-2 session. An explicit operator `hart.sessionSupervisor.swayCommand`
    # (priority < 900, e.g. mkForce) still wins. Gated on the supervisor being
    # enabled so this is a pure no-op when the ladder is off (the supervisor option
    # always exists — its module is imported unconditionally — but writing the
    # value only matters when greetd drives the boot).
    hart.sessionSupervisor.swayCommand = lib.mkIf cfg.sessionSupervisor.enable
      (lib.mkOverride 900 "${sessionLauncher}/bin/hart-glass-shell-gtk4-session");

    # Floor invariant the supervisor honors: a GTK4-host crash/hang can NEVER drop
    # below the cage GTK3 Tier-3 floor.
  };
}
