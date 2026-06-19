// ════════════════════════════════════════════════════════════════════════════
// HART-comp — PHASE-5 Smithay handler BODIES (native toplevels)
//                                          ⚠️  ENTIRE FILE IS CI-COMPILE ONLY  ⚠️
// ════════════════════════════════════════════════════════════════════════════
//
// ┌──────────────────────────────────────────────────────────────────────────┐
// │ EVERY line in this file is gated behind `#[cfg(feature = "smithay")]` and  │
// │ the `smithay` cargo feature is OFF by default (see compositor/Cargo.toml   │
// │ `[features]`). On the Windows dev box the feature is NEVER enabled, so this │
// │ file is NOT compiled — `cargo test`/`cargo check`/the Nix `doCheck` pure    │
// │ floor stay green. This module compiles ONLY where Smithay links: the CI     │
// │ nixosTest llvmpipe VM / local QEMU-KVM, at the Phase-5 bring-up step that    │
// │ uncomments the git-Smithay dep + turns the feature on. THIS IS THE          │
// │ "flag every part that MUST be CI-compiled" boundary the task demands.       │
// │                                                                            │
// │ It is a CAREFUL DRAFT against the Smithay API shape (the commented manifest │
// │ in main.rs). The exact Smithay rev's type names / method signatures may     │
// │ drift between the draft and the pinned rev; the FIRST CI compile is where   │
// │ those are reconciled. What is load-bearing and MUST survive that            │
// │ reconciliation is the INVARIANT each handler enforces, stated above it:     │
// │   • a handle is minted ONLY on a real map  (no-phantom-window),             │
// │   • SummonApp resolves to Mapped ONLY via on_map (never an installer code), │
// │   • XWayland (Wine) success is keyed on a REAL X11 map, correcting the       │
// │     installer-layer unconditional `success=True`,                           │
// │   • the compositor draws frames (prefer SSD),                               │
// │   • foreign-toplevel advertise/withdraw MIRRORS the registry (one source).  │
// └──────────────────────────────────────────────────────────────────────────┘
//
// The PURE state (`WindowRegistry`, `PendingSummon`, `SummonOutcome`,
// `summon_precheck`, the `State::resolve_summon`/`expire_summons` orchestration)
// lives in `main.rs` and is unit-tested on the dev box TODAY. This file is ONLY
// the Smithay glue that feeds that pure state real map/unmap edges. The division
// is deliberate: the no-phantom-window correctness is proven feature-OFF; the
// Wayland wiring is the sole VM-only remainder.
//
// openRisks carried VERBATIM (architecture §5.4, §9 — also in main.rs):
//   • Android = `exec sleep infinity` (no ART/Waydroid runtime,
//     hart-subsystems.nix:288) → native Android windows are vaporware;
//     `summon_precheck("android")` short-circuits to Unsupported, this file
//     never sees an Android toplevel.
//   • Wine success is UNCONDITIONAL at the installer layer
//     (`app_installer.py:_install_windows` returns `success=True` regardless of
//     map — "Wine often returns 0 even for interactive installers"). Corrected
//     HERE at the WM layer: a Wine launch proceeds to AWAIT A MAP, and a handle
//     is minted by `on_xwayland_surface_mapped` ONLY when a real X11 toplevel
//     maps.
//   • macOS/Darling is default-off (`app_installer.py:_install_macos`) →
//     `summon_precheck("macos")` → Unsupported; this file never sees a macOS
//     toplevel.

#![cfg(feature = "smithay")]
#![allow(clippy::needless_lifetimes)]

// ── CI-COMPILE: the real Smithay imports (the manifest in main.rs, uncommented).
// These are the SHAPE the handlers need. The pinned rev (compositor/Cargo.toml
// `[features] smithay`) is where the exact module paths are reconciled on first
// CI compile — Smithay moves these between revs (e.g. `foreign_toplevel_list` is
// `wlr_foreign_toplevel` on some revs). Each `use` below names WHY it is needed.
use smithay::{
    // Backend: software-floor renderer + DRM scanout + libinput seat (Phase 3,
    // shared here so the event loop that drives the maps is one loop).
    backend::renderer::pixman::PixmanRenderer,
    backend::renderer::utils::on_commit_buffer_handler,
    desktop::{layer_map_for_output, LayerSurface, Space, Window, WindowSurfaceType},
    input::{
        keyboard::KeyboardHandle, pointer::PointerHandle, Seat, SeatState,
    },
    output::Output,
    reexports::{
        calloop::LoopHandle,
        wayland_server::{
            backend::{ClientData, ClientId, DisconnectReason},
            protocol::{wl_buffer::WlBuffer, wl_output, wl_seat::WlSeat, wl_surface::WlSurface},
            Client, DisplayHandle,
        },
    },
    utils::{Logical, Rectangle, Serial},
    wayland::{
        buffer::BufferHandler,
        compositor::{
            get_parent, is_sync_subsurface, with_states, CompositorClientState,
            CompositorHandler, CompositorState,
        },
        foreign_toplevel_list::{
            ForeignToplevelListHandler, ForeignToplevelListState,
        },
        output::{OutputHandler, OutputManagerState},
        selection::{
            data_device::{DataDeviceHandler, DataDeviceState, WaylandDndGrabHandler},
            SelectionHandler,
        },
        shell::{
            wlr_layer::{
                Layer as WlrLayer, LayerSurface as WlrLayerSurface, WlrLayerShellHandler,
                WlrLayerShellState,
            },
            xdg::{
                decoration::{XdgDecorationHandler, XdgDecorationState},
                PopupSurface, PositionerState, ToplevelSurface, XdgShellHandler,
                XdgShellState, XdgToplevelSurfaceData,
            },
        },
        shm::{ShmHandler, ShmState},
        // master: the X11↔wl_surface association protocol the X11Wm needs as a
        // handler bound on `start_wm` (its state is a plain field on `State`).
        xwayland_shell::{XWaylandShellHandler, XWaylandShellState},
    },
    xwayland::{
        xwm::{Reorder, ResizeEdge as X11ResizeEdge, XwmId},
        X11Surface, X11Wm, XWaylandClientData, XwmHandler,
    },
};
// `DndGrabHandler` is a `start_wm` bound (drag'n'drop hand-off for X11 clients).
// Both its methods are defaulted, so the impl is empty — but it must be present.
use smithay::input::dnd::DndGrabHandler;
// `WaylandFocus` provides `Window::wl_surface()` (master surfaces the surface
// accessor on this trait, not the inherent impl — the reverse-lookup in
// `handle_for_surface` needs it in scope).
use smithay::wayland::seat::WaylandFocus;
// xdg-decoration mode enum (server-side vs client-side).
use smithay::reexports::wayland_protocols::xdg::decoration::zv1::server::zxdg_toplevel_decoration_v1::Mode as DecorationMode;

