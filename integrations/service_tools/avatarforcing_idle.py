"""An avatar's IDLE face: 60 s of silence spoken by its portrait, made by the same AvatarForcing model as
the live face, so idle and live look like one person.

Owner 2026-11-27: "60 seconds of idle video using the same no volume audio that is used in central";
"idle face video link gets created at agent creation time ... set when we get agent list".  Central's
MakeItTalk idle filler is its Blank.wav (60.0 s, every sample 0) spoken by the portrait (MakeItTalk
task_c.py:2094), posted as the avatar's filler_type 'idle'.  The engine reads 16 kHz mono, so the same
silence is written once here (1.9 MB) instead of shipping that 11.5 MB file.

How a clip is made (design: review hartos-17 GO, MUSTs I1-I8):
  * want_clip(avatar) is asked where the agent list is built (an agent with a face that has no clip for
    its current portrait): at creation, on sync, on a portrait change and at boot alike, from ONE
    predicate, with no stored queue, so a restart loses nothing.
  * The job runs only while the face model is already running (it never starts the model: a video turn
    does that through the lifecycle's own decision, and it never stamps the face as used, so it never
    keeps a 4.8 GB model loaded for itself), and only when it is quiet (_quiet_enough): no turn
    being served (core.foreground.foreground_active: /chat's mark_view), nobody at the computer
    (dispatch.person_using_the_machine, the yield gate's one definition), no chat in the last
    IDLE_QUIET_S (the reply's voice and face are still being made after the handler returns; read
    from the yield gate's own last-chat stamps, dispatch.seconds_since_user_chat), and no reply's
    face being made (avatarforcing_tool.live_face_open).  It does NOT wait for the 10-minute chat
    cooldown: the face is evicted 600 s after its last live reply, so that window would almost never
    open (review hartos-17, 27 Nov).  The turn signal is polled while the clip is read, and any turn
    -- voice or video -- stops it at once.  A clip a turn interrupted stays wanted, and each retry
    waits twice as long (IDLE_RETRY_S up to IDLE_BACKOFF_MAX_S) until one completes.  It is the
    lowest-priority GPU work.
  * It is a normal sidecar session: POST /session with the silence and the portrait, then this module
    reads GET /live.mp4?sid (a viewer is what starts generation) and discards the bytes.  The sidecar's
    save_stream keeps every COMPLETE stream as <sid>.mp4, so when the read ends the file is there; it
    is renamed (same folder, no copy) to idle_<avatar>_<portrait mtime_ns>.mp4, and that avatar's older
    clips are removed.
  * cancel_idle() stops a clip being made, so a live reply never waits behind it -- also one whose
    session is still being opened (the POST in flight).  Its stream is then incomplete, nothing is
    kept, and the next listing asks again.
  * The agent list carries it with the live face's own link builder (avatarforcing_tool.face_links):
    an absolute ``video_link`` a phone can play and a same-origin ``page_link`` for the page.

A WARM session (warm_session) uses the same session path for a different job: right after a video
turn's setup brought the face model up, one 2 s silent session for that turn's avatar, so the
person's next reply starts warm.  It is not idle work: it skips the quiet period and the presence
gate (the person just asked for this face) and keeps nothing; it still yields to a turn being
served or a reply's face, and cancel_idle stops it.  One session at a time goes through this
module, warm or clip.
"""
import logging
import os
import threading
import time
import wave
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

