// â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•
// HART-comp â€” Milestone 1: a REAL running compositor, winit backend (WSL/WSLg).
// â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•
//
// This is the FIRST actually-running HART-comp: not the skeleton in main.rs, not
// the DRM CI-draft in wayland.rs, but a compositor that boots, nests in an
// existing Wayland host (WSLg's wayland-0) as a winit/wayland-client window,
// creates its OWN wayland-N socket, and PAINTS real client surfaces with the
// GlesRenderer. No DRM/KMS â€” runnable on any box with a Wayland host + EGL.
//
// â”€â”€ Why a SEPARATE module + a winit-flavoured State (NOT a parallel path) â”€â”€
//   â€¢ `wayland.rs` is the DRM/real-hardware backend: it holds a `PixmanRenderer`
//     and a huge XWayland/foreign-toplevel/decoration handler set drafted against
//     a LATER Smithay API. It is gated behind the `smithay` (DRM) cargo feature
//     and is CI-only; it does not compile on this rev as-is.
//   â€¢ This file is gated behind the DISTINCT `winit` cargo feature. winit's
//     renderer MUST be `GlesRenderer` (winit::init::<GlesRenderer>() requires
//     renderer_gl + backend_egl; pixman is not a winit renderer), so the State's
//     renderer type necessarily differs from the DRM path â€” the two backends own
//     two backend-shaped States by construction. That is the design the M1 plan
//     mandates ("factor State construction so the renderer type is the backend's"),
//     not a second copy of one canonical type.
//   â€¢ The NO-PHANTOM-WINDOW bookkeeping is NOT duplicated: this State embeds the
//     SAME pure `WindowRegistry` / `SummonResolver` / `ToplevelKind` from main.rs,
//     so a handle is still minted ONLY on a real map, here too.
//
// â”€â”€ Modelled 1:1 on the pinned Smithay rev (47843391â€¦) â”€â”€
//   examples/minimal.rs (the canonical minimal winit server compositor for THIS
//   rev) + anvil/src/winit.rs. NOTE the rev migrated to the unified
//   `delegate_dispatch2!(State)` macro: the per-protocol `delegate_compositor!`/
//   `delegate_shm!`/â€¦ macros named in older guides DO NOT EXIST on this rev â€” one
//   `delegate_dispatch2!` generates every Dispatch/GlobalDispatch impl from the
//   Handler traits we impl below. Verified against the checked-out source.

#![cfg(feature = "winit")]

use std::sync::Arc;
use std::time::{Duration, Instant};

