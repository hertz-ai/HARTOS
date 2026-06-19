"""
Phase-3 foundations source-guards — sway-Tier-1-now + first Rust-in-Nix precedent
+ HART-comp Smithay skeleton + opt-in hart-comp.nix session.

WHY THESE ARE SOURCE-GUARDS (and that is the correct kind of test here):
  Per CLAUDE.md Gate 5 / memory/feedback_no_grep_tests.md, behavioural tests are
  the default. But Phase-3 deliverables are Nix expressions + a Rust compositor
  skeleton that CANNOT be compiled or booted on this Windows dev box (no Wayland/
  KMS/Smithay/cargo). The behaviour they encode (paint on llvmpipe, swaymsg
  arrangement, buildRustPackage resolution) is VM/CI-only. The ONLY dev-box-
  verifiable invariants are STRUCTURAL: the never-fail floor is wired, the right
  crate is packaged on the right pin, defaultSession is NOT flipped, the skeleton
  is honestly compile-pending-marked, and there is no parallel renderer. These are
  exactly the cross-file DRY/structural invariants the no-grep-tests rule names as
  the acceptable source-guard class — and they are clearly labelled as such.

  The behavioural floor that DOES run lives in the Rust skeleton itself
  (compositor/src/main.rs `#[cfg(test)] mod tests`: force-software pins software,
  unprobed hardware falls to software, splash alpha is opaque) — those execute in
  CI once Smithay compiles. This file guards the SHAPE on the dev box.

Run:
  pytest tests/unit/test_phase3_compositor_foundations.py -v --noconftest -p no:capture
"""

import os
import pytest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
NIXOS_DIR = os.path.join(REPO_ROOT, "nixos")
MODULES_DIR = os.path.join(NIXOS_DIR, "modules")
COMPOSITOR_DIR = os.path.join(REPO_ROOT, "compositor")


def _read(path):
    with open(path, "r", encoding="utf-8") as f:
        return f.read()


# ═══════════════════════════════════════════════════════════════
# 0. All four Phase-3 files exist
# ═══════════════════════════════════════════════════════════════

class TestPhase3FilesExist:
    def test_sway_tier1_module_exists(self):
        assert os.path.isfile(os.path.join(MODULES_DIR, "hart-sway-tier1.nix"))

    def test_rust_precedent_module_exists(self):
        assert os.path.isfile(os.path.join(MODULES_DIR, "hart-rust-precedent.nix"))

    def test_hart_comp_module_exists(self):
        assert os.path.isfile(os.path.join(MODULES_DIR, "hart-comp.nix"))

    def test_compositor_crate_files_exist(self):
        assert os.path.isfile(os.path.join(COMPOSITOR_DIR, "Cargo.toml"))
        assert os.path.isfile(os.path.join(COMPOSITOR_DIR, "src", "main.rs"))

    def test_existing_claw_crate_is_the_precedent_target(self):
        # The precedent must package an EXISTING crate; that crate + its committed
        # lock must really be present (the whole point of "prove on a crate we have").
        assert os.path.isfile(
            os.path.join(REPO_ROOT, "claw_native", "rust", "Cargo.toml")
        )
        assert os.path.isfile(
            os.path.join(REPO_ROOT, "claw_native", "rust", "Cargo.lock")
        )


# ═══════════════════════════════════════════════════════════════
# 1. sway-Tier-1: OS-native windowing NOW, reuses the canonical shell,
#    swaymsg shim present, opt-in, does NOT flip defaultSession
# ═══════════════════════════════════════════════════════════════

