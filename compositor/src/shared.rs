// ════════════════════════════════════════════════════════════════════════════
// HART-comp — backend-AGNOSTIC Smithay glue shared by BOTH the winit (dev/WSL) and
// the DRM/udev (real-hardware) backends.
//                                          ⚠️  CI-COMPILE (winit OR smithay only)  ⚠️
// ════════════════════════════════════════════════════════════════════════════
//
// ── Why this module exists (the M7 Stage-B "hoist", DRY gate) ──
//   Through M6, these helpers lived in `winit.rs` behind `#[cfg(feature="winit")]`.
//   M7 adds the DRM backend (`udev.rs`, `#[cfg(feature="smithay")]`) which needs the
//   EXACT SAME surface-tree / map-edge / app-id reading logic. Copying them into
//   `udev.rs`/`wayland.rs` would be a parallel path that drifts (CLAUDE.md Gate 4).
//   Instead the truly backend-agnostic pieces are hoisted HERE and gated to
//   `any(feature="winit", feature="smithay")` so ONE implementation feeds both
//   backends — exactly the convergence the M1 winit.rs header promised ("factor
//   State construction so the renderer type is the backend's").
//
//   What stays backend-SPECIFIC (NOT here): the `State` struct (its renderer type
//   differs — GlesRenderer for winit, PixmanRenderer for DRM), the input routing
//   (winit `WinitEvent` vs libinput `InputEvent`), and the render-frame submission
//   (winit `backend.submit` vs DRM `compositor.queue_frame`). Those live in their
//   own backend module. Only the protocol-surface helpers that touch neither the
//   renderer concretely nor the backend transport are shared here.

#![cfg(any(feature = "winit", feature = "smithay"))]

use smithay::reexports::wayland_server::protocol::wl_surface::WlSurface;
use smithay::wayland::compositor::with_states;
use smithay::wayland::shell::xdg::{ToplevelSurface, XdgToplevelSurfaceData};
use smithay::xwayland::X11Surface;

