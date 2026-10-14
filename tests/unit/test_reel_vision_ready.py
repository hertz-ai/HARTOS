"""A reel's keyframe ratings reach the vision model's EXISTING lazy start
(amendment A13, owner correction 2026-10-14: "we are creating tasks for
mechanisms which already exist" -- call the mechanism, build none).

Before b263da313, score_moments only ever called rate_frame, which by design
never starts a server (tests/unit/test_vision_rate_frame.py): a reel made
while the 0.8B caption server was down rated nothing and nothing asked for
the server.  Now score_moments asks the backend's own lazy start -- the one
describe() and read_document() already use, the public
VisionBackend.ensure_running (Qwen08BBackend's: a 'vlm_caption.requested'
event that Nunba's subscriber answers with llama_config.start_caption_server,
then the standalone launch) -- ONCE per reel, just before the first frame
that is not already cached.  Her chat says the vision model is being made
ready only when that start has to wait for the server (not when it is
already up), and a progress line that fails never skips the start.  The
start keeps its own waits: the time it takes COUNTS AGAINST the moments
budget and is NOT capped by it, and when it used all of it -- no frame was
asked at all (the moment list's stats.calls is 0) -- the reply says the model
was still starting.  A model that was asked and did not answer in time is
"no answer", never "still starting" (review of 3b0473794).  rate_frame itself
still never starts anything.

Real code throughout: get_vision_backend(REEL_VLM_DEFAULT) builds the real
Qwen08BBackend and its real ensure_running runs.  Only the boundaries are
replaced: the caption server's HTTP (/health and /v1/chat/completions, at
lightweight_backend's pooled_get / pooled_post), the platform event Nunba
subscribes to (core.platform.events.emit_event), the standalone launcher's
binary lookup, and sleep.

What the reel did is counted on the reel's own backend (``_Reel``: its starts
and its /health probes), and the waits on the thread the reel runs on, never
as every call of a patched module function: those are process-wide, and any
other thread may call them meanwhile.  Finding F1 (2026-10-14): in a batch,
hart_intelligence_entry's import-time vision_init thread ran
VisionService().start() inside a reel's window, and its two probes of the
same /health URL were counted as the reel's (5 for 3 keyframes).

    python -m pytest tests/unit/test_reel_vision_ready.py -q -p no:cacheprovider
"""
import contextlib
import io
import json
import threading
import time
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
#: time.sleep as it is, before any test replaces it.
REAL_SLEEP = time.sleep


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
    return [Candidate(clip='a' * 64, t_s=float(i + 1), segment=0,
                      jpeg=_jpeg((30 + 60 * i) % 256),
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
    simulated time the start takes, read by moments' budget.  ``request_s``:
    each chat-completions request takes that long, or its timeout when that
    is shorter, and then times out (None: it answers at once).  ``chat``: her
    chat's progress list; ``shown`` records its last line at each ask.
    ``health_urls``: every /health probe the port answered, from any backend
    on any thread (a reel's own are counted on its backend: _Reel.probes)."""

    def __init__(self, *, up=False, nunba_starts_it=True, clock=None, start_s=0.0,
                 reply=None, chat=None, request_s=None):
        self.up = up
        self.nunba_starts_it = nunba_starts_it
        self.clock = clock
        self.start_s = start_s
        self.request_s = request_s
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
        if self.request_s is not None:
            self.clock.t += min(timeout, self.request_s)
            raise requests.Timeout('read timed out')
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


class _Reel:
    """The vision backends the reel's moments step resolved
    (moments._default_backend: get_vision_backend(REEL_VLM_DEFAULT), one per
    score_moments call with keyframes), and what THEY did: ``starts``, calls
    of their ensure_running, and ``probes``, calls of their _is_serving (one
    GET /health each).  Another backend in this process probing the same URL,
    on any thread, is not counted (finding F1, module docstring)."""

    def __init__(self):
        self.backends = []

    @property
    def starts(self):
        return sum(b.ensure_running.call_count for b in self.backends)

    @property
    def probes(self):
        return sum(b._is_serving.call_count for b in self.backends)


@contextlib.contextmanager
def _the_reels_backend(start=None):
    """Yields a ``_Reel`` for the backends moments resolves inside the
    block, each with a spy on its own health check (the real one runs) and
    on its own ensure_running: the real one, or ``start`` as its side effect
    when given."""
    reel = _Reel()
    resolve = M._default_backend

    def resolved():
        backend = resolve()
        if backend is not None:
            backend._is_serving = MagicMock(wraps=backend._is_serving)
            backend.ensure_running = (MagicMock(side_effect=start) if start is not None
                                      else MagicMock(wraps=backend.ensure_running))
            reel.backends.append(backend)
        return backend
    with patch.object(M, '_default_backend', side_effect=resolved):
        yield reel


@contextlib.contextmanager
def _caption_port(srv):
    """Every boundary of the caption server replaced by ``srv``.  Yields
    ``reel`` (the reel's backend: its own starts and probes, ``_Reel``),
    ``post`` (the chat-completions POST) and ``slept`` (the waits asked for
    on this thread, the one the reel runs on; they return at once); nothing
    may be spawned.  time.sleep is process-wide: another thread's sleep (a
    running VisionService's description loop sleeps every 4 s,
    vision_service.py ~:905) stays its own, really slept and not recorded."""
    me = threading.get_ident()
    slept = []

    def sleep(seconds=0):
        if threading.get_ident() == me:
            slept.append(seconds)
        else:
            REAL_SLEEP(seconds)
    with contextlib.ExitStack() as stack:
        reel = stack.enter_context(_the_reels_backend())
        stack.enter_context(patch.object(lvb, 'pooled_get', side_effect=srv.health))
        post = stack.enter_context(patch.object(lvb, 'pooled_post', side_effect=srv.complete))
        stack.enter_context(patch('core.platform.events.emit_event', side_effect=srv.emit))
        stack.enter_context(patch(STANDALONE_BINARY, return_value=None))
        popen = stack.enter_context(patch('subprocess.Popen'))
        stack.enter_context(patch('time.sleep', sleep))
        yield SimpleNamespace(reel=reel, post=post, slept=slept)
        popen.assert_not_called()


@pytest.fixture
def server(tmp_path):
    """Run score_moments the way a reel does (no backend named: moments'
    REEL_VLM_DEFAULT through get_vision_backend), against ``_CaptionServer``.

    Returns ``score(srv, keys, **kw) -> (doc, reel, completions)``:
    ``reel`` is the reel's backend (``_Reel``: ``starts`` counts calls of its
    real ensure_running, ``probes`` its health checks), ``completions`` is
    the mocked chat-completions POST.  Calls in one test share one rating
    cache."""
    def score(srv, keys, **kw):
        with _caption_port(srv) as port:
            doc = M.score_moments(keys, cache_dir=str(tmp_path / 'cache'), **kw)
        assert C.validate_moments(doc) == [], C.validate_moments(doc)
        return doc, port.reel, port.post
    return score


# ── the reel reaches the existing lazy start ──────────────────────────────

def test_a_reel_with_the_caption_server_down_asks_for_it_and_is_rated(server):
    """The 0.8B caption server is down when her reel starts.  The reel asks
    for it through the backend's own lazy start (the event Nunba's
    subscriber answers with start_caption_server), and the frames are then
    rated by the model, not the deterministic fallback."""
    srv = _CaptionServer(up=False, nunba_starts_it=True)
    doc, reel, post = server(srv, _keys(3))
    assert reel.starts == 1
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
    doc, reel, post = server(srv, _keys(4))
    assert reel.starts == 1 and len(srv.asks) == 1
    assert doc['src'] == 'fallback' and M.NOTE_VLM_FALLBACK in doc['notes']
    post.assert_not_called()


def test_a_shared_server_already_running_is_reused_never_restarted(server):
    """:8081 is shared with draft-first chat: when it answers, the lazy start
    returns at once -- nothing is asked for, nothing spawned (the fixture
    asserts no Popen) -- and the frames go to it."""
    srv = _CaptionServer(up=True)
    doc, reel, post = server(srv, _keys(2))
    assert reel.starts == 1
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
    doc, reel, post = server(srv, _keys(2), budget_s=10.0)
    assert reel.starts == 1
    post.assert_not_called()
    assert M.NOTE_VLM_BUDGET in doc['notes'] and M.NOTE_VLM_FALLBACK in doc['notes']
    assert doc['stats']['elapsed_s'] == pytest.approx(start_s)


@pytest.mark.parametrize('budget_s', [0.0, M.MIN_CALL_S / 2])
def test_a_budget_under_min_call_s_asks_for_no_start(server, budget_s):
    """Under MIN_CALL_S no frame can be asked, so nothing is started for
    them: no lazy start, no event, no health probe, no request."""
    srv = _CaptionServer(up=False, nunba_starts_it=True)
    doc, reel, post = server(srv, _keys(2), budget_s=budget_s)
    assert reel.starts == 0 and srv.asks == [] and reel.probes == 0
    post.assert_not_called()
    assert doc['src'] == 'fallback' and M.NOTE_VLM_BUDGET in doc['notes']


def test_no_keyframes_asks_for_no_server(server):
    srv = _CaptionServer(up=False)
    doc, reel, post = server(srv, [])
    assert reel.starts == 0 and srv.asks == []
    post.assert_not_called()


def test_the_start_is_asked_at_the_first_frame_that_is_not_cached(server):
    """A reel whose first frame was rated before (cached) and whose others
    were not: the start is still asked, once, for the frames that need it."""
    keys = _keys(3)
    server(_CaptionServer(up=True), keys[:1])
    srv = _CaptionServer(up=False, nunba_starts_it=True)
    doc, reel, post = server(srv, keys)
    assert reel.starts == 1 and len(srv.asks) == 1
    assert doc['stats']['cached'] == 1 and post.call_count == 2
    assert doc['src'] == 'vlm'


# ── a start that raises ───────────────────────────────────────────────────

def _a_reel_whose_start_raises(clip_files, tmp_path, monkeypatch, *, meanwhile=None):
    """make_reel on three real clips with the caption server down, nobody to
    start it, and the reel's backend's ensure_running raising.  ``meanwhile``
    runs inside that start, just before it raises: while the reel is being
    made.  Returns (result, reel (``_Reel``), picked keyframes, srv, POST
    mock)."""
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

    def broken_start(on_wait=None):
        if meanwhile is not None:
            meanwhile()
        raise RuntimeError('the launcher broke')
    srv = _CaptionServer(up=False, nunba_starts_it=False)
    with _the_reels_backend(start=broken_start) as reel, \
         patch.object(lvb, 'pooled_get', side_effect=srv.health), \
         patch.object(lvb, 'pooled_post', side_effect=srv.complete) as post:
        result = P.make_reel(user_id=UID, prompt_id=PID, request_id='req-3',
                             clips=[sf.to_dict() for sf in clips],
                             style=C.make_style({'target_s': 5}), encoder='libx264')
    return result, reel, picked, srv, post


def _assert_each_frame_asked_once(reel, picked):
    """One /health probe per keyframe, by the reel's own backend.  Its start
    is the mock that raises, so every probe it made was a frame's rate_frame
    asking /health (and, the server being down, nothing more)."""
    assert picked and len(reel.backends) == 1, (picked, reel.backends)
    assert reel.probes == len(picked), (reel.probes, len(picked))


@requires_ffmpeg
def test_a_lazy_start_that_raises_still_finishes_the_reel(clip_files, tmp_path, monkeypatch):
    """The backend's ensure_running raises.  The raise is logged, every
    keyframe is still offered to rate_frame one by one and falls back on its
    own, and the real reel is made, checked and delivered -- not a
    'could not render' failure."""
    result, reel, picked, _srv, post = _a_reel_whose_start_raises(
        clip_files, tmp_path, monkeypatch)
    assert result['status'] == 'ok', result
    assert reel.starts == 1
    _assert_each_frame_asked_once(reel, picked)             # each frame asked, one by one
    post.assert_not_called()
    assert result['moments']['src'] == 'fallback'
    assert M.NOTE_VLM_FALLBACK in result['notes']
    assert C.validate_media_item(result['media']) == []


@requires_ffmpeg
def test_another_threads_vision_start_is_not_counted_as_the_reels(clip_files, tmp_path,
                                                                   monkeypatch):
    """Finding F1 (2026-10-14), simulated: hart_intelligence_entry's
    import-time vision_init thread runs VisionService().start() while a reel
    is being made.  Its get_vision_backend() asks a new Qwen08BBackend
    is_available(), then start(): two GETs of the same /health URL, through
    the same lightweight_backend.pooled_get, from another thread.  The port
    answers them; the reel's own counts do not change."""
    joined = []

    def vision_init():
        stray = lvb.Qwen08BBackend()        # the env's caption port, as the reel's
        stray.is_available()
        stray.start()

    def meanwhile():
        thread = threading.Thread(target=vision_init, name='vision_init', daemon=True)
        thread.start()
        thread.join(30)
        joined.append(not thread.is_alive())
    result, reel, picked, srv, post = _a_reel_whose_start_raises(
        clip_files, tmp_path, monkeypatch, meanwhile=meanwhile)
    assert joined == [True]
    assert result['status'] == 'ok', result
    # The other thread's two probes reached the port, at the reel's own URL:
    # every pooled_get, or every probe of that URL, is the frames plus two.
    assert srv.health_urls == ['http://127.0.0.1:18081/health'] * (len(picked) + 2)
    assert reel.starts == 1
    _assert_each_frame_asked_once(reel, picked)
    post.assert_not_called()


@requires_ffmpeg
def test_a_reel_that_probes_twice_per_frame_fails_the_one_probe_check(clip_files, tmp_path,
                                                                      monkeypatch):
    """The reel's own count still sees all of the reel: a rate_frame that
    asks /health twice per frame fails the check the two tests above make."""
    rate_frame = lvb.Qwen08BBackend.rate_frame

    def asks_twice(self, *a, **kw):
        self._is_serving()
        return rate_frame(self, *a, **kw)
    monkeypatch.setattr(lvb.Qwen08BBackend, 'rate_frame', asks_twice)
    result, reel, picked, srv, _post = _a_reel_whose_start_raises(
        clip_files, tmp_path, monkeypatch)
    assert result['status'] == 'ok', result
    assert reel.probes == 2 * len(picked) == len(srv.health_urls)
    with pytest.raises(AssertionError):
        _assert_each_frame_asked_once(reel, picked)


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
    assert port.reel.starts == 1 and len(srv.asks) == 1
    assert len(_readies(said)) == 1, said
    assert srv.shown == _readies(said), (srv.shown, said)
    assert _readies(said)[0][0] == 'reel_moments'
    assert doc['src'] == 'vlm'


def test_her_chat_is_not_told_the_model_is_being_readied_when_it_is_already_up(turn):
    """Review of 42735064e: the shared caption server is up (draft-first
    chat keeps it so), the start has nothing to wait for, and her chat must
    not say the vision model is being made ready."""
    said = []
    srv = _CaptionServer(up=True, chat=said)
    with _caption_port(srv):
        doc = _moments_step(turn, _keys(2), said)
    assert srv.asks == []
    assert _readies(said) == [], said
    assert doc['src'] == 'vlm'


def test_a_progress_line_that_fails_never_skips_the_start(tmp_path):
    """Review of 42735064e: on_start sat inside the try that guards the
    start, so a progress line that raised skipped the start and every frame
    fell back.  The start is still asked and the frames are rated."""
    def broken():
        raise RuntimeError('her chat is unreachable')
    srv = _CaptionServer(up=False, nunba_starts_it=True)
    with _caption_port(srv) as port:
        doc = M.score_moments(_keys(2), cache_dir=str(tmp_path / 'cache'), on_start=broken)
    assert len(srv.asks) == 1, 'the start was skipped because its progress line raised'
    assert doc['src'] == 'vlm' and port.post.call_count == 2


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
    assert port.reel.starts == 0 and down.asks == [] and port.reel.probes == 0
    assert port.slept == []
    port.post.assert_not_called()
    assert again['src'] == 'vlm' and again['stats']['cached'] == 3
    assert _readies(said) == [], said


def test_a_reel_with_no_keyframes_never_says_the_vision_model_is_being_readied(turn):
    said = []
    srv = _CaptionServer(up=False, nunba_starts_it=True, chat=said)
    with _caption_port(srv) as port:
        _moments_step(turn, [], said)
    assert port.reel.starts == 0 and srv.asks == []
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
    assert 'still starting' not in message, result['message']


def _reply_for(turn, moments):
    """What her agent is told (make_reel's message, lower case) for a reel
    whose moments step returned ``moments``; the rest of the reel is the
    pipeline tests' fake production."""
    from integrations.reel_studio import pipeline as P
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
    return result['message'].lower()


def _scored(turn, srv, keys):
    """The real score_moments on the reel's default budget, against ``srv``,
    on ``srv.clock``.  Returns (moment list, chat-completions POST mock)."""
    turn.mp.setattr(M, 'monotonic', srv.clock)
    with _caption_port(srv) as port:
        doc = M.score_moments(keys, cache_dir=str(turn.tmp / 'cache'))
    assert C.validate_moments(doc) == [], C.validate_moments(doc)
    return doc, port.post


def test_a_server_that_is_up_but_never_answers_in_time_is_not_called_still_starting(turn):
    """Review of 3b0473794, P3 (measured with the real score_moments and
    _ok_message): the shared caption server is up -- ensure_running returns
    at once, on_wait never fires -- and every rating runs to its timeout (a
    busy shared :8081, a slow CPU).  Six keyframes on the 90 s budget: five
    requests (4 x 20 s + 10 s), then the time runs out: vlm_fallback with
    vlm_budget, stats.calls 5.  3b0473794 told her agent the model 'was still
    starting when the time for judging the shots ran out': false, nothing
    was starting.  It gave no answer, and the reply says that."""
    srv = _CaptionServer(up=True, clock=_Clock(), request_s=60.0)
    moments, post = _scored(turn, srv, _keys(6))
    assert srv.asks == [] and post.call_count == 5
    assert moments['notes'] == [M.NOTE_VLM_FALLBACK, M.NOTE_VLM_BUDGET], moments['notes']
    assert moments['stats']['calls'] == 5
    message = _reply_for(turn, moments)
    assert 'still starting' not in message, message
    assert 'vision model' in message and 'no answer' in message, message


def test_a_start_that_gave_up_before_the_frames_says_the_model_gave_no_answer(turn):
    """Review of 3b0473794, P4: the start waited 35 s of the budget and the
    server never came up (nobody to start it); each frame then asked /health
    and fell back, with time left: vlm_fallback only.  The reply says the
    model gave no answer -- the start had ended before any frame was asked."""
    srv = _CaptionServer(up=False, nunba_starts_it=False, clock=_Clock(), start_s=35.0)
    moments, post = _scored(turn, srv, _keys(3))
    assert len(srv.asks) == 1 and post.call_count == 0
    assert moments['notes'] == [M.NOTE_VLM_FALLBACK], moments['notes']
    message = _reply_for(turn, moments)
    assert 'still starting' not in message, message
    assert 'vision model' in message and 'no answer' in message, message


def test_a_start_that_used_the_whole_budget_says_the_model_was_still_starting(turn):
    """P5: the start took longer than the moments budget (95 s of 90), so no
    frame was asked at all: stats.calls 0, with vlm_fallback and vlm_budget.
    That, and only that, is a start that used the time: her agent is told the
    vision model was still starting when the time ran out."""
    srv = _CaptionServer(up=False, nunba_starts_it=True, clock=_Clock(), start_s=95.0)
    moments, post = _scored(turn, srv, _keys(2))
    assert len(srv.asks) == 1 and post.call_count == 0
    assert moments['notes'] == [M.NOTE_VLM_FALLBACK, M.NOTE_VLM_BUDGET], moments['notes']
    assert moments['stats']['calls'] == 0
    message = _reply_for(turn, moments)
    assert 'vision model' in message and 'still starting' in message, message
    assert 'ran out' in message, message
    assert 'no answer' not in message, message


def test_a_moment_list_that_does_not_say_what_was_asked_claims_no_start(turn):
    """Both notes but no stats (the shape 3b0473794's own test wrote by
    hand): nothing says the start used the time, so the reply says only what
    is known -- the model gave no answer."""
    message = _reply_for(turn, {'src': 'fallback', 'final_look_found': True,
                                'notes': [M.NOTE_VLM_FALLBACK, M.NOTE_VLM_BUDGET]})
    assert 'still starting' not in message, message
    assert 'no answer' in message, message


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
