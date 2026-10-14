"""integrations.reel_studio.pipeline -- her clips in, a checked reel on her phone.

``make_reel`` is one reel_make call, start to finish, as one job
(jobs.start_job .. jobs.finish_job, so only one reel is made on the desktop
at a time):

1. Refuse early, before anything is decoded: no clips (``needs_clips``),
   another reel being made (``busy``), less free disk than MIN_FREE_BYTES
   plus the clips' size where the work dir lives (``no_space``), or no room
   left in her store quota for the finished reel (``quota``).
2. ``produce_reel`` makes the reel in a work dir of its own under
   ``get_data_dir()/reel_studio/work/<reel_id>/``:
   probe every clip -> put them in the order she filmed them (the phone's
   captured_at from her upload, else the container's creation time, else the
   upload order: contracts.resolve_capture_order, amendment A1) -> one
   streaming analysis pass per clip -> at most 25 keyframes -> the VLM's
   judgement of them (falls back per frame) -> the beat grid (her song audio
   when she attached it, else a steady 120 BPM grid) -> the plan (EDL) ->
   the two-pass render -> QA.
3. A reel that fails a hard QA check is withheld (``qa_failed``): it is
   never put in her outbox or pushed, and her clips are kept for a retry.
4. Otherwise it goes into her outbox (user_file_store.offer_to_device, which
   writes the file's own facts into the reply ``media`` item), is pushed to
   her chat topic with the turn's request id and never through central's
   crossbar (decision U4(d)), and the clips (and song audio) it used are
   marked consumed, so the store's retention removes them a day later.
   A reel with no song to suggest says so, and why (``song_note``, the
   trending list's note), in the push, the reply's message and its notes,
   and her agent is asked to name no song.

The work dir is removed whatever happens (and, when the process dies
mid-reel, by jobs when the job is interrupted or taken over), the job is
finished with the status the call returns, and the store's GC runs once the
job is done (so retention does not wait for her next upload).  Progress is
shown on her chat through the existing publish_chat_stage (action 'Status'),
with generic texts only; each step also beats the job (jobs.beat), so a slow
reel is never taken for a dead one.

Every result is a dict with ``status`` (contracts.REEL_STATUSES) and a
``message`` for her agent to act on; ``ok`` adds reel_id, media, qa,
moments and notes (plan 4.2(g)).  make_reel never raises: an unexpected bug
is a ``render_failed`` with its type in ``detail``, logged with a traceback.

Nothing heavy is imported at module level: the media modules (cv2, numpy,
ffmpeg) load when a reel is made.
"""
from __future__ import annotations

import errno
import logging
import math
import os
import shutil
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence

from integrations.reel_studio import contracts as C
from integrations.reel_studio import jobs
# A job's work dir belongs to the job (jobs removes it when a job dies);
# the names stay importable from here for the E2E CLI.
from integrations.reel_studio.jobs import remove_work_dir, work_root

logger = logging.getLogger(__name__)

#: Free disk the work dir needs besides the clips themselves: the analysis
#: holds little, but the pass-1 mezzanines of a 30 s reel from 4K HDR clips
#: and the final encode need room (plan WS-9).
MIN_FREE_BYTES = 3 << 30
#: The most clips one reel uses (the phone's picker allows 10).
MAX_CLIPS = 10
MIME = 'video/mp4'
#: Notes make_reel adds to the reel's own (EDL, moments, render).
NOTE_SYNTHETIC_BEATS = 'beats:synthetic'
NOTE_PUSH_NOT_SENT = 'push_not_sent'
#: An advisory QA row that did not pass, e.g. 'advisory:face_render'.
ADVISORY_NOTE = 'advisory:%s'

#: Why a clip cannot be used, in words her agent can pass on.
_CLIP_REASONS = {
    'dolby_vision_p5': 'it is a Dolby Vision (profile 5) video, which has no standard picture '
                       'to edit; she can record without Dolby Vision or export it as HDR10/SDR',
    'unreadable': 'the file could not be read as a video',
    'no_video': 'it has no video in it',
    'too_small': 'it is too small to fill a 1080x1920 reel',
}


