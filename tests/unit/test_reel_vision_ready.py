"""A reel's keyframe ratings reach the vision model's EXISTING lazy start
(amendment A13, owner correction 2026-10-14: "we are creating tasks for
mechanisms which already exist" -- call the mechanism, build none).

Before b263da313, score_moments only ever called rate_frame, which by design
never starts a server (tests/unit/test_vision_rate_frame.py): a reel made
while the 0.8B caption server was down rated nothing and nothing asked for
the server.  Now score_moments asks the backend's own lazy start -- the one
describe() and read_document() already use, Qwen08BBackend._ensure_running:
a 'vlm_caption.requested' event that Nunba's subscriber answers with
llama_config.start_caption_server, then the standalone launch -- ONCE per
reel, just before the first frame that is not already cached, and her chat
says the vision model is being made ready only then.  The start keeps its own
waits: the time it takes COUNTS AGAINST the moments budget and is NOT capped
by it.  rate_frame itself still never starts anything.

Real code throughout: get_vision_backend(REEL_VLM_DEFAULT) builds the real
Qwen08BBackend and its real _ensure_running runs.  Only the boundaries are
replaced: the caption server's HTTP (/health and /v1/chat/completions, at
lightweight_backend's pooled_get / pooled_post), the platform event Nunba
subscribes to (core.platform.events.emit_event), the standalone launcher's
binary lookup, and sleep.

    python -m pytest tests/unit/test_reel_vision_ready.py -q -p no:cacheprovider
"""
import contextlib
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
from tests.unit.reel_media_fixtures import requires_ffmpeg
from tests.unit.test_reel_pipeline import (  # noqa: F401  (fixtures: turn, clip_files)
    PID, T0, UID, _env, _fake_production, _small_clip, _upload, clip_files, turn)

STANDALONE_BINARY = ('integrations.service_tools.model_lifecycle.'
                     'ModelLifecycleManager._find_llama_server_binary')
#: score_moments as moments defines it, before any test wraps it.
REAL_SCORE_MOMENTS = M.score_moments


@pytest.fixture(autouse=True)
def _no_network(monkeypatch):
    refuse_all_network(monkeypatch, reason='test_reel_vision_ready: the caption server is mocked')


@pytest.fixture(autouse=True)
def _caption_port_env(monkeypatch):
    """Moments' REEL_VLM_DEFAULT through get_vision_backend, on a test port."""
    monkeypatch.delenv('HEVOLVE_VISION_BACKEND', raising=False)
    monkeypatch.setenv('HEVOLVE_VLM_CAPTION_PORT', '18081')


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


def _answer(content=None):
    """A chat-completions reply: ``content``, or a usable rating JSON."""
    if content is None:
        content = json.dumps({'stage': 'eyes', 'appeal': 8, 'face_visible': True})
    resp = MagicMock(status_code=200)
    resp.json.return_value = {'choices': [{'finish_reason': 'stop',
                                           'message': {'content': content}}]}
    return resp


class _CaptionServer:
    """The caption server's port, as lightweight_backend sees it.

    ``up``: answering /health 200.  ``nunba_starts_it``: Nunba's
    'vlm_caption.requested' subscriber (main.py) brings it up when asked.
    ``reply``: what the model answers (None: a usable rating).
    ``asks``: every 'vlm_caption.requested' event; ``clock``/``start_s``:
    simulated time the start takes, read by moments' budget.  ``chat``: her
    chat's progress list; ``shown`` records its last line at each ask."""

    def __init__(self, *, up=False, nunba_starts_it=True, clock=None, start_s=0.0,
                 reply=None, chat=None):
        self.up = up
        self.nunba_starts_it = nunba_starts_it
        self.clock = clock
        self.start_s = start_s
        self.reply = reply
        self.chat = chat
        self.asks = []
        self.shown = []
        self.health_urls = []

    def health(self, url, timeout=None, **kw):
        self.health_urls.append(url)
        if not self.up:
            raise requests.ConnectionError('refused')
        return MagicMock(status_code=200)

    def complete(self, url, json=None, timeout=None, **kw):
        return _answer(self.reply)

    def emit(self, topic, data=None, *a, **kw):
        if topic == 'vlm_caption.requested':
            self.asks.append(data)
            if self.chat is not None:
                self.shown.append(self.chat[-1] if self.chat else None)
            if self.clock is not None:
                self.clock.t += self.start_s
            if self.nunba_starts_it:
                self.up = True


