"""An avatar's idle face: 60 s of silence spoken by its portrait, by the live face's own model (owner
27 Nov: "60 seconds of idle video using the same no volume audio that is used in central")."""
import importlib
import itertools
import json
import os
import wave
from pathlib import Path

import pytest

idle = importlib.import_module('integrations.service_tools.avatarforcing_idle')
af = importlib.import_module('integrations.service_tools.avatarforcing_tool')


@pytest.fixture(autouse=True)
def clean_job(monkeypatch):
    """The module's one job record starts each test empty (backoff, interruption)."""
    monkeypatch.setattr(idle, '_job', {'thread': None, 'sid': None, 'port': None, 'posting': False,
                                       'cancel': False, 'interrupted': False, 'backoff_s': 0.0})


@pytest.fixture
def out(monkeypatch, tmp_path):
    monkeypatch.setenv('HEVOLVE_MODEL_DIR', str(tmp_path / 'models'))
    d = tmp_path / 'models' / 'avatarforcing' / 'output'
    d.mkdir(parents=True)
    return d


@pytest.fixture
def portrait(tmp_path, monkeypatch):
    p = tmp_path / 'face.png'
    p.write_bytes(b'png')
    monkeypatch.setattr(af, '_portrait', lambda a: str(p) if a == 7 else None)
    return p


class _Post:
    def __init__(self, sid='abcdef012345', status=200):
        self.status_code, self._sid = status, sid

    def json(self):
        return {'sid': self._sid}


class _Stream:
    """GET /live.mp4: the sidecar writes <sid>.mp4 as the read ends when the stream completes."""
    def __init__(self, out, sid, complete=True):
        self.out, self.sid, self.complete = out, sid, complete

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def iter_content(self, n):
        yield b'x' * 10
        if self.complete:
            (self.out / f'{self.sid}.mp4').write_bytes(b'idle-face')


def _sidecar(monkeypatch, out, complete=True, sid='abcdef012345'):
    posted = []
    monkeypatch.setattr('core.http_pool.pooled_post_once',
                        lambda url, timeout=None, json=None: posted.append(json) or _Post(sid))
    import requests
    monkeypatch.setattr(requests, 'get', lambda url, **k: _Stream(out, sid, complete))
    return posted


def test_the_silence_is_sixty_seconds_of_zeros_at_the_engines_rate_written_once(out):
    path = idle._silence()
    with wave.open(str(path), 'rb') as w:
        assert (w.getnchannels(), w.getframerate(), w.getnframes()) == (1, 16000, 60 * 16000)
        assert set(w.readframes(16000)) == {0}
    before = os.stat(path).st_mtime_ns
    assert idle._silence() == path and os.stat(path).st_mtime_ns == before
    assert path.parent == out.parent  # an input beside the output folder, never served


def test_a_clip_is_made_kept_under_its_portrait_version_and_replaces_the_old_one(out, portrait,
                                                                                 monkeypatch):
    old = out / 'idle_7_1.mp4'
    old.write_bytes(b'old')
    posted = _sidecar(monkeypatch, out)
    got = idle.make_clip(7, 4321)
    assert got == out / f'idle_7_{os.stat(portrait).st_mtime_ns}.mp4' and got.read_bytes() == b'idle-face'
    assert not old.exists() and not (out / 'abcdef012345.mp4').exists()
    assert posted[0]['audio_url'].endswith('idle_silence_60s.wav')
    assert posted[0]['image_url'] == Path(portrait).resolve().as_uri()
    assert idle.stored_clip(7) == got


def test_an_incomplete_stream_keeps_nothing(out, portrait, monkeypatch):
    _sidecar(monkeypatch, out, complete=False)
    assert idle.make_clip(7, 4321) is None
    assert idle.stored_clip(7) is None


def test_a_clip_already_made_for_this_portrait_is_not_made_again(out, portrait, monkeypatch):
    (out / idle.clip_name(7, portrait)).write_bytes(b'kept')
    monkeypatch.setattr('core.http_pool.pooled_post_once',
                        lambda *a, **k: pytest.fail('no new session for a kept clip'))
    assert idle.make_clip(7, 4321).read_bytes() == b'kept'


