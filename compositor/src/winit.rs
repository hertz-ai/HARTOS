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
        input::{
            AbsolutePositionEvent, Axis, AxisSource, ButtonState, Event, InputBackend, InputEvent,
            KeyboardKeyEvent, PointerAxisEvent, PointerButtonEvent,
        },
        renderer::{
            Color32F, Frame, Renderer,
            element::{
                AsRenderElements, Kind,
                surface::{WaylandSurfaceRenderElement, render_elements_from_surface_tree},
            },
            gles::GlesRenderer,
            utils::{draw_render_elements, on_commit_buffer_handler},
        },
        winit::{self, WinitEvent},
    },
    desktop::{Space, Window, WindowSurfaceType},
    input::{
        Seat, SeatHandler, SeatState,
        keyboard::{FilterResult, KeyboardHandle},
        pointer::{AxisFrame, ButtonEvent, CursorImageStatus, MotionEvent, PointerHandle},
    },
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
    utils::{Logical, Point, Rectangle, SERIAL_COUNTER, Serial, Transform},
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
                Layer as WlrLayer, LayerSurface as WlrLayerSurface, WlrLayerShellHandler,
                WlrLayerShellState,
            },
            xdg::{
                PopupSurface, PositionerState, ToplevelSurface, XdgShellHandler, XdgShellState,
                XdgToplevelSurfaceData,
                decoration::{XdgDecorationHandler, XdgDecorationState},
            },
        },
        shm::{ShmHandler, ShmState},
        socket::ListeningSocketSource,
    },
};
use smithay::desktop::{LayerSurface, layer_map_for_output};
// `Window::wl_surface()` is provided by the `WaylandFocus` trait on this rev (not
// an inherent method) — it MUST be in scope for `window_for_surface` to call it.
use smithay::wayland::seat::WaylandFocus;
// xdg-decoration mode enum (server-side vs client-side) — the SSD negotiation in
// `XdgDecorationHandler` below.
use smithay::reexports::wayland_protocols::xdg::decoration::zv1::server::zxdg_toplevel_decoration_v1::Mode as DecorationMode;
// ── XWayland (Wine / legacy X11): the headline M3 feature. These types only exist
// when the `smithay/xwayland` feature is enabled (added to the `winit` cargo feature
// in Cargo.toml). The X11Wm routes X11 surface map/unmap through `XwmHandler`; the
// XWaylandShell association protocol + the DnD grab hand-off are `start_wm` bounds.
use smithay::xwayland::{
    X11Surface, X11Wm, XWayland, XWaylandClientData, XWaylandEvent,
    xwm::{Reorder, ResizeEdge as X11ResizeEdge, XwmHandler, XwmId},
};
use smithay::wayland::xwayland_shell::{XWaylandShellHandler, XWaylandShellState};
use smithay::input::dnd::DndGrabHandler;
use std::process::Stdio;
use tracing::{error, info, warn};

use crate::{
    BootConfig, HART_SPLASH_RGBA, ToplevelKind, WindowHandle, WindowRegistry, select_render_path,
};

/// Last painted wlr-layer-surface count, so the render loop logs a one-line
/// transition (0→N / N→0) instead of spamming every frame. Pure observability.
static LAYERS_PAINTED: std::sync::atomic::AtomicUsize = std::sync::atomic::AtomicUsize::new(0);