use smithay::{
    backend::{
        renderer::{
            Color32F, Frame, Renderer,
            // M6: the killswitch black surface buffer is constructed + held here; the
            // baked-cursor MemoryRenderBuffer too. The render-element BUILD (the
            // heterogeneous `HartRenderElement<R>` list) lives in `comp_core` now.
            element::solid::SolidColorBuffer,
            gles::GlesRenderer,
            utils::on_commit_buffer_handler,
        },
        winit::{self, WinitEvent},
    },
    desktop::{Space, Window, WindowSurfaceType},
    input::{
        Seat, SeatHandler, SeatState,
        keyboard::{Keycode, KeyboardHandle},
        pointer::{CursorImageStatus, PointerHandle},
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
use smithay::desktop::LayerSurface;
// `Window::wl_surface()` is provided by the `WaylandFocus` trait on this rev (not
// an inherent method) â€” it MUST be in scope for the handler bodies that call it.
use smithay::wayland::seat::WaylandFocus;
// xdg-decoration mode enum (server-side vs client-side) â€” the SSD negotiation in
// `XdgDecorationHandler` below.
use smithay::reexports::wayland_protocols::xdg::decoration::zv1::server::zxdg_toplevel_decoration_v1::Mode as DecorationMode;
// â”€â”€ XWayland (Wine / legacy X11): the headline M3 feature. These types only exist
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
// Backend-AGNOSTIC surface-tree / map-edge / app-id readers (M7 Stage-B hoist) â€”
// SHARED with the DRM/udev backend so there is ONE implementation, not a parallel path.
use crate::shared::{
    send_frame_callbacks, surface_has_buffer, toplevel_app_id, toplevel_title, x11_app_id,
    x11_title,
};

// â”€â”€ M5/M6 WM + effects types, the unified `HartRenderElement<R>`, the chord map, the
// cursor bake, the fade clocks, and the whole WM/IPC/input/workspace/cursor/killswitch
// brain are HOISTED to `comp_core` (M8 Stage-B) and SHARED with the DRM backend â€” ONE
// implementation, not a parallel path. winit's `State` impls `comp_core::CompState`
// (below) to feed that brain; the handlers in this file call the `comp_core::*` fns. â”€â”€
use crate::comp_core::{self, CompState, HartRenderElement, HiddenWindow, MapAnim, X11Focused};

// â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
// Per-client state. Carries the compositor-side client bookkeeping Smithay needs.
// â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
#[derive(Default)]
pub struct ClientState {
    pub compositor_state: CompositorClientState,
}
impl ClientData for ClientState {
    fn initialized(&self, _client_id: ClientId) {}
    fn disconnected(&self, _client_id: ClientId, _reason: DisconnectReason) {}
}

// â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
// The live compositor State (winit backend). `windows` is the SAME pure
// no-phantom-window registry from main.rs; everything else is Smithay protocol
// state + the winit GlesRenderer-driven desktop.
// â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
pub struct State {
    pub dh: DisplayHandle,
    pub loop_handle: LoopHandle<'static, State>,
    pub running: bool,
    pub start_time: Instant,

    /// The desktop window tree (xdg toplevels mapped above the layer-shell desktop).
    pub space: Space<Window>,
    /// The manifestâ†”toplevel map (no-phantom-window source of truth, from main.rs).
    pub windows: WindowRegistry,

    // â”€â”€ Smithay protocol globals (the S4 minimal set a shm/xdg client needs) â”€â”€
    pub compositor_state: CompositorState,
    pub xdg_shell_state: XdgShellState,
    pub shm_state: ShmState,
    /// wl_output / xdg-output manager. RAII-HELD: the per-output globals are created
    /// via `Output::create_global`; `delegate_output!` needs no accessor returning
    /// this manager, so the field is never read after `new` â€” held only to keep the
    /// `xdg_output_manager` global advertised (same as wayland.rs::State).
    #[allow(dead_code)]
    pub output_manager_state: OutputManagerState,
    pub layer_shell_state: WlrLayerShellState,
    pub data_device_state: DataDeviceState,
    pub seat_state: SeatState<State>,
    /// The live `wl_seat`. RAII-HELD: input/focus route through the cached
    /// `keyboard`/`pointer` handles below (extracted from this seat), so the `seat`
    /// itself is never read again â€” held only to keep the `wl_seat` global alive
    /// (same as wayland.rs::State).
    #[allow(dead_code)]
    pub seat: Seat<State>,

    // â”€â”€ M3: input handles (cached so the loop can route winit input + read the
    // cursor position for click-to-focus). `pointer.current_location()` is the
    // hit-test origin; both are cheap clones of the seat's handles.
    pub pointer: PointerHandle<State>,
    pub keyboard: KeyboardHandle<State>,

    // â”€â”€ M3: xdg-decoration â€” negotiate server-side decorations (the compositor owns
    // the chrome). A plain field constructed with `XdgDecorationState::new::<State>`.
    // RAII-HELD: `XdgDecorationHandler` has no `xdg_decoration_state()` accessor on
    // this rev, so the field is never read after `new` â€” held only to keep the
    // `zxdg_decoration_manager_v1` global advertised (same as wayland.rs::State).
    #[allow(dead_code)]
    pub xdg_decoration_state: XdgDecorationState,

    // â”€â”€ M3: XWayland (Wine / legacy X11). `xwayland_shell_state` is the X11â†”wl_surface
    // association protocol (a `start_wm` bound); `xwm` is the live X11 window manager,
    // `None` until `XWaylandEvent::Ready` attaches it.
    pub xwayland_shell_state: XWaylandShellState,
    pub xwm: Option<X11Wm>,

    // â”€â”€ M3: cascade placement cursor â€” each newly-mapped toplevel is offset from the
    // last so multiple windows don't fully overlap (the "MULTIPLE WINDOWS" gate).
    pub next_window_loc: Point<i32, Logical>,

    /// The single winit output (the HART-comp window inside WSLg).
    pub output: Output,

    // â”€â”€ M4: the com.hart.Compositor IPC server's per-compositor state (the
    // `events.subscribe` sinks). The IPC command handlers (src/ipc.rs) mutate the
    // fields ABOVE (space/seat/xwm) against a verb; this holds only the event
    // fan-out subscribers so the map/unmap/focus edges below can push event frames.
    pub ipc: crate::ipc::IpcState,

    // â”€â”€ M5: WORKSPACES. The visible `space` above holds ONLY the active workspace's
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
    /// press's â€” anvil's keysym-keyed set would then MISS the release and leak a dangling
    /// key-up to the focused client). Keycode-keyed suppression closes that leak.
    pub suppressed_keys: Vec<Keycode>,

    // â”€â”€ M6 â€” SCREENCOPY (zwlr_screencopy_v1). `pending_screencopy` holds `copy`-
    // requested frames awaiting the next paint (the read-back needs the live
    // GlesRenderer + framebuffer, which only exist inside the render closure). The
    // render loop drains it via `screencopy::service_pending_screencopy`. See
    // src/screencopy.rs. â”€â”€
    pub pending_screencopy: Vec<crate::screencopy::PendingScreencopy>,

    // â”€â”€ M6 â€” SCREEN KILL-SWITCH (constitutional). When the human cuts `screen`, the
    // brain pushes `screen.kill {on:true}` over the EXISTING IPC, setting this flag.
    // While set: (a) the render loop draws a full-output OPAQUE BLACK element ABOVE
    // everything, and (b) input is NOT forwarded to clients, and (c) every screencopy
    // `copy` immediately `failed()`s. One bool drives all three â€” the no-capture +
    // privacy + control kill, enforced at the compositor with zero per-frame IPC. â”€â”€
    pub capture_blocked: bool,

    // â”€â”€ M6 â€” SOFTWARE CURSOR. `cursor_status` is the latest client-requested cursor
    // image (a surface the client set, Hidden, or the default named arrow). The render
    // loop draws it ABOVE all windows/layers (but BELOW the killswitch). `cursor_buffer`
    // caches the baked default-arrow `MemoryRenderBuffer` (built once at boot) so the
    // common "no client cursor" case costs one cached element + a reposition, no
    // per-frame allocation. â”€â”€
    pub cursor_status: CursorImageStatus,
    pub cursor_buffer: smithay::backend::renderer::element::memory::MemoryRenderBuffer,
    /// The baked arrow's hotspot (the click point, relative to the image top-left), so
    /// the cursor is positioned so its tip â€” not its top-left â€” sits at the pointer.
    pub cursor_hotspot: Point<i32, Logical>,

    // â”€â”€ M6 â€” EFFECTS clocks. `ws_switch_at` is set when a workspace switch happens so
    // the render loop crossfades the newly-shown set in over `WS_FADE_MS`. `None` once
    // settled. Per-window map fade lives in each window's `MapAnim` user-data. â”€â”€
    pub ws_switch_at: Option<Instant>,

    // â”€â”€ M6 â€” the killswitch black surface buffer (full-output opaque black). Cached +
    // resized on output change so the kill draw is one cheap solid element. â”€â”€
    pub black_buffer: SolidColorBuffer,
    /// NATIVE SHELL M1 — the composed aura backdrop, cached across frames so the
    /// per-pixel compose runs once per (size, theme) rather than every frame.
    pub bloom: crate::comp_core::BloomCache,
    /// NATIVE SHELL M2 — the voice orb, composed once and animated per frame by
    /// scale+alpha on the GPU.
    pub orb: crate::comp_core::OrbCache,
    /// NATIVE SHELL M3 text: cosmic-text rasterizer. The dev build carries it too
    /// (the trait method is required); native_shell_on is off by default here.
    pub text_rasterizer: crate::text_render::TextRasterizer,
    /// NATIVE SHELL M3 — rounded-rect buffer cache (cards, omnibox).
    pub rect_cache: crate::comp_core::RectCache,
    /// NATIVE SHELL: the RETAINED scene tree. Rebuilt only when the output size, the
    /// composed home payload or the theme changes, so a steady desktop stops rebuilding
    /// its layout every frame (the zero-per-frame-alloc NFR).
    pub scene_cache: crate::scene::SceneCache,
    /// NATIVE SHELL M2 press half: pointer buttons currently held on the seat, kept
    /// current by the shared `on_pointer_button`. The native orb reads it via
    /// `pointer_pressed` to react to a click held over it.
    pub pointer_buttons_down: u32,
}

impl State {
    /// The single mint site for THIS backend (mirrors wayland.rs::on_real_map):
    /// a handle is minted ONLY on a real map, here too. M1 does not yet thread a
    /// SummonApp manifest in (no IPC launcher wired on the winit path), so every
    /// map is an externally-opened window (`manifest_id = None`) â€” but the honesty
    /// invariant (handle â‡’ a toplevel mapped) holds identically.
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

// â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•
// M8 â€” winit::State feeds the SHARED WM brain by impl'ing `comp_core::CompState`.
// Every accessor hands back a field this State already holds; the brain
// (comp_core::*) drives them. The seat handles are `â€¦Handle<State>` (State impls
// SeatHandler below), so the supertrait bound is satisfied â€” that is what lets
// `keyboard.set_focus(state, â€¦)` type-check inside the shared generic code.
// â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•
impl CompState for State {
    fn space(&self) -> &Space<Window> {
        &self.space
    }
    fn space_mut(&mut self) -> &mut Space<Window> {
        &mut self.space
    }
    fn keyboard(&self) -> &KeyboardHandle<State> {
        &self.keyboard
    }
    fn pointer(&self) -> &PointerHandle<State> {
        &self.pointer
    }
    fn output(&self) -> &Output {
        &self.output
    }
    fn xwm_mut(&mut self) -> &mut Option<X11Wm> {
        &mut self.xwm
    }
    fn next_window_loc(&self) -> Point<i32, Logical> {
        self.next_window_loc
    }
    fn set_next_window_loc(&mut self, loc: Point<i32, Logical>) {
        self.next_window_loc = loc;
    }
    fn active_workspace(&self) -> usize {
        self.active_workspace
    }
    fn set_active_workspace(&mut self, n: usize) {
        self.active_workspace = n;
    }
    fn hidden_windows(&self) -> &[HiddenWindow] {
        &self.hidden_windows
    }
    fn hidden_windows_mut(&mut self) -> &mut Vec<HiddenWindow> {
        &mut self.hidden_windows
    }
    fn desktop_shown(&self) -> bool {
        self.desktop_shown
    }
    fn set_desktop_shown(&mut self, on: bool) {
        self.desktop_shown = on;
    }
    fn suppressed_keys_mut(&mut self) -> &mut Vec<Keycode> {
        &mut self.suppressed_keys
    }
    fn cursor_status(&self) -> &CursorImageStatus {
        &self.cursor_status
    }
    fn cursor_buffer(&self) -> &smithay::backend::renderer::element::memory::MemoryRenderBuffer {
        &self.cursor_buffer
    }
    fn cursor_hotspot(&self) -> Point<i32, Logical> {
        self.cursor_hotspot
    }
    fn ws_switch_at(&self) -> Option<Instant> {
        self.ws_switch_at
    }
    fn set_ws_switch_at(&mut self, at: Option<Instant>) {
        self.ws_switch_at = at;
    }
    fn capture_blocked(&self) -> bool {
        self.capture_blocked
    }
    fn set_capture_blocked_flag(&mut self, on: bool) {
        self.capture_blocked = on;
    }
    fn black_buffer_mut(&mut self) -> &mut SolidColorBuffer {
        &mut self.black_buffer
    }
    fn bloom_mut(&mut self) -> &mut crate::comp_core::BloomCache {
        &mut self.bloom
    }
    fn orb_mut(&mut self) -> &mut crate::comp_core::OrbCache {
        &mut self.orb
    }
    fn text_rasterizer_mut(&mut self) -> &mut crate::text_render::TextRasterizer {
        &mut self.text_rasterizer
    }
    fn native_scene_caches(
        &mut self,
    ) -> (
        &mut crate::text_render::TextRasterizer,
        &mut crate::comp_core::OrbCache,
        &mut crate::comp_core::RectCache,
        &mut crate::scene::SceneCache,
    ) {
        (
            &mut self.text_rasterizer,
            &mut self.orb,
            &mut self.rect_cache,
            &mut self.scene_cache,
        )
    }
    fn note_pointer_button(&mut self, down: bool) {
        if down {
            self.pointer_buttons_down = self.pointer_buttons_down.saturating_add(1);
        } else {
            self.pointer_buttons_down = self.pointer_buttons_down.saturating_sub(1);
        }
    }
    fn pointer_pressed(&self) -> bool {
        self.pointer_buttons_down > 0
    }
    /// winit OVERRIDE: the shared flag-flip/log PLUS fail any in-flight screencopy
    /// frames so no capture queued just-as-the-kill-engaged leaks a frame painted before
    /// the black surface (winit owns the read-back queue; the DRM backend has none yet,
    /// so it uses the trait default). The `screen.kill` IPC verb calls this through the
    /// trait, so the queue drain is no longer dead code (the M8 inherent-vs-trait fix).
    fn set_capture_blocked(&mut self, on: bool) -> bool {
        let blocked = comp_core::set_capture_blocked_shared(self, on);
        if on {
            for p in self.pending_screencopy.drain(..) {
                p.frame.failed();
            }
        }
        blocked
    }
    fn emit_window_event(&mut self, event: &str, window: &Window, handle: &str) {
        let payload = ipc_event_window_json(self, window, handle);
        self.ipc.emit_event(event, payload);
    }
    fn registry_on_unmap(&mut self, handle: &WindowHandle) -> bool {
        self.windows.on_unmap(handle)
    }
    fn loop_handle(&self) -> &LoopHandle<'static, State> {
        &self.loop_handle
    }
    fn ipc_state_mut(&mut self) -> &mut crate::ipc::IpcState {
        &mut self.ipc
    }
}

// â”€â”€ BufferHandler â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
impl BufferHandler for State {
    fn buffer_destroyed(&mut self, _buffer: &WlBuffer) {}
}

// â”€â”€ CompositorHandler â€” the REAL map edge lives here (first buffer commit) â”€â”€â”€â”€
impl CompositorHandler for State {
    fn compositor_state(&mut self) -> &mut CompositorState {
        &mut self.compositor_state
    }

    fn client_compositor_state<'a>(&self, client: &'a Client) -> &'a CompositorClientState {
        // The XWayland client is inserted by smithay with `XWaylandClientData` (NOT our
        // `ClientState`), which carries its OWN `CompositorClientState`. Check it first,
        // then fall back to our socket-inserted clients. Mirrors anvil's shell/mod.rs â€”
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
        // Latch the newly-committed buffer into Smithay's surface state â€” without
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
        if let Some(window) = comp_core::window_for_surface(self, &root) {
            window.on_commit();

            // THE MAP EDGE (the no-phantom-window mint site): a toplevel is "mapped"
            // the first time it commits a buffer. We detect that transition by:
            //   (a) the commit being on the ROOT (toplevel) surface, AND
            //   (b) the window not yet carrying a WindowHandle (so we mint once), AND
            //   (c) the surface now actually having a buffer (is_mapped).
            // Only THEN do we mint the handle via `on_real_map` â€” exactly like
            // wayland.rs's xdg path, so a handle still proves a real map here too.
            let already_mapped = window.user_data().get::<WindowHandle>().is_some();
            if &root == surface && !already_mapped && surface_has_buffer(surface) {
                if let Some(toplevel) = window.toplevel() {
                    let app_id = toplevel_app_id(toplevel);
                    let title = toplevel_title(toplevel);
                    let handle = self.on_real_map(app_id, title, ToplevelKind::Xdg);
                    let handle_str = handle.as_str().to_string();
                    // Stash the handle on the Window so destroy can reverse it.
                    if let Some(w) = comp_core::window_for_surface(self, &root) {
                        w.user_data().insert_if_missing(|| handle);
                    }
                    // M5: tag the new toplevel with the active workspace (so
                    // `window.list` + move/switch can read it). It maps onto the
                    // workspace the user is currently looking at.
                    comp_core::tag_window_workspace(self, &window);
                    // M6: start the map-in fade clock (the render loop ramps this
                    // window's alpha 0â†’1 over FADE_IN_MS). Stamped once on the real map.
                    window.user_data().insert_if_missing(|| MapAnim(Instant::now()));
                    // M4: emit window.opened to IPC subscribers (IPC_PROTOCOL.md Â§5) â€”
                    // the same edge that minted the handle, so the event proves a real map.
                    let payload = ipc_event_window_json(self, &window, &handle_str);
                    self.ipc.emit_event("window.opened", payload);
                    // M3: give the freshly-mapped toplevel the keyboard focus + raise
                    // it, so a just-launched app receives keystrokes without a click.
                    self.space.raise_element(&window, true);
                    let serial = SERIAL_COUNTER.next_serial();
                    let keyboard = self.keyboard.clone();
                    keyboard.set_focus(self, Some(root.clone()), serial);
                    // M4: input focus changed â†’ window.focused (additive signal, Â§5).
                    let focus_payload = ipc_event_window_json(self, &window, &handle_str);
                    self.ipc.emit_event("window.focused", focus_payload);
                }
            }

            // X11 (XWayland) keyboard-focus-on-association. An X11 toplevel mints its
            // handle EARLY (in `XwmHandler::map_window_request`, before the wl_surface
            // exists), so the xdg map-edge branch above is skipped for it. But the
            // X11â†”wl_surface association is ASYNC: `wl_surface()` only becomes real on
            // the client's first commit (the xwayland-shell serial handshake). THIS is
            // that first commit â€” the surface now has a buffer and `window_for_surface`
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

        // #134 keyboard-focus-on-map (parity with the DRM/wayland.rs commit handler): once
        // the desktop glass shell has mapped (committed a buffer), give it the keyboard if
        // nothing else is focused, so the shell is typeable WITHOUT a click. No-op for
        // non-layer/unmapped surfaces and whenever a toplevel already holds focus.
        if surface_has_buffer(surface) {
            let serial = SERIAL_COUNTER.next_serial();
            comp_core::focus_desktop_shell_if_idle(self, surface, serial);
        }
    }
}

// â”€â”€ ShmHandler â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
impl ShmHandler for State {
    fn shm_state(&self) -> &ShmState {
        &self.shm_state
    }
}

// â”€â”€ XdgShellHandler â€” native Wayland toplevels (weston-simple-shm/foot map here)â”€
impl XdgShellHandler for State {
    fn xdg_shell_state(&mut self) -> &mut XdgShellState {
        &mut self.xdg_shell_state
    }

    fn new_toplevel(&mut self, surface: ToplevelSurface) {
        // Wrap as a desktop Window and place it; it is NOT mapped until the client
        // commits its first buffer (detected in `commit` â†’ `ensure_initial_configure`).
        let window = Window::new_wayland_window(surface.clone());
        // M3 cascade: place each new toplevel offset from the previous so multiple
        // windows are visibly distinct (the "tile or cascade" gate), then advance the
        // cursor + wrap before it walks off the output.
        let loc = comp_core::next_cascade_loc(self);
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
        if let Some(window) = comp_core::window_for_surface(self, &wl) {
            if let Some(handle) = window.user_data().get::<WindowHandle>().cloned() {
                if self.windows.on_unmap(&handle) {
                    info!(handle = handle.as_str(), "window.closed (toplevel destroyed)");
                    // M4: emit window.closed to IPC subscribers, invalidating the
                    // handle (IPC_PROTOCOL.md Â§5). Built before unmap so geometry is real.
                    let payload = ipc_event_window_json(self, &window, handle.as_str());
                    self.ipc.emit_event("window.closed", payload);
                }
            }
            self.space.unmap_elem(&window);
        } else {
            // M5: the destroyed toplevel may be on a NON-active workspace (so it lives
            // in `hidden_windows`, not the visible space). Purge it there.
            comp_core::purge_hidden_window(self, |w| {
                w.wl_surface().map(|s| &*s == &wl).unwrap_or(false)
            });
        }
    }
}

// â”€â”€ WlrLayerShellHandler â€” the glass-shell desktop mounts as a BACKGROUND layer â”€
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
                "layer.mapped (wlr-layer-shell surface tracked â€” the glass-shell desktop mount point)"
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

// â”€â”€ SeatHandler â€” WlSurface satisfies all three input-focus targets on this rev â”€
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
    /// default arrow, or nothing when Hidden). The standard anvil pattern â€” store, draw
    /// in the loop, no work here.
    fn cursor_image(&mut self, _seat: &Seat<Self>, image: CursorImageStatus) {
        self.cursor_status = image;
    }
}