class ReelFailed(Exception):
    """A reel that could not be made, with its reel_make ``status``."""

    def __init__(self, status: str, message: str = '', **data: Any) -> None:
        super().__init__('%s: %s' % (status, message) if message else status)
        if status not in C.REEL_STATUSES:
            raise ValueError('not a reel status: %r' % (status,))
        self.status = status
        self.message = message
        self.data = data


@dataclass
class Production:
    """What produce_reel made: the file, its EDL, moments, QA report and render."""
    path: str
    edl: Dict[str, Any]
    moments: Dict[str, Any]
    qa: Dict[str, Any]
    render: Any                       # contracts.RenderResult
    beats_source: str
    notes: List[str] = field(default_factory=list)
    timings: Dict[str, float] = field(default_factory=dict)
    analyses: List[Any] = field(default_factory=list)
    #: Where in her song audio the beats were read from (seconds).
    song_start_s: float = 0.0


# ── how much room ─────────────────────────────────────────────────────────

def free_bytes(path: str) -> int:
    """Free disk where ``path`` is or will be.  shutil.disk_usage needs a
    path that exists, so it is asked of ``path``'s nearest existing
    ancestor; nothing is created to measure it (amendment A11b)."""
    probe = os.path.abspath(path)
    while not os.path.isdir(probe):
        parent = os.path.dirname(probe)
        if parent == probe:         # a drive or root that is not there: let it say so
            break
        probe = parent
    return shutil.disk_usage(probe).free


def reel_bytes_cap(target_s: float) -> int:
    """The most bytes a reel of ``target_s`` may have: QA's size cap at the
    longest duration the planner may cut."""
    cap = C.QUALITY_BAR['size_cap']
    longest = float(target_s) + C.QUALITY_BAR['duration']['tol_s']
    return int(longest * cap['mbps'] * 1e6 / 8.0 + cap['slack_bytes'])


# ── small helpers ─────────────────────────────────────────────────────────

def _source(clip) -> C.ClipSource:
    return clip if isinstance(clip, C.ClipSource) else C.ClipSource.from_stored(clip)


def _gib(n: int) -> str:
    return '%.1f GB' % (n / float(1 << 30))


def _say(job) -> Callable[[str, str], None]:
    """Each step of the reel: the job beats (jobs.beat), and her chat shows
    the step through publish_chat_stage with a generic text."""
    def say(stage: str, text: str) -> None:
        jobs.beat(job.reel_id)
        try:
            from core.peer_link.crossbar_publish import publish_chat_stage
            publish_chat_stage(stage, user_id=str(job.user_id),
                               request_id=str(job.request_id or ''), text=text)
        except Exception as e:      # progress never stops the reel
            logger.debug('reel progress %s not shown: %s', stage, e)
    return say


def _status(status: str, message: str, **data: Any) -> Dict[str, Any]:
    out = {'status': status, 'message': message}
    out.update(data)
    return out


# ── making the reel ───────────────────────────────────────────────────────

def _probe_all(probe_mod, sources: Sequence[C.ClipSource]) -> list:
    infos = []
    for s in sources:
        try:
            infos.append(probe_mod.probe(s.path))
        except C.ClipUnsupported as e:
            raise ReelFailed('clip_unsupported', _clip_message(s.name, e.reason),
                             xid=s.xid, reason=e.reason)
    return infos


def _clip_message(name: str, reason: str) -> str:
    why = _CLIP_REASONS.get(reason, reason)
    return ('The clip %s cannot be used: %s. Ask her to send it again that way, or leave it '
            'out.' % (name or 'she sent', why))


def _need_more(have_s: float, target_s: float) -> float:
    need = target_s - C.QUALITY_BAR['duration']['tol_s'] - have_s
    return max(0.1, math.ceil(need * 10.0) / 10.0)


def _footage_message(need_s: float, target_s: float) -> str:
    return ('The clips are too short for a %g s reel: about %.1f s more footage is needed. Ask '
            'her for more clips, or make a shorter reel.' % (target_s, need_s))


