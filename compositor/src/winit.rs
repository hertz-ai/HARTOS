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

use std::cell::Cell;
use std::sync::Arc;
use std::time::{Duration, Instant};

use smithay::{
    backend::{
        input::{
            AbsolutePositionEvent, Axis, AxisSource, ButtonState, Event, InputBackend, InputEvent,
            KeyState, Keycode, KeyboardKeyEvent, PointerAxisEvent, PointerButtonEvent,
        },
        renderer::{
            Color32F, Frame, ImportAll, ImportMem, Renderer,
            element::{
                AsRenderElements, Kind,
                // M6: the render-element list is now HETEROGENEOUS — window/layer
                // surfaces (faded via the alpha arg), the software cursor (a client
                // surface OR a baked default arrow), and the killswitch black surface.
                // The `render_elements!` macro builds the single enum that unifies
                // them so one `draw_render_elements` pass paints all three kinds in
                // z-order. `SolidColorRenderElement`/`SolidColorBuffer` draw the
                // killswitch (+ the fallback cursor) with zero texture deps;
                // `MemoryRenderBufferRenderElement` + `MemoryRenderBuffer` bake the
                // default-arrow cursor once.
                memory::MemoryRenderBufferRenderElement,
                solid::{SolidColorBuffer, SolidColorRenderElement},
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
        // M5: keyboard-shortcut interception needs the keysym + modifier types and the
        // `keysyms as xkb` constant module (the `KEY_1..=KEY_9` digit range), exactly as
        // anvil/src/input_handler.rs imports them at this pinned rev. `Keysym::Tab` /
        // `Keysym::Left` / … are associated consts on `xkeysym::Keysym`; `keysym.raw()`
        // gives the `u32` the digit-range `.contains()` compares.
        keyboard::{FilterResult, KeyboardHandle, Keysym, ModifiersState, keysyms as xkb},
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
    utils::{Logical, Physical, Point, Rectangle, SERIAL_COUNTER, Serial, Size, Transform},
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

// ════════════════════════════════════════════════════════════════════════════
// M6 — the unified render element. Through M5 the render list was a homogeneous
// `Vec<WaylandSurfaceRenderElement<GlesRenderer>>`. M6 makes it HETEROGENEOUS:
//   • `Surface` — window + layer client surfaces (faded via the alpha arg on map/close)
//   • `Memory`  — the software cursor (a baked default-arrow MemoryRenderBuffer)
//   • `Solid`   — the killswitch full-output black surface (+ the cursor fallback)
// `render_elements!` generates the enum + its `RenderElement<GlesRenderer>` impl so a
// single `draw_render_elements` pass paints all three kinds in one z-ordered slice.
// Modelled on anvil's `CustomRenderElements` (render.rs) — same macro, trimmed to the
// element kinds HART-comp actually produces.
// NOTE: the `render_elements!` macro parses each trait bound as a single token tree
// (`$bound:tt`), so the bounds MUST be bare idents — `smithay::…::ImportAll` (a path
// with `::`) fails to match. The traits are imported by bare name in the `use smithay`
// block above (`renderer::{ImportAll, ImportMem}`).
smithay::backend::renderer::element::render_elements! {
    pub HartRenderElement<R> where R: ImportAll + ImportMem;
    Surface=WaylandSurfaceRenderElement<R>,
    Memory=MemoryRenderBufferRenderElement<R>,
    Solid=SolidColorRenderElement,
}

/// One-shot marker stashed in an X11 `Window`'s user-data the first time it is given
/// keyboard focus (on its first associated commit — see `CompositorHandler::commit`).
/// X11 surfaces associate their `wl_surface` ASYNCHRONOUSLY under XWayland, so the
/// focus-on-map cannot happen in `map_window_request` (the surface is still `None`
/// there); it is deferred to the first commit and de-duplicated by this marker.
struct X11Focused;

// ════════════════════════════════════════════════════════════════════════════
// M5 — WM completeness: keyboard-shortcut actions + per-window workspace tag +
// pre-snap geometry stash.
// ════════════════════════════════════════════════════════════════════════════

/// The action a compositor keyboard shortcut resolves to (anvil's `KeyAction`
/// analogue, IPC_PROTOCOL.md §4 verbs surfaced as chords). `process_keyboard_shortcut`
/// maps a `(ModifiersState, Keysym)` to one of these; the chord is INTERCEPTED (never
/// forwarded to the focused client) iff the map returns `Some`. The action is executed
/// AFTER `KeyboardHandle::input` returns (outside the filter closure) to avoid a second
/// `&mut self` borrow — exactly anvil's two-phase pattern.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum WmAction {
    /// Alt+Tab — cycle keyboard focus to the next window (stack-order MRU).
    CycleFocus,
    /// Alt+Shift+Tab — cycle focus to the previous window.
    CycleFocusBack,
    /// Super+1..9 — switch to workspace N (0-based; N = keysym - KEY_1).
    SwitchWorkspace(usize),
    /// Super+Shift+1..9 — move the focused window to workspace N.
    MoveToWorkspace(usize),
    /// Super+Q — close the focused toplevel.
    CloseFocused,
    /// Super+Left — snap the focused window to the left half.
    SnapLeft,
    /// Super+Right — snap the focused window to the right half.
    SnapRight,
    /// Super+Up — maximize the focused window.
    Maximize,
    /// Super+Down — restore the focused window's pre-snap geometry.
    RestoreWindow,
    /// Super+D — toggle show-desktop (hide all, then restore).
    ShowDesktop,
}

/// Which workspace a mapped `Window` belongs to. Stashed in the `Window`'s user-data
/// (the SAME mechanism as `WindowHandle`) on map, read by `window.list` + by the
/// move/switch logic. `Cell` because `UserDataMap` only offers `insert_if_missing` +
/// `get` (no replace) — `move_to_workspace` re-tags via `.set()`. Default 0 (the
/// first workspace), matching `State.active_workspace`'s initial value.
struct WorkspaceTag(Cell<usize>);

/// The focused window's geometry captured the FIRST time it is snapped/maximized, so
/// Super+Down (`RestoreWindow`) can put it back. Stashed in user-data; `Cell` for the
/// same insert-if-missing-then-set reason as `WorkspaceTag`. `None` until the first
/// snap — `RestoreWindow` with no stash falls back to a centered default.
struct PreSnapGeom(Cell<Option<Rectangle<i32, Logical>>>);

/// A window that has been moved OFF the visible `Space` (it lives on a non-active
/// workspace, or is hidden by show-desktop). Held here with the location to restore
/// it to, so switching back re-maps it exactly where it was. Keeping inactive windows
/// OUT of `self.space` is what lets the render loop + input path stay UNCHANGED — they
/// naturally only ever see the active workspace (one source of truth, no parallel
/// "is this window visible" filter sprinkled through the render path).
pub(crate) struct HiddenWindow {
    pub(crate) window: Window,
    /// The workspace this window belongs to (so `switch_workspace(n)` knows which held
    /// windows to bring back).
    pub(crate) workspace: usize,
    /// Where it was on the visible output before being hidden.
    pub(crate) loc: Point<i32, Logical>,
}

// ════════════════════════════════════════════════════════════════════════════
// M6 — effects: per-window fade clock + the workspace-switch crossfade clock.
// ════════════════════════════════════════════════════════════════════════════

/// Fade duration for a window map-in (the close-out fade is observed differently —
/// see `close_focused_window`/the IPC close — because the surface is gone once the
/// client acks the close, so we fade the LIVE map-in and let close be instant; a
/// proper close-fade needs holding a snapshot texture, deferred as polish). 150ms is
/// the M6 spec figure: long enough to capture mid-fade, short enough to feel instant.
const FADE_IN_MS: u128 = 150;
/// Workspace-switch crossfade duration — the whole active set fades in on switch.
const WS_FADE_MS: u128 = 120;

/// When a window was mapped, so the render loop can compute its fade-in alpha
/// (`elapsed/FADE_IN_MS`, clamped to 1.0). Stashed in the window's user-data (the
/// `WindowHandle` mechanism). `Cell` for the insert-if-missing-then-read pattern the
/// other M5 user-data carriers use. The instant is monotonic (`Instant`).
struct MapAnim(Instant);

impl MapAnim {
    /// The fade-in alpha for this window NOW: 0→1 over `FADE_IN_MS`, then pinned 1.0.
    /// A fully-faded-in window returns exactly 1.0 so the common steady-state path
    /// hits the renderer's opaque fast-path (no per-frame alpha blend once settled).
    fn alpha(&self) -> f32 {
        let e = self.0.elapsed().as_millis();
        if e >= FADE_IN_MS {
            1.0
        } else {
            (e as f32 / FADE_IN_MS as f32).clamp(0.0, 1.0)
        }
    }
    /// Is this window still animating (so the loop must keep redrawing)?
    fn animating(&self) -> bool {
        self.0.elapsed().as_millis() < FADE_IN_MS
    }
}

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

    // ── M4: the com.hart.Compositor IPC server's per-compositor state (the
    // `events.subscribe` sinks). The IPC command handlers (src/ipc.rs) mutate the
    // fields ABOVE (space/seat/xwm) against a verb; this holds only the event
    // fan-out subscribers so the map/unmap/focus edges below can push event frames.
    pub ipc: crate::ipc::IpcState,

    // ── M5: WORKSPACES. The visible `space` above holds ONLY the active workspace's
    // windows (so render + input stay single-source). `active_workspace` is the
    // currently-shown index (0-based); `hidden_windows` holds every window on a
    // NON-active workspace (and the show-desktop stash), each with the location to
    // restore it to. Switching = unmap the active set into `hidden_windows` + map the
    // target set back. The M4 `workspace.switch`/`move_to_workspace` IPC verbs drive
    // this SAME state (no parallel workspace model).
    pub active_workspace: usize,
    pub hidden_windows: Vec<HiddenWindow>,
    /// Show-desktop (Super+D) toggle: when true the active workspace's windows are
    /// stashed in `hidden_windows` (workspace = `active_workspace`) and the desktop is
    /// bare; the next toggle restores them. A bool so a second Super+D un-hides.
    pub desktop_shown: bool,

    /// M5: KEYCODES whose PRESS triggered an intercepted shortcut, so the matching
    /// RELEASE is also swallowed (else the focused client gets a dangling key-up for a
    /// key it never saw pressed). Anvil's `suppressed_keys` mechanism, but keyed on the
    /// physical KEYCODE rather than the resolved keysym: a keycode is INVARIANT between a
    /// key's press and its release, whereas the modified keysym is NOT (release a held
    /// Shift/Super before the key and the release's `modified_sym` differs from the
    /// press's — anvil's keysym-keyed set would then MISS the release and leak a dangling
    /// key-up to the focused client). Keycode-keyed suppression closes that leak.
    pub suppressed_keys: Vec<Keycode>,

    // ── M6 — SCREENCOPY (zwlr_screencopy_v1). `pending_screencopy` holds `copy`-
    // requested frames awaiting the next paint (the read-back needs the live
    // GlesRenderer + framebuffer, which only exist inside the render closure). The
    // render loop drains it via `screencopy::service_pending_screencopy`. See
    // src/screencopy.rs. ──
    pub pending_screencopy: Vec<crate::screencopy::PendingScreencopy>,

    // ── M6 — SCREEN KILL-SWITCH (constitutional). When the human cuts `screen`, the
    // brain pushes `screen.kill {on:true}` over the EXISTING IPC, setting this flag.
    // While set: (a) the render loop draws a full-output OPAQUE BLACK element ABOVE
    // everything, and (b) input is NOT forwarded to clients, and (c) every screencopy
    // `copy` immediately `failed()`s. One bool drives all three — the no-capture +
    // privacy + control kill, enforced at the compositor with zero per-frame IPC. ──
    pub capture_blocked: bool,

    // ── M6 — SOFTWARE CURSOR. `cursor_status` is the latest client-requested cursor
    // image (a surface the client set, Hidden, or the default named arrow). The render
    // loop draws it ABOVE all windows/layers (but BELOW the killswitch). `cursor_buffer`
    // caches the baked default-arrow `MemoryRenderBuffer` (built once at boot) so the
    // common "no client cursor" case costs one cached element + a reposition, no
    // per-frame allocation. ──
    pub cursor_status: CursorImageStatus,
    pub cursor_buffer: smithay::backend::renderer::element::memory::MemoryRenderBuffer,
    /// The baked arrow's hotspot (the click point, relative to the image top-left), so
    /// the cursor is positioned so its tip — not its top-left — sits at the pointer.
    pub cursor_hotspot: Point<i32, Logical>,

    // ── M6 — EFFECTS clocks. `ws_switch_at` is set when a workspace switch happens so
    // the render loop crossfades the newly-shown set in over `WS_FADE_MS`. `None` once
    // settled. Per-window map fade lives in each window's `MapAnim` user-data. ──
    pub ws_switch_at: Option<Instant>,

    // ── M6 — the killswitch black surface buffer (full-output opaque black). Cached +
    // resized on output change so the kill draw is one cheap solid element. ──
    pub black_buffer: SolidColorBuffer,
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

    /// M6 — the current output size in PHYSICAL (framebuffer) pixels. Screencopy
    /// reports this as the capturable region; the killswitch + cursor math also use it.
    /// Scale is 1.0 on the winit path, so logical == physical here; reading the output's
    /// current mode keeps it correct across a winit resize.
    pub fn output_physical_size(&self) -> Size<i32, Physical> {
        self.output
            .current_mode()
            .map(|m| m.size)
            .unwrap_or_else(|| (1280, 800).into())
    }

    /// M6 — the screen kill-switch. The brain pushes `screen.kill {on}` over the IPC
    /// when the human cuts/restores `screen`; this flips the one flag that drives all
    /// three effects (black surface ABOVE everything + input not forwarded + screencopy
    /// refused). Returns the new state. A force-redraw is implicit (the loop always
    /// repaints), so the black surface appears on the very next frame.
    pub fn set_capture_blocked(&mut self, on: bool) -> bool {
        if self.capture_blocked != on {
            self.capture_blocked = on;
            info!(blocked = on, "screen.kill — capture/input/screencopy gate toggled");
        }
        // If turning the kill OFF, also fail any frames that queued just as it engaged
        // (defensive — queue_copy already refuses while blocked, so this is normally a
        // no-op). If turning ON, fail everything already queued so no in-flight capture
        // leaks a frame painted before the black surface.
        if on {
            for p in self.pending_screencopy.drain(..) {
                p.frame.failed();
            }
        }
        self.capture_blocked
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
        // M6 — screen kill-switch: while the human has cut `screen`, do NOT forward ANY
        // input to clients (the privacy/control half of the kill — apps behind the black
        // surface receive nothing). We still SWALLOW the events (vs. ignoring earlier in
        // the pipe) so no key/pointer leaks through; the compositor's own shutdown path
        // (WinitEvent::CloseRequested) is unaffected as it never reaches here.
        if self.capture_blocked {
            return;
        }
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

    /// M5 — intercept compositor keyboard shortcuts BEFORE forwarding to the focused
    /// client; forward everything else. Modelled 1:1 on anvil's `keyboard_key_to_action`
    /// (`input_handler.rs`, this pinned rev): the seat's `KeyboardHandle::input` filter
    /// closure runs before the key reaches the focused client, so returning
    /// `FilterResult::Intercept(action)` SWALLOWS the chord (the app never sees it) and
    /// `input()` returns `Some(action)`; `Forward` delivers it normally.
    ///
    /// Two things make the chord NOT also reach the client (the explicit M5 ask):
    ///   1. On a PRESS that maps to an action, return `Intercept` — that stops the press.
    ///   2. The matching RELEASE is also swallowed via `suppressed_keys` (else the client
    ///      gets a dangling key-up for a key it never saw pressed): on the action-press we
    ///      push the keysym; on release, if present we remove it + `Intercept(())` (do
    ///      nothing), else `Forward`.
    ///
    /// The action is EXECUTED after `input()` returns (outside the closure) to avoid a
    /// second `&mut self` borrow — the closure only RESOLVES the action; the caller runs
    /// it. (`apply_wm_action` re-borrows `self` freely.)
    fn on_keyboard_key<B: InputBackend>(&mut self, evt: B::KeyboardKeyEvent) {
        let serial = SERIAL_COUNTER.next_serial();
        let time = evt.time_msec();
        let code = evt.key_code();
        let state = evt.state();
        let keyboard = self.keyboard.clone();

        // Clone the suppressed-keys set into the closure, write it back after (anvil's
        // pattern — the closure can't borrow `self.suppressed_keys` while `input` holds
        // `&mut self`). The closure returns the resolved `WmAction` (if any) as the
        // filter's `T`; `Intercept(None)` swallows a key with no action (a suppressed
        // release), `Intercept(Some(a))` swallows + carries the action to run.
        let mut suppressed = self.suppressed_keys.clone();
        let action: Option<WmAction> = keyboard
            .input::<Option<WmAction>, _>(self, code, state, serial, time, |_, modifiers, handle| {
                // The MODIFIED sym (Shift→uppercase etc.) drives the letter/arrow/Tab
                // chords (so Super+Shift+letter would match its uppercase sym, anvil's
                // convention). The DIGIT row, however, must NOT use the modified sym:
                // Shift maps the US digits to `!@#$%^&*(`, which fall OUTSIDE the
                // `KEY_1..=KEY_9` range, so a Super+Shift+N "move to workspace" chord
                // would never resolve. `raw_latin_sym_or_raw_current_sym` returns the
                // layout-agnostic LEVEL-0 sym (the bare `1`..`9`) regardless of Shift, so
                // both Super+N and Super+Shift+N see the same digit. (None only when the
                // keycode produces no valid keysym — then no digit chord can match.)
                let keysym = handle.modified_sym();
                let digit_sym = handle.raw_latin_sym_or_raw_current_sym();
                // M5 DEBUG (HART_COMP_DEBUG_KEYS): trace every key so the harness can
                // verify the chord reaches the seat + the modifier state is tracked.
                if std::env::var_os("HART_COMP_DEBUG_KEYS").is_some() {
                    info!(
                        ?state,
                        logo = modifiers.logo, alt = modifiers.alt, shift = modifiers.shift,
                        raw = keysym.raw(),
                        digit = digit_sym.map(|s| s.raw()),
                        keycode = code.raw(),
                        "key.seen"
                    );
                }
                if state == KeyState::Pressed {
                    match process_keyboard_shortcut(*modifiers, keysym, digit_sym) {
                        Some(act) => {
                            // Remember the KEYCODE so its release is swallowed too (the
                            // keycode is invariant across press→release; the keysym is not).
                            suppressed.push(code);
                            FilterResult::Intercept(Some(act))
                        }
                        None => FilterResult::Forward,
                    }
                } else {
                    // Release: swallow iff this KEYCODE's press was intercepted.
                    if suppressed.contains(&code) {
                        suppressed.retain(|k| *k != code);
                        FilterResult::Intercept(None)
                    } else {
                        FilterResult::Forward
                    }
                }
            })
            .flatten();
        self.suppressed_keys = suppressed;

        // Run the resolved action now that `input()` released its `&mut self`.
        if let Some(act) = action {
            self.apply_wm_action(act, serial);
        }
    }

    // ════════════════════════════════════════════════════════════════════════
    // M5 — keyboard-shortcut action execution. Each arm calls EXISTING helpers
    // (the M3 focus body / the M4 IPC geometry+close methods / the M5 workspace
    // methods) so there is no new geometry/focus code path here — the chords are
    // a second TRIGGER for the same verbs the IPC already drives.
    // ════════════════════════════════════════════════════════════════════════

    /// Execute a resolved `WmAction`. Called from `on_keyboard_key` AFTER the seat's
    /// `input()` returns, so re-borrowing `self` is free.
    fn apply_wm_action(&mut self, action: WmAction, serial: Serial) {
        match action {
            WmAction::CycleFocus => self.cycle_focus(true, serial),
            WmAction::CycleFocusBack => self.cycle_focus(false, serial),
            // switch/move return a bool (changed?) for the IPC verb; the chord ignores it.
            WmAction::SwitchWorkspace(n) => {
                let _ = self.switch_workspace(n);
            }
            WmAction::MoveToWorkspace(n) => {
                let _ = self.move_focused_to_workspace(n);
            }
            WmAction::CloseFocused => self.close_focused_window(),
            WmAction::SnapLeft => self.snap_focused("left-half"),
            WmAction::SnapRight => self.snap_focused("right-half"),
            WmAction::Maximize => self.snap_focused("maximize"),
            WmAction::RestoreWindow => self.restore_focused_window(),
            WmAction::ShowDesktop => self.toggle_show_desktop(),
        }
    }

    /// The currently keyboard-focused mapped `Window`, resolved via the seat's current
    /// focus surface (walking to the root) — the single "what does the user have
    /// focused" query the snap/close/restore actions share.
    fn focused_window(&self) -> Option<Window> {
        let focus = self.keyboard.current_focus()?;
        let mut root = focus.clone();
        while let Some(parent) = get_parent(&root) {
            root = parent;
        }
        self.window_for_surface(&root)
    }

    /// Alt+Tab focus cycle. Stack-order rotation: `space.elements()` yields bottom→top,
    /// the top (`order[n-1]`) being the currently-raised/focused window. To visit EVERY
    /// window across repeated presses (not just toggle the top two), FORWARD raises the
    /// BOTTOM-most window (`order[0]`, the least-recently-raised) — each press rotates
    /// the whole stack bottom→up, so a third Alt+Tab reaches the third window. BACKWARD
    /// raises the window just below the top (`order[n-2]`), undoing the last forward.
    /// Raising the chosen window + focusing it is the identical body as `ipc_focus_window`
    /// / the click path. (A strict most-recently-USED order would need a
    /// `focus_history: Vec<Window>`; anvil has no MRU either — this is stack-order, which
    /// for the common "raise each in turn" case behaves the same.)
    fn cycle_focus(&mut self, forward: bool, serial: Serial) {
        // Bottom→top stacking order of the active workspace.
        let order: Vec<Window> = self.space.elements().cloned().collect();
        let n = order.len();
        if n < 2 {
            return; // 0 or 1 window — nothing to cycle.
        }
        // FORWARD → the bottom window rotates to front; BACKWARD → the one below the top.
        let next_idx = if forward { 0 } else { n - 2 };
        let target = order[next_idx].clone();
        self.space.raise_element(&target, true);
        if let Some(x11) = target.x11_surface() {
            if let Some(xwm) = self.xwm.as_mut() {
                let _ = xwm.raise_window(x11);
            }
        }
        let surface = target.wl_surface().map(|s| s.into_owned());
        let keyboard = self.keyboard.clone();
        keyboard.set_focus(self, surface, serial);
        // Keep IPC subscribers in sync (the M4 focus edge).
        if let Some(handle) = target.user_data().get::<WindowHandle>().map(|h| h.as_str().to_string()) {
            let payload = ipc_event_window_json(self, &target, &handle);
            self.ipc.emit_event("window.focused", payload);
        }
    }

    /// Super+Q — close the focused toplevel (reuses the M4 `ipc_close_window` body:
    /// xdg `send_close()` / X11 `set_mapped(false)`; the real destroy + `window.closed`
    /// flow through `toplevel_destroyed`/`unmapped_window`).
    fn close_focused_window(&mut self) {
        if let Some(window) = self.focused_window() {
            if let Some(handle) = window.user_data().get::<WindowHandle>().map(|h| h.as_str().to_string()) {
                self.ipc_close_window(&handle);
            }
        }
    }

    /// Super+Left/Right/Up — snap the focused window to a named zone (left/right half,
    /// or maximize). Stashes the window's PRE-snap geometry the FIRST time so Super+Down
    /// can restore it, then reuses the M4 `ipc_zone_rect` + `ipc_place_window` geometry
    /// (no new geometry math). `zone` is one of `ipc_zone_rect`'s names.
    fn snap_focused(&mut self, zone: &str) {
        let window = match self.focused_window() {
            Some(w) => w,
            None => return,
        };
        let handle = match window.user_data().get::<WindowHandle>().map(|h| h.as_str().to_string()) {
            Some(h) => h,
            None => return,
        };
        // Stash the current geometry once (so RestoreWindow has a target).
        if let Some(cur) = self.space.element_geometry(&window) {
            window
                .user_data()
                .insert_if_missing(|| PreSnapGeom(Cell::new(None)));
            if let Some(stash) = window.user_data().get::<PreSnapGeom>() {
                if stash.0.get().is_none() {
                    stash.0.set(Some(cur));
                }
            }
        }
        if let Some((x, y, w, h)) = self.ipc_zone_rect(zone) {
            self.ipc_place_window(&handle, x, y, w, h);
        }
    }

    /// Super+Down — restore the focused window to its stashed pre-snap geometry, or a
    /// centered 60% default if it was never snapped. Clears the stash so a later snap
    /// re-captures. Reuses `ipc_place_window`.
    fn restore_focused_window(&mut self) {
        let window = match self.focused_window() {
            Some(w) => w,
            None => return,
        };
        let handle = match window.user_data().get::<WindowHandle>().map(|h| h.as_str().to_string()) {
            Some(h) => h,
            None => return,
        };
        let stashed = window
            .user_data()
            .get::<PreSnapGeom>()
            .and_then(|s| s.0.take()); // take() clears it so the next snap re-captures.
        let (x, y, w, h) = match stashed {
            Some(g) => (g.loc.x, g.loc.y, g.size.w, g.size.h),
            None => {
                // No stash → a sensible centered 60% of the output.
                match self.space.output_geometry(&self.output) {
                    Some(o) => {
                        let w = o.size.w * 3 / 5;
                        let h = o.size.h * 3 / 5;
                        (o.loc.x + (o.size.w - w) / 2, o.loc.y + (o.size.h - h) / 2, w, h)
                    }
                    None => return,
                }
            }
        };
        self.ipc_place_window(&handle, x, y, w, h);
    }

    // ════════════════════════════════════════════════════════════════════════
    // M5 — workspaces. The visible `space` holds ONLY the active workspace; every
    // other window lives in `hidden_windows` (off-screen, with its restore loc).
    // Switching = stash the active set + restore the target set. This keeps the
    // render + input paths single-source (they only ever see `space`).
    // ════════════════════════════════════════════════════════════════════════

    /// Tag a freshly-mapped window with the active workspace (called from the map
    /// edges). The tag rides in the window's user-data (the `WindowHandle` mechanism)
    /// so `window.list` + move/switch can read it. Idempotent: only sets it once.
    fn tag_window_workspace(&self, window: &Window) {
        let ws = self.active_workspace;
        window
            .user_data()
            .insert_if_missing(|| WorkspaceTag(Cell::new(ws)));
    }

    /// Read a window's workspace tag (0 if somehow untagged — the first workspace).
    fn window_workspace(window: &Window) -> usize {
        window
            .user_data()
            .get::<WorkspaceTag>()
            .map(|t| t.0.get())
            .unwrap_or(0)
    }

    /// The held (non-visible) windows, for `window.list` to enumerate alongside the
    /// visible space. `pub(crate)` so ipc.rs can read it without exposing the field's
    /// mutation surface.
    pub(crate) fn ipc_hidden_windows(&self) -> &[HiddenWindow] {
        &self.hidden_windows
    }

    /// Super+1..9 / `workspace.switch(n)` — show workspace `n`. Stashes every window
    /// currently on the visible space into `hidden_windows`, then restores every held
    /// window tagged `n` and focuses the top one. No-op if already on `n`. Returns true
    /// if the active workspace changed (the IPC verb reports it).
    pub fn switch_workspace(&mut self, n: usize) -> bool {
        if n == self.active_workspace {
            return false;
        }
        // 1. Stash the active set (each visible window → hidden_windows, tagged with the
        //    workspace we are LEAVING so a later switch-back restores it here).
        let leaving = self.active_workspace;
        let active: Vec<Window> = self.space.elements().cloned().collect();
        for window in active {
            let loc = self.space.element_location(&window).unwrap_or_default();
            // Keep the tag authoritative (it is already `leaving` for these, but a
            // window that was move_to_workspace'd here carries the right tag already).
            window
                .user_data()
                .insert_if_missing(|| WorkspaceTag(Cell::new(leaving)));
            let ws = Self::window_workspace(&window);
            self.space.unmap_elem(&window);
            self.hidden_windows.push(HiddenWindow { window, workspace: ws, loc });
        }
        // 2. Restore the target set (drain hidden_windows where workspace == n).
        self.active_workspace = n;
        let mut restored: Vec<Window> = Vec::new();
        let mut i = 0;
        while i < self.hidden_windows.len() {
            if self.hidden_windows[i].workspace == n {
                let hw = self.hidden_windows.remove(i);
                self.space.map_element(hw.window.clone(), hw.loc, false);
                restored.push(hw.window);
            } else {
                i += 1;
            }
        }
        // 3. Focus the top restored window (if any) so the new workspace is live.
        if let Some(top) = restored.last().cloned() {
            self.space.raise_element(&top, true);
            let serial = SERIAL_COUNTER.next_serial();
            let surface = top.wl_surface().map(|s| s.into_owned());
            let keyboard = self.keyboard.clone();
            keyboard.set_focus(self, surface, serial);
        } else {
            // Empty workspace — drop keyboard focus so stale keys don't leak to a
            // window that is no longer visible.
            let serial = SERIAL_COUNTER.next_serial();
            let keyboard = self.keyboard.clone();
            keyboard.set_focus(self, None, serial);
        }
        // Switching workspaces means the desktop is showing its windows again.
        self.desktop_shown = true;
        // M6: start the workspace-switch crossfade — the render loop fades the
        // newly-shown set in over WS_FADE_MS (a cheap whole-set alpha ramp).
        self.ws_switch_at = Some(Instant::now());
        info!(workspace = n, restored = restored.len(), "workspace.switched");
        true
    }

    /// Super+Shift+1..9 / `move_to_workspace(handle,n)` — move the focused window to
    /// workspace `n`. If `n` is the active workspace it is a no-op. Otherwise the window
    /// is re-tagged, unmapped off the visible space into `hidden_windows`, and focus
    /// moves to the next remaining active window. Returns true if a window moved.
    pub fn move_focused_to_workspace(&mut self, n: usize) -> bool {
        let window = match self.focused_window() {
            Some(w) => w,
            None => return false,
        };
        self.move_window_to_workspace(&window, n)
    }

    /// The handle-keyed twin of `move_focused_to_workspace`, for the IPC verb. Moves a
    /// specific mapped window (by handle) to workspace `n`. Resolves the window across
    /// BOTH the visible space and the hidden set (an agent may move a window that lives
    /// on a non-active workspace).
    pub fn move_window_to_workspace_by_handle(&mut self, handle: &str, n: usize) -> bool {
        // Visible window?
        if let Some(window) = self.ipc_window_for_handle(handle) {
            return self.move_window_to_workspace(&window, n);
        }
        // Hidden window? Re-tag it + relocate it within the hidden set (or surface it
        // if `n` is the active workspace).
        if let Some(idx) = self
            .hidden_windows
            .iter()
            .position(|hw| hw.window.user_data().get::<WindowHandle>().map(|h| h.as_str() == handle).unwrap_or(false))
        {
            let window = self.hidden_windows[idx].window.clone();
            window.user_data().insert_if_missing(|| WorkspaceTag(Cell::new(n)));
            if let Some(tag) = window.user_data().get::<WorkspaceTag>() {
                tag.0.set(n);
            }
            if n == self.active_workspace {
                // Surface it onto the visible space at its stored location.
                let hw = self.hidden_windows.remove(idx);
                self.space.map_element(hw.window, hw.loc, false);
            } else {
                self.hidden_windows[idx].workspace = n;
            }
            return true;
        }
        false
    }

    /// A toplevel that was on a NON-active workspace (so it lived in `hidden_windows`,
    /// not the visible space) has been destroyed. Resolve it in the hidden set, emit
    /// `window.closed` + invalidate its handle, and drop it — the symmetric cleanup the
    /// space-based destroy path does for visible windows. `pred` matches the destroyed
    /// surface (xdg or X11). Returns true if a hidden window was purged.
    fn purge_hidden_window(&mut self, pred: impl Fn(&Window) -> bool) -> bool {
        let idx = match self.hidden_windows.iter().position(|hw| pred(&hw.window)) {
            Some(i) => i,
            None => return false,
        };
        let hw = self.hidden_windows.remove(idx);
        if let Some(handle) = hw.window.user_data().get::<WindowHandle>().cloned() {
            if self.windows.on_unmap(&handle) {
                info!(handle = handle.as_str(), "window.closed (hidden-workspace toplevel destroyed)");
                let payload = ipc_event_window_json(self, &hw.window, handle.as_str());
                self.ipc.emit_event("window.closed", payload);
            }
        }
        true
    }

    /// Shared body: move `window` to workspace `n`. If `n == active`, ensure the tag is
    /// `n` and keep it visible (no-op move). Otherwise stash it off-screen and refocus
    /// the next active window.
    fn move_window_to_workspace(&mut self, window: &Window, n: usize) -> bool {
        // Re-tag (set, not insert — the window may already be tagged).
        window
            .user_data()
            .insert_if_missing(|| WorkspaceTag(Cell::new(n)));
        if let Some(tag) = window.user_data().get::<WorkspaceTag>() {
            tag.0.set(n);
        }
        if n == self.active_workspace {
            return true; // Stays visible; only the tag changed.
        }
        let loc = self.space.element_location(window).unwrap_or_default();
        self.space.unmap_elem(window);
        self.hidden_windows.push(HiddenWindow {
            window: window.clone(),
            workspace: n,
            loc,
        });
        // Refocus the next remaining active window (top of the stack), or clear focus.
        let serial = SERIAL_COUNTER.next_serial();
        let keyboard = self.keyboard.clone();
        if let Some(top) = self.space.elements().last().cloned() {
            self.space.raise_element(&top, true);
            let surface = top.wl_surface().map(|s| s.into_owned());
            keyboard.set_focus(self, surface, serial);
        } else {
            keyboard.set_focus(self, None, serial);
        }
        info!(workspace = n, "window.moved_to_workspace");
        true
    }

    /// Super+D — toggle show-desktop. When showing the desktop (hiding windows): stash
    /// every visible window into `hidden_windows` tagged with the active workspace.
    /// When restoring: bring back exactly those (the same path `switch_workspace` uses
    /// to restore the active set). A second Super+D un-hides.
    pub fn toggle_show_desktop(&mut self) {
        if self.desktop_shown {
            // Hide: stash the active set (tagged active so restore brings them back).
            let active: Vec<Window> = self.space.elements().cloned().collect();
            if active.is_empty() {
                return; // Nothing to hide.
            }
            let ws = self.active_workspace;
            for window in active {
                let loc = self.space.element_location(&window).unwrap_or_default();
                window
                    .user_data()
                    .insert_if_missing(|| WorkspaceTag(Cell::new(ws)));
                self.space.unmap_elem(&window);
                self.hidden_windows.push(HiddenWindow { window, workspace: ws, loc });
            }
            let serial = SERIAL_COUNTER.next_serial();
            let keyboard = self.keyboard.clone();
            keyboard.set_focus(self, None, serial);
            self.desktop_shown = false;
            info!("desktop.shown (windows hidden)");
        } else {
            // Restore: re-map the active-workspace held windows.
            let n = self.active_workspace;
            let mut restored: Vec<Window> = Vec::new();
            let mut i = 0;
            while i < self.hidden_windows.len() {
                if self.hidden_windows[i].workspace == n {
                    let hw = self.hidden_windows.remove(i);
                    self.space.map_element(hw.window.clone(), hw.loc, false);
                    restored.push(hw.window);
                } else {
                    i += 1;
                }
            }
            if let Some(top) = restored.last().cloned() {
                self.space.raise_element(&top, true);
                let serial = SERIAL_COUNTER.next_serial();
                let surface = top.wl_surface().map(|s| s.into_owned());
                let keyboard = self.keyboard.clone();
                keyboard.set_focus(self, surface, serial);
            }
            self.desktop_shown = true;
            info!(restored = restored.len(), "desktop.restored (windows back)");
        }
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
                    let handle_str = handle.as_str().to_string();
                    // Stash the handle on the Window so destroy can reverse it.
                    if let Some(w) = self.window_for_surface(&root) {
                        w.user_data().insert_if_missing(|| handle);
                    }
                    // M5: tag the new toplevel with the active workspace (so
                    // `window.list` + move/switch can read it). It maps onto the
                    // workspace the user is currently looking at.
                    self.tag_window_workspace(&window);
                    // M6: start the map-in fade clock (the render loop ramps this
                    // window's alpha 0→1 over FADE_IN_MS). Stamped once on the real map.
                    window.user_data().insert_if_missing(|| MapAnim(Instant::now()));
                    // M4: emit window.opened to IPC subscribers (IPC_PROTOCOL.md §5) —
                    // the same edge that minted the handle, so the event proves a real map.
                    let payload = ipc_event_window_json(self, &window, &handle_str);
                    self.ipc.emit_event("window.opened", payload);
                    // M3: give the freshly-mapped toplevel the keyboard focus + raise
                    // it, so a just-launched app receives keystrokes without a click.
                    self.space.raise_element(&window, true);
                    let serial = SERIAL_COUNTER.next_serial();
                    let keyboard = self.keyboard.clone();
                    keyboard.set_focus(self, Some(root.clone()), serial);
                    // M4: input focus changed → window.focused (additive signal, §5).
                    let focus_payload = ipc_event_window_json(self, &window, &handle_str);
                    self.ipc.emit_event("window.focused", focus_payload);
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
                    // M4: emit window.closed to IPC subscribers, invalidating the
                    // handle (IPC_PROTOCOL.md §5). Built before unmap so geometry is real.
                    let payload = ipc_event_window_json(self, &window, handle.as_str());
                    self.ipc.emit_event("window.closed", payload);
                }
            }
            self.space.unmap_elem(&window);
        } else {
            // M5: the destroyed toplevel may be on a NON-active workspace (so it lives
            // in `hidden_windows`, not the visible space). Purge it there.
            self.purge_hidden_window(|w| {
                w.wl_surface().map(|s| &*s == &wl).unwrap_or(false)
            });
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
    /// M6: a client set (or hid) its cursor. Stash the latest status; the render loop
    /// reads it each frame to draw the software cursor (a client surface, the baked
    /// default arrow, or nothing when Hidden). The standard anvil pattern — store, draw
    /// in the loop, no work here.
    fn cursor_image(&mut self, _seat: &Seat<Self>, image: CursorImageStatus) {
        self.cursor_status = image;
    }
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
        let handle_str = handle.as_str().to_string();
        win.user_data().insert_if_missing(|| handle);
        // M5: tag the new X11 toplevel with the active workspace.
        self.tag_window_workspace(&win);
        // M6: start the map-in fade clock (render loop ramps alpha 0→1 over FADE_IN_MS).
        win.user_data().insert_if_missing(|| MapAnim(Instant::now()));
        // M4: emit window.opened to IPC subscribers (IPC_PROTOCOL.md §5) — minted on
        // the REAL X11 map (the corrected Wine path: a handle only here, never on the
        // installer's unconditional success). Note an X11 window's geometry is known
        // (we configured it above) even before its wl_surface associates.
        let payload = ipc_event_window_json(self, &win, &handle_str);
        self.ipc.emit_event("window.opened", payload);
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
                    // M4: emit window.closed to IPC subscribers (IPC_PROTOCOL.md §5).
                    let payload = ipc_event_window_json(self, &elem, handle.as_str());
                    self.ipc.emit_event("window.closed", payload);
                }
            }
            self.space.unmap_elem(&elem);
        } else {
            // M5: an X11 toplevel on a non-active workspace lives in `hidden_windows`.
            self.purge_hidden_window(|w| w.x11_surface() == Some(&window));
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

/// M5 — map a chord to a compositor `WmAction`, or `None` to forward the key to the
/// focused client. Modelled 1:1 on anvil's `process_keyboard_shortcut` (`KeyAction`
/// map): `mods.logo` = the Super/Windows key, `mods.alt`/`mods.shift` the obvious
/// modifiers; letter/arrow/Tab chords match the named `Keysym` consts
/// (`Keysym::q`/`Left`/…) on the MODIFIED sym, and the digit row matches the
/// `KEY_1..=KEY_9` range with `n = raw - KEY_1` the 0-based workspace index.
///
/// `keysym` is the MODIFIED sym (Shift→uppercase applied), used for the letter/arrow/
/// Tab chords. `digit_sym` is the LAYOUT-AGNOSTIC level-0 sym from the keyboard handle
/// (`raw_latin_sym_or_raw_current_sym`), used ONLY for the digit-row chords — because
/// Shift maps the US digits to `!@#$%^&*(` (NOT a uniform offset), so matching the
/// MODIFIED sym against `KEY_1..=KEY_9` would make every Super+Shift+N "move to
/// workspace" chord silently fail. The level-0 sym is the bare `1`..`9` whether or not
/// Shift is held, so Super+N and Super+Shift+N resolve the same digit. `digit_sym` is
/// `None` only when the keycode produces no valid keysym (then no digit chord matches).
///
/// The MAP is the single source of truth for "which chords HART-comp owns"; everything
/// not listed returns `None` and the app keeps the key (so e.g. an app's own Ctrl+C,
/// Super+Space IME toggle, etc. are untouched). Order: the Super+Shift digit case is
/// checked BEFORE the plain Super digit case (shift is the more specific match).
fn process_keyboard_shortcut(
    mods: ModifiersState,
    keysym: Keysym,
    digit_sym: Option<Keysym>,
) -> Option<WmAction> {
    // The digit-row workspace index (0-based) IFF the key is a top-row digit 1..9, read
    // from the layout-agnostic level-0 sym so Shift never knocks it out of range.
    let workspace_digit = digit_sym
        .map(|s| s.raw())
        .filter(|raw| (xkb::KEY_1..=xkb::KEY_9).contains(raw))
        .map(|raw| (raw - xkb::KEY_1) as usize);
    // ── Alt+Tab / Alt+Shift+Tab — focus cycle (Super must NOT be held). xkb emits
    //    ISO_Left_Tab for Shift+Tab, so accept either that or Tab+shift. ──
    if mods.alt && !mods.logo {
        if mods.shift && (keysym == Keysym::ISO_Left_Tab || keysym == Keysym::Tab) {
            return Some(WmAction::CycleFocusBack);
        }
        if keysym == Keysym::Tab {
            return Some(WmAction::CycleFocus);
        }
    }
    // ── Super-based window-management chords. ──
    if mods.logo {
        // Super+Shift+1..9 — move the focused window to workspace N (more specific
        // than the plain Super+digit below, so check it first).
        if mods.shift {
            if let Some(n) = workspace_digit {
                return Some(WmAction::MoveToWorkspace(n));
            }
        }
        // Super+1..9 — switch to workspace N (0-based).
        if !mods.shift {
            if let Some(n) = workspace_digit {
                return Some(WmAction::SwitchWorkspace(n));
            }
        }
        // Super+Q — close the focused toplevel.
        if keysym == Keysym::q {
            return Some(WmAction::CloseFocused);
        }
        // Super+arrows — snap / maximize / restore.
        if keysym == Keysym::Left {
            return Some(WmAction::SnapLeft);
        }
        if keysym == Keysym::Right {
            return Some(WmAction::SnapRight);
        }
        if keysym == Keysym::Up {
            return Some(WmAction::Maximize);
        }
        if keysym == Keysym::Down {
            return Some(WmAction::RestoreWindow);
        }
        // Super+D — toggle show-desktop.
        if keysym == Keysym::d {
            return Some(WmAction::ShowDesktop);
        }
    }
    None
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

    // M6 — register the zwlr_screencopy_v1 manager global so `grim` can capture
    // HART-comp DIRECTLY. Version 3 (the buffer_done/linux_dmabuf level grim uses);
    // the per-client bind filter defaults to "allow all" — the socket-owner boundary
    // (§6.5) already constrains who connects, and the killswitch gates the actual copy.
    let _screencopy_global = dh
        .create_global::<State, smithay::reexports::wayland_protocols_wlr::screencopy::v1::server::zwlr_screencopy_manager_v1::ZwlrScreencopyManagerV1, _>(3, ());

    // M6 — bake the default-arrow cursor once (a dependency-free RGBA fallback so a
    // cursor is visible on llvmpipe without an xcursor theme load). `MemoryRenderBuffer`
    // owns the bytes; the render loop imports it to a texture lazily on first draw.
    let (cur_rgba, cur_w, cur_h, cur_hotspot) = bake_default_cursor();
    let cursor_buffer = smithay::backend::renderer::element::memory::MemoryRenderBuffer::from_slice(
        &cur_rgba,
        smithay::backend::allocator::Fourcc::Argb8888,
        (cur_w, cur_h),
        1,
        Transform::Normal,
        None,
    );
    // M6 — the killswitch full-output black buffer (resized each frame it is drawn).
    let black_buffer = SolidColorBuffer::new((win_size.w, win_size.h), [0.0, 0.0, 0.0, 1.0]);

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
        ipc: crate::ipc::IpcState::default(),
        // M5: workspace 0 is active at boot; nothing hidden; desktop visible.
        active_workspace: 0,
        hidden_windows: Vec::new(),
        desktop_shown: true,
        suppressed_keys: Vec::new(),
        // M6: screencopy queue empty; capture allowed; default cursor; no effect in flight.
        pending_screencopy: Vec::new(),
        capture_blocked: false,
        cursor_status: CursorImageStatus::default_named(),
        cursor_buffer,
        cursor_hotspot: cur_hotspot,
        ws_switch_at: None,
        black_buffer,
    };

    // 6. (No calloop Generic source for the Display — see step 1. The Display is
    //    dispatched directly in the loop below, the safe-code minimal.rs pattern.)

    // 6b. M3 — spawn XWayland nested in OUR display, so X11 clients (Wine / xterm /
    //    xeyes) get an X server. On `Ready` we attach the X11 WM (start_wm), which
    //    routes X11 surface map/unmap through `XwmHandler`. `DISPLAY=:N` is published
    //    to the environment + logged so the harness can launch X11 children against it.
    spawn_xwayland(&dh, &event_loop.handle());

    // 6c. M4 — bind the com.hart.Compositor IPC server (Unix-socket twin) into the
    //    SAME calloop loop, so an agent (the brain's HartWmClient, or the M4 test
    //    client) arranges REAL windows via framed-JSON verbs against `state.space`.
    //    Best-effort: a bind failure leaves the compositor running for everything
    //    else (the IPC is an add-on, never a boot gate — same posture as XWayland).
    let _ipc_socket = crate::ipc::start_ipc(&event_loop.handle());

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

        // ── (b) Render the desktop into the winit framebuffer (+ M6: cursor, fades,
        //    killswitch black, then service screencopy against the painted frame). ──
        let size = backend.window_size();
        let damage = [smithay::utils::Rectangle::from_size(size)];
        if let Err(err) = render_frame(&mut state, &mut backend, &output, size, &damage) {
            warn!(?err, "render error");
        }
        // M6 — settle finished effects: clear the workspace-switch clock once the
        // crossfade completed so it doesn't re-evaluate forever. The loop renders every
        // iteration (calloop's 16ms timeout below paces it ~60fps), so an in-flight fade
        // PLAYS frame-by-frame without extra scheduling — `effects_animating` only gates
        // this tidy-up. (Map-in fades self-settle: `MapAnim::alpha` pins 1.0 past 150ms.)
        if !effects_animating(&state) {
            state.ws_switch_at = None;
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

// ════════════════════════════════════════════════════════════════════════════
// M6 — the render frame. Factored out of the loop so the screencopy read-back (which
// needs `&mut State` AFTER the immutable element-build) can run against the SAME bound
// framebuffer without a closure borrow tangle. Z-order, TOP→bottom (draw_render_elements
// paints slice-order = index 0 is top-most):
//   0. killswitch black surface (when capture_blocked) — ABOVE everything
//   1. software cursor (client surface or the baked arrow), hidden when blocked
//   2. window toplevels (xdg + X11), faded on map-in + the workspace-switch crossfade
//   3. layer surfaces (the glass-shell desktop) underneath
// then services queued screencopy frames against the just-painted output.
fn render_frame(
    state: &mut State,
    backend: &mut smithay::backend::winit::WinitGraphicsBackend<GlesRenderer>,
    output: &Output,
    size: Size<i32, Physical>,
    damage: &[Rectangle<i32, Physical>],
) -> Result<(), Box<dyn std::error::Error>> {
    use smithay::backend::renderer::element::Kind as ElemKind;

    let (renderer, mut framebuffer) = backend.bind()?;

    // The unified heterogeneous element list (windows + cursor + killswitch).
    let mut elements: Vec<HartRenderElement<GlesRenderer>> = Vec::new();

    // ── 0. KILLSWITCH (top): a full-output opaque black solid ABOVE all windows. ──
    if state.capture_blocked {
        state.black_buffer.update((size.w, size.h), [0.0, 0.0, 0.0, 1.0]);
        let solid = SolidColorRenderElement::from_buffer(
            &state.black_buffer,
            (0, 0),
            smithay::utils::Scale::from(1.0),
            1.0,
            ElemKind::Unspecified,
        );
        elements.push(HartRenderElement::Solid(solid));
    } else {
        // ── 1. SOFTWARE CURSOR (below the killswitch, above windows). Skipped while
        //    the killswitch is up (a black privacy screen shows no cursor). ──
        build_cursor_elements(state, renderer, &mut elements);
    }

    // DEBUG: once a second, dump each space element's loc/size/surface/buffer so a
    // non-painting window is diagnosable. Gated behind HART_COMP_DEBUG_RENDER.
    if std::env::var_os("HART_COMP_DEBUG_RENDER").is_some()
        && state.start_time.elapsed().as_millis() % 1000 < 20
    {
        for window in state.space.elements() {
            let loc = state.space.element_location(window).unwrap_or_default();
            let bbox = state.space.element_bbox(window);
            let surf = window.wl_surface();
            let has_buf = surf.as_ref().map(|s| surface_has_buffer(s)).unwrap_or(false);
            let is_x11 = window.x11_surface().is_some();
            info!(?loc, ?bbox, has_surface = surf.is_some(), has_buffer = has_buf, is_x11, "render.element");
        }
    }

    // ── 2. WINDOW TOPLEVELS (above the layers, below the cursor), top-most first.
    //    Each window's alpha = its map-in fade × the workspace-switch crossfade, so a
    //    just-mapped window fades 0→1 and a fresh workspace fades the whole set in.
    //    The alpha is the LAST arg of `render_elements` (the existing M3 call already
    //    passed 1.0 there — M6 just makes it dynamic). ──
    let ws_alpha = workspace_fade_alpha(state);
    for window in state.space.elements().rev() {
        let loc = state.space.element_location(window).unwrap_or_default();
        let phys = loc.to_physical_precise_round(1.0);
        let map_alpha = window
            .user_data()
            .get::<MapAnim>()
            .map(|a| a.alpha())
            .unwrap_or(1.0);
        let alpha = (map_alpha * ws_alpha).clamp(0.0, 1.0);
        // M6 fade PROOF: when an effect is mid-flight this logs the actual sub-1.0 alpha
        // being handed to the renderer, so the fade is observable in the journal even
        // when the nested WSL EGL surface freezes before a mid-fade frame can be grabbed
        // (the alpha math runs regardless of whether `submit` to the host succeeds).
        if alpha < 0.999 && std::env::var_os("HART_COMP_DEBUG_FADE").is_some() {
            let handle = window
                .user_data()
                .get::<WindowHandle>()
                .map(|h| h.as_str().to_string())
                .unwrap_or_default();
            info!(handle = %handle, map_alpha, ws_alpha, alpha, "effect.fade (sub-1.0 alpha → renderer)");
        }
        let win_elems: Vec<WaylandSurfaceRenderElement<GlesRenderer>> =
            AsRenderElements::<GlesRenderer>::render_elements(
                window,
                renderer,
                phys,
                smithay::utils::Scale::from(1.0),
                alpha,
            );
        elements.extend(win_elems.into_iter().map(HartRenderElement::Surface));
    }

    // ── 3. LAYER surfaces (below the toplevels in this list = drawn under them). ──
    let mut layers_painted = 0usize;
    {
        let map = smithay::desktop::layer_map_for_output(output);
        for layer in map.layers() {
            layers_painted += 1;
            let loc = map.layer_geometry(layer).map(|g| g.loc).unwrap_or_default();
            let layer_elems: Vec<WaylandSurfaceRenderElement<GlesRenderer>> =
                render_elements_from_surface_tree(
                    renderer,
                    layer.wl_surface(),
                    (loc.x, loc.y),
                    1.0,
                    ws_alpha,
                    Kind::Unspecified,
                );
            elements.extend(layer_elems.into_iter().map(HartRenderElement::Surface));
        }
    }
    // Positive render proof, logged ONLY when the painted-layer count CHANGES.
    {
        let prev = LAYERS_PAINTED.swap(layers_painted, std::sync::atomic::Ordering::Relaxed);
        if prev != layers_painted {
            info!(layers_painted, "layer.composited (wlr-layer surfaces now in the rendered frame)");
        }
    }

    // Paint the frame.
    {
        let mut frame = renderer.render(&mut framebuffer, size, Transform::Flipped180)?;
        let clear = Color32F::new(
            HART_SPLASH_RGBA[0],
            HART_SPLASH_RGBA[1],
            HART_SPLASH_RGBA[2],
            HART_SPLASH_RGBA[3],
        );
        frame.clear(clear, damage)?;
        draw_render_elements(&mut frame, 1.0, &elements, damage)?;
        let _sync = frame.finish()?;
    }
    // `elements` borrows `renderer` (the surface textures) — drop it before the
    // screencopy read-back re-borrows `renderer` mutably.
    drop(elements);

    // ── M6 SCREENCOPY: service queued frames against the JUST-PAINTED framebuffer. The
    //    read-back captures EXACTLY what was painted (cursor + fades + killswitch), the
    //    whole reason to capture HART-comp's own output rather than the host recomposite. ──
    crate::screencopy::service_pending_screencopy(
        state,
        renderer,
        &framebuffer,
        size,
        Transform::Flipped180,
    );
    Ok(())
}

/// M6 — build the software-cursor render element(s) at the pointer location, PREPENDED
/// so the cursor draws on top of windows. Three cases, mirroring anvil's cursor draw:
///   • `CursorImageStatus::Surface(s)` — the client set a cursor surface; render its
///     surface tree at the pointer minus the client's hotspot.
///   • `CursorImageStatus::Named(_)` / default — draw the baked default arrow (a
///     cached `MemoryRenderBuffer`), tip at the pointer (minus the arrow's hotspot).
///   • `CursorImageStatus::Hidden` — the client hid the cursor; draw nothing.
/// Position is physical (scale 1.0 here, matching the window/layer element math).
fn build_cursor_elements(
    state: &State,
    renderer: &mut GlesRenderer,
    elements: &mut Vec<HartRenderElement<GlesRenderer>>,
) {
    use smithay::backend::renderer::element::Kind as ElemKind;
    let pos = state.pointer.current_location();
    match &state.cursor_status {
        CursorImageStatus::Hidden => {}
        CursorImageStatus::Surface(surface) => {
            // The client's hotspot is stored in the surface's CursorImageSurfaceData.
            let hotspot = with_states(surface, |states| {
                states
                    .data_map
                    .get::<smithay::input::pointer::CursorImageSurfaceData>()
                    .and_then(|d| {
                        let attrs = d.lock().unwrap();
                        Some(attrs.hotspot)
                    })
                    .unwrap_or_default()
            });
            // i32-physical location for the surface-tree helper.
            let cpos = (pos - hotspot.to_f64()).to_physical_precise_round(1.0);
            let surf_elems: Vec<WaylandSurfaceRenderElement<GlesRenderer>> =
                render_elements_from_surface_tree(renderer, surface, cpos, 1.0, 1.0, ElemKind::Cursor);
            // Cursor on top → insert at the FRONT (slice index 0 is top-most).
            for (i, e) in surf_elems.into_iter().enumerate() {
                elements.insert(i, HartRenderElement::Surface(e));
            }
        }
        // Named / default → the baked arrow.
        _ => {
            // f64-physical location for the memory-buffer element ctor.
            let cpos: Point<f64, Physical> =
                (pos - state.cursor_hotspot.to_f64()).to_physical(1.0);
            match MemoryRenderBufferRenderElement::from_buffer(
                renderer,
                cpos,
                &state.cursor_buffer,
                None,
                None,
                None,
                ElemKind::Cursor,
            ) {
                Ok(e) => elements.insert(0, HartRenderElement::Memory(e)),
                Err(err) => warn!(?err, "cursor: failed to build the default-arrow element"),
            }
        }
    }
}

/// M6 — the workspace-switch crossfade factor NOW: 0→1 over `WS_FADE_MS` after a
/// switch, then a steady 1.0. Multiplies every visible surface's alpha so the whole
/// new workspace fades in. Returns 1.0 (no fade) when no switch is in flight.
fn workspace_fade_alpha(state: &State) -> f32 {
    match state.ws_switch_at {
        None => 1.0,
        Some(t) => {
            let e = t.elapsed().as_millis();
            if e >= WS_FADE_MS {
                1.0
            } else {
                (e as f32 / WS_FADE_MS as f32).clamp(0.0, 1.0)
            }
        }
    }
}

/// M6 — is any effect still animating (a window mid-fade, or a workspace crossfade in
/// flight)? The loop forces a redraw next iteration while true, so a fade actually
/// PLAYS rather than freezing on the first frame (clients only damage on content
/// change; an alpha ramp is compositor-side, so we must self-schedule the frames).
fn effects_animating(state: &State) -> bool {
    if let Some(t) = state.ws_switch_at {
        if t.elapsed().as_millis() < WS_FADE_MS {
            return true;
        }
    }
    state
        .space
        .elements()
        .any(|w| w.user_data().get::<MapAnim>().map(|a| a.animating()).unwrap_or(false))
}

/// Bake a small default arrow cursor as RGBA bytes — a dependency-free fallback so a
/// visible cursor renders on llvmpipe with no xcursor theme load. A classic left-
/// pointing arrow: a white fill with a 1px black outline, drawn into a 24×24 buffer by
/// a compact edge-function rasterizer. Returns (rgba, width, height, hotspot). The
/// hotspot is the arrow TIP (top-left), so the cursor points where the user clicks.
fn bake_default_cursor() -> (Vec<u8>, i32, i32, Point<i32, Logical>) {
    const W: i32 = 24;
    const H: i32 = 24;
    // The arrow polygon (classic pointer), in buffer pixels. Tip at (0,0).
    // Points trace the outline clockwise: tip → down-left edge → notch → tail.
    let poly: [(f32, f32); 7] = [
        (0.0, 0.0),
        (0.0, 17.0),
        (4.0, 13.0),
        (7.0, 19.0),
        (10.0, 18.0),
        (7.0, 12.0),
        (12.0, 12.0),
    ];
    // Point-in-polygon (even-odd) for the fill test.
    let inside = |px: f32, py: f32| -> bool {
        let mut c = false;
        let n = poly.len();
        let mut j = n - 1;
        for i in 0..n {
            let (xi, yi) = poly[i];
            let (xj, yj) = poly[j];
            if ((yi > py) != (yj > py)) && (px < (xj - xi) * (py - yi) / (yj - yi) + xi) {
                c = !c;
            }
            j = i;
        }
        c
    };
    let mut rgba = vec![0u8; (W * H * 4) as usize];
    for y in 0..H {
        for x in 0..W {
            let cx = x as f32 + 0.5;
            let cy = y as f32 + 0.5;
            let fill = inside(cx, cy);
            // Outline: a pixel that is empty but adjacent to a filled pixel (1px border).
            let mut outline = false;
            if !fill {
                'scan: for dy in -1..=1 {
                    for dx in -1..=1 {
                        let nx = cx + dx as f32;
                        let ny = cy + dy as f32;
                        if inside(nx, ny) {
                            outline = true;
                            break 'scan;
                        }
                    }
                }
            }
            let idx = ((y * W + x) * 4) as usize;
            if fill {
                // White, opaque (premultiplied; alpha 255 so RGB unchanged).
                rgba[idx] = 255;
                rgba[idx + 1] = 255;
                rgba[idx + 2] = 255;
                rgba[idx + 3] = 255;
            } else if outline {
                // Black outline, opaque.
                rgba[idx] = 0;
                rgba[idx + 1] = 0;
                rgba[idx + 2] = 0;
                rgba[idx + 3] = 255;
            }
            // else: transparent (already zeroed).
        }
    }
    (rgba, W, H, Point::from((0, 0)))
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

// ── M4 — uniform app_id/title readers for the IPC `window.list` serializer
// (src/ipc.rs). A `Window` is either an xdg toplevel or an X11 surface; these pick
// the right accessor so `window.list` reports provenance for BOTH kinds from the one
// `space.elements()` source of truth. Reused by the event-frame builders below.
/// app_id for any mapped `Window` (xdg `app_id` or X11 WM_CLASS).
pub(crate) fn ipc_window_app_id(window: &Window) -> Option<String> {
    if let Some(toplevel) = window.toplevel() {
        return toplevel_app_id(toplevel);
    }
    window.x11_surface().and_then(x11_app_id)
}

/// title for any mapped `Window` (xdg `title` or X11 window title).
pub(crate) fn ipc_window_title(window: &Window) -> Option<String> {
    if let Some(toplevel) = window.toplevel() {
        return toplevel_title(toplevel);
    }
    window.x11_surface().and_then(x11_title)
}

/// Build the IPC event-frame `window` payload (IPC_PROTOCOL.md §5) for one mapped
/// `Window`. Same shape as a `window.list` row, from the same source of truth.
fn ipc_event_window_json(state: &State, window: &Window, handle: &str) -> serde_json::Value {
    let geo = state.space.element_geometry(window);
    let (x, y, w, h) = geo
        .map(|g| (g.loc.x, g.loc.y, g.size.w, g.size.h))
        .unwrap_or((0, 0, 0, 0));
    let is_x11 = window.x11_surface().is_some();
    serde_json::json!({
        "handle": handle,
        "app_id": ipc_window_app_id(window),
        "title": ipc_window_title(window),
        "geometry": { "x": x, "y": y, "w": w, "h": h },
        "kind": if is_x11 { "x11" } else { "xdg" },
    })
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

// ════════════════════════════════════════════════════════════════════════════
// M5 — behavioural unit floor for the keyboard-shortcut MAP.
//
// `process_keyboard_shortcut` is a PURE function (ModifiersState, modified Keysym,
// level-0 digit Keysym) -> the resolved WmAction, so the full chord→action contract is
// unit-testable without a live compositor — which matters because the
// WSL-nested-in-headless-sway harness CANNOT inject a real keystroke into the winit
// backend: wtype rewrites its virtual keymap and presses evdev keycode 1 for every
// key, and neither the nested winit backend nor Smithay's seat honour that per-press
// keymap, so every injected key collapses to keycode 9 = Escape (proven live via
// WAYLAND_DEBUG + HART_COMP_DEBUG_KEYS). On real evdev hardware (the DRM/libinput
// `wayland.rs` backend) keycodes arrive straight from the kernel and resolve correctly,
// so the chord LOGIC is exercised here at the unit level and the EXECUTORS are exercised
// live via the IPC verbs that call the SAME switch/move/place/close bodies.
//
// These assert the MAP: every documented chord resolves to its action, the modifier
// discrimination is exact (Super+Shift+N ≠ Super+N), the digit row survives Shift
// (the `digit_sym` fix), and non-chord keys are forwarded (None) so apps keep their
// own keystrokes. Mirrors anvil's `process_keyboard_shortcut` table.
// ════════════════════════════════════════════════════════════════════════════
#[cfg(test)]
mod m5_keybinding_tests {
    use super::{WmAction, process_keyboard_shortcut};
    use smithay::input::keyboard::{Keysym, ModifiersState};

    fn mods(logo: bool, alt: bool, shift: bool) -> ModifiersState {
        ModifiersState {
            logo,
            alt,
            shift,
            ..Default::default()
        }
    }

    /// A non-digit chord: the live closure passes the modified sym as BOTH the letter/
    /// arrow sym and (harmlessly) the level-0 sym — a letter/arrow's level-0 sym is never
    /// in `KEY_1..=KEY_9`, so it can never spuriously match a digit chord. Model that by
    /// passing the same sym for `digit_sym`.
    fn chord(m: ModifiersState, keysym: Keysym) -> Option<WmAction> {
        process_keyboard_shortcut(m, keysym, Some(keysym))
    }

    /// A DIGIT chord, modelling the real seat: `modified` is what `modified_sym()`
    /// returns (Shift maps US `1`→`!`, `3`→`#`, …) and `level0` is the layout-agnostic
    /// `raw_latin_sym_or_raw_current_sym()` (the bare digit, Shift-independent).
    fn digit_chord(m: ModifiersState, modified: Keysym, level0: Keysym) -> Option<WmAction> {
        process_keyboard_shortcut(m, modified, Some(level0))
    }

    #[test]
    fn alt_tab_cycles_focus_forward() {
        // Alt+Tab (no Super) → CycleFocus. Super must NOT be held.
        assert_eq!(chord(mods(false, true, false), Keysym::Tab), Some(WmAction::CycleFocus));
    }

    #[test]
    fn alt_shift_tab_cycles_focus_back() {
        // xkb emits ISO_Left_Tab for Shift+Tab; accept that OR Tab+shift.
        assert_eq!(
            chord(mods(false, true, true), Keysym::ISO_Left_Tab),
            Some(WmAction::CycleFocusBack)
        );
        assert_eq!(chord(mods(false, true, true), Keysym::Tab), Some(WmAction::CycleFocusBack));
    }

    #[test]
    fn super_digits_switch_workspaces_zero_based() {
        // Super+1 → workspace 0, Super+9 → workspace 8 (anvil's `raw - KEY_1`). No Shift,
        // so the modified sym IS the bare digit.
        assert_eq!(
            digit_chord(mods(true, false, false), Keysym::_1, Keysym::_1),
            Some(WmAction::SwitchWorkspace(0))
        );
        assert_eq!(
            digit_chord(mods(true, false, false), Keysym::_2, Keysym::_2),
            Some(WmAction::SwitchWorkspace(1))
        );
        assert_eq!(
            digit_chord(mods(true, false, false), Keysym::_9, Keysym::_9),
            Some(WmAction::SwitchWorkspace(8))
        );
    }

    #[test]
    fn super_shift_digits_move_to_workspace() {
        // Super+Shift+N → MoveToWorkspace(N-1). Shift is the MORE specific match and
        // must win over the plain Super+N switch.
        assert_eq!(
            digit_chord(mods(true, false, true), Keysym::_3, Keysym::_3),
            Some(WmAction::MoveToWorkspace(2))
        );
        // Same digit WITHOUT shift is a switch, not a move — the discrimination is exact.
        assert_eq!(
            digit_chord(mods(true, false, false), Keysym::_3, Keysym::_3),
            Some(WmAction::SwitchWorkspace(2))
        );
    }

    #[test]
    fn super_shift_digit_resolves_when_modified_sym_is_shifted() {
        // REGRESSION GUARD for the real defect: on a US keymap, Shift maps the digit row
        // to `!@#$%^&*(`, so `modified_sym()` for the Super+Shift+3 chord is `numbersign`
        // (#), NOT `3`. Matching the MODIFIED sym against `KEY_1..=KEY_9` therefore FAILED
        // (numbersign is out of range) → MoveToWorkspace never fired from the keyboard.
        // The fix reads the LEVEL-0 sym (the bare `3`) for the digit range. Prove that the
        // chord still resolves even though the modified sym is the shifted symbol.
        assert_eq!(
            digit_chord(mods(true, false, true), Keysym::numbersign, Keysym::_3),
            Some(WmAction::MoveToWorkspace(2))
        );
        assert_eq!(
            digit_chord(mods(true, false, true), Keysym::exclam, Keysym::_1),
            Some(WmAction::MoveToWorkspace(0))
        );
        assert_eq!(
            digit_chord(mods(true, false, true), Keysym::parenleft, Keysym::_9),
            Some(WmAction::MoveToWorkspace(8))
        );
    }

    #[test]
    fn super_q_closes_focused() {
        assert_eq!(chord(mods(true, false, false), Keysym::q), Some(WmAction::CloseFocused));
    }

    #[test]
    fn super_arrows_snap_and_restore() {
        assert_eq!(chord(mods(true, false, false), Keysym::Left), Some(WmAction::SnapLeft));
        assert_eq!(chord(mods(true, false, false), Keysym::Right), Some(WmAction::SnapRight));
        assert_eq!(chord(mods(true, false, false), Keysym::Up), Some(WmAction::Maximize));
        assert_eq!(chord(mods(true, false, false), Keysym::Down), Some(WmAction::RestoreWindow));
    }

    #[test]
    fn super_d_toggles_show_desktop() {
        assert_eq!(chord(mods(true, false, false), Keysym::d), Some(WmAction::ShowDesktop));
    }

    #[test]
    fn non_chord_keys_are_forwarded_to_the_app() {
        // A plain letter (no modifier) is NOT a WM chord → None → the app keeps it.
        assert_eq!(chord(mods(false, false, false), Keysym::a), None);
        // Ctrl+C (no logo/alt) is the app's, not ours.
        assert_eq!(chord(mods(false, false, false), Keysym::c), None);
        // A bare arrow (no Super) is the app's (cursor movement), not a snap.
        assert_eq!(chord(mods(false, false, false), Keysym::Left), None);
        // Super+letter we don't bind (e.g. Super+Z) is forwarded.
        assert_eq!(chord(mods(true, false, false), Keysym::z), None);
        // A bare digit (no Super) is the app's — even though its level-0 sym IS a digit,
        // without Super neither the switch nor the move chord fires.
        assert_eq!(digit_chord(mods(false, false, false), Keysym::_1, Keysym::_1), None);
    }
}

// ════════════════════════════════════════════════════════════════════════════
// M6 — behavioural unit floor for the PURE effect helpers (the parts that don't need
// a live GlesRenderer/Display). The cursor BAKE + the two fade clocks are pure
// functions, so the contract (a visible arrow with fill+outline; alpha ramps 0→1 then
// pins) is unit-testable without the WSL-nested-EGL harness — which matters because the
// nested winit EGL surface goes ContextLost ~3s in (so the LIVE black-surface/crossfade
// proofs are flaky), but the LOGIC that produces them is deterministic and asserted
// here. The screencopy WIRE path (manager bind → capture_output → copy → ready/failed)
// is exercised live by `grim` against $HART_SOCK (it captures hart-comp directly), and
// the kill-switch REFUSAL is proven by the "capture blocked … failing the frame" log +
// grim getting a failed (unreadable) frame.
#[cfg(test)]
mod m6_effect_tests {
    use super::{FADE_IN_MS, MapAnim, WS_FADE_MS, bake_default_cursor};
    use std::time::{Duration, Instant};

    #[test]
    fn default_cursor_bakes_a_visible_arrow_with_fill_and_outline() {
        // The fallback cursor MUST be a real, visible arrow: a non-trivial RGBA buffer
        // with BOTH white-fill pixels (the arrow body) and black-outline pixels (the
        // 1px border), and a fully-transparent background — so it shows on llvmpipe with
        // no xcursor theme. Hotspot is the tip at (0,0).
        let (rgba, w, h, hot) = bake_default_cursor();
        assert_eq!(w, 24);
        assert_eq!(h, 24);
        assert_eq!(rgba.len(), (w * h * 4) as usize);
        assert_eq!((hot.x, hot.y), (0, 0), "hotspot must be the arrow tip");

        let (mut white, mut black, mut transparent) = (0u32, 0u32, 0u32);
        for px in rgba.chunks_exact(4) {
            match (px[0], px[1], px[2], px[3]) {
                (255, 255, 255, 255) => white += 1,
                (0, 0, 0, 255) => black += 1,
                (_, _, _, 0) => transparent += 1,
                _ => {}
            }
        }
        assert!(white > 40, "arrow body should have a meaningful white fill (got {white})");
        assert!(black > 10, "arrow should have a black outline (got {black})");
        assert!(transparent > 100, "most of the 24x24 buffer is transparent (got {transparent})");
        // The opaque arrow (fill+outline) must be a MINORITY of the buffer (it is a small
        // pointer, not a filled square) — guards against an all-white regression.
        assert!(white + black < (w * h) as u32 / 2, "arrow must not fill the whole buffer");
    }

    #[test]
    fn map_fade_alpha_ramps_then_pins_at_one() {
        // A freshly-mapped window starts near-transparent and ramps to fully opaque over
        // FADE_IN_MS, then pins at exactly 1.0 (so the steady state hits the opaque
        // fast-path). We can't pause time, but a JUST-created clock is < FADE_IN_MS old,
        // so its alpha is < 1.0; a clock backdated past FADE_IN_MS reads exactly 1.0.
        let fresh = MapAnim(Instant::now());
        let a = fresh.alpha();
        assert!((0.0..=1.0).contains(&a), "alpha in [0,1], got {a}");
        assert!(fresh.animating(), "a fresh map is still animating");

        // Backdate the clock to well past the fade → settled at 1.0, not animating.
        let settled = MapAnim(Instant::now() - Duration::from_millis(FADE_IN_MS as u64 + 50));
        assert_eq!(settled.alpha(), 1.0, "past FADE_IN_MS the alpha pins at 1.0");
        assert!(!settled.animating(), "a settled window no longer animates");
    }

    #[test]
    fn workspace_fade_constant_is_short_and_positive() {
        // The crossfade must be perceptible-but-snappy. Guards against a 0ms (no fade) or
        // a multi-second (sluggish) regression of the tuning constants.
        assert!(WS_FADE_MS > 0 && WS_FADE_MS <= 500, "ws fade should be a short ramp");
        assert!(FADE_IN_MS > 0 && FADE_IN_MS <= 500, "map fade should be a short ramp");
    }
}
