// ════════════════════════════════════════════════════════════════════════════
// HART-comp — Milestone 1: a REAL running compositor, winit backend (WSL/WSLg).
// ════════════════════════════════════════════════════════════════════════════
//
// This is the FIRST actually-running HART-comp: not the skeleton in main.rs, not
// the DRM CI-draft in wayland.rs, but a compositor that boots, nests in an
// existing Wayland host (WSLg's wayland-0) as a winit/wayland-client window,
// creates its OWN wayland-N socket, and PAINTS real client surfaces with the
// GlesRenderer. No DRM/KMS — runnable on any box with a Wayland host + EGL.
//
// ── Why a SEPARATE module + a winit-flavoured State (NOT a parallel path) ──
//   • `wayland.rs` is the DRM/real-hardware backend: it holds a `PixmanRenderer`
//     and a huge XWayland/foreign-toplevel/decoration handler set drafted against
//     a LATER Smithay API. It is gated behind the `smithay` (DRM) cargo feature
//     and is CI-only; it does not compile on this rev as-is.
//   • This file is gated behind the DISTINCT `winit` cargo feature. winit's
//     renderer MUST be `GlesRenderer` (winit::init::<GlesRenderer>() requires
//     renderer_gl + backend_egl; pixman is not a winit renderer), so the State's
//     renderer type necessarily differs from the DRM path — the two backends own
//     two backend-shaped States by construction. That is the design the M1 plan
//     mandates ("factor State construction so the renderer type is the backend's"),
//     not a second copy of one canonical type.
//   • The NO-PHANTOM-WINDOW bookkeeping is NOT duplicated: this State embeds the
//     SAME pure `WindowRegistry` / `SummonResolver` / `ToplevelKind` from main.rs,
//     so a handle is still minted ONLY on a real map, here too.
//
// ── Modelled 1:1 on the pinned Smithay rev (47843391…) ──
//   examples/minimal.rs (the canonical minimal winit server compositor for THIS
//   rev) + anvil/src/winit.rs. NOTE the rev migrated to the unified
//   `delegate_dispatch2!(State)` macro: the per-protocol `delegate_compositor!`/
//   `delegate_shm!`/… macros named in older guides DO NOT EXIST on this rev — one
//   `delegate_dispatch2!` generates every Dispatch/GlobalDispatch impl from the
//   Handler traits we impl below. Verified against the checked-out source.

#![cfg(feature = "winit")]

use std::sync::Arc;
use std::time::{Duration, Instant};

use smithay::{
    backend::{
        renderer::{
            Color32F, Frame, Renderer,
            element::{
                Kind,
                surface::{WaylandSurfaceRenderElement, render_elements_from_surface_tree},
            },
            gles::GlesRenderer,
            utils::{draw_render_elements, on_commit_buffer_handler},
        },
        winit::{self, WinitEvent},
    },
    desktop::{Space, Window, WindowSurfaceType},
    input::{Seat, SeatHandler, SeatState, pointer::CursorImageStatus},
    output::{Mode, Output, PhysicalProperties, Subpixel},
    reexports::{
        calloop::{EventLoop, LoopHandle},
        wayland_server::{
            Display, DisplayHandle,
            backend::{ClientData, ClientId, DisconnectReason},
            protocol::{wl_buffer::WlBuffer, wl_output, wl_seat::WlSeat, wl_surface::WlSurface},
            Client,
        },
    },
    utils::{Serial, Transform},
    wayland::{
        buffer::BufferHandler,
        compositor::{
            CompositorClientState, CompositorHandler, CompositorState, get_parent,
            is_sync_subsurface, with_states,
        },
        output::OutputManagerState,
        selection::{
            SelectionHandler,
            data_device::{DataDeviceHandler, DataDeviceState, WaylandDndGrabHandler},
        },
        shell::{
            wlr_layer::{
                Layer, LayerSurface as WlrLayerSurface, WlrLayerShellHandler, WlrLayerShellState,
            },
            xdg::{
                PopupSurface, PositionerState, ToplevelSurface, XdgShellHandler, XdgShellState,
                XdgToplevelSurfaceData,
            },
        },
        shm::{ShmHandler, ShmState},
        socket::ListeningSocketSource,
    },
};
use smithay::desktop::LayerSurface;
// `Window::wl_surface()` is provided by the `WaylandFocus` trait on this rev (not
// an inherent method) — it MUST be in scope for `window_for_surface` to call it.
use smithay::wayland::seat::WaylandFocus;
use tracing::{error, info, warn};

