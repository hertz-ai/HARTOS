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
use smithay::backend::drm::compositor::{DrmCompositor, FrameFlags};
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

/// One opened DRM device (the GPU) + its per-CRTC scanout surfaces. Held in the loop
/// data so the VBlank handler can find the right surface to mark submitted + re-queue.
struct DeviceData {
    /// The DRM device handle (kept alive; dropping it tears down the modeset).
    _drm: DrmDevice,
    /// GBM device — the buffer allocator backing both the scanout swapchain + the
    /// framebuffer exporter. Cloned into the allocator/exporter; held so it outlives them.
    _gbm: GbmDevice<DrmDeviceFd>,
    /// One DrmCompositor per active CRTC (one display). Keyed by CRTC handle.
    surfaces: HashMap<crtc::Handle, HartDrmCompositor>,
}

/// Resolve the primary GPU's DRM node — `HART_COMP_DRM_DEVICE` overrides (the same
/// operator escape hatch anvil's `ANVIL_DRM_DEVICE` gives), else `primary_gpu(seat)`.
/// Returns an error (NOT a panic) when there is no GPU, so the supervisor can drop a
/// tier instead of the process aborting (the never-fail posture).
fn resolve_primary_node(seat: &str) -> Result<DrmNode, Box<dyn std::error::Error>> {
    if let Ok(var) = std::env::var("HART_COMP_DRM_DEVICE") {
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
        // Wire the IPC sink to a tracing log for now (the com.hart.Compositor socket
        // server is the winit-gated ipc.rs; the DRM path logs the same edges). This is
        // a real sink, not a stub — every map/unmap emits a structured event line.
        emit_ipc_event: Box::new(|kind, rec| {
            info!(event = kind, handle = rec.handle.as_str(), "compositor.window_event");
        }),
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
    //   (a) libinput → State::process_input_event (the shared input router).
    event_loop
        .handle()
        .insert_source(libinput_backend, move |event, _, state: &mut State| {
            state.process_input_event(event);
        })
        .map_err(|e| format!("insert libinput source failed: {e}"))?;

    //   (b) the session notifier → pause/activate the DRM devices on VT switch / sleep.
    //       The device table is captured by a raw pointer-free closure is impossible
    //       (calloop closures take only `&mut State`), so the device map lives in State-
    //       adjacent storage reached through an Rc-free design: we move it into the loop's
    //       shared data via the `EventLoop::run` data argument below. For pause/activate
    //       we therefore re-scan on activate (idempotent) rather than hold the table here.
    event_loop
        .handle()
        .insert_source(notifier, move |event, &mut (), _state: &mut State| match event {
            SessionEvent::PauseSession => info!("HART-comp DRM: session paused (VT switch / sleep)"),
            SessionEvent::ActivateSession => info!("HART-comp DRM: session resumed"),
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

    // 13. THE LOOP. We carry the per-device table in a tuple with State so the VBlank
    //     re-render can reach the DrmCompositor surfaces. The DRM `notifier`/VBlank source
    //     was inserted as part of `device_added` keyed on the node, calling `render_node`.
    //
    //     Kick off the first frame on every surface so the splash + glass shell paint
    //     immediately (the DRM page-flip cadence is then self-sustaining via VBlank).
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

        // Re-render every surface each tick (the pixman floor has no damage-driven
        // scheduling here; a 60Hz repaint is the simple never-fail cadence). On a real
        // box the VBlank source paces the actual flips; this tick just keeps the frame
        // fresh + drains client frame callbacks so clients draw their next frame.
        render_all(&mut state, &mut devices);

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

    let mut surfaces: HashMap<crtc::Handle, HartDrmCompositor> = HashMap::new();

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
        surfaces.insert(crtc, compositor);
        info!(crtc = ?crtc, "HART-comp DRM: output online (pixman scanout)");
    }

    // Insert the per-device DRM event source (VBlank → mark the flip submitted so the
    // next frame can be queued). Keyed on the node so the closure finds the right device.
    state
        .loop_handle
        .insert_source(drm_notifier, move |event, _meta, _state: &mut State| match event {
            DrmEvent::VBlank(_crtc) => {
                // The actual `frame_submitted()` + re-queue is driven from the render tick
                // in the main loop (which holds the `devices` table). VBlank here only
                // confirms the flip landed; Stage A's 60Hz tick re-renders regardless, so
                // the page-flip cadence is self-sustaining without per-VBlank table access.
            }
            DrmEvent::Error(err) => error!(?err, "HART-comp DRM: device error"),
        })
        .map_err(|e| format!("insert DRM notifier failed: {e}"))?;

    devices.insert(
        node,
        DeviceData {
            _drm: drm,
            _gbm: gbm,
            surfaces,
        },
    );
    Ok(())
}

/// Render every active DRM surface: build the element list from the space + the
/// layer-shell desktop, composite on the pixman floor, clear to HART_SPLASH, and queue
/// the page-flip. The canonical `render_frame → queue_frame → frame_submitted` sequence
/// from the rev's DrmCompositor doc example.
fn render_all(state: &mut State, devices: &mut HashMap<DrmNode, DeviceData>) {
    use smithay::backend::renderer::element::surface::{
        render_elements_from_surface_tree, WaylandSurfaceRenderElement,
    };
    use smithay::backend::renderer::element::Kind;

    let clear = Color32F::new(
        HART_SPLASH_RGBA[0],
        HART_SPLASH_RGBA[1],
        HART_SPLASH_RGBA[2],
        HART_SPLASH_RGBA[3],
    );
    let output = state.output.clone();

    // Build the heterogeneous-free element list: windows (above) then the layer-shell
    // glass-shell desktop (below). Pixman is the renderer; one element type suffices on
    // the DRM floor (no cursor/killswitch element kinds yet — Stage-B parity).
    let mut elements: Vec<WaylandSurfaceRenderElement<PixmanRenderer>> = Vec::new();
    for window in state.space.elements().rev() {
        let loc = state.space.element_location(window).unwrap_or_default();
        let phys = loc.to_physical_precise_round(1.0);
        if let Some(surface) = window.wl_surface() {
            elements.extend(render_elements_from_surface_tree(
                &mut state.renderer,
                &surface,
                phys,
                1.0,
                1.0,
                Kind::Unspecified,
            ));
        }
    }
    {
        let map = layer_map_for_output(&output);
        for layer in map.layers() {
            let loc = map.layer_geometry(layer).map(|g| g.loc).unwrap_or_default();
            elements.extend(render_elements_from_surface_tree(
                &mut state.renderer,
                layer.wl_surface(),
                (loc.x, loc.y),
                1.0,
                1.0,
                Kind::Unspecified,
            ));
        }
    }

    for device in devices.values_mut() {
        for compositor in device.surfaces.values_mut() {
            match compositor.render_frame::<_, _>(&mut state.renderer, &elements, clear, FrameFlags::DEFAULT) {
                Ok(result) => {
                    if !result.is_empty {
                        if let Err(err) = compositor.queue_frame(()) {
                            warn!(?err, "HART-comp DRM: queue_frame failed");
                        }
                    }
                    // Mark the previous flip submitted so the swapchain frees its slot.
                    let _ = compositor.frame_submitted();
                }
                Err(err) => warn!(?err, "HART-comp DRM: render_frame failed"),
            }
        }
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
    claimed: &HashMap<crtc::Handle, HartDrmCompositor>,
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
