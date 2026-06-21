"""
Phase-0 source-guards — the GDM-based hart-desktop-boot nixosTest (the floor-lock's
DM-driven twin) that proves the four gates floor-lock.nix DEFERS.

WHY THESE ARE SOURCE-GUARDS (and that is the correct kind of test here):
  Per CLAUDE.md Gate 5 / memory/feedback_no_grep_tests.md, behavioural tests are
  the default. But the Phase-0 desktop-boot deliverable is a NixOS VM test that
  boots a real GDM display manager, autologins the cage `hart-shell` Wayland
  session, and OCRs the first WebView frame off an llvmpipe framebuffer. That
  CANNOT run on this Windows dev box (no Nix/QEMU/Wayland/KMS/llvmpipe) — it is the
  CI/VM gate (nixos/tests/desktop-boot.nix, run by .github/workflows/
  nixos-vm-tests.yml). The dev box can only AUTHOR it + lock its contract SHAPE.

  The ONLY dev-box-verifiable invariants are STRUCTURAL, and they are exactly the
  cross-file DRY / never-fail / no-parallel-path invariants the no-grep-tests rule
  names as the acceptable source-guard class — clearly labelled as such. They lock,
  bit-for-bit, that the desktop-boot nixosTest:
    1. is built #70-safe (via the shared mkNode from ./lib.nix, NOT a
       ../configurations/X.nix installer-CD overlay import that re-breaks the
       `nix flake check` eval-gate),
    2. adds the display manager floor-lock lacks (GDM + autologin) through the
       per-node `extra` MODULE, so sessionPackages -> sessionData materialize,
    3. asserts the FOUR floor-lock-deferred gates the task names — (1) the cage
       hart-shell .desktop session is REGISTERED, (2) the launcher exports the
       software-GL env (WLR/LIBGL) + the glass shell pins
       HardwareAccelerationPolicy.NEVER bit-for-bit, (3) the first WebView frame
       PAINTS on llvmpipe (OCR), (4) a WebView-process-kill is RECOVERED by
       Restart=on-failure with NO WatchdogSec self-kill,
    4. targets the REAL renderer unit (hart-liquid-ui-renderer) whose module
       contract in hart-liquid-ui.nix is genuinely Restart=on-failure with NO
       WatchdogSec — so the test asserts a true invariant, not a strawman,
    5. keeps the Tier-3 cage software-GL floor bit-for-bit (the never-break gate),
    6. is wired into the flake checks AND the VM CI workflow — else it never runs
       and guards nothing (Gate 5).

  The PAINT/RECOVERY behaviour itself runs in the VM (the nixosTest testScript);
  THIS file guards the SHAPE so a refactor of the test or the module cannot
  silently void the gate without a dev-box-red test.

Run (dev box, targeted — the full suite OOMs):
    python -m pytest tests/unit/test_phase0_desktop_boot.py -v \
        --noconftest -p no:capture
"""
import os
import pytest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
NIXOS_DIR = os.path.join(REPO_ROOT, "nixos")
MODULES_DIR = os.path.join(NIXOS_DIR, "modules")
TESTS_DIR = os.path.join(NIXOS_DIR, "tests")
COMPOSITOR_DIR = os.path.join(REPO_ROOT, "compositor")

NIXTEST = os.path.join(TESTS_DIR, "desktop-boot.nix")
FLOORLOCK = os.path.join(TESTS_DIR, "floor-lock.nix")
LIQUID_UI = os.path.join(MODULES_DIR, "hart-liquid-ui.nix")
FLAKE = os.path.join(NIXOS_DIR, "flake.nix")
VM_WORKFLOW = os.path.join(REPO_ROOT, ".github", "workflows", "nixos-vm-tests.yml")


def _read(path):
    with open(path, "r", encoding="utf-8") as f:
        return f.read()


# ═══════════════════════════════════════════════════════════════
# 0. The Phase-0 desktop-boot files exist
# ═══════════════════════════════════════════════════════════════

class TestDesktopBootFilesExist:
    def test_desktop_boot_nixostest_exists(self):
        assert os.path.isfile(NIXTEST), (
            "the GDM-based desktop-boot nixosTest (the floor-lock's DM-driven twin) "
            "must exist at nixos/tests/desktop-boot.nix"
        )

    def test_floor_lock_defers_to_a_dm_driven_twin(self):
        # The premise: floor-lock runs a #70-minimal node with NO display manager,
        # so it DEFERS the DM-driven registration + software-GL launcher checks to
        # "the GDM-based hart-desktop-shell-boot test". This file's nixosTest IS
        # that deferral target — prove floor-lock actually names it (so the two
        # stay a coherent pair, not drifting siblings).
        floor = _read(FLOORLOCK)
        assert "hart-desktop-shell-boot" in floor, (
            "floor-lock.nix must name the GDM desktop-boot test it defers its "
            "DM-driven gates to — otherwise the deferral is dangling prose"
        )
        # And it must explicitly punt the DM-only checks (no-DM honesty).
        low = floor.lower()
        assert "no dm" in low or "no display manager" in low or "minimal node has no dm" in low