/// One-shot marker stashed in an X11 `Window`'s user-data the first time it is given
/// keyboard focus (on its first associated commit — see `CompositorHandler::commit`).
/// X11 surfaces associate their `wl_surface` ASYNCHRONOUSLY under XWayland, so the
/// focus-on-map cannot happen in `map_window_request` (the surface is still `None`
/// there); it is deferred to the first commit and de-duplicated by this marker.
struct X11Focused;

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

    // ── M3: input handles (cached so the loop can route winit input + read the
    // cursor position for click-to-focus). `pointer.current_location()` is the
    // hit-test origin; both are cheap clones of the seat's handles.
    pub pointer: PointerHandle<State>,
    pub keyboard: KeyboardHandle<State>,

    // ── M3: xdg-decoration — negotiate server-side decorations (the compositor owns
    // the chrome). A plain field constructed with `XdgDecorationState::new::<State>`.
    pub xdg_decoration_state: XdgDecorationState,

    // ── M3: XWayland (Wine / legacy X11). `xwayland_shell_state` is the X11↔wl_surface
    // association protocol (a `start_wm` bound); `xwm` is the live X11 window manager,
    // `None` until `XWaylandEvent::Ready` attaches it.
    pub xwayland_shell_state: XWaylandShellState,
    pub xwm: Option<X11Wm>,

    // ── M3: cascade placement cursor — each newly-mapped toplevel is offset from the
    // last so multiple windows don't fully overlap (the "MULTIPLE WINDOWS" gate).
    pub next_window_loc: Point<i32, Logical>,

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

    // ════════════════════════════════════════════════════════════════════════
    // M3 — multi-window placement
    // ════════════════════════════════════════════════════════════════════════

    /// Cascade the next toplevel's initial position so multiple windows don't fully
    /// overlap (the "tile or cascade" gate). Advances a diagonal cursor by a fixed
    /// step, wrapping back near the origin before it walks off the bottom-right of the
    /// output. Pure placement policy — the AI-native WM (Phase 6 IPC) refines it later.
    fn next_cascade_loc(&mut self) -> Point<i32, Logical> {
        // A generous diagonal step so multiple windows are CLEARLY distinct (not just
        // a hairline offset). Tuned for the 1280x800 dev output; the AI-native WM
        // (Phase 6 IPC) refines real placement later.
        const STEP_X: i32 = 230;
        const STEP_Y: i32 = 150;
        const MARGIN: i32 = 16;
        let loc = self.next_window_loc;
        let out_size = self
            .space
            .output_geometry(&self.output)
            .map(|g| g.size)
            .unwrap_or((1280, 800).into());
        // Advance the cascade cursor; wrap back near the origin (offset slightly each
        // wrap is overkill for M3) before it walks off the bottom-right.
        let mut next = Point::from((loc.x + STEP_X, loc.y + STEP_Y));
        if next.x + 200 > out_size.w || next.y + 150 > out_size.h {
            next = Point::from((MARGIN, MARGIN));
        }
        self.next_window_loc = next;
        loc
    }

    // ════════════════════════════════════════════════════════════════════════
    // M3 — input routing (keyboard focus + pointer hit-test + click-to-focus)
    // ════════════════════════════════════════════════════════════════════════

    /// Hit-test the surface under `pos` for POINTER focus, honouring z-order:
    /// Overlay/Top layer surfaces first, then mapped toplevels (newest on top via the
    /// space), then Bottom/Background layer surfaces. Returns the bare `WlSurface`
    /// (winit's `PointerFocus = WlSurface`, so no focus-target enum is needed) plus
    /// the surface-local origin the pointer handle wants. Modelled on anvil's
    /// `surface_under` (trimmed: no FullscreenSurface, single output).
    fn surface_under(&self, pos: Point<f64, Logical>) -> Option<(WlSurface, Point<f64, Logical>)> {
        let output = &self.output;
        let output_geo = self.space.output_geometry(output)?;
        let layers = layer_map_for_output(output);

        // Overlay / Top layer surfaces sit above the toplevels.
        if let Some(layer) = layers
            .layer_under(WlrLayer::Overlay, pos)
            .or_else(|| layers.layer_under(WlrLayer::Top, pos))
        {
            let layer_loc = layers.layer_geometry(layer).map(|g| g.loc).unwrap_or_default();
            if let Some((surface, loc)) =
                layer.surface_under(pos - layer_loc.to_f64(), WindowSurfaceType::ALL)
            {
                return Some((surface, (loc + layer_loc).to_f64()));
            }
        }

        // Mapped toplevels (xdg + X11), in the space's stacking order.
        if let Some((window, win_loc)) = self.space.element_under(pos) {
            if let Some((surface, surf_loc)) =
                window.surface_under(pos - win_loc.to_f64(), WindowSurfaceType::ALL)
            {
                return Some((surface, (surf_loc + win_loc).to_f64()));
            }
        }

        // Bottom / Background layer surfaces (the glass-shell desktop) sit below.
        if let Some(layer) = layers
            .layer_under(WlrLayer::Bottom, pos)
            .or_else(|| layers.layer_under(WlrLayer::Background, pos))
        {
            let layer_loc = layers.layer_geometry(layer).map(|g| g.loc).unwrap_or_default();
            if let Some((surface, loc)) =
                layer.surface_under(pos - layer_loc.to_f64(), WindowSurfaceType::ALL)
            {
                return Some((surface, (loc + layer_loc).to_f64()));
            }
        }
        let _ = output_geo;
        None
    }

    /// Move the KEYBOARD focus to whatever is under `pos` (called on click), raising a
    /// clicked toplevel to the top of the stack (click-to-focus + raise). Uses the
    /// toplevel's ROOT surface for focus (clicking a subsurface/popup focuses the
    /// window). Modelled on anvil's `update_keyboard_focus` (trimmed: no grabs check
    /// beyond the basics, single output, no FullscreenSurface / input-method grab).
    fn update_keyboard_focus(&mut self, pos: Point<f64, Logical>, serial: Serial) {
        let keyboard = self.keyboard.clone();
        // Respect an active pointer/keyboard grab (e.g. a popup menu) — don't steal
        // focus out from under it.
        if self.pointer.is_grabbed() || keyboard.is_grabbed() {
            return;
        }

        // A toplevel under the cursor → raise it + focus it.
        if let Some((window, _)) = self.space.element_under(pos).map(|(w, l)| (w.clone(), l)) {
            self.space.raise_element(&window, true);
            // For an X11 window also raise it in the X11 stacking order so XWayland
            // keeps the same z-order the compositor shows.
            if let Some(x11) = window.x11_surface() {
                if let Some(xwm) = self.xwm.as_mut() {
                    let _ = xwm.raise_window(x11);
                }
            }
            let surface = window.wl_surface().map(|s| s.into_owned());
            keyboard.set_focus(self, surface, serial);
            return;
        }

        // Otherwise a focusable layer surface (Overlay/Top) under the cursor.
        let output = self.output.clone();
        let layers = layer_map_for_output(&output);
        if let Some(layer) = layers
            .layer_under(WlrLayer::Overlay, pos)
            .or_else(|| layers.layer_under(WlrLayer::Top, pos))
        {
            if layer.can_receive_keyboard_focus() {
                let layer_loc = layers.layer_geometry(layer).map(|g| g.loc).unwrap_or_default();
                if layer
                    .surface_under(pos - layer_loc.to_f64(), WindowSurfaceType::ALL)
                    .is_some()
                {
                    keyboard.set_focus(self, Some(layer.wl_surface().clone()), serial);
                }
            }
        }
    }

    /// Route a single winit input event into the seat. Replaces the M1 no-op stub.
    /// Trimmed to the events `WinitInput` actually emits (keyboard, absolute pointer
    /// motion, button, axis); touch is handled by the existing M2 tap path elsewhere.
    fn process_input_event<B: InputBackend>(&mut self, event: InputEvent<B>) {
        match event {
            InputEvent::Keyboard { event } => self.on_keyboard_key::<B>(event),
            InputEvent::PointerMotionAbsolute { event } => {
                self.on_pointer_move_absolute::<B>(event)
            }
            InputEvent::PointerButton { event } => self.on_pointer_button::<B>(event),
            InputEvent::PointerAxis { event } => self.on_pointer_axis::<B>(event),
            _ => {}
        }
    }

    /// Forward a key press/release to the focused surface's client. The seat tracks
    /// the focused surface (set by `update_keyboard_focus` on click and on first map),
    /// so this is a straight forward — no compositor shortcut interception in M3.
    fn on_keyboard_key<B: InputBackend>(&mut self, evt: B::KeyboardKeyEvent) {
        let serial = SERIAL_COUNTER.next_serial();
        let time = evt.time_msec();
        let code = evt.key_code();
        let state = evt.state();
        let keyboard = self.keyboard.clone();
        keyboard.input::<(), _>(self, code, state, serial, time, |_, _, _| {
            FilterResult::Forward
        });
    }

    /// Route absolute pointer motion (winit gives us window-relative coords) to the
    /// surface under the cursor, then send a pointer frame.
    fn on_pointer_move_absolute<B: InputBackend>(&mut self, evt: B::PointerMotionAbsoluteEvent) {
        let output_geo = match self.space.output_geometry(&self.output) {
            Some(g) => g,
            None => return,
        };
        let pos = evt.position_transformed(output_geo.size) + output_geo.loc.to_f64();
        let serial = SERIAL_COUNTER.next_serial();
        let pointer = self.pointer.clone();
        let under = self.surface_under(pos);
        pointer.motion(
            self,
            under,
            &MotionEvent {
                location: pos,
                serial,
                time: evt.time_msec(),
            },
        );
        pointer.frame(self);
    }

    /// Route a pointer button. On press, first move the keyboard focus + raise the
    /// clicked window (click-to-focus), then forward the button to the pointer-focused
    /// surface's client.
    fn on_pointer_button<B: InputBackend>(&mut self, evt: B::PointerButtonEvent) {
        let serial = SERIAL_COUNTER.next_serial();
        let button = evt.button_code();
        let state = evt.state();
        if state == ButtonState::Pressed {
            self.update_keyboard_focus(self.pointer.current_location(), serial);
        }
        let pointer = self.pointer.clone();
        pointer.button(
            self,
            &ButtonEvent {
                button,
                state,
                serial,
                time: evt.time_msec(),
            },
        );
        pointer.frame(self);
    }

    /// Route a scroll/axis event to the pointer-focused surface.
    fn on_pointer_axis<B: InputBackend>(&mut self, evt: B::PointerAxisEvent) {
        let horizontal = evt.amount(Axis::Horizontal).unwrap_or_else(|| {
            evt.amount_v120(Axis::Horizontal).unwrap_or(0.0) * 15.0 / 120.0
        });
        let vertical = evt.amount(Axis::Vertical).unwrap_or_else(|| {
            evt.amount_v120(Axis::Vertical).unwrap_or(0.0) * 15.0 / 120.0
        });
        let mut frame = AxisFrame::new(evt.time_msec()).source(evt.source());
        if horizontal != 0.0 {
            frame = frame.value(Axis::Horizontal, horizontal);
            if let Some(d) = evt.amount_v120(Axis::Horizontal) {
                frame = frame.v120(Axis::Horizontal, d as i32);
            }
        }
        if vertical != 0.0 {
            frame = frame.value(Axis::Vertical, vertical);
            if let Some(d) = evt.amount_v120(Axis::Vertical) {
                frame = frame.v120(Axis::Vertical, d as i32);
            }
        }
        if evt.source() == AxisSource::Finger {
            if evt.amount(Axis::Horizontal) == Some(0.0) {
                frame = frame.stop(Axis::Horizontal);
            }
            if evt.amount(Axis::Vertical) == Some(0.0) {
                frame = frame.stop(Axis::Vertical);
            }
        }
        let pointer = self.pointer.clone();
        pointer.axis(self, frame);
        pointer.frame(self);
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
        // The XWayland client is inserted by smithay with `XWaylandClientData` (NOT our
        // `ClientState`), which carries its OWN `CompositorClientState`. Check it first,
        // then fall back to our socket-inserted clients. Mirrors anvil's shell/mod.rs —
        // without this the XWayland connection panics ("client without ClientState").
        if let Some(state) = client.get_data::<XWaylandClientData>() {
            return &state.compositor_state;
        }
        if let Some(state) = client.get_data::<ClientState>() {
            return &state.compositor_state;
        }
        panic!("client_compositor_state: unknown client data type")
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
                    // M3: give the freshly-mapped toplevel the keyboard focus + raise
                    // it, so a just-launched app receives keystrokes without a click.
                    self.space.raise_element(&window, true);
                    let serial = SERIAL_COUNTER.next_serial();
                    let keyboard = self.keyboard.clone();
                    keyboard.set_focus(self, Some(root.clone()), serial);
                }
            }

            // X11 (XWayland) keyboard-focus-on-association. An X11 toplevel mints its
            // handle EARLY (in `XwmHandler::map_window_request`, before the wl_surface
            // exists), so the xdg map-edge branch above is skipped for it. But the
            // X11↔wl_surface association is ASYNC: `wl_surface()` only becomes real on
            // the client's first commit (the xwayland-shell serial handshake). THIS is
            // that first commit — the surface now has a buffer and `window_for_surface`
            // matched it, so it IS associated. Give the X11 window keyboard focus once,
            // here, so a just-launched X11 app receives keystrokes without a click. The
            // one-shot guard is a marker in the window's user-data.
            if &root == surface
                && window.x11_surface().is_some()
                && surface_has_buffer(surface)
                && window.user_data().get::<X11Focused>().is_none()
            {
                window.user_data().insert_if_missing(|| X11Focused);
                self.space.raise_element(&window, true);
                let serial = SERIAL_COUNTER.next_serial();
                let keyboard = self.keyboard.clone();
                keyboard.set_focus(self, Some(root.clone()), serial);
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
        // M3 cascade: place each new toplevel offset from the previous so multiple
        // windows are visibly distinct (the "tile or cascade" gate), then advance the
        // cursor + wrap before it walks off the output.
        let loc = self.next_cascade_loc();
        self.space.map_element(window, loc, true);
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
        _layer: WlrLayer,
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

// ── XdgDecorationHandler — M3 server-side decoration negotiation ────────────────
// The compositor prefers to own the chrome (SSD), so GTK/Qt apps drop their own CSD
// titlebar. Clients that hard-refuse SSD (request ClientSide) keep their frame — the
// correct fallback, not a bug. Ported from wayland.rs (the same SSD policy). M3 does
// NOT draw a HeaderBar yet (that is Phase-8 chrome polish) — it negotiates the mode;
// windows are visually distinguished by the cascade placement above.
impl XdgDecorationHandler for State {
    fn new_decoration(&mut self, toplevel: ToplevelSurface) {
        toplevel.with_pending_state(|state| {
            state.decoration_mode = Some(DecorationMode::ServerSide);
        });
        toplevel.send_pending_configure();
    }

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
        toplevel.with_pending_state(|state| {
            state.decoration_mode = Some(DecorationMode::ServerSide);
        });
        toplevel.send_pending_configure();
    }
}