def test_no_portrait_no_clip(out, portrait):
    assert idle.make_clip(8, 4321) is None


def test_a_live_reply_stops_a_clip_being_made(monkeypatch):
    import requests
    deleted = []
    monkeypatch.setattr(requests, 'delete', lambda url, **k: deleted.append((url, k['params'])))
    monkeypatch.setitem(idle._job, 'sid', 'abcdef012345')
    monkeypatch.setitem(idle._job, 'port', 4321)
    assert idle.cancel_idle() is True
    assert deleted == [('http://127.0.0.1:4321/live.mp4', {'sid': 'abcdef012345'})]
    monkeypatch.setitem(idle._job, 'sid', None)
    assert idle.cancel_idle() is False


def test_a_live_reply_during_the_idle_post_stops_the_session_it_opens(out, portrait, monkeypatch):
    """rv_face2 A3 race: the idle sid is known only after its POST; a live request in that window is
    remembered and the session is stopped the moment its sid is known, never read to the end."""
    import requests
    deleted, read = [], []

    def post(url, timeout=None, json=None):
        idle.cancel_idle()  # a live reply arrives while the idle session is being opened
        return _Post('abcdef012345')
    monkeypatch.setattr('core.http_pool.pooled_post_once', post)
    monkeypatch.setattr(requests, 'delete', lambda url, **k: deleted.append(k['params']['sid']))
    monkeypatch.setattr(requests, 'get', lambda url, **k: read.append(url) or _Stream(out, 'x'))
    assert idle.make_clip(7, 4321) is None
    assert deleted == ['abcdef012345'] and read == []


# ── the warm session after a setup (#43; review of the warm design, 28 Nov) ─────────────────────

def _warm_ready(monkeypatch, turn=False, live=False):
    monkeypatch.setattr(idle, '_turn_in_flight', lambda: turn)
    monkeypatch.setattr(af, 'live_face_open', lambda: live)
    # a warm is never a reply: never live_session's path (its .owner, notify_access, back-off)
    monkeypatch.setattr(af, 'live_session', lambda *a, **k: pytest.fail('a warm is not a reply'))
    monkeypatch.setattr(af, '_remember_owner', lambda *a, **k: pytest.fail('a warm has no owner'))


def test_a_warm_session_runs_for_a_kept_clips_avatar_and_keeps_nothing(out, portrait, monkeypatch):
    """Build #8: the first reply after a load reached its first frame in 1.0-2.7 s, later ones in
    0.4-0.5 s.  One short silent session for the turn's avatar warms it; nothing of it is kept."""
    clip = out / idle.clip_name(7, portrait)
    clip.write_bytes(b'kept')  # an idle clip already made does not skip the warm
    posted = _sidecar(monkeypatch, out)
    _warm_ready(monkeypatch)
    backoff = dict(af._backoff)
    assert idle.warm_session(7, 4321) is True
    assert posted[0]['audio_url'].endswith(f'idle_silence_{idle.WARM_SECONDS}s.wav')
    assert posted[0]['image_url'] == Path(portrait).resolve().as_uri()
    assert not (out / 'abcdef012345.mp4').exists() and clip.read_bytes() == b'kept'
    assert list(out.glob('*.owner')) == [] and af._backoff == backoff
    assert idle._job['sid'] is None and idle._job['posting'] is False


def test_a_warm_session_is_skipped_while_a_turn_or_a_reply_face_runs(out, portrait, monkeypatch):
    monkeypatch.setattr('core.http_pool.pooled_post_once',
                        lambda *a, **k: pytest.fail('no warm session now'))
    _warm_ready(monkeypatch, turn=True)
    assert idle.warm_session(7, 4321) is False
    _warm_ready(monkeypatch, live=True)
    assert idle.warm_session(7, 4321) is False


