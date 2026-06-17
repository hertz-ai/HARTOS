// ════════════════════════════════════════════════════════════════════════════
// HART-comp — HART OS AI-native Wayland compositor (Smithay) — PHASE-3 SKELETON
// ════════════════════════════════════════════════════════════════════════════
//
// ⚠️  STATUS: COMPILE-PENDING SKELETON — authored on a Windows dev box where no
//     Wayland/KMS/Smithay build can run. NOTHING here is compiled or booted yet.
//     Every paint, DRM scanout, libinput seat, and layer-shell mount is
//     VM-pending (CI nixosTest on an llvmpipe software-GL VM, or local QEMU-KVM).
//     See ../docs/architecture/HART_OS_NATIVE_ARCHITECTURE.md §L1 + ./ROADMAP.md
//     Phase 3, and the honest hardware limit in ROADMAP §"Honest hardware limit".
//
// WHY THIS IS A SKELETON, NOT THE COMPOSITOR:
//   The ROADMAP ordering invariant is sway-as-Tier-1-NOW, Smithay-as-later-moat.
//   OS-native agent windowing ships TODAY via nixos/modules/hart-sway-tier1.nix
//   (sway + swaymsg shim). This file is the eventual first-party compositor that
//   will own the agent-driven window tree + the com.hart.Compositor IPC (the moat
//   GNOME/Copilot cannot match: an AI that owns window-PLACEMENT POLICY). It is
//   intentionally a tinywl-class scaffold so the FIRST Rust-in-Nix build can be
//   bring-up-proven on llvmpipe before any real window management is wired.
//
// WHAT THE SKELETON ESTABLISHES (the three things Phase 3 must prove):
//   1. An event loop (calloop) that comes up without panicking.
//   2. A MANDATORY software-render path (pixman) selected when hardware GL is
//      unavailable or `--force-software` is passed — the broken-GPU never-fail
//      floor, as a first-class type-checked code path, NOT an env-var prayer.
//   3. A KMS solid brand-color clear painted BEFORE the first client frame (no
//      flash-of-black) + a layer-shell client mount point for the glass shell.
//
// EVERY real Smithay handler body below is `todo!()` / a stub returning the
// honest "not wired yet" — so a reviewer can never mistake the scaffold for a
// working compositor, and CI compiles the SHAPE before the behavior lands.

#![forbid(unsafe_code)]

use std::time::Duration;

// NOTE: these `use smithay::...` imports are the SHAPE the real compositor needs.
// They are commented at module scope intentionally — uncommenting + filling the
// handler bodies is the Phase-3 CI bring-up work, done where Smithay can compile.
// Keeping them as a documented manifest (not live imports) means the skeleton's
// `cargo check` surface stays minimal while the intent is explicit.
//
//   use smithay::backend::drm::{DrmDevice, DrmNode};
//   use smithay::backend::renderer::pixman::PixmanRenderer;     // software floor
//   use smithay::backend::libinput::LibinputInputBackend;
//   use smithay::backend::session::libseat::LibSeatSession;
//   use smithay::reexports::calloop::EventLoop;
//   use smithay::reexports::wayland_server::Display;
//   use smithay::wayland::shell::wlr_layer::WlrLayerShellState;  // glass shell mount
//   use smithay::wayland::shell::xdg::XdgShellState;             // native toplevels

/// The brand-color KMS clear painted before the first WebView/client frame, so a
/// slow-booting glass shell never flashes black. Matches the HART OS splash hue
/// used by Plymouth + hartBootSplash.js (kept in sync there; this is the L1 clear
/// the architecture's "solid-color KMS splash before the shell paints" row needs).
const HART_SPLASH_RGBA: [f32; 4] = [0.043, 0.047, 0.063, 1.0]; // ~#0b0c10 deep slate

/// Which render path the compositor will use. The software path is MANDATORY and
/// always available; hardware is an opportunistic upgrade. This mirrors the cage
/// Tier-3 floor contract (WLR_RENDERER_ALLOW_SOFTWARE / LIBGL_ALWAYS_SOFTWARE)
/// but as a typed decision instead of an env var the compositor might ignore.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum RenderPath {
    /// pixman / llvmpipe — paints on ANY GPU (the broken-GPU never-fail floor).
    Software,
    /// GBM/EGL hardware acceleration — used only when probed-good AND not forced off.
    Hardware,
}

