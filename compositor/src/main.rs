// ════════════════════════════════════════════════════════════════════════════
// HART-comp — HART OS AI-native Wayland compositor (Smithay) — REAL DRM/PIXMAN
//                                                            + PHASE-5 WINDOWS
// ════════════════════════════════════════════════════════════════════════════
//
// ⚠️  STATUS: REAL Smithay compositor — BUILDS IN NIX/CI (M9 green) + VM-PROVEN.
//     The smithay DRM/pixman software-scanout path (src/wayland.rs + src/udev.rs,
//     `--features smithay`) is a real ~8400-line compositor that COMPILES under
//     `nix build .#…hart.comp.package` and whose pixman KMS scanout is VM-PROVEN
//     (a real virgl-QEMU scanout PNG exists — M9 + the integration milestone).
//     Real-HARDWARE paint on the target GPU is STILL BEING VERIFIED (the flash's
//     job — the dev box has no DRM device). What remains a true placeholder is the
//     feature-OFF pure-logic fallback in THIS file (`main.rs` with neither `winit`
//     nor `smithay` on): its Smithay handler shims are `todo!()` and its event
//     loop is NOT started — that fallback is all the Windows dev box compiles, by
//     design, so the never-fail render-path + no-phantom-window invariants get a
//     behavioural unit floor even where Smithay cannot link.
//     See ../docs/architecture/HART_OS_NATIVE_ARCHITECTURE.md §L1 + §5.4 +
//     ./ROADMAP.md Phase 3 + Phase 5, and the honest hardware limit in
//     ROADMAP §"Honest hardware limit".
//
// ── WHAT COMPILES HERE TODAY vs WHAT IS CI-ONLY (read before editing) ──
//   COMPILES + UNIT-TESTED on the dev box (PURE LOGIC, no Wayland/Smithay):
//     • the render-path / boot-config never-fail-floor decision (Phase 3), AND
//     • the Phase-5 WINDOW BOOKKEEPING: handle minting (`WindowHandle`), the
//       manifest↔toplevel map (`WindowRegistry` — the "AppRegistry window-handle
//       field" the ROADMAP Phase-5 deliverable names), and the no-phantom-window
//       `SummonApp` state machine (`PendingSummon`/`SummonOutcome`) that keys
//       success on a REAL map event within a timeout — NEVER on an installer exit
//       code (IPC_PROTOCOL.md §1.4/§4.6, architecture §5.4).
//   CI-ONLY — must be compiled where Smithay links (every fn marked
//   `// ⚠️ CI-COMPILE` below): the xdg-shell / XWayland / xdg-decoration /
//     wlr-foreign-toplevel-management Smithay handler bodies. Their Smithay calls
//     are `todo!()`/stub; the PURE registry mutation they drive is the real code
//     above, so the map→handle / no-map→no-handle invariant is exercised by unit
//     tests TODAY and the Smithay wiring is the only VM bring-up left.
//
// WHAT THIS main.rs IS (vs the real Smithay handler bodies):
//   The REAL compositor lives in src/wayland.rs (DRM, `--features smithay`),
//   src/udev.rs (KMS scanout + libinput seat), src/winit.rs (the WSL/WSLg dev
//   backend, `--features winit`), and the backend-AGNOSTIC brain in
//   src/comp_core.rs + src/ipc.rs + src/screencopy.rs. Those BUILD: M9 is green
//   (`nix build .#…hart.comp.package` with `buildFeatures = ["smithay"]`), and the
//   pixman DRM scanout is VM-PROVEN (virgl-QEMU PNG). The ROADMAP ordering
//   invariant is sway-as-Tier-1-NOW, Smithay-HART-comp-as-the-deeper-moat: OS-
//   native agent windowing also ships TODAY via nixos/modules/hart-sway-tier1.nix
//   (sway + swaymsg shim — the brain-side HartWmClient already drives it,
//   integrations/agent_engine/hart_wm_client.py, LIVE-verified in WSL sway).
//   This file's job in the REAL build is the boot entrypoint + the typed never-
//   fail render-path decision + the Phase-5 window bookkeeping (handle minting,
//   the manifest↔toplevel map, the no-phantom-window SummonApp state machine) that
//   the real backends drive.
//
// THE ONE PART THAT IS STILL A PLACEHOLDER is the feature-OFF fallback compiled on
// the Windows dev box (neither `winit` nor `smithay`): there the Smithay handler
// shims below are `todo!()` and the event loop is NOT started — they are the honest
// "not wired in the feature-off floor", kept so a reader of the always-compiled
// dev-box crate is never misled, and so the pure-logic invariants have a unit floor
// even where Smithay cannot link. With a feature ON, the real bodies in
// wayland.rs/udev.rs/winit.rs replace them.

// `deny` (not `forbid`) for ONE audited exception: M6's zwlr_screencopy read-back must
// memcpy the framebuffer into the client's shm `wl_buffer`, whose mapping Smithay hands
// over as a raw `*mut u8` (`with_buffer_contents_mut`) with no safe write helper at this
// pinned rev. That single bounds-checked copy carries a scoped `#[allow(unsafe_code)]` +
// SAFETY comment in src/screencopy.rs::fill_one; `deny` keeps every OTHER line in the
// crate unsafe-free (a stray unsafe block anywhere else is still a hard error). See the
// matching note in Cargo.toml [lints.rust]. The DRM path + all pure logic stay
// unsafe-free.
#![deny(unsafe_code)]

use std::collections::HashMap;
use std::sync::atomic::{AtomicU64, Ordering};
use std::time::{Duration, Instant};

// ── Backend-AGNOSTIC Smithay glue shared by BOTH backends (winit + DRM/udev) ──
// The surface-tree / map-edge / app-id readers that touch neither the renderer
// concretely nor the backend transport live in `shared.rs`, gated to
// `any(feature="winit", feature="smithay")` so ONE implementation feeds both
// backends (M7 Stage-B hoist; the DRY gate — no parallel helper path). Off on the
// default dev-box build (neither feature on). See src/shared.rs.
#[cfg(any(feature = "winit", feature = "smithay"))]
mod shared;

// ── Milestone 8 (Stage B): the SHARED compositor brain — the AI-native WM IPC verbs,
// the input router, the workspace machinery, the keyboard-shortcut actions, the
// software cursor + screen kill-switch + fade effects, and the render-element z-order,
// ALL hoisted out of winit.rs and made generic over the backend (the `CompState`
// trait). ONE implementation feeds BOTH the winit (dev/WSL) and DRM (real-HW)
// backends — no parallel WM path. Gated to `any(winit, smithay)` (off on the default
// dev-box build). See src/comp_core.rs for the full rationale + the seat-handle
// ownership constraint that made a TRAIT (not a shared struct field) the only path. ──
#[cfg(any(feature = "winit", feature = "smithay"))]
mod comp_core;

// ── NATIVE SHELL PARITY PROGRAM, M1: the aura bloom drawn by the COMPOSITOR ──
// The desktop backdrop used to be a flat HART_SPLASH_RGBA clear with the real
// aurora painted by a browser inside a WebView. `bloom.rs` composes that same
// field natively (parity source: the ONE conky-themes palette the HTML shell
// reads, so there is no second theme table). It is pure CPU math with no smithay
// types, and `comp_core` owns the caching + element assembly — so this is gated
// exactly like `comp_core`, the only thing that consumes it. See src/bloom.rs
// for the compose-once performance contract.
//
// NOTE this module existed since 2026-07-20 with NO `mod` declaration, so it was
// never compiled and the desktop kept clearing to flat black. Declaring it here
// is what makes M1 real; do not drop this line.
#[cfg(any(feature = "winit", feature = "smithay"))]
mod bloom;

// ── NATIVE SHELL PARITY PROGRAM, M2: the voice orb, drawn by the COMPOSITOR ──
// The orb breathes continuously, so in the HTML shell it forces the browser to
// rasterise forever: measured 2026-08-28, WebKitWebProcess burning a full core
// with ZERO ioctls and only 0.64s of 6s in syscalls, i.e. ~5.4s of pure
// userspace pixel work that never reached the GPU. hart-comp DOES hold a live
// GLES context on this hardware, so the orb belongs here. Same gate and same
// shape as `bloom` above: pure CPU compose, cached, assembled by comp_core.
// See src/orb.rs for the compose-once-per-phase-step contract.
#[cfg(any(feature = "winit", feature = "smithay"))]
mod orb;

// ── NATIVE SHELL PARITY PROGRAM, M3: the SceneNode foundation (top bar + hero +
// rows + taskbar as a scene tree, text via glyph atlas). M0 named "land the
// SceneNode enum + A2UI->Scene decoder" but never did, so latency.rs still reads
// "there is no native scene graph". src/scene.rs is PURE geometry + the home_compose
// decoder + the a2 layout, with its unit floor; comp_core lowers a SceneNode tree to
// HartRenderElements on the render path. Same gate as bloom/orb (its only consumer is
// comp_core), so the smithay doCheck exercises the layout + decode tests. ──
#[cfg(any(feature = "winit", feature = "smithay"))]
mod scene;

// ── Phase-5 Smithay handler BODIES (CI-COMPILE only) ──
// The real xdg-shell / XWayland / xdg-decoration / wlr-foreign-toplevel-management
// handler bodies live in `wayland.rs`, gated behind the `smithay` cargo feature
// (OFF by default — see Cargo.toml `[features]`). On the Windows dev box the
// feature is never on, so that module is NOT compiled and the pure-logic floor +
// `#[cfg(test)]` below stay green. CI's Phase-5 bring-up turns the feature on
// where Smithay links. The `todo!()` shims further below stay in `main.rs` as the
// feature-OFF placeholders (so a reader of the always-compiled crate still sees
// the honest "not wired here" — and the source-guard asserts they remain so).
#[cfg(feature = "smithay")]
mod wayland;

// ── Milestone 7: the REAL-HARDWARE DRM/udev backend (KMS scanout + libinput seat),
// the software-floor twin of `winit`. Gated behind the `smithay` cargo feature (the
// DRM stack). It reuses the SAME `wayland.rs` State + handlers + the `shared.rs`
// helpers; the only backend-specific parts are the DRM/GBM/libinput wiring + the
// PixmanRenderer scanout. `--backend drm` routes here. See src/udev.rs.
#[cfg(feature = "smithay")]
mod udev;

// ── Milestone 1: the REAL running compositor (winit backend, WSL/WSLg) ──
// Gated behind the DISTINCT `winit` cargo feature (parallel to the DRM `wayland`
// module, NOT to the always-compiled pure-logic floor). On the Windows dev box neither
// `winit` nor `smithay` is on, so this module is NOT compiled and the pure-logic
// floor + `#[cfg(test)]` below stay green. `cargo build --features winit` (in WSL,
// nested in WSLg) compiles + RUNS it. See src/winit.rs for the full rationale.
#[cfg(feature = "winit")]
mod winit;