use crate::{
    BootConfig, HART_SPLASH_RGBA, ToplevelKind, WindowHandle, WindowRegistry, select_render_path,
};

/// Last painted wlr-layer-surface count, so the render loop logs a one-line
/// transition (0→N / N→0) instead of spamming every frame. Pure observability.
static LAYERS_PAINTED: std::sync::atomic::AtomicUsize = std::sync::atomic::AtomicUsize::new(0);

// ────────────────────────────────────────────────────────────────────────────
// Per-client state. Carries the compositor-side client bookkeeping Smithay needs.
// ────────────────────────────────────────────────────────────────────────────
#[derive(Default)]
pub struct ClientState {
    pub compositor_state: CompositorClientState,
}
impl ClientData for ClientState {
    fn initialized(&self, _client_id: ClientId) {}
    fn disconnected(&self, _client_id: ClientId, _reason: DisconnectReason) {}
}

// ────────────────────────────────────────────────────────────────────────────
// The live compositor State (winit backend). `windows` is the SAME pure
// no-phantom-window registry from main.rs; everything else is Smithay protocol
// state + the winit GlesRenderer-driven desktop.
// ────────────────────────────────────────────────────────────────────────────
pub struct State {
    pub dh: DisplayHandle,
    pub loop_handle: LoopHandle<'static, State>,
    pub running: bool,
    pub start_time: Instant,

    /// The desktop window tree (xdg toplevels mapped above the layer-shell desktop).
    pub space: Space<Window>,
    /// The manifest↔toplevel map (no-phantom-window source of truth, from main.rs).
    pub windows: WindowRegistry,

    // ── Smithay protocol globals (the S4 minimal set a shm/xdg client needs) ──
    pub compositor_state: CompositorState,
    pub xdg_shell_state: XdgShellState,
    pub shm_state: ShmState,
    pub output_manager_state: OutputManagerState,
    pub layer_shell_state: WlrLayerShellState,
    pub data_device_state: DataDeviceState,
    pub seat_state: SeatState<State>,
    pub seat: Seat<State>,

    /// The single winit output (the HART-comp window inside WSLg).
    pub output: Output,
}

impl State {
    /// Reverse-lookup the live `Window` whose ROOT surface is `surface`. Callers
    /// walk to the root via `get_parent` before calling this, so matching the
    /// window's main `wl_surface()` is sufficient (mirrors anvil's
    /// `window_for_surface`). `Window::wl_surface()` comes from `WaylandFocus`.
    fn window_for_surface(&self, surface: &WlSurface) -> Option<Window> {
        self.space
            .elements()
            .find(|w| w.wl_surface().map(|s| &*s == surface).unwrap_or(false))
            .cloned()
    }

    /// The single mint site for THIS backend (mirrors wayland.rs::on_real_map):
    /// a handle is minted ONLY on a real map, here too. M1 does not yet thread a
    /// SummonApp manifest in (no IPC launcher wired on the winit path), so every
    /// map is an externally-opened window (`manifest_id = None`) — but the honesty
    /// invariant (handle ⇒ a toplevel mapped) holds identically.
    fn on_real_map(
        &mut self,
        app_id: Option<String>,
        title: Option<String>,
        kind: ToplevelKind,
    ) -> WindowHandle {
        let handle = self.windows.on_map(None, kind, app_id, title);
        info!(handle = handle.as_str(), "window.opened (toplevel mapped + painted)");
        handle
    }
}