/// Boot-time configuration resolved from argv + environment. Kept tiny on purpose.
#[derive(Debug, Clone)]
struct BootConfig {
    /// `--force-software`: hard-pin the pixman path. The Phase-1 tier-drop
    /// supervisor / the hart-comp.nix unit pass this on broken-GPU boxes so a
    /// half-finished hardware path can never brick the box.
    force_software: bool,
}

impl BootConfig {
    fn from_args() -> Self {
        let force_software = std::env::args().any(|a| a == "--force-software")
            // Honor the same env contract the cage floor uses, so operators have
            // one mental model across tiers.
            || std::env::var("WLR_RENDERER_ALLOW_SOFTWARE").is_ok()
            || std::env::var("LIBGL_ALWAYS_SOFTWARE").is_ok()
            || std::env::var("HART_COMP_FORCE_SOFTWARE").is_ok();
        BootConfig { force_software }
    }
}

/// Decide the render path. Software is MANDATORY: if `--force-software` is set, or
/// hardware GL cannot be probed-good, we paint with pixman. A compositor MUST
/// paint on any GPU — correctness/robustness over a few fps (the exact lesson the
/// cage kiosk launcher comment encodes in hart-liquid-ui.nix).
fn select_render_path(cfg: &BootConfig) -> RenderPath {
    if cfg.force_software {
        tracing::info!("render path: SOFTWARE (forced via --force-software / env)");
        return RenderPath::Software;
    }
    // TODO[phase3-vm]: real GBM/EGL probe behind `smithay::backend::egl`. On the
    // Windows dev box there is no DRM node to probe, so the probe is stubbed to
    // FALSE (fail toward the always-safe software floor) until CI wires the real
    // device walk. NEVER default to hardware on an unprobed box.
    let hardware_gl_probed_good = false; // stub — see TODO above
    if hardware_gl_probed_good {
        tracing::info!("render path: HARDWARE (GBM/EGL probed good)");
        RenderPath::Hardware
    } else {
        tracing::info!("render path: SOFTWARE (hardware GL not probed-good — never-fail floor)");
        RenderPath::Software
    }
}

/// Paint the brand-color clear. In the real compositor this binds the pixman (or
/// EGL) renderer to the DRM framebuffer and clears to HART_SPLASH_RGBA before any
/// client maps, so there is no flash-of-black while the glass shell boots.
///
/// SKELETON: logs intent only. The real body is Phase-3 CI work (needs a DRM
/// framebuffer that does not exist on Windows).
fn paint_splash_clear(path: RenderPath) {
    tracing::info!(
        ?path,
        rgba = ?HART_SPLASH_RGBA,
        "TODO[phase3-vm]: paint KMS solid brand-color clear before first client frame"
    );
    // TODO[phase3-vm]: with RenderPath::Software ->
    //   let mut renderer = PixmanRenderer::new(...)?;  bind fb; clear(HART_SPLASH_RGBA)
    // with RenderPath::Hardware -> EGL/GBM bind + clear. Both MUST succeed before
    // the layer-shell client is allowed to map (no flash-of-black guarantee).
}

/// Mount the glass shell as a `wlr-layer-shell` BACKGROUND surface (exclusive
/// zone 0) — the desktop itself, served unchanged from LiquidUIService :6800
/// render_desktop_shell + /shell/static. Native toplevels (Phase 5) sit ABOVE it;
/// A2UI/orb overlays (Phase 4 z-order decision) sit above those.
///
/// SKELETON: records the mount intent. The real body initializes
/// WlrLayerShellState and waits for the shell's layer surface to map, keying boot
/// health on a REAL /shell/static fetch (dead-husk-aware, the f294f52 lesson),
/// NOT on inline render. That fetch + paint proof is the Phase-3/4 nixosTest gate.
fn mount_glass_shell_layer() {
    tracing::info!(
        "TODO[phase3-vm]: mount glass shell as wlr-layer-shell BACKGROUND surface \
         (exclusive zone 0), boot-health = real /shell/static fetch 200 (not inline render)"
    );
    // TODO[phase3-vm]: WlrLayerShellState::new(&display);
    //   spawn the glass-shell client (same WebKitGTK host the cage floor uses, or
    //   the Phase-4 GTK4 layer-shell host); await its layer-surface map; assert a
    //   real curl/test_client GET /shell/static/* returns 200 within a bounded
    //   window before declaring the compositor healthy.
}

