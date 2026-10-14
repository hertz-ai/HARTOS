"""integrations.reel_studio.media.render -- an EDL to the posted mp4, in two passes.

PASS 1, one ffmpeg process per segment, one at a time, each with exactly
one input (so a reel of fifteen shots never holds fifteen 4K HEVC decoders
at once):

    -ss in -to out -i <clip>          input seek: decode only what is used
    scale=W:H                         cover the canvas (display orientation;
                                      ffmpeg turns the frame by its display
                                      matrix before the filters)
    <tone-map chain>                  HDR only, after the scale (cheaper on
                                      phone 4K), decode.tonemap_filter: the
                                      same chain the analysis saw
    crop=1080:1920:x:y                the EDL's window (contracts.crop_window)
    setpts=(PTS-STARTPTS)/speed       speed ramps
    fps=30                            LAST, so every segment is 30 fps CFR
    tpad + -frames:v N                exactly N = round(length x 30) frames
    -an, libx264 veryfast CRF 14      a near-lossless mezzanine in workdir

PASS 2 joins the mezzanines with the concat demuxer, draws the hook text
with drawtext reading a UTF-8 ``textfile=`` (so ``:`` ``'`` ``%`` and Tamil
need no filter escaping; ``expansion=none`` keeps ``%`` literal) and an
escaped ``fontfile``, lays the logo in its corner (contracts.logo_box, the
box the EDL check used), lays each animated ('motion') overlay's clip with
its alpha at its box from its from_s (amendment A14: rendered beforehand by
integrations.reel_studio.motion; the cut passes through outside its
window), and encodes once: h264_nvenc VBR CQ 21, else
libx264 medium CRF 20, both under a 12 Mbps maxrate (never a fixed
bitrate: a calm face shot needs far less), High profile, yuv420p, BT.709
tags, silent, faststart.  If NVENC fails at run time (no free session, the
GPU busy with the language models) the pass is redone with libx264 and the
result says so.

``canvas`` renders the same EDL at another (even, 9:16) size; tests use it
to stay fast.  Production always renders contracts.CANVAS.

The work dir belongs to the caller; render removes only its own
intermediates (``reel_*`` files) and never leaves a partial output.
"""
from __future__ import annotations

import logging
import math
import os
import re
import time
import unicodedata
from typing import Dict, List, Mapping, Optional, Sequence, Tuple

from core import media_bin
from integrations.reel_studio.contracts import (
    CANVAS, SAFE_ZONE, RenderResult, crop_window, logo_box, validate_edl)
from integrations.reel_studio.media import fonts
from integrations.reel_studio.media.decode import tonemap_filter

logger = logging.getLogger(__name__)

FPS = CANVAS['fps']
ENCODERS = ('auto', 'h264_nvenc', 'libx264')
#: Bound on a whole render, and on each pass-1 segment: a fixed allowance
#: plus this much per output second, times the source's load (frame rate
#: over 30, twice for HDR tone-mapping).
RENDER_TIMEOUT_S = 600.0
SEGMENT_TIMEOUT_BASE_S = 60.0
SEGMENT_TIMEOUT_PER_S = 20.0

#: Pass 1 (mezzanine) and pass 2 (posted file) encoder settings.
PASS1_ENCODE = ('-c:v', 'libx264', '-preset', 'veryfast', '-crf', '14', '-pix_fmt', 'yuv420p')
X264_FINAL = ('-c:v', 'libx264', '-preset', 'medium', '-crf', '20')
NVENC_FINAL = ('-c:v', 'h264_nvenc', '-preset', 'p5', '-rc', 'vbr', '-cq', '21', '-b:v', '0')
RATE_CAP = ('-maxrate', '12M', '-bufsize', '24M')
#: Frame colour tags: the tone-mapped (or SDR) picture is BT.709 limited range.
BT709 = 'setparams=color_primaries=bt709:color_trc=bt709:colorspace=bt709:range=tv'

