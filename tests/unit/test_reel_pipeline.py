"""integrations.reel_studio.pipeline: her clips in, a checked reel on her phone.

make_reel is one job (jobs.start .. jobs.finish): it checks the disk and her
quota before anything is decoded, makes the reel (produce_reel: probe ->
capture order -> one analysis pass per clip -> keyframes -> VLM moments ->
beat grid -> plan -> two-pass render -> QA), withholds a reel that fails a
hard QA check, and otherwise puts it in her outbox (user_file_store.
offer_to_device), pushes it to her chat topic with the turn's request id and
never through central's crossbar, and marks the source clips used.  The work
dir is removed whatever happens.

The end-to-end tests use real ffmpeg on synthetic clips (three tinted
testsrc2 clips standing in for bare face, eyes and final look), the real
store on a temporary root, a fake vision model that names the stage from the
frame's colour, and the HARTOS publisher (publish_async) as the recorded
boundary.  Uploaded in the wrong order, the clips carry the phone's
captured_at (A1), so the reel must follow the capture times.
"""
import hashlib
import json
import os
import sys
from types import SimpleNamespace

import pytest

from integrations.reel_studio import contracts as C
# The real disk check, taken before any fixture replaces it (_env does: this
# box's own free disk must not decide the tests).
from integrations.reel_studio.pipeline import free_bytes as REAL_FREE_BYTES
from tests.unit.reel_media_fixtures import make_clip, make_png, requires_ffmpeg

UID = '10077'
PID = '42'
T0 = 1_760_247_082_000          # 2025-10-12, epoch ms (a phone's DATE_TAKEN)


# ── shared set-up ─────────────────────────────────────────────────────────

class FakeVision:
    """A vision backend that reads the stage off the frame's tint:
    green = bare face, blue = eyes, red = the final look."""
    name = 'fake-vlm'

    def __init__(self, answer=True):
        self.answer = answer
        self.calls = 0

    def ensure_running(self, on_wait=None):
        """VisionBackend's: a fake has no server to start."""
        return True

    def rate_frame(self, image_bytes, prompt, *, long_side=768, max_tokens=80, timeout_s=20.0):
        self.calls += 1
        if not self.answer:
            return None             # the server is down
        import cv2
        import numpy as np
        img = cv2.imdecode(np.frombuffer(image_bytes, np.uint8), cv2.IMREAD_COLOR)
        b, g, r = (float(img[:, :, k].mean()) for k in range(3))
        stage = {0: 'eyes', 1: 'bare_face', 2: 'final_look'}[max(range(3), key=lambda k: (b, g, r)[k])]
        return json.dumps({'stage': stage, 'appeal': 8, 'face_visible': False})


def _env(mp, root):
    """Temporary data root, the real store as the process singleton, and a
    recorded publish_async.  Returns (store, pushes)."""
    from tests.conftest import refuse_all_network
    refuse_all_network(mp)
    mp.setenv('NUNBA_DATA_DIR', str(root))
    from core import platform_paths
    platform_paths.reset_cache()
    from core import user_file_store as ufs
    store = ufs.UserFileStore(str(root / 'user_files'), disk_free=lambda _p: 1 << 40)
    mp.setattr(ufs, '_STORE', store)
    from integrations.reel_studio import jobs
    mp.setattr(jobs, '_ACTIVE', set())
    # This box's own free disk must not decide the tests (one test lowers it).
    from integrations.reel_studio import pipeline
    mp.setattr(pipeline, 'free_bytes', lambda _path: 1 << 40)
    pushes = []

    def publish_async(topic, message, timeout=2.0, skip_crossbar=False):
        pushes.append((topic, message, {'skip_crossbar': skip_crossbar}))
    mp.setitem(sys.modules, 'hart_intelligence', SimpleNamespace(publish_async=publish_async))
    return store, pushes


