"""
Phase-4 source-guards — the GTK3 → GTK4 WebKitGTK layer-shell host-window port.

WHY THESE ARE SOURCE-GUARDS (and that is the correct kind of test here):
  Per CLAUDE.md Gate 5 / memory/feedback_no_grep_tests.md, behavioural tests are
  the default. But the Phase-4 deliverable is a Nix module that embeds a GTK4 +
  WebKitGTK-6.0 + gtk4-layer-shell host window which CANNOT be compiled or booted
  on this Windows dev box (no Wayland/wlroots/gtk4-layer-shell/WebKitGTK). The
  behaviour it encodes (a layer-shell BACKGROUND surface painting the served shell
  on llvmpipe) is VM/CI-only — it lives in the Phase-4 nixosTest
  (nixos/tests/layer-shell-host.nix), which boots an llvmpipe VM, asserts the GTK4
  toolkit typelibs + the served /shell/static 200 + that a GTK4-host crash drops
  to the GTK3 cage Tier-3 floor.

  The ONLY dev-box-verifiable invariants are STRUCTURAL, and they are exactly the
  cross-file DRY / never-fail invariants the no-grep-tests rule names as the
  acceptable source-guard class — clearly labelled as such:
    - the GTK4 host re-hosts the SAME served shell (no second renderer of the JS),
    - it picks Z-ORDER MODEL (1) IN CODE (single BACKGROUND layer, zone 0; JS
      unchanged) — the model decision is load-bearing, not prose,
    - the GTK3 cage Tier-3 floor is kept VERBATIM (untouched),
    - gtk4-layer-shell + webkitgtk_6_0 are added to the package set,
    - it is opt-in + does NOT flip defaultSession (cage stays the floor),
    - it is wired into the flake hartModules[] + the checks nixosTest,
    - it is honestly VM/CI-pending-marked (not booted on Windows).

Run (dev box, targeted — the full suite OOMs):
    python -m pytest tests/unit/test_phase4_layer_shell_host.py -v \
        --noconftest -p no:capture
"""
import os
import pytest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
NIXOS_DIR = os.path.join(REPO_ROOT, "nixos")
MODULES_DIR = os.path.join(NIXOS_DIR, "modules")
TESTS_DIR = os.path.join(NIXOS_DIR, "tests")
COMPOSITOR_DIR = os.path.join(REPO_ROOT, "compositor")

MODULE = os.path.join(MODULES_DIR, "hart-layer-shell-host.nix")
NIXTEST = os.path.join(TESTS_DIR, "layer-shell-host.nix")


def _read(path):
    with open(path, "r", encoding="utf-8") as f:
        return f.read()


# ═══════════════════════════════════════════════════════════════
# 0. The Phase-4 files exist
# ═══════════════════════════════════════════════════════════════

class TestPhase4FilesExist:
    def test_layer_shell_host_module_exists(self):
        assert os.path.isfile(MODULE)

    def test_layer_shell_host_nixostest_exists(self):
        assert os.path.isfile(NIXTEST)


# ═══════════════════════════════════════════════════════════════
# 1. The GTK4 host: GTK4 + WebKitGTK-6.0 + gtk4-layer-shell, not GTK3
# ═══════════════════════════════════════════════════════════════

class TestGtk4Host:
    @pytest.fixture(scope="class")
    def src(self):
        return _read(MODULE)

    def test_has_enable_option_and_mkif_guard(self, src):
        assert "layerShellHost" in src
        assert "mkEnableOption" in src
        assert "mkIf host.enable" in src

    def test_is_a_gtk4_host_not_gtk3(self, src):
        # The toolkit port made concrete: GTK 4.0 (not 3.0).
        assert "gi.require_version('Gtk', '4.0')" in src

    def test_uses_webkitgtk_6_0_namespace_webkit(self, src):
        # WebKitGTK 6.0 is the GTK4 binding; the GI namespace is 'WebKit'
        # (version 6.0), NOT the GTK3 'WebKit2' (4.1) the cage floor uses.
        assert "gi.require_version('WebKit', '6.0')" in src
        # And it must NOT have regressed to the GTK3 WebKit2 binding here.
        assert "gi.require_version('WebKit2'" not in src

    def test_uses_gtk4_layer_shell_binding(self, src):
        # The whole point: anchor the surface via gtk4-layer-shell.
        assert "gi.require_version('Gtk4LayerShell', '1.0')" in src
        assert "Gtk4LayerShell" in src
        assert "init_for_window" in src

    def test_gtk4_api_not_gtk3_api(self, src):
        # GTK4 host APIs (no GTK3-only calls that would crash under GTK4).
        assert "set_child" in src          # GTK4: set_child, not .add()
        assert "EventControllerKey" in src  # GTK4: key events via controller
        assert ".present()" in src          # GTK4: present(), not show_all()
        # GTK3-only call FORMS must be absent from the GTK4 host. Match the actual
        # invocation shapes, not bare substrings that also appear in explanatory
        # prose (e.g. a comment "no .show_all()"): the GTK3 key signal connect and
        # the bare Gtk.main() loop are the calls that would crash under GTK4.
        assert "connect('key-press-event'" not in src
        assert "Gtk.main()" not in src      # GTK4 uses Gtk.Application.run
        # The host must NOT add the WebView via the GTK3 container .add(); GTK4
        # is set_child. (A real `webview)` add call, not the comment mention.)
        assert ".add(webview)" not in src


# ═══════════════════════════════════════════════════════════════
# 2. Z-ORDER MODEL (1) picked IN CODE: single BACKGROUND layer, zone 0
# ═══════════════════════════════════════════════════════════════