// ── Milestone 4 (+ M8): the com.hart.Compositor IPC server (Unix-socket twin) — the
// framed-JSON transport that lets an agent arrange REAL native windows. M8 made it
// GENERIC over the backend (via `comp_core::CompState`), so it is gated to
// `any(winit, smithay)`: BOTH the winit (dev/WSL) and the DRM (real-HW) backends serve
// the SAME socket surface (the moat on real hardware too). The verb BODIES live in
// `comp_core`; this is the transport + dispatch. Off on the default dev-box build.
// See src/ipc.rs.
// Input-to-photon instrument (LATENCY_HARNESS.md M0). NO cfg gate on purpose:
// the core is pure (no Smithay types, no clock reads — timestamps are
// arguments), so its unit tests run in BOTH CI legs, including the default
// no-feature build. Only the WIRING lives in udev.rs/comp_core.rs behind
// their existing gates.
mod latency;

#[cfg(any(feature = "winit", feature = "smithay"))]
mod ipc;

// ── Milestone 6 (headline): zwlr_screencopy_v1 served against HART-comp's OWN
// output framebuffer, so `grim` captures HART-comp DIRECTLY (not the sway host
// re-composite). Gated behind the SAME `winit` feature as the live compositor whose
// framebuffer it reads back. Off on the default dev-box build. See src/screencopy.rs
// (the cursor / animations / screen kill-switch all land in winit.rs and are PROVEN
// through this capture path — see the module header for the ordering rationale).
#[cfg(feature = "winit")]
mod screencopy;

// NOTE: these `use smithay::...` imports are the SHAPE the real compositor needs.
// They are commented at module scope intentionally — uncommenting + filling the
// handler bodies is the Phase-3/Phase-5 CI bring-up work, done where Smithay can
// compile. Keeping them as a documented manifest (not live imports) means the
// feature-OFF `cargo check` surface stays minimal while the intent is explicit.
//
//   // ── Phase 3: backend + render + shell mount ──
//   use smithay::backend::drm::{DrmDevice, DrmNode};
//   use smithay::backend::renderer::pixman::PixmanRenderer;     // software floor
//   use smithay::backend::libinput::LibinputInputBackend;
//   use smithay::backend::session::libseat::LibSeatSession;
//   use smithay::reexports::calloop::EventLoop;
//   use smithay::reexports::wayland_server::Display;
//   use smithay::wayland::shell::wlr_layer::WlrLayerShellState;  // glass shell mount
//
//   // ── Phase 5: native toplevels (xdg-shell + XWayland + decoration + foreign) ──
//   use smithay::wayland::shell::xdg::{XdgShellState, ToplevelSurface};   // native toplevels
//   use smithay::xwayland::{XWayland, XWaylandEvent, X11Surface};         // Wine/X11 apps
//   use smithay::wayland::shell::xdg::decoration::XdgDecorationState;     // SSD/CSD policy
//   use smithay::wayland::foreign_toplevel_list::ForeignToplevelListState; // wlr-foreign-toplevel-management
//   use smithay::desktop::{Space, Window};                                // window tree
//
// The corresponding Smithay HANDLERS the real compositor must impl (each a
// `// ⚠️ CI-COMPILE` stub below): `XdgShellHandler` (new_toplevel / map / unmap /
// destroyed), the XWayland `XWaylandEvent::{Ready, Exited}` + `X11Surface` map,
// `XdgDecorationHandler` (request_mode → prefer SSD so the compositor draws the
// frame), and `ForeignToplevelListHandler` (advertise/withdraw each toplevel to
// the `com.hart.Compositor` `window.list` consumers).

/// The brand-color KMS clear painted before the first WebView/client frame, so a
/// slow-booting glass shell never flashes black. Matches the HART OS splash hue
/// used by Plymouth + hartBootSplash.js (kept in sync there; this is the L1 clear
/// the architecture's "solid-color KMS splash before the shell paints" row needs).
const HART_SPLASH_RGBA: [f32; 4] = [0.043, 0.047, 0.063, 1.0]; // ~#0b0c10 deep slate

/// How long `SummonApp` waits for a launched app to actually MAP a toplevel before
/// it reports an honest `timeout` (no handle). The brain's `HartWmClient`/IPC
/// caller (IPC_PROTOCOL.md §4.6) treats the absence of a map within this window as
/// a real failure it can react to — NOT a phantom success. 10s covers Wine/Flatpak
/// cold-start; tune on the VM. The exact value is policy, the never-phantom
/// invariant is not.
#[allow(dead_code)]
const SUMMON_MAP_TIMEOUT: Duration = Duration::from_secs(10);

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

/// Which backend the compositor drives. `Winit` nests in an existing Wayland host
/// (WSLg's wayland-0) for dev/WSL — runnable with no DRM/KMS. `Drm` is the real
/// hardware path (src/wayland.rs). The `--backend` arg selects it; the cargo
/// feature decides which is even compiled in.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum Backend {
    /// winit/wayland-client nested backend (dev/WSL). Default when the `winit`
    /// feature is built.
    Winit,
    /// DRM/KMS real-hardware backend (src/wayland.rs).
    Drm,
}

/// Boot-time configuration resolved from argv + environment. Kept tiny on purpose.
#[derive(Debug, Clone)]
struct BootConfig {
    /// `--force-software`: hard-pin the pixman path. The Phase-1 tier-drop
    /// supervisor / the hart-comp.nix unit pass this on broken-GPU boxes so a
    /// half-finished hardware path can never brick the box.
    force_software: bool,
    /// `--prefer-hardware` / `HART_COMP_PREFER_HARDWARE`: the OPERATOR OVERRIDE that
    /// forces the GLES hardware path WITHOUT re-reading the boot GPU-probe verdict.
    /// The hart-comp.nix launcher's `preferHardwareGL` arm sets this so the compositor
    /// HONORS the operator's force-hardware intent (before this field the launcher
    /// only dropped the `--force-software` pin, but `select_render_path` still re-read
    /// `/run/hart/gpu-render` and stayed on pixman when the probe fail-safed to
    /// `software` — the override never actually reached GLES). `force_software` ALWAYS
    /// wins over this (the never-fail floor is not overridable), and a GLES init/runtime
    /// fault still degrades to the pixman renderer of record (degrade-not-die), so
    /// forcing hardware can upgrade the path but can never brick the box.
    prefer_hardware: bool,
    /// `--backend winit|drm`. Defaults to `Winit` (the runnable dev/WSL path) so
    /// `cargo run --features winit` Just Works under WSLg; a real-HW deploy passes
    /// `--backend drm` (and is built with `--features smithay`).
    #[allow(dead_code)] // read only on the winit/drm feature builds, not the floor
    backend: Backend,
}

impl BootConfig {
    fn from_args() -> Self {
        let force_software = std::env::args().any(|a| a == "--force-software")
            // Honor the same env contract the cage floor uses, so operators have
            // one mental model across tiers.
            || std::env::var("WLR_RENDERER_ALLOW_SOFTWARE").is_ok()
            || std::env::var("LIBGL_ALWAYS_SOFTWARE").is_ok()
            || std::env::var("HART_COMP_FORCE_SOFTWARE").is_ok();

        // The operator force-hardware override (hart.liquidUI.preferHardwareGL): the
        // launcher passes `--prefer-hardware` / exports `HART_COMP_PREFER_HARDWARE=1`
        // when it armed the hardware path, so `select_render_path` upgrades to GLES on
        // the launcher's verdict instead of independently re-reading the probe file (a
        // second, drift-prone read of the SAME decision). `force_software` still wins.
        let prefer_hardware = std::env::args().any(|a| a == "--prefer-hardware")
            || std::env::var("HART_COMP_PREFER_HARDWARE").is_ok();

        // `--backend winit|drm` (default winit). Parsed positionally: the token
        // AFTER `--backend` is the value.
        let mut backend = Backend::Winit;
        let args: Vec<String> = std::env::args().collect();
        if let Some(i) = args.iter().position(|a| a == "--backend") {
            match args.get(i + 1).map(String::as_str) {
                Some("drm") => backend = Backend::Drm,
                Some("winit") => backend = Backend::Winit,
                Some(other) => {
                    tracing::warn!(backend = other, "unknown --backend; defaulting to winit");
                }
                None => tracing::warn!("--backend given without a value; defaulting to winit"),
            }
        }

        BootConfig {
            force_software,
            prefer_hardware,
            backend,
        }
    }
}

/// The boot-time GPU smoke-test's verdict file (hart-gpu-probe.nix runs `eglinfo`
/// before greetd and writes `hardware` when a usable GL context was proven, else the
/// fail-safe `software`). The DRM/udev backend reads this to decide whether to bring up
/// the GLES GPU renderer (PART 3 of the GPU lever) or stay on the pixman software floor.
const GPU_VERDICT_PATH: &str = "/run/hart/gpu-render";
/// The one verdict token that upgrades to the hardware (GLES) render path.
const GPU_VERDICT_HARDWARE: &str = "hardware";

/// PURE: does the GPU probe verdict authorise the hardware (GLES) render path? `true`
/// ONLY when the file read back exactly `hardware` (after trimming surrounding
/// whitespace/newline). Any other value — `software`, an empty/garbled file, or `None`
/// (the probe file is absent, e.g. the Windows dev box / an unprobed boot) — stays on
/// the never-fail software floor. Split out of `select_render_path` so the verdict→path
/// decision is unit-tested on the dev box with no `/run/hart` present.
fn gpu_verdict_is_hardware(verdict: Option<&str>) -> bool {
    verdict.map(str::trim) == Some(GPU_VERDICT_HARDWARE)
}

/// Decide the render path. Software is MANDATORY: if `--force-software` is set, or the
/// boot-time GPU probe did not prove a usable GL context, we paint with pixman. A
/// compositor MUST paint on any GPU — correctness/robustness over a few fps (the exact
/// lesson the cage kiosk launcher comment encodes in hart-liquid-ui.nix). When the path
/// IS `Hardware`, the DRM backend (src/udev.rs) brings up a `GlesRenderer` on the primary
/// node and GPU-composites, keeping the PixmanRenderer as the fallback on ANY GLES fault.
fn select_render_path(cfg: &BootConfig) -> RenderPath {
    if cfg.force_software {
        tracing::info!("render path: SOFTWARE (forced via --force-software / env)");
        return RenderPath::Software;
    }
    // The operator force-hardware override (preferHardwareGL). This is the SECOND check
    // on purpose: the never-fail software floor (`force_software`) is not overridable, but
    // once the floor is not forced, an explicit operator arm upgrades to GLES WITHOUT
    // re-reading the probe file — closing the gap where the launcher armed hardware yet the
    // compositor stayed on pixman because the probe had fail-safed to `software`. A GLES
    // init/runtime fault still falls back to the pixman renderer of record (udev.rs), so
    // this can raise the path but never brick the box.
    if cfg.prefer_hardware {
        tracing::info!("render path: HARDWARE (operator override — --prefer-hardware / HART_COMP_PREFER_HARDWARE)");
        return RenderPath::Hardware;
    }
    // Read the boot-time GPU smoke-test verdict (hart-gpu-probe). The probe is the
    // device walk that used to be a TODO here: it fail-safes to `software` and the file
    // is simply absent on a box that never ran it (the Windows dev box, an unprobed
    // boot) — both of which `gpu_verdict_is_hardware` maps to the software floor. We
    // NEVER default to hardware on an unprobed box.
    let verdict = std::fs::read_to_string(GPU_VERDICT_PATH).ok();
    if gpu_verdict_is_hardware(verdict.as_deref()) {
        tracing::info!(path = %GPU_VERDICT_PATH, "render path: HARDWARE (GPU probe verdict = hardware)");
        RenderPath::Hardware
    } else {
        tracing::info!(
            path = %GPU_VERDICT_PATH,
            ?verdict,
            "render path: SOFTWARE (GPU probe not 'hardware' — never-fail floor)"
        );
        RenderPath::Software
    }
}

