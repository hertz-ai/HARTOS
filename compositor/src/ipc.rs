// ════════════════════════════════════════════════════════════════════════════
// HART-comp — Milestone 4: the `com.hart.Compositor` IPC server, wired to the
// REAL winit `Space<Window>` so an agent arranges real native windows.
// ════════════════════════════════════════════════════════════════════════════
//
// This is the FIRST actually-running `com.hart.Compositor` server. Phase 6 / task
// #12 shipped only the CONTRACT (../IPC_PROTOCOL.md) + the brain-side Python
// `HartWmClient` (which shells out to `swaymsg` — sway Tier-2, NOT a client of
// HART-comp). NOTHING here ran against the live compositor's window tree. M4 wires
// the Unix-socket transport (IPC_PROTOCOL.md §2 "Unix-socket twin") into the winit
// calloop loop and implements the command handlers AGAINST `state.space`.
//
// ── Why the Unix-socket twin first (NOT D-Bus) ──
//   IPC_PROTOCOL.md §2 specifies two transports exposing the SAME surface: D-Bus
//   (`com.hart.Compositor` on the session bus) and a Unix-socket twin
//   (`$XDG_RUNTIME_DIR/hart-comp.sock`, 0600, length-prefixed JSON). The socket
//   twin drops straight into the existing calloop loop as a `Generic<UnixListener>`
//   source — no async runtime, no zbus, no second event loop. `serde` is already in
//   the dep tree (smithay pulls it); only `serde_json` is added as a direct dep.
//   D-Bus can be added later or proxied by Python over this socket. The brain
//   reaches it via the EXISTING `HartWmClient` singleton — swap its swaymsg shim for
//   a socket client speaking the SAME framed JSON, same `dispatch_verb` surface.
//
// ── Framing (IPC_PROTOCOL.md §2/§3) ──
//   4-byte big-endian uint32 length, then a UTF-8 JSON object. One request → one
//   response; events are unsolicited JSON frames on a subscription. The request /
//   response / event envelopes match §3 + §5.
//
// ── No-phantom-window honesty (IPC_PROTOCOL.md §1.4/§9.2) ──
//   `window.list` enumerates ONLY the `space.elements()` that actually mapped (a
//   handle exists only because `winit.rs::on_real_map` minted it on a real buffer
//   commit). Every mutating verb resolves its `handle` against a live mapped window
//   or returns `not_found` — never a fabricated success.
//
// ── Security boundary (IPC_PROTOCOL.md §6) ──
//   The full constitutional gate (HiveCircuitBreaker + GuardrailEnforcer + per-agent
//   rate cap + immutable audit + PREVIEW for destructive geometry) lives BRAIN-SIDE
//   in `integrations/agent_engine/hart_wm_client.py::_guard_destructive` — the same
//   fail-closed gate every verb already passes there. The compositor is the
//   privileged executor the gated brain drives; it re-checks nothing the brain
//   already proved on THIS rev (the socket is 0600, owner = the session user, so the
//   only writer is the same trust domain as the brain). When the D-Bus transport +
//   server-side re-check land (IPC_PROTOCOL.md §6.2 "re-checked server-side"), they
//   hang off `dispatch_request` below. Until then the socket-permission boundary
//   (§6.5) is the server-side control, and the brain's gate is authoritative.

#![cfg(feature = "winit")]

use std::io::{ErrorKind, Read, Write};
use std::os::unix::net::{UnixListener, UnixStream};
use std::path::PathBuf;

use serde::{Deserialize, Serialize};
use serde_json::{json, Value};
use smithay::reexports::calloop::{
    generic::Generic, Interest, LoopHandle, Mode, PostAction,
};
use smithay::utils::SERIAL_COUNTER;
// `Window::wl_surface()` / `KeyboardFocus::wl_surface()` come from the `WaylandFocus`
// trait on this Smithay rev (not inherent methods) — it MUST be in scope, exactly as
// winit.rs imports it for `window_for_surface`.
use smithay::wayland::seat::WaylandFocus;
use tracing::{info, warn};

use crate::winit::State;

/// Protocol version (IPC_PROTOCOL.md §3 — the `v` field).
const PROTOCOL_VERSION: u64 = 1;
/// Hard cap on a single framed message so a malformed length prefix cannot make us
/// allocate gigabytes. 1 MiB is far above any real window-op request.
const MAX_FRAME_LEN: u32 = 1024 * 1024;

