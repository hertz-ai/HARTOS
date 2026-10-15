"""integrations.reel_studio.media.moments -- the VLM's judgement of the keyframes.

``score_moments`` asks this node's vision model about each keyframe
(keyframes.pick_keyframes: at most 25, in story order) through
VisionBackend.rate_frame: which makeup stage the frame shows, how appealing
it is, whether the face can be seen.  The answer is JSON requested in the
prompt (RUBRIC_PROMPT), parsed with the shared VLM parser
(integrations.vlm.parser.extract_json), normalised and clamped
(``parse_rating``).

Judgement is needed for "is this the finished bridal look?" and "how good
does this frame look?"; nothing else in the editor uses a model.  Every
frame the model does not answer for -- server down, a prose answer, the
time budget spent -- falls back on its own:

* stage from capture order: the last keyframe filmed is the final look and
  the ones before it cover the six process stages in equal steps;
* appeal = 10 * Quality.score (the analysis pass's sharpness and exposure);
* face_visible from the Haar face box the analysis pass found.

A rated frame's appeal is 0.6 * the model's + 0.4 * 10 * Quality.score.

Stage labels are then smoothed to a monotone progression over the story
(``smooth_stages``): bare_face -> base -> eyes -> lips -> hair -> jewellery
-> final_look, with 'other' left alone.  A small model's slips (a finished
looking mid-process frame called the final look, a late frame called bare
face) are corrected deterministically.  Smoothing changes LABELS only: the
moments stay in the order they were filmed, because the reel is told in
capture order and ends on the reveal.  Stage labels may later decide which
shots are dropped or kept per stage; they never reorder the story.

The requests are bounded: BUDGET_S for the whole list, and each request
gets min(what is left, PER_CALL_CAP_S) as its timeout.  rate_frame never
starts a model server; just before the first frame that is not cached,
score_moments asks the backend's own existing start once
(VisionBackend.ensure_running, the one describe() uses; for the 0.8B the
'vlm_caption.requested' event Nunba answers with start_caption_server, then
the standalone launch; amendment A13).  That start keeps its own waits: the
time it takes COUNTS AGAINST BUDGET_S and is NOT capped by it, so a slow
start leaves the frames less time or none, and the step can run past
BUDGET_S.  A reel whose frames are all cached asks for no start.

Ratings are cached under the data dir (<data>/reel_studio/cache/moments/),
keyed by the frame's bytes, the backend, the prompt and the request size;
only parsed ratings are cached, never a fallback, and entries older than
CACHE_MAX_AGE_S are pruned.

The result is a ``hart.reel.moments.v1`` document (contracts.
validate_moments), plus ``stats`` (frames, rated, cached, calls, seconds).
"""
from __future__ import annotations

import hashlib
import json
import logging
import math
import os
import re
import time
from dataclasses import replace
from time import monotonic
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence

from core.config_cache import flag_value
from core.file_cache import atomic_json_write
from integrations.reel_studio.contracts import (
    FINAL_LOOK, MOMENTS_SCHEMA, PROGRESSION, STAGES, Keyframe, Moment)
from integrations.reel_studio.media import decode
from integrations.vlm.parser import extract_json

logger = logging.getLogger(__name__)

#: The vision backend (integrations.vision.lightweight_backend._BACKENDS)
#: that rates keyframes when the caller names none: the Qwen3.5-0.8B caption
#: server on this machine (127.0.0.1, its 'vlm_caption' port), so a bride's
#: face never leaves the desktop.  To be revisited once 30 hand-labelled
#: frames from real makeup clips have been scored on the 0.8B and the 2B
#: main model (accuracy and seconds per frame).
REEL_VLM_DEFAULT = 'qwen08b'