// ════════════════════════════════════════════════════════════════════════════
// #131 — FIRST-SCANOUT beacon (kill the "black-but-healthy" Tier-1)
// ════════════════════════════════════════════════════════════════════════════
//
// The session supervisor's paint watchdog reads HEALTHY off the shell-ready marker,
// which the glass-shell WebView host touches when IT renders its first frame. But the
// WebView renders CLIENT-SIDE into its own wl_buffer and fires shell-ready regardless
// of whether that buffer ever reached the physical display — so a compositor that lost
// DRM master (EACCES page-flips forever) or never completed a page-flip is BLACK yet
// still passes the paint watchdog (the exact "black, SETTLED Tier-1 the paint watchdog
// can't catch" the udev.rs master-recovery comment names).
//
// The first-scanout beacon is the COMPOSITOR-side proof the shell-ready marker cannot
// give: it is written EXACTLY ONCE, on the first REAL page-flip vblank (a frame that
// actually scanned out to the CRTC — a kernel page-flip completion event only fires
// after a real scanout). The supervisor's scanout watchdog (fail-safe OFF until armed,
// exactly like the input-alive twin) can then require this marker to declare Tier-1
// truly painted, distinguishing a live desktop from a black one.
//
// The DECISION + PATH + WRITE are split into PURE helpers (unit-tested on the dev box
// with no DRM), mirroring the `master_step`/`flip_action`/`gpu_verdict_is_hardware`
// pure-policy split; only the udev.rs call site (reap_completed_vblanks) is Smithay-
// gated / CI-compiled. All three markers (shell-ready, input-alive, first-scanout) share
// the same /run/hart/session dir + the same "env override, else pinned default" contract.

/// Default path of the first-scanout marker (the compositor-scanout twin of the shell-ready
/// and input-alive markers). Overridable via `HART_SCANOUT_ALIVE_FLAG` so the compositor
/// writer and the supervisor reader share ONE path with no hardcoded divergence.
#[allow(dead_code)] // consumed by the smithay udev backend (reap_completed_vblanks) + tests
const SCANOUT_MARKER_DEFAULT: &str = "/run/hart/session/first-scanout";

/// PURE: resolve where the first-scanout marker is written — the `HART_SCANOUT_ALIVE_FLAG`
/// env override when set + non-empty, else the pinned default. Split out so the path
/// contract is unit-tested on the dev box (mirrors the GPU_VERDICT_PATH read).
#[allow(dead_code)] // consumed by note_first_scanout_once (smithay udev) + tests
fn scanout_marker_path() -> String {
    match std::env::var("HART_SCANOUT_ALIVE_FLAG") {
        Ok(v) if !v.trim().is_empty() => v,
        _ => SCANOUT_MARKER_DEFAULT.to_string(),
    }
}

/// PURE: should the first-scanout marker be emitted on THIS vblank? `true` EXACTLY when
/// the marker has not been emitted yet AND a real scanout (page-flip vblank) just
/// completed. Mirrors `master_step`'s pure-policy split so the one-shot decision is tested
/// with NO DRM hardware.
#[allow(dead_code)] // consumed by note_first_scanout_once (smithay udev) + tests
fn first_scanout_step(already_marked: bool, scanned_out: bool) -> bool {
    !already_marked && scanned_out
}

/// Best-effort write of the first-scanout marker to `path`. Returns `true` iff the write
/// succeeded. NEVER blocks / NEVER aborts: the marker is advisory (a read-only FS or a
/// missing `/run/hart/session` dir just leaves the journal line as the signal), exactly
/// like the input-alive beacon.
#[allow(dead_code)] // consumed by note_first_scanout_once (smithay udev) + tests
fn write_scanout_marker(path: &str) -> bool {
    std::fs::write(path, b"1\n").is_ok()
}

/// Emit the first-scanout beacon EXACTLY ONCE. `latch` is a caller-owned one-shot flag
/// (a module `static AtomicBool` on the udev backend); the first call with `scanned_out`
/// true flips it and writes the marker, every later call is a single atomic load. Passing
/// the latch in (rather than a hidden module static) keeps the "exactly once" behaviour
/// unit-testable on the dev box. Best-effort + non-blocking — the never-fail posture.
#[allow(dead_code)] // called from the smithay udev backend's reap_completed_vblanks
fn note_first_scanout_once(latch: &std::sync::atomic::AtomicBool, scanned_out: bool) {
    // Only a real scanout arms the beacon, and only the first one writes: the pure
    // `first_scanout_step` decides, the atomic swap enforces once-only across ticks.
    if !first_scanout_step(latch.load(Ordering::Relaxed), scanned_out) {
        return;
    }
    if latch.swap(true, Ordering::Relaxed) {
        return; // lost the race to a concurrent caller — someone else already wrote it
    }
    let path = scanout_marker_path();
    if write_scanout_marker(&path) {
        tracing::info!(marker = %path, "hart-comp: first real scanout (page-flip vblank) completed — the physical display is LIVE (#131 first-scanout beacon)");
    } else {
        tracing::info!("hart-comp: first real scanout completed (marker write skipped — advisory only)");
    }
}

// ════════════════════════════════════════════════════════════════════════════
// #137 — FRAME-BUDGET repaint scheduler (on-demand rendering; stop the idle 60Hz burn)
// ════════════════════════════════════════════════════════════════════════════
//
// Through M8 the DRM render loop (udev.rs) rebuilt the element list + ran the pixman
// damage pass + attempted a page-flip on EVERY 16ms tick, even for a perfectly static
// desktop — "the pixman floor has no damage-driven scheduling here; a 60Hz repaint is
// the simple never-fail cadence" (udev.rs run_udev). That is CPU + power the never-fail
// floor does not need to spend: when nothing changed there is nothing to composite and
// nothing to flip. The DrmCompositor already tracks damage at the REGION level inside
// `render_frame` (it returns `is_empty`, and the tick already skips the flip on that) —
// what was missing is the TICK-level gate that avoids rebuilding + re-importing the
// whole element list when the scene is provably idle.
//
// `RepaintScheduler` is that pure, damage-driven decision. It tracks a single `dirty`
// latch the compositor sets on any real change (a client committed a new buffer, an
// input moved the cursor/focus, a window mapped/unmapped, a workspace switched, the
// kill-switch toggled, a session/master edge). The render tick asks `should_paint(...)`;
// when it returns false the tick skips the whole build+render+flip and the loop idles
// until the next real edge — the compositor's OWN on-demand rendering, the architecture
// every mature compositor has.
//
// THE NEVER-FAIL FLOOR IS PRESERVED, two independent ways:
//   1. It starts DIRTY (the first frame ALWAYS paints — the splash / glass shell come
//      up exactly as before), and ANY doubt marks dirty, so the scheduler can only ever
//      skip a provably-idle frame — it can never black or freeze a live scene.
//   2. A HEARTBEAT backstop: even with ZERO observed damage the tick force-repaints at
//      least every `IDLE_HEARTBEAT`, so a MISSED damage edge self-corrects within one
//      heartbeat (a brief stutter — exactly the #186 degrade-not-die posture) rather
//      than wedging the screen. It is impossible for a forgotten `mark_damaged()` to
//      permanently freeze the display.
//
// PURE (no Smithay types) so the decision is unit-tested on the dev box with no DRM,
// mirroring the master_step / flip_action / first_scanout_step split; only the udev.rs
// call sites (the render tick + the damage edges) are Smithay-gated / CI-compiled.

/// The idle-repaint backstop. Even with no observed damage the render tick paints at
/// least this often, so a missed `mark_damaged()` edge self-heals within one interval
/// instead of wedging the screen. 200ms = a 5 Hz idle floor: ~12× fewer idle repaints
/// than the old unconditional 60 Hz burn, yet well under a human-perceptible stall and
/// FAR under the session supervisor's 45s first-paint watchdog. Interactive latency is
/// UNAFFECTED — a real edge marks dirty and paints on the very next 16ms tick.
#[allow(dead_code)] // consumed by the smithay udev render tick (udev.rs) + tests
const IDLE_HEARTBEAT: Duration = Duration::from_millis(200);

/// PURE damage-driven repaint scheduler (#137). See the section header. The udev render
/// tick owns exactly one instance (a field on the DRM `State`); every damage edge calls
/// `mark_damaged`, the tick gates on `should_paint`, and `note_painted` closes the loop.
#[allow(dead_code)] // one instance lives on the smithay DRM State; unit-tested here
#[derive(Debug)]
struct RepaintScheduler {
    /// A real change is pending composite. Starts TRUE (the first frame always paints).
    dirty: bool,
    /// When the last real paint happened, for the idle heartbeat. `None` until the first.
    last_paint: Option<Instant>,
}

#[allow(dead_code)] // methods are called from the smithay udev backend + the tests below
impl RepaintScheduler {
    /// Dirty at boot: the splash + glass-shell first frame must always paint.
    fn new() -> Self {
        Self { dirty: true, last_paint: None }
    }

    /// Mark the scene changed — the next tick will composite. Cheap + idempotent; call
    /// from EVERY damage edge (commit, input, map/unmap, workspace, kill-switch,
    /// session/master). Over-calling only costs one extra repaint; under-calling is
    /// caught by the heartbeat, so this is safe to sprinkle liberally.
    fn mark_damaged(&mut self) {
        self.dirty = true;
    }

    /// PURE: should this tick composite + flip, given whether an effect is mid-animation
    /// and how long since the last real paint? True when damage is pending, an effect is
    /// animating (fades must play frame-by-frame), we have never painted, or the idle
    /// heartbeat elapsed. Split from `should_paint` so the branch is tested with a
    /// synthetic clock (no `Instant::now()` in the test).
    fn wants_paint(&self, effects_animating: bool, since_last_paint: Option<Duration>) -> bool {
        if self.dirty || effects_animating {
            return true;
        }
        match since_last_paint {
            None => true,                    // never painted → must paint
            Some(d) => d >= IDLE_HEARTBEAT,  // idle backstop (self-heals a missed edge)
        }
    }

    /// The live wrapper: derive the elapsed-since-last-paint from `now` and decide.
    /// `saturating_duration_since` clamps a non-monotonic clock hiccup to zero (never a
    /// panic) — the never-fail posture even on the wall-clock edge cases.
    fn should_paint(&self, now: Instant, effects_animating: bool) -> bool {
        let since = self.last_paint.map(|t| now.saturating_duration_since(t));
        self.wants_paint(effects_animating, since)
    }

    /// Call AFTER a tick actually composited. Records the paint time for the heartbeat
    /// and clears the dirty latch — UNLESS an effect is still animating, which must keep
    /// painting next tick (so a fade is never frozen mid-way). Keeping `dirty` in lockstep
    /// with `effects_animating` here is what lets an in-flight crossfade play frame-by-
    /// frame with no separate scheduling, exactly as the old unconditional loop did.
    fn note_painted(&mut self, now: Instant, effects_animating: bool) {
        self.last_paint = Some(now);
        self.dirty = effects_animating;
    }
}

