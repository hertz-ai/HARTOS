"""integrations.reel_studio.media.qa -- the one quality bar, run on a rendered reel.

``run_qa(path, edl)`` returns a ``hart.reel.qa.v1`` report: one row per
contracts.QUALITY_BAR check, ``{id, ok, value, want, hard}``.  Hard rows go
in ``checks`` and decide ``passed``; a reel that fails one is withheld.
Advisory rows go in ``advisory`` and only inform the reply.  Every
threshold comes from ``bar`` (default QUALITY_BAR); none is restated here.

WHAT IS MEASURED, AND HOW (ffmpeg via core.media_bin only; cv2 compares):

* one ``ffmpeg -i <reel> -vf vfrdet,cropdetect,blackdetect -f null`` run
  gives the stream facts (probe.parse_ffmpeg_info reads the same banner:
  size, rotation, codec, pixel format, frame rate, duration, audio), the
  H.264 profile, SAR and colour tags from the video stream line, whether
  any frame interval differs (VFR), the picture area cropdetect saw over the
  whole reel, and any black stretch;
* one decode of the reel at COMPARE_W x COMPARE_H gives the frame count
  (passthrough, so every coded frame counts), the luma at
  first_frame_luma.at_s, the visible cuts (the same hue x saturation
  histogram correlation shots.py uses for her clips), and the frames the
  cross-checks compare;
* the file's own bytes give its size and whether ``moov`` precedes
  ``mdat`` (faststart).

HARD: resolution, codec, fps (30 CFR, frame count within tolerance of
duration x 30), duration (the file and the EDL against target_s), audio
(none in the file, none in the EDL), size_cap, faststart,
first_frame_luma, and every contracts.validate_edl rule (schema,
contiguous, shot_rules, beats, no_bars, face, order, reveal, teaser,
overlays, transition).

ADVISORY: color_tags; bitrate_floor (a calm face shot at CRF can sit below
it); reveal_render (the frame reveal_render.from_end_s before the end
against the expected source frame, overlay regions masked);
segment_fidelity (each segment's middle frame, masked the same way);
beats_render (each EDL cut seen in the picture; consecutive shots of the
same scene can hide a cut); no_bars_render (cropdetect and blackdetect,
which dark hair or a black dupatta can trigger); face_render (Haar finds a
face in each segment the EDL framed one in); shot_quality (from the
analysis arrays, when ``analyses`` are given).

The expected source frame is made with render.geometry_filter, the same
scale/tone-map/crop chain the render used, at the comparison size, for the
time of the reel frame it is compared with.  The two are compared by the
correlation of their pixels at CORR_W x CORR_H with overlay regions masked
(``_masked_corr`` says why pixels and not a colour histogram).

run_qa never raises for a bad reel or EDL: a missing or unreadable file
fails the file rows, a structurally broken EDL fails ``schema`` (and the
rows that need its structure are left out).  Only a missing ffmpeg raises.
"""
from __future__ import annotations

import logging
import math
import os
import re
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

import cv2
import numpy as np

from core import media_bin
from integrations.reel_studio.contracts import (
    CANVAS, EDL_CHECKS, QA_SCHEMA, QUALITY_BAR, ClipInfo, ClipUnsupported, logo_box,
    validate_edl)
from integrations.reel_studio.media import render
from integrations.reel_studio.media.face import HaarFaceDetector
from integrations.reel_studio.media.probe import parse_ffmpeg_info
from integrations.reel_studio.media.shots import hist_corr, hs_histogram

logger = logging.getLogger(__name__)