// ── XWaylandShellHandler — the X11↔wl_surface association protocol state accessor;
// a `start_wm` bound. The association is observed; the map edge that mints a handle is
// `XwmHandler::map_window_request`. Ported from wayland.rs.
impl XWaylandShellHandler for State {
    fn xwayland_shell_state(&mut self) -> &mut XWaylandShellState {
        &mut self.xwayland_shell_state
    }
}

// ── DndGrabHandler — drag'n'drop hand-off for X11 clients (a `start_wm` bound). Both
// callbacks are defaulted; HART-comp has no extra DnD bookkeeping at M3. Because the
// winit State impls SeatHandler + DataDeviceHandler and PointerFocus/TouchFocus =
// WlSurface, `WlSurface: DndFocus<State>` is satisfied (data_device's impl), so
// start_wm's `PointerFocus: DndFocus` / `TouchFocus: DndFocus` bounds hold without an
// X11Surface focus target.
impl DndGrabHandler for State {}

// ── XwmHandler — the live X11 window-manager callbacks. The map/unmap bodies carry
// the no-phantom-window bookkeeping (ported 1:1 from wayland.rs): `map_window_request`
// is the corrected Wine map edge — it mints the handle via `on_real_map(..,
// ToplevelKind::XWayland)` ONLY when a real X11 toplevel maps. All non-listed methods
// are trait-defaulted.
impl XwmHandler for State {
    fn xwm_state(&mut self, _xwm: XwmId) -> &mut X11Wm {
        self.xwm
            .as_mut()
            .expect("xwm_state called before the X11Wm was attached")
    }