class TestZOrderModelOne:
    @pytest.fixture(scope="class")
    def src(self):
        return _read(MODULE)

    def test_anchors_background_layer(self, src):
        # Model (1): the desktop plane is the BACKGROUND layer — below native
        # toplevels. This is what makes the shell the desktop, not an app.
        assert "Layer.BACKGROUND" in src

    def test_exclusive_zone_zero(self, src):
        # The backdrop reserves no space (exclusive zone 0) — it is not a panel.
        assert "set_exclusive_zone" in src

    def test_single_webview_keeps_js_unchanged_model_1(self, src):
        # Model (1) is exactly ONE WebView (overlays/orb co-planar) so the served
        # JS is UNCHANGED. Model (2) (two WebViews + a cross-WebView bus) would
        # break 'JS unchanged' and is explicitly NOT chosen.
        assert src.count("WebKit.WebView()") == 1, (
            "GTK4 host created != 1 WebView — Model (1) is a single layer-shell "
            "surface (JS unchanged); a second WebView would be Model (2)."
        )

    def test_model_decision_is_documented_as_chosen_in_code(self, src):
        low = src.lower()
        # The decision must be explicit (ROADMAP Phase 4 demands ONE model in code).
        assert "model (1)" in low or "model 1" in low
        # And the honest Model-1 limitation (native windows ABOVE the orb) stated.
        assert "above" in low and "orb" in low


# ═══════════════════════════════════════════════════════════════
# 3. The GTK4 broken-GPU paint floor — its OWN proof, not inherited
# ═══════════════════════════════════════════════════════════════

class TestGtk4SoftwareGLFloor:
    @pytest.fixture(scope="class")
    def src(self):
        return _read(MODULE)

    def test_forces_software_gl_in_the_session_launcher(self, src):
        # Same never-fail software-GL contract as cage Tier-3 + sway + hart-comp.
        assert "WLR_RENDERER_ALLOW_SOFTWARE=1" in src
        assert "LIBGL_ALWAYS_SOFTWARE" in src

    def test_webkit_disable_gl_paths_for_llvmpipe(self, src):
        # WebKitGTK on a GL-less display crashes on DMABUF + compositing — disable
        # both so the GTK4 host paints on llvmpipe (the GTK4 path's OWN proof).
        assert "WEBKIT_DISABLE_DMABUF_RENDERER=1" in src
        assert "WEBKIT_DISABLE_COMPOSITING_MODE=1" in src

    def test_pins_hardware_acceleration_never(self, src):
        # NEVER (not ON_DEMAND) on the default (software-GL) path — robustness over
        # a few fps, the exact lesson the GTK3 floor encodes, re-applied on GTK4.
        assert "HardwareAccelerationPolicy.NEVER" in src

    def test_forces_gsk_software_renderer_the_real_hw_paint_fix(self, src):
        # THE real-HW paint-hang fix. GTK4 draws via GSK, whose DEFAULT renderer is
        # GL — a SEPARATE GL context from WebKit's, NOT covered by WEBKIT_DISABLE_*.
        # On a real GPU that GSK GL/EGL/GBM context hangs on the layer-shell surface
        # (pointer-only black screen + shell-ready never fires); on llvmpipe it
        # resolves to software GL and paints (why the CI nixosTest passes). The GTK3
        # cage floor is immune ONLY because it has no GSK (cairo-direct). Pin GSK to
        # the cairo software renderer + disable GDK GL so the GTK4 host paints on ANY
        # GPU. Gated on !preferHardwareGL like the WEBKIT_DISABLE_* belt.
        assert "GSK_RENDERER=cairo" in src, (
            "GTK4 host missing GSK_RENDERER=cairo — GSK's default GL renderer hangs "
            "on a real GPU (works on llvmpipe, black-screens on real HW)."
        )
        assert "GDK_GL=disable" in src, (
            "GTK4 host missing GDK_GL=disable — GDK would still create a GL context."
        )

    def test_first_paint_marker_handler_is_defined_and_signals(self, src):
        # The session-supervisor's paint-watchdog drops the tier to the cage floor
        # unless /run/hart/session/shell-ready is touched within its budget. The host
        # connects 'load-changed' to _on_load_changed; that handler MUST exist and
        # call _signal_painted() on LoadEvent.FINISHED — otherwise the marker never
        # fires and a HEALTHY GTK4 tier is wrongly dropped as HUNG. (The original
        # GTK4 host connected the signal but never DEFINED the method.)
        assert "webview.connect('load-changed', self._on_load_changed)" in src
        assert "def _on_load_changed" in src, (
            "GTK4 host connects load-changed but never DEFINES _on_load_changed — "
            "_signal_painted() never runs and the shell-ready marker never fires."
        )
        assert "_signal_painted()" in src, (
            "GTK4 host never CALLS _signal_painted() — the watchdog drops the tier."
        )
        assert "WebKit.LoadEvent.FINISHED" in src, (
            "GTK4 host must gate the marker on WebKit.LoadEvent.FINISHED (first frame)."
        )


# ═══════════════════════════════════════════════════════════════
# 4. DRY: re-hosts the SAME served shell; cage GTK3 floor kept VERBATIM
# ═══════════════════════════════════════════════════════════════

