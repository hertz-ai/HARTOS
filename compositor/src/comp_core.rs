// ════════════════════════════════════════════════════════════════════════════
// HART-comp — Milestone 8 (Stage B): the SHARED compositor core.
//                                  ⚠️  CI-COMPILE (winit OR smithay only)  ⚠️
// ════════════════════════════════════════════════════════════════════════════
//
// ── Why this module exists (the M8 "hoist + converge", DRY gate) ──
//   Through M7, the ENTIRE window-management brain — the AI-native WM IPC verbs
//   (window.list/tile/focus/place/close/move), the input router, the M5 workspace
//   machinery, the M5 keyboard-shortcut actions, the M6 software cursor + screen
//   kill-switch + fade effects, and the render-element z-order — lived ONLY in
//   `winit.rs` behind `#[cfg(feature="winit")]`, typed against the concrete
//   `winit::State` + its `GlesRenderer`. The DRM/udev backend (`wayland.rs` +
//   `udev.rs`) had only a Stage-A boot floor (layer-shell scanout + a forward-only
//   input stub) — it could NOT arrange windows, had no workspaces, no cursor, no
//   killswitch, no effects. Copying winit's WM brain into the DRM path would be a
//   parallel path that drifts (CLAUDE.md Gate 4) — the SAME mistake M7's shared.rs
//   header warned against, at 10× the surface.
//
//   M8 hoists ALL of it here, ONCE, generic over the backend. The two backends keep
//   their OWN concrete `State` (the renderer type differs — GlesRenderer for winit,
//   PixmanRenderer for DRM — and the per-frame submit differs — winit `backend.submit`
//   vs DRM `DrmCompositor::queue_frame`), but they share this brain by implementing
//   the `CompState` trait (a set of field accessors). Every WM verb, every workspace
//   switch, every cursor draw, every keybinding is now ONE implementation feeding both.
//
// ── Why a `CompState` TRAIT and not a `CompCore` struct field ──
//   The state-converge plan's first instinct was "a shared `CompCore` struct both
//   States embed". That is IMPOSSIBLE here without a parallel path, because the
//   load-bearing calls `keyboard.set_focus(self, …)` / `pointer.motion(self, …)`
//   require `self: &mut D` where the seat is `KeyboardHandle<D>` / `PointerHandle<D>`
//   and `D: SeatHandler` (verified against the pinned rev:
//   `KeyboardHandle<D: SeatHandler>::set_focus(&self, data: &mut D, …)`). The seat
//   is bound to the CONCRETE State that impls `SeatHandler` + is the `Display<State>`
//   dispatch target; a `CompCore` field would need its OWN `KeyboardHandle<CompCore>`
//   and `SeatHandler for CompCore`, diverging from the `Display<State>` the protocol
//   handlers dispatch into — two seats, a guaranteed drift. The TRAIT keeps the ONE
//   real seat on each backend's State and lets the shared logic call
//   `state.keyboard().clone().set_focus(state, …)` generically (`state: &mut S`,
//   `S: CompState: SeatHandler`, the handle is `KeyboardHandle<S>` — it composes).
//   This is the standard Rust answer to the seat-handle ownership problem.
//
// ── What STAYS backend-specific (NOT here) ──
//   • each backend's `State` struct (renderer type differs) + its construction,
//   • `run_winit` / `run_udev` (the event loop + the renderer/submit),
//   • the per-frame BIND+SUBMIT (winit `backend.bind()`/`submit`; DRM
//     `DrmCompositor::render_frame`/`queue_frame`/`frame_submitted`),
//   • `spawn_xwayland` (per-backend stdio/source),
//   • every Smithay protocol-handler impl (`XdgShellHandler`/`CompositorHandler`/…)
//     + `delegate_dispatch2!(State)` — impl'd on each concrete `State`, already in
//     BOTH winit.rs and wayland.rs.
//   This module builds ON `shared.rs` (the surface-tree/app-id readers) — it does not
//   duplicate them.

#![cfg(any(feature = "winit", feature = "smithay"))]

use std::cell::Cell;
use std::time::Instant;

use smithay::backend::renderer::{
    ImportAll, ImportMem, Renderer,
    element::{
        AsRenderElements, Kind,
        memory::MemoryRenderBufferRenderElement,
        solid::{SolidColorBuffer, SolidColorRenderElement},
        surface::{WaylandSurfaceRenderElement, render_elements_from_surface_tree},
        memory::MemoryRenderBuffer,
    },
};
// `Color32F`/`Frame`/`RendererSuper`/`draw_render_elements` are used ONLY by
// `draw_elements` (the winit manual-paint helper); gated so a smithay-only build (where
// DrmCompositor owns the clear+draw) does not flag them unused.
#[cfg(feature = "winit")]
use smithay::backend::renderer::{Color32F, Frame, RendererSuper, utils::draw_render_elements};
use smithay::backend::input::{
    AbsolutePositionEvent, Axis, AxisSource, ButtonState, Event, InputBackend, InputEvent,
    KeyState, Keycode, KeyboardKeyEvent, PointerAxisEvent, PointerButtonEvent, PointerMotionEvent,
};
use smithay::desktop::{Space, Window, WindowSurfaceType, layer_map_for_output};
use smithay::input::{
    SeatHandler,
    keyboard::{FilterResult, Keysym, ModifiersState, keysyms as xkb},
    pointer::{AxisFrame, ButtonEvent, CursorImageStatus, MotionEvent, RelativeMotionEvent},
};
use smithay::backend::allocator::Fourcc;
use smithay::output::Output;
use smithay::reexports::wayland_server::protocol::wl_surface::WlSurface;
use smithay::utils::{
    Buffer as BufferCoord, Logical, Physical, Point, Rectangle, SERIAL_COUNTER, Scale, Serial,
    Size, Transform,
};
use smithay::wayland::compositor::{get_parent, with_states};
use smithay::wayland::shell::wlr_layer::Layer as WlrLayer;
// `Window::wl_surface()` / `X11Surface`-focus come from `WaylandFocus` on this rev.
use smithay::wayland::seat::WaylandFocus;
use smithay::xwayland::X11Wm;
use tracing::{debug, info, warn};

use crate::WindowHandle;
use crate::shared::{toplevel_app_id, toplevel_title, x11_app_id, x11_title};

// ════════════════════════════════════════════════════════════════════════════
// THE unified render element (M6, hoisted from winit.rs). Already generic over R:
//   • `Surface` — window + layer client surfaces (faded via the alpha arg)
//   • `Memory`  — the software cursor (a baked default-arrow MemoryRenderBuffer)
//   • `Solid`   — the killswitch full-output black surface (+ cursor fallback)
// Generic over R, so BOTH the winit GlesRenderer and the DRM PixmanRenderer build
// the SAME element list — this is what lets the z-order + cursor + killswitch be one
// implementation across both backends.
// NOTE: the `render_elements!` macro parses each trait bound as a single token tree
// (`$bound:tt`), so the bounds MUST be bare idents — `smithay::…::ImportAll` (a path)
// fails to match. `ImportAll`/`ImportMem` are imported by bare name above.
// ════════════════════════════════════════════════════════════════════════════
smithay::backend::renderer::element::render_elements! {
    pub HartRenderElement<R> where R: ImportAll + ImportMem;
    Surface=WaylandSurfaceRenderElement<R>,
    Memory=MemoryRenderBufferRenderElement<R>,
    Solid=SolidColorRenderElement,
}

// ════════════════════════════════════════════════════════════════════════════
// PURE WM/effects DATA TYPES (M5/M6, hoisted from winit.rs). None touch the
// renderer; all are keyed on `Window` user-data, so they are backend-agnostic.
// ════════════════════════════════════════════════════════════════════════════

/// Fade duration for a window map-in. 150ms is the M6 spec figure: long enough to
/// capture mid-fade, short enough to feel instant.
pub const FADE_IN_MS: u128 = 150;
/// Workspace-switch crossfade duration — the whole active set fades in on switch.
pub const WS_FADE_MS: u128 = 120;

/// Last painted wlr-layer-surface count, so the render loop logs a one-line
/// transition (0→N / N→0) instead of spamming every frame. Pure observability.
pub static LAYERS_PAINTED: std::sync::atomic::AtomicUsize =
    std::sync::atomic::AtomicUsize::new(0);

/// Which native chrome the LAST built frame actually contained, as a bitmask
/// (NATIVE_CHROME_BLOOM | NATIVE_CHROME_ORB).
///
/// The shell only stands down for chrome we can prove we are drawing, so the
/// claim must be published from the RENDER PATH rather than from configuration:
/// "the flag is set" is a promise, "this element was in the frame that reached
/// the screen" is evidence. Getting that backwards yields a desktop with no
/// background, which the paint watchdog does NOT catch — it watches for hangs,
/// not for wrong-looking desktops.
///
/// A static for the same reason LAYERS_PAINTED is one: the value is produced
/// deep in the generic frame builder and consumed by the backend's flip
/// handler, and threading it through the CompState trait would put a render
/// detail into the backend-agnostic accessor surface for no gain.
pub static NATIVE_CHROME_EMITTED: std::sync::atomic::AtomicU8 =
    std::sync::atomic::AtomicU8::new(0);
pub const NATIVE_CHROME_BLOOM: u8 = 1 << 0;
pub const NATIVE_CHROME_ORB: u8 = 1 << 1;

/// The action a compositor keyboard shortcut resolves to (anvil's `KeyAction`
/// analogue). `process_keyboard_shortcut` maps a `(ModifiersState, Keysym)` to one of
/// these; the chord is INTERCEPTED (never forwarded to the focused client) iff the map
/// returns `Some`. The action is executed AFTER `KeyboardHandle::input` returns.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum WmAction {
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

/// Which workspace a mapped `Window` belongs to. Stashed in the `Window`'s user-data.
/// `Cell` because `UserDataMap` only offers `insert_if_missing` + `get` (no replace).
pub struct WorkspaceTag(pub Cell<usize>);

/// The focused window's geometry captured the FIRST time it is snapped/maximized, so
/// Super+Down (`RestoreWindow`) can put it back. Stashed in user-data.
pub struct PreSnapGeom(pub Cell<Option<Rectangle<i32, Logical>>>);

/// A window that has been moved OFF the visible `Space` (it lives on a non-active
/// workspace, or is hidden by show-desktop). Held with the location to restore it to.
pub struct HiddenWindow {
    pub window: Window,
    /// The workspace this window belongs to.
    pub workspace: usize,
    /// Where it was on the visible output before being hidden.
    pub loc: Point<i32, Logical>,
}

/// One-shot marker stashed in an X11 `Window`'s user-data the first time it is given
/// keyboard focus (on its first associated commit). X11 surfaces associate their
/// `wl_surface` ASYNCHRONOUSLY under XWayland, so focus-on-map is deferred to the first
/// commit and de-duplicated by this marker.
pub struct X11Focused;

/// When a window was mapped, so the render loop can compute its fade-in alpha. Stashed
/// in the window's user-data. The instant is monotonic (`Instant`).
pub struct MapAnim(pub Instant);

impl MapAnim {
    /// The fade-in alpha for this window NOW: 0→1 over `FADE_IN_MS`, then pinned 1.0.
    pub fn alpha(&self) -> f32 {
        let e = self.0.elapsed().as_millis();
        if e >= FADE_IN_MS {
            1.0
        } else {
            (e as f32 / FADE_IN_MS as f32).clamp(0.0, 1.0)
        }
    }
    /// Is this window still animating (so the loop must keep redrawing)?
    pub fn animating(&self) -> bool {
        self.0.elapsed().as_millis() < FADE_IN_MS
    }
}

// ════════════════════════════════════════════════════════════════════════════
// NATIVE SHELL PARITY PROGRAM, M1 — the composed aura backdrop.
// ════════════════════════════════════════════════════════════════════════════

/// The composed bloom field, held as a texture-ready buffer across frames.
///
/// This is the "COMPOSE ONCE, REUSE FOREVER" half of `bloom.rs`'s performance
/// contract. `bloom::compose` walks every pixel, which is a few milliseconds at
/// panel size — perfectly fine ONCE, and catastrophic at 60Hz. So the result is
/// cached against `(width, height, palette)` and recomposed ONLY when the output
/// mode or the theme actually changes. A steady desktop does zero bloom work per
/// frame, which is what keeps the #137 idle-skip meaningful.
///
/// Held on each backend's `State` (reached via `CompState::bloom_mut`) rather
/// than in a static, mirroring how `black_buffer` is already owned.
#[derive(Default)]
pub struct BloomCache {
    /// Resolved ONCE, not per frame. `bloom::theme_palette` reads a JSON file off
    /// disk; doing that at 60Hz would be a syscall storm behind a static image.
    palette: Option<crate::bloom::BloomPalette>,
    key: Option<(i32, i32, crate::bloom::BloomPalette)>,
    buffer: Option<MemoryRenderBuffer>,
}

impl BloomCache {
    // KNOWN GAP, deliberately not papered over with an unused method: the
    // palette is resolved once and never re-read, so a theme change at runtime
    // ("switch theme" through the agent) will not restyle this backdrop until
    // the compositor restarts. An `invalidate()` was written here and removed
    // again because nothing calls it, and a dead pub method is worse than an
    // absent one: it warns on every build and reads as though the wiring exists.
    // Whoever lands the theme-change signal adds it back with a caller.

    /// The backdrop for this size, composing only on a genuine miss.
    ///
    /// Returns `None` for a degenerate output size (a disconnected or
    /// not-yet-moded connector reports 0x0). The caller simply paints no
    /// backdrop then and the clear colour still covers the frame, so a bad mode
    /// can never panic the render loop.
    pub fn get(&mut self, w: i32, h: i32) -> Option<&MemoryRenderBuffer> {
        if w <= 0 || h <= 0 {
            return None;
        }
        // `BloomPalette` is `Copy`, so this reads the cached value and does NOT
        // hold the borrow across the compose below.
        let pal = *self.palette.get_or_insert_with(crate::bloom::theme_palette);
        if self.key != Some((w, h, pal)) {
            let started = Instant::now();
            let rgba = crate::bloom::compose(w, h, &pal);
            self.buffer = Some(MemoryRenderBuffer::from_slice(
                &rgba,
                Fourcc::Argb8888,
                (w, h),
                1,
                Transform::Normal,
                None,
            ));
            self.key = Some((w, h, pal));
            info!(
                width = w,
                height = h,
                took_ms = started.elapsed().as_millis() as u64,
                "bloom.composed (native aura backdrop; cached until the mode or theme changes)"
            );
        }
        self.buffer.as_ref()
    }
}

/// The breathing orb: ONE texture, animated by two scalars (NATIVE SHELL M2).
///
/// The bloom is composed once because it never moves. The orb breathes, and the
/// tempting shortcut is to recompose it every frame — which would simply move
/// the browser's cost into Rust — or to cache a ring of animation phases, which
/// quantises a smooth breath into steps and pays memory linear in the step
/// count for an approximation of what the GPU does exactly.
///
/// Neither is what a real compositor does. Core Animation and DWM both
/// rasterise once and then vary cheap per-frame parameters on the GPU. smithay
/// exposes exactly that: `MemoryRenderBufferRenderElement::from_buffer` takes
/// `alpha` and `size`, so one buffer plus two floats per frame gives continuous
/// motion at the display's own rate. Per-frame CPU cost is arithmetic on two
/// scalars. Memory is O(1) rather than O(steps).
#[derive(Default)]
pub struct OrbCache {
    key: Option<(i32, crate::orb::OrbPalette)>,
    buffer: Option<MemoryRenderBuffer>,
    /// When this orb started breathing. Owned HERE rather than passed in, so the
    /// phase is COMPUTED from a clock at the point of use and there is nowhere
    /// to store a "target" value to ease toward — the program's binding rule for
    /// `animated`, which exists because CSS-style easing is what made the orb
    /// drag rubber-band on 2026-07-20.
    epoch: Option<Instant>,
}

impl OrbCache {
    /// The composed orb plus its motion RIGHT NOW.
    ///
    /// Returns the buffer to draw and the (scale, alpha) to draw it with. The
    /// caller hands those straight to the render element, so the CPU never
    /// touches a pixel after the first compose.
    ///
    /// `energy` is the live signal (mic RMS, 0..=1) P2 calls for. It is threaded
    /// through rather than sampled here so this stays a pure cache: the source
    /// of the signal can change without this type changing.
    ///
    /// `None` for a degenerate size, matching BloomCache: the caller then emits
    /// no orb and the frame is the desktop without it, never a panic.
    pub fn current(
        &mut self,
        side: i32,
        energy: f32,
    ) -> Option<(&MemoryRenderBuffer, crate::orb::OrbMotion)> {
        if side <= 0 {
            return None;
        }
        let now = Instant::now();
        let epoch = *self.epoch.get_or_insert(now);
        let motion = crate::orb::motion_at(now.saturating_duration_since(epoch), energy);

        let pal = crate::orb::OrbPalette::default();
        if self.key != Some((side, pal)) {
            let started = Instant::now();
            let rgba = crate::orb::compose(side, &pal);
            self.buffer = Some(MemoryRenderBuffer::from_slice(
                &rgba,
                Fourcc::Argb8888,
                (side, side),
                1,
                Transform::Normal,
                None,
            ));
            self.key = Some((side, pal));
            info!(
                side,
                took_ms = started.elapsed().as_millis() as u64,
                "orb.composed (once; breathing is per-frame scale+alpha on the GPU)"
            );
        }
        self.buffer.as_ref().map(|b| (b, motion))
    }
}

/// Rasterize a rounded rectangle of `color` into a premultiplied [B,G,R,A] buffer,
/// anti-aliased at the corners via a rounded-box signed-distance field. The scene
/// carries a `radius` on the card / omnibox rects that a `SolidColorRenderElement`
/// (always a hard quad) cannot express, so those rects lower through a cached
/// MemoryRenderBuffer of THIS shape instead. Byte order + premultiply match
/// text_render.rs and bloom.rs (Argb8888 little-endian = B,G,R,A, premultiplied).
fn rounded_rect_rgba(w: u32, h: u32, radius: f32, color: [f32; 4]) -> Vec<u8> {
    let mut rgba = vec![0u8; (w * h * 4) as usize];
    let hw = w as f32 / 2.0;
    let hh = h as f32 / 2.0;
    // A radius past half the short side is just a fuller pill / circle.
    let r = radius.clamp(0.0, hw.min(hh));
    let ca = color[3].clamp(0.0, 1.0);
    let cr = color[0].clamp(0.0, 1.0);
    let cg = color[1].clamp(0.0, 1.0);
    let cb = color[2].clamp(0.0, 1.0);
    for y in 0..h {
        for x in 0..w {
            // Pixel centre relative to the rect centre.
            let px = x as f32 + 0.5 - hw;
            let py = y as f32 + 0.5 - hh;
            // Rounded-box SDF (<=0 inside): distance to the shape's edge.
            let qx = px.abs() - (hw - r);
            let qy = py.abs() - (hh - r);
            let dist =
                (qx.max(0.0).powi(2) + qy.max(0.0).powi(2)).sqrt() + qx.max(qy).min(0.0) - r;
            // ~1px anti-aliased coverage across the edge.
            let cov = (0.5 - dist).clamp(0.0, 1.0);
            if cov <= 0.0 {
                continue;
            }
            let a = ca * cov;
            let idx = ((y * w + x) * 4) as usize;
            rgba[idx] = (cb * a * 255.0) as u8;
            rgba[idx + 1] = (cg * a * 255.0) as u8;
            rgba[idx + 2] = (cr * a * 255.0) as u8;
            rgba[idx + 3] = (a * 255.0) as u8;
        }
    }
    rgba
}