class _Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


@contextlib.contextmanager
def _caption_port(srv):
    """Every boundary of the caption server replaced by ``srv``.  Yields
    ``lazy`` (a spy on the backend's real _ensure_running), ``post`` (the
    chat-completions POST) and ``sleep``; nothing may be spawned."""
    real = lvb.Qwen08BBackend._ensure_running
    with patch.object(lvb, 'pooled_get', side_effect=srv.health), \
         patch.object(lvb, 'pooled_post', side_effect=srv.complete) as post, \
         patch.object(lvb.Qwen08BBackend, '_ensure_running', autospec=True,
                      side_effect=real) as lazy, \
         patch('core.platform.events.emit_event', side_effect=srv.emit), \
         patch(STANDALONE_BINARY, return_value=None), \
         patch('subprocess.Popen') as popen, \
         patch('time.sleep') as sleep:
        yield SimpleNamespace(lazy=lazy, post=post, sleep=sleep)
    popen.assert_not_called()


@pytest.fixture
def server(tmp_path):
    """Run score_moments the way a reel does (no backend named: moments'
    REEL_VLM_DEFAULT through get_vision_backend), against ``_CaptionServer``.

    Returns ``score(srv, keys, **kw) -> (doc, lazy_starts, completions)``:
    ``lazy_starts`` counts calls of the backend's real _ensure_running,
    ``completions`` is the mocked chat-completions POST.  Calls in one test
    share one rating cache."""
    def score(srv, keys, **kw):
        with _caption_port(srv) as port:
            doc = M.score_moments(keys, cache_dir=str(tmp_path / 'cache'), **kw)
        assert C.validate_moments(doc) == [], C.validate_moments(doc)
        return doc, port.lazy.call_count, port.post
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


@pytest.mark.parametrize('start_s', [9.5, 12.0])
def test_the_start_counts_against_the_moments_budget_and_is_not_capped_by_it(
        server, monkeypatch, start_s):
    """The lazy start keeps its own waits (its polls after the event and
    after a standalone launch), and score_moments cannot cut them short: the
    time the start takes COUNTS AGAINST the reel's moments budget and is NOT
    capped by it (review of b263da313, 2026-10-14: a 2.0 s budget took
    11.05 s).  A start that spends the 10 s budget, or runs past it, leaves
    no time to ask: the reel goes on with the fallback, says the budget ran
    out, and the step took as long as the start did."""
    clock = _Clock()
    monkeypatch.setattr(M, 'monotonic', clock)
    srv = _CaptionServer(up=False, nunba_starts_it=True, clock=clock, start_s=start_s)
    doc, lazy_starts, post = server(srv, _keys(2), budget_s=10.0)
    assert lazy_starts == 1
    post.assert_not_called()
    assert M.NOTE_VLM_BUDGET in doc['notes'] and M.NOTE_VLM_FALLBACK in doc['notes']
    assert doc['stats']['elapsed_s'] == pytest.approx(start_s)


@pytest.mark.parametrize('budget_s', [0.0, M.MIN_CALL_S / 2])
def test_a_budget_under_min_call_s_asks_for_no_start(server, budget_s):
    """Under MIN_CALL_S no frame can be asked, so nothing is started for
    them: no lazy start, no event, no health probe, no request."""
    srv = _CaptionServer(up=False, nunba_starts_it=True)
    doc, lazy_starts, post = server(srv, _keys(2), budget_s=budget_s)
    assert lazy_starts == 0 and srv.asks == [] and srv.health_urls == []
    post.assert_not_called()
    assert doc['src'] == 'fallback' and M.NOTE_VLM_BUDGET in doc['notes']