def test_a_person_at_the_computer_does_not_skip_the_warm(out, portrait, monkeypatch):
    """They just asked for this face; the idle clip's presence and quiet gates are not the warm's."""
    _sidecar(monkeypatch, out)
    _warm_ready(monkeypatch)
    monkeypatch.setattr(idle, '_person_at_the_machine', lambda: True)
    monkeypatch.setattr(idle, '_seconds_since_user_chat', lambda: 1.0)
    assert idle.warm_session(7, 4321) is True


def test_a_live_reply_stops_a_warm_session_being_read(out, portrait, monkeypatch):
    import requests
    deleted = []
    monkeypatch.setattr(requests, 'delete', lambda url, **k: deleted.append(k['params']['sid']))

    class _Interrupted(_Stream):
        def iter_content(self, n):
            idle.cancel_idle('a live reply')  # the reply arrives mid-read; the stream ends incomplete
            yield b'x'
    monkeypatch.setattr('core.http_pool.pooled_post_once', lambda *a, **k: _Post('abcdef012345'))
    monkeypatch.setattr(requests, 'get', lambda url, **k: _Interrupted(out, 'abcdef012345'))
    _warm_ready(monkeypatch)
    assert idle.warm_session(7, 4321) is False
    assert deleted == ['abcdef012345']


def test_one_session_at_a_time_and_a_busy_idle_clip_stays_wanted(out, portrait, monkeypatch):
    """cancel_idle knows one session: a second one is not opened beside it.  An idle clip that finds
    a warm running counts as interrupted (kept wanted); a warm that finds a clip running just skips."""
    monkeypatch.setattr('core.http_pool.pooled_post_once',
                        lambda *a, **k: pytest.fail('no second session'))
    _warm_ready(monkeypatch)
    monkeypatch.setitem(idle._job, 'sid', 'running01')
    assert idle.warm_session(7, 4321) is False and idle._job['interrupted'] is False
    assert idle.make_clip(7, 4321) is None and idle._job['interrupted'] is True


def test_a_setup_a_video_turn_started_warms_its_avatar_once_the_model_is_ready(monkeypatch):
    """_start_setup's success: wait for /health ready, then one warm session for the turn's avatar;
    a failed setup warms nothing."""
    import concurrent.futures
    import threading
    import types
    warmed, health = [], iter([{'ready': False}, {'ready': True}])

    class _Health:
        def __init__(self, body):
            self.body = body

        def json(self):
            return self.body
    monkeypatch.setattr('core.http_pool.pooled_get', lambda url, timeout=None: _Health(next(health)))
    monkeypatch.setattr(idle, 'warm_session', lambda aid, port: warmed.append((aid, port)) or True)
    monkeypatch.setattr(af, '_can_run_here', lambda rtm: True)
    monkeypatch.setattr(af, '_setup', {'future': None, 'started_at': 0.0, 'failed': False})
    started = []

    def _thread(*a, **k):  # the module's own threads only, really run and joined below
        t = threading.Thread(*a, **k)
        started.append(t)
        return t
    monkeypatch.setattr(af, 'threading', types.SimpleNamespace(Thread=_thread))

    def run_setup(result):
        fut = concurrent.futures.Future()

        class _Rtm:
            def setup_tool_async(self, tool):
                return fut

            def get_tool_port(self, tool):
                return 5123
        af._setup['started_at'] = 0.0
        af._start_setup(_Rtm(), 'req1', 7)
        fut.set_result(result)

    run_setup({'port': 5123})
    for t in started:
        t.join(timeout=10)
    assert warmed == [(7, 5123)] and [t.name for t in started] == ['avatar-warm']
    warmed.clear()
    run_setup({'error': 'no room'})
    assert warmed == [] and len(started) == 1


slept = []


def _worker_with(monkeypatch, out, portrait, here, port=4321, make=None):
    made = []
    slept.clear()
    monkeypatch.setattr(idle, '_sidecar_port', lambda: port)
    monkeypatch.setattr(idle, '_quiet_enough', lambda: not here.pop(0) if here else True)
    monkeypatch.setattr(idle.time, 'sleep', lambda s: slept.append(s))
    monkeypatch.setattr(idle, 'make_clip', make or (lambda aid, p: made.append(aid)))
    idle._wanted.clear()
    idle._wanted.add(7)
    idle._worker()
    return made


