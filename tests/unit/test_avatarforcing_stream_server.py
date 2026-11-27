"""The AvatarForcing sidecar's session rules (servers/avatarforcing/stream_server.py), without
the model: torch, PyAV, OpenCV and the engine are stubbed, the HTTP handler is the real one.

Pinned here:
  * a finished reply's stream is remembered by (portrait version, audio) and the same words in
    the same face are replayed from memory with no generation -- only a complete stream, never
    a stopped or broken one; a re-saved portrait or other words never match; the memory is
    bounded;
  * DELETE (a viewer leaving) stops generation only when no other viewer is watching (another
    tab, the same user's phone).
"""
import http.client
import importlib.util
import json
import os
import sys
import threading
import types
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import MagicMock, patch

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from tests.unit.module_swap import swap_modules  # noqa: E402

SERVER = (Path(__file__).resolve().parents[2] / 'integrations' / 'service_tools' / 'servers'
          / 'avatarforcing' / 'stream_server.py')
AUDIO = np.linspace(-0.5, 0.5, 16000, dtype=np.float32)  # one second


@pytest.fixture(scope='module')
def ss(tmp_path_factory):
    """stream_server imported with its heavy imports stubbed; cwd and sys.path restored."""
    rl = types.ModuleType('realtime_lowvram')
    rl.FPS, rl.SR, rl.SPF = 25, 16000, 640
    rl.FaceCropper = MagicMock()
    rl.load_audio = lambda path: AUDIO.copy()
    rl.load_video_25fps = rl.to_tensor = MagicMock()
    stubs = {name: MagicMock() for name in ('av', 'cv2', 'torch', 'omegaconf', 'lowvram',
                                            'lowvram.engine', 'lowvram.session')}
    stubs['realtime_lowvram'] = rl
    cwd, path = os.getcwd(), list(sys.path)
    model_dir = tmp_path_factory.mktemp('avatarforcing_model')
    try:
        with swap_modules(stubs), \
                patch.dict(os.environ, {'AVATARFORCING_MODEL_DIR': str(model_dir)}):
            spec = importlib.util.spec_from_file_location('_stream_server_under_test', SERVER)
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
    finally:
        os.chdir(cwd)
        sys.path[:] = path
    return mod


@pytest.fixture(autouse=True)
def fresh(ss):
    ss.SESSIONS.clear()
    ss.STREAM_MEMO.clear()
    yield
    ss.SESSIONS.clear()
    ss.STREAM_MEMO.clear()


@pytest.fixture
def face(tmp_path):
    p = tmp_path / 'face.png'
    p.write_bytes(b'png-1')
    return p


@pytest.fixture
def reply(tmp_path):
    p = tmp_path / 'reply.wav'
    p.write_bytes(b'wav')
    return p


def _session(ss, reply, face):
    return ss.new_session({}, reply.as_uri(), face.as_uri())


# ── the same words in the same face are replayed, with no GPU ──────────────────

def test_a_reply_heard_before_in_this_face_is_replayed(ss, reply, face):
    first = _session(ss, reply, face)
    assert first['buf'] is None  # never heard: it will be generated
    ss.remember_stream(first['memo'], [b'moov', b'frag'])
    again = _session(ss, reply, face)
    buf = again['buf']
    assert buf is not None and buf.done and buf.complete
    assert buf.chunks == [b'moov', b'frag']
    assert again['sid'] != first['sid']


def test_a_re_saved_portrait_never_matches(ss, reply, face):
    ss.remember_stream(_session(ss, reply, face)['memo'], [b'moov'])
    face.write_bytes(b'png-2, a new photo')
    assert _session(ss, reply, face)['buf'] is None


def test_other_words_never_match(ss, reply, face, monkeypatch):
    ss.remember_stream(_session(ss, reply, face)['memo'], [b'moov'])
    monkeypatch.setattr(ss, 'load_audio', lambda p: AUDIO[::-1].copy())
    assert _session(ss, reply, face)['buf'] is None