TOOL = 'avatarforcing'
#: The owner's length, as central's Blank.wav.
IDLE_SECONDS = 60
#: The rate the engine reads (realtime_lowvram SR); zeros at this rate are central's silence after its
#: resample.
SILENCE_RATE = 16000
#: While it is not quiet yet, a wanted clip is looked at again this often (a pause between turns is
#: what a clip needs, so it is not waited out by minutes).
IDLE_RETRY_S = 15.0
#: A clip starts only this long after the person's last chat (review hartos-17, snapshot9): the
#: /chat turn signal ends with the handler, while the reply's voice (the TTS thread) and its face are
#: still being made for a few seconds more, and the person is often reading or thinking.  45 s clears
#: that tail and is still well inside the face's 600 s idle timeout.
IDLE_QUIET_S = 45.0
#: After each clip a turn interrupted, the next attempt waits twice as long, from IDLE_RETRY_S up to
#: this (15, 30, 60, ... 300 s), so a busy conversation costs a few 1-3 s attempts, not one per gap.
#: A completed clip resets it.
IDLE_BACKOFF_MAX_S = 300.0
#: While a clip is read, a turn starting is looked for this often.
TURN_POLL_S = 1.0
#: 60 s of face at the measured 1.46x real time is ~41 s; this bounds a slow GPU.
IDLE_READ_TIMEOUT_S = 300.0
POST_TIMEOUT_S = 5.0
IDLE_PREFIX = 'idle_'
#: A warm session's silence (warm_session): one audio window, the 50 frames at 25 fps the sidecar's
#: audio graph is captured for (stream_server: prepare_graphs(audio_frames=50)); shorter audio is
#: padded to that window (engine.audio_features), so this runs the same kernels a reply does.
WARM_SECONDS = 2
#: Where this node serves an agent's idle face (keyed by AGENT, review hartos-17 I1: one avatar can front
#: several agents, and an agent's visibility is the listing's rule).  The listing carries it through the
#: live face's own builder (avatarforcing_tool.face_links): ``page_link`` same-origin for the desktop
#: page, whose loopback origin is what the LAN gate trusts, and ``video_link`` absolute on the address
#: this node advertises, for a phone -- or no ``video_link`` at all when there is no such address.
IDLE_PATH = '/avatar/idle/{prompt_id}.mp4'

_lock = threading.Lock()
_wanted = set()  # avatar ids waiting for a clip
# 'interrupted': the clip being made was stopped by a turn (kept wanted, made in the next pause).
_job = {'thread': None, 'sid': None, 'port': None, 'posting': False, 'cancel': False,
        'interrupted': False, 'backoff_s': 0.0}


def face_output_dir() -> Path:
    """The face sidecar's output folder: the one answer, avatarforcing_tool.face_output_dir."""
    from .avatarforcing_tool import face_output_dir as _face_output_dir
    return _face_output_dir()


def _silence(seconds: int = IDLE_SECONDS) -> Path:
    """``seconds`` of digital silence at SILENCE_RATE, mono 16-bit, written once beside the output
    folder (not in it: it is an input, never served or pruned)."""
    path = face_output_dir().parent / f'idle_silence_{seconds}s.wav'
    want = seconds * SILENCE_RATE
    try:
        with wave.open(str(path), 'rb') as w:
            if w.getnframes() == want and w.getframerate() == SILENCE_RATE:
                return path
    except (OSError, wave.Error, EOFError):
        pass
    path.parent.mkdir(parents=True, exist_ok=True)
    part = path.with_name(path.name + '.part')
    with wave.open(str(part), 'wb') as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SILENCE_RATE)
        w.writeframes(b'\x00\x00' * want)
    os.replace(part, path)
    return path


def clip_name(avatar_id, portrait) -> str:
    """The idle clip's file name for this portrait version."""
    return f"{IDLE_PREFIX}{int(avatar_id)}_{os.stat(portrait).st_mtime_ns}.mp4"


def stored_clip(avatar_id) -> Optional[Path]:
    """The newest idle clip kept for this avatar, or None.  Reads only; makes nothing."""
    try:
        aid = int(avatar_id)
        found = list(face_output_dir().glob(f"{IDLE_PREFIX}{aid}_*.mp4"))
        return max(found, key=lambda p: p.stat().st_mtime) if found else None
    except (OSError, TypeError, ValueError):
        return None


def want_clip(avatar_id) -> None:
    """Ask for this avatar's idle clip; made in the background when the face is running and quiet."""
    from core.teacher_avatar import avatar_id_from
    aid = avatar_id_from(avatar_id)
    if aid is None:
        return
    with _lock:
        _wanted.add(aid)
        if _job['thread'] is not None and _job['thread'].is_alive():
            return
        t = threading.Thread(target=_worker, name='avatar-idle', daemon=True)
        _job['thread'] = t
    t.start()


