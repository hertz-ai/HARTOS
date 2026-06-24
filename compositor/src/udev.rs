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
use std::sync::Arc;
use std::time::Duration;

use smithay::backend::allocator::gbm::{GbmAllocator, GbmBufferFlags, GbmDevice};
use smithay::backend::allocator::Fourcc;
use smithay::backend::drm::compositor::{DrmCompositor, FrameError, FrameFlags};
use smithay::backend::drm::exporter::gbm::GbmFramebufferExporter;
use smithay::backend::drm::{DrmDevice, DrmDeviceFd, DrmEvent, DrmNode, DrmSurface};
use smithay::backend::libinput::{LibinputInputBackend, LibinputSessionInterface};
use smithay::backend::renderer::pixman::PixmanRenderer;
use smithay::backend::renderer::{Color32F, ImportDma};
use smithay::backend::session::libseat::LibSeatSession;
use smithay::backend::session::{Event as SessionEvent, Session};
use smithay::backend::udev::{primary_gpu, UdevBackend, UdevEvent};
use smithay::desktop::{layer_map_for_output, Space, Window};
use smithay::input::{Seat, SeatState};
use smithay::output::{Mode as WlMode, Output, PhysicalProperties, Subpixel};
use smithay::reexports::calloop::EventLoop;
use smithay::reexports::drm::control::{connector, crtc, Device as ControlDevice};
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
use tracing::{error, info, warn};

use crate::comp_core::{self, HartRenderElement};
use crate::shared::send_frame_callbacks;
use crate::wayland::{ClientState, State};
use crate::{select_render_path, BootConfig, WindowRegistry, HART_SPLASH_RGBA};

/// Color formats DrmCompositor will try for the primary plane framebuffer. The pixman
/// software floor + virtually all KMS drivers support Argb8888/Xrgb8888 — the never-
/// fail floor formats (no 10-bit gamble on an unproven box).
const COLOR_FORMATS: &[Fourcc] = &[Fourcc::Argb8888, Fourcc::Xrgb8888];

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
}

/// If a queued page-flip's vblank has not arrived within this long, assume it was lost
/// (VT switch / suspend / driver hiccup) and let the CRTC re-render. ~5 frames at 60Hz —
/// comfortably past a real vblank (16.7ms) so we never pre-empt a healthy flip, but short
/// enough that a dropped vblank is an imperceptible stutter rather than a frozen screen.
const VBLANK_STALL_TIMEOUT: Duration = Duration::from_millis(100);

/// One opened DRM device (the GPU) + its per-CRTC scanout surfaces. Held in the loop
/// data so the VBlank handler can find the right surface to mark submitted + re-queue.
struct DeviceData {
    /// The DRM device. Held to keep the modeset alive (dropping it tears it down) AND to
    /// `activate()`/`pause()` it on libseat session changes — VT switch / suspend / resume
    /// and the unprivileged-at-startup master recovery (see `apply_pending_session`).
    drm: DrmDevice,
    /// GBM device — the buffer allocator backing both the scanout swapchain + the
    /// framebuffer exporter. Cloned into the allocator/exporter; held so it outlives them.
    _gbm: GbmDevice<DrmDeviceFd>,
    /// One scanout surface per active CRTC (one display). Keyed by CRTC handle.
    surfaces: HashMap<crtc::Handle, SurfaceData>,
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
    // The render-path decision is shared with the skeleton + winit; on the DRM path the
    // PixmanRenderer is the concrete renderer (the MANDATORY software floor — pixman is
    // not a hardware path, so `--force-software` is implied/honoured by construction).
    let _ = select_render_path(cfg);

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
    let keyboard = seat.add_keyboard(Default::default(), 200, 25)?;
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
        ipc: crate::ipc::IpcState::default(),
        // F1 (#166) — no flips in flight at boot; the VBlank source populates this.
        vblank_completed: std::collections::HashSet::new(),
        // No libseat session activate/pause pending at boot; the notifier parks them here and
        // the proactive activate before the loop primes the unprivileged-startup recovery.
        pending_session_activate: None,
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