class TestReusesServedShellAndKeepsFloor:
    @pytest.fixture(scope="class")
    def src(self):
        return _read(MODULE)

    def test_points_at_the_same_served_shell_no_second_renderer(self, src):
        # The GTK4 host re-hosts the SAME :6800 served shell (render_desktop_shell
        # + /shell/static) — it is a host-WINDOW port, not a second renderer of the
        # HTML/JS. It must reuse the LiquidUI port + the Nunba SPA fallback (the
        # dead-husk avoidance), not template its own page.
        assert "liquidPort" in src
        assert "/health" in src           # the same readiness probe the cage uses
        assert "nunbaPort" in src         # the same SPA fallback so it is never blank
        # It must point a URL at the server, NOT re-render the shell itself: the
        # host loads SHELL_URL via WebKit, it does not CALL render_desktop_shell()
        # (the comments legitimately NAME the served method; only an invocation
        # would be a second renderer).
        assert "load_uri(SHELL_URL)" in src
        assert "render_desktop_shell()" not in src  # no in-process re-render call

    def test_asserts_liquidui_webkit_renderer_present(self, src):
        # Coherent only when the canonical shell is served — same assertion shape
        # as sway-Tier-1 / hart-comp (no parallel renderer).
        assert "renderer" in src and "webkit" in src
        assert "assertion" in src

    def test_keeps_gtk3_cage_floor_verbatim_untouched(self):
        # The never-break gate: hart-liquid-ui.nix's GTK3 cage path is the audited
        # Tier-3 floor and MUST be byte-for-byte unchanged. This module is a
        # SEPARATE file; it must not edit the cage GTK3 host. Prove the floor still
        # carries its GTK3 + WebKit2-4.1 host (the audited shape).
        floor = _read(os.path.join(MODULES_DIR, "hart-liquid-ui.nix"))
        assert "gi.require_version('Gtk', '3.0')" in floor, (
            "the cage GTK3 floor lost its Gtk-3.0 host — Phase-4 must keep it verbatim"
        )
        assert "gi.require_version('WebKit2', '4.1')" in floor, (
            "the cage GTK3 floor lost its WebKit2-4.1 host — Phase-4 must keep it verbatim"
        )
        # The cage launcher's forced-software-GL floor is intact.
        assert "WLR_RENDERER_ALLOW_SOFTWARE=1" in floor


# ═══════════════════════════════════════════════════════════════
# 5. gtk4-layer-shell + webkitgtk_6_0 added to the package set
# ═══════════════════════════════════════════════════════════════

class TestPackageSet:
    @pytest.fixture(scope="class")
    def src(self):
        return _read(MODULE)

    def test_gtk4_layer_shell_in_package_set(self, src):
        # The task's explicit deliverable: add gtk4-layer-shell to the package set.
        assert "gtk4-layer-shell" in src

    def test_webkitgtk_6_0_in_package_set(self, src):
        assert "webkitgtk_6_0" in src

    def test_gtk4_in_package_set(self, src):
        assert "pkgs.gtk4" in src

    def test_layer_shell_capable_compositor_present(self, src):
        # cage implements no zwlr_layer_shell_v1; the host needs a layer-shell-
        # capable compositor (sway/hart-comp) to actually anchor the surface.
        assert "pkgs.sway" in src

    def test_typelibs_on_gi_path(self, src):
        # The GTK4 toolkit typelibs must be on GI_TYPELIB_PATH (the #99-103
        # makeSearchPathOutput "out" lesson) or gi.require_version raises + the
        # host dies on launch — exactly the cage SIGABRT class.
        assert "GI_TYPELIB_PATH" in src
        assert 'makeSearchPathOutput "out"' in src
        for tl in ("gtk4", "webkitgtk_6_0", "gtk4-layer-shell"):
            assert tl in src


# ═══════════════════════════════════════════════════════════════
# 5b. #150 MIC: WebKit getUserMedia capture needs a GStreamer plugin path
# ═══════════════════════════════════════════════════════════════
# Clearly-labelled SOURCE GUARD (per feedback_no_grep_tests.md): the BEHAVIOURAL
# proof — that a real GStreamer capture element resolves on the host's exported
# path so WebKit getUserMedia has a mic source — is the VM nixosTest's #150 subtest
# (it runs gst-inspect against the path); that cannot run on the Windows dev box.
# These dev-box guards assert the structural wiring the VM proof depends on: the
# host must EXPORT GST_PLUGIN_SYSTEM_PATH_1_0 built from the capture plugin set.

class TestMicCaptureGStreamerWiring:
    @pytest.fixture(scope="class")
    def src(self):
        return _read(MODULE)

    def test_host_exports_gst_plugin_system_path(self, src):
        # Without this export the host's minimal env hides every GStreamer plugin,
        # so WebKitGTK 6.0 finds NO audio source element and getUserMedia fails
        # "microphone access denied" despite the permission handler allowing it.
        assert "GST_PLUGIN_SYSTEM_PATH_1_0" in src
        # It must be a real export inside the host wrapper, derived from the plugin
        # search path binding (not only named in the explanatory comment).
        assert 'export GST_PLUGIN_SYSTEM_PATH_1_0="${gstPluginPath}"' in src

    def test_plugin_path_is_built_from_the_gstreamer_plugin_dir(self, src):
        # makeSearchPath over lib/gstreamer-1.0 is what makes the capture elements
        # discoverable (and pins them into the host closure).
        assert 'makeSearchPath "lib/gstreamer-1.0"' in src

    def test_capture_plugin_set_includes_an_audio_source(self, src):
        # gst-plugins-good ships pulsesrc (capture via PipeWire's pulse compat) and
        # pipewire ships pipewiresrc (native capture) — at least the proven pulse
        # path + the native one must be in the plugin set the path is built from.
        assert "gst-plugins-good" in src   # pulsesrc — the proven capture element
        assert "gst_all_1" in src          # the GStreamer plugin family
        assert "pipewire" in src           # pipewiresrc — native PipeWire capture

    def test_permission_handler_still_allows_first_party_capture(self, src):
        # The capture path is only useful if the WebView still ALLOWs the mic
        # request (the other half of #150) — guard the existing permission wiring
        # so a refactor cannot silently drop it and re-deny the mic.
        assert "permission-request" in src
        assert "set_enable_media_stream" in src


