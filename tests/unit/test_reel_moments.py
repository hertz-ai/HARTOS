"""integrations.reel_studio.media.moments: the VLM's judgement of the keyframes.

At most 25 keyframes (pick_keyframes, in story order) are rated by this
node's vision model through VisionBackend.rate_frame: which makeup stage the
frame shows, how appealing it is, whether the face is visible.  The answer
is parsed with the shared VLM parser, normalised and clamped; the stage
labels are smoothed to a monotone progression over capture order (labels
only: the moments stay in the order they were filmed, because the reel is
told in capture order and ends on the reveal); and every frame
the model does not rate in time falls back to a deterministic score.

The VLM is a fake backend here; only its rate_frame boundary is replaced.
"""
import json
import math
import os
import time

import pytest

from integrations.reel_studio import contracts as C
from integrations.reel_studio.contracts import Box, Candidate, Moment, Quality
from integrations.reel_studio.media import moments as M
from tests.conftest import refuse_all_network


@pytest.fixture(autouse=True)
def _no_network(monkeypatch):
    refuse_all_network(monkeypatch, reason='test_reel_moments: the VLM is a fake')


XA = 'a' * 64
XB = 'b' * 64


def _kf(clip=XA, t=1.0, score=0.5, face=True, seg=0):
    return Candidate(clip=clip, t_s=t, segment=seg,
                     jpeg=b'\xff\xd8' + ('%s@%.3f' % (clip, t)).encode(),
                     quality=Quality(score=score, sharp=100.0, luma=0.5, clipped=0.0),
                     face=Box(0.4, 0.2, 0.2, 0.15) if face else None)


def _ans(stage, appeal=7, face=True):
    return json.dumps({'stage': stage, 'appeal': appeal, 'face_visible': face})


class FakeVLM:
    """A vision backend whose rate_frame answers from a script.

    ``answers``: a list (one per call, in call order) or a dict keyed by the
    frame's JPEG bytes.  An Exception in the script is raised.  ``clock`` and
    ``cost_s`` let a call take simulated time."""

    def __init__(self, answers, *, name='fake-vlm', clock=None, cost_s=0.0):
        self.answers = answers
        self.name = name
        self.clock = clock
        self.cost_s = cost_s
        self.calls = []

    def rate_frame(self, image_bytes, prompt, *, long_side=768, max_tokens=80, timeout_s=20.0):
        self.calls.append({'jpeg': image_bytes, 'prompt': prompt, 'long_side': long_side,
                           'max_tokens': max_tokens, 'timeout_s': timeout_s})
        if self.clock is not None:
            self.clock.t += self.cost_s
        if isinstance(self.answers, dict):
            answer = self.answers.get(image_bytes)
        else:
            answer = self.answers[len(self.calls) - 1]
        if isinstance(answer, Exception):
            raise answer
        return answer


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


def _stages(doc):
    return [m['stage'] for m in doc['moments']]


def _score(keyframes, backend, tmp_path, **kw):
    doc = M.score_moments(keyframes, backend=backend, cache_dir=str(tmp_path / 'cache'), **kw)
    assert C.validate_moments(doc) == [], C.validate_moments(doc)
    return doc


# ── parse_rating ──────────────────────────────────────────────────────────

def test_a_clean_answer_parses():
    assert M.parse_rating('{"stage": "eyes", "appeal": 7, "face_visible": true}') == {
        'stage': 'eyes', 'appeal': 7.0, 'face_visible': True}


def test_a_fenced_answer_parses():
    text = 'Here you go:\n```json\n{"stage": "Final Look", "appeal": "9/10", ' \
           '"face_visible": "yes"}\n```'
    assert M.parse_rating(text) == {'stage': 'final_look', 'appeal': 9.0, 'face_visible': True}


def test_an_answer_wrapped_in_prose_parses():
    text = 'Sure! My rating: {"Stage": "jewelry", "Appeal": 8.5, "face visible": false} Hope it helps.'
    assert M.parse_rating(text) == {'stage': 'jewellery', 'appeal': 8.5, 'face_visible': False}