/// The compositor event loop. SKELETON: builds a calloop loop, paints the splash
/// clear, registers the layer-shell mount point, and idles. The real loop drives
/// DRM page-flips, libinput events, the Wayland display dispatch, and the
/// com.hart.Compositor IPC server (Phase 6). Nothing destructive happens here yet.
fn run_event_loop(cfg: &BootConfig) -> Result<(), Box<dyn std::error::Error>> {
    let path = select_render_path(cfg);

    // 1. Paint the brand clear BEFORE anything else (no flash-of-black).
    paint_splash_clear(path);

    // 2. Mount the glass shell layer-shell surface (the desktop).
    mount_glass_shell_layer();

    // 3. TODO[phase3-vm]: real calloop event loop.
    //    let mut event_loop: EventLoop<State> = EventLoop::try_new()?;
    //    insert DRM device source, libinput source, wayland display source,
    //    com.hart.Compositor IPC socket source (Phase 6).
    //    event_loop.run(None, &mut state, |_| { /* dispatch */ })?;
    //
    // SKELETON stand-in: the loop is not started (there is no Wayland socket on
    // Windows). We log readiness and return so `cargo check` proves the SHAPE.
    tracing::info!(
        ?path,
        "HART-comp skeleton initialized (event loop NOT started — compile-pending, VM-only)"
    );

    // A real run would block here. The skeleton returns immediately; the integer
    // below documents the intended idle tick the real loop uses for housekeeping.
    let _idle_tick = Duration::from_millis(16); // ~60fps housekeeping cadence
    Ok(())
}

fn main() {
    tracing_subscriber::fmt()
        .with_env_filter(
            tracing_subscriber::EnvFilter::try_from_default_env()
                .unwrap_or_else(|_| "info".into()),
        )
        .init();

    tracing::warn!(
        "HART-comp is a COMPILE-PENDING Phase-3 skeleton. OS-native windowing ships \
         TODAY via sway Tier-1 (hart-sway-tier1.nix). This binary is opt-in only; \
         defaultSession stays cage until the software-render path is VM-proven."
    );

    let cfg = BootConfig::from_args();
    if let Err(e) = run_event_loop(&cfg) {
        tracing::error!(error = %e, "HART-comp skeleton failed to initialize");
        std::process::exit(1);
    }
}

// ────────────────────────────────────────────────────────────────────────────
// Skeleton-only tests: prove the PURE decision logic (render-path selection +
// boot-config parsing) without any Wayland/DRM dependency, so even the skeleton
// has a behavioural unit floor that CI can run the moment Smithay compiles.
// These assert the never-fail invariant: an unprobed/forced box ALWAYS lands on
// the software path. Real paint/scanout proof remains VM-only.
// ────────────────────────────────────────────────────────────────────────────
#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn force_software_pins_software_path() {
        let cfg = BootConfig { force_software: true };
        assert_eq!(select_render_path(&cfg), RenderPath::Software);
    }

    #[test]
    fn unprobed_hardware_falls_to_software_never_fail_floor() {
        // With the hardware probe stubbed FALSE (the safe Windows-dev default),
        // even a non-forced boot MUST select the software floor — never a blank
        // screen waiting on an unproven hardware path.
        let cfg = BootConfig { force_software: false };
        assert_eq!(select_render_path(&cfg), RenderPath::Software);
    }

    #[test]
    fn splash_clear_is_opaque_brand_color() {
        // Alpha MUST be 1.0 (opaque) so the pre-frame clear actually hides black.
        assert_eq!(HART_SPLASH_RGBA[3], 1.0);
    }
}