class TestSwayTier1:
    @pytest.fixture(scope="class")
    def src(self):
        return _read(os.path.join(MODULES_DIR, "hart-sway-tier1.nix"))

    def test_has_enable_option_and_mkif_guard(self, src):
        assert "swayTier1" in src
        assert "mkEnableOption" in src
        assert "mkIf sway.enable" in src

    def test_runs_sway_kiosk(self, src):
        assert "pkgs.sway" in src
        assert "hart-sway-session" in src

    def test_reuses_canonical_glass_shell_no_parallel_renderer(self, src):
        # DRY gate: sway Tier-1 must REUSE hart-glass-shell, not fork a renderer.
        assert "hart-glass-shell" in src
        # And it must assert LiquidUI/webkit so the canonical renderer is present.
        assert "rustPrecedent" not in src  # wrong module — sanity
        assert "renderer" in src and "webkit" in src

    def test_swaymsg_agent_windowing_shim_present(self, src):
        # The Tier-2 moat: agent tile/summon/focus/move via swaymsg.
        assert "hart-swaymsg-shim" in src
        assert "swaymsg" in src
        for verb in ("list", "focus", "close", "tile", "move_to_workspace", "switch_workspace"):
            assert verb in src, f"swaymsg shim missing verb mapping: {verb}"

    def test_forces_software_gl_paint_on_any_gpu(self, src):
        # Same never-fail software-GL contract as the cage floor.
        assert "WLR_RENDERER_ALLOW_SOFTWARE=1" in src
        assert "LIBGL_ALWAYS_SOFTWARE" in src

    def test_registers_selectable_session_but_does_not_flip_default(self, src):
        assert 'providedSessions = [ "hart-sway" ]' in src
        assert "services.displayManager.sessionPackages" in src
        # CRITICAL never-fail invariant: this module must NEVER ASSIGN defaultSession
        # (prose mentioning "defaultSession stays cage" is fine; an assignment is not).
        assert "defaultSession =" not in src
        assert "displayManager.defaultSession" not in src

    def test_gate_is_brain_side_not_in_shim(self, src):
        # The shim is transport-only; the fail-closed gate stays brain-side.
        assert "TRANSPORT only" in src or "transport-only" in src.lower()

    def test_marked_vm_pending_not_booted_on_windows(self, src):
        low = src.lower()
        assert "vm" in low and ("pending" in low or "ci-pending" in low)
        assert "windows dev box" in low


# ═══════════════════════════════════════════════════════════════
# 2. Rust-in-Nix precedent: existing crate, pinned, isolated gate
# ═══════════════════════════════════════════════════════════════

class TestRustPrecedent:
    @pytest.fixture(scope="class")
    def src(self):
        return _read(os.path.join(MODULES_DIR, "hart-rust-precedent.nix"))

    def test_uses_buildrustpackage(self, src):
        assert "buildRustPackage" in src
        assert "rustPlatform" in src

    def test_packages_the_existing_claw_crate(self, src):
        assert "claw_native/rust" in src

    def test_uses_committed_lockfile(self, src):
        # The crate ships a Cargo.lock — lockFile is the DRY/reproducible path.
        assert "cargoLock" in src
        assert "lockFile" in src

    def test_documents_the_fixed_cargohash_placeholder_alternative(self, src):
        # The ROADMAP "fixed cargoHash placeholder" recipe must be documented so
        # hart-comp.nix (no committed lock) has the pattern.
        assert "cargoHash" in src
        assert "placeholder" in src.lower()

    def test_first_rust_in_nix_rationale_recorded(self, src):
        low = src.lower()
        assert "first" in low and "rust" in low

    def test_opt_in_default_off(self, src):
        assert "mkEnableOption" in src
        # Exposes a read-only package for CI/manifest reach.
        assert "readOnly = true" in src
        assert "rustPrecedent.package" in src

    def test_no_second_nixpkgs_or_rust_overlay(self, src):
        # The whole point is the STOCK pinned toolchain — no rust-overlay/fenix
        # INPUT or overlay usage. (Prose saying "NO rust-overlay" is the intent,
        # not a usage.) Assert neither is imported/applied as an overlay/input.
        assert "rust-overlay.overlays" not in src
        assert "inputs.rust-overlay" not in src
        assert "rustChannelOf" not in src  # rust-overlay's signature fn
        assert "fenix.packages" not in src
        # And the toolchain comes from the stock pkgs.rustPlatform.
        assert "pkgs.rustPlatform.buildRustPackage" in src

    def test_marked_not_built_on_windows(self, src):
        low = src.lower()
        assert "windows dev box" in low
        assert "vm" in low and ("pending" in low or "ci" in low)


