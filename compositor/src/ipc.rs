// â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•
// HART-comp â€” Milestone 4: the `com.hart.Compositor` IPC server, wired to the
// REAL winit `Space<Window>` so an agent arranges real native windows.
// â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•
//
// This is the FIRST actually-running `com.hart.Compositor` server. Phase 6 / task
// #12 shipped only the CONTRACT (../IPC_PROTOCOL.md) + the brain-side Python
// `HartWmClient` (which shells out to `swaymsg` â€” sway Tier-2, NOT a client of
// HART-comp). NOTHING here ran against the live compositor's window tree. M4 wires
// the Unix-socket transport (IPC_PROTOCOL.md Â§2 "Unix-socket twin") into the winit
// calloop loop and implements the command handlers AGAINST `state.space`.
//
// â”€â”€ Why the Unix-socket twin first (NOT D-Bus) â”€â”€
//   IPC_PROTOCOL.md Â§2 specifies two transports exposing the SAME surface: D-Bus
//   (`com.hart.Compositor` on the session bus) and a Unix-socket twin
//   (`$XDG_RUNTIME_DIR/hart-comp.sock`, 0600, length-prefixed JSON). The socket
//   twin drops straight into the existing calloop loop as a `Generic<UnixListener>`
//   source â€” no async runtime, no zbus, no second event loop. `serde` is already in
//   the dep tree (smithay pulls it); only `serde_json` is added as a direct dep.
//   D-Bus can be added later or proxied by Python over this socket. The brain
//   reaches it via the EXISTING `HartWmClient` singleton â€” swap its swaymsg shim for
//   a socket client speaking the SAME framed JSON, same `dispatch_verb` surface.
//
// â”€â”€ Framing (IPC_PROTOCOL.md Â§2/Â§3) â”€â”€
//   4-byte big-endian uint32 length, then a UTF-8 JSON object. One request â†’ one
//   response; events are unsolicited JSON frames on a subscription. The request /
//   response / event envelopes match Â§3 + Â§5.
//
// â”€â”€ No-phantom-window honesty (IPC_PROTOCOL.md Â§1.4/Â§9.2) â”€â”€
//   `window.list` enumerates ONLY the `space.elements()` that actually mapped (a
//   handle exists only because `winit.rs::on_real_map` minted it on a real buffer
//   commit). Every mutating verb resolves its `handle` against a live mapped window
//   or returns `not_found` â€” never a fabricated success.
//
// â”€â”€ Security boundary (IPC_PROTOCOL.md Â§6) â”€â”€
//   The full constitutional gate (HiveCircuitBreaker + GuardrailEnforcer + per-agent
//   rate cap + immutable audit + PREVIEW for destructive geometry) lives BRAIN-SIDE
//   in `integrations/agent_engine/hart_wm_client.py::_guard_destructive` â€” the same
//   fail-closed gate every verb already passes there. The compositor is the
//   privileged executor the gated brain drives; it re-checks nothing the brain
//   already proved on THIS rev (the socket is 0600, owner = the session user, so the
//   only writer is the same trust domain as the brain). When the D-Bus transport +
//   server-side re-check land (IPC_PROTOCOL.md Â§6.2 "re-checked server-side"), they
//   hang off `dispatch_request` below. Until then the socket-permission boundary
//   (Â§6.5) is the server-side control, and the brain's gate is authoritative.

// â”€â”€ M8 â€” re-gated to `any(winit, smithay)`: the framed-JSON com.hart.Compositor
// transport is now GENERIC over the backend `State` (via `comp_core::CompState`), so
// BOTH the winit (dev/WSL) and DRM (real-HW) backends serve the SAME socket surface â€”
// the moat on real hardware too, not a winit-only path. The verb BODIES live in
// `comp_core` (shared); this file is the transport + dispatch that calls them. â”€â”€
#![cfg(any(feature = "winit", feature = "smithay"))]

use std::io::{ErrorKind, Read, Write};
use std::os::unix::net::{UnixListener, UnixStream};
use std::path::PathBuf;

