#!/usr/bin/env python3
"""M6 IPC client — framed JSON to hart-comp.sock. Generic verb driver.

Usage:
  m6_ipc.py <sock> screen.kill on        -> {"method":"screen.kill","args":{"on":true}}
  m6_ipc.py <sock> screen.kill off
  m6_ipc.py <sock> workspace.switch 2    -> {"args":{"workspace":2}}
  m6_ipc.py <sock> window.list
"""
import socket, json, struct, sys


def call(sock_path, method, args):
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.connect(sock_path)
    body = json.dumps({"id": "1", "method": method, "args": args}).encode()
    s.sendall(struct.pack(">I", len(body)) + body)
    s.settimeout(4)
    hdr = b""
    while len(hdr) < 4:
        c = s.recv(4 - len(hdr))
        if not c:
            break
        hdr += c
    if len(hdr) < 4:
        s.close()
        return {"error": "no response header"}
    n = struct.unpack(">I", hdr)[0]
    buf = b""
    while len(buf) < n:
        c = s.recv(n - len(buf))
        if not c:
            break
        buf += c
    s.close()
    return json.loads(buf.decode())


def main():
    sock = sys.argv[1]
    method = sys.argv[2]
    rest = sys.argv[3:]
    args = {}
    if method in ("screen.kill", "ScreenKill"):
        on = (rest[0].lower() in ("on", "true", "1")) if rest else True
        args = {"on": on}
    elif method in ("workspace.switch", "SwitchWorkspace"):
        args = {"workspace": int(rest[0])} if rest else {}
    elif method in ("window.focus", "window.close"):
        args = {"handle": rest[0]} if rest else {}
    # window.list / others: no args
    resp = call(sock, method, args)
    print(json.dumps(resp))


if __name__ == "__main__":
    main()