# ═══════════════════════════════════════════════════════════════
# 3. HART-comp module: buildRustPackage of compositor/, opt-in,
#    defaultSession stays cage, structural dep on the precedent
# ═══════════════════════════════════════════════════════════════

class TestHartComp:
    @pytest.fixture(scope="class")
    def src(self):
        return _read(os.path.join(MODULES_DIR, "hart-comp.nix"))

    def test_buildrustpackage_of_compositor_dir(self, src):
        assert "buildRustPackage" in src
        assert 'hartSrc + "/compositor"' in src

    def test_uses_committed_cargo_lock(self, src):
        # compositor/ now ships a committed Cargo.lock, so hart-comp uses the
        # reproducible cargoLock.lockFile path (mirrors hart-rust-precedent), NOT
        # the all-A cargoHash placeholder that would fail the first `nix build`.
        assert "cargoLock" in src
        assert "lockFile" in src
        assert 'compositorSrc + "/Cargo.lock"' in src
        assert "sha256-AAAA" not in src  # the broken placeholder is gone

    def test_mandatory_software_render_floor(self, src):
        assert "pixman" in src  # the mandatory software renderer C dep
        assert "--force-software" in src
        assert "WLR_RENDERER_ALLOW_SOFTWARE=1" in src

    def test_opt_in_and_default_session_stays_cage(self, src):
        assert "mkEnableOption" in src
        assert "mkIf comp.enable" in src
        # CRITICAL never-fail invariant: HART-comp must NOT ASSIGN defaultSession
        # (prose "defaultSession STAYS cage" is fine; an assignment is not).
        assert "defaultSession =" not in src
        assert "displayManager.defaultSession" not in src
        # It registers an opt-in session instead.
        assert 'providedSessions = [ "hart-comp" ]' in src

    def test_structural_dependency_on_the_precedent(self, src):
        # HART-comp is the FIRST Rust-in-Nix build; it must assert the precedent.
        assert "rustPrecedent.enable" in src
        assert "assertion" in src

    def test_reuses_canonical_renderer_no_third_renderer(self, src):
        assert "glass-shell" in src.lower()
        assert "renderer" in src and "webkit" in src

    def test_exposes_package_for_node_integrity_manifest(self, src):
        # The security task hashes this binary into the signed manifest.
        assert "hart.comp.package" in src
        assert "readOnly = true" in src

    def test_dbus_name_reserved_gate_is_brain_side(self, src):
        assert "com.hart.Compositor" in src
        # The gate is brain-side, not in the dbus policy.
        assert "brain-side" in src.lower() or "BRAIN-SIDE" in src

    def test_marked_compile_pending_not_built_on_windows(self, src):
        low = src.lower()
        assert "windows dev box" in low
        assert "compile-pending" in low or "skeleton" in low


# ═══════════════════════════════════════════════════════════════
# 4. compositor/ Smithay skeleton — honest, compile-pending, software floor
# ═══════════════════════════════════════════════════════════════