// ────────────────────────────────────────────────────────────────────────────
// Per-compositor IPC state — held in `State.ipc`. Tracks event subscribers (the
// `events.subscribe` sinks) so map/unmap/focus edges in winit.rs can push
// unsolicited event frames (IPC_PROTOCOL.md §5).
// ────────────────────────────────────────────────────────────────────────────
#[derive(Default)]
pub struct IpcState {
    /// Open subscriber streams that asked for `events.subscribe`. Each gets the
    /// unsolicited event frames (window.opened/closed/focused/...). A dead stream is
    /// dropped on the first failed write.
    subscribers: Vec<UnixStream>,
    /// Monotonic subscription id source (`sub_<n>`).
    next_sub: u64,
}

impl IpcState {
    /// Push an event frame (IPC_PROTOCOL.md §5) to every live subscriber. Drops a
    /// subscriber whose socket has gone away. Pure fan-out; the caller built the
    /// `window` payload from the SAME `WindowRegistry`/`Space` source of truth.
    pub fn emit_event(&mut self, event: &str, window: Value) {
        if self.subscribers.is_empty() {
            return;
        }
        let frame = json!({
            "v": PROTOCOL_VERSION,
            "event": event,
            "window": window,
        });
        let bytes = match serde_json::to_vec(&frame) {
            Ok(b) => b,
            Err(_) => return,
        };
        self.subscribers.retain_mut(|s| write_frame(s, &bytes).is_ok());
    }
}

// ────────────────────────────────────────────────────────────────────────────
// Framed-JSON wire helpers (IPC_PROTOCOL.md §2 framing).
// ────────────────────────────────────────────────────────────────────────────

/// Write one length-prefixed frame (4-byte BE length + body). Small blocking write;
/// the body is a single window-op response, well under a pipe buffer.
fn write_frame(stream: &mut UnixStream, body: &[u8]) -> std::io::Result<()> {
    let len = (body.len() as u32).to_be_bytes();
    stream.write_all(&len)?;
    stream.write_all(body)?;
    stream.flush()
}

/// A single client connection's read buffer + framing cursor. Accumulates bytes a
/// non-blocking read at a time and yields whole frames (one request each). Kept per
/// connection so a request split across two `read()`s reassembles correctly.
struct Connection {
    stream: UnixStream,
    buf: Vec<u8>,
}

impl Connection {
    fn new(stream: UnixStream) -> Self {
        Connection { stream, buf: Vec::with_capacity(4096) }
    }

    /// Drain whatever is readable into `buf`. Returns `Ok(true)` if the peer closed
    /// (EOF), `Ok(false)` if it just drained the currently-available bytes (would
    /// block), or an error for a real failure.
    fn fill(&mut self) -> std::io::Result<bool> {
        let mut chunk = [0u8; 8192];
        loop {
            match self.stream.read(&mut chunk) {
                Ok(0) => return Ok(true), // EOF — peer closed
                Ok(n) => self.buf.extend_from_slice(&chunk[..n]),
                Err(e) if e.kind() == ErrorKind::WouldBlock => return Ok(false),
                Err(e) if e.kind() == ErrorKind::Interrupted => continue,
                Err(e) => return Err(e),
            }
        }
    }

    /// Pop the next complete frame body (the JSON bytes) from `buf`, if one is fully
    /// buffered. Returns `None` when more bytes are needed.
    fn next_frame(&mut self) -> Option<Vec<u8>> {
        if self.buf.len() < 4 {
            return None;
        }
        let len = u32::from_be_bytes([self.buf[0], self.buf[1], self.buf[2], self.buf[3]]);
        if len > MAX_FRAME_LEN {
            // Poison frame — drop the whole buffer so we resync rather than allocate.
            warn!(len, "IPC frame exceeds MAX_FRAME_LEN; dropping connection buffer");
            self.buf.clear();
            return None;
        }
        let total = 4 + len as usize;
        if self.buf.len() < total {
            return None;
        }
        let body = self.buf[4..total].to_vec();
        self.buf.drain(..total);
        Some(body)
    }
}

// ────────────────────────────────────────────────────────────────────────────
// Request / response envelope (IPC_PROTOCOL.md §3).
// ────────────────────────────────────────────────────────────────────────────
#[derive(Deserialize)]
struct Request {
    #[serde(default)]
    id: Option<String>,
    method: String,
    #[serde(default)]
    args: Value,
}