def _upload(store, path, *, purpose='reel_source', mime='video/mp4', captured_at=None,
            request_id='req-1'):
    """Offer, put and commit one file as the phone does, then bind it to the turn."""
    with open(path, 'rb') as f:
        data = f.read()
    xid = hashlib.sha256(data).hexdigest()
    meta = {'captured_at': captured_at} if captured_at else None
    ans = store.offer(UID, os.path.basename(path), len(data), xid, mime, purpose, meta=meta)
    assert ans['type'] == 'file_accept', ans
    cs = ans['chunk_size']
    for i in range(ans['n']):
        chunk = data[i * cs:(i + 1) * cs]
        assert store.put_chunk(UID, xid, i, chunk, hashlib.sha256(chunk).hexdigest())['type'] == 'file_ack'
    assert store.commit(UID, xid) == {'type': 'file_done', 'xid': xid}
    return store.bind(UID, [xid], prompt_id=PID, request_id=request_id)[0]


_TINTS = {
    'green': 'lutrgb=r=val*0.15:b=val*0.15',
    'blue': 'lutrgb=r=val*0.15:g=val*0.15',
    'red': 'lutrgb=g=val*0.15:b=val*0.15',
}


@pytest.fixture(scope='module')
def clip_files(tmp_path_factory):
    from tests.unit.reel_media_fixtures import ffmpeg_available
    if not ffmpeg_available():
        pytest.skip('no ffmpeg')
    d = tmp_path_factory.mktemp('reel_clips')
    out = {name: make_clip(d, name + '.mp4', size=(608, 1080), duration_s=3.0, vf=vf)
           for name, vf in _TINTS.items()}
    out['logo'] = make_png(d, 'logo.png', size=(120, 60), color='white')
    return out


@pytest.fixture(scope='module')
def made(clip_files, tmp_path_factory):
    """One real reel, made once for the module's assertions."""
    from integrations.reel_studio import pipeline as P
    from integrations.reel_studio.media import edl as E
    with pytest.MonkeyPatch.context() as mp:
        store, pushes = _env(mp, tmp_path_factory.mktemp('made_root'))
        planned = []
        real_plan = E.plan_edit

        def spy_plan(*a, **k):
            planned.append(real_plan(*a, **k))
            return planned[-1]
        mp.setattr(E, 'plan_edit', spy_plan)
        # Uploaded red, green, blue; filmed green, blue, red (the phone says so).
        uploaded = [
            _upload(store, clip_files['red'], captured_at=T0 + 600_000),
            _upload(store, clip_files['green'], captured_at=T0),
            _upload(store, clip_files['blue'], captured_at=T0 + 300_000),
        ]
        vision = FakeVision()
        result = P.make_reel(user_id=UID, prompt_id=PID, request_id='req-1',
                             clips=[sf.to_dict() for sf in uploaded],
                             style=C.make_style({'target_s': 5, 'hook_text': 'Bridal glow'}),
                             song={'song_key': 'cluster:7', 'title': 'Kannazhaga', 'artist': 'Anirudh',
                                   'lang': 'ta', 'start_ms': 38_500},
                             logo_path=clip_files['logo'], caption='Meena bridal, Madurai',
                             hashtags=['#bridalmakeup', '#maduraimua'], backend=vision,
                             encoder='libx264')
        from integrations.reel_studio import jobs
        yield SimpleNamespace(result=result, store=store, pushes=pushes, uploaded=uploaded,
                              edl=planned[-1] if planned else None, vision=vision,
                              work_root=P.work_root(), job=jobs.reel_job(result.get('reel_id', '')),
                              active=jobs.active_job())


# ── one real reel ─────────────────────────────────────────────────────────