#: Bound on one whole QA run, and on its stream-facts pass.
QA_TIMEOUT_S = 180.0
FACTS_TIMEOUT_S = 90.0
#: Size the reel and the expected source frames are compared at (9:16).
COMPARE_W, COMPARE_H = 270, 480
#: Size of the copy the visible-cut histograms are taken from.
CUT_W, CUT_H = 90, 160
#: Size the frame cross-checks correlate pixels at (small enough that a
#: pixel of crop rounding or compression noise does not matter).
CORR_W, CORR_H = 72, 128
#: A text overlay is masked over this many font sizes above and below its
#: anchor (up to four wrapped lines).
TEXT_MASK_HALF = 2.5
#: cropdetect may trim this share of a side and still find no bars.
BAR_SLACK = 0.01
CROPDETECT = 'cropdetect=limit=24:round=2:reset=0'
BLACKDETECT = 'blackdetect=d=0.5:pix_th=0.10'
#: The checks measured on the file (``duration`` and ``audio`` also read the EDL).
FILE_CHECKS = ('resolution', 'codec', 'fps', 'duration', 'audio', 'size_cap', 'faststart',
               'first_frame_luma')

FPS = CANVAS['fps']
_STREAM = re.compile(r'^\s*Stream #0:\d+\S*: Video: (.*)$')
_PROFILE = re.compile(r'^\w+ \(([^)]*)\)')
_SAR = re.compile(r'\[SAR (\d+):(\d+) DAR')
_PIXFMT = re.compile(r'\b((?:yuvj?|nv|gray|rgb|bgr|gbr|p0)[a-z0-9]*)\(([^)]*)\)')
_VFR = re.compile(r'VFR:([0-9.]+|nan) \((\d+)/(\d+)\)')
_CROP = re.compile(r'crop=(\d+):(\d+):(\d+):(\d+)')
_BLACK = re.compile(r'black_duration:\s*([0-9.]+)')
_RANGE_OR_FIELD = frozenset({'tv', 'pc', 'progressive', 'top first', 'bottom first',
                             'top coded first (swapped)', 'bottom coded first (swapped)'})


# ── the file ──────────────────────────────────────────────────────────────

def mp4_top_level_boxes(path: str, *, limit: int = 64) -> List[str]:
    """The types of the file's top-level MP4 boxes, in file order (at most ``limit``)."""
    out: List[str] = []
    try:
        total = os.path.getsize(path)
        with open(path, 'rb') as fh:
            pos = 0
            while pos + 8 <= total and len(out) < limit:
                fh.seek(pos)
                head = fh.read(16)
                size = int.from_bytes(head[:4], 'big')
                kind = head[4:8].decode('latin-1')
                if size == 1 and len(head) >= 16:
                    size = int.from_bytes(head[8:16], 'big')
                elif size == 0:
                    size = total - pos
                if size < 8:
                    break
                out.append(kind)
                pos += size
    except OSError as e:
        logger.info('could not read the boxes of %s: %s', path, e)
    return out


@dataclass
class _Facts:
    error: Optional[str] = None
    info: Optional[ClipInfo] = None
    size: int = 0
    boxes: List[str] = field(default_factory=list)
    profile: str = ''
    sar: str = '1:1'
    colors: Dict[str, str] = field(default_factory=dict)
    vfr: Optional[float] = None
    crop: Optional[Tuple[int, int, int, int]] = None
    black_s: float = 0.0


def _left(deadline: float, cap: float) -> float:
    return max(1.0, min(cap, deadline - time.monotonic()))


def _video_line(stderr: str) -> str:
    for line in stderr.splitlines():
        if line.startswith('Output #') or line.startswith('Stream mapping'):
            break
        m = _STREAM.match(line)
        if m and '(attached pic)' not in m.group(1):
            return m.group(1)
    return ''


def _colors(summary: str) -> Dict[str, str]:
    m = _PIXFMT.search(summary)
    if not m:
        return {}
    for part in (p.strip() for p in m.group(2).split(',')):
        if not part or part in _RANGE_OR_FIELD:
            continue
        names = part.split('/')
        if len(names) == 3:
            return {'space': names[0], 'primaries': names[1], 'trc': names[2]}
        return {'space': part, 'primaries': part, 'trc': part}
    return {}