#[derive(Serialize)]
struct Response {
    v: u64,
    id: Option<String>,
    ok: bool,
    result: Value,
    error: Option<ErrorBody>,
}

#[derive(Serialize)]
struct ErrorBody {
    code: String,
    message: String,
}

impl Response {
    fn ok(id: Option<String>, result: Value) -> Self {
        Response { v: PROTOCOL_VERSION, id, ok: true, result, error: None }
    }
    fn err(id: Option<String>, code: &str, message: impl Into<String>) -> Self {
        Response {
            v: PROTOCOL_VERSION,
            id,
            ok: false,
            result: Value::Null,
            error: Some(ErrorBody { code: code.into(), message: message.into() }),
        }
    }
}

// ────────────────────────────────────────────────────────────────────────────
// The socket path + the calloop wiring (called from winit.rs::run_winit).
// ────────────────────────────────────────────────────────────────────────────

/// `$XDG_RUNTIME_DIR/hart-comp.sock` (IPC_PROTOCOL.md §2). Falls back to `/tmp` only
/// if XDG_RUNTIME_DIR is unset (a misconfigured session) so the server still binds.
pub fn socket_path() -> PathBuf {
    let dir = std::env::var_os("XDG_RUNTIME_DIR")
        .map(PathBuf::from)
        .unwrap_or_else(|| PathBuf::from("/tmp"));
    dir.join("hart-comp.sock")
}

/// Bind the IPC listening socket + insert it into the calloop loop. The listener
/// source ACCEPTS connections; each accepted stream is registered as its OWN calloop
/// `Generic` source (so many clients are served without blocking the compositor).
/// Best-effort: a bind failure logs + leaves the compositor running for everything
/// else (the IPC is an add-on, never a boot gate — same posture as XWayland).
pub fn start_ipc(loop_handle: &LoopHandle<'static, State>) -> Option<PathBuf> {
    let path = socket_path();
    // Remove a stale socket from a previous run (the bind fails on AddrInUse otherwise).
    let _ = std::fs::remove_file(&path);

    let listener = match UnixListener::bind(&path) {
        Ok(l) => l,
        Err(err) => {
            warn!(?err, path = %path.display(), "IPC: failed to bind hart-comp.sock (window IPC unavailable)");
            return None;
        }
    };
    // 0600, owner = the session user (IPC_PROTOCOL.md §6.5 socket boundary).
    if let Err(err) = set_socket_mode_0600(&path) {
        warn!(?err, "IPC: could not chmod 0600 the socket (continuing)");
    }
    if let Err(err) = listener.set_nonblocking(true) {
        warn!(?err, "IPC: could not set the listener non-blocking");
    }

    let source = Generic::new(listener, Interest::READ, Mode::Level);
    let inserted = loop_handle.insert_source(source, |_readiness, listener, state| {
        // Accept every pending connection (Level-triggered → drain the backlog).
        loop {
            match listener.accept() {
                Ok((stream, _addr)) => {
                    if let Err(err) = stream.set_nonblocking(true) {
                        warn!(?err, "IPC: could not set an accepted stream non-blocking");
                    }
                    register_connection(state, stream);
                }
                Err(e) if e.kind() == ErrorKind::WouldBlock => break,
                Err(e) if e.kind() == ErrorKind::Interrupted => continue,
                Err(err) => {
                    warn!(?err, "IPC: accept failed");
                    break;
                }
            }
        }
        Ok(PostAction::Continue)
    });
    if let Err(err) = inserted {
        warn!(?err, "IPC: failed to insert the listener source into the event loop");
        return None;
    }
    info!(path = %path.display(), "HART-comp IPC listening (com.hart.Compositor Unix-socket twin)");
    Some(path)
}

/// chmod 0600 the socket (owner-only). Pure libc via std — no extra crate.
fn set_socket_mode_0600(path: &std::path::Path) -> std::io::Result<()> {
    use std::os::unix::fs::PermissionsExt;
    let perms = std::fs::Permissions::from_mode(0o600);
    std::fs::set_permissions(path, perms)
}