@requires_ffmpeg
def test_synthetic_clips_make_a_reel_that_passes_qa(made):
    r = made.result
    assert r['status'] == 'ok', r
    assert r['qa'] == {'passed': True, 'failed': []}
    media = r['media']
    assert C.validate_media_item(media) == []
    assert (media['width'], media['height'], media['has_audio']) == (1080, 1920, False)
    assert abs(media['duration_s'] - 5.0) <= C.QUALITY_BAR['duration']['tol_s']
    assert media['prompt_id'] == PID and media['request_id'] == 'req-1'
    assert media['reel_id'] == r['reel_id']
    assert media['caption'] == 'Meena bridal, Madurai'
    assert media['hashtags'] == ['#bridalmakeup', '#maduraimua']
    assert media['song'] == {'song_key': 'cluster:7', 'title': 'Kannazhaga', 'artist': 'Anirudh',
                             'lang': 'ta', 'start_ms': 38_500, 'start_label': '0:38',
                             'beats': 'synthetic'}


@requires_ffmpeg
def test_the_reel_is_in_her_outbox_and_pushed_with_the_turns_request_id(made):
    media = made.result['media']
    held = made.store.resolve(UID, media['xid'], box='out')
    assert held is not None and held.size == media['size']
    with open(held.path, 'rb') as f:
        assert hashlib.sha256(f.read()).hexdigest() == media['xid']
    assert held.meta['media'] == media
    assert [i['xid'] for i in made.store.list_outbox(UID, prompt_id=PID)] == [media['xid']]
    final = [p for p in made.pushes if isinstance(p[1], dict) and p[1].get('media')]
    assert len(final) == 1
    topic, payload, flags = final[0]
    assert topic.endswith('.' + UID) and flags == {'skip_crossbar': True}
    assert payload['request_id'] == 'req-1' and payload['prompt_id'] == PID
    assert payload['text'] and payload['text'][0].strip()
    assert payload['media'] == [media]


@requires_ffmpeg
def test_progress_is_shown_on_her_chat_while_it_works(made):
    stages = [p for p in made.pushes if not (isinstance(p[1], dict) and p[1].get('media'))]
    assert len(stages) >= 3
    for topic, message, _flags in stages:
        envelope = json.loads(message) if isinstance(message, str) else message
        assert envelope['action'] == 'Status' and envelope['request_id'] == 'req-1'


@requires_ffmpeg
def test_the_clips_play_in_the_order_she_filmed_them(made):
    edl = made.edl
    assert edl is not None and edl['capture_src'] == 'phone'
    assert C.capture_note('phone') in made.result['media']['notes']
    by_xid = {sf.xid: sf for sf in made.uploaded}
    orders = {x: c['order'] for x, c in edl['clips'].items()}
    filmed = sorted(orders, key=lambda x: by_xid[x].meta['captured_at'])
    assert [orders[x] for x in filmed] == sorted(orders.values())
    # The reveal is the red clip (filmed last), and it ends the reel.
    assert edl['final_look_found'] is True
    assert edl['segments'][-1]['role'] == 'reveal'
    assert edl['segments'][-1]['clip'] == made.uploaded[0].xid


@requires_ffmpeg
def test_moments_came_from_the_vision_model(made):
    assert made.result['moments'] == {'src': 'vlm', 'final_look_found': True}
    assert made.vision.calls >= 3


@requires_ffmpeg
def test_her_clips_are_marked_used(made):
    assert made.store.query_inbox(UID, purpose='reel_source') == []
    assert len(made.store.query_inbox(UID, purpose='reel_source', unconsumed_only=False)) == 3


@requires_ffmpeg
def test_the_work_dir_is_removed(made):
    root = made.work_root
    assert not os.path.isdir(root) or os.listdir(root) == []


@requires_ffmpeg
def test_the_job_is_recorded_ok_and_the_machine_is_free(made):
    assert made.job is not None and made.job.status == 'ok'
    assert made.job.user_id == UID and made.job.request_id == 'req-1'
    assert made.active is None


# ── degraded but delivered ────────────────────────────────────────────────

