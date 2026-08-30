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
  debugfs's atomic state dump names the framebuffer bound to a live CRTC -- the
  one actually on the glass -- along with its format and modifier;
  DRM_IOCTL_MODE_GETFB turns that id into a GEM handle; PRIME exports it as a
  dmabuf; mmap snapshots it in one memcpy, and the conversion runs on the copy.

WHAT IT ASSUMES, AND WHAT IT REFUSES
  Nothing about the machine is hardcoded. The channel order comes from the
  fourcc the kernel reports and mirrors itself for a big-endian buffer, so the
  same code is correct on i915, amdgpu, nouveau, vc4 or a Mali SoC, on 32- or
  64-bit, on either byte order. The ioctl structs are all fixed-width u32, so
  they need no per-arch packing. Everything it CANNOT do, it refuses by name
  with the kernel's own value attached, rather than emitting wrong pixels:
    - non-LINEAR modifiers (Y-tiled, CCS, DCC, AFBC ...) -- would be noise
    - anything but packed 32-bit RGB, e.g. 10-bit XR30, which needs a real
      bit-unpack rather than a byte shuffle
    - a buffer debugfs and the GETFB ioctl describe differently
  The genuinely non-portable parts are Linux itself: DRM, debugfs, and root.
  A legacy non-atomic driver has no state file; the fallback there reads the fb
  LIST, which cannot tell a front buffer from a back one, and labels every
  capture it produces as such instead of pretending otherwise.

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

import collections
import glob
import os
import re
import struct
import sys
import time
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


#: The refusal reason that means a REACHABLE authority reported the human cut
#: the sense. `main` matches on it, so it must be one constant, not a literal
#: repeated in two places that can drift apart.
CUT_BY_HUMAN = "the human has CUT the 'screen' sense"


def screen_capture_allowed():
    """Ask the human's sense gate. FAIL-CLOSED on any doubt.

    Returns ``(allowed, why)``. When ``allowed`` is False and ``why`` is
    exactly ``CUT_BY_HUMAN``, a reachable authority said NO and no flag may
    override it; every other False is "we could not ask".

    That distinction is why this uses `query_authority_state` rather than the
    boolean `query_authority`. The boolean returns False for BOTH a human's cut
    and an unreachable socket, so this function used to report a dead authority
    as "the human has CUT the 'screen' sense" — and `--allow-ungated`, which
    exists for a node with no authority, then sailed past a real human cut
    because it could not tell the two apart. The docstring at the top of this
    file promised it could. It could not, at any layer.
    """
    try:
        import core.ai_sensing as _sensing
    except Exception:
        return False, "ai_sensing unavailable (cannot confirm the human's consent)"

    tri = getattr(_sensing, 'query_authority_state', None)
    if tri is None:
        # An OLDER ai_sensing that only has the boolean. It cannot tell a cut
        # from a dead socket, so neither can we — report the SAFE reading:
        # refuse, and do NOT claim it was a human's cut, because claiming that
        # wrongly would block --allow-ungated on the bring-up node the flag
        # exists for. Fail-closed either way; only the reason differs.
        try:
            if _sensing.query_authority('screen'):
                return True, "screen sense allowed by the human's gate"
            return False, ("sense denied and this ai_sensing cannot say whether "
                           "that is the human's cut or an unreachable gate")
        except Exception as exc:
            return False, "sense authority unreachable (%s)" % exc

    try:
        state = tri('screen')
    except Exception as exc:
        return False, "sense authority unreachable (%s)" % exc
    if state == getattr(_sensing, 'SENSE_ALLOW', 'allow'):
        return True, "screen sense allowed by the human's gate"
    if state == getattr(_sensing, 'SENSE_CUT', 'cut'):
        return False, CUT_BY_HUMAN
    return False, "sense authority unreachable (no answer from the gate)"


# ── Pixel format: ASK the kernel, never assume ───────────────────────────────
# The scanout buffer is in whatever format the driver and the compositor agreed
# on. Hardcoding one is how a reader silently swaps red and blue on a different
# GPU, or returns noise from a tiled buffer, and looks like a compositor bug
# while doing it. Everything below is derived from the fourcc the kernel itself
# reports, so this works the same on i915, amdgpu, nouveau, vc4 or a Mali SoC,
# on 32- or 64-bit and on either byte order -- and where it genuinely cannot
# cope it refuses by name instead of guessing.