// ── BufferHandler ───────────────────────────────────────────────────────────
impl BufferHandler for State {
    fn buffer_destroyed(&mut self, _buffer: &WlBuffer) {}
}

// ── CompositorHandler — the REAL map edge lives here (first buffer commit) ────
impl CompositorHandler for State {
    fn compositor_state(&mut self) -> &mut CompositorState {
        &mut self.compositor_state
    }

    fn client_compositor_state<'a>(&self, client: &'a Client) -> &'a CompositorClientState {
        &client
            .get_data::<ClientState>()
            .expect("client without ClientState")
            .compositor_state
    }

    fn commit(&mut self, surface: &WlSurface) {
        // Latch the newly-committed buffer into Smithay's surface state — without
        // this the surface has no content and nothing paints.
        on_commit_buffer_handler::<Self>(surface);

        if is_sync_subsurface(surface) {
            return;
        }

        // Walk to the root surface (the toplevel's surface).
        let mut root = surface.clone();
        while let Some(parent) = get_parent(&root) {
            root = parent;
        }

        // Drive the window's per-commit bookkeeping + the initial configure.
        if let Some(window) = self.window_for_surface(&root) {
            window.on_commit();

            // THE MAP EDGE (the no-phantom-window mint site): a toplevel is "mapped"
            // the first time it commits a buffer. We detect that transition by:
            //   (a) the commit being on the ROOT (toplevel) surface, AND
            //   (b) the window not yet carrying a WindowHandle (so we mint once), AND
            //   (c) the surface now actually having a buffer (is_mapped).
            // Only THEN do we mint the handle via `on_real_map` — exactly like
            // wayland.rs's xdg path, so a handle still proves a real map here too.
            let already_mapped = window.user_data().get::<WindowHandle>().is_some();
            if &root == surface && !already_mapped && surface_has_buffer(surface) {
                if let Some(toplevel) = window.toplevel() {
                    let app_id = toplevel_app_id(toplevel);
                    let title = toplevel_title(toplevel);
                    let handle = self.on_real_map(app_id, title, ToplevelKind::Xdg);
                    // Stash the handle on the Window so destroy can reverse it.
                    if let Some(w) = self.window_for_surface(&root) {
                        w.user_data().insert_if_missing(|| handle);
                    }
                }
            }
        }
        ensure_initial_configure(self, surface);
    }
}

// ── ShmHandler ───────────────────────────────────────────────────────────────
impl ShmHandler for State {
    fn shm_state(&self) -> &ShmState {
        &self.shm_state
    }
}

// ── XdgShellHandler — native Wayland toplevels (weston-simple-shm/foot map here)─
impl XdgShellHandler for State {
    fn xdg_shell_state(&mut self) -> &mut XdgShellState {
        &mut self.xdg_shell_state
    }

    fn new_toplevel(&mut self, surface: ToplevelSurface) {
        // Wrap as a desktop Window and place it; it is NOT mapped until the client
        // commits its first buffer (detected in `commit` → `ensure_initial_configure`).
        let window = Window::new_wayland_window(surface.clone());
        self.space.map_element(window, (0, 0), true);
        // Send the initial configure so the client can commit its first buffer.
        surface.send_configure();
    }

    fn new_popup(&mut self, _surface: PopupSurface, _positioner: PositionerState) {}

    fn grab(&mut self, _surface: PopupSurface, _seat: WlSeat, _serial: Serial) {}

    fn reposition_request(
        &mut self,
        _surface: PopupSurface,
        _positioner: PositionerState,
        _token: u32,
    ) {
    }

