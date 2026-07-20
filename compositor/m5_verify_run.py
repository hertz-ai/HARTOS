#!/usr/bin/env python3
"""M5 live verification driver — runs as sathish, talks framed JSON to hart-comp.sock.
Each step prints the IPC response + a compact window.list so geometry/workspace/focus
deltas are observable. No shell-quote nesting."""
import socket, json, struct, os, sys, time

SOCK = "/run/user/1000/hart-comp.sock"


def call(method, args=None):
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.connect(SOCK)
    req = {"id": "1", "method": method, "args": args or {}}
    body = json.dumps(req).encode()
    s.sendall(struct.pack(">I", len(body)) + body)
    s.settimeout(4)
    hdr = b""
    while len(hdr) < 4:
        c = s.recv(4 - len(hdr))
        if not c:
            break
        hdr += c
    n = struct.unpack(">I", hdr)[0]
    buf = b""
    while len(buf) < n:
        c = s.recv(n - len(buf))
        if not c:
            break
        buf += c
    s.close()
    return json.loads(buf.decode())


def listw():
    r = call("window.list")
    return r["result"]["windows"]


def show(label):
    ws = listw()
    print(f"  [{label}]")
    for w in sorted(ws, key=lambda x: x["handle"]):
        g = w["geometry"]
        print(
            f"    {w['handle']:6} {w['title'][:14]:14} ws={w['workspace']} "
            f"vis={str(w['visible']):5} foc={str(w['focused']):5} "
            f"geo=({g['x']},{g['y']},{g['w']},{g['h']})"
        )


def main():
    test = sys.argv[1]

    if test == "list":
        show("current")

    elif test == "snap_right":
        # Find the focused window (or pick win_3) and snap right via zone place.
        show("before")
        # focus a known window first
        h = sys.argv[2] if len(sys.argv) > 2 else "win_3"
        print("  focus", h, "->", call("window.focus", {"handle": h})["ok"])
        r = call("window.place", {"handle": h, "target": {"zone": "right-half"}})
        print("  place right-half resp:", json.dumps(r["result"] if r["ok"] else r["error"]))
        show("after")

    elif test == "snap_left":
        h = sys.argv[2] if len(sys.argv) > 2 else "win_3"
        print("  focus", h, "->", call("window.focus", {"handle": h})["ok"])
        r = call("window.place", {"handle": h, "target": {"zone": "left-half"}})
        print("  place left-half resp:", json.dumps(r["result"] if r["ok"] else r["error"]))
        show("after")

    elif test == "maximize":
        h = sys.argv[2] if len(sys.argv) > 2 else "win_3"
        print("  focus", h, "->", call("window.focus", {"handle": h})["ok"])
        r = call("window.place", {"handle": h, "target": {"zone": "maximize"}})
        print("  place maximize resp:", json.dumps(r["result"] if r["ok"] else r["error"]))
        show("after")

    elif test == "switch_ws":
        n = int(sys.argv[2])
        show("before switch")
        r = call("workspace.switch", {"workspace": n})
        print(f"  workspace.switch({n}) resp:", json.dumps(r["result"] if r["ok"] else r["error"]))
        show(f"after switch to ws{n}")

    elif test == "move_ws":
        h = sys.argv[2]
        n = int(sys.argv[3])
        show("before move")
        r = call("window.move_to_workspace", {"handle": h, "workspace": n})
        print(f"  move {h} -> ws{n} resp:", json.dumps(r["result"] if r["ok"] else r["error"]))
        show("after move")

    elif test == "close":
        h = sys.argv[2]
        show("before close")
        r = call("window.close", {"handle": h})
        print(f"  close {h} resp:", json.dumps(r["result"] if r["ok"] else r["error"]))
        time.sleep(1)
        show("after close")

    elif test == "focus":
        h = sys.argv[2]
        r = call("window.focus", {"handle": h})
        print(f"  focus {h} resp:", json.dumps(r["result"] if r["ok"] else r["error"]))
        show("after focus")


if __name__ == "__main__":
    main()
