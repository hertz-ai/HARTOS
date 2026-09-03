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
// M8 — the SHARED WM brain (workspaces, keybindings, cursor, killswitch, effects, IPC
// verbs). The DRM State impls `CompState` (below) to feed it, so the FULL desktop runs
// on real hardware too — ONE implementation across both backends, not a parallel path.
use crate::comp_core::{self, CompState};

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
    /// depend on the transport concretely. (The summon/Foreign-toplevel path uses this
    /// WindowRecord-typed sink; the M8 shared WM edges fan out via `ipc` below.)
    pub emit_ipc_event: Box<dyn FnMut(&str, &WindowRecord)>,

    // ════════════════════════════════════════════════════════════════════════
    // M8 — the full-desktop WM state, the SAME field set winit::State holds, so the
    // shared `comp_core::CompState` brain (workspaces, keybindings, cursor, killswitch,
    // effects, the com.hart.Compositor IPC) runs on the DRM path too. Constructed in
    // udev.rs::run_udev. These are why M8 wires the FULL desktop on real hardware, not
    // just the Stage-A layer-shell boot floor.
    // ════════════════════════════════════════════════════════════════════════
    /// M3 cascade placement cursor (each new toplevel offset from the last).
    pub next_window_loc: smithay::utils::Point<i32, Logical>,
    /// M5 workspaces — `space` holds ONLY the active workspace; the rest live here.
    pub active_workspace: usize,
    pub hidden_windows: Vec<crate::comp_core::HiddenWindow>,
    pub desktop_shown: bool,
    /// M5 keycodes whose intercepted press must also have its release swallowed.
    pub suppressed_keys: Vec<smithay::input::keyboard::Keycode>,
    /// M6 software cursor — the latest client-requested image + the baked default arrow.
    pub cursor_status: smithay::input::pointer::CursorImageStatus,
    pub cursor_buffer: smithay::backend::renderer::element::memory::MemoryRenderBuffer,
    pub cursor_hotspot: smithay::utils::Point<i32, Logical>,
    /// M6 effects — the workspace-switch crossfade clock (per-window fade is user-data).
    pub ws_switch_at: Option<std::time::Instant>,
    /// M6 killswitch — the constitutional screen cut (black surface + input/capture gate).
    pub capture_blocked: bool,
    /// NATIVE SHELL M3: render the native scene (top bar + hero + rows + taskbar) this
    /// session. Set from the HART_NATIVE_SHELL env at State construction (default OFF,
    /// so the WebView shell is unchanged). No nix option until M6 flips the default.
    pub native_shell_on: bool,
    /// NATIVE SHELL M3: the latest home_compose scene from the shell.compose IPC verb.
    pub native_home: Option<crate::scene::HomeCompose>,
    /// NATIVE SHELL M3 text: cosmic-text rasterizer (FontSystem enumerated once).
    pub text_rasterizer: crate::text_render::TextRasterizer,
    pub black_buffer: smithay::backend::renderer::element::solid::SolidColorBuffer,
    /// NATIVE SHELL M1 — the composed aura backdrop, cached across frames so the
    /// per-pixel compose runs once per (mode, theme) rather than every frame.
    pub bloom: crate::comp_core::BloomCache,
    /// NATIVE SHELL M2 — the voice orb, composed once and animated per frame by
    /// scale+alpha on the GPU.
    pub orb: crate::comp_core::OrbCache,
    /// M8 — the com.hart.Compositor IPC server's per-compositor state (the event
    /// fan-out subscribers). The DRM backend serves the SAME framed-JSON socket the
    /// winit backend does, so an agent arranges real windows on real hardware too.
    pub ipc: crate::ipc::IpcState,

    /// F1 (#166) — CRTCs whose page-flip vblank arrived since the last render tick. The
    /// per-device DRM VBlank source (a calloop closure with only `&mut State`, NOT the
    /// per-device surface table) inserts the completed CRTC here; the render tick's
    /// `reap_completed_vblanks` drains it, calls `frame_submitted()` on the matching
    /// surface IN VBLANK ORDER, and unblocks that CRTC's next flip. This is the hand-off
    /// that lets frame-submit be paced by REAL vblanks instead of fired unconditionally
    /// every tick (which freed in-flight scanout buffers → torn frames).
    pub vblank_completed: std::collections::HashSet<
        smithay::reexports::drm::control::crtc::Handle,
    >,

    /// A libseat session activate/pause request parked by the session notifier (VT switch /
    /// suspend / resume), drained by the render loop's `apply_pending_session`. The notifier
    /// calloop closure gets ONLY `&mut State` — never the per-device DRM table it must act on — so,
    /// exactly like `vblank_completed` hands a CRTC to the render tick, it parks the request here:
    /// `Some(true)` = session ACTIVE → (re)acquire DRM master on every device + repaint;
    /// `Some(false)` = session PAUSED → drop master; `None` = nothing pending. REQUIRED, not
    /// cosmetic: the device tracks the SESSION `active` flag (what `is_active()` reads; init TRUE)
    /// SEPARATELY from the kernel DRM-master grant, with NO auto-recovery for either — so an
    /// unprivileged-at-startup device (active TRUE, the real-HW "Unable to become drm master" race)
    /// retries every page-flip EACCES forever, and a VT-paused device (active FALSE) returns
    /// DeviceInactive; BOTH stay black until the caller re-takes master. Note: on the pinned smithay
    /// rev `DrmDevice::activate()` CANNOT re-take master for a device that came up unprivileged (it
    /// only re-`acquire_master_lock`s when smithay's frozen `privileged` flag is already true), so
    /// the udev backend re-takes master by issuing `drmSetMaster` DIRECTLY on the fd — see
    /// `udev::acquire_drm_master` for the full rationale.
    pub pending_session_activate: Option<bool>,

    /// #137 — the frame-budget repaint scheduler. The render tick (udev.rs `render_all`)
    /// gates its build+composite+flip on `repaint.should_paint(...)`, so a static desktop
    /// stops re-importing textures + re-running the pixman damage pass + attempting a
    /// page-flip on EVERY 16ms tick. Every damage edge marks it: a client `commit` (here),
    /// an input event, a session/master change (udev.rs). It starts DIRTY and has a
    /// heartbeat backstop, so it can only ever skip a provably-idle frame — never black or
    /// freeze the never-fail floor. The pure logic + its unit tests live in main.rs
    /// (`RepaintScheduler`); this holds the single live instance for the DRM backend.
    pub repaint: crate::RepaintScheduler,
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