/// Caches rounded-rect buffers keyed by (size, radius, colour) so the per-pixel SDF
/// rasterization runs ONCE per unique rect (cards are a single size), never per
/// frame. Mirrors OrbCache / TextRasterizer: compose once, reuse the buffer, so the
/// per-frame cost of a rounded panel is a GPU blit, not a CPU rasterize.
#[derive(Default)]
pub struct RectCache {
    cache: std::collections::HashMap<(u32, u32, u32, u32, u32, u32, u32), MemoryRenderBuffer>,
}

impl RectCache {
    /// The rounded-rect buffer for these dims / radius / colour, composed on first
    /// use. `None` for a degenerate size (the caller then draws nothing for it).
    pub fn rounded(
        &mut self,
        w: i32,
        h: i32,
        radius: f32,
        color: [f32; 4],
    ) -> Option<&MemoryRenderBuffer> {
        if w < 1 || h < 1 {
            return None;
        }
        let key = (
            w as u32,
            h as u32,
            radius.to_bits(),
            color[0].to_bits(),
            color[1].to_bits(),
            color[2].to_bits(),
            color[3].to_bits(),
        );
        if !self.cache.contains_key(&key) {
            let rgba = rounded_rect_rgba(w as u32, h as u32, radius, color);
            let buf = MemoryRenderBuffer::from_slice(
                &rgba,
                Fourcc::Argb8888,
                (w, h),
                1,
                Transform::Normal,
                None,
            );
            self.cache.insert(key, buf);
        }
        self.cache.get(&key)
    }
}

// ════════════════════════════════════════════════════════════════════════════
// THE `CompState` trait — the backend-agnostic accessor surface the shared WM brain
// drives. Each backend's concrete `State` impls it by handing back references to the
// fields it already holds. The supertrait `SeatHandler<KeyboardFocus = WlSurface,
// PointerFocus = WlSurface>` is what makes `keyboard.set_focus(state, …)` /
// `pointer.motion(state, …)` type-check generically (the seat is `…Handle<Self>`).
// ════════════════════════════════════════════════════════════════════════════

/// The shared compositor brain's view of a backend `State`. ONE implementation of the
/// WM/IPC/input/workspace/cursor/killswitch logic acts through these accessors, so the
/// winit + DRM backends share it without a parallel path.
pub trait CompState:
    SeatHandler<KeyboardFocus = WlSurface, PointerFocus = WlSurface> + Sized + 'static
{
    // ── window tree + the no-phantom-window registry ──
    fn space(&self) -> &Space<Window>;
    fn space_mut(&mut self) -> &mut Space<Window>;

    // ── the seat handles (cloned by callers before re-borrowing `self`) ──
    fn keyboard(&self) -> &smithay::input::keyboard::KeyboardHandle<Self>;
    fn pointer(&self) -> &smithay::input::pointer::PointerHandle<Self>;

    // ── the single output (the winit window / the DRM connector) ──
    fn output(&self) -> &Output;

    // ── XWayland WM handle (None until XWaylandEvent::Ready) ──
    fn xwm_mut(&mut self) -> &mut Option<X11Wm>;

    // ── M3 cascade placement cursor ──
    fn next_window_loc(&self) -> Point<i32, Logical>;
    fn set_next_window_loc(&mut self, loc: Point<i32, Logical>);

    // ── M5 workspaces ──
    fn active_workspace(&self) -> usize;
    fn set_active_workspace(&mut self, n: usize);
    fn hidden_windows(&self) -> &[HiddenWindow];
    fn hidden_windows_mut(&mut self) -> &mut Vec<HiddenWindow>;
    fn desktop_shown(&self) -> bool;
    fn set_desktop_shown(&mut self, on: bool);

    // ── M5 keycode suppression (intercepted-chord release swallow) ──
    fn suppressed_keys_mut(&mut self) -> &mut Vec<Keycode>;

    // ── M6 software cursor (the `cursor_image` SeatHandler callback sets the status
    //    field on each backend directly; the shared render path only READS it) ──
    fn cursor_status(&self) -> &CursorImageStatus;
    fn cursor_buffer(&self) -> &MemoryRenderBuffer;
    fn cursor_hotspot(&self) -> Point<i32, Logical>;

    // ── M6 effects clocks ──
    fn ws_switch_at(&self) -> Option<Instant>;
    fn set_ws_switch_at(&mut self, at: Option<Instant>);

    // ── M6 killswitch ──
    fn capture_blocked(&self) -> bool;
    fn set_capture_blocked_flag(&mut self, on: bool);
    fn black_buffer_mut(&mut self) -> &mut SolidColorBuffer;

    // ── NATIVE SHELL M1: the composed aura backdrop (see BloomCache). ──
    fn bloom_mut(&mut self) -> &mut BloomCache;

    // ── NATIVE SHELL M2: the breathing voice orb (see OrbCache). ──
    fn orb_mut(&mut self) -> &mut OrbCache;

    /// The orb's live energy signal, 0..=1 (mic RMS while listening/speaking).
    ///
    /// Default 0.0 = resting breath, so a backend that has not wired voice yet
    /// still gets a correct, calm orb rather than no orb. The DRM backend
    /// overrides this once the voice IPC lands (M5); until then the orb breathes
    /// on the clock alone, which is exactly P2's resting behaviour.
    fn orb_energy(&self) -> f32 {
        0.0
    }

    /// Toggle the screen kill-switch (the `screen.kill` IPC verb's executor). Default
    /// = the shared flag-flip + log. The winit backend OVERRIDES it to ALSO fail any
    /// in-flight screencopy frames (it owns the read-back queue); the DRM backend has no
    /// screencopy queue yet, so it uses this default. Returns the new state.
    fn set_capture_blocked(&mut self, on: bool) -> bool {
        set_capture_blocked_shared(self, on)
    }

    /// NATIVE SHELL M3: is the native scene render path on? Default OFF, so the
    /// WebView shell stays the desktop and this is a pure additive test path with no
    /// regression. The DRM backend flips it from the HART_NATIVE_SHELL env at session
    /// start (no nix option shipped until M6). Read once per frame; cheap bool.
    fn native_shell_on(&self) -> bool {
        false
    }

    /// NATIVE SHELL M3: the latest home_compose scene pushed over the `shell.compose`
    /// IPC verb, or None to fall back to the demo scene. Default None / no-op setter,
    /// so only the DRM backend stores it (winit dev build uses the demo).
    fn native_home(&self) -> Option<&crate::scene::HomeCompose> {
        None
    }
    fn set_native_home(&mut self, _home: crate::scene::HomeCompose) {}

    /// NATIVE SHELL M3 text: the cosmic-text rasterizer, held on State so
    /// FontSystem::new() (font enumeration) runs ONCE, not per frame. Required so
    /// every backend that can render the native scene supplies one.
    fn text_rasterizer_mut(&mut self) -> &mut crate::text_render::TextRasterizer;

    /// Both native-scene caches at once, as DISJOINT field borrows. `lower_scene`
    /// walks one leaf list that interleaves Text runs (rasterizer) and OrbSlots
    /// (orb cache), so it must hold BOTH `&mut` across the loop — which the separate
    /// `text_rasterizer_mut`/`orb_mut` accessors cannot give (each borrows all of
    /// `self`). One combined accessor split-borrows the two fields, so the lowering
    /// stays a SINGLE path shared by `render_native_scene` (via State) and its test
    /// (via directly-constructed caches). Required so every native-capable backend
    /// supplies both.
    fn native_scene_caches(
        &mut self,
    ) -> (
        &mut crate::text_render::TextRasterizer,
        &mut OrbCache,
        &mut RectCache,
    );

    // ── IPC event fan-out (window.opened/closed/focused…). The winit backend pushes
    //    framed JSON to its `IpcState` subscribers; the DRM backend logs the edge.
    //    The shared WM edges call this so the event surface is identical on both. ──
    fn emit_window_event(&mut self, event: &str, window: &Window, handle: &str);

    // ── the no-phantom-window registry bridge. The pure `WindowRegistry` lives in
    //    main.rs but each backend stores it on its OWN State field (winit: `windows`;
    //    DRM: `windows` on wayland::State). `registry_on_unmap` lets the shared
    //    hidden-workspace destroy invalidate a handle without knowing the field. ──
    fn registry_on_unmap(&mut self, handle: &WindowHandle) -> bool;

    // ── the calloop loop handle + the com.hart.Compositor IPC server state. BOTH
    //    backends run one calloop loop over their own concrete `State`, so the IPC
    //    socket transport (ipc.rs) registers per-connection sources via this handle
    //    and fans events out through this `IpcState`. Exposed on the trait so the
    //    framed-JSON server is ONE implementation serving both backends (the moat on
    //    real hardware too), not a winit-only path. ──
    fn loop_handle(&self) -> &smithay::reexports::calloop::LoopHandle<'static, Self>;
    fn ipc_state_mut(&mut self) -> &mut crate::ipc::IpcState;
}

// ════════════════════════════════════════════════════════════════════════════
// THE shared WM brain — every method below was an `impl winit::State` method or an
// `impl ipc::State` method, hoisted VERBATIM (behaviour-preserving) to act on
// `&mut S` where `S: CompState`. The two backends call these via their own State.
// ════════════════════════════════════════════════════════════════════════════

/// Reverse-lookup the live `Window` whose ROOT surface is `surface` (mirrors anvil's
/// `window_for_surface`). `Window::wl_surface()` comes from `WaylandFocus`.
pub fn window_for_surface<S: CompState>(state: &S, surface: &WlSurface) -> Option<Window> {
    state
        .space()
        .elements()
        .find(|w| w.wl_surface().map(|s| &*s == surface).unwrap_or(false))
        .cloned()
}

/// Cascade the next toplevel's initial position so multiple windows don't fully
/// overlap. Advances a diagonal cursor, wrapping near the origin before it walks off
/// the bottom-right. Pure placement policy.
pub fn next_cascade_loc<S: CompState>(state: &mut S) -> Point<i32, Logical> {
    const STEP_X: i32 = 230;
    const STEP_Y: i32 = 150;
    const MARGIN: i32 = 16;
    // Cascade inside the WORK AREA, never the raw output: the origin the cascade
    // resets to has to sit below whatever chrome the shell reserved, or every
    // fresh window opens underneath the taskbar.
    let (ax, ay, aw, ah) = work_area_for(state).unwrap_or((0, 0, 1280, 800));
    // Clamp the location we hand OUT too, so even the first window -- whose
    // stored location predates any reservation -- lands below the chrome.
    let stored = state.next_window_loc();
    let loc = Point::from((stored.x.max(ax), stored.y.max(ay)));
    let mut next = Point::from((loc.x + STEP_X, loc.y + STEP_Y));
    if next.x + 200 > ax + aw || next.y + 150 > ay + ah {
        next = Point::from((ax + MARGIN, ay + MARGIN));
    }
    state.set_next_window_loc(next);
    loc
}

// ── PANEL RESERVATION ────────────────────────────────────────────────────────
// The HART shell is a SINGLE fullscreen layer surface on the Background layer,
// with its taskbar painted inside it (Z-ORDER MODEL 1 in hart-layer-shell-host.nix
// -- one WebView, so the shell JS keeps its window.* globals). The consequence the
// user hit on 2026-08-29: a window maximizes over the whole output and the bar it
// covers is unreachable, because the bar is not a surface of its own that could
// claim an exclusive zone.
//
// So the compositor reserves the strip on the shell's behalf. Every window-placement
// path resolves its rect through `work_area_for` instead of the output geometry,
// which means maximize, all nine snap zones, all five tiling layouts and the
// new-window cascade honour the bar from ONE definition rather than four.

/// Where the shell publishes how much chrome it owns, in logical pixels per edge.
///
/// The CSS that draws the bars is the only thing that knows their size, so the
/// compositor ASKS rather than hardcoding numbers that would silently drift the
/// first time either bar is restyled. Same runtime-file contract as the
/// native-chrome bridge next door, for the same reason: it is the channel that
/// already crosses from the shell to the compositor.
///
/// Format is one `key=pixels` per line, unknown keys ignored, so left/right docks
/// can be added later without a flag day:
///     top=40
///     bottom=44
pub const PANEL_RESERVATION_PATH: &str = "/run/hart/session/panel-reservation";

/// Logical pixels of chrome the shell reserves on each edge. The HART shell has
/// TWO: a 40px top bar (Home / Agents / Apps / Hive / Earn) and a 44px bottom
/// taskbar, both painted inside the one Background layer surface.
#[derive(Debug, Clone, Copy, Default, PartialEq, Eq)]
pub struct PanelReservation {
    pub top: i32,
    pub bottom: i32,
}

/// Parse a published reservation. PURE, so every fail-safe path below is testable.
///
/// FAIL-SAFE TO ZERO, per field. A junk line, a negative number, an unknown key --
/// each is skipped and leaves that edge unreserved. Zero everywhere reproduces the
/// pre-existing layout exactly, which is what lets the compositor half of this ship
/// inert before the shell half exists.
pub fn parse_panel_reservation(text: &str) -> PanelReservation {
    let mut r = PanelReservation::default();
    for line in text.lines() {
        let (key, value) = match line.split_once('=') {
            Some(kv) => kv,
            None => continue,
        };
        let v = match value.trim().parse::<i32>() {
            Ok(v) if v > 0 => v,
            _ => continue,
        };
        match key.trim() {
            "top" => r.top = v,
            "bottom" => r.bottom = v,
            _ => {} // forward-compatible: an older compositor ignores newer edges
        }
    }
    r
}

/// The reservation the shell has published, or none at all.
pub fn panel_reservation() -> PanelReservation {
    std::fs::read_to_string(PANEL_RESERVATION_PATH)
        .map(|s| parse_panel_reservation(&s))
        .unwrap_or_default()
}

/// The output rect MINUS the reserved chrome: the region windows may occupy.
/// PURE, so the clamping below is unit-testable without a mapped output.
///
/// The reservation is CAPPED AT HALF the output height, and when both edges
/// together exceed that they are scaled down in proportion rather than one edge
/// winning. These numbers cross a process boundary as a text file, and a corrupt or
/// absurd value must not be able to squeeze the usable area to nothing: a desktop
/// with no room for windows is a far worse failure than a bar that overlaps one.
pub fn work_area(
    ox: i32, oy: i32, ow: i32, oh: i32, reserved: PanelReservation,
) -> (i32, i32, i32, i32) {
    let cap = (oh / 2).max(0);
    let top = reserved.top.clamp(0, cap);
    let bottom = reserved.bottom.clamp(0, cap);
    let total = top + bottom;
    let (top, bottom) = if total > cap && total > 0 {
        let t = top * cap / total;
        (t, cap - t)
    } else {
        (top, bottom)
    };
    (ox, oy + top, ow, oh - top - bottom)
}

/// The live work area. THE single place window placement learns where it may lay
/// things out; `output_geometry` must not be read directly for that purpose again.
pub fn work_area_for<S: CompState>(state: &S) -> Option<(i32, i32, i32, i32)> {
    let g = state.space().output_geometry(state.output())?;
    Some(work_area(g.loc.x, g.loc.y, g.size.w, g.size.h, panel_reservation()))
}

/// The current output size in PHYSICAL (framebuffer) pixels. Screencopy reports this
/// as the capturable region; the killswitch + cursor math also use it.
pub fn output_physical_size<S: CompState>(state: &S) -> Size<i32, Physical> {
    state
        .output()
        .current_mode()
        .map(|m| m.size)
        .unwrap_or_else(|| (1280, 800).into())
}

// ════════════════════════════════════════════════════════════════════════════
// PURE screencopy region/time math (M6, hoisted from screencopy.rs). These are the
// framebuffer read-back's geometry helpers — region clamping (the no-out-of-bounds
// gate), the output-transform region map (upright capture), and the wall-clock split
// for the `ready` presentation timestamp. They touch NO renderer / wl_buffer / live
// State — just i32 region arithmetic + the system clock — so they live HERE under the
// shared `any(winit, smithay)` cfg (NOT in screencopy.rs's winit-only `#![cfg]`). That
// is load-bearing: hart-comp.nix's `doCheck` runs `cargo test --features smithay`, which
// does NOT compile screencopy.rs (`#![cfg(feature = "winit")]`); hoisting these here is
// what lets the smithay build's check exercise their unit floor (the tests are alongside
// in this module's #[cfg(test)] block). screencopy.rs CALLS these (one source of truth,
// no parallel path).
// ════════════════════════════════════════════════════════════════════════════

/// Clamp a client-requested `CaptureOutputRegion` rect to the output bounds, so a
/// client can never read outside the framebuffer. PURE region math (no renderer / no
/// Smithay state): the requested `(x, y, width, height)` is clamped against the output's
/// `(out_w, out_h)` — extracted so the clamp is one source of truth AND unit-testable
/// without a live output.
///
/// Invariants the clamp guarantees: origin in `[0, out]`, width/height ≥ 1, and the rect
/// never extends past the right/bottom edge (`rx + rw ≤ out_w`, `ry + rh ≤ out_h`). The
/// width/height floor is a hard `.max(1)` AFTER the right-edge clamp, so even a 0-sized
/// output (`out_w == 0`) yields a degenerate-but-valid 1px rect rather than an empty
/// read-back the ExportMem contract rejects.
pub fn clamp_region(
    x: i32,
    y: i32,
    width: i32,
    height: i32,
    out_w: i32,
    out_h: i32,
) -> Rectangle<i32, BufferCoord> {
    let rx = x.max(0).min(out_w);
    let ry = y.max(0).min(out_h);
    // Trim to the remaining span, then floor to 1px: `(out_w - rx)` can be 0 (origin at
    // the far edge, or a 0-wide output), and the read-back invariant is width/height ≥ 1.
    let rw = width.max(1).min(out_w - rx).max(1);
    let rh = height.max(1).min(out_h - ry).max(1);
    Rectangle::new((rx, ry).into(), (rw, rh).into())
}

/// Map a logical capture region to the physical framebuffer rectangle under the
/// output's render transform, so a read-back of the raw framebuffer yields an upright
/// image. Smithay's `Transform::transform_rect_in(rect, area_size)` is the canonical
/// helper (the same one the renderer uses to place elements).
pub fn transform_region(
    region: Rectangle<i32, BufferCoord>,
    output_size: Size<i32, Physical>,
    transform: Transform,
) -> Rectangle<i32, BufferCoord> {
    let area: Size<i32, BufferCoord> = (output_size.w, output_size.h).into();
    transform.transform_rect_in(region, &area)
}

