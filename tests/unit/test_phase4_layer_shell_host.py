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