def _beat_grid(beats_mod, song_audio: Optional[str], start_s: float, style: Mapping,
               song_key: Optional[str]):
    """(grid, start used): her song's beats from ``start_s`` when it has them
    there, else from the song's start, else the synthetic grid."""
    dur = float(style['target_s']) + C.QUALITY_BAR['duration']['tol_s']
    start_s = max(0.0, float(start_s or 0.0))
    grid = beats_mod.reel_beat_grid(song_audio, dur_s=dur, start_s=start_s, song_key=song_key)
    if song_audio and grid.source == 'synthetic' and start_s > 0:
        # Her file may be the song from its beginning or a short excerpt that
        # ends before the suggested start: read it from 0.
        retry = beats_mod.reel_beat_grid(song_audio, dur_s=dur, start_s=0.0, song_key=song_key)
        if retry.source != 'synthetic':
            return retry, 0.0
    return grid, start_s


def produce_reel(sources: Sequence[Any], *, style: Mapping, workdir: str, out_path: str,
                 song_audio: Optional[str] = None, song_start_s: float = 0.0,
                 song_key: Optional[str] = None, logo_path: Optional[str] = None,
                 backend=None, encoder: str = 'auto',
                 progress: Optional[Callable[[str, str], None]] = None,
                 detector=None) -> Production:
    """Make the reel from ``sources`` (ClipSource, or StoredFile dicts, in
    upload order) into ``out_path``, working in ``workdir`` (the caller
    removes it).

    ``style`` goes through contracts.make_style.  ``song_audio`` is her song
    file (store purpose 'song_audio') read from ``song_start_s``;
    ``backend`` is the vision backend (None: moments.REEL_VLM_DEFAULT);
    ``encoder`` is render_reel's; ``progress(stage, text)`` is told what is
    happening; ``detector`` is the face detector (None: Haar).

    Raises ReelFailed with status clip_unsupported (xid, reason),
    not_enough_footage (need_s) or render_failed (detail).  The QA report is
    returned, not judged: make_reel withholds a failed reel, the E2E CLI
    shows it.
    """
    from core import media_bin
    from integrations.reel_studio.media import analyze as analyze_mod
    from integrations.reel_studio.media import beats as beats_mod
    from integrations.reel_studio.media import edl as edl_mod
    from integrations.reel_studio.media import keyframes as keyframes_mod
    from integrations.reel_studio.media import moments as moments_mod
    from integrations.reel_studio.media import probe as probe_mod
    from integrations.reel_studio.media import qa as qa_mod
    from integrations.reel_studio.media import render as render_mod

    style = C.make_style(dict(style))
    srcs = [_source(s) for s in sources]
    if not srcs:
        raise ReelFailed('needs_clips', NEEDS_CLIPS_MESSAGE)
    say = progress or (lambda stage, text: None)
    timings: Dict[str, float] = {}
    clock = [time.monotonic()]

    def lap(name: str) -> None:
        now = time.monotonic()
        timings[name] = round(now - clock[0], 3)
        clock[0] = now

    os.makedirs(workdir, exist_ok=True)
    try:
        say('reel_probe', 'Checking your %d clip%s...' % (len(srcs), '' if len(srcs) == 1 else 's'))
        infos = _probe_all(probe_mod, srcs)
        have = sum(float(i.duration_s) for i in infos)
        if have < style['target_s'] - C.QUALITY_BAR['duration']['tol_s']:
            need = _need_more(have, style['target_s'])
            raise ReelFailed('not_enough_footage', _footage_message(need, style['target_s']),
                             need_s=need)
        captures, _batch = C.resolve_capture_order(
            [{'clip': s.xid, 'captured_at_ms': s.captured_at_ms,
              'creation_time': i.creation_time} for s, i in zip(srcs, infos)])
        by_xid = {c.clip: c for c in captures}
        story = sorted(range(len(srcs)), key=lambda k: by_xid[srcs[k].xid].order)
        lap('probe')

        analyses = []
        for n, k in enumerate(story, 1):
            say('reel_analyse', 'Watching clip %d of %d...' % (n, len(story)))
            try:
                analysis = analyze_mod.analyze_clip(infos[k], clip_id=srcs[k].xid,
                                                    detector=detector)
            except C.ClipUnsupported as e:
                raise ReelFailed('clip_unsupported', _clip_message(srcs[k].name, e.reason),
                                 xid=srcs[k].xid, reason=e.reason)
            analysis.capture = by_xid[srcs[k].xid]
            analyses.append(analysis)
        lap('analyse')

        say('reel_moments', 'Finding the best moments...')
        keys = keyframes_mod.pick_keyframes(analyses)
        moments = moments_mod.score_moments(keys, backend=backend)
        lap('moments')

        grid, start_used = _beat_grid(beats_mod, song_audio, song_start_s, style, song_key)
        lap('beats')

        say('reel_plan', 'Planning the cuts...')
        try:
            edl = edl_mod.plan_edit(moments, analyses, grid, style, logo_path=logo_path)
        except C.PlanError as e:
            if e.code == 'not_enough_footage':
                need = float(e.data.get('need_s') or 0.1)
                raise ReelFailed('not_enough_footage', _footage_message(need, style['target_s']),
                                 need_s=need)
            if e.code == 'clip_unsupported':
                xid = e.data.get('xid')
                name = next((s.name for s in srcs if s.xid == xid), '')
                raise ReelFailed('clip_unsupported', _clip_message(name, e.data.get('reason', '')),
                                 xid=xid, reason=e.data.get('reason'))
            raise ReelFailed('render_failed', RENDER_FAILED_MESSAGE, detail='plan: %s' % e)
        lap('plan')

        say('reel_render', 'Rendering your reel...')
        rendered = render_mod.render_reel(edl, out_path, workdir=workdir, encoder=encoder)
        lap('render')

        say('reel_qa', 'Checking the reel...')
        report = qa_mod.run_qa(out_path, edl, analyses=analyses)
        lap('qa')
    except media_bin.MediaToolMissing as e:
        raise ReelFailed('render_failed', RENDER_FAILED_MESSAGE,
                         detail='no ffmpeg on this desktop: %s' % e)
    except media_bin.FFmpegFailed as e:
        raise ReelFailed('render_failed', RENDER_FAILED_MESSAGE,
                         detail=('ffmpeg timed out' if e.timed_out else str(e))[:300])
    return Production(path=out_path, edl=edl, moments=moments, qa=report, render=rendered,
                      beats_source=grid.source, notes=list(rendered.notes), timings=timings,
                      analyses=analyses, song_start_s=start_used)