#: Hook text look: box behind it, padding and line spacing as fractions of
#: the font size, most lines before the rest run on in the last.
TEXT_BOX_COLOR = '0x000000@0.45'
TEXT_PAD = 0.3
TEXT_LINE_SPACING = 0.2
TEXT_MAX_LINES = 4
#: Drawn height of one line in ems (the font's ascent plus descent, as
#: drawtext lays it; see text_block_size).  Measured 2026-10-15 on Windows
#: with Nirmala.ttc, the font fonts.resolve_font picks there for Latin and
#: Tamil: 1.18-1.26 em per line, sizes 28-56 px, 2-4 lines, the boxed block
#: drawn through _drawtext on a white frame.  Rounded up, so an over-estimate.
TEXT_LINE_EM = 1.3
#: Estimated advance widths in ems (see text_width): a Latin letter or digit
#: of a bold sans, an Indic letter, a spacing vowel sign, a space.
TEXT_EM_LATIN = 0.62
TEXT_EM_INDIC = 0.9
TEXT_EM_MARK = 0.45
TEXT_EM_SPACE = 0.3

NOTE_TEXT_NO_FONT = 'text_skipped_no_font'
NOTE_EMOJI_DROPPED = 'emoji_dropped'
NOTE_NVENC_FAILED = 'nvenc_failed'
NOTE_HDR_UNTONED = 'hdr_not_tone_mapped'

_COLOR = re.compile(r'#?([0-9A-Fa-f]{6}(?:[0-9A-Fa-f]{2})?)$')
_LEVEL1 = "\\':"
_LEVEL2 = "\\'[],;"


# ── pure helpers ──────────────────────────────────────────────────────────

def _escape(text: str, special: str) -> str:
    return ''.join('\\' + ch if ch in special else ch for ch in text)


def escape_filter_path(path: str) -> str:
    """``path`` as a drawtext option value inside a filtergraph (argv, no shell).

    Backslashes become forward slashes (Windows accepts them); then the two
    escaping levels ffmpeg undoes: the option value (``\\`` ``'`` ``:``, so
    a drive letter's colon is not an option separator) and the filtergraph
    (``\\`` ``'`` ``[`` ``]`` ``,`` ``;``).
    """
    return _escape(_escape(str(path).replace('\\', '/'), _LEVEL1), _LEVEL2)


def _num(value: float) -> str:
    return ('%.6f' % float(value)).rstrip('0').rstrip('.')


def _even_up(value: float) -> int:
    n = int(math.ceil(float(value) - 1e-3))
    return n + (n % 2)


def segment_frames(seg: Mapping) -> int:
    """Output frames of an EDL segment at 30 fps."""
    return int(round((seg['out_end_s'] - seg['out_start_s']) * FPS))


def geometry_filter(seg: Mapping, clip: Mapping, *, canvas: Mapping = CANVAS,
                    tonemap: bool = True) -> str:
    """scale-to-cover, (HDR) tone-map, crop to the segment's window, square pixels.

    The window is contracts.crop_window's, so the picture is the one the EDL
    checks judged.  Both scaled sides are even (zscale needs that for 4:2:0).
    """
    x0, y0, _x1, _y1, scale = crop_window(clip, seg['crop'], canvas=canvas)
    cw, ch = int(canvas['w']), int(canvas['h'])
    sw = max(cw, _even_up(float(clip['display_w']) * scale))
    sh = max(ch, _even_up(float(clip['display_h']) * scale))
    cx = min(max(int(round(x0 * scale)), 0), sw - cw)
    cy = min(max(int(round(y0 * scale)), 0), sh - ch)
    chain = ['scale=%d:%d:flags=lanczos' % (sw, sh)]
    if clip.get('transfer', 'sdr') != 'sdr' and tonemap:
        chain.append(tonemap_filter(clip['transfer']))
    chain += ['crop=%d:%d:%d:%d' % (cw, ch, cx, cy), 'setsar=1']
    return ','.join(chain)


def plan_segment_args(seg: Mapping, clip: Mapping, out_path: str, *,
                      canvas: Mapping = CANVAS, tonemap: bool = True) -> List[str]:
    """Pure: pass-1 ffmpeg arguments rendering one segment to ``out_path``."""
    n = segment_frames(seg)
    vf = ','.join([geometry_filter(seg, clip, canvas=canvas, tonemap=tonemap),
                   'setpts=(PTS-STARTPTS)/%s' % _num(seg['speed']),
                   'fps=%d' % FPS,
                   'tpad=stop_mode=clone:stop=%d' % n,
                   BT709, 'format=yuv420p'])
    return (['-y', '-loglevel', 'error', '-ss', '%.6f' % seg['in_s'], '-to', '%.6f' % seg['out_s'],
             '-i', clip['path'], '-map', '0:V:0', '-an', '-sn', '-dn', '-vf', vf,
             '-frames:v', str(n)] + list(PASS1_ENCODE) + [out_path])