# ═══════════════════════════════════════════════════════════════
# 6. Opt-in, does NOT flip defaultSession, registers a session, VM-pending
# ═══════════════════════════════════════════════════════════════

class TestNeverFailInvariants:
    @pytest.fixture(scope="class")
    def src(self):
        return _read(MODULE)

    def test_registers_selectable_session_but_does_not_flip_default(self, src):
        assert 'providedSessions = [ "hart-glass-gtk4" ]' in src
        assert "services.displayManager.sessionPackages" in src
        # CRITICAL never-fail invariant: this module must NEVER ASSIGN
        # defaultSession (prose mentioning "defaultSession STAYS cage" is fine; an
        # assignment is not). Cage GTK3 stays the floor until the GTK4 broken-GPU
        # proof passes in CI.
        assert "defaultSession =" not in src
        assert "displayManager.defaultSession" not in src

    def test_opt_in_default_off(self, src):
        assert "mkEnableOption" in src

    def test_marked_vm_pending_not_booted_on_windows(self, src):
        low = src.lower()
        assert "windows dev box" in low
        assert "vm" in low and ("pending" in low or "ci-pending" in low or "vm/ci" in low)

    def test_supervisor_wiring_is_documented_not_self_assigned(self, src):
        # The GTK4 host can become the supervisor's Tier-2 launcher ONLY after its
        # broken-GPU proof — and the supervisor is a SEPARATE module/owner. This
        # module documents the contract; it must NOT edit the supervisor itself.
        assert "sessionSupervisor" in src or "supervisor" in src.lower()
        assert "hart.sessionSupervisor.swayCommand" in src


# ═══════════════════════════════════════════════════════════════
# 7. The Phase-4 nixosTest exists, is dead-husk-aware, drops to the floor
# ═══════════════════════════════════════════════════════════════

class TestPhase4NixosTest:
    @pytest.fixture(scope="class")
    def src(self):
        return _read(NIXTEST)

    def test_built_from_mknode_70_safe(self, src):
        # #70 discipline: built from hartModules via the shared mkNode, NOT a full
        # ISO config import (which would drag the installer-CD overlay). mkNode
        # (from ./lib.nix) imports hartModules ALONE — it is the #70-safe builder by
        # construction, so asserting its use IS the #70 guarantee (the floor-lock /
        # session-supervisor tests rely on the same positive signal). The negative
        # signal is an interpolated config IMPORT (`configurations/${variant}.nix`,
        # the mkSystem idiom), matched by the `${` interpolation so the #70
        # discipline COMMENT ("NO ../configurations/X.nix") does not trip it.
        assert "import ./lib.nix" in src
        assert "mkNode" in src
        assert "configurations/${" not in src  # no interpolated installer-CD import

    def test_enables_the_gtk4_host_on_a_desktop_node(self, src):
        assert "hart.layerShellHost.enable = true" in src
        assert 'mkNode "desktop"' in src

    def test_asserts_gtk4_toolkit_typelibs_in_closure(self, src):
        # The GTK4 host can only launch if its toolkit typelibs are realized.
        assert "Gtk-4.0.typelib" in src
        assert "WebKit-6.0.typelib" in src        # the GTK4 WebKit binding
        assert "Gtk4LayerShell-1.0.typelib" in src

    def test_dead_husk_aware_real_static_fetch(self, src):
        # The f294f52 lesson carried into the GTK4 path: a REAL /shell/static fetch
        # (curl, not inline render) returns 200 + non-empty.
        assert "/shell/static/hartHero.js" in src
        assert "curl -fs" in src
        assert "dead husk" in src.lower() or "dead-husk" in src.lower()

    def test_proves_gtk4_crash_drops_to_gtk3_cage_floor(self, src):
        # The never-break gate: the GTK3 cage Tier-3 floor is present + hardened so
        # a GTK4-host crash always lands on a tier that paints.
        assert "hart-shell-session" in src           # the cage GTK3 floor launcher
        assert "WebKit2-4.1.typelib" in src          # the floor's GTK3 host intact
        low = src.lower()
        assert "floor" in low and ("crash" in low or "drop" in low)

    def test_asserts_model_1_zorder_in_the_host(self, src):
        # The test re-asserts the in-code z-order decision (load-bearing).
        assert "Layer.BACKGROUND" in src
        assert "set_exclusive_zone" in src

    def test_needs_ci_vm_only(self, src):
        low = src.lower()
        assert "[vm]" in low or "llvmpipe" in low
        assert "cannot run on the windows dev box" in low or "windows dev box" in low

    def test_has_a_fresh_gtk4_paint_proof_node(self, src):
        # ROADMAP Phase 4 demands a FRESH broken-GPU paint proof under the GTK4
        # NEVER-accel equivalents — not just the structural node. A second GDM-
        # driven node must autologin the GTK4 session and OCR a painted frame off
        # the llvmpipe framebuffer (the toolkit port's OWN paint floor). The
        # structural node has no DM and explicitly defers the live paint.
        assert "hart-layer-shell-host-paint" in src, (
            "missing the FRESH GTK4 paint-proof node — structural-only does not "
            "satisfy ROADMAP Phase 4's own-broken-GPU paint proof"
        )
        # It must autologin the GTK4 session (not the cage session) so the GTK4
        # layer-shell surface is what paints.
        assert 'defaultSession = "hart-glass-gtk4"' in src
        assert "autoLogin" in src
        # OCR proof of pixels presented (un-fakeable), exactly mirroring the cage
        # desktop-boot paint proof but on the GTK4 session.
        assert "enableOCR = true" in src
        assert 'wait_for_text("HART"' in src
        # The paint must be of the Model-1 BACKGROUND/zone-0 surface under NEVER.
        assert "HardwareAccelerationPolicy.NEVER" in src
        assert "Layer.BACKGROUND" in src

    def test_paint_proof_drops_to_cage_floor_on_gtk4_crash(self, src):
        # The never-break gate, exercised live in the paint node: killing the GTK4
        # host lands on the cage GTK3 floor, which is still software-GL and whose
        # served shell still serves (post-drop screen is not blank/dead-husk).
        assert "hart-shell-session" in src          # the cage floor launcher
        low = src.lower()
        assert "kill" in low and "cage" in low and "floor" in low

    def test_paint_node_asserts_shell_ready_marker_touched_e2e(self, src):
        # The full paint+marker E2E the task names: OCR proves PIXELS, this proves
        # the GTK4 host's _on_load_changed actually touched /run/hart/session/
        # shell-ready on first paint (so the supervisor's HUNG guard sees a HEALTHY
        # Tier-2 — a painting surface must NOT be dropped as hung). The paint node
        # waits for the marker after the OCR.
        assert "/run/hart/session/shell-ready" in src
        low = src.lower()
        assert "marker" in low
        # Asserted via a real filesystem wait, not prose.
        assert "wait_until_succeeds" in src and "shell-ready" in src

    def test_structural_node_asserts_tier1_tier2_same_host_binary(self, src):
        # DRY across tiers: the structural node reads back the Tier-2 sway host
        # config + (when armed) the Tier-1 hart-comp launcher and asserts BOTH
        # reference the identical hart-glass-shell-gtk4 binary — one glass host, not
        # a per-tier copy.
        assert "hart-comp-session" in src
        assert "hart-gtk4-layer-host.conf" in src
        # The binary basename is the single source of truth shared across tiers.
        assert src.count("hart-glass-shell-gtk4") >= 1


