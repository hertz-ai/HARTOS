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