    fn new_window(&mut self, _xwm: XwmId, _window: X11Surface) {}

    fn new_override_redirect_window(&mut self, _xwm: XwmId, _window: X11Surface) {}

    fn map_window_request(&mut self, _xwm: XwmId, window: X11Surface) {
        // Accept the client's map request so XWayland composites it.
        let _ = window.set_mapped(true);
        let app_id = x11_app_id(&window);
        let title = x11_title(&window);
        // Map the X11 surface into the space at the cascade location FIRST, then
        // configure it to the geometry the space gave it — modelled 1:1 on anvil's
        // `map_window_request` (place → `element_bbox` → `configure`). Configuring to
        // the post-map bbox (rather than the client's raw pre-map geometry) is the
        // robust path: the space owns the window's on-screen rect, and X11 needs an
        // explicit configure to learn its position. `element_bbox` carries the
        // client's last-configure size (non-empty for a normal X11 toplevel), so the
        // window is sized correctly, not 0×0.
        let loc = self.next_cascade_loc();
        let win = Window::new_x11_window(window);
        self.space.map_element(win.clone(), loc, true);
        if let Some(bbox) = self.space.element_bbox(&win) {
            if let Some(xsurface) = win.x11_surface() {
                let _ = xsurface.configure(Some(bbox));
            }
        }
        let handle = self.on_real_map(app_id, title, ToplevelKind::XWayland);
        win.user_data().insert_if_missing(|| handle);
        // Raise the freshly-mapped X11 toplevel in BOTH the desktop stack and the X11
        // stacking order (so XWayland keeps the same z-order the compositor shows).
        self.space.raise_element(&win, true);
        if let Some(x11) = win.x11_surface() {
            if let Some(xwm) = self.xwm.as_mut() {
                let _ = xwm.raise_window(x11);
            }
        }
        // Keyboard focus: the X11↔wl_surface association is ASYNC under XWayland — at
        // map-request time `wl_surface()` is usually still `None` (it resolves on the
        // client's first commit, via the xwayland-shell association). So focusing the
        // surface here would no-op. Stash the intent on the seat by raising + giving
        // the window the activated state; the actual keyboard focus is set on the X11
        // surface's first commit in `CompositorHandler::commit` (which now handles X11
        // roots too), once `wl_surface()` is real. If it already resolved (fast path),
        // focus it immediately.
        if let Some(surface) = win.wl_surface().map(|s| s.into_owned()) {
            let serial = SERIAL_COUNTER.next_serial();
            let keyboard = self.keyboard.clone();
            keyboard.set_focus(self, Some(surface), serial);
        }
    }