# ── what she gets ─────────────────────────────────────────────────────────

NEEDS_CLIPS_MESSAGE = ('There are no clips to make a reel from. Ask her to attach the videos '
                       'for this reel (1 to 10 from her gallery), then call reel_make again.')
BUSY_MESSAGE = ('This desktop is making another reel right now. Tell her it will be free in a '
                'few minutes, then try again.')
RENDER_FAILED_MESSAGE = ('The desktop could not render the reel. Try once more; if it fails '
                         'again, tell her the desktop could not make this reel.')


def _song_item(song: Optional[Mapping], beats_source: str, start_used_s: float):
    if not isinstance(song, Mapping):
        return None
    start = song.get('start_ms')
    start = start if isinstance(start, int) and not isinstance(start, bool) and start >= 0 else 0
    if beats_source != 'synthetic':
        start = int(round(start_used_s * 1000))
    key, lang = song.get('song_key'), song.get('lang')
    return {'song_key': key if isinstance(key, str) and key else None,
            'title': str(song.get('title') or ''), 'artist': str(song.get('artist') or ''),
            'lang': lang if isinstance(lang, str) and lang else None,
            'start_ms': start, 'start_label': C.start_label(start), 'beats': beats_source}


def _reel_notes(production: Production) -> List[str]:
    notes = list(production.edl.get('notes') or [])
    notes += list(production.moments.get('notes') or [])
    notes += list(production.notes)
    if production.beats_source == 'synthetic':
        notes.append(NOTE_SYNTHETIC_BEATS)
    notes += [ADVISORY_NOTE % row.get('id') for row in production.qa.get('advisory') or ()
              if isinstance(row, Mapping) and not row.get('ok')]
    return list(dict.fromkeys(n for n in notes if isinstance(n, str) and n))