def _facts(path: str, deadline: float) -> _Facts:
    if not os.path.isfile(path):
        return _Facts(error='missing')
    facts = _Facts(size=os.path.getsize(path), boxes=mp4_top_level_boxes(path))
    result = media_bin.run_ffmpeg(
        ['-i', path, '-map', '0:v:0', '-vf', 'vfrdet,%s,%s' % (CROPDETECT, BLACKDETECT),
         '-f', 'null', '-'], timeout_s=_left(deadline, FACTS_TIMEOUT_S))
    stderr = result.stderr or ''
    try:
        facts.info = parse_ffmpeg_info(stderr, path=path)
    except ClipUnsupported as e:
        facts.error = 'unreadable: %s' % e
        return facts
    if result.timed_out:
        facts.error = 'ffmpeg timed out reading the reel'
    elif result.returncode != 0:
        facts.error = 'ffmpeg could not decode the reel: %s' % stderr.strip()[-200:]
    summary = _video_line(stderr)
    m = _PROFILE.match(summary)
    facts.profile = m.group(1) if m else ''
    m = _SAR.search(summary)
    facts.sar = '%s:%s' % (m.group(1), m.group(2)) if m else '1:1'
    facts.colors = _colors(summary)
    vfr = _VFR.findall(stderr)
    if vfr:
        ratio = vfr[-1][0]
        facts.vfr = 0.0 if ratio == 'nan' else float(ratio)
    crop = _CROP.findall(stderr)
    if crop:
        facts.crop = tuple(int(v) for v in crop[-1])
    facts.black_s = sum(float(v) for v in _BLACK.findall(stderr))
    return facts


@dataclass
class _Frames:
    count: int = 0
    kept: Dict[int, np.ndarray] = field(default_factory=dict)
    cuts: List[int] = field(default_factory=list)
    error: Optional[str] = None


def _decode(path: str, wanted: Sequence[int], cut_threshold: float,
            deadline: float) -> _Frames:
    """Every frame of the reel at the comparison size: count, keep ``wanted``, find cuts."""
    out = _Frames()
    want = set(wanted)
    stream = media_bin.ffmpeg_stream(
        ['-loglevel', 'error', '-i', path, '-map', '0:v:0',
         '-vf', 'scale=%d:%d:flags=area' % (COMPARE_W, COMPARE_H), '-fps_mode', 'passthrough',
         '-f', 'rawvideo', '-pix_fmt', 'bgr24', 'pipe:1'],
        frame_bytes=COMPARE_W * COMPARE_H * 3, timeout_s=_left(deadline, QA_TIMEOUT_S))
    previous = None
    try:
        for k, chunk in enumerate(stream):
            frame = np.frombuffer(chunk, np.uint8).reshape(COMPARE_H, COMPARE_W, 3)
            if k in want:
                out.kept[k] = frame.copy()
            hist = hs_histogram(cv2.resize(frame, (CUT_W, CUT_H), interpolation=cv2.INTER_AREA))
            if previous is not None and 1.0 - hist_corr(previous, hist) > cut_threshold:
                out.cuts.append(k)
            previous = hist
            out.count = k + 1
    except media_bin.FFmpegFailed as e:
        out.error = str(e)
    finally:
        stream.close()
    return out


# ── comparisons ───────────────────────────────────────────────────────────

def _frame_index(t_s: float) -> int:
    """The frame on screen at ``t_s``."""
    return max(0, int(math.floor(t_s * FPS + 1e-6)))


def _segment_at(edl: Mapping, t_s: float) -> Optional[Tuple[int, Mapping]]:
    for i, seg in enumerate(edl['segments']):
        if seg['out_start_s'] - 1e-6 <= t_s < seg['out_end_s'] - 1e-6:
            return i, seg
    if edl['segments'] and t_s >= edl['segments'][-1]['out_start_s']:
        return len(edl['segments']) - 1, edl['segments'][-1]
    return None