// ════════════════════════════════════════════════════════════════════════════
// PHASE 5 — native window bookkeeping (PURE LOGIC, compiles + unit-tested TODAY)
// ════════════════════════════════════════════════════════════════════════════
//
// This is the part the ROADMAP Phase-5 deliverable calls "AppRegistry gains a
// window-handle field mapping manifest ↔ toplevel" and "SummonApp returns success
// ONLY on a map event within a timeout — NOT on app_installer return-True". It is
// kept Wayland-FREE on purpose: the no-phantom-window correctness lives here, is
// exercised by unit tests on the dev box, and the Smithay handlers below only feed
// it real map/unmap edges. (Same separation the Phase-3 render-path decision uses:
// pure logic the dev box can prove, Smithay glue the VM compiles.)
//
// Each item below is `#[allow(dead_code)]` for the SAME reason as the Smithay
// handler stubs: it is reachable from the `#[cfg(test)]` floor (which proves the
// invariants TODAY) and from the CI-compiled Smithay handlers (`on_map`/`on_unmap`
// callers), but NOT yet from the feature-OFF `main` (its event loop is unstarted). The
// allow is per-item so dead-code detection stays LIVE for the rest of the crate;
// the reads/calls land the moment the Smithay event loop is wired in CI.

/// Which Wayland protocol mapped the toplevel. Drives the honest openRisk surface
/// (a Wine X11 window is XWayland; a Flatpak/PWA is usually Xdg) and lets the
/// `window.list` IPC report provenance. Carried VERBATIM into the IPC `app_id`
/// reasoning the brain does.
#[allow(dead_code)]
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum ToplevelKind {
    /// Native Wayland xdg-shell toplevel (Flatpak / PWA / most modern apps).
    Xdg,
    /// X11 window surfaced through XWayland (Wine apps, legacy X11). The installer
    /// layer returns success unconditionally for Wine; the WM layer corrects that
    /// by only minting a handle when THIS actually maps (openRisk, §below).
    XWayland,
}

/// An opaque, stable per-toplevel handle minted by the compositor on MAP. Matches
/// the IPC contract: `handle` is an opaque string (`win_7f3a`), stable for the
/// toplevel's lifetime, invalid after `window.closed` (IPC_PROTOCOL.md §4). It is
/// minted ONLY on a real map (`WindowRegistry::on_map`), never on a launch attempt
/// — that is the no-phantom-window guarantee made concrete in a type.
#[allow(dead_code)]
#[derive(Debug, Clone, PartialEq, Eq, Hash)]
struct WindowHandle(String);

#[allow(dead_code)]
impl WindowHandle {
    fn as_str(&self) -> &str {
        &self.0
    }
}

/// Monotonic source for handle ids. A real toplevel map increments this and stamps
/// `win_<hex>`; nothing else can mint a handle, so a handle's existence PROVES a
/// toplevel mapped. (A process-global counter is sufficient — handles only need to
/// be unique within one compositor process lifetime.)
#[allow(dead_code)]
static NEXT_HANDLE_ID: AtomicU64 = AtomicU64::new(1);

#[allow(dead_code)]
fn mint_handle() -> WindowHandle {
    let n = NEXT_HANDLE_ID.fetch_add(1, Ordering::Relaxed);
    WindowHandle(format!("win_{n:x}"))
}

/// One mapped toplevel as the compositor + IPC see it. `manifest_id` is `Some`
/// when the window was launched by `SummonApp` via the AppRegistry/AppInstaller,
/// `None` for a window opened outside the brain (IPC_PROTOCOL.md §4.1). This IS the
/// manifest↔toplevel map row. All fields are populated on every map; `kind`/
/// `app_id`/`title` are READ by the CI-only IPC serializer (`on_foreign_toplevel_
/// sync` + the `window.list`/`ListWindows` handler, IPC_PROTOCOL.md §4.1). They are
/// real Phase-5 data, NOT dead code; the read lands with the Smithay wiring.
#[allow(dead_code)]
#[derive(Debug, Clone)]
struct WindowRecord {
    handle: WindowHandle,
    /// AppManifest id (e.g. "blender") when summoned via the brain; None otherwise.
    manifest_id: Option<String>,
    kind: ToplevelKind,
    app_id: Option<String>,
    title: Option<String>,
}

/// The compositor's live window tree as a manifest↔toplevel map — the Rust-side
/// "AppRegistry window-handle field" (ROADMAP Phase 5). It is the single source of
/// truth for `window.list` + for resolving a `SummonApp` to the toplevel that
/// actually mapped. PURE: the Smithay handlers call `on_map`/`on_unmap`; this owns
/// no Wayland state itself, so it is unit-tested without a compositor.
#[allow(dead_code)]
#[derive(Debug, Default)]
struct WindowRegistry {
    /// handle string -> record.
    by_handle: HashMap<String, WindowRecord>,
    /// manifest_id -> handle string. Lets `SummonApp` and the agent reason
    /// "which window is app X". Only present for brain-summoned windows.
    by_manifest: HashMap<String, String>,
}

#[allow(dead_code)]
impl WindowRegistry {
    fn new() -> Self {
        Self::default()
    }

    /// Record a toplevel that JUST MAPPED and mint its handle. This is the ONLY
    /// place a handle comes into existence — called from the xdg-shell / XWayland
    /// map handlers below with a real Smithay surface. `manifest_id` is threaded
    /// from the pending `SummonApp` that launched it (looked up by the launcher),
    /// or `None` for an externally-opened window.
    fn on_map(
        &mut self,
        manifest_id: Option<String>,
        kind: ToplevelKind,
        app_id: Option<String>,
        title: Option<String>,
    ) -> WindowHandle {
        let handle = mint_handle();
        let rec = WindowRecord {
            handle: handle.clone(),
            manifest_id: manifest_id.clone(),
            kind,
            app_id,
            title,
        };
        if let Some(mid) = manifest_id {
            self.by_manifest.insert(mid, handle.0.clone());
        }
        self.by_handle.insert(handle.0.clone(), rec);
        handle
    }

    /// Invalidate a handle when its toplevel is destroyed (`window.closed`). After
    /// this, the handle is invalid per the IPC contract.
    fn on_unmap(&mut self, handle: &WindowHandle) -> bool {
        if let Some(rec) = self.by_handle.remove(handle.as_str()) {
            if let Some(mid) = rec.manifest_id {
                // Only clear the manifest mapping if it still points at THIS handle
                // (a relaunch may have already re-pointed it).
                if self.by_manifest.get(&mid).map(String::as_str) == Some(handle.as_str()) {
                    self.by_manifest.remove(&mid);
                }
            }
            true
        } else {
            false
        }
    }

    /// `window.list` view (IPC_PROTOCOL.md §4.1). A real read off the live map.
    fn list(&self) -> Vec<&WindowRecord> {
        self.by_handle.values().collect()
    }

    /// The record for one handle (the `window.opened`/`window.closed` event
    /// payload). Read by the CI-only Smithay handlers (`wayland.rs`) to fill the
    /// IPC frame from the single source of truth; `None` after `on_unmap`.
    #[cfg_attr(not(feature = "smithay"), allow(dead_code))]
    fn record(&self, handle: &WindowHandle) -> Option<&WindowRecord> {
        self.by_handle.get(handle.as_str())
    }

    /// Resolve which window (if any) a manifest currently maps to — the agent's
    /// "is app X open, and what's its handle" query.
    fn handle_for_manifest(&self, manifest_id: &str) -> Option<&WindowHandle> {
        self.by_manifest
            .get(manifest_id)
            .and_then(|h| self.by_handle.get(h))
            .map(|rec| &rec.handle)
    }
}

/// The terminal outcome of a `SummonApp` (IPC_PROTOCOL.md §4.6). There is NO
/// `Mapped` variant that lacks a handle and NO way to fabricate one — the type
/// makes the no-phantom-window guarantee unrepresentable to violate.
#[allow(dead_code)]
#[derive(Debug, Clone, PartialEq, Eq)]
enum SummonOutcome {
    /// A real toplevel mapped within the timeout; the handle is live in the
    /// `WindowRegistry`.
    Mapped(WindowHandle),
    /// The launcher ran but no toplevel mapped within `SUMMON_MAP_TIMEOUT`. The
    /// brain surfaces this as `error.code = "timeout"` — never a handle.
    /// (Wine returns 0 even when nothing maps; this is where that lie is caught.)
    TimedOut,
    /// The subsystem is inert and CANNOT map a native window at all. Returned for
    /// Android (`exec sleep infinity` — no ART/Waydroid runtime in
    /// hart-subsystems.nix:288) and macOS/Darling (default-off). The brain
    /// surfaces `error.code = "unsupported"`. Carried VERBATIM as an openRisk.
    Unsupported,
}

/// A `SummonApp` in flight: the launcher has been kicked off and we are awaiting a
/// real map event. PURE state machine — the Smithay map handler calls `resolve`
/// when a toplevel for this manifest maps; the event-loop timer calls
/// `poll_timeout`. No Wayland types here, so the timeout/no-phantom logic is
/// unit-tested on the dev box.
#[allow(dead_code)]
#[derive(Debug)]
struct PendingSummon {
    manifest_id: String,
    /// Provenance of the launch — only the toplevel kind we expect can resolve it
    /// (a Wine launch expects an XWayland map). `None` = accept either kind.
    expect_kind: Option<ToplevelKind>,
    started: Instant,
    timeout: Duration,
}

#[allow(dead_code)]
impl PendingSummon {
    fn new(manifest_id: impl Into<String>, expect_kind: Option<ToplevelKind>) -> Self {
        Self::new_at(manifest_id, expect_kind, Instant::now(), SUMMON_MAP_TIMEOUT)
    }

    /// Constructor with explicit clock + timeout so tests can drive deterministic
    /// timeout behavior without sleeping.
    fn new_at(
        manifest_id: impl Into<String>,
        expect_kind: Option<ToplevelKind>,
        started: Instant,
        timeout: Duration,
    ) -> Self {
        PendingSummon {
            manifest_id: manifest_id.into(),
            expect_kind,
            started,
            timeout,
        }
    }

    /// Does a freshly-mapped toplevel satisfy THIS pending summon? Matches on
    /// manifest id (the launcher tags the child) and, if set, the expected kind.
    fn accepts(&self, manifest_id: &str, kind: ToplevelKind) -> bool {
        self.manifest_id == manifest_id && self.expect_kind.map_or(true, |k| k == kind)
    }

    /// Has the map window elapsed as of `now`? When true the summon resolves to
    /// `TimedOut` — an honest failure, NEVER a handle.
    fn is_timed_out_at(&self, now: Instant) -> bool {
        now.duration_since(self.started) >= self.timeout
    }
}

/// Subsystems that CANNOT map a native window today — `SummonApp` short-circuits to
/// `Unsupported` for these BEFORE launching anything, so the agent gets an honest
/// "inert" instead of a launcher that returns 0 and maps nothing. Carried VERBATIM
/// as openRisks (architecture §5.4, §9; hart-subsystems.nix:288 Android stub;
/// app_installer.py:_install_macos Darling default-off).
#[allow(dead_code)]
fn summon_subsystem_is_inert(manifest_platform: &str) -> bool {
    matches!(manifest_platform, "android" | "macos")
}