/// Register one accepted client stream as its own calloop `Generic` source. The
/// source reads framed requests, dispatches each against `&mut State` (the live
/// window tree), and writes the framed response. On EOF/error it removes itself.
fn register_connection(state: &mut State, stream: UnixStream) {
    // The Generic source needs an `AsFd` — clone the stream for the source's fd, and
    // move an owning Connection into the closure (the clone + the Connection share
    // the same underlying socket via dup, so reads/writes hit the same peer).
    let fd_stream = match stream.try_clone() {
        Ok(s) => s,
        Err(err) => {
            warn!(?err, "IPC: could not clone an accepted stream; dropping it");
            return;
        }
    };
    let mut conn = Connection::new(stream);
    let source = Generic::new(fd_stream, Interest::READ, Mode::Level);
    let inserted = state.loop_handle.insert_source(source, move |_readiness, _fd, state| {
        let closed = match conn.fill() {
            Ok(c) => c,
            Err(err) => {
                warn!(?err, "IPC: connection read error; closing");
                return Ok(PostAction::Remove);
            }
        };
        // Dispatch every fully-buffered request, writing one response each.
        while let Some(body) = conn.next_frame() {
            let resp = handle_frame(state, &body, &mut conn.stream);
            let bytes = serde_json::to_vec(&resp).unwrap_or_else(|_| b"{}".to_vec());
            if let Err(err) = write_frame(&mut conn.stream, &bytes) {
                warn!(?err, "IPC: failed to write response; closing connection");
                return Ok(PostAction::Remove);
            }
        }
        if closed {
            return Ok(PostAction::Remove);
        }
        Ok(PostAction::Continue)
    });
    if let Err(err) = inserted {
        warn!(?err, "IPC: failed to register a client connection source");
    }
}

/// Parse + dispatch a single request frame, returning the response. `stream` is the
/// connection's own stream (passed so `events.subscribe` can clone it into the
/// subscriber set). Parse failures return a structured `invalid_args` error rather
/// than dropping the connection.
fn handle_frame(state: &mut State, body: &[u8], stream: &mut UnixStream) -> Response {
    let req: Request = match serde_json::from_slice(body) {
        Ok(r) => r,
        Err(err) => return Response::err(None, "invalid_args", format!("malformed request: {err}")),
    };
    let id = req.id.clone();
    dispatch_request(state, &req.method, &req.args, id, stream)
}

// ────────────────────────────────────────────────────────────────────────────
// THE command surface — every verb runs against the REAL `state.space`
// (IPC_PROTOCOL.md §4). Geometry is logical pixels on the single winit output.
// ────────────────────────────────────────────────────────────────────────────