def _color(value: str) -> str:
    m = _COLOR.match(str(value or ''))
    return '0x' + (m.group(1).upper() if m else 'FFFFFF')


def _enable(start: float, end: Optional[float]) -> str:
    expr = 'gte(t\\,%s)' % _num(start)
    if end is not None:
        expr += '*lt(t\\,%s)' % _num(end)
    return expr


def _scaled_zone(f: float) -> Dict[str, float]:
    return {k: v * f for k, v in SAFE_ZONE.items()}


def _text_pad(size_px: float, f: float = 1.0) -> int:
    """The box padding (pixels) around text of ``size_px`` drawn at scale
    ``f`` (the canvas over contracts.CANVAS)."""
    return max(1, int(round(TEXT_PAD * size_px * f)))


def _drawtext(ov: Mapping, textfile: str, fontfile: str, canvas: Mapping) -> str:
    f = canvas['w'] / float(CANVAS['w'])
    size = max(8, int(round(ov['size_px'] * f)))
    pad = _text_pad(ov['size_px'], f)
    zone = _scaled_zone(f)
    cx, cy = ov['x'] * canvas['w'], ov['y'] * canvas['h']
    x = 'max(%d\\,min(%d-text_w\\,%d-text_w/2))' % (
        round(zone['x_min'] + pad), round(zone['x_max'] - pad), round(cx))
    y = 'max(%d\\,min(%d-text_h\\,%d-text_h/2))' % (
        round(zone['y_min'] + pad), round(zone['y_max'] - pad), round(cy))
    parts = ['drawtext=textfile=' + escape_filter_path(textfile),
             'fontfile=' + escape_filter_path(fontfile),
             'expansion=none', 'fontsize=%d' % size, 'fontcolor=' + _color(ov['color']),
             'line_spacing=%d' % round(TEXT_LINE_SPACING * size), 'text_align=center',
             'x=' + x, 'y=' + y]
    if ov.get('box'):
        parts += ['box=1', 'boxcolor=' + TEXT_BOX_COLOR, 'boxborderw=%d' % pad]
    parts.append('enable=' + _enable(ov['from_s'], ov['to_s']))
    return ':'.join(parts)