def cancel_idle(reason: str = 'a live reply') -> bool:
    """Stop the session this module is running (a turn takes the GPU): an idle clip being made, or
    a warm session, also one still being opened.  True when there was one; an idle clip stays
    wanted and is made in the next pause, a warm is simply not run again."""
    with _lock:
        sid, port, posting = _job['sid'], _job['port'], _job['posting']
        if posting:
            _job['cancel'] = True  # make_clip stops it as soon as its sid is known
        if sid or posting:
            _job['interrupted'] = True
    if sid and port:
        _stop_session(port, sid)
    if sid or posting:
        logger.info(f"{TOOL} idle: session {sid or '(opening)'} stopped: {reason}")
    return bool(sid or posting)


def _stop_session(port, sid) -> None:
    try:
        import requests
        requests.delete(f'http://127.0.0.1:{port}/live.mp4', params={'sid': sid}, json={}, timeout=2)
    except Exception as e:
        logger.debug(f"{TOOL} idle: stop of {sid} not delivered: {e}")


def _sidecar_port() -> Optional[int]:
    from .runtime_manager import runtime_tool_manager
    return runtime_tool_manager.get_tool_port(TOOL)


def _turn_in_flight() -> bool:
    """A person's turn is being served right now: core.foreground.foreground_active (/chat's
    mark_view; this process's count, or another process's foreground marker).  Fails CLOSED: where
    core.foreground has no yield gate registered (the HARTOS gates are not wired in this process) or
    the signal cannot be read, a turn is assumed and no clip is made (review hartos-17)."""
    try:
        from core import foreground
        if not foreground.yield_gate_registered():
            return True
        return bool(foreground.foreground_active())
    except Exception:
        return True


def _seconds_since_user_chat() -> Optional[float]:
    """Seconds since the person's last chat, from the one source the yield gate's cooldown reads
    (dispatch.seconds_since_user_chat); None when none is that recent.  Fails closed: 0.0 (just
    chatted) when it cannot be read."""
    try:
        from integrations.agent_engine.dispatch import seconds_since_user_chat
        return seconds_since_user_chat()
    except Exception:
        return 0.0


def _person_at_the_machine() -> bool:
    """A person is using this computer: dispatch.person_using_the_machine, the ONE definition the
    yield gate names its person reasons from (a request being served, or input at the desk within
    the governor's idle threshold).  Not dispatch.background_work_must_yield: that one also asks
    whose TURN is running, and this job runs on no turn (no request id), where it always answers
    False.  Measured on build #8, 28 Nov 19:58: a clip rendered while the owner was at Nunba
    talking to the voice orb, and live audio stuttered.  The definition itself reads an
    unreadable foreground or governor as nobody (fail-open, so a broken read never starves the
    clip); only when dispatch cannot be imported at all is someone assumed and no clip made, as
    _seconds_since_user_chat already does.  Input at the desk is the OS probe (120 s threshold):
    a person who only talks to the voice orb is seen through the chat stamp, not here."""
    try:
        from integrations.agent_engine.dispatch import person_using_the_machine
        return person_using_the_machine() is not None
    except Exception:
        return True


def _quiet_enough() -> bool:
    """A clip may start now: no turn being served, nobody at the computer (_person_at_the_machine),
    no chat in the last IDLE_QUIET_S (the reply's voice and face tail, the person reading), and no
    reply's face being made (review hartos-17)."""
    if _turn_in_flight() or _person_at_the_machine():
        return False
    since = _seconds_since_user_chat()
    if since is not None and since < IDLE_QUIET_S:
        return False
    try:
        from .avatarforcing_tool import live_face_open
        return not live_face_open()
    except Exception:
        return False


