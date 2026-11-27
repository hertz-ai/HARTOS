"""Generate_video (avatar) reports a video only when the video service took the job.

Walk 27 Nov step 11 (msg_1795728921734_t7qoi3): on a desktop the database URL is this Nunba, which has
no /video_generate_save; the POST reached nothing, its failure was swallowed, and the tool still said
"Video Generation completed", so the agent told a child "Here is a video" with none shown.
"""
from unittest.mock import MagicMock, patch

import pytest

from core import teacher_avatar as ta

DB = 'http://localhost:5000'


def _generate_video():
    from core import agent_tools
    ctx = {
        'user_id': 'u1', 'prompt_id': 'p1', 'agent_data': {},
        'helper_fun': MagicMock(), 'user_prompt': 'u1_p1',
        'request_id_list': {}, 'recent_file_id': {}, 'scheduler': MagicMock(),
        'send_message_to_user1': MagicMock(), 'retrieve_json': MagicMock(),
        'strip_json_values': MagicMock(),
        'save_conversation_db': MagicMock(return_value=77),
        'log_tool_execution': lambda f: f,
    }
    tools = agent_tools.build_core_tool_closures(ctx)
    return next(fn for name, _desc, fn in tools if name == 'Generate_video')


class _Resp:
    def __init__(self, status):
        self.status_code = status


def _run(post, realtime=True):
    generate = _generate_video()
    no_avatar = {'image_url': None, 'voice_id': None, 'audio_sample_url': None, 'openvoice': True}
    with patch.object(ta, 'lookup_avatar', lambda *a, **k: dict(no_avatar)), \
            patch('core.http_pool.pooled_post_once', post), \
            patch('core.config_cache.get_db_url', return_value=DB):
        return generate(text='hello', avatar_id=5, realtime=realtime)


def test_an_http_error_is_never_reported_as_a_video():
    out = _run(lambda *a, **k: _Resp(404))
    assert out.startswith('No video was made')


def test_a_job_the_service_took_is_reported_as_before():
    assert _run(lambda *a, **k: _Resp(200)).startswith('Video Generation completed with conv_id:77')
    assert _run(lambda *a, **k: _Resp(200), realtime=False).startswith(
        'Video Generation task added to queue with conv_id:77')


# ── the real transport, with the wire replaced below the HTTP adapter (review rv_face2's probe) ──
# HTTPConnectionPool._make_request is where every send arrives, so the adapter's own Retry and its
# exception mapping run for real and nothing leaves the machine.
REMOTE = 'https://central.invalid'   # a store the shared session mounts on its RETRYING adapter
LOOPBACK = 'http://localhost:5000'   # the desktop's own store


def _wire(monkeypatch, how):
    from urllib3.connectionpool import HTTPConnectionPool
    from urllib3.exceptions import NewConnectionError, ReadTimeoutError
    sends = []

    def make_request(self, conn, method, url, *a, **k):
        sends.append((self.scheme, self.host, method, url))
        if how == 'read_timeout':
            raise ReadTimeoutError(self, url, 'Read timed out (a slow video service)')
        raise NewConnectionError(conn, 'connection refused (nothing listens)')
    monkeypatch.setattr(HTTPConnectionPool, '_make_request', make_request)
    return sends


def _run_on(store, realtime=True):
    generate = _generate_video()
    no_avatar = {'image_url': None, 'voice_id': None, 'audio_sample_url': None, 'openvoice': True}
    with patch.object(ta, 'lookup_avatar', lambda *a, **k: dict(no_avatar)), \
            patch('core.config_cache.get_db_url', return_value=store):
        return generate(text='hello', avatar_id=5, realtime=realtime)


@pytest.mark.parametrize('store', [REMOTE, LOOPBACK])
def test_a_slow_service_gets_the_job_once_and_it_is_reported(monkeypatch, store):
    """The 1 s timeout is a send, not a wait: the service has the job.  Sent ONCE (pooled_post_once):
    the shared session's retrying adapter posted a paid job three times (rv_face2 C)."""
    sends = _wire(monkeypatch, 'read_timeout')
    out = _run_on(store)
    assert len(sends) == 1
    assert out.startswith('Video Generation completed with conv_id:77')


@pytest.mark.parametrize('store', [REMOTE, LOOPBACK])
def test_a_service_that_is_not_there_is_never_reported_as_a_video(monkeypatch, store):
    sends = _wire(monkeypatch, 'refused')
    out = _run_on(store)
    assert len(sends) == 1
    assert out.startswith('No video was made') and 'completed' not in out