def test_the_clip_waits_while_a_turn_is_served(out, portrait, monkeypatch):
    """rv_face2 A3 / hartos-17 ruling: the turn-in-flight signal, not a quiet check of its own."""
    made = _worker_with(monkeypatch, out, portrait, here=[True, True, False])
    assert made == [7]


def test_a_clip_a_turn_stopped_stays_wanted_and_is_made_in_the_next_pause(out, portrait, monkeypatch):
    calls = []

    def make(aid, port):
        calls.append(aid)
        if len(calls) == 1:
            idle._job['interrupted'] = True  # what cancel_idle marks when a turn stops the clip
        return None
    _worker_with(monkeypatch, out, portrait, here=[], make=make)
    assert calls == [7, 7] and idle._wanted == set()


def test_a_clip_that_failed_is_not_retried_until_the_next_listing(out, portrait, monkeypatch):
    calls = []
    _worker_with(monkeypatch, out, portrait, here=[], make=lambda aid, p: calls.append(aid))
    assert calls == [7] and idle._wanted == set()


def test_the_gate_is_a_turn_in_flight_not_the_ten_minute_chat_cooldown(monkeypatch):
    """hartos-17 ruling: should_yield_to_user's 600 s cooldown outlasts the face (evicted 600 s after
    its last reply), so the clip would never be made.  Only a turn being served now holds it."""
    from core import foreground
    monkeypatch.setattr(foreground, '_yield_gate', lambda: True)  # chatted a minute ago
    monkeypatch.setattr(foreground, 'foreground_active', lambda: False)
    assert idle._turn_in_flight() is False
    monkeypatch.setattr(foreground, 'foreground_active', lambda: True)
    assert idle._turn_in_flight() is True


def test_the_gate_fails_closed(monkeypatch):
    """No gate wired in this process, or a signal that cannot be read: a turn is assumed."""
    from core import foreground
    monkeypatch.setattr(foreground, 'foreground_active', lambda: False)
    monkeypatch.setattr(foreground, '_yield_gate', None)
    assert idle._turn_in_flight() is True
    monkeypatch.setattr(foreground, '_yield_gate', lambda: False)
    monkeypatch.setattr(foreground, 'foreground_active',
                        lambda: (_ for _ in ()).throw(OSError('marker dir')))
    assert idle._turn_in_flight() is True


class _LongStream(_Stream):
    """A clip still being generated: chunks until the sidecar is told to stop."""
    def __init__(self, out, sid, stopped):
        super().__init__(out, sid, complete=False)
        self.stopped = stopped

    def iter_content(self, n):
        for _ in range(50):
            if self.stopped:
                return
            yield b'x' * 10


def test_a_turn_starting_while_a_clip_is_read_stops_it_at_once(out, portrait, monkeypatch):
    """hartos-17 ruling: the signal is polled during the ~41 s read; a VOICE turn (which never reaches
    live_session's cancel_idle) stops the clip too.  Nothing is kept; the clip stays wanted."""
    import requests
    stopped = []
    monkeypatch.setattr('core.http_pool.pooled_post_once',
                        lambda url, timeout=None, json=None: _Post('abcdef012345'))
    monkeypatch.setattr(requests, 'get', lambda url, **k: _LongStream(out, 'abcdef012345', stopped))
    monkeypatch.setattr(requests, 'delete', lambda url, **k: stopped.append(k['params']['sid']))
    clock = itertools.count(0, 2)
    monkeypatch.setattr(idle.time, 'monotonic', lambda: next(clock))
    turns = [False, False, True]
    monkeypatch.setattr(idle, '_turn_in_flight', lambda: turns.pop(0) if turns else True)
    idle._job['interrupted'] = False
    assert idle.make_clip(7, 4321) is None
    assert stopped == ['abcdef012345'] and idle._job['interrupted'] is True
    assert idle.stored_clip(7) is None
    idle._job['interrupted'] = False