    fn toplevel_destroyed(&mut self, surface: ToplevelSurface) {
        let wl = surface.wl_surface().clone();
        if let Some(window) = self.window_for_surface(&wl) {
            if let Some(handle) = window.user_data().get::<WindowHandle>().cloned() {
                if self.windows.on_unmap(&handle) {
                    info!(handle = handle.as_str(), "window.closed (toplevel destroyed)");
                }
            }
            self.space.unmap_elem(&window);
        }
    }
}

// ── WlrLayerShellHandler — the glass-shell desktop mounts as a BACKGROUND layer ─
impl WlrLayerShellHandler for State {
    fn shell_state(&mut self) -> &mut WlrLayerShellState {
        &mut self.layer_shell_state
    }

    fn new_layer_surface(
        &mut self,
        surface: WlrLayerSurface,
        _wl_output: Option<wl_output::WlOutput>,
        _layer: Layer,
        namespace: String,
    ) {
        let output = self.output.clone();
        let mut map = smithay::desktop::layer_map_for_output(&output);
        // map_layer arranges + tracks the layer surface; the initial configure is
        // sent from `ensure_initial_configure` on its first commit.
        let ns = namespace.clone();
        match map.map_layer(&LayerSurface::new(surface, namespace)) {
            Ok(()) => info!(
                namespace = %ns,
                layer = ?_layer,
                "layer.mapped (wlr-layer-shell surface tracked — the glass-shell desktop mount point)"
            ),
            Err(err) => warn!(?err, namespace = %ns, "failed to map layer surface"),
        }
    }

    fn layer_destroyed(&mut self, surface: WlrLayerSurface) {
        let output = self.output.clone();
        let mut map = smithay::desktop::layer_map_for_output(&output);
        let found = map
            .layers()
            .find(|l| l.layer_surface() == &surface)
            .cloned();
        if let Some(layer) = found {
            map.unmap_layer(&layer);
        }
    }
}

// ── SeatHandler — WlSurface satisfies all three input-focus targets on this rev ─
impl SeatHandler for State {
    type KeyboardFocus = WlSurface;
    type PointerFocus = WlSurface;
    type TouchFocus = WlSurface;

    fn seat_state(&mut self) -> &mut SeatState<State> {
        &mut self.seat_state
    }

    fn focus_changed(&mut self, _seat: &Seat<Self>, _focused: Option<&WlSurface>) {}
    fn cursor_image(&mut self, _seat: &Seat<Self>, _image: CursorImageStatus) {}
}

// ── SelectionHandler + DataDevice* — required by the dispatch2 protocol bundle ──
impl SelectionHandler for State {
    type SelectionUserData = ();
}
impl DataDeviceHandler for State {
    fn data_device_state(&mut self) -> &mut DataDeviceState {
        &mut self.data_device_state
    }
}
impl WaylandDndGrabHandler for State {}

// ── OutputHandler — required for the output global's dispatch ──
impl smithay::wayland::output::OutputHandler for State {}

// One macro generates every Dispatch/GlobalDispatch impl from the Handler traits
// above (the unified dispatch model on this Smithay rev).
smithay::delegate_dispatch2!(State);

/// Send the initial xdg/layer configure once, on the surface's first commit, so
/// the client can proceed to attach a buffer (the map edge). Modelled on anvil's
/// `ensure_initial_configure` (trimmed to xdg-toplevel + layer-surface for M1).
fn ensure_initial_configure(state: &mut State, surface: &WlSurface) {
    // xdg toplevel?
    if let Some(window) = state.window_for_surface(surface) {
        if let Some(toplevel) = window.toplevel() {
            let initial_configure_sent = with_states(surface, |states| {
                states
                    .data_map
                    .get::<XdgToplevelSurfaceData>()
                    .map(|d| d.lock().unwrap().initial_configure_sent)
                    .unwrap_or(false)
            });
            if !initial_configure_sent {
                toplevel.send_configure();
            }
        }
        return;
    }

    // wlr layer surface?
    let output = state.output.clone();
    let mut map = smithay::desktop::layer_map_for_output(&output);
    if let Some(layer) = map.layer_for_surface(surface, WindowSurfaceType::TOPLEVEL).cloned() {
        map.arrange();
        let initial_configure_sent = with_states(surface, |states| {
            states
                .data_map
                .get::<smithay::wayland::shell::wlr_layer::LayerSurfaceData>()
                .map(|d| d.lock().unwrap().initial_configure_sent)
                .unwrap_or(false)
        });
        if !initial_configure_sent {
            layer.layer_surface().send_configure();
        }
    }
}

