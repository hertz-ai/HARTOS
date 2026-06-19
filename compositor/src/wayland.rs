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
use std::sync::atomic::Ordering;

use smithay::{
    // Backend: software-floor renderer + DRM scanout + libinput seat (Phase 3,
    // shared here so the event loop that drives the maps is one loop).
    backend::renderer::pixman::PixmanRenderer,
    desktop::{Space, Window, WindowSurfaceType},
    input::{Seat, SeatState},
    reexports::{
        calloop::{EventLoop, LoopHandle},
        wayland_server::{
            protocol::{wl_seat::WlSeat, wl_surface::WlSurface},
            Client, Display, DisplayHandle,
        },
    },
    utils::{Logical, Rectangle, Serial},
    wayland::{
        compositor::CompositorState,
        foreign_toplevel_list::{
            ForeignToplevelListHandler, ForeignToplevelListState,
        },
        shell::xdg::{
            decoration::{XdgDecorationHandler, XdgDecorationState},
            PopupSurface, PositionerState, ToplevelSurface, XdgShellHandler,
            XdgShellState,
        },
        // master: the X11↔wl_surface association protocol the X11Wm needs as a
        // handler bound on `start_wm` (its state is a plain field on `State`).
        xwayland_shell::{XWaylandShellHandler, XWaylandShellState},
    },
    xwayland::{
        xwm::{Reorder, ResizeEdge as X11ResizeEdge, XwmId},
        X11Surface, X11Wm, XWayland, XWaylandEvent, XwmHandler,
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
    PendingSummon, SummonOutcome, ToplevelKind, WindowHandle, WindowRecord,
    WindowRegistry, NEXT_HANDLE_ID,
};

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
    pub xdg_decoration_state: XdgDecorationState,
    pub foreign_toplevel_state: ForeignToplevelListState,
    /// Seat protocol state. `seat_state()` (SeatHandler) returns this; the live
    /// `seat` is created from it. A real field (NOT a stub) — the X11Wm + input
    /// dispatch borrow it.
    pub seat_state: SeatState<State>,
    pub seat: Seat<State>,
    /// X11↔wl_surface association protocol state (master `xwayland_shell`). The
    /// X11Wm requires a `XWaylandShellHandler` returning this; a plain field, made
    /// by `XWaylandShellState::new::<State>(&dh)` at construction.
    pub xwayland_shell_state: XWaylandShellState,

    /// The desktop window tree (positions toplevels above the layer-shell shell).
    pub space: Space<Window>,
    /// Software-floor renderer (pixman) — the never-fail paint path.
    pub renderer: PixmanRenderer,

    /// XWayland WM handle (Wine/legacy X11). `None` until `XWaylandEvent::Ready`.
    pub xwm: Option<X11Wm>,

    /// IPC event sink — `window.opened`/`window.closed`/… frames to subscribers
    /// (the com.hart.Compositor server, Phase 6). Boxed so this module does not
    /// depend on the transport concretely.
    pub emit_ipc_event: Box<dyn FnMut(&str, &WindowRecord)>,
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
// to exist AND as a transitive `X11Wm::start_wm` bound. The focus associated types
// pick concrete focus targets that satisfy the trait bounds master imposes:
//   • KeyboardFocus = WlSurface   (impls KeyboardTarget; no DnD bound on keyboard)
//   • PointerFocus  = X11Surface  (impls PointerTarget AND DndFocus — the latter is
//                                  required by start_wm's `PointerFocus: DndFocus`;
//                                  X11Surface's DndFocus needs only XwmHandler+
//                                  SeatHandler, so it pulls in NO DataDeviceHandler)
//   • TouchFocus    = X11Surface  (impls TouchTarget AND DndFocus, same reason)
// `seat_state()` returns the REAL `SeatState<State>` field on `State` (not a stub).
// focus_changed/cursor_image/led_state_changed are trait-defaulted → omitted (that
// is the trait's intended default, not a fake).
// ════════════════════════════════════════════════════════════════════════════

impl smithay::input::SeatHandler for State {
    type KeyboardFocus = WlSurface;
    type PointerFocus = X11Surface;
    type TouchFocus = X11Surface;

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
    let app_id = surface_app_id(surface);
    let title = surface_title(surface);
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

/// ⚠️ CI-COMPILE. Drive the XWayland lifecycle. On `Ready` the X11 WM attaches; on
/// `Error` XWayland crashed on startup. Inserted as a calloop event source at
/// startup. master: `XWayland::spawn` returns `(XWayland, Client)` — the `Client`
/// is captured at the spawn site and threaded in HERE (the `Ready` event itself
/// carries only the privileged X11 socket + display number, NOT the client). The
/// X11Wm borrows the `DisplayHandle` (`&state.dh`) and takes the `Client`.
pub fn handle_xwayland_event(state: &mut State, event: XWaylandEvent, client: &Client) {
    match event {
        XWaylandEvent::Ready {
            x11_socket,
            display_number: _,
        } => {
            // Build the X11Wm bound to our DisplayHandle + the XWayland connection
            // + the XWayland client, so X11 surfaces route through `XwmHandler`.
            match X11Wm::start_wm(
                state.loop_handle.clone(),
                &state.dh,
                x11_socket,
                client.clone(),
            ) {
                Ok(wm) => state.xwm = Some(wm),
                Err(e) => {
                    tracing::error!(error = %e, "XWayland: failed to start X11 WM");
                }
            }
        }
        XWaylandEvent::Error => {
            tracing::warn!("XWayland crashed on startup");
            state.xwm = None;
        }
    }
}

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

// Smithay accessor shims — the exact getters differ by rev; these name intent.
// master: `ToplevelSurface::wl_surface()` returns `&WlSurface` (not Option) and
// `with_states(&WlSurface, f) -> T` returns the closure value directly (no Result),
// so the closure's `Option<String>` IS the return — no `.ok()`/`.flatten()`.
fn surface_app_id(surface: &ToplevelSurface) -> Option<String> {
    smithay::wayland::compositor::with_states(surface.wl_surface(), |states| {
        states
            .data_map
            .get::<smithay::wayland::shell::xdg::XdgToplevelSurfaceData>()
            .and_then(|d| d.lock().unwrap().app_id.clone())
    })
}
fn surface_title(surface: &ToplevelSurface) -> Option<String> {
    smithay::wayland::compositor::with_states(surface.wl_surface(), |states| {
        states
            .data_map
            .get::<smithay::wayland::shell::xdg::XdgToplevelSurfaceData>()
            .and_then(|d| d.lock().unwrap().title.clone())
    })
}
fn x11_app_id(x11: &X11Surface) -> Option<String> {
    // X11 WM_CLASS instance/class → app_id analogue.
    Some(x11.class()).filter(|s| !s.is_empty())
}
fn x11_title(x11: &X11Surface) -> Option<String> {
    Some(x11.title()).filter(|s| !s.is_empty())
}

// Touch the imports the draft references structurally so a future reviewer sees
// the full surface the real loop wires (renderer/space/seat/EventLoop/Display).
// Removed at first CI compile when the real `run_event_loop_smithay` lands.
#[allow(dead_code)]
fn _imports_touch() {
    let _ = NEXT_HANDLE_ID.load(Ordering::Relaxed);
    let _sz: Option<Rectangle<i32, Logical>> = None;
    let _surface_type = WindowSurfaceType::TOPLEVEL;
    fn _types(_d: &Display<State>, _e: &EventLoop<State>, _x: &XWayland) {}
    let _ = SummonOutcome::TimedOut;
}