@requires_ffmpeg
def test_with_the_vision_model_down_the_reel_still_ships_and_says_so(clip_files, tmp_path,
                                                                   monkeypatch):
    from integrations.reel_studio import pipeline as P
    store, pushes = _env(monkeypatch, tmp_path)
    clips = [_upload(store, clip_files[c], captured_at=T0 + k * 1000)
             for k, c in enumerate(('green', 'blue', 'red'))]
    result = P.make_reel(user_id=UID, prompt_id=PID, request_id='req-2',
                         clips=[sf.to_dict() for sf in clips], style=C.make_style({'target_s': 5}),
                         backend=FakeVision(answer=False), encoder='libx264')
    assert result['status'] == 'ok', result
    assert result['moments']['src'] == 'fallback'
    assert 'vlm_fallback' in result['notes'] and 'vlm_fallback' in result['media']['notes']
    assert C.validate_media_item(result['media']) == []


# ── refused before anything is decoded ────────────────────────────────────

@pytest.fixture
def turn(tmp_path, monkeypatch):
    """A temp store and publisher, and analysis that must not run."""
    store, pushes = _env(monkeypatch, tmp_path)
    from integrations.reel_studio.media import analyze, probe

    def must_not_run(*a, **k):
        raise AssertionError('decoded a clip it should have refused first')
    monkeypatch.setattr(analyze, 'analyze_clip', must_not_run)
    return SimpleNamespace(store=store, pushes=pushes, tmp=tmp_path, mp=monkeypatch,
                           probe=probe)


def _small_clip(turn, name='a.mp4', size=4096):
    path = turn.tmp / name
    path.write_bytes(os.urandom(size))
    return _upload(turn.store, str(path))


def test_low_disk_is_no_space_before_any_decode(turn):
    from integrations.reel_studio import jobs
    from integrations.reel_studio import pipeline as P
    clip = _small_clip(turn)
    turn.mp.setattr(turn.probe, 'probe', lambda *a, **k: (_ for _ in ()).throw(
        AssertionError('probed a clip with no disk to work on')))
    turn.mp.setattr(P, 'free_bytes', lambda path: P.MIN_FREE_BYTES - 1)
    result = P.make_reel(user_id=UID, prompt_id=PID, request_id='r', clips=[clip.to_dict()],
                         style=C.make_style({}))
    assert result['status'] == 'no_space' and result['message']
    assert jobs.reel_job(result['reel_id']).status == 'no_space'
    assert turn.store.query_inbox(UID) != []                 # nothing consumed
    assert turn.pushes == [] or all('media' not in (p[1] if isinstance(p[1], dict) else {})
                                    for p in turn.pushes)


def test_measuring_free_disk_creates_nothing(tmp_path):
    """Amendment A11b: the disk check reads the free space where the work
    dir will be, and makes no dir to read it."""
    where = tmp_path / 'reel_studio' / 'work'
    free = REAL_FREE_BYTES(str(where))
    assert isinstance(free, int) and free > 0
    assert not (tmp_path / 'reel_studio').exists()


def test_a_low_disk_refusal_leaves_no_work_dir_behind(turn):
    """The real disk check on a fresh data root: refused for no_space, and
    the work root was never made just to measure it (amendment A11b)."""
    from integrations.reel_studio import jobs
    from integrations.reel_studio import pipeline as P
    clip = _small_clip(turn)
    turn.mp.setattr(P, 'free_bytes', REAL_FREE_BYTES)
    asked = []

    def disk_usage(path):
        asked.append(path)
        return SimpleNamespace(total=1 << 40, used=1 << 39, free=P.MIN_FREE_BYTES - 1)
    import shutil       # the boundary of core.platform_paths.free_bytes, the one rule (D27)
    turn.mp.setattr(shutil, 'disk_usage', disk_usage)
    result = P.make_reel(user_id=UID, prompt_id=PID, request_id='r', clips=[clip.to_dict()],
                         style=C.make_style({}))
    assert result['status'] == 'no_space', result
    assert asked and all(os.path.isdir(p) for p in asked)        # measured where it exists
    assert not os.path.exists(jobs.work_root())


