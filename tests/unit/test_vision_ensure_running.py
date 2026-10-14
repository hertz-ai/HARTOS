"""VisionBackend.ensure_running: the caption server's existing start, public.

Qwen08BBackend's lazy start (the 'vlm_caption.requested' event Nunba answers
with llama_config.start_caption_server, then the standalone launch) used to
be the private _ensure_running, and Reel Studio's score_moments reached it
with a getattr probe.  It is now the public VisionBackend.ensure_running, and
its three callers -- describe(), read_document() and score_moments -- each
ask it.  A backend with no server to start (it loads in start(), or talks to
a server something else runs, or has no model) is ready: True.

``on_wait`` (review of 42735064e) is told only when the start has to wait
for, or start, a server -- never when it is already up -- so a reel's chat
says "Getting the vision model ready" only then; a callback that raises never
skips the start it announces.

Only the boundaries are replaced: the caption server's HTTP (pooled_get /
pooled_post), the platform event, the standalone launcher's binary lookup,
the process spawn and sleep.

    python -m pytest tests/unit/test_vision_ensure_running.py -q -p no:cacheprovider
"""
import io
import json
from unittest.mock import MagicMock, patch

import pytest
import requests

import integrations.vision.lightweight_backend as lvb
from integrations.reel_studio import contracts as C
from integrations.reel_studio.contracts import Box, Candidate, Quality
from integrations.reel_studio.media import moments as M
from tests.conftest import refuse_all_network

STANDALONE_BINARY = ('integrations.service_tools.model_lifecycle.'
                     'ModelLifecycleManager._find_llama_server_binary')
PORT = 9555


@pytest.fixture(autouse=True)
def _no_network(monkeypatch):
    refuse_all_network(monkeypatch, reason='test_vision_ensure_running: HTTP is mocked')


def _jpeg(size=(96, 160), shade=60):
    from PIL import Image
    buf = io.BytesIO()
    Image.new('RGB', size, (shade, 40, 90)).save(buf, 'JPEG')
    return buf.getvalue()


def _answer(content='a bride at a mirror'):
    resp = MagicMock(status_code=200)
    resp.json.return_value = {'choices': [{'finish_reason': 'stop',
                                           'message': {'content': content}}]}
    return resp


def _health(up):
    if not up:
        return dict(side_effect=requests.ConnectionError('refused'))
    return dict(return_value=MagicMock(status_code=200))


# ── the three callers reach it ────────────────────────────────────────────

@pytest.mark.parametrize('ready', [False, True])
def test_describe_asks_ensure_running_before_it_sends_a_frame(ready):
    backend = lvb.Qwen08BBackend(port=PORT)
    with patch.object(lvb.Qwen08BBackend, 'ensure_running', autospec=True,
                      return_value=ready) as ensure, \
         patch.object(lvb, 'pooled_post', return_value=_answer()) as post:
        out = backend.describe(_jpeg(), 'what is here?')
    assert ensure.call_count == 1 and ensure.call_args.args[0] is backend
    if ready:
        assert out == 'a bride at a mirror'
        assert post.call_args.args[0] == f'http://127.0.0.1:{PORT}/v1/chat/completions'
    else:
        assert out is None
        post.assert_not_called()


@pytest.mark.parametrize('ready', [False, True])
def test_read_document_asks_ensure_running_before_it_sends_a_page(ready):
    backend = lvb.Qwen08BBackend(port=PORT)
    with patch.object(lvb.Qwen08BBackend, 'ensure_running', autospec=True,
                      return_value=ready) as ensure, \
         patch.object(lvb, 'pooled_post', return_value=_answer('page text')) as post:
        out = backend.read_document(_jpeg((600, 800)), 'read every line')
    assert ensure.call_count == 1 and ensure.call_args.args[0] is backend
    assert out == ('page text' if ready else None)
    assert post.call_count == (1 if ready else 0)


class _Rater(lvb.NoneBackend):
    """A vision backend that rates every frame and records each start it is
    asked for.  It names ensure_running, the public method, and nothing
    private: score_moments must reach it as every caller does."""
    name = 'rater'

    def __init__(self):
        self.starts = []

    def ensure_running(self, on_wait=None):
        self.starts.append(on_wait)
        return True

    def rate_frame(self, image_bytes, prompt, **kw):
        return json.dumps({'stage': 'eyes', 'appeal': 7, 'face_visible': True})


