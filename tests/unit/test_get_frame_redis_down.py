"""A down Redis means "no camera frame", never a Redis error.

Live 2026-09-13: get_user_camera_inp failed 246 times with "Error 10061
connecting to localhost:6379".  hartos/helper.py get_frame falls through the
FrameStore to the legacy Redis read and calls redis_client.get() unguarded --
the client is built lazily, so it is never None on a box with no Redis.  The
exception escaped the tool; two agents then drove the desktop to "start
Redis", and one told the user Redis Sentinel had started.  A refused connect
costs 4.07 s on this box (measured, redis-py 4.6.0), so a Redis that is down
must not be dialled on every call either.

    python -m pytest tests/unit/test_get_frame_redis_down.py -q
"""
import pickle
from unittest.mock import patch

import pytest

np = pytest.importorskip('numpy')
redis = pytest.importorskip('redis')
flask = pytest.importorskip('flask')

@pytest.fixture
def helper():
    # Each test patches in its own client; the breaker is keyed by client, so
    # no test inherits another's open breaker.
    import hartos.helper as h
    app = flask.Flask('get_frame_redis_down')
    with app.app_context():
        # No in-process FrameStore, so the Redis fallback is what runs.
        with patch('core.safe_hartos_attr.safe_hartos_attr',
                   return_value=None):
            yield h


def test_one_clients_open_breaker_does_not_block_another(helper):
    with patch.object(helper, 'redis_client') as dead:
        dead.get.side_effect = _refused()
        assert helper.get_frame('u-dead') is None
    frame = np.random.randint(0, 255, (8, 8, 3), dtype=np.uint8)
    with patch.object(helper, 'redis_client') as live:
        live.get.return_value = pickle.dumps(frame)
        assert helper.get_frame('u-live2') is not None


def _refused():
    return redis.exceptions.ConnectionError(
        'Error 10061 connecting to localhost:6379. No connection could be '
        'made because the target machine actively refused it.')


def test_refused_redis_means_no_frame(helper):
    with patch.object(helper, 'redis_client') as rc:
        rc.get.side_effect = _refused()
        assert helper.get_frame('u-redis-down') is None


def test_camera_tool_reports_no_camera_frame_not_a_redis_error(helper):
    # No frame is a failure of the camera question (it raises, and
    # core.tool_logging turns that into the failure envelope; see
    # test_camera_tool_no_frame_is_a_failure.py), but what it says is the
    # camera, never the Redis connect that failed underneath.
    with patch.object(helper, 'redis_client') as rc:
        rc.get.side_effect = _refused()
        with pytest.raises(RuntimeError) as err:
            helper.get_user_camera_inp('what do you see?', 7, 'r-1')
    assert not isinstance(err.value, redis.RedisError)
    assert 'camera' in str(err.value).lower()
    assert 'redis' not in str(err.value).lower()
    assert '10061' not in str(err.value)


def test_a_refused_redis_is_not_dialled_again_inside_the_cooldown(helper):
    with patch.object(helper, 'redis_client') as rc:
        rc.get.side_effect = _refused()
        for uid in ('u-a', 'u-b', 'u-c'):
            assert helper.get_frame(uid) is None
        assert rc.get.call_count == 1, (
            "a Redis that refused is dialled again on every frame lookup, "
            "4 s each on Windows")


def test_a_live_redis_still_serves_its_frame(helper):
    frame = np.random.randint(0, 255, (48, 64, 3), dtype=np.uint8)
    with patch.object(helper, 'redis_client') as rc:
        rc.get.return_value = pickle.dumps(frame)
        got = helper.get_frame('u-live')
    assert got is not None and got.shape == (48, 64, 3)


def test_a_live_redis_with_no_frame_does_not_trip_the_breaker(helper):
    with patch.object(helper, 'redis_client') as rc:
        rc.get.return_value = None
        assert helper.get_frame('u-empty') is None
        assert helper.get_frame('u-empty') is None
        assert rc.get.call_count == 2