def _mask(edl: Mapping, t_s: float) -> np.ndarray:
    """255 where the picture is the footage's, 0 over overlays shown at ``t_s``."""
    mask = np.full((COMPARE_H, COMPARE_W), 255, np.uint8)
    fx, fy = COMPARE_W / float(CANVAS['w']), COMPARE_H / float(CANVAS['h'])
    for ov in edl['overlays']:
        if t_s < ov['from_s'] - 1e-6 or (ov['to_s'] is not None and t_s >= ov['to_s']):
            continue
        if ov['type'] == 'logo':
            x0, y0, x1, y1 = logo_box(ov)
            mask[int(y0 * fy):int(math.ceil(y1 * fy)), int(x0 * fx):int(math.ceil(x1 * fx))] = 0
        else:
            y = ov['y'] * CANVAS['h']
            half = TEXT_MASK_HALF * ov['size_px']
            mask[max(0, int((y - half) * fy)):int(math.ceil((y + half) * fy)), :] = 0
    return mask


def _masked_corr(a: np.ndarray, b: np.ndarray, mask: np.ndarray) -> Optional[float]:
    """Pearson correlation of two frames' unmasked pixels (B, G, R) at CORR_W x CORR_H.

    Pixels, not a hue x saturation histogram: on tinted test footage the
    histogram scored the RIGHT frame 0.72 after one re-encode and a wrong
    frame 0.55, while this scored them 0.99 and 0.03.  None when nothing is
    left unmasked or a frame is flat.
    """
    small = [cv2.resize(img, (CORR_W, CORR_H), interpolation=cv2.INTER_AREA).astype(np.float64)
             for img in (a, b)]
    keep = cv2.resize(mask, (CORR_W, CORR_H), interpolation=cv2.INTER_NEAREST) > 0
    if not keep.any():
        return None
    x, y = small[0][keep].ravel(), small[1][keep].ravel()
    x -= x.mean()
    y -= y.mean()
    denom = math.sqrt(float((x * x).sum()) * float((y * y).sum()))
    if denom <= 0.0:
        return None
    return float((x * y).sum()) / denom


def _source_frame(seg: Mapping, clip: Mapping, t_s: float, tonemap: bool,
                  deadline: float) -> Optional[np.ndarray]:
    """The source frame the reel should show at ``t_s``, at the comparison size."""
    path = clip['path']
    if not os.path.isfile(path):
        return None
    src_t = seg['in_s'] + (t_s - seg['out_start_s']) * seg['speed']
    vf = render.geometry_filter(seg, clip, canvas={'w': COMPARE_W, 'h': COMPARE_H},
                                tonemap=tonemap)
    try:
        chunks = list(media_bin.ffmpeg_stream(
            ['-loglevel', 'error', '-ss', '%.6f' % max(0.0, src_t), '-i', path, '-map', '0:V:0',
             '-frames:v', '1', '-vf', vf, '-f', 'rawvideo', '-pix_fmt', 'bgr24', 'pipe:1'],
            frame_bytes=COMPARE_W * COMPARE_H * 3, timeout_s=_left(deadline, 60.0)))
    except media_bin.FFmpegFailed as e:
        logger.info('could not read the source frame of %s at %.3f s: %s', path, src_t, e)
        return None
    if not chunks:
        return None
    return np.frombuffer(chunks[0], np.uint8).reshape(COMPARE_H, COMPARE_W, 3)


# ── rows ──────────────────────────────────────────────────────────────────