@pytest.mark.parametrize('text', [
    None, '', '   ', 'I cannot rate this image.', '{"stage": }', '[1, 2, 3]',
    '{"appeal": 7, "face_visible": true}',                  # no stage
    '{"stage": "eyes", "face_visible": true}',              # no appeal
    '{"stage": "...", "appeal": 5}',                        # the template echoed back
    '{"stage": "", "appeal": 5}',
    '{"stage": 3, "appeal": 5}',
    '{"stage": "eyes", "appeal": "high"}',
    '{"stage": "eyes", "appeal": true}',
    '{"stage": "eyes", "appeal": NaN}',
    '{"stage": "eyes", "appeal": Infinity}',
    '{"stage": "eyes", "appeal": null}',
])
def test_garbage_is_a_miss(text):
    assert M.parse_rating(text) is None


@pytest.mark.parametrize('raw,stage', [
    ('bare_face', 'bare_face'), ('Bare face', 'bare_face'), ('no makeup', 'bare_face'),
    ('foundation', 'base'), ('Base makeup', 'base'), ('contouring', 'base'),
    ('eye makeup', 'eyes'), ('Eyeliner', 'eyes'), ('eyes and lips', 'eyes'),
    ('Lipstick', 'lips'), ('LIPS', 'lips'),
    ('hairstyle', 'hair'), ('hair styling', 'hair'),
    ('Jewelry', 'jewellery'), ('jewellery & dupatta', 'jewellery'), ('accessories', 'jewellery'),
    ('final', 'final_look'), ('Final Look', 'final_look'), ('FINAL_LOOK', 'final_look'),
    ('finished look', 'final_look'), ('final-look (bride ready)', 'final_look'),
    ('other', 'other'), ('a unicorn', 'other'),
])
def test_stage_names_are_normalised(raw, stage):
    rating = M.parse_rating(json.dumps({'stage': raw, 'appeal': 5}))
    assert rating is not None and rating['stage'] == stage


@pytest.mark.parametrize('raw,appeal', [
    (15, 10.0), (-2, 0.0), ('7.5', 7.5), ('8/10', 8.0), (' 6 ', 6.0), (0, 0.0), (10, 10.0),
])
def test_appeal_is_clamped_to_0_10(raw, appeal):
    assert M.parse_rating(json.dumps({'stage': 'lips', 'appeal': raw}))['appeal'] == appeal


@pytest.mark.parametrize('raw,face', [
    (True, True), (False, False), ('yes', True), ('No', False), ('true', True),
    ('false', False), (1, True), (0, False), ('maybe', None),
])
def test_face_visible_is_read_when_it_can_be(raw, face):
    rating = M.parse_rating(json.dumps({'stage': 'lips', 'appeal': 5, 'face_visible': raw}))
    assert rating['face_visible'] is face


def test_a_missing_face_flag_is_none_not_false():
    assert M.parse_rating('{"stage": "lips", "appeal": 5}')['face_visible'] is None


def test_the_rubric_asks_for_every_stage_the_parser_accepts():
    for stage in C.STAGES:
        assert stage in M.RUBRIC_PROMPT
    assert 'appeal' in M.RUBRIC_PROMPT and 'face_visible' in M.RUBRIC_PROMPT


# ── smooth_stages ─────────────────────────────────────────────────────────

def _m(stage, t, clip=XA, src='vlm', appeal=5.0, face=True):
    return Moment(clip=clip, t_s=t, stage=stage, appeal=appeal, face_visible=face, src=src)


def _run(stages, src='vlm'):
    srcs = src if isinstance(src, list) else [src] * len(stages)
    return [m.stage for m in M.smooth_stages(
        [_m(s, float(i), src=k) for i, (s, k) in enumerate(zip(stages, srcs))])]


def test_a_monotone_story_is_left_alone():
    story = ['bare_face', 'base', 'base', 'eyes', 'lips', 'hair', 'jewellery', 'final_look']
    assert _run(story) == story


def test_an_early_final_look_is_corrected_to_its_nearest_neighbour():
    assert _run(['bare_face', 'final_look', 'base', 'eyes', 'lips', 'final_look']) == [
        'bare_face', 'base', 'base', 'eyes', 'lips', 'final_look']


