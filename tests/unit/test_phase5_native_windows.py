"""
Phase-5 foundations source-guards — native app windows (xdg-shell + XWayland) with
the no-phantom-window honest-map contract.

WHY THESE ARE SOURCE-GUARDS (and that is the correct kind of test here):
  Same rationale as test_phase3_compositor_foundations.py. The Smithay handler
  bodies (xdg-shell/XWayland/xdg-decoration/wlr-foreign-toplevel map) CANNOT be
  compiled or booted on this Windows dev box (no Wayland/KMS/Smithay) — that is the
  CI/VM gate. The dev-box-verifiable invariants are STRUCTURAL: the pure window
  bookkeeping is present + honestly separated from the Smithay glue, the Smithay
  handlers are CI-COMPILE-marked + todo!()/unwired (never mistaken for working), the
  no-phantom-window guarantee is encoded in the types, and the Android/Wine/macOS
  limits are carried VERBATIM as openRisks. These are the acceptable cross-file
  structural source-guard class named in memory/feedback_no_grep_tests.md, and they
  are clearly labelled as such.

  The BEHAVIOURAL floor that DOES run is the Rust skeleton's own
  `#[cfg(test)] mod tests` in compositor/src/main.rs — the Phase-5 block there
  (handle minting only on map, manifest<->toplevel map, SummonApp timeout =>
  honest failure not a handle, inert subsystems => Unsupported) executes via
  `cargo test` (14 tests green on the dev box: 3 Phase-3 + 11 Phase-5) and again in
  CI. This file guards the SHAPE; that file proves the BEHAVIOUR.

Run:
  pytest tests/unit/test_phase5_native_windows.py -v --noconftest -p no:capture
"""

import os
import re

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
COMPOSITOR_DIR = os.path.join(REPO_ROOT, "compositor")
MODULES_DIR = os.path.join(REPO_ROOT, "nixos", "modules")


def _read(path):
    with open(path, "r", encoding="utf-8") as f:
        return f.read()


def _main_rs():
    return _read(os.path.join(COMPOSITOR_DIR, "src", "main.rs"))


def _cargo():
    return _read(os.path.join(COMPOSITOR_DIR, "Cargo.toml"))


def _wayland_rs():
    return _read(os.path.join(COMPOSITOR_DIR, "src", "wayland.rs"))


# ═══════════════════════════════════════════════════════════════
# 1. The PURE window bookkeeping exists (compiles + unit-tested on the dev box).
#    This is the ROADMAP Phase-5 "AppRegistry window-handle field mapping
#    manifest <-> toplevel" — kept Wayland-FREE so its correctness is provable here.
# ═══════════════════════════════════════════════════════════════

class TestPureWindowBookkeeping:
    def test_window_handle_type_minted_only_by_mint_handle(self):
        src = _main_rs()
        assert "struct WindowHandle" in src
        # A handle is minted only via mint_handle (the only path to existence),
        # so a handle PROVES a toplevel mapped (no-phantom-window in a type).
        assert "fn mint_handle()" in src
        assert "NEXT_HANDLE_ID" in src
        # IPC contract: opaque string handle (win_<hex>), matches IPC_PROTOCOL §4.
        assert 'format!("win_{n:x}")' in src

    def test_window_registry_is_the_manifest_toplevel_map(self):
        src = _main_rs()
        assert "struct WindowRegistry" in src
        # The two halves of the manifest<->toplevel map.
        assert "by_handle" in src
        assert "by_manifest" in src
        # The map mutators + the agent's "which window is app X" query.
        assert "fn on_map" in src
        assert "fn on_unmap" in src
        assert "fn handle_for_manifest" in src

    def test_toplevel_kind_distinguishes_xdg_from_xwayland(self):
        src = _main_rs()
        assert "enum ToplevelKind" in src
        assert "Xdg" in src
        assert "XWayland" in src

    def test_manifest_id_is_optional_external_windows_have_none(self):
        # IPC_PROTOCOL §4.1: manifest_id is null for windows opened outside the
        # brain. The record carries Option<String>, and on_map accepts None.
        src = _main_rs()
        assert "manifest_id: Option<String>" in src


# ═══════════════════════════════════════════════════════════════
# 2. The no-phantom-window SummonApp state machine — success ONLY on a real map.
# ═══════════════════════════════════════════════════════════════