def test_the_face_is_never_started_for_an_idle_clip_and_the_wish_is_kept(out, portrait, monkeypatch):
    made = _worker_with(monkeypatch, out, portrait, here=[], port=None)
    assert made == [] and idle._wanted == {7}
    idle._wanted.clear()


def test_a_clip_kept_for_this_portrait_is_not_made_again_by_the_worker(out, portrait, monkeypatch):
    (out / idle.clip_name(7, portrait)).write_bytes(b'kept')
    assert _worker_with(monkeypatch, out, portrait, here=[]) == []


def test_finding_a_clip_creates_nothing(tmp_path, monkeypatch):
    monkeypatch.setenv('HEVOLVE_MODEL_DIR', str(tmp_path / 'm'))
    assert idle.stored_clip(7) is None
    assert not (tmp_path / 'm').exists()


# ── the agent list carries it ───────────────────────────────────────────────────────────────

def test_a_kept_clip_is_the_agents_first_idle_filler_before_centrals(out, monkeypatch):
    """One link builder (rv_face2 A1): a phone gets an absolute link, the page a same-origin one."""
    monkeypatch.setattr(idle, 'want_clip', lambda a: None)
    (out / 'idle_7_5.mp4').write_bytes(b'kept')
    central = {'type': 'idle', 'video_link': 'https://central.test/idle.mp4'}
    got = idle.with_idle_face([central], 42, 7, phone_base='http://192.168.0.10:5000/')
    assert got == [{'type': 'idle', 'video_link': 'http://192.168.0.10:5000/avatar/idle/42.mp4',
                    'page_link': '/avatar/idle/42.mp4', 'source': 'local_face'}, central]


def test_no_address_for_a_phone_means_no_phone_link(out, monkeypatch):
    """hartos-17: with no advertisable base, the filler carries the page's link only, never a relative
    video_link a phone would read against whatever host it listed through."""
    monkeypatch.setattr(idle, 'want_clip', lambda a: None)
    (out / 'idle_7_5.mp4').write_bytes(b'kept')
    got = idle.with_idle_face([], 42, 7, phone_base='')
    assert got == [{'type': 'idle', 'page_link': '/avatar/idle/42.mp4', 'source': 'local_face'}]


_ENTRY = os.path.join(os.path.dirname(__file__), '..', '..', 'hart_intelligence_entry.py')


def _prompts_client(merge, central=None):
    """GET /prompts from its shipping source: hart_intelligence_entry cannot be imported in a unit
    test (it boots the runtime and dials the network: rv_face2's socket seal refused 3 attempts), so
    get_prompts is lifted out by AST and run on a fresh Flask app with the names it reads supplied,
    as test_prompts_list_excludes_proposed_plans does."""
    import ast
    import json as _json
    import flask
    with open(_ENTRY, encoding='utf-8') as f:
        tree = ast.parse(f.read())
    nodes = [n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == 'get_prompts']
    assert len(nodes) == 1
    app = flask.Flask('idle_face_listing_test')
    ns = {'app': app, 'request': flask.request, 'jsonify': flask.jsonify, 'os': os,
          'json': _json, 'PROMPTS_DIR': 'unused', 'CENTRAL_DB_URL': central,
          '_merge_prompts_with_cloud': merge}
    exec(compile(ast.Module(body=nodes, type_ignores=[]), _ENTRY, 'exec'), ns)
    return app.test_client()


def _listing(monkeypatch, agents):
    from core import prompt_files
    monkeypatch.setattr(prompt_files, 'local_agent_prompts', lambda d: agents)
    monkeypatch.setattr(prompt_files, 'agent_account', lambda data: 'u1')