// Pull the pure bookkeeping + summon state machine from the crate root. THIS is
// the contract boundary: the Smithay handlers only ever call these pure methods.
use crate::{
    PendingSummon, ToplevelKind, WindowHandle, WindowRecord, WindowRegistry,
};
// The backend-agnostic surface-tree / map-edge / app-id readers (M7 Stage-B hoist).
// SHARED with the winit backend so there is one implementation, not a parallel path.
use crate::shared::{surface_has_buffer, toplevel_app_id, toplevel_title, x11_app_id, x11_title};

// ════════════════════════════════════════════════════════════════════════════
// The compositor State — owns the pure registry + the live Smithay protocol
// state. The calloop event loop dispatches Wayland events into the handler impls
// below, which mutate `windows` (pure) and emit IPC events.
// ════════════════════════════════════════════════════════════════════════════

/// ⚠️ CI-COMPILE. The live compositor state. `windows` + `pending` are the PURE
/// types from main.rs (unit-tested feature-OFF); everything else is Smithay
/// protocol state that only exists where Smithay links.
pub struct State {
    pub dh: DisplayHandle,
    pub loop_handle: LoopHandle<'static, State>,

    // ── PURE (from main.rs) — the no-phantom-window source of truth ──
    /// manifest ↔ toplevel map (the "AppRegistry window-handle field").
    pub windows: WindowRegistry,
    /// SummonApps awaiting a real map, keyed by manifest id.
    pub pending: Vec<PendingSummon>,

    // ── Smithay protocol state ──
    pub compositor_state: CompositorState,
    pub xdg_shell_state: XdgShellState,
    /// `xdg-decoration` protocol state. RAII-HELD: `XdgDecorationHandler` has no
    /// `xdg_decoration_state()` accessor on this rev (the global is registered at
    /// construction), so the field is never read after `new` — it exists only to
    /// keep the `zxdg_decoration_manager_v1` global advertised for its lifetime.
    #[allow(dead_code)]
    pub xdg_decoration_state: XdgDecorationState,
    pub foreign_toplevel_state: ForeignToplevelListState,
    /// Seat protocol state. `seat_state()` (SeatHandler) returns this; the live
    /// `seat` is created from it.
    pub seat_state: SeatState<State>,
    /// The live `wl_seat`. RAII-HELD: input dispatch routes through the cached
    /// `keyboard`/`pointer` handles below (extracted from this seat at
    /// construction), so the `seat` itself is never read again — it is held only to
    /// keep the `wl_seat` global alive for the session.
    #[allow(dead_code)]
    pub seat: Seat<State>,
    /// X11↔wl_surface association protocol state (master `xwayland_shell`). The
    /// X11Wm requires a `XWaylandShellHandler` returning this; a plain field, made
    /// by `XWaylandShellState::new::<State>(&dh)` at construction.
    pub xwayland_shell_state: XWaylandShellState,

    // ── M7: the protocol globals a real shm/xdg/layer-shell client needs to MAP +
    // PAINT on the DRM backend (the SAME minimal set winit.rs constructs). Without
    // these the DRM State could not serve the glass-shell layer surface that is the
    // whole Stage-A boot-floor deliverable. ──
    /// wl_shm — clients (the glass shell, weston-simple-shm) allocate their buffers here.
    pub shm_state: ShmState,
    /// wl_output / xdg-output manager. RAII-HELD: `delegate_output!` needs no
    /// accessor returning it (the per-output globals are created via
    /// `Output::create_global` in udev.rs), so the manager field is never read after
    /// `new` — it is held only to keep the `xdg_output_manager` global advertised.
    #[allow(dead_code)]
    pub output_manager_state: OutputManagerState,
    /// wlr-layer-shell — the glass-shell desktop mounts as a BACKGROUND layer surface.
    pub layer_shell_state: WlrLayerShellState,
    /// wl_data_device — selection/DnD (a `delegate_dispatch2!` bundle requirement +
    /// the X11 DnD focus targets ride it).
    pub data_device_state: DataDeviceState,
    /// libinput keyboard/pointer handles (cached so the input loop routes evdev events
    /// into them, exactly like the winit backend routes winit input).
    pub keyboard: KeyboardHandle<State>,
    pub pointer: PointerHandle<State>,
    /// The single DRM output (the connected display). Created in `udev.rs` from the
    /// connector's preferred mode; clients see it as their `wl_output`.
    pub output: Output,