def test_a_late_bare_face_is_corrected():
    assert _run(['bare_face', 'base', 'eyes', 'bare_face', 'lips']) == [
        'bare_face', 'base', 'eyes', 'eyes', 'lips']


def test_other_frames_are_untouched_and_do_not_constrain():
    story = ['bare_face', 'other', 'base', 'other', 'final_look', 'other']
    assert _run(story) == story


def test_a_tie_resolves_toward_progress_so_the_story_still_ends_on_the_look():
    # The model saw the finished look, then called the last frame
    # 'jewellery'.  Either label could be the wrong one; the finished look
    # is filmed last, so the later frame keeps the progress.
    assert _run(['bare_face', 'base', 'final_look', 'jewellery']) == [
        'bare_face', 'base', 'final_look', 'final_look']


def test_a_position_guess_yields_to_the_model():
    stages = ['bare_face', 'lips', 'eyes', 'final_look']
    srcs = ['vlm', 'vlm', 'fallback', 'fallback']
    assert _run(stages, srcs) == ['bare_face', 'lips', 'lips', 'final_look']


def test_smoothing_changes_labels_only_never_order_or_anything_else():
    given = [_m('final_look', 0.5, appeal=9.0), _m('lips', 2.0, clip=XB, src='fallback', face=False),
             _m('bare_face', 9.0, clip=XB, appeal=2.5)]
    out = M.smooth_stages(given)
    assert [(m.clip, m.t_s, m.appeal, m.face_visible, m.src) for m in out] == [
        (m.clip, m.t_s, m.appeal, m.face_visible, m.src) for m in given]
    ranks = [C.PROGRESSION.index(m.stage) for m in out]
    assert ranks == sorted(ranks)
    assert [m.stage for m in given] == ['final_look', 'lips', 'bare_face']   # input untouched


def test_smoothing_is_idempotent_and_handles_nothing():
    once = M.smooth_stages([_m(s, float(i)) for i, s in enumerate(
        ['eyes', 'bare_face', 'final_look', 'lips', 'other', 'base'])])
    assert M.smooth_stages(once) == once
    assert M.smooth_stages([]) == []
    assert _run(['other', 'other']) == ['other', 'other']


# ── fallback_moments ──────────────────────────────────────────────────────

def test_the_fallback_reads_the_stage_from_capture_order():
    keys = [_kf(t=float(i), score=0.1 * i, face=(i % 2 == 0)) for i in range(7)]
    out = M.fallback_moments(keys)
    assert [m.stage for m in out] == list(C.PROGRESSION)
    assert [m.src for m in out] == ['fallback'] * 7
    assert [m.appeal for m in out] == pytest.approx([10 * 0.1 * i for i in range(7)])
    assert [m.face_visible for m in out] == [i % 2 == 0 for i in range(7)]
    assert [(m.clip, m.t_s) for m in out] == [(k.clip, k.t_s) for k in keys]


def test_only_the_last_filmed_keyframe_is_the_fallback_final_look():
    out = M.fallback_moments([_kf(t=float(i)) for i in range(25)])
    stages = [m.stage for m in out]
    assert stages[0] == 'bare_face' and stages[-1] == 'final_look'
    assert stages.count('final_look') == 1
    ranks = [C.PROGRESSION.index(s) for s in stages]
    assert ranks == sorted(ranks)
    assert [m.stage for m in M.fallback_moments([_kf()])] == ['final_look']
    assert M.fallback_moments([]) == []


def test_a_keyframe_without_quality_falls_back_to_zero_appeal():
    key = Candidate(clip=XA, t_s=0.0, segment=0, jpeg=b'\xff\xd8x')
    assert M.fallback_moments([key])[0].appeal == 0.0


# ── score_moments ─────────────────────────────────────────────────────────