/// Wall-clock split into (whole seconds u64, sub-second nanoseconds u32) for the
/// `ready` presentation timestamp. CLOCK_REALTIME is fine here — grim only logs it.
pub fn now_secs_nsecs() -> (u64, u32) {
    use std::time::{SystemTime, UNIX_EPOCH};
    match SystemTime::now().duration_since(UNIX_EPOCH) {
        Ok(d) => (d.as_secs(), d.subsec_nanos()),
        Err(err) => {
            debug!(?err, "now_secs_nsecs: system clock is before UNIX_EPOCH; using (0, 0)");
            (0, 0)
        }
    }
}

/// The screen kill-switch toggle (the `CompState::set_capture_blocked` default body).
/// Flips the one flag that drives all three effects (black surface ABOVE everything +
/// input not forwarded + screencopy refused). Returns the new state. Failing in-flight
/// screencopy frames is the BACKEND's job (it owns the queue) — the winit override does
/// that after calling this (it is named distinctly from the trait method to avoid the
/// default recursing into itself).
pub fn set_capture_blocked_shared<S: CompState>(state: &mut S, on: bool) -> bool {
    if state.capture_blocked() != on {
        state.set_capture_blocked_flag(on);
        info!(blocked = on, "screen.kill — capture/input/screencopy gate toggled");
    }
    state.capture_blocked()
}

// ── input routing (keyboard focus + pointer hit-test + click-to-focus) ──

/// Hit-test the surface under `pos` for POINTER focus, honouring z-order:
/// Overlay/Top layer surfaces first, then mapped toplevels (newest on top), then
/// Bottom/Background layer surfaces. Returns the bare `WlSurface` + the surface-local
/// origin the pointer handle wants. Modelled on anvil's `surface_under`.
pub fn surface_under<S: CompState>(
    state: &S,
    pos: Point<f64, Logical>,
) -> Option<(WlSurface, Point<f64, Logical>)> {
    let output = state.output();
    let output_geo = state.space().output_geometry(output)?;
    let layers = layer_map_for_output(output);

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

    if let Some((window, win_loc)) = state.space().element_under(pos) {
        if let Some((surface, surf_loc)) =
            window.surface_under(pos - win_loc.to_f64(), WindowSurfaceType::ALL)
        {
            return Some((surface, (surf_loc + win_loc).to_f64()));
        }
    }

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
/// clicked toplevel to the top of the stack (click-to-focus + raise). Modelled on
/// anvil's `update_keyboard_focus`.
pub fn update_keyboard_focus<S: CompState>(state: &mut S, pos: Point<f64, Logical>, serial: Serial) {
    let keyboard = state.keyboard().clone();
    if state.pointer().is_grabbed() || keyboard.is_grabbed() {
        return;
    }

    if let Some((window, _)) = state.space().element_under(pos).map(|(w, l)| (w.clone(), l)) {
        state.space_mut().raise_element(&window, true);
        if let Some(x11) = window.x11_surface() {
            if let Some(xwm) = state.xwm_mut().as_mut() {
                if let Err(err) = xwm.raise_window(x11) {
                    warn!(?err, "focus: X11Wm::raise_window failed");
                }
            }
        }
        let surface = window.wl_surface().map(|s| s.into_owned());
        keyboard.set_focus(state, surface, serial);
        return;
    }

    let output = state.output().clone();
    let layers = layer_map_for_output(&output);
    // Overlay / Top layer surfaces (panels, popups) win the click over the desktop.
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
                keyboard.set_focus(state, Some(layer.wl_surface().clone()), serial);
                return;
            }
        }
    }
    // #134 — Bottom / Background fallback: a click on the desktop glass shell (a BACKGROUND
    // wlr-layer-shell surface with OnDemand keyboard interactivity) re-focuses it once
    // focus has drifted to a toplevel, so the user can always type back into the shell.
    // Mirrors anvil's `update_keyboard_focus` Bottom/Background tail (a parity gap before
    // this). Reached only when no toplevel and no Overlay/Top surface was under the click.
    if let Some(layer) = layers
        .layer_under(WlrLayer::Bottom, pos)
        .or_else(|| layers.layer_under(WlrLayer::Background, pos))
    {
        if layer.can_receive_keyboard_focus() {
            let layer_loc = layers.layer_geometry(layer).map(|g| g.loc).unwrap_or_default();
            if layer
                .surface_under(pos - layer_loc.to_f64(), WindowSurfaceType::ALL)
                .is_some()
            {
                keyboard.set_focus(state, Some(layer.wl_surface().clone()), serial);
            }
        }
    }
}

/// THE #134 keyboard-focus-on-map for the desktop glass shell. The HART glass shell maps
/// as a BACKGROUND wlr-layer-shell surface with `OnDemand` keyboard interactivity, so on a
/// fresh boot the compositor never hands it the keyboard: there is no toplevel and no click
/// yet (and the pointer itself may be a fresh-boot casualty). The result is the #134
/// symptom's keyboard half — a painted desktop that cannot be typed into. This grants the
/// keyboard to a committed layer surface that (a) can receive keyboard focus
/// (Exclusive/OnDemand) and (b) is the mapped layer surface for `surface`, but ONLY while
/// nothing else holds focus. That guard makes it safe to call on every commit: it never
/// steals the keyboard from a focused toplevel, and it naturally re-homes focus to the
/// desktop whenever a toplevel closes and leaves focus idle (smithay clears focus when the
/// focused surface dies). Idempotent — once focus is set, `current_focus().is_some()` makes
/// every later call a single cheap check. Returns whether focus was granted (the test seam).
pub fn focus_desktop_shell_if_idle<S: CompState>(
    state: &mut S,
    surface: &WlSurface,
    serial: Serial,
) -> bool {
    // Never steal focus from a focused toplevel / an already-focused shell.
    if state.keyboard().current_focus().is_some() {
        return false;
    }
    let output = state.output().clone();
    // Resolve the MAPPED layer surface for this committed wl_surface (TOPLEVEL role only —
    // a subsurface/popup commit returns None and is ignored). Clone it so the LayerMap
    // borrow is dropped before `set_focus` re-borrows `state`.
    let layer = {
        let map = layer_map_for_output(&output);
        map.layer_for_surface(surface, WindowSurfaceType::TOPLEVEL).cloned()
    };
    let layer = match layer {
        Some(l) => l,
        None => return false,
    };
    if !layer.can_receive_keyboard_focus() {
        return false;
    }
    let keyboard = state.keyboard().clone();
    keyboard.set_focus(state, Some(surface.clone()), serial);
    true
}

/// Route a single input event into the seat. Handles the events BOTH backends emit:
/// keyboard, RELATIVE pointer motion (real-HW touchpad/mouse via libinput), ABSOLUTE
/// pointer motion (winit/tablet), button, axis. M6 screen kill-switch: while the human
/// has cut `screen`, do NOT forward ANY input to clients.
///
/// #134 — the `PointerMotion` (relative) arm is THE real-hardware pointer fix: libinput
/// emits relative motion for touchpads + mice, and before this arm existed every such
/// event hit the `_ => {}` sink, so the cursor was frozen at (0,0) on a real boot while
/// the shell still painted. The winit backend only ever emits the absolute variant, so a
/// winit-only test could never surface the regression (the gap the #134 symptom exposed).
pub fn process_input_event<S: CompState, B: InputBackend>(state: &mut S, event: InputEvent<B>) {
    // #134/#128 observability — the FIRST real seat event (pointer OR keyboard) proves the
    // libinput → Seat delivery path is live. A painted-but-input-dead Tier-1 is invisible
    // to the paint-only watchdog (it reads HEALTHY off the shell-ready marker), so emit a
    // one-shot liveness signal here. Done BEFORE the kill-switch gate: a delivered-then-
    // blocked event still proves the seat is alive. Matches by reference so `event` is not
    // consumed before the real routing below.
    // T_input capture for the input-to-photon instrument (latency.rs, harness
    // M0). Same by-reference match as the liveness beacon and BEFORE the
    // kill-switch gate for the same reason: a delivered-then-blocked event
    // still carries a true kernel timestamp, and refusing to record it would
    // bias the estimator toward busy periods. `Event::time()` is libinput's
    // CLOCK_MONOTONIC microseconds — the kernel stamp, taken before any of
    // our code ran, which is the entire point of the instrument.
    match &event {
        InputEvent::Keyboard { event } => {
            crate::latency::on_input(crate::latency::Kind::Key, event.time());
            note_input_alive();
        }
        InputEvent::PointerMotion { event } => {
            crate::latency::on_motion(event.time());
            note_input_alive();
        }
        InputEvent::PointerMotionAbsolute { event } => {
            crate::latency::on_motion(event.time());
            note_input_alive();
        }
        InputEvent::PointerButton { event } => {
            crate::latency::on_button(
                event.state() == ButtonState::Pressed,
                event.time(),
            );
            note_input_alive();
        }
        InputEvent::PointerAxis { event } => {
            crate::latency::on_input(crate::latency::Kind::Scroll, event.time());
            note_input_alive();
        }
        _ => {}
    }
    if state.capture_blocked() {
        return;
    }
    match event {
        InputEvent::Keyboard { event } => on_keyboard_key::<S, B>(state, event),
        InputEvent::PointerMotion { event } => on_pointer_move_relative::<S, B>(state, event),
        InputEvent::PointerMotionAbsolute { event } => {
            on_pointer_move_absolute::<S, B>(state, event)
        }
        InputEvent::PointerButton { event } => on_pointer_button::<S, B>(state, event),
        InputEvent::PointerAxis { event } => on_pointer_axis::<S, B>(state, event),
        _ => {}
    }
}

/// One-shot input-liveness beacon (#134/#128). On the FIRST real pointer/keyboard event,
/// log a journal line and best-effort touch `/run/hart/session/input-alive` — the marker
/// the out-of-process session supervisor / HARTLOG can later read to tell a
/// painted-but-input-starved boot (HEALTHY paint, dead seat) apart from a working desktop,
/// and drop a tier next time. The flag is a single relaxed atomic: the marker write fires
/// exactly once and every later event is one atomic load. The file write is best-effort
/// (a missing `/run/hart/session` dir on the dev box, or a read-only FS, just leaves the
/// journal line as the signal); it never blocks and never aborts the compositor.
fn note_input_alive() {
    use std::sync::atomic::{AtomicBool, Ordering};
    static INPUT_SEEN: AtomicBool = AtomicBool::new(false);
    if INPUT_SEEN.swap(true, Ordering::Relaxed) {
        return;
    }
    info!("hart-comp: first seat input delivered — libinput/Seat path is LIVE (#134 liveness beacon)");
    if let Err(err) = std::fs::write("/run/hart/session/input-alive", b"1\n") {
        debug!(?err, "note_input_alive: could not write the input-alive marker (the journal line above is the primary signal)");
    }
}

/// Intercept compositor keyboard shortcuts BEFORE forwarding to the focused client;
/// forward everything else. Modelled 1:1 on anvil's `keyboard_key_to_action`. The chord
/// is swallowed (press Intercept + release suppressed via `suppressed_keys`); the
/// resolved action is executed AFTER `input()` returns (outside the closure).
pub fn on_keyboard_key<S: CompState, B: InputBackend>(state: &mut S, evt: B::KeyboardKeyEvent) {
    let serial = SERIAL_COUNTER.next_serial();
    let time = evt.time_msec();
    let code = evt.key_code();
    let key_state = evt.state();
    let keyboard = state.keyboard().clone();

    let mut suppressed = state.suppressed_keys_mut().clone();
    let action: Option<WmAction> = keyboard
        .input::<Option<WmAction>, _>(state, code, key_state, serial, time, |_, modifiers, handle| {
            let keysym = handle.modified_sym();
            let digit_sym = handle.raw_latin_sym_or_raw_current_sym();
            if std::env::var_os("HART_COMP_DEBUG_KEYS").is_some() {
                info!(
                    ?key_state,
                    logo = modifiers.logo, alt = modifiers.alt, shift = modifiers.shift,
                    raw = keysym.raw(),
                    digit = digit_sym.map(|s| s.raw()),
                    keycode = code.raw(),
                    "key.seen"
                );
            }
            if key_state == KeyState::Pressed {
                match process_keyboard_shortcut(*modifiers, keysym, digit_sym) {
                    Some(act) => {
                        suppressed.push(code);
                        FilterResult::Intercept(Some(act))
                    }
                    None => FilterResult::Forward,
                }
            } else if suppressed.contains(&code) {
                suppressed.retain(|k| *k != code);
                FilterResult::Intercept(None)
            } else {
                FilterResult::Forward
            }
        })
        .flatten();
    *state.suppressed_keys_mut() = suppressed;

    if let Some(act) = action {
        apply_wm_action(state, act, serial);
    }
}

/// Execute a resolved `WmAction`. Each arm calls an EXISTING shared helper so there is
/// no new geometry/focus code path — the chords are a second TRIGGER for the same verbs
/// the IPC drives.
pub fn apply_wm_action<S: CompState>(state: &mut S, action: WmAction, serial: Serial) {
    match action {
        WmAction::CycleFocus => cycle_focus(state, true, serial),
        WmAction::CycleFocusBack => cycle_focus(state, false, serial),
        WmAction::SwitchWorkspace(n) => {
            let _ = switch_workspace(state, n);
        }
        WmAction::MoveToWorkspace(n) => {
            let _ = move_focused_to_workspace(state, n);
        }
        WmAction::CloseFocused => close_focused_window(state),
        WmAction::SnapLeft => snap_focused(state, "left-half"),
        WmAction::SnapRight => snap_focused(state, "right-half"),
        WmAction::Maximize => snap_focused(state, "maximize"),
        WmAction::RestoreWindow => restore_focused_window(state),
        WmAction::ShowDesktop => toggle_show_desktop(state),
    }
}

/// The currently keyboard-focused mapped `Window`, resolved via the seat's current
/// focus surface (walking to the root).
pub fn focused_window<S: CompState>(state: &S) -> Option<Window> {
    let focus = state.keyboard().current_focus()?;
    let mut root = focus.clone();
    while let Some(parent) = get_parent(&root) {
        root = parent;
    }
    window_for_surface(state, &root)
}

/// Alt+Tab focus cycle (stack-order rotation). FORWARD raises the BOTTOM-most window;
/// BACKWARD raises the one just below the top. Identical body to `ipc_focus_window`.
pub fn cycle_focus<S: CompState>(state: &mut S, forward: bool, serial: Serial) {
    let order: Vec<Window> = state.space().elements().cloned().collect();
    let n = order.len();
    if n < 2 {
        return;
    }
    let next_idx = if forward { 0 } else { n - 2 };
    let target = order[next_idx].clone();
    state.space_mut().raise_element(&target, true);
    if let Some(x11) = target.x11_surface() {
        if let Some(xwm) = state.xwm_mut().as_mut() {
            if let Err(err) = xwm.raise_window(x11) {
                warn!(?err, "focus cycle: X11Wm::raise_window failed");
            }
        }
    }
    let surface = target.wl_surface().map(|s| s.into_owned());
    let keyboard = state.keyboard().clone();
    keyboard.set_focus(state, surface, serial);
    if let Some(handle) = target.user_data().get::<WindowHandle>().map(|h| h.as_str().to_string()) {
        state.emit_window_event("window.focused", &target, &handle);
    }
}

/// Super+Q — close the focused toplevel (reuses the IPC `ipc_close_window` body).
pub fn close_focused_window<S: CompState>(state: &mut S) {
    if let Some(window) = focused_window(state) {
        if let Some(handle) = window.user_data().get::<WindowHandle>().map(|h| h.as_str().to_string()) {
            ipc_close_window(state, &handle);
        }
    }
}

/// Super+Left/Right/Up — snap the focused window to a named zone. Stashes the PRE-snap
/// geometry the FIRST time so Super+Down can restore it, then reuses `ipc_zone_rect` +
/// `ipc_place_window`.
pub fn snap_focused<S: CompState>(state: &mut S, zone: &str) {
    let window = match focused_window(state) {
        Some(w) => w,
        None => return,
    };
    let handle = match window.user_data().get::<WindowHandle>().map(|h| h.as_str().to_string()) {
        Some(h) => h,
        None => return,
    };
    if let Some(cur) = state.space().element_geometry(&window) {
        window
            .user_data()
            .insert_if_missing(|| PreSnapGeom(Cell::new(None)));
        if let Some(stash) = window.user_data().get::<PreSnapGeom>() {
            if stash.0.get().is_none() {
                stash.0.set(Some(cur));
            }
        }
    }
    if let Some((x, y, w, h)) = ipc_zone_rect(state, zone) {
        ipc_place_window(state, &handle, x, y, w, h);
    }
}

