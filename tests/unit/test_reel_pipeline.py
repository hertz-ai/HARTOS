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
    # The song file was used too, so it goes with the clips.
    assert turn.store.query_inbox(UID, purpose='song_audio') == []