def test_every_frame_rated_by_the_model(tmp_path):
    keys = [_kf(t=1.0, score=0.5), _kf(t=4.0, score=0.8), _kf(clip=XB, t=2.0, score=0.2, face=False)]
    vlm = FakeVLM([_ans('bare_face', 3), _ans('lips', 7, False),
                   '{"stage": "final_look", "appeal": 9}'])
    doc = _score(keys, vlm, tmp_path)
    assert doc['schema'] == C.MOMENTS_SCHEMA
    assert doc['src'] == 'vlm' and doc['model'] == 'fake-vlm'
    assert doc['final_look_found'] is True
    assert doc['notes'] == []
    assert _stages(doc) == ['bare_face', 'lips', 'final_look']
    # appeal = 0.6 * the model's + 0.4 * (10 * quality)
    assert [m['appeal'] for m in doc['moments']] == pytest.approx(
        [0.6 * 3 + 0.4 * 5, 0.6 * 7 + 0.4 * 8, 0.6 * 9 + 0.4 * 2])
    # The model's face flag when it gave one, the Haar box when it did not.
    assert [m['face_visible'] for m in doc['moments']] == [True, False, False]
    assert [(m['clip'], m['t_s']) for m in doc['moments']] == [(k.clip, k.t_s) for k in keys]
    assert doc['stats']['frames'] == 3 and doc['stats']['vlm'] == 3


def test_each_frame_goes_with_the_rubric_at_768_px_and_80_tokens(tmp_path):
    keys = [_kf(t=float(i)) for i in range(3)]
    vlm = FakeVLM([_ans('base')] * 3)
    _score(keys, vlm, tmp_path)
    assert [c['jpeg'] for c in vlm.calls] == [k.jpeg for k in keys]
    for call in vlm.calls:
        assert call['prompt'] == M.RUBRIC_PROMPT
        assert call['long_side'] == 768
        assert call['max_tokens'] == 80


def test_the_model_down_means_every_frame_falls_back(tmp_path):
    keys = [_kf(t=float(i), score=0.3) for i in range(5)]
    vlm = FakeVLM([None] * 5)
    doc = _score(keys, vlm, tmp_path)
    assert doc['src'] == 'fallback' and doc['model'] is None
    assert M.NOTE_VLM_FALLBACK in doc['notes']
    # Capture order: the last frame filmed is the look, the frames before it
    # cover the six process stages in equal steps.
    assert _stages(doc) == ['bare_face', 'base', 'lips', 'hair', 'final_look']
    assert [m['appeal'] for m in doc['moments']] == pytest.approx([3.0] * 5)
    assert doc['final_look_found'] is True


def test_an_answer_that_does_not_parse_falls_back_for_that_frame_only(tmp_path):
    keys = [_kf(t=float(i)) for i in range(4)]
    vlm = FakeVLM([_ans('bare_face'), 'A beautiful bride!', '', _ans('final_look', 9)])
    doc = _score(keys, vlm, tmp_path)
    assert doc['src'] == 'mixed' and doc['model'] == 'fake-vlm'
    assert [m['src'] for m in doc['moments']] == ['vlm', 'fallback', 'fallback', 'vlm']
    assert M.NOTE_VLM_PARTIAL in doc['notes']
    assert _stages(doc)[0] == 'bare_face' and _stages(doc)[-1] == 'final_look'


def test_a_backend_that_raises_is_a_fallback_not_a_crash(tmp_path):
    keys = [_kf(t=float(i)) for i in range(3)]
    vlm = FakeVLM([RuntimeError('boom'), _ans('lips'), ValueError('bad')])
    doc = _score(keys, vlm, tmp_path)
    assert [m['src'] for m in doc['moments']] == ['fallback', 'vlm', 'fallback']


def test_the_budget_runs_out_and_the_rest_fall_back(tmp_path, monkeypatch):
    clock = Clock()
    monkeypatch.setattr(M, 'monotonic', clock)
    keys = [_kf(t=float(i)) for i in range(6)]
    vlm = FakeVLM([_ans('base')] * 6, clock=clock, cost_s=4.0)
    doc = _score(keys, vlm, tmp_path, budget_s=12.0, per_call_cap_s=5.0)
    # Each call gets min(what is left of the budget, the cap).
    assert [c['timeout_s'] for c in vlm.calls] == pytest.approx([5.0, 5.0, 4.0])
    assert [m['src'] for m in doc['moments']] == ['vlm'] * 3 + ['fallback'] * 3
    assert M.NOTE_VLM_BUDGET in doc['notes']


