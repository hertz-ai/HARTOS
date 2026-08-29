"""The DRM capture must read the RIGHT buffer and decode it the RIGHT way.

Two defects motivated these tests, both found 2026-08-29 while trying to verify
a UI fix by looking at the box:

1. WRONG BUFFER. find_scanout_fb() took the first entry of debugfs's
   `framebuffer` file. That file lists every fb that exists, in no defined
   order, and a compositor holds a swapchain -- the box listed hart-comp's fb
   104 AND fb 101 plus fbcon's fb 83. Picking the first was a coin flip between
   the buffer on screen and the buffer being rendered into. The atomic `state`
   file says which fb is bound to a live CRTC, so it is the source now.

2. HARDCODED PIXELS. The converter assumed XR24-little-endian BGRX and only
   checked bpp==32. A different fourcc (XBGR, RGBX) would have silently swapped
   red and blue; a tiled modifier would have produced noise; XR30 is 32bpp and
   10-bit and would have been garbage. All of it would have looked like a
   compositor bug. Channel offsets are now derived from the fourcc the kernel
   reports, and anything unsupported refuses by name.

The debugfs fixtures below are VERBATIM from the box (i915, 1600x900) so the
parsers are tested against real kernel output rather than an invented format.

Run:
  pytest tests/unit/test_drm_capture_pixels.py -v --noconftest
"""

import importlib.util
import os

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
TOOL = os.path.join(REPO, "scripts", "hart_drm_capture.py")


def _tool():
    spec = importlib.util.spec_from_file_location("hart_drm_capture", TOOL)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# Verbatim from /sys/kernel/debug/dri/1/state on the box. Trimmed to the planes
# that matter: one primary bound to pipe A holding fb 104, and unbound planes
# that must all be ignored.
STATE = """plane[32]: primary A
\tcrtc=pipe A
\tfb=104
\t\tallocated by = hart-comp
\t\trefcount=3
\t\tformat=XR24 little-endian (0x34325258)
\t\tmodifier=0x0
\t\tsize=1600x900
\t\tlayers:
\t\t\tsize[0]=1600x900
\t\t\tpitch[0]=6400
\tcrtc-pos=1600x900+0+0
plane[36]: sprite A
\tcrtc=(null)
\tfb=0
\tcrtc-pos=0x0+0+0
plane[42]: cursor A
\tcrtc=(null)
\tfb=0
\tcrtc-pos=0x0+0+0
plane[48]: primary B
\tcrtc=(null)
\tfb=0
\tcrtc-pos=0x0+0+0
"""

# Verbatim from /sys/kernel/debug/dri/1/framebuffer on the same box. Note it
# lists hart-comp TWICE -- this is the swapchain that made "take the first one"
# a coin flip.
FRAMEBUFFER = """framebuffer[104]:
\tallocated by = hart-comp
\trefcount=3
\tformat=XR24 little-endian (0x34325258)
\tmodifier=0x0
\tsize=1600x900
framebuffer[101]:
\tallocated by = hart-comp
\trefcount=1
\tformat=XR24 little-endian (0x34325258)
\tmodifier=0x0
\tsize=1600x900
framebuffer[83]:
\tallocated by = [fbcon]
\trefcount=1
\tformat=XR24 little-endian (0x34325258)
\tmodifier=0x0
\tsize=1600x900
"""


def _fake_debugfs(mod, monkeypatch, files):
    """Serve `files` (path -> text) as the only readable debugfs entries."""
    monkeypatch.setattr(mod.glob, "glob",
                        lambda pat: sorted(p for p in files
                                           if _matches(pat, p)))
    real_open = open

    def fake_open(path, *a, **kw):
        if path in files:
            import io
            return io.StringIO(files[path])
        return real_open(path, *a, **kw)

    monkeypatch.setitem(mod.__dict__, "open", fake_open)


def _matches(pattern, path):
    import fnmatch
    return fnmatch.fnmatch(path, pattern)


# ── which buffer ────────────────────────────────────────────────────────────

def test_picks_the_fb_bound_to_a_live_crtc(monkeypatch):
    """fb 104 is on pipe A. Nothing else is on any CRTC."""
    mod = _tool()
    _fake_debugfs(mod, monkeypatch, {
        "/sys/kernel/debug/dri/1/state": STATE,
        "/sys/kernel/debug/dri/1/framebuffer": FRAMEBUFFER,
    })
    fb = mod.find_scanout_fb()
    assert fb is not None
    assert fb.fb_id == 104
    assert fb.card == "1"
    assert fb.owner == "hart-comp"
    assert "atomic state" in fb.source


def test_unbound_planes_are_never_chosen(monkeypatch):
    """Every plane but the primary has crtc=(null) and fb=0. If those were
    considered we would capture nothing, or a stale cursor buffer."""
    mod = _tool()
    only_unbound = "\n".join(STATE.split("plane[")[2:])
    _fake_debugfs(mod, monkeypatch,
                  {"/sys/kernel/debug/dri/1/state": "plane[" + only_unbound})
    assert mod.find_scanout_fb() is None


