// ════════════════════════════════════════════════════════════════════════════
// HART-comp — Milestone 7: the REAL-HARDWARE DRM/udev backend (KMS scanout +
// libinput seat) on the PixmanRenderer software floor.
//                                          ⚠️  CI-COMPILE (smithay feature only)  ⚠️
// ════════════════════════════════════════════════════════════════════════════
//
// This is the never-fail boot path the `hart-comp.nix` Tier-1 session + the B4
// supervisor's Tier-1 rung drive on real hardware. It is the software-floor twin of
// `winit.rs`: where winit nests in a host Wayland (WSLg) and paints with the
// GlesRenderer, this owns DRM/KMS scanout directly and paints with the MANDATORY
// pixman software renderer (the broken-GPU never-fail floor the whole ROADMAP §6
// tiering rests on — paint on ANY GPU, correctness over fps).
//
// ── Honest status (the M7 deliverable, Stage A of the udev plan) ──
//   This COMPILES under `--features smithay` (the M7 compile-proof for the DRM
//   path) but CANNOT RUN on a box with no DRM device — WSL/the Windows dev box have
//   no `/dev/dri/card0`, so `LibSeatSession::new()` / `primary_gpu()` fail and
//   `run_udev` returns an error. That is EXPECTED and is exactly why the B4
//   supervisor exists: a Tier-1 that cannot bring up its session drops to sway
//   (Tier-2) / cage (Tier-3), so the screen still paints. The real paint/scanout
//   proof is the flash onto real hardware (or a QEMU-KVM guest with a virtio-gpu
//   DRM node) — the ROADMAP's "honest hardware limit".
//
// ── What it brings up (Stage A) ──
//   DRM/KMS scanout of the LAYER-SHELL glass shell + a solid HART_SPLASH clear, on
//   the pixman floor, proving the never-fail boot path. It reuses the SAME
//   `wayland.rs` `State` + handlers (xdg/XWayland/decoration/foreign-toplevel +
//   the M7 compositor/shm/layer/output bundle) + the SAME pure WindowRegistry /
//   no-phantom-window summon logic + the `shared.rs` helpers. The ONLY
//   backend-specific code here is the DRM/GBM/libinput wiring + the
//   DrmCompositor::render_frame scanout — there is no second compositor, no second
//   handler set, no parallel path.
//
// ── Modelled on the pinned Smithay rev (4784339) ──
//   The `DrmCompositor::new(...)` + `render_frame` + `queue_frame` + `frame_submitted`
//   sequence is the canonical one from the rev's own
//   `src/backend/drm/compositor/mod.rs` module doc example. The session/udev/libinput
//   wiring mirrors `anvil/src/udev.rs::run_udev` (LibSeatSession + UdevBackend +
//   Libinput::new_with_udev + the DrmDevice/GbmDevice/connector scan), but uses the
//   PixmanRenderer software floor instead of anvil's GpuManager/MultiRenderer/EGL
//   GpuManager — the ROADMAP's MANDATORY floor. (The pinned rev's PixmanRenderer
//   impls `Bind<Dmabuf>`, so it binds the GBM scanout buffer DrmCompositor hands it.)
//
// ── `#![deny(unsafe_code)]` discipline (crate-wide) ──
//   anvil's `unsafe { display.get_mut() }` Generic-source pattern is FORBIDDEN here.
//   The Display is dispatched DIRECTLY each loop iteration (the same safe pattern
//   winit.rs uses); the calloop DRM/udev/libinput/session sources need no unsafe.

#![cfg(feature = "smithay")]
#![allow(clippy::too_many_lines)]

use std::collections::HashMap;
use std::path::Path;
use std::sync::atomic::AtomicBool;
use std::sync::Arc;
use std::time::Duration;

use smithay::backend::allocator::dmabuf::Dmabuf;
use smithay::backend::allocator::gbm::{GbmAllocator, GbmBufferFlags, GbmDevice};
use smithay::backend::allocator::Fourcc;
use smithay::backend::drm::compositor::{DrmCompositor, FrameError, FrameFlags, RenderFrameError};
use smithay::backend::drm::exporter::gbm::GbmFramebufferExporter;
use smithay::backend::drm::{DrmDevice, DrmDeviceFd, DrmEvent, DrmNode, DrmSurface};
// PART 3 of the GPU lever — the GLES2 GPU renderer + the EGL platform display it is
// built on (an `EGLDisplay` from the GBM device of the primary render node). Compiled
// by the `smithay/renderer_gl` + `smithay/backend_egl` passthroughs added to the
// `smithay` cargo feature. The PixmanRenderer below stays the renderer of record (the
// never-fail software floor); GLES is the opportunistic upgrade gated on the GPU probe.
use smithay::backend::egl::{EGLContext, EGLDisplay};
use smithay::backend::libinput::{LibinputInputBackend, LibinputSessionInterface};
use smithay::backend::renderer::element::RenderElement;
use smithay::backend::renderer::gles::GlesRenderer;
use smithay::backend::renderer::pixman::PixmanRenderer;
use smithay::backend::renderer::{Bind, Color32F, ImportDma, Renderer, Texture};
use smithay::backend::session::libseat::LibSeatSession;
use smithay::backend::session::{Event as SessionEvent, Session};
use smithay::backend::udev::{primary_gpu, UdevBackend, UdevEvent};
use smithay::desktop::{layer_map_for_output, Space, Window};
use smithay::input::{Seat, SeatState};
use smithay::output::{Mode as WlMode, Output, PhysicalProperties, Subpixel};
use smithay::reexports::calloop::EventLoop;
// `Device as BasicDevice` is the base `drm::Device` trait — it provides
// `acquire_master_lock()` / `release_master_lock()` (the `drmSetMaster` / `drmDropMaster`
// ioctls). We call these DIRECTLY on the `DrmDeviceFd` to (re)take DRM master, which is
// the ONLY way to recover master after smithay's one-shot construction-time grab loses
// the boot-VT race: smithay freezes its private `privileged` flag at `DrmDeviceFd::new`
// and `DrmDevice::activate()` only re-`acquire_master_lock`s when that frozen flag is
// already true — so an unprivileged-at-startup device can NEVER recover via `activate()`.
// `drmSetMaster` on the raw fd has no such gate; the kernel grants it once the prior
// master (fbcon/simpledrm on the boot VT) has dropped and our logind session is active.
use smithay::reexports::drm::control::{connector, crtc, Device as ControlDevice};
use smithay::reexports::drm::Device as BasicDevice;
use smithay::reexports::input::Libinput;
use smithay::reexports::rustix::fs::OFlags;
use smithay::reexports::wayland_server::Display;
use smithay::utils::{DeviceFd, Size, Transform};
// `Window::wl_surface()` is provided by the `WaylandFocus` trait on this rev (not an
// inherent method) — it MUST be in scope for the render element build to call it.
use smithay::wayland::seat::WaylandFocus;
use smithay::wayland::compositor::CompositorState;
use smithay::wayland::foreign_toplevel_list::ForeignToplevelListState;
use smithay::wayland::output::OutputManagerState;
use smithay::wayland::selection::data_device::DataDeviceState;
use smithay::wayland::shell::wlr_layer::WlrLayerShellState;
use smithay::wayland::shell::xdg::decoration::XdgDecorationState;
use smithay::wayland::shell::xdg::XdgShellState;
use smithay::wayland::shm::ShmState;
use smithay::wayland::socket::ListeningSocketSource;
use smithay::wayland::xwayland_shell::XWaylandShellState;
use smithay::xwayland::{X11Wm, XWayland, XWaylandEvent};
use tracing::{debug, error, info, warn};

use crate::comp_core::{self, HartRenderElement};
use crate::shared::send_frame_callbacks;
use crate::wayland::{ClientState, State};
use crate::{
    note_first_scanout_once, select_render_path, BootConfig, RenderPath, WindowRegistry,
    HART_SPLASH_RGBA,
};

/// #131 — one-shot latch for the first-scanout beacon. Flipped by `reap_completed_vblanks`
/// on the FIRST reaped page-flip vblank (a frame that ACTUALLY scanned out to the CRTC),
/// which writes `/run/hart/session/first-scanout` so the supervisor's scanout watchdog can
/// tell a live desktop from a black-but-healthy Tier-1 (the shell-ready marker cannot — the
/// WebView fires it from its own client buffer even if nothing reached the display). The
/// pure decision + path + write live in main.rs (unit-tested); this owns only the latch.
static FIRST_SCANOUT: AtomicBool = AtomicBool::new(false);

/// Color formats DrmCompositor will try for the primary plane framebuffer. The pixman
/// software floor + virtually all KMS drivers support Argb8888/Xrgb8888 — the never-
/// fail floor formats (no 10-bit gamble on an unproven box).
const COLOR_FORMATS: &[Fourcc] = &[Fourcc::Argb8888, Fourcc::Xrgb8888];

/// The frame flags the pixman software floor renders with — DELIBERATELY `empty()`, i.e.
/// no `ALLOW_*_PLANE_SCANOUT` bits. With no plane-offload flags, `DrmCompositor` composites
/// EVERY element (windows, the layer-shell glass desktop, AND the baked software cursor that
/// `comp_core::build_frame_elements` already puts in the list) into its own primary swapchain
/// buffer with the PixmanRenderer and page-flips that single buffer — it never tries to assign
/// the cursor to a hardware cursor plane or an element to an overlay/direct-scanout plane.
///
/// Two reasons this is the never-fail floor (correctness over fps), not `FrameFlags::DEFAULT`:
///   1. The cursor is a COMPOSITED software cursor, so a driver whose hardware cursor plane is
///      missing / unusable can never blank or abort the frame. This is what makes the real-HW
///      "Failed to set cursor plane" log a NON-EVENT: with the cursor-plane bit off, smithay's
///      `try_assign_cursor_plane` early-returns `None` (it never issues the cursor-plane atomic
///      commit at all), so that ioctl — and the "Failed to destroy old node property" plane
///      cleanup that follows it — simply never happen. The arrow still draws (it is in the
///      element list); it is just painted by pixman onto the primary plane like everything else.
///   2. While DRM master is still mid-handoff at boot, every per-element plane-assignment atomic
///      TEST commit would fail EACCES and churn; emitting none of them keeps the floor quiet and
///      deterministic. Once master is held (see `acquire_drm_master`) the composited path keeps
///      working unchanged — there is no fast/slow divergence to drift.
const SOFTWARE_FLOOR_FRAME_FLAGS: FrameFlags = FrameFlags::empty();

/// The pixman-backed DRM scanout surface for one output. `DrmCompositor` walks the
/// render elements, composites them on the primary plane with the PixmanRenderer, and
/// page-flips the GBM buffer to the CRTC. The unit type for the per-frame user-data
/// (`queue_frame(())`) — HART-comp carries no presentation-feedback payload yet.
type HartDrmCompositor =
    DrmCompositor<GbmAllocator<DrmDeviceFd>, GbmFramebufferExporter<DrmDeviceFd>, (), DrmDeviceFd>;

/// One CRTC's scanout surface + its page-flip pacing state. `awaiting_vblank` is the
/// F1 (#166) torn-frame fix: it is set the instant a frame is `queue_frame`'d (a flip
/// is in flight to the CRTC) and cleared only when the matching `DrmEvent::VBlank`
/// lands and `frame_submitted()` reaps it. The render tick refuses to `queue_frame` a
/// NEW frame while this is true — so a frame is submitted ONLY after the prior flip's
/// vblank completes, never on top of an in-flight flip (which the kernel would reject
/// with EBUSY and which tears the scanout).
struct SurfaceData {
    /// The DrmCompositor that composites + page-flips this CRTC.
    compositor: HartDrmCompositor,
    /// A page-flip is in flight to this CRTC; the next queue must wait for its vblank.
    awaiting_vblank: bool,
    /// When the in-flight flip was queued. Guards against a LOST vblank (a VT switch /
    /// suspend / driver hiccup can swallow the page-flip event): if no vblank arrives
    /// within `VBLANK_STALL_TIMEOUT`, `render_all` force-clears `awaiting_vblank` and
    /// retries, so one dropped vblank degrades to a stutter — NOT a permanent freeze that
    /// the paint watchdog would turn into a boot loop (#166 + #186 robustness).
    flip_queued_at: Option<std::time::Instant>,
    /// When this CRTC last actually PRESENTED (a flip was queued successfully). `None`
    /// until the first. Diagnostic only: it feeds the silent-freeze beacon below and
    /// changes no scheduling decision.
    last_flip_at: Option<std::time::Instant>,
    /// Rate-limiter for that beacon, so a frozen CRTC logs once every
    /// `SILENT_FREEZE_LOG_EVERY` instead of 60 times a second.
    last_stall_log_at: Option<std::time::Instant>,
}

/// If a queued page-flip's vblank has not arrived within this long, assume it was lost
/// (VT switch / suspend / driver hiccup) and let the CRTC re-render. ~5 frames at 60Hz —
/// comfortably past a real vblank (16.7ms) so we never pre-empt a healthy flip, but short
/// enough that a dropped vblank is an imperceptible stutter rather than a frozen screen.
const VBLANK_STALL_TIMEOUT: Duration = Duration::from_millis(100);

/// SILENT-FREEZE BEACON (real-HW 2026-08-19). The box froze on an orb click+drag and
/// left NO evidence: hart-comp's loop kept running at its full 66 wakeups/sec, the
/// Wayland side stayed healthy (a fresh client connected, and a brand-new toplevel was
/// accepted and logged as window.opened), the cursor still moved, the GPU was awake with
/// zero i915 errors — and yet nothing was ever presented again, for ANY client. The new
/// window never appeared either, which is what rules the shell out as the cause.
///
/// Every FAILURE path in `present_surfaces` already logs (queue_frame errors,
/// render_frame faults, the lost-vblank recovery). None of them fired. By elimination
/// the tick is taking the ONE silent path, `Ok(true) => continue` ("render_frame says
/// nothing changed"), on every tick forever — which cannot be a scheduling stall,
/// because RepaintScheduler's 200ms IDLE_HEARTBEAT forces a paint attempt 5x/sec
/// regardless of damage.
///
/// So the question this beacon exists to answer is precisely: when the screen is frozen
/// but the loop is healthy, what does the render path actually SEE? It reports the
/// element count it was handed, the per-CRTC flip state, and how long since this CRTC
/// last presented. Diagnostic ONLY: it never changes a scheduling decision, so it cannot
/// perturb the thing it is measuring.
const SILENT_FREEZE_AFTER: Duration = Duration::from_secs(3);
/// Rate-limit for the beacon: once per this interval per CRTC, so a permanently frozen
/// output produces a readable trail instead of flooding the journal at 60Hz.
const SILENT_FREEZE_LOG_EVERY: Duration = Duration::from_secs(5);