#: The keyframes are JPEGs at the decode long side (the analysis pass keeps
#: them at that size), so the model sees them unscaled: about 336 image
#: tokens for a 432x768 portrait frame on Qwen3.5.
LONG_SIDE = decode.LONG_SIDE
#: The answer is one short JSON object (about 25 tokens).
MAX_TOKENS = 80
#: Seconds for the whole keyframe list, and the most one request may take.
BUDGET_S = 90.0
PER_CALL_CAP_S = 20.0
#: No request starts with less than this left of the budget: a frame takes
#: about half a second to a second on the 0.8B, so less is a wasted call.
MIN_CALL_S = 1.0
#: appeal = VLM_WEIGHT * the model's + (1 - VLM_WEIGHT) * 10 * Quality.score.
VLM_WEIGHT = 0.6
#: Cached ratings outlive the source clips (deleted a day after a reel) only
#: long enough for a re-run of the same reel.
CACHE_MAX_AGE_S = 7 * 24 * 3600
_CACHE_VERSION = 1
_CACHE_NAME = re.compile(r'[0-9a-f]{64}\.json')

#: Notes the moment list may carry.
NOTE_VLM_FALLBACK = 'vlm_fallback'      # no keyframe was rated: all fell back
NOTE_VLM_PARTIAL = 'vlm_partial'        # some keyframes fell back
NOTE_VLM_BUDGET = 'vlm_budget'          # the time budget ran out before the end
NOTE_STAGES_SMOOTHED = 'stages_smoothed'  # a label the model gave was corrected

#: What each stage means, for the prompt.  Keyed by contracts.STAGES, so the
#: prompt names exactly the labels the parser accepts.
_STAGE_HINTS = {
    'bare_face': 'no makeup yet',
    'base': 'foundation, concealer, contour or blush',
    'eyes': 'eyeshadow, liner, lashes or brows',
    'lips': 'lip colour',
    'hair': 'the hair being styled',
    'jewellery': 'jewellery, dupatta or accessories going on',
    'final_look': 'the finished look, shown off',
    'other': 'no face, or not about the makeup',
}

#: The rubric every keyframe is rated with.  Part of the cache key: changing
#: it re-rates frames.
#:
#: appeal asks for ONE bare whole number.  Until 2026-10-15 the template read
#: '"appeal": <1-10>' and the bands '9-10 finished, sharp, ...'; the 0.8B
#: (REEL_VLM_DEFAULT) copied the first band, words and all, into appeal on 3
#: of 3 frames (A13 live proof, CPU) and 8 of 11 (shared GPU server), so
#: every such frame fell back.  With this wording it answered a bare number
#: on 11 of 11 of the same frames.  The parser still reads a band it copies
#: (``_appeal``), so either guard alone keeps the model's answer.
RUBRIC_PROMPT = (
    'This is one frame from a video of a makeup artist doing a bridal look. '
    'Answer with ONLY this JSON object and nothing else:\n'
    '{"stage": "<stage>", "appeal": <whole number>, "face_visible": <true or false>}\n'
    'stage is the step the frame shows, one of: '
    + '; '.join('%s (%s)' % (s, _STAGE_HINTS[s]) for s in STAGES) + '.\n'
    'appeal is one whole number from 1 to 10, written as a bare number: how good '
    'the frame would look in a reel (9 or 10: finished, sharp, well lit, face '
    'clearly visible; 5 or 6: ordinary; 1 to 3: blurry, dark or the face hidden).\n'
    'face_visible is true when the face can be clearly seen.'
)