def test_pci_alias_debugfs_nodes_are_skipped(monkeypatch):
    """/dri/0000:00:02.0 is the same GPU as /dri/1, but the name is not a valid
    /dev/dri/card suffix -- it built '/dev/dri/card0000:00:02.0'."""
    mod = _tool()
    _fake_debugfs(mod, monkeypatch, {
        "/sys/kernel/debug/dri/0000:00:02.0/state": STATE,
    })
    assert mod.find_scanout_fb() is None


def test_falls_back_to_the_fb_list_and_says_it_may_be_a_back_buffer(monkeypatch):
    """Legacy non-atomic drivers have no state file. We still capture, but the
    caller is told the frame may be a back buffer rather than the screen."""
    mod = _tool()
    _fake_debugfs(mod, monkeypatch,
                  {"/sys/kernel/debug/dri/1/framebuffer": FRAMEBUFFER})
    fb = mod.find_scanout_fb()
    assert fb.fb_id == 104
    assert "back buffer" in fb.source
    assert fb.fourcc == "XR24"          # format still parsed from the same block


def test_format_and_modifier_come_from_the_kernel(monkeypatch):
    mod = _tool()
    _fake_debugfs(mod, monkeypatch, {"/sys/kernel/debug/dri/1/state": STATE})
    fb = mod.find_scanout_fb()
    assert (fb.fourcc, fb.endian, fb.modifier) == ("XR24", "little", 0)


# ── how the bytes are decoded ───────────────────────────────────────────────

def test_xrgb_little_endian_offsets():
    """XRGB8888 has X in bits 31-24 and B in 7-0, so little-endian memory is
    B G R X: red at byte 2, blue at byte 0. This is the box's format and the
    one the old hardcoded converter happened to be right about."""
    assert _tool().rgb_offsets("XR24", "little") == (2, 1, 0)


def test_xbgr_is_not_decoded_like_xrgb():
    """The regression the hardcoding would have caused: XBGR8888 puts red at
    byte 0. Decoding it as XRGB swaps red and blue with no error anywhere."""
    mod = _tool()
    assert mod.rgb_offsets("XB24", "little") == (0, 1, 2)
    assert mod.rgb_offsets("XB24", "little") != mod.rgb_offsets("XR24", "little")


@pytest.mark.parametrize("fourcc,expected", [
    ("AR24", (2, 1, 0)),    # ARGB8888
    ("AB24", (0, 1, 2)),    # ABGR8888
    ("RX24", (3, 2, 1)),    # RGBX8888
    ("RA24", (3, 2, 1)),    # RGBA8888
    ("BX24", (1, 2, 3)),    # BGRX8888
    ("BA24", (1, 2, 3)),    # BGRA8888
])
def test_every_packed32_variant(fourcc, expected):
    assert _tool().rgb_offsets(fourcc, "little") == expected


def test_big_endian_buffers_mirror_the_offsets():
    """A big-endian buffer holds the same word with its bytes reversed, so each
    offset mirrors across the 4-byte pixel. Assuming little-endian here is how a
    reader silently transposes red and blue on a big-endian host."""
    mod = _tool()
    assert mod.rgb_offsets("XR24", "big") == (1, 2, 3)
    assert mod.rgb_offsets("XB24", "big") == (3, 2, 1)


def test_unsupported_formats_return_none_rather_than_a_guess():
    """XR30 is 32bpp and 10-bit-per-channel: a byte shuffle produces garbage.
    NV12 is planar YUV. Neither may be silently treated as packed RGB."""
    mod = _tool()
    assert mod.rgb_offsets("XR30", "little") is None
    assert mod.rgb_offsets("NV12", "little") is None


def test_the_conversion_is_a_slice_not_a_per_pixel_loop():
    """The tearing fix. A per-pixel Python loop over the LIVE scanout took 4.67s
    on the box (1.44M iterations), during which the compositor flipped ~280
    times and the capture came out stitched from many frames. The snapshot must
    be one bulk read and the conversion must use C-level slice steps."""
    src = open(TOOL, encoding="utf-8").read()
    assert "raw = mm[:]" in src, "the whole buffer must be snapshotted in one memcpy"
    assert "line[0::3] = row[r_off::4]" in src, "conversion must be slice-stepped"
    convert = src.split("rows = []", 1)[1].split("def ", 1)[0]
    assert "for x in range" not in convert, (
        "a per-pixel Python loop is what made captures tear")


def test_non_linear_modifiers_are_refused_by_name():
    """Reading a Y-tiled/CCS/AFBC buffer linearly is noise that looks exactly
    like a compositor bug. It must fail loudly with the modifier value."""
    src = open(TOOL, encoding="utf-8").read()
    assert "DRM_FORMAT_MOD_LINEAR" in src
    assert "would produce noise" in src