# ═══════════════════════════════════════════════════════════════
# 8. Wired into the flake: hartModules[] + checks (else it never runs)
# ═══════════════════════════════════════════════════════════════

class TestFlakeWiring:
    @pytest.fixture(scope="class")
    def flake(self):
        return _read(os.path.join(NIXOS_DIR, "flake.nix"))

    def test_module_in_hart_modules(self, flake):
        # An opt-in module that is not in hartModules[] would not expose its option
        # to any variant (and the nixosTest enabling it would not evaluate).
        assert "hart-layer-shell-host.nix" in flake

    def test_nixostest_wired_into_checks(self, flake):
        # A test that never runs guards nothing (CLAUDE.md Gate 5). It must be
        # imported AND merged into `checks`.
        assert "tests/layer-shell-host.nix" in flake
        assert "layerShellHost" in flake

    def test_roadmap_phase4_is_the_gtk4_port(self):
        roadmap = _read(os.path.join(COMPOSITOR_DIR, "ROADMAP.md"))
        low = roadmap.lower()
        assert "gtk3" in low and "gtk4" in low
        assert "gtk4-layer-shell" in low


# ═══════════════════════════════════════════════════════════════
# 9. Cross-file: still NO module flips defaultSession (floor holds)
# ═══════════════════════════════════════════════════════════════

class TestNeverFailOrderingInvariant:
    def test_no_session_module_flips_default_session(self):
        # The GTK4 host JOINS the never-fail ladder; like sway/hart-comp it must
        # not assign defaultSession (cage stays the floor until VM-proof).
        for name in (
            "hart-sway-tier1.nix",
            "hart-comp.nix",
            "hart-layer-shell-host.nix",
        ):
            src = _read(os.path.join(MODULES_DIR, name))
            assert "defaultSession =" not in src, (
                f"{name} must NOT assign defaultSession — cage stays the floor "
                f"until a higher tier is VM-proven (ROADMAP §6 never-fail invariant)."
            )
            assert "displayManager.defaultSession" not in src, (
                f"{name} must NOT touch displayManager.defaultSession."
            )


# ═══════════════════════════════════════════════════════════════
# Extraction helpers — pull the EMBEDDED host scripts out of the Nix
# modules so they can be SYNTAX-checked on the dev box (py_compile /
# dash -n), not merely grepped. The Nix wrapper cannot be built here
# (no Wayland/wlroots/WebKitGTK), but the shell + python it embeds ARE
# plain text whose SYNTAX is verifiable today. This is the dev-box-side
# of the task's "py_compile the host python; dash -n the host scripts".
# ═══════════════════════════════════════════════════════════════

import re
import shutil
import subprocess
import sys
import tempfile


def _skip_nix_antiquote(s, i):
    """Given ``s[i:i+2] == '${'`` (the start of a Nix antiquotation), return the
    index just past its matching ``}``. Tracks brace nesting AND skips over any
    nested Nix strings inside the antiquote — both double-quoted (``"…"``, which
    may legitimately contain ``"ON_DEMAND"`` etc.) and indented (``''…''``, e.g.
    the cage host's ``${lib.optionalString ui.runOnboardingInKiosk ''…''}``) — so
    a ``"`` or ``''`` INSIDE the antiquote is never mistaken for the enclosing
    string's terminator. This is the crux: the host scripts embed antiquotations
    whose Nix code contains the very quote chars that delimit the outer string."""
    n = len(s)
    j = i + 2          # past the '${'
    depth = 1
    while j < n and depth > 0:
        c = s[j]
        if c == "{":
            depth += 1
            j += 1
        elif c == "}":
            depth -= 1
            j += 1
        elif c == '"':
            # nested double-quoted Nix string — skip to its close (handle \").
            j += 1
            while j < n and s[j] != '"':
                if s[j] == "\\":
                    j += 1
                j += 1
            j += 1
        elif c == "'" and j + 1 < n and s[j + 1] == "'":
            # nested indented Nix string ''…'' — skip to its close ''.
            j += 2
            while j < n - 1 and not (s[j] == "'" and s[j + 1] == "'"):
                j += 1
            j += 2
        else:
            j += 1
    return j