    // 11. Bind the calloop event sources.
    //   (a) libinput → State::process_input_event (the shared input router). As each
    //       device appears, enable touchpad TAP-TO-CLICK: libinput defaults it OFF, so a
    //       light tap on a laptop touchpad registers NOTHING (only a physical button press
    //       clicks) — the real-HW 2026-06-24 "touchpad taps not registering". A non-touchpad
    //       reports finger_count 0 and is left untouched; a failed set is ignored (best-
    //       effort, never fatal — the never-fail floor). Tier-2 sway sets the same via its
    //       libinput config; the cage Tier-3 floor cannot configure libinput, so this is
    //       hart-comp's own. `config_tap_set_enabled` takes &mut self, hence `mut event`.
    event_loop
        .handle()
        .insert_source(libinput_backend, move |mut event, _, state: &mut State| {
            if let smithay::backend::input::InputEvent::DeviceAdded { device } = &mut event {
                if device.config_tap_finger_count() > 0 {
                    let _ = device.config_tap_set_enabled(true);
                }
            }
            state.process_input_event(event);
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
    event_loop
        .handle()
        .insert_source(notifier, move |event, &mut (), state: &mut State| match event {
            SessionEvent::PauseSession => {
                info!("HART-comp DRM: session paused (VT switch / sleep) — dropping DRM master");
                state.pending_session_activate = Some(false);
            }
            SessionEvent::ActivateSession => {
                info!("HART-comp DRM: session resumed — re-acquiring DRM master");
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
    //     But first re-acquire DRM master: a device may have come up UNPRIVILEGED in a startup
    //     VT race (the real-HW "Unable to become drm master, assuming unprivileged mode" log —
    //     the master `privileged` flag stays false). Its session `active` flag is TRUE, so
    //     `render_frame` proceeds, but every page-flip ioctl returns EACCES and #186 retries it
    //     FOREVER (master is never re-acquired on its own) → the first kick and every frame
    //     after paint NOTHING: a black, SETTLED Tier-1 the paint watchdog can't catch (the shell
    //     page still loads, so the shell-ready marker still fires). Park + apply an activate now
    //     so we re-take master before the first kick if the session is already active; if it
    //     isn't yet, the EACCES retries bridge until the libseat ActivateSession in the loop
    //     re-drives it. activate() is idempotent on a healthy boot (one extra repaint), so this
    //     is always safe.
    state.pending_session_activate = Some(true);
    apply_pending_session(&mut state, &mut devices);
    render_all(&mut state, &mut devices);

    info!(socket = %socket_name, "HART-comp DRM compositor initialized — entering the loop (real-HW scanout on the pixman floor)");

    while state.running {
        // Dispatch calloop sources (libinput, session, udev, the client socket, and the
        // per-device DRM VBlank sources) + a 16ms housekeeping tick, then the Display.
        if event_loop
            .dispatch(Some(Duration::from_millis(16)), &mut state)
            .is_err()
        {
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
        // fresh + drains client frame callbacks so clients draw their next frame.
        render_all(&mut state, &mut devices);

        // M8 — settle finished effects: clear the workspace-switch crossfade clock once it
        // completes so it does not re-evaluate forever (the 60Hz tick PLAYS an in-flight
        // fade frame-by-frame; map-in fades self-settle past FADE_IN_MS). The SAME tidy-up
        // the winit loop does, so the DRM desktop's crossfade behaves identically.
        if !comp_core::effects_animating(&state) {
            state.ws_switch_at = None;
        }

        // Send frame callbacks so clients (the glass shell) draw their next frame.
        let now_ms = 0u32; // monotonic ms is unused by the shell; 0 is a valid "now".
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
            Err(_) => continue,
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
            SurfaceData { compositor, awaiting_vblank: false, flip_queued_at: None },
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
            _gbm: gbm,
            surfaces,
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
}

/// Apply a libseat session activate/pause request the notifier parked in
/// `state.pending_session_activate` (VT switch / suspend / resume, or the proactive startup
/// activate). REQUIRED on the pinned Smithay rev because a DRM device tracks TWO independent
/// states with NO auto-recovery for either:
///   • the SESSION `active` flag (what `is_active()` reads) — init TRUE, set false by `pause()`
///     and true by `activate()`; `render_frame`/`queue_frame` return `DeviceInactive` while it
///     is false (a VT-switched-away device → hold, never paint a dark CRTC); and
///   • the DRM-master `privileged` flag — init FALSE, set true ONLY by a successful
///     `acquire_master_lock()` inside `DrmDevice::new` or `activate()`; while false every
///     page-flip ioctl returns EACCES (`FrameError::Access` → our #186 Transient → retried).
/// So a device that came up unprivileged in a startup VT race (the real-HW "Unable to become
/// drm master, assuming unprivileged mode") has active=TRUE but privileged=FALSE → its flips
/// retry EACCES FOREVER (master is never re-acquired on its own) → a BLACK, settled Tier-1; a
/// paused device has active=FALSE → holds. BOTH are recovered ONLY by `activate()`, which
/// re-acquires master AND sets active — the one required call.
///
/// On ACTIVATE: `activate(false)` every device (re-take drmSetMaster + set active, keep
/// connectors) then `reset_state()` every surface — the rev's documented "call after the session
/// is re-activated / VT switched to" step, which forces a full repaint — and clear the in-flight-
/// flip gate (a VT switch / the unprivileged startup may have swallowed the prior flip's vblank).
/// We deliberately do NOT guard on is_active() (it reads the SESSION flag, TRUE at an unprivileged
/// startup — a guard would skip exactly the device we must re-master; activate() is idempotent on
/// a device that already holds master). On PAUSE: `pause()` every device (drop master + clear
/// active) and clear the gate (a paused device delivers no vblank, so `awaiting_vblank` would
/// otherwise wedge until the stall timeout). A failed activate is logged + skipped (master not
/// grantable yet → flips keep retrying / holding → a later ActivateSession re-drives it) — never a
/// panic (the never-fail floor).
fn apply_pending_session(state: &mut State, devices: &mut HashMap<DrmNode, DeviceData>) {
    let activate = match state.pending_session_activate.take() {
        Some(v) => v,
        None => return,
    };
    for device in devices.values_mut() {
        if activate {
            // Do NOT guard on is_active() here: on this rev is_active() reads the SESSION
            // `active` flag (initialised TRUE), NOT the DRM-master `privileged` flag
            // (initialised FALSE — the unprivileged-startup case, where master must be
            // re-acquired). A guard on is_active() would SKIP exactly the device we have to
            // fix (active=true, privileged=false). activate() re-takes drmSetMaster and is
            // idempotent on a device that already holds master (the only cost is one extra
            // reset_state() repaint on a healthy boot).
            if let Err(err) = device.drm.activate(false) {
                warn!(?err, "HART-comp DRM: DRM master re-acquire failed — a later session-activate re-drives it");
                continue;
            }
            info!("HART-comp DRM: (re)acquired DRM master (session active) — scanning out");
            for surface in device.surfaces.values_mut() {
                if let Err(err) = surface.compositor.reset_state() {
                    warn!(?err, "HART-comp DRM: surface reset_state() after activate failed — frame retries next tick");
                }
                surface.awaiting_vblank = false;
                surface.flip_queued_at = None;
            }
        } else {
            device.drm.pause();
            for surface in device.surfaces.values_mut() {
                surface.awaiting_vblank = false;
                surface.flip_queued_at = None;
            }
            info!("HART-comp DRM: dropped DRM master (session paused)");
        }
    }
}

/// Render every active DRM surface that is NOT mid-flip: build the element list from the
/// space + the layer-shell desktop, composite on the pixman floor, clear to HART_SPLASH,
/// and queue the page-flip. The canonical `render_frame → queue_frame → (await vblank) →
/// frame_submitted` sequence from the rev's DrmCompositor doc example — the
/// `frame_submitted` half is driven by the vblank event (`reap_completed_vblanks`), NOT
/// here, so frames are paced by real flips (F1, #166). EVERY render/flip error degrades
/// (log + skip + retry next tick) and KEEPS THE COMPOSITOR ALIVE — a flip error is a
/// logged warning and a re-tried frame, never a `panic!`/`.unwrap()` death (F2/F3, #186).
fn render_all(state: &mut State, devices: &mut HashMap<DrmNode, DeviceData>) {
    // Retire any flips whose vblank arrived since the last tick first, so a CRTC freed
    // this tick can be re-queued immediately below (one-frame-latency, no stall).
    reap_completed_vblanks(state, devices);

    let clear = Color32F::new(
        HART_SPLASH_RGBA[0],
        HART_SPLASH_RGBA[1],
        HART_SPLASH_RGBA[2],
        HART_SPLASH_RGBA[3],
    );

    // M8 — the FULL z-order, built by the SHARED comp_core builder (killswitch → cursor
    // → windows-faded → layer-shell desktop), the SAME element list the winit backend
    // composites. PixmanRenderer satisfies the builder's `Renderer + ImportAll +
    // ImportMem` bounds, so the DRM desktop gets the cursor, the screen kill-switch, and
    // the map/workspace fades with ZERO parallel render code. The size is the output's
    // current physical mode (the capturable framebuffer extent).
    let size = comp_core::output_physical_size(state);
    // The renderer lives ON `state`, but `build_frame_elements` needs BOTH `&mut state`
    // (reads the space/cursor/effects, updates the killswitch buffer) AND `&mut renderer`
    // (imports surface textures) — disjoint fields the borrow checker can't see through
    // the `CompState` accessors. Swap the renderer OUT for the build (anvil's pattern),
    // then restore it. The placeholder is a fresh PixmanRenderer; if it can't be made we
    // skip this frame (the next tick retries) rather than panic — the never-fail posture.
    let mut renderer = match PixmanRenderer::new() {
        Ok(placeholder) => std::mem::replace(&mut state.renderer, placeholder),
        Err(err) => {
            warn!(?err, "HART-comp DRM: could not allocate a render scratch renderer; skipping frame");
            return;
        }
    };
    let elements: Vec<HartRenderElement<PixmanRenderer>> =
        comp_core::build_frame_elements(state, &mut renderer, size);

    let now = std::time::Instant::now();
    for device in devices.values_mut() {
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
            // `render_frame` returns a result that BORROWS the compositor; reduce it to the
            // Copy `is_empty` bool immediately so no borrow of `surface.compositor` spans
            // the sibling-field writes (`awaiting_vblank`/`flip_queued_at`) below.
            let render_outcome = surface
                .compositor
                .render_frame::<_, _>(&mut renderer, &elements, clear, FrameFlags::DEFAULT)
                .map(|result| result.is_empty);
            match render_outcome {
                Ok(true) => {
                    // Nothing changed — no flip to schedule, no vblank to await.
                    continue;
                }
                Ok(false) => match surface.compositor.queue_frame(()) {
                    // The flip is in flight; gate this CRTC until its vblank lands.
                    Ok(()) => {
                        surface.awaiting_vblank = true;
                        surface.flip_queued_at = Some(now);
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
                // render_frame failed (renderer import / no free slot / device inactive):
                // skip THIS frame, retry next tick. Never a panic (#186).
                Err(err) => warn!(?err, ?crtc, "HART-comp DRM: render_frame failed — degrading, retry next frame"),
            }
        }
    }
    // Restore the real renderer (it carries no per-frame state for pixman, but keeping the
    // ORIGINAL instance avoids re-allocating its internal caches every tick).
    state.renderer = renderer;
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
}