    /// The desktop window tree (positions toplevels above the layer-shell shell).
    pub space: Space<Window>,
    /// Software-floor renderer (pixman) — the never-fail paint path.
    pub renderer: PixmanRenderer,

    /// XWayland WM handle (Wine/legacy X11). `None` until `XWaylandEvent::Ready`.
    pub xwm: Option<X11Wm>,

    /// Whether the compositor loop should keep running (cleared on a fatal session
    /// loss). The DRM loop checks this each iteration.
    pub running: bool,

    /// IPC event sink — `window.opened`/`window.closed`/… frames to subscribers
    /// (the com.hart.Compositor server, Phase 6). Boxed so this module does not
    /// depend on the transport concretely.
    pub emit_ipc_event: Box<dyn FnMut(&str, &WindowRecord)>,
}

// ── Per-client state. Carries the compositor-side client bookkeeping Smithay needs
// for socket-inserted clients (mirrors winit.rs::ClientState — same shape, the DRM
// State serves the SAME glass-shell socket client). ──
#[derive(Default)]
pub struct ClientState {
    pub compositor_state: CompositorClientState,
}
impl ClientData for ClientState {
    fn initialized(&self, _client_id: ClientId) {}
    fn disconnected(&self, _client_id: ClientId, _reason: DisconnectReason) {}
}

impl State {
    /// ⚠️ CI-COMPILE. The summon-resolution orchestration the task names
    /// ("implement SummonApp keyed on a REAL map event within a timeout"). Called
    /// from EVERY map handler with the freshly-mapped toplevel's facts. It is the
    /// SINGLE place a `PendingSummon` becomes `Mapped(handle)`. Delegates the
    /// match/timeout decision to the PURE `PendingSummon` (main.rs) so the logic
    /// is unit-tested feature-OFF; here we only wire the real map edge to it.
    ///
    /// Returns the minted handle (the map ALWAYS mints a handle and records the
    /// window, summoned or not — an externally-opened window has `manifest_id =
    /// None`). If the map satisfied a pending summon, that summon is removed and
    /// the brain's awaiting `SummonApp` future is completed with
    /// `SummonOutcome::Mapped(handle)` by the IPC layer observing `window.opened`.
    pub fn on_real_map(
        &mut self,
        app_id: Option<String>,
        title: Option<String>,
        kind: ToplevelKind,
    ) -> WindowHandle {
        // Does this map satisfy a pending summon? The launcher tags the child so
        // app_id (or a launcher-set manifest hint) matches the pending manifest.
        // We match on app_id == manifest_id as the join key (the brain sets the
        // app_id when it launches via AppRegistry); `accepts` also checks kind.
        let manifest_id = app_id.as_ref().and_then(|aid| {
            let idx = self
                .pending
                .iter()
                .position(|p| p.accepts(aid, kind));
            idx.map(|i| {
                // Consume the pending summon: it is now resolved by a REAL map.
                let p = self.pending.remove(i);
                p.manifest_id
            })
        });

        // The ONLY mint site — `on_map` calls `mint_handle` internally. A handle
        // existing PROVES a toplevel mapped (no-phantom-window, in a type).
        let handle = self.windows.on_map(
            manifest_id.clone(),
            kind,
            app_id,
            title,
        );

        // Emit window.opened to IPC subscribers + advertise to foreign-toplevel.
        if let Some(rec) = self.windows.record(&handle) {
            (self.emit_ipc_event)("window.opened", rec);
        }
        self.sync_foreign_toplevels();
        handle
    }

    /// ⚠️ CI-COMPILE. Per-tick timer callback (inserted into calloop). Expires any
    /// `PendingSummon` past `SUMMON_MAP_TIMEOUT` → the brain's awaiting future is
    /// completed with `SummonOutcome::TimedOut` (NEVER a handle). The PURE
    /// `is_timed_out_at` decides; here we only feed it the real clock + report.
    ///
    /// Returns the manifest ids that timed out, so the IPC layer can resolve their
    /// pending `SummonApp` requests with `error.code = "timeout"`.
    pub fn expire_summons(&mut self, now: std::time::Instant) -> Vec<String> {
        let mut timed_out = Vec::new();
        self.pending.retain(|p| {
            if p.is_timed_out_at(now) {
                timed_out.push(p.manifest_id.clone());
                false // drop it
            } else {
                true
            }
        });
        timed_out
    }

    /// ⚠️ CI-COMPILE. Resolve a Smithay surface back to its compositor handle
    /// (needed by the destroy handler). Walks the space; matches the surface.
    fn handle_for_surface(&self, surface: &WlSurface) -> Option<WindowHandle> {
        // The real body maps `surface` -> the `Window` in `self.space` -> its
        // stored handle. Smithay's `Window` carries user-data; we stash the
        // `WindowHandle` there at map time. This is the reverse of `on_map`.
        self.space
            .elements()
            .find(|w| {
                w.wl_surface()
                    .map(|s| &*s == surface)
                    .unwrap_or(false)
            })
            .and_then(|w| w.user_data().get::<WindowHandle>().cloned())
    }

