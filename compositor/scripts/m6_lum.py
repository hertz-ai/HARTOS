#!/usr/bin/env python3
"""Decode a PNG (RGB/RGBA, all filter types) and report mean luminance + the
fraction of near-black pixels. Proves the killswitch capture is ~all-black vs a
normal capture's mid/high luminance — orientation-independent."""
import struct, zlib, sys


def load(path):
    try:
        d = open(path, "rb").read()
    except Exception:
        return None
    if d[:8] != b"\x89PNG\r\n\x1a\n":
        return None
    i = 8
    w = h = bd = ct = 0
    idat = b""
    while i < len(d):
        ln = struct.unpack(">I", d[i : i + 4])[0]
        typ = d[i + 4 : i + 8]
        chunk = d[i + 8 : i + 8 + ln]
        i += 12 + ln
        if typ == b"IHDR":
            w, h, bd, ct = struct.unpack(">IIBB", chunk[:10])
        elif typ == b"IDAT":
            idat += chunk
        elif typ == b"IEND":
            break
    try:
        raw = zlib.decompress(idat)
    except Exception:
        return None
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


def stats(path):
    r = load(path)
    if not r:
        print(f"  {path.split('/')[-1]:28} (unreadable)")
        return
    w, h, ch, px = r
    n = w * h
    step = max(1, n // 30000)
    tot = black = cnt = 0
    for p in range(0, n, step):
        o = p * ch
        rr, gg, bb = px[o], px[o + 1], px[o + 2]
        lum = (rr * 299 + gg * 587 + bb * 114) // 1000
        tot += lum
        cnt += 1
        if lum < 8:
            black += 1
    print(
        f"  {path.split('/')[-1]:28} {w}x{h} mean_lum={tot/max(1,cnt):6.1f} "
        f"frac_black={black/max(1,cnt):.3f}"
    )


for f in sys.argv[1:]:
    stats(f)