/// One opened DRM device (the GPU) + its per-CRTC scanout surfaces. Held in the loop
/// data so the VBlank handler can find the right surface to mark submitted + re-queue.
struct DeviceData {
    /// The DRM device. Held to keep the modeset alive (dropping it tears it down) AND to
    /// `activate()`/`pause()` it on libseat session changes (VT switch / suspend / resume).
    /// NOTE: `activate()` alone CANNOT re-take DRM master after an unprivileged-at-startup
    /// grab — it only re-`acquire_master_lock`s when smithay's frozen `privileged` flag is
    /// already true. The real master (re)acquire goes through `fd` below; see `acquire_drm_master`.
    drm: DrmDevice,
    /// A clone of the device fd, kept SPECIFICALLY so we can call `acquire_master_lock()` /
    /// `release_master_lock()` (the raw `drmSetMaster` / `drmDropMaster` ioctls) on it directly.
    /// This is the ONLY path that can flip an unprivileged device to master on this smithay rev:
    /// `DrmDeviceFd::new` grabs master exactly once at construction and freezes `privileged`, and
    /// the kernel only grants master once the boot-VT's prior master (fbcon/simpledrm) has dropped
    /// and our logind session is active — which on a fresh boot happens AFTER construction, so the
    /// construction grab races and loses ("Unable to become drm master, assuming unprivileged
    /// mode"). Re-issuing `drmSetMaster` on this fd from the render tick recovers it.
    fd: DrmDeviceFd,
    /// GBM device — the buffer allocator backing both the scanout swapchain + the
    /// framebuffer exporter. Cloned into the allocator/exporter; held so it outlives them.
    /// ALSO read by `build_gles_renderer` to create the EGL platform display for the GLES
    /// GPU renderer (the `EGLDisplay::new(gbm.clone())` path) — hence no longer `_`-prefixed.
    gbm: GbmDevice<DrmDeviceFd>,
    /// One scanout surface per active CRTC (one display). Keyed by CRTC handle.
    surfaces: HashMap<crtc::Handle, SurfaceData>,
    /// Whether THIS device currently holds DRM master at the kernel level (a confirmed
    /// `drmSetMaster` succeeded). Init FALSE and confirmed lazily: the render tick re-attempts
    /// `acquire_drm_master` every frame while this is false (cheap ioctl) until the kernel grants
    /// master, then leaves it alone. Reset to false on a libseat PauseSession (we drop master).
    /// This is the per-device latch that turns the one-shot construction grab into a bounded,
    /// self-healing retry — without it the unprivileged-at-startup device stays black forever.
    master: bool,
    /// One-shot throttle for the drmSetMaster-failure errno log (the retry runs ~60x/s so it
    /// must never spam). Reset to false when master is acquired so a later loss re-logs. This
    /// surfaces WHY the kernel refuses master (EACCES = not the seat's active session / another
    /// holder; EBUSY = a pending master; EPERM = capability) -- the datum the bare "Unable to
    /// become drm master" warning omits, needed to actually diagnose the real-HW block.
    master_err_logged: bool,
}

/// What the render tick should do with one CRTC after attempting to present a frame.
/// PURE policy value (no Smithay types) so the flip-error→action decision is unit-
/// testable on the dev box (#186) — the `FrameError` → `FlipOutcome` adapter that
/// feeds it (`classify_frame_error`) is the only Smithay-touching, CI-compiled half.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum FrameAction {
    /// The flip was queued (or there was nothing to draw): leave the CRTC alone, the
    /// vblank will pace the next frame.
    Presented,
    /// A transient error (swapchain exhausted, a flip the kernel refused with EBUSY/
    /// EACCES, an empty frame): skip THIS frame, mark the output for redraw, retry on
    /// the next tick. The compositor stays alive (the never-fail floor).
    Reschedule,
    /// The DRM device is inactive (VT-switched away / asleep): hold off rendering to it
    /// until the session reactivates. Not an error — just don't flip to a dark CRTC.
    DeviceInactive,
}

/// A renderer-agnostic classification of a DRM flip/commit/render failure. Mapped from
/// Smithay's `FrameError`/`RenderFrameError` by `classify_frame_error`; consumed by the
/// PURE `flip_action`. The split keeps the policy (which is what we actually want to
/// test) free of Smithay types so it compiles + runs under the default `cargo test`.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum FlipOutcome {
    /// The device is not currently the DRM master (VT switch / suspend).
    DeviceInactive,
    /// A transient, retryable failure: the swapchain is momentarily out of buffers, the
    /// kernel refused the atomic commit/flip (EBUSY/EACCES/EINVAL — a busy CRTC, a seat
    /// hiccup), or the frame turned out empty. Retry next tick.
    Transient,
    /// A hard renderer/import/framebuffer failure (e.g. lost GL context analogue, no
    /// usable plane format). Still must NOT kill the compositor — we degrade by skipping
    /// the frame and retrying; if it persists the supervisor drops a tier.
    Fatal,
}

/// PURE: map a DRM present outcome to the render tick's next action. A flip error is a
/// logged, retried frame — NEVER a process death (#186). Every non-success outcome
/// degrades to `Reschedule` (retry the frame next tick) except an inactive device,
/// which is a deliberate "don't paint a dark CRTC" hold, not a failure. Unit-tested on
/// the dev box (no Smithay types in the signature).
fn flip_action(outcome: Result<(), FlipOutcome>) -> FrameAction {
    match outcome {
        Ok(()) => FrameAction::Presented,
        Err(FlipOutcome::DeviceInactive) => FrameAction::DeviceInactive,
        // Transient AND fatal both degrade-not-die: skip + retry. The distinction is
        // only for logging severity at the call site; neither ever aborts the loop.
        Err(FlipOutcome::Transient | FlipOutcome::Fatal) => FrameAction::Reschedule,
    }
}

/// The Smithay-touching half (#186): collapse a `FrameError` (from `queue_frame`) into the
/// renderer-agnostic `FlipOutcome` the pure `flip_action` understands. `DeviceInactive`
/// (VT switch / suspend) is held; an `Access` ioctl failure (EBUSY/EACCES/EINVAL — the
/// "permission denied" / "resource busy" the real laptop boot hit), `DrmMasterFailed`,
/// `TestFailed`, an exhausted swapchain, or an empty frame are all TRANSIENT → retried;
/// anything else (no usable format / framebuffer) is `Fatal` but STILL degrades (skip +
/// retry), never aborts. This is the single chokepoint that turns "a flip ioctl returned
/// an error" into "log it + retry the frame" instead of a `panic!`/`.unwrap()` death.
fn classify_frame_error<A, B, F>(err: &FrameError<A, B, F>) -> FlipOutcome
where
    A: std::error::Error + Send + Sync + 'static,
    B: std::error::Error + Send + Sync + 'static,
    F: std::error::Error + Send + Sync + 'static,
{
    use smithay::backend::drm::DrmError;
    match err {
        FrameError::DrmError(DrmError::DeviceInactive) => FlipOutcome::DeviceInactive,
        // EBUSY/EACCES/EINVAL on the atomic commit/page-flip, lost DRM master, or a
        // failed atomic test — all transient seat/CRTC hiccups: retry the frame.
        FrameError::DrmError(DrmError::Access(_))
        | FrameError::DrmError(DrmError::DrmMasterFailed)
        | FrameError::DrmError(DrmError::TestFailed(_)) => FlipOutcome::Transient,
        // The swapchain is momentarily exhausted (we owe a `frame_submitted`) or there
        // was nothing to flip — both clear on a subsequent tick.
        FrameError::NoFreeSlotsError | FrameError::EmptyFrame => FlipOutcome::Transient,
        // Any other DRM error or a hard format/framebuffer failure: degrade (skip +
        // retry); a persistent one is what makes the supervisor drop a tier.
        _ => FlipOutcome::Fatal,
    }
}

/// The operator escape-hatch override for the DRM node path (anvil's `ANVIL_DRM_DEVICE`
/// analogue). PURE: reads `HART_COMP_DRM_DEVICE` and returns `Some(path)` only when the
/// var is set AND non-empty — an empty override is treated as unset so a stray
/// `HART_COMP_DRM_DEVICE=` in the unit environment does not force a bogus node. Extracted
/// from `resolve_primary_node` so the override precedence is unit-testable without a GPU.
fn drm_device_override() -> Option<String> {
    std::env::var("HART_COMP_DRM_DEVICE")
        .ok()
        .filter(|v| !v.is_empty())
}

/// Resolve the primary GPU's DRM node — `HART_COMP_DRM_DEVICE` overrides (the same
/// operator escape hatch anvil's `ANVIL_DRM_DEVICE` gives), else `primary_gpu(seat)`.
/// Returns an error (NOT a panic) when there is no GPU, so the supervisor can drop a
/// tier instead of the process aborting (the never-fail posture).
fn resolve_primary_node(seat: &str) -> Result<DrmNode, Box<dyn std::error::Error>> {
    if let Some(var) = drm_device_override() {
        return DrmNode::from_path(&var)
            .map_err(|e| format!("HART_COMP_DRM_DEVICE={var} is not a DRM node: {e}").into());
    }
    let path = primary_gpu(seat)
        .map_err(|e| format!("primary_gpu({seat}) failed: {e}"))?
        .ok_or("no primary GPU on this seat (no /dev/dri/card*?)")?;
    DrmNode::from_path(&path).map_err(|e| format!("primary GPU {path:?} is not a DRM node: {e}").into())
}