def plan_final_args(edl: Mapping, concat_list: str, out_path: str, *, encoder: str,
                    textfiles: Mapping[int, Tuple[str, str]],
                    canvas: Mapping = CANVAS) -> List[str]:
    """Pure: pass-2 ffmpeg arguments (concat + overlays + the one encode).

    ``concat_list`` is the concat-demuxer list of pass-1 files;
    ``textfiles`` maps a text overlay's index in ``edl['overlays']`` to
    (UTF-8 text file, font file); a text overlay without an entry is not
    drawn.  ``encoder`` is 'h264_nvenc' or 'libx264'.
    """
    if encoder not in ('h264_nvenc', 'libx264'):
        raise ValueError('encoder must be h264_nvenc or libx264, got %r' % (encoder,))
    f = canvas['w'] / float(CANVAS['w'])
    args = ['-y', '-loglevel', 'error', '-f', 'concat', '-safe', '0', '-i', concat_list]
    graph: List[str] = []
    label = '0:v'
    texts = [_drawtext(ov, textfiles[i][0], textfiles[i][1], canvas)
             for i, ov in enumerate(edl['overlays']) if ov['type'] == 'text' and i in textfiles]
    if texts:
        graph.append('[0:v]%s[vt]' % ','.join(texts))
        label = 'vt'
    inputs = 1
    for ov in edl['overlays']:
        if ov['type'] != 'logo':
            continue
        args += ['-i', ov['src']]
        x0, y0, x1, y1 = logo_box(ov)
        w, h = max(1, int(round((x1 - x0) * f))), max(1, int(round((y1 - y0) * f)))
        graph.append('[%d:v]format=rgba,scale=%d:%d,colorchannelmixer=aa=%s[lg%d]'
                     % (inputs, w, h, _num(ov['opacity']), inputs))
        graph.append('[%s][lg%d]overlay=x=%d:y=%d:format=auto:enable=%s[vl%d]'
                     % (label, inputs, round(x0 * f), round(y0 * f),
                        _enable(ov['from_s'], ov['to_s']), inputs))
        label = 'vl%d' % inputs
        inputs += 1
    for ov in edl['overlays']:
        # An animated overlay (amendment A14): its clip with alpha starts at
        # from_s, sits at its box, and once it ends the cut passes through.
        if ov['type'] != 'motion':
            continue
        args += ['-i', ov['src']]
        x0, y0, x1, y1 = ov['box']
        w, h = max(1, int(round((x1 - x0) * f))), max(1, int(round((y1 - y0) * f)))
        graph.append('[%d:v]setpts=PTS-STARTPTS+%s/TB,scale=%d:%d[mo%d]'
                     % (inputs, _num(ov['from_s']), w, h, inputs))
        graph.append('[%s][mo%d]overlay=x=%d:y=%d:format=auto:eof_action=pass:enable=%s[vm%d]'
                     % (label, inputs, round(x0 * f), round(y0 * f),
                        _enable(ov['from_s'], ov['to_s']), inputs))
        label = 'vm%d' % inputs
        inputs += 1
    graph.append('[%s]%s,format=yuv420p[out]' % (label, BT709))
    args += ['-filter_complex', ';'.join(graph), '-map', '[out]', '-an', '-sn', '-dn']
    args += list(NVENC_FINAL if encoder == 'h264_nvenc' else X264_FINAL) + list(RATE_CAP)
    args += ['-profile:v', 'high', '-pix_fmt', 'yuv420p', '-colorspace', 'bt709',
             '-color_primaries', 'bt709', '-color_trc', 'bt709', '-color_range', 'tv',
             '-r', str(FPS), '-fps_mode', 'cfr', '-movflags', '+faststart', out_path]
    return args


# ── text layout ───────────────────────────────────────────────────────────

def _em_width(ch: str) -> float:
    """A generous advance width of ``ch`` in ems (bold sans; Indic letters wider)."""
    category = unicodedata.category(ch)
    if category == 'Mn' or ch in '‌‍':
        return 0.0
    if category == 'Mc':
        return TEXT_EM_MARK
    if ch.isspace():
        return TEXT_EM_SPACE
    if fonts.needs_indic_font(ch):
        return TEXT_EM_INDIC
    return TEXT_EM_LATIN


def text_width(text: str, size_px: float) -> float:
    """Estimated width of one line of ``text`` at ``size_px`` (an over-estimate).

    Widths are estimated, not measured: Pillow's FreeType text path (the
    pinned 9.5.0) crashed the interpreter with a segmentation fault on
    Nirmala.ttc under Python 3.12, so no font is opened in-process.
    """
    return size_px * sum(_em_width(ch) for ch in text)


def wrap_text(text: str, size_px: int, max_width_px: float) -> List[str]:
    """``text`` broken at spaces into lines no wider than ``max_width_px`` (estimated).

    A word wider than the line stays whole; past TEXT_MAX_LINES the rest
    runs on in the last line rather than being cut.
    """
    lines: List[str] = []
    current = ''
    for word in text.split():
        candidate = word if not current else current + ' ' + word
        if current and text_width(candidate, size_px) > max_width_px:
            lines.append(current)
            current = word
        else:
            current = candidate
    if current:
        lines.append(current)
    if len(lines) > TEXT_MAX_LINES:
        lines = lines[:TEXT_MAX_LINES - 1] + [' '.join(lines[TEXT_MAX_LINES - 1:])]
    return lines


def text_block_size(text: str, size_px: int, max_width_px: float) -> Tuple[List[str], float, float]:
    """``text`` as pass 2 draws it boxed at ``size_px`` in a column
    ``max_width_px`` wide (the safe zone's width, for a reel): its lines
    (wrap_text inside the box's padding, as _write_texts wraps), and the
    estimated width and height of the drawn block, padding included.

    Estimates, over rather than under (text_width; TEXT_LINE_EM per line,
    TEXT_LINE_SPACING between lines).  A width over ``max_width_px`` means a
    word, or the run-on last line past TEXT_MAX_LINES, does not fit."""
    pad = _text_pad(size_px)
    lines = wrap_text(text, size_px, max_width_px - 2 * pad)
    if not lines:
        return [], 0.0, 0.0
    n = len(lines)
    width = max(text_width(line, size_px) for line in lines) + 2 * pad
    height = size_px * (TEXT_LINE_EM * n + TEXT_LINE_SPACING * (n - 1)) + 2 * pad
    return lines, width, height