def _neutralize_nix_interp(text):
    """Replace Nix ``${...}`` antiquotations with a syntactically-inert token so
    the extracted body parses as plain shell / python.

    ``${pkgs.curl}`` / ``${liquidPort}`` / ``${if … then "ON_DEMAND" else …}`` are
    real Nix antiquotations (Nix substitutes a store path / string at build time).
    After extraction these are not valid shell/python, so collapse each to the
    bareword ``NIX`` — a valid identifier fragment in both languages
    (``${pkgs.curl}/bin/curl`` -> ``NIX/bin/curl``;
    ``HardwareAccelerationPolicy.${if …}`` -> ``HardwareAccelerationPolicy.NIX``).
    Antiquote boundaries are found with the brace/string-aware scanner above (NOT
    a naive regex), so an antiquote containing braces or quotes is collapsed whole.

    NOTE: ``''${VAR}'' `` (Nix's escape for a LITERAL ``${VAR}`` that must reach the
    generated bash) is un-escaped to a genuine ``${VAR}`` by the caller BEFORE this
    runs, so a real shell parameter expansion survives and is not collapsed."""
    out = []
    i = 0
    n = len(text)
    while i < n:
        if text[i] == "$" and i + 1 < n and text[i + 1] == "{":
            end = _skip_nix_antiquote(text, i)
            out.append("NIX")
            i = end
        else:
            out.append(text[i])
            i += 1
    return "".join(out)


def _scan_nix_indented_string(s, start):
    """Return (body, end) for a Nix indented string whose body begins at ``start``
    (just past the opening ``''``). Walks to the closing ``''`` while honoring the
    two Nix escapes — ``''${`` (escaped antiquote) and ``'''`` (escaped quote) — and
    SKIPPING any ``${…}`` antiquotation (whose nested ``''…''`` would otherwise look
    like the terminator). ``body`` is the raw substring; ``end`` is past the ``''``."""
    n = len(s)
    i = start
    while i < n - 1:
        if s[i] == "$" and s[i + 1] == "{":
            i = _skip_nix_antiquote(s, i)
            continue
        if s[i] == "'" and s[i + 1] == "'":
            nxt = s[i + 2] if i + 2 < n else ""
            if nxt == "$" and i + 3 < n and s[i + 3] == "{":
                i += 2          # ''${ escaped antiquote — not a terminator
                continue
            if nxt == "'":
                i += 3          # ''' escaped single-quote
                continue
            return s[start:i], i + 2   # genuine closing ''
        i += 1
    return s[start:n], n


def _extract_shell_script_body(nix_src, drv_name):
    """Return the bash body of ``writeShellScriptBin "<drv_name>" ''…''`` with the
    Nix escape un-applied (``''${`` -> ``${``) and antiquotations neutralized,
    ready for ``dash -n``. Uses the antiquote-aware indented-string scanner so a
    nested ``${lib.optionalString … ''…''}`` does not truncate the body."""
    marker = 'writeShellScriptBin "%s" \'\'' % drv_name
    start = nix_src.index(marker) + len(marker)
    body, _ = _scan_nix_indented_string(nix_src, start)
    # Un-escape Nix's ''${ -> ${ so a LITERAL shell ${VAR} expansion survives
    # (e.g. HART_SHELL_READY_FLAG="''${HART_SHELL_READY_FLAG:-…}"), BEFORE the
    # antiquote-collapse pass (which would otherwise eat the real expansion).
    body = body.replace("''${", "${")
    return _neutralize_nix_interp(body)


def _extract_python_c_body(nix_src):
    """Return the python passed to ``python -c "…"`` inside the host wrapper, with
    Nix antiquotations neutralized, ready for ``py_compile``.

    The python is a Nix DOUBLE-quoted string (``python -c "<py>"``). Its body can
    contain ``${if … then "ON_DEMAND" else "NEVER"}`` — antiquotations whose nested
    ``"`` would fool a naive ``index('"')``. Walk to the real closing ``"`` while
    skipping ``${…}`` antiquotations and honoring ``\\`` escapes."""
    marker = 'python -c "'
    start = nix_src.index(marker) + len(marker)
    i = start
    n = len(nix_src)
    while i < n:
        c = nix_src[i]
        if c == "\\":
            i += 2
            continue
        if c == "$" and i + 1 < n and nix_src[i + 1] == "{":
            i = _skip_nix_antiquote(nix_src, i)
            continue
        if c == '"':
            break               # the real closing quote of the Nix string
        i += 1
    body = nix_src[start:i]
    return _neutralize_nix_interp(body)


def _dash_check(script_text):
    """Run ``dash -n`` (POSIX-sh syntax check, no execution) on script_text.
    Returns (ok, stderr). Skips cleanly if dash is unavailable."""
    dash = shutil.which("dash") or "/usr/bin/dash"
    if not os.path.exists(dash):
        pytest.skip("dash not available on this host")
    # newline="\n": force LF line endings. On Windows the default text mode writes
    # CRLF, and dash chokes on the trailing \r ("word unexpected" at the first
    # compound statement) — a false failure that has nothing to do with the script.
    with tempfile.NamedTemporaryFile(
        "w", suffix=".sh", delete=False, encoding="utf-8", newline="\n"
    ) as f:
        f.write(script_text)
        path = f.name
    try:
        proc = subprocess.run(
            [dash, "-n", path], capture_output=True, text=True
        )
        return proc.returncode == 0, proc.stderr
    finally:
        os.unlink(path)