def test_the_per_call_timeout_is_the_cap_while_the_budget_is_large(tmp_path, monkeypatch):
    clock = Clock()
    monkeypatch.setattr(M, 'monotonic', clock)
    vlm = FakeVLM([_ans('base')] * 3, clock=clock, cost_s=1.0)
    _score([_kf(t=float(i)) for i in range(3)], vlm, tmp_path)
    assert M.BUDGET_S == 90.0 and M.PER_CALL_CAP_S == 20.0
    assert [c['timeout_s'] for c in vlm.calls] == [20.0, 20.0, 20.0]


def test_no_call_starts_with_less_than_a_call_s_worth_left(tmp_path, monkeypatch):
    clock = Clock()
    monkeypatch.setattr(M, 'monotonic', clock)
    vlm = FakeVLM([_ans('base')] * 3, clock=clock, cost_s=9.5)
    doc = _score([_kf(t=float(i)) for i in range(3)], vlm, tmp_path, budget_s=10.0)
    assert len(vlm.calls) == 1                  # 0.5 s left is under MIN_CALL_S
    assert M.MIN_CALL_S == 1.0
    assert [m['src'] for m in doc['moments']] == ['vlm', 'fallback', 'fallback']


def test_a_zero_budget_asks_nobody(tmp_path):
    vlm = FakeVLM([])
    doc = _score([_kf(t=1.0), _kf(t=2.0)], vlm, tmp_path, budget_s=0)
    assert vlm.calls == [] and doc['src'] == 'fallback'
    assert M.NOTE_VLM_BUDGET in doc['notes']


@pytest.mark.parametrize('kw', [dict(per_call_cap_s=0), dict(per_call_cap_s=-1),
                                dict(budget_s=-1), dict(budget_s=float('nan')),
                                dict(per_call_cap_s=float('inf'))])
def test_nonsense_budgets_are_refused(tmp_path, kw):
    with pytest.raises(ValueError):
        M.score_moments([_kf()], backend=FakeVLM([_ans('base')]),
                        cache_dir=str(tmp_path), **kw)


def test_a_cache_hit_makes_no_second_call(tmp_path):
    keys = [_kf(t=float(i)) for i in range(3)]
    first = FakeVLM([_ans('bare_face', 4), _ans('lips', 6), _ans('final_look', 9)])
    doc1 = _score(keys, first, tmp_path)
    again = FakeVLM([])
    doc2 = _score(keys, again, tmp_path)
    assert again.calls == []
    assert doc2['moments'] == doc1['moments'] and doc2['src'] == 'vlm'
    assert doc2['stats']['cached'] == 3


def test_a_failed_rating_is_not_cached(tmp_path):
    keys = [_kf(t=1.0)]
    _score(keys, FakeVLM(['no idea']), tmp_path)
    retry = FakeVLM([_ans('eyes')])
    doc = _score(keys, retry, tmp_path)
    assert len(retry.calls) == 1 and doc['src'] == 'vlm'


def test_another_model_does_not_reuse_the_cache(tmp_path):
    keys = [_kf(t=1.0)]
    _score(keys, FakeVLM([_ans('eyes')], name='qwen08b'), tmp_path)
    other = FakeVLM([_ans('lips')], name='qwen3vl')
    assert _stages(_score(keys, other, tmp_path)) == ['lips']
    assert len(other.calls) == 1


def test_a_keyframe_without_jpeg_bytes_falls_back_without_raising(tmp_path):
    broken = Candidate(clip=XA, t_s=1.0, segment=0, jpeg=None,
                       quality=Quality(score=0.4, sharp=1.0, luma=0.5, clipped=0.0))
    vlm = FakeVLM([None, _ans('final_look', 9)])
    doc = _score([broken, _kf(t=2.0)], vlm, tmp_path)
    assert [m['src'] for m in doc['moments']] == ['fallback', 'vlm']


def test_a_damaged_cache_entry_is_a_miss(tmp_path):
    keys = [_kf(t=1.0)]
    _score(keys, FakeVLM([_ans('eyes')]), tmp_path)
    cache = tmp_path / 'cache'
    files = [p for p in cache.iterdir() if p.suffix == '.json']
    assert len(files) == 1
    files[0].write_text('{"stage": "nonsense", "appe', encoding='utf-8')
    retry = FakeVLM([_ans('lips')])
    assert _stages(_score(keys, retry, tmp_path)) == ['lips']
    assert len(retry.calls) == 1


