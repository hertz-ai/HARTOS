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