/// THE compositor on real hardware: bring up DRM/KMS scanout + a libinput seat, mount
/// the glass-shell layer surface, and paint the pixman software floor. The DRM twin of
/// `winit::run_winit`. Returns an error on any boot failure (no DRM device, no GPU, a
/// session that cannot be acquired) so the B4 supervisor drops to sway/cage — the
/// screen is NEVER left blank waiting on an unbringable Tier-1.
pub fn run_udev(cfg: &BootConfig) -> Result<(), Box<dyn std::error::Error>> {
    // The render-path decision is shared with the skeleton + winit. The PixmanRenderer is
    // ALWAYS the renderer of record (the MANDATORY never-fail software floor); when the
    // boot-time GPU probe verdict (`/run/hart/gpu-render`) authorises Hardware AND the
    // GLES renderer actually initialises below, the compositor GPU-composites through it
    // and keeps pixman as the fallback on ANY GLES fault. `--force-software` forces the
    // floor by construction (select_render_path returns Software → want_gles false).
    let want_gles = matches!(select_render_path(cfg), RenderPath::Hardware);

    // 1. The calloop loop + our OWN server Display. The Display is owned here and
    //    dispatched DIRECTLY each iteration (NOT inserted as a calloop Generic source) —
    //    avoiding the `unsafe { display.get_mut() }` anvil's Generic pattern needs, which
    //    the crate's `#![deny(unsafe_code)]` rejects. The same safe pattern winit.rs uses.
    let mut event_loop: EventLoop<State> = EventLoop::try_new()?;
    let mut display: Display<State> = Display::new()?;
    let dh = display.handle();

    // 2. The libseat session (DRM master + input device open via logind/seatd). On a box
    //    with no seat (WSL/dev) this fails → honest error → supervisor drops a tier.
    let (session, notifier) = LibSeatSession::new()
        .map_err(|e| format!("LibSeatSession::new failed (no seat/logind/seatd — expected on WSL/dev): {e}"))?;
    let seat_name = session.seat();

    // 3. The primary GPU's DRM node (operator-overridable via HART_COMP_DRM_DEVICE).
    let primary_node = resolve_primary_node(&seat_name)?;
    info!(node = %primary_node, seat = %seat_name, "HART-comp DRM: using primary GPU");

    // 4. The protocol globals (the SAME minimal set winit.rs constructs) — what a
    //    shm/xdg/layer-shell client (the glass shell) needs to map + paint.
    let compositor_state = CompositorState::new::<State>(&dh);
    let xdg_shell_state = XdgShellState::new::<State>(&dh);
    let shm_state = ShmState::new::<State>(&dh, vec![]);
    let output_manager_state = OutputManagerState::new_with_xdg_output::<State>(&dh);
    let layer_shell_state = WlrLayerShellState::new::<State>(&dh);
    let data_device_state = DataDeviceState::new::<State>(&dh);
    let xdg_decoration_state = XdgDecorationState::new::<State>(&dh);
    let foreign_toplevel_state = ForeignToplevelListState::new::<State>(&dh);
    let xwayland_shell_state = XWaylandShellState::new::<State>(&dh);
    let mut seat_state = SeatState::<State>::new();
    let mut seat: Seat<State> = seat_state.new_wl_seat(&dh, "hart-seat");
    // Default::default() would look like it honours XKB_DEFAULT_*, but the empty
    // options string it produces makes libxkbcommon skip the environment entirely
    // (proved on real HW by reading the served keymap out of /memfd:smithay-keymap
    // -- see shared::XkbEnv). Plumb the environment through ourselves.
    let xkb_env = crate::shared::XkbEnv::from_env();
    let keyboard = seat.add_keyboard(xkb_env.config(), 200, 25)?;
    let pointer = seat.add_pointer();

    // 5. The software-floor renderer (pixman) — the MANDATORY never-fail paint path.
    let renderer = PixmanRenderer::new()?;

    // 6. A placeholder output — replaced the moment a connector is scanned. A real
    //    output is created per-connector in `connector_connected` below; this keeps the
    //    State's `output` field non-Option (the layer-shell mount + render math read it).
    //    Its mode is a safe 1080p default until the real connector's preferred mode lands.
    let output = Output::new(
        "HART-DRM".to_string(),
        PhysicalProperties {
            size: (0, 0).into(),
            subpixel: Subpixel::Unknown,
            make: "HART".into(),
            model: "DRM".into(),
            serial_number: "0".into(),
        },
    );
    let _output_global = output.create_global::<State>(&dh);
    let boot_mode = WlMode { size: (1920, 1080).into(), refresh: 60_000 };
    output.change_current_state(Some(boot_mode), Some(Transform::Normal), None, Some((0, 0).into()));
    output.set_preferred(boot_mode);

    let mut space: Space<Window> = Space::default();
    space.map_output(&output, (0, 0));

    // M8 — bake the default-arrow cursor once + the killswitch black buffer, the SAME
    // dependency-free RGBA fallback the winit backend uses (so a cursor is visible on the
    // pixman floor with no xcursor theme, and the screen-kill draw is one cheap solid).
    let (cur_rgba, cur_w, cur_h, cur_hotspot) = comp_core::bake_default_cursor();
    let cursor_buffer = smithay::backend::renderer::element::memory::MemoryRenderBuffer::from_slice(
        &cur_rgba,
        Fourcc::Argb8888,
        (cur_w, cur_h),
        1,
        Transform::Normal,
        None,
    );
    let black_buffer =
        smithay::backend::renderer::element::solid::SolidColorBuffer::new((1920, 1080), [0.0, 0.0, 0.0, 1.0]);

    let mut state = State {
        dh: dh.clone(),
        loop_handle: event_loop.handle(),
        windows: WindowRegistry::new(),
        pending: Vec::new(),
        compositor_state,
        xdg_shell_state,
        xdg_decoration_state,
        foreign_toplevel_state,
        seat_state,
        seat,
        xwayland_shell_state,
        shm_state,
        output_manager_state,
        layer_shell_state,
        data_device_state,
        keyboard,
        pointer,
        output: output.clone(),
        space,
        renderer,
        xwm: None,
        running: true,
        // Wire the WindowRecord-typed summon/foreign sink to a tracing log (the
        // socket-based com.hart.Compositor IPC fan-out is `state.ipc` below). Every
        // map/unmap emits a structured event line here too.
        emit_ipc_event: Box::new(|kind, rec| {
            info!(event = kind, handle = rec.handle.as_str(), "compositor.window_event");
        }),
        // M8 full-desktop WM state (the SAME field set winit::State holds).
        next_window_loc: (32, 32).into(),
        active_workspace: 0,
        hidden_windows: Vec::new(),
        desktop_shown: true,
        suppressed_keys: Vec::new(),
        cursor_status: smithay::input::pointer::CursorImageStatus::default_named(),
        cursor_buffer,
        cursor_hotspot: cur_hotspot,
        ws_switch_at: None,
        capture_blocked: false,
        black_buffer,
        // NATIVE SHELL M1 — empty until the first frame composes the backdrop at
        // the output's real mode (the 1920x1080 guess above is only the killswitch
        // solid's initial size, and the bloom must match the ACTUAL scanout).
        bloom: Default::default(),
        // NATIVE SHELL M2 — composed on the first frame at the real output size.
        orb: Default::default(),
        ipc: crate::ipc::IpcState::default(),
        // F1 (#166) — no flips in flight at boot; the VBlank source populates this.
        vblank_completed: std::collections::HashSet::new(),
        // No libseat session activate/pause pending at boot; the notifier parks them here and
        // the proactive activate before the loop primes the unprivileged-startup recovery.
        pending_session_activate: None,
        // #137 — the frame-budget repaint scheduler starts DIRTY, so the splash + glass-shell
        // first frame always paints; thereafter the render tick paints on damage + a heartbeat.
        repaint: crate::RepaintScheduler::new(),
    };

    // 7. The per-device backend table (one entry per opened GPU). Held OUTSIDE State so
    //    the DRM/udev calloop sources (which take `&mut State`) don't alias it — they
    //    reach it via the loop's shared data tuple `(State, DeviceMap)`.
    let mut devices: HashMap<DrmNode, DeviceData> = HashMap::new();

    // 8. The udev backend (device hotplug) — enumerate GPUs present at boot + watch for
    //    add/remove. Best-effort: a udev failure is fatal to the DRM path (no devices to
    //    scan) → honest error → supervisor drops a tier.
    let udev_backend = UdevBackend::new(&seat_name)
        .map_err(|e| format!("UdevBackend::new({seat_name}) failed: {e}"))?;

    // 9. libinput — the evdev seat (keyboard/pointer/touch). Routed into the SAME seat
    //    handles the winit backend uses; `State::process_input_event` is the shared body.
    let mut libinput_context =
        Libinput::new_with_udev::<LibinputSessionInterface<LibSeatSession>>(session.clone().into());
    libinput_context
        .udev_assign_seat(&seat_name)
        .map_err(|()| format!("libinput udev_assign_seat({seat_name}) failed"))?;
    let libinput_backend = LibinputInputBackend::new(libinput_context.clone());

    // 10. Open every GPU present at boot (the primary first so it always has a node).
    for (device_id, path) in udev_backend.device_list() {
        match DrmNode::from_dev_id(device_id) {
            Ok(node) => {
                if let Err(e) = device_added(&mut state, &mut devices, &session, node, path) {
                    warn!(node = %node, ?e, "HART-comp DRM: skipping device");
                }
            }
            Err(e) => warn!(device_id, ?e, "HART-comp DRM: bad DRM node id"),
        }
    }
    // Advertise the renderer's shm formats now that a device/renderer exists.
    let shm_formats: Vec<_> = state.renderer.dmabuf_formats().iter().map(|f| f.code).collect();
    let _ = shm_formats; // pixman's mandatory Argb/Xrgb already advertised via ShmState::new

    // PART 3 of the GPU lever — the GLES2 GPU renderer. When the boot-time GPU probe
    // verdict authorised Hardware (`want_gles`), build ONE `GlesRenderer` on the PRIMARY
    // node's GBM device (an EGL platform display from that GBM device + a configless GLES
    // context). This is a single-GPU bring-up by design: the target laptop has exactly one
    // usable iGPU (the Intel i915; the GeForce 940MX is nouveau-blacklisted), so there is
    // no anvil-style GpuManager/MultiRenderer — one renderer, one EGL context, on the
    // calloop thread. The PixmanRenderer software floor (`state.renderer`) is UNTOUCHED and
    // stays the renderer of record: if the verdict is software, the primary node is somehow
    // absent, OR GLES init fails for ANY reason, `gles` stays `None` and every frame paints
    // via pixman exactly as before (degrade-not-die — a GLES failure is invisible, never a
    // black screen). The DrmCompositors were all built with pixman/LINEAR formats, which a
    // GLES renderer can also render into, so the SAME compositors scan out under either
    // renderer and a runtime GLES→pixman demotion is always bind-safe.
    let mut gles: Option<GlesRenderer> = None;
    if want_gles {
        match devices.get(&primary_node) {
            // build_gles_renderer can PANIC, not just return Err: smithay loads
            // libEGL.so.1 / libGLESv2.so.2 via `libloading …expect(…)`, which PANICS
            // when the lib is absent from the runtime search path (real-HW 2026-07-09:
            // rc=134 abort → the supervisor dropped to sway, because the plain
            // `match … Err(e)` below can only catch a Result, never a panic). Under
            // panic="unwind" (Cargo.toml) catch_unwind converts that panic into an
            // Err, so ANY EGL/GLES init failure — Result OR third-party panic —
            // degrades to the pixman software floor IN-PROCESS instead of crashing to
            // a lower tier. This is the true degrade-not-die (the LD_LIBRARY_PATH fix
            // in hart-comp.nix makes the native path SUCCEED on a good GPU; this makes
            // a still-broken EGL fall back silently rather than abort). AssertUnwindSafe
            // is sound: on a caught panic we discard the renderer and keep the pixman
            // renderer of record, mutating no shared state across the boundary.
            Some(dev) => match std::panic::catch_unwind(
                std::panic::AssertUnwindSafe(|| build_gles_renderer(&dev.gbm))
            ) {
                Ok(Ok(renderer)) => {
                    info!(node = %primary_node, "HART-comp DRM: GLES GPU renderer initialised on the primary node — GPU-compositing (pixman software floor kept as the fallback)");
                    gles = Some(renderer);
                }
                Ok(Err(e)) => warn!(node = %primary_node, ?e, "HART-comp DRM: GLES init failed — staying on the pixman software floor (never-blank)"),
                Err(_panic) => warn!(node = %primary_node, "HART-comp DRM: GLES init PANICKED (e.g. libEGL/libGLESv2 not loadable) — caught, staying on the pixman software floor (degrade-not-die, never a lower-tier drop)"),
            },
            None => warn!(node = %primary_node, "HART-comp DRM: primary node has no opened device — staying on the pixman software floor"),
        }
    } else {
        info!("HART-comp DRM: GPU probe did not authorise hardware — rendering on the pixman software floor");
    }

    // 11. Bind the calloop event sources.
    //   (a) libinput → State::process_input_event (the shared input router). As each device
    //       appears, enable touchpad TAP-TO-CLICK (fix (c)): libinput defaults tap OFF, so a light
    //       tap on a laptop touchpad registers NOTHING (only a physical button press clicks) — the
    //       real-HW "touchpad taps not registering". We enable BOTH tap-to-click (a 1/2/3-finger
    //       tap synthesises a left/right/middle button) AND tap-and-drag (tap-then-slide drags, so
    //       a tap behaves like a full click+hold for selecting / moving), the natural laptop UX. A
    //       non-touchpad reports tap_finger_count 0 and is left untouched; every set is best-effort
    //       (a device that does not support the knob returns Err, ignored — never fatal, the never-
    //       fail floor). Tier-2 sway sets the same via its libinput config; the cage Tier-3 floor
    //       cannot configure libinput, so this is hart-comp's own. The setters take &mut self,
    //       hence `mut event`.
    event_loop
        .handle()
        .insert_source(libinput_backend, move |mut event, _, state: &mut State| {
            if let smithay::backend::input::InputEvent::DeviceAdded { device } = &mut event {
                // Only a touchpad reports a non-zero tap finger count; gate on it so we never
                // poke a mouse/keyboard with a touchpad-only knob.
                if device.config_tap_finger_count() > 0 {
                    if let Err(err) = device.config_tap_set_enabled(true) {
                        debug!(?err, "libinput: touchpad tap-to-click enable refused (best-effort; taps may not click)");
                    }
                    if let Err(err) = device.config_tap_set_drag_enabled(true) {
                        debug!(?err, "libinput: touchpad tap-and-drag enable refused (best-effort)");
                    }
                }
            }
            state.process_input_event(event);
            // #137 — an input event moved the cursor / changed focus / clicked: the pointer
            // (a composited software cursor) and any focus/raise must re-paint. Mark the
            // frame-budget scheduler damaged so the next render tick composites, keeping the
            // cursor responsive even when the desktop is otherwise idle.
            state.repaint.mark_damaged();
        })
        .map_err(|e| format!("insert libinput source failed: {e}"))?;

    //   (b) the session notifier → activate/pause the DRM devices on VT switch / suspend /
    //       resume. The calloop closure gets ONLY `&mut State`, never the per-device DRM
    //       table it must act on — so, exactly like the VBlank source hands a CRTC to the
    //       render tick via `vblank_completed`, it PARKS the request in
    //       `state.pending_session_activate` and the loop body (which holds `devices`)
    //       applies it via `apply_pending_session`. This is REQUIRED, not cosmetic: on the
    //       pinned rev `render_frame`/`queue_frame` hard-gate on `surface.is_active()` and
    //       there is NO auto-recovery, so a device left unprivileged (the real-HW "Unable to
    //       become drm master" startup race) or paused on a VT-away stays black until we
    //       explicitly re-acquire master.
    //       INPUT across a VT switch / suspend (#134 LATENT, not fresh-boot): a libseat
    //       PauseSession REVOKES the evdev fds the kernel had handed libinput; libinput then
    //       reads EBADF forever and delivers NOTHING after a resume unless it is explicitly
    //       cycled. So on PauseSession we `libinput_context.suspend()` (close the devices)
    //       and on ActivateSession `libinput_context.resume()` (re-open + re-enumerate every
    //       device) — exactly anvil's udev seat handling. Without this, the FIRST VT switch
    //       (Ctrl+Alt+F2 → recovery TTY → back) or lid-close/suspend-resume on the Lenovo
    //       would leave the pointer + keyboard dead even though the fresh-boot path now works.
    //       The notifier owns the original `libinput_context` (the LibinputInputBackend holds
    //       a clone; both refer to the SAME context, so cycling either side affects delivery).
    let mut session_libinput = libinput_context;
    event_loop
        .handle()
        .insert_source(notifier, move |event, &mut (), state: &mut State| match event {
            SessionEvent::PauseSession => {
                info!("HART-comp DRM: session paused (VT switch / sleep) — suspending libinput + dropping DRM master");
                session_libinput.suspend();
                state.pending_session_activate = Some(false);
            }
            SessionEvent::ActivateSession => {
                info!("HART-comp DRM: session resumed — resuming libinput + re-acquiring DRM master");
                if session_libinput.resume().is_err() {
                    // Degrade, never die (#186 floor): a failed libinput resume is logged and
                    // the next ActivateSession edge retries. We still re-take DRM master so the
                    // screen comes back; a wedged seat is observable via the input-alive beacon.
                    warn!("HART-comp DRM: libinput resume failed — input may be degraded until the next session-activate");
                }
                state.pending_session_activate = Some(true);
            }
        })
        .map_err(|e| format!("insert session notifier failed: {e}"))?;

    //   (c) the udev backend → device add/change/remove (hotplug). The device table is
    //       threaded via the loop data (see step 13). Here we only LOG; the actual table
    //       mutation for hotplug is a Stage-B enrichment (boot-time enumeration in step
    //       10 already brings up every display present at boot — the Stage-A floor).
    event_loop
        .handle()
        .insert_source(udev_backend, move |event, _, _state: &mut State| match event {
            UdevEvent::Added { device_id, .. } => info!(device_id, "HART-comp DRM: device added (hotplug — re-scan on next boot)"),
            UdevEvent::Changed { device_id } => info!(device_id, "HART-comp DRM: device changed"),
            UdevEvent::Removed { device_id } => info!(device_id, "HART-comp DRM: device removed"),
        })
        .map_err(|e| format!("insert udev source failed: {e}"))?;

    // 12. Our OWN listening socket (wayland-N) — the display the glass-shell client (and
    //     any app) connects to. Published to the environment so the session launcher's
    //     glass-shell child binds to US.
    let socket = ListeningSocketSource::new_auto()?;
    let socket_name = socket.socket_name().to_string_lossy().into_owned();
    event_loop
        .handle()
        .insert_source(socket, move |client_stream, _, state: &mut State| {
            if let Err(err) = state
                .dh
                .insert_client(client_stream, Arc::new(ClientState::default()))
            {
                warn!(?err, "HART-comp DRM: failed to add wayland client");
            }
        })
        .map_err(|e| format!("insert socket source failed: {e}"))?;
    std::env::set_var("WAYLAND_DISPLAY", &socket_name);
    info!(socket = %socket_name, "HART-comp DRM: listening on its own wayland socket");

    // 12b. XWayland (Wine / legacy X11), nested in OUR display — the SAME spawn the winit
    //      backend uses, reused here so the DRM path surfaces X11 toplevels too. Best-
    //      effort: a spawn failure leaves xwm=None (Wayland-native clients unaffected).
    spawn_xwayland(&dh, &event_loop.handle());

    // 12c. M8 — bind the com.hart.Compositor IPC server (the SAME framed-JSON Unix-socket
    //      twin the winit backend serves), so an agent arranges REAL windows on real
    //      hardware too — the MOAT on the DRM path. `ipc::start_ipc` is generic over the
    //      backend State (via `comp_core::CompState`), so this is ONE server, not a
    //      winit-only path. Best-effort: a bind failure leaves the compositor running.
    let _ipc_socket = crate::ipc::start_ipc(&event_loop.handle());

    // 13. THE LOOP. We carry the per-device table in a tuple with State so the VBlank
    //     re-render can reach the DrmCompositor surfaces. The DRM `notifier`/VBlank source
    //     was inserted as part of `device_added` keyed on the node, calling `render_node`.
    //
    //     Kick off the first frame on every surface so the splash + glass shell paint
    //     immediately (the DRM page-flip cadence is then self-sustaining via VBlank).
    //
    //     But first re-acquire DRM master: a device may have come up UNPRIVILEGED in a startup VT
    //     race (the real-HW "Unable to become drm master, assuming unprivileged mode" log — smithay
    //     grabs master ONCE inside DrmDevice::new and freezes its `privileged` flag false on
    //     failure). Its session `active` flag is TRUE, so `render_frame` proceeds, but every
    //     page-flip ioctl returns EACCES and #186 retries it FOREVER unless we re-take master — a
    //     black, SETTLED Tier-1 the paint watchdog can't catch (the shell page still loads, so the
    //     shell-ready marker still fires). We can't recover via `activate()` (it skips
    //     acquire_master_lock on a frozen-unprivileged device); instead `apply_pending_session` ->
    //     `acquire_drm_master` issues `drmSetMaster` DIRECTLY on the fd, which the kernel grants
    //     once fbcon has dropped master. If it isn't grantable yet, the per-tick `acquire_drm_master`
    //     retry inside `render_all` keeps trying every frame (no ActivateSession edge needed on a
    //     fresh boot) until it lands — well inside the supervisor's 45s first-paint watchdog.
    state.pending_session_activate = Some(true);
    apply_pending_session(&mut state, &mut devices);
    render_all(&mut state, &mut devices, &mut gles);

    info!(socket = %socket_name, "HART-comp DRM compositor initialized — entering the loop (real-HW scanout on the pixman floor)");

    // Monotonic base for the frame-callback timestamps sent below. This backend used to
    // send a CONSTANT 0, which is wrong per the wl_callback protocol (see the note there,
    // including the correction that it was NOT the cause of the desktop freeze).
    // winit.rs has always derived its timestamp from `state.start_time`; the udev State
    // has no such field, so the base is captured here instead.
    let frame_clock_base = std::time::Instant::now();

    while state.running {
        // Dispatch calloop sources (libinput, session, udev, the client socket, and the
        // per-device DRM VBlank sources) + a 16ms housekeeping tick, then the Display.
        if let Err(err) = event_loop
            .dispatch(Some(Duration::from_millis(16)), &mut state)
        {
            error!(?err, "HART-comp DRM: event loop dispatch failed; exiting the loop");
            state.running = false;
            continue;
        }

        // Apply any libseat session activate/pause the notifier parked during this dispatch
        // (VT switch / suspend / resume): re-acquire or drop DRM master on every device
        // BEFORE we render to it, so a reactivated CRTC paints THIS tick and we never flip to
        // a device that just went away. No-op when nothing is parked.
        apply_pending_session(&mut state, &mut devices);

        // Re-render every surface each tick (the pixman floor has no damage-driven
        // scheduling here; a 60Hz repaint is the simple never-fail cadence). On a real
        // box the VBlank source paces the actual flips; this tick just keeps the frame
        // fresh + drains client frame callbacks so clients draw their next frame. `gles`
        // is the GPU renderer when the probe authorised it (else None = pixman floor);
        // render_all picks GLES when present and self-demotes to pixman on a GLES fault.
        render_all(&mut state, &mut devices, &mut gles);

        // M8 — settle finished effects: clear the workspace-switch crossfade clock once it
        // completes so it does not re-evaluate forever (the 60Hz tick PLAYS an in-flight
        // fade frame-by-frame; map-in fades self-settle past FADE_IN_MS). The SAME tidy-up
        // the winit loop does, so the DRM desktop's crossfade behaves identically.
        if !comp_core::effects_animating(&state) {
            state.ws_switch_at = None;
        }

        // Send frame callbacks so clients (the glass shell) draw their next frame.
        //
        // A REAL MONOTONIC TIMESTAMP, NOT 0 (real-HW 2026-08-19). This line used to read
        // `let now_ms = 0u32;` with the comment "monotonic ms is unused by the shell; 0 is
        // a valid now". That assumption is wrong on its own terms, and this fixes it.
        //
        // CORRECTION, so the next reader is not misled the way I was: the original commit
        // message for this change (79391a6) claimed it was THE fix for the orb-hover /
        // click-drag desktop freeze. IT IS NOT. With this change in place and verified
        // live on the box, the freeze still reproduces on the first orb click. It was
        // later reproduced with the GStreamer audio path removed entirely, and on wholly
        // stock code, so the freeze has a different cause that is still open. What
        // remains true of this line is only the protocol point below.
        //
        // wl_callback.done carries the frame time in MILLISECONDS, and GTK4's Wayland
        // backend feeds it into its frame clock, which derives the refresh interval and
        // the predicted presentation time from successive values. A constant is simply
        // wrong to send, whatever else is or is not true about it.
        //
        // winit.rs, the dev/VM backend, has ALWAYS sent
        // `state.start_time.elapsed().as_millis()`; only this hardware path sent 0. The
        // udev State has no start_time field, so the base is captured at the loop above.
        let now_ms = frame_clock_base.elapsed().as_millis() as u32;
        let surfaces: Vec<_> = state
            .space
            .elements()
            .filter_map(|w| w.wl_surface().map(|s| s.into_owned()))
            .collect();
        for s in &surfaces {
            send_frame_callbacks(s, now_ms);
        }
        {
            let map = layer_map_for_output(&state.output);
            for layer in map.layers() {
                send_frame_callbacks(layer.wl_surface(), now_ms);
            }
        }

        // Sweep the no-phantom-window timeout (the Phase-5 guarantee on the DRM path):
        // a `SummonApp` whose toplevel never maps within `SUMMON_MAP_TIMEOUT` resolves
        // to an HONEST timeout — NEVER a fabricated handle. The pure `is_timed_out_at`
        // decides; this only feeds it the real clock each tick + reports each timed-out
        // manifest to the IPC sink (the brain's awaiting SummonApp future is then
        // completed with error.code="timeout"). This is the same sweep the winit
        // backend's loop owns — the DRM Tier-1 must not silently drop it, or a Wine
        // launch that returned 0 but mapped nothing would hang the summon forever.
        for manifest_id in state.expire_summons(std::time::Instant::now()) {
            warn!(manifest = %manifest_id, "HART-comp DRM: SummonApp timed out (no toplevel mapped) — honest timeout, no handle");
        }

        // Dispatch the Wayland clients (process their requests into our handlers), refresh
        // the space, and flush. Display dispatched DIRECTLY (no unsafe Generic source).
        if let Err(err) = display.dispatch_clients(&mut state) {
            warn!(?err, "HART-comp DRM: failed to dispatch wayland clients");
        }
        state.space.refresh();
        if let Err(err) = display.flush_clients() {
            warn!(?err, "HART-comp DRM: failed to flush clients");
        }
    }

    info!("HART-comp DRM compositor exited cleanly");
    Ok(())
}