DRM_FORMAT_MOD_LINEAR = 0

# R, G, B byte offsets inside each 4-byte pixel. DRM names a format's channels
# from the TOP of its 32-bit word downwards, so XRGB8888 has X in bits 31-24 and
# B in bits 7-0 -- which, stored little-endian, puts B at byte 0 and R at byte 2.
_PACKED32 = {
    'XR24': (2, 1, 0), 'AR24': (2, 1, 0),   # XRGB8888 / ARGB8888 -> mem B G R X
    'XB24': (0, 1, 2), 'AB24': (0, 1, 2),   # XBGR8888 / ABGR8888 -> mem R G B X
    'RX24': (3, 2, 1), 'RA24': (3, 2, 1),   # RGBX8888 / RGBA8888 -> mem X B G R
    'BX24': (1, 2, 3), 'BA24': (1, 2, 3),   # BGRX8888 / BGRA8888 -> mem X R G B
}

Scanout = collections.namedtuple(
    'Scanout', 'fb_id card owner fourcc endian modifier source')


def rgb_offsets(fourcc, endian):
    """(r, g, b) byte offsets within a 4-byte pixel, or None if unsupported.

    `endian` is the kernel's own word for the buffer ('little' or 'big'), NOT
    the host's. A big-endian buffer holds the same 32-bit word with its bytes
    reversed, so each offset mirrors across the pixel. Deriving it rather than
    assuming little-endian is what keeps the colours right on a big-endian host
    instead of quietly transposing red and blue.
    """
    off = _PACKED32.get(fourcc)
    if off is None:
        return None
    if endian == 'big':
        return tuple(3 - o for o in off)
    return off


def _fb_props(block):
    """(owner, fourcc, endian, modifier) out of a debugfs framebuffer block.

    Both debugfs sources below print an fb the same way -- 'allocated by = X',
    'format=XR24 little-endian (0x34325258)', 'modifier=0x0' -- because it is
    one shared DRM core printer, so one parser serves both.
    """
    owner = re.search(r'allocated by = (\S+)', block)
    fmt = re.search(r'format=(\w+) (little|big)-endian', block)
    mod = re.search(r'modifier=(0x[0-9a-fA-F]+)', block)
    return (owner.group(1) if owner else '?',
            fmt.group(1) if fmt else None,
            fmt.group(2) if fmt else None,
            int(mod.group(1), 16) if mod else None)


def find_scanout_fb():
    """The framebuffer a live CRTC is actually scanning out.

    PREFERRED SOURCE -- /sys/kernel/debug/dri/<n>/state, the DRM core's atomic
    state dump. It lists every plane, the CRTC it is bound to, and the fb id on
    it, with that fb's format and modifier nested underneath. One read answers
    both "which buffer is on the glass" and "how do I read its bytes". It is
    drm_atomic.c core code rather than driver code, so it reads the same on
    every atomic KMS driver.

    WHY NOT THE framebuffer FILE -- it lists every fb that EXISTS, in no
    defined order, and a compositor holds a swapchain. Measured on the box
    2026-08-29 it listed hart-comp's fb 104 AND fb 101 plus fbcon's fb 83;
    taking the first match is a coin flip between the buffer being scanned out
    and the buffer being rendered INTO. It stays as the fallback for legacy
    non-atomic drivers, which have no state file, and labels itself as such.

    Only NUMERIC debugfs directories are considered. Newer kernels expose the
    same device twice -- /sys/kernel/debug/dri/1 and
    /sys/kernel/debug/dri/0000:00:02.0 (the PCI address) -- and the directory
    name is only a valid /dev/dri/card suffix in the numeric case. Taking the
    first sorted match built the path '/dev/dri/card0000:00:02.0', which does
    not exist.
    """
    for path in sorted(glob.glob('/sys/kernel/debug/dri/*/state')):
        node = path.split('/')[-2]
        if not node.isdigit():
            continue                      # PCI-address alias of a numeric node
        try:
            txt = open(path).read()
        except OSError:
            continue
        fallback = None
        for block in txt.split('plane[')[1:]:
            head = block.split('\n', 1)[0]
            if re.search(r'crtc=\(null\)', block):
                continue                  # plane not bound to any CRTC
            m = re.search(r'\n\s*fb=(\d+)', block)
            if not m or m.group(1) == '0':
                continue                  # bound but nothing on it
            owner, fourcc, endian, modifier = _fb_props(block)
            found = Scanout(int(m.group(1)), node, owner, fourcc, endian,
                            modifier, 'atomic state (live CRTC)')
            if 'primary' in head:
                return found              # the desktop plane: what we want
            fallback = fallback or found  # an overlay/cursor plane, second best
        if fallback:
            return fallback

    for path in sorted(glob.glob('/sys/kernel/debug/dri/*/framebuffer')):
        node = path.split('/')[-2]
        if not node.isdigit():
            continue
        try:
            txt = open(path).read()
        except OSError:
            continue
        m = re.search(r'framebuffer\[(\d+)\]:\s*\n\s*allocated by = (\S+)', txt)
        if m:
            block = txt[m.start():]
            _owner, fourcc, endian, modifier = _fb_props(block)
            return Scanout(int(m.group(1)), node, m.group(2), fourcc, endian,
                           modifier,
                           'framebuffer list (no atomic state; this may be a '
                           'back buffer rather than the one on screen)')
    return None