def test_no_keyframes_asks_for_no_server(server):
    srv = _CaptionServer(up=False)
    doc, lazy_starts, post = server(srv, [])
    assert lazy_starts == 0 and srv.asks == []
    post.assert_not_called()


def test_the_start_is_asked_at_the_first_frame_that_is_not_cached(server):
    """A reel whose first frame was rated before (cached) and whose others
    were not: the start is still asked, once, for the frames that need it."""
    keys = _keys(3)
    server(_CaptionServer(up=True), keys[:1])
    srv = _CaptionServer(up=False, nunba_starts_it=True)
    doc, lazy_starts, post = server(srv, keys)
    assert lazy_starts == 1 and len(srv.asks) == 1
    assert doc['stats']['cached'] == 1 and post.call_count == 2
    assert doc['src'] == 'vlm'


# ── a start that raises ───────────────────────────────────────────────────

@requires_ffmpeg
def test_a_lazy_start_that_raises_still_finishes_the_reel(clip_files, tmp_path, monkeypatch):
    """The backend's _ensure_running raises.  The raise is logged, every
    keyframe is still offered to rate_frame one by one and falls back on its
    own, and the real reel is made, checked and delivered -- not a
    'could not render' failure."""
    from integrations.reel_studio import pipeline as P
    from integrations.reel_studio.media import keyframes
    store, _pushes = _env(monkeypatch, tmp_path)
    clips = [_upload(store, clip_files[c], captured_at=T0 + k * 1000)
             for k, c in enumerate(('green', 'blue', 'red'))]
    picked = []
    real_pick = keyframes.pick_keyframes

    def pick(analyses):
        picked[:] = real_pick(analyses)
        return list(picked)
    monkeypatch.setattr(keyframes, 'pick_keyframes', pick)
    srv = _CaptionServer(up=False, nunba_starts_it=False)
    with patch.object(lvb, 'pooled_get', side_effect=srv.health), \
         patch.object(lvb, 'pooled_post', side_effect=srv.complete) as post, \
         patch.object(lvb.Qwen08BBackend, '_ensure_running', autospec=True,
                      side_effect=RuntimeError('the launcher broke')) as lazy:
        result = P.make_reel(user_id=UID, prompt_id=PID, request_id='req-3',
                             clips=[sf.to_dict() for sf in clips],
                             style=C.make_style({'target_s': 5}), encoder='libx264')
    assert result['status'] == 'ok', result
    assert lazy.call_count == 1
    assert picked and len(srv.health_urls) == len(picked)   # each frame asked, one by one
    post.assert_not_called()
    assert result['moments']['src'] == 'fallback'
    assert M.NOTE_VLM_FALLBACK in result['notes']
    assert C.validate_media_item(result['media']) == []


# ── what she sees ─────────────────────────────────────────────────────────

class _Stop(Exception):
    pass


def _readies(said):
    """The progress lines that say the vision model is being made ready."""
    return [(stage, text) for stage, text in said
            if 'vision model' in text.lower() and 'ready' in text.lower()]


def _moments_step(turn, keys, said):
    """produce_reel as a reel runs it -- probe and analysis stubbed, the
    keyframes given, the real moments step with no backend named -- stopped
    at the beat grid.  ``said`` collects her chat's progress lines.  Returns
    the moment list."""
    from integrations.reel_studio import pipeline as P
    from integrations.reel_studio.media import analyze, beats, keyframes
    clip = _small_clip(turn)
    turn.mp.setattr(turn.probe, 'probe',
                    lambda path: SimpleNamespace(duration_s=30.0, creation_time=None))
    turn.mp.setattr(analyze, 'analyze_clip', lambda info, **kw: SimpleNamespace())
    turn.mp.setattr(keyframes, 'pick_keyframes', lambda analyses: list(keys))
    scored = []

    def score_moments(*a, **kw):
        scored.append(REAL_SCORE_MOMENTS(*a, **kw))
        return scored[-1]
    turn.mp.setattr(M, 'score_moments', score_moments)

    def stop(*a, **kw):
        raise _Stop()
    turn.mp.setattr(beats, 'reel_beat_grid', stop)
    with pytest.raises(_Stop):
        P.produce_reel([clip.to_dict()], style=C.make_style({'target_s': 5}),
                       workdir=str(turn.tmp / 'work'), out_path=str(turn.tmp / 'work' / 'r.mp4'),
                       progress=lambda stage, text: said.append((stage, text)))
    return scored[0]


