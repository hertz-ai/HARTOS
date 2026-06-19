#!/usr/bin/env python3
"""Find the software cursor (a small bright white arrow) near an expected (x,y) in a
DIRECT hart-comp capture, proving the cursor renders AT the pointer location. The arrow
is ~24x24, white fill + black outline, so we look for a cluster of near-white pixels in
a window around the target and report whether it's present there (and absent far away).

Usage: m6_curfind.py img1 x1 y1 [img2 x2 y2 ...]
"""
import struct, zlib, sys


def load(path):
    d = open(path, "rb").read()
    if d[:8] != b"\x89PNG\r\n\x1a\n":
        return None
    i = 8
    w = h = ct = 0
    idat = b""
    while i < len(d):
        ln = struct.unpack(">I", d[i : i + 4])[0]
        typ = d[i + 4 : i + 8]
        chunk = d[i + 8 : i + 8 + ln]
        i += 12 + ln
        if typ == b"IHDR":
            w, h, _bd, ct = struct.unpack(">IIBB", chunk[:10])
        elif typ == b"IDAT":
            idat += chunk
        elif typ == b"IEND":
            break
    raw = zlib.decompress(idat)
    ch = {0: 1, 2: 3, 3: 1, 4: 2, 6: 4}.get(ct, 3)
    stride = w * ch

    def paeth(a, b, c):
        p = a + b - c
        pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
        return a if (pa <= pb and pa <= pc) else (b if pb <= pc else c)

    out = bytearray()
    prev = bytearray(stride)
    pos = 0
    for _y in range(h):
        f = raw[pos]
        pos += 1
        line = bytearray(raw[pos : pos + stride])
        pos += stride
        for x in range(stride):
            a = line[x - ch] if x >= ch else 0
            b = prev[x]
            c = prev[x - ch] if x >= ch else 0
            v = line[x]
            if f == 1:
                v = (v + a) & 255
            elif f == 2:
                v = (v + b) & 255
            elif f == 3:
                v = (v + ((a + b) // 2)) & 255
            elif f == 4:
                v = (v + paeth(a, b, c)) & 255
            line[x] = v
        out += line
        prev = line
    return (w, h, ch, bytes(out))


def white_count(px, w, h, ch, cx, cy, rad):
    """# of near-white pixels in the [cx±rad, cy±rad] box."""
    cnt = 0
    for y in range(max(0, cy - rad), min(h, cy + rad)):
        base = y * w * ch
        for x in range(max(0, cx - rad), min(w, cx + rad)):
            o = base + x * ch
            if px[o] > 230 and px[o + 1] > 230 and px[o + 2] > 230:
                cnt += 1
    return cnt


def main():
    args = sys.argv[1:]
    triples = [(args[i], int(args[i + 1]), int(args[i + 2])) for i in range(0, len(args), 3)]
    for path, ex, ey in triples:
        r = load(path)
        if not r:
            print(f"  {path.split('/')[-1]}: unreadable")
            continue
        w, h, ch, px = r
        near = white_count(px, w, h, ch, ex, ey, 20)
        # A far-away reference box (opposite corner) to show the arrow is LOCALIZED.
        fx, fy = (w - ex), (h - ey)
        far = white_count(px, w, h, ch, fx, fy, 20)
        verdict = "CURSOR PRESENT" if near >= 15 else "not found"
        print(
            f"  {path.split('/')[-1]:28} target=({ex},{ey}) white_near={near:4} "
            f"white_far@({fx},{fy})={far:4} -> {verdict}"
        )


if __name__ == "__main__":
    main()