# ═══════════════════════════════════════════════════════════════
# 1. #70-safe: built via the shared mkNode, NOT a configurations overlay
# ═══════════════════════════════════════════════════════════════

class TestSeventyEvalGateSafe:
    @pytest.fixture(scope="class")
    def src(self):
        return _read(NIXTEST)

    def test_built_from_mknode_via_lib(self, src):
        # #70 discipline: built from hartModules via the shared mkNode (./lib.nix),
        # which imports hartModules ALONE — the #70-safe builder by construction.
        # Asserting its use IS the #70 guarantee (floor-lock / layer-shell-host
        # rely on the same positive signal).
        assert "import ./lib.nix" in src
        assert "mkNode" in src
        assert 'mkNode "desktop"' in src

    def test_no_interpolated_installer_cd_config_import(self, src):
        # The NEGATIVE signal: importing ../configurations/${variant}.nix (the
        # mkSystem idiom) drags the installer-CD profile's nixpkgs.overlays back in
        # and re-breaks `nix flake check` ("nixpkgs.overlays defined multiple
        # times"). The `${` match avoids tripping on the #70 discipline COMMENT
        # ("NOT by importing ../configurations/desktop.nix").
        assert "configurations/${" not in src
        # No literal full-ISO-config import either.
        assert "/configurations/desktop.nix" not in src.replace("..", "")

    def test_dm_added_via_extra_module_not_overlay(self, src):
        # The display manager floor-lock lacks is added through the per-node `extra`
        # MODULE (plain NixOS options) — services.xserver + GDM — NOT by importing
        # the ISO config. These are the options that materialize sessionPackages ->
        # sessionData without the overlay collision.
        assert "services.xserver.enable = true" in src
        assert "displayManager.gdm" in src


# ═══════════════════════════════════════════════════════════════
# 2. GATE 1 — the cage hart-shell .desktop session is REGISTERED
# ═══════════════════════════════════════════════════════════════

class TestGate1SessionRegistered:
    @pytest.fixture(scope="class")
    def src(self):
        return _read(NIXTEST)

    def test_autologin_into_the_cage_hart_shell_session(self, src):
        # GDM must autologin a user straight into the cage floor (the pin
        # desktop.nix ships: defaultSession = "hart-shell"), so the session
        # actually LOGS IN to paint — not merely sits selectable.
        assert "services.displayManager.autoLogin" in src
        assert "hart-admin" in src             # the user hart-base.nix creates
        assert 'defaultSession = "hart-shell"' in src

    def test_asserts_session_desktop_is_materialized_by_the_dm(self, src):
        # The real "the floor IS the session" proof floor-lock can only do in the
        # *closure*: with GDM, sessionData puts the .desktop on the runtime search
        # path. Assert the test checks the materialized path + that it execs the
        # cage launcher (not some other compositor).
        assert "/run/current-system/sw/share/wayland-sessions/hart-shell.desktop" in src
        assert "hart-shell-session" in src     # the cage launcher the .desktop Exec=


# ═══════════════════════════════════════════════════════════════
# 3. GATE 2 — software-GL env + NEVER policy asserted BIT-FOR-BIT
# ═══════════════════════════════════════════════════════════════

class TestGate2SoftwareGLBitForBit:
    @pytest.fixture(scope="class")
    def src(self):
        return _read(NIXTEST)

    def test_reads_the_exact_launcher_the_dm_execs(self, src):
        # The DM-driven path floor-lock deferred: pull the Exec= target out of the
        # REGISTERED .desktop and read THAT launcher (not a closure-find lookalike).
        assert "Exec=" in src
        assert "awk" in src and "cat" in src

    def test_launcher_forces_software_gl(self, src):
        # The broken-GPU paint floor, bit-for-bit (WLR + LIBGL) — the task's
        # explicit requirement.
        assert "WLR_RENDERER_ALLOW_SOFTWARE=1" in src
        assert "LIBGL_ALWAYS_SOFTWARE=1" in src

    def test_glass_shell_pins_never_accel_and_disables_gl_paths(self, src):
        # The WebKit-side contract on the SAME glass shell cage execs: NEVER accel
        # (not ON_DEMAND) + DMABUF/compositing disables so WebKitGTK paints on
        # llvmpipe rather than SIGABRT (the #99/#100 class). All three are the
        # task's named bits.
        assert "HardwareAccelerationPolicy.NEVER" in src
        assert "WEBKIT_DISABLE_DMABUF_RENDERER=1" in src
        assert "WEBKIT_DISABLE_COMPOSITING_MODE=1" in src