def _worker() -> None:
    from .avatarforcing_tool import _portrait
    while True:
        with _lock:
            if not _wanted:
                _job['thread'] = None
                return
            aid = next(iter(_wanted))
        port = _sidecar_port()
        if not port:  # the face is not running: never started for an idle clip; the wish is kept
            with _lock:
                _job['thread'] = None
            return
        face = _portrait(aid)
        if not face or (face_output_dir() / clip_name(aid, face)).is_file():
            with _lock:  # no portrait, or this portrait's clip is kept already
                _wanted.discard(aid)
            continue
        if not _quiet_enough():
            time.sleep(IDLE_RETRY_S)
            continue
        with _lock:
            _job['interrupted'] = False
        made, wait = None, 0.0
        try:
            made = make_clip(aid, port)
        except Exception as e:
            logger.warning(f"{TOOL} idle: no clip for avatar {aid}: {type(e).__name__}: {e}")
        finally:
            with _lock:
                if _job['interrupted']:  # kept wanted; the next attempt waits longer each time
                    _job['backoff_s'] = (min(IDLE_BACKOFF_MAX_S, _job['backoff_s'] * 2)
                                         if _job['backoff_s'] else IDLE_RETRY_S)
                    wait = _job['backoff_s']
                else:  # made, or failed: not asked again until the next listing
                    _wanted.discard(aid)
                    if made is not None:
                        _job['backoff_s'] = 0.0
                _job['interrupted'] = False
        if wait:
            time.sleep(wait)


def make_clip(avatar_id, port, warm_seconds: Optional[int] = None) -> Optional[Path]:
    """Make this avatar's idle clip now through the running sidecar on ``port``; its path, or None.

    ``warm_seconds`` (warm_session): the same session with that much silence, made only to warm
    the engine for this portrait; nothing is kept -- the sidecar's saved stream is removed and its
    former path returned, so None still means "did not complete".  One session at a time goes
    through this module: a call while another is being opened or read returns None (an idle clip
    then counts as interrupted and stays wanted)."""
    from .avatarforcing_tool import _portrait
    face = _portrait(avatar_id)
    if not face:
        logger.info(f"{TOOL} idle: avatar {avatar_id} has no portrait here; no idle clip")
        return None
    out = face_output_dir()
    final = out / clip_name(avatar_id, face)
    if not warm_seconds and final.is_file():
        return final
    import requests
    from core.http_pool import pooled_post_once
    with _lock:
        if _job['posting'] or _job['sid']:  # cancel_idle can stop only one session it knows
            if not warm_seconds:
                _job['interrupted'] = True
            return None
        _job.update(posting=True, cancel=False)
    sid, r = None, None
    try:
        r = pooled_post_once(f'http://127.0.0.1:{port}/session', timeout=POST_TIMEOUT_S, json={
            'audio_url': _silence(warm_seconds or IDLE_SECONDS).resolve().as_uri(),
            'image_url': Path(face).resolve().as_uri()})
        sid = (r.json() or {}).get('sid') if r.status_code == 200 else None
    finally:
        with _lock:  # the sid is known in the same step, so cancel_idle never sees neither
            _job['posting'] = False
            if sid:
                _job.update(sid=sid, port=port)
    if not sid:
        logger.info(f"{TOOL} idle: the sidecar refused the idle session ({r.status_code})")
        return None
    with _lock:
        _job.update(sid=sid, port=port)
        cancelled = _job['cancel']
    if cancelled:  # a live reply arrived while the session was being opened (review rv_face2 A3)
        _stop_session(port, sid)
        with _lock:
            _job.update(sid=None, port=None, cancel=False)
        return None
    try:
        with requests.get(f'http://127.0.0.1:{port}/live.mp4', params={'sid': sid}, stream=True,
                          timeout=(5, IDLE_READ_TIMEOUT_S)) as resp:
            looked = time.monotonic()
            for _chunk in resp.iter_content(65536):
                now = time.monotonic()
                if now - looked < TURN_POLL_S:
                    continue
                looked = now
                if _turn_in_flight():  # any turn, voice or video, takes the GPU back at once
                    cancel_idle('a turn started')
                    break
    finally:
        with _lock:
            _job.update(sid=None, port=None)
    kept = out / f'{sid}.mp4'
    if not kept.is_file():  # stopped (a live reply) or incomplete: nothing kept, asked again later
        logger.info(f"{TOOL} idle: avatar {avatar_id}'s "
                    f"{'warm session' if warm_seconds else 'idle clip'} was not completed"
                    f"{'' if warm_seconds else '; asked again later'}")
        return None
    if warm_seconds:  # made only to warm the engine: never served, never kept
        try:
            kept.unlink()
        except OSError as e:
            logger.debug(f"{TOOL} idle: warm session {sid}'s file not removed: {e}")
        logger.info(f"{TOOL} idle: engine warmed for avatar {avatar_id} (session {sid})")
        return kept
    os.replace(kept, final)
    for old in out.glob(f"{IDLE_PREFIX}{int(avatar_id)}_*.mp4"):
        if old != final:
            try:
                old.unlink()
            except OSError:
                pass
    logger.info(f"{TOOL} idle: avatar {avatar_id}'s idle clip kept: {final.name}")
    return final