/// Pure decision the launcher uses: given the target platform, either short-circuit
/// to an honest `Unsupported` OR signal "go ahead and launch, then await a real
/// map" (returns `None` = proceed to await-map; `Some(outcome)` = terminal now).
/// This is where the installer-layer lie ("Wine returned 0 == success") is refused
/// at the WM layer: a Wine/`windows` platform proceeds to AWAIT A MAP, it does not
/// short-circuit to success.
#[allow(dead_code)]
fn summon_precheck(manifest_platform: &str) -> Option<SummonOutcome> {
    if summon_subsystem_is_inert(manifest_platform) {
        return Some(SummonOutcome::Unsupported);
    }
    // windows(Wine) / linux / flatpak / pwa: do NOT trust the installer exit code —
    // proceed to await a real toplevel map (resolved by the Smithay handlers).
    None
}

/// The in-flight `SummonApp` set + the resolution rules — the orchestration that
/// turns a launched app into a terminal `SummonOutcome` keyed on a REAL map event
/// within a timeout. PURE: the Smithay map/timeout edges call `resolve`/`expire`;
/// `wayland.rs`'s `State` holds the SAME `Vec<PendingSummon>` and drives these
/// exact transitions with live surfaces. Kept here (Wayland-FREE) so the
/// no-phantom-window guarantee — Mapped only via a real map, TimedOut never a
/// handle — is unit-tested on the dev box.
#[allow(dead_code)]
#[derive(Debug, Default)]
struct SummonResolver {
    pending: Vec<PendingSummon>,
}

#[allow(dead_code)]
impl SummonResolver {
    fn new() -> Self {
        Self::default()
    }

    /// Begin awaiting a map for `manifest_id` AFTER `summon_precheck` returned
    /// `None` (i.e. the platform can map; inert subsystems never reach here — they
    /// short-circuit to `Unsupported` before any launch). The launcher has been
    /// kicked off; we now wait for a real toplevel.
    fn begin(&mut self, manifest_id: impl Into<String>, expect_kind: Option<ToplevelKind>) {
        self.pending
            .push(PendingSummon::new(manifest_id, expect_kind));
    }

    /// A REAL toplevel mapped. If it satisfies a pending summon, consume that
    /// summon and resolve it to `Mapped(handle)` — the handle was minted by the
    /// caller's `WindowRegistry::on_map` (the ONLY mint site), so a `Mapped` here
    /// always carries proof a toplevel mapped. Returns `None` if this map matched
    /// no pending summon (an externally-opened window — still a real window, just
    /// not one the brain summoned).
    ///
    /// `handle` is threaded in (not minted here) so the type system keeps the
    /// "handle ⇒ a map happened" invariant: this fn cannot fabricate one.
    fn resolve(
        &mut self,
        manifest_id: &str,
        kind: ToplevelKind,
        handle: WindowHandle,
    ) -> Option<SummonOutcome> {
        let idx = self
            .pending
            .iter()
            .position(|p| p.accepts(manifest_id, kind))?;
        self.pending.remove(idx);
        Some(SummonOutcome::Mapped(handle))
    }

    /// Expire every summon whose map window has elapsed as of `now`. Each becomes
    /// an honest `(manifest_id, TimedOut)` the brain surfaces as
    /// `error.code="timeout"` — NEVER a handle. This is the no-phantom-window
    /// guarantee on the failure side: a Wine launch that returned 0 but mapped
    /// nothing times out here instead of fabricating success.
    fn expire(&mut self, now: Instant) -> Vec<(String, SummonOutcome)> {
        let mut out = Vec::new();
        self.pending.retain(|p| {
            if p.is_timed_out_at(now) {
                out.push((p.manifest_id.clone(), SummonOutcome::TimedOut));
                false
            } else {
                true
            }
        });
        out
    }

    fn pending_count(&self) -> usize {
        self.pending.len()
    }
}

// ════════════════════════════════════════════════════════════════════════════
// PHASE 5 — Smithay handler stubs (⚠️ CI-COMPILE — bodies are todo!()/unwired)
// ════════════════════════════════════════════════════════════════════════════
//
// THE REAL BODIES NOW EXIST — in `wayland.rs`, behind `#[cfg(feature = "smithay")]`
// (the `mod wayland;` at the top of this file). That module impls the actual
// `XdgShellHandler` / `XdgDecorationHandler` / `ForeignToplevelListHandler` traits
// + the XWayland lifecycle + the `State::on_real_map`/`expire_summons` summon
// orchestration against the live Smithay API. It is compiled ONLY where Smithay
// links (CI nixosTest llvmpipe VM / QEMU-KVM); the dev box never turns the feature
// on, so the always-compiled crate (this file + its `#[cfg(test)]` floor) stays
// green.
//
// The free functions below remain as the feature-OFF placeholders: in the
// always-compiled crate they are the honest "not wired here" (`todo!()`), and a
// source-guard (tests/unit/test_phase5_native_windows.py) asserts they stay
// CI-COMPILE-marked + `todo!()` so a reader of the default build can never mistake
// the scaffold for a working compositor. Each one's doc says which `wayland.rs`
// impl carries its real body. They are the SHAPE; `wayland.rs` is the behaviour;
// the PURE registry/summon logic they drive is unit-tested above TODAY.

/// ⚠️ CI-COMPILE — `XdgShellHandler::new_toplevel` + map.
/// REAL BODY: `wayland.rs` `impl XdgShellHandler for State` (`new_toplevel` maps
/// the surface into the space + initial configure; `toplevel_mapped` is the real
/// map edge that calls `State::on_real_map(.., ToplevelKind::Xdg)`).
/// When a native xdg-shell toplevel maps, look up any `PendingSummon` whose
/// manifest the launcher tagged onto this client, then
/// `registry.on_map(manifest_id, ToplevelKind::Xdg, app_id, title)` to mint the
/// handle and emit `window.opened` to IPC subscribers. THIS is what resolves a
/// `SummonApp` to `Mapped(handle)` — success keyed on the MAP, never the launcher.
#[allow(dead_code)]
fn on_xdg_toplevel_mapped(_surface: (), _registry: &mut WindowRegistry) -> WindowHandle {
    // ⚠️ CI-COMPILE: real body needs a `smithay::wayland::shell::xdg::ToplevelSurface`.
    //   let app_id = surface.app_id(); let title = surface.title();
    //   let manifest_id = pending.take_matching(app_id, ToplevelKind::Xdg);
    //   let h = registry.on_map(manifest_id, ToplevelKind::Xdg, app_id, title);
    //   emit_event("window.opened", &h); h
    todo!("Phase-5 CI: wire Smithay xdg-shell map -> WindowRegistry::on_map (Xdg)")
}

/// ⚠️ CI-COMPILE — XWayland `X11Surface` map (Wine / legacy X11).
/// REAL BODY: `wayland.rs` `State::on_xwayland_mapped` (+ `handle_xwayland_event`
/// for the `XWaylandEvent::Ready`/`Exited` lifecycle that attaches the X11 WM).
/// Same as the xdg path but `ToplevelKind::XWayland`. This is the corrected Wine
/// path: `app_installer._install_windows` returns `success=True` unconditionally
/// (Wine returns 0 even when nothing maps); the handle is minted HERE, only when a
/// real X11 toplevel actually maps — so an agent never arranges a phantom Wine
/// window. Wine-success-unconditional is carried as an openRisk corrected at THIS
/// (WM) layer.
#[allow(dead_code)]
fn on_xwayland_surface_mapped(_x11_surface: (), _registry: &mut WindowRegistry) -> WindowHandle {
    // ⚠️ CI-COMPILE: real body needs `smithay::xwayland::X11Surface` + the XWayland
    // `XWaylandEvent::Ready` server. Mirror on_xdg_toplevel_mapped with XWayland.
    todo!("Phase-5 CI: wire Smithay XWayland X11 map -> WindowRegistry::on_map (XWayland)")
}

/// ⚠️ CI-COMPILE — toplevel destroyed (both xdg + XWayland).
/// REAL BODY: `wayland.rs` `on_surface_destroyed` (the one destroy path both
/// `XdgShellHandler::toplevel_destroyed` and `State::on_xwayland_unmapped` funnel
/// into).
/// `registry.on_unmap(&handle)` then emit `window.closed`, invalidating the handle.
#[allow(dead_code)]
fn on_toplevel_destroyed(_surface: (), _registry: &mut WindowRegistry, _handle: &WindowHandle) {
    // ⚠️ CI-COMPILE: resolve the destroyed Smithay surface -> its handle, then:
    //   registry.on_unmap(handle); emit_event("window.closed", handle);
    todo!("Phase-5 CI: wire Smithay toplevel destroy -> WindowRegistry::on_unmap")
}

/// ⚠️ CI-COMPILE — `XdgDecorationHandler::request_mode`.
/// REAL BODY: `wayland.rs` `impl XdgDecorationHandler for State`
/// (`new_decoration`/`request_mode`/`unset_mode` — default + prefer ServerSide).
/// HART-comp draws window frames itself (server-side decorations) so the AI-native
/// WM owns the chrome + placement policy uniformly. Reply preferring SSD; fall back
/// to CSD only for clients that hard-refuse SSD.
#[allow(dead_code)]
fn on_decoration_request(_toplevel: ()) {
    // ⚠️ CI-COMPILE: needs `smithay::wayland::shell::xdg::decoration`. Real body:
    //   toplevel.with_pending_state(|s| s.decoration_mode = Some(ServerSide));
    //   toplevel.send_pending_configure();
    todo!("Phase-5 CI: wire xdg-decoration request_mode -> prefer ServerSide")
}

/// ⚠️ CI-COMPILE — `ForeignToplevelListHandler` advertise/withdraw.
/// REAL BODY: `wayland.rs` `impl ForeignToplevelListHandler for State` +
/// `State::sync_foreign_toplevels` (the registry→protocol projection).
/// `wlr-foreign-toplevel-management` is how the `com.hart.Compositor` `window.list`
/// consumers (and any taskbar) enumerate toplevels. On map advertise the toplevel;
/// on destroy withdraw it. This is purely a mirror of `WindowRegistry`; the
/// registry stays the source of truth (one source per object class).
#[allow(dead_code)]
fn on_foreign_toplevel_sync(_registry: &WindowRegistry) {
    // ⚠️ CI-COMPILE: needs `smithay::wayland::foreign_toplevel_list`. Real body
    // diffs the registry against advertised handles and advertises/withdraws.
    todo!("Phase-5 CI: mirror WindowRegistry -> wlr-foreign-toplevel-management")
}

/// Paint the brand-color clear. In the real compositor this binds the pixman (or
/// EGL) renderer to the DRM framebuffer and clears to HART_SPLASH_RGBA before any
/// client maps, so there is no flash-of-black while the glass shell boots.
///
/// FEATURE-OFF FALLBACK: logs intent only. The real body lives in the `winit`/
/// `smithay` builds (which paint for real — the smithay path builds in Nix/CI +
/// is VM-scanout-proven); this no-feature stand-in needs no DRM framebuffer.
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
/// FEATURE-OFF FALLBACK: records the mount intent. The real body (the `winit`/
/// `smithay` builds) initializes WlrLayerShellState and waits for the shell's layer
/// surface to map, keying boot health on a REAL /shell/static fetch (dead-husk-
/// aware, the f294f52 lesson), NOT on inline render. That fetch + paint proof is
/// the nixosTest gate; the smithay scanout path is VM-proven.
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