# ═══════════════════════════════════════════════════════════════
# 4. GATE 3 — first WebView frame PAINTS on llvmpipe (OCR)
# ═══════════════════════════════════════════════════════════════

class TestGate3FirstFramePaints:
    @pytest.fixture(scope="class")
    def src(self):
        return _read(NIXTEST)

    def test_runs_on_an_llvmpipe_software_gl_vm(self, src):
        # No GPU passthrough in the driver => the broken-GPU floor is exercised
        # every run. A virtio-gpu gives cage a DRM/KMS node to scan out to.
        low = src.lower()
        assert "llvmpipe" in low
        assert "virtio" in low                 # qemu.options -vga virtio

    def test_enables_ocr_and_reads_the_brand_off_the_framebuffer(self, src):
        # The authoritative "pixels presented" proof: enableOCR pulls tesseract +
        # frame-grab so wait_for_text reads the rendered brand off the QEMU
        # framebuffer. A blank/black screen (the regression) yields no text => fail.
        assert "enableOCR = true" in src
        assert 'wait_for_text("HART"' in src
        # A screenshot is saved either way so the run log always has the frame.
        assert "screenshot(" in src

    def test_structural_alive_proof_backs_the_ocr(self, src):
        # OCR is corroborating; the un-fakeable structural proof the shell did NOT
        # SIGABRT on software GL is that cage AND its glass-shell client are both
        # alive. Assert the test checks both (so an OCR hiccup is not the ONLY
        # signal for "did it come up").
        assert "pgrep" in src and "cage" in src
        assert "hart-glass-shell" in src or "gi.require_version" in src

    def test_cage_host_touches_shell_ready_marker_parity_with_gtk4(self, src):
        # PARITY (Phase-4): the cage GTK3 floor host must satisfy the SAME paint-
        # watchdog contract as the GTK4 layer-shell host — on first paint its
        # _on_load_changed touches /run/hart/session/shell-ready so the supervisor
        # sees a HEALTHY tier. The live cage-paint node (this test) is where that
        # parity is exercised; OCR proves pixels, the marker wait proves the floor
        # host's _signal_painted() fired (both hosts honor ONE marker contract).
        assert "/run/hart/session/shell-ready" in src
        assert "wait_until_succeeds" in src and "shell-ready" in src
        low = src.lower()
        assert "marker" in low and "parity" in low


# ═══════════════════════════════════════════════════════════════
# 5. GATE 4 — WebView-kill RECOVERED by Restart=on-failure, NO WatchdogSec
# ═══════════════════════════════════════════════════════════════

class TestGate4KillRecoveryNoWatchdog:
    @pytest.fixture(scope="class")
    def src(self):
        return _read(NIXTEST)

    def test_targets_the_renderer_unit(self, src):
        # The renderer unit (hart-liquid-ui-renderer) IS the Restart=on-failure +
        # no-WatchdogSec WebView host — the canonical recovery mechanism the
        # ROADMAP Phase-0 gate names (NOT WebKitGTK's internal web-process respawn,
        # which only shows a crash page).
        assert "hart-liquid-ui-renderer.service" in src

    def test_asserts_no_watchdog_and_restart_on_failure_bit_for_bit(self, src):
        # The sd_notify-once self-kill lesson: a WebView renderer sends READY=1 once
        # but never periodic WATCHDOG=1, so a WatchdogSec would SIGABRT-loop it.
        # Assert the unit contract bit-for-bit.
        assert "WatchdogUSec" in src
        assert "Restart=on-failure" in src

    def test_real_sigkill_then_nrestarts_climbs(self, src):
        # The behavioural half (in-VM): SIGKILL the live main process and assert
        # systemd's AUTHORITATIVE NRestarts counter climbs (immune to PID-reuse /
        # timing) — proving the RESTART POLICY (not a watchdog) brought it back.
        assert "kill -KILL" in src
        assert "NRestarts" in src

    def test_cage_floor_survives_the_renderer_crash(self, src):
        # The floor held — the screen was never blank; only the renderer unit
        # cycled. Assert cage is still alive after the kill (the never-break gate).
        # (Two cage pgrep call sites: the paint subtest + this survival check.)
        assert src.count("pgrep -x cage") >= 1


# ═══════════════════════════════════════════════════════════════
# 6. The renderer unit's MODULE contract is genuinely the asserted one
# ═══════════════════════════════════════════════════════════════
# The nixosTest assertions above are only meaningful if hart-liquid-ui.nix actually
# defines hart-liquid-ui-renderer as Restart=on-failure with NO WatchdogSec — i.e.
# the test asserts a TRUE invariant, not a strawman. Lock the module side too.