/// Spawn a Wayland test client against HART-comp's OWN socket so a window maps.
/// Tries a sequence of clients (terminal first, then the simple-shm demo) so M1's
/// "a client surface composited" bar is met on whatever is installed. The child
/// inherits `WAYLAND_DISPLAY=<our socket>` so it connects to US, not the host.
///
/// Set `HART_COMP_NO_TEST_CLIENT=1` to suppress the auto-client entirely — used in
/// Milestone 2 when an EXTERNAL client (swaybg / the WebKit glass-shell host) is
/// attached deliberately and the auto foot toplevel would only add noise. The map
/// bar is still met by that external client; this just hands control of "what binds"
/// to the harness.
fn spawn_test_client(socket_name: &str) {
    if std::env::var_os("HART_COMP_NO_TEST_CLIENT").is_some() {
        info!(
            socket = socket_name,
            "HART_COMP_NO_TEST_CLIENT set — not spawning the auto test client (attach one with WAYLAND_DISPLAY={socket_name})"
        );
        return;
    }
    let candidates: &[&[&str]] = &[
        &["foot"],
        &["weston-terminal"],
        &["weston-simple-shm"],
        &["weston-simple-egl"],
    ];
    for argv in candidates {
        let prog = argv[0];
        if which(prog).is_none() {
            continue;
        }
        let mut cmd = std::process::Command::new(prog);
        cmd.args(&argv[1..]);
        cmd.env("WAYLAND_DISPLAY", socket_name);
        // Make sure the child does NOT inherit a stale display that points at the host.
        match cmd.spawn() {
            Ok(child) => {
                info!(client = prog, pid = child.id(), socket = socket_name, "spawned test client");
                return;
            }
            Err(err) => warn!(client = prog, ?err, "failed to spawn test client"),
        }
    }
    warn!("no test client found (tried foot/weston-terminal/weston-simple-shm); map a client manually with WAYLAND_DISPLAY={socket_name}");
}

/// `which`-style lookup on PATH (avoids a hard dep just for this).
fn which(prog: &str) -> Option<std::path::PathBuf> {
    let path = std::env::var_os("PATH")?;
    std::env::split_paths(&path).find_map(|dir| {
        let cand = dir.join(prog);
        if cand.is_file() { Some(cand) } else { None }
    })
}