/// Open a DRM device, build its GBM allocator + framebuffer exporter, scan its
/// connectors, and bring up a DrmCompositor per connected display. Mirrors anvil's
/// `device_added` + `connector_connected`, trimmed to the pixman software floor (no
/// EGL/GpuManager). Inserts the per-device DRM event (VBlank) source into the loop.
fn device_added(
    state: &mut State,
    devices: &mut HashMap<DrmNode, DeviceData>,
    session: &LibSeatSession,
    node: DrmNode,
    path: &Path,
) -> Result<(), Box<dyn std::error::Error>> {
    // Open the device through the session (so it is DRM-master + survives VT switches).
    let fd = session
        .clone()
        .open(path, OFlags::RDWR | OFlags::CLOEXEC | OFlags::NOCTTY | OFlags::NONBLOCK)
        .map_err(|e| format!("session.open({path:?}) failed: {e}"))?;
    let fd = DrmDeviceFd::new(DeviceFd::from(fd));

    let (mut drm, drm_notifier) = DrmDevice::new(fd.clone(), true)
        .map_err(|e| format!("DrmDevice::new failed: {e}"))?;
    // Keep our OWN clone of the fd (a third Arc handle alongside the DrmDevice + GbmDevice)
    // so `acquire_drm_master` can re-issue `drmSetMaster` on it directly — the construction
    // grab inside `DrmDevice::new` above may have lost the boot-VT master race, and that is the
    // only handle through which we can recover (smithay froze the device's `privileged` flag).
    let master_fd = fd.clone();
    let gbm = GbmDevice::new(fd).map_err(|e| format!("GbmDevice::new failed: {e}"))?;

    // The scanout allocator (RENDERING|SCANOUT so the buffer can be both rendered into by
    // pixman AND page-flipped to the CRTC) + the framebuffer exporter (turns a GBM bo into
    // a DRM framebuffer for the flip). Both clone the GBM device; it is held in DeviceData.
    let allocator = GbmAllocator::new(gbm.clone(), GbmBufferFlags::RENDERING | GbmBufferFlags::SCANOUT);
    let exporter = GbmFramebufferExporter::new(gbm.clone(), node.into());

    let mut surfaces: HashMap<crtc::Handle, SurfaceData> = HashMap::new();

    // Scan connectors + pick a CRTC for each connected one, then build a DrmSurface +
    // DrmCompositor. Uses the raw `drm` Device trait (no smithay-drm-extras dep) — the
    // simple "first compatible CRTC per connected connector" never-fail walk.
    let res = drm
        .resource_handles()
        .map_err(|e| format!("drm.resource_handles failed: {e}"))?;
    for conn_handle in res.connectors() {
        let conn = match drm.get_connector(*conn_handle, false) {
            Ok(c) => c,
            Err(err) => {
                warn!(?err, "HART-comp DRM: get_connector failed; skipping this connector");
                continue;
            }
        };
        if conn.state() != connector::State::Connected {
            continue;
        }
        // Pick the preferred mode (or the first), then a CRTC the connector's encoders
        // can drive that we have not already claimed.
        let mode = pick_mode(&conn);
        let mode = match mode {
            Some(m) => m,
            None => {
                warn!(connector = ?conn.interface(), "HART-comp DRM: connector has no modes");
                continue;
            }
        };
        let crtc = match pick_crtc(&drm, &res, &conn, &surfaces) {
            Some(c) => c,
            None => {
                warn!(connector = ?conn.interface(), "HART-comp DRM: no free CRTC for connector");
                continue;
            }
        };

        // Build the scanout surface for this CRTC + connector.
        let drm_surface: DrmSurface = match drm.create_surface(crtc, mode, &[conn.handle()]) {
            Ok(s) => s,
            Err(e) => {
                warn!(?e, "HART-comp DRM: create_surface failed");
                continue;
            }
        };

        // The connector's real output (replacing the boot placeholder so the glass shell
        // sees the true mode + physical size). Map it into the space.
        let (phys_w, phys_h) = conn.size().unwrap_or((0, 0));
        let output_name = format!("{}-{}", conn.interface().as_str(), conn.interface_id());
        let output = Output::new(
            output_name,
            PhysicalProperties {
                size: (phys_w as i32, phys_h as i32).into(),
                subpixel: conn.subpixel().into(),
                make: "HART".into(),
                model: "DRM".into(),
                serial_number: "0".into(),
            },
        );
        let _global = output.create_global::<State>(&state.dh);
        let wl_mode = WlMode::from(mode);
        output.set_preferred(wl_mode);
        output.change_current_state(Some(wl_mode), Some(Transform::Normal), None, Some((0, 0).into()));
        // Swap the State's output to the real one (Stage A drives a single display).
        state.space.unmap_output(&state.output);
        state.space.map_output(&output, (0, 0));
        state.output = output.clone();

        // The PixmanRenderer's dmabuf formats are the DrmCompositor `renderer_formats`.
        let renderer_formats = state.renderer.dmabuf_formats();

        // DIAGNOSTIC (2026-08-17): why Tier-1 can spin without ever painting.
        // PixmanRenderer::bind accepts ONLY DrmModifier::Linear — it returns
        // UnsupportedModifier for anything else. DrmCompositor picks its swapchain
        // modifier from plane_formats ∩ renderer_formats, and when the PLANE
        // advertises only the implicit modifier (Invalid — no IN_FORMATS blob) while
        // the renderer advertises only Linear, Smithay takes its own special case:
        //   "if a format supports explicit LINEAR (but no implicit Modifiers) and the
        //    other doesn't support any modifier, force Implicit. This should at least
        //    result in a working pipeline possibly with a linear buffer, but we cannot
        //    be sure."
        // It then allocates an Invalid-modifier buffer that its own PixmanRenderer
        // refuses, so render_frame fails EVERY tick — 199 identical
        // RenderFrame(UnsupportedModifier(Invalid)) faults in the VM run, no first
        // paint, and the session supervisor drops the tier at 45s. Adding Invalid to
        // renderer_formats cannot help: the plane offers only Invalid, so the
        // intersection is Invalid either way.
        // Smithay logs these sets at debug! only, which the session does not capture,
        // so record them here. If the plane really is implicit-only this is an
        // environment property (a real GPU advertises IN_FORMATS; qemu virtio-gpu may
        // not) rather than anything this module can choose differently.
        let plane_modifiers: Vec<_> = drm_surface
            .plane_info()
            .formats
            .iter()
            .map(|f| f.modifier)
            .collect();
        info!(
            ?plane_modifiers,
            renderer_formats = renderer_formats.iter().count(),
            "HART-comp DRM: primary-plane modifiers vs pixman renderer formats (pixman binds LINEAR only)"
        );

        let compositor = DrmCompositor::new(
            &output,
            drm_surface,
            None, // planes: let DrmCompositor query them
            allocator.clone(),
            exporter.clone(),
            COLOR_FORMATS.iter().copied(),
            renderer_formats,
            drm.cursor_size(),
            Some(gbm.clone()),
        )
        .map_err(|e| format!("DrmCompositor::new failed: {e}"))?;
        surfaces.insert(
            crtc,
            SurfaceData {
                compositor,
                awaiting_vblank: false,
                flip_queued_at: None,
                // Seeded at construction, NOT None: the beacon measures "how long since
                // anything reached this CRTC's screen", and a surface that never presents
                // AT ALL is the most serious version of that, not a case to stay silent
                // on. Seeding makes never-first-paint report after SILENT_FREEZE_AFTER
                // instead of being swallowed by an `unwrap_or(false)`.
                last_flip_at: Some(std::time::Instant::now()),
                last_stall_log_at: None,
            },
        );
        info!(crtc = ?crtc, "HART-comp DRM: output online (pixman scanout)");
    }

    // Insert the per-device DRM event source. On a VBlank the matching CRTC's flip has
    // completed scan-out: record the CRTC so the render tick can `frame_submitted()` it
    // (the table-holding half) IN VBLANK ORDER and unblock its next flip (F1, #166). The
    // calloop closure only gets `&mut State`, not the per-device `surfaces` table, so the
    // hand-off is this `vblank_completed` set — drained by `reap_completed_vblanks`. This
    // replaces the old unconditional per-tick `frame_submitted()`, which freed swapchain
    // buffers before the hardware had finished scanning them out (the torn-frame cause).
    state
        .loop_handle
        .insert_source(drm_notifier, move |event, _meta, state: &mut State| match event {
            DrmEvent::VBlank(crtc) => {
                state.vblank_completed.insert(crtc);
            }
            // A device-level DRM error must NOT abort the compositor (#186): log it and
            // keep running. A genuinely dead device stops producing vblanks, which the
            // paint watchdog / B4 supervisor observes and drops a tier on — the loop
            // itself never panics on a transient seat/DRM error event.
            DrmEvent::Error(err) => error!(?err, "HART-comp DRM: device error (kept alive — supervisor handles a dead device)"),
        })
        .map_err(|e| format!("insert DRM notifier failed: {e}"))?;

    devices.insert(
        node,
        DeviceData {
            drm,
            fd: master_fd,
            gbm,
            surfaces,
            // Master is confirmed lazily by the render tick's `acquire_drm_master` retry, not
            // assumed here: even if the construction grab succeeded we re-confirm on the first
            // tick (an idempotent `drmSetMaster`), so the latch reflects the kernel, not a guess.
            master: false,
            master_err_logged: false,
        },
    );
    Ok(())
}