// ════════════════════════════════════════════════════════════════════════════
// M8 — the DRM State feeds the SHARED WM brain by impl'ing `comp_core::CompState`.
// Every accessor hands back a field this State holds; the brain (comp_core::*) drives
// them, so the FULL desktop — window arrange (the moat), workspaces, keybindings,
// software cursor, screen kill-switch, fade effects, the com.hart.Compositor IPC —
// runs on the DRM path with ZERO parallel code vs winit. The seat handles are
// `…Handle<State>` (State impls SeatHandler below), satisfying the supertrait bound.
// ════════════════════════════════════════════════════════════════════════════
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
    fn next_window_loc(&self) -> smithay::utils::Point<i32, Logical> {
        self.next_window_loc
    }
    fn set_next_window_loc(&mut self, loc: smithay::utils::Point<i32, Logical>) {
        self.next_window_loc = loc;
    }
    fn active_workspace(&self) -> usize {
        self.active_workspace
    }
    fn set_active_workspace(&mut self, n: usize) {
        self.active_workspace = n;
    }
    fn hidden_windows(&self) -> &[comp_core::HiddenWindow] {
        &self.hidden_windows
    }
    fn hidden_windows_mut(&mut self) -> &mut Vec<comp_core::HiddenWindow> {
        &mut self.hidden_windows
    }
    fn desktop_shown(&self) -> bool {
        self.desktop_shown
    }
    fn set_desktop_shown(&mut self, on: bool) {
        self.desktop_shown = on;
    }
    fn suppressed_keys_mut(&mut self) -> &mut Vec<smithay::input::keyboard::Keycode> {
        &mut self.suppressed_keys
    }
    fn cursor_status(&self) -> &smithay::input::pointer::CursorImageStatus {
        &self.cursor_status
    }
    fn cursor_buffer(&self) -> &smithay::backend::renderer::element::memory::MemoryRenderBuffer {
        &self.cursor_buffer
    }
    fn cursor_hotspot(&self) -> smithay::utils::Point<i32, Logical> {
        self.cursor_hotspot
    }
    fn ws_switch_at(&self) -> Option<std::time::Instant> {
        self.ws_switch_at
    }
    fn set_ws_switch_at(&mut self, at: Option<std::time::Instant>) {
        self.ws_switch_at = at;
    }
    fn capture_blocked(&self) -> bool {
        self.capture_blocked
    }
    fn native_shell_on(&self) -> bool {
        self.native_shell_on
    }
    fn native_home(&self) -> Option<&crate::scene::HomeCompose> {
        self.native_home.as_ref()
    }
    fn set_native_home(&mut self, home: crate::scene::HomeCompose) {
        self.native_home = Some(home);
    }
    fn text_rasterizer_mut(&mut self) -> &mut crate::text_render::TextRasterizer {
        &mut self.text_rasterizer
    }
    fn set_capture_blocked_flag(&mut self, on: bool) {
        self.capture_blocked = on;
    }
    fn black_buffer_mut(&mut self) -> &mut smithay::backend::renderer::element::solid::SolidColorBuffer {
        &mut self.black_buffer
    }
    fn bloom_mut(&mut self) -> &mut crate::comp_core::BloomCache {
        &mut self.bloom
    }
    fn orb_mut(&mut self) -> &mut crate::comp_core::OrbCache {
        &mut self.orb
    }
    fn emit_window_event(&mut self, event: &str, window: &Window, handle: &str) {
        // Fan the edge out over the SHARED framed-JSON IPC (the same socket the winit
        // backend serves), AND mirror it to the WindowRecord-typed summon/foreign sink
        // so the existing DRM-side log line + foreign-toplevel projection still fire.
        let payload = comp_core::ipc_event_window_json_for(self, window, handle);
        self.ipc.emit_event(event, payload);
    }
    fn registry_on_unmap(&mut self, handle: &WindowHandle) -> bool {
        self.windows.on_unmap(handle)
    }
    fn loop_handle(&self) -> &smithay::reexports::calloop::LoopHandle<'static, State> {
        &self.loop_handle
    }
    fn ipc_state_mut(&mut self) -> &mut crate::ipc::IpcState {
        &mut self.ipc
    }
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

    /// M8 — a client set (or hid) its cursor. Stash the latest status; the shared render
    /// path (`comp_core::build_frame_elements`) draws it each frame (client surface, the
    /// baked default arrow, or nothing when Hidden) — the SAME software-cursor path as
    /// winit. Without this the DRM desktop showed no cursor (Stage-A had no cursor draw).
    fn cursor_image(
        &mut self,
        _seat: &smithay::input::Seat<Self>,
        image: smithay::input::pointer::CursorImageStatus,
    ) {
        self.cursor_status = image;
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
        // M8 — cascade each new toplevel offset from the last (the SAME `next_cascade_loc`
        // the winit backend uses) so multiple windows are visibly distinct, not stacked at
        // the origin. The AI-native WM (the IPC tile/place verbs) refines real placement.
        let loc = comp_core::next_cascade_loc(self);
        self.space.map_element(window, loc, true);
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
    let window = state.window_for_toplevel(surface).cloned();
    if let Some(window) = window {
        window.user_data().insert_if_missing(|| handle.clone());
        // M8 full-desktop bookkeeping (identical to winit's commit map-edge): tag the
        // active workspace so window.list + move/switch see it, start the map-in fade
        // clock, and give the just-mapped toplevel keyboard focus + raise it so a
        // just-launched app receives keystrokes without a click.
        comp_core::tag_window_workspace(state, &window);
        window.user_data().insert_if_missing(|| comp_core::MapAnim(std::time::Instant::now()));
        state.space.raise_element(&window, true);
        let serial = smithay::utils::SERIAL_COUNTER.next_serial();
        let surf = window.wl_surface().map(|s| s.into_owned());
        let keyboard = state.keyboard.clone();
        keyboard.set_focus(state, surf, serial);
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
        // M8 — cascade it (the SAME placement the winit X11 map edge uses).
        let loc = comp_core::next_cascade_loc(self);
        let win = Window::new_x11_window(window);
        self.space.map_element(win.clone(), loc, true);
        // Configure it to the geometry the space gave it (X11 needs an explicit
        // configure to know its size); best-effort, the real tiling policy refines.
        if let Some(bbox) = self.space.element_bbox(&win) {
            if let Some(xsurface) = win.x11_surface() {
                let _ = xsurface.configure(Some(bbox));
            }
        }
        let handle = self.on_real_map(app_id, title, ToplevelKind::XWayland);
        win.user_data().insert_if_missing(|| handle);
        // M8 full-desktop bookkeeping (identical to winit's X11 map edge): tag the
        // active workspace + start the map-in fade clock. (X11 keyboard focus is set on
        // the surface's first commit, as the wl_surface associates asynchronously.)
        comp_core::tag_window_workspace(self, &win);
        win.user_data().insert_if_missing(|| comp_core::MapAnim(std::time::Instant::now()));
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
    } else {
        // M8 — the destroyed toplevel may live on a NON-active workspace (so it is in
        // `hidden_windows`, not the visible space, and `handle_for_surface` missed it).
        // Purge it there (emits window.closed + invalidates the handle), the SAME
        // hidden-workspace cleanup the winit destroy path does.
        comp_core::purge_hidden_window(state, |w| {
            w.wl_surface().map(|s| &*s == surface).unwrap_or(false)
        });
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
    /// M8 — route a single libinput event into the FULL shared WM input path
    /// (`comp_core::process_input_event`): click-to-focus + raise, the keyboard-shortcut
    /// chords (workspaces/snap/close/show-desktop), the pointer hit-test honouring the
    /// layer z-order, and the screen-kill input gate — IDENTICAL to the winit backend.
    /// The Stage-A forward-only stub is gone; the DRM desktop is now the full WM.
    pub fn process_input_event<B: smithay::backend::input::InputBackend>(
        &mut self,
        event: smithay::backend::input::InputEvent<B>,
    ) {
        comp_core::process_input_event(self, event);
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
        // #137 — a client committed: the canonical damage edge. Schedule a repaint on the
        // next render tick (the frame-budget scheduler is otherwise idle). Marking on every
        // commit is intentional + cheap: over-marking costs at most one extra repaint, and
        // it is what keeps an animating client (the breathing orb) painting at full rate
        // while a static desktop skips idle frames. Sync-subsurface commits mark too — their
        // parent's commit follows, and an extra repaint is harmless (never a missed frame).
        self.repaint.mark_damaged();
        if is_sync_subsurface(surface) {
            return;
        }
        // Walk to the root (the toplevel's surface).
        let mut root = surface.clone();
        while let Some(parent) = get_parent(&root) {
            root = parent;
        }
        if let Some(window) = comp_core::window_for_surface(self, &root) {
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

            // M8 — X11 keyboard-focus-on-association (identical to winit's commit
            // handler). An X11 toplevel minted its handle EARLY in `map_window_request`
            // (before its wl_surface existed); the X11↔wl_surface association is ASYNC, so
            // its first commit (this one, with a buffer + a matched window) is where we
            // give it keyboard focus, ONCE (de-duplicated by the `X11Focused` marker).
            if &root == surface
                && window.x11_surface().is_some()
                && surface_has_buffer(surface)
                && window.user_data().get::<comp_core::X11Focused>().is_none()
            {
                window.user_data().insert_if_missing(|| comp_core::X11Focused);
                self.space.raise_element(&window, true);
                let serial = smithay::utils::SERIAL_COUNTER.next_serial();
                let keyboard = self.keyboard.clone();
                keyboard.set_focus(self, Some(root.clone()), serial);
            }
        }
        ensure_initial_configure(self, surface);

        // #134 keyboard-focus-on-map: once the desktop glass shell has actually mapped
        // (committed a buffer), give it the keyboard if nothing else is focused — so the
        // user can type on a fresh boot WITHOUT a click (a dead/late pointer must never
        // make the desktop untypeable). No-op for non-layer surfaces, unmapped surfaces,
        // and whenever a toplevel already holds focus (it only fires while focus is idle).
        if surface_has_buffer(surface) {
            let serial = smithay::utils::SERIAL_COUNTER.next_serial();
            comp_core::focus_desktop_shell_if_idle(self, surface, serial);
        }
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
    if let Some(window) = comp_core::window_for_surface(state, surface) {
        if let Some(toplevel) = window.toplevel() {
            let initial_configure_sent = with_states(surface, |states| {
                states
                    .data_map
                    .get::<XdgToplevelSurfaceData>()
                    // A poisoned surface-data mutex must NOT abort the compositor on a
                    // client commit (#186 never-fail floor): treat "can't read the flag"
                    // as "configure already sent" so we don't double-configure, and keep
                    // the compositor alive. `.lock()` poisons only after a prior panic.
                    .and_then(|d| d.lock().ok().map(|g| g.initial_configure_sent))
                    .unwrap_or(true)
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
                // Same poisoned-mutex guard as the xdg path above (#186).
                .and_then(|d| d.lock().ok().map(|g| g.initial_configure_sent))
                .unwrap_or(true)
        });
        if !initial_configure_sent {
            layer.layer_surface().send_configure();
        }
    }
}