fn dispatch_request(
    state: &mut State,
    method: &str,
    args: &Value,
    id: Option<String>,
    stream: &mut UnixStream,
) -> Response {
    match method {
        // ── §4.1 window.list — read-only enumeration of mapped toplevels ──
        "window.list" | "ListWindows" => {
            Response::ok(id, json!({ "windows": state.ipc_list_windows() }))
        }

        // ── §4.2 window.focus(handle) — keyboard focus + raise ──
        "window.focus" | "FocusWindow" => match arg_handle(args) {
            Some(h) => {
                if state.ipc_focus_window(&h) {
                    Response::ok(id, json!({ "handle": h, "focused": true }))
                } else {
                    Response::err(id, "not_found", format!("no mapped window for handle {h}"))
                }
            }
            None => Response::err(id, "invalid_args", "window.focus needs args.handle"),
        },

        // ── §4.4 window.place(handle, target{x,y,w,h}|{zone}) — move (+resize) ──
        "window.place" | "PlaceWindow" => match arg_handle(args) {
            Some(h) => match resolve_target(state, args.get("target")) {
                Some((x, y, w, h_sz)) => {
                    if state.ipc_place_window(&h, x, y, w, h_sz) {
                        Response::ok(id, json!({
                            "handle": h,
                            "geometry": { "x": x, "y": y, "w": w, "h": h_sz }
                        }))
                    } else {
                        Response::err(id, "not_found", format!("no mapped window for handle {h}"))
                    }
                }
                None => Response::err(id, "invalid_args", "window.place needs target {x,y,w,h} or {zone}"),
            },
            None => Response::err(id, "invalid_args", "window.place needs args.handle"),
        },

        // ── move(handle,x,y) — a plain reposition (no resize). Not in the spec's
        //    numbered list as its own verb (place covers it) but the milestone names
        //    `move(id,x,y)` explicitly, so it is wired as a first-class verb too. ──
        "window.move" | "MoveWindow" => match (arg_handle(args), arg_i32(args, "x"), arg_i32(args, "y")) {
            (Some(h), Some(x), Some(y)) => {
                if state.ipc_move_window(&h, x, y) {
                    let geo = state.ipc_window_geometry(&h).unwrap_or((x, y, 0, 0));
                    Response::ok(id, json!({
                        "handle": h,
                        "geometry": { "x": geo.0, "y": geo.1, "w": geo.2, "h": geo.3 }
                    }))
                } else {
                    Response::err(id, "not_found", format!("no mapped window for handle {h}"))
                }
            }
            _ => Response::err(id, "invalid_args", "window.move needs args.handle, args.x, args.y"),
        },

        // ── resize(handle,w,h) — the milestone's resize verb (xdg configure / X11
        //    configure). Keeps the window's current location, changes its size. ──
        "window.resize" | "ResizeWindow" => match (arg_handle(args), arg_i32(args, "w"), arg_i32(args, "h")) {
            (Some(h), Some(w), Some(hh)) => {
                if state.ipc_resize_window(&h, w, hh) {
                    let geo = state.ipc_window_geometry(&h).unwrap_or((0, 0, w, hh));
                    Response::ok(id, json!({
                        "handle": h,
                        "geometry": { "x": geo.0, "y": geo.1, "w": geo.2, "h": geo.3 }
                    }))
                } else {
                    Response::err(id, "not_found", format!("no mapped window for handle {h}"))
                }
            }
            _ => Response::err(id, "invalid_args", "window.resize needs args.handle, args.w, args.h"),
        },

        // ── §4.5 window.tile(layout) — arrange ALL mapped toplevels. ──
        "window.tile" | "TileLayout" | "window.arrange" => {
            let layout = args.get("layout").and_then(Value::as_str).unwrap_or("grid").to_string();
            let arranged = state.ipc_tile(&layout);
            Response::ok(id, json!({ "layout": layout, "arranged": arranged }))
        }

        // ── §4.3 window.close(handle) — send_close (xdg) / set_mapped(false) (X11) ──
        "window.close" | "CloseWindow" => match arg_handle(args) {
            Some(h) => {
                if state.ipc_close_window(&h) {
                    Response::ok(id, json!({ "handle": h, "closed": true }))
                } else {
                    Response::err(id, "not_found", format!("no mapped window for handle {h}"))
                }
            }
            None => Response::err(id, "invalid_args", "window.close needs args.handle"),
        },

        // ── §4.8 workspace.switch(n) — show workspace N (M5 drives the SAME
        //    State.active_workspace the Super+N chord does). `n` is 0-based internally;
        //    the response echoes the 1-based wire value the caller sent. ──
        "workspace.switch" | "SwitchWorkspace" => match arg_workspace(args) {
            Some(n) => {
                let changed = state.switch_workspace(n);
                Response::ok(id, json!({ "workspace": n + 1, "switched": changed }))
            }
            None => Response::err(id, "invalid_args", "workspace.switch needs args.workspace (>=1)"),
        },

        // ── §4.7 window.move_to_workspace(handle, n) — move a window to workspace N. ──
        "window.move_to_workspace" | "MoveToWorkspace" => {
            match (arg_handle(args), arg_workspace(args)) {
                (Some(h), Some(n)) => {
                    if state.move_window_to_workspace_by_handle(&h, n) {
                        // Echo the 1-based wire value (n is 0-based internally).
                        Response::ok(id, json!({ "handle": h, "workspace": n + 1 }))
                    } else {
                        Response::err(id, "not_found", format!("no mapped window for handle {h}"))
                    }
                }
                _ => Response::err(
                    id,
                    "invalid_args",
                    "window.move_to_workspace needs args.handle + args.workspace (>=1)",
                ),
            }
        }

        // ── §4.10 events.subscribe — register this stream for unsolicited events ──
        "events.subscribe" | "Subscribe" => {
            let events = args.get("events").cloned().unwrap_or_else(|| json!([
                "window.opened", "window.closed", "window.focused"
            ]));
            let sub = state.ipc_add_subscriber(stream);
            Response::ok(id, json!({ "subscription": sub, "events": events }))
        }

        other => Response::err(id, "unsupported", format!("unknown method: {other}")),
    }
}