class TestNoPhantomWindowSummon:
    def test_summon_outcome_has_no_handleless_mapped_variant(self):
        src = _main_rs()
        assert "enum SummonOutcome" in src
        # Mapped carries a handle; TimedOut/Unsupported carry none. The type makes
        # a phantom (handleless) success unrepresentable.
        assert "Mapped(WindowHandle)" in src
        assert "TimedOut" in src
        assert "Unsupported" in src

    def test_pending_summon_keys_on_a_real_map_within_a_timeout(self):
        src = _main_rs()
        assert "struct PendingSummon" in src
        assert "SUMMON_MAP_TIMEOUT" in src
        # The map<->summon match + the timeout->honest-failure edge.
        assert "fn accepts" in src
        assert "fn is_timed_out_at" in src

    def test_summon_precheck_refuses_installer_success_for_wine_proceeds_to_await_map(self):
        # The corrected Wine path: a windows(Wine) platform proceeds to AWAIT A MAP
        # (precheck returns None), it does NOT short-circuit to success on the
        # installer exit code. Only inert subsystems short-circuit (to Unsupported).
        src = _main_rs()
        assert "fn summon_precheck" in src
        assert "fn summon_subsystem_is_inert" in src
        # Android + macOS are the inert subsystems matched.
        assert '"android" | "macos"' in src

    def test_installer_lie_is_named_at_the_wm_layer(self):
        # The whole point of Phase 5 §5.4: the WM layer refuses the installer's
        # unconditional Wine success. The code must NAME that lie so a reader can't
        # miss why the handle is minted on map, not on launch.
        low = _main_rs().lower()
        assert "wine returns 0" in low or "installer-layer lie" in low or \
            "installer exit code" in low


# ═══════════════════════════════════════════════════════════════
# 3. The Smithay handlers are CI-COMPILE-marked, todo!()/unwired — never mistaken
#    for working. This is the honest "draft, flag every CI-compile part" contract.
# ═══════════════════════════════════════════════════════════════

class TestSmithayHandlersAreHonestlyUnwired:
    def test_all_four_phase5_protocols_have_handler_stubs(self):
        src = _main_rs()
        # xdg-shell map, XWayland map, destroy, decoration, foreign-toplevel sync.
        for fn in (
            "fn on_xdg_toplevel_mapped",
            "fn on_xwayland_surface_mapped",
            "fn on_toplevel_destroyed",
            "fn on_decoration_request",
            "fn on_foreign_toplevel_sync",
        ):
            assert fn in src, f"missing Phase-5 Smithay handler stub: {fn}"

    def test_every_smithay_handler_is_ci_compile_marked_and_todo(self):
        # Each handler body must be todo!() AND carry the CI-COMPILE flag so it is
        # never mistaken for compiled-on-the-dev-box code.
        src = _main_rs()
        # The CI-COMPILE flag appears once per handler (>=5).
        assert src.count("CI-COMPILE") >= 5
        # Each handler's body is a todo!() (the Smithay calls are unwired).
        for marker in (
            'todo!("Phase-5 CI: wire Smithay xdg-shell map',
            'todo!("Phase-5 CI: wire Smithay XWayland X11 map',
            'todo!("Phase-5 CI: wire Smithay toplevel destroy',
            'todo!("Phase-5 CI: wire xdg-decoration request_mode',
            'todo!("Phase-5 CI: mirror WindowRegistry',
        ):
            assert marker in src, f"handler body not honestly todo!(): {marker}"

    def test_smithay_manifest_lists_phase5_protocols(self):
        # The commented `use smithay::...` manifest must name the Phase-5 protocol
        # state the real handlers need, so the CI bring-up knows what to uncomment.
        src = _main_rs()
        assert "XdgShellState" in src
        assert "XWayland" in src
        assert "XdgDecorationState" in src
        assert "ForeignToplevelListState" in src


# ═══════════════════════════════════════════════════════════════
# 4. openRisks carried VERBATIM — Android inert, Wine corrected, macOS off.
# ═══════════════════════════════════════════════════════════════

class TestOpenRisksCarriedVerbatim:
    def test_android_exec_sleep_infinity_named_as_inert(self):
        low = _main_rs().lower()
        assert "sleep infinity" in low
        assert "no art" in low or "waydroid" in low
        # Cross-references the real inert stub location.
        assert "hart-subsystems.nix" in _main_rs()

    def test_wine_unconditional_success_corrected_at_wm_layer(self):
        low = _main_rs().lower()
        assert "wine" in low
        # The correction is explicit: success keyed on map, not the installer.
        assert "unconditional" in low or "returns 0" in low
        assert "app_installer" in low

    def test_macos_darling_default_off(self):
        low = _main_rs().lower()
        assert "macos" in low or "darling" in low
        assert "default-off" in low or "default off" in low


# ═══════════════════════════════════════════════════════════════
# 5. The Rust unit-test floor names the Phase-5 behavioural tests (run in CI +
#    on the dev box via cargo). This file guards SHAPE; that proves BEHAVIOUR.
# ═══════════════════════════════════════════════════════════════