use serde::{Deserialize, Serialize};
use serde_json::{json, Value};
use smithay::reexports::calloop::{
    generic::Generic, Interest, LoopHandle, Mode, PostAction,
};
// `Window::wl_surface()` comes from the `WaylandFocus` trait on this Smithay rev (not an
// inherent method) â€” it MUST be in scope for `ipc_list_windows`'s focus check.
use smithay::wayland::seat::WaylandFocus;
use tracing::{info, warn};

use crate::comp_core::{self, CompState};

/// Protocol version (IPC_PROTOCOL.md Â§3 â€” the `v` field).
const PROTOCOL_VERSION: u64 = 1;
/// Hard cap on a single framed message so a malformed length prefix cannot make us
/// allocate gigabytes. 1 MiB is far above any real window-op request.
const MAX_FRAME_LEN: u32 = 1024 * 1024;

// â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
// Per-compositor IPC state â€” held in `State.ipc`. Tracks event subscribers (the
// `events.subscribe` sinks) so map/unmap/focus edges in winit.rs can push
// unsolicited event frames (IPC_PROTOCOL.md Â§5).
// â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
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
    /// Push an event frame (IPC_PROTOCOL.md Â§5) to every live subscriber. Drops a
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

// â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
// Framed-JSON wire helpers (IPC_PROTOCOL.md Â§2 framing).
// â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

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
                Ok(0) => return Ok(true), // EOF â€” peer closed
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
            // Poison frame â€” drop the whole buffer so we resync rather than allocate.
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

// â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
// Request / response envelope (IPC_PROTOCOL.md Â§3).
// â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
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

// â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
// The socket path + the calloop wiring (called from winit.rs::run_winit).
// â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