def capture(out_path):
    import fcntl        # Linux-only; see the module header
    import mmap

    fb = find_scanout_fb()
    if fb is None:
        raise SystemExit("no scanout framebuffer in debugfs (is a compositor running?)")

    # ── Refuse rather than return wrong pixels ──────────────────────────────
    # A tiled or compressed buffer read linearly is noise, and an unexpected
    # fourcc read as XRGB is the wrong colours. Both look like a compositor bug
    # and neither is one, so each fails by NAME with the value the kernel gave
    # us -- that string is the spec for whoever adds support.
    if fb.modifier not in (None, DRM_FORMAT_MOD_LINEAR):
        raise SystemExit(
            "scanout fb %d uses modifier 0x%x (not LINEAR); reading it linearly "
            "would produce noise. De-tiling for that modifier is not implemented."
            % (fb.fb_id, fb.modifier))
    offsets = rgb_offsets(fb.fourcc, fb.endian) if fb.fourcc else None
    if offsets is None:
        raise SystemExit(
            "scanout fb %d is format %s %s-endian, which this reader cannot "
            "convert. Supported: %s (packed 32-bit RGB, either byte order). "
            "10-bit formats such as XR30 need a real bit-unpack, not a byte "
            "shuffle." % (fb.fb_id, fb.fourcc, fb.endian,
                          ' '.join(sorted(_PACKED32))))
    r_off, g_off, b_off = offsets

    fb_id, card, owner = fb.fb_id, fb.card, fb.owner
    fd = os.open('/dev/dri/card%s' % card, os.O_RDWR)
    dmabuf = None
    try:
        buf = bytearray(struct.pack('7I', fb_id, 0, 0, 0, 0, 0, 0))
        fcntl.ioctl(fd, DRM_IOCTL_MODE_GETFB, buf, True)
        _id, width, height, pitch, bpp, _depth, handle = struct.unpack('7I', bytes(buf))
        if not handle:
            raise SystemExit("DRM returned no GEM handle (need root / DRM master)")
        if bpp != 32:
            # Cross-check against the fourcc we already accepted: every format
            # in _PACKED32 is 32bpp, so a mismatch means debugfs and the ioctl
            # disagree about this fb and neither can be trusted.
            raise SystemExit(
                "DRM reports bpp=%d for fb %d but debugfs called it %s (32-bit); "
                "refusing a buffer the kernel describes two ways"
                % (bpp, fb_id, fb.fourcc))

        pb = bytearray(struct.pack('IIi', handle, 0, -1))
        fcntl.ioctl(fd, DRM_IOCTL_PRIME_HANDLE_TO_FD, pb, True)
        _h, _f, dmabuf = struct.unpack('IIi', bytes(pb))

        # ── SNAPSHOT FIRST, CONVERT AFTER (this ordering is the whole point) ──
        # We are reading the buffer the display is actively scanning out and the
        # compositor recycles through its swapchain. Nothing here can lock it, so
        # the ONLY defence against a torn frame is to make the read window short.
        #
        # The original code converted pixel-by-pixel straight off the mapping:
        # 1600x900 = 1.44M Python iterations, each building a tuple and a bytes.
        # Measured on the box 2026-08-29: 4.67s wall / 4.33s user for one frame.
        # At 60Hz the compositor flipped ~280 times mid-read, so captures came out
        # stitched from many frames -- full-width horizontal seams at whatever y
        # the read happened to race. Two captures of a STATIC screen put the seams
        # at different y (shared 139,320; A-only 55,100,465,580; B-only 196,299,
        # 509,617,783), which is what proved it was the reader and not hart-comp.
        #
        # So: ONE bulk memcpy of the whole ~5.8MB scanout into private memory,
        # release the mapping, and do every byte of conversion on the copy. The
        # race window drops from seconds to milliseconds -- comfortably inside a
        # single 16.7ms frame -- and the pixels can no longer change underneath us
        # once the copy is taken.
        snap_ms = 0.0
        mm = mmap.mmap(dmabuf, pitch * height, mmap.MAP_SHARED, mmap.PROT_READ)
        try:
            t0 = time.monotonic()
            raw = mm[:]                            # the snapshot: one memcpy
            snap_ms = (time.monotonic() - t0) * 1000.0
        finally:
            mm.close()

        # Conversion off the private copy. Slice-stepping is a C-level loop, so
        # this is ~900 slice ops instead of 1.44M interpreted ones. The channel
        # offsets come from the fourcc the kernel reported (see rgb_offsets), so
        # the same code reads XRGB, XBGR, RGBX or BGRX in either byte order
        # without a branch here. Each row is `pitch` bytes but only the first
        # width*4 are pixels. Stdlib only, on purpose -- this script has no numpy
        # dependency and must keep running from the hart-app python env on a bare
        # bring-up box.
        rows = []
        for y in range(height):
            off = y * pitch
            row = raw[off:off + width * 4]
            line = bytearray(width * 3)
            line[0::3] = row[r_off::4]
            line[1::3] = row[g_off::4]
            line[2::3] = row[b_off::4]
            rows.append(b'\x00' + bytes(line))     # PNG filter byte: None
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
    return width, height, fb, len(png), snap_ms


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    allow_ungated = '--allow-ungated' in argv
    argv = [a for a in argv if a != '--allow-ungated']
    if not argv:
        raise SystemExit(__doc__.strip().splitlines()[0] + "\nusage: hart_drm_capture.py OUT.png")

    ok, why = screen_capture_allowed()
    if not ok:
        # A HUMAN'S CUT IS NOT OVERRIDABLE. --allow-ungated exists for a bare
        # bring-up node with no authority running; it was written to be "NOT a
        # way around a human's cut" (see the module docstring) but it bypassed
        # every refusal, because the gate could not distinguish a cut from an
        # unreachable socket. Both halves are fixed: the gate is tri-state now,
        # and the cut is checked BEFORE the flag.
        if why == CUT_BY_HUMAN or not allow_ungated:
            raise SystemExit("REFUSED: %s. Capture is fail-closed; pass "
                             "--allow-ungated only on a node with no authority." % why)
        sys.stderr.write("[hart-drm-capture] proceeding UNGATED (%s)\n" % why)

    w, h, fb, size, snap_ms = capture(argv[0])
    # Everything the frame depended on is printed, because a capture nobody can
    # audit is a capture nobody should trust:
    #   fb id + source  -- WHICH buffer, and whether we knew it was the live one
    #                      or fell back to guessing from the fb list
    #   format          -- the fourcc the conversion was derived from, so wrong
    #                      colours can be traced without reading the source
    #   snapshot ms     -- the coherence guarantee. The shorter it is, the fewer
    #                      compositor flips a frame can be stitched from; anything
    #                      creeping toward a frame interval (16.7ms at 60Hz) means
    #                      torn captures are possible again, and the number says so
    #                      before anyone debugs seams that are the reader's own.
    print("captured %dx%d from '%s' -> %s (%d bytes, %.1fms snapshot)"
          % (w, h, fb.owner, argv[0], size, snap_ms))
    print("  fb %d via %s, format %s %s-endian, modifier 0x%x"
          % (fb.fb_id, fb.source, fb.fourcc, fb.endian, fb.modifier or 0))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