// ── argument extractors ──
fn arg_handle(args: &Value) -> Option<String> {
    args.get("handle").and_then(Value::as_str).map(str::to_string)
}
fn arg_i32(args: &Value, key: &str) -> Option<i32> {
    args.get(key).and_then(Value::as_i64).map(|v| v as i32)
}

/// The `workspace` arg — the IPC contract numbers workspaces from 1 on the wire
/// (`{"workspace": 2}`, IPC_PROTOCOL.md §4.5/§4.7/§4.8), but the compositor indexes
/// them from 0 internally (the Super+1 chord = `SwitchWorkspace(0)`, `n = keysym -
/// KEY_1`). Convert 1-based wire → 0-based internal here so the two triggers (chord +
/// IPC) drive the SAME `active_workspace` space. Rejects `< 1`.
fn arg_workspace(args: &Value) -> Option<usize> {
    let n = args.get("workspace").and_then(Value::as_i64)?;
    if n < 1 {
        return None;
    }
    Some((n - 1) as usize)
}

/// Resolve a `target` payload to an `(x, y, w, h)` rect — either explicit
/// `{x,y,w,h}` or a named `{zone}` computed over the output geometry (§4.4).
fn resolve_target(state: &State, target: Option<&Value>) -> Option<(i32, i32, i32, i32)> {
    let t = target?;
    if let Some(zone) = t.get("zone").and_then(Value::as_str) {
        return state.ipc_zone_rect(zone);
    }
    let x = t.get("x").and_then(Value::as_i64)? as i32;
    let y = t.get("y").and_then(Value::as_i64)? as i32;
    let w = t.get("w").and_then(Value::as_i64)? as i32;
    let h = t.get("h").and_then(Value::as_i64)? as i32;
    Some((x, y, w, h))
}

// ════════════════════════════════════════════════════════════════════════════
// `State` IPC methods — the bridge from a verb to the live Smithay window tree.
// These live in this module (as an `impl State`) so winit.rs stays focused on the
// protocol handlers; they call ONLY the same `space`/`seat`/`xwm` mutators the M3
// input path already uses (raise_element / map_element / set_focus / configure).
// ════════════════════════════════════════════════════════════════════════════
impl State {
    /// `window.list` (§4.1): one JSON object per mapped toplevel, from the SAME
    /// source of truth M3 paints — `space.elements()` (the VISIBLE active workspace),
    /// PLUS the M5 `hidden_windows` set (windows on non-active workspaces), each joined
    /// with the per-window `WindowHandle` minted on real map. Never lists a phantom.
    ///
    /// Each row carries `workspace` (1-based, the IPC wire convention) + `visible` (true
    /// for the active workspace's windows, false for the held set) so an agent — or the
    /// M5 test — can SEE that e.g. switching to workspace 2 left the workspace-1 windows
    /// real-but-hidden, not destroyed. Geometry for a hidden window is its stored
    /// restore rect (size from the last visible geometry, loc from the held location).
    pub fn ipc_list_windows(&self) -> Vec<Value> {
        use crate::winit::{ipc_window_app_id, ipc_window_title};
        let focused_surface = self.keyboard.current_focus();
        let mut out = Vec::new();
        // Visible windows (the active workspace).
        for window in self.space.elements() {
            let handle = match window.user_data().get::<crate::WindowHandle>() {
                Some(h) => h.as_str().to_string(),
                None => continue, // not yet mapped (no real handle) — honesty rule
            };
            let geo = self.space.element_geometry(window);
            let (x, y, w, h) = geo
                .map(|g| (g.loc.x, g.loc.y, g.size.w, g.size.h))
                .unwrap_or((0, 0, 0, 0));
            let is_x11 = window.x11_surface().is_some();
            let focused = window
                .wl_surface()
                .map(|s| focused_surface.as_ref() == Some(&*s))
                .unwrap_or(false);
            out.push(json!({
                "handle": handle,
                "app_id": ipc_window_app_id(window),
                "title": ipc_window_title(window),
                "geometry": { "x": x, "y": y, "w": w, "h": h },
                "focused": focused,
                "kind": if is_x11 { "x11" } else { "xdg" },
                "mapped": true,
                "workspace": self.active_workspace + 1, // 1-based on the wire
                "visible": true,
            }));
        }
        // Hidden windows (non-active workspaces + show-desktop stash). Real, mapped,
        // just not on the visible output — listed so workspace state is observable.
        for hw in self.ipc_hidden_windows() {
            let window = &hw.window;
            let handle = match window.user_data().get::<crate::WindowHandle>() {
                Some(h) => h.as_str().to_string(),
                None => continue,
            };
            let is_x11 = window.x11_surface().is_some();
            // Size from the last-known geometry; loc from the held restore point.
            let (w, h) = self
                .space
                .element_geometry(window)
                .map(|g| (g.size.w, g.size.h))
                .unwrap_or((0, 0));
            out.push(json!({
                "handle": handle,
                "app_id": ipc_window_app_id(window),
                "title": ipc_window_title(window),
                "geometry": { "x": hw.loc.x, "y": hw.loc.y, "w": w, "h": h },
                "focused": false, // a hidden window cannot hold input focus
                "kind": if is_x11 { "x11" } else { "xdg" },
                "mapped": true,
                "workspace": hw.workspace + 1, // 1-based on the wire
                "visible": false,
            }));
        }
        out
    }