/// Reap every vblank that landed since the last tick (F1, #166). Called from the render
/// tick BEFORE rendering: for each CRTC the VBlank source recorded in `vblank_completed`,
/// mark its flip submitted (`frame_submitted()` — frees the just-scanned-out swapchain
/// slot AND submits any frame that was held back while this flip was in flight) and clear
/// its `awaiting_vblank` gate so the tick may queue its NEXT frame. Reaping ONLY on the
/// real vblank (not unconditionally every tick) is the torn-frame fix: a buffer is freed
/// strictly after the hardware finished scanning it out, and frame N+1 is page-flipped
/// only after frame N's flip completed — never on top of an in-flight flip.
fn reap_completed_vblanks(state: &mut State, devices: &mut HashMap<DrmNode, DeviceData>) {
    if state.vblank_completed.is_empty() {
        return;
    }
    let completed: Vec<crtc::Handle> = state.vblank_completed.drain().collect();
    for crtc in completed {
        for device in devices.values_mut() {
            if let Some(surface) = device.surfaces.get_mut(&crtc) {
                // frame_submitted() can itself attempt the deferred submit (a flip), which
                // can fail on a transient seat/DRM hiccup — degrade, never panic (#186).
                match surface.compositor.frame_submitted() {
                    Ok(_) => {}
                    Err(err) => {
                        let outcome = classify_frame_error(&err);
                        match flip_action(Err(outcome)) {
                            FrameAction::DeviceInactive => {
                                info!(?crtc, "HART-comp DRM: frame_submitted on inactive device — holding");
                            }
                            _ => warn!(?err, ?crtc, "HART-comp DRM: frame_submitted failed — will retry next frame"),
                        }
                    }
                }
                // The in-flight flip for this CRTC is now retired; the tick may queue again.
                surface.awaiting_vblank = false;
                surface.flip_queued_at = None;
            }
        }
    }
    // #131 — a vblank was reaped, so a page-flip COMPLETED: a frame actually scanned out to
    // a CRTC. Emit the first-scanout beacon exactly once (best-effort, one atomic load in the
    // steady state). This is the compositor-side proof of paint the shell-ready marker cannot
    // give — it fires from the DISPLAY side, not a client buffer, so a black-but-healthy
    // Tier-1 (master lost / never flipped) never writes it and the supervisor can catch it.
    // `completed` is guaranteed non-empty here (the early return above), so a reap == a scanout.
    note_first_scanout_once(&FIRST_SCANOUT, true);
}

/// What to do about DRM master for one device this attempt. PURE policy (no Smithay types),
/// so the master-recovery state machine is unit-testable on the dev box with NO DRM hardware —
/// the exact gap investigation 2 called out ("validate the retry state machine; WSL cannot do
/// the live logind master grant"). The Smithay-touching adapter that feeds it is
/// `acquire_drm_master` (it issues the real `drmSetMaster` and reads its `Ok`/`Err`).
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum MasterStep {
    /// We already hold confirmed master — do nothing (do NOT re-issue `drmSetMaster` every tick).
    AlreadyHeld,
    /// We just took master on this attempt — latch it, sync smithay's `active` flag, repaint.
    Acquired,
    /// `drmSetMaster` was refused (the boot-VT's prior master has not dropped yet, or our session
    /// is not the seat's active master yet). Stay unmastered and retry on a later tick. This is
    /// the never-fail floor's "keep trying", NOT an abort — there is intentionally no `Fail`
    /// variant that could tear the compositor down.
    NotYet,
}

/// PURE: decide the master step from (do-we-already-hold-it, did-this-acquire-succeed). Extracted
/// so the recovery loop's branching is tested directly (a fake unprivileged-then-privileged device
/// is modelled by feeding `held=false` with `acquire_succeeded=false` then `true`, asserting the
/// loop goes `NotYet → Acquired` and NEVER aborts). The real `drmSetMaster` ioctl is the only part
/// that needs hardware; this decision is not.
fn master_step(held: bool, acquire_succeeded: bool) -> MasterStep {
    if held {
        MasterStep::AlreadyHeld
    } else if acquire_succeeded {
        MasterStep::Acquired
    } else {
        MasterStep::NotYet
    }
}

/// (Re)acquire DRM master for one device by issuing `drmSetMaster` DIRECTLY on its fd, and on a
/// fresh grab sync smithay's session `active` flag + force a clean repaint. Returns true once the
/// device holds confirmed master.
///
/// THIS is the recovery the old `activate()`-only path could NEVER do. On the pinned Smithay rev
/// (4784339) `DrmDeviceFd::new` calls `acquire_master_lock()` exactly once and freezes the private
/// `privileged` flag (`device/fd.rs`); `DrmDevice::activate()` only re-`acquire_master_lock`s when
/// that frozen flag is already true (`device/mod.rs`: `if self.device_fd().is_privileged()`). So a
/// device that lost the construction-time grab ("Unable to become drm master, assuming
/// unprivileged mode") has `privileged=false` FOREVER and `activate()` is a no-op for master —
/// every page-flip then returns EACCES and #186 retries it forever → a black, settled Tier-1.
/// `drmSetMaster` on the raw fd has no such gate: the kernel grants it the instant the boot-VT's
/// prior master (fbcon/simpledrm) has dropped and our logind session is the seat's active master.
/// Once the fd is the real kernel master, smithay's atomic commits succeed regardless of its
/// frozen `privileged` belief (that flag only governs whether smithay itself drops/re-takes master
/// on pause/activate/drop — which is why PAUSE below drops master explicitly).
///
/// Idempotent + cheap when already held (the `master` latch short-circuits BEFORE the ioctl). A
/// refusal is not logged per-tick (it would spam at 60Hz during the brief handoff) and is retried
/// next tick; it is NEVER fatal — a device that can truly never master simply never paints, and the
/// supervisor drops a tier on the missing first paint (the never-fail posture).
fn acquire_drm_master(device: &mut DeviceData) -> bool {
    // Short-circuit the steady state so we do not re-issue drmSetMaster ~60x/s once mastered.
    let acquired_now = if device.master {
        false
    } else {
        match device.fd.acquire_master_lock() {
            Ok(()) => true,
            Err(err) => {
                // Log the errno EXACTLY ONCE per unprivileged episode (this runs ~60x/s). This
                // names WHY the kernel refuses master on this hardware -- the missing datum behind
                // the bare "Unable to become drm master" warning. Re-armed on a later master loss.
                if !device.master_err_logged {
                    warn!(?err, "HART-comp DRM: drmSetMaster STILL refused (unprivileged) -- errno names the reason/holder; retrying every tick");
                    device.master_err_logged = true;
                }
                false
            }
        }
    };
    match master_step(device.master, acquired_now) {
        MasterStep::AlreadyHeld => true,
        MasterStep::Acquired => {
            device.master = true;
            device.master_err_logged = false; // re-arm the one-shot errno log for a future loss
            // The fd is now the real kernel master. Call activate(false) to set the SESSION
            // `active` flag (so render_frame/queue_frame do not short-circuit on DeviceInactive)
            // and reset connector/plane state for a clean first scanout. activate() may also try
            // its own acquire_master_lock if smithay thinks it is privileged — harmless (a second
            // drmSetMaster on an fd that already holds master is a no-op).
            if let Err(err) = device.drm.activate(false) {
                warn!(?err, "HART-comp DRM: activate() after taking master failed (active-flag/reset); frame retries next tick");
            }
            for surface in device.surfaces.values_mut() {
                if let Err(err) = surface.compositor.reset_state() {
                    warn!(?err, "HART-comp DRM: surface reset_state() after taking master failed; frame retries next tick");
                }
                surface.awaiting_vblank = false;
                surface.flip_queued_at = None;
            }
            info!("HART-comp DRM: acquired DRM master via drmSetMaster (session active); scanning out");
            true
        }
        MasterStep::NotYet => false,
    }
}

/// Apply a libseat session activate/pause request the notifier parked in
/// `state.pending_session_activate` (VT switch / suspend / resume, or the proactive startup
/// activate). A DRM device tracks TWO independent states with NO auto-recovery for either:
///   • the SESSION `active` flag (what `is_active()` reads) — init TRUE, set false by `pause()`
///     and true by `activate()`; `render_frame`/`queue_frame` return `DeviceInactive` while it
///     is false (a VT-switched-away device → hold, never paint a dark CRTC); and
///   • the kernel DRM-master grant — held when a `drmSetMaster` has succeeded on the fd; while
///     NOT held every page-flip ioctl returns EACCES (`FrameError::Access` → our #186 Transient).
///
/// On ACTIVATE (resume / startup): force a fresh master re-confirm (`master=false`) then
/// `acquire_drm_master`, which issues `drmSetMaster` on the fd directly — the ONLY recovery that
/// works for a device that came up unprivileged (smithay's `activate()` cannot re-master a frozen-
/// unprivileged device; see `acquire_drm_master`). The render tick ALSO retries `acquire_drm_master`
/// every frame while unmastered, so a fresh boot (which has no ActivateSession edge) still recovers;
/// this edge handler simply makes a real resume re-confirm master against the kernel immediately.
/// On PAUSE: `pause()` the device (clears the SESSION active flag) AND `release_master_lock()` the
/// fd ourselves (smithay only auto-drops master when ITS privileged flag is true, which is false on
/// a device we mastered directly), then clear the in-flight-flip gate (a paused device delivers no
/// vblank, so `awaiting_vblank` would otherwise wedge until the stall timeout). Nothing here panics
/// (the never-fail floor); a refused grant just leaves the device unmastered for the next retry.
fn apply_pending_session(state: &mut State, devices: &mut HashMap<DrmNode, DeviceData>) {
    let activate = match state.pending_session_activate.take() {
        Some(v) => v,
        None => return,
    };
    // #137 — a session activate/pause (VT switch / suspend / resume / the startup activate)
    // must re-paint: on resume the reactivated CRTC needs a fresh frame, on pause the gate
    // must not skip the tick that drops master. Mark the frame-budget scheduler damaged so
    // the render tick composites this session transition instead of idling through it.
    state.repaint.mark_damaged();
    for device in devices.values_mut() {
        if activate {
            // A real session-activate edge (or the startup proactive activate): drop the latch so
            // we re-confirm master from the kernel via a fresh drmSetMaster, then re-acquire. This
            // is the defensive resume path; the per-tick render retry covers the no-edge fresh boot.
            device.master = false;
            acquire_drm_master(device);
        } else {
            // PAUSE (VT switch away / suspend): clear the active flag, then drop master so the
            // incoming session's compositor can take it.
            device.drm.pause();
            // A double-drop (smithay's pause already released it on a privileged device) just
            // returns Err — ignore it; the goal state is simply "we no longer hold master".
            if let Err(err) = device.fd.release_master_lock() {
                debug!(?err, "HART-comp DRM: release_master_lock on pause returned Err (expected on a double-drop; master already released)");
            }
            device.master = false;
            for surface in device.surfaces.values_mut() {
                surface.awaiting_vblank = false;
                surface.flip_queued_at = None;
            }
            info!("HART-comp DRM: dropped DRM master (session paused)");
        }
    }
}