def _py_compile_check(py_text):
    """Compile py_text in-process via ``compile()`` (py_compile's core) — proves
    the embedded host python PARSES + builds bytecode. Returns (ok, err)."""
    try:
        compile(py_text, "<embedded-glass-host>", "exec")
        return True, ""
    except SyntaxError as e:
        return False, f"{e.__class__.__name__}: {e}"


# ═══════════════════════════════════════════════════════════════
# 10. The embedded host scripts COMPILE: py_compile the host python +
#     dash -n the host shell wrappers (BOTH the GTK4 host AND the GTK3
#     cage floor — the two real paint hosts). A grep test proves a string
#     survived; THIS proves the script the kiosk execs is not malformed.
# ═══════════════════════════════════════════════════════════════

class TestHostScriptsCompile:
    @pytest.fixture(scope="class")
    def gtk4_src(self):
        return _read(MODULE)

    @pytest.fixture(scope="class")
    def cage_src(self):
        return _read(os.path.join(MODULES_DIR, "hart-liquid-ui.nix"))

    def test_gtk4_host_python_py_compiles(self, gtk4_src):
        # The GTK4 host's `python -c` body must PARSE — a malformed host crashes
        # the GTK4 Tier-2 session the instant it execs (no syntax error is caught
        # by the nixosTest's grep assertions). compile() is py_compile's core.
        py = _extract_python_c_body(gtk4_src)
        # Sanity: we actually extracted the GTK4 host, not an empty slice.
        assert "GlassShellLayer" in py and "gi.require_version('Gtk', '4.0')" in py, (
            "GTK4 host python extraction failed — body did not contain the host class"
        )
        ok, err = _py_compile_check(py)
        assert ok, f"GTK4 host python does NOT compile:\n{err}"

    def test_cage_gtk3_host_python_py_compiles(self, cage_src):
        # The GTK3 cage Tier-3 FLOOR host python must also parse — it is the tier
        # a GTK4 crash drops to; a malformed floor host would defeat the never-fail
        # ladder. Same compile guard, the floor's host body.
        py = _extract_python_c_body(cage_src)
        assert "class GlassShell" in py and "gi.require_version('Gtk', '3.0')" in py, (
            "cage GTK3 host python extraction failed — body did not contain GlassShell"
        )
        ok, err = _py_compile_check(py)
        assert ok, f"cage GTK3 floor host python does NOT compile:\n{err}"

    def test_gtk4_host_shell_wrapper_dash_n_clean(self, gtk4_src):
        # The GTK4 host's shell wrapper (the curl-probe + env-export preamble that
        # execs python) must be POSIX-sh valid — a syntax error there means the
        # host never launches. dash -n is the no-execute POSIX syntax check.
        sh = _extract_shell_script_body(gtk4_src, "hart-glass-shell-gtk4")
        assert "HART_SHELL_READY_FLAG" in sh and "GI_TYPELIB_PATH" in sh, (
            "GTK4 host shell extraction failed — preamble markers missing"
        )
        ok, err = _dash_check(sh)
        assert ok, f"GTK4 host shell wrapper fails dash -n:\n{err}"

    def test_gtk4_session_launcher_dash_n_clean(self, gtk4_src):
        # The session launcher (forces software GL, execs sway onto the host) is a
        # separate writeShellScriptBin — syntax-check it too.
        sh = _extract_shell_script_body(gtk4_src, "hart-glass-shell-gtk4-session")
        assert "WLR_RENDERER_ALLOW_SOFTWARE=1" in sh, (
            "GTK4 session launcher extraction failed — software-GL env missing"
        )
        ok, err = _dash_check(sh)
        assert ok, f"GTK4 session launcher fails dash -n:\n{err}"

    def test_cage_gtk3_host_shell_wrapper_dash_n_clean(self, cage_src):
        # Parity: the cage floor's glass-shell wrapper must be POSIX-sh valid too
        # (it is the tier a GTK4 crash lands on).
        sh = _extract_shell_script_body(cage_src, "hart-glass-shell")
        assert "HART_SHELL_READY_FLAG" in sh, (
            "cage host shell extraction failed — marker preamble missing"
        )
        ok, err = _dash_check(sh)
        assert ok, f"cage GTK3 host shell wrapper fails dash -n:\n{err}"

    def test_cage_gtk3_session_launcher_dash_n_clean(self, cage_src):
        sh = _extract_shell_script_body(cage_src, "hart-shell-session")
        assert "WLR_RENDERER_ALLOW_SOFTWARE=1" in sh, (
            "cage session launcher extraction failed — software-GL env missing"
        )
        ok, err = _dash_check(sh)
        assert ok, f"cage GTK3 session launcher fails dash -n:\n{err}"


# ═══════════════════════════════════════════════════════════════
# 11. PARITY — the GTK3 cage floor host satisfies the SAME paint-watchdog
#     contract as the GTK4 host: define _on_load_changed, call
#     _signal_painted() on LoadEvent.FINISHED, touch /run/hart/session/
#     shell-ready. BOTH hosts must keep the marker or the supervisor wrongly
#     drops a HEALTHY tier (the pointer-only regression, on either host).
# ═══════════════════════════════════════════════════════════════