/// `$XDG_RUNTIME_DIR/hart-comp.sock` (IPC_PROTOCOL.md Â§2). Falls back to `/tmp` only
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
/// else (the IPC is an add-on, never a boot gate â€” same posture as XWayland).
pub fn start_ipc<S: CompState>(loop_handle: &LoopHandle<'static, S>) -> Option<PathBuf> {
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
    // 0600, owner = the session user (IPC_PROTOCOL.md Â§6.5 socket boundary).
    if let Err(err) = set_socket_mode_0600(&path) {
        warn!(?err, "IPC: could not chmod 0600 the socket (continuing)");
    }
    if let Err(err) = listener.set_nonblocking(true) {
        warn!(?err, "IPC: could not set the listener non-blocking");
    }

    let source = Generic::new(listener, Interest::READ, Mode::Level);
    let inserted = loop_handle.insert_source(source, |_readiness, listener, state: &mut S| {
        // Accept every pending connection (Level-triggered â†’ drain the backlog).
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

/// chmod 0600 the socket (owner-only). Pure libc via std â€” no extra crate.
fn set_socket_mode_0600(path: &std::path::Path) -> std::io::Result<()> {
    use std::os::unix::fs::PermissionsExt;
    let perms = std::fs::Permissions::from_mode(0o600);
    std::fs::set_permissions(path, perms)
}

/// Register one accepted client stream as its own calloop `Generic` source. The
/// source reads framed requests, dispatches each against `&mut State` (the live
/// window tree), and writes the framed response. On EOF/error it removes itself.
fn register_connection<S: CompState>(state: &mut S, stream: UnixStream) {
    // The Generic source needs an `AsFd` â€” clone the stream for the source's fd, and
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
    // Clone the loop handle (it is `Clone`) so the immutable `state` borrow ends before
    // the closure (which takes `&mut S`) is registered â€” no borrow tangle.
    let loop_handle = state.loop_handle().clone();
    let inserted = loop_handle.insert_source(source, move |_readiness, _fd, state: &mut S| {
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
fn handle_frame<S: CompState>(state: &mut S, body: &[u8], stream: &mut UnixStream) -> Response {
    let req: Request = match serde_json::from_slice(body) {
        Ok(r) => r,
        Err(err) => return Response::err(None, "invalid_args", format!("malformed request: {err}")),
    };
    let id = req.id.clone();
    dispatch_request(state, &req.method, &req.args, id, stream)
}

// â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
// THE command surface â€” every verb runs against the REAL `state.space`
// (IPC_PROTOCOL.md Â§4). Geometry is logical pixels on the single winit output.
// â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

fn dispatch_request<S: CompState>(
    state: &mut S,
    method: &str,
    args: &Value,
    id: Option<String>,
    stream: &mut UnixStream,
) -> Response {
    match method {
        // â”€â”€ Â§4.1 window.list â€” read-only enumeration of mapped toplevels â”€â”€
        "window.list" | "ListWindows" => {
            Response::ok(id, json!({ "windows": ipc_list_windows(state) }))
        }

        // â”€â”€ Â§4.2 window.focus(handle) â€” keyboard focus + raise â”€â”€
        "window.focus" | "FocusWindow" => match arg_handle(args) {
            Some(h) => {
                if comp_core::ipc_focus_window(state, &h) {
                    Response::ok(id, json!({ "handle": h, "focused": true }))
                } else {
                    Response::err(id, "not_found", format!("no mapped window for handle {h}"))
                }
            }
            None => Response::err(id, "invalid_args", "window.focus needs args.handle"),
        },

        // â”€â”€ Â§4.4 window.place(handle, target{x,y,w,h}|{zone}) â€” move (+resize) â”€â”€
        "window.place" | "PlaceWindow" => match arg_handle(args) {
            Some(h) => match resolve_target(state, args.get("target")) {
                Some((x, y, w, h_sz)) => {
                    if comp_core::ipc_place_window(state, &h, x, y, w, h_sz) {
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

        // â”€â”€ move(handle,x,y) â€” a plain reposition (no resize). Not in the spec's
        //    numbered list as its own verb (place covers it) but the milestone names
        //    `move(id,x,y)` explicitly, so it is wired as a first-class verb too. â”€â”€
        "window.move" | "MoveWindow" => match (arg_handle(args), arg_i32(args, "x"), arg_i32(args, "y")) {
            (Some(h), Some(x), Some(y)) => {
                if comp_core::ipc_move_window(state, &h, x, y) {
                    let geo = comp_core::ipc_window_geometry(state, &h).unwrap_or((x, y, 0, 0));
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

        // â”€â”€ resize(handle,w,h) â€” the milestone's resize verb (xdg configure / X11
        //    configure). Keeps the window's current location, changes its size. â”€â”€
        "window.resize" | "ResizeWindow" => match (arg_handle(args), arg_i32(args, "w"), arg_i32(args, "h")) {
            (Some(h), Some(w), Some(hh)) => {
                if comp_core::ipc_resize_window(state, &h, w, hh) {
                    let geo = comp_core::ipc_window_geometry(state, &h).unwrap_or((0, 0, w, hh));
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

        // â”€â”€ Â§4.5 window.tile(layout) â€” arrange ALL mapped toplevels. â”€â”€
        "window.tile" | "TileLayout" | "window.arrange" => {
            let layout = args.get("layout").and_then(Value::as_str).unwrap_or("grid").to_string();
            let arranged = comp_core::ipc_tile(state, &layout);
            Response::ok(id, json!({ "layout": layout, "arranged": arranged }))
        }

        // â”€â”€ Â§4.3 window.close(handle) â€” send_close (xdg) / set_mapped(false) (X11) â”€â”€
        "window.close" | "CloseWindow" => match arg_handle(args) {
            Some(h) => {
                if comp_core::ipc_close_window(state, &h) {
                    Response::ok(id, json!({ "handle": h, "closed": true }))
                } else {
                    Response::err(id, "not_found", format!("no mapped window for handle {h}"))
                }
            }
            None => Response::err(id, "invalid_args", "window.close needs args.handle"),
        },

        // â”€â”€ Â§4.8 workspace.switch(n) â€” show workspace N (M5 drives the SAME
        //    active_workspace the Super+N chord does). `n` is 0-based internally;
        //    the response echoes the 1-based wire value the caller sent. â”€â”€
        "workspace.switch" | "SwitchWorkspace" => match arg_workspace(args) {
            Some(n) => {
                let changed = comp_core::switch_workspace(state, n);
                Response::ok(id, json!({ "workspace": n + 1, "switched": changed }))
            }
            None => Response::err(id, "invalid_args", "workspace.switch needs args.workspace (>=1)"),
        },

        // â”€â”€ Â§4.7 window.move_to_workspace(handle, n) â€” move a window to workspace N. â”€â”€
        "window.move_to_workspace" | "MoveToWorkspace" => {
            match (arg_handle(args), arg_workspace(args)) {
                (Some(h), Some(n)) => {
                    if comp_core::move_window_to_workspace_by_handle(state, &h, n) {
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

        // â”€â”€ M6 screen.kill(on) â€” the constitutional screen kill-switch. The brain
        //    pushes this when the human cuts/restores `screen` (IPC_PROTOCOL.md Â§6.4 +
        //    killswitch_plan). It sets ONE compositor flag that (a) draws a full-output
        //    opaque black surface ABOVE all windows, (b) stops forwarding input to
        //    clients, and (c) refuses every zwlr_screencopy `copy`. The backend's
        //    `set_capture_blocked` override (winit drains its screencopy queue) runs it. â”€â”€
        "screen.kill" | "ScreenKill" => {
            let on = args.get("on").and_then(Value::as_bool).unwrap_or(true);
            let blocked = state.set_capture_blocked(on);
            Response::ok(id, json!({ "blocked": blocked }))
        }

        // â”€â”€ Â§4.10 events.subscribe â€” register this stream for unsolicited events â”€â”€
        "events.subscribe" | "Subscribe" => {
            let events = args.get("events").cloned().unwrap_or_else(|| json!([
                "window.opened", "window.closed", "window.focused"
            ]));
            let sub = ipc_add_subscriber(state, stream);
            Response::ok(id, json!({ "subscription": sub, "events": events }))
        }

        other => Response::err(id, "unsupported", format!("unknown method: {other}")),
    }
}

// â”€â”€ argument extractors â”€â”€
fn arg_handle(args: &Value) -> Option<String> {
    args.get("handle").and_then(Value::as_str).map(str::to_string)
}
fn arg_i32(args: &Value, key: &str) -> Option<i32> {
    args.get(key).and_then(Value::as_i64).map(|v| v as i32)
}

/// The `workspace` arg â€” the IPC contract numbers workspaces from 1 on the wire
/// (`{"workspace": 2}`, IPC_PROTOCOL.md Â§4.5/Â§4.7/Â§4.8), but the compositor indexes
/// them from 0 internally (the Super+1 chord = `SwitchWorkspace(0)`, `n = keysym -
/// KEY_1`). Convert 1-based wire â†’ 0-based internal here so the two triggers (chord +
/// IPC) drive the SAME `active_workspace` space. Rejects `< 1`.
fn arg_workspace(args: &Value) -> Option<usize> {
    let n = args.get("workspace").and_then(Value::as_i64)?;
    if n < 1 {
        return None;
    }
    Some((n - 1) as usize)
}

/// Resolve a `target` payload to an `(x, y, w, h)` rect â€” either explicit
/// `{x,y,w,h}` or a named `{zone}` computed over the output geometry (Â§4.4).
fn resolve_target<S: CompState>(state: &S, target: Option<&Value>) -> Option<(i32, i32, i32, i32)> {
    let t = target?;
    if let Some(zone) = t.get("zone").and_then(Value::as_str) {
        return comp_core::ipc_zone_rect(state, zone);
    }
    let x = t.get("x").and_then(Value::as_i64)? as i32;
    let y = t.get("y").and_then(Value::as_i64)? as i32;
    let w = t.get("w").and_then(Value::as_i64)? as i32;
    let h = t.get("h").and_then(Value::as_i64)? as i32;
    Some((x, y, w, h))
}

// â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•
// `State` IPC methods â€” the bridge from a verb to the live Smithay window tree.
// These live in this module (as an `impl State`) so winit.rs stays focused on the
// protocol handlers; they call ONLY the same `space`/`seat`/`xwm` mutators the M3
// input path already uses (raise_element / map_element / set_focus / configure).
// â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•
// â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•
// The two IPC verbs that touch the per-connection subscriber set (`IpcState`) â€”
// generic over the backend `State` via `CompState`. The window-mutating verbs
// (focus/place/move/resize/tile/close/zone/workspace) live in `comp_core` (shared
// with the keyboard chords); these two stay here because they read the
// hidden-windows list + the subscriber stream set, both reached through the trait.
// â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•

/// `window.list` (Â§4.1): one JSON object per mapped toplevel, from the SAME source of
/// truth the render path paints â€” `space().elements()` (the VISIBLE active workspace),
/// PLUS the M5 `hidden_windows()` set (windows on non-active workspaces), each joined
/// with the per-window `WindowHandle` minted on real map. Never lists a phantom.
///
/// Each row carries `workspace` (1-based, the IPC wire convention) + `visible` (true for
/// the active workspace's windows, false for the held set) so an agent â€” or the M5 test
/// â€” can SEE that e.g. switching to workspace 2 left the workspace-1 windows
/// real-but-hidden, not destroyed.
fn ipc_list_windows<S: CompState>(state: &S) -> Vec<Value> {
    let focused_surface = state.keyboard().current_focus();
    let mut out = Vec::new();
    // Visible windows (the active workspace).
    for window in state.space().elements() {
        let handle = match window.user_data().get::<crate::WindowHandle>() {
            Some(h) => h.as_str().to_string(),
            None => continue, // not yet mapped (no real handle) â€” honesty rule
        };
        let geo = state.space().element_geometry(window);
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
            "app_id": comp_core::ipc_window_app_id(window),
            "title": comp_core::ipc_window_title(window),
            "geometry": { "x": x, "y": y, "w": w, "h": h },
            "focused": focused,
            "kind": if is_x11 { "x11" } else { "xdg" },
            "mapped": true,
            "workspace": state.active_workspace() + 1, // 1-based on the wire
            "visible": true,
        }));
    }
    // Hidden windows (non-active workspaces + show-desktop stash). Real, mapped, just not
    // on the visible output â€” listed so workspace state is observable.
    for hw in state.hidden_windows() {
        let window = &hw.window;
        let handle = match window.user_data().get::<crate::WindowHandle>() {
            Some(h) => h.as_str().to_string(),
            None => continue,
        };
        let is_x11 = window.x11_surface().is_some();
        let (w, h) = state
            .space()
            .element_geometry(window)
            .map(|g| (g.size.w, g.size.h))
            .unwrap_or((0, 0));
        out.push(json!({
            "handle": handle,
            "app_id": comp_core::ipc_window_app_id(window),
            "title": comp_core::ipc_window_title(window),
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

/// `events.subscribe` (Â§4.10): clone the connection's stream into the subscriber set so
/// map/unmap/focus edges push it event frames. Returns the subscription id. The
/// subscriber set lives in the backend's `IpcState` (reached via `ipc_state_mut`).
fn ipc_add_subscriber<S: CompState>(state: &mut S, stream: &mut UnixStream) -> String {
    let sub = match stream.try_clone() {
        Ok(s) => s,
        Err(err) => {
            warn!(?err, "IPC: could not clone subscriber stream");
            return "sub_error".to_string();
        }
    };
    let ipc = state.ipc_state_mut();
    ipc.next_sub += 1;
    let id = format!("sub_{}", ipc.next_sub);
    ipc.subscribers.push(sub);
    id
}