def test_the_agent_list_puts_the_local_idle_face_before_centrals(monkeypatch):
    """rv_face2 A4 / I5: /prompts step 3 runs AFTER the cloud merge, so the merged agent's central
    fillers are already there and the node's own clip goes first."""
    from core import port_registry
    agent = {'name': 'A', 'goal': 'g', 'status': 'completed', 'teacher_avatar_id': 7, 'fillers': []}
    _listing(monkeypatch, [('42', agent)])
    central = {'type': 'idle', 'video_link': 'https://central.test/idle.mp4'}

    def merge(prompts, url):
        for p in prompts:
            p['fillers'] = list(p['fillers']) + [central]
    monkeypatch.setattr(port_registry, 'get_advertisable_base_url', lambda: 'http://192.168.0.10:5000')
    seen = []

    def with_idle_face(fillers, prompt_id, avatar_id, phone_base=''):
        seen.append(list(fillers))
        return [{'type': 'idle', 'source': 'local_face', 'page_link': f'/avatar/idle/{prompt_id}.mp4',
                 'video_link': f'{phone_base}/avatar/idle/{prompt_id}.mp4'}] + list(fillers)
    monkeypatch.setattr(idle, 'with_idle_face', with_idle_face)
    resp = _prompts_client(merge, central='https://central.test').get('/prompts?user_id=u1')
    assert resp.status_code == 200, resp.get_data(as_text=True)
    fillers = resp.get_json()[0]['fillers']
    assert seen == [[central]]  # called after the merge
    assert [f.get('source') for f in fillers] == ['local_face', None]
    assert fillers[0]['video_link'] == 'http://192.168.0.10:5000/avatar/idle/42.mp4'


def test_a_list_with_no_face_agent_never_asks_for_the_lan_address(monkeypatch):
    """rv_face2 A: the LAN address is a socket call; a listing with no face agent makes none."""
    from core import port_registry
    _listing(monkeypatch, [('43', {'name': 'V', 'goal': 'g', 'status': 'completed'})])
    monkeypatch.setattr(port_registry, 'get_advertisable_base_url',
                        lambda: pytest.fail('no face agent listed: no LAN lookup'))
    monkeypatch.setattr(idle, 'with_idle_face', lambda *a, **k: pytest.fail('no face: no idle face'))
    resp = _prompts_client(lambda prompts, url: None).get('/prompts?user_id=u1')
    assert resp.status_code == 200 and resp.get_json()[0]['prompt_id'] == '43'


def test_a_face_agent_without_a_clip_asks_for_one_and_lists_as_before(out, monkeypatch):
    asked = []
    monkeypatch.setattr(idle, 'want_clip', asked.append)
    assert idle.with_idle_face([], 42, 7) == []
    assert asked == [7]


def test_a_kept_clip_is_still_checked_against_the_current_portrait(out, monkeypatch):
    """rv_face2 A2: a portrait change must get a new clip, so the listing always asks the worker."""
    asked = []
    monkeypatch.setattr(idle, 'want_clip', asked.append)
    (out / 'idle_7_5.mp4').write_bytes(b'old portrait')
    idle.with_idle_face([], 42, 7)
    assert asked == [7]


def test_a_voice_agent_gets_no_idle_face_and_asks_nothing(out, monkeypatch):
    monkeypatch.setattr(idle, 'want_clip', lambda a: pytest.fail('no clip for an agent with no face'))
    assert idle.with_idle_face([{'type': 'idle', 'video_link': 'x'}], 42, None) == [
        {'type': 'idle', 'video_link': 'x'}]
    assert idle.with_idle_face(None, 42, 0) == []


def test_the_listing_never_breaks_on_it(monkeypatch):
    monkeypatch.setattr(idle, 'want_clip', lambda a: None)
    monkeypatch.setattr(idle, 'stored_clip', lambda a: (_ for _ in ()).throw(OSError('disk')))
    assert idle.with_idle_face([{'type': 'intro'}], 42, 7) == [{'type': 'intro'}]


# ── hartos-17's ruling (snapshot9): a quiet period, no live face, back off ─────────────────