/// Build a GLES2 GPU renderer on the GBM device of the primary render node (PART 3 of the
/// GPU lever). An EGL platform display is created from the GBM device (which impls
/// `EGLNativeDisplay`), a configless GLES context is made on it, and a `GlesRenderer` wraps
/// that context. Single-GPU, single-context, created + used ONLY on the calloop thread (EGL
/// contexts are thread-bound). Returns an error (NEVER panics) so `run_udev` stays on the
/// pixman software floor on any failure — degrade-not-die.
///
/// This is the ONLY `unsafe` in `udev.rs` (the SAME audited-exception posture as
/// `screencopy.rs::fill_one`; Cargo.toml lints `unsafe_code = "deny"`, not forbid). The
/// EGL/GLES construction never crosses a thread boundary and is built from a live DRM
/// render-node fd, so the audited blocks below are sound.
fn build_gles_renderer(gbm: &GbmDevice<DrmDeviceFd>) -> Result<GlesRenderer, Box<dyn std::error::Error>> {
    // SAFETY: `EGLDisplay::new` is `unsafe` because smithay cannot statically prove the
    // native display handle is valid. The `GbmDevice` wraps a live DRM render-node fd
    // (opened via the libseat session in `device_added`) and impls `EGLNativeDisplay`, so
    // the platform display IS valid. The display/context/renderer are created on, and only
    // ever used on, the single calloop thread (EGL contexts are thread-bound) and are never
    // sent across threads.
    #[allow(unsafe_code)]
    let display = unsafe { EGLDisplay::new(gbm.clone()) }
        .map_err(|e| format!("EGLDisplay::new(gbm) failed: {e}"))?;
    // EGLContext::new is SAFE on this rev (it only queries/creates a context on the display
    // above). A configless context is sufficient — HART-comp renders into the DrmCompositor
    // swapchain dmabuf via `Bind<Dmabuf>`, never an EGLSurface (EGL_KHR_surfaceless implied).
    let context = EGLContext::new(&display).map_err(|e| format!("EGLContext::new failed: {e}"))?;
    // SAFETY: `GlesRenderer::new` is `unsafe` for the same context-validity reason. The
    // context was just created on the display above and is used only on this thread.
    #[allow(unsafe_code)]
    let renderer = unsafe { GlesRenderer::new(context) }
        .map_err(|e| format!("GlesRenderer::new failed: {e}"))?;
    Ok(renderer)
}

/// Tell the shell which desktop chrome hart-comp is painting itself, so it can
/// stop painting the same thing on top (NATIVE CHROME BRIDGE, part 4 of 4).
///
/// Called after a page-flip is accepted, i.e. only once we have EVIDENCE the
/// native elements reached the screen, never from configuration. The bitmask
/// comes from the frame builder and is set only where an element was actually
/// pushed, so a failed buffer import cannot make the shell hide its own copy.
///
/// Writes at most once per (re)start and only when the claim GROWS, because the
/// shell re-reads this on every render: rewriting the same value 60 times a
/// second would be pointless churn on a tmpfs, and shrinking it mid-session
/// would make the desktop flicker between native and HTML chrome.
///
/// Best-effort throughout. If this file cannot be written the shell simply keeps
/// drawing everything, which is exactly today's behaviour — the failure mode is
/// "no speedup", never "no desktop".
fn publish_native_chrome() {
    use std::sync::atomic::{AtomicU8, Ordering};
    /// What we have already told the shell. Starts at 0 = "we claim nothing".
    static PUBLISHED: AtomicU8 = AtomicU8::new(0);

    let mask = crate::comp_core::NATIVE_CHROME_EMITTED.load(Ordering::Relaxed);
    if mask == 0 {
        return;
    }
    let prev = PUBLISHED.load(Ordering::Relaxed);
    // Only ever grow. A frame that happens to omit the orb (capture blocked, a
    // degenerate mode) must not retract a claim the shell has already acted on.
    let next = prev | mask;
    if next == prev {
        return;
    }

    let mut names: Vec<&str> = Vec::new();
    if next & crate::comp_core::NATIVE_CHROME_BLOOM != 0 {
        names.push("bloom");
    }
    if next & crate::comp_core::NATIVE_CHROME_ORB != 0 {
        names.push("orb");
    }

    // /run/hart/session is the group-writable (0770) dir the session may write;
    // /run/hart itself is 0750 owner-only. Writing the wrong one fails silently
    // and defeats the whole bridge — the same perms trap already documented in
    // liquid_ui_service.py for shell-render.
    let path = "/run/hart/session/native-chrome";
    let tmp = "/run/hart/session/.native-chrome.tmp";
    // Write-then-rename so a reader never sees a half-written claim: the shell
    // polls this on every render and a torn read would flicker the desktop.
    let body = names.join(",");
    let wrote = std::fs::write(tmp, &body)
        .and_then(|()| std::fs::rename(tmp, path))
        .is_ok();
    if wrote {
        PUBLISHED.store(next, Ordering::Relaxed);
        info!(
            chrome = %body,
            "native-chrome published (the shell may stop painting these; \
             claimed only after a presented frame contained them)"
        );
    } else {
        // Do NOT retry-storm: a missing dir or a perms problem will not fix
        // itself mid-session, and this runs on the flip path.
        let _ = std::fs::remove_file(tmp);
    }
}

/// PURE policy (no Smithay types): given whether a `render_frame` error was the
/// `RenderFrame` variant (a renderer fault — a lost GL context / a failed import) versus
/// the `PrepareFrame` variant (a DRM/swapchain/master hiccup that would hit pixman too),
/// should the live renderer be demoted? Only a renderer fault demotes; a prepare fault is
/// just retried next tick like every other transient. Split out so the demote decision is
/// unit-tested on the dev box with NO Smithay types — the `matches!(err,
/// RenderFrameError::RenderFrame(_))` adapter that feeds it is the only Smithay-touching,
/// CI-compiled half (mirrors the `flip_action`/`master_step` pure-policy split).
fn gles_should_demote(is_render_frame_variant: bool) -> bool {
    is_render_frame_variant
}

/// Present the built element list to every active DRM surface that is NOT mid-flip:
/// `render_frame` (composite into the primary swapchain buffer) → `queue_frame` (page-flip)
/// → gate on vblank (F1, #166; the `frame_submitted` half is driven by `reap_completed_
/// vblanks`). GENERIC over the renderer `R`: the SAME body presents with EITHER the
/// PixmanRenderer software floor OR the GlesRenderer GPU path — it never names a concrete
/// renderer, so there is no parallel present path. EVERY render/flip error degrades (log +
/// skip + retry next tick) and KEEPS THE COMPOSITOR ALIVE — never a `panic!`/`.unwrap()`
/// death (F2/F3, #186). Returns `true` if a RENDERER fault (`RenderFrameError::RenderFrame`)
/// was seen, so the caller can demote a GLES renderer to the pixman floor (degrade-not-die).
fn present_surfaces<R>(
    devices: &mut HashMap<DrmNode, DeviceData>,
    renderer: &mut R,
    elements: &[HartRenderElement<R>],
    clear: Color32F,
    now: std::time::Instant,
    // Diagnostic context for the silent-freeze beacon ONLY (never a render input).
    // These are the two collections the frame-callback loop in run_udev walks, so
    // they answer the question the first beacon could not: is the client starving
    // because we are sending it nothing? A client that never receives a frame
    // callback stops committing, and then "render_frame says nothing changed" is
    // TRUE but is a SYMPTOM, not the cause. layer_count == 0 means the glass shell's
    // wlr-layer surface is not in the map we send callbacks to.
    space_count: usize,
    layer_count: usize,
) -> bool
where
    R: Renderer + Bind<Dmabuf>,
    R::TextureId: Texture + Clone + Send + 'static,
    HartRenderElement<R>: RenderElement<R>,
{
    let mut renderer_fault = false;
    for device in devices.values_mut() {
        // Self-healing DRM master retry (THE fresh-boot recovery, fix (a)): the construction-time
        // drmSetMaster inside DrmDevice::new may have lost the boot-VT master race ("Unable to
        // become drm master, assuming unprivileged mode"), and a fresh boot has NO libseat
        // ActivateSession edge to recover it. Re-attempt drmSetMaster DIRECTLY on the fd each tick
        // until the kernel grants master (fbcon/simpledrm drops it once logind finishes the VT-7
        // handoff); the `master` latch short-circuits this to a no-op the instant it succeeds, so
        // there is no per-frame ioctl in steady state. Without this, an unprivileged-at-startup
        // device returns EACCES on every flip forever -> a black, settled Tier-1 the paint watchdog
        // turns into a tier-drop. acquire_drm_master never aborts (the never-fail floor).
        if !device.master {
            acquire_drm_master(device);
        }
        // Copied out BEFORE the surfaces loop: the loop takes `device.surfaces` mutably,
        // so reading `device.master` inside it would be a second borrow. Diagnostic only
        // (the silent-freeze beacon reports whether we still hold DRM master).
        let device_master = device.master;
        for (crtc, surface) in device.surfaces.iter_mut() {
            // F1 (#166): never start a new flip while the prior one is still in flight to
            // this CRTC — wait for its vblank (which clears `awaiting_vblank` in
            // `reap_completed_vblanks`). Submitting on top of an in-flight flip is what
            // the kernel rejects (EBUSY) and what tears the scanout.
            if surface.awaiting_vblank {
                // …UNLESS the vblank was lost (VT switch / suspend / driver hiccup ate the
                // page-flip event). Without this escape a single dropped vblank would
                // freeze the CRTC forever (we'd wait on a vblank that never comes), the
                // paint watchdog would see a frozen screen, and the supervisor would crash-
                // loop the boot. A lost vblank instead degrades to a brief stutter (#186).
                let stalled = surface
                    .flip_queued_at
                    .map(|t| now.duration_since(t) >= VBLANK_STALL_TIMEOUT)
                    .unwrap_or(true);
                if !stalled {
                    continue;
                }
                warn!(?crtc, "HART-comp DRM: page-flip vblank lost (>100ms) — recovering, re-rendering");
                surface.awaiting_vblank = false;
                surface.flip_queued_at = None;
            }
            // `render_frame` returns a result whose Ok BORROWS the compositor; reduce it to
            // the Copy `is_empty` bool immediately so no borrow of `surface.compositor` spans
            // the sibling-field writes (`awaiting_vblank`/`flip_queued_at`) below. The Err
            // (`RenderFrameError`) is owned, so it survives the reduction for classification.
            let render_outcome = surface
                .compositor
                .render_frame::<_, _>(renderer, elements, clear, SOFTWARE_FLOOR_FRAME_FLAGS)
                .map(|result| result.is_empty);
            match render_outcome {
                Ok(true) => {
                    // Nothing changed — no flip to schedule, no vblank to await.
                    //
                    // This is the ONLY silent path in this match, and the 2026-08-19
                    // freeze proved it can also be the WRONG answer: taken forever while
                    // a real client (and even a freshly mapped toplevel) is on screen and
                    // never gets presented. Report it when it persists, with the state
                    // that decides which of the two it is: `elements` is what the render
                    // path was actually handed, so elements=0 means the scene collapsed
                    // upstream in build_frame_elements, while a NON-zero count means the
                    // scene is intact and the DrmCompositor's own damage tracking is
                    // wedged. Those need opposite fixes, which is the whole reason this
                    // logs rather than guesses.
                    let frozen_for = surface.last_flip_at.map(|t| now.saturating_duration_since(t));
                    let overdue = frozen_for.map(|d| d >= SILENT_FREEZE_AFTER).unwrap_or(false);
                    let due_to_log = surface
                        .last_stall_log_at
                        .map(|t| now.saturating_duration_since(t) >= SILENT_FREEZE_LOG_EVERY)
                        .unwrap_or(true);
                    if overdue && due_to_log {
                        surface.last_stall_log_at = Some(now);
                        warn!(
                            ?crtc,
                            elements = elements.len(),
                            // The two collections run_udev's frame-callback loop walks.
                            // THE question the first beacon could not answer: a client
                            // that receives no frame callback stops committing, and then
                            // "nothing changed" is TRUE but is the SYMPTOM. If
                            // layer_surfaces == 0 while the glass shell is on screen, we
                            // are starving it and the freeze starts here, not in damage.
                            space_windows = space_count,
                            layer_surfaces = layer_count,
                            // `as u64`: Duration::as_millis is u128, which does NOT
                            // implement tracing::Value — it would not compile as a field.
                            frozen_for_ms = frozen_for.map(|d| d.as_millis() as u64).unwrap_or(0),
                            awaiting_vblank = surface.awaiting_vblank,
                            master = device_master,
                            "HART-comp DRM: SILENT FREEZE — render_frame reports \"nothing changed\" \
                             but this CRTC has not presented in a long time. layer_surfaces=0 means \
                             the shell is being sent NO frame callbacks (it can never commit again, \
                             so this is a symptom); layer_surfaces>0 with elements>0 means the scene \
                             is intact and reaching the client, and the damage tracking is wedged."
                        );
                    }
                    continue;
                }
                Ok(false) => match surface.compositor.queue_frame(()) {
                    // The flip is in flight; gate this CRTC until its vblank lands.
                    Ok(()) => {
                        surface.awaiting_vblank = true;
                        surface.flip_queued_at = Some(now);
                        // This CRTC just presented. Sole input to the silent-freeze
                        // beacon's "how long since anything reached the screen".
                        surface.last_flip_at = Some(now);
                        // NATIVE CHROME BRIDGE (part 4 of 4). The frame just
                        // accepted by the kernel is the FIRST evidence that the
                        // native bloom/orb actually reached the screen, so this
                        // is where the shell is told it may stand down. Claiming
                        // from configuration instead would let a compositor that
                        // composed but never flipped turn the shell transparent
                        // over nothing.
                        publish_native_chrome();
                    }
                    Err(err) => {
                        // A flip/commit ioctl error (the real-HW EACCES/EBUSY/ENODEV/
                        // EINVAL): classify → log → leave `awaiting_vblank` false so the
                        // NEXT tick re-renders + retries. The compositor stays ALIVE.
                        match flip_action(Err(classify_frame_error(&err))) {
                            FrameAction::DeviceInactive => info!(
                                ?crtc,
                                "HART-comp DRM: queue_frame on inactive device (VT switch/suspend) — holding"
                            ),
                            // flip_action returns Reschedule for every other Err; Presented
                            // is unreachable for an Err input but matched for exhaustiveness.
                            FrameAction::Reschedule | FrameAction::Presented => warn!(
                                ?err, ?crtc,
                                "HART-comp DRM: queue_frame failed — degrading, retry next frame"
                            ),
                        }
                    }
                },
                // render_frame failed: a `RenderFrame`-class error is a RENDERER fault (a
                // GLES path lost its GL context / an import failed) → flag it so the caller
                // demotes a GLES renderer to the pixman floor. A `PrepareFrame`-class error
                // is a DRM/swapchain/master hiccup that would hit pixman too → NOT a renderer
                // fault, just retried next tick. EITHER way the compositor stays ALIVE (#186).
                Err(err) => {
                    if gles_should_demote(matches!(err, RenderFrameError::RenderFrame(_))) {
                        renderer_fault = true;
                        warn!(?err, ?crtc, "HART-comp DRM: render_frame RENDERER fault (RenderFrame) — degrading; caller may demote to the pixman floor");
                    } else {
                        warn!(?err, ?crtc, "HART-comp DRM: render_frame failed (PrepareFrame transient) — degrading, retry next frame");
                    }
                }
            }
        }
    }
    renderer_fault
}