def _write_texts(edl: Mapping, workdir: str, canvas: Mapping, notes: List[str],
                 made: List[str]) -> Dict[int, Tuple[str, str]]:
    f = canvas['w'] / float(CANVAS['w'])
    zone = _scaled_zone(f)
    out: Dict[int, Tuple[str, str]] = {}
    for i, ov in enumerate(edl['overlays']):
        if ov['type'] != 'text':
            continue
        text = fonts.drop_emoji(ov['text'])
        if text != ' '.join(ov['text'].split()):
            notes.append(NOTE_EMOJI_DROPPED)
        if not text:
            continue
        fontfile = fonts.resolve_font(text)
        if fontfile is None:
            notes.append(NOTE_TEXT_NO_FONT)
            logger.info('no font on this machine draws the hook text; leaving it out')
            continue
        size = max(8, int(round(ov['size_px'] * f)))
        pad = _text_pad(ov['size_px'], f)
        lines = wrap_text(text, size, zone['x_max'] - zone['x_min'] - 2 * pad)
        path = os.path.join(workdir, 'reel_text_%d.txt' % i)
        made.append(path)
        with open(path, 'w', encoding='utf-8', newline='\n') as fh:
            fh.write('\n'.join(lines))
        out[i] = (path, fontfile)
    return out


# ── running ───────────────────────────────────────────────────────────────

def _run(args: Sequence[str], deadline: float, bound: Optional[float]) -> None:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise media_bin.FFmpegFailed('the render ran out of its time budget', timed_out=True)
    seconds = remaining if bound is None else min(bound, remaining)
    result = media_bin.run_ffmpeg(list(args), timeout_s=seconds)
    if result.timed_out or result.returncode != 0:
        tail = (result.stderr or '').strip()[-500:]
        raise media_bin.FFmpegFailed('ffmpeg %s: %s' % ('timed out' if result.timed_out
                                                        else 'exited %s' % result.returncode,
                                                        tail),
                                     returncode=result.returncode,
                                     stderr_tail=(result.stderr or '')[-2000:],
                                     timed_out=result.timed_out)


def _segment_bound(seg: Mapping, clip: Mapping) -> float:
    load = max(1.0, float(clip.get('fps_avg') or FPS) / FPS)
    if clip.get('transfer', 'sdr') != 'sdr':
        load *= 2.0
    return SEGMENT_TIMEOUT_BASE_S + SEGMENT_TIMEOUT_PER_S * segment_frames(seg) / FPS * load


def _write_concat(listing: str, paths: Sequence[str]) -> None:
    with open(listing, 'w', encoding='utf-8', newline='\n') as fh:
        for path in paths:
            fh.write("file '%s'\n" % os.path.abspath(path).replace('\\', '/').replace("'", "'\\''"))


def _canvas(canvas: Optional[Mapping]) -> Dict[str, int]:
    if canvas is None:
        return {'w': int(CANVAS['w']), 'h': int(CANVAS['h'])}
    w, h = int(canvas['w']), int(canvas['h'])
    if w < 2 or h < 2 or w % 2 or h % 2:
        raise ValueError('canvas sides must be even and >= 2, got %dx%d' % (w, h))
    return {'w': w, 'h': h}


def _dedupe(items: Sequence[str]) -> Tuple[str, ...]:
    return tuple(dict.fromkeys(items))


def _remove(paths: Sequence[str]) -> None:
    for path in paths:
        if not os.path.exists(path):
            continue
        try:
            os.remove(path)
        except OSError as e:
            logger.warning('could not remove render intermediate %s: %s', path, e)