#: Other words a model uses for a stage (after _token).
_STAGE_SYNONYMS = {
    'bare': 'bare_face', 'barefaced': 'bare_face', 'bare_faced': 'bare_face',
    'no_makeup': 'bare_face', 'without_makeup': 'bare_face', 'natural': 'bare_face',
    'base_makeup': 'base', 'foundation': 'base', 'concealer': 'base', 'contour': 'base',
    'contouring': 'base', 'blush': 'base', 'highlighter': 'base', 'primer': 'base',
    'prep': 'base', 'skin_prep': 'base', 'powder': 'base', 'complexion': 'base',
    'eye': 'eyes', 'eye_makeup': 'eyes', 'eyeshadow': 'eyes', 'eye_shadow': 'eyes',
    'eyeliner': 'eyes', 'eye_liner': 'eyes', 'liner': 'eyes', 'kajal': 'eyes',
    'kohl': 'eyes', 'mascara': 'eyes', 'lashes': 'eyes', 'eyelashes': 'eyes',
    'brows': 'eyes', 'eyebrows': 'eyes',
    'lip': 'lips', 'lipstick': 'lips', 'lip_color': 'lips', 'lip_colour': 'lips',
    'lipliner': 'lips', 'lip_liner': 'lips', 'gloss': 'lips', 'lip_gloss': 'lips',
    'hairstyle': 'hair', 'hair_style': 'hair', 'hairdo': 'hair', 'hairstyling': 'hair',
    'hair_styling': 'hair', 'bun': 'hair', 'braid': 'hair',
    'jewelry': 'jewellery', 'jewelery': 'jewellery', 'jewels': 'jewellery',
    'jewel': 'jewellery', 'accessories': 'jewellery', 'ornaments': 'jewellery',
    'dupatta': 'jewellery', 'veil': 'jewellery',
    'final': 'final_look', 'finished': 'final_look', 'finished_look': 'final_look',
    'complete': 'final_look', 'completed': 'final_look', 'done': 'final_look',
    'final_makeup': 'final_look', 'reveal': 'final_look',
}

#: The keys a model uses for each field (after _token).
_FIELD_KEYS = {
    'stage': ('stage', 'makeup_stage', 'step'),
    'appeal': ('appeal', 'score', 'rating'),
    'face_visible': ('face_visible', 'facevisible', 'face'),
}
#: An appeal written as text: a LEADING number ('7', '7.5', '8/10') or range
#: ('9-10', with an en or em dash, '9 to 10', '9 or 10'), then anything (the
#: band's own words a model copied out of the rubric).
_APPEAL_TEXT = re.compile(
    r'\s*([-+]?\d+(?:\.\d+)?)'
    r'(?:\s*(?:-|–|—|to|or)\s*(\d+(?:\.\d+)?))?'
    r'\s*(?:/\s*10)?(?!\d)', re.IGNORECASE)

#: What changing one label costs the smoothing: a label the model gave is
#: worth four position guesses (fallback frames' capture-order stages).
_RELABEL_COST = {'vlm': 4, 'fallback': 1}


# ── parsing ───────────────────────────────────────────────────────────────

def _token(text: str) -> str:
    """'Final Look (bride)' -> 'final_look_bride'."""
    return re.sub(r'[^a-z0-9]+', '_', text.lower()).strip('_')


def _stage(value) -> Optional[str]:
    """A stage label, normalised; 'other' for a word no stage has; None when
    there is no word at all (missing, empty, the template's '...')."""
    if not isinstance(value, str):
        return None
    token = _token(value)
    if not token:
        return None
    words = token.split('_')
    for candidate in (token, '_'.join(words[:2]), words[0]):
        if candidate in STAGES:
            return candidate
        if candidate in _STAGE_SYNONYMS:
            return _STAGE_SYNONYMS[candidate]
    return 'other'


def _appeal(value) -> Optional[float]:
    """A number in 0..10 (clamped), or None.

    From 7, 7.5, '7', '8/10', or text that STARTS with a number or a range:
    '7 ordinary' is 7; a range is its midpoint, so the rubric's own band
    copied into the field, '9-10 finished, sharp, well lit, face clearly
    visible', is 9.5 ('1 to 3 ...' is 2.0, '9 or 10' 9.5).  The whole scale
    ('1-10', '1 to 10': the template echoed back) says nothing and is None,
    and so is text with no leading number ('high', 'about 7').
    """
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        number = float(value)
    elif isinstance(value, str):
        match = _APPEAL_TEXT.match(value)
        if match is None:
            return None
        number = float(match.group(1))
        if match.group(2) is not None:
            low, high = sorted((number, float(match.group(2))))
            if low <= 1 and high >= 10:
                return None
            number = (low + high) / 2.0
    else:
        return None
    if not math.isfinite(number):
        return None
    return min(10.0, max(0.0, number))


