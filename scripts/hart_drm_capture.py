#!/usr/bin/env python3
"""Capture the LIVE scanout buffer straight from DRM — the Tier-1 screenshot path.

WHY THIS EXISTS
  Tier-1 (hart-comp, the Smithay/DRM compositor) implements no capture protocol
  on the backend that runs on real hardware. compositor/src/screencopy.rs is
  gated `#![cfg(feature = "winit")]` and binds `crate::winit::State`, and
  comp_core.rs says so plainly: "the DRM backend has no screencopy queue yet".
  So on the tier the OS most wants to run:
      grim -> "compositor doesn't support wlr-screencopy-unstable-v1"
      /dev/fb0 -> the legacy console buffer, black once a DRM master takes over
      xdg-desktop-portal -> not implemented by hart-comp
  Tier-1 painted a desktop nobody could photograph: no screenshots, no screen
  sharing, and nothing for a VLM to look at (task #24 needs frames).

  Porting screencopy to the DRM backend is a real cross-backend refactor (new
  Dispatch impls for wayland::State, a queue field, global registration, and a
  drain point inside udev.rs's render loop where the GlesRenderer and the
  just-painted framebuffer are both live). This reads the same pixels from
  outside the compositor instead, so Tier-1 becomes visible today and the
  refactor stays a separate, unrushed change.

HOW
  debugfs names the framebuffer hart-comp is scanning out; DRM_IOCTL_MODE_GETFB
  turns that id into a GEM handle; PRIME exports it as a dmabuf; mmap reads it.
  Measured on the fleet box 2026-08-27: 1600x900, pitch 6400, bpp 32, XR24 and
  modifier=0x0 (LINEAR), so the bytes are readable with no de-tiling.

THE GATE (this is the important part)
  Reading the scanout buffer bypasses every consent surface the OS has: it does
  not go through the portal, so the xdg-desktop-portal ScreenCast gate never
  sees it. A capture tool that ignores the human's kill-switch is precisely the
  thing core/ai_sensing.py exists to prevent ("humans are always in control").
  So this consults the SAME cross-process authority the portal gate consults,
  and FAILS CLOSED: if the human has cut 'screen', or the authority cannot be
  reached at all, this refuses and captures nothing.

Usage:
  hart_drm_capture.py OUT.png [--allow-ungated]

  --allow-ungated is for a node with no running authority (a bare bring-up box)
  and says so loudly in the exit message. It is NOT a way around a human's cut:
  a REACHABLE authority reporting 'screen' cut still refuses.
"""

import glob
import os
import re
import struct
import sys
import zlib

# fcntl and mmap are Linux-only and are imported inside capture(), NOT here.
# The consent gate below is pure logic and must stay importable (and therefore
# testable) on a non-Linux dev box; a module-level `import fcntl` made the
# whole file unimportable on Windows and took every gate test down with it.
# Nothing outside capture() needs them.


def _iowr(nr, size):
    """_IOWR('d', nr, size) — the DRM ioctl encoding."""
    return (3 << 30) | (size << 16) | (ord('d') << 8) | nr


DRM_IOCTL_MODE_GETFB = _iowr(0xAD, 28)          # drm_mode_fb_cmd: 7 x u32
DRM_IOCTL_PRIME_HANDLE_TO_FD = _iowr(0x2D, 12)  # drm_prime_handle: u32,u32,s32


def screen_capture_allowed():
    """Ask the human's sense gate. FAIL-CLOSED on any doubt.

    Mirrors the portal's use of core.ai_sensing.query_authority('screen'): a
    definitive allow is the ONLY thing that lets a capture proceed.
    """
    try:
        from core.ai_sensing import query_authority
    except Exception:
        return False, "ai_sensing unavailable (cannot confirm the human's consent)"
    try:
        if query_authority('screen'):
            return True, "screen sense allowed by the human's gate"
        return False, "the human has CUT the 'screen' sense"
    except Exception as exc:
        return False, "sense authority unreachable (%s)" % exc