def _suggests_song(song: Optional[Mapping]) -> bool:
    """Does the reel suggest a song: one with a title (from the trending
    list, or her own audio's name)?  A key the list does not hold has none."""
    return isinstance(song, Mapping) and bool(song.get('title'))


def _ready_text(media: Mapping, song_note: Optional[str] = None) -> str:
    """The push's text (never empty): the reel, the song -- or that none is
    suggested, and ``song_note``, why -- and which beat the cuts follow
    (decision U2: the reply says when they follow a steady one)."""
    parts = ['Your reel is ready (%d s).' % int(round(media['duration_s']))]
    song = media.get('song')
    if _suggests_song(song):
        who = (' by %s' % song['artist']) if song.get('artist') else ''
        parts.append('Suggested song: %s%s, start it at %s in Instagram.'
                     % (song['title'], who, song['start_label']))
    else:
        parts.append('No song is suggested.')
        if song_note:
            parts.append(song_note)
    if (song or {}).get('beats', 'synthetic') == 'synthetic':
        parts.append("The cuts follow a steady beat, not a song's own; attach the song's audio "
                     'next time to cut on its beats.')
    return ' '.join(parts)


def _ok_message(media: Mapping, notes: Sequence[str], song_note: Optional[str] = None) -> str:
    """What her agent is told to say.  It carries what the push's text says
    too (decision U2: the steady beat; no song, and why), because her phone
    shows one of the reply and the push (it dedups them by xid), not always
    the push.  With no song suggested it asks for none: a song named here
    would be one the reel does not have."""
    text = ('The reel is made, checked and on its way to her phone (it also rides this '
            'reply). Tell her it is ready, ')
    if _suggests_song(media.get('song')):
        text += ('give her the caption and the hashtags to paste, and the song to add in '
                 'Instagram at the start time given.')
    else:
        text += ('and give her the caption and the hashtags to paste. No song is suggested '
                 'for this reel, so do not name one.')
        if song_note:
            text += ' Tell her why: ' + song_note
    if NOTE_SYNTHETIC_BEATS in notes:
        text += (" The cuts follow a steady beat, not the song's own: say so, and that "
                 "attaching the song's audio next time cuts the reel on its beats.")
    if C.NOTE_NO_FINAL_LOOK in notes:
        text += (' No finished look was found in the clips, so the reel ends on the last shot; '
                 'say so.')
    return text


def _media_item(*, reel_id: str, production: Production, caption: str,
                hashtags: Sequence[str], song: Optional[Mapping], notes: Sequence[str],
                qa: Mapping) -> Dict[str, Any]:
    """The reply media item, less what the store writes from the file itself
    (xid, sha256, size, mime, prompt_id, request_id)."""
    return {
        'kind': 'video', 'width': int(C.CANVAS['w']), 'height': int(C.CANVAS['h']),
        'duration_s': round(float(production.render.duration_s), 3), 'has_audio': False,
        'reel_id': reel_id, 'caption': str(caption or ''),
        'hashtags': [t for t in hashtags if isinstance(t, str)][:C.HASHTAG_LIMIT],
        'song': _song_item(song, production.beats_source, production.song_start_s),
        'notes': list(notes), 'qa': dict(qa),
    }


def _deliver(job, store, out_path: str, item: Dict[str, Any]) -> Dict[str, Any]:
    """Put the reel in her outbox and return the media item the store completed."""
    preview = dict(item, xid='0' * 64, sha256='0' * 64, mime=MIME,
                   size=max(1, os.path.getsize(out_path)),
                   prompt_id=job.prompt_id, request_id=job.request_id)
    problems = C.validate_media_item(preview)
    if problems:
        raise ReelFailed('render_failed', RENDER_FAILED_MESSAGE,
                         detail='media item: %s' % '; '.join(problems[:3]))
    try:
        stored = store.offer_to_device(job.user_id, out_path, mime=MIME,
                                       purpose=C.OUTBOUND_PURPOSE, prompt_id=job.prompt_id,
                                       request_id=job.request_id, meta={'media': item},
                                       name='reel_%s.mp4' % job.reel_id)
    except OSError as e:
        if e.errno == errno.ENOSPC:
            raise ReelFailed('no_space', 'The desktop ran out of disk space saving the reel. '
                             'Ask her to free some space on the desktop, then try again.')
        raise ReelFailed('render_failed', RENDER_FAILED_MESSAGE, detail='outbox: %s' % e)
    return stored.meta['media']