/// THE compositor: boot the winit backend nested in the host Wayland (WSLg),
/// create our own socket, run the calloop loop, and paint client surfaces.
/// This is what the skeleton's `run_event_loop` never did.
pub fn run_winit(cfg: &BootConfig) -> Result<(), Box<dyn std::error::Error>> {
    // The render path decision is shared with the skeleton; on the winit path the
    // GlesRenderer is the concrete renderer (software-or-hardware is Mesa's call
    // under WSLg — llvmpipe/d3d12). We still log the decision for parity.
    let _ = select_render_path(cfg);

    // 1. The calloop event loop + our OWN server display. The `Display` is owned
    //    here in the loop (NOT inserted as a calloop Generic source) and dispatched
    //    directly each iteration — this avoids the `unsafe { display.get_mut() }`
    //    that anvil's Generic-source pattern needs, which the crate's
    //    `#![forbid(unsafe_code)]` would reject. Mirrors examples/minimal.rs.
    let mut event_loop: EventLoop<State> = EventLoop::try_new()?;
    let mut display: Display<State> = Display::new()?;
    let dh = display.handle();

    // 2. The winit backend — transparently connects to the host's wayland-0 as a
    //    winit/wayland-client WINDOW, and gives us a GlesRenderer bound to its EGL
    //    surface. THIS is the nesting in WSLg.
    let (mut backend, mut winit) = match winit::init::<GlesRenderer>() {
        Ok(ret) => ret,
        Err(err) => {
            error!(?err, "failed to initialize the winit backend (need a Wayland host + EGL; in WSL ensure WAYLAND_DISPLAY=wayland-0 and libgles2)");
            return Err(format!("winit init failed: {err}").into());
        }
    };
    let win_size = backend.window_size();

    // 3. Our OWN listening socket (wayland-N). This is the display the TEST CLIENT
    //    connects to — distinct from the host's wayland-0 we are nested in.
    let source = ListeningSocketSource::new_auto()?;
    let socket_name = source.socket_name().to_string_lossy().into_owned();
    event_loop
        .handle()
        .insert_source(source, move |client_stream, _, state| {
            if let Err(err) = state
                .dh
                .insert_client(client_stream, Arc::new(ClientState::default()))
            {
                warn!(?err, "failed to add wayland client");
            }
        })?;
    info!(socket = socket_name, "HART-comp listening on its own wayland socket");

    // 4. The output (the HART-comp window). Size = the winit window size.
    let mode = Mode {
        size: win_size,
        refresh: 60_000,
    };
    let output = Output::new(
        "hart-winit".to_string(),
        PhysicalProperties {
            size: (0, 0).into(),
            subpixel: Subpixel::Unknown,
            make: "HART".into(),
            model: "winit".into(),
            serial_number: "0".into(),
        },
    );
    let _output_global = output.create_global::<State>(&dh);
    output.change_current_state(Some(mode), Some(Transform::Flipped180), None, Some((0, 0).into()));
    output.set_preferred(mode);

    // 5. The protocol globals (the S4 minimal set a shm/xdg client needs to map).
    let compositor_state = CompositorState::new::<State>(&dh);
    let xdg_shell_state = XdgShellState::new::<State>(&dh);
    let shm_state = ShmState::new::<State>(&dh, vec![]);
    let output_manager_state = OutputManagerState::new_with_xdg_output::<State>(&dh);
    let layer_shell_state = WlrLayerShellState::new::<State>(&dh);
    let data_device_state = DataDeviceState::new::<State>(&dh);
    let mut seat_state = SeatState::new();
    let mut seat = seat_state.new_wl_seat(&dh, "hart-winit");
    let _keyboard = seat.add_keyboard(Default::default(), 200, 25)?;
    let _pointer = seat.add_pointer();

    let mut space: Space<Window> = Space::default();
    space.map_output(&output, (0, 0));

    // NOTE: ShmState::new(.., vec![]) already advertises the two MANDATORY shm
    // formats (Argb8888 + Xrgb8888) that weston-simple-shm/foot use — no
    // `update_formats(renderer.shm_formats())` needed (that would require the
    // `ImportMemWl` trait import just to add the same mandatory formats). Matches
    // examples/minimal.rs, which maps shm clients with exactly `vec![]`.

    let mut state = State {
        dh: dh.clone(),
        loop_handle: event_loop.handle(),
        running: true,
        start_time: Instant::now(),
        space,
        windows: WindowRegistry::new(),
        compositor_state,
        xdg_shell_state,
        shm_state,
        output_manager_state,
        layer_shell_state,
        data_device_state,
        seat_state,
        seat,
        output: output.clone(),
    };

    // 6. (No calloop Generic source for the Display — see step 1. The Display is
    //    dispatched directly in the loop below, the safe-code minimal.rs pattern.)

    // 7. Spawn a test client so a window MAPS + PAINTS (the M1 done-bar). It
    //    connects to OUR socket, not the host's.
    spawn_test_client(&socket_name);

    info!(
        socket = socket_name,
        size = ?win_size,
        "HART-comp winit compositor initialized — entering the loop (THE thing the skeleton never did)"
    );

    // 8. THE LOOP. Pump winit events, render the desktop, dispatch + flush clients.
    while state.running {
        // ── (a) Pump winit (host) events: resize / input / close / redraw. ──
        let status = winit.dispatch_new_events(|event| match event {
            WinitEvent::Resized { size, .. } => {
                let new_mode = Mode { size, refresh: 60_000 };
                output.change_current_state(Some(new_mode), None, None, None);
                output.set_preferred(new_mode);
                state.space.map_output(&output, (0, 0));
            }
            // M1: input is a no-op (the done-bar is map+paint, not interaction).
            // The seat exists so clients bind wl_seat; routing winit input into it
            // is Milestone 2.
            WinitEvent::Input(_) => {}
            WinitEvent::CloseRequested => {
                info!("winit window close requested — shutting down");
                state.running = false;
            }
            _ => {}
        });
        if let smithay::reexports::winit::event_loop::pump_events::PumpStatus::Exit(_) = status {
            state.running = false;
            break;
        }

        // ── (b) Render the desktop into the winit framebuffer. ──
        let size = backend.window_size();
        let damage = [smithay::utils::Rectangle::from_size(size)];
        let render_result = (|| -> Result<(), Box<dyn std::error::Error>> {
            let (renderer, mut framebuffer) = backend.bind()?;

            // Build paint elements: every mapped xdg toplevel + every layer
            // surface, newest on top. Modelled on minimal.rs's proven
            // render_elements_from_surface_tree path (no Space render dependency,
            // so it compiles cleanly on this rev) while still honouring z-order:
            // layer-shell BACKGROUND first (drawn at the bottom), toplevels above.
            let mut elements: Vec<WaylandSurfaceRenderElement<GlesRenderer>> = Vec::new();

            // toplevels (above)
            for toplevel in state.xdg_shell_state.toplevel_surfaces() {
                elements.extend(render_elements_from_surface_tree(
                    renderer,
                    toplevel.wl_surface(),
                    (0, 0),
                    1.0,
                    1.0,
                    Kind::Unspecified,
                ));
            }
            // layer surfaces (below the toplevels in this list = drawn under them,
            // since draw_render_elements paints front-to-back in slice order)
            let mut layers_painted = 0usize;
            {
                let map = smithay::desktop::layer_map_for_output(&output);
                for layer in map.layers() {
                    layers_painted += 1;
                    let loc = map.layer_geometry(layer).map(|g| g.loc).unwrap_or_default();
                    elements.extend(render_elements_from_surface_tree(
                        renderer,
                        layer.wl_surface(),
                        (loc.x, loc.y),
                        1.0,
                        1.0,
                        Kind::Unspecified,
                    ));
                }
            }
            // Positive render proof, logged ONLY when the painted-layer count CHANGES
            // (0→1 when the glass-shell/swaybg layer maps, 1→0 on unmap) — so a tail of
            // the log shows "the layer surface is in the composited frame" without
            // spamming 60×/s. This is the BACKGROUND-layer paint signal the M2
            // "the background fills" gate needs.
            {
                let prev = LAYERS_PAINTED.swap(layers_painted, std::sync::atomic::Ordering::Relaxed);
                if prev != layers_painted {
                    info!(
                        layers_painted,
                        "layer.composited (wlr-layer surfaces now in the rendered frame)"
                    );
                }
            }

            let mut frame = renderer.render(&mut framebuffer, size, Transform::Flipped180)?;
            // HART splash clear — the brand color, so a slow client never flashes black.
            let clear = Color32F::new(
                HART_SPLASH_RGBA[0],
                HART_SPLASH_RGBA[1],
                HART_SPLASH_RGBA[2],
                HART_SPLASH_RGBA[3],
            );
            frame.clear(clear, &damage)?;
            draw_render_elements(&mut frame, 1.0, &elements, &damage)?;
            let _sync = frame.finish()?;
            Ok(())
        })();
        if let Err(err) = render_result {
            warn!(?err, "render error");
        }

        // ── (c) Send frame callbacks so clients draw their NEXT frame. ──
        let now_ms = state.start_time.elapsed().as_millis() as u32;
        for toplevel in state.xdg_shell_state.toplevel_surfaces() {
            send_frame_callbacks(toplevel.wl_surface(), now_ms);
        }
        {
            let map = smithay::desktop::layer_map_for_output(&output);
            for layer in map.layers() {
                send_frame_callbacks(layer.wl_surface(), now_ms);
            }
        }

        // ── (d) Submit the winit frame to the host compositor. ──
        if let Err(err) = backend.submit(Some(&damage)) {
            warn!(?err, "failed to submit winit frame");
        }

        // ── (e) Dispatch the calloop socket source (accepts new clients), then the
        //    Wayland display (processes those clients' requests into our handlers),
        //    refresh the space, and flush. The Display is dispatched DIRECTLY (not
        //    via a calloop Generic source) so no `unsafe` is needed.
        if event_loop
            .dispatch(Some(Duration::from_millis(16)), &mut state)
            .is_err()
        {
            state.running = false;
            continue;
        }
        if let Err(err) = display.dispatch_clients(&mut state) {
            warn!(?err, "failed to dispatch wayland clients");
        }
        state.space.refresh();
        if let Err(err) = display.flush_clients() {
            warn!(?err, "failed to flush clients");
        }
    }

    info!("HART-comp winit compositor exited cleanly");
    Ok(())
}