def _plain(value):
    if isinstance(value, Mapping):
        return {str(k): _plain(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(v) for v in value]
    if isinstance(value, (np.floating, np.integer)):
        return value.item()
    if isinstance(value, float):
        return round(value, 4)
    return value


def _row(bar: Mapping, check: str, ok: bool, value: Any) -> Dict[str, Any]:
    return {'id': check, 'ok': bool(ok), 'value': _plain(value),
            'want': _plain({k: v for k, v in bar[check].items() if k != 'hard'}),
            'hard': bool(bar[check]['hard'])}


def _by_check(problems: Sequence[str]) -> Dict[str, List[str]]:
    out: Dict[str, List[str]] = {}
    for p in problems:
        check, _, what = p.partition(':')
        out.setdefault(check, []).append(what.strip())
    return out


def _file_rows(facts: _Facts, frames: Optional[_Frames], edl: Mapping, edl_ok: bool,
               found: Dict[str, List[str]], bar: Mapping) -> List[Dict[str, Any]]:
    rows = []
    info = facts.info
    bad = facts.error if info is None else None

    def add(check, ok, value):
        rows.append(_row(bar, check, ok, value))

    if bad:
        for check in ('resolution', 'codec', 'fps', 'size_cap', 'faststart', 'first_frame_luma'):
            add(check, False, bad)
    else:
        want = bar['resolution']
        add('resolution', (info.width, info.height) == (want['w'], want['h'])
            and facts.sar == want['sar'] and info.rotation == 0,
            {'w': info.width, 'h': info.height, 'sar': facts.sar, 'rotation': info.rotation})
        want = bar['codec']
        add('codec', info.codec == want['codec'] and facts.profile == want['profile']
            and info.pix_fmt == want['pix_fmt'],
            {'codec': info.codec, 'profile': facts.profile, 'pix_fmt': info.pix_fmt})
        want_frames = int(round(info.duration_s * bar['fps']['fps']))
        count = frames.count if frames is not None and frames.error is None else None
        add('fps', facts.error is None and abs(info.fps_avg - bar['fps']['fps']) < 0.01
            and not facts.vfr and count is not None
            and abs(count - want_frames) <= bar['fps']['frame_count_tol'],
            {'fps': info.fps_avg, 'vfr': facts.vfr, 'frames': count, 'want_frames': want_frames,
             'error': facts.error or (frames.error if frames is not None else None)})
        cap = info.duration_s * bar['size_cap']['mbps'] * 1e6 / 8.0 + bar['size_cap']['slack_bytes']
        add('size_cap', facts.size <= cap, {'bytes': facts.size, 'cap_bytes': int(cap)})
        boxes = facts.boxes
        add('faststart', 'moov' in boxes and 'mdat' in boxes
            and boxes.index('moov') < boxes.index('mdat'), boxes[:8])
        at = _frame_index(bar['first_frame_luma']['at_s'])
        frame = frames.kept.get(at) if frames is not None else None
        luma = (float(cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY).mean()) / 255.0
                if frame is not None else None)
        add('first_frame_luma', luma is not None and luma >= bar['first_frame_luma']['min_luma'],
            luma)

    target = edl.get('target_s') if isinstance(edl, Mapping) else None
    file_s = info.duration_s if info is not None else None
    ok = (file_s is not None and isinstance(target, (int, float))
          and abs(file_s - target) <= bar['duration']['tol_s'])
    add('duration', ok and (edl_ok and not found.get('duration')),
        {'file_s': file_s, 'target_s': target, 'edl': found.get('duration', [])})
    has_audio = info.has_audio if info is not None else None
    add('audio', has_audio is False and not found.get('audio') and edl_ok,
        {'has_audio': has_audio, 'edl': found.get('audio', [])})
    order = {c: k for k, c in enumerate(FILE_CHECKS)}
    return sorted(rows, key=lambda r: order[r['id']])