// â”€â”€ SelectionHandler + DataDevice* â€” required by the dispatch2 protocol bundle â”€â”€
impl SelectionHandler for State {
    type SelectionUserData = ();
}
impl DataDeviceHandler for State {
    fn data_device_state(&mut self) -> &mut DataDeviceState {
        &mut self.data_device_state
    }
}
impl WaylandDndGrabHandler for State {}

// â”€â”€ XdgDecorationHandler â€” M3 server-side decoration negotiation â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
// The compositor prefers to own the chrome (SSD), so GTK/Qt apps drop their own CSD
// titlebar. Clients that hard-refuse SSD (request ClientSide) keep their frame â€” the
// correct fallback, not a bug. Ported from wayland.rs (the same SSD policy). M3 does
// NOT draw a HeaderBar yet (that is Phase-8 chrome polish) â€” it negotiates the mode;
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

// â”€â”€ XWaylandShellHandler â€” the X11â†”wl_surface association protocol state accessor;
// a `start_wm` bound. The association is observed; the map edge that mints a handle is
// `XwmHandler::map_window_request`. Ported from wayland.rs.
impl XWaylandShellHandler for State {
    fn xwayland_shell_state(&mut self) -> &mut XWaylandShellState {
        &mut self.xwayland_shell_state
    }
}