/// Drain a surface tree's frame callbacks (mirrors minimal.rs's helper). Without
/// this, well-behaved clients (which wait for the frame callback before drawing the
/// next frame) freeze after the first frame. Backend-agnostic: it only walks the
/// Wayland surface tree, no renderer/backend involved — so BOTH winit and DRM call it.
pub(crate) fn send_frame_callbacks(surface: &WlSurface, time: u32) {
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

/// Has `surface` actually got a committed buffer latched (i.e. is it MAPPED)? This is
/// the post-`on_commit_buffer_handler` truth: the renderer surface state holds the
/// latched buffer only once the client attached + committed one. Used as the map-edge
/// gate (no handle is minted until this is true) on BOTH backends. The
/// `RendererSurfaceStateUserData` is populated by `on_commit_buffer_handler`, which
/// both backends call in their `CompositorHandler::commit`, so this read is identical.
pub(crate) fn surface_has_buffer(surface: &WlSurface) -> bool {
    use smithay::backend::renderer::utils::RendererSurfaceStateUserData;
    with_states(surface, |states| {
        states
            .data_map
            .get::<RendererSurfaceStateUserData>()
            .map(|d| d.lock().unwrap().buffer().is_some())
            .unwrap_or(false)
    })
}

/// Read a toplevel's `app_id` (the join key the brain's launcher tags). Backend-
/// agnostic xdg-shell surface-state read.
pub(crate) fn toplevel_app_id(toplevel: &ToplevelSurface) -> Option<String> {
    with_states(toplevel.wl_surface(), |states| {
        states
            .data_map
            .get::<XdgToplevelSurfaceData>()
            .and_then(|d| d.lock().unwrap().app_id.clone())
    })
}

/// Read a toplevel's `title`.
pub(crate) fn toplevel_title(toplevel: &ToplevelSurface) -> Option<String> {
    with_states(toplevel.wl_surface(), |states| {
        states
            .data_map
            .get::<XdgToplevelSurfaceData>()
            .and_then(|d| d.lock().unwrap().title.clone())
    })
}

/// X11 WM_CLASS → app_id analogue (the join key the brain's launcher tags).
pub(crate) fn x11_app_id(x11: &X11Surface) -> Option<String> {
    Some(x11.class()).filter(|s| !s.is_empty())
}

/// X11 window title.
pub(crate) fn x11_title(x11: &X11Surface) -> Option<String> {
    Some(x11.title()).filter(|s| !s.is_empty())
}

// ── KEYBOARD LAYOUT: read XKB_DEFAULT_* OURSELVES (real HW, 2026-08-12) ──────
//
// `XkbConfig::default()` looks like it defers to libxkbcommon's environment
// defaults. IT DOES NOT, and the failure is completely silent.
//
// libxkbcommon fills a field from XKB_DEFAULT_* only when the pointer is NULL:
//     if (isempty(rmlvo->rules))  rmlvo->rules = default_rules;   /* NULL or "" */
//     if (!rmlvo->options)        rmlvo->options = default_options; /* NULL ONLY */
// The Rust binding turns `options: None` into an empty CString, which is a
// NON-NULL pointer to "\0" -- so libxkbcommon reads it as "the caller explicitly
// asked for NO options" and never consults the environment.
//
// Measured on the box, three ways:
//   * `XKB_DEFAULT_OPTIONS=numpad:mac` present in the compositor's
//     /proc/PID/environ, yet the keymap it serves clients (read straight out of
//     /memfd:smithay-keymap) still carried the stock type:
//         type "KEYPAD" { modifiers= Shift+NumLock; map[NumLock]= 2; ... }
//   * compiling the SAME empty-RMLVO names by hand reproduced it exactly:
//         --options ''  -> modifiers= Shift+NumLock   (env ignored)
//         (no --options) -> modifiers= none           (env applied)
//   * setting XKB_DEFAULT_RULES to a custom ruleset changed nothing either, so
//     it is not specific to `options`.
//
// The user-visible symptom was the whole numeric keypad typing navigation
// (KP_End, KP_Down, …) instead of digits, because NumLock boots off and nothing
// turns it on. `numpad:mac` fixes that properly by making the keypad key TYPE
// stop consulting the NumLock modifier at all:
//     type "KEYPAD" { modifiers= none; map[none]= 2; }
// so the digit level is unconditional. The symbol lists on the keys are
// untouched, and the NumLock key itself is unchanged (diffed, no regression).
//
// So: plumb the environment through EXPLICITLY. This keeps the seam the session
// supervisor already exports (one declaration, inherited by hart-comp, sway and
// cage) genuinely working for Tier-1 instead of silently dropped, and defaults
// to numpad:mac when the operator sets nothing. Owned strings because
// `XkbConfig<'a>` borrows; `config()` hands out a borrow that lives as long as
// the holder.
pub(crate) struct XkbEnv {
    rules: String,
    model: String,
    layout: String,
    variant: String,
    options: Option<String>,
}

impl XkbEnv {
    /// Read the standard XKB_DEFAULT_* variables, defaulting the OPTIONS to
    /// `numpad:mac` so a keypad types digits out of the box. An empty value is
    /// treated as unset for `options` (matching libxkbcommon's own `isempty`
    /// handling of the other four fields); set `XKB_DEFAULT_OPTIONS` to any
    /// other value to override, exactly as on a normal desktop.
    pub(crate) fn from_env() -> Self {
        let var = |k: &str| std::env::var(k).unwrap_or_default();
        Self {
            rules: var("XKB_DEFAULT_RULES"),
            model: var("XKB_DEFAULT_MODEL"),
            layout: var("XKB_DEFAULT_LAYOUT"),
            variant: var("XKB_DEFAULT_VARIANT"),
            options: Some(
                std::env::var("XKB_DEFAULT_OPTIONS")
                    .ok()
                    .filter(|s| !s.is_empty())
                    .unwrap_or_else(|| "numpad:mac".to_string()),
            ),
        }
    }

    pub(crate) fn config(&self) -> smithay::input::keyboard::XkbConfig<'_> {
        smithay::input::keyboard::XkbConfig {
            rules: &self.rules,
            model: &self.model,
            layout: &self.layout,
            variant: &self.variant,
            options: self.options.clone(),
        }
    }
}