def _keys(n):
    return [Candidate(clip='a' * 64, t_s=float(i + 1), segment=0, jpeg=_jpeg(shade=30 + 50 * i),
                      quality=Quality(score=0.5, sharp=100.0, luma=0.5, clipped=0.0),
                      face=Box(0.4, 0.2, 0.2, 0.15))
            for i in range(n)]


def test_score_moments_asks_the_backends_ensure_running_once_per_reel(tmp_path):
    backend = _Rater()
    said = []
    doc = M.score_moments(_keys(3), backend=backend, cache_dir=str(tmp_path),
                          on_start=lambda: said.append('readying'))
    assert C.validate_moments(doc) == []
    assert len(backend.starts) == 1, 'score_moments never asked the public ensure_running'
    # The reel's progress line rides the start, to be said only if it waits.
    assert callable(backend.starts[0])
    assert said == [], 'the backend had nothing to wait for, yet her chat was told it did'
    assert doc['src'] == 'vlm' and doc['stats']['vlm'] == 3


# ── a backend with no server to start ─────────────────────────────────────

@pytest.mark.parametrize('cls', [lvb.MiniCPMBackend, lvb.MobileVLMBackend, lvb.CLIPBackend,
                                 lvb.Qwen3VLVisionBackend, lvb.NoneBackend])
def test_a_backend_with_no_server_to_start_is_ready_and_waits_for_nothing(cls):
    waited = []
    with patch.object(lvb, 'pooled_get') as get, \
         patch.object(lvb, 'pooled_post') as post, \
         patch('subprocess.Popen') as popen, \
         patch('core.platform.events.emit_event') as emit, \
         patch('time.sleep') as sleep:
        backend = cls()
        assert backend.ensure_running() is True
        assert backend.ensure_running(on_wait=lambda: waited.append(1)) is True
    assert waited == []
    for boundary in (get, post, popen, emit, sleep):
        boundary.assert_not_called()


# ── the caption server: on_wait only when it has to wait ──────────────────

class _Port:
    """The caption port: ``up`` answers /health 200; Nunba's subscriber
    brings it up on 'vlm_caption.requested' when ``nunba`` is True.
    ``log`` records the order of on_wait and the event."""

    def __init__(self, up=False, nunba=True):
        self.up, self.nunba, self.log = up, nunba, []

    def health(self, url, timeout=None, **kw):
        if not self.up:
            raise requests.ConnectionError('refused')
        return MagicMock(status_code=200)

    def emit(self, topic, data=None, *a, **kw):
        self.log.append(topic)
        if topic == 'vlm_caption.requested' and self.nunba:
            self.up = True


def _ensure(port, backend=None, on_wait=None):
    backend = backend or lvb.Qwen08BBackend(port=PORT)
    with patch.object(lvb, 'pooled_get', side_effect=port.health), \
         patch('core.platform.events.emit_event', side_effect=port.emit), \
         patch(STANDALONE_BINARY, return_value=None), \
         patch('subprocess.Popen') as popen, \
         patch('time.sleep'):
        ok = backend.ensure_running(on_wait=on_wait)
    popen.assert_not_called()
    return ok


def test_a_server_already_up_is_ready_and_nobody_is_told_to_wait():
    port = _Port(up=True)
    assert _ensure(port, on_wait=lambda: port.log.append('on_wait')) is True
    assert port.log == []


def test_a_server_that_must_be_started_tells_on_wait_first_then_asks_for_it():
    port = _Port(up=False, nunba=True)
    assert _ensure(port, on_wait=lambda: port.log.append('on_wait')) is True
    assert port.log == ['on_wait', 'vlm_caption.requested']


def test_an_on_wait_that_raises_never_skips_the_start():
    """Review of 42735064e: the progress line sat inside the try that
    guarded the start, so a progress line that failed skipped the start."""
    port = _Port(up=False, nunba=True)

    def broken():
        port.log.append('on_wait')
        raise RuntimeError('her chat is unreachable')
    assert _ensure(port, on_wait=broken) is True
    assert port.log == ['on_wait', 'vlm_caption.requested']


def test_a_start_in_its_cooldown_waits_for_nothing_and_tells_nobody():
    port = _Port(up=False, nunba=False)
    backend = lvb.Qwen08BBackend(port=PORT)
    assert _ensure(port, backend) is False          # nobody started it: cooldown begins
    port.log.clear()
    assert _ensure(port, backend, on_wait=lambda: port.log.append('on_wait')) is False
    assert port.log == []