class TestCageGtk3MarkerParity:
    @pytest.fixture(scope="class")
    def cage(self):
        return _read(os.path.join(MODULES_DIR, "hart-liquid-ui.nix"))

    @pytest.fixture(scope="class")
    def gtk4(self):
        return _read(MODULE)

    def test_cage_host_defines_and_connects_load_changed(self, cage):
        # The cage GTK3 host connects 'load-changed' AND defines the handler — the
        # original GTK4 bug (connected-but-undefined) must never exist on the floor.
        assert "webview.connect('load-changed', self._on_load_changed)" in cage
        assert "def _on_load_changed" in cage, (
            "cage GTK3 host connects load-changed but never DEFINES it — the floor "
            "would never touch shell-ready and the watchdog would drop a healthy floor."
        )

    def test_cage_host_signals_paint_on_finished(self, cage):
        # On LoadEvent.FINISHED the cage host must call _signal_painted() — the same
        # first-frame marker the GTK4 host fires (WebKit2 enum on the GTK3 binding).
        assert "_signal_painted()" in cage
        assert "WebKit2.LoadEvent.FINISHED" in cage, (
            "cage host must gate the marker on WebKit2.LoadEvent.FINISHED (first frame)."
        )

    def test_cage_host_touches_the_same_shell_ready_marker(self, cage):
        # Same /run/hart contract path + same HART_SHELL_READY_FLAG override the
        # GTK4 host uses — ONE marker path both hosts honor (DRY watchdog contract).
        assert "/run/hart/session/shell-ready" in cage
        assert "HART_SHELL_READY_FLAG" in cage

    def test_both_hosts_share_the_identical_marker_contract(self, cage, gtk4):
        # The marker path + env var + write semantics are byte-identical across the
        # two hosts (one watchdog contract, two toolkit hosts). Assert the shared
        # tokens appear in BOTH so a future edit to one can't silently diverge.
        for token in (
            "/run/hart/session/shell-ready",
            'os.environ.get(\'HART_SHELL_READY_FLAG\'',
            "os.makedirs(os.path.dirname(READY_FLAG), exist_ok=True)",
        ):
            assert token in cage, f"cage host missing shared marker token: {token!r}"
            assert token in gtk4, f"GTK4 host missing shared marker token: {token!r}"

    def test_cage_floor_signal_painted_is_oserror_safe(self, cage):
        # The marker write must NEVER crash the floor host (a missing /run/hart dir
        # / EROFS must degrade, not SIGABRT) — the supervisor escalates DOWN on a
        # missing marker, so a crash here would be strictly worse than no marker.
        assert "except OSError:" in cage, (
            "cage _signal_painted must swallow OSError — a marker write must never "
            "crash the never-fail floor host."
        )


# ═══════════════════════════════════════════════════════════════
# 12. DRY — Tier-1 (hart-comp) and Tier-2 (sway/layer-shell host) launch the
#     SAME `hart-glass-shell-gtk4` glass host binary (ONE source). The whole
#     point of the layer-shell host is that every higher tier re-hosts the
#     one served shell through the one host window — not a per-tier copy.
# ═══════════════════════════════════════════════════════════════

class TestTier1Tier2SameGlassHostBinary:
    @pytest.fixture(scope="class")
    def comp(self):
        return _read(os.path.join(MODULES_DIR, "hart-comp.nix"))

    @pytest.fixture(scope="class")
    def layer(self):
        return _read(MODULE)

    def test_tier2_sessions_exec_the_gtk4_host_binary(self, layer):
        # Tier-2 = the layer-shell host module: its sway config execs the GTK4 host
        # binary as sway's single startup client (the one source of the host).
        assert "hart-glass-shell-gtk4" in layer
        assert "exec ${layerShellHost}/bin/hart-glass-shell-gtk4" in layer, (
            "Tier-2 sway host config must exec the layerShellHost GTK4 binary."
        )

    def test_tier1_hartcomp_launches_the_same_gtk4_host_binary(self, comp):
        # Tier-1 = hart-comp: its session launcher must run the SAME
        # `hart-glass-shell-gtk4` binary (preferred) so Tier-1 and Tier-2 are the
        # one host, not two. hart-comp finds it on PATH (it is added to
        # systemPackages by the layer-shell host module), the GTK3 cage host is the
        # documented fallback only.
        assert "hart-glass-shell-gtk4" in comp, (
            "Tier-1 hart-comp must launch the SAME hart-glass-shell-gtk4 host as "
            "Tier-2 — one glass host across the tiers (DRY), not a per-tier copy."
        )

    def test_both_tiers_name_the_identical_binary_one_source(self, comp, layer):
        # The binary NAME is the single source of truth shared across both tiers.
        # If the layer-shell module renamed the drv, Tier-1's PATH lookup would
        # silently miss and fall back to the GTK3 cage host (a parity regression).
        binary = "hart-glass-shell-gtk4"
        assert binary in layer and binary in comp, (
            "Tier-1 (hart-comp) and Tier-2 (layer-shell host) must reference the "
            f"identical glass host binary name {binary!r} — one source."
        )
        # And the layer-shell module is the SOLE definer (writeShellScriptBin) of
        # that binary — hart-comp only references it, it does not redefine a copy.
        assert 'writeShellScriptBin "hart-glass-shell-gtk4"' in layer, (
            "the GTK4 host binary must be DEFINED once in hart-layer-shell-host.nix."
        )
        assert 'writeShellScriptBin "hart-glass-shell-gtk4"' not in comp, (
            "hart-comp must NOT redefine the GTK4 host — it references the one "
            "source on PATH (a second definition would be a parallel path)."
        )