/// The compositor event loop. With a backend feature ON this DELEGATES to the REAL
/// loop: `--features smithay` + `--backend drm` → `udev::run_udev` (KMS scanout +
/// libinput seat on the PixmanRenderer software floor — builds in Nix/CI, VM-
/// scanout-proven); `--features winit` → `winit::run_winit` (WSL/WSLg). That real
/// loop drives DRM page-flips, libinput events, the Wayland display dispatch, the
/// xdg-shell / XWayland toplevel maps (Phase 5, feeding `WindowRegistry`), and the
/// com.hart.Compositor IPC server (Phase 6). The feature-OFF fallback at the bottom
/// (neither feature — the Windows dev-box build) does NOT start a loop; it only
/// exercises the typed render-path + window-bookkeeping invariants and returns.
fn run_event_loop(cfg: &BootConfig) -> Result<(), Box<dyn std::error::Error>> {
    // ── Milestone 7: when built with the `smithay` (DRM) feature AND asked for the
    // DRM backend, run the REAL-HARDWARE compositor (KMS scanout + libinput seat on
    // the PixmanRenderer software floor). This is the never-fail boot path the
    // hart-comp.nix session + the B4 supervisor's Tier-1 rung drive. It cannot RUN on
    // a box with no DRM device (WSL/dev) — there it errors honestly and the supervisor
    // drops to sway/cage — but it BUILDS in Nix/CI (M9 green) and its pixman scanout is
    // VM-proven, which is the real-HW proof short of the target-GPU flash. `--backend
    // winit` on a smithay build still falls through to the feature-OFF pure-logic floor
    // below (winit needs the distinct `winit` feature's GlesRenderer).
    #[cfg(feature = "smithay")]
    {
        if cfg.backend == Backend::Drm {
            return udev::run_udev(cfg);
        }
    }

    // ── Milestone 1: when built with the `winit` feature, run the REAL compositor
    // (winit backend, nested in WSLg). On that build it boots the event loop, creates
    // a wayland-N socket, and paints clients. The pure-logic fallback below remains the
    // feature-OFF path so the dev-box build (no Wayland/Smithay) stays green. `--backend
    // drm` is reserved for the DRM path (src/udev.rs); on a winit-only build we always
    // run winit.
    #[cfg(feature = "winit")]
    {
        return winit::run_winit(cfg);
    }

    #[allow(unreachable_code)]
    {
        let path = select_render_path(cfg);

        // 1. Paint the brand clear BEFORE anything else (no flash-of-black).
        paint_splash_clear(path);

        // 2. Mount the glass shell layer-shell surface (the desktop).
        mount_glass_shell_layer();

        // 3. Phase-5 native-window tree (the manifest↔toplevel map). Constructed
        //    here so the real loop's xdg-shell / XWayland map handlers feed it; the
        //    feature-OFF fallback only proves it constructs (unit-tested below).
        let _windows = WindowRegistry::new();

        // 4. Feature-OFF fallback stand-in: the event loop is NOT started (this build
        //    has no backend linked — no Wayland socket). We log readiness and return so
        //    `cargo check` proves the SHAPE. The REAL loop is src/winit.rs (winit, built
        //    `--features winit`) and src/wayland.rs + src/udev.rs (DRM, `--features
        //    smithay` — builds in Nix/CI, VM-scanout-proven).
        tracing::info!(
            ?path,
            "HART-comp feature-OFF fallback initialized (event loop NOT started — no \
             backend linked; build --features smithay for the real DRM compositor or \
             --features winit to RUN it nested in WSL)"
        );

        // A real run would block here. The feature-off fallback returns immediately; the integer
        // below documents the intended idle tick the real loop uses for housekeeping.
        let _idle_tick = Duration::from_millis(16); // ~60fps housekeeping cadence
        Ok(())
    }
}

fn main() {
    tracing_subscriber::fmt()
        .with_env_filter(
            tracing_subscriber::EnvFilter::try_from_default_env()
                .unwrap_or_else(|_| "info".into()),
        )
        .init();

    // NOTE: the REAL compositor is this same binary built `--features smithay`
    // (DRM/pixman, M9-green in Nix/CI + VM-scanout-proven) or `--features winit`
    // (WSL); the Nix session launcher (hart-comp.nix) runs the `--features smithay`
    // build with `--backend drm`. With NEITHER feature compiled, run_event_loop hits
    // the pure-logic FALLBACK that does not start a Wayland loop (no backend linked).
    // The warning below is written to be true on BOTH builds and to NOT mislead a
    // reader into thinking the compositor is unbuilt (the M9 build proves otherwise).
    tracing::warn!(
        "HART-comp: OS-native windowing ships TODAY via sway Tier-1 \
         (hart-sway-tier1.nix); HART-comp itself is opt-in. The REAL HART-comp is the \
         `--features smithay` DRM/pixman build (builds in Nix/CI, VM-scanout-proven) — \
         the Nix session runs it with `--backend drm`; a NO-feature build of this binary \
         is only the pure-logic fallback (no event loop). defaultSession stays cage until \
         the software-render path is real-hardware-proven on the target GPU (VM scanout \
         already proven)."
    );

    let cfg = BootConfig::from_args();
    if let Err(e) = run_event_loop(&cfg) {
        tracing::error!(error = %e, "HART-comp failed to initialize");
        std::process::exit(1);
    }
}

// ────────────────────────────────────────────────────────────────────────────
// Pure-logic tests (feature-INDEPENDENT — they run on the default dev-box build):
// prove the PURE decision logic (render-path selection + boot-config parsing) AND
// the Phase-5 window-bookkeeping invariants (handle minting only on map,
// manifest↔toplevel map, the no-phantom-window SummonApp state machine) WITHOUT any
// Wayland/DRM dependency, so the behavioural floor runs even where Smithay cannot
// link. The REAL Smithay paint/scanout/toplevel-map path (wayland.rs/udev.rs)
// builds in Nix/CI (M9 green) and is VM-scanout-proven; real-HARDWARE paint on the
// target GPU is still being verified. The feature-OFF shims below are the one part
// still todo!() — by design, the dev-box fallback.
// ────────────────────────────────────────────────────────────────────────────
#[cfg(test)]
mod tests {
    use super::*;

    // ── Phase 3: render-path never-fail floor ──

    #[test]
    fn force_software_pins_software_path() {
        let cfg = BootConfig { force_software: true, prefer_hardware: false, backend: Backend::Winit };
        assert_eq!(select_render_path(&cfg), RenderPath::Software);
    }

    #[test]
    fn unprobed_hardware_falls_to_software_never_fail_floor() {
        // With the hardware probe stubbed FALSE (the safe Windows-dev default),
        // even a non-forced boot MUST select the software floor — never a blank
        // screen waiting on an unproven hardware path.
        let cfg = BootConfig { force_software: false, prefer_hardware: false, backend: Backend::Winit };
        assert_eq!(select_render_path(&cfg), RenderPath::Software);
    }

    #[test]
    fn prefer_hardware_override_upgrades_to_gles_without_the_probe_file() {
        // The operator override (preferHardwareGL) forces the GLES path EVEN when the
        // boot GPU-probe file is absent/`software` (the exact case the override exists
        // for). Before the `prefer_hardware` field the launcher armed hardware yet the
        // compositor re-read `/run/hart/gpu-render`, saw no `hardware`, and stayed on
        // pixman — the override never reached GLES. This proves it now does.
        let cfg = BootConfig { force_software: false, prefer_hardware: true, backend: Backend::Drm };
        assert_eq!(select_render_path(&cfg), RenderPath::Hardware);
    }

    #[test]
    fn force_software_always_wins_over_prefer_hardware_the_floor_is_not_overridable() {
        // The never-fail software floor is NOT overridable: if BOTH are set (a broken-GPU
        // box that also carries an operator hardware arm), force_software wins so a
        // half-finished hardware path can never brick the box.
        let cfg = BootConfig { force_software: true, prefer_hardware: true, backend: Backend::Drm };
        assert_eq!(select_render_path(&cfg), RenderPath::Software);
    }

    #[test]
    fn splash_clear_is_opaque_brand_color() {
        // Alpha MUST be 1.0 (opaque) so the pre-frame clear actually hides black.
        assert_eq!(HART_SPLASH_RGBA[3], 1.0);
    }

    #[test]
    fn gpu_verdict_hardware_only_on_the_exact_trimmed_token() {
        // The GLES GPU path is authorised ONLY by an exact `hardware` verdict (the
        // hart-gpu-probe smoke-test output). A trailing newline / surrounding whitespace
        // (how the probe writes the file) is tolerated; everything else — `software`, an
        // empty/garbled file, a near-miss token, or `None` (the probe file absent, e.g.
        // the Windows dev box) — stays on the never-fail software floor.
        assert!(gpu_verdict_is_hardware(Some("hardware")));
        assert!(gpu_verdict_is_hardware(Some("hardware\n")), "trailing newline tolerated");
        assert!(gpu_verdict_is_hardware(Some("  hardware  ")), "surrounding whitespace tolerated");
        assert!(!gpu_verdict_is_hardware(Some("software")), "fail-safe verdict stays software");
        assert!(!gpu_verdict_is_hardware(Some("")), "empty verdict → software floor");
        assert!(!gpu_verdict_is_hardware(Some("hardware-ish")), "only the exact token upgrades");
        assert!(!gpu_verdict_is_hardware(None), "absent probe file → software floor (never-fail)");
    }

    // ── #131: the first-scanout beacon (kill the black-but-healthy Tier-1) ──

    #[test]
    fn first_scanout_step_emits_once_on_the_first_real_scanout() {
        // Emit ONLY when not-yet-marked AND a real scanout just completed. The truth
        // table IS the one-shot contract the udev backend relies on.
        assert!(first_scanout_step(false, true), "unmarked + scanned out => emit");
        assert!(!first_scanout_step(true, true), "already marked => never re-emit");
        assert!(!first_scanout_step(false, false), "no scanout => nothing to mark");
        assert!(!first_scanout_step(true, false), "marked + no scanout => no-op");
    }

    #[test]
    fn write_scanout_marker_writes_the_advisory_byte_and_reports_success() {
        // The writer creates the marker file with the sentinel byte and reports true; a
        // path under a missing dir just reports false (best-effort — never a panic).
        let mut ok_path = std::env::temp_dir();
        ok_path.push(format!("hart-scanout-writer-{}.marker", std::process::id()));
        let _ = std::fs::remove_file(&ok_path);
        assert!(write_scanout_marker(&ok_path.to_string_lossy()), "write to a real temp dir succeeds");
        assert_eq!(std::fs::read(&ok_path).unwrap(), b"1\n", "marker carries the sentinel byte");
        let _ = std::fs::remove_file(&ok_path);

        let missing = std::env::temp_dir().join("hart-no-such-dir-xyz").join("m");
        assert!(!write_scanout_marker(&missing.to_string_lossy()), "unwritable path degrades to false, never panics");
    }