    fn mapped_override_redirect_window(&mut self, _xwm: XwmId, window: X11Surface) {
        let location = window.geometry().loc;
        let win = Window::new_x11_window(window);
        self.space.map_element(win, location, true);
    }

    fn unmapped_window(&mut self, _xwm: XwmId, window: X11Surface) {
        let elem = self
            .space
            .elements()
            .find(|w| w.x11_surface() == Some(&window))
            .cloned();
        if let Some(elem) = elem {
            if let Some(handle) = elem.user_data().get::<WindowHandle>().cloned() {
                if self.windows.on_unmap(&handle) {
                    info!(handle = handle.as_str(), "window.closed (X11 toplevel unmapped)");
                }
            }
            self.space.unmap_elem(&elem);
        }
        if !window.is_override_redirect() {
            let _ = window.set_mapped(false);
        }
    }

    fn destroyed_window(&mut self, _xwm: XwmId, _window: X11Surface) {}

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
        self.xwm = None;
    }
}

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

    // 5. The protocol globals (the S4 minimal set a shm/xdg client needs to map +
    //    the M3 set: xdg-decoration for SSD negotiation, xwayland-shell for the X11
    //    association protocol the X11Wm needs).
    let compositor_state = CompositorState::new::<State>(&dh);
    let xdg_shell_state = XdgShellState::new::<State>(&dh);
    let shm_state = ShmState::new::<State>(&dh, vec![]);
    let output_manager_state = OutputManagerState::new_with_xdg_output::<State>(&dh);
    let layer_shell_state = WlrLayerShellState::new::<State>(&dh);
    let data_device_state = DataDeviceState::new::<State>(&dh);
    let xdg_decoration_state = XdgDecorationState::new::<State>(&dh);
    let xwayland_shell_state = XWaylandShellState::new::<State>(&dh);
    let mut seat_state = SeatState::new();
    let mut seat = seat_state.new_wl_seat(&dh, "hart-winit");
    // M3: keep the keyboard + pointer handles — the loop routes winit input into them
    // and reads the cursor position for click-to-focus.
    let keyboard = seat.add_keyboard(Default::default(), 200, 25)?;
    let pointer = seat.add_pointer();

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
        pointer,
        keyboard,
        xdg_decoration_state,
        xwayland_shell_state,
        xwm: None,
        next_window_loc: (32, 32).into(),
        output: output.clone(),
    };

    // 6. (No calloop Generic source for the Display — see step 1. The Display is
    //    dispatched directly in the loop below, the safe-code minimal.rs pattern.)

    // 6b. M3 — spawn XWayland nested in OUR display, so X11 clients (Wine / xterm /
    //    xeyes) get an X server. On `Ready` we attach the X11 WM (start_wm), which
    //    routes X11 surface map/unmap through `XwmHandler`. `DISPLAY=:N` is published
    //    to the environment + logged so the harness can launch X11 children against it.
    spawn_xwayland(&dh, &event_loop.handle());

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
            // M3: route winit input (keyboard / pointer motion / button / axis) into
            // the seat — keyboard to the focused surface, pointer to the surface under
            // the cursor, click-to-focus + raise. Replaces the M1 no-op stub.
            WinitEvent::Input(event) => state.process_input_event(event),
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

            // Build paint elements front-to-back (draw_render_elements paints in slice
            // order, so the TOP-most element comes FIRST). Z-order, top→bottom:
            //   1. mapped toplevels (xdg + X11) in the space's stacking order, raised
            //      (clicked / newest) ones on top — `space.elements()` yields
            //      bottom→top, so we reverse to get top-first. Each window paints at its
            //      `element_location` so M3's cascade + click-to-raise are VISIBLE.
            //   2. layer-shell surfaces (the glass-shell desktop) underneath.
            // This replaces M1's `toplevel_surfaces()` (unordered, xdg-only, all at
            // (0,0)) so multiple windows stack + the X11 (XWayland) windows paint too.
            let mut elements: Vec<WaylandSurfaceRenderElement<GlesRenderer>> = Vec::new();

            // DEBUG: once a second, dump each space element's loc/size/surface/buffer so
            // a non-painting window (e.g. an X11 toplevel that mapped but committed no
            // buffer) is diagnosable. Gated behind HART_COMP_DEBUG_RENDER.
            if std::env::var_os("HART_COMP_DEBUG_RENDER").is_some()
                && state.start_time.elapsed().as_millis() % 1000 < 20
            {
                for window in state.space.elements() {
                    let loc = state.space.element_location(window).unwrap_or_default();
                    let bbox = state.space.element_bbox(window);
                    let surf = window.wl_surface();
                    let has_buf = surf.as_ref().map(|s| surface_has_buffer(s)).unwrap_or(false);
                    let is_x11 = window.x11_surface().is_some();
                    info!(
                        ?loc, ?bbox, has_surface = surf.is_some(), has_buffer = has_buf, is_x11,
                        "render.element"
                    );
                }
            }

            // toplevels (above), top-most first. Render via the Window's own
            // `AsRenderElements` (NOT a manual `window.wl_surface()` walk): for X11
            // windows that delegates to `X11Surface::render_elements`, which paints the
            // X11 surface tree even though the public `wl_surface()` accessor can be None
            // for XWayland-shell-associated surfaces (the bug that left xterm blank). The
            // location is PHYSICAL (scale 1.0 here, so it equals the logical coords).
            for window in state.space.elements().rev() {
                let loc = state.space.element_location(window).unwrap_or_default();
                let phys = loc.to_physical_precise_round(1.0);
                elements.extend(AsRenderElements::<GlesRenderer>::render_elements(
                    window,
                    renderer,
                    phys,
                    smithay::utils::Scale::from(1.0),
                    1.0,
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

        // ── (c) Send frame callbacks so clients draw their NEXT frame. Iterate the
        //    space (covers BOTH xdg + X11 windows) plus the layer surfaces.
        let now_ms = state.start_time.elapsed().as_millis() as u32;
        for window in state.space.elements() {
            if let Some(surface) = window.wl_surface() {
                send_frame_callbacks(&surface, now_ms);
            }
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

/// X11 WM_CLASS → app_id analogue (the join key the brain's launcher tags).
fn x11_app_id(x11: &X11Surface) -> Option<String> {
    Some(x11.class()).filter(|s| !s.is_empty())
}

/// X11 window title.
fn x11_title(x11: &X11Surface) -> Option<String> {
    Some(x11.title()).filter(|s| !s.is_empty())
}

/// M3 — spawn XWayland nested in OUR display + attach the X11 WM on `Ready`. Modelled
/// 1:1 on anvil `state.rs::start_xwayland`: `XWayland::spawn` returns `(XWayland,
/// Client)`; the `Client` is captured here and threaded into the `Ready` handler so
/// `X11Wm::start_wm` can bind it (the `Ready` event carries only the privileged X11
/// socket + display number, NOT the client). On `Ready` we publish `DISPLAY=:N` so X11
/// children (Wine / xterm / xeyes) connect to THIS X server, and log it so the harness
/// can launch them. Best-effort: a spawn failure logs + leaves `xwm = None` (the
/// compositor still runs for Wayland-native clients — XWayland is an opportunistic
/// add-on, never a boot gate).
/// XWayland child stdio: inherit (visible in hart-comp's log) when
/// `HART_COMP_XWAYLAND_VERBOSE` is set, else null. Used for both stdout + stderr.
fn xwayland_stdio() -> Stdio {
    if std::env::var_os("HART_COMP_XWAYLAND_VERBOSE").is_some() {
        Stdio::inherit()
    } else {
        Stdio::null()
    }
}

fn spawn_xwayland(dh: &DisplayHandle, loop_handle: &LoopHandle<'static, State>) {
    let (xwayland, client) = match XWayland::spawn(
        dh,
        None,
        std::iter::empty::<(String, String)>(),
        std::iter::empty::<String>(),
        true,
        // Inherit XWayland's stdout/stderr when HART_COMP_XWAYLAND_VERBOSE is set so a
        // failed bring-up is diagnosable; default null to keep the journal quiet.
        xwayland_stdio(),
        xwayland_stdio(),
        |_| {},
    ) {
        Ok(ret) => ret,
        Err(err) => {
            warn!(?err, "XWayland: spawn failed (X11 apps unavailable; Wayland-native clients unaffected)");
            return;
        }
    };

    let inserted = loop_handle.insert_source(xwayland, move |event, _, state| match event {
        XWaylandEvent::Ready {
            x11_socket,
            display_number,
        } => {
            match X11Wm::start_wm(
                state.loop_handle.clone(),
                &state.dh,
                x11_socket,
                client.clone(),
            ) {
                Ok(wm) => {
                    state.xwm = Some(wm);
                    // Publish DISPLAY so X11 children connect to OUR XWayland, and log
                    // it as the launch hint for the harness (DISPLAY=:N xterm/xeyes).
                    // (set_var is safe on edition 2021; the crate forbids `unsafe`.)
                    let x11_display = format!(":{display_number}");
                    std::env::set_var("DISPLAY", &x11_display);
                    info!(
                        x11_display = %x11_display,
                        "XWayland ready — X11 WM attached (launch X11 apps with this DISPLAY)"
                    );
                }
                Err(err) => {
                    error!(?err, "XWayland: failed to start the X11 WM");
                    state.xwm = None;
                }
            }
        }
        XWaylandEvent::Error => {
            warn!("XWayland crashed on startup");
            state.xwm = None;
        }
    });
    if let Err(err) = inserted {
        error!(?err, "XWayland: failed to insert the XWayland source into the event loop");
    }
}