def test_the_memory_is_bounded(ss, monkeypatch):
    monkeypatch.setattr(ss, 'STREAM_MEMO_BYTES', 400)
    for i in range(5):
        ss.remember_stream(('face', str(i)), [b'x' * 90])
    assert list(ss.STREAM_MEMO) == [('face', str(i)) for i in (1, 2, 3, 4)]  # the oldest went
    ss.remember_stream(('face', 'long'), [b'x' * 101])  # over a quarter of the room: not kept
    assert ('face', 'long') not in ss.STREAM_MEMO


class TestOnlyACompleteStreamIsRemembered:

    def _generate(self, ss, monkeypatch, reply, face, *, complete=True, cancel=False, frames=None):
        sess = _session(ss, reply, face)
        buf = ss.LiveBuffer()

        def blocks(ctx, b, session, t_req, audio, user_audio, user_frame, T, demo):
            b.write(b'moov')
            b.write(b'frag')
            b.complete = complete
            ss.STATE['frames_sent'] = T if frames is None else frames
            if cancel:
                b.cancel.set()
        monkeypatch.setattr(ss, '_generate_blocks', blocks)
        monkeypatch.setattr(ss, 'face_reference', lambda ctx, path: 'ref')
        ctx = {'eng': MagicMock(), 'cur_ref': 'ref'}
        ss.generate(ctx, buf, 1, 0.0, sess)
        return sess

    def test_complete(self, ss, monkeypatch, reply, face):
        sess = self._generate(ss, monkeypatch, reply, face)
        assert ss.STREAM_MEMO[sess['memo']][0] == [b'moov', b'frag']

    @pytest.mark.parametrize('kw', [{'cancel': True}, {'complete': False}, {'frames': 3}],
                             ids=['every viewer left', 'the encoder died', 'cut short'])
    def test_never_a_stopped_or_broken_one(self, ss, monkeypatch, reply, face, kw):
        self._generate(ss, monkeypatch, reply, face, **kw)
        assert ss.STREAM_MEMO == {}


# ── over HTTP: a replay generates nothing; a stop waits for the last viewer ───

@pytest.fixture
def server(ss):
    srv = ThreadingHTTPServer(('127.0.0.1', 0), ss.make_handler({'ready': True}))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield srv.server_address[1]
    srv.shutdown()
    srv.server_close()


def _request(port, method, path, body=None):
    c = http.client.HTTPConnection('127.0.0.1', port, timeout=5)
    headers = {'Content-Type': 'application/json'} if body is not None else {}
    c.request(method, path, body=None if body is None else json.dumps(body), headers=headers)
    r = c.getresponse()
    data = r.read()
    c.close()
    return r.status, data


def test_a_replayed_stream_is_served_without_generating(ss, server, reply, face, monkeypatch):
    ss.remember_stream(_session(ss, reply, face)['memo'], [b'moov', b'frag'])
    sess = _session(ss, reply, face)
    monkeypatch.setattr(ss, 'generate', MagicMock(side_effect=AssertionError('generated')))
    status, data = _request(server, 'GET', f"/live.mp4?sid={sess['sid']}")
    assert status == 200 and data == b'moovfrag'


@pytest.mark.parametrize('viewers, stopped', [(2, False), (1, True), (0, True)])
def test_a_stop_ends_generation_only_for_the_last_viewer(ss, server, viewers, stopped):
    buf = ss.LiveBuffer()
    buf.viewers = viewers
    ss.SESSIONS['abcdef012345'] = {'sid': 'abcdef012345', 'buf': buf}
    status, data = _request(server, 'DELETE', '/live.mp4?sid=abcdef012345', body={})
    assert status == 200
    assert json.loads(data)['stopped'] is stopped
    assert buf.cancel.is_set() is stopped


def test_a_stop_before_the_stream_started_means_it_never_will(ss, server):
    ss.SESSIONS['abcdef012345'] = {'sid': 'abcdef012345', 'buf': None}
    status, data = _request(server, 'DELETE', '/live.mp4?sid=abcdef012345', body={})
    assert status == 200 and json.loads(data) == {'stopped': True}
    buf = ss.SESSIONS['abcdef012345']['buf']
    assert buf.done and buf.cancel.is_set()
