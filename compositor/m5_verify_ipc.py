#!/usr/bin/env python3
"""M5 verification IPC client — framed JSON (4-byte BE length prefix), method field."""
import socket, json, sys, struct, os

SOCK = os.environ.get("HART_COMP_SOCK", "/run/user/1000/hart-comp.sock")


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


if __name__ == "__main__":
    method = sys.argv[1]
    args = json.loads(sys.argv[2]) if len(sys.argv) > 2 else {}
    print(json.dumps(call(method, args)))
