"""A reel's keyframe ratings reach the vision model's EXISTING lazy start
(amendment A13, owner correction 2026-10-14: "we are creating tasks for
mechanisms which already exist" -- call the mechanism, build none).

Before this, score_moments only ever called rate_frame, which by design never
starts a server (tests/unit/test_vision_rate_frame.py): a reel made while the
0.8B caption server was down rated nothing and nothing asked for the server.
Now score_moments asks the backend's own lazy start -- the one describe() and
read_document() already use, Qwen08BBackend._ensure_running: a
'vlm_caption.requested' event that Nunba's subscriber answers with
llama_config.start_caption_server, then the standalone launch -- ONCE per reel,
before the first frame, with the time it takes counted against the moments
budget.  rate_frame itself still never starts anything.

Real code throughout: get_vision_backend(REEL_VLM_DEFAULT) builds the real
Qwen08BBackend and its real _ensure_running runs.  Only the boundaries are
replaced: the caption server's HTTP (/health and /v1/chat/completions, at
lightweight_backend's pooled_get / pooled_post), the platform event Nunba
subscribes to (core.platform.events.emit_event), the standalone launcher's
binary lookup, and sleep.

    python -m pytest tests/unit/test_reel_vision_ready.py -q -p no:cacheprovider
"""
import io
import json
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
import requests

from integrations.reel_studio import contracts as C
from integrations.reel_studio.contracts import Box, Candidate, Quality
from integrations.reel_studio.media import moments as M
from integrations.vision import lightweight_backend as lvb
from tests.conftest import refuse_all_network
from tests.unit.test_reel_pipeline import (  # noqa: F401  (fixture)
    PID, UID, _fake_production, _small_clip, turn)

STANDALONE_BINARY = ('integrations.service_tools.model_lifecycle.'
                     'ModelLifecycleManager._find_llama_server_binary')


@pytest.fixture(autouse=True)
def _no_network(monkeypatch):
    refuse_all_network(monkeypatch, reason='test_reel_vision_ready: the caption server is mocked')


def _jpeg(shade):
    from PIL import Image
    buf = io.BytesIO()
    Image.new('RGB', (96, 160), (shade, 40, 90)).save(buf, 'JPEG')
    return buf.getvalue()


def _keys(n=3):
    """``n`` keyframes, each a real (distinct) JPEG, in story order."""
    return [Candidate(clip='a' * 64, t_s=float(i + 1), segment=0, jpeg=_jpeg(30 + 60 * i),
                      quality=Quality(score=0.5, sharp=100.0, luma=0.5, clipped=0.0),
                      face=Box(0.4, 0.2, 0.2, 0.15))
            for i in range(n)]


def _answer(stage='eyes'):
    resp = MagicMock(status_code=200)
    resp.json.return_value = {'choices': [{'finish_reason': 'stop', 'message': {
        'content': json.dumps({'stage': stage, 'appeal': 8, 'face_visible': True})}}]}
    return resp


class _CaptionServer:
    """The caption server's port, as lightweight_backend sees it.

    ``up``: answering /health 200.  ``nunba_starts_it``: Nunba's
    'vlm_caption.requested' subscriber (main.py) brings it up when asked.
    ``asks``: every 'vlm_caption.requested' event; ``clock``/``start_s``:
    simulated time the start takes, read by moments' budget."""

    def __init__(self, *, up=False, nunba_starts_it=True, clock=None, start_s=0.0):
        self.up = up
        self.nunba_starts_it = nunba_starts_it
        self.clock = clock
        self.start_s = start_s
        self.asks = []
        self.health_urls = []

    def health(self, url, timeout=None, **kw):
        self.health_urls.append(url)
        if not self.up:
            raise requests.ConnectionError('refused')
        return MagicMock(status_code=200)

    def emit(self, topic, data=None, *a, **kw):
        if topic == 'vlm_caption.requested':
            self.asks.append(data)
            if self.clock is not None:
                self.clock.t += self.start_s
            if self.nunba_starts_it:
                self.up = True


class _Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


@pytest.fixture
def server(monkeypatch, tmp_path):
    """Run score_moments the way a reel does (no backend named: moments'
    REEL_VLM_DEFAULT through get_vision_backend), against ``_CaptionServer``.

    Returns ``score(srv, keys, **kw) -> (doc, lazy_starts, completions)``:
    ``lazy_starts`` counts calls of the backend's real _ensure_running,
    ``completions`` is the mocked chat-completions POST."""
    monkeypatch.delenv('HEVOLVE_VISION_BACKEND', raising=False)
    monkeypatch.setenv('HEVOLVE_VLM_CAPTION_PORT', '18081')

    def score(srv, keys, **kw):
        real = lvb.Qwen08BBackend._ensure_running
        with patch.object(lvb, 'pooled_get', side_effect=srv.health), \
             patch.object(lvb, 'pooled_post', return_value=_answer()) as post, \
             patch.object(lvb.Qwen08BBackend, '_ensure_running', autospec=True,
                          side_effect=real) as lazy, \
             patch('core.platform.events.emit_event', side_effect=srv.emit), \
             patch(STANDALONE_BINARY, return_value=None), \
             patch('subprocess.Popen') as popen, \
             patch('time.sleep'):
            doc = M.score_moments(keys, cache_dir=str(tmp_path / 'cache'), **kw)
        popen.assert_not_called()
        assert C.validate_moments(doc) == [], C.validate_moments(doc)
        return doc, lazy.call_count, post
    return score


# ── the reel reaches the existing lazy start ──────────────────────────────