def test_a_full_quota_is_quota_before_any_decode(turn):
    from core import user_file_store as ufs
    from integrations.reel_studio import pipeline as P
    clip = _small_clip(turn)
    tight = ufs.UserFileStore(turn.store.root, disk_free=lambda _p: 1 << 40,
                              policy={'user_quota': turn.store.usage(UID) + (1 << 20)})
    turn.mp.setattr(turn.probe, 'probe', lambda *a, **k: (_ for _ in ()).throw(
        AssertionError('probed a clip past her quota')))
    result = P.make_reel(user_id=UID, prompt_id=PID, request_id='r', clips=[clip.to_dict()],
                         style=C.make_style({'target_s': 20}), store=tight)
    assert result['status'] == 'quota' and result['message']


def test_a_second_reel_while_one_is_being_made_is_busy(turn):
    from integrations.reel_studio import jobs
    from integrations.reel_studio import pipeline as P
    clip = _small_clip(turn)
    other = jobs.start_job('20088', '7', 'theirs')
    try:
        result = P.make_reel(user_id=UID, prompt_id=PID, request_id='r', clips=[clip.to_dict()],
                             style=C.make_style({}))
    finally:
        jobs.finish_job(other.reel_id, 'ok')
    assert result['status'] == 'busy' and result['message']
    assert 'reel_id' not in result


def test_no_clips_is_needs_clips(turn):
    from integrations.reel_studio import pipeline as P
    result = P.make_reel(user_id=UID, prompt_id=PID, request_id='r', clips=[],
                         style=C.make_style({}))
    assert result['status'] == 'needs_clips' and result['message']


def _canned_ffmpeg(mp, path, fixture):
    """ffmpeg -i <path> answers with a canned banner; everything else runs."""
    from core import media_bin
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    with open(os.path.join(here, 'fixtures', 'reel', fixture), encoding='utf-8') as f:
        banner = f.read()
    real = media_bin.run_ffmpeg

    def run(args, *, timeout_s):
        if list(args) == ['-i', os.path.abspath(path)]:
            return SimpleNamespace(returncode=1, stdout='', stderr=banner, timed_out=False)
        return real(args, timeout_s=timeout_s)
    mp.setattr(media_bin, 'run_ffmpeg', run)


def test_a_dolby_vision_profile_5_clip_is_clip_unsupported(turn):
    from integrations.reel_studio import pipeline as P
    clip = _small_clip(turn, 'dovi.mp4')
    _canned_ffmpeg(turn.mp, clip.path, 'ffmpeg_info_dovi_p5.txt')
    result = P.make_reel(user_id=UID, prompt_id=PID, request_id='r', clips=[clip.to_dict()],
                         style=C.make_style({}))
    assert result['status'] == 'clip_unsupported', result
    assert result['xid'] == clip.xid and result['reason'] == 'dolby_vision_p5'
    assert not os.path.isdir(os.path.join(P.work_root(), result['reel_id']))


@requires_ffmpeg
def test_too_little_footage_is_said_before_analysis(turn, clip_files):
    from integrations.reel_studio import pipeline as P
    clip = _upload(turn.store, clip_files['green'])          # 3 s of footage
    result = P.make_reel(user_id=UID, prompt_id=PID, request_id='r', clips=[clip.to_dict()],
                         style=C.make_style({'target_s': 20}))
    assert result['status'] == 'not_enough_footage', result
    assert result['need_s'] >= 20 - 1.0 - 3.0 - 0.1


# ── what make_reel does with what production returns ────────────────────