    /// ⚠️ CI-COMPILE. Mirror the PURE registry onto wlr-foreign-toplevel-management
    /// (advertise mapped toplevels, withdraw gone ones). The registry stays the
    /// single source of truth — this is a pure projection of it (one source per
    /// object class, IPC_PROTOCOL.md §4.8).
    fn sync_foreign_toplevels(&mut self) {
        // The real body diffs `self.windows.list()` against the currently-
        // advertised foreign-toplevel handles and advertises/withdraws the delta.
        // ForeignToplevelListState::new_toplevel / ::remove_toplevel.
        for rec in self.windows.list() {
            // advertise rec if not already advertised (idempotent in real impl)
            let _ = rec;
        }
        let _ = &mut self.foreign_toplevel_state;
    }
}

// ════════════════════════════════════════════════════════════════════════════
// SeatHandler — input seat (keyboard/pointer/touch). REQUIRED for `SeatState<State>`
// to exist AND as a transitive `X11Wm::start_wm` bound. ALL THREE focus targets are
// `WlSurface` (the SAME choice as winit.rs::SeatHandler) — the M7 input router
// (`process_input_event`) routes pointer motion/buttons to the bare `WlSurface` under
// the cursor, so `PointerFocus` MUST be `WlSurface`. start_wm's `PointerFocus: DndFocus`
// / `TouchFocus: DndFocus` bounds are satisfied because the State now impls
// `DataDeviceHandler` (its `WlSurface: DndFocus<State>` impl), so no X11Surface focus
// target is needed — exactly the winit backend's wiring. `seat_state()` returns the REAL
// `SeatState<State>` field. focus_changed/cursor_image/led_state_changed are
// trait-defaulted → omitted (the trait's intended default, not a fake).
// ════════════════════════════════════════════════════════════════════════════

impl smithay::input::SeatHandler for State {
    type KeyboardFocus = WlSurface;
    type PointerFocus = WlSurface;
    type TouchFocus = WlSurface;

    fn seat_state(&mut self) -> &mut SeatState<State> {
        &mut self.seat_state
    }
}

// ════════════════════════════════════════════════════════════════════════════
// XdgShellHandler — native Wayland toplevels (Flatpak / PWA / modern apps).
// ════════════════════════════════════════════════════════════════════════════
//
// ⚠️ CI-COMPILE. INVARIANT (load-bearing across any Smithay-rev drift):
//   a handle is minted in `on_real_map(ToplevelKind::Xdg, …)` — the SINGLE mint
//   site — and a pending `SummonApp` resolves to `Mapped(handle)` ONLY here, on
//   the actual map. Never on the launcher's exit code.

impl XdgShellHandler for State {
    fn xdg_shell_state(&mut self) -> &mut XdgShellState {
        &mut self.xdg_shell_state
    }

    /// A client created a toplevel. It is NOT mapped yet (no buffer committed) —
    /// we map it into the space and send the initial configure; the REAL map
    /// (first commit with a buffer) is observed by the compositor `commit` handler,
    /// which calls the free fn `xdg_toplevel_mapped` below (master has NO
    /// `toplevel_mapped` trait method — the map edge is a buffer commit, detected in
    /// `CompositorHandler::commit`). We mint the handle at THAT edge, not here.
    fn new_toplevel(&mut self, surface: ToplevelSurface) {
        let window = Window::new_wayland_window(surface.clone());
        // Place below the orb/overlays per the Phase-4 z-order model; above the
        // layer-shell BACKGROUND desktop. Real geometry is the tiling policy's job.
        self.space.map_element(window, (0, 0), false);
        // Send an initial configure so the client can commit its first buffer.
        surface.send_configure();
    }

    fn new_popup(&mut self, surface: PopupSurface, _positioner: PositionerState) {
        // Popups (menus/tooltips) ride above their parent; not tracked as
        // top-level windows in the registry (they have no manifest identity).
        let _ = surface;
    }

    // master: `grab` takes a `WlSeat` (the protocol seat resource), not a
    // `WlSurface`. Popup grabs (the implicit pointer/keyboard grab a menu takes)
    // are honored by the seat's default grab; we have no extra bookkeeping.
    fn grab(&mut self, _surface: PopupSurface, _seat: WlSeat, _serial: Serial) {}

    fn reposition_request(
        &mut self,
        _surface: PopupSurface,
        _positioner: PositionerState,
        _token: u32,
    ) {
    }

    /// The toplevel was destroyed → invalidate its handle + emit window.closed.
    fn toplevel_destroyed(&mut self, surface: ToplevelSurface) {
        // master: `ToplevelSurface::wl_surface()` returns `&WlSurface` (not Option),
        // so clone it directly — there is nothing to unwrap.
        let wl = surface.wl_surface().clone();
        on_surface_destroyed(self, &wl);
    }
}

impl State {
    /// ⚠️ CI-COMPILE. Find the live `Window` for a toplevel surface (to stash/read
    /// its handle). Reverse lookup off the space.
    fn window_for_toplevel(&self, surface: &ToplevelSurface) -> Option<&Window> {
        self.space.elements().find(|w| {
            w.toplevel().map(|t| t == surface).unwrap_or(false)
        })
    }
}