def _flag(value) -> Optional[bool]:
    """face_visible as the model said it, or None when it did not say.

    Booleans and words are read by HARTOS's one yes/no table
    (core.config_cache.flag_value).  A number is an answer only as 0 or 1: a
    model's 0.7 is a confidence, not a yes, so the Haar face box decides
    (flag_value reads any non-zero number as yes, right for a setting, wrong
    for a model's score)."""
    if isinstance(value, (int, float)) and not isinstance(value, bool) and value not in (0, 1):
        return None
    return flag_value(value, None)


def _rating(data: Any) -> Optional[Dict[str, Any]]:
    """{'stage', 'appeal', 'face_visible'} from a parsed object, or None
    unless it has both a stage word and a usable appeal."""
    if not isinstance(data, Mapping):
        return None
    fields: Dict[str, Any] = {}
    for key, value in data.items():
        if isinstance(key, str):
            fields.setdefault(_token(key), value)

    def pick(name):
        return next((fields[k] for k in _FIELD_KEYS[name] if k in fields), None)

    stage = _stage(pick('stage'))
    appeal = _appeal(pick('appeal'))
    if stage is None or appeal is None:
        return None
    return {'stage': stage, 'appeal': appeal, 'face_visible': _flag(pick('face_visible'))}


def parse_rating(text: Optional[str]) -> Optional[Dict[str, Any]]:
    """The model's answer as ``{'stage', 'appeal', 'face_visible'}``, or None.

    The JSON may be fenced or wrapped in prose (extract_json finds it).
    ``stage`` is normalised to contracts.STAGES (synonyms such as 'jewelry',
    'eye makeup', 'finished look'; an unknown word is 'other'); ``appeal`` is
    a number clamped to 0..10, also read from text that starts with a number
    or a range (a range is its midpoint: a copied band '9-10 finished, ...'
    is 9.5; see _appeal); ``face_visible`` is True, False or None when the
    model did not say.  No stage or no usable appeal is a miss (None), and
    the frame falls back.
    """
    if not isinstance(text, str) or not text.strip():
        return None
    return _rating(extract_json(text))


# ── stages ────────────────────────────────────────────────────────────────

def _monotone_ranks(observed: Sequence[int], costs: Sequence[int]) -> List[int]:
    """Non-decreasing ranks (indexes into PROGRESSION) closest to ``observed``.

    Cheapest first by the summed ``costs`` of the labels changed, then by
    the total rank distance moved.  Among equal answers, later positions
    keep the higher rank: the transformation moves forward, and the finished
    look is filmed last, so a tie is not settled by un-finishing the end of
    the story.  Exact (integer) dynamic programming over at most
    len(PROGRESSION) ranks per position.
    """
    k = len(PROGRESSION)
    best: List[List[tuple]] = []
    for j, (seen, cost) in enumerate(zip(observed, costs)):
        row = []
        run = (math.inf, math.inf)
        for rank in range(k):
            step = (cost if rank != seen else 0, abs(rank - seen))
            if j == 0:
                row.append(step)
                continue
            run = min(run, best[j - 1][rank])
            row.append((run[0] + step[0], run[1] + step[1]))
        best.append(row)
    ranks = [0] * len(observed)
    ceiling = k - 1
    for j in range(len(observed) - 1, -1, -1):
        cheapest = min(best[j][:ceiling + 1])
        ceiling = max(r for r in range(ceiling + 1) if best[j][r] == cheapest)
        ranks[j] = ceiling
    return ranks