class TestCompositorSkeleton:
    @pytest.fixture(scope="class")
    def cargo(self):
        return _read(os.path.join(COMPOSITOR_DIR, "Cargo.toml"))

    @pytest.fixture(scope="class")
    def main_rs(self):
        return _read(os.path.join(COMPOSITOR_DIR, "src", "main.rs"))

    def test_cargo_declares_smithay_and_software_renderer(self, cargo):
        assert "smithay" in cargo
        assert "renderer_pixman" in cargo  # the mandatory software path feature

    def test_cargo_pins_edition_and_denies_unsafe(self, cargo):
        assert 'edition = "2021"' in cargo
        # M6 narrowed `forbid` → `deny` for ONE audited screencopy memcpy exception
        # (src/screencopy.rs::fill_one writes the framebuffer into the client's shm
        # wl_buffer via a `*mut u8` the rev gives no safe helper for). `deny` keeps
        # every OTHER line unsafe-free (a stray unsafe block is still a hard error);
        # the DRM path (wayland.rs/udev.rs) + all pure logic remain unsafe-free.
        assert 'unsafe_code = "deny"' in cargo

    def test_main_denies_unsafe(self, main_rs):
        # See test_cargo_pins_edition_and_denies_unsafe: deny (not forbid) for the one
        # audited screencopy memcpy exception; every other line stays unsafe-free.
        assert "#![deny(unsafe_code)]" in main_rs

    def test_software_path_is_first_class_decision_not_env_prayer(self, main_rs):
        # The never-fail floor is a typed decision (RenderPath enum), not just env.
        assert "enum RenderPath" in main_rs
        assert "Software" in main_rs
        assert "fn select_render_path" in main_rs

    def test_unprobed_or_forced_always_lands_on_software(self, main_rs):
        # The behavioural floor that the Rust unit tests assert in CI.
        assert "force_software" in main_rs
        assert "never-fail floor" in main_rs.lower() or "never_fail" in main_rs.lower()

    def test_splash_clear_before_first_frame(self, main_rs):
        assert "HART_SPLASH_RGBA" in main_rs
        assert "paint_splash_clear" in main_rs
        # Alpha opaque so the clear actually hides black.
        assert "1.0]" in main_rs

    def test_layer_shell_mount_point_present(self, main_rs):
        assert "mount_glass_shell_layer" in main_rs
        assert "layer-shell" in main_rs.lower()
        # Boot health keyed on a REAL /shell/static fetch (the f294f52 lesson).
        assert "/shell/static" in main_rs

    def test_honestly_marked_compile_pending_and_todo(self, main_rs):
        low = main_rs.lower()
        assert "compile-pending" in low or "compile_pending" in low
        assert "todo" in low  # real handler bodies are todo!()/stub
        # The event loop is explicitly NOT started on the skeleton.
        assert "not started" in low or "NOT started" in main_rs

    def test_has_rust_unit_test_floor(self, main_rs):
        # The skeleton carries pure-logic behavioural tests that run in CI.
        assert "#[cfg(test)]" in main_rs
        assert "force_software_pins_software_path" in main_rs
        assert "unprobed_hardware_falls_to_software" in main_rs


# ═══════════════════════════════════════════════════════════════
# 5. Cross-file ordering invariant: sway is the NOW path, Smithay the LATER moat;
#    NONE of the three new modules flip defaultSession (the floor holds)
# ═══════════════════════════════════════════════════════════════

class TestNeverFailOrderingInvariant:
    def test_no_new_module_flips_default_session(self):
        for name in ("hart-sway-tier1.nix", "hart-comp.nix", "hart-rust-precedent.nix"):
            src = _read(os.path.join(MODULES_DIR, name))
            # Assert no ASSIGNMENT of defaultSession (prose may reference it).
            assert "defaultSession =" not in src, (
                f"{name} must NOT assign defaultSession — cage stays the floor until a "
                f"higher tier is VM-proven (ROADMAP §6 never-fail invariant)."
            )
            assert "displayManager.defaultSession" not in src, (
                f"{name} must NOT touch displayManager.defaultSession."
            )

    def test_roadmap_reflects_sway_tier1_now(self):
        roadmap = _read(os.path.join(COMPOSITOR_DIR, "ROADMAP.md"))
        low = roadmap.lower()
        assert "sway-tier-1-now" in low or "sway as tier-1 now" in low or "sway as tier-1" in low
        assert "hart-sway-tier1.nix" in roadmap
        assert "hart-rust-precedent.nix" in roadmap
        assert "hart-comp.nix" in roadmap
        # And it records that defaultSession stays cage.
        assert "defaultSession stays cage" in roadmap or "stays cage" in roadmap