def render_reel(edl: Mapping, out_path: str, *, workdir: str, encoder: str = 'auto',
                timeout_s: float = RENDER_TIMEOUT_S,
                canvas: Optional[Mapping] = None) -> RenderResult:
    """Render ``edl`` to ``out_path`` (see the module note).

    ``encoder`` is 'auto' (h264_nvenc when its test encode works, else
    libx264), 'h264_nvenc' or 'libx264'.  ``timeout_s`` bounds the whole
    render; each ffmpeg run gets what is left of it (pass-1 runs also a
    per-segment bound).  Raises ValueError for an unknown encoder or an EDL
    that is not structurally a ``hart.reel.edl.v1`` document (its quality
    rules are QA's job), media_bin.FFmpegFailed when ffmpeg fails or the
    budget runs out, media_bin.MediaToolMissing without ffmpeg.
    """
    if encoder not in ENCODERS:
        raise ValueError('encoder must be one of %s, got %r' % (ENCODERS, encoder))
    broken = [p for p in validate_edl(edl) if p.startswith('schema:')]
    if broken:
        raise ValueError('not a hart.reel.edl.v1 document: %s' % '; '.join(broken[:5]))
    size = _canvas(canvas)
    deadline = time.monotonic() + float(timeout_s)
    notes: List[str] = []
    hdr = any(c['transfer'] != 'sdr' for c in edl['clips'].values())
    caps = media_bin.ffmpeg_caps() if (hdr or encoder == 'auto') else None
    tonemap = bool(caps and 'zscale' in caps.filters and 'tonemap' in caps.filters)
    if hdr and not tonemap:
        notes.append(NOTE_HDR_UNTONED)
        logger.warning('this ffmpeg has no zscale/tonemap; HDR clips render untoned')
    chosen = encoder
    if encoder == 'auto':
        chosen = 'h264_nvenc' if caps.nvenc_ok else 'libx264'

    out_path = os.path.abspath(out_path)
    stem, ext = os.path.splitext(out_path)
    part = stem + '.part' + (ext or '.mp4')
    os.makedirs(workdir, exist_ok=True)
    made: List[str] = [part]
    try:
        segment_paths = []
        for i, seg in enumerate(edl['segments']):
            clip = edl['clips'][seg['clip']]
            path = os.path.join(workdir, 'reel_seg_%03d.mp4' % i)
            made.append(path)
            _run(plan_segment_args(seg, clip, path, canvas=size, tonemap=tonemap), deadline,
                 _segment_bound(seg, clip))
            segment_paths.append(path)
        listing = os.path.join(workdir, 'reel_concat.txt')
        made.append(listing)
        _write_concat(listing, segment_paths)
        textfiles = _write_texts(edl, workdir, size, notes, made)
        try:
            _run(plan_final_args(edl, listing, part, encoder=chosen, textfiles=textfiles,
                                 canvas=size), deadline, None)
        except media_bin.FFmpegFailed as e:
            if chosen != 'h264_nvenc' or e.timed_out:
                raise
            logger.warning('h264_nvenc failed (%s); encoding with libx264', e)
            notes.append(NOTE_NVENC_FAILED)
            chosen = 'libx264'
            _run(plan_final_args(edl, listing, part, encoder=chosen, textfiles=textfiles,
                                 canvas=size), deadline, None)
        os.replace(part, out_path)
    finally:
        _remove(made)
    frames = sum(segment_frames(s) for s in edl['segments'])
    return RenderResult(path=out_path, duration_s=frames / float(FPS),
                        size_bytes=os.path.getsize(out_path), encoder=chosen,
                        notes=_dedupe(notes))


__all__ = ['FPS', 'ENCODERS', 'RENDER_TIMEOUT_S', 'SEGMENT_TIMEOUT_BASE_S',
           'SEGMENT_TIMEOUT_PER_S', 'PASS1_ENCODE', 'X264_FINAL', 'NVENC_FINAL', 'RATE_CAP',
           'BT709', 'TEXT_MAX_LINES', 'TEXT_LINE_EM', 'TEXT_EM_LATIN', 'TEXT_EM_INDIC',
           'TEXT_EM_MARK', 'TEXT_EM_SPACE', 'NOTE_TEXT_NO_FONT', 'NOTE_EMOJI_DROPPED',
           'NOTE_NVENC_FAILED', 'NOTE_HDR_UNTONED', 'escape_filter_path', 'segment_frames',
           'geometry_filter', 'plan_segment_args', 'plan_final_args', 'text_width', 'wrap_text',
           'text_block_size', 'render_reel']