def smooth_stages(moments: Sequence[Moment]) -> List[Moment]:
    """The moments with their stage labels made a monotone progression.

    ``moments`` are in story order (the order the keyframes were filmed:
    clips by capture time, frames by time within a clip), and so is the
    result: same length, same order, same clip, time, appeal, face flag and
    source.  Only ``stage`` may change.  'other' frames keep their label
    and do not constrain the rest.  A label the model gave costs more to
    change than a fallback frame's capture-order guess (_RELABEL_COST).
    """
    out = list(moments)
    where = [i for i, m in enumerate(out) if m.stage in PROGRESSION]
    if not where:
        return out
    ranks = _monotone_ranks([PROGRESSION.index(out[i].stage) for i in where],
                            [_RELABEL_COST.get(out[i].src, 1) for i in where])
    for i, rank in zip(where, ranks):
        if PROGRESSION[rank] != out[i].stage:
            out[i] = replace(out[i], stage=PROGRESSION[rank])
    return out


def _position_stage(index: int, count: int) -> str:
    """A keyframe's stage from its place in the story: the last one is the
    final look, the ones before it cover the process stages in equal steps."""
    if index >= count - 1:
        return FINAL_LOOK
    return PROGRESSION[index * (len(PROGRESSION) - 1) // (count - 1)]


def _quality10(keyframe: Keyframe) -> Optional[float]:
    quality = getattr(keyframe, 'quality', None)
    score = getattr(quality, 'score', None)
    if isinstance(score, bool) or not isinstance(score, (int, float)) \
            or not math.isfinite(score):
        return None
    return 10.0 * min(1.0, max(0.0, float(score)))


def _fallback_moment(keyframe: Keyframe, stage: str) -> Moment:
    quality = _quality10(keyframe)
    return Moment(clip=keyframe.clip, t_s=float(keyframe.t_s), stage=stage,
                  appeal=round(quality, 3) if quality is not None else 0.0,
                  face_visible=keyframe.face is not None, src='fallback')


def _rated_moment(keyframe: Keyframe, rating: Mapping[str, Any]) -> Moment:
    quality = _quality10(keyframe)
    appeal = rating['appeal'] if quality is None else (
        VLM_WEIGHT * rating['appeal'] + (1.0 - VLM_WEIGHT) * quality)
    face = rating['face_visible']
    return Moment(clip=keyframe.clip, t_s=float(keyframe.t_s), stage=rating['stage'],
                  appeal=round(min(10.0, max(0.0, appeal)), 3),
                  face_visible=face if face is not None else keyframe.face is not None,
                  src='vlm')


def fallback_moments(keyframes: Sequence[Keyframe]) -> List[Moment]:
    """Every keyframe scored without a model, in the order given (story order):
    stage from capture order, appeal = 10 * Quality.score (0 without one),
    face_visible from the Haar face box, src 'fallback'."""
    keys = list(keyframes)
    return [_fallback_moment(k, _position_stage(i, len(keys))) for i, k in enumerate(keys)]


# ── the cache ─────────────────────────────────────────────────────────────

class _RatingCache:
    """Parsed ratings on disk, one JSON file per (frame, backend, prompt)."""

    def __init__(self, root: str, model: str):
        self.root = root
        self.model = model

    def _path(self, jpeg: bytes) -> str:
        key = json.dumps([_CACHE_VERSION, hashlib.sha256(jpeg).hexdigest(), self.model,
                          RUBRIC_PROMPT, LONG_SIDE, MAX_TOKENS])
        return os.path.join(self.root, hashlib.sha256(key.encode('utf-8')).hexdigest() + '.json')

    def get(self, jpeg: bytes) -> Optional[Dict[str, Any]]:
        path = None
        try:
            path = self._path(jpeg)
            with open(path, encoding='utf-8') as f:
                return _rating(json.load(f))
        except FileNotFoundError:
            return None
        except Exception as e:      # damaged entry: a miss, rewritten after the call
            logger.warning('moments: cached rating %s unreadable (%s)', path, e)
            return None

    def put(self, jpeg: bytes, rating: Mapping[str, Any]) -> None:
        try:
            atomic_json_write(self._path(jpeg), dict(rating, model=self.model))
        except Exception as e:
            logger.warning('moments: rating not cached (%s)', e)

    def prune(self, max_age_s: float = CACHE_MAX_AGE_S) -> int:
        """Delete this cache's entries older than ``max_age_s``; other files
        are left alone.  Best effort: returns how many were removed."""
        removed = 0
        horizon = time.time() - max_age_s
        try:
            names = os.listdir(self.root)
        except OSError:
            return 0
        for name in names:
            if not _CACHE_NAME.fullmatch(name):
                continue
            path = os.path.join(self.root, name)
            try:
                if os.path.getmtime(path) < horizon:
                    os.remove(path)
                    removed += 1
            except OSError:
                pass
        return removed


def _cache_root(cache_dir: Optional[str]) -> Optional[str]:
    if cache_dir:
        return cache_dir
    try:
        from core.platform_paths import get_data_dir
        return os.path.join(get_data_dir(), 'reel_studio', 'cache', 'moments')
    except Exception as e:
        logger.warning('moments: no cache dir (%s); every frame is asked', e)
        return None


# ── scoring ───────────────────────────────────────────────────────────────

def _seconds(name: str, value, *, positive: bool) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) \
            or not math.isfinite(value) or value < 0 or (positive and value == 0):
        raise ValueError('%s must be a finite number %s 0, got %r'
                         % (name, '>' if positive else '>=', value))
    return float(value)