    #[test]
    fn first_scanout_beacon_writes_exactly_once_to_the_resolved_path() {
        // The FULL behavioural path: HART_SCANOUT_ALIVE_FLAG resolves the marker location,
        // a caller-owned latch enforces once-only, and no scanout writes nothing. This is
        // the ONLY test that touches HART_SCANOUT_ALIVE_FLAG, so its process-global env set
        // never races another case.
        use std::sync::atomic::AtomicBool;
        // Default resolution first (this test is the ONLY toucher of the env var, so the
        // default read here cannot race another case).
        std::env::remove_var("HART_SCANOUT_ALIVE_FLAG");
        assert_eq!(scanout_marker_path(), SCANOUT_MARKER_DEFAULT, "no override => the pinned /run/hart/session default");

        let mut marker = std::env::temp_dir();
        marker.push(format!("hart-scanout-beacon-{}.marker", std::process::id()));
        let marker_s = marker.to_string_lossy().to_string();
        let _ = std::fs::remove_file(&marker);
        std::env::set_var("HART_SCANOUT_ALIVE_FLAG", &marker_s);

        assert_eq!(scanout_marker_path(), marker_s, "env override resolves the marker path");

        let latch = AtomicBool::new(false);
        // No real scanout yet → no marker, latch stays clear.
        note_first_scanout_once(&latch, false);
        assert!(!latch.load(Ordering::Relaxed), "no scanout leaves the latch clear");
        assert!(!marker.exists(), "no scanout writes no marker");

        // First real scanout → marker written, latch set.
        note_first_scanout_once(&latch, true);
        assert!(latch.load(Ordering::Relaxed), "first scanout sets the latch");
        assert_eq!(std::fs::read(&marker).unwrap(), b"1\n", "first scanout writes the marker");

        // A later scanout must NOT re-write (one-shot): delete the file, call again, and
        // confirm it was not recreated.
        let _ = std::fs::remove_file(&marker);
        note_first_scanout_once(&latch, true);
        assert!(!marker.exists(), "the beacon is one-shot — a later scanout never re-writes");

        std::env::remove_var("HART_SCANOUT_ALIVE_FLAG");
    }

    // ── #137: the frame-budget repaint scheduler. The pure decision that turns the DRM
    // loop from an unconditional 60Hz burn into on-demand rendering, WITHOUT ever being
    // able to freeze the never-fail floor. The `mark_damaged`/`should_paint`/`note_painted`
    // trio is the only Smithay-touching part's policy; the real `Instant::now()` clock +
    // the udev call sites are the CI-compiled half. These assert the invariants that
    // matter: a live scene always paints, an idle scene is skipped, and NO state can wedge
    // the display (the heartbeat always breaks a skip streak). ──

    #[test]
    fn repaint_scheduler_first_frame_always_paints() {
        // Boot posture: dirty, never painted. The splash + glass shell MUST come up, so
        // the very first tick paints regardless of animation or clock.
        let s = RepaintScheduler::new();
        assert!(s.wants_paint(false, None), "a fresh scheduler is dirty → first frame paints");
        assert!(s.wants_paint(false, Some(Duration::from_millis(0))), "even at t=0 the first frame paints (dirty)");
    }

    #[test]
    fn repaint_scheduler_skips_a_provably_idle_frame() {
        // A clean (already-painted, not-dirtied) scheduler with no animation and the last
        // paint well within the heartbeat MUST skip — this is the whole frame-budget win.
        let mut s = RepaintScheduler::new();
        s.note_painted(Instant::now(), false); // clears dirty, records the paint
        assert!(!s.wants_paint(false, Some(Duration::from_millis(16))), "clean + static + fresh paint → skip");
        assert!(!s.wants_paint(false, Some(IDLE_HEARTBEAT - Duration::from_millis(1))), "just under the heartbeat → still skip");
    }

    #[test]
    fn repaint_scheduler_any_damage_forces_a_paint() {
        // A commit / input / map / kill-switch edge marks dirty → the next tick paints even
        // when the clock says "idle" (last paint 1ms ago). This is interactive latency = 0.
        let mut s = RepaintScheduler::new();
        s.note_painted(Instant::now(), false);
        assert!(!s.wants_paint(false, Some(Duration::from_millis(1))), "clean → would skip");
        s.mark_damaged();
        assert!(s.wants_paint(false, Some(Duration::from_millis(1))), "a damage edge forces a paint on the very next tick");
    }

    #[test]
    fn repaint_scheduler_effects_animate_frame_by_frame() {
        // An in-flight fade must play every tick: even clean + fresh-paint, `effects_animating`
        // keeps painting; and `note_painted(animating=true)` re-arms dirty so the NEXT tick also
        // paints (the crossfade is never frozen mid-way).
        let mut s = RepaintScheduler::new();
        s.note_painted(Instant::now(), true); // painted a fade frame; still animating
        assert!(s.dirty, "note_painted keeps dirty while an effect animates");
        assert!(s.wants_paint(true, Some(Duration::from_millis(1))), "an animating effect paints every tick");
    }

    #[test]
    fn repaint_scheduler_heartbeat_self_heals_a_missed_edge() {
        // THE never-wedge invariant: even if a damage edge was FORGOTTEN (clean + not
        // animating), once the heartbeat elapses the tick force-repaints — so a missed
        // mark_damaged() is at worst a ≤200ms stutter, never a frozen screen.
        let mut s = RepaintScheduler::new();
        s.note_painted(Instant::now(), false);
        assert!(!s.wants_paint(false, Some(IDLE_HEARTBEAT - Duration::from_millis(1))), "under heartbeat → skip");
        assert!(s.wants_paint(false, Some(IDLE_HEARTBEAT)), "at the heartbeat → force a repaint (self-heal)");
        assert!(s.wants_paint(false, Some(IDLE_HEARTBEAT + Duration::from_secs(5))), "long past the heartbeat → always paint");
    }

    #[test]
    fn repaint_scheduler_note_painted_clears_dirty_when_static() {
        // After painting a static frame the dirty latch clears, so the FOLLOWING static tick
        // can be skipped. This is the transition from "just showed a change" back to idle.
        let mut s = RepaintScheduler::new();
        s.mark_damaged();
        assert!(s.dirty);
        s.note_painted(Instant::now(), false);
        assert!(!s.dirty, "painting a static frame clears the dirty latch → next static tick is skippable");
    }

    #[test]
    fn repaint_scheduler_can_never_wedge_the_display() {
        // Exhaustive guard mirroring `no_flip_outcome_maps_to_a_compositor_death`: across
        // every (dirty, animating) combination, a scheduler whose heartbeat has elapsed
        // ALWAYS paints. There is no reachable state in which an elapsed heartbeat skips —
        // so a forgotten damage edge can never permanently freeze the never-fail floor.
        for dirty in [false, true] {
            for animating in [false, true] {
                let s = RepaintScheduler { dirty, last_paint: Some(Instant::now()) };
                assert!(
                    s.wants_paint(animating, Some(IDLE_HEARTBEAT)),
                    "dirty={dirty} animating={animating}: an elapsed heartbeat must always repaint (never wedge)"
                );
            }
        }
    }

    #[test]
    fn repaint_heartbeat_is_a_sane_idle_floor() {
        // The idle backstop must be LONGER than one 60Hz tick (so idle actually saves work)
        // yet FAR under the supervisor's 45s first-paint watchdog (so a self-heal is a
        // stutter, never a tier-drop) — the same "sits in the safe window" shape as
        // VBLANK_STALL_TIMEOUT.
        assert!(IDLE_HEARTBEAT > Duration::from_millis(16), "heartbeat must exceed one 60Hz tick to save idle work");
        assert!(IDLE_HEARTBEAT < Duration::from_secs(45), "heartbeat must be far under the 45s paint watchdog");
    }

    // ── Phase 5: handle minting + manifest↔toplevel map ──

    #[test]
    fn handles_are_unique_and_prefixed() {
        let a = mint_handle();
        let b = mint_handle();
        assert_ne!(a, b, "every minted handle is distinct");
        assert!(a.as_str().starts_with("win_"));
        assert!(b.as_str().starts_with("win_"));
    }

    #[test]
    fn on_map_mints_handle_and_records_manifest_mapping() {
        let mut reg = WindowRegistry::new();
        let h = reg.on_map(
            Some("blender".into()),
            ToplevelKind::Xdg,
            Some("org.blender.Blender".into()),
            Some("Blender".into()),
        );
        // The handle resolves back to the manifest (the agent's "which window is
        // app X" query) — this IS the manifest↔toplevel field.
        assert_eq!(reg.handle_for_manifest("blender"), Some(&h));
        assert_eq!(reg.list().len(), 1);
    }

    #[test]
    fn externally_opened_window_has_no_manifest_mapping() {
        // A window opened OUTSIDE the brain (manifest_id None) still lists, but has
        // no manifest mapping (IPC_PROTOCOL.md §4.1 manifest_id = null).
        let mut reg = WindowRegistry::new();
        let _h = reg.on_map(None, ToplevelKind::XWayland, Some("xterm".into()), None);
        assert_eq!(reg.list().len(), 1);
        assert_eq!(reg.handle_for_manifest("xterm"), None);
    }

    #[test]
    fn on_unmap_invalidates_handle_and_clears_manifest() {
        let mut reg = WindowRegistry::new();
        let h = reg.on_map(Some("blender".into()), ToplevelKind::Xdg, None, None);
        assert!(reg.on_unmap(&h), "first unmap removes the live handle");
        assert_eq!(reg.handle_for_manifest("blender"), None);
        assert!(reg.list().is_empty());
        assert!(!reg.on_unmap(&h), "second unmap is a no-op (handle already invalid)");
    }

    #[test]
    fn relaunch_repoints_manifest_without_orphaning_on_old_unmap() {
        // Summon blender (h1), it maps; relaunch maps a new toplevel (h2) for the
        // same manifest BEFORE h1 is destroyed; then h1 unmaps. The manifest must
        // still point at h2 (the relaunch), not be cleared by the stale unmap.
        let mut reg = WindowRegistry::new();
        let h1 = reg.on_map(Some("blender".into()), ToplevelKind::Xdg, None, None);
        let h2 = reg.on_map(Some("blender".into()), ToplevelKind::Xdg, None, None);
        assert_eq!(reg.handle_for_manifest("blender"), Some(&h2));
        reg.on_unmap(&h1);
        assert_eq!(
            reg.handle_for_manifest("blender"),
            Some(&h2),
            "stale unmap of h1 must not clear the relaunched h2 mapping"
        );
    }

    // ── Phase 5: the no-phantom-window SummonApp state machine ──

    #[test]
    fn summon_inert_subsystems_short_circuit_unsupported() {
        // Android (exec sleep infinity — no ART/Waydroid) + macOS/Darling default-off
        // CANNOT map a native window: SummonApp must report Unsupported BEFORE
        // launching, never a phantom handle. (Carried openRisk, architecture §5.4.)
        assert_eq!(summon_precheck("android"), Some(SummonOutcome::Unsupported));
        assert_eq!(summon_precheck("macos"), Some(SummonOutcome::Unsupported));
    }

    #[test]
    fn summon_wine_and_native_proceed_to_await_map_not_installer_success() {
        // The installer returns success=True for Wine UNCONDITIONALLY (Wine returns
        // 0 even when nothing maps). At the WM layer we REFUSE to treat that as
        // success: windows(Wine)/linux/flatpak/pwa proceed to AWAIT A REAL MAP
        // (precheck returns None = "go launch, then wait for a toplevel"), they do
        // NOT short-circuit to Mapped here. The handle only comes from on_map.
        assert_eq!(summon_precheck("windows"), None);
        assert_eq!(summon_precheck("linux"), None);
        assert_eq!(summon_precheck("flatpak"), None);
    }