def _advisory_rows(facts: _Facts, frames: Optional[_Frames], edl: Mapping, bar: Mapping,
                   analyses, sources: Dict[int, Optional[np.ndarray]],
                   reveal_t: float) -> List[Dict[str, Any]]:
    rows = []

    def add(check, ok, value):
        rows.append(_row(bar, check, ok, value))

    info = facts.info
    if info is not None:
        add('color_tags', all(facts.colors.get(k) == bar['color_tags'][k]
                              for k in ('primaries', 'trc', 'space')), facts.colors or 'untagged')
        mbps = facts.size * 8.0 / 1e6 / info.duration_s if info.duration_s > 0 else 0.0
        add('bitrate_floor', mbps >= bar['bitrate_floor']['min_mbps'], {'mbps': mbps})
        w, h = info.width, info.height
        crop = facts.crop
        bars = crop is not None and (crop[0] < w * (1 - BAR_SLACK) or crop[1] < h * (1 - BAR_SLACK))
        add('no_bars_render', not bars and facts.black_s == 0.0,
            {'crop': ':'.join(str(v) for v in crop) if crop else None, 'black_s': facts.black_s})
    if frames is None or frames.error is not None or info is None:
        return rows

    segs = edl['segments']
    # reveal_render: the frame shortly before the end against its source.
    at = _segment_at(edl, reveal_t)
    corr = None
    if at is not None:
        expected = sources.get(-1)
        got = frames.kept.get(_frame_index(reveal_t))
        if expected is not None and got is not None:
            corr = _masked_corr(got, expected, _mask(edl, reveal_t))
    add('reveal_render', corr is not None and corr >= bar['reveal_render']['min_corr'],
        {'t_s': reveal_t, 'corr': corr})

    fidelity = []
    for i, seg in enumerate(segs):
        mid = (seg['out_start_s'] + seg['out_end_s']) / 2.0
        expected, got = sources.get(i), frames.kept.get(_frame_index(mid))
        c = (_masked_corr(got, expected, _mask(edl, mid))
             if expected is not None and got is not None else None)
        fidelity.append({'segment': i, 'corr': c})
    add('segment_fidelity', all(f['corr'] is not None
                                and f['corr'] >= bar['segment_fidelity']['min_corr']
                                for f in fidelity), fidelity)

    tol = max(bar['beats']['tol_s'], 1.5 / FPS)
    seen = [k / float(FPS) for k in frames.cuts]
    cuts = [s['out_end_s'] for s in segs[:-1]]
    matched = [c for c in cuts if any(abs(c - t) <= tol for t in seen)]
    extra = [round(t, 3) for t in seen if not any(abs(c - t) <= tol for c in cuts)]
    add('beats_render', len(matched) == len(cuts),
        {'edl_cuts': len(cuts), 'matched': len(matched), 'unexpected_cuts_s': extra})

    detector = HaarFaceDetector()
    framed = [i for i, s in enumerate(segs) if s['face'] is not None]
    found = []
    for i in framed:
        mid = (segs[i]['out_start_s'] + segs[i]['out_end_s']) / 2.0
        frame = frames.kept.get(_frame_index(mid))
        if frame is not None and detector.detect(frame) is not None:
            found.append(i)
    add('face_render', len(found) == len(framed),
        {'segments_with_face': len(framed), 'found': len(found)})

    if analyses is not None:
        add(*_shot_quality(edl, analyses, bar))
    return rows


def _shot_quality(edl: Mapping, analyses, bar: Mapping) -> Tuple[str, bool, Any]:
    rule = bar['shot_quality']
    by_clip = {a.clip: a for a in analyses}
    weak = []
    for i, seg in enumerate(edl['segments']):
        a = by_clip.get(seg['clip'])
        if a is None or a.times is None or not len(a.times):
            continue
        times = np.asarray(a.times, dtype=np.float64)
        pick = (times >= seg['in_s'] - 1e-6) & (times < seg['out_s'])
        if not pick.any():
            pick = np.abs(times - seg['in_s']) == np.abs(times - seg['in_s']).min()
        why = []
        if float(np.mean(np.asarray(a.sharp)[pick])) < float(np.percentile(a.sharp, rule['sharp_pctl'])):
            why.append('soft')
        luma = float(np.mean(np.asarray(a.luma)[pick]))
        if not rule['luma_min'] <= luma <= rule['luma_max']:
            why.append('dark' if luma < rule['luma_min'] else 'bright')
        if float(np.mean(np.asarray(a.clipped)[pick])) >= rule['max_clipped']:
            why.append('blown highlights')
        if why:
            weak.append({'segment': i, 'why': why})
    return 'shot_quality', not weak, weak