def test_a_reel_with_the_caption_server_down_asks_for_it_and_is_rated(server):
    """The 0.8B caption server is down when her reel starts.  The reel asks
    for it through the backend's own lazy start (the event Nunba's
    subscriber answers with start_caption_server), and the frames are then
    rated by the model, not the deterministic fallback."""
    srv = _CaptionServer(up=False, nunba_starts_it=True)
    doc, lazy_starts, post = server(srv, _keys(3))
    assert lazy_starts == 1
    assert len(srv.asks) == 1, 'the existing vlm_caption.requested event was never sent'
    assert doc['src'] == 'vlm' and doc['stats']['vlm'] == 3, doc
    assert M.NOTE_VLM_FALLBACK not in doc['notes']
    assert post.call_count == 3
    assert post.call_args.args[0].endswith(':18081/v1/chat/completions')


def test_the_lazy_start_is_asked_once_per_reel_not_once_per_frame(server):
    """Nobody can start the server (no Nunba answering, no llama-server for
    the standalone launch): one ask for the reel, every frame falls back,
    and no frame is sent to a server that is not there."""
    srv = _CaptionServer(up=False, nunba_starts_it=False)
    doc, lazy_starts, post = server(srv, _keys(4))
    assert lazy_starts == 1 and len(srv.asks) == 1
    assert doc['src'] == 'fallback' and M.NOTE_VLM_FALLBACK in doc['notes']
    post.assert_not_called()


def test_a_shared_server_already_running_is_reused_never_restarted(server):
    """:8081 is shared with draft-first chat: when it answers, the lazy start
    returns at once -- nothing is asked for, nothing spawned (the fixture
    asserts no Popen) -- and the frames go to it."""
    srv = _CaptionServer(up=True)
    doc, lazy_starts, post = server(srv, _keys(2))
    assert lazy_starts == 1
    assert srv.asks == []
    assert doc['src'] == 'vlm' and post.call_count == 2


def test_the_wait_for_the_server_is_inside_the_moments_budget(server, monkeypatch):
    """The start is bounded by the reel's 90 s moments budget, not added on
    top of it: a start that spends the budget leaves no time to ask, and the
    reel goes on with the fallback and says the budget ran out."""
    clock = _Clock()
    monkeypatch.setattr(M, 'monotonic', clock)
    srv = _CaptionServer(up=False, nunba_starts_it=True, clock=clock, start_s=9.5)
    doc, lazy_starts, post = server(srv, _keys(2), budget_s=10.0)
    assert lazy_starts == 1
    post.assert_not_called()
    assert M.NOTE_VLM_BUDGET in doc['notes'] and M.NOTE_VLM_FALLBACK in doc['notes']


def test_no_keyframes_asks_for_no_server(server):
    srv = _CaptionServer(up=False)
    doc, lazy_starts, post = server(srv, [])
    assert lazy_starts == 0 and srv.asks == []
    post.assert_not_called()


# ── what she sees ─────────────────────────────────────────────────────────

class _Stop(Exception):
    pass


def test_her_chat_says_the_vision_model_is_being_readied_while_it_is(turn):
    """The lazy start may wait for the server, inside the moments step: the
    progress text shown for that step says so."""
    from integrations.reel_studio import pipeline as P
    from integrations.reel_studio.media import analyze, keyframes
    from integrations.reel_studio.media import moments as moments_mod
    clip = _small_clip(turn)
    turn.mp.setattr(turn.probe, 'probe',
                    lambda path: SimpleNamespace(duration_s=30.0, creation_time=None))
    turn.mp.setattr(analyze, 'analyze_clip', lambda info, **kw: SimpleNamespace())
    turn.mp.setattr(keyframes, 'pick_keyframes', lambda analyses: [])
    shown = []

    def score_moments(keys, **kw):
        shown.append(said[-1])
        raise _Stop()
    turn.mp.setattr(moments_mod, 'score_moments', score_moments)
    said = []
    with pytest.raises(_Stop):
        P.produce_reel([clip.to_dict()], style=C.make_style({'target_s': 5}),
                       workdir=str(turn.tmp / 'work'), out_path=str(turn.tmp / 'work' / 'r.mp4'),
                       progress=lambda stage, text: said.append((stage, text)))
    stage, text = shown[0]
    assert stage == 'reel_moments'
    assert 'vision model' in text.lower() and 'ready' in text.lower(), text


def test_the_reply_says_why_the_fallback_ran(turn):
    """No keyframe was rated (moments.NOTE_VLM_FALLBACK): what her agent is
    told says the vision model did not answer, so she knows why."""
    from integrations.reel_studio import pipeline as P
    clip = _small_clip(turn)
    turn.mp.setattr(P, 'produce_reel', _fake_production(P))
    result = P.make_reel(user_id=UID, prompt_id=PID, request_id='r', clips=[clip.to_dict()],
                         style=C.make_style({}))
    assert result['status'] == 'ok', result
    assert M.NOTE_VLM_FALLBACK in result['notes']
    assert 'vision model' in result['message'].lower(), result['message']


def test_a_reel_the_model_rated_does_not_say_it_fell_back(turn):
    from integrations.reel_studio import pipeline as P
    clip = _small_clip(turn)
    fake = _fake_production(P)

    def rated(*a, **kw):
        production = fake(*a, **kw)
        production.moments = {'src': 'vlm', 'final_look_found': True, 'notes': []}
        return production
    turn.mp.setattr(P, 'produce_reel', rated)
    result = P.make_reel(user_id=UID, prompt_id=PID, request_id='r', clips=[clip.to_dict()],
                         style=C.make_style({}))
    assert result['status'] == 'ok', result
    assert 'vision model' not in result['message'].lower(), result['message']