/// Render every active DRM surface this tick: build the FULL z-order element list (the
/// SHARED `comp_core` builder — killswitch → cursor → windows-faded → layer-shell desktop,
/// the SAME list the winit backend composites) and present it. The renderer is the live
/// GPU `GlesRenderer` when the boot GPU probe authorised it AND it initialised (`gles`),
/// else the `PixmanRenderer` software floor of record (`state.renderer`). ONE present body
/// (`present_surfaces`, generic over the renderer) drives both — no parallel render path.
///
/// THE NEVER-BLANK FLOOR IS SACRED: a GLES renderer fault (a lost GL context / a failed
/// import) does NOT panic and does NOT black the screen — `present_surfaces` reports it,
/// this fn drops the GLES renderer (`*gles = None`) + resets every surface, and the VERY
/// NEXT tick paints through the untouched pixman renderer of record. The DrmCompositors
/// were all built with pixman/LINEAR formats (which GLES can also render into), so the SAME
/// compositors scan out under either renderer and the demotion is always bind-safe.
fn render_all(
    state: &mut State,
    devices: &mut HashMap<DrmNode, DeviceData>,
    gles: &mut Option<GlesRenderer>,
) {
    // Retire any flips whose vblank arrived since the last tick first, so a CRTC freed
    // this tick can be re-queued immediately below (one-frame-latency, no stall). This runs
    // BEFORE the frame-budget gate below, so even a skipped (idle) tick still reaps vblanks —
    // the in-flight-flip gate (#166) and the first-scanout beacon (#131) stay live regardless.
    reap_completed_vblanks(state, devices);

    // ── #137 FRAME-BUDGET GATE ── Skip the whole build+composite+flip when the scene is
    // provably idle, so a static desktop stops re-importing textures + re-running the pixman
    // damage pass + attempting a page-flip on every 16ms tick (the "no damage-driven
    // scheduling here" cost the run_udev loop comment names). The DrmCompositor still tracks
    // damage at the REGION level inside render_frame; this is the TICK-level gate on top.
    let now = std::time::Instant::now();
    // While any device is still UNMASTERED (the boot master-handoff race — see
    // acquire_drm_master), keep painting at full rate: the per-tick drmSetMaster retry lives
    // inside present_surfaces, so throttling it would slow first-paint / delay the never-blank
    // recovery. A dead GPU that can never master just paints every tick (correctness over the
    // idle saving in the degraded case; the supervisor drops a tier on the missing first paint).
    if devices.values().any(|d| !d.master) {
        state.repaint.mark_damaged();
    }
    let effects_animating = comp_core::effects_animating(state);
    if !state.repaint.should_paint(now, effects_animating) {
        // Nothing changed, nothing animating, still within the heartbeat: skip this tick's
        // paint. Vblanks were already reaped above; the loop still dispatches clients + sends
        // frame callbacks, so a fresh commit/input marks the scheduler damaged and repaints on
        // the very next tick — never a missed frame, just a saved idle composite (the win).
        return;
    }

    let clear = Color32F::new(
        HART_SPLASH_RGBA[0],
        HART_SPLASH_RGBA[1],
        HART_SPLASH_RGBA[2],
        HART_SPLASH_RGBA[3],
    );
    // The output's current physical mode (the capturable framebuffer extent).
    let size = comp_core::output_physical_size(state);

    // ── GLES GPU path ── The GlesRenderer is NOT a `State` field, so there is no
    // borrow-checker mem::replace dance: `build_frame_elements` borrows `state` + `gles`
    // disjointly. GlesRenderer satisfies the builder's `Renderer + ImportAll + ImportMem`
    // bounds (proven by winit.rs) AND `present_surfaces`'s `Bind<Dmabuf>` bound, so the
    // GPU desktop gets the SAME cursor / kill-switch / fades with ZERO parallel render code.
    // Diagnostic counts for the silent-freeze beacon: EXACTLY the two collections
    // run_udev's frame-callback loop walks, sampled here where `state` is borrowable.
    // The layer-map guard is scoped so it is dropped before the render borrows below.
    let space_count = state.space.elements().count();
    let layer_count = {
        let map = layer_map_for_output(&state.output);
        map.layers().count()
    };

    let mut demote_gles = false;
    if let Some(renderer) = gles.as_mut() {
        let elements: Vec<HartRenderElement<GlesRenderer>> =
            comp_core::build_frame_elements(state, renderer, size);
        demote_gles = present_surfaces(devices, renderer, &elements, clear, now, space_count, layer_count);
    } else {
        // ── Pixman software-floor path (the never-fail renderer of record) ── The renderer
        // lives ON `state`, but `build_frame_elements` needs BOTH `&mut state` (reads the
        // space/cursor/effects, updates the killswitch buffer) AND `&mut renderer` (imports
        // surface textures) — disjoint fields the borrow checker can't see through the
        // `CompState` accessors. Swap the renderer OUT for the build (anvil's pattern), then
        // restore it. The placeholder is a fresh PixmanRenderer; if it can't be made we skip
        // this frame (the next tick retries) rather than panic — the never-fail posture.
        let mut renderer = match PixmanRenderer::new() {
            Ok(placeholder) => std::mem::replace(&mut state.renderer, placeholder),
            Err(err) => {
                warn!(?err, "HART-comp DRM: could not allocate a render scratch renderer; skipping frame");
                return;
            }
        };
        let elements: Vec<HartRenderElement<PixmanRenderer>> =
            comp_core::build_frame_elements(state, &mut renderer, size);
        // The pixman floor has NO lower renderer to demote to, so its renderer-fault return
        // is ignored (a pixman RenderFrame fault is a transient retried next tick — there is
        // no GL context to lose on the CPU path).
        let _ = present_surfaces(devices, &mut renderer, &elements, clear, now, space_count, layer_count);
        // Restore the real renderer (keeping the ORIGINAL instance avoids re-allocating its
        // internal caches every tick).
        state.renderer = renderer;
    }

    // #137 — this tick composited: record the paint so the idle heartbeat is measured from
    // now, and clear the dirty latch (so the NEXT static tick can be skipped) UNLESS an effect
    // is still animating, in which case dirty is re-armed to keep the fade playing frame-by-
    // frame. This is the single place the frame-budget scheduler is told "a frame went out".
    state.repaint.note_painted(now, effects_animating);

    // ── GLES → pixman demotion (degrade-not-die) ── A renderer fault was seen on the GPU
    // path: drop the GLES renderer so EVERY subsequent tick paints via the pixman renderer
    // of record, and reset each surface's swapchain/commit state + clear its in-flight-flip
    // gate so the next pixman frame starts clean and flips immediately. Done OUTSIDE the
    // `if let` above so `*gles = None` does not alias the `gles.as_mut()` borrow.
    if demote_gles {
        warn!("HART-comp DRM: GLES renderer fault — demoting to the pixman software floor (never-blank; the pixman renderer of record paints from the next tick)");
        *gles = None;
        for device in devices.values_mut() {
            for surface in device.surfaces.values_mut() {
                if let Err(err) = surface.compositor.reset_state() {
                    warn!(?err, "HART-comp DRM: surface reset_state() after GLES demotion failed; retries next tick");
                }
                surface.awaiting_vblank = false;
                surface.flip_queued_at = None;
            }
        }
        // The demotion reset the surfaces; the pixman renderer of record must paint them
        // NEXT tick, so re-arm the frame-budget scheduler (note_painted above just cleared
        // it) — a GLES fault becomes an imperceptible one-frame pixman repaint, not a
        // ≤heartbeat-stale hold.
        state.repaint.mark_damaged();
    }
}

/// Pick a connector's preferred mode (or the first available).
fn pick_mode(conn: &connector::Info) -> Option<smithay::reexports::drm::control::Mode> {
    use smithay::reexports::drm::control::ModeTypeFlags;
    let modes = conn.modes();
    modes
        .iter()
        .find(|m| m.mode_type().contains(ModeTypeFlags::PREFERRED))
        .or_else(|| modes.first())
        .copied()
}

/// Pick a CRTC the connector can drive that is not already claimed by another surface in
/// this device. Walks the connector's current encoder (or its possible encoders) and
/// resolves each encoder's `possible_crtcs` mask to real CRTC handles via the drm crate's
/// `ResourceHandles::filter_crtcs` — the standard KMS connector→encoder→crtc resolution,
/// no smithay-drm-extras dep.
fn pick_crtc(
    drm: &DrmDevice,
    res: &smithay::reexports::drm::control::ResourceHandles,
    conn: &connector::Info,
    claimed: &HashMap<crtc::Handle, SurfaceData>,
) -> Option<crtc::Handle> {
    // Encoders to consider: the currently-bound one first (if any), else all possible.
    let encoders: Vec<_> = conn
        .current_encoder()
        .into_iter()
        .chain(conn.encoders().iter().copied())
        .collect();
    for enc_handle in encoders {
        if let Ok(enc) = drm.get_encoder(enc_handle) {
            // `filter_crtcs` resolves the encoder's possible-CRTC bitmask into the
            // actual `crtc::Handle`s that can drive it (the canonical drm-crate helper).
            for crtc in res.filter_crtcs(enc.possible_crtcs()) {
                if !claimed.contains_key(&crtc) {
                    return Some(crtc);
                }
            }
        }
    }
    // Fallback: the first unclaimed CRTC (a single-display box almost always has one).
    res.crtcs().iter().find(|c| !claimed.contains_key(c)).copied()
}

/// Spawn XWayland nested in OUR display + attach the X11 WM on `Ready`. The DRM twin of
/// winit.rs::spawn_xwayland — the SAME lifecycle, reused so Wine/legacy-X11 toplevels
/// surface on the real-hardware path too. Best-effort: a spawn failure leaves xwm=None.
fn spawn_xwayland(
    dh: &smithay::reexports::wayland_server::DisplayHandle,
    loop_handle: &smithay::reexports::calloop::LoopHandle<'static, State>,
) {
    let (xwayland, client) = match XWayland::spawn(
        dh,
        None,
        std::iter::empty::<(String, String)>(),
        std::iter::empty::<String>(),
        true,
        std::process::Stdio::null(),
        std::process::Stdio::null(),
        |_| {},
    ) {
        Ok(ret) => ret,
        Err(err) => {
            warn!(?err, "HART-comp DRM: XWayland spawn failed (X11 apps unavailable; Wayland-native unaffected)");
            return;
        }
    };
    let inserted = loop_handle.insert_source(xwayland, move |event, _, state: &mut State| match event {
        XWaylandEvent::Ready { x11_socket, display_number } => {
            match X11Wm::start_wm(state.loop_handle.clone(), &state.dh, x11_socket, client.clone()) {
                Ok(wm) => {
                    state.xwm = Some(wm);
                    std::env::set_var("DISPLAY", format!(":{display_number}"));
                    info!(display = display_number, "HART-comp DRM: XWayland ready — X11 WM attached");
                }
                Err(err) => {
                    error!(?err, "HART-comp DRM: failed to start the X11 WM");
                    state.xwm = None;
                }
            }
        }
        XWaylandEvent::Error => {
            warn!("HART-comp DRM: XWayland crashed on startup");
            state.xwm = None;
        }
    });
    if let Err(err) = inserted {
        error!(?err, "HART-comp DRM: failed to insert the XWayland source");
    }
}

/// Touch `Size`/`Subpixel` so a future reviewer sees the output construction surface.
#[allow(dead_code)]
fn _types_touch() {
    let _s: Option<Size<i32, smithay::utils::Physical>> = None;
}

// ════════════════════════════════════════════════════════════════════════════
// PURE DRM-config unit floor. The DRM backend is overwhelmingly hardware-bound — it
// opens /dev/dri via libseat, creates a GbmDevice, binds a DrmDevice, and page-flips
// to a CRTC; NONE of that is testable off real hardware (those paths are exercised by
// the M7 virgl-QEMU scanout harness + the CI nixosTest llvmpipe VM). What IS pure +
// testable in isolation: the operator DRM-node override precedence + the never-fail
// color-format floor. `pick_mode`/`pick_crtc` are pure ALGORITHMS but take live
// Smithay `connector::Info`/`ResourceHandles`/`DrmDevice` snapshots that cannot be
// constructed without a DRM node, so they are covered by the QEMU scanout harness, not
// here (see the report's "untestable without DRM" list). Compiled under `smithay`.
// ════════════════════════════════════════════════════════════════════════════
#[cfg(test)]
mod tests {
    use super::*;