def _fake_production(P, *, passed=True, failed=(), notes=(), beats='synthetic'):
    def produce(sources, *, style, workdir, out_path, **kw):
        os.makedirs(os.path.dirname(out_path), exist_ok=True)
        with open(out_path, 'wb') as f:
            f.write(b'\x00\x00\x00\x18ftypmp42' + os.urandom(2048))
        qa = {'schema': C.QA_SCHEMA, 'passed': passed,
              'checks': [{'id': c, 'ok': False, 'value': None, 'want': None, 'hard': True}
                         for c in failed], 'advisory': []}
        render = C.RenderResult(path=out_path, duration_s=float(style['target_s']),
                                size_bytes=os.path.getsize(out_path), encoder='libx264')
        return P.Production(path=out_path, edl={'notes': ['capture_order:upload_order']},
                            moments={'src': 'fallback', 'final_look_found': False,
                                     'notes': ['vlm_fallback']},
                            qa=qa, render=render, beats_source=beats, notes=list(notes),
                            timings={}, analyses=[])
    return produce


def test_a_reel_that_fails_a_hard_check_is_withheld(turn):
    from integrations.reel_studio import jobs
    from integrations.reel_studio import pipeline as P
    clip = _small_clip(turn)
    turn.mp.setattr(P, 'produce_reel', _fake_production(P, passed=False, failed=['fps']))
    result = P.make_reel(user_id=UID, prompt_id=PID, request_id='r', clips=[clip.to_dict()],
                         style=C.make_style({}))
    assert result['status'] == 'qa_failed' and result['failed'] == ['fps']
    assert turn.store.list_outbox(UID) == []
    assert not any(isinstance(p[1], dict) and p[1].get('media') for p in turn.pushes)
    assert turn.store.query_inbox(UID) != []                 # her clips are kept for a retry
    assert jobs.reel_job(result['reel_id']).status == 'qa_failed'


def test_a_render_failure_is_render_failed_with_the_reason(turn):
    from core import media_bin
    from integrations.reel_studio import pipeline as P
    clip = _small_clip(turn)

    def broken(*a, **k):
        raise P.ReelFailed('render_failed', 'ffmpeg exited 1', detail='ffmpeg exited 1')
    turn.mp.setattr(P, 'produce_reel', broken)
    result = P.make_reel(user_id=UID, prompt_id=PID, request_id='r', clips=[clip.to_dict()],
                         style=C.make_style({}))
    assert result['status'] == 'render_failed' and 'ffmpeg' in result['detail']
    assert media_bin  # the runner itself is untouched


def test_an_unexpected_bug_is_a_render_failure_not_a_crash(turn):
    from integrations.reel_studio import jobs
    from integrations.reel_studio import pipeline as P
    clip = _small_clip(turn)
    turn.mp.setattr(P, 'produce_reel', lambda *a, **k: 1 / 0)
    result = P.make_reel(user_id=UID, prompt_id=PID, request_id='r', clips=[clip.to_dict()],
                         style=C.make_style({}))
    assert result['status'] == 'render_failed'
    assert jobs.active_job() is None                             # the machine is free again


def _fails_once(calls, real, error=(13, 'Permission denied')):
    """``real``, except that its first call raises OSError(*error)."""
    def once(*a, **k):
        if not calls:
            calls.append(1)
            raise OSError(*error)
        return real(*a, **k)
    return once