def warm_session(avatar_id, port) -> bool:
    """One short silent session for this avatar, right after the face model came up for it
    (avatarforcing_tool._start_setup), so the person's next video reply starts warm: the portrait's
    crop is in the sidecar's REF_CACHE, it is the engine's current reference, and the per-session
    work has run once.  Build #8, 28 Nov: the first reply after a load reached its first frame in
    1.0-2.7 s, later ones in 0.4-0.5 s.

    Through make_clip's own session path (review of the warm design, 28 Nov): no .owner record, so
    no audit row; nothing kept; the model is not stamped as used (it is not kept loaded on this
    session's account); the live face's back-off is never set; and cancel_idle stops it, so a reply
    never waits behind it.  Skipped while a turn is being served or a reply's face is open.  A person
    at the computer does not skip it (they just asked for this face), nor does the idle clip's quiet
    period.  True when the session ran to its end."""
    if _turn_in_flight():
        logger.info(f"{TOOL} idle: no warm session for avatar {avatar_id}: a turn is being served")
        return False
    try:
        from .avatarforcing_tool import live_face_open
        if live_face_open():
            logger.info(f"{TOOL} idle: no warm session for avatar {avatar_id}: a reply's face is open")
            return False
    except Exception:
        return False
    try:
        return make_clip(avatar_id, port, warm_seconds=WARM_SECONDS) is not None
    except Exception as e:
        logger.info(f"{TOOL} idle: warm session for avatar {avatar_id} failed: {type(e).__name__}: {e}")
        return False


def with_idle_face(fillers, prompt_id, avatar_id, phone_base: str = '') -> list:
    """An agent's fillers for the agent list, with its idle face first when one is kept (review
    hartos-17 I5: the local clip, by the same model as the live face, before central's MakeItTalk
    one).  An agent with a face but no clip for it yet asks for one (want_clip): this is the ONE place
    the "no clip" predicate is checked, at creation, sync and boot alike.  Never raises."""
    fillers = list(fillers or [])
    try:
        from core.teacher_avatar import avatar_id_from
        aid = avatar_id_from(avatar_id)
        if aid is None or prompt_id in (None, ''):
            return fillers
        want_clip(aid)  # the worker checks this portrait's clip (a new portrait gets a new one)
        if stored_clip(aid) is None:
            return fillers
        from .avatarforcing_tool import face_links
        page_link, phone_link = face_links(IDLE_PATH.format(prompt_id=prompt_id),
                                           phone_base=phone_base)
        filler = {'type': 'idle', 'page_link': page_link, 'source': 'local_face'}
        if phone_link.startswith(('http://', 'https://')):
            # A phone's link only when this node has an address to give it: a relative one would be
            # read against whatever host the phone listed through (review hartos-17).
            filler['video_link'] = phone_link
        return [filler] + fillers
    except Exception as e:
        logger.debug(f"{TOOL} idle: no idle filler for agent {prompt_id}: {e}")
        return fillers