    /// Find the live mapped `Window` whose minted handle is `handle` (mirrors
    /// `window_for_surface`, but keyed on the IPC handle stamped in user_data on map).
    /// `pub(crate)` so the M5 workspace methods in winit.rs can resolve a handle too.
    pub(crate) fn ipc_window_for_handle(&self, handle: &str) -> Option<smithay::desktop::Window> {
        self.space
            .elements()
            .find(|w| {
                w.user_data()
                    .get::<crate::WindowHandle>()
                    .map(|h| h.as_str() == handle)
                    .unwrap_or(false)
            })
            .cloned()
    }

    /// `window.focus` (§4.2): raise + keyboard-focus the window (the exact M3
    /// click-to-focus body, by handle instead of by cursor hit-test). Returns false
    /// if the handle resolves to no mapped window.
    pub fn ipc_focus_window(&mut self, handle: &str) -> bool {
        let window = match self.ipc_window_for_handle(handle) {
            Some(w) => w,
            None => return false,
        };
        self.space.raise_element(&window, true);
        if let Some(x11) = window.x11_surface() {
            if let Some(xwm) = self.xwm.as_mut() {
                let _ = xwm.raise_window(x11);
            }
        }
        let serial = SERIAL_COUNTER.next_serial();
        let surface = window.wl_surface().map(|s| s.into_owned());
        let keyboard = self.keyboard.clone();
        keyboard.set_focus(self, surface, serial);
        true
    }

    /// `window.move` — reposition only (§4.4 without resize). `map_element` is the
    /// exact call M3 uses for placement; passing `activate=true` also raises it.
    pub fn ipc_move_window(&mut self, handle: &str, x: i32, y: i32) -> bool {
        let window = match self.ipc_window_for_handle(handle) {
            Some(w) => w,
            None => return false,
        };
        self.space.map_element(window.clone(), (x, y), true);
        // An X11 window also needs an explicit configure to learn its new position.
        if let Some(x11) = window.x11_surface() {
            if let Some(bbox) = self.space.element_bbox(&window) {
                let _ = x11.configure(Some(bbox));
            }
        }
        true
    }

    /// `window.resize` — change size, keep location (§4.4). xdg: pending-state size +
    /// configure; X11: `X11Surface::configure` with the new rect. The new size is
    /// applied by the client on its next commit (xdg) / immediately (X11), so the
    /// next painted frame shows it.
    pub fn ipc_resize_window(&mut self, handle: &str, w: i32, h: i32) -> bool {
        let window = match self.ipc_window_for_handle(handle) {
            Some(win) => win,
            None => return false,
        };
        let loc = self.space.element_location(&window).unwrap_or_default();
        if let Some(toplevel) = window.toplevel() {
            toplevel.with_pending_state(|s| {
                s.size = Some((w, h).into());
            });
            toplevel.send_pending_configure();
        }
        if let Some(x11) = window.x11_surface() {
            let rect = smithay::utils::Rectangle::new((loc.x, loc.y).into(), (w, h).into());
            let _ = x11.configure(Some(rect));
        }
        true
    }