@pytest.mark.parametrize('broken', ['store', 'work_dir'])
def test_a_store_or_work_dir_that_cannot_be_opened_never_holds_the_machine(turn, broken):
    """Amendment A11a: the store and the job's work dir are resolved inside
    the job.  One that cannot be opened ends that job as render_failed (never
    a raise), the machine is free at once, and the next reel is made -- not
    answered 'busy' while a dead job holds the claim."""
    from core import user_file_store as ufs
    from integrations.reel_studio import jobs
    from integrations.reel_studio import pipeline as P
    clip = _small_clip(turn)
    calls = []
    if broken == 'store':
        turn.mp.setattr(ufs, 'get_user_file_store', _fails_once(calls, ufs.get_user_file_store))
    else:
        turn.mp.setattr(jobs, 'work_dir', _fails_once(calls, jobs.work_dir))
    turn.mp.setattr(P, 'produce_reel', _fake_production(P))
    first = P.make_reel(user_id=UID, prompt_id=PID, request_id='r', clips=[clip.to_dict()],
                        style=C.make_style({}))
    assert calls == [1]
    assert first['status'] == 'render_failed' and first['message'], first
    assert 'Permission denied' in first['detail']
    assert jobs.active_job() is None
    assert jobs.reel_job(first['reel_id']).status == 'render_failed'
    again = P.make_reel(user_id=UID, prompt_id=PID, request_id='r2', clips=[clip.to_dict()],
                        style=C.make_style({}))
    assert again['status'] == 'ok', again


def test_a_job_record_that_cannot_be_written_is_a_status_not_a_raise(turn):
    """make_reel never raises: the disk filling as the job's record is first
    written is answered (render_failed with the reason), and frees the
    machine for the next reel."""
    from integrations.reel_studio import jobs
    from integrations.reel_studio import pipeline as P
    clip = _small_clip(turn)
    calls = []
    turn.mp.setattr(jobs, 'atomic_json_write', _fails_once(
        calls, jobs.atomic_json_write, (28, 'No space left on device')))
    result = P.make_reel(user_id=UID, prompt_id=PID, request_id='r', clips=[clip.to_dict()],
                         style=C.make_style({}))
    assert calls == [1]
    assert result['status'] == 'render_failed' and result['message'], result
    assert 'No space left' in result['detail']
    nxt = jobs.start_job('20088', '7', 'next')
    assert nxt is not None
    jobs.finish_job(nxt.reel_id, 'ok')


def test_a_delivered_reel_carries_every_note_and_the_beat_source(turn):
    from integrations.reel_studio import pipeline as P
    clip = _small_clip(turn)
    turn.mp.setattr(P, 'produce_reel', _fake_production(P, notes=['face_unframed']))
    result = P.make_reel(user_id=UID, prompt_id=PID, request_id='r', clips=[clip.to_dict()],
                         style=C.make_style({}), song=None)
    assert result['status'] == 'ok', result
    notes = result['media']['notes']
    assert 'face_unframed' in notes and 'vlm_fallback' in notes
    assert 'capture_order:upload_order' in notes and 'beats:synthetic' in notes
    assert result['media']['song'] is None
    # The push says the cuts follow a steady beat, not a song's (decision U2).
    push = [p for p in turn.pushes if isinstance(p[1], dict) and p[1].get('media')][0]
    assert 'beat' in push[1]['text'][0].lower()
    # ... and so does what her agent is told to say: the phone shows the reply
    # or the push (one per xid), so the push alone may never reach her.
    assert 'steady beat' in result['message']


def _media_push(turn):
    return [p for p in turn.pushes if isinstance(p[1], dict) and p[1].get('media')][0][1]


NO_ACCOUNT = ('No research Instagram account is set up on this computer, so there is no '
              'trending-song list.')


@pytest.mark.parametrize('song', [
    None,
    # a key her agent named that the list does not hold: no title, no start
    {'song_key': 'cluster:404', 'title': '', 'artist': '', 'lang': None, 'start_ms': 0},
])
def test_a_reel_with_no_song_to_suggest_says_why_and_asks_for_none(turn, song):
    """Reviewer's measurement on c17513ec4: with no research account (every
    fresh install) the reel's reply still told her agent to give her "the
    song to add in Instagram", a song that does not exist, and nobody said
    why.  Now neither what her agent is told nor the push names a song, and
    both carry the reason her tools were given (the trending list's note)."""
    from integrations.reel_studio import pipeline as P
    clip = _small_clip(turn)
    turn.mp.setattr(P, 'produce_reel', _fake_production(P))
    result = P.make_reel(user_id=UID, prompt_id=PID, request_id='r', clips=[clip.to_dict()],
                         style=C.make_style({}), song=song, song_note=NO_ACCOUNT)
    assert result['status'] == 'ok', result
    assert NO_ACCOUNT in result['notes']
    message = result['message']
    assert 'song to add' not in message
    assert 'do not name one' in message and NO_ACCOUNT in message
    text = _media_push(turn)['text'][0]
    assert 'No song is suggested' in text and NO_ACCOUNT in text
    assert 'Suggested song' not in text