/// Drain a surface tree's frame callbacks (mirrors minimal.rs's helper). Without
/// this, well-behaved clients (which wait for the frame callback before drawing
/// the next frame) freeze after the first frame.
fn send_frame_callbacks(surface: &WlSurface, time: u32) {
    use smithay::wayland::compositor::{
        SurfaceAttributes, TraversalAction, with_surface_tree_downward,
    };
    with_surface_tree_downward(
        surface,
        (),
        |_, _, &()| TraversalAction::DoChildren(()),
        |_surf, states, &()| {
            for callback in states
                .cached_state
                .get::<SurfaceAttributes>()
                .current()
                .frame_callbacks
                .drain(..)
            {
                callback.done(time);
            }
        },
        |_, _, &()| true,
    );
}

/// Has `surface` actually got a committed buffer latched (i.e. is it MAPPED)? This
/// is the post-`on_commit_buffer_handler` truth: the renderer surface state holds
/// the latched buffer only once the client attached + committed one. Used as the
/// map-edge gate in `commit` (no handle is minted until this is true).
fn surface_has_buffer(surface: &WlSurface) -> bool {
    use smithay::backend::renderer::utils::RendererSurfaceStateUserData;
    with_states(surface, |states| {
        states
            .data_map
            .get::<RendererSurfaceStateUserData>()
            .map(|d| d.lock().unwrap().buffer().is_some())
            .unwrap_or(false)
    })
}

/// Read a toplevel's `app_id` (the join key the brain's launcher would tag).
fn toplevel_app_id(toplevel: &ToplevelSurface) -> Option<String> {
    with_states(toplevel.wl_surface(), |states| {
        states
            .data_map
            .get::<XdgToplevelSurfaceData>()
            .and_then(|d| d.lock().unwrap().app_id.clone())
    })
}

/// Read a toplevel's `title`.
fn toplevel_title(toplevel: &ToplevelSurface) -> Option<String> {
    with_states(toplevel.wl_surface(), |states| {
        states
            .data_map
            .get::<XdgToplevelSurfaceData>()
            .and_then(|d| d.lock().unwrap().title.clone())
    })
}