def _signals(monkeypatch, turn=False, since=None, live=False, person=False):
    monkeypatch.setattr(idle, '_turn_in_flight', lambda: turn)
    monkeypatch.setattr(idle, '_person_at_the_machine', lambda: person)
    monkeypatch.setattr(idle, '_seconds_since_user_chat', lambda: since)
    monkeypatch.setattr(af, 'live_face_open', lambda: live)


def test_a_person_at_the_computer_holds_the_clip(monkeypatch):
    """Build #8, 28 Nov 19:58: a clip rendered while the owner was at Nunba; live audio stuttered."""
    _signals(monkeypatch, since=None, person=True)
    assert idle._quiet_enough() is False
    _signals(monkeypatch, since=None, person=False)
    assert idle._quiet_enough() is True


def test_presence_is_the_yield_gates_one_definition_on_a_thread_with_no_turn(monkeypatch):
    """The idle worker runs on no request id, where background_work_must_yield always answers
    False; the presence read must still see the person through person_using_the_machine."""
    dispatch = importlib.import_module('integrations.agent_engine.dispatch')
    from hartos.threadlocal import thread_local_data
    monkeypatch.setattr(thread_local_data, 'get_request_id', lambda: None)
    monkeypatch.setattr('core.foreground.foreground_active', lambda: False)

    class _Governor:
        def __init__(self, present):
            self.present = present

        def user_present(self):
            return self.present

    monkeypatch.setattr('core.resource_governor.get_governor', lambda: _Governor(True))
    assert dispatch.background_work_must_yield() is False  # why the gate itself is not asked
    assert idle._person_at_the_machine() is True
    monkeypatch.setattr('core.resource_governor.get_governor', lambda: _Governor(False))
    assert idle._person_at_the_machine() is False
    monkeypatch.setattr(dispatch, 'person_using_the_machine',
                        lambda: (_ for _ in ()).throw(RuntimeError('no gate')))
    assert idle._person_at_the_machine() is True  # the check itself broken: no clip, as the chat read does


def test_a_chat_that_just_ended_holds_the_clip_for_the_quiet_period(monkeypatch):
    """The /chat turn signal ends with the handler; the reply's voice and face are made after it."""
    _signals(monkeypatch, since=idle.IDLE_QUIET_S - 1)
    assert idle._quiet_enough() is False
    _signals(monkeypatch, since=idle.IDLE_QUIET_S + 1)
    assert idle._quiet_enough() is True
    _signals(monkeypatch, since=None)  # no chat within the yield gate's cooldown at all
    assert idle._quiet_enough() is True


def test_a_reply_face_being_made_or_a_turn_holds_the_clip(monkeypatch):
    _signals(monkeypatch, since=None, live=True)
    assert idle._quiet_enough() is False
    _signals(monkeypatch, since=None, turn=True)
    assert idle._quiet_enough() is False


def test_the_last_chat_comes_from_the_yield_gates_own_stamps_and_fails_closed(monkeypatch):
    dispatch = importlib.import_module('integrations.agent_engine.dispatch')
    import time as _t
    monkeypatch.setattr(dispatch, '_last_user_chat_at', _t.time() - 30)
    assert 29 <= idle._seconds_since_user_chat() <= 31
    monkeypatch.setattr(dispatch, '_last_user_chat_at', 0.0)
    monkeypatch.setattr('core.foreground.marker_age_s', lambda name, max_age, require_live=True: 12.0)
    assert idle._seconds_since_user_chat() == 12.0  # another process served the chat
    monkeypatch.setattr(dispatch, 'seconds_since_user_chat',
                        lambda: (_ for _ in ()).throw(RuntimeError('no gate')))
    assert idle._seconds_since_user_chat() == 0.0  # unreadable: just chatted, no clip


def test_each_interruption_doubles_the_wait_to_a_cap_and_a_completed_clip_resets_it(
        out, portrait, monkeypatch):
    calls = []

    def make(aid, port):
        calls.append(aid)
        if len(calls) <= 3:
            idle._job['interrupted'] = True
            return None
        return out / 'idle_7_1.mp4'
    _worker_with(monkeypatch, out, portrait, here=[], make=make)
    assert calls == [7, 7, 7, 7]
    assert slept == [idle.IDLE_RETRY_S, 2 * idle.IDLE_RETRY_S, 4 * idle.IDLE_RETRY_S]
    assert idle._job['backoff_s'] == 0.0 and idle._wanted == set()