def test_a_suggested_song_is_given_with_its_start_and_no_reason_against_it(turn):
    from integrations.reel_studio import pipeline as P
    clip = _small_clip(turn)
    turn.mp.setattr(P, 'produce_reel', _fake_production(P))
    result = P.make_reel(user_id=UID, prompt_id=PID, request_id='r', clips=[clip.to_dict()],
                         style=C.make_style({}),
                         song={'song_key': 'cluster:7', 'title': 'Kannazhaga', 'artist': 'Anirudh',
                               'lang': 'ta', 'start_ms': 38_500},
                         song_note=NO_ACCOUNT)
    assert result['status'] == 'ok', result
    assert 'the song to add in Instagram at the start time given' in result['message']
    assert NO_ACCOUNT not in result['message'] and NO_ACCOUNT not in result['notes']
    text = _media_push(turn)['text'][0]
    assert 'Suggested song: Kannazhaga by Anirudh, start it at 0:38' in text
    assert 'No song is suggested' not in text


def test_each_step_of_the_reel_beats_its_job(turn):
    """A slow reel (ten long HDR clips) is still working, not stale: every
    progress step beats its job, so it is never taken over (jobs.MAX_JOB_S
    counts from the last beat)."""
    from integrations.reel_studio import jobs
    from integrations.reel_studio import pipeline as P
    clip = _small_clip(turn)
    beats = []
    turn.mp.setattr(jobs, 'beat', lambda reel_id: beats.append(reel_id))
    fake = _fake_production(P)

    def produce(sources, **kw):
        kw['progress']('reel_analyse', 'Watching clip 1 of 1...')
        kw['progress']('reel_render', 'Rendering your reel...')
        return fake(sources, **kw)
    turn.mp.setattr(P, 'produce_reel', produce)
    result = P.make_reel(user_id=UID, prompt_id=PID, request_id='r', clips=[clip.to_dict()],
                         style=C.make_style({}))
    assert result['status'] == 'ok', result
    assert len(beats) >= 2 and set(beats) == {result['reel_id']}


def test_a_song_whose_beats_were_heard_is_labelled_so(turn):
    from integrations.reel_studio import pipeline as P
    clip = _small_clip(turn)
    song_file = turn.tmp / 'song.mp3'
    song_file.write_bytes(os.urandom(4096))
    audio = _upload(turn.store, str(song_file), purpose='song_audio', mime='audio/mpeg')
    seen = {}
    fake = _fake_production(P, beats='local_audio')

    def produce(sources, **kw):
        seen.update(kw)
        return fake(sources, **kw)
    turn.mp.setattr(P, 'produce_reel', produce)
    result = P.make_reel(user_id=UID, prompt_id=PID, request_id='r', clips=[clip.to_dict()],
                         style=C.make_style({}),
                         song={'song_key': None, 'title': 'my song', 'artist': '', 'lang': None,
                               'start_ms': 0, 'audio_path': audio.path, 'audio_xid': audio.xid})
    assert result['status'] == 'ok', result
    assert seen['song_audio'] == audio.path
    assert result['media']['song']['beats'] == 'local_audio'
    assert 'steady beat' not in result['message']
    # The song file was used too, so it goes with the clips.
    assert turn.store.query_inbox(UID, purpose='song_audio') == []