def find_scanout_fb():
    """(fb_id, card_number, owner) for the framebuffer being scanned out.

    Only NUMERIC debugfs directories are considered. Newer kernels expose the
    same device twice -- /sys/kernel/debug/dri/1 and
    /sys/kernel/debug/dri/0000:00:02.0 (the PCI address) -- and the directory
    name is only a valid /dev/dri/card suffix in the numeric case. Taking the
    first sorted match built the path '/dev/dri/card0000:00:02.0', which does
    not exist.
    """
    for path in sorted(glob.glob('/sys/kernel/debug/dri/*/framebuffer')):
        node = path.split('/')[-2]
        if not node.isdigit():
            continue                      # PCI-address alias of a numeric node
        try:
            txt = open(path).read()
        except OSError:
            continue
        m = re.search(r'framebuffer\[(\d+)\]:\s*\n\s*allocated by = (\S+)', txt)
        if m:
            return int(m.group(1)), node, m.group(2)
    return None, None, None


def capture(out_path):
    import fcntl        # Linux-only; see the module header
    import mmap

    fb_id, card, owner = find_scanout_fb()
    if fb_id is None:
        raise SystemExit("no scanout framebuffer in debugfs (is a compositor running?)")

    fd = os.open('/dev/dri/card%s' % card, os.O_RDWR)
    dmabuf = None
    try:
        buf = bytearray(struct.pack('7I', fb_id, 0, 0, 0, 0, 0, 0))
        fcntl.ioctl(fd, DRM_IOCTL_MODE_GETFB, buf, True)
        _id, width, height, pitch, bpp, _depth, handle = struct.unpack('7I', bytes(buf))
        if not handle:
            raise SystemExit("DRM returned no GEM handle (need root / DRM master)")
        if bpp != 32:
            raise SystemExit("unsupported bpp=%d (expected 32/XR24)" % bpp)

        pb = bytearray(struct.pack('IIi', handle, 0, -1))
        fcntl.ioctl(fd, DRM_IOCTL_PRIME_HANDLE_TO_FD, pb, True)
        _h, _f, dmabuf = struct.unpack('IIi', bytes(pb))

        mm = mmap.mmap(dmabuf, pitch * height, mmap.MAP_SHARED, mmap.PROT_READ)
        try:
            rows = []
            for y in range(height):
                off = y * pitch
                row = mm[off:off + width * 4]
                line = bytearray(b'\x00')          # PNG filter byte: None
                for x in range(0, len(row), 4):
                    # XR24 is little-endian BGRX in memory.
                    line += bytes((row[x + 2], row[x + 1], row[x]))
                rows.append(bytes(line))
        finally:
            mm.close()
    finally:
        if dmabuf is not None and dmabuf >= 0:
            os.close(dmabuf)
        os.close(fd)

    def chunk(tag, data):
        return (struct.pack('>I', len(data)) + tag + data
                + struct.pack('>I', zlib.crc32(tag + data) & 0xffffffff))

    png = (b'\x89PNG\r\n\x1a\n'
           + chunk(b'IHDR', struct.pack('>IIBBBBB', width, height, 8, 2, 0, 0, 0))
           + chunk(b'IDAT', zlib.compress(b''.join(rows), 6))
           + chunk(b'IEND', b''))
    with open(out_path, 'wb') as fh:
        fh.write(png)
    return width, height, owner, len(png)


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    allow_ungated = '--allow-ungated' in argv
    argv = [a for a in argv if a != '--allow-ungated']
    if not argv:
        raise SystemExit(__doc__.strip().splitlines()[0] + "\nusage: hart_drm_capture.py OUT.png")

    ok, why = screen_capture_allowed()
    if not ok:
        if not allow_ungated:
            raise SystemExit("REFUSED: %s. Capture is fail-closed; pass "
                             "--allow-ungated only on a node with no authority." % why)
        sys.stderr.write("[hart-drm-capture] proceeding UNGATED (%s)\n" % why)

    w, h, owner, size = capture(argv[0])
    print("captured %dx%d from '%s' scanout -> %s (%d bytes)"
          % (w, h, owner, argv[0], size))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