// â”€â”€ DndGrabHandler â€” drag'n'drop hand-off for X11 clients (a `start_wm` bound). Both
// callbacks are defaulted; HART-comp has no extra DnD bookkeeping at M3. Because the
// winit State impls SeatHandler + DataDeviceHandler and PointerFocus/TouchFocus =
// WlSurface, `WlSurface: DndFocus<State>` is satisfied (data_device's impl), so
// start_wm's `PointerFocus: DndFocus` / `TouchFocus: DndFocus` bounds hold without an
// X11Surface focus target.
impl DndGrabHandler for State {}

// â”€â”€ XwmHandler â€” the live X11 window-manager callbacks. The map/unmap bodies carry
// the no-phantom-window bookkeeping (ported 1:1 from wayland.rs): `map_window_request`
// is the corrected Wine map edge â€” it mints the handle via `on_real_map(..,
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
        // configure it to the geometry the space gave it â€” modelled 1:1 on anvil's
        // `map_window_request` (place â†’ `element_bbox` â†’ `configure`). Configuring to
        // the post-map bbox (rather than the client's raw pre-map geometry) is the
        // robust path: the space owns the window's on-screen rect, and X11 needs an
        // explicit configure to learn its position. `element_bbox` carries the
        // client's last-configure size (non-empty for a normal X11 toplevel), so the
        // window is sized correctly, not 0Ã—0.
        let loc = comp_core::next_cascade_loc(self);
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
        comp_core::tag_window_workspace(self, &win);
        // M6: start the map-in fade clock (render loop ramps alpha 0â†’1 over FADE_IN_MS).
        win.user_data().insert_if_missing(|| MapAnim(Instant::now()));
        // M4: emit window.opened to IPC subscribers (IPC_PROTOCOL.md Â§5) â€” minted on
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
        // Keyboard focus: the X11â†”wl_surface association is ASYNC under XWayland â€” at
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
                    // M4: emit window.closed to IPC subscribers (IPC_PROTOCOL.md Â§5).
                    let payload = ipc_event_window_json(self, &elem, handle.as_str());
                    self.ipc.emit_event("window.closed", payload);
                }
            }
            self.space.unmap_elem(&elem);
        } else {
            // M5: an X11 toplevel on a non-active workspace lives in `hidden_windows`.
            comp_core::purge_hidden_window(self, |w| w.x11_surface() == Some(&window));
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

// â”€â”€ OutputHandler â€” required for the output global's dispatch â”€â”€
impl smithay::wayland::output::OutputHandler for State {}

// One macro generates every Dispatch/GlobalDispatch impl from the Handler traits
// above (the unified dispatch model on this Smithay rev).
smithay::delegate_dispatch2!(State);

/// Send the initial xdg/layer configure once, on the surface's first commit, so
/// the client can proceed to attach a buffer (the map edge). Modelled on anvil's
/// `ensure_initial_configure` (trimmed to xdg-toplevel + layer-surface for M1).
fn ensure_initial_configure(state: &mut State, surface: &WlSurface) {
    // xdg toplevel?
    if let Some(window) = comp_core::window_for_surface(state, surface) {
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
/// Set `HART_COMP_NO_TEST_CLIENT=1` to suppress the auto-client entirely â€” used in
/// Milestone 2 when an EXTERNAL client (swaybg / the WebKit glass-shell host) is
/// attached deliberately and the auto foot toplevel would only add noise. The map
/// bar is still met by that external client; this just hands control of "what binds"
/// to the harness.
fn spawn_test_client(socket_name: &str) {
    if std::env::var_os("HART_COMP_NO_TEST_CLIENT").is_some() {
        info!(
            socket = socket_name,
            "HART_COMP_NO_TEST_CLIENT set â€” not spawning the auto test client (attach one with WAYLAND_DISPLAY={socket_name})"
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
    // under WSLg â€” llvmpipe/d3d12). We still log the decision for parity.
    let _ = select_render_path(cfg);

    // 1. The calloop event loop + our OWN server display. The `Display` is owned
    //    here in the loop (NOT inserted as a calloop Generic source) and dispatched
    //    directly each iteration â€” this avoids the `unsafe { display.get_mut() }`
    //    that anvil's Generic-source pattern needs, which the crate's
    //    `#![forbid(unsafe_code)]` would reject. Mirrors examples/minimal.rs.
    let mut event_loop: EventLoop<State> = EventLoop::try_new()?;
    let mut display: Display<State> = Display::new()?;
    let dh = display.handle();

    // 2. The winit backend â€” transparently connects to the host's wayland-0 as a
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
    //    connects to â€” distinct from the host's wayland-0 we are nested in.
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
    // M3: keep the keyboard + pointer handles â€” the loop routes winit input into them
    // and reads the cursor position for click-to-focus.
    // Same environment plumbing as the DRM backend -- see shared::XkbEnv for why
    // Default::default() silently drops XKB_DEFAULT_*.
    let xkb_env = crate::shared::XkbEnv::from_env();
    let keyboard = seat.add_keyboard(xkb_env.config(), 200, 25)?;
    let pointer = seat.add_pointer();

    let mut space: Space<Window> = Space::default();
    space.map_output(&output, (0, 0));

    // NOTE: ShmState::new(.., vec![]) already advertises the two MANDATORY shm
    // formats (Argb8888 + Xrgb8888) that weston-simple-shm/foot use â€” no
    // `update_formats(renderer.shm_formats())` needed (that would require the
    // `ImportMemWl` trait import just to add the same mandatory formats). Matches
    // examples/minimal.rs, which maps shm clients with exactly `vec![]`.

    // M6 â€” register the zwlr_screencopy_v1 manager global so `grim` can capture
    // HART-comp DIRECTLY. Version 3 (the buffer_done/linux_dmabuf level grim uses);
    // the per-client bind filter defaults to "allow all" â€” the socket-owner boundary
    // (Â§6.5) already constrains who connects, and the killswitch gates the actual copy.
    let _screencopy_global = dh
        .create_global::<State, smithay::reexports::wayland_protocols_wlr::screencopy::v1::server::zwlr_screencopy_manager_v1::ZwlrScreencopyManagerV1, _>(3, ());

    // M6 â€” bake the default-arrow cursor once (a dependency-free RGBA fallback so a
    // cursor is visible on llvmpipe without an xcursor theme load). `MemoryRenderBuffer`
    // owns the bytes; the render loop imports it to a texture lazily on first draw.
    let (cur_rgba, cur_w, cur_h, cur_hotspot) = comp_core::bake_default_cursor();
    let cursor_buffer = smithay::backend::renderer::element::memory::MemoryRenderBuffer::from_slice(
        &cur_rgba,
        smithay::backend::allocator::Fourcc::Argb8888,
        (cur_w, cur_h),
        1,
        Transform::Normal,
        None,
    );
    // M6 â€” the killswitch full-output black buffer (resized each frame it is drawn).
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
        // NATIVE SHELL M1 — composed lazily on the first frame at the real size.
        bloom: Default::default(),
        // NATIVE SHELL M2 — the voice orb, same lazy-compose contract.
        orb: Default::default(),
        text_rasterizer: crate::text_render::TextRasterizer::new(),
        rect_cache: Default::default(),
        scene_cache: Default::default(),
        pointer_buttons_down: 0,
    };

    // 6. (No calloop Generic source for the Display â€” see step 1. The Display is
    //    dispatched directly in the loop below, the safe-code minimal.rs pattern.)

    // 6b. M3 â€” spawn XWayland nested in OUR display, so X11 clients (Wine / xterm /
    //    xeyes) get an X server. On `Ready` we attach the X11 WM (start_wm), which
    //    routes X11 surface map/unmap through `XwmHandler`. `DISPLAY=:N` is published
    //    to the environment + logged so the harness can launch X11 children against it.
    spawn_xwayland(&dh, &event_loop.handle());

    // 6c. M4 â€” bind the com.hart.Compositor IPC server (Unix-socket twin) into the
    //    SAME calloop loop, so an agent (the brain's HartWmClient, or the M4 test
    //    client) arranges REAL windows via framed-JSON verbs against `state.space`.
    //    Best-effort: a bind failure leaves the compositor running for everything
    //    else (the IPC is an add-on, never a boot gate â€” same posture as XWayland).
    let _ipc_socket = crate::ipc::start_ipc(&event_loop.handle());

    // 7. Spawn a test client so a window MAPS + PAINTS (the M1 done-bar). It
    //    connects to OUR socket, not the host's.
    spawn_test_client(&socket_name);

    info!(
        socket = socket_name,
        size = ?win_size,
        "HART-comp winit compositor initialized â€” entering the loop (THE thing the skeleton never did)"
    );

    // 8. THE LOOP. Pump winit events, render the desktop, dispatch + flush clients.
    //
    // Env-gated FPS / frame-time probe (mirrors the HART_COMP_DEBUG_RENDER convention):
    // when `HART_COMP_FPS=1`, log a measured frame count + mean GLES frame time once a
    // second. This is the live latency signal for the GPU-render lever (A1): it reports
    // the ACTUAL paint cadence of the GlesRenderer path, not the 16ms loop target. Off by
    // default (zero cost when the env var is unset), so it never touches the boot path.
    let fps_probe = std::env::var_os("HART_COMP_FPS").is_some();
    let mut fps_frames: u32 = 0;
    let mut fps_since = Instant::now();
    while state.running {
        // â”€â”€ (a) Pump winit (host) events: resize / input / close / redraw. â”€â”€
        let status = winit.dispatch_new_events(|event| match event {
            WinitEvent::Resized { size, .. } => {
                let new_mode = Mode { size, refresh: 60_000 };
                output.change_current_state(Some(new_mode), None, None, None);
                output.set_preferred(new_mode);
                state.space.map_output(&output, (0, 0));
            }
            // M3: route winit input (keyboard / pointer motion / button / axis) into
            // the seat â€” keyboard to the focused surface, pointer to the surface under
            // the cursor, click-to-focus + raise. Replaces the M1 no-op stub.
            WinitEvent::Input(event) => comp_core::process_input_event(&mut state, event),
            WinitEvent::CloseRequested => {
                info!("winit window close requested â€” shutting down");
                state.running = false;
            }
            _ => {}
        });
        if let smithay::reexports::winit::event_loop::pump_events::PumpStatus::Exit(_) = status {
            state.running = false;
            break;
        }

        // â”€â”€ (b) Render the desktop into the winit framebuffer (+ M6: cursor, fades,
        //    killswitch black, then service screencopy against the painted frame). â”€â”€
        let size = backend.window_size();
        let damage = [smithay::utils::Rectangle::from_size(size)];
        if let Err(err) = render_frame(&mut state, &mut backend, &output, size, &damage) {
            warn!(?err, "render error");
        }
        // M6 â€” settle finished effects: clear the workspace-switch clock once the
        // crossfade completed so it doesn't re-evaluate forever. The loop renders every
        // iteration (calloop's 16ms timeout below paces it ~60fps), so an in-flight fade
        // PLAYS frame-by-frame without extra scheduling â€” `effects_animating` only gates
        // this tidy-up. (Map-in fades self-settle: `MapAnim::alpha` pins 1.0 past 150ms.)
        if !comp_core::effects_animating(&state) {
            state.ws_switch_at = None;
        }

        // â”€â”€ (c) Send frame callbacks so clients draw their NEXT frame. Iterate the
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

        // â”€â”€ (d) Submit the winit frame to the host compositor. â”€â”€
        if let Err(err) = backend.submit(Some(&damage)) {
            warn!(?err, "failed to submit winit frame");
        }

        // Env-gated frame-cadence probe (A1 GPU-render latency signal). Counts every
        // submitted GlesRenderer frame and, once a second, logs the measured FPS + mean
        // ms/frame so a live run reports its real paint cadence (e.g. "fps=60 frame_ms=2.1
        // renderer=GlesRenderer"). Cheap: a counter + one Instant compare per frame.
        if fps_probe {
            fps_frames += 1;
            let el = fps_since.elapsed();
            if el.as_millis() >= 1000 {
                let secs = el.as_secs_f64();
                let fps = fps_frames as f64 / secs;
                let frame_ms = (secs * 1000.0) / fps_frames as f64;
                info!(
                    fps = format!("{fps:.1}"),
                    frame_ms = format!("{frame_ms:.2}"),
                    frames = fps_frames,
                    renderer = "GlesRenderer",
                    "HART-comp winit: measured paint cadence"
                );
                fps_frames = 0;
                fps_since = Instant::now();
            }
        }

        // â”€â”€ (e) Dispatch the calloop socket source (accepts new clients), then the
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

// â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•
// M6 â€” the render frame. Factored out of the loop so the screencopy read-back (which
// needs `&mut State` AFTER the immutable element-build) can run against the SAME bound
// framebuffer without a closure borrow tangle. Z-order, TOPâ†’bottom (draw_render_elements
// paints slice-order = index 0 is top-most):
//   0. killswitch black surface (when capture_blocked) â€” ABOVE everything
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
    let (renderer, mut framebuffer) = backend.bind()?;

    // DEBUG: once a second, dump each space element's loc/size/surface/buffer so a
    // non-painting window is diagnosable. Gated behind HART_COMP_DEBUG_RENDER. (Kept
    // winit-side as a dev diagnostic; the element BUILD itself is the shared
    // comp_core::build_frame_elements below.)
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

    // The unified heterogeneous element list (killswitch â†’ cursor â†’ windows â†’ layers),
    // built by the SHARED comp_core z-order — the SAME builder the DRM backend calls, so
    // both compose the identical frame. The bind + paint + submit below stay winit-side
    // (they bind THIS backend's GlesRenderer framebuffer + flip to the WSLg host).
    let elements: Vec<HartRenderElement<GlesRenderer>> =
        comp_core::build_frame_elements(state, renderer, size);
    let _ = output;

    // Paint the frame.
    {
        let mut frame = renderer.render(&mut framebuffer, size, Transform::Flipped180)?;
        let clear = Color32F::new(
            HART_SPLASH_RGBA[0],
            HART_SPLASH_RGBA[1],
            HART_SPLASH_RGBA[2],
            HART_SPLASH_RGBA[3],
        );
        comp_core::draw_elements::<GlesRenderer>(&mut frame, clear, &elements, damage)?;
        let _sync = frame.finish()?;
    }
    // `elements` borrows `renderer` (the surface textures) â€” drop it before the
    // screencopy read-back re-borrows `renderer` mutably.
    drop(elements);

    // â”€â”€ M6 SCREENCOPY: service queued frames against the JUST-PAINTED framebuffer. The
    //    read-back captures EXACTLY what was painted (cursor + fades + killswitch), the
    //    whole reason to capture HART-comp's own output rather than the host recomposite. â”€â”€
    crate::screencopy::service_pending_screencopy(
        state,
        renderer,
        &framebuffer,
        size,
        Transform::Flipped180,
    );
    Ok(())
}

// NOTE: `send_frame_callbacks` / `surface_has_buffer` / `toplevel_app_id` /
// `toplevel_title` / `x11_app_id` / `x11_title` are backend-AGNOSTIC and now live in
// `crate::shared` (imported at the top of this module) â€” ONE implementation shared with
// the DRM/udev backend, not a parallel copy (M7 Stage-B hoist, CLAUDE.md Gate 4).

/// Build the IPC event-frame `window` payload (IPC_PROTOCOL.md §5) for one mapped
/// `Window`. Thin winit-side alias for the SHARED `comp_core::ipc_event_window_json_for`
/// (one serializer, no parallel path — the handlers in this file built the payload via
/// this name before the M8 hoist; keeping the name avoids churning every call site).
fn ipc_event_window_json(state: &State, window: &Window, handle: &str) -> serde_json::Value {
    comp_core::ipc_event_window_json_for(state, window, handle)
}

/// M3 â€” spawn XWayland nested in OUR display + attach the X11 WM on `Ready`. Modelled
/// 1:1 on anvil `state.rs::start_xwayland`: `XWayland::spawn` returns `(XWayland,
/// Client)`; the `Client` is captured here and threaded into the `Ready` handler so
/// `X11Wm::start_wm` can bind it (the `Ready` event carries only the privileged X11
/// socket + display number, NOT the client). On `Ready` we publish `DISPLAY=:N` so X11
/// children (Wine / xterm / xeyes) connect to THIS X server, and log it so the harness
/// can launch them. Best-effort: a spawn failure logs + leaves `xwm = None` (the
/// compositor still runs for Wayland-native clients â€” XWayland is an opportunistic
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
                        "XWayland ready â€” X11 WM attached (launch X11 apps with this DISPLAY)"
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

// â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•
// M5 â€” behavioural unit floor for the keyboard-shortcut MAP.
//
// `process_keyboard_shortcut` is a PURE function (ModifiersState, modified Keysym,
// level-0 digit Keysym) -> the resolved WmAction, so the full chordâ†’action contract is
// unit-testable without a live compositor â€” which matters because the
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
// discrimination is exact (Super+Shift+N â‰  Super+N), the digit row survives Shift
// (the `digit_sym` fix), and non-chord keys are forwarded (None) so apps keep their
// own keystrokes. Mirrors anvil's `process_keyboard_shortcut` table.
// â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•