def _default_backend():
    """get_vision_backend(REEL_VLM_DEFAULT), or None when there is none."""
    try:
        from integrations.vision.lightweight_backend import get_vision_backend
        return get_vision_backend(REEL_VLM_DEFAULT)
    except Exception as e:
        logger.warning('moments: no vision backend (%s); every keyframe falls back', e)
        return None


def _backend_name(backend) -> str:
    name = getattr(backend, 'name', None)
    return name if isinstance(name, str) and name else type(backend).__name__


def _ask(backend, keyframe: Keyframe, timeout_s: float) -> Optional[str]:
    try:
        return backend.rate_frame(keyframe.jpeg, RUBRIC_PROMPT, long_side=LONG_SIDE,
                                  max_tokens=MAX_TOKENS, timeout_s=timeout_s)
    except Exception as e:
        logger.warning('moments: rate_frame raised (%s); the frame falls back', e)
        return None


def score_moments(keyframes: Sequence[Keyframe], *, backend=None, cache_dir: Optional[str] = None,
                  budget_s: float = BUDGET_S, per_call_cap_s: float = PER_CALL_CAP_S,
                  on_start: Optional[Callable[[], None]] = None) -> dict:
    """Rate ``keyframes`` (story order) and return the moment list document.

    ``backend``: a VisionBackend (anything with ``name``, ``ensure_running``
    and ``rate_frame``); None means get_vision_backend(REEL_VLM_DEFAULT).
    ``cache_dir``: where ratings are cached; None means
    <data dir>/reel_studio/cache/moments.
    ``budget_s`` bounds the requests (0 asks nobody): each gets min(what is
    left, ``per_call_cap_s``) and none starts with under MIN_CALL_S left.
    Just before the first frame that is not cached and still has MIN_CALL_S
    left, the backend's ``ensure_running`` is asked, once, with
    ``on_start`` as its ``on_wait``: it is called only when the start has
    to wait for the server (not when it is already up), so the caller says
    the vision model is being made ready only then, and one that raises
    never skips the start.  All frames cached, a budget under MIN_CALL_S,
    or no keyframes: no start is asked.
    The start keeps its own waits, which this cannot shorten: the time it
    takes COUNTS AGAINST ``budget_s`` and is NOT capped by it, so this call
    can take longer than ``budget_s`` (review of b263da313, 2026-10-14: a
    2.0 s budget took 11.05 s).  Frames the model does not rate fall back
    one by one, also when the start raises.

    Returns ``{'schema': 'hart.reel.moments.v1', 'src': 'vlm'|'fallback'|
    'mixed', 'model': backend name or None, 'final_look_found', 'notes',
    'moments': [Moment.to_dict(), ...] in the keyframes' order, 'stats':
    {'frames', 'vlm', 'cached', 'calls', 'elapsed_s'}}``.  Raises
    ValueError for a negative or non-finite budget, or a cap that is not
    a positive finite number.  A backend that is missing, raises or
    answers nonsense, and a cache that cannot be read or written, never
    raise here: the frame falls back.
    """
    budget_s = _seconds('budget_s', budget_s, positive=False)
    per_call_cap_s = _seconds('per_call_cap_s', per_call_cap_s, positive=True)
    keys = list(keyframes)
    started = monotonic()
    if backend is None and keys:
        backend = _default_backend()
    cache = None
    if backend is not None:
        root = _cache_root(cache_dir)
        if root:
            cache = _RatingCache(root, _backend_name(backend))
    start_asked = False

    ratings: List[Optional[Dict[str, Any]]] = []
    calls = cached = 0
    out_of_time = False
    for key in keys:
        rating = cache.get(key.jpeg) if cache is not None else None
        if rating is not None:
            cached += 1
        elif backend is not None:
            left = budget_s - (monotonic() - started)
            if not start_asked and left >= MIN_CALL_S:
                # The first frame the model is needed for asks the backend's
                # ensure_running (the start describe() uses), once per reel;
                # its time counts against the budget, uncapped (amendment
                # A13).  on_start is said only if that start has to wait.
                # rate_frame itself never starts a server.
                start_asked = True
                try:
                    backend.ensure_running(on_wait=on_start)
                except Exception as e:      # the frames fall back one by one
                    logger.warning('moments: the vision model could not be started (%s)', e)
                left = budget_s - (monotonic() - started)
            if left < MIN_CALL_S:
                out_of_time = True
            else:
                calls += 1
                rating = parse_rating(_ask(backend, key, min(left, per_call_cap_s)))
                if rating is not None and cache is not None:
                    cache.put(key.jpeg, rating)
        ratings.append(rating)

    count = len(keys)
    given = [_rated_moment(k, r) if r is not None else _fallback_moment(k, _position_stage(i, count))
             for i, (k, r) in enumerate(zip(keys, ratings))]
    moments = smooth_stages(given)

    rated = sum(m.src == 'vlm' for m in moments)
    notes: List[str] = []
    if count and not rated:
        notes.append(NOTE_VLM_FALLBACK)
    elif rated < count:
        notes.append(NOTE_VLM_PARTIAL)
    if out_of_time:
        notes.append(NOTE_VLM_BUDGET)
    if any(a.src == 'vlm' and a.stage != b.stage for a, b in zip(given, moments)):
        notes.append(NOTE_STAGES_SMOOTHED)
    kinds = {m.src for m in moments}
    if cache is not None:
        cache.prune()
    return {
        'schema': MOMENTS_SCHEMA,
        'src': kinds.pop() if len(kinds) == 1 else ('mixed' if kinds else 'fallback'),
        'model': _backend_name(backend) if rated else None,
        'final_look_found': any(m.stage == FINAL_LOOK for m in moments),
        'notes': notes,
        'moments': [m.to_dict() for m in moments],
        'stats': {'frames': count, 'vlm': rated, 'cached': cached, 'calls': calls,
                  'elapsed_s': round(monotonic() - started, 3)},
    }


__all__ = ['REEL_VLM_DEFAULT', 'RUBRIC_PROMPT', 'LONG_SIDE', 'MAX_TOKENS', 'BUDGET_S',
           'PER_CALL_CAP_S', 'MIN_CALL_S', 'VLM_WEIGHT', 'CACHE_MAX_AGE_S',
           'NOTE_VLM_FALLBACK', 'NOTE_VLM_PARTIAL', 'NOTE_VLM_BUDGET', 'NOTE_STAGES_SMOOTHED',
           'parse_rating', 'smooth_stages', 'fallback_moments', 'score_moments']