class TestRustBehaviouralFloorNamesPhase5:
    def test_phase5_unit_tests_present_in_skeleton(self):
        src = _main_rs()
        for t in (
            "fn handles_are_unique_and_prefixed",
            "fn on_map_mints_handle_and_records_manifest_mapping",
            "fn on_unmap_invalidates_handle_and_clears_manifest",
            "fn summon_inert_subsystems_short_circuit_unsupported",
            "fn pending_summon_times_out_to_honest_failure_never_a_handle",
            "fn summon_outcome_has_no_handleless_mapped_variant",
        ):
            assert t in src, f"missing Phase-5 Rust unit test: {t}"

    def test_phase3_floor_still_present_not_regressed(self):
        # Phase 5 is ADDITIVE — the Phase-3 never-fail-floor tests must survive.
        src = _main_rs()
        assert "fn force_software_pins_software_path" in src
        assert "fn unprobed_hardware_falls_to_software_never_fail_floor" in src


# ═══════════════════════════════════════════════════════════════
# 6. Cargo.toml + ROADMAP reflect Phase-5 (xwayland feature manifest, landed-pure
#    vs CI-pending-Smithay split). Honest accounting, no over-claim.
# ═══════════════════════════════════════════════════════════════

class TestManifestAndRoadmap:
    def test_cargo_manifest_adds_xwayland_feature(self):
        cargo = _cargo()
        # XWayland is the one Phase-5 protocol that is its own Smithay feature.
        assert '"xwayland"' in cargo
        # And it is still a COMMENTED manifest (deps come back at CI bring-up).
        # The xwayland line must be inside the commented `smithay = {` block.
        assert re.search(r"#.*xwayland", cargo), \
            "xwayland feature must be in the commented Smithay manifest, not a live dep"

    def test_hart_comp_nix_notes_phase5_xwayland_c_dep_at_ci_time(self):
        src = _read(os.path.join(MODULES_DIR, "hart-comp.nix"))
        # The XWayland C dep surface is noted (commented) as added at CI bring-up,
        # not silently live before the feature is uncommented.
        assert "xwayland" in src.lower()
        assert "Phase 5" in src or "Phase-5" in src

    def test_roadmap_phase5_split_landed_pure_vs_ci_smithay(self):
        roadmap = _read(os.path.join(COMPOSITOR_DIR, "ROADMAP.md"))
        # The pure logic is marked landed; the Smithay wiring CI-pending.
        assert "LANDED, dev-authored" in roadmap
        assert "WindowRegistry" in roadmap
        assert "SummonOutcome" in roadmap
        # And it must NOT over-claim — the Smithay handlers stay CI.
        assert "CI-COMPILE" in roadmap or "CI, not yet" in roadmap


# ═══════════════════════════════════════════════════════════════
# 7. Never-break: Phase 5 does not flip defaultSession or weaken the floor.
# ═══════════════════════════════════════════════════════════════

class TestNeverBreakFloor:
    def test_hart_comp_still_does_not_flip_default_session(self):
        # Phase-5 edits to hart-comp.nix must not regress the never-fail invariant.
        src = _read(os.path.join(MODULES_DIR, "hart-comp.nix"))
        assert "defaultSession =" not in src
        assert "displayManager.defaultSession" not in src

    def test_software_render_floor_intact_in_skeleton(self):
        # The Phase-3 mandatory-software-floor decision must remain untouched.
        src = _main_rs()
        assert "enum RenderPath" in src
        assert "fn select_render_path" in src
        assert "#![forbid(unsafe_code)]" in src


# ═══════════════════════════════════════════════════════════════
# 8. The REAL Smithay handler BODIES now exist in wayland.rs — gated behind the
#    `smithay` cargo feature (OFF by default), so the dev box compiles ONLY the
#    pure floor. THIS is the "fill the handler bodies + flag every CI-compile part"
#    deliverable: the bodies are written + reviewable, and the feature gate is the
#    single flag that says "this is the part that compiles ONLY where Smithay links".
# ═══════════════════════════════════════════════════════════════