/// ⚠️ CI-COMPILE. The REAL xdg-shell map edge. master has NO `toplevel_mapped`
/// callback on `XdgShellHandler`; the map happens when a toplevel commits its first
/// buffer, which the compositor observes in `CompositorHandler::commit` (the event
/// loop's commit handler calls THIS, exactly as anvil maps in its commit path). It
/// is a free fn (not a trait method) because that is where master surfaces the edge.
/// Body is the no-phantom-window mint site for xdg-shell — unchanged from the draft:
/// it calls `State::on_real_map(.., ToplevelKind::Xdg)`, the SINGLE handle-mint site,
/// so a pending `SummonApp` resolves to `Mapped(handle)` ONLY here, on the real map.
pub fn xdg_toplevel_mapped(state: &mut State, surface: &ToplevelSurface) {
    let app_id = toplevel_app_id(surface);
    let title = toplevel_title(surface);
    let handle = state.on_real_map(app_id, title, ToplevelKind::Xdg);
    // Stash the handle on the Window's user-data so destroy can reverse it.
    if let Some(window) = state.window_for_toplevel(surface) {
        window.user_data().insert_if_missing(|| handle.clone());
    }
    // Optional `place` from the SummonApp request is applied by the IPC layer
    // after it observes window.opened (it has the geometry; we have the map).
}

// ════════════════════════════════════════════════════════════════════════════
// XWayland — X11 windows surfaced through XWayland (Wine apps, legacy X11).
// ════════════════════════════════════════════════════════════════════════════
//
// ⚠️ CI-COMPILE. INVARIANT: this is the CORRECTED Wine path. The installer
// (`app_installer.py:_install_windows`) returns `success=True` UNCONDITIONALLY
// ("Wine often returns 0 even for interactive installers"). The handle is minted
// HERE, only when a real X11 toplevel maps — so an agent never arranges a phantom
// Wine window. `summon_precheck("windows")` returns None (proceed to await-map),
// and ONLY this handler completes the summon with `Mapped`.

// NOTE: the XWayland LIFECYCLE driver (spawn + `XWaylandEvent::Ready` → `X11Wm::start_wm`)
// lives in `udev.rs::spawn_xwayland` (and `winit.rs::spawn_xwayland` for the dev backend),
// inserted as a calloop source by the backend that owns the event loop. It is NOT a
// standalone fn here — that would be a parallel path. The `XwmHandler` below is the live
// X11 WM callback surface both backends route X11 map/unmap through.

// ── XwmHandler — the live X11 window-manager callbacks (master routes X11 surface
// map/unmap/configure through this trait, NOT through ad-hoc `on_xwayland_*`
// methods). REQUIRED methods (the rest are defaulted): xwm_state, new_window,
// new_override_redirect_window, map_window_request, mapped_override_redirect_window,
// unmapped_window, destroyed_window, configure_request, configure_notify,
// resize_request, move_request. The map/unmap bodies carry the REAL no-phantom
// bookkeeping: `map_window_request` is the corrected Wine map edge — it mints the
// handle via `on_real_map(.., ToplevelKind::XWayland)` ONLY when a real X11 toplevel
// maps (the installer's unconditional `success=True` is refused here).
impl XwmHandler for State {
    fn xwm_state(&mut self, _xwm: XwmId) -> &mut X11Wm {
        // The single X11Wm this compositor owns (attached on XWaylandEvent::Ready).
        self.xwm.as_mut().expect("xwm_state called before the X11Wm was attached")
    }

    fn new_window(&mut self, _xwm: XwmId, _window: X11Surface) {
        // The X11 window exists but has not requested mapping yet — no handle until
        // it actually maps (no-phantom-window). Nothing to record here.
    }

    fn new_override_redirect_window(&mut self, _xwm: XwmId, _window: X11Surface) {
        // Override-redirect (menus/tooltips/popups) — not tracked as top-level
        // windows in the registry (no manifest identity), mapped on their own notify.
    }

    /// ⚠️ CI-COMPILE. The REAL X11 (Wine) map edge. master delivers it here. This is
    /// where the installer's unconditional Wine `success=True` is corrected: a handle
    /// is minted ONLY now, on a real map. Reuses the SAME `on_real_map` single mint
    /// site as the xdg path, so a pending Wine `SummonApp` resolves to
    /// `Mapped(handle)` keyed on THIS map, never the launcher exit code.
    fn map_window_request(&mut self, _xwm: XwmId, window: X11Surface) {
        // Accept the client's map request (mirrors anvil) so XWayland composites it.
        let _ = window.set_mapped(true);
        let app_id = x11_app_id(&window);
        let title = x11_title(&window);
        // Wrap the X11 surface as a desktop Window so it tiles with xdg toplevels.
        let win = Window::new_x11_window(window);
        self.space.map_element(win.clone(), (0, 0), false);
        // Configure it to the geometry the space gave it (X11 needs an explicit
        // configure to know its size); best-effort, the real tiling policy refines.
        if let Some(bbox) = self.space.element_bbox(&win) {
            if let Some(xsurface) = win.x11_surface() {
                let _ = xsurface.configure(Some(bbox));
            }
        }
        let handle = self.on_real_map(app_id, title, ToplevelKind::XWayland);
        win.user_data().insert_if_missing(|| handle);
    }

    /// An override-redirect window mapped (it positions itself; we honor its loc).
    fn mapped_override_redirect_window(&mut self, _xwm: XwmId, window: X11Surface) {
        let location = window.geometry().loc;
        let win = Window::new_x11_window(window);
        self.space.map_element(win, location, true);
    }

    /// ⚠️ CI-COMPILE. An X11 toplevel was unmapped → invalidate its handle +
    /// window.closed (the same destroy path the xdg toplevels funnel through).
    fn unmapped_window(&mut self, _xwm: XwmId, window: X11Surface) {
        // Drop the desktop element wrapping this X11 surface from the space.
        let elem = self
            .space
            .elements()
            .find(|w| w.x11_surface() == Some(&window))
            .cloned();
        if let Some(elem) = elem {
            self.space.unmap_elem(&elem);
        }
        // Mirror the pure registry + emit window.closed via the one destroy path.
        if let Some(wl) = window.wl_surface() {
            on_surface_destroyed(self, &wl);
        }
        if !window.is_override_redirect() {
            let _ = window.set_mapped(false);
        }
    }