/// Super+Down — restore the focused window to its stashed pre-snap geometry, or a
/// centered 60% default if it was never snapped. Reuses `ipc_place_window`.
pub fn restore_focused_window<S: CompState>(state: &mut S) {
    let window = match focused_window(state) {
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
        .and_then(|s| s.0.take());
    let (x, y, w, h) = match stashed {
        Some(g) => (g.loc.x, g.loc.y, g.size.w, g.size.h),
        // Centred 60% of the WORK AREA, so an un-snapped restore also lands clear
        // of the reserved chrome.
        None => match work_area_for(state) {
            Some((ax, ay, aw, ah)) => {
                let w = aw * 3 / 5;
                let h = ah * 3 / 5;
                (ax + (aw - w) / 2, ay + (ah - h) / 2, w, h)
            }
            None => return,
        },
    };
    ipc_place_window(state, &handle, x, y, w, h);
}

// ── M5 workspaces ──

/// Tag a freshly-mapped window with the active workspace (called from the map edges).
/// Idempotent: only sets it once.
pub fn tag_window_workspace<S: CompState>(state: &S, window: &Window) {
    let ws = state.active_workspace();
    window
        .user_data()
        .insert_if_missing(|| WorkspaceTag(Cell::new(ws)));
}

/// Read a window's workspace tag (0 if somehow untagged — the first workspace).
pub fn window_workspace(window: &Window) -> usize {
    window
        .user_data()
        .get::<WorkspaceTag>()
        .map(|t| t.0.get())
        .unwrap_or(0)
}

/// Super+1..9 / `workspace.switch(n)` — show workspace `n`. Stashes the visible set into
/// `hidden_windows`, restores every held window tagged `n`, focuses the top. No-op if
/// already on `n`. Returns true if the active workspace changed.
pub fn switch_workspace<S: CompState>(state: &mut S, n: usize) -> bool {
    if n == state.active_workspace() {
        return false;
    }
    let leaving = state.active_workspace();
    let active: Vec<Window> = state.space().elements().cloned().collect();
    for window in active {
        let loc = state.space().element_location(&window).unwrap_or_default();
        window
            .user_data()
            .insert_if_missing(|| WorkspaceTag(Cell::new(leaving)));
        let ws = window_workspace(&window);
        state.space_mut().unmap_elem(&window);
        state.hidden_windows_mut().push(HiddenWindow { window, workspace: ws, loc });
    }
    state.set_active_workspace(n);
    let mut restored: Vec<Window> = Vec::new();
    let mut i = 0;
    while i < state.hidden_windows().len() {
        if state.hidden_windows()[i].workspace == n {
            let hw = state.hidden_windows_mut().remove(i);
            state.space_mut().map_element(hw.window.clone(), hw.loc, false);
            restored.push(hw.window);
        } else {
            i += 1;
        }
    }
    if let Some(top) = restored.last().cloned() {
        state.space_mut().raise_element(&top, true);
        let serial = SERIAL_COUNTER.next_serial();
        let surface = top.wl_surface().map(|s| s.into_owned());
        let keyboard = state.keyboard().clone();
        keyboard.set_focus(state, surface, serial);
    } else {
        let serial = SERIAL_COUNTER.next_serial();
        let keyboard = state.keyboard().clone();
        keyboard.set_focus(state, None, serial);
    }
    state.set_desktop_shown(true);
    state.set_ws_switch_at(Some(Instant::now()));
    info!(workspace = n, restored = restored.len(), "workspace.switched");
    true
}

/// Super+Shift+1..9 / `move_to_workspace` — move the focused window to workspace `n`.
pub fn move_focused_to_workspace<S: CompState>(state: &mut S, n: usize) -> bool {
    let window = match focused_window(state) {
        Some(w) => w,
        None => return false,
    };
    move_window_to_workspace(state, &window, n)
}

/// The handle-keyed twin of `move_focused_to_workspace`, for the IPC verb. Resolves the
/// window across BOTH the visible space and the hidden set.
pub fn move_window_to_workspace_by_handle<S: CompState>(state: &mut S, handle: &str, n: usize) -> bool {
    if let Some(window) = ipc_window_for_handle(state, handle) {
        return move_window_to_workspace(state, &window, n);
    }
    if let Some(idx) = state
        .hidden_windows()
        .iter()
        .position(|hw| hw.window.user_data().get::<WindowHandle>().map(|h| h.as_str() == handle).unwrap_or(false))
    {
        let window = state.hidden_windows()[idx].window.clone();
        window.user_data().insert_if_missing(|| WorkspaceTag(Cell::new(n)));
        if let Some(tag) = window.user_data().get::<WorkspaceTag>() {
            tag.0.set(n);
        }
        if n == state.active_workspace() {
            let hw = state.hidden_windows_mut().remove(idx);
            state.space_mut().map_element(hw.window, hw.loc, false);
        } else {
            state.hidden_windows_mut()[idx].workspace = n;
        }
        return true;
    }
    false
}

/// A toplevel that was on a NON-active workspace (so it lived in `hidden_windows`) has
/// been destroyed. Resolve it in the hidden set, emit `window.closed` + invalidate its
/// handle, and drop it. `pred` matches the destroyed surface. Returns true if purged.
pub fn purge_hidden_window<S: CompState>(state: &mut S, pred: impl Fn(&Window) -> bool) -> bool {
    let idx = match state.hidden_windows().iter().position(|hw| pred(&hw.window)) {
        Some(i) => i,
        None => return false,
    };
    let hw = state.hidden_windows_mut().remove(idx);
    if let Some(handle) = hw.window.user_data().get::<WindowHandle>().cloned() {
        if window_registry_unmap(state, &handle) {
            info!(handle = handle.as_str(), "window.closed (hidden-workspace toplevel destroyed)");
            state.emit_window_event("window.closed", &hw.window, handle.as_str());
        }
    }
    true
}

/// Shared body: move `window` to workspace `n`. If `n == active`, keep it visible (no-op
/// move). Otherwise stash it off-screen and refocus the next active window.
pub fn move_window_to_workspace<S: CompState>(state: &mut S, window: &Window, n: usize) -> bool {
    window
        .user_data()
        .insert_if_missing(|| WorkspaceTag(Cell::new(n)));
    if let Some(tag) = window.user_data().get::<WorkspaceTag>() {
        tag.0.set(n);
    }
    if n == state.active_workspace() {
        return true;
    }
    let loc = state.space().element_location(window).unwrap_or_default();
    state.space_mut().unmap_elem(window);
    state.hidden_windows_mut().push(HiddenWindow {
        window: window.clone(),
        workspace: n,
        loc,
    });
    let serial = SERIAL_COUNTER.next_serial();
    let keyboard = state.keyboard().clone();
    if let Some(top) = state.space().elements().last().cloned() {
        state.space_mut().raise_element(&top, true);
        let surface = top.wl_surface().map(|s| s.into_owned());
        keyboard.set_focus(state, surface, serial);
    } else {
        keyboard.set_focus(state, None, serial);
    }
    info!(workspace = n, "window.moved_to_workspace");
    true
}

/// Super+D — toggle show-desktop. Hide stashes every visible window into
/// `hidden_windows` tagged with the active workspace; restore brings back exactly those.
pub fn toggle_show_desktop<S: CompState>(state: &mut S) {
    if state.desktop_shown() {
        let active: Vec<Window> = state.space().elements().cloned().collect();
        if active.is_empty() {
            return;
        }
        let ws = state.active_workspace();
        for window in active {
            let loc = state.space().element_location(&window).unwrap_or_default();
            window
                .user_data()
                .insert_if_missing(|| WorkspaceTag(Cell::new(ws)));
            state.space_mut().unmap_elem(&window);
            state.hidden_windows_mut().push(HiddenWindow { window, workspace: ws, loc });
        }
        let serial = SERIAL_COUNTER.next_serial();
        let keyboard = state.keyboard().clone();
        keyboard.set_focus(state, None, serial);
        state.set_desktop_shown(false);
        info!("desktop.shown (windows hidden)");
    } else {
        let n = state.active_workspace();
        let mut restored: Vec<Window> = Vec::new();
        let mut i = 0;
        while i < state.hidden_windows().len() {
            if state.hidden_windows()[i].workspace == n {
                let hw = state.hidden_windows_mut().remove(i);
                state.space_mut().map_element(hw.window.clone(), hw.loc, false);
                restored.push(hw.window);
            } else {
                i += 1;
            }
        }
        if let Some(top) = restored.last().cloned() {
            state.space_mut().raise_element(&top, true);
            let serial = SERIAL_COUNTER.next_serial();
            let surface = top.wl_surface().map(|s| s.into_owned());
            let keyboard = state.keyboard().clone();
            keyboard.set_focus(state, surface, serial);
        }
        state.set_desktop_shown(true);
        info!(restored = restored.len(), "desktop.restored (windows back)");
    }
}

// ── pointer routing ──

/// Advance a pointer location by a relative `delta` and clamp it inside the output rect,
/// so a touchpad/mouse relative-motion event can never push the cursor outside the
/// framebuffer. PURE geometry (no Seat, no surface hit-test) — the ONE clamp both the
/// relative-motion router and its unit tests call (no parallel path). Mirrors anvil's
/// `clamp_coords` for the single-output case: the cursor may rest exactly on the right/
/// bottom edge (`[loc, loc+size]`). A degenerate 0-sized output (no real mode latched
/// yet) is left UNCLAMPED on that axis, so a pre-mode event is not pinned to the origin.
pub fn advance_and_clamp_pointer(
    loc: Point<f64, Logical>,
    delta: Point<f64, Logical>,
    output_geo: Rectangle<i32, Logical>,
) -> Point<f64, Logical> {
    let mut next = loc + delta;
    let min_x = output_geo.loc.x as f64;
    let min_y = output_geo.loc.y as f64;
    let max_x = (output_geo.loc.x + output_geo.size.w) as f64;
    let max_y = (output_geo.loc.y + output_geo.size.h) as f64;
    if max_x > min_x {
        next.x = next.x.clamp(min_x, max_x);
    }
    if max_y > min_y {
        next.y = next.y.clamp(min_y, max_y);
    }
    next
}

/// Route RELATIVE pointer motion — the real-hardware touchpad + mouse path (libinput
/// emits `InputEvent::PointerMotion`, NOT the absolute variant a winit/tablet device
/// emits). THE #134 fix: read the current pointer location, add the device delta, clamp
/// to the output, hit-test the surface under the new position, then send BOTH a
/// `relative_motion` (for clients using the relative-pointer protocol / active grabs) and
/// an absolute `motion` (the actual cursor move + enter/leave), plus a `frame`. Mirrors
/// anvil's `on_pointer_move`, minus the pointer-constraints lock/confine (hart-comp binds
/// no pointer-constraints global, so there is nothing to honour). Without this arm every
/// touchpad/mouse motion hit `process_input_event`'s `_ => {}` sink and the cursor stayed
/// frozen at (0,0) while the shell still painted — the #134 input-dead symptom.
pub fn on_pointer_move_relative<S: CompState, B: InputBackend>(
    state: &mut S,
    evt: B::PointerMotionEvent,
) {
    let serial = SERIAL_COUNTER.next_serial();
    let pointer = state.pointer().clone();
    let current = pointer.current_location();
    let pos = match state.space().output_geometry(state.output()) {
        Some(geo) => advance_and_clamp_pointer(current, evt.delta(), geo),
        // No output geometry yet (pre-mode): apply the raw delta rather than pin to (0,0).
        None => current + evt.delta(),
    };
    let under = surface_under(state, pos);
    pointer.relative_motion(
        state,
        under.clone(),
        &RelativeMotionEvent {
            delta: evt.delta(),
            delta_unaccel: evt.delta_unaccel(),
            utime: evt.time(),
        },
    );
    pointer.motion(
        state,
        under,
        &MotionEvent { location: pos, serial, time: evt.time_msec() },
    );
    pointer.frame(state);
}

/// Route absolute pointer motion (window-relative coords) to the surface under the
/// cursor, then send a pointer frame.
pub fn on_pointer_move_absolute<S: CompState, B: InputBackend>(
    state: &mut S,
    evt: B::PointerMotionAbsoluteEvent,
) {
    let output_geo = match state.space().output_geometry(state.output()) {
        Some(g) => g,
        None => return,
    };
    let pos = evt.position_transformed(output_geo.size) + output_geo.loc.to_f64();
    let serial = SERIAL_COUNTER.next_serial();
    let pointer = state.pointer().clone();
    let under = surface_under(state, pos);
    pointer.motion(
        state,
        under,
        &MotionEvent { location: pos, serial, time: evt.time_msec() },
    );
    pointer.frame(state);
}

/// Route a pointer button. On press, first move the keyboard focus + raise the clicked
/// window (click-to-focus), then forward the button to the pointer-focused surface.
pub fn on_pointer_button<S: CompState, B: InputBackend>(state: &mut S, evt: B::PointerButtonEvent) {
    let serial = SERIAL_COUNTER.next_serial();
    let button = evt.button_code();
    let button_state = evt.state();
    if button_state == ButtonState::Pressed {
        update_keyboard_focus(state, state.pointer().current_location(), serial);
    }
    let pointer = state.pointer().clone();
    pointer.button(
        state,
        &ButtonEvent { button, state: button_state, serial, time: evt.time_msec() },
    );
    pointer.frame(state);
}

/// Route a scroll/axis event to the pointer-focused surface.
pub fn on_pointer_axis<S: CompState, B: InputBackend>(state: &mut S, evt: B::PointerAxisEvent) {
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
    let pointer = state.pointer().clone();
    pointer.axis(state, frame);
    pointer.frame(state);
}

// ════════════════════════════════════════════════════════════════════════════
// M4 — the com.hart.Compositor IPC verb BODIES (hoisted from ipc.rs's `impl State`).
// Every verb runs against the REAL `space`/`seat`/`xwm` mutators — the SAME ones the
// input path uses — so the chords + IPC drive the same verbs. The framed-JSON
// transport (ipc.rs) is winit-only; these BODIES are shared so the DRM backend can
// drive the SAME window-arrange surface from its own IPC sink later.
// ════════════════════════════════════════════════════════════════════════════

/// app_id for any mapped `Window` (xdg `app_id` or X11 WM_CLASS).
pub fn ipc_window_app_id(window: &Window) -> Option<String> {
    if let Some(toplevel) = window.toplevel() {
        return toplevel_app_id(toplevel);
    }
    window.x11_surface().and_then(x11_app_id)
}

/// title for any mapped `Window` (xdg `title` or X11 window title).
pub fn ipc_window_title(window: &Window) -> Option<String> {
    if let Some(toplevel) = window.toplevel() {
        return toplevel_title(toplevel);
    }
    window.x11_surface().and_then(x11_title)
}

/// Build the IPC event-frame `window` payload (IPC_PROTOCOL.md §5) for one mapped
/// `Window` — the SHARED serializer BOTH backends' `emit_window_event` use, so a
/// `window.opened`/`closed`/`focused` frame has the identical shape on winit + DRM (one
/// source of truth, the same fields as a `window.list` row). `serde_json` is in the dep
/// tree for both features (winit + smithay), so this compiles on both.
pub fn ipc_event_window_json_for<S: CompState>(
    state: &S,
    window: &Window,
    handle: &str,
) -> serde_json::Value {
    let geo = state.space().element_geometry(window);
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

/// Find the live mapped `Window` whose minted handle is `handle`.
pub fn ipc_window_for_handle<S: CompState>(state: &S, handle: &str) -> Option<Window> {
    state
        .space()
        .elements()
        .find(|w| {
            w.user_data()
                .get::<WindowHandle>()
                .map(|h| h.as_str() == handle)
                .unwrap_or(false)
        })
        .cloned()
}

/// `window.focus` (§4.2): raise + keyboard-focus the window. Returns false if the handle
/// resolves to no mapped window.
pub fn ipc_focus_window<S: CompState>(state: &mut S, handle: &str) -> bool {
    let window = match ipc_window_for_handle(state, handle) {
        Some(w) => w,
        None => return false,
    };
    state.space_mut().raise_element(&window, true);
    if let Some(x11) = window.x11_surface() {
        if let Some(xwm) = state.xwm_mut().as_mut() {
            if let Err(err) = xwm.raise_window(x11) {
                warn!(?err, "window.focus: X11Wm::raise_window failed");
            }
        }
    }
    let serial = SERIAL_COUNTER.next_serial();
    let surface = window.wl_surface().map(|s| s.into_owned());
    let keyboard = state.keyboard().clone();
    keyboard.set_focus(state, surface, serial);
    true
}

/// `window.move` — reposition only (§4.4 without resize).
pub fn ipc_move_window<S: CompState>(state: &mut S, handle: &str, x: i32, y: i32) -> bool {
    let window = match ipc_window_for_handle(state, handle) {
        Some(w) => w,
        None => return false,
    };
    state.space_mut().map_element(window.clone(), (x, y), true);
    if let Some(x11) = window.x11_surface() {
        if let Some(bbox) = state.space().element_bbox(&window) {
            if let Err(err) = x11.configure(Some(bbox)) {
                warn!(?err, "window.move: X11Surface::configure failed");
            }
        }
    }
    true
}

/// `window.resize` — change size, keep location (§4.4).
pub fn ipc_resize_window<S: CompState>(state: &mut S, handle: &str, w: i32, h: i32) -> bool {
    let window = match ipc_window_for_handle(state, handle) {
        Some(win) => win,
        None => return false,
    };
    let loc = state.space().element_location(&window).unwrap_or_default();
    if let Some(toplevel) = window.toplevel() {
        toplevel.with_pending_state(|s| {
            s.size = Some((w, h).into());
        });
        toplevel.send_pending_configure();
    }
    if let Some(x11) = window.x11_surface() {
        let rect = Rectangle::new((loc.x, loc.y).into(), (w, h).into());
        if let Err(err) = x11.configure(Some(rect)) {
            warn!(?err, "window.resize: X11Surface::configure failed");
        }
    }
    true
}

/// `window.place` (§4.4): move AND resize in one op (the zone/rect target).
pub fn ipc_place_window<S: CompState>(state: &mut S, handle: &str, x: i32, y: i32, w: i32, h: i32) -> bool {
    let window = match ipc_window_for_handle(state, handle) {
        Some(win) => win,
        None => return false,
    };
    if let Some(toplevel) = window.toplevel() {
        toplevel.with_pending_state(|s| {
            s.size = Some((w, h).into());
        });
        toplevel.send_pending_configure();
    }
    state.space_mut().map_element(window.clone(), (x, y), true);
    if let Some(x11) = window.x11_surface() {
        let rect = Rectangle::new((x, y).into(), (w, h).into());
        if let Err(err) = x11.configure(Some(rect)) {
            warn!(?err, "window.place: X11Surface::configure failed");
        }
    }
    true
}

/// Read a mapped window's current `(x, y, w, h)` (post-op geometry for the response).
pub fn ipc_window_geometry<S: CompState>(state: &S, handle: &str) -> Option<(i32, i32, i32, i32)> {
    let window = ipc_window_for_handle(state, handle)?;
    let g = state.space().element_geometry(&window)?;
    Some((g.loc.x, g.loc.y, g.size.w, g.size.h))
}

/// PURE named-zone geometry over an output rect `(ox, oy, ow, oh)`. No live State — just
/// the §4.4 zone arithmetic — so the zone set is one source of truth AND unit-testable
/// without a mapped output. `ipc_zone_rect` is the thin State wrapper that reads the
/// output geometry then defers here. Returns `None` for an unknown zone name.
///
/// Right/bottom-edge coverage: the "right"/"bottom" halves use `ow - half_w` / `oh -
/// half_h` (NOT a second `half`), so on an ODD output dimension the two halves still
/// TILE the full extent with no 1px seam (e.g. ow=1921 → left 960 + right 961 == 1921).
pub fn zone_rect(ox: i32, oy: i32, ow: i32, oh: i32, zone: &str) -> Option<(i32, i32, i32, i32)> {
    let half_w = ow / 2;
    let half_h = oh / 2;
    let r = match zone {
        "left-half" => (ox, oy, half_w, oh),
        "right-half" => (ox + half_w, oy, ow - half_w, oh),
        "top-half" => (ox, oy, ow, half_h),
        "bottom-half" => (ox, oy + half_h, ow, oh - half_h),
        "top-left" => (ox, oy, half_w, half_h),
        "top-right" => (ox + half_w, oy, ow - half_w, half_h),
        "bottom-left" => (ox, oy + half_h, half_w, oh - half_h),
        "bottom-right" => (ox + half_w, oy + half_h, ow - half_w, oh - half_h),
        "center" => (ox + ow / 4, oy + oh / 4, ow / 2, oh / 2),
        "maximize" | "fullscreen" => (ox, oy, ow, oh),
        _ => return None,
    };
    Some(r)
}

/// Compute a named-zone rect over the WORK AREA (§4.4 zones). Logical pixels. Thin
/// wrapper: reads the live work area then defers to the pure `zone_rect`.
///
/// The work area rather than the output geometry is what makes "maximize" stop at
/// the taskbar instead of burying it, and it lands the same fix on all nine zones
/// at once -- a top-half snap now means the top half of the usable desktop.
pub fn ipc_zone_rect<S: CompState>(state: &S, zone: &str) -> Option<(i32, i32, i32, i32)> {
    let (ax, ay, aw, ah) = work_area_for(state)?;
    zone_rect(ax, ay, aw, ah, zone)
}

/// PURE tile geometry: lay `n` windows over an output rect `(ox, oy, ow, oh)` per the
/// named `layout` (grid (default), cols/columns, rows, master-stack, fullscreen). No live
/// State — just the §4.5 arithmetic — so each layout is unit-testable without mapped
/// windows. Returns one `(x, y, w, h)` per window, in tile order. `n == 0` → empty.
///
/// KNOWN NON-COVERAGE on indivisible extents (intentional, documented): the cols/rows/
/// grid cell size is an INTEGER `ow / cols` (truncating), so when the extent is not a
/// multiple of the divisor the LAST column/row leaves a remainder strip uncovered on the
/// right/bottom edge — e.g. ow=1920, n=7 grid: cols=3, cw=640, 3*640=1920 (exact here),
/// but cols=3 with ow=1921 → cw=640, last col ends at 1920, a 1px strip uncovered. This
/// is the simple-tiler contract (no fractional pixels, no last-cell stretch); the gap is
/// at most `(divisor-1)`px and the test below pins it so a future "fix" is a conscious
/// choice, not an accident.
pub fn tile_rects(ox: i32, oy: i32, ow: i32, oh: i32, n: usize, layout: &str) -> Vec<(i32, i32, i32, i32)> {
    if n == 0 {
        return Vec::new();
    }
    match layout {
        "fullscreen" => (0..n).map(|_| (ox, oy, ow, oh)).collect(),
        "cols" | "columns" => {
            let cw = ow / n as i32;
            (0..n).map(|i| (ox + i as i32 * cw, oy, cw, oh)).collect()
        }
        "rows" => {
            let rh = oh / n as i32;
            (0..n).map(|i| (ox, oy + i as i32 * rh, ow, rh)).collect()
        }
        "master-stack" => {
            if n == 1 {
                vec![(ox, oy, ow, oh)]
            } else {
                let master_w = ow / 2;
                let stack_n = (n - 1) as i32;
                let stack_h = oh / stack_n.max(1);
                let mut v = vec![(ox, oy, master_w, oh)];
                for i in 0..(n - 1) {
                    v.push((ox + master_w, oy + i as i32 * stack_h, ow - master_w, stack_h));
                }
                v
            }
        }
        _ => {
            let cols = (n as f64).sqrt().ceil() as i32;
            let rows = ((n as i32) + cols - 1) / cols;
            let cw = ow / cols;
            let ch = oh / rows;
            (0..n)
                .map(|i| {
                    let col = i as i32 % cols;
                    let row = i as i32 / cols;
                    (ox + col * cw, oy + row * ch, cw, ch)
                })
                .collect()
        }
    }
}

/// `window.tile` (§4.5): arrange EVERY mapped toplevel over the output. Supported
/// layouts: grid (default), cols/columns, rows, master-stack, fullscreen. Returns the
/// arranged handles in the order applied. Thin wrapper: reads the live handle list +
/// output geometry, then defers the rect math to the pure `tile_rects`.
pub fn ipc_tile<S: CompState>(state: &mut S, layout: &str) -> Vec<String> {
    let handles: Vec<String> = state
        .space()
        .elements()
        .filter_map(|w| w.user_data().get::<WindowHandle>().map(|h| h.as_str().to_string()))
        .collect();
    let n = handles.len();
    if n == 0 {
        return Vec::new();
    }
    // Tile over the WORK AREA: a tiling layout that paved over the taskbar would
    // hide it on every single window, which is the worst case of all.
    let (ax, ay, aw, ah) = match work_area_for(state) {
        Some(a) => a,
        None => return Vec::new(),
    };
    let rects = tile_rects(ax, ay, aw, ah, n, layout);

    for (handle, (x, y, w, h)) in handles.iter().zip(rects.iter()) {
        ipc_place_window(state, handle, *x, *y, *w, *h);
    }
    handles
}

/// `window.close` (§4.3): ask the window to close — xdg `send_close()`, X11
/// `set_mapped(false)`. The real destroy flows through the destroy handler.
pub fn ipc_close_window<S: CompState>(state: &mut S, handle: &str) -> bool {
    let window = match ipc_window_for_handle(state, handle) {
        Some(w) => w,
        None => return false,
    };
    if let Some(toplevel) = window.toplevel() {
        toplevel.send_close();
        return true;
    }
    if let Some(x11) = window.x11_surface() {
        if let Err(err) = x11.set_mapped(false) {
            warn!(?err, "window.close: X11Surface::set_mapped(false) failed");
        }
        return true;
    }
    false
}

// ════════════════════════════════════════════════════════════════════════════
// The WindowRegistry bridge. The pure `WindowRegistry` lives in main.rs; the two
// backends store it differently (winit: a `windows` field; DRM: `windows` on
// wayland::State). The shared WM code reaches it through these thin accessors so a
// hidden-workspace destroy (`purge_hidden_window`) can invalidate a handle uniformly.
// ════════════════════════════════════════════════════════════════════════════

/// Invalidate a handle in the backend's `WindowRegistry`. Defined per-backend (the
/// registry field name/location differs), so the shared `purge_hidden_window` calls it
/// generically. Returns true if the handle was live.
pub fn window_registry_unmap<S: CompState>(state: &mut S, handle: &WindowHandle) -> bool {
    S::registry_on_unmap(state, handle)
}

// ════════════════════════════════════════════════════════════════════════════
// THE shared RENDER element build — z-order, generic over the renderer R, so BOTH
// the winit GlesRenderer (`render_frame`) and the DRM PixmanRenderer (`render_all`)
// composite the IDENTICAL frame (killswitch → cursor → windows → layers). The
// per-frame BIND + SUBMIT stays in each backend (it binds its own framebuffer); this
// only builds the element list the backend then draws.
// ════════════════════════════════════════════════════════════════════════════

/// The workspace-switch crossfade factor NOW: 0→1 over `WS_FADE_MS` after a switch,
/// then a steady 1.0. Multiplies every visible surface's alpha so the whole new
/// workspace fades in.
pub fn workspace_fade_alpha<S: CompState>(state: &S) -> f32 {
    match state.ws_switch_at() {
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

/// Is any effect still animating (a window mid-fade, or a workspace crossfade in
/// flight)? The loop forces a redraw next iteration while true.
pub fn effects_animating<S: CompState>(state: &S) -> bool {
    if let Some(t) = state.ws_switch_at() {
        if t.elapsed().as_millis() < WS_FADE_MS {
            return true;
        }
    }
    state
        .space()
        .elements()
        .any(|w| w.user_data().get::<MapAnim>().map(|a| a.animating()).unwrap_or(false))
}

/// Build the software-cursor render element(s) at the pointer location, PREPENDED so the
/// cursor draws on top of windows. Generic over R (GlesRenderer / PixmanRenderer both
/// satisfy the bounds). Three cases mirroring anvil's cursor draw.
pub fn build_cursor_elements<S, R>(
    state: &S,
    renderer: &mut R,
    elements: &mut Vec<HartRenderElement<R>>,
) where
    S: CompState,
    R: Renderer + ImportAll + ImportMem,
    R::TextureId: Send + Clone + 'static,
{
    let pos = state.pointer().current_location();
    match state.cursor_status() {
        CursorImageStatus::Hidden => {}
        CursorImageStatus::Surface(surface) => {
            let hotspot = with_states(surface, |states| {
                states
                    .data_map
                    .get::<smithay::input::pointer::CursorImageSurfaceData>()
                    // A poisoned cursor-data mutex must NOT abort the compositor mid-frame
                    // (the never-fail render floor, #186): fall back to a zero hotspot and
                    // keep painting. `.lock()` poisons only if a prior holder panicked — at
                    // which point dropping the cursor offset is strictly better than dying.
                    .and_then(|d| match d.lock() {
                        Ok(g) => Some(g.hotspot),
                        Err(err) => {
                            debug!(?err, "cursor: CursorImageSurfaceData mutex poisoned; using a zero hotspot");
                            None
                        }
                    })
                    .unwrap_or_default()
            });
            let cpos = (pos - hotspot.to_f64()).to_physical_precise_round(1.0);
            let surf_elems: Vec<WaylandSurfaceRenderElement<R>> =
                render_elements_from_surface_tree(renderer, surface, cpos, 1.0, 1.0, Kind::Cursor);
            for (i, e) in surf_elems.into_iter().enumerate() {
                elements.insert(i, HartRenderElement::Surface(e));
            }
        }
        _ => {
            let cpos: Point<f64, Physical> =
                (pos - state.cursor_hotspot().to_f64()).to_physical(1.0);
            match MemoryRenderBufferRenderElement::from_buffer(
                renderer,
                cpos,
                state.cursor_buffer(),
                None,
                None,
                None,
                Kind::Cursor,
            ) {
                Ok(e) => elements.insert(0, HartRenderElement::Memory(e)),
                Err(err) => warn!(?err, "cursor: failed to build the default-arrow element"),
            }
        }
    }
}

/// Does a wlr-layer-shell layer sit ABOVE mapped toplevels?
///
/// The protocol stacks background < bottom < WINDOWS < top < overlay. This states
/// it in ONE place because the renderer and the input hit-test have to agree, and
/// they did not: `surface_under` has routed Overlay/Top above windows since it was
/// written (its own docstring says so), while `build_frame_elements` painted EVERY
/// layer below EVERY window. A Top-layer panel therefore swallowed clicks in a
/// strip where nothing of it was drawn -- pixels saying one thing and input
/// another, in exactly the surfaces the Top layer exists for.
pub fn layer_is_above_windows(layer: WlrLayer) -> bool {
    matches!(layer, WlrLayer::Top | WlrLayer::Overlay)
}

/// Push the layer surfaces on ONE side of the toplevels, returning how many were
/// painted. Called twice per frame, before and after the window loop.
///
/// `elements` is TOP→bottom (index 0 paints first, on top), and `LayerMap::layers`
/// yields bottom-to-top, so the `.rev()` is what keeps two surfaces on the same
/// layer from stacking upside down. This mirrors anvil, which partitions
/// `layers().rev()` on `Background | Bottom` for the same reason.
fn push_layer_elements<R>(
    renderer: &mut R,
    elements: &mut Vec<HartRenderElement<R>>,
    output: &Output,
    ws_alpha: f32,
    above_windows: bool,
) -> usize
where
    R: Renderer + ImportAll + ImportMem,
    R::TextureId: Send + Clone + 'static,
{
    let map = layer_map_for_output(output);
    let mut painted = 0usize;
    for layer in map.layers().rev() {
        if layer_is_above_windows(layer.layer()) != above_windows {
            continue;
        }
        painted += 1;
        let loc = map.layer_geometry(layer).map(|g| g.loc).unwrap_or_default();
        let layer_elems: Vec<WaylandSurfaceRenderElement<R>> =
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
    painted
}

/// Build the FULL frame element list in z-order (TOP→bottom; `draw_render_elements`
/// paints index 0 first = top-most): killswitch → cursor → Top/Overlay layers →
/// windows (faded) → Bottom/Background layers.
/// Generic over R so BOTH backends build the identical frame; the backend then binds its
/// framebuffer + draws this slice. This is the single source of the desktop's z-order.
/// NATIVE SHELL M2 input: the pointer position mapped into the native scene's own
/// coordinate space (the physical `size` the scene is laid out in), or None when there
/// is no output geometry yet (pre-mode) or the cursor is off this output. Used ONLY to
/// energise the orb under the cursor, so returning None simply means no hover lift and
/// today's flag-off behaviour stays byte-identical. The pointer lives in the GLOBAL
/// logical space, so it is made output-local and scaled into physical coords, which keeps
/// the hit-test aligned with the painted orb on a HiDPI output too.
fn native_pointer_scene_pos<S: CompState>(
    state: &S,
    size: Size<i32, Physical>,
) -> Option<(f32, f32)> {
    let loc = state.pointer().current_location();
    let geo = state.space().output_geometry(state.output())?;
    if geo.size.w <= 0 || geo.size.h <= 0 {
        return None;
    }
    let lx = loc.x - geo.loc.x as f64;
    let ly = loc.y - geo.loc.y as f64;
    let sx = size.w as f64 / geo.size.w as f64;
    let sy = size.h as f64 / geo.size.h as f64;
    Some(((lx * sx) as f32, (ly * sy) as f32))
}

/// NATIVE SHELL M3 GL LOWERING: lower the native shell scene to render elements.
/// Rect leaves (top bar, taskbar, hero and card tiles) become SolidColorRenderElements;
/// Text runs are shaped + rasterized into cached MemoryRenderBuffers; OrbSlots reuse the
/// M2 orb texture. Image (texture) is the remaining leaf kind. Gated by `native_shell_on`
/// at the call site, so with the flag OFF this never runs. The actual lowering lives in
/// `lower_scene` (State-free, so it is render-tested); this wrapper just pulls the scene
/// and caches off `state`.
///
/// FIRST-CUT alloc note: builds the scene and one SolidColorBuffer per rect PER FRAME.
/// That violates the zero-per-frame-alloc NFR on purpose: step one is proving the
/// native scene scans out; step two retains the tree (rebuild on compose/resize only)
/// and pools the buffers. Not shipped past M3 as-is.
pub fn render_native_scene<S, R>(
    state: &mut S,
    renderer: &mut R,
    size: Size<i32, Physical>,
    elements: &mut Vec<HartRenderElement<R>>,
) where
    S: CompState,
    R: Renderer + ImportAll + ImportMem,
    R::TextureId: Send + Clone + 'static,
{
    // Pull the scene + caches OFF `state` here, then hand the concrete pieces to
    // `lower_scene`. The demo scene is the fallback until a `shell.compose` IPC feed
    // stores one. Energy is read before the cache borrow (a plain f32). This is the
    // ONLY caller that goes through State; the render test calls `lower_scene`
    // directly with constructed caches, so there is one lowering path, not two.
    let home = state
        .native_home()
        .cloned()
        .unwrap_or_else(crate::scene::HomeCompose::demo);
    let orb_energy = state.orb_energy();
    // Read the pointer BEFORE the `&mut` cache borrow (both are plain owned values), so
    // the hover lift rides the same single lowering call. None on a pre-mode frame.
    let pointer = native_pointer_scene_pos(state, size);
    let (rasterizer, orb_cache, rect_cache) = state.native_scene_caches();
    lower_scene(
        &home, size, renderer, rasterizer, orb_cache, rect_cache, orb_energy, pointer, elements,
    );
}

/// Lower a `HomeCompose` to render elements against the concrete caches — the
/// State-free core of `render_native_scene`, so it is unit-testable with a
/// `PixmanRenderer` + freshly-constructed caches (no compositor State needed). The
/// leaf list interleaves Text (needs `rasterizer`) and OrbSlot (needs `orb_cache`),
/// which is why both `&mut` are passed together rather than fetched per-leaf.
pub fn lower_scene<R>(
    home: &crate::scene::HomeCompose,
    size: Size<i32, Physical>,
    renderer: &mut R,
    rasterizer: &mut crate::text_render::TextRasterizer,
    orb_cache: &mut OrbCache,
    rect_cache: &mut RectCache,
    orb_energy: f32,
    pointer: Option<(f32, f32)>,
    elements: &mut Vec<HartRenderElement<R>>,
) where
    R: Renderer + ImportAll + ImportMem,
    R::TextureId: Send + Clone + 'static,
{
    let theme = crate::scene::Theme::cosmic_default();
    let tree = crate::scene::layout_home(size.w as f32, size.h as f32, home, &theme);

    // M2 input half: the pointer energises the orb it sits over. Fold the hover lift into
    // the ambient energy ONCE here, against the SAME tree the leaves come from, so orb
    // reactivity rides the existing orb path (orb::motion_at clamps the sum to 0..=1).
    let orb_energy = orb_energy + tree.pointer_orb_energy(pointer);

    let mut leaves: Vec<&crate::scene::SceneNode> = Vec::new();
    tree.flatten(&mut leaves);
    for leaf in leaves {
        match leaf {
            crate::scene::SceneNode::Rect { rect, color, radius } => {
                if rect.w < 1.0 || rect.h < 1.0 {
                    continue;
                }
                if *radius > 0.5 {
                    // Rounded (cards, omnibox): lower through a cached rounded-rect
                    // buffer so the corner radius the scene specifies is actually
                    // drawn. A SolidColorRenderElement is always a hard quad, so this
                    // is the ONLY way the native chrome gets soft corners like the
                    // shell has. Alpha is baked into the premultiplied buffer, so the
                    // element alpha is 1.0.
                    if let Some(buffer) = rect_cache.rounded(
                        rect.w as i32,
                        rect.h as i32,
                        *radius,
                        [color.r, color.g, color.b, color.a],
                    ) {
                        let origin: Point<f64, Physical> =
                            Point::from((rect.x as f64, rect.y as f64));
                        match MemoryRenderBufferRenderElement::from_buffer(
                            renderer,
                            origin,
                            buffer,
                            Some(1.0),
                            None,
                            Some((rect.w as i32, rect.h as i32).into()),
                            Kind::Unspecified,
                        ) {
                            Ok(e) => elements.push(HartRenderElement::Memory(e)),
                            Err(err) => warn!(?err, "native scene: rounded rect import failed"),
                        }
                    }
                } else {
                    // Sharp (desktop ground, bars): the cheap solid quad, no per-pixel
                    // rasterize.
                    let mut buf = SolidColorBuffer::new(
                        (rect.w as i32, rect.h as i32),
                        [color.r, color.g, color.b, color.a],
                    );
                    let el = SolidColorRenderElement::from_buffer(
                        &mut buf,
                        (rect.x as i32, rect.y as i32),
                        Scale::from(1.0),
                        color.a,
                        Kind::Unspecified,
                    );
                    elements.push(HartRenderElement::Solid(el));
                }
            }
            crate::scene::SceneNode::Text {
                rect,
                text,
                size_px,
                color,
                ..
            } => {
                if rect.w < 1.0 || rect.h < 1.0 || text.is_empty() {
                    continue;
                }
                let buffer = rasterizer.rasterize(
                    text,
                    *size_px,
                    rect.w as i32,
                    rect.h as i32,
                    [color.r, color.g, color.b, color.a],
                );
                let origin: Point<f64, Physical> = Point::from((rect.x as f64, rect.y as f64));
                match MemoryRenderBufferRenderElement::from_buffer(
                    renderer,
                    origin,
                    buffer,
                    Some(1.0),
                    None,
                    Some((rect.w as i32, rect.h as i32).into()),
                    Kind::Unspecified,
                ) {
                    Ok(e) => elements.push(HartRenderElement::Memory(e)),
                    Err(err) => warn!(?err, "native scene: text run import failed"),
                }
            }
            crate::scene::SceneNode::OrbSlot { rect, .. } => {
                // The scene OWNS the orb (the hardcoded M2 draw is gated off when
                // native_shell_on), so ONE orb path. Both the large home orb and the
                // compact top-bar orb-sm share ONE cached texture composed at a fixed
                // size and render at their own slot size via GPU scale, so two slots in
                // one frame never thrash the single-buffer OrbCache.
                if rect.w < 1.0 || rect.h < 1.0 {
                    continue;
                }
                let side = (size.w.min(size.h) as f32 * 0.30) as i32;
                if let Some((buffer, motion)) = orb_cache.current(side, orb_energy) {
                    let dst = (rect.w.min(rect.h) * motion.scale) as i32;
                    if dst < 1 {
                        continue;
                    }
                    let origin: Point<f64, Physical> = Point::from((
                        (rect.x + (rect.w - dst as f32) / 2.0) as f64,
                        (rect.y + (rect.h - dst as f32) / 2.0) as f64,
                    ));
                    match MemoryRenderBufferRenderElement::from_buffer(
                        renderer,
                        origin,
                        buffer,
                        Some(motion.alpha),
                        None,
                        Some((dst, dst).into()),
                        Kind::Unspecified,
                    ) {
                        Ok(e) => elements.push(HartRenderElement::Memory(e)),
                        Err(err) => warn!(?err, "native scene: orb import failed"),
                    }
                }
            }
            // Image lowering is the M3 remainder; Container only groups.
            _ => {}
        }
    }
}

pub fn build_frame_elements<S, R>(
    state: &mut S,
    renderer: &mut R,
    size: Size<i32, Physical>,
) -> Vec<HartRenderElement<R>>
where
    S: CompState,
    R: Renderer + ImportAll + ImportMem,
    R::TextureId: Send + Clone + 'static,
{
    let mut elements: Vec<HartRenderElement<R>> = Vec::new();
    // What native chrome THIS frame ends up containing. Accumulated as elements
    // are actually pushed (never on a failed import), and published at the end
    // for the backend's flip handler to turn into the shell's verdict file.
    let mut native_mask: u8 = 0;

    // ── 0. KILLSWITCH (top): a full-output opaque black solid ABOVE all windows. ──
    if state.capture_blocked() {
        state.black_buffer_mut().update((size.w, size.h), [0.0, 0.0, 0.0, 1.0]);
        let solid = SolidColorRenderElement::from_buffer(
            state.black_buffer_mut(),
            (0, 0),
            Scale::from(1.0),
            1.0,
            Kind::Unspecified,
        );
        elements.push(HartRenderElement::Solid(solid));
    } else {
        // ── 1. SOFTWARE CURSOR (below killswitch, above windows). ──
        build_cursor_elements(state, renderer, &mut elements);
    }

    // ── 1b. NATIVE SHELL M3 scene (gated OFF by default via native_shell_on). Pushed
    //    here so native chrome sits above the app windows and below the cursor. A pure
    //    additive path: flag off = no-op, the WebView shell is untouched. ──
    if state.native_shell_on() {
        render_native_scene(state, renderer, size, &mut elements);
    }

    let ws_alpha = workspace_fade_alpha(state);
    let output = state.output().clone();
    let mut layers_painted = 0usize;

    // ── 2. TOP / OVERLAY layer surfaces — ABOVE the toplevels. ──
    // wlr-layer-shell stacks background < bottom < WINDOWS < top < overlay, and
    // `surface_under` has always routed clicks that way (its own docstring says
    // so). The renderer did not: every layer, whatever its layer, was painted
    // below every window. So a Top-layer panel took clicks in a strip where it
    // was nowhere to be seen -- pixels said one thing and input another, which
    // is unusable for exactly the panels and notification surfaces the Top layer
    // exists for. `layer_is_above_windows` is now the single statement of that
    // order, shared by both.
    layers_painted += push_layer_elements(
        renderer, &mut elements, &output, ws_alpha, /* above_windows = */ true);

    // ── 3. WINDOW TOPLEVELS (below Top/Overlay layers and the cursor). ──
    let windows: Vec<Window> = state.space().elements().rev().cloned().collect();
    for window in &windows {
        let loc = state.space().element_location(window).unwrap_or_default();
        let phys = loc.to_physical_precise_round(1.0);
        let map_alpha = window
            .user_data()
            .get::<MapAnim>()
            .map(|a| a.alpha())
            .unwrap_or(1.0);
        let alpha = (map_alpha * ws_alpha).clamp(0.0, 1.0);
        if alpha < 0.999 && std::env::var_os("HART_COMP_DEBUG_FADE").is_some() {
            let handle = window
                .user_data()
                .get::<WindowHandle>()
                .map(|h| h.as_str().to_string())
                .unwrap_or_default();
            info!(handle = %handle, map_alpha, ws_alpha, alpha, "effect.fade (sub-1.0 alpha → renderer)");
        }
        let win_elems: Vec<WaylandSurfaceRenderElement<R>> =
            AsRenderElements::<R>::render_elements(window, renderer, phys, Scale::from(1.0), alpha);
        elements.extend(win_elems.into_iter().map(HartRenderElement::Surface));
    }

    // ── 4. BOTTOM / BACKGROUND layer surfaces — BELOW the toplevels. ──
    // This is the desktop plane: the HART glass shell anchors here, which is what
    // makes it the desktop rather than an app.
    layers_painted += push_layer_elements(
        renderer, &mut elements, &output, ws_alpha, /* above_windows = */ false);
    {
        let prev = LAYERS_PAINTED.swap(layers_painted, std::sync::atomic::Ordering::Relaxed);
        if prev != layers_painted {
            info!(layers_painted, "layer.composited (wlr-layer surfaces now in the rendered frame)");
        }
    }

    // ── 3b. NATIVE ORB (NATIVE SHELL M2), above the backdrop, below the shell. ──
    // Placed here in the z-order so it composites over the bloom but under the
    // layer surfaces, matching M1's staging: while the HTML shell still paints an
    // opaque background this is OCCLUDED, exactly as the bloom is. It is wired
    // now rather than later so the module cannot rot unreferenced — which is
    // precisely how bloom.rs sat dormant from 2026-07-20 to 2026-08-27.
    //
    // The whole point of M2 is here: `motion` is two floats from a clock, handed
    // to the element as `alpha` and `size`. The GPU scales and blends a texture
    // composed once. No pixel is touched by the CPU per frame, which is the
    // difference between this and the ~5.4s/6s of userspace rasterisation
    // measured in WebKit while it breathed the same orb.
    if !state.capture_blocked() && !state.native_shell_on() {
        // Placement per checklist rule c7: "Home mode: orb floats to the RIGHT
        // of the hero copy". An earlier draft centred it, which contradicts a
        // binding rule — the checklist is the instruction record, not a
        // suggestion. Vertically it sits on the hero's own line
        // (`.hart-hero{top:46%}`), sized as a fraction of the short edge.
        //
        // Still PROVISIONAL: the scene owns this once A2UI drives the native
        // tree (M4), and c7's compact orb-sm docked in the top bar is not
        // modelled here at all. Hard-coding a c7-shaped default keeps M2 to one
        // new idea while not shipping a placement the checklist forbids.
        let short = size.w.min(size.h);
        let side = (short as f32 * 0.30) as i32;
        let energy = state.orb_energy();
        if let Some((buffer, motion)) = state.orb_mut().current(side, energy) {
            // Breathing scales about the CENTRE, so the top-left moves by half
            // the growth. Computed from the motion rather than stored, so there
            // is no second source of truth for where the orb is.
            let drawn = (side as f32 * motion.scale) as i32;
            // 0.72 of the width = right of the hero copy (c7), not centred.
            let cx = (size.w as f32 * 0.72) as i32;
            let cy = (size.h as f32 * 0.46) as i32;
            let origin: Point<f64, Physical> =
                Point::from(((cx - drawn / 2) as f64, (cy - drawn / 2) as f64));
            match MemoryRenderBufferRenderElement::from_buffer(
                renderer,
                origin,
                buffer,
                Some(motion.alpha),
                None,
                Some((drawn, drawn).into()),
                Kind::Unspecified,
            ) {
                Ok(e) => {
                    elements.push(HartRenderElement::Memory(e));
                    native_mask |= NATIVE_CHROME_ORB;
                }
                // Never fatal: a missing orb is a desktop without an orb, not a
                // dead session. Same posture as the backdrop below. Note the
                // mask is NOT set here — a failed import must never let the
                // shell hide its own orb.
                Err(err) => warn!(?err, "orb: failed to import the composed orb"),
            }
        }
    }

    // ── 4. BLOOM BACKDROP (last in the list = drawn UNDER everything). ──
    // NATIVE SHELL M1. Before this, the bottom of the frame was the flat
    // HART_SPLASH_RGBA clear and the aurora was painted by a browser in a
    // WebView above it. Now the compositor owns its own backdrop.
    //
    // Deliberately built LAST and pushed LAST: it must sit beneath the layer
    // surfaces (the glass shell) so a shell that paints transparency reveals the
    // native field rather than flat slate. The clear colour still runs, so if
    // this element is skipped the frame is exactly what it was before.
    //
    // Cheap by construction: `BloomCache::get` is a key comparison on every
    // frame but the first at a given size/theme.
    if !state.capture_blocked() {
        // Split the borrow: `bloom_mut` holds `state` mutably, and
        // `MemoryRenderBufferRenderElement::from_buffer` needs the buffer while
        // `renderer` is also borrowed. They are disjoint (`renderer` is a separate
        // parameter, not a `state` field), so this type-checks and stays short.
        if let Some(buffer) = state.bloom_mut().get(size.w, size.h) {
            let origin: Point<f64, Physical> = Point::from((0.0, 0.0));
            match MemoryRenderBufferRenderElement::from_buffer(
                renderer,
                origin,
                buffer,
                None,
                None,
                None,
                Kind::Unspecified,
            ) {
                Ok(e) => {
                    elements.push(HartRenderElement::Memory(e));
                    native_mask |= NATIVE_CHROME_BLOOM;
                }
                // Never fatal: without the backdrop the clear colour shows, which
                // is precisely the pre-M1 desktop. A failed import must not cost
                // the user their session, and must NOT set the mask — the shell
                // would then go transparent over a backdrop we never drew.
                Err(err) => warn!(?err, "bloom: failed to import the backdrop; falling back to the clear colour"),
            }
        }
    }

    // Publish what this frame contains. The backend turns it into the shell's
    // verdict ONLY after the frame is actually presented, so a composed-but-
    // never-flipped frame can never make the shell go transparent.
    NATIVE_CHROME_EMITTED.store(native_mask, std::sync::atomic::Ordering::Relaxed);

    elements
}

/// Draw a built element list into an already-bound frame, clearing to `clear` first.
/// Used by the winit render path (which acquires the `Frame` via `renderer.render(...)`
/// then paints this slice). The DRM path does NOT call this — `DrmCompositor::render_frame`
/// owns the clear + draw, taking the element slice directly — so this is gated to the
/// `winit` feature (it would otherwise be dead code on a smithay-only build).
#[cfg(feature = "winit")]
pub fn draw_elements<R>(
    frame: &mut <R as RendererSuper>::Frame<'_, '_>,
    clear: Color32F,
    elements: &[HartRenderElement<R>],
    damage: &[Rectangle<i32, Physical>],
) -> Result<(), <R as RendererSuper>::Error>
where
    R: Renderer + ImportAll + ImportMem,
    R::TextureId: Clone + 'static,
{
    frame.clear(clear, damage)?;
    draw_render_elements(frame, 1.0, elements, damage)?;
    Ok(())
}

// ════════════════════════════════════════════════════════════════════════════
// PURE free fns — `process_keyboard_shortcut` (the chord map) + `bake_default_cursor`
// (the dependency-free arrow). Both hoisted VERBATIM from winit.rs; both unit-tested.
// ════════════════════════════════════════════════════════════════════════════

/// Map a chord to a compositor `WmAction`, or `None` to forward the key to the focused
/// client. Modelled 1:1 on anvil's `process_keyboard_shortcut`. `keysym` is the MODIFIED
/// sym (letter/arrow/Tab chords); `digit_sym` is the LAYOUT-AGNOSTIC level-0 sym (the
/// digit row ONLY) — because Shift maps US digits to `!@#$%^&*(` (NOT a uniform offset),
/// so matching the modified sym against `KEY_1..=KEY_9` would make every Super+Shift+N
/// "move to workspace" chord silently fail.
pub fn process_keyboard_shortcut(
    mods: ModifiersState,
    keysym: Keysym,
    digit_sym: Option<Keysym>,
) -> Option<WmAction> {
    let workspace_digit = digit_sym
        .map(|s| s.raw())
        .filter(|raw| (xkb::KEY_1..=xkb::KEY_9).contains(raw))
        .map(|raw| (raw - xkb::KEY_1) as usize);
    if mods.alt && !mods.logo {
        if mods.shift && (keysym == Keysym::ISO_Left_Tab || keysym == Keysym::Tab) {
            return Some(WmAction::CycleFocusBack);
        }
        if keysym == Keysym::Tab {
            return Some(WmAction::CycleFocus);
        }
    }
    if mods.logo {
        if mods.shift {
            if let Some(n) = workspace_digit {
                return Some(WmAction::MoveToWorkspace(n));
            }
        }
        if !mods.shift {
            if let Some(n) = workspace_digit {
                return Some(WmAction::SwitchWorkspace(n));
            }
        }
        if keysym == Keysym::q {
            return Some(WmAction::CloseFocused);
        }
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
        if keysym == Keysym::d {
            return Some(WmAction::ShowDesktop);
        }
    }
    None
}

/// Bake a small default arrow cursor as RGBA bytes — a dependency-free fallback so a
/// visible cursor renders on llvmpipe with no xcursor theme load. Returns (rgba, width,
/// height, hotspot). The hotspot is the arrow TIP (top-left).
pub fn bake_default_cursor() -> (Vec<u8>, i32, i32, Point<i32, Logical>) {
    const W: i32 = 24;
    const H: i32 = 24;
    let poly: [(f32, f32); 7] = [
        (0.0, 0.0),
        (0.0, 17.0),
        (4.0, 13.0),
        (7.0, 19.0),
        (10.0, 18.0),
        (7.0, 12.0),
        (12.0, 12.0),
    ];
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
                rgba[idx] = 255;
                rgba[idx + 1] = 255;
                rgba[idx + 2] = 255;
                rgba[idx + 3] = 255;
            } else if outline {
                rgba[idx] = 0;
                rgba[idx + 1] = 0;
                rgba[idx + 2] = 0;
                rgba[idx + 3] = 255;
            }
        }
    }
    (rgba, W, H, Point::from((0, 0)))
}

// ════════════════════════════════════════════════════════════════════════════
// Behavioural unit floor for the PURE helpers (chord map + cursor bake + fade clocks).
// These need no live renderer/seat, so they assert the contract on the dev box. The
// EXECUTORS (focus/place/close/workspace) are exercised live via the IPC verbs against
// $HART_SOCK (they call these SAME bodies). Hoisted from winit.rs's test modules.
// ════════════════════════════════════════════════════════════════════════════
#[cfg(test)]
mod tests {
    use super::*;

    fn mods(logo: bool, alt: bool, shift: bool) -> ModifiersState {
        ModifiersState { logo, alt, shift, ..Default::default() }
    }
    fn chord(m: ModifiersState, keysym: Keysym) -> Option<WmAction> {
        process_keyboard_shortcut(m, keysym, Some(keysym))
    }
    fn digit_chord(m: ModifiersState, modified: Keysym, level0: Keysym) -> Option<WmAction> {
        process_keyboard_shortcut(m, modified, Some(level0))
    }

    // ════════════════════════════════════════════════════════════════════════
    // layer_is_above_windows — the ONE statement of wlr-layer-shell stacking,
    // shared by the renderer and the pointer hit-test. They disagreed: the
    // hit-test routed Overlay/Top above windows from the start while the frame
    // builder painted every layer below every window, so a Top-layer panel took
    // clicks in a strip where nothing of it was visible.
    // ════════════════════════════════════════════════════════════════════════

    #[test]
    fn top_and_overlay_are_above_the_toplevels() {
        assert!(layer_is_above_windows(WlrLayer::Top));
        assert!(layer_is_above_windows(WlrLayer::Overlay));
    }

    #[test]
    fn background_and_bottom_are_below_the_toplevels() {
        // The HART glass shell anchors to Background. That is what makes it the
        // desktop rather than an app, and it must keep sitting under windows.
        assert!(!layer_is_above_windows(WlrLayer::Background));
        assert!(!layer_is_above_windows(WlrLayer::Bottom));
    }

    #[test]
    fn every_layer_falls_on_exactly_one_side() {
        // A partition, not a filter: each of the four layers is either above or
        // below, so the two push_layer_elements passes paint each surface once.
        // If a layer were ever missed by both, it would silently vanish.
        let all = [WlrLayer::Background, WlrLayer::Bottom, WlrLayer::Top, WlrLayer::Overlay];
        let above = all.iter().filter(|l| layer_is_above_windows(**l)).count();
        assert_eq!(above, 2);
        assert_eq!(all.len() - above, 2);
    }

    #[test]
    fn the_renderer_and_the_hit_test_use_the_same_rule() {
        // surface_under tries Overlay then Top BEFORE space().element_under, and
        // Bottom then Background after it. Those are the two groups this function
        // returns, so the pixels and the pointer cannot drift apart again without
        // this assertion failing.
        for layer in [WlrLayer::Overlay, WlrLayer::Top] {
            assert!(layer_is_above_windows(layer),
                    "{layer:?} is hit-tested before windows, so it must paint above them");
        }
        for layer in [WlrLayer::Bottom, WlrLayer::Background] {
            assert!(!layer_is_above_windows(layer),
                    "{layer:?} is hit-tested after windows, so it must paint below them");
        }
    }

    // ════════════════════════════════════════════════════════════════════════
    // work_area — the panel reservation. The HART taskbar lives INSIDE the single
    // Background layer surface, so it cannot claim an exclusive zone of its own;
    // the compositor reserves the strip for it instead. Every window-placement
    // path resolves through here, so these cases cover maximize, all nine snap
    // zones, all five tiling layouts and the new-window cascade at once.
    // ════════════════════════════════════════════════════════════════════════

    /// The HART shell's real chrome: a 40px top bar and a 44px bottom taskbar,
    /// both read off the served CSS (--hart-topbar-height and .taskbar height).
    fn hart_chrome() -> PanelReservation {
        PanelReservation { top: 40, bottom: 44 }
    }

    #[test]
    fn no_reservation_leaves_the_output_untouched() {
        // The pre-existing behaviour, and what every node without a published
        // reservation keeps getting.
        let none = PanelReservation::default();
        assert_eq!(work_area(0, 0, 1600, 900, none), (0, 0, 1600, 900));
    }

    #[test]
    fn both_edges_are_taken_off_the_usable_area() {
        let (x, y, w, h) = work_area(0, 0, 1600, 900, hart_chrome());
        assert_eq!((x, y, w, h), (0, 40, 1600, 816));
        assert_eq!(y + h, 900 - 44, "the bottom taskbar must stay clear too");
    }

    #[test]
    fn a_reservation_respects_a_non_zero_output_origin() {
        // Multi-output layouts put the origin somewhere other than 0,0.
        assert_eq!(work_area(1600, 100, 1280, 800, hart_chrome()),
                   (1600, 140, 1280, 716));
    }

    #[test]
    fn an_absurd_reservation_cannot_squeeze_the_desktop_to_nothing() {
        // These numbers cross a process boundary as a text file. A desktop with no
        // room for windows is a worse failure than a bar that overlaps one.
        let absurd = PanelReservation { top: 100_000, bottom: 100_000 };
        let (_, y, _, h) = work_area(0, 0, 1600, 900, absurd);
        assert!(h >= 450, "at least half the output must stay usable, got {h}");
        assert!(y <= 450);
    }

    #[test]
    fn two_oversized_edges_shrink_in_proportion_rather_than_one_winning() {
        // Each edge is capped at half the output (450) first, so 600/200 becomes
        // 450/200, then the pair is scaled to fit 450 total: 311 top, 139 bottom.
        // Both edges survive; neither is zeroed out to let the other have its way.
        let greedy = PanelReservation { top: 600, bottom: 200 };
        let (_, y, _, h) = work_area(0, 0, 1600, 900, greedy);
        assert_eq!(h, 450);
        assert!(y > 300 && y < 380, "top edge kept its share, got {y}");
    }

    #[test]
    fn a_negative_reservation_is_ignored_rather_than_growing_the_area() {
        let bad = PanelReservation { top: -40, bottom: -44 };
        assert_eq!(work_area(0, 0, 1600, 900, bad), (0, 0, 1600, 900));
    }

    #[test]
    fn maximize_over_the_work_area_stops_at_the_chrome() {
        // The user-visible bug: a maximized window covered the bar and there was no
        // way back to Home/Agents/Apps without minimizing it.
        let (ax, ay, aw, ah) = work_area(0, 0, 1600, 900, hart_chrome());
        let (x, y, w, h) = zone_rect(ax, ay, aw, ah, "maximize").unwrap();
        assert_eq!((x, y, w, h), (0, 40, 1600, 816));
        assert!(y >= 40, "a maximized window must start below the top bar");
        assert!(y + h <= 900 - 44, "and stop above the taskbar");
    }

    #[test]
    fn no_snap_zone_reaches_into_either_bar() {
        let c = hart_chrome();
        let (ax, ay, aw, ah) = work_area(0, 0, 1600, 900, c);
        for zone in ["left-half", "right-half", "top-half", "bottom-half", "top-left",
                     "top-right", "bottom-left", "bottom-right", "center",
                     "maximize", "fullscreen"] {
            let (_, y, _, h) = zone_rect(ax, ay, aw, ah, zone).unwrap();
            assert!(y >= c.top, "zone {zone} starts at y={y}, inside the top bar");
            assert!(y + h <= 900 - c.bottom,
                    "zone {zone} ends at y={}, inside the taskbar", y + h);
        }
    }

    #[test]
    fn no_tiling_layout_paves_over_either_bar() {
        // A tiler that ignored the chrome would hide the bars behind EVERY window,
        // which is the worst case of the lot.
        let c = hart_chrome();
        let (ax, ay, aw, ah) = work_area(0, 0, 1600, 900, c);
        for layout in ["grid", "cols", "columns", "rows", "master-stack", "fullscreen"] {
            for n in 1..=6 {
                for (_, y, _, h) in tile_rects(ax, ay, aw, ah, n, layout) {
                    assert!(y >= c.top,
                            "layout {layout} n={n} placed a tile at y={y}");
                    assert!(y + h <= 900 - c.bottom,
                            "layout {layout} n={n} tile ends at y={}", y + h);
                }
            }
        }
    }

    #[test]
    fn the_published_format_parses_both_edges() {
        let r = parse_panel_reservation("top=40\nbottom=44\n");
        assert_eq!(r, PanelReservation { top: 40, bottom: 44 });
    }

    #[test]
    fn junk_in_the_published_file_reserves_nothing_rather_than_guessing() {
        // Every one of these is a way the file could be wrong in the field.
        for junk in ["", "garbage", "top=", "top=abc", "top=-5", "=40", "top:40",
                     "\n\n\n", "top=40px"] {
            assert_eq!(parse_panel_reservation(junk), PanelReservation::default(),
                       "{junk:?} should have reserved nothing");
        }
    }

    #[test]
    fn a_partial_or_extended_file_still_works() {
        // One edge only, and an edge this build has never heard of. Neither may
        // break the ones it does understand -- that is what lets an older
        // compositor keep running against a newer shell.
        assert_eq!(parse_panel_reservation("top=40"),
                   PanelReservation { top: 40, bottom: 0 });
        assert_eq!(parse_panel_reservation("top=40\nleft=64\nbottom=44"),
                   PanelReservation { top: 40, bottom: 44 });
    }

    #[test]
    fn a_missing_file_reserves_nothing() {
        // Fail-safe: this runs on a dev box with no /run/hart, and on a node that
        // has not published anything yet. Either way the answer is "no
        // reservation", so the compositor half ships inert ahead of the shell half.
        assert_eq!(panel_reservation(), PanelReservation::default());
    }

    // ════════════════════════════════════════════════════════════════════════
    // clamp_region — the screencopy no-out-of-bounds gate (hoisted here from
    // screencopy.rs so the smithay-feature doCheck exercises it; screencopy.rs is
    // winit-only and never compiles under `--features smithay`).
    // ════════════════════════════════════════════════════════════════════════

    #[test]
    fn clamp_region_passes_through_an_in_bounds_rect() {
        let r = clamp_region(100, 50, 320, 240, 1920, 1080);
        assert_eq!((r.loc.x, r.loc.y), (100, 50));
        assert_eq!((r.size.w, r.size.h), (320, 240));
    }

    #[test]
    fn clamp_region_full_output_is_the_whole_framebuffer() {
        let r = clamp_region(0, 0, 1920, 1080, 1920, 1080);
        assert_eq!((r.loc.x, r.loc.y), (0, 0));
        assert_eq!((r.size.w, r.size.h), (1920, 1080));
    }

    #[test]
    fn clamp_region_negative_origin_is_pinned_to_zero() {
        let r = clamp_region(-50, -30, 200, 200, 1920, 1080);
        assert_eq!((r.loc.x, r.loc.y), (0, 0), "negative origin clamps to (0,0)");
        assert_eq!((r.size.w, r.size.h), (200, 200));
    }

    #[test]
    fn clamp_region_oversized_width_is_trimmed_to_the_right_edge() {
        // Origin at x=1800 on a 1920-wide output: only 120px remain, so an asked-for
        // 500px width is trimmed so rx+rw never exceeds out_w.
        let r = clamp_region(1800, 0, 500, 100, 1920, 1080);
        assert_eq!(r.loc.x, 1800);
        assert_eq!(r.size.w, 120, "width trimmed so rx+rw == out_w (1920)");
        assert_eq!(r.loc.x + r.size.w, 1920);
    }

    #[test]
    fn clamp_region_oversized_height_is_trimmed_to_the_bottom_edge() {
        let r = clamp_region(0, 1000, 100, 500, 1920, 1080);
        assert_eq!(r.loc.y, 1000);
        assert_eq!(r.size.h, 80, "height trimmed so ry+rh == out_h (1080)");
        assert_eq!(r.loc.y + r.size.h, 1080);
    }

    #[test]
    fn clamp_region_zero_or_negative_size_floors_to_one_px() {
        // width/height ≥ 1 always (a 0-px or negative request would make an empty
        // framebuffer read-back the ExportMem contract rejects).
        let r = clamp_region(10, 10, 0, -5, 1920, 1080);
        assert_eq!(r.size.w, 1, "width floors to 1");
        assert_eq!(r.size.h, 1, "height floors to 1");
    }

    #[test]
    fn clamp_region_origin_past_the_far_edge_still_yields_a_valid_one_px_rect() {
        // x beyond out_w: rx pins to out_w, then rw = (out_w - rx).max(1) = 1 — the
        // rect is degenerate-but-valid (1px), never out-of-bounds or empty.
        let r = clamp_region(5000, 5000, 100, 100, 1920, 1080);
        assert_eq!(r.loc.x, 1920);
        assert_eq!(r.loc.y, 1080);
        assert_eq!(r.size.w, 1);
        assert_eq!(r.size.h, 1);
    }

    #[test]
    fn clamp_region_zero_output_still_yields_a_valid_one_px_rect() {
        // Defensive (output is never 0x0 in practice): a 0-wide/0-tall output would make
        // `out_w - rx == 0`, which a bare `.min()` would let through as a 0-sized rect —
        // violating the width/height ≥ 1 doc invariant the read-back relies on. The
        // explicit `.max(1)` after the right-edge clamp floors BOTH axes to 1px.
        let r = clamp_region(0, 0, 100, 100, 0, 0);
        assert_eq!((r.loc.x, r.loc.y), (0, 0));
        assert_eq!(r.size.w, 1, "width floors to 1 even on a 0-wide output");
        assert_eq!(r.size.h, 1, "height floors to 1 even on a 0-tall output");
    }

    // ════════════════════════════════════════════════════════════════════════
    // advance_and_clamp_pointer — THE #134 relative-motion math. This is the load-
    // bearing half of the real-hardware pointer fix: `on_pointer_move_relative` reads
    // `current_location()`, calls THIS to apply the libinput delta + clamp to the
    // output, then sends the absolute `motion`. A winit-only test never exercised it
    // (winit emits only ABSOLUTE motion), which is exactly how the frozen-at-(0,0)
    // regression shipped. These assert the contract a live Seat then forwards.
    // ════════════════════════════════════════════════════════════════════════

    fn out_geo(x: i32, y: i32, w: i32, h: i32) -> Rectangle<i32, Logical> {
        Rectangle::new((x, y).into(), (w, h).into())
    }

    #[test]
    fn pointer_relative_delta_advances_the_cursor() {
        // THE anti-regression: a relative motion MUST move the cursor (the dropped-event
        // bug left every delta unapplied, pinning the cursor at the origin). An in-bounds
        // delta from (100,100) lands exactly at (105, 97).
        let next = advance_and_clamp_pointer(
            Point::from((100.0, 100.0)),
            Point::from((5.0, -3.0)),
            out_geo(0, 0, 1920, 1080),
        );
        assert_eq!((next.x, next.y), (105.0, 97.0));
    }

    #[test]
    fn pointer_relative_motion_off_the_origin_is_not_pinned() {
        // The literal #134 symptom guard: starting at (0,0) a positive delta yields a
        // location that is NO LONGER (0,0) — proving relative motion unfreezes the cursor.
        let next = advance_and_clamp_pointer(
            Point::from((0.0, 0.0)),
            Point::from((12.0, 8.0)),
            out_geo(0, 0, 1920, 1080),
        );
        assert_ne!((next.x, next.y), (0.0, 0.0));
        assert_eq!((next.x, next.y), (12.0, 8.0));
    }

    #[test]
    fn pointer_clamps_to_the_right_and_bottom_edge() {
        // A big delta near the far corner pins exactly to the output edge (anvil rests the
        // cursor ON the edge, [loc, loc+size]), never past the framebuffer.
        let next = advance_and_clamp_pointer(
            Point::from((1915.0, 1075.0)),
            Point::from((50.0, 50.0)),
            out_geo(0, 0, 1920, 1080),
        );
        assert_eq!((next.x, next.y), (1920.0, 1080.0));
    }

    #[test]
    fn pointer_clamps_to_the_left_and_top_edge() {
        // A negative delta past the origin pins to (0,0) — the cursor can never go
        // negative (off the top-left of the framebuffer).
        let next = advance_and_clamp_pointer(
            Point::from((5.0, 5.0)),
            Point::from((-50.0, -50.0)),
            out_geo(0, 0, 1920, 1080),
        );
        assert_eq!((next.x, next.y), (0.0, 0.0));
    }

    #[test]
    fn pointer_clamp_honours_a_nonzero_output_origin() {
        // An inset/multi-monitor output at (100,200): the clamp window is
        // [100,900]x[200,800], so a far-negative delta pins to the output's own origin,
        // not to global (0,0).
        let geo = out_geo(100, 200, 800, 600);
        let pinned = advance_and_clamp_pointer(
            Point::from((110.0, 210.0)),
            Point::from((-500.0, -500.0)),
            geo,
        );
        assert_eq!((pinned.x, pinned.y), (100.0, 200.0));
        let far = advance_and_clamp_pointer(
            Point::from((850.0, 750.0)),
            Point::from((500.0, 500.0)),
            geo,
        );
        assert_eq!((far.x, far.y), (900.0, 800.0));
    }

    #[test]
    fn pointer_pre_mode_zero_output_applies_the_raw_delta() {
        // Before a real mode latches the output can be 0-sized; clamping to it would pin
        // the cursor to the origin forever. The helper leaves a 0-sized axis UNCLAMPED so
        // motion still flows until the real mode arrives.
        let next = advance_and_clamp_pointer(
            Point::from((40.0, 30.0)),
            Point::from((10.0, 10.0)),
            out_geo(0, 0, 0, 0),
        );
        assert_eq!((next.x, next.y), (50.0, 40.0), "no clamp on a 0-sized output");
    }

    // NOTE: the keyboard-focus-on-map path (`focus_desktop_shell_if_idle`) and the live
    // forwarding of a relative `MotionEvent` through the Seat both need a live
    // Display/Seat/layer-map, so they are exercised on the real-HW boot (the flash) and
    // the QEMU/winit integration session, not this pure dev-box floor.

    // ════════════════════════════════════════════════════════════════════════
    // transform_region — upright-capture region map under the output transform.
    // ════════════════════════════════════════════════════════════════════════

    #[test]
    fn transform_region_normal_is_identity() {
        let region = Rectangle::new((100, 50).into(), (320, 240).into());
        let out = (1920, 1080).into();
        let mapped = transform_region(region, out, Transform::Normal);
        assert_eq!(mapped, region, "Normal transform leaves the region unchanged");
    }

    #[test]
    fn transform_region_full_output_under_flipped180_is_the_same_rect() {
        // Flipped180 is a Y-axis flip; the FULL-output rect maps back onto itself (the M6
        // winit render transform is Flipped180; a full-screen grab is unaffected).
        let region = Rectangle::new((0, 0).into(), (1920, 1080).into());
        let out = (1920, 1080).into();
        let mapped = transform_region(region, out, Transform::Flipped180);
        assert_eq!((mapped.size.w, mapped.size.h), (1920, 1080));
        assert_eq!((mapped.loc.x, mapped.loc.y), (0, 0));
    }

    #[test]
    fn transform_region_subregion_under_flipped180_flips_only_the_y_axis() {
        // Flipped180 is a Y-axis flip (NOT a full point reflection): the x origin is
        // preserved, the y origin maps to `area.h - y - height`, and the size is
        // unchanged. A top-left 100x100 rect at (0,0) maps to the BOTTOM-LEFT corner
        // (x stays 0, y becomes 1080-0-100). (Matches Smithay's own
        // `transform_rect_f180` semantics for `Transform::Flipped180`.)
        let region = Rectangle::new((0, 0).into(), (100, 100).into());
        let out = (1920, 1080).into();
        let mapped = transform_region(region, out, Transform::Flipped180);
        assert_eq!((mapped.size.w, mapped.size.h), (100, 100), "size preserved");
        assert_eq!(mapped.loc.x, 0, "x origin is NOT flipped by Flipped180");
        assert_eq!(mapped.loc.y, 1080 - 100, "y origin flips to area.h - y - height");
    }

    // ════════════════════════════════════════════════════════════════════════
    // now_secs_nsecs — the `ready` presentation timestamp split.
    // ════════════════════════════════════════════════════════════════════════

    #[test]
    fn now_secs_nsecs_is_a_plausible_wall_clock() {
        let (sec, nsec) = now_secs_nsecs();
        // Well after 2021 (1.6e9) and the nanosecond part is a valid sub-second value.
        assert!(sec > 1_600_000_000, "seconds is a real UNIX wall clock: {sec}");
        assert!(nsec < 1_000_000_000, "nsec is a sub-second remainder: {nsec}");
    }

    #[test]
    fn ready_timestamp_hi_lo_split_round_trips() {
        // The wire splits the u64 seconds into (hi, lo) u32 halves for `ready`. Prove
        // the split the render path uses reconstructs the original on a value whose hi
        // half is non-zero (so a truncating split would be caught).
        let sec: u64 = 0x0000_0001_2345_6789;
        let hi = (sec >> 32) as u32;
        let lo = (sec & 0xFFFF_FFFF) as u32;
        assert_eq!(hi, 1);
        assert_eq!(lo, 0x2345_6789);
        assert_eq!(((hi as u64) << 32) | lo as u64, sec);
    }

    // ════════════════════════════════════════════════════════════════════════
    // zone_rect — the §4.4 snap-zone geometry (pure, extracted from ipc_zone_rect).
    // Every zone is asserted against a 1920x1080 output at origin (0,0).
    // ════════════════════════════════════════════════════════════════════════

    #[test]
    fn zone_rect_covers_every_named_zone() {
        let (ow, oh) = (1920, 1080);
        assert_eq!(zone_rect(0, 0, ow, oh, "left-half"), Some((0, 0, 960, 1080)));
        assert_eq!(zone_rect(0, 0, ow, oh, "right-half"), Some((960, 0, 960, 1080)));
        assert_eq!(zone_rect(0, 0, ow, oh, "top-half"), Some((0, 0, 1920, 540)));
        assert_eq!(zone_rect(0, 0, ow, oh, "bottom-half"), Some((0, 540, 1920, 540)));
        assert_eq!(zone_rect(0, 0, ow, oh, "top-left"), Some((0, 0, 960, 540)));
        assert_eq!(zone_rect(0, 0, ow, oh, "top-right"), Some((960, 0, 960, 540)));
        assert_eq!(zone_rect(0, 0, ow, oh, "bottom-left"), Some((0, 540, 960, 540)));
        assert_eq!(zone_rect(0, 0, ow, oh, "bottom-right"), Some((960, 540, 960, 540)));
        assert_eq!(zone_rect(0, 0, ow, oh, "center"), Some((480, 270, 960, 540)));
        assert_eq!(zone_rect(0, 0, ow, oh, "maximize"), Some((0, 0, 1920, 1080)));
        assert_eq!(zone_rect(0, 0, ow, oh, "fullscreen"), Some((0, 0, 1920, 1080)));
    }

    #[test]
    fn zone_rect_unknown_zone_is_none() {
        assert_eq!(zone_rect(0, 0, 1920, 1080, "nope"), None);
        assert_eq!(zone_rect(0, 0, 1920, 1080, ""), None);
    }

    #[test]
    fn zone_rect_honours_a_nonzero_output_origin() {
        // A multi-monitor/inset output at (100, 200): zones are offset by the origin.
        assert_eq!(zone_rect(100, 200, 1920, 1080, "right-half"), Some((100 + 960, 200, 960, 1080)));
        assert_eq!(zone_rect(100, 200, 1920, 1080, "bottom-right"), Some((100 + 960, 200 + 540, 960, 540)));
    }

    #[test]
    fn zone_rect_left_right_halves_tile_an_odd_width_with_no_seam() {
        // ODD width 1921: left = 1921/2 = 960, right = 1921 - 960 = 961. The two halves
        // butt edge-to-edge AND together cover the full width — no 1px gap or overlap.
        let ow = 1921;
        let (lx, _ly, lw, _lh) = zone_rect(0, 0, ow, 1080, "left-half").unwrap();
        let (rx, _ry, rw, _rh) = zone_rect(0, 0, ow, 1080, "right-half").unwrap();
        assert_eq!(lx + lw, rx, "right-half begins exactly where left-half ends (no seam)");
        assert_eq!(rx + rw, ow, "right edge of right-half reaches the full width");
    }

    // ════════════════════════════════════════════════════════════════════════
    // tile_rects — the §4.5 tile geometry (pure, extracted from ipc_tile).
    // ════════════════════════════════════════════════════════════════════════

    #[test]
    fn tile_rects_empty_for_zero_windows() {
        assert!(tile_rects(0, 0, 1920, 1080, 0, "grid").is_empty());
    }

    #[test]
    fn tile_rects_fullscreen_stacks_all_on_the_whole_output() {
        let r = tile_rects(0, 0, 1920, 1080, 3, "fullscreen");
        assert_eq!(r.len(), 3);
        for cell in &r {
            assert_eq!(*cell, (0, 0, 1920, 1080));
        }
    }

    #[test]
    fn tile_rects_cols_splits_the_width_evenly() {
        // 4 windows, 1920 wide → 4 columns of 480, full height, butting edge-to-edge.
        let r = tile_rects(0, 0, 1920, 1080, 4, "cols");
        assert_eq!(r, vec![
            (0, 0, 480, 1080),
            (480, 0, 480, 1080),
            (960, 0, 480, 1080),
            (1440, 0, 480, 1080),
        ]);
    }

    #[test]
    fn tile_rects_rows_splits_the_height_evenly() {
        let r = tile_rects(0, 0, 1920, 900, 3, "rows");
        assert_eq!(r, vec![
            (0, 0, 1920, 300),
            (0, 300, 1920, 300),
            (0, 600, 1920, 300),
        ]);
    }

    #[test]
    fn tile_rects_master_stack_one_window_is_fullscreen() {
        assert_eq!(tile_rects(0, 0, 1920, 1080, 1, "master-stack"), vec![(0, 0, 1920, 1080)]);
    }

    #[test]
    fn tile_rects_master_stack_master_plus_stack() {
        // 3 windows: master = left half full height; the other 2 stack the right half.
        let r = tile_rects(0, 0, 1920, 1080, 3, "master-stack");
        assert_eq!(r[0], (0, 0, 960, 1080), "master is the left half, full height");
        assert_eq!(r[1], (960, 0, 960, 540), "stack[0] is top of the right half");
        assert_eq!(r[2], (960, 540, 960, 540), "stack[1] is bottom of the right half");
    }

    #[test]
    fn tile_rects_grid_is_the_default_layout() {
        // 4 windows → 2x2 grid of 960x540. Unknown layout name falls through to grid.
        let grid = tile_rects(0, 0, 1920, 1080, 4, "grid");
        let unknown = tile_rects(0, 0, 1920, 1080, 4, "whatever");
        assert_eq!(grid, unknown, "an unknown layout name defaults to grid");
        assert_eq!(grid, vec![
            (0, 0, 960, 540),
            (960, 0, 960, 540),
            (0, 540, 960, 540),
            (960, 540, 960, 540),
        ]);
    }

    #[test]
    fn tile_rects_grid_nondivisible_width_leaves_a_documented_edge_gap() {
        // 7 windows on a 1920-wide output: cols = ceil(sqrt(7)) = 3, cw = 1920/3 = 640.
        // The grid is 3 cols x 3 rows (last row holds 1). The RIGHTMOST column starts at
        // 2*640 = 1280 and ends at 1280+640 = 1920 — exact here (1920 % 3 == 0). To force
        // an indivisible case, use ow = 1922: cw = 640, last col ends at 1920, leaving a
        // 2px strip (1922-1920) uncovered. This is the simple-tiler contract (integer
        // cells, no last-cell stretch) — the gap is at most (cols-1)px and is asserted so
        // any future "fill to edge" change is a CONSCIOUS choice, not an accident.
        let r = tile_rects(0, 0, 1922, 1080, 7, "grid");
        assert_eq!(r.len(), 7);
        let cols = 3i32;
        let cw = 1922 / cols; // 640
        let rightmost_x = 2 * cw; // start of the last column
        let covered_right = rightmost_x + cw; // 1920
        let gap = 1922 - covered_right; // 2px uncovered strip
        assert_eq!(cw, 640);
        assert_eq!(covered_right, 1920);
        assert_eq!(gap, 2, "indivisible width leaves a documented <cols px edge gap (simple-tiler contract)");
        // Every cell is the same integer size — none is stretched to absorb the remainder.
        for cell in &r {
            assert_eq!(cell.2, cw, "every grid cell is the integer column width (no last-cell stretch)");
        }
    }

    #[test]
    fn alt_tab_cycles_focus_forward() {
        assert_eq!(chord(mods(false, true, false), Keysym::Tab), Some(WmAction::CycleFocus));
    }

    #[test]
    fn alt_shift_tab_cycles_focus_back() {
        assert_eq!(chord(mods(false, true, true), Keysym::ISO_Left_Tab), Some(WmAction::CycleFocusBack));
        assert_eq!(chord(mods(false, true, true), Keysym::Tab), Some(WmAction::CycleFocusBack));
    }

    #[test]
    fn super_digits_switch_workspaces_zero_based() {
        assert_eq!(digit_chord(mods(true, false, false), Keysym::_1, Keysym::_1), Some(WmAction::SwitchWorkspace(0)));
        assert_eq!(digit_chord(mods(true, false, false), Keysym::_2, Keysym::_2), Some(WmAction::SwitchWorkspace(1)));
        assert_eq!(digit_chord(mods(true, false, false), Keysym::_9, Keysym::_9), Some(WmAction::SwitchWorkspace(8)));
    }

    #[test]
    fn super_shift_digits_move_to_workspace() {
        assert_eq!(digit_chord(mods(true, false, true), Keysym::_3, Keysym::_3), Some(WmAction::MoveToWorkspace(2)));
        assert_eq!(digit_chord(mods(true, false, false), Keysym::_3, Keysym::_3), Some(WmAction::SwitchWorkspace(2)));
    }

    #[test]
    fn super_shift_digit_resolves_when_modified_sym_is_shifted() {
        // REGRESSION GUARD: on a US keymap Shift maps digits to `!@#$%^&*(`, so
        // modified_sym() for Super+Shift+3 is `numbersign`, NOT `3`. The fix reads the
        // LEVEL-0 sym (the bare `3`) for the digit range.
        assert_eq!(digit_chord(mods(true, false, true), Keysym::numbersign, Keysym::_3), Some(WmAction::MoveToWorkspace(2)));
        assert_eq!(digit_chord(mods(true, false, true), Keysym::exclam, Keysym::_1), Some(WmAction::MoveToWorkspace(0)));
        assert_eq!(digit_chord(mods(true, false, true), Keysym::parenleft, Keysym::_9), Some(WmAction::MoveToWorkspace(8)));
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
        assert_eq!(chord(mods(false, false, false), Keysym::a), None);
        assert_eq!(chord(mods(false, false, false), Keysym::c), None);
        assert_eq!(chord(mods(false, false, false), Keysym::Left), None);
        assert_eq!(chord(mods(true, false, false), Keysym::z), None);
        assert_eq!(digit_chord(mods(false, false, false), Keysym::_1, Keysym::_1), None);
    }

    #[test]
    fn digit_zero_is_not_a_workspace_chord() {
        // The workspace range is KEY_1..=KEY_9 — KEY_0 falls outside, so Super+0 is
        // forwarded to the app (there is no workspace 0 on the wire / 10th workspace).
        assert_eq!(digit_chord(mods(true, false, false), Keysym::_0, Keysym::_0), None);
        assert_eq!(digit_chord(mods(true, false, true), Keysym::_0, Keysym::_0), None);
    }

    #[test]
    fn alt_takes_tab_only_when_logo_is_not_also_held() {
        // The Alt+Tab arm is gated `mods.alt && !mods.logo`. With BOTH Alt and Super
        // held, Tab is NOT a focus-cycle (Super owns the chord space) — it forwards.
        assert_eq!(chord(mods(true, true, false), Keysym::Tab), None);
        // Plain Alt+Tab still cycles.
        assert_eq!(chord(mods(false, true, false), Keysym::Tab), Some(WmAction::CycleFocus));
    }

    #[test]
    fn super_shift_with_a_non_digit_key_is_not_a_move_chord() {
        // Super+Shift only resolves a MoveToWorkspace for a digit; Super+Shift+letter is
        // not mapped (digit_sym is None for a letter), so it forwards.
        assert_eq!(process_keyboard_shortcut(mods(true, false, true), Keysym::a, None), None);
        // And a digit with NO digit_sym (e.g. a layout the reader couldn't resolve)
        // also falls through rather than guessing.
        assert_eq!(process_keyboard_shortcut(mods(true, false, true), Keysym::_3, None), None);
    }

    #[test]
    fn logo_arrows_outrank_nothing_else_no_modifier_collision() {
        // Bare arrows (no Super) forward; Super+arrow is the snap/restore chord. Guards
        // that the snap arm does not fire without the logo modifier.
        assert_eq!(chord(mods(false, false, false), Keysym::Up), None);
        assert_eq!(chord(mods(false, false, false), Keysym::Down), None);
        assert_eq!(chord(mods(true, false, false), Keysym::Up), Some(WmAction::Maximize));
        assert_eq!(chord(mods(true, false, false), Keysym::Down), Some(WmAction::RestoreWindow));
    }

    #[test]
    fn no_modifier_at_all_forwards_every_key() {
        // The bare-key floor: with no logo/alt/shift, NOTHING is intercepted — every
        // keystroke reaches the focused client (the compositor steals only its chords).
        for k in [Keysym::Tab, Keysym::q, Keysym::d, Keysym::Left, Keysym::Right, Keysym::_5] {
            assert_eq!(process_keyboard_shortcut(mods(false, false, false), k, Some(k)), None);
        }
    }

    #[test]
    fn map_fade_alpha_is_monotonic_across_the_ramp() {
        use std::time::Duration;
        // alpha at 0ms ≤ alpha at 75ms ≤ alpha at 150ms, and the midpoint is strictly
        // between the endpoints (a real ramp, not a step). Built from explicit past
        // instants so no real time elapses.
        let now = Instant::now();
        let a0 = MapAnim(now).alpha();
        let a_mid = MapAnim(now - Duration::from_millis(75)).alpha();
        let a_end = MapAnim(now - Duration::from_millis(FADE_IN_MS as u64)).alpha();
        assert!(a0 <= a_mid && a_mid <= a_end, "fade alpha is monotonic: {a0} {a_mid} {a_end}");
        assert!(a_mid > 0.0 && a_mid < 1.0, "midpoint is strictly mid-ramp: {a_mid}");
        assert_eq!(a_end, 1.0, "at FADE_IN_MS the ramp has reached full opacity");
    }

    #[test]
    fn default_cursor_bakes_a_visible_arrow_with_fill_and_outline() {
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
        assert!(white + black < (w * h) as u32 / 2, "arrow must not fill the whole buffer");
    }

    #[test]
    fn map_fade_alpha_ramps_then_pins_at_one() {
        use std::time::Duration;
        let fresh = MapAnim(Instant::now());
        let a = fresh.alpha();
        assert!((0.0..=1.0).contains(&a), "alpha in [0,1], got {a}");
        assert!(fresh.animating(), "a fresh map is still animating");
        let settled = MapAnim(Instant::now() - Duration::from_millis(FADE_IN_MS as u64 + 50));
        assert_eq!(settled.alpha(), 1.0, "past FADE_IN_MS the alpha pins at 1.0");
        assert!(!settled.animating(), "a settled window no longer animates");
    }

    #[test]
    fn workspace_fade_constant_is_short_and_positive() {
        assert!(WS_FADE_MS > 0 && WS_FADE_MS <= 500, "ws fade should be a short ramp");
        assert!(FADE_IN_MS > 0 && FADE_IN_MS <= 500, "map fade should be a short ramp");
    }

    #[test]
    fn rounded_rect_cuts_corners_and_fills_the_centre() {
        // A 12px radius on a 40x40 box: the exact corner pixel is outside the arc and
        // must be fully transparent, while the centre and the straight top edge are
        // fully covered. This is precisely what a hard SolidColorRenderElement cannot
        // express, so it is the reason rounded rects lower through a buffer.
        let (w, h) = (40u32, 40u32);
        let rgba = rounded_rect_rgba(w, h, 12.0, [1.0, 1.0, 1.0, 1.0]);
        assert_eq!(rgba.len(), (w * h * 4) as usize);
        let alpha_at = |x: u32, y: u32| rgba[((y * w + x) * 4 + 3) as usize];
        assert_eq!(alpha_at(0, 0), 0, "top-left corner must be cut away");
        assert!(alpha_at(w / 2, h / 2) > 250, "centre must be opaque");
        assert!(alpha_at(w / 2, 0) > 250, "the straight top edge must be covered");
        // A zero radius is a plain filled rect: the corner is now covered too.
        let sharp = rounded_rect_rgba(w, h, 0.0, [1.0, 1.0, 1.0, 1.0]);
        assert!(sharp[3] > 250, "radius 0 fills the corner");
    }
}

// NATIVE SHELL PARITY PROGRAM, M3 render proof. Gated on `smithay` because it uses
// the real `PixmanRenderer` (the never-fail software floor, no GPU) to exercise the
// SAME `lower_scene` the DRM/pixman frame path runs. This closes the gap the other
// tests leave: scene.rs proves LAYOUT, text_render.rs proves colour packing, and the
// build proves the API TYPE-checks — but only actually lowering a scene against a
// live renderer proves the buffers IMPORT and the elements are produced. Runs headless
// in CI (pixman is pure CPU), so the native chrome is validated without the box.
#[cfg(all(test, feature = "smithay"))]
mod native_render_tests {
    use super::*;
    use smithay::backend::renderer::element::Element;
    use smithay::backend::renderer::pixman::PixmanRenderer;

    #[test]
    fn demo_scene_lowers_and_imports_buffers_on_pixman() {
        // The never-fail software renderer of record — allocates with no GPU, so this
        // holds in any CI sandbox.
        let mut renderer = PixmanRenderer::new().expect("pixman renderer allocates headless");
        let size: Size<i32, Physical> = (1280, 800).into();

        let home = crate::scene::HomeCompose::demo();
        let mut rasterizer = crate::text_render::TextRasterizer::new();
        let mut orb = OrbCache::default();
        let mut rects = RectCache::default();

        let mut elements: Vec<HartRenderElement<PixmanRenderer>> = Vec::new();
        lower_scene(
            &home,
            size,
            &mut renderer,
            &mut rasterizer,
            &mut orb,
            &mut rects,
            0.5,
            None,
            &mut elements,
        );

        // The demo home is a full desktop (top bar + hero + rows + taskbar), so it
        // lowers to a non-trivial element set, not one stray rect.
        assert!(
            elements.len() >= 3,
            "demo scene lowered to only {} elements",
            elements.len()
        );

        // The orb slots, the rounded card/omnibox rects, and (with a font) the text
        // runs all lower to Memory elements — each exists ONLY when
        // `MemoryRenderBufferRenderElement::from_buffer` returned Ok, so their presence
        // is evidence the MemoryRenderBuffer -> PixmanRenderer ImportMem path actually
        // works. The demo always carries orb slots, so this never depends on fonts
        // being installed in the sandbox.
        let memory = elements
            .iter()
            .filter(|e| matches!(e, HartRenderElement::Memory(_)))
            .count();
        assert!(
            memory >= 1,
            "orb/rounded-rect/text buffers must import as Memory elements, got {memory}"
        );

        // Every lowered element has a positive on-screen footprint — nothing collapsed
        // to zero area (the <1px skip) or lowered with an empty box.
        for e in &elements {
            let g = e.geometry(Scale::from(1.0));
            assert!(
                g.size.w > 0 && g.size.h > 0,
                "element lowered with empty geometry: {g:?}"
            );
        }
    }

    #[test]
    fn demo_scene_composites_visible_pixels_on_pixman() {
        // Color32F / Frame / draw_render_elements are cfg-gated to the winit path in
        // this module's own imports, so bring them in directly for the smithay test.
        use smithay::backend::renderer::utils::draw_render_elements;
        use smithay::backend::renderer::{Bind, Color32F, ExportMem, Frame, Offscreen};

        // Compose the lowered scene into an offscreen pixman image OVER a magenta
        // sentinel the scene never paints, then read the pixels back. This is the
        // on-screen COMPOSITE proof the import test does not give: the elements must
        // actually PAINT onto a framebuffer, with the right byte order and premultiply,
        // not merely be produced. Pure CPU (pixman), so it runs headless in CI without
        // a GPU or the thermal-blocked box.
        let mut renderer = PixmanRenderer::new().expect("pixman renderer allocates headless");
        let size: Size<i32, Physical> = (640, 400).into();
        let buf_size: Size<i32, BufferCoord> = (640, 400).into();

        let home = crate::scene::HomeCompose::demo();
        let mut rasterizer = crate::text_render::TextRasterizer::new();
        let mut orb = OrbCache::default();
        let mut rects = RectCache::default();
        let mut elements: Vec<HartRenderElement<PixmanRenderer>> = Vec::new();
        lower_scene(
            &home,
            size,
            &mut renderer,
            &mut rasterizer,
            &mut orb,
            &mut rects,
            0.5,
            None,
            &mut elements,
        );

        let mut image = renderer
            .create_buffer(Fourcc::Argb8888, buf_size)
            .expect("offscreen image");
        let mut target = renderer.bind(&mut image).expect("bind offscreen");
        let full: Rectangle<i32, Physical> = Rectangle::from_size(size);
        {
            let mut frame = renderer
                .render(&mut target, size, Transform::Normal)
                .expect("begin frame");
            frame
                .clear(Color32F::new(1.0, 0.0, 1.0, 1.0), &[full])
                .expect("clear to sentinel");
            draw_render_elements(&mut frame, 1.0, &elements, &[full]).expect("draw scene");
            let _ = frame.finish().expect("finish frame");
        }

        let region: Rectangle<i32, BufferCoord> = Rectangle::from_size(buf_size);
        let mapping = renderer
            .copy_framebuffer(&target, region, Fourcc::Argb8888)
            .expect("copy_framebuffer");
        let bytes = renderer.map_texture(&mapping).expect("map_texture");

        // Argb8888 little-endian = [B,G,R,A]; the opaque magenta clear reads
        // B=255,G=0,R=255. Count pixels the scene painted over it: the chrome (top bar,
        // taskbar, hero, orb, text) must cover a real fraction of the frame.
        let total = (size.w * size.h) as usize;
        let painted = bytes
            .chunks_exact(4)
            .filter(|px| !(px[0] > 250 && px[1] < 5 && px[2] > 250))
            .count();
        assert!(
            painted > total / 20,
            "native scene painted only {painted}/{total} px over the clear sentinel"
        );
    }
}