def test_her_chat_says_the_vision_model_is_being_readied_when_the_start_is_asked(turn):
    """The caption server is down: the moments step asks for it, and at that
    moment her chat says the vision model is being made ready, once."""
    said = []
    srv = _CaptionServer(up=False, nunba_starts_it=True, chat=said)
    with _caption_port(srv) as port:
        doc = _moments_step(turn, _keys(2), said)
    assert port.lazy.call_count == 1 and len(srv.asks) == 1
    assert len(_readies(said)) == 1, said
    assert srv.shown == _readies(said), (srv.shown, said)
    assert _readies(said)[0][0] == 'reel_moments'
    assert doc['src'] == 'vlm'


def test_a_retried_reel_whose_frames_are_all_cached_neither_waits_nor_starts_a_server(turn):
    """Her reel is made again from the same clips (a retry): every keyframe
    was rated and cached the first time.  The retry asks the model nothing,
    so it asks for no start, waits for nothing, and her chat is not told the
    vision model is being made ready -- even with the server down now."""
    keys = _keys(3)
    with _caption_port(_CaptionServer(up=True)):
        first = _moments_step(turn, keys, [])
    assert first['src'] == 'vlm' and first['stats']['calls'] == 3
    said = []
    down = _CaptionServer(up=False, nunba_starts_it=True, chat=said)
    with _caption_port(down) as port:
        again = _moments_step(turn, keys, said)
    assert port.lazy.call_count == 0 and down.asks == [] and down.health_urls == []
    port.sleep.assert_not_called()
    port.post.assert_not_called()
    assert again['src'] == 'vlm' and again['stats']['cached'] == 3
    assert _readies(said) == [], said


def test_a_reel_with_no_keyframes_never_says_the_vision_model_is_being_readied(turn):
    said = []
    srv = _CaptionServer(up=False, nunba_starts_it=True, chat=said)
    with _caption_port(srv) as port:
        _moments_step(turn, [], said)
    assert port.lazy.call_count == 0 and srv.asks == []
    assert _readies(said) == [], said


@pytest.mark.parametrize('model', ['no_answer', 'unusable_answer'])
def test_the_reply_says_why_the_fallback_ran(turn, model):
    """No keyframe was rated (moments.NOTE_VLM_FALLBACK), because the vision
    model gave no answer (its server down, nobody able to start it) or only
    answers that could not be used (prose, not the rubric's JSON).  What her
    agent is told says so in plain words, and never claims a model that
    answered did not."""
    from integrations.reel_studio import pipeline as P
    srv = (_CaptionServer(up=False, nunba_starts_it=False) if model == 'no_answer'
           else _CaptionServer(up=True, reply='What a beautiful bride!'))
    with _caption_port(srv) as port:
        moments = M.score_moments(_keys(2), cache_dir=str(turn.tmp / 'cache'))
    assert port.post.call_count == (0 if model == 'no_answer' else 2)
    assert moments['src'] == 'fallback' and M.NOTE_VLM_FALLBACK in moments['notes']
    fake = _fake_production(P)

    def produce(*a, **kw):
        production = fake(*a, **kw)
        production.moments = moments
        return production
    turn.mp.setattr(P, 'produce_reel', produce)
    clip = _small_clip(turn)
    result = P.make_reel(user_id=UID, prompt_id=PID, request_id='r', clips=[clip.to_dict()],
                         style=C.make_style({}))
    assert result['status'] == 'ok', result
    assert M.NOTE_VLM_FALLBACK in result['notes']
    message = result['message'].lower()
    assert 'vision model' in message and 'no answer' in message, result['message']
    assert 'could not be used' in message, result['message']
    assert 'did not answer' not in message, result['message']


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