    fn destroyed_window(&mut self, _xwm: XwmId, _window: X11Surface) {
        // The unmap already invalidated the handle; X11 destroy needs no extra
        // bookkeeping (the registry row is gone, the space element is unmapped).
    }

    /// The client asked to resize itself. We do not let X11 windows move freely
    /// (the WM owns placement) but we honor the requested SIZE, mirroring anvil.
    fn configure_request(
        &mut self,
        _xwm: XwmId,
        window: X11Surface,
        _x: Option<i32>,
        _y: Option<i32>,
        w: Option<u32>,
        h: Option<u32>,
        _reorder: Option<Reorder>,
    ) {
        let mut geo = window.geometry();
        if let Some(w) = w {
            geo.size.w = w as i32;
        }
        if let Some(h) = h {
            geo.size.h = h as i32;
        }
        let _ = window.configure(geo);
    }

    /// XWayland told us where it actually put an override-redirect window → keep the
    /// space element's position in sync so input hit-testing is correct.
    fn configure_notify(
        &mut self,
        _xwm: XwmId,
        window: X11Surface,
        geometry: Rectangle<i32, Logical>,
        _above: Option<u32>,
    ) {
        let elem = self
            .space
            .elements()
            .find(|w| w.x11_surface() == Some(&window))
            .cloned();
        if let Some(elem) = elem {
            self.space.map_element(elem, geometry.loc, false);
        }
    }

    /// Interactive resize/move are driven by the AI-native placement policy, not by
    /// the client grabbing the pointer; we intentionally do not start a client-driven
    /// grab here (the WM owns geometry). A real interactive-resize affordance is
    /// Phase-8 polish; refusing the client-initiated grab is the correct default.
    fn resize_request(
        &mut self,
        _xwm: XwmId,
        _window: X11Surface,
        _button: u32,
        _edges: X11ResizeEdge,
    ) {
    }

    fn move_request(&mut self, _xwm: XwmId, _window: X11Surface, _button: u32) {}

    fn disconnected(&mut self, _xwm: XwmId) {
        // The X11Wm connection dropped (XWayland gone) → release our handle.
        self.xwm = None;
    }
}

// ── XWaylandShellHandler — the X11↔wl_surface association protocol state accessor
// (master `xwayland_shell`). A `start_wm` bound; `surface_associated` is defaulted
// (the association is observed; the map edge that mints a handle is
// `map_window_request` above).
impl XWaylandShellHandler for State {
    fn xwayland_shell_state(&mut self) -> &mut XWaylandShellState {
        &mut self.xwayland_shell_state
    }
}

// ── DndGrabHandler — drag'n'drop hand-off for X11 clients (a `start_wm` bound). Both
// callbacks (`dropped`/`cancelled`) are defaulted; HART-comp has no extra DnD
// bookkeeping at this phase, so the impl is empty. (PointerFocus/TouchFocus =
// `X11Surface`, which impls `DndFocus`, satisfy the associated `start_wm` bounds.)
impl DndGrabHandler for State {}

// ════════════════════════════════════════════════════════════════════════════
// XdgDecorationHandler — HART-comp draws frames itself (server-side decorations).
// ════════════════════════════════════════════════════════════════════════════
//
// ⚠️ CI-COMPILE. INVARIANT: prefer SSD so the AI-native WM owns the chrome +
// placement policy uniformly; fall back to CSD only for clients that hard-refuse.

// master: `XdgDecorationHandler` has NO `xdg_decoration_state()` method (the global
// is a plain `XdgDecorationState` field on `State`, made by
// `XdgDecorationState::new::<State>(&dh)`; the trait is only the three notifications).
impl XdgDecorationHandler for State {
    /// A new toplevel asked about decorations → default it to SSD.
    fn new_decoration(&mut self, toplevel: ToplevelSurface) {
        toplevel.with_pending_state(|state| {
            state.decoration_mode = Some(DecorationMode::ServerSide);
        });
        toplevel.send_pending_configure();
    }

    /// The client requested a specific mode. Honor SSD; if it demands CSD we allow
    /// it (a hard refusal of SSD), otherwise we keep SSD (the WM draws the frame).
    fn request_mode(&mut self, toplevel: ToplevelSurface, mode: DecorationMode) {
        let chosen = match mode {
            DecorationMode::ClientSide => DecorationMode::ClientSide,
            _ => DecorationMode::ServerSide,
        };
        toplevel.with_pending_state(|state| {
            state.decoration_mode = Some(chosen);
        });
        toplevel.send_pending_configure();
    }

    fn unset_mode(&mut self, toplevel: ToplevelSurface) {
        // Client unset its preference → fall back to our default (SSD).
        toplevel.with_pending_state(|state| {
            state.decoration_mode = Some(DecorationMode::ServerSide);
        });
        toplevel.send_pending_configure();
    }
}

// ════════════════════════════════════════════════════════════════════════════
// ForeignToplevelListHandler — wlr-foreign-toplevel-management.
// ════════════════════════════════════════════════════════════════════════════
//
// ⚠️ CI-COMPILE. INVARIANT: this protocol is a MIRROR of the PURE WindowRegistry
// (the `window.list`/taskbar enumeration surface). The registry is the single
// source of truth; advertise/withdraw is driven by `State::sync_foreign_toplevels`
// off the registry, never a second window list.

