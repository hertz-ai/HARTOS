"""A camera frame larger than an entry point keeps is shrunk to caption size.

The step was Nunba's (routes/chatbot_routes._shrink_frame, for the phone
frames its /api/vision/frame answered 413, live 13 Nov 2026).  It is
frame_store.shrink_frame now, and these are the behaviours that route had:
at most 640 px on the long side, the shape kept, a small frame never
enlarged, a body that is no image answered None, and the JPEG quality the
caller asks for.

Real OpenCV, real JPEGs.

    python -m pytest tests/unit/test_a_large_frame_is_shrunk_to_caption_size.py -q
"""
import cv2
import numpy as np
import pytest

from integrations.vision import frame_store
from integrations.vision.frame_store import shrink_frame


def _jpeg(width, height, quality=95, seed=7):
    """A JPEG of noise: it does not compress, so it is as heavy as a phone
    camera's own frame."""
    rng = np.random.default_rng(seed)
    img = rng.integers(0, 256, size=(height, width, 3), dtype=np.uint8)
    ok, buf = cv2.imencode('.jpg', img, [cv2.IMWRITE_JPEG_QUALITY, quality])
    assert ok
    return buf.tobytes()


def _size(jpeg):
    img = cv2.imdecode(np.frombuffer(jpeg, np.uint8), cv2.IMREAD_COLOR)
    assert img is not None
    h, w = img.shape[:2]
    return w, h


def _about(size, expected):
    """The sizes agree to the pixel a float scale may round away."""
    return all(e - 1 <= s <= e for s, e in zip(size, expected))


@pytest.mark.parametrize('width, height, expected', [
    (1920, 1080, (640, 360)),      # landscape: the width is the long side
    (1080, 1920, (360, 640)),      # a phone held upright: the height is
    (1280, 1280, (640, 640)),
    (641, 100, (640, 99)),         # one pixel over
])
def test_a_large_frame_comes_back_at_most_640_on_its_long_side(width, height, expected):
    big = _jpeg(width, height)
    small = shrink_frame(big)
    assert small[:2] == b'\xff\xd8'            # a JPEG
    assert _about(_size(small), expected), _size(small)
    assert max(_size(small)) <= 640
    assert len(small) < len(big)


@pytest.mark.parametrize('width, height', [(640, 480), (480, 640), (320, 240), (1, 1)])
def test_a_frame_that_is_small_enough_keeps_its_size(width, height):
    assert _size(shrink_frame(_jpeg(width, height))) == (width, height)


def test_the_caller_names_the_long_side_and_the_quality():
    big = _jpeg(1920, 1080)
    assert _about(_size(shrink_frame(big, max_side=320)), (320, 180))
    fine = shrink_frame(big, quality=95)
    coarse = shrink_frame(big, quality=30)
    assert _size(fine) == _size(coarse) and _about(_size(fine), (640, 360))
    assert len(coarse) < len(shrink_frame(big)) < len(fine)     # default is 70


@pytest.mark.parametrize('body', [b'not an image at all' * 100,
                                  b'\xff\xd8\xff\xe0' + b'cut short'])
def test_a_body_that_is_no_image_is_none(body):
    assert shrink_frame(body) is None


def test_a_very_thin_frame_keeps_at_least_one_pixel():
    thin = _size(shrink_frame(_jpeg(4000, 2)))
    assert thin[1] == 1 and 639 <= thin[0] <= 640


# ── bytes that used to raise, and a size claimed but never sent ────────
# (review of c41ba37a4: the route answered 500 where it says 413, and a
# 656 KB body claiming 8000x8000 cost 0.5 s and 355 MB)