def _make(job, store, sources: List[C.ClipSource], style: Dict[str, Any],
          song: Optional[Mapping], logo_path: Optional[str], caption: str,
          hashtags: Sequence[str], backend, encoder: str, workdir: str,
          song_note: Optional[str] = None) -> Dict[str, Any]:
    need = MIN_FREE_BYTES + sum(max(0, int(s.size)) for s in sources)
    free = free_bytes(work_root())
    if free < need:
        raise ReelFailed('no_space', 'The desktop needs about %s of free disk to make this reel '
                         'and has %s. Ask her to free some space on the desktop, then try again.'
                         % (_gib(need), _gib(free)), need_bytes=need, free_bytes=free)
    reserve = reel_bytes_cap(style['target_s'])
    if store.usage(job.user_id) + reserve > store.policy['user_quota']:
        raise ReelFailed('quota', 'Her space for clips on this desktop is full. Clips are cleared '
                         'a day after each reel; ask her to send fewer or shorter clips, or to try '
                         'again tomorrow.')

    say = _say(job)
    out_path = os.path.join(workdir, 'reel_%s.mp4' % job.reel_id)
    song = song if isinstance(song, Mapping) else None
    start_ms = (song or {}).get('start_ms')
    production = produce_reel(
        sources, style=style, workdir=workdir, out_path=out_path,
        song_audio=(song or {}).get('audio_path') or None,
        song_start_s=(start_ms / 1000.0 if isinstance(start_ms, int) and start_ms > 0 else 0.0),
        song_key=(song or {}).get('song_key') or None, logo_path=logo_path, backend=backend,
        encoder=encoder, progress=say)

    qa = C.qa_summary(production.qa)
    if not qa['passed']:
        raise ReelFailed('qa_failed', 'The reel was made but failed its checks (%s), so it was '
                         'not sent. Try once more; if it fails again, tell her.'
                         % ', '.join(qa['failed']), failed=qa['failed'])

    notes = _reel_notes(production)
    item = _media_item(reel_id=job.reel_id, production=production, caption=caption,
                       hashtags=hashtags, song=song, notes=notes, qa=qa)
    say('reel_deliver', 'Sending the reel to your phone...')
    media = _deliver(job, store, out_path, item)

    # Why no song is suggested, when none is (the trending list's note, from
    # reel_make): in the push, what her agent is told, and the notes.
    song_note = (song_note.strip() if isinstance(song_note, str) else '') or None
    if _suggests_song(media.get('song')):
        song_note = None
    from core.peer_link.crossbar_publish import publish_agent_message
    sent = publish_agent_message(text=_ready_text(media, song_note), user_id=job.user_id,
                                 request_id=job.request_id, prompt_id=job.prompt_id,
                                 media=[media], skip_crossbar=True)
    from core.user_file_store import is_xid
    used = [s.xid for s in sources]
    if song and is_xid(song.get('audio_xid')):
        used.append(song['audio_xid'])
    try:
        store.mark_consumed(job.user_id, used)
    except Exception as e:      # the reel is out; retention just waits longer
        logger.warning('reel %s: could not mark %d source file(s) used: %s', job.reel_id,
                       len(used), e)
    out_notes = (notes + ([] if sent else [NOTE_PUSH_NOT_SENT])
                 + ([song_note] if song_note else []))
    logger.info('reel %s for user %s: ok, %.1f s, %d bytes, moments %s, timings %s',
                job.reel_id, job.user_id, media['duration_s'], media['size'],
                production.moments.get('src'), production.timings)
    return {'status': 'ok', 'reel_id': job.reel_id, 'media': media, 'qa': qa,
            'moments': {'src': production.moments.get('src'),
                        'final_look_found': bool(production.moments.get('final_look_found'))},
            'notes': out_notes, 'message': _ok_message(media, notes, song_note)}