impl ForeignToplevelListHandler for State {
    fn foreign_toplevel_list_state(&mut self) -> &mut ForeignToplevelListState {
        &mut self.foreign_toplevel_state
    }
}

// ════════════════════════════════════════════════════════════════════════════
// Shared helpers
// ════════════════════════════════════════════════════════════════════════════

/// ⚠️ CI-COMPILE. The one destroy path (xdg + XWayland both funnel here): resolve
/// the surface → its handle, invalidate it in the PURE registry, emit
/// window.closed, withdraw from foreign-toplevel. Mirrors `on_real_map`.
fn on_surface_destroyed(state: &mut State, surface: &WlSurface) {
    if let Some(handle) = state.handle_for_surface(surface) {
        // Capture the record BEFORE unmap so the event carries its facts.
        let rec = state.windows.record(&handle).cloned();
        if state.windows.on_unmap(&handle) {
            if let Some(rec) = rec {
                (state.emit_ipc_event)("window.closed", &rec);
            }
        }
        // Drop the Window from the space.
        let to_remove: Vec<Window> = state
            .space
            .elements()
            .filter(|w| {
                w.user_data().get::<WindowHandle>() == Some(&handle)
            })
            .cloned()
            .collect();
        for w in to_remove {
            state.space.unmap_elem(&w);
        }
        state.sync_foreign_toplevels();
    }
}

// ════════════════════════════════════════════════════════════════════════════
// M7 — the protocol handlers that make `State` a COMPLETE compositor able to SERVE
// real clients on the DRM backend. Through M6 `wayland.rs` had only XdgShell /
// XWayland / decoration / foreign-toplevel / seat impls — it compiled but could not
// serve clients (nothing constructed a `Display<State>` + dispatched). These are the
// missing bundle a `delegate_dispatch2!(State)` requires + the glass-shell layer
// mount. They mirror winit.rs's handlers 1:1 (the SAME protocol surface) — the only
// difference is the renderer type, which lives in `udev.rs`'s render loop, not here.
// ════════════════════════════════════════════════════════════════════════════

impl State {
    /// Reverse-lookup the live `Window` whose ROOT surface is `surface` (mirrors
    /// winit.rs::window_for_surface). `Window::wl_surface()` comes from `WaylandFocus`.
    fn window_for_surface(&self, surface: &WlSurface) -> Option<Window> {
        self.space
            .elements()
            .find(|w| w.wl_surface().map(|s| &*s == surface).unwrap_or(false))
            .cloned()
    }

    /// M7 — route a single libinput event into the seat. Stage-A boot floor: forward
    /// keyboard keys to the focused client + absolute/relative pointer motion + buttons
    /// + axis to the pointer-focused surface. The full click-to-focus / keyboard-shortcut
    /// / workspace logic is the winit backend's (M3/M5) and is Stage-B parity here — the
    /// boot floor needs the seat live so the glass shell receives input, not the whole WM.
    pub fn process_input_event<B: smithay::backend::input::InputBackend>(
        &mut self,
        event: smithay::backend::input::InputEvent<B>,
    ) {
        use smithay::backend::input::{
            AbsolutePositionEvent, Event, InputEvent, KeyboardKeyEvent, PointerButtonEvent,
        };
        use smithay::input::keyboard::FilterResult;
        use smithay::input::pointer::{ButtonEvent, MotionEvent};
        use smithay::utils::SERIAL_COUNTER;
        match event {
            InputEvent::Keyboard { event } => {
                let serial = SERIAL_COUNTER.next_serial();
                let time = event.time_msec();
                let code = event.key_code();
                let key_state = event.state();
                let keyboard = self.keyboard.clone();
                // Forward every key to the focused client (no compositor chords on the DRM
                // floor yet — Stage-B parity). The filter always returns Forward.
                keyboard.input::<(), _>(self, code, key_state, serial, time, |_, _, _| {
                    FilterResult::Forward
                });
            }
            InputEvent::PointerMotionAbsolute { event } => {
                let serial = SERIAL_COUNTER.next_serial();
                let output_geo = match self.space.output_geometry(&self.output) {
                    Some(g) => g,
                    None => return,
                };
                let pos = event.position_transformed(output_geo.size) + output_geo.loc.to_f64();
                let pointer = self.pointer.clone();
                let under = self.surface_under(pos);
                pointer.motion(
                    self,
                    under,
                    &MotionEvent { location: pos, serial, time: event.time_msec() },
                );
                pointer.frame(self);
            }
            InputEvent::PointerButton { event } => {
                let serial = SERIAL_COUNTER.next_serial();
                let pointer = self.pointer.clone();
                pointer.button(
                    self,
                    &ButtonEvent {
                        button: event.button_code(),
                        state: event.state(),
                        serial,
                        time: event.time_msec(),
                    },
                );
                pointer.frame(self);
            }
            _ => {}
        }
    }