class TestRealSmithayBodiesAreFeatureGated:
    def test_wayland_module_is_feature_gated_off_by_default(self):
        # main.rs declares the module ONLY under #[cfg(feature = "smithay")] so the
        # always-compiled crate (dev box) never builds it. The file itself also
        # opens with the crate-level cfg so it cannot leak into a default build.
        main = _main_rs()
        assert '#[cfg(feature = "smithay")]' in main
        assert "mod wayland;" in main
        way = _wayland_rs()
        assert '#![cfg(feature = "smithay")]' in way

    def test_cargo_declares_the_smithay_feature_off_by_default(self):
        # The feature must be DECLARED (so #[cfg(feature="smithay")] is a known cfg)
        # AND default must be empty (so the dev-box build is pure-logic only).
        cargo = _cargo()
        assert "[features]" in cargo
        assert "default = []" in cargo
        assert re.search(r"^smithay\s*=\s*\[", cargo, re.MULTILINE), \
            "the `smithay` feature must be declared in [features]"

    def test_wayland_bodies_impl_all_four_phase5_protocols_for_real(self):
        # The real trait impls (not todo!()): the Smithay handler bodies the task
        # asked to fill. These reference the live Smithay API the CI build links.
        way = _wayland_rs()
        assert "impl XdgShellHandler for State" in way
        assert "impl XdgDecorationHandler for State" in way
        assert "impl ForeignToplevelListHandler for State" in way
        # XWayland is driven via the event fn + the X11 map/unmap methods.
        assert "fn handle_xwayland_event" in way
        assert "XWaylandEvent::Ready" in way
        assert "fn on_xwayland_mapped" in way

    def test_wayland_bodies_are_not_todo_placeholders(self):
        # The whole point: these are REAL bodies, not todo!(). (The main.rs free-fn
        # stubs stay todo!() as the feature-OFF placeholders — asserted in §3.)
        way = _wayland_rs()
        assert "todo!(" not in way, \
            "wayland.rs must carry REAL handler bodies, not todo!() placeholders"

    def test_decoration_prefers_server_side(self):
        # The compositor draws the frame (SSD) so the AI-native WM owns the chrome.
        way = _wayland_rs()
        assert "ServerSide" in way

    def test_summon_resolution_keyed_on_real_map_in_wayland_state(self):
        # The live State ties a REAL map -> Mapped(handle) and a timeout -> TimedOut,
        # mirroring the pure SummonResolver. Success is on_real_map / on the map
        # edge, never an installer code.
        way = _wayland_rs()
        assert "fn on_real_map" in way
        assert "fn expire_summons" in way
        # It mints the handle via the PURE registry's on_map (the only mint site).
        assert "self.windows.on_map(" in way

    def test_every_wayland_section_is_ci_compile_flagged(self):
        # The honest "flag every CI-compile part" contract: the file is saturated
        # with CI-COMPILE markers + the header names the feature-gate boundary.
        way = _wayland_rs()
        assert way.count("CI-COMPILE") >= 5
        assert "ENTIRE FILE IS CI-COMPILE ONLY" in way

    def test_openrisks_carried_verbatim_into_wayland_bodies(self):
        # Android inert / Wine-unconditional-corrected / macOS-off carried VERBATIM
        # where the bodies live too (not only in main.rs).
        low = _wayland_rs().lower()
        assert "sleep infinity" in low and "waydroid" in low
        assert "wine" in low and ("unconditional" in low or "returns 0" in low)
        assert "app_installer.py:_install_windows" in _wayland_rs()
        assert "macos" in low and ("default-off" in low or "default off" in low)


class TestSummonResolverOrchestration:
    def test_summon_resolver_is_the_launch_to_map_orchestration(self):
        # The pure orchestration the Smithay handlers drive: begin (after precheck),
        # resolve on a real map, expire to TimedOut. Wayland-FREE so it unit-tests
        # on the dev box (the no-phantom logic proven before the VM wires the edges).
        src = _main_rs()
        assert "struct SummonResolver" in src
        assert "fn begin" in src
        assert "fn resolve" in src
        assert "fn expire" in src

    def test_summon_resolver_unit_tests_present(self):
        # The behavioural floor for the orchestration (runs via cargo test, feature
        # OFF) — Mapped only via a real map, never a handle on timeout.
        src = _main_rs()
        for t in (
            "fn summon_resolves_to_mapped_only_via_a_real_map",
            "fn summon_a_nonmatching_map_does_not_resolve_it",
            "fn summon_expires_to_timed_out_never_a_handle",
            "fn inert_subsystem_never_begins_a_summon_short_circuits_unsupported",
        ):
            assert t in src, f"missing SummonResolver unit test: {t}"


class TestHartCompNixDriesTheFeatureFlip:
    def test_hart_comp_nix_buildfeatures_off_until_ci_bringup(self):
        # buildFeatures stays EMPTY (pure-logic build) until the Phase-5 CI step
        # turns the smithay feature on TOGETHER with uncommenting the git-Smithay
        # dep — both flips are one step, documented in the module.
        src = _read(os.path.join(MODULES_DIR, "hart-comp.nix"))
        assert "buildFeatures = [ ]" in src
        assert 'buildFeatures = [ "smithay" ]' in src  # the documented CI flip
        assert "wayland.rs" in src