    // Env reads/writes are process-global; serialize the override tests so they don't
    // race when the test binary runs them in parallel.
    static ENV_LOCK: std::sync::Mutex<()> = std::sync::Mutex::new(());

    #[test]
    fn drm_device_override_is_none_when_the_var_is_unset() {
        let _g = ENV_LOCK.lock().unwrap();
        let saved = std::env::var("HART_COMP_DRM_DEVICE").ok();
        std::env::remove_var("HART_COMP_DRM_DEVICE");
        assert_eq!(drm_device_override(), None, "unset var → no override");
        if let Some(v) = saved {
            std::env::set_var("HART_COMP_DRM_DEVICE", v);
        }
    }

    #[test]
    fn drm_device_override_returns_a_set_non_empty_path() {
        let _g = ENV_LOCK.lock().unwrap();
        let saved = std::env::var("HART_COMP_DRM_DEVICE").ok();
        std::env::set_var("HART_COMP_DRM_DEVICE", "/dev/dri/card9");
        assert_eq!(
            drm_device_override().as_deref(),
            Some("/dev/dri/card9"),
            "a set override path wins over primary_gpu()"
        );
        match saved {
            Some(v) => std::env::set_var("HART_COMP_DRM_DEVICE", v),
            None => std::env::remove_var("HART_COMP_DRM_DEVICE"),
        }
    }

    #[test]
    fn drm_device_override_treats_an_empty_var_as_unset() {
        // A stray `HART_COMP_DRM_DEVICE=` must NOT force an empty (invalid) node path —
        // it falls through to the real primary_gpu() probe.
        let _g = ENV_LOCK.lock().unwrap();
        let saved = std::env::var("HART_COMP_DRM_DEVICE").ok();
        std::env::set_var("HART_COMP_DRM_DEVICE", "");
        assert_eq!(drm_device_override(), None, "empty override → treated as unset");
        match saved {
            Some(v) => std::env::set_var("HART_COMP_DRM_DEVICE", v),
            None => std::env::remove_var("HART_COMP_DRM_DEVICE"),
        }
    }

    #[test]
    fn color_formats_are_the_never_fail_floor_set() {
        // The DrmCompositor primary-plane formats MUST include the 8-bit Argb/Xrgb floor
        // that pixman + virtually every KMS driver supports (no 10-bit gamble on an
        // unproven box). Order matters: Argb first (alpha-capable), Xrgb fallback.
        assert!(!COLOR_FORMATS.is_empty(), "an empty format set fails DrmCompositor::new");
        assert_eq!(COLOR_FORMATS.len(), 2);
        assert_eq!(COLOR_FORMATS[0], Fourcc::Argb8888, "Argb (alpha-capable) is first");
        assert_eq!(COLOR_FORMATS[1], Fourcc::Xrgb8888, "Xrgb (opaque) is the fallback");
    }

    // ── F2/F3 (#186) — the flip-error → action policy. This is the seam that turns "a
    // DRM page-flip/commit/render call returned Err" into "log + retry the frame", NOT a
    // `panic!`/`.unwrap()` that kills the whole compositor and crash-loops the boot. The
    // pure `flip_action` decides; `classify_frame_error` (the Smithay adapter, exercised
    // by the QEMU/HW scanout harness — it needs a live `FrameError`) feeds it. These
    // assert the INVARIANT that matters: every error path degrades, none aborts. ──

    #[test]
    fn flip_action_ok_presents() {
        // A successful flip leaves the CRTC alone — the vblank paces the next frame.
        assert_eq!(flip_action(Ok(())), FrameAction::Presented);
    }

    #[test]
    fn flip_action_transient_reschedules_never_dies() {
        // The real-laptop EBUSY/EACCES "resource busy"/"permission denied" on the atomic
        // commit classifies Transient → the tick must Reschedule (retry next frame). The
        // KEY assertion is that this is NOT a process death: a flip error is a retried
        // frame. There is no `Die`/`Abort` variant in `FrameAction` BY DESIGN.
        assert_eq!(flip_action(Err(FlipOutcome::Transient)), FrameAction::Reschedule);
    }

    #[test]
    fn flip_action_fatal_still_degrades_not_dies() {
        // Even a "fatal" renderer/format failure degrades to Reschedule (skip + retry) —
        // the compositor stays alive; a PERSISTENT fatal is what the supervisor observes
        // (no paint) and drops a tier on. The compositor itself never aborts the loop.
        assert_eq!(flip_action(Err(FlipOutcome::Fatal)), FrameAction::Reschedule);
    }

    #[test]
    fn flip_action_device_inactive_holds_not_dies() {
        // A VT switch / suspend (DeviceInactive) is a deliberate "don't paint a dark CRTC"
        // hold — not an error, and crucially not a death. The compositor waits for the
        // session to reactivate and resumes painting; it must never panic on this.
        assert_eq!(flip_action(Err(FlipOutcome::DeviceInactive)), FrameAction::DeviceInactive);
    }

    #[test]
    fn no_flip_outcome_maps_to_a_compositor_death() {
        // Exhaustive guard: EVERY FlipOutcome must map to a NON-fatal FrameAction. If a
        // future variant is added and forgotten, this fails — there is intentionally no
        // FrameAction that tears down the compositor, so a flip error can NEVER crash-loop
        // the boot (the whole point of #186).
        for outcome in [FlipOutcome::DeviceInactive, FlipOutcome::Transient, FlipOutcome::Fatal] {
            let action = flip_action(Err(outcome));
            assert!(
                matches!(action, FrameAction::Reschedule | FrameAction::DeviceInactive),
                "{outcome:?} must degrade (Reschedule/DeviceInactive), never abort — got {action:?}"
            );
        }
    }

    #[test]
    fn vblank_stall_timeout_is_past_a_real_vblank_but_under_a_freeze() {
        // The lost-vblank escape hatch (#166/#186 robustness) must be LONGER than a real
        // 60Hz vblank interval (16.7ms) so it never pre-empts a healthy in-flight flip,
        // yet SHORT enough that a dropped vblank is a stutter, not a watchdog-tripping
        // freeze. 100ms sits in that window (≈6 missed frames).
        assert!(
            VBLANK_STALL_TIMEOUT > Duration::from_millis(17),
            "stall timeout must clear a real 60Hz vblank interval"
        );
        assert!(
            VBLANK_STALL_TIMEOUT < Duration::from_millis(500),
            "stall timeout must recover well before a paint-watchdog freeze"
        );
    }

    // ── Fix (a): the DRM-master recovery state machine (`master_step`). The real `drmSetMaster`
    // ioctl is the only part that needs hardware; the DECISION that drives the retry loop is pure
    // and tested here. The model that matters: a device that came up unprivileged (held=false)
    // keeps trying until the kernel grants master, then latches — and NO outcome aborts. This is
    // exactly investigation 2's "validate the reconstruct/retry state machine in WSL without the
    // live logind grant". ──

    #[test]
    fn master_step_unprivileged_then_granted_is_notyet_then_acquired() {
        // The real-HW boot sequence: the construction grab lost the VT race (held=false) and the
        // first few drmSetMaster retries are refused while fbcon still holds master
        // (acquire_succeeded=false → NotYet), then the kernel grants it (true → Acquired).
        assert_eq!(master_step(false, false), MasterStep::NotYet, "refused grab → keep retrying");
        assert_eq!(master_step(false, true), MasterStep::Acquired, "granted grab → latch + repaint");
    }

    #[test]
    fn master_step_already_held_never_reacquires() {
        // Once master is latched, the step is AlreadyHeld REGARDLESS of a (not even attempted)
        // acquire result — the render tick must NOT re-issue drmSetMaster ~60x/s in steady state.
        assert_eq!(master_step(true, false), MasterStep::AlreadyHeld);
        assert_eq!(master_step(true, true), MasterStep::AlreadyHeld);
    }

    #[test]
    fn master_step_has_no_abort_outcome() {
        // The never-fail invariant: across every (held, acquired) input there is NO outcome that
        // tears the compositor down. A device that can truly never master just stays NotYet and
        // never paints; the supervisor drops a tier on the missing first paint — the loop never
        // aborts on the master race itself. (There is intentionally no `Fail`/`Abort` variant.)
        for held in [false, true] {
            for ok in [false, true] {
                let step = master_step(held, ok);
                assert!(
                    matches!(step, MasterStep::AlreadyHeld | MasterStep::Acquired | MasterStep::NotYet),
                    "master_step({held},{ok}) = {step:?} must be a non-fatal step"
                );
            }
        }
    }

    #[test]
    fn master_retry_converges_within_the_paint_watchdog_window() {
        // Behavioural: simulate the per-tick render retry against a fake device that is refused
        // for the first few 16ms ticks (fbcon still master) then granted. Drive the SAME decision
        // the real loop drives (`master_step` on the latch) and assert it latches master well
        // inside the supervisor's 45s first-paint watchdog — i.e. the retry actually terminates,
        // it is not an unbounded spin that the watchdog turns into a tier-drop.
        let grant_after_ms: u128 = 800; // fbcon-handoff settle on a slow box; comfortably < 45s
        let tick = Duration::from_millis(16); // run_udev's dispatch cadence
        let mut held = false;
        let mut elapsed_ms: u128 = 0;
        let mut latched_at_ms: Option<u128> = None;
        for _ in 0..4000 {
            // The fake kernel grants master once enough time has passed AND we are still asking.
            let acquire_succeeded = !held && elapsed_ms >= grant_after_ms;
            match master_step(held, acquire_succeeded) {
                MasterStep::Acquired => {
                    held = true;
                    latched_at_ms = Some(elapsed_ms);
                    break;
                }
                MasterStep::NotYet => {}
                MasterStep::AlreadyHeld => break,
            }
            elapsed_ms += tick.as_millis();
        }
        assert!(held, "the retry must eventually latch master, not spin forever");
        let at = latched_at_ms.expect("must record when master latched");
        assert!(at >= grant_after_ms, "must not claim master before the kernel grants it");
        assert!(
            at < 45_000,
            "master must latch ({at}ms) well inside the 45s paint watchdog so the supervisor does not drop to sway"
        );
    }

    // ── Fix (b): the software-floor cursor. The render path passes `SOFTWARE_FLOOR_FRAME_FLAGS`,
    // which DELIBERATELY omits the cursor-plane (and every other plane-scanout) bit so smithay
    // never issues the hardware-cursor atomic commit that logs "Failed to set cursor plane" — the
    // arrow is composited by pixman instead. This guards that the flag the render path actually
    // uses keeps that property (a real `FrameFlags` value, not a string match). ──

    #[test]
    fn software_floor_flags_never_use_the_hardware_cursor_plane() {
        // The whole point of fix (b): with the cursor-plane bit OFF, `try_assign_cursor_plane`
        // early-returns None, so the cursor-plane atomic commit (and its "Failed to set cursor
        // plane" / "Failed to destroy old node property" fallout) never happens — the baked cursor
        // composites onto the primary plane as a software cursor.
        assert!(
            !SOFTWARE_FLOOR_FRAME_FLAGS.contains(FrameFlags::ALLOW_CURSOR_PLANE_SCANOUT),
            "the software floor must NOT offload the cursor to a hardware plane"
        );
    }

    #[test]
    fn software_floor_flags_offload_nothing_to_planes() {
        // The pixman floor composites EVERYTHING into one primary swapchain buffer and flips that:
        // no overlay, no direct primary-element scanout, no cursor plane. This keeps the never-fail
        // floor free of every per-element plane-assignment atomic TEST commit (each of which would
        // fail EACCES while master is mid-handoff). `empty()` is that posture.
        assert_eq!(
            SOFTWARE_FLOOR_FRAME_FLAGS,
            FrameFlags::empty(),
            "the software floor renders with no plane-offload flags"
        );
        assert!(!SOFTWARE_FLOOR_FRAME_FLAGS.contains(FrameFlags::ALLOW_OVERLAY_PLANE_SCANOUT));
        assert!(!SOFTWARE_FLOOR_FRAME_FLAGS.contains(FrameFlags::ALLOW_PRIMARY_PLANE_SCANOUT));
    }

    // ── PART 3 (GPU lever): the GLES→pixman demotion policy (`gles_should_demote`). The
    // real `RenderFrameError` (whether the failure was the `RenderFrame` renderer-fault
    // variant or the `PrepareFrame` DRM/swapchain variant) is the only Smithay-touching
    // part, exercised by the QEMU/HW scanout harness; the DECISION that drives the
    // GLES→pixman fallback is pure and tested here. The invariant that matters: a renderer
    // fault demotes (the GPU path falls back to the never-blank pixman floor), a prepare
    // fault does NOT (it would hit pixman too — just retry). Mirrors `flip_action`/
    // `master_step`'s pure-policy split. ──

    #[test]
    fn gles_demotes_only_on_a_render_frame_renderer_fault() {
        // A `RenderFrameError::RenderFrame(_)` is a renderer fault (a lost GL context / a
        // failed import) — the GPU path must fall back to the pixman software floor.
        assert!(
            gles_should_demote(true),
            "a RenderFrame-class fault must demote GLES to the never-blank pixman floor"
        );
        // A `RenderFrameError::PrepareFrame(_)` (DRM/swapchain/master hiccup) is NOT a
        // renderer fault — it would hit pixman too, so it does NOT demote (just retry).
        assert!(
            !gles_should_demote(false),
            "a PrepareFrame-class fault must NOT demote (it is a transient, not a renderer fault)"
        );
    }

    #[test]
    fn gles_demotion_never_panics_and_is_a_total_decision() {
        // Exhaustive guard: across BOTH variant classes the demote decision is a plain
        // bool — there is no third outcome that could panic or black the screen. The
        // never-blank floor is preserved because the ONLY effect of `true` is "drop the
        // GLES renderer and paint via pixman next tick" (see render_all's demotion block).
        for is_render_frame in [false, true] {
            let demote = gles_should_demote(is_render_frame);
            assert_eq!(
                demote, is_render_frame,
                "the demote decision tracks the RenderFrame variant exactly (no surprise outcome)"
            );
        }
    }
}