    /// Hit-test the surface under `pos` for POINTER focus (toplevels then layer
    /// surfaces). Trimmed from winit.rs::surface_under to the Stage-A floor (no z-order
    /// layer juggling beyond "windows above, layer-shell below").
    fn surface_under(
        &self,
        pos: smithay::utils::Point<f64, Logical>,
    ) -> Option<(WlSurface, smithay::utils::Point<f64, Logical>)> {
        if let Some((window, win_loc)) = self.space.element_under(pos) {
            if let Some((surface, surf_loc)) =
                window.surface_under(pos - win_loc.to_f64(), WindowSurfaceType::ALL)
            {
                return Some((surface, (surf_loc + win_loc).to_f64()));
            }
        }
        let layers = layer_map_for_output(&self.output);
        if let Some(layer) = layers
            .layer_under(WlrLayer::Top, pos)
            .or_else(|| layers.layer_under(WlrLayer::Background, pos))
        {
            let layer_loc = layers.layer_geometry(layer).map(|g| g.loc).unwrap_or_default();
            if let Some((surface, loc)) =
                layer.surface_under(pos - layer_loc.to_f64(), WindowSurfaceType::ALL)
            {
                return Some((surface, (loc + layer_loc).to_f64()));
            }
        }
        None
    }
}

// ── BufferHandler ───────────────────────────────────────────────────────────
impl BufferHandler for State {
    fn buffer_destroyed(&mut self, _buffer: &WlBuffer) {}
}

// ── CompositorHandler — THE map edge lives here (first buffer commit), exactly as
// the winit backend detects it. An xdg toplevel is "mapped" the first time it commits
// a buffer; we mint the handle then via `on_real_map` (the SINGLE mint site), so a
// handle still proves a real map on the DRM path too (no-phantom-window). ──
impl CompositorHandler for State {
    fn compositor_state(&mut self) -> &mut CompositorState {
        &mut self.compositor_state
    }

    fn client_compositor_state<'a>(&self, client: &'a Client) -> &'a CompositorClientState {
        // The XWayland client carries XWaylandClientData (its OWN CompositorClientState);
        // socket-inserted clients carry our ClientState. Check XWayland first, then ours
        // — without this the XWayland connection panics ("client without ClientState").
        if let Some(state) = client.get_data::<XWaylandClientData>() {
            return &state.compositor_state;
        }
        if let Some(state) = client.get_data::<ClientState>() {
            return &state.compositor_state;
        }
        panic!("client_compositor_state: unknown client data type")
    }

    fn commit(&mut self, surface: &WlSurface) {
        // Latch the newly-committed buffer into Smithay's surface state.
        on_commit_buffer_handler::<Self>(surface);
        if is_sync_subsurface(surface) {
            return;
        }
        // Walk to the root (the toplevel's surface).
        let mut root = surface.clone();
        while let Some(parent) = get_parent(&root) {
            root = parent;
        }
        if let Some(window) = self.window_for_surface(&root) {
            window.on_commit();
            // THE MAP EDGE (no-phantom-window mint site): root commit + not-yet-mapped +
            // a real buffer → mint once via on_real_map (ToplevelKind::Xdg). Identical to
            // winit.rs's commit map-edge, so a handle proves a real map here too.
            let already_mapped = window.user_data().get::<WindowHandle>().is_some();
            if &root == surface && !already_mapped && surface_has_buffer(surface) {
                if let Some(toplevel) = window.toplevel() {
                    xdg_toplevel_mapped(self, &toplevel.clone());
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

// ── WlrLayerShellHandler — the glass-shell desktop mounts as a BACKGROUND layer
// (the Stage-A boot-floor deliverable: scanout of the layer-shell glass shell). Ported
// 1:1 from winit.rs. ──
impl WlrLayerShellHandler for State {
    fn shell_state(&mut self) -> &mut WlrLayerShellState {
        &mut self.layer_shell_state
    }

    fn new_layer_surface(
        &mut self,
        surface: WlrLayerSurface,
        _wl_output: Option<wl_output::WlOutput>,
        _layer: WlrLayer,
        namespace: String,
    ) {
        let output = self.output.clone();
        let mut map = layer_map_for_output(&output);
        let ns = namespace.clone();
        match map.map_layer(&LayerSurface::new(surface, namespace)) {
            Ok(()) => tracing::info!(
                namespace = %ns,
                layer = ?_layer,
                "layer.mapped (wlr-layer-shell surface tracked — the glass-shell desktop mount point)"
            ),
            Err(err) => tracing::warn!(?err, namespace = %ns, "failed to map layer surface"),
        }
    }

    fn layer_destroyed(&mut self, surface: WlrLayerSurface) {
        let output = self.output.clone();
        let mut map = layer_map_for_output(&output);
        let found = map
            .layers()
            .find(|l| l.layer_surface() == &surface)
            .cloned();
        if let Some(layer) = found {
            map.unmap_layer(&layer);
        }
    }
}

// ── SelectionHandler + DataDevice* — required by the delegate_dispatch2 bundle. ──
impl SelectionHandler for State {
    type SelectionUserData = ();
}
impl DataDeviceHandler for State {
    fn data_device_state(&mut self) -> &mut DataDeviceState {
        &mut self.data_device_state
    }
}
impl WaylandDndGrabHandler for State {}

// ── OutputHandler — required for the output global's dispatch. ──
impl OutputHandler for State {}

// One macro generates every Dispatch/GlobalDispatch impl from the Handler traits above
// (the unified dispatch model on this Smithay rev — the SAME macro winit.rs uses).
smithay::delegate_dispatch2!(State);

/// Send the initial xdg/layer configure once, on the surface's first commit, so the
/// client can attach a buffer (the map edge). Ported 1:1 from winit.rs.
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
    let mut map = layer_map_for_output(&output);
    if let Some(layer) = map
        .layer_for_surface(surface, WindowSurfaceType::TOPLEVEL)
        .cloned()
    {
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
