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
/// Set once the native scene has actually put elements into a frame. Read by the
/// compositor's shell-ready writer, which must mean "the scene really painted" rather than
/// "the flag was set": a marker that fires off configuration instead of pixels is the
/// false-healthy this whole marker family exists to avoid.
pub static NATIVE_SCENE_PAINTED: std::sync::atomic::AtomicBool =
    std::sync::atomic::AtomicBool::new(false);

/// Everything one lowering needs off the backend `State`, handed back together.
///
/// One accessor rather than five because these are DISJOINT fields and two `&mut self`
/// accessors cannot overlap: the tree has to be borrowed alongside the buffer caches for
/// the whole walk. The composed home rides along as a SHARED borrow, which is what let the
/// per-frame clone go. Named because the tuple is wide enough that spelling it at every
/// implementor was its own kind of noise.
pub type NativeSceneCaches<'a> = (
    Option<&'a crate::scene::HomeCompose>,
    &'a mut crate::text_render::TextRasterizer,
    &'a mut OrbCache,
    &'a mut RectCache,
    &'a mut crate::scene::SceneCache,
);

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
    /// `animate` is the same hardware condition `scene_animates` gates on. With motion
    /// off the elapsed time handed to `motion_at` is ZERO, which is its resting scale and
    /// alpha, so the orb sits still rather than freezing wherever the last painted frame
    /// happened to catch it. That matches `animation: none`, which is what the shell
    /// applies on the software floor, and it does it through the SAME motion function
    /// rather than a second resting-state constant.
    pub fn current(
        &mut self,
        side: i32,
        energy: f32,
        animate: bool,
    ) -> Option<(&MemoryRenderBuffer, crate::orb::OrbMotion)> {
        if side <= 0 {
            return None;
        }
        let now = Instant::now();
        let epoch = *self.epoch.get_or_insert(now);
        let elapsed = if animate {
            now.saturating_duration_since(epoch)
        } else {
            std::time::Duration::ZERO
        };
        let motion = crate::orb::motion_at(elapsed, energy);

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

/// Rasterize a rounded rectangle into a premultiplied [B,G,R,A] buffer, anti-aliased at
/// the corners via a rounded-box signed-distance field, filled with a linear gradient from
/// `from` to `to` along `angle_deg`. A SOLID tile is this with `from == to`, which is why
/// there is one rasterizer and not two: the scene's card art and its card background are
/// the same shape with a different fill, and a second copy of the SDF is exactly the drift
/// the shell's own brand-art module was written to end.
///
/// The scene carries a `radius` on the card / omnibox / art rects that a
/// `SolidColorRenderElement` (always a hard quad) cannot express, so those lower through a
/// cached MemoryRenderBuffer of THIS shape instead. Byte order + premultiply match
/// text_render.rs and bloom.rs (Argb8888 little-endian = B,G,R,A, premultiplied).
///
/// `angle_deg` follows CSS `linear-gradient`: 0 points UP the tile and the angle increases
/// clockwise, so 135 runs top-left to bottom-right. The gradient line is centred on the
/// tile and its length is `|w*sin| + |h*cos|`, which is what makes the last stop land
/// exactly on the far corner rather than short of it.
fn rounded_rect_rgba(
    w: u32,
    h: u32,
    radius: f32,
    from: [f32; 4],
    mid: [f32; 4],
    mid_at: f32,
    to: [f32; 4],
    angle_deg: f32,
) -> Vec<u8> {
    let mut rgba = vec![0u8; (w * h * 4) as usize];
    let hw = w as f32 / 2.0;
    let hh = h as f32 / 2.0;
    // A radius past half the short side is just a fuller pill / circle.
    let r = radius.clamp(0.0, hw.min(hh));
    let solid = from == to;
    // Screen space has y DOWN, so the CSS "up" axis is -y: the unit vector along the
    // gradient line is (sin, -cos).
    let rad = angle_deg.to_radians();
    let (dx, dy) = (rad.sin(), -rad.cos());
    let len = (w as f32 * dx).abs() + (h as f32 * dy).abs();
    let inv_len = if len > 0.0 { 1.0 / len } else { 0.0 };
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
            // Position along the gradient line, 0 at the first stop's end. The projection
            // is centred, so shifting by half the length puts 0 at the start edge.
            let t = if solid {
                0.0
            } else {
                ((px * dx + py * dy) * inv_len + 0.5).clamp(0.0, 1.0)
            };
            // Three stops, because the shell's own no-blur floor is a three-stop ramp
            // (teal leads, violet accents) and a two-stop copy of it loses the accent.
            // A TWO-stop gradient is this with `mid` on the line between the ends, so
            // there is one ramp here and not a second code path for the simpler case.
            let ch = |i: usize| {
                let v = if t <= mid_at {
                    let k = if mid_at > 0.0 { t / mid_at } else { 0.0 };
                    from[i] + (mid[i] - from[i]) * k
                } else {
                    let span = (1.0 - mid_at).max(f32::EPSILON);
                    let k = (t - mid_at) / span;
                    mid[i] + (to[i] - mid[i]) * k
                };
                v.clamp(0.0, 1.0)
            };
            let a = ch(3) * cov;
            let idx = ((y * w + x) * 4) as usize;
            rgba[idx] = (ch(2) * a * 255.0) as u8;
            rgba[idx + 1] = (ch(1) * a * 255.0) as u8;
            rgba[idx + 2] = (ch(0) * a * 255.0) as u8;
            rgba[idx + 3] = (a * 255.0) as u8;
        }
    }
    rgba
}