def _claiming(width, height, pad_to=0):
    """A small real JPEG whose header claims ``width`` x ``height``, padded
    with comment segments to ``pad_to`` bytes."""
    ok, buf = cv2.imencode('.jpg', np.full((8, 8, 3), 128, np.uint8))
    assert ok
    b = bytearray(buf.tobytes())
    sof = b.find(b'\xff\xc0')
    b[sof + 5:sof + 7] = height.to_bytes(2, 'big')
    b[sof + 7:sof + 9] = width.to_bytes(2, 'big')
    comment = b'\xff\xfe' + (65535).to_bytes(2, 'big') + b'\x00' * 65533
    return bytes(b[:2]) + comment * (pad_to // len(comment)) + bytes(b[2:])


def _png_claiming(width, height):
    """A PNG whose header (checksum and all) claims ``width`` x ``height``
    and carries no picture."""
    import struct
    import zlib

    def chunk(kind, data):
        return (struct.pack('>I', len(data)) + kind + data
                + struct.pack('>I', zlib.crc32(kind + data)))
    return (b'\x89PNG\r\n\x1a\n'
            + chunk(b'IHDR', struct.pack('>IIBBBBB', width, height, 8, 2, 0, 0, 0))
            + chunk(b'IDAT', zlib.compress(b'\x00' * 16)) + chunk(b'IEND', b''))


_CAP = 8192 * 6144        # frame_store.MAX_FRAME_PIXELS, written out
_SMALL = cv2.imencode('.jpg', np.full((48, 64, 3), 128, np.uint8))[1].tobytes()


def _the_step_before(frame_bytes, max_side=640, quality=70):
    """shrink_frame as c41ba37a4 wrote it, for the bytes it must still give."""
    img = cv2.imdecode(np.frombuffer(frame_bytes, np.uint8), cv2.IMREAD_COLOR)
    if img is None:
        return None
    h, w = img.shape[:2]
    scale = min(1.0, float(max_side) / max(h, w))
    if scale < 1.0:
        img = cv2.resize(img, (max(1, int(w * scale)), max(1, int(h * scale))),
                         interpolation=cv2.INTER_AREA)
    ok, buf = cv2.imencode('.jpg', img, [cv2.IMWRITE_JPEG_QUALITY, quality])
    return buf.tobytes() if ok else None


def test_no_bytes_at_all_is_none():
    assert shrink_frame(b'') is None


@pytest.mark.parametrize('width, height', [
    (40000, 40000),         # past OpenCV's own limit: it raised
    (65535, 65535),         # the most a JPEG header can say
    (8000, 8000),           # the reviewer's: decoded, 355 MB
    (8192, 6145),           # one row over the cap
    (60000, 900),
])
def test_a_frame_claiming_more_pixels_than_a_camera_gives_is_none(width, height):
    assert width * height > _CAP
    assert shrink_frame(_claiming(width, height, pad_to=600_000)) is None
    assert shrink_frame(_png_claiming(width, height)) is None


def test_a_claim_past_the_cap_is_refused_before_anything_is_decoded(monkeypatch):
    def decoded(*_a, **_k):
        raise AssertionError('decoded a frame whose claim is past the cap')
    monkeypatch.setattr(cv2, 'imdecode', decoded)
    assert shrink_frame(_claiming(8000, 8000, pad_to=600_000)) is None
    assert shrink_frame(_png_claiming(30000, 30000)) is None


def test_the_cap_is_the_largest_50_megapixel_frame_and_it_still_decodes(monkeypatch):
    """The cap is on the claim: at 8192x6144 the bytes go to the decoder, one
    row more and they do not.  (The decoder is stood in for here so the test
    does not hold 150 MB; the real one runs in every other test.)"""
    assert frame_store.MAX_FRAME_PIXELS == _CAP
    seen = []

    def decoder(buf, flags):
        seen.append(len(buf))
        return np.full((6144, 8192, 3), 128, np.uint8)[:60, :80].copy()
    monkeypatch.setattr(cv2, 'imdecode', decoder)
    at_cap = _claiming(8192, 6144)
    assert shrink_frame(at_cap)[:2] == b'\xff\xd8' and seen == [len(at_cap)]
    assert shrink_frame(_claiming(6144, 8192))[:2] == b'\xff\xd8'      # upright
    assert shrink_frame(_claiming(8192, 6145)) is None and len(seen) == 2


def _bmp_claiming(width, height):
    """A bitmap header claiming ``width`` x ``height``, and no picture."""
    import struct
    row = (width * 3 + 3) & ~3
    return (b'BM' + struct.pack('<IHHI', 54 + row * height, 0, 0, 54)
            + struct.pack('<IiiHHIIiiII', 40, width, height, 1, 24, 0,
                          row * height, 2835, 2835, 0, 0))


@pytest.mark.parametrize('width, height', [((1 << 20) + 1, 1), (1, (1 << 20) + 1)])
def test_bytes_opencv_refuses_are_none_not_an_error(width, height):
    """Under the pixel cap and still past what OpenCV takes (a side over
    2^20): it raised cv2.error, and the route answered 500.  None now."""
    long = _bmp_claiming(width, height)
    assert width * height <= _CAP
    try:
        _the_step_before(long)
        raised = False
    except cv2.error:
        raised = True
    assert raised, 'OpenCV took these bytes: the test needs others'
    assert shrink_frame(long) is None


def test_an_error_while_shrinking_or_encoding_is_none_too(monkeypatch):
    def refuses(*_a, **_k):
        raise cv2.error('refused')
    monkeypatch.setattr(cv2, 'resize', refuses)
    assert shrink_frame(_jpeg(1920, 1080)) is None
    monkeypatch.undo()
    monkeypatch.setattr(cv2, 'imencode', refuses)
    assert shrink_frame(_SMALL) is None


def test_every_frame_it_took_before_comes_back_byte_for_byte():
    """The cap and the two None answers change nothing else: the same bytes
    out as the step gave before, for a camera's frames in the shapes and
    formats it took."""
    rng = np.random.default_rng(11)
    noise = rng.integers(0, 256, size=(1000, 1500, 3), dtype=np.uint8)
    gray = rng.integers(0, 256, size=(1200, 1600), dtype=np.uint8)
    # a 12 megapixel camera still (4000x3000), the largest frame a phone
    # sends by default: a smooth picture, so the test stays light
    still = np.tile(np.linspace(0, 255, 4000, dtype=np.uint8)[None, :, None],
                    (3000, 1, 3))
    frames = {
        'large': _jpeg(1920, 1080), 'upright': _jpeg(1080, 1920),
        'small': _jpeg(320, 240), 'exact': _jpeg(640, 480),
        'thin': _jpeg(4000, 2), 'one pixel': _jpeg(1, 1),
        'png': cv2.imencode('.png', noise)[1].tobytes(),
        'bmp': cv2.imencode('.bmp', noise)[1].tobytes(),
        'webp': cv2.imencode('.webp', noise)[1].tobytes(),
        'gray jpeg': cv2.imencode('.jpg', gray)[1].tobytes(),
        'gray png': cv2.imencode('.png', gray)[1].tobytes(),
        '12 megapixel still': cv2.imencode('.jpg', still)[1].tobytes(),
        'not an image': b'not an image at all' * 100,
        'jpeg cut short': b'\xff\xd8\xff\xe0' + b'cut short',
    }
    for name, frame in frames.items():
        assert shrink_frame(frame) == _the_step_before(frame), name
        assert shrink_frame(frame, max_side=320, quality=40) == _the_step_before(
            frame, max_side=320, quality=40), name
    assert shrink_frame(frames['12 megapixel still'])[:2] == b'\xff\xd8'
    # a frame cut off part way still gives the part that arrived
    assert shrink_frame(_jpeg(1920, 1080)[:20000])[:2] == b'\xff\xd8'


def test_bytes_whose_header_gives_no_size_are_not_decoded(monkeypatch):
    """The size is read from the header before the decoder runs.  Where no
    size can be read (a format the header reader does not know: here a
    Radiance .hdr picture, which OpenCV alone decodes) there is nothing to
    hold a claim to, so the bytes are answered None and never decoded.  No
    camera sends one: frames are JPEGs."""
    ok, buf = cv2.imencode('.hdr', np.full((48, 64, 3), 0.5, np.float32))
    assert ok
    picture = buf.tobytes()
    assert _the_step_before(picture) is not None     # OpenCV does decode it

    def decoded(*_a, **_k):
        raise AssertionError('decoded bytes whose size was never read')
    monkeypatch.setattr(cv2, 'imdecode', decoded)
    assert shrink_frame(picture) is None
