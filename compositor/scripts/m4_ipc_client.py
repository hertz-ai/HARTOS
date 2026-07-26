#!/usr/bin/env python3
"""HART-comp M4 — the com.hart.Compositor IPC TEST CLIENT.

Speaks the Unix-socket twin transport from IPC_PROTOCOL.md §2/§3 verbatim:
length-prefixed JSONframing (4-byte big-endian uint32 length + a UTF-8 JSON
object), one request -> one response. This is the agent side of THE MOAT — it
arranges REAL native windows in the live winit compositor, proving the IPC is
wired to `state.space` (not the swaymsg Tier-2 shim).

It does NOT import any HARTOS code on purpose — it is a black-box client that any
agent / the brain's HartWmClient must be able to be (same framed JSON). The brain's
HartWmClient swaps its swaymsg shim for exactly this socket+framing later.

Usage:
    m4_ipc_client.py <socket_path> <command> [args...]
Commands:
    list                       -> window.list, prints the windows table
    tile <layout>              -> window.tile (grid|cols|rows|master-stack|fullscreen)
    focus <handle>             -> window.focus
    move <handle> <x> <y>      -> window.move
    resize <handle> <w> <h>    -> window.resize
    place <handle> <zone>      -> window.place {zone}
    close <handle>             -> window.close
    raw <json>                 -> send an arbitrary request body
"""
import json
import socket
import struct
import sys
import uuid


def send_request(sock_path: str, method: str, args: dict) -> dict:
    """One framed request -> one framed response (IPC_PROTOCOL.md §2)."""
    req = {
        "v": 1,
        "id": "req_" + uuid.uuid4().hex[:8],
        "method": method,
        "agent_id": "m4-test-client",
        "origin": "agent",
        "args": args,
    }
    body = json.dumps(req).encode("utf-8")
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(10)
    s.connect(sock_path)
    # 4-byte big-endian length prefix + body.
    s.sendall(struct.pack(">I", len(body)) + body)
    # Read the response frame: 4-byte length, then that many bytes.
    hdr = _recv_exact(s, 4)
    (resp_len,) = struct.unpack(">I", hdr)
    resp = _recv_exact(s, resp_len)
    s.close()
    return json.loads(resp.decode("utf-8"))


def _recv_exact(s: socket.socket, n: int) -> bytes:
    buf = b""
    while len(buf) < n:
        chunk = s.recv(n - len(buf))
        if not chunk:
            raise ConnectionError("socket closed mid-frame")
        buf += chunk
    return buf


def print_windows(resp: dict) -> None:
    if not resp.get("ok"):
        print("  ERROR:", resp.get("error"))
        return
    wins = resp.get("result", {}).get("windows", [])
    print("  %-10s %-22s %-26s %-22s %-7s %s" %
          ("handle", "app_id", "title", "geometry(x,y,w,h)", "focused", "kind"))
    for w in wins:
        g = w.get("geometry", {})
        geo = "%d,%d %dx%d" % (g.get("x", 0), g.get("y", 0),
                               g.get("w", 0), g.get("h", 0))
        print("  %-10s %-22s %-26s %-22s %-7s %s" % (
            w.get("handle"), str(w.get("app_id"))[:22],
            str(w.get("title"))[:26], geo,
            w.get("focused"), w.get("kind")))
    return wins


def main() -> int:
    if len(sys.argv) < 3:
        print(__doc__)
        return 2
    sock_path = sys.argv[1]
    cmd = sys.argv[2]
    a = sys.argv[3:]

    if cmd == "list":
        resp = send_request(sock_path, "window.list", {})
        print("[client] window.list ->")
        print_windows(resp)
    elif cmd == "tile":
        layout = a[0] if a else "grid"
        resp = send_request(sock_path, "window.tile", {"layout": layout})
        print("[client] window.tile(%s) -> ok=%s arranged=%s" % (
            layout, resp.get("ok"),
            resp.get("result", {}).get("arranged")))
    elif cmd == "focus":
        resp = send_request(sock_path, "window.focus", {"handle": a[0]})
        print("[client] window.focus(%s) -> %s" % (a[0], resp))
    elif cmd == "move":
        resp = send_request(sock_path, "window.move",
                            {"handle": a[0], "x": int(a[1]), "y": int(a[2])})
        print("[client] window.move -> %s" % resp)
    elif cmd == "resize":
        resp = send_request(sock_path, "window.resize",
                            {"handle": a[0], "w": int(a[1]), "h": int(a[2])})
        print("[client] window.resize -> %s" % resp)
    elif cmd == "place":
        resp = send_request(sock_path, "window.place",
                            {"handle": a[0], "target": {"zone": a[1]}})
        print("[client] window.place(%s, zone=%s) -> %s" % (a[0], a[1], resp))
    elif cmd == "close":
        resp = send_request(sock_path, "window.close", {"handle": a[0]})
        print("[client] window.close(%s) -> %s" % (a[0], resp))
    elif cmd == "raw":
        body = json.loads(a[0])
        resp = send_request(sock_path, body["method"], body.get("args", {}))
        print("[client] raw -> %s" % json.dumps(resp))
    else:
        print("unknown command:", cmd)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