def test_the_back_off_stops_at_its_cap(out, portrait, monkeypatch):
    calls = []

    def make(aid, port):
        calls.append(aid)
        if len(calls) <= 2:
            idle._job['interrupted'] = True
        return None
    idle._job['backoff_s'] = 240.0
    _worker_with(monkeypatch, out, portrait, here=[], make=make)
    assert slept == [idle.IDLE_BACKOFF_MAX_S, idle.IDLE_BACKOFF_MAX_S]
    assert idle._job['backoff_s'] == idle.IDLE_BACKOFF_MAX_S  # a failure, not a completion: kept


class _Health:
    def __init__(self, running):
        self._running = running

    def json(self):
        return {'ok': True, 'ready': True, 'running': self._running}


def _sidecar_health(monkeypatch, running=False, port=4321, down=False):
    monkeypatch.setattr(af, 'sidecar_port', lambda: port)

    def get(url, timeout=None):
        assert url == f'http://127.0.0.1:{port}/health' and timeout == 2
        if down:
            raise ConnectionError('sidecar down')
        return _Health(running)
    monkeypatch.setattr('core.http_pool.pooled_get', get)


def test_a_face_is_being_made_by_the_sidecars_own_answer(out, monkeypatch):
    """hartos-17: GET /health 'running' (one stream at a time), not a guessed window."""
    monkeypatch.setattr(af, '_owners', af.OrderedDict())
    _sidecar_health(monkeypatch, running=True)
    assert af.live_face_open() is True
    _sidecar_health(monkeypatch, running=False)
    assert af.live_face_open() is False


def test_a_reply_face_opened_but_not_yet_read_counts_for_a_few_seconds(out, monkeypatch):
    """The sidecar generates only for a reader: a just-opened reply reads running=False."""
    monkeypatch.setattr(af, '_owners', af.OrderedDict())
    _sidecar_health(monkeypatch, running=False)
    af._remember_owner('abcdef012345', 42, 'req-1')
    assert af.live_face_open() is True
    (out / 'abcdef012345.mp4').write_bytes(b'face')  # finished
    assert af.live_face_open() is False


def test_a_stopped_face_does_not_hold_idle_clips_back_for_an_hour(out, monkeypatch):
    """rv_face2's probe: a barge-in DELETE, an audio-mode page that never played it, or nobody
    watching leaves an unaudited .owner with no face; the audit keeps it an hour, "now" does not."""
    monkeypatch.setattr(af, '_owners', af.OrderedDict())
    _sidecar_health(monkeypatch, running=False)
    af._remember_owner('abcdef012345', 42, 'req-1')
    rec = json.loads((out / 'abcdef012345.owner').read_text(encoding='utf-8'))
    rec['started_at'] -= af.FACE_START_GRACE_S + 60
    (out / 'abcdef012345.owner').write_text(json.dumps(rec), encoding='utf-8')
    assert af.live_face_open() is False


def test_a_sidecar_that_is_down_or_silent_is_making_no_face(out, monkeypatch):
    monkeypatch.setattr(af, '_owners', af.OrderedDict())
    af._remember_owner('abcdef012345', 42, 'req-1')  # even with a fresh record
    _sidecar_health(monkeypatch, port=None)
    assert af.live_face_open() is False
    _sidecar_health(monkeypatch, down=True)
    assert af.live_face_open() is False


def test_the_gate_registration_is_read_through_its_public_accessor(monkeypatch):
    from core import foreground
    monkeypatch.setattr(foreground, '_yield_gate', None)
    assert foreground.yield_gate_registered() is False
    monkeypatch.setattr(foreground, '_yield_gate', lambda: False)
    assert foreground.yield_gate_registered() is True