/// Rasterize a DROP SHADOW: the rounded box, blurred, into a premultiplied buffer.
///
/// The buffer is the caster grown by `blur` on every side, so the caller draws it at
/// `(rect.x - blur, rect.y + offset_y - blur)` and the shape lands where the box is.
///
/// APPROXIMATED, and worth saying which way. A CSS box-shadow is the shape convolved with
/// a Gaussian of about `blur/2`; this ramps the alpha across `blur` centred on the edge
/// with a smoothstep instead. The difference is a fraction of a pixel of softness at the
/// extremes and no convolution at all, which matters because this is the software floor's
/// depth: the shell keeps this shadow on every tier precisely because it "rasters ONCE and
/// composites cheaply forever", and a real blur here would make it the opposite.
fn shadow_rgba(w: u32, h: u32, radius: f32, blur: f32, color: [f32; 4]) -> Vec<u8> {
    let mut rgba = vec![0u8; (w * h * 4) as usize];
    // The CASTER sits inset by `blur` inside this buffer.
    let bw = w as f32 - 2.0 * blur;
    let bh = h as f32 - 2.0 * blur;
    let (hw, hh) = (bw / 2.0, bh / 2.0);
    let r = radius.clamp(0.0, hw.min(hh).max(0.0));
    let cx = w as f32 / 2.0;
    let cy = h as f32 / 2.0;
    let ramp = blur.max(f32::EPSILON);
    for y in 0..h {
        for x in 0..w {
            let px = x as f32 + 0.5 - cx;
            let py = y as f32 + 0.5 - cy;
            let qx = px.abs() - (hw - r);
            let qy = py.abs() - (hh - r);
            let dist =
                (qx.max(0.0).powi(2) + qy.max(0.0).powi(2)).sqrt() + qx.max(qy).min(0.0) - r;
            // 1 well inside, 0 a blur-radius outside, smooth across the edge.
            let t = (0.5 - dist / ramp).clamp(0.0, 1.0);
            let cov = t * t * (3.0 - 2.0 * t);
            if cov <= 0.0 {
                continue;
            }
            let a = color[3].clamp(0.0, 1.0) * cov;
            let i = ((y * w + x) * 4) as usize;
            rgba[i] = (color[2] * a * 255.0) as u8;
            rgba[i + 1] = (color[1] * a * 255.0) as u8;
            rgba[i + 2] = (color[0] * a * 255.0) as u8;
            rgba[i + 3] = (a * 255.0) as u8;
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
    /// Unbounded on purpose, unlike the text cache which had to gain a cap: every part of
    /// this key comes from LAYOUT, never from the feed. Sizes are the layout constants
    /// plus a handful that track the output width, radii are constants, and colours are
    /// the theme's plus one hover lift per card. That is a few dozen combinations for a
    /// given output, not a set that grows with what the agent writes.
    #[allow(clippy::type_complexity)]
    cache: std::collections::HashMap<
        (u32, u32, u32, [u32; 4], [u32; 4], u32, [u32; 4], u32),
        MemoryRenderBuffer,
    >,
    /// POOL for the sharp-rect path. The first cut built a `SolidColorBuffer` per rect per
    /// frame, which is the other half of the zero-per-frame-alloc NFR (the retained tree in
    /// `scene::SceneCache` was the first). Buffers are handed out in paint order and reused
    /// next frame via `update`, so a steady desktop allocates none.
    solids: Vec<SolidColorBuffer>,
    /// How far into `solids` this frame has got. Reset by `begin_frame`.
    solid_next: usize,
    /// How many solid buffers were ever actually allocated. The pooling PROOF: a steady
    /// desktop must not grow this per frame.
    solid_allocs: u64,
    /// How many rounded-rect buffers were ever composed. The other half of the same
    /// proof: a rounded rect is a per-pixel SDF rasterize, so recomposing one per frame
    /// would be far more expensive than the solid pool it sits beside.
    rounded_composes: u64,
}

impl RectCache {
    /// Start a frame: hand out pooled solids from the top again. Paint order is
    /// deterministic for a given tree, so frame N+1 reuses the same buffer for the same
    /// rect. Must be called once per lowering, before any `solid` call.
    pub fn begin_frame(&mut self) {
        self.solid_next = 0;
    }

    /// The next pooled solid buffer, sized and coloured for this rect. Allocates only
    /// while the pool is still shorter than the frame needs; afterwards it is `update` on
    /// a buffer this cache already owns.
    pub fn solid(&mut self, w: i32, h: i32, color: [f32; 4]) -> &mut SolidColorBuffer {
        if self.solid_next == self.solids.len() {
            self.solids.push(SolidColorBuffer::new((w, h), color));
            self.solid_allocs += 1;
        } else {
            self.solids[self.solid_next].update((w, h), color);
        }
        let idx = self.solid_next;
        self.solid_next += 1;
        &mut self.solids[idx]
    }

    /// Total solid buffers ever allocated (test hook for the pooling proof).
    pub fn solid_allocs(&self) -> u64 {
        self.solid_allocs
    }

    /// The rounded-rect buffer for these dims / radius / colour, composed on first
    /// use. `None` for a degenerate size (the caller then draws nothing for it).
    pub fn rounded(
        &mut self,
        w: i32,
        h: i32,
        radius: f32,
        color: [f32; 4],
    ) -> Option<&MemoryRenderBuffer> {
        // A flat fill is the degenerate gradient, so it goes through the same tile: one
        // rasterizer, one cache, one compose-once counter.
        self.tile(w, h, radius, color, color, 0.5, color, 0.0)
    }

    /// The card-art buffer: the same rounded tile, filled with the scene's two-stop
    /// gradient. Keyed on both stops and the angle, so the whole desktop's art is a
    /// handful of buffers (six hues by three angles at one card size), each composed once.
    pub fn gradient(
        &mut self,
        w: i32,
        h: i32,
        radius: f32,
        from: [f32; 4],
        to: [f32; 4],
        angle_deg: f32,
    ) -> Option<&MemoryRenderBuffer> {
        // Two stops is three with the middle ON the line between the ends, which is
        // exactly the ramp it already was: same pixels, no second path.
        let mid = [
            (from[0] + to[0]) * 0.5,
            (from[1] + to[1]) * 0.5,
            (from[2] + to[2]) * 0.5,
            (from[3] + to[3]) * 0.5,
        ];
        self.tile(w, h, radius, from, mid, 0.5, to, angle_deg)
    }

    /// The blurred shadow buffer for a caster of these dims. Same cache, same
    /// compose-once counter: a desktop of identical cards composes exactly one.
    ///
    /// Keyed through the tile map by putting the blur where the angle goes and a sentinel
    /// radius, so a shadow and a tile of the same size can never collide.
    pub fn shadow(
        &mut self,
        w: i32,
        h: i32,
        radius: f32,
        blur: f32,
        color: [f32; 4],
    ) -> Option<&MemoryRenderBuffer> {
        let (bw, bh) = (w + 2.0_f32.mul_add(blur, 0.0) as i32, h + (2.0 * blur) as i32);
        if bw < 1 || bh < 1 {
            return None;
        }
        let bits = |c: [f32; 4]| [c[0].to_bits(), c[1].to_bits(), c[2].to_bits(), c[3].to_bits()];
        // `SHADOW` in the mid slot: a marker no colour can produce, so the key space is
        // shared with the tiles without either being able to answer for the other.
        const SHADOW: [u32; 4] = [u32::MAX, u32::MAX, u32::MAX, u32::MAX];
        let key = (
            bw as u32,
            bh as u32,
            radius.to_bits(),
            bits(color),
            SHADOW,
            blur.to_bits(),
            bits(color),
            0u32,
        );
        let Self {
            cache,
            rounded_composes,
            ..
        } = self;
        match cache.entry(key) {
            std::collections::hash_map::Entry::Occupied(e) => Some(e.into_mut()),
            std::collections::hash_map::Entry::Vacant(e) => {
                let rgba = shadow_rgba(bw as u32, bh as u32, radius, blur, color);
                *rounded_composes += 1;
                Some(e.insert(MemoryRenderBuffer::from_slice(
                    &rgba,
                    Fourcc::Argb8888,
                    (bw, bh),
                    1,
                    Transform::Normal,
                    None,
                )))
            }
        }
    }

    /// A THREE-stop tile, for the one place the shell uses one: its no-blur chrome floor.
    pub fn gradient3(
        &mut self,
        w: i32,
        h: i32,
        radius: f32,
        from: [f32; 4],
        mid: [f32; 4],
        mid_at: f32,
        to: [f32; 4],
        angle_deg: f32,
    ) -> Option<&MemoryRenderBuffer> {
        self.tile(w, h, radius, from, mid, mid_at, to, angle_deg)
    }

    /// The one composed-tile path behind `rounded` and `gradient`.
    #[allow(clippy::too_many_arguments)]
    fn tile(
        &mut self,
        w: i32,
        h: i32,
        radius: f32,
        from: [f32; 4],
        mid: [f32; 4],
        mid_at: f32,
        to: [f32; 4],
        angle_deg: f32,
    ) -> Option<&MemoryRenderBuffer> {
        if w < 1 || h < 1 {
            return None;
        }
        let bits = |c: [f32; 4]| [c[0].to_bits(), c[1].to_bits(), c[2].to_bits(), c[3].to_bits()];
        // A solid tile keys its angle as zero whatever was passed, so the same colour at
        // two angles is one buffer rather than two identical ones.
        let angle_key = if from == to && mid == to { 0.0f32 } else { angle_deg };
        let key = (
            w as u32,
            h as u32,
            radius.to_bits(),
            bits(from),
            bits(mid),
            mid_at.to_bits(),
            bits(to),
            angle_key.to_bits(),
        );
        // Destructured so the counter and the map are DISJOINT borrows: that is what lets
        // the vacant arm bump `rounded_composes` while still holding the entry, and so
        // lets one `entry` lookup replace the contains_key/insert/get triple this used to
        // hash the key three times for.
        let Self {
            cache,
            rounded_composes,
            ..
        } = self;
        match cache.entry(key) {
            std::collections::hash_map::Entry::Occupied(e) => Some(e.into_mut()),
            std::collections::hash_map::Entry::Vacant(e) => {
                let rgba =
                rounded_rect_rgba(w as u32, h as u32, radius, from, mid, mid_at, to, angle_deg);
                *rounded_composes += 1;
                Some(e.insert(MemoryRenderBuffer::from_slice(
                    &rgba,
                    Fourcc::Argb8888,
                    (w, h),
                    1,
                    Transform::Normal,
                    None,
                )))
            }
        }
    }

    /// Total composed tiles ever rasterized, solid and gradient alike (test hook for the
    /// compose-once proof).
    pub fn rounded_composes(&self) -> u64 {
        self.rounded_composes
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

    /// Store the native-scene flag. Default = a NO-OP, and deliberately so: a backend
    /// with no such field has no native scene to turn on, and pretending otherwise is
    /// the failure mode `window.resize` was caught in on 2026-09-10 (it answered `ok`
    /// for a resize the client had declined). The caller reads `native_shell_on` back
    /// and reports what the flag ACTUALLY says, so a backend that cannot honour the
    /// request says so rather than appearing to obey.
    fn set_native_shell_flag(&mut self, _on: bool) {}

    /// Toggle the native scene render path at RUNTIME (the `shell.native` IPC verb's
    /// executor), mirroring `set_capture_blocked` next door. Returns the new state,
    /// which is what the flag reads AFTER the attempt, never what was asked for.
    ///
    /// Why a runtime toggle exists at all, when M6 will flip the default: the one
    /// measurement this program has never taken is its own headline claim.
    /// `pointer_surface` can attribute an input to TOPBAR/CARD/ORB only while the
    /// native scene is drawn, so with the flag off every latency sample is
    /// `component=shell` by construction, which is exactly what the box reported on
    /// 2026-09-10, ~4,200 samples with one distinct component. Turning the scene on
    /// meant an env var read once at session start: a new generation, a reboot, and no
    /// way back except another reboot. Over this socket it is one call with an instant
    /// undo, so the native-versus-shell delta can be measured in one session on one
    /// machine minutes apart, and a surprise is reverted in the time it takes to send
    /// `{"on": false}`.
    fn set_native_shell(&mut self, on: bool) -> bool {
        set_native_shell_shared(self, on)
    }

    /// Is the compositor GPU-compositing RIGHT NOW (a live GLES renderer), as opposed to
    /// painting through the pixman software floor?
    ///
    /// This is the native mirror of the shell's `body.gpu-hardware` class, and it exists
    /// for the same reason that class does: the HTML shell runs its breathing orb, its
    /// hover transforms and its live dots ONLY under `body.gpu-hardware`, because on a
    /// software renderer those re-rasterise on the CPU every frame. That is not
    /// hypothetical here either. liquid_ui_service records the real-HW consequence
    /// (2026-07-12): GPU-only effects armed on a CPU renderer "re-rasterised a 60fps
    /// canvas + an animated software blur on the ONE WebKit thread and HUNG the whole
    /// shell".
    ///
    /// Defaults to FALSE, the floor, so a backend that does not track its renderer never
    /// claims hardware motion it cannot afford.
    fn motion_hardware(&self) -> bool {
        false
    }
    /// Record whether the live renderer is the GPU one. The DRM backend sets this each
    /// tick from whether its `GlesRenderer` is still present, so a mid-session demotion
    /// to the pixman floor stands the animation down on the very next frame.
    fn set_motion_hardware(&mut self, _on: bool) {}

    /// NATIVE SHELL M2 press half: how many pointer buttons the seat currently holds
    /// down, maintained by the shared `on_pointer_button` via `note_pointer_button`.
    /// The native scene reads `pointer_pressed` so the orb reacts to a click held over
    /// it. Defaults keep the press reaction OFF for any backend that does not store the
    /// count, which is exactly today's behaviour (additive, no regression). A count
    /// rather than a bool so two buttons held then one released stays pressed; the
    /// release decrement saturates, so a stray release never underflows. Known edge,
    /// accepted: a release swallowed by the capture killswitch can leave the count high
    /// until the next click, which only over-energises the orb cosmetically.
    fn note_pointer_button(&mut self, _down: bool) {}
    fn pointer_pressed(&self) -> bool {
        false
    }

    /// How far each card row is scrolled sideways. Default-empty for a backend that
    /// keeps no scroll state, which is also the honest answer for the WebView desktop:
    /// its rows scroll themselves.
    fn row_scroll(&self) -> crate::scene::RowScroll {
        crate::scene::RowScroll::default()
    }
    /// Record a new scroll state. A no-op for a backend that keeps none.
    fn set_row_scroll(&mut self, _s: crate::scene::RowScroll) {}

    /// The retained native scene tree, for asking what a point is over. None for a
    /// backend that keeps no scene, which is also the honest answer for the WebView
    /// desktop: nothing native is laid out, so nothing native can be named.
    fn native_tree(&self) -> Option<&crate::scene::SceneNode> {
        None
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

    /// ALL native-scene caches at once, as DISJOINT field borrows. `lower_scene`
    /// walks one leaf list that interleaves Text runs (rasterizer) and OrbSlots
    /// (orb cache), so it must hold BOTH `&mut` across the loop — which the separate
    /// `text_rasterizer_mut`/`orb_mut` accessors cannot give (each borrows all of
    /// `self`). One combined accessor split-borrows the fields, so the lowering
    /// stays a SINGLE path shared by `render_native_scene` (via State) and its test
    /// (via directly-constructed caches). Required so every native-capable backend
    /// supplies all of them.
    ///
    /// The 4th is the RETAINED scene tree: it must come through this same accessor
    /// rather than a separate method, because the lowering needs the tree borrowed at
    /// the same time as the buffer caches, and two `&mut self` accessors cannot overlap.
    ///
    /// The 1st is the composed home as a SHARED borrow riding alongside those `&mut`s
    /// (disjoint fields, so this is one split borrow, not a conflict). It is here purely
    /// to kill an allocation: `render_native_scene` used to CLONE the HomeCompose every
    /// frame just to end the state borrow before taking the caches, which is the last
    /// per-frame heap traffic the zero-alloc NFR named. None means no `shell.compose`
    /// has landed and the caller falls back to `scene::demo_ref()`.
    fn native_scene_caches(&mut self) -> NativeSceneCaches<'_>;

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

/// The chrome the NATIVE scene paints, expressed as a reservation.
///
/// THE CONTRACT INVERTS AT M6, and this is the half that inverts it. While the
/// WebView draws the bars, the shell is the only thing that knows their size and it
/// publishes `PANEL_RESERVATION_PATH`. Once the compositor paints them, the
/// compositor is what knows -- and the file's only publisher is exactly the process
/// M6 demotes. Nothing would write it, `panel_reservation` would fail safe to zero,
/// and a maximized window would cover the native bars: the 2026-08-29 "taskbar
/// unreachable" report arriving through the new renderer.
///
/// Both numbers come from the SAME sources the scene lays out from, so the
/// reservation cannot disagree with the pixels. `top_bar_h` is off the active theme,
/// because four of the ten shipped themes move it (36/38/40/44). `TASKBAR_H` is the
/// scene constant that tests/unit/test_panel_reservation.py already pins to the
/// shell's Python constant. Neither is a new number.
pub fn native_chrome_reservation() -> PanelReservation {
    let theme = active_theme();
    PanelReservation {
        top: theme.top_bar_h.round().max(0.0) as i32,
        bottom: crate::scene::TASKBAR_H.round().max(0.0) as i32,
    }
}

/// What placement must avoid, given what the shell published and what the scene
/// draws. PURE, so the merge rule is testable with no output and no theme file.
///
/// `None` -- the flag off -- returns the published value UNCHANGED. Every existing
/// placement path is then byte-identical to before, which is what lets this ship
/// ahead of the flip with zero risk to the desktop that is actually running.
///
/// With the scene on, the edges merge BY MAXIMUM rather than the native value
/// replacing the published one. That is deliberate, and it is the honest reading of
/// every state this can be in:
///
///   * Transition, which is where the box is today: `shell.native {on}` draws the
///     scene WITHOUT standing the WebView down, since that hand-off is a separate M6
///     obligation. Both sets of bars are genuinely on screen, so reserving the larger
///     of each edge is the only value that covers what is drawn.
///   * After the demotion: the file is absent, `panel_reservation` fails safe to
///     zero, and the maximum is the native value. Which is the whole point.
///   * A STALE file left behind by the demoted shell can then only ever
///     OVER-reserve. That asymmetry is the reason for the maximum: over-reserving
///     costs a band of unused desktop, under-reserving costs a bar the user cannot
///     reach, and `work_area` already caps the absurd case at half the output.
pub fn effective_reservation(
    published: PanelReservation,
    native: Option<PanelReservation>,
) -> PanelReservation {
    match native {
        None => published,
        Some(n) => PanelReservation {
            top: published.top.max(n.top),
            bottom: published.bottom.max(n.bottom),
        },
    }
}

/// The live work area. THE single place window placement learns where it may lay
/// things out; `output_geometry` must not be read directly for that purpose again.
pub fn work_area_for<S: CompState>(state: &S) -> Option<(i32, i32, i32, i32)> {
    let g = state.space().output_geometry(state.output())?;
    // `native_shell_on`, NOT `native_scene_drawn`. The killswitch blacks the screen
    // out and skips the scene for that frame, but the bars have not stopped existing
    // and window placement must not shuffle every window because the display went
    // dark for a moment.
    let reserved = effective_reservation(
        panel_reservation(),
        state.native_shell_on().then(native_chrome_reservation),
    );
    Some(work_area(g.loc.x, g.loc.y, g.size.w, g.size.h, reserved))
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

/// The native-scene toggle (the `CompState::set_native_shell` default body). Named
/// distinctly from the trait method for the same reason as its neighbour above: a
/// default body that called the trait method would recurse into itself.
///
/// It logs the OUTCOME, not the request. `set_native_shell_flag` is a no-op on any
/// backend without the field, so `took` can be false, and a log line saying the scene
/// was turned on when it was not is worse than no line at all.
pub fn set_native_shell_shared<S: CompState>(state: &mut S, on: bool) -> bool {
    if state.native_shell_on() != on {
        state.set_native_shell_flag(on);
        info!(
            native = on,
            took = state.native_shell_on() == on,
            "shell.native: the native scene render path toggled at runtime"
        );
    }
    state.native_shell_on()
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
    // WHICH surface this input touched, resolved once for the whole match against the
    // retained scene tree. Before this every sample was reported as `component=shell`,
    // so latency_budgets.json's 23 per-component rows were dead and a slow orb was
    // indistinguishable from a slow marketplace.
    let surface = pointer_surface(state);
    match &event {
        InputEvent::Keyboard { event } => {
            crate::latency::on_input(surface, crate::latency::Kind::Key, event.time());
            note_input_alive();
        }
        InputEvent::PointerMotion { event } => {
            crate::latency::on_motion(surface, event.time());
            note_input_alive();
        }
        InputEvent::PointerMotionAbsolute { event } => {
            crate::latency::on_motion(surface, event.time());
            note_input_alive();
        }
        InputEvent::PointerButton { event } => {
            crate::latency::on_button(
                surface,
                event.state() == ButtonState::Pressed,
                event.time(),
            );
            note_input_alive();
        }
        InputEvent::PointerAxis { event } => {
            crate::latency::on_input(surface, crate::latency::Kind::Scroll, event.time());
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
    // M2 press half: keep the seat's held-button count current for the native scene
    // (the orb's press reaction). One line on the SAME shared handler both backends
    // route through, so there is no second button path.
    state.note_pointer_button(button_state == ButtonState::Pressed);
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
    // A2's card rails, before the frame goes to the client: a wheel over a row scrolls
    // THAT row sideways. Handled here rather than as a client event because the rows are
    // ours, painted by the compositor; a client under the pointer still gets its frame
    // below, exactly as it did.
    // libinput reports axis amounts as f64; the scene works in f32 logical px.
    scroll_row_under_pointer(state, vertical as f32, horizontal as f32);

    let pointer = state.pointer().clone();
    pointer.axis(state, frame);
    pointer.frame(state);
}

/// One wheel notch, in logical px of row travel.
///
/// libinput reports a mouse notch as 15 units (or 120 in the v120 axis, normalised to 15
/// above), and a touchpad reports continuous units. Eight px per unit puts a notch at
/// 120px, which is the browser's own wheel step and so what the shell's `overflow-x`
/// rails already move by: the same gesture travels the same distance on both renderers.
const SCROLL_PX_PER_UNIT: f32 = 8.0;

/// PURE: how far one axis event moves a row, in logical px.
///
/// Extracted like `gles_should_demote` / `flip_action` / `master_step`, and for the same
/// reason: the decision is unit-testable on any dev box while the state-touching glue
/// around it is not. A vertical wheel scrolls a horizontal rail, which is what a browser
/// does over an `overflow-x` element with nothing to scroll vertically, and so what this
/// desktop's users already get from the shell. A sideways swipe scrolls it too, and the
/// two are SUMMED rather than one winning: a diagonal touchpad gesture should move the
/// row by what the finger actually travelled.
fn row_scroll_delta(vertical: f32, horizontal: f32) -> f32 {
    let d = (vertical + horizontal) * SCROLL_PX_PER_UNIT;
    if d.is_finite() {
        d
    } else {
        0.0
    }
}

/// Scroll the card row under the pointer, if a card row is under the pointer.
///
/// A vertical wheel scrolls a horizontal rail, which is what a browser does over an
/// `overflow-x` element with nothing to scroll vertically, and so what this desktop's
/// users already expect from the shell. A horizontal wheel or a two-finger sideways swipe
/// scrolls it too, and the two are summed rather than fought over.
fn scroll_row_under_pointer<S: CompState>(state: &mut S, vertical: f32, horizontal: f32) {
    if !native_scene_drawn(state.native_shell_on(), state.capture_blocked()) {
        return;
    }
    let delta = row_scroll_delta(vertical, horizontal);
    if delta == 0.0 {
        return;
    }
    let size = output_physical_size(state);
    let Some((px, py)) = native_pointer_scene_pos(state, size) else {
        return;
    };
    let Some(row) = state.native_tree().and_then(|t| t.row_at(px, py)) else {
        return;
    };
    // The row's own extents, from the payload that laid it out. A row with fewer cards
    // than fit has nothing to scroll and the clamp pins it, which is what stops a stray
    // wheel sliding a two-card row off its gutter.
    let cards = state
        .native_home()
        .map(|h| h.rows.get(row).map(|r| r.cards.len()).unwrap_or(0))
        .unwrap_or(0);
    let content_w = crate::scene::RowScroll::content_width(cards);
    let view_w = crate::scene::row_view_width(size.w as f32, size.h as f32);
    let mut sc = state.row_scroll();
    sc.scroll(row, delta, content_w, view_w);
    state.set_row_scroll(sc);
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
    let ws_fading = state
        .ws_switch_at()
        .is_some_and(|t| t.elapsed().as_millis() < WS_FADE_MS);
    let map_animating = state
        .space()
        .elements()
        .any(|w| w.user_data().get::<MapAnim>().map(|a| a.animating()).unwrap_or(false));
    scene_animates(
        native_scene_drawn(state.native_shell_on(), state.capture_blocked()),
        state.motion_hardware(),
        theme_potato(),
        motion_reduced(),
        ws_fading,
        map_animating,
    )
}

/// PURE: is the native scene actually drawn this frame? The flag alone is not the answer.
/// Under the privacy killswitch an opaque full-output black solid is pushed above
/// everything, so the scene beneath it is invisible: lowering it is wasted work, and
/// because a drawn native scene holds the frame-budget gate open (see `scene_animates`),
/// counting it would composite at full rate behind a blacked-out screen.
///
/// ONE predicate for both decisions, so "we draw it" and "it animates" can never drift
/// apart. The bloom backdrop and the M2 orb already carry the same `!capture_blocked`
/// condition inline; this is the M3 scene joining them rather than a new policy.
pub fn native_scene_drawn(native_shell_on: bool, capture_blocked: bool) -> bool {
    native_shell_on && !capture_blocked
}

/// PURE: does the scene animate CONTINUOUSLY, so the tick must composite rather than
/// coast on the idle heartbeat? Split out for the same reason `wants_paint` is split from
/// `should_paint`: the rule is then unit-testable without building a compositor State.
///
/// The native shell counts because its orb BREATHES. `orb::motion_at` is a function of
/// the clock, so while the native scene is drawn the desktop is animating by
/// construction, and the 200ms idle heartbeat would render that breath as a 5 Hz stutter,
/// against the program's 60fps NFR. What makes this affordable rather than the
/// "catastrophic at 60Hz" full repaint is damage tracking: the tick composites, but only
/// the orb's own region actually changed, which is exactly the case damage tracking
/// exists for (and which the element-identity test pins down).
///
/// It is gated on the flag, not on the orb's existence, because the orb is drawn beneath
/// the WebView shell today and OCCLUDED by it: invisible breathing must not cost the
/// shipped desktop its idle saving. So flag off, behaviour is exactly what it was.
pub fn scene_animates(
    native_scene_drawn: bool,
    motion_hardware: bool,
    theme_potato: bool,
    motion_reduced: bool,
    ws_fading: bool,
    map_animating: bool,
) -> bool {
    // REDUCED MOTION is not a performance floor and is not overridable by one: it is the
    // user saying stop. The shell has three independent motion kill-switches and the CSS
    // parity ledger is explicit that all three must exist natively; this is the one that
    // is a stated preference rather than a hardware verdict, so it wins over everything,
    // including the transients below. A workspace fade the user asked not to see is
    // exactly what `prefers-reduced-motion` exists to stop.
    if motion_reduced {
        return false;
    }
    // The native scene animates because its ORB breathes, and the orb breathes only on
    // hardware, exactly as `body.gpu-hardware #hart-voice-orb` does. Without the second
    // condition a drawn native scene held this gate open forever, so the pixman software
    // floor would have CPU-composited a still desktop at 60fps: the frame-budget gate
    // (#137) exists precisely to stop that, and the native shell was the one thing that
    // could defeat it, on the weakest hardware in the fleet.
    //
    // The workspace fade and the map animation are unconditional because they are
    // TRANSIENT: a few hundred milliseconds once, not a permanent 60fps hold, and both
    // are motion the user just asked for by switching or opening something.
    // `theme_potato` is the OTHER half of the shell's own reduced-effects verdict.
    // liquid_ui_service computes `is_potato = perf.disable_blur or gpu_mode == 'software'`
    // and that one flag strips its animation strings before they are ever emitted. The
    // GPU half was already mirrored by `motion_hardware`; this is the theme half, and it
    // is one key in a file the compositor already reads, so the third of the ledger's
    // motion kill-switches was never as far away as it looked.
    //
    // It sheds the same thing the hardware floor sheds and no more, which is rule 5's
    // "degrade gracefully, never gut": the PERPETUAL breath goes, the brief transients
    // stay. Only a stated preference stops those.
    (native_scene_drawn && motion_hardware && !theme_potato) || ws_fading || map_animating
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

/// Which surface the pointer is over RIGHT NOW, for latency attribution.
///
/// `Shell` whenever the native scene is not what is on screen (the flag is off, the
/// killswitch is up, or nothing has been laid out yet), which is exactly right: those
/// samples belong to the WebView shell, and the harness wants it measured by this same
/// instrument so "native is faster" is a demonstrated delta rather than a claim.
///
/// KNOWN AND ACCEPTED, stated rather than hidden: this reads the pointer BEFORE the event
/// is applied, because that is where T_input is captured and moving the capture would
/// bias the clock estimator toward busy periods. So a relative-motion sample is
/// attributed to the surface the pointer is LEAVING. It differs only at a component
/// boundary, and only for the one sample that crosses it; a drag stays inside its
/// component for hundreds of samples, which is where the headline numbers come from.
/// Why an input could not be attributed to a named component. Each reason is
/// reported ONCE per boot, the first time it is taken.
///
/// `Shell` is the answer to two completely different questions: "the pointer was
/// over the WebView shell" and "I could not work out where the pointer was". The
/// function returned the same value for both, so on hardware 2026-09-10 a sweep
/// of ~4,200 hover samples across the entire output, including a dwell on the
/// top bar where ORB_SM sits, came back 100% `component=shell` and looked like
/// clean data. It was not clean data. It was the attribution failing silently,
/// which left every one of latency_budgets.json's per-component rows dead and
/// made the native-versus-shell delta (the whole case for native chrome, and by
/// its own doc comment "a demonstrated delta rather than a claim") impossible to
/// measure.
///
/// The returned Surface is deliberately UNCHANGED: `Shell` stays the fallback, so
/// the journal contract and the budget file keep their meaning and no consumer
/// has to learn a new component name. What changes is that the fallback stops
/// being silent about which branch produced it.
static ATTRIB_REPORTED: std::sync::atomic::AtomicU8 = std::sync::atomic::AtomicU8::new(0);

fn report_attrib_gap(bit: u8, reason: &str) {
    use std::sync::atomic::Ordering;
    let prev = ATTRIB_REPORTED.fetch_or(bit, Ordering::Relaxed);
    if prev & bit == 0 {
        info!(
            reason,
            "hart-latency attribution: falling back to component=shell. Samples from              here on carry the shell's budget row, and every per-component row stays              empty until this reason is resolved."
        );
    }
}

/// The ambiguous branch also carries WHERE the pointer was, in the same scene
/// space the tree was laid out in. That is the one number that separates "the
/// desktop really is bare here" from "the two coordinate spaces do not line up",
/// and without it the reason line asks the reader to guess between them.
fn report_attrib_gap_at(
    bit: u8,
    reason: &str,
    px: f32,
    py: f32,
    size: Size<i32, Physical>,
) {
    use std::sync::atomic::Ordering;
    let prev = ATTRIB_REPORTED.fetch_or(bit, Ordering::Relaxed);
    if prev & bit == 0 {
        info!(
            reason,
            scene_x = px,
            scene_y = py,
            scene_w = size.w,
            scene_h = size.h,
            "hart-latency attribution: falling back to component=shell. Compare these              coordinates against the layout: a point inside the output but over no              component means the tree has no tagged container there."
        );
    }
}

fn pointer_surface<S: CompState>(state: &S) -> crate::latency::Surface {
    if !native_scene_drawn(state.native_shell_on(), state.capture_blocked()) {
        report_attrib_gap(
            1 << 0,
            if state.capture_blocked() {
                "capture blocked (killswitch up), so the native scene is not on screen"
            } else {
                "native_shell_on is false, so there is no native scene to attribute to"
            },
        );
        return crate::latency::Surface::Shell;
    }
    let size = output_physical_size(state);
    let Some((px, py)) = native_pointer_scene_pos(state, size) else {
        report_attrib_gap(
            1 << 1,
            "no pointer position in scene space (output geometry absent or zero-sized)",
        );
        return crate::latency::Surface::Shell;
    };
    let Some(tree) = state.native_tree() else {
        // Distinct from "nothing under the pointer": the tree is built by
        // lower_scene, so its absence means the native scene has not been
        // lowered even once, and NO position could ever attribute.
        report_attrib_gap(
            1 << 2,
            "native_tree() is None, so the scene has never been lowered and no              position can attribute",
        );
        return crate::latency::Surface::Shell;
    };
    match tree.component_at(px, py).map(|c| c.surface()) {
        Some(surface) => surface,
        None => {
            // The one genuinely ambiguous branch: the tree EXISTS and the pointer
            // has a position in its space, but that point lies over no tagged
            // component. Over bare desktop that is correct and expected. Covering
            // the whole output without ever hitting one is not, so the reported
            // coordinates are the thing to compare against the layout.
            report_attrib_gap_at(
                1 << 3,
                "pointer is over no tagged component (bare desktop, or the tree's                  geometry does not line up with the pointer's scene space)",
                px,
                py,
                size,
            );
            crate::latency::Surface::Shell
        }
    }
}

/// NATIVE SHELL M3 GL LOWERING: lower the native shell scene to render elements.
/// Rect leaves (top bar, taskbar, hero and card tiles) become SolidColorRenderElements;
/// Text runs are shaped + rasterized into cached MemoryRenderBuffers; OrbSlots reuse the
/// M2 orb texture. Image (texture) is the remaining leaf kind. Gated by `native_shell_on`
/// and the killswitch at the call site (`native_scene_drawn`), so with the flag OFF, or
/// while capture is blocked, this never runs. The actual lowering lives in `lower_scene`
/// (State-free, so it is render-tested); this wrapper just pulls the scene and caches off
/// `state`.
///
/// Alloc note: the zero-per-frame-alloc NFR is MET, in four parts. The scene tree is
/// retained (`scene::SceneCache`, rebuilt only on a real layout change); the sharp-rect
/// SolidColorBuffers are pooled (`RectCache::solid`, reused via `update`); the composed
/// home rides out of the accessor as a borrow rather than a clone; and the leaves are
/// walked by callback (`SceneNode::for_each_leaf`) rather than collected, which is what a
/// list of leaf references needs, since it borrows the tree the cache owns and so could
/// never be retained the way the tree and the pools are.
///
/// The one allocation left per frame is the caller's own `elements` vector, which is
/// architectural: smithay's render path takes a slice of elements, so the frame has to
/// build one. It is not counted against the NFR here for that reason. The claim is
/// structural rather than measured: no counting allocator is installed, so what the tests
/// pin is that the tree-rebuild, solid-allocation, text-compose and rounded-compose counts
/// all stay flat across a steady desktop's frames.
/// Returns the NATIVE_CHROME_* mask this frame actually emitted, so the shell bridge can
/// stand down the HTML chrome the compositor has taken over. See `lower_scene`.
pub fn render_native_scene<S, R>(
    state: &mut S,
    renderer: &mut R,
    size: Size<i32, Physical>,
    elements: &mut Vec<HartRenderElement<R>>,
) -> u8
where
    S: CompState,
    R: Renderer + ImportAll + ImportMem,
    R::TextureId: Send + Clone + 'static,
{
    // Pull the scene + caches OFF `state` here, then hand the concrete pieces to
    // `lower_scene`. Energy, pointer and button state are read FIRST because they are
    // plain owned values and the accessor below takes `&mut state`. This is the ONLY
    // caller that goes through State; the render test calls `lower_scene` directly with
    // constructed caches, so there is one lowering path, not two.
    let orb_energy = state.orb_energy();
    let pointer = native_pointer_scene_pos(state, size);
    let pressed = state.pointer_pressed();
    // Read BEFORE `native_scene_caches` takes its `&mut` borrow of state, and it is the
    // SAME bool `effects_animating` gates the frame budget on, so the orb's motion and
    // the frame rate that carries it can never disagree.
    let animate = state.motion_hardware() && !theme_potato() && !motion_reduced();
    // Read here too, for the same reason: `lower_scene` is state-free so the layout can
    // be render-tested with constructed caches, and the offset is state.
    //
    // RE-CLAMPED against the live extents first. A row scrolled to its end and then given
    // fewer cards, or shown on a narrower output, would otherwise keep an offset past its
    // own content and render EMPTY, with every card off the left edge. Clamping an
    // in-range value changes nothing, so a steady desktop writes back the same struct and
    // the cache key does not move: this costs a handful of float compares, not a rebuild.
    let mut scroll = state.row_scroll();
    {
        let extents = state
            .native_home()
            .map(|h| crate::scene::row_extents(h, size.w as f32, size.h as f32))
            .unwrap_or_default();
        let before = scroll;
        scroll.reclamp(&extents);
        if scroll != before {
            state.set_row_scroll(scroll);
        }
    }
    // The home now rides OUT of the accessor as a shared borrow beside the `&mut`
    // caches, so the frame no longer clones a HomeCompose just to release the state
    // borrow. `demo_ref` is the allocation-free fallback until `shell.compose` lands.
    let (home, rasterizer, orb_cache, rect_cache, scene_cache) = state.native_scene_caches();
    // A `match`, not `unwrap_or_else`: passing a `fn() -> &'static HomeCompose` makes the
    // compiler unify the Option's item type WITH 'static, which would demand that the
    // borrow of `state` outlive the program. The arms of a match unify at the shorter
    // lifetime instead, and the 'static demo simply coerces down to it.
    let home = match home {
        Some(h) => h,
        None => crate::scene::demo_ref(),
    };
    lower_scene(
        home, size, renderer, rasterizer, orb_cache, rect_cache, scene_cache, orb_energy,
        pointer, pressed, animate, &scroll, elements,
    )
}

/// Lower a `HomeCompose` to render elements against the concrete caches — the
/// Does the active THEME ask for the reduced-effects tier? Resolved once, beside the
/// others, and carrying the same restart-to-change gap.
///
/// `performance.disable_blur` is half of the shell's `is_potato`; the other half is the
/// software floor, which the compositor knows directly. Only `potato.json` sets it today.
///
/// Its sibling `performance.disable_animations` is NOT read here, deliberately: nothing
/// in the tree reads it either, so it is a dead key rather than a contract, and honouring
/// it natively would invent a behaviour the shell does not have.
fn theme_potato() -> bool {
    static POTATO: std::sync::OnceLock<bool> = std::sync::OnceLock::new();
    *POTATO.get_or_init(|| {
        crate::bloom::SettingsFile::active()
            .flag("disable_blur")
            .unwrap_or(false)
    })
}

/// Has the user declared reduced motion? Resolved ONCE, like the theme beside it, and
/// carrying the same documented gap: a runtime PUT to /api/shell/accessibility lives in
/// the shell process's memory and reaches this at the next start.
///
/// A `OnceLock` because the frame path must not touch the disk, and the answer is a
/// declarative setting rather than something that changes under us.
fn motion_reduced() -> bool {
    static REDUCED: std::sync::OnceLock<bool> = std::sync::OnceLock::new();
    *REDUCED.get_or_init(crate::bloom::reduced_motion)
}

/// The scene's colours, resolved ONCE from the same theme file the backdrop reads.
///
/// A `OnceLock` rather than a per-frame call because resolving it touches the disk, and
/// the frame path must not. This carries the SAME known gap `BloomCache` documents beside
/// it: resolved once and never re-read, so a theme change at runtime does not restyle the
/// native desktop until the compositor restarts. Whoever lands the theme-change signal
/// invalidates both together, and they are wrong in the same direction meanwhile, which
/// is the point of them reading one file.
fn active_theme() -> &'static crate::scene::Theme {
    static ACTIVE: std::sync::OnceLock<crate::scene::Theme> = std::sync::OnceLock::new();
    ACTIVE.get_or_init(|| {
        let file = crate::bloom::SettingsFile::active();
        theme_from_file(&file)
    })
}

/// The accessibility FONT SCALE applied to a theme metric, exactly as the shell applies
/// it: clamp to 0.8..=2.0, ignore anything within 0.01 of 1.0, and ROUND, because
/// liquid_ui_service emits `str(round(icon_size * fs))` and a half-pixel difference here
/// would be a different glyph size on the two renderers.
///
/// Only `--hart-icon-size` matters to the native scene today, and that is worth being
/// exact about rather than sweeping: the a11y override rewrites three tokens, and the
/// only two consumers in the whole served shell are `html,body{font-size:...}` (the root
/// size, which the home surface does not inherit because hartHome.css sizes everything in
/// absolute px) and `.tray-btn .mi{font-size:var(--hart-icon-size)}`, which IS a thing the
/// native scene draws. So a user at font_scale 1.5 got 30px tray glyphs in the shell and
/// 20px natively.
///
/// `None` scale, or a scale that rounds to no change, returns the metric untouched.
fn a11y_scaled(metric: Option<f32>, font_scale: Option<f32>) -> Option<f32> {
    let m = metric?;
    let s = match font_scale {
        Some(s) if s.is_finite() => s.clamp(0.8, 2.0),
        _ => return Some(m),
    };
    if (s - 1.0).abs() <= 0.01 {
        return Some(m);
    }
    Some((m * s).round())
}

/// Fold a loaded theme file's colours into the shipped defaults. Split out so it is
/// testable against a real file with no environment and no OnceLock in the way.
fn theme_from_file(file: &crate::bloom::SettingsFile) -> crate::scene::Theme {
    theme_from_files(file, &crate::bloom::SettingsFile::load(std::path::Path::new(
        crate::bloom::A11Y_SETTINGS_PATH,
    )))
}

/// The same fold with the accessibility file passed in, so a test can drive both without
/// touching /etc. The two files are separate on purpose: one is the look the user picked,
/// the other is what they need to be able to see it.
fn theme_from_files(
    file: &crate::bloom::SettingsFile,
    a11y: &crate::bloom::SettingsFile,
) -> crate::scene::Theme {
    // Applied LAST, after the theme's own colours and metrics, because that is what the
    // cascade does: `html.a11y-contrast` is a later source than `css_vars`, so it wins
    // over whatever the theme chose. A theme cannot opt out of high contrast.
    let contrast = a11y.flag("high_contrast").unwrap_or(false);
    let hue = |key: &str| {
        file.hex(key)
            .map(|[r, g, b]| {
                crate::scene::Color::rgba(
                    r as f32 / 255.0,
                    g as f32 / 255.0,
                    b as f32 / 255.0,
                    1.0,
                )
            })
    };
    let themed = crate::scene::Theme::cosmic_default()
        .with_theme_colors(
            hue("background"),
            hue("accent"),
            hue("secondary"),
            hue("text"),
            hue("muted"),
            hue("surface"),
        )
        // The same three keys theme_service emits as --hart-topbar-height,
        // --hart-icon-size and --hart-radius, so the native scene and the browser are
        // sized by one number each rather than two that happen to agree today.
        .with_shell_metrics(
            file.num("topbar_height"),
            // The ONE theme metric the accessibility font scale rewrites, applied with
            // the shell's own arithmetic so both renderers land on the same integer.
            a11y_scaled(file.num("icon_size"), a11y.num("font_scale")),
            file.num("border_radius"),
            file.rgba("glass_border").map(|([r, g, b], a)| {
                crate::scene::Color::rgba(r as f32 / 255.0, g as f32 / 255.0, b as f32 / 255.0, a)
            }),
        );
    if contrast {
        return themed.with_high_contrast();
    }
    themed
}

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
    scene_cache: &mut crate::scene::SceneCache,
    orb_energy: f32,
    pointer: Option<(f32, f32)>,
    pressed: bool,
    // `animate`: whether the orb breathes, the SAME hardware condition `scene_animates`
    // gates the frame budget on. Passed in rather than read here because this fn is
    // state-free. The two must agree, or the orb animates while the gate holds the frame
    // rate down (a stuttering orb) or the gate stays open for an orb standing still.
    animate: bool,
    // `scroll`: how far each card row is pushed sideways. A parameter, not a read, so the
    // lowering stays state-free and the render test can drive a scrolled desktop.
    scroll: &crate::scene::RowScroll,
    elements: &mut Vec<HartRenderElement<R>>,
) -> u8
where
    R: Renderer + ImportAll + ImportMem,
    R::TextureId: Send + Clone + 'static,
{
    // What native chrome this lowering ACTUALLY emitted, accumulated only on a successful
    // push exactly as the M2 orb and bloom blocks do. The shell bridge stands its own HTML
    // chrome down on the strength of this (liquid_ui_service.read_native_chrome), so a
    // claim that is not backed by real pixels would blank the orb on both sides, and a
    // claim that is missing leaves TWO orbs breathing on top of each other with the
    // WebView still paying the per-frame cost the native orb exists to remove.
    let mut emitted: u8 = 0;

    // RETAINED TREE (zero-per-frame-alloc, step two): the layout is rebuilt only when the
    // size, the composed home, or the theme changes, so a steady desktop reuses the tree
    // it already owns instead of allocating a fresh one every frame. The pointer is NOT a
    // key, so hover costs no rebuild. `scene_cache` is a disjoint field borrow, so holding
    // the tree across the loop does not conflict with the buffer caches below.
    let theme = *active_theme();
    // The rasterizer doubles as the layout's text measure (it already shapes), so the bar
    // can butt one run against another. It is a disjoint borrow from `scene_cache`, and
    // the reborrow ends when `tree_for` returns, leaving it free for the lowering below.
    let tree = scene_cache.tree_for(
        size.w as f32,
        size.h as f32,
        home,
        &theme,
        scroll,
        rasterizer,
    );

    // Hand out pooled solid buffers from the top for this frame (see RectCache::solid).
    rect_cache.begin_frame();

    // M2 input half: the pointer energises the orb it sits over (more while a button is
    // held). Fold the lift into the ambient energy ONCE here, against the SAME tree the
    // leaves come from, so orb reactivity rides the existing orb path (orb::motion_at
    // clamps the sum to 0..=1).
    let orb_energy = orb_energy + tree.pointer_orb_energy(pointer, pressed);

    // M2 input half, card slice: which leaf paints its hover state this frame (the
    // background of the interactive group under the cursor), or None. Resolved ONCE here
    // against the SAME tree the leaves come from, so the card highlight and the orb lift
    // above read one consistent pointer position.
    let hover_leaf = tree.hover_leaf(pointer);

    // Walked by CALLBACK, not collected into a Vec: a list of leaf references borrows the
    // tree the cache owns and so cannot be retained across frames, which made collecting
    // one the last per-frame allocation the NFR named. `return` inside the closure skips
    // this leaf, exactly where the loop said `continue`.
    tree.for_each_leaf(&mut |idx, leaf| {
        match leaf {
            crate::scene::SceneNode::Rect { rect, color, radius } => {
                if rect.w < 1.0 || rect.h < 1.0 {
                    return;
                }
                // The hover lift: the SAME rect, one brighter colour, so hovering changes
                // no geometry and no element count. The rounded cache keys on colour, so a
                // hovered card composes ONE extra buffer on the first frame of the hover
                // and reuses it for every frame after, never per frame.
                let color = if hover_leaf == Some(idx) {
                    color.lift(crate::scene::CARD_HOVER_LIFT)
                } else {
                    *color
                };
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
                    // rasterize. The buffer comes from the POOL, so a steady desktop
                    // reuses the one it handed out last frame instead of allocating.
                    let buf = rect_cache.solid(
                        rect.w as i32,
                        rect.h as i32,
                        [color.r, color.g, color.b, color.a],
                    );
                    let el = SolidColorRenderElement::from_buffer(
                        buf,
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
                stroke,
                weight,
                letter_spacing,
            } => {
                if rect.w < 1.0 || rect.h < 1.0 || text.is_empty() {
                    return;
                }
                let buffer = rasterizer.rasterize(
                    text,
                    *size_px,
                    rect.w as i32,
                    rect.h as i32,
                    [color.r, color.g, color.b, color.a],
                    *stroke,
                    *weight,
                    *letter_spacing,
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
            crate::scene::SceneNode::Fill {
                rect,
                from,
                mid,
                mid_at,
                to,
                angle_deg,
                radius,
                // The photo is not lowered yet (M3 remainder). The gradient beneath it is
                // what the shell paints first and never removes, so the card is a card
                // with or without one; before this, a card the feed gave a picture drew
                // its picture's ABSENCE, and a ranked card drew nothing whatsoever.
                photo: _,
            } => {
                if rect.w < 1.0 || rect.h < 1.0 {
                    return;
                }
                // Hovering an art tile lifts BOTH stops, so the whole tile brightens by the
                // same amount and the gradient keeps its shape. This is the ranked card's
                // only hover state, its background being transparent by design.
                let (from, mid, to) = if hover_leaf == Some(idx) {
                    (
                        from.lift(crate::scene::CARD_HOVER_LIFT),
                        mid.lift(crate::scene::CARD_HOVER_LIFT),
                        to.lift(crate::scene::CARD_HOVER_LIFT),
                    )
                } else {
                    (*from, *mid, *to)
                };
                if let Some(buffer) = rect_cache.gradient3(
                    rect.w as i32,
                    rect.h as i32,
                    *radius,
                    [from.r, from.g, from.b, from.a],
                    [mid.r, mid.g, mid.b, mid.a],
                    *mid_at,
                    [to.r, to.g, to.b, to.a],
                    *angle_deg,
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
                        Err(err) => warn!(?err, "native scene: card art import failed"),
                    }
                }
            }
            crate::scene::SceneNode::Shadow {
                rect,
                radius,
                blur,
                offset_y,
                color,
            } => {
                if rect.w < 1.0 || rect.h < 1.0 || *blur <= 0.0 {
                    return;
                }
                if let Some(buffer) = rect_cache.shadow(
                    rect.w as i32,
                    rect.h as i32,
                    *radius,
                    *blur,
                    [color.r, color.g, color.b, color.a],
                ) {
                    // The buffer is the caster grown by `blur` on every side, so it is
                    // drawn back by that much and down by the CSS offset.
                    let origin: Point<f64, Physical> = Point::from((
                        (rect.x - blur) as f64,
                        (rect.y + offset_y - blur) as f64,
                    ));
                    let side = (
                        rect.w as i32 + (2.0 * blur) as i32,
                        rect.h as i32 + (2.0 * blur) as i32,
                    );
                    match MemoryRenderBufferRenderElement::from_buffer(
                        renderer,
                        origin,
                        buffer,
                        Some(1.0),
                        None,
                        Some(side.into()),
                        Kind::Unspecified,
                    ) {
                        Ok(e) => elements.push(HartRenderElement::Memory(e)),
                        Err(err) => warn!(?err, "native scene: card shadow import failed"),
                    }
                }
            }
            crate::scene::SceneNode::OrbSlot { rect, .. } => {
                // The scene OWNS the orb (the hardcoded M2 draw is gated off when
                // native_shell_on), so ONE orb path. Both the large home orb and the
                // compact top-bar orb-sm share ONE cached texture composed at a fixed
                // size and render at their own slot size via GPU scale, so two slots in
                // one frame never thrash the single-buffer OrbCache.
                if rect.w < 1.0 || rect.h < 1.0 {
                    return;
                }
                let side = (size.w.min(size.h) as f32 * 0.30) as i32;
                if let Some((buffer, motion)) = orb_cache.current(side, orb_energy, animate) {
                    let dst = (rect.w.min(rect.h) * motion.scale) as i32;
                    if dst < 1 {
                        return;
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
                        Ok(e) => {
                            elements.push(HartRenderElement::Memory(e));
                            emitted |= NATIVE_CHROME_ORB;
                        }
                        Err(err) => warn!(?err, "native scene: orb import failed"),
                    }
                }
            }
            // Container only groups; it paints nothing of its own.
            _ => {}
        }
    });
    emitted
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
    //    additive path: flag off = no-op, the WebView shell is untouched. Skipped under
    //    the killswitch for the SAME reason the bloom and the M2 orb below are: the black
    //    solid pushed above already hides it, so lowering it is pure waste, and since a
    //    drawn native scene holds the frame-budget gate open it would otherwise composite
    //    at full rate behind a blacked-out screen. ──
    if native_scene_drawn(state.native_shell_on(), state.capture_blocked()) {
        // The scene CLAIMS the chrome it draws. Without this the flag would silently
        // un-claim the orb, because the M2 block below that used to set the bit is
        // skipped precisely when the native shell is on, and the shell would then keep
        // its own HTML orb: two orbs breathing over each other, the browser still paying
        // the per-frame cost, and the entire point of the native orb lost.
        let before = elements.len();
        let scene_mask = render_native_scene(state, renderer, size, &mut elements);
        native_mask |= scene_mask;
        if elements.len() > before {
            // Evidence for the compositor's shell-ready writer: elements the SCENE itself
            // put into this frame. Deliberately measured by growth of the element list
            // rather than by `scene_mask != 0`, which is what this used to test.
            //
            // The mask is not that evidence. `lower_scene` sets exactly ONE bit, and only
            // where the ORB's buffer imports; Rect, Text and Art all push elements and set
            // nothing. So `scene_mask != 0` means "the orb drew", and a frame that painted
            // the hero, the rows, the cards and both bars while the orb was skipped (a
            // sub-pixel slot, a cache miss, a failed import) claimed nothing had painted.
            // shell-ready would then never be written, the paint watchdog would stop seeing
            // HEALTHY, and the ladder would demote off the native shell on its own, which
            // is the exact failure the writer beside it in udev.rs was added to prevent.
            //
            // Growth of the list is also what this static's own doc says it means: "set
            // once the native scene has actually put elements into a frame".
            //
            // Still the SCENE's own contribution, not the accumulated mask: the bloom below
            // pushes an element whether or not the native shell drew anything, and it is
            // measured after this point, so the backdrop alone can never claim a painted
            // native shell.
            NATIVE_SCENE_PAINTED.store(true, std::sync::atomic::Ordering::Relaxed);
        }
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
        let animate = state.motion_hardware() && !theme_potato() && !motion_reduced();
        if let Some((buffer, motion)) = state.orb_mut().current(side, energy, animate) {
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
    bake_default_cursor_at(cursor_side())
}

/// The conventional default cursor size, and the size this arrow's polygon is drawn in.
const CURSOR_SIDE_DEFAULT: i32 = 24;

/// The cursor side from `XCURSOR_SIZE`, the standard the rest of the desktop already
/// speaks.
///
/// hart-accessibility.nix sets `XCURSOR_SIZE = "48"` when `largeCursor` is on, so a user
/// who turns Large Cursor on in the shell's own accessibility panel gets a 48px cursor
/// from every CLIENT that draws its own, and got a 24px one from the compositor, which is
/// the one that draws the desktop's. The toggle was offered, stored, wired through NixOS,
/// and had no effect on the arrow the user actually sees on the desktop.
///
/// Clamped, because it arrives from the environment: a zero or negative side has no
/// cursor at all and an enormous one is a full-screen arrow. Anything unparseable keeps
/// the conventional 24.
fn cursor_side() -> i32 {
    std::env::var("XCURSOR_SIZE")
        .ok()
        .and_then(|v| v.trim().parse::<i32>().ok())
        .map(|n| n.clamp(12, 256))
        .unwrap_or(CURSOR_SIDE_DEFAULT)
}

/// The arrow baked at an explicit side, so the scaling is testable without touching
/// process-global environment (cargo runs tests as threads in one process, and an
/// env-mutating test would race every other test in this module).
pub fn bake_default_cursor_at(side: i32) -> (Vec<u8>, i32, i32, Point<i32, Logical>) {
    let side = side.clamp(12, 256);
    let scale = side as f32 / CURSOR_SIDE_DEFAULT as f32;
    let (w, h) = (side, side);
    #[allow(non_snake_case)]
    let (W, H) = (w, h);
    // The polygon is authored in the 24-unit space this arrow was drawn in; every vertex
    // scales with the side so the SHAPE is identical at any size rather than an arrow
    // sitting in the corner of a bigger buffer.
    let poly: [(f32, f32); 7] = [
        (0.0, 0.0),
        (0.0, 17.0 * scale),
        (4.0 * scale, 13.0 * scale),
        (7.0 * scale, 19.0 * scale),
        (10.0 * scale, 18.0 * scale),
        (7.0 * scale, 12.0 * scale),
        (12.0 * scale, 12.0 * scale),
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

    // ── The M6 inversion: who OWNS the reservation once the compositor paints ──

    #[test]
    fn with_the_scene_off_the_reservation_is_exactly_what_the_shell_published() {
        // The zero-regression claim, stated as a test. Nothing about the shipped
        // WebView desktop may move because this code exists.
        let published = PanelReservation { top: 40, bottom: 44 };
        assert_eq!(effective_reservation(published, None), published);
        assert_eq!(
            effective_reservation(PanelReservation::default(), None),
            PanelReservation::default()
        );
    }

    #[test]
    fn the_demoted_webview_publishes_nothing_and_the_native_bars_are_still_reserved() {
        // The failure this whole inversion exists to prevent: M6 stands the shell
        // down, the file stops being written, panel_reservation fails safe to zero,
        // and a maximized window swallows bars the compositor is still painting.
        let native = PanelReservation { top: 36, bottom: 44 };
        assert_eq!(
            effective_reservation(PanelReservation::default(), Some(native)),
            native
        );
    }

    #[test]
    fn while_both_renderers_draw_bars_each_edge_takes_the_larger() {
        // Today's transition state: shell.native turns the scene on without standing
        // the WebView down, so both sets of bars are really on screen. Per edge,
        // independently, because the top can come from one and the bottom the other.
        let published = PanelReservation { top: 44, bottom: 44 };
        let native = PanelReservation { top: 36, bottom: 52 };
        assert_eq!(
            effective_reservation(published, Some(native)),
            PanelReservation { top: 44, bottom: 52 }
        );
    }

    #[test]
    fn the_native_reservation_reads_its_two_numbers_rather_than_restating_them() {
        // Guards the "no third source" property. Restating 40 and 44 here would let
        // a theme change or a TASKBAR_H change pass while the bars and the area they
        // reserve silently disagreed, which is the drift the cross-language guard in
        // test_panel_reservation.py exists to stop on the Python side.
        let r = native_chrome_reservation();
        assert_eq!(r.top, active_theme().top_bar_h.round() as i32);
        assert_eq!(r.bottom, crate::scene::TASKBAR_H.round() as i32);
    }

    #[test]
    fn an_absurd_native_reservation_still_cannot_squeeze_the_desktop_to_nothing() {
        // The maximum merge can only push the reservation UP, so the half-height cap
        // in work_area is what stops it becoming a desktop with no room for windows.
        let huge = PanelReservation { top: 4000, bottom: 4000 };
        let merged = effective_reservation(PanelReservation { top: 40, bottom: 44 }, Some(huge));
        let (_, y, _, h) = work_area(0, 0, 1600, 900, merged);
        assert!(h > 0, "the work area must never collapse: got h={h}");
        assert!(y <= 900 / 2, "the top reservation must stay inside the cap");
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
    fn a_large_cursor_setting_actually_grows_the_arrow_the_desktop_draws() {
        // hart-accessibility.nix sets XCURSOR_SIZE=48 when largeCursor is on, so every
        // CLIENT that draws its own cursor gets a 48px one. The compositor draws the
        // desktop's, from a polygon authored in a fixed 24-unit space, so the toggle was
        // offered in the shell's accessibility panel, stored, wired through NixOS, and
        // had no effect on the arrow the user actually sees.
        let (small, sw, sh, s_hot) = bake_default_cursor_at(24);
        let (big, bw, bh, b_hot) = bake_default_cursor_at(48);
        assert_eq!((sw, sh), (24, 24));
        assert_eq!((bw, bh), (48, 48));
        assert_eq!(big.len(), (bw * bh * 4) as usize);
        assert_eq!(s_hot, b_hot, "the tip is the hotspot at any size");

        // The SHAPE scales, rather than the same small arrow sitting in a bigger buffer.
        // Count opaque pixels: at twice the side the arrow covers about four times the
        // area, so a fixed-size arrow in a 48px buffer would be nowhere near.
        let opaque = |px: &[u8]| px.chunks_exact(4).filter(|p| p[3] == 255).count();
        let (a, b) = (opaque(&small), opaque(&big));
        assert!(a > 0 && b > 0, "both sizes draw something");
        let ratio = b as f32 / a as f32;
        assert!(
            (3.0..5.0).contains(&ratio),
            "doubling the side should roughly quadruple the ink, got {ratio:.2}x"
        );

        // The far corner of the big buffer is still empty: an arrow, not a filled square.
        let last = (bw * bh - 1) as usize * 4;
        assert_eq!(big[last + 3], 0, "the opposite corner stays transparent");

        // Absurd sides are clamped rather than trusted: this comes from the environment,
        // and a zero side is no cursor while a huge one is a full-screen arrow.
        let (_, tiny_w, _, _) = bake_default_cursor_at(0);
        let (_, huge_w, _, _) = bake_default_cursor_at(100_000);
        assert!(tiny_w >= 12, "a zero side is clamped up, not drawn");
        assert!(huge_w <= 256, "an enormous side is clamped down");
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

    /// The stop halfway between two, which is what a TWO-stop gradient's middle is.
    /// Written out here so the two-stop tests read as two-stop tests.
    fn mid_of(a: [f32; 4], b: [f32; 4]) -> [f32; 4] {
        [
            (a[0] + b[0]) * 0.5,
            (a[1] + b[1]) * 0.5,
            (a[2] + b[2]) * 0.5,
            (a[3] + b[3]) * 0.5,
        ]
    }

    #[test]
    fn rounded_rect_cuts_corners_and_fills_the_centre() {
        // A 12px radius on a 40x40 box: the exact corner pixel is outside the arc and
        // must be fully transparent, while the centre and the straight top edge are
        // fully covered. This is precisely what a hard SolidColorRenderElement cannot
        // express, so it is the reason rounded rects lower through a buffer.
        let (w, h) = (40u32, 40u32);
        let white = [1.0, 1.0, 1.0, 1.0];
        let rgba = rounded_rect_rgba(w, h, 12.0, white, mid_of(white, white), 0.5, white, 0.0);
        assert_eq!(rgba.len(), (w * h * 4) as usize);
        let alpha_at = |x: u32, y: u32| rgba[((y * w + x) * 4 + 3) as usize];
        assert_eq!(alpha_at(0, 0), 0, "top-left corner must be cut away");
        assert!(alpha_at(w / 2, h / 2) > 250, "centre must be opaque");
        assert!(alpha_at(w / 2, 0) > 250, "the straight top edge must be covered");
        // A zero radius is a plain filled rect: the corner is now covered too.
        let sharp = rounded_rect_rgba(w, h, 0.0, white, mid_of(white, white), 0.5, white, 0.0);
        assert!(sharp[3] > 250, "radius 0 fills the corner");
    }

    #[test]
    fn the_scene_takes_its_colours_from_the_same_theme_file_the_backdrop_does() {
        // The compositor was its own counter-example to Gate 4: bloom.rs reads
        // conky-themes/<id>.json for the backdrop while the scene carried a hardcoded
        // copy of the same colours, so changing the theme restyled the wallpaper under a
        // desktop that did not move. One file, both consumers.
        let dir = std::env::temp_dir().join("hart_scene_theme_test");
        std::fs::create_dir_all(&dir).unwrap();
        let f = dir.join("sunset.json");
        std::fs::write(
            &f,
            r#"{"id":"sunset","colors":{"background":"160910","accent":"FF8A4C",
               "secondary":"FF2E9A","text":"FFF3EC","muted":"C9A79B",
               "surface":"241118","ambient_1":"FF8A4C"}}"#,
        )
        .unwrap();
        let file = crate::bloom::SettingsFile::load(&f);
        let themed = theme_from_file(&file);
        let shipped = crate::scene::Theme::cosmic_default();
        assert_ne!(themed.accent, shipped.accent, "the theme's accent must win");
        assert_eq!(themed.accent, crate::scene::Color::rgba(1.0, 138.0 / 255.0, 76.0 / 255.0, 1.0));
        assert_eq!(themed.accent2.r, 1.0, "and its secondary");
        assert_eq!(
            themed.spectrum[0], themed.accent,
            "the spectrum leads with the functional accent, as the shipped one does"
        );
        // ALPHA is the surface treatment's, never the palette's: a theme names hues, and
        // letting it set opacity would let one make the top bar transparent.
        assert_eq!(themed.bar_bg.a, shipped.bar_bg.a, "bar opacity is not the theme's");
        assert_eq!(themed.card_bg.a, shipped.card_bg.a, "nor a card's");
        assert_ne!(themed.bar_bg.r, shipped.bar_bg.r, "but its hue is");

        // The live-tag scrim stays FIXED: it is a legibility guarantee over card art,
        // not a palette slot, so a pale theme cannot turn it pale-on-pale.
        assert_eq!(themed.chip_bg, shipped.chip_bg, "the live scrim is not the theme's");

        // The SAME file drives the backdrop, which is the whole point.
        let pal = crate::bloom::palette_from(&file);
        assert_eq!(pal.base, [0x16, 0x09, 0x10]);
        assert_eq!(
            (themed.bar_bg.r * 255.0).round() as u8,
            pal.base[0],
            "the bar and the backdrop must ground on one colour"
        );
    }

    #[test]
    fn a_theme_that_moves_the_bar_moves_the_native_bar_with_it() {
        // The shell publishes the panel reservation from `shell.topbar_height`, and the
        // native scene drew a fixed 40. Four of the ten shipped themes move that number
        // (36, 38, 40, 44), so on `potato` the native bar would have drawn 40px over a
        // 36px reservation: the 2026-08-29 "taskbar unreachable" report arriving through
        // the new renderer. Every shipped theme also carries its own corner radius, and
        // the DEFAULT one (aura) sets 22 against the hardcoded 16, so the cards were
        // already the wrong shape before anyone chose a theme.
        let dir = std::env::temp_dir().join("hart_shell_metrics_test");
        std::fs::create_dir_all(&dir).unwrap();
        let f = dir.join("potato.json");
        std::fs::write(
            &f,
            r#"{"id":"potato","colors":{"accent":"00E6C3"},
               "shell":{"topbar_height":36,"icon_size":18,"border_radius":4}}"#,
        )
        .unwrap();
        let themed = theme_from_file(&crate::bloom::SettingsFile::load(&f));
        assert_eq!(themed.top_bar_h, 36.0, "the bar takes the theme's height");
        assert_eq!(themed.icon_px, 18.0, "and the tray its glyph size");
        assert_eq!(themed.card_radius, 4.0, "and the cards their corner");

        // And the LAYOUT actually uses them: a shorter bar means the content band starts
        // higher, which is the whole point. Asserting the theme field alone would pass
        // while the layout still read a constant.
        let home = crate::scene::HomeCompose::demo();
        let tree = crate::scene::layout_home(
            1920.0,
            1080.0,
            &home,
            &themed,
            &crate::scene::RowScroll::default(),
            &mut crate::scene::MonoMeasure,
        );
        let mut leaves: Vec<&crate::scene::SceneNode> = Vec::new();
        tree.flatten(&mut leaves);
        let bar = leaves
            .iter()
            .find_map(|n| match n {
                // The strip is a Fill, not a Rect: the shell's no-blur chrome floor is a
                // three-stop ramp, so the native strips are gradient tiles.
                crate::scene::SceneNode::Fill { rect, .. }
                    if rect.x == 0.0 && rect.y == 0.0 && rect.w == 1920.0 =>
                {
                    Some(*rect)
                }
                _ => None,
            })
            .expect("the top bar strip");
        assert_eq!(bar.h, 36.0, "the bar the scene DRAWS is the theme's height");
    }

    #[test]
    fn a_wheel_notch_moves_a_row_by_the_browsers_own_step() {
        // libinput reports a mouse notch as 15 units (its v120 axis is normalised to 15
        // by the caller), so a notch has to land on 120px: that is the browser's wheel
        // step, and the shell's `overflow-x` rails already move by it. The same gesture
        // must travel the same distance on both renderers or the two desktops feel
        // different under the same hand.
        assert_eq!(row_scroll_delta(15.0, 0.0), 120.0, "one notch is one browser step");
        assert_eq!(row_scroll_delta(-15.0, 0.0), -120.0, "and back the other way");

        // A sideways swipe scrolls it too, and the two are SUMMED rather than one
        // winning: a diagonal touchpad gesture moves the row by what the finger
        // travelled, not by whichever axis happened to be tested first.
        assert_eq!(row_scroll_delta(0.0, 15.0), 120.0, "horizontal alone works");
        assert_eq!(row_scroll_delta(5.0, 10.0), 120.0, "and a diagonal sums");
        assert_eq!(row_scroll_delta(10.0, -10.0), 0.0, "opposing axes cancel");

        // Nothing non-finite reaches the offset: this comes from a device.
        assert_eq!(row_scroll_delta(f32::NAN, 0.0), 0.0);
        assert_eq!(row_scroll_delta(f32::INFINITY, 0.0), 0.0);
        assert_eq!(row_scroll_delta(0.0, 0.0), 0.0, "a null event moves nothing");
    }

    #[test]
    fn high_contrast_makes_the_chrome_solid_and_doubles_its_rule() {
        // `html.a11y-contrast` overrides four tokens and thickens the glass border. The
        // native scene read its colours from the theme file and knew nothing about the
        // class, so a high-contrast desktop would have gone native at ordinary contrast:
        // translucent bars, a faint rule, and dim secondary text, which is the whole set
        // of things the setting exists to remove.
        let dir = std::env::temp_dir().join("hart_contrast_test");
        std::fs::create_dir_all(&dir).unwrap();
        let theme = dir.join("t.json");
        std::fs::write(
            &theme,
            r#"{"colors":{"background":"04050B","text":"F2F4FF","muted":"9AA0C6",
               "glass_border":"rgba(255,255,255,0.10)"}}"#,
        )
        .unwrap();
        let on = dir.join("on.json");
        std::fs::write(&on, r#"{"high_contrast":true}"#).unwrap();
        let off = dir.join("off.json");
        std::fs::write(&off, r#"{"high_contrast":false}"#).unwrap();

        let plain = theme_from_files(
            &crate::bloom::SettingsFile::load(&theme),
            &crate::bloom::SettingsFile::load(&off),
        );
        let hc = theme_from_files(
            &crate::bloom::SettingsFile::load(&theme),
            &crate::bloom::SettingsFile::load(&on),
        );

        // The chrome goes SOLID. This is the one place a palette sets opacity, and on
        // purpose: translucency is what high contrast exists to remove.
        assert!(plain.bar_bg.a < 1.0, "the ordinary bar is translucent");
        assert_eq!(hc.bar_bg.a, 1.0, "the high-contrast bar is not");
        assert_eq!(hc.taskbar_bg, hc.bar_bg, "both strips take the same solid");

        // The rule goes white and DOUBLES.
        assert_eq!(hc.chrome_border, crate::scene::Color::rgba(1.0, 1.0, 1.0, 1.0));
        assert_eq!(hc.chrome_rule_px, plain.chrome_rule_px * 2.0);

        // Ink goes to pure white and the secondary ink to near-white, so the two are
        // still distinguishable rather than collapsed into one.
        assert_eq!(hc.card_ink, crate::scene::Color::rgba(1.0, 1.0, 1.0, 1.0));
        assert_ne!(hc.hero_copy, hc.hero_title, "muted stays a step below text");
        assert!(hc.hero_copy.r > plain.hero_copy.r, "and it is far brighter than before");

        // A theme cannot opt out: the class is a LATER source than css_vars, so it wins
        // over whatever the theme chose. Applying it before the theme would let a theme
        // with its own glass_border quietly undo the accessibility setting.
        assert_ne!(hc.chrome_border, plain.chrome_border);
    }

    #[test]
    fn a_declared_font_scale_grows_the_tray_glyph_the_way_the_shell_does() {
        // liquid_ui_service emits `--hart-icon-size: round(icon_size * fs)px` when the
        // scale is set, and `.tray-btn .mi` reads it. The native scene draws that glyph
        // and ignored the scale, so a user at 1.5 got 30px in the shell and 20 natively.
        let dir = std::env::temp_dir().join("hart_fontscale_test");
        std::fs::create_dir_all(&dir).unwrap();
        let theme = dir.join("t.json");
        std::fs::write(&theme, r#"{"shell":{"icon_size":20,"topbar_height":40}}"#).unwrap();
        let big = dir.join("big.json");
        std::fs::write(&big, r#"{"font_scale":1.5,"reduced_motion":false}"#).unwrap();

        let t = theme_from_files(
            &crate::bloom::SettingsFile::load(&theme),
            &crate::bloom::SettingsFile::load(&big),
        );
        assert_eq!(t.icon_px, 30.0, "20 * 1.5, the shell's own arithmetic");
        assert_eq!(t.top_bar_h, 40.0, "the bar is NOT font-scaled, and the shell agrees");

        // The rounding is the shell's too: `str(round(...))`, so 20 * 1.15 is 23, not
        // 23.0000004 and not 22. A half-pixel difference is a different glyph.
        let odd = dir.join("odd.json");
        std::fs::write(&odd, r#"{"font_scale":1.15}"#).unwrap();
        assert_eq!(
            theme_from_files(
                &crate::bloom::SettingsFile::load(&theme),
                &crate::bloom::SettingsFile::load(&odd),
            )
            .icon_px,
            23.0
        );
    }

    #[test]
    fn the_font_scale_rule_matches_the_shells_clamp_and_deadband() {
        // Pure, so the edges are checkable without files. The shell clamps 0.8..2.0 and
        // ignores anything within 0.01 of 1.0; both matter, because a hostile or
        // fat-fingered setting reaches this from a file and "no change" must mean the
        // metric is untouched rather than multiplied by something near one and rounded.
        assert_eq!(a11y_scaled(Some(20.0), None), Some(20.0), "no scale, no change");
        assert_eq!(a11y_scaled(Some(20.0), Some(1.0)), Some(20.0), "exactly one");
        assert_eq!(a11y_scaled(Some(20.0), Some(1.005)), Some(20.0), "inside the deadband");
        assert_eq!(a11y_scaled(Some(20.0), Some(0.1)), Some(16.0), "clamped up to 0.8");
        assert_eq!(a11y_scaled(Some(20.0), Some(99.0)), Some(40.0), "clamped down to 2.0");
        assert_eq!(a11y_scaled(Some(20.0), Some(f32::NAN)), Some(20.0), "NaN is not a scale");
        assert_eq!(a11y_scaled(None, Some(1.5)), None, "nothing to scale");
        // ROUNDING, on a value where it plainly matters: the shell emits an integer
        // pixel string, so 20 * 1.13 is a 23px glyph on both renderers, not 22.6 on one.
        assert_eq!(a11y_scaled(Some(20.0), Some(1.13)), Some(23.0));
        assert_eq!(a11y_scaled(Some(20.0), Some(1.12)), Some(22.0), "and rounds DOWN too");
    }

    #[test]
    fn the_chrome_strips_take_their_rule_colour_from_the_theme() {
        // `--hart-glass-border` is written as `rgba(...)`, not hex, which is exactly why
        // it could not be read before: `hex` finds no `#RRGGBB` and returns None, so the
        // native strips had no separator at all and their edge was wherever the
        // translucency happened to stop. It varies real amounts by theme, so it is not a
        // constant that could have been mirrored.
        let dir = std::env::temp_dir().join("hart_border_test");
        std::fs::create_dir_all(&dir).unwrap();
        let f = dir.join("cyber.json");
        std::fs::write(
            &f,
            r#"{"colors":{"accent":"FF0090","glass_border":"rgba(255, 0, 144, 0.2)"}}"#,
        )
        .unwrap();
        let t = theme_from_files(
            &crate::bloom::SettingsFile::load(&f),
            &crate::bloom::SettingsFile::load(std::path::Path::new("/nope.json")),
        );
        assert_eq!(t.chrome_border.r, 1.0);
        assert_eq!(t.chrome_border.g, 0.0);
        assert!((t.chrome_border.b - 144.0 / 255.0).abs() < 1e-6);
        assert!((t.chrome_border.a - 0.2).abs() < 1e-6, "the ALPHA is the point");

        // Spacing varies across the shipped themes and both forms are in the tree.
        let tight = dir.join("aura.json");
        std::fs::write(&tight, r#"{"colors":{"glass_border":"rgba(255,255,255,0.10)"}}"#)
            .unwrap();
        let t2 = theme_from_files(
            &crate::bloom::SettingsFile::load(&tight),
            &crate::bloom::SettingsFile::load(std::path::Path::new("/nope.json")),
        );
        assert_eq!((t2.chrome_border.r, t2.chrome_border.g, t2.chrome_border.b), (1.0, 1.0, 1.0));
        assert!((t2.chrome_border.a - 0.10).abs() < 1e-6);

        // A theme with no glass_border keeps the shell's own ThemeService-failure
        // fallback rather than drawing nothing: a missing key must not delete the rule.
        let bare = dir.join("bare.json");
        std::fs::write(&bare, r#"{"colors":{"accent":"00E6C3"}}"#).unwrap();
        assert_eq!(
            theme_from_files(
                &crate::bloom::SettingsFile::load(&bare),
                &crate::bloom::SettingsFile::load(std::path::Path::new("/nope.json")),
            )
            .chrome_border,
            crate::scene::Theme::cosmic_default().chrome_border
        );
    }

    #[test]
    fn a_theme_cannot_hand_the_compositor_an_absurd_bar() {
        // These numbers come from a FILE. A zero or negative bar inverts the content
        // band's arithmetic and an enormous one leaves no desktop, so they are clamped
        // rather than trusted. The bounds are wide enough that every shipped theme passes
        // through untouched, which the guard test asserts from the other side.
        let dir = std::env::temp_dir().join("hart_shell_metrics_test");
        std::fs::create_dir_all(&dir).unwrap();
        let f = dir.join("absurd.json");
        std::fs::write(
            &f,
            r#"{"id":"absurd","shell":{"topbar_height":0,"icon_size":9999,
               "border_radius":-40}}"#,
        )
        .unwrap();
        let themed = theme_from_file(&crate::bloom::SettingsFile::load(&f));
        assert!(themed.top_bar_h >= 16.0, "a zero bar is clamped, not drawn");
        assert!(themed.icon_px <= 64.0, "a giant glyph is clamped");
        assert!(themed.card_radius >= 0.0, "a negative radius is clamped");
        // A non-numeric value is not a number at all: keep the shipped default.
        let g = dir.join("text.json");
        std::fs::write(&g, r#"{"shell":{"topbar_height":"tall"}}"#).unwrap();
        assert_eq!(
            theme_from_file(&crate::bloom::SettingsFile::load(&g)).top_bar_h,
            crate::scene::Theme::cosmic_default().top_bar_h,
            "a malformed height keeps the shipped bar"
        );
    }

    #[test]
    fn an_absent_theme_file_leaves_the_shipped_desktop_exactly_as_it_was() {
        // The fallback is the safety property: this runs in the process that owns
        // scanout, so an unreadable theme must cost nothing at all rather than a colour
        // the user cannot explain. Byte-identical to before the file was ever read.
        let missing = crate::bloom::SettingsFile::load(std::path::Path::new("/definitely/not/here.json"));
        assert_eq!(theme_from_file(&missing), crate::scene::Theme::cosmic_default());
        // A file that parses but names nothing we use is the same case.
        let dir = std::env::temp_dir().join("hart_scene_theme_test");
        std::fs::create_dir_all(&dir).unwrap();
        let f = dir.join("bare.json");
        std::fs::write(&f, r#"{"id":"bare","font":{"size":14}}"#).unwrap();
        assert_eq!(
            theme_from_file(&crate::bloom::SettingsFile::load(&f)),
            crate::scene::Theme::cosmic_default()
        );
    }

    #[test]
    fn a_card_shadow_is_dark_under_the_card_and_gone_a_blur_away() {
        // `.hh-card`'s own comment: the static drop-shadow "rasters ONCE and composites
        // cheaply forever, so the software floor KEEPS it (degrade gracefully, not gut)
        // ... Without this the software home read as flat rectangles." The native cards
        // had none, which is that reported symptom exactly.
        let (cw, ch, blur) = (100u32, 60u32, 20.0f32);
        let px = shadow_rgba(cw + 40, ch + 40, 16.0, blur, [0.0, 0.0, 0.0, 0.46]);
        let (w, h) = (cw + 40, ch + 40);
        let at = |x: u32, y: u32| px[((y * w + x) * 4 + 3) as usize];

        // Solid under the middle of the caster, at the colour's own alpha.
        let mid = at(w / 2, h / 2);
        assert!(mid >= 115 && mid <= 118, "the core is the shadow's alpha, got {mid}");
        // Gone at the buffer's edge, a full blur out from the shape.
        assert_eq!(at(0, 0), 0, "the corner of the buffer is clear");
        assert_eq!(at(w - 1, h - 1), 0, "and so is the far one");
        // And MONOTONIC outward across the edge: a shadow that brightened partway would
        // be a ring rather than a falloff.
        let mut prev = 255u8;
        for x in (w / 2)..w {
            let a = at(x, h / 2);
            assert!(a <= prev, "brightened at x={x}: {a} after {prev}");
            prev = a;
        }
        // Softness scales with the blur: the same caster with twice the blur reaches
        // further, which is what makes 38px depth read as depth rather than an outline.
        let wide = shadow_rgba(cw + 80, ch + 80, 16.0, 40.0, [0.0, 0.0, 0.0, 0.46]);
        let ww = cw + 80;
        let edge_of = |buf: &[u8], stride: u32, y: u32| -> u32 {
            (0..stride)
                .filter(|x| buf[((y * stride + x) * 4 + 3) as usize] > 0)
                .count() as u32
        };
        assert!(
            edge_of(&wide, ww, (ch + 80) / 2) > edge_of(&px, w, h / 2),
            "a bigger blur covers more of its row"
        );
    }

    #[test]
    fn every_card_on_the_desktop_shares_one_composed_shadow() {
        // The whole reason the shell keeps this on the software floor is that it rasters
        // once. A per-card compose would make it the opposite of what it is for, and the
        // cards are all one size, so one buffer must serve the lot.
        let mut rects = RectCache::default();
        let black = [0.0, 0.0, 0.0, 0.46];
        for _ in 0..12 {
            assert!(rects.shadow(258, 150, 16.0, 38.0, black).is_some());
        }
        assert_eq!(rects.rounded_composes(), 1, "twelve cards, one composed shadow");

        // A shadow and a TILE of the same size are different buffers: they share the
        // cache, so a key collision would hand a card its own shadow as its art.
        assert!(rects.gradient(258, 150, 16.0, black, black, 0.0).is_some());
        assert_eq!(rects.rounded_composes(), 2, "the tile composed separately");
    }

    #[test]
    fn a_gradient_tile_actually_varies_along_its_angle() {
        // The card art is the ONLY thing on the desktop whose fill is not constant, so
        // "it drew something" is not enough: a solid fill would satisfy a coverage check
        // and still be the flat tile this replaced. Probe the two ends of the gradient
        // line and require them to differ, then require the same tile at a different
        // angle to differ from it as well (which a fill ignoring `angle_deg` would fail).
        let (w, h) = (64u32, 64u32);
        let black = [0.0, 0.0, 0.0, 1.0];
        let red = [1.0, 0.0, 0.0, 1.0];
        // 180deg points straight DOWN the tile, so t runs with y and the probe is exact.
        let g = rounded_rect_rgba(w, h, 0.0, black, mid_of(black, red), 0.5, red, 180.0);
        let red_at = |buf: &[u8], x: u32, y: u32| buf[((y * w + x) * 4 + 2) as usize];
        let top = red_at(&g, w / 2, 1);
        let bottom = red_at(&g, w / 2, h - 2);
        assert!(top < 16, "the first stop end must still be the FROM colour");
        assert!(bottom > 239, "the far end must have reached the TO colour");
        assert!(bottom > top, "the fill must ramp from `from` to `to`, not average them");
        // 0deg is the same line reversed, so the ramp must invert rather than repeat.
        let up = rounded_rect_rgba(w, h, 0.0, black, mid_of(black, red), 0.5, red, 0.0);
        assert!(
            red_at(&up, w / 2, 1) > red_at(&up, w / 2, h - 2),
            "the angle must actually steer the gradient"
        );
        // A solid tile is the degenerate case and must stay perfectly flat, whatever
        // angle it is handed: that is what lets `rounded` share this one rasterizer.
        let flat = rounded_rect_rgba(w, h, 0.0, red, mid_of(red, red), 0.5, red, 135.0);
        assert_eq!(
            red_at(&flat, 1, 1),
            red_at(&flat, w - 2, h - 2),
            "from == to must fill flat"
        );
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
    fn the_scene_mask_is_not_evidence_that_the_scene_painted() {
        // WHY: build_frame_elements used to set NATIVE_SCENE_PAINTED (which gates the
        // compositor's shell-ready writer) on `scene_mask != 0`. lower_scene sets exactly
        // one bit and only where the ORB imports, so that test really asked "did the orb
        // draw". This lowers at an output small enough that the orb slot is skipped and
        // shows the two answers coming apart: real elements in the frame, empty mask.
        //
        // If that gate is ever rewritten back to the mask, this is the frame that breaks
        // it: shell-ready never gets written, the paint watchdog stops seeing HEALTHY, and
        // the ladder demotes off the native shell by itself.
        let mut renderer = PixmanRenderer::new().expect("pixman renderer allocates headless");
        let size: Size<i32, Physical> = (3, 3).into();

        let home = crate::scene::HomeCompose::demo();
        let mut rasterizer = crate::text_render::TextRasterizer::new();
        let mut orb = OrbCache::default();
        let mut rects = RectCache::default();
        let mut scenes = crate::scene::SceneCache::default();

        let mut elements: Vec<HartRenderElement<PixmanRenderer>> = Vec::new();
        let mask = lower_scene(
            &home,
            size,
            &mut renderer,
            &mut rasterizer,
            &mut orb,
            &mut rects,
            &mut scenes,
            0.5,
            None,
            false,
            true,
            &crate::scene::RowScroll::default(),
            &mut elements,
        );

        assert_eq!(
            mask & NATIVE_CHROME_ORB,
            0,
            "expected an output too small for the orb slot; pick a smaller one"
        );
        assert!(
            !elements.is_empty(),
            "the scene still paints chrome at this size, which is the whole point"
        );
        // So this is a frame the OLD gate called unpainted while it demonstrably painted.
        assert_eq!(mask, 0, "no other bit should be standing in for the orb's");
    }

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
        let mut scenes = crate::scene::SceneCache::default();

        let mut elements: Vec<HartRenderElement<PixmanRenderer>> = Vec::new();
        lower_scene(
            &home,
            size,
            &mut renderer,
            &mut rasterizer,
            &mut orb,
            &mut rects,
            &mut scenes,
            0.5,
            None,
            false,
            true,
            &crate::scene::RowScroll::default(),
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
    fn a_steady_desktop_reuses_its_buffers_instead_of_allocating_each_frame() {
        // The zero-per-frame-alloc NFR, proven the way a frame loop actually runs: same
        // caches, same scene, repeated lowerings, with the element vector rebuilt each
        // time exactly as build_frame_elements does.
        let mut renderer = PixmanRenderer::new().expect("pixman renderer allocates headless");
        let size: Size<i32, Physical> = (1280, 800).into();

        let home = crate::scene::HomeCompose::demo();
        let mut rasterizer = crate::text_render::TextRasterizer::new();
        let mut orb = OrbCache::default();
        let mut rects = RectCache::default();
        let mut scenes = crate::scene::SceneCache::default();

        let mut first = 0usize;
        let mut solids_after_first_frame = 0u64;
        let mut text_after_first_frame = 0u64;
        let mut rounded_after_first_frame = 0u64;
        for frame in 0..6 {
            // A fresh element vector each pass, exactly as build_frame_elements does, so
            // the previous frame's elements are dropped before the buffers are reused.
            let mut elements: Vec<HartRenderElement<PixmanRenderer>> = Vec::new();
            lower_scene(
                &home,
                size,
                &mut renderer,
                &mut rasterizer,
                &mut orb,
                &mut rects,
                &mut scenes,
                0.5,
                None,
                false,
                true,
                &crate::scene::RowScroll::default(),
                &mut elements,
            );
            if frame == 0 {
                first = elements.len();
                solids_after_first_frame = rects.solid_allocs();
                text_after_first_frame = rasterizer.composes();
                rounded_after_first_frame = rects.rounded_composes();
                assert!(first > 0, "the demo scene lowered to nothing");
                assert_eq!(scenes.rebuilds(), 1, "the first frame builds the tree once");
                assert!(
                    solids_after_first_frame > 0,
                    "the demo scene has sharp rects, so the first frame allocates solids"
                );
                assert!(
                    rounded_after_first_frame > 0,
                    "the demo scene has rounded cards, so the first frame composes some"
                );
                // Without this the compose-once assertion below could hold simply because
                // nothing was ever composed. The bar and hero carry real runs, and the
                // counter advances even with no fonts installed (compose returns a blank
                // buffer but is still a compose), so this holds in a bare sandbox too.
                assert!(
                    text_after_first_frame > 0,
                    "the demo scene has text runs, so the first frame composes some"
                );
                // The demo is the payload the box shows before any compose arrives, and
                // an empty title lowers to nothing, so a demo of default cards would
                // render blank tiles. Every card carries text; this catches a regression
                // back to Card::default() in the one payload that must look like a
                // desktop unaided.
                assert!(
                    text_after_first_frame >= 12,
                    "the demo must carry real card text, only {text_after_first_frame} runs"
                );
            } else {
                assert_eq!(
                    elements.len(),
                    first,
                    "a steady desktop must lower the same element set every frame"
                );
            }
        }

        assert_eq!(
            scenes.rebuilds(),
            1,
            "a steady desktop must not rebuild the scene tree per frame"
        );
        assert_eq!(
            rects.solid_allocs(),
            solids_after_first_frame,
            "five further frames must reuse the pooled solids, not allocate new ones"
        );
        // COMPOSE-ONCE, the other binding NFR. These two are the expensive per-pixel work
        // in a frame: shaping and drawing a text run, and rasterizing a rounded-rect SDF.
        // A cache key that accidentally carried something unstable would redo all of it
        // every frame and the element counts above would still match, so these assertions
        // are the only thing standing between a cached desktop and a re-rasterized one.
        assert_eq!(
            rasterizer.composes(),
            text_after_first_frame,
            "a steady desktop must not re-shape its text runs"
        );
        assert_eq!(
            rects.rounded_composes(),
            rounded_after_first_frame,
            "a steady desktop must not re-rasterize its rounded rects"
        );
    }

    #[test]
    fn the_native_shell_counts_as_animating_so_the_orb_is_not_throttled_to_the_heartbeat() {
        // The frame-budget gate skips a tick when nothing is dirty and nothing animates,
        // falling back to a 200ms idle heartbeat. The native orb breathes off the clock,
        // so without this the flip to the native shell would quietly render that breath
        // at 5 Hz: a stutter, not a breath, and against the 60fps NFR.
        assert!(
            scene_animates(true, true, false, false, false, false),
            "a drawn native scene animates by construction, its orb never stops breathing"
        );
        // Flag OFF is untouched, which is what keeps the shipped WebView desktop's idle
        // saving: the orb still breathes down there, but occluded, so it costs nothing.
        assert!(!scene_animates(false, true, false, false, false, false));
        // The two effects that already forced a paint still do, with the flag off.
        assert!(scene_animates(false, true, false, false, true, false), "a workspace fade must play out");
        assert!(scene_animates(false, true, false, false, false, true), "a map animation must play out");
    }

    #[test]
    fn a_degenerate_output_lowers_without_panicking() {
        // The layout half of this is proven in scene.rs; this is the other half, where a
        // bad rect would actually land: buffer allocation and texture import. A zero-size
        // output is reachable while a mode is being set or a CRTC returns from DPMS, and
        // the compositor cannot afford a panic on the render path at any size.
        let mut renderer = PixmanRenderer::new().expect("pixman renderer allocates headless");
        let home = crate::scene::HomeCompose::demo();
        let mut rasterizer = crate::text_render::TextRasterizer::new();
        let mut orb = OrbCache::default();
        let mut rects = RectCache::default();
        let mut scenes = crate::scene::SceneCache::default();

        for (w, h) in [(0, 0), (1, 1), (0, 900), (1600, 0), (320, 40), (2, 84)] {
            let size: Size<i32, Physical> = (w, h).into();
            let mut elements: Vec<HartRenderElement<PixmanRenderer>> = Vec::new();
            lower_scene(
                &home, size, &mut renderer, &mut rasterizer, &mut orb, &mut rects, &mut scenes,
                0.5, Some((10.0, 10.0)), true, true,
                &crate::scene::RowScroll::default(),
                &mut elements,
            );
            // Whatever survived must still have a real footprint: the <1px skips exist so
            // nothing reaches the renderer with an empty or inverted box.
            for e in &elements {
                let g = e.geometry(Scale::from(1.0));
                assert!(
                    g.size.w > 0 && g.size.h > 0,
                    "{w}x{h} lowered an element with empty geometry {g:?}"
                );
            }
        }
    }

    #[test]
    fn the_native_scene_claims_the_orb_it_draws() {
        // The shell hides its own HTML orb only when the compositor claims 'orb' through
        // NATIVE_CHROME_EMITTED (liquid_ui_service.read_native_chrome). The M2 block that
        // used to set that bit is skipped exactly when the native shell is on, so the
        // scene must claim it itself or the flip ships two orbs, one breathing under the
        // other, with the WebView still burning a core on the one nobody needed.
        let mut renderer = PixmanRenderer::new().expect("pixman renderer allocates headless");
        let size: Size<i32, Physical> = (1280, 800).into();
        let home = crate::scene::HomeCompose::demo();
        let mut rasterizer = crate::text_render::TextRasterizer::new();
        let mut orb = OrbCache::default();
        let mut rects = RectCache::default();
        let mut scenes = crate::scene::SceneCache::default();
        let mut elements: Vec<HartRenderElement<PixmanRenderer>> = Vec::new();

        let emitted = lower_scene(
            &home, size, &mut renderer, &mut rasterizer, &mut orb, &mut rects, &mut scenes,
            0.5, None, false, true, &crate::scene::RowScroll::default(), &mut elements,
        );
        assert_eq!(
            emitted & NATIVE_CHROME_ORB,
            NATIVE_CHROME_ORB,
            "the home scene draws an orb, so it must claim one"
        );
        // The claim is only ever made on a real push, so it cannot outrun the pixels.
        assert!(!elements.is_empty());
        // Bloom is NOT the scene's to claim: the backdrop block still emits it, and
        // claiming it here would blank the shell's backdrop against nothing.
        assert_eq!(emitted & NATIVE_CHROME_BLOOM, 0);
    }

    #[test]
    fn the_killswitch_stops_the_native_scene_being_drawn_or_holding_the_gate_open() {
        // The killswitch pushes an opaque full-output black solid ABOVE everything, so
        // the native scene under it is invisible either way and nothing leaks. What it
        // must not do is keep costing: lowering a hidden scene is waste, and because a
        // drawn native scene holds the frame-budget gate open, an unguarded one would
        // composite at full rate behind a blacked-out screen.
        assert!(native_scene_drawn(true, false), "flag on, not blocked: drawn");
        assert!(!native_scene_drawn(true, true), "the killswitch hides it, so skip it");
        assert!(!native_scene_drawn(false, false), "flag off: never drawn");
        assert!(!native_scene_drawn(false, true));
        // And the gate agrees, because both decisions read the same predicate.
        assert!(!scene_animates(native_scene_drawn(true, true), true, false, false, false, false));
        assert!(scene_animates(native_scene_drawn(true, false), true, false, false, false, false));
        // A real animation still plays out under the killswitch: correctness first, the
        // saving is only ever about the native scene.
        assert!(scene_animates(native_scene_drawn(true, true), true, false, false, true, false));
    }

    #[test]
    fn the_software_floor_stops_the_native_desktop_compositing_at_full_rate() {
        // The frame-budget gate (#137) exists so a still desktop stops re-importing
        // textures and attempting a page-flip every 16ms. A drawn native scene held it
        // open unconditionally, because its orb breathes, so the native shell was the one
        // thing in the compositor that could defeat that gate entirely, on the pixman
        // software floor, where the CPU pays for every composite. The HTML shell has
        // never done this: its breathing is `body.gpu-hardware #hart-voice-orb` and
        // nothing else, and liquid_ui_service records why (real-HW 2026-07-12, GPU-only
        // effects on a CPU renderer hung the whole shell).
        assert!(
            scene_animates(true, true, false, false, false, false),
            "GPU-composited with the scene drawn: the orb breathes"
        );
        assert!(
            !scene_animates(true, false, false, false, false, false),
            "on the software floor a still native desktop must let the gate close"
        );
        // Transients are unconditional: a workspace fade and a map animation are a few
        // hundred milliseconds of motion the user just asked for, not a permanent hold,
        // and they must play out on the floor too.
        assert!(scene_animates(true, false, false, false, true, false), "a ws fade plays on the floor");
        assert!(scene_animates(true, false, false, false, false, true), "so does a map animation");
        assert!(
            !scene_animates(false, false, false, false, false, false),
            "nothing drawn, nothing animating, nothing to hold the gate open"
        );
    }

    #[test]
    fn the_themes_reduced_effects_tier_sheds_the_breath_and_keeps_the_transients() {
        // liquid_ui_service computes `is_potato = perf.disable_blur or gpu_mode ==
        // 'software'` and that one flag strips its animation strings before they are
        // emitted. The GPU half was already mirrored; this is the THEME half, which is a
        // single key in a file the compositor already reads, so the third of the ledger's
        // motion kill-switches was never as far away as it looked.
        assert!(
            scene_animates(true, true, false, false, false, false),
            "a capable GPU on an ordinary theme: the orb breathes"
        );
        assert!(
            !scene_animates(true, true, true, false, false, false),
            "the theme asked for the reduced-effects tier"
        );
        // It sheds exactly what the hardware floor sheds and no more, which is rule 5's
        // "degrade gracefully, never gut": the perpetual breath goes, the brief
        // transients stay. Only a stated preference stops those.
        assert!(
            scene_animates(true, true, true, false, true, false),
            "a workspace fade still plays on the potato tier"
        );
        assert!(
            !scene_animates(true, true, true, true, true, true),
            "but reduced motion still outranks everything"
        );
        // The two halves are independent: either one alone is enough.
        assert!(!scene_animates(true, false, false, false, false, false));
        assert!(!scene_animates(true, false, true, false, false, false));
    }

    #[test]
    fn the_potato_flag_is_the_key_the_shell_actually_reads() {
        // `disable_blur`, not its sibling `disable_animations`. Only potato.json sets
        // either, and NOTHING in the tree reads disable_animations, so honouring that one
        // natively would invent a behaviour the shell does not have.
        let dir = std::env::temp_dir().join("hart_potato_test");
        std::fs::create_dir_all(&dir).unwrap();
        let spud = dir.join("potato.json");
        std::fs::write(
            &spud,
            r#"{"performance":{"disable_blur":true,"disable_animations":true}}"#,
        )
        .unwrap();
        let f = crate::bloom::SettingsFile::load(&spud);
        assert_eq!(f.flag("disable_blur"), Some(true));

        let rich = dir.join("aura.json");
        std::fs::write(&rich, r#"{"performance":{"lazy_load_iframes":true}}"#).unwrap();
        assert_eq!(
            crate::bloom::SettingsFile::load(&rich).flag("disable_blur"),
            None,
            "a theme that says nothing is not asking for the tier"
        );
    }

    #[test]
    fn declared_reduced_motion_stops_the_desktop_moving_at_all() {
        // The CSS parity ledger's rule 4: the shell has THREE independent motion
        // kill-switches and all three must exist natively. The native scene honoured only
        // the GPU floor, so a user who had declared reduced motion would still have got a
        // breathing orb the moment the shell went native.
        //
        // It is not a performance floor and is not overridden by one. On the fastest GPU
        // in the fleet, with the scene drawn, reduced motion still means still.
        assert!(
            scene_animates(true, true, false, false, false, false),
            "GPU, not reduced: the orb breathes"
        );
        assert!(
            !scene_animates(true, true, false, true, false, false),
            "reduced motion wins over a perfectly capable GPU"
        );
        // And it wins over the TRANSIENTS too, which is the difference between this and
        // the hardware floor: a workspace fade the user asked not to see is exactly what
        // the preference exists to stop, where a slow CPU is a reason to skip the breath
        // and still show the fade.
        assert!(scene_animates(true, false, false, false, true, false), "ws fade on the CPU floor");
        assert!(
            !scene_animates(true, true, false, true, true, true),
            "nothing animates when the user said stop"
        );
    }

    #[test]
    fn the_reduced_motion_flag_is_read_from_the_file_the_shell_reads() {
        // shell_os_apis.py seeds _A11Y_SETTINGS from /etc/hart/accessibility.json at
        // import; this reads the same key out of the same shape. Absent file, absent key
        // and a non-boolean all mean "not declared", which is what the shell defaults to.
        let dir = std::env::temp_dir().join("hart_a11y_test");
        std::fs::create_dir_all(&dir).unwrap();

        let on = dir.join("on.json");
        std::fs::write(&on, r#"{"font_scale":1.0,"reduced_motion":true}"#).unwrap();
        assert_eq!(
            crate::bloom::SettingsFile::load(&on).flag("reduced_motion"),
            Some(true)
        );
        let off = dir.join("off.json");
        std::fs::write(&off, r#"{"reduced_motion":false,"high_contrast":true}"#).unwrap();
        assert_eq!(
            crate::bloom::SettingsFile::load(&off).flag("reduced_motion"),
            Some(false)
        );
        assert_eq!(
            crate::bloom::SettingsFile::load(&off).flag("high_contrast"),
            Some(true),
            "the reader is not special-cased to one key"
        );
        // Missing key, missing file, and a value that is not a bool: all None, so the
        // caller keeps the shipped default rather than guessing.
        let bare = dir.join("bare.json");
        std::fs::write(&bare, r#"{"font_scale":1.25,"reduced_motion":"yes"}"#).unwrap();
        assert_eq!(
            crate::bloom::SettingsFile::load(&bare).flag("reduced_motion"),
            None,
            "a string is not a JSON bool"
        );
        assert_eq!(
            crate::bloom::SettingsFile::load(&bare).flag("large_cursor"),
            None
        );
        assert_eq!(
            crate::bloom::SettingsFile::load(std::path::Path::new("/definitely/not/here.json"))
                .flag("reduced_motion"),
            None
        );
    }

    #[test]
    fn an_orb_with_motion_off_rests_rather_than_freezing_mid_breath() {
        // `animation: none` is not `animation-play-state: paused`. The shell's software
        // floor never STARTS the breathing, so the orb sits at its resting scale; freezing
        // it wherever the last painted frame caught it would leave a random half-inflated
        // orb on screen for the whole session. Same motion function either way, so there
        // is no second resting-state constant to drift.
        let mut cache = OrbCache::default();
        let rest = cache
            .current(64, 0.0, false)
            .map(|(_, m)| m)
            .expect("a real size composes");
        assert_eq!(rest.scale, crate::orb::motion_at(std::time::Duration::ZERO, 0.0).scale);
        // Energy still reads through with motion off: a speaking orb is brighter even
        // when it does not breathe, which is the shell's behaviour too (the canvas viz
        // reacts on both floors; only the CSS float/breathe is GPU-gated).
        let hot = cache.current(64, 1.0, false).map(|(_, m)| m).expect("composed");
        assert!(hot.alpha > rest.alpha, "energy lifts the orb without motion");
        assert_eq!(hot.scale, rest.scale, "but it does not inflate it");
    }

    #[test]
    fn a_steady_desktop_keeps_its_element_identities_so_damage_tracking_works() {
        // Damage-tracked redraw is a binding NFR and it rests on something no other test
        // here checks. The compositor decides what changed by comparing each element's ID
        // and commit counter against the previous frame. If the lowering handed back fresh
        // identities every frame, every frame would be FULLY damaged, a static desktop
        // would repaint end to end at 60Hz, and every count-based assertion in this file
        // would still pass while it happened. Element identity comes from the underlying
        // buffer, so this is what the retained tree, the solid pool and the compose-once
        // caches actually buy at the damage level, as opposed to the allocation level.
        let mut renderer = PixmanRenderer::new().expect("pixman renderer allocates headless");
        let size: Size<i32, Physical> = (1280, 800).into();
        let home = crate::scene::HomeCompose::demo();
        let mut rasterizer = crate::text_render::TextRasterizer::new();
        let mut orb = OrbCache::default();
        let mut rects = RectCache::default();
        let mut scenes = crate::scene::SceneCache::default();

        let idents = |els: &Vec<HartRenderElement<PixmanRenderer>>| {
            els.iter()
                .map(|e| (e.id().clone(), e.current_commit()))
                .collect::<Vec<_>>()
        };

        let mut first: Vec<HartRenderElement<PixmanRenderer>> = Vec::new();
        lower_scene(
            &home, size, &mut renderer, &mut rasterizer, &mut orb, &mut rects, &mut scenes,
            0.5, None, false, true, &crate::scene::RowScroll::default(), &mut first,
        );
        let a = idents(&first);
        assert!(!a.is_empty(), "the demo scene lowered to nothing");
        drop(first);

        let mut second: Vec<HartRenderElement<PixmanRenderer>> = Vec::new();
        lower_scene(
            &home, size, &mut renderer, &mut rasterizer, &mut orb, &mut rects, &mut scenes,
            0.5, None, false, true, &crate::scene::RowScroll::default(), &mut second,
        );
        assert_eq!(
            idents(&second),
            a,
            "an unchanged desktop must present the SAME element identities, or the \
             compositor sees a whole new frame and damages everything"
        );
    }

    #[test]
    fn hovering_a_card_recolours_it_without_changing_the_frame_shape() {
        // The card slice of M2 input. A highlight is a different COLOUR for a rect the
        // scene already draws, so the lowered frame must carry exactly the same elements,
        // in the same order, at the same geometry, hovered or not. That invariant is what
        // makes hover free: no relayout, no extra element, no damage beyond the card.
        let mut renderer = PixmanRenderer::new().expect("pixman renderer allocates headless");
        let size: Size<i32, Physical> = (1280, 800).into();
        let home = crate::scene::HomeCompose::demo();
        let theme = crate::scene::Theme::cosmic_default();

        // The centre of the first card, read from the same layout the lowering walks. It
        // must be measured by the SAME rasterizer the lowering hands to tree_for: a
        // different measure could place the cards elsewhere, the hover point would miss,
        // and the test would pass by comparing two UNhovered frames.
        let mut rasterizer = crate::text_render::TextRasterizer::new();
        let tree = crate::scene::layout_home(
            size.w as f32,
            size.h as f32,
            &home,
            &theme,
            &crate::scene::RowScroll::default(),
            &mut rasterizer,
        );
        // Depth-agnostic: cards sit inside their ROW's group now, which is what
        // `.hh-row` is in the shell, so a walk that only looked at root's children
        // found none. A test that pins tree depth fails on every regrouping without
        // anything actually being wrong.
        fn first_card_centre(node: &crate::scene::SceneNode) -> Option<(f32, f32)> {
            if let crate::scene::SceneNode::Container {
                rect,
                interactive,
                children,
                ..
            } = node
            {
                if *interactive {
                    return Some((rect.x + rect.w * 0.5, rect.y + rect.h * 0.5));
                }
                for c in children {
                    if let Some(found) = first_card_centre(c) {
                        return Some(found);
                    }
                }
            }
            None
        }
        let card = first_card_centre(&tree);
        let centre = card.expect("the demo home lays out cards");
        // The hover point must actually LAND on that card, or the two lowerings below
        // would both be unhovered and compare equal for the wrong reason.
        assert!(
            tree.hover_leaf(Some(centre)).is_some(),
            "the chosen point must be a real hover target"
        );

        let mut orb = OrbCache::default();
        let mut rects = RectCache::default();
        let mut scenes = crate::scene::SceneCache::default();
        let geo = |els: &Vec<HartRenderElement<PixmanRenderer>>| -> Vec<(i32, i32, i32, i32)> {
            els.iter()
                .map(|e| {
                    let g = e.geometry(Scale::from(1.0));
                    (g.loc.x, g.loc.y, g.size.w, g.size.h)
                })
                .collect()
        };

        let mut plain: Vec<HartRenderElement<PixmanRenderer>> = Vec::new();
        lower_scene(
            &home,
            size,
            &mut renderer,
            &mut rasterizer,
            &mut orb,
            &mut rects,
            &mut scenes,
            0.5,
            None,
            false,
            true,
            &crate::scene::RowScroll::default(),
            &mut plain,
        );
        let plain_geo = geo(&plain);
        let rebuilds = scenes.rebuilds();
        let solids = rects.solid_allocs();
        assert!(!plain_geo.is_empty(), "the demo scene lowered to nothing");
        // Drop before re-lowering, exactly as the frame loop does, so the pooled buffers
        // are free to be handed out again.
        drop(plain);

        let mut hovered: Vec<HartRenderElement<PixmanRenderer>> = Vec::new();
        lower_scene(
            &home,
            size,
            &mut renderer,
            &mut rasterizer,
            &mut orb,
            &mut rects,
            &mut scenes,
            0.5,
            Some(centre),
            false,
            true,
            &crate::scene::RowScroll::default(),
            &mut hovered,
        );

        assert_eq!(
            geo(&hovered),
            plain_geo,
            "a hover must not move, add or drop a single element"
        );
        assert_eq!(
            scenes.rebuilds(),
            rebuilds,
            "hover must not rebuild the retained tree"
        );
        assert_eq!(
            rects.solid_allocs(),
            solids,
            "a card highlight is a rounded rect, so it must not touch the solid pool"
        );
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
        let mut scenes = crate::scene::SceneCache::default();
        let mut elements: Vec<HartRenderElement<PixmanRenderer>> = Vec::new();
        lower_scene(
            &home,
            size,
            &mut renderer,
            &mut rasterizer,
            &mut orb,
            &mut rects,
            &mut scenes,
            0.5,
            None,
            false,
            true,
            &crate::scene::RowScroll::default(),
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

        // STRUCTURE, not just coverage. A fraction-of-the-frame count says something was
        // drawn; it does not say the desktop has a top bar and a taskbar. Both strips are
        // full-width rects, so every pixel of both must be off the sentinel, and a hole in
        // either is the flicker class the DRM path already fights showing up in lowering
        // instead. This is the assertion that would have caught the demo drawing blank
        // tiles, which coverage alone happily passed.
        let (w, h) = (size.w as usize, size.h as usize);
        let is_sentinel =
            |px: &[u8]| px[0] > 250 && px[1] < 5 && px[2] > 250;
        let row_sentinels = |y: usize| -> usize {
            (0..w)
                .filter(|x| {
                    let i = (y * w + x) * 4;
                    is_sentinel(&bytes[i..i + 4])
                })
                .count()
        };
        for y in 0..crate::scene::TOP_BAR_H as usize {
            assert_eq!(
                row_sentinels(y),
                0,
                "row {y} of the top bar left {} px unpainted",
                row_sentinels(y)
            );
        }
        for y in (h - crate::scene::TASKBAR_H as usize)..h {
            assert_eq!(
                row_sentinels(y),
                0,
                "row {y} of the taskbar left {} px unpainted",
                row_sentinels(y)
            );
        }
        // And the band between them is not empty: the hero, the orb and the cards live
        // there, so a desktop that painted only its two bars is not a desktop.
        let mid = (crate::scene::TOP_BAR_H as usize..h - crate::scene::TASKBAR_H as usize)
            .map(|y| w - row_sentinels(y))
            .sum::<usize>();
        assert!(
            mid > w,
            "the content band painted only {mid} px, so nothing but the bars drew"
        );
    }
}
