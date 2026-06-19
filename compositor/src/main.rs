// ════════════════════════════════════════════════════════════════════════════
// HART-comp — HART OS AI-native Wayland compositor (Smithay) — PHASE-3 SKELETON
//                                                            + PHASE-5 WINDOW DRAFT
// ════════════════════════════════════════════════════════════════════════════
//
// ⚠️  STATUS: COMPILE-PENDING SKELETON — authored on a Windows dev box where no
//     Wayland/KMS/Smithay build can run. The Smithay HANDLER BODIES below are
//     NOT compiled or booted yet. Every paint, DRM scanout, libinput seat,
//     layer-shell mount, AND xdg-shell/XWayland toplevel map is VM-pending
//     (CI nixosTest on an llvmpipe software-GL VM, or local QEMU-KVM).
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
// WHY THIS IS A SKELETON, NOT THE COMPOSITOR:
//   The ROADMAP ordering invariant is sway-as-Tier-1-NOW, Smithay-as-later-moat.
//   OS-native agent windowing ships TODAY via nixos/modules/hart-sway-tier1.nix
//   (sway + swaymsg shim — and the brain-side HartWmClient already drives it,
//   integrations/agent_engine/hart_wm_client.py, LIVE-verified in WSL sway). This
//   file is the eventual first-party compositor that will own the agent-driven
//   window tree + the com.hart.Compositor IPC (the moat GNOME/Copilot cannot
//   match: an AI that owns window-PLACEMENT POLICY). It stays a tinywl-class
//   scaffold so the FIRST Rust-in-Nix build is bring-up-proven on llvmpipe before
//   real window management is wired.
//
// EVERY real Smithay handler body below is `todo!()` / a stub returning the
// honest "not wired yet" — so a reviewer can never mistake the scaffold for a
// working compositor, and CI compiles the SHAPE before the behavior lands.

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
// module, NOT to the always-compiled skeleton). On the Windows dev box neither
// `winit` nor `smithay` is on, so this module is NOT compiled and the pure-logic
// floor + `#[cfg(test)]` below stay green. `cargo build --features winit` (in WSL,
// nested in WSLg) compiles + RUNS it. See src/winit.rs for the full rationale.
#[cfg(feature = "winit")]
mod winit;

// ── Milestone 4: the com.hart.Compositor IPC server (Unix-socket twin), wired to
// the winit Space so an agent arranges REAL native windows. Gated behind the SAME
// `winit` feature as the live compositor it drives (the IPC handlers mutate
// `winit::State.space`). Off on the default dev-box build. See src/ipc.rs.
#[cfg(feature = "winit")]
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
// skeleton's `cargo check` surface stays minimal while the intent is explicit.
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
            backend,
        }
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
// callers), but NOT yet from the skeleton `main` (the event loop is unstarted). The
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
/// DRM page-flips, libinput events, the Wayland display dispatch, the xdg-shell /
/// XWayland toplevel maps (Phase 5, feeding `WindowRegistry`), and the
/// com.hart.Compositor IPC server (Phase 6). Nothing destructive happens here yet.
fn run_event_loop(cfg: &BootConfig) -> Result<(), Box<dyn std::error::Error>> {
    // ── Milestone 7: when built with the `smithay` (DRM) feature AND asked for the
    // DRM backend, run the REAL-HARDWARE compositor (KMS scanout + libinput seat on
    // the PixmanRenderer software floor). This is the never-fail boot path the
    // hart-comp.nix session + the B4 supervisor's Tier-1 rung drive. It cannot RUN on
    // a box with no DRM device (WSL/dev) — there it errors honestly and the supervisor
    // drops to sway/cage — but it COMPILES here, which is the M7 proof for the DRM
    // path. `--backend winit` on a smithay build still falls through to the skeleton
    // floor below (winit needs the distinct `winit` feature's GlesRenderer).
    #[cfg(feature = "smithay")]
    {
        if cfg.backend == Backend::Drm {
            return udev::run_udev(cfg);
        }
    }

    // ── Milestone 1: when built with the `winit` feature, run the REAL compositor
    // (winit backend, nested in WSLg). This is no longer a skeleton on that build —
    // it boots the event loop, creates a wayland-N socket, and paints clients. The
    // pure-logic skeleton below remains the feature-OFF fallback so the dev-box
    // build (no Wayland/Smithay) stays green. `--backend drm` is reserved for the
    // DRM path (src/udev.rs); on a winit-only build we always run winit.
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
        //    skeleton only proves it constructs (the behavior is unit-tested below).
        let _windows = WindowRegistry::new();

        // 4. SKELETON stand-in: the loop is not started (there is no Wayland socket
        //    on Windows). We log readiness and return so `cargo check` proves the
        //    SHAPE. The REAL loop is src/winit.rs (winit, built `--features winit`)
        //    and src/wayland.rs (DRM, `--features smithay`).
        tracing::info!(
            ?path,
            "HART-comp skeleton initialized (event loop NOT started — feature-OFF floor; \
             build --features winit to RUN the real compositor)"
        );

        // A real run would block here. The skeleton returns immediately; the integer
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

    tracing::warn!(
        "HART-comp is a COMPILE-PENDING Phase-3 skeleton (+ Phase-5 window-bookkeeping \
         draft). OS-native windowing ships TODAY via sway Tier-1 (hart-sway-tier1.nix). \
         This binary is opt-in only; defaultSession stays cage until the software-render \
         path is VM-proven."
    );

    let cfg = BootConfig::from_args();
    if let Err(e) = run_event_loop(&cfg) {
        tracing::error!(error = %e, "HART-comp skeleton failed to initialize");
        std::process::exit(1);
    }
}

// ────────────────────────────────────────────────────────────────────────────
// Skeleton-only tests: prove the PURE decision logic (render-path selection +
// boot-config parsing) AND the Phase-5 window-bookkeeping invariants (handle
// minting only on map, manifest↔toplevel map, the no-phantom-window SummonApp
// state machine) WITHOUT any Wayland/DRM dependency, so even the skeleton has a
// behavioural unit floor that CI can run the moment Smithay compiles. Real
// paint/scanout/toplevel-map proof remains VM-only (the Smithay handler bodies
// above are todo!() until then).
// ────────────────────────────────────────────────────────────────────────────
#[cfg(test)]
mod tests {
    use super::*;

    // ── Phase 3: render-path never-fail floor ──

    #[test]
    fn force_software_pins_software_path() {
        let cfg = BootConfig { force_software: true, backend: Backend::Winit };
        assert_eq!(select_render_path(&cfg), RenderPath::Software);
    }

    #[test]
    fn unprobed_hardware_falls_to_software_never_fail_floor() {
        // With the hardware probe stubbed FALSE (the safe Windows-dev default),
        // even a non-forced boot MUST select the software floor — never a blank
        // screen waiting on an unproven hardware path.
        let cfg = BootConfig { force_software: false, backend: Backend::Winit };
        assert_eq!(select_render_path(&cfg), RenderPath::Software);
    }

    #[test]
    fn splash_clear_is_opaque_brand_color() {
        // Alpha MUST be 1.0 (opaque) so the pre-frame clear actually hides black.
        assert_eq!(HART_SPLASH_RGBA[3], 1.0);
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
}