def make_reel(*, user_id, prompt_id, request_id, clips: Sequence[Any], style: Mapping,
              song: Optional[Mapping] = None, logo_path: Optional[str] = None, caption: str = '',
              hashtags: Sequence[str] = (), backend=None, encoder: str = 'auto',
              store=None, song_note: Optional[str] = None) -> Dict[str, Any]:
    """Make, check and deliver one reel for this turn (see the module note).

    ``clips``: StoredFile dicts (or contracts.ClipSource), at most MAX_CLIPS
    used, in upload order.  ``style``: a style dict (contracts.make_style).
    ``song``: ``{song_key, title, artist, lang, start_ms}`` plus, when she
    attached the song's audio, ``audio_path`` and ``audio_xid``; or None.
    The reel suggests a song only when it has a title.  ``song_note``: why
    it suggests none (the trending list's note, e.g. no research account on
    this computer); with no song suggested, the push, what her agent is told
    and the result's notes carry it, and her agent is asked to name no song.
    ``logo_path``: her logo image, or None.  ``hashtags``: '#tags', at most
    contracts.HASHTAG_LIMIT used.  ``backend``: the vision backend (None:
    moments.REEL_VLM_DEFAULT).  ``store``: the user file store (None: the
    process's).  Returns the reel_make result; never raises.
    """
    sources = [_source(c) for c in (clips or [])][:MAX_CLIPS]
    if not sources:
        return _status('needs_clips', NEEDS_CLIPS_MESSAGE)
    style = C.make_style(dict(style or {}))
    try:
        job = jobs.start_job(user_id, prompt_id, request_id)
    except Exception as e:      # e.g. the disk filled writing its record: no job holds the machine
        logger.exception('reel for user %s: the job could not be started', user_id)
        return _status('render_failed', RENDER_FAILED_MESSAGE, detail=_detail(e))
    if job is None:
        return _status('busy', BUSY_MESSAGE)
    workdir = None
    try:
        # Both inside the job (amendment A11a): a store or data dir that
        # cannot be opened ends this job like any failure, and never leaves
        # it holding the machine.
        if store is None:
            from core.user_file_store import get_user_file_store
            store = get_user_file_store()
        workdir = jobs.work_dir(job.reel_id)
        result = _make(job, store, sources, style, song, logo_path, caption, hashtags, backend,
                       encoder, workdir, song_note=song_note)
    except ReelFailed as e:
        logger.info('reel %s for user %s: %s', job.reel_id, job.user_id, e)
        result = dict(_status(e.status, e.message or e.status, **e.data), reel_id=job.reel_id)
    except Exception as e:      # a bug: report it, never crash the agent's turn
        logger.exception('reel %s for user %s failed', job.reel_id, job.user_id)
        result = _status('render_failed', RENDER_FAILED_MESSAGE, reel_id=job.reel_id,
                         detail=_detail(e))
    finally:
        if workdir is not None:
            remove_work_dir(workdir)
    try:
        jobs.finish_job(job.reel_id, result['status'], result.get('detail') or '')
    except Exception as e:
        logger.error('reel %s: job record not finished: %s', job.reel_id, e)
    if store is not None:
        try:
            store.gc()
        except Exception as e:
            logger.warning('reel %s: store retention pass failed: %s', job.reel_id, e)
    return result


def _detail(e: BaseException) -> str:
    """An unexpected error, for the result's ``detail`` and the job record."""
    return ('%s: %s' % (type(e).__name__, e))[:300]


__all__ = ['MIN_FREE_BYTES', 'MAX_CLIPS', 'MIME', 'NOTE_SYNTHETIC_BEATS', 'NOTE_PUSH_NOT_SENT',
           'ADVISORY_NOTE', 'NEEDS_CLIPS_MESSAGE', 'BUSY_MESSAGE', 'RENDER_FAILED_MESSAGE',
           'ReelFailed', 'Production', 'work_root', 'free_bytes', 'remove_work_dir',
           'reel_bytes_cap',
           'produce_reel', 'make_reel']