def test_the_cache_lives_under_the_data_dir_by_default(tmp_path, monkeypatch):
    monkeypatch.setattr('core.platform_paths.get_data_dir', lambda: str(tmp_path))
    M.score_moments([_kf(t=1.0)], backend=FakeVLM([_ans('eyes')]))
    where = tmp_path / 'reel_studio' / 'cache' / 'moments'
    assert len([p for p in where.iterdir() if p.suffix == '.json']) == 1


def test_old_cache_entries_are_pruned(tmp_path):
    cache = tmp_path / 'cache'
    cache.mkdir()
    old, fresh = cache / ('0' * 64 + '.json'), cache / ('1' * 64 + '.json')
    for p in (old, fresh):
        p.write_text('{}', encoding='utf-8')
    stale = time.time() - M.CACHE_MAX_AGE_S - 60
    os.utime(old, (stale, stale))
    other = cache / 'keep.txt'
    other.write_text('not ours', encoding='utf-8')
    os.utime(other, (stale, stale))
    _score([_kf(t=1.0)], FakeVLM([_ans('eyes')]), tmp_path)
    assert not old.exists() and fresh.exists() and other.exists()


def test_the_default_backend_is_reel_vlm_default(tmp_path, monkeypatch):
    import integrations.vision.lightweight_backend as lvb
    asked = []
    vlm = FakeVLM([_ans('eyes')])
    monkeypatch.setattr(lvb, 'get_vision_backend', lambda name='': asked.append(name) or vlm)
    doc = M.score_moments([_kf()], cache_dir=str(tmp_path))
    assert asked == [M.REEL_VLM_DEFAULT]
    assert M.REEL_VLM_DEFAULT in lvb._BACKENDS
    assert doc['src'] == 'vlm'


def test_a_backend_that_cannot_be_resolved_falls_back(tmp_path, monkeypatch):
    import integrations.vision.lightweight_backend as lvb

    def _boom(name=''):
        raise RuntimeError('no vision here')
    monkeypatch.setattr(lvb, 'get_vision_backend', _boom)
    doc = M.score_moments([_kf(t=1.0), _kf(t=2.0)], cache_dir=str(tmp_path))
    assert doc['src'] == 'fallback' and M.NOTE_VLM_FALLBACK in doc['notes']
    assert C.validate_moments(doc) == []


def test_no_keyframes_is_an_empty_valid_list(tmp_path):
    vlm = FakeVLM([])
    doc = _score([], vlm, tmp_path)
    assert doc['moments'] == [] and doc['final_look_found'] is False and vlm.calls == []


# ── stage labels never reorder the story ──────────────────────────────────

def test_the_moments_stay_in_the_order_they_were_filmed(tmp_path):
    # Clip A was filmed first, clip B after it; pick_keyframes hands them
    # over in that order.  The model calls the first frame the final look
    # and the last one bare face: the labels are smoothed, the order is not.
    keys = [_kf(XA, 1.0), _kf(XA, 5.0), _kf(XB, 2.0), _kf(XB, 8.0)]
    vlm = FakeVLM([_ans('final_look', 9), _ans('lips'), _ans('base'), _ans('bare_face', 2)])
    doc = _score(keys, vlm, tmp_path)
    assert [(m['clip'], m['t_s']) for m in doc['moments']] == [(XA, 1.0), (XA, 5.0),
                                                                 (XB, 2.0), (XB, 8.0)]
    ranks = [C.PROGRESSION.index(s) for s in _stages(doc)]
    assert ranks == sorted(ranks)
    assert M.NOTE_STAGES_SMOOTHED in doc['notes']


def test_appeal_and_numbers_are_finite_and_in_range(tmp_path):
    keys = [_kf(t=float(i), score=s) for i, s in enumerate([0.0, 1.0, 0.5])]
    doc = _score(keys, FakeVLM([_ans('base', 10), _ans('eyes', 0), None]), tmp_path)
    for m in doc['moments']:
        assert math.isfinite(m['appeal']) and 0.0 <= m['appeal'] <= 10.0