# ── public ────────────────────────────────────────────────────────────────

def run_qa(path: str, edl: Mapping, *, bar: Mapping = QUALITY_BAR, analyses=None,
           timeout_s: float = QA_TIMEOUT_S) -> Dict[str, Any]:
    """The ``hart.reel.qa.v1`` report for the reel at ``path`` made from ``edl``.

    ``analyses`` (the clips' ClipAnalysis results) adds the advisory
    shot_quality row.  ``timeout_s`` bounds the whole run.  Raises only
    media_bin.MediaToolMissing (no ffmpeg); see the module note.
    """
    deadline = time.monotonic() + float(timeout_s)
    path = os.path.abspath(os.fspath(path))
    found = _by_check(validate_edl(edl, bar=bar))
    edl_ok = not found.get('schema')
    facts = _facts(path, deadline)

    reveal_t = None
    wanted = [_frame_index(bar['first_frame_luma']['at_s'])]
    if edl_ok:
        reveal_t = max(0.0, edl['duration_s'] - bar['reveal_render']['from_end_s'])
        wanted.append(_frame_index(reveal_t))
        wanted += [_frame_index((s['out_start_s'] + s['out_end_s']) / 2.0)
                   for s in edl['segments']]
    frames = None
    if facts.info is not None:
        frames = _decode(path, wanted, bar['beats_render']['scene_threshold'], deadline)

    checks = _file_rows(facts, frames, edl, edl_ok, found, bar)
    for check in EDL_CHECKS:
        if check in ('duration', 'audio') or (not edl_ok and check != 'schema'):
            continue
        checks.append(_row(bar, check, not found.get(check), found.get(check, [])))

    advisory: List[Dict[str, Any]] = []
    if edl_ok:
        sources: Dict[int, Optional[np.ndarray]] = {}
        if frames is not None and frames.error is None:
            hdr = any(c['transfer'] != 'sdr' for c in edl['clips'].values())
            caps = media_bin.ffmpeg_caps() if hdr else None
            tonemap = bool(caps and 'zscale' in caps.filters and 'tonemap' in caps.filters)
            # Each expected frame is taken at the time of the reel frame it is
            # compared with (frame k is on screen from k/30 s).
            for i, seg in enumerate(edl['segments']):
                k = _frame_index((seg['out_start_s'] + seg['out_end_s']) / 2.0)
                sources[i] = _source_frame(seg, edl['clips'][seg['clip']], k / float(FPS),
                                           tonemap, deadline)
            at = _segment_at(edl, reveal_t)
            if at is not None:
                seg = at[1]
                sources[-1] = _source_frame(seg, edl['clips'][seg['clip']],
                                            _frame_index(reveal_t) / float(FPS), tonemap,
                                            deadline)
        advisory = _advisory_rows(facts, frames, edl, bar, analyses, sources, reveal_t)

    rows = checks + advisory
    report = {'schema': QA_SCHEMA,
              'passed': all(r['ok'] for r in rows if r['hard']),
              'checks': [r for r in rows if r['hard']],
              'advisory': [r for r in rows if not r['hard']]}
    return report


__all__ = ['QA_TIMEOUT_S', 'FACTS_TIMEOUT_S', 'COMPARE_W', 'COMPARE_H', 'TEXT_MASK_HALF',
           'BAR_SLACK', 'CROPDETECT', 'BLACKDETECT', 'FILE_CHECKS', 'mp4_top_level_boxes',
           'run_qa']