    #[test]
    fn pending_summon_resolves_only_on_a_matching_real_map() {
        // A pending summon for "blender" (expect Xdg) is satisfied by a matching
        // map, and NOT by a different manifest or a mismatched kind. This is the
        // gate between "launched" and "Mapped(handle)".
        let p = PendingSummon::new("blender", Some(ToplevelKind::Xdg));
        assert!(p.accepts("blender", ToplevelKind::Xdg));
        assert!(!p.accepts("gimp", ToplevelKind::Xdg), "wrong manifest must not resolve");
        assert!(
            !p.accepts("blender", ToplevelKind::XWayland),
            "wrong toplevel kind must not resolve"
        );
    }

    #[test]
    fn pending_summon_any_kind_accepts_either_protocol() {
        // expect_kind None = accept whichever protocol maps (used when the launcher
        // can't predict Xdg-vs-XWayland).
        let p = PendingSummon::new("app", None);
        assert!(p.accepts("app", ToplevelKind::Xdg));
        assert!(p.accepts("app", ToplevelKind::XWayland));
    }

    #[test]
    fn pending_summon_times_out_to_honest_failure_never_a_handle() {
        // Deterministic timeout via explicit clock: a summon whose map window has
        // elapsed reports timed-out. The brain maps this to error.code="timeout"
        // — NEVER a handle. (No real time passes; we construct the start in the
        // past.) This is the core no-phantom-window invariant.
        let start = Instant::now() - Duration::from_secs(30);
        let p = PendingSummon::new_at("blender", Some(ToplevelKind::Xdg), start,
                                      Duration::from_secs(10));
        assert!(p.is_timed_out_at(Instant::now()), "elapsed summon must time out");

        // A fresh summon within the window has NOT timed out.
        let fresh = PendingSummon::new("blender", Some(ToplevelKind::Xdg));
        assert!(!fresh.is_timed_out_at(Instant::now()));
    }

    #[test]
    fn summon_outcome_has_no_handleless_mapped_variant() {
        // Type-level guarantee made executable: the ONLY way to get a Mapped is to
        // hand it a real minted handle (which only on_map produces). TimedOut /
        // Unsupported carry NO handle. A reviewer reading this test sees the
        // no-phantom-window guarantee is unrepresentable to violate.
        let mapped = SummonOutcome::Mapped(mint_handle());
        match mapped {
            SummonOutcome::Mapped(h) => assert!(h.as_str().starts_with("win_")),
            _ => panic!("constructed a Mapped"),
        }
        assert_eq!(SummonOutcome::TimedOut, SummonOutcome::TimedOut);
        assert_eq!(SummonOutcome::Unsupported, SummonOutcome::Unsupported);
    }

    // ── Phase 5: the SummonResolver orchestration (launch → await real map) ──
    // These prove the END-TO-END SummonApp flow the Smithay handlers drive: the
    // ONLY way a summon becomes Mapped is a real map feeding `resolve`; everything
    // else is an honest failure. `wayland.rs::State` mirrors this exact logic on a
    // live compositor — proving it here (Wayland-free) is what lets the VM bring-up
    // be "just wire the edges", not "also get the no-phantom logic right".

    #[test]
    fn summon_resolves_to_mapped_only_via_a_real_map() {
        let mut r = SummonResolver::new();
        // Precheck says "go" for a native/Wine launch (not inert) → we begin.
        assert_eq!(summon_precheck("flatpak"), None);
        r.begin("blender", Some(ToplevelKind::Xdg));
        assert_eq!(r.pending_count(), 1);

        // A real map arrives (handle minted by WindowRegistry::on_map upstream).
        let h = mint_handle();
        let outcome = r.resolve("blender", ToplevelKind::Xdg, h.clone());
        assert_eq!(outcome, Some(SummonOutcome::Mapped(h)));
        // The pending summon was consumed — it can resolve exactly once.
        assert_eq!(r.pending_count(), 0);
    }

    #[test]
    fn summon_a_nonmatching_map_does_not_resolve_it() {
        // A map for a DIFFERENT manifest (or wrong kind) is a real window but not
        // THIS summon — returns None (an externally-opened window), and the summon
        // stays pending until its own map or its timeout.
        let mut r = SummonResolver::new();
        r.begin("blender", Some(ToplevelKind::Xdg));

        assert_eq!(r.resolve("gimp", ToplevelKind::Xdg, mint_handle()), None);
        assert_eq!(
            r.resolve("blender", ToplevelKind::XWayland, mint_handle()),
            None,
            "wrong toplevel kind must not resolve the Xdg summon"
        );
        assert_eq!(r.pending_count(), 1, "summon still awaiting its real map");
    }

    #[test]
    fn summon_expires_to_timed_out_never_a_handle() {
        // A summon whose map never arrives times out to an honest failure. We
        // construct it already-elapsed (no real time passes) and expire it.
        let mut r = SummonResolver::new();
        r.pending.push(PendingSummon::new_at(
            "blender",
            Some(ToplevelKind::Xdg),
            Instant::now() - Duration::from_secs(30),
            Duration::from_secs(10),
        ));
        let timed_out = r.expire(Instant::now());
        assert_eq!(
            timed_out,
            vec![("blender".to_string(), SummonOutcome::TimedOut)],
            "an unmapped summon must time out to TimedOut (error.code=timeout), no handle"
        );
        assert_eq!(r.pending_count(), 0);
    }

    #[test]
    fn summon_within_window_is_not_expired() {
        // A fresh summon is NOT swept by expire — only elapsed ones.
        let mut r = SummonResolver::new();
        r.begin("blender", Some(ToplevelKind::Xdg));
        assert!(r.expire(Instant::now()).is_empty());
        assert_eq!(r.pending_count(), 1);
    }

    #[test]
    fn inert_subsystem_never_begins_a_summon_short_circuits_unsupported() {
        // The full no-phantom path for Android/macOS: precheck returns Unsupported
        // BEFORE any launch, so the resolver never even begins awaiting a map —
        // there is no pending summon to ever (wrongly) resolve.
        let mut r = SummonResolver::new();
        for inert in ["android", "macos"] {
            match summon_precheck(inert) {
                Some(SummonOutcome::Unsupported) => { /* correct: do NOT begin */ }
                other => panic!("inert subsystem {inert} must precheck Unsupported, got {other:?}"),
            }
        }
        assert_eq!(r.pending_count(), 0, "no summon begun for inert subsystems");
        // And nothing to expire / resolve.
        assert!(r.expire(Instant::now()).is_empty());
    }

    // ── render-path / backend selection coverage (the boot-config decisions) ──

    #[test]
    fn drm_backend_still_selects_the_software_floor_when_unprobed() {
        // The DRM backend (pixman) is a software floor by construction; the path
        // decision is still Software for an unprobed/non-forced DRM boot.
        let cfg = BootConfig { force_software: false, prefer_hardware: false, backend: Backend::Drm };
        assert_eq!(select_render_path(&cfg), RenderPath::Software);
    }

    #[test]
    fn render_path_variants_are_distinct() {
        // A trivial-but-load-bearing guard: Software != Hardware (the never-fail floor
        // must be a different decision from the opportunistic upgrade).
        assert_ne!(RenderPath::Software, RenderPath::Hardware);
        assert_eq!(RenderPath::Software, RenderPath::Software);
    }

    // ── registry record read (the IPC event-payload source) ──

    #[test]
    fn record_returns_the_window_record_until_unmapped() {
        // `record(handle)` is the source the IPC `window.opened`/`closed` frames read
        // (app_id/title/kind). It resolves while the handle is live and is None after
        // on_unmap — the same invalidation the IPC contract promises.
        let mut reg = WindowRegistry::new();
        let h = reg.on_map(
            Some("gimp".into()),
            ToplevelKind::XWayland,
            Some("Gimp".into()),
            Some("GNU Image Manip".into()),
        );
        let rec = reg.record(&h).expect("a live handle has a record");
        assert_eq!(rec.kind, ToplevelKind::XWayland);
        assert_eq!(rec.app_id.as_deref(), Some("Gimp"));
        assert_eq!(rec.title.as_deref(), Some("GNU Image Manip"));
        assert_eq!(rec.manifest_id.as_deref(), Some("gimp"));
        reg.on_unmap(&h);
        assert!(reg.record(&h).is_none(), "record is None after the handle is invalidated");
    }

    #[test]
    fn list_enumerates_every_live_handle() {
        // window.list reads the registry — it must enumerate every mapped handle,
        // brain-summoned or not, and shrink as windows close.
        let mut reg = WindowRegistry::new();
        let h1 = reg.on_map(Some("a".into()), ToplevelKind::Xdg, None, None);
        let _h2 = reg.on_map(None, ToplevelKind::Xdg, None, None); // externally opened
        assert_eq!(reg.list().len(), 2, "both mapped windows list");
        reg.on_unmap(&h1);
        assert_eq!(reg.list().len(), 1, "closing one shrinks the list");
    }

    // ── inert-subsystem precheck completeness ──

    #[test]
    fn summon_subsystem_is_inert_only_for_android_and_macos() {
        assert!(summon_subsystem_is_inert("android"));
        assert!(summon_subsystem_is_inert("macos"));
        // Every mappable platform is NOT inert (so it proceeds to await a real map).
        for live in ["windows", "linux", "flatpak", "pwa", "web"] {
            assert!(!summon_subsystem_is_inert(live), "{live} can map a native window");
        }
    }

    // ── multi-summon resolution order (FIFO match by manifest+kind) ──

    #[test]
    fn two_pending_summons_resolve_independently_by_manifest() {
        // Two apps summoned; each resolves ONLY on its own matching map, in any order,
        // without consuming the other's pending slot.
        let mut r = SummonResolver::new();
        r.begin("blender", Some(ToplevelKind::Xdg));
        r.begin("inkscape", Some(ToplevelKind::Xdg));
        assert_eq!(r.pending_count(), 2);

        let hi = mint_handle();
        assert_eq!(
            r.resolve("inkscape", ToplevelKind::Xdg, hi.clone()),
            Some(SummonOutcome::Mapped(hi)),
            "inkscape resolves on its own map"
        );
        assert_eq!(r.pending_count(), 1, "blender still pending after inkscape resolved");

        let hb = mint_handle();
        assert_eq!(
            r.resolve("blender", ToplevelKind::Xdg, hb.clone()),
            Some(SummonOutcome::Mapped(hb))
        );
        assert_eq!(r.pending_count(), 0);
    }

    #[test]
    fn expire_only_sweeps_the_elapsed_summon_leaving_the_fresh_one() {
        // A mixed pending set: one elapsed, one fresh. expire() returns only the
        // elapsed manifest and leaves the fresh summon awaiting its map.
        let mut r = SummonResolver::new();
        r.pending.push(PendingSummon::new_at(
            "stale",
            Some(ToplevelKind::Xdg),
            Instant::now() - Duration::from_secs(60),
            Duration::from_secs(10),
        ));
        r.begin("fresh", Some(ToplevelKind::Xdg));
        let expired = r.expire(Instant::now());
        assert_eq!(expired, vec![("stale".to_string(), SummonOutcome::TimedOut)]);
        assert_eq!(r.pending_count(), 1, "the fresh summon survives the sweep");
    }
}
