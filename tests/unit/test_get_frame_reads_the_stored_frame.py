"""The camera tool reads the frame the camera stored.

Live 2026-11-10 (Nunba build 780c432a, HARTOS ca0852c0f): a camera session
for the owner was open 12:39:36-12:41:18, the description loop logged
"cam_frame=yes" at 12:40:03.191, and get_user_camera_inp failed at
12:40:03.377 with "this computer's Nunba window is not sending camera frames
yet", five times in a minute.  The frames were in the FrameStore.
hartos/helper.py get_frame decoded them with ``np.frombuffer`` and the module
never bound ``np`` (not since 3200888d3, 2026-08-30), so every stored frame
raised NameError inside ``except Exception: pass`` and fell through to Redis,
which a desktop does not run.  The Redis-down tests stub the store away, so
no test ever ran this branch.

    python -m pytest tests/unit/test_get_frame_reads_the_stored_frame.py -q
"""
import logging
from unittest.mock import patch

import pytest

np = pytest.importorskip('numpy')
cv2 = pytest.importorskip('cv2')
flask = pytest.importorskip('flask')


def _jpeg(height=48, width=64, bgr=(255, 0, 0)):
    """A solid-colour JPEG, as the frame receiver stores one (BGR in)."""
    frame = np.zeros((height, width, 3), dtype=np.uint8)
    frame[:, :] = bgr
    ok, encoded = cv2.imencode('.jpg', frame)
    assert ok
    return encoded.tobytes()


@pytest.fixture
def store():
    from integrations.vision.frame_store import FrameStore
    with patch('core.ai_sensing.allowed', return_value=True):
        yield FrameStore()


@pytest.fixture
def helper(store):
    """hartos.helper inside an app context, with the node's FrameStore
    reachable the way a desktop reaches it (safe_hartos_attr) and a Redis
    that would fail the test if it were dialled."""
    import hartos.helper as h
    app = flask.Flask('get_frame_reads_the_stored_frame')
    with app.app_context():
        with patch('core.safe_hartos_attr.safe_hartos_attr',
                   side_effect=lambda name: (lambda: store) if name == 'get_frame_store' else None):
            with patch.object(h, 'redis_client') as rc:
                rc.get.side_effect = AssertionError('Redis dialled though the store had a frame')
                yield h


def test_a_stored_camera_frame_reaches_the_camera_tool(helper, store):
    store.put_frame('10202', _jpeg(48, 64))
    frame = helper.get_frame('10202')
    assert frame is not None
    assert frame.shape == (48, 64, 3)
    assert helper.redis_client.get.call_count == 0


def test_the_frame_comes_back_in_rgb(helper, store):
    """The store holds BGR JPEGs; the tool's callers take RGB."""
    store.put_frame('10202', _jpeg(bgr=(255, 0, 0)))        # pure blue, BGR
    frame = helper.get_frame('10202')
    blue, red = int(frame[10, 10, 2]), int(frame[10, 10, 0])
    assert blue > 200 and red < 50


def test_a_store_handed_in_directly_is_read_too(helper):
    """The embedded, headless path: the caller passes its own store."""
    from integrations.vision.frame_store import FrameStore
    with patch('core.ai_sensing.allowed', return_value=True):
        own = FrameStore()
        own.put_frame('device-1', _jpeg(32, 40))
        frame = helper.get_frame('device-1', frame_store=own)
    assert frame is not None and frame.shape == (32, 40, 3)


def test_another_accounts_frame_is_not_this_accounts(helper, store):
    store.put_frame('10202', _jpeg())
    with patch.object(helper, 'redis_client') as rc:
        rc.get.return_value = None
        assert helper.get_frame('someone-else') is None


def test_a_stored_frame_that_cannot_be_read_is_logged_not_swallowed(helper, store, caplog):
    """The guard around the store read hid the NameError for ten weeks.  A
    failure there is now a warning that names the account."""
    store.put_frame('10202', _jpeg())
    with patch.object(helper, '_decode_stored_frame', side_effect=RuntimeError('decode broke')):
        with patch.object(helper, 'redis_client') as rc:
            rc.get.return_value = None
            with caplog.at_level(logging.WARNING):
                assert helper.get_frame('10202') is None
    assert any('could not be read' in r.getMessage() and '10202' in r.getMessage()
               for r in caplog.records)


def test_the_decode_needs_no_module_level_name(helper):
    """The decode imports what it uses: the module binding ``np`` or not
    changes nothing."""
    assert helper._decode_stored_frame(_jpeg(16, 24)).shape == (16, 24, 3)
    assert helper._decode_stored_frame(b'not a jpeg') is None