    /// `window.place` (§4.4): move AND resize in one op (the zone/rect target). Both
    /// the location and size land — `map_element` repositions, the pending/X11
    /// configure resizes.
    pub fn ipc_place_window(&mut self, handle: &str, x: i32, y: i32, w: i32, h: i32) -> bool {
        let window = match self.ipc_window_for_handle(handle) {
            Some(win) => win,
            None => return false,
        };
        if let Some(toplevel) = window.toplevel() {
            toplevel.with_pending_state(|s| {
                s.size = Some((w, h).into());
            });
            toplevel.send_pending_configure();
        }
        self.space.map_element(window.clone(), (x, y), true);
        if let Some(x11) = window.x11_surface() {
            let rect = smithay::utils::Rectangle::new((x, y).into(), (w, h).into());
            let _ = x11.configure(Some(rect));
        }
        true
    }

    // ---- (place's X11 configure already uses the non-deprecated Rectangle::new) ----

    /// Read a mapped window's current `(x, y, w, h)` (post-op geometry for the
    /// response). None if the handle is unknown.
    pub fn ipc_window_geometry(&self, handle: &str) -> Option<(i32, i32, i32, i32)> {
        let window = self.ipc_window_for_handle(handle)?;
        let g = self.space.element_geometry(&window)?;
        Some((g.loc.x, g.loc.y, g.size.w, g.size.h))
    }

    /// Compute a named-zone rect over the output (§4.4 zones). Logical pixels.
    pub fn ipc_zone_rect(&self, zone: &str) -> Option<(i32, i32, i32, i32)> {
        let geo = self.space.output_geometry(&self.output)?;
        let (ox, oy, ow, oh) = (geo.loc.x, geo.loc.y, geo.size.w, geo.size.h);
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

    /// `window.tile` (§4.5): arrange EVERY mapped toplevel over the output. Supported
    /// layouts: `grid` (near-square), `cols`/`columns` (vertical splits), `rows`
    /// (horizontal splits), `master-stack` (one big left + the rest stacked right),
    /// `fullscreen` (all stacked, each maximized). Returns the arranged handles in the
    /// order applied. Each window is moved+resized via `ipc_place_window` so the next
    /// frame shows the new layout.
    pub fn ipc_tile(&mut self, layout: &str) -> Vec<String> {
        // Snapshot the mapped handles (so we don't borrow `space` while mutating).
        let handles: Vec<String> = self
            .space
            .elements()
            .filter_map(|w| w.user_data().get::<crate::WindowHandle>().map(|h| h.as_str().to_string()))
            .collect();
        let n = handles.len();
        if n == 0 {
            return Vec::new();
        }
        let geo = match self.space.output_geometry(&self.output) {
            Some(g) => g,
            None => return Vec::new(),
        };
        let (ox, oy, ow, oh) = (geo.loc.x, geo.loc.y, geo.size.w, geo.size.h);

        // Compute each window's (x,y,w,h) for the chosen layout.
        let rects: Vec<(i32, i32, i32, i32)> = match layout {
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
            // default: grid (near-square)
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
        };

        for (handle, (x, y, w, h)) in handles.iter().zip(rects.iter()) {
            self.ipc_place_window(handle, *x, *y, *w, *h);
        }
        handles
    }

    /// `window.close` (§4.3): ask the window to close — xdg `send_close()`, X11
    /// `set_mapped(false)`. The real destroy flows through `toplevel_destroyed` /
    /// `unmapped_window` (which run `WindowRegistry::on_unmap` + emit `window.closed`),
    /// so this just requests it; the next frame no longer paints the window.
    pub fn ipc_close_window(&mut self, handle: &str) -> bool {
        let window = match self.ipc_window_for_handle(handle) {
            Some(w) => w,
            None => return false,
        };
        if let Some(toplevel) = window.toplevel() {
            toplevel.send_close();
            return true;
        }
        if let Some(x11) = window.x11_surface() {
            let _ = x11.set_mapped(false);
            return true;
        }
        false
    }

    /// `events.subscribe` (§4.10): clone the connection's stream into the subscriber
    /// set so map/unmap/focus edges push it event frames. Returns the subscription id.
    pub fn ipc_add_subscriber(&mut self, stream: &mut UnixStream) -> String {
        let sub = match stream.try_clone() {
            Ok(s) => s,
            Err(err) => {
                warn!(?err, "IPC: could not clone subscriber stream");
                return "sub_error".to_string();
            }
        };
        self.ipc.next_sub += 1;
        let id = format!("sub_{}", self.ipc.next_sub);
        self.ipc.subscribers.push(sub);
        id
    }
}