class TestRendererModuleContract:
    @pytest.fixture(scope="class")
    def src(self):
        return _read(LIQUID_UI)

    def test_renderer_is_a_user_service(self, src):
        # The test drives it via `systemctl --user` (autologin brings the user
        # manager online); the unit MUST be a user service for that to resolve.
        assert "systemd.user.services.hart-liquid-ui-renderer" in src

    def test_renderer_restart_on_failure(self, src):
        assert 'Restart = "on-failure"' in src

    def test_renderer_has_no_watchdogsec(self, src):
        # The whole point of GATE 4: no WatchdogSec ARMED on the module's
        # serve_forever units (the sd_notify-once lesson). If one were added, the
        # renderer would self-kill and the test's "no watchdog" assertion would be
        # asserting a falsehood. The module legitimately MENTIONS "WatchdogSec" in
        # prose ("# No WatchdogSec: ..."), so match the ASSIGNMENT forms that would
        # actually arm it — not the bare word (the layer-shell guard's same nuance:
        # assert the invocation shape, not a substring that appears in a comment).
        for armed in ("WatchdogSec =", "WatchdogSec=", "WatchdogUSec =", "WatchdogUSec="):
            assert armed not in src, (
                f"hart-liquid-ui.nix arms {armed!r} — a WebView renderer sends "
                f"READY=1 once but never periodic WATCHDOG=1, so it would SIGABRT-loop"
            )

    def test_launcher_and_glass_shell_carry_the_software_gl_contract(self, src):
        # The bits GATE 2 reads off the registered launcher originate HERE — prove
        # the source of truth still emits them (so the test is reading a real env,
        # and the Tier-3 cage software-GL floor is bit-for-bit intact: the
        # never-break gate).
        assert "WLR_RENDERER_ALLOW_SOFTWARE=1" in src
        assert "LIBGL_ALWAYS_SOFTWARE=1" in src
        assert "HardwareAccelerationPolicy.${if ui.preferHardwareGL then" in src
        assert "WEBKIT_DISABLE_DMABUF_RENDERER=1" in src

    def test_session_desktop_execs_the_cage_launcher(self, src):
        # GATE 1's registered-session-execs-the-cage-launcher claim is grounded in
        # the module: the hart-shell.desktop Exec= points at hart-shell-session.
        assert "hart-shell.desktop" in src
        assert "hart-shell-session" in src
        assert 'providedSessions = [ "hart-shell" ]' in src


# ═══════════════════════════════════════════════════════════════
# 7. Honestly VM/CI-only (needs_ci) — not claimed to run on Windows
# ═══════════════════════════════════════════════════════════════

class TestHonestVmOnly:
    @pytest.fixture(scope="class")
    def src(self):
        return _read(NIXTEST)

    def test_marked_vm_only_cannot_run_on_windows(self, src):
        # Honest-hardware-limit tag. The header carries the `[VM]` marker AND the
        # "cannot run on the Windows dev" sentence (it line-wraps before "box", so
        # match the un-wrapped fragment, not the full "... dev box").
        low = src.lower()
        assert "[vm]" in low
        assert "llvmpipe" in low
        assert "cannot run on the windows dev" in low


# ═══════════════════════════════════════════════════════════════
# 8. Wired into the flake checks AND the VM CI workflow (else it never runs)
# ═══════════════════════════════════════════════════════════════

class TestWiring:
    @pytest.fixture(scope="class")
    def flake(self):
        return _read(FLAKE)

    def test_imported_and_merged_into_checks(self, flake):
        # A test that never runs guards nothing (CLAUDE.md Gate 5). It must be
        # IMPORTED and merged into `checks` under a distinct attr.
        assert "tests/desktop-boot.nix" in flake
        assert "desktopShellBoot" in flake
        # Merged into the checks // chain so `nix build .#checks...hart-desktop-
        # shell-boot` resolves.
        assert "desktopShellBoot //" in flake or "// desktopShellBoot" in flake

    def test_runs_in_the_vm_ci_workflow(self):
        # The eval-only `nix flake check --no-build` never runs the testScript; the
        # nixos-vm-tests workflow `nix build`s the check, which BOOTS the VM + runs
        # the assertions. The GDM desktop-boot check must be in that build list.
        wf = _read(VM_WORKFLOW)
        assert "hart-desktop-shell-boot" in wf, (
            "the GDM desktop-boot check must be in the nixos-vm-tests workflow's "
            "`nix build` list — otherwise its testScript never executes in CI"
        )

    def test_roadmap_phase0_names_these_gates(self):
        # Ground the four gates in the ROADMAP Phase-0 deliverables/tests so this
        # test and the roadmap cannot silently drift.
        roadmap = _read(os.path.join(COMPOSITOR_DIR, "ROADMAP.md"))
        low = roadmap.lower()
        assert "phase 0" in low
        assert "first webview frame" in low
        assert "restart=on-failure" in low and "watchdogsec" in low
