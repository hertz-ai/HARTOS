"""AvatarForcing -- an avatar's face speaking a reply, as a live stream in the reply's bundle.

Service: integrations/service_tools/servers/avatarforcing_server.py, a RuntimeToolManager
sidecar (port read with get_tool_port).  POST /session takes the reply's TTS audio (already in
the avatar's cloned voice) and the avatar's portrait, as files on this machine, and answers at
once with the session's stream: ONE fragmented MP4 (H.264 + AAC) that plays while its frames
are still being made -- through Media Source Extensions on the page, as a plain URL in a
phone's player.  About 1 GB of VRAM; faster than real time on a free RTX 3070 Laptop GPU.

A spoken reply reaches every client in one bundle (the TTS publish in
hart_intelligence_entry._tts_synthesize_and_publish, and Nunba's fallback publisher).
add_live_avatar puts the stream into that same bundle: ``generated_video_url`` +
``video_seconds`` for the page (same origin), and in the phone's ``video_link``
``video`` (absolute, on the address this node advertises) with ``stream: 'live'``.  The
phone's link keeps ``is_cartoon: True`` and ``aud_url``: the Android app in the field plays
``video`` only for ``is_cartoon: False`` and its video player sends no device token (its audio
player does), so flipping the flag would leave a phone silent on a video turn.  An app that
knows ``stream: 'live'`` plays the face with its token and falls back to ``aud_url``.  A reply it cannot give a face within
SESSION_BUDGET_S -- not a video turn, no portrait, the model not running or busy -- is
published with its audio alone, never later than that.
"""

import logging
import os
import threading
import time
from collections import OrderedDict, deque
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

TOOL = 'avatarforcing'

#: Where this node serves a session's stream (Nunba main.py relays it to the sidecar, which
#: listens on loopback only): the page plays it same-origin, a phone at the advertised base.
STREAM_PATH = '/avatar/live/{sid}.mp4'

#: The longest a reply's audio publish waits for its face.  The portrait lookup, a first
#: portrait download and the session all run on one worker thread; past the budget the reply
#: goes out with its audio alone and the worker finishes its lookup for the next turn (a
#: session it opens late is never watched, so it never generates).
SESSION_BUDGET_S = 2.0
#: The sidecar answers POST /session from memory (the face crop runs when the stream starts).
SESSION_POST_TIMEOUT_S = 2.0
#: A portrait found is reused for this long, so a conversation looks it up once; an avatar
#: with none (or a lookup that failed) is asked again only after PORTRAIT_MISS_TTL_S.
PORTRAIT_TTL_S = 600.0
PORTRAIT_MISS_TTL_S = 60.0

#: Between two setups started by video turns: each clones, builds a venv, starts a sidecar and
#: loads ~1 GB, and a node where the model crashes or cannot run would otherwise repeat that
#: on every reply.  Longer after a failure.
SETUP_RETRY_S = 120.0
SETUP_RETRY_AFTER_FAILURE_S = 600.0
#: A sidecar that is up but does not answer POST /session (hung, or busy past its timeout)
#: is not asked again for this long: every video turn would otherwise pay the whole post
#: timeout before its audio goes out (review rv_9a3a).  Its turns are heard meanwhile.
SIDECAR_BACKOFF_S = 30.0

_worker = ThreadPoolExecutor(max_workers=1, thread_name_prefix='avatar-session')
_lock = threading.Lock()
_portraits = {}  # avatar id -> (local path, found at)
_setup = {'started_at': 0.0, 'failed': False, 'future': None}
_backoff = {'until': 0.0}  # the sidecar did not answer: not asked again before this time
#: sid -> the user whose turn it is: only that user (or this machine) may watch or stop it.
_owners = OrderedDict()
OWNERS_KEPT = 256
#: Turns whose audio already went out at SESSION_BUDGET_S (logged "no face within"): the
#: worker's own reason for one of them, when it comes, is logged at DEBUG, so a heard turn
#: says why once at INFO (review rv_g6).
_late = deque(maxlen=64)


def _say(request_id, reason: str) -> None:
    """The one line saying why a turn is heard: INFO, or DEBUG for a turn already logged as
    past the budget."""
    log = logger.debug if (request_id is not None and request_id in _late) else logger.info
    log(f"{TOOL}: request {request_id} heard, not seen: {reason}")


def face_links(path: str, *, page_base: str = '', phone_base: str = '') -> tuple:
    """(the page's URL, a phone's URL) of a face path this node serves: the ONE builder, used by the
    live bundle (add_live_avatar) and the idle filler (avatarforcing_idle).  The page's is same-origin
    when ``page_base`` is empty (its loopback origin is what the LAN gate trusts); a phone's is
    absolute on the address this node advertises."""
    return (f"{(page_base or '').rstrip('/')}{path}", f"{(phone_base or '').rstrip('/')}{path}")


def face_output_dir() -> Path:
    """The face sidecar's output folder, where a finished face is kept (stream_server.save_stream):
    <models root>/avatarforcing/output, the model folder RuntimeToolManager gives the sidecar."""
    from .model_storage import get_base_dir
    return get_base_dir() / TOOL / 'output'


def _owner_file(sid) -> Optional[Path]:
    """Where a stream's owner is kept beside its face, or None for a sid that is not one."""
    import re
    if not isinstance(sid, str) or not re.fullmatch(r'[0-9a-f]{12}', sid):
        return None
    return face_output_dir() / f'{sid}.owner'


def _write_owner_record(sid, record: dict) -> bool:
    """Write ``<sid>.owner`` (JSON).  The one writer of the file; True when it is on disk."""
    path = _owner_file(sid)
    if path is None:
        return False
    try:
        import json
        path.parent.mkdir(parents=True, exist_ok=True)
        part = path.with_name(path.name + '.part')
        part.write_text(json.dumps(record), encoding='utf-8')
        os.replace(part, path)
        return True
    except OSError as e:
        logger.info(f"{TOOL}: owner record of {sid} not kept on disk ({e})")
        return False


_MALFORMED = object()  # a record that names nobody: removed by settle_faces, never blocks the rest


def _read_owner_record(path):
    """A ``.owner`` record as ``{user, request_id, started_at, audited}``; None when it cannot be read
    now (a disk error: tried again later); _MALFORMED when it never will be.  The first format (the
    user id as plain text, before the record carried the audit fields) still reads: its request id
    is unknown and its start is the file's time."""
    try:
        text = Path(path).read_text(encoding='utf-8')
        mtime = os.path.getmtime(path)
    except OSError:
        return None
    import json
    try:
        rec = json.loads(text)
    except ValueError:
        user = text.strip()
        if not user or any(c.isspace() for c in user) or len(user) > 128:
            return _MALFORMED
        return {'user': user, 'request_id': None, 'started_at': mtime, 'audited': False}
    if isinstance(rec, (int, str)) and not isinstance(rec, bool):
        # the first format: a numeric user id parses as a JSON number (review rv_face2 D1)
        user = str(rec).strip()
        if not user or any(c.isspace() for c in user) or len(user) > 128:
            return _MALFORMED
        return {'user': user, 'request_id': None, 'started_at': mtime, 'audited': False}
    if not isinstance(rec, dict):
        return _MALFORMED
    try:
        started = float(rec.get('started_at') or mtime)
    except (TypeError, ValueError):
        started = mtime
    user = rec.get('user')
    return {'user': None if user in (None, '') else str(user),
            'request_id': rec.get('request_id'), 'agent_id': rec.get('agent_id'),
            'started_at': started, 'audited': rec.get('audited') is True}


def _remember_owner(sid, user, request_id=None) -> None:
    """The ONE writer of a stream's owner (review hartos-17 MUST 2): the in-memory map, which is a
    read-through cache, and ``<sid>.owner`` beside the face, written at the session's start, so a kept
    face is still its owner's after a restart.  The record is also the face's audit source (MUST 4):
    ``{user, request_id, started_at, audited}``, settled by settle_faces.  A disk that refuses loses
    only the restart case and the audit line, logged."""
    owner = None if user is None else str(user)
    with _lock:
        _owners[sid] = owner
        while len(_owners) > OWNERS_KEPT:
            _owners.popitem(last=False)
    try:  # the agent the turn was bound to, kept on disk so a restart keeps it (rv_face2 D3)
        from security.audit_trail import bound
        agent_id = bound(request_id).get('agent_id')
    except Exception:
        agent_id = None
    _write_owner_record(sid, {'user': owner, 'request_id': None if request_id is None else str(request_id),
                              'agent_id': agent_id, 'started_at': time.time(), 'audited': False})


def _being_made(path, rec, now) -> bool:
    """A reply's face session still being made, by its owner record ``path`` / ``rec``: no finished
    ``<sid>.mp4``, and a ``.part`` on disk or a start younger than STALE_PART_S: the AUDIT's
    conservative rule (settle_faces leaves such a session for later).  Not "being made now":
    live_face_open asks the sidecar."""
    from core.spoken_media import STALE_PART_S
    sid = path.stem
    if path.with_name(f'{sid}.mp4').is_file():
        return False
    return path.with_name(f'{sid}.mp4.part').exists() or now - rec['started_at'] <= STALE_PART_S


#: A reply's face whose session is open but whose viewer has not started reading it yet: the sidecar
#: generates only for a reader, so /health says not running for these few seconds.  An owner record
#: this young counts as a face being made (review hartos-17); the audit's own window (STALE_PART_S,
#: an hour) is far too long for "now" and stays settle_faces' alone.
FACE_START_GRACE_S = 30.0


def live_face_open() -> bool:
    """True while a talking face is being made on this node, so an idle clip waits for it
    (avatarforcing_idle; review hartos-17: never compete with a reply's face).  Asked before the
    idle job opens its own session, never during it.

    The sidecar's own answer first: GET /health ``running`` (it makes one stream at a time).  Then
    the gap before a reply's viewer starts reading: an owner record younger than FACE_START_GRACE_S
    with no finished face.  A sidecar that is down or does not answer: no face is being made."""
    port = sidecar_port()
    if not port:
        return False
    try:
        from core.http_pool import pooled_get
        if (pooled_get(f'http://127.0.0.1:{port}/health', timeout=2).json() or {}).get('running'):
            return True
    except Exception:
        return False
    now = time.time()
    try:
        owners = list(face_output_dir().glob('*.owner'))
    except OSError:
        return False
    for path in owners:
        try:
            rec = _read_owner_record(path)
            if (isinstance(rec, dict) and not rec.get('audited')
                    and now - rec['started_at'] <= FACE_START_GRACE_S
                    and not path.with_name(f'{path.stem}.mp4').is_file()):
                return True
        except Exception:
            continue
    return False


def settle_faces() -> int:
    """Put every finished face session on the audit trail once (review hartos-17 MUST 4).  Called by
    the lifecycle's disk phase BEFORE it prunes, so no face is pruned unaudited.  For each ``.owner``
    not yet audited: a ``<sid>.mp4`` (renamed into place only when complete) is recorded 'complete'
    with its hash; no face and no ``.part`` after STALE_PART_S is recorded 'stopped' (the person saw
    its frames live), with no hash.  Then the record is marked audited by its one writer.  The number
    recorded; never raises."""
    try:
        from security.audit_trail import record, sha256_file
    except Exception as e:
        logger.debug(f"{TOOL}: no audit trail here ({e}); faces not settled")
        return 0
    done = 0
    try:
        owners = list(face_output_dir().glob('*.owner'))
    except OSError:
        return 0
    now = time.time()
    for path in owners:
        try:  # one record never blocks the rest (review hartos-17)
            rec = _read_owner_record(path)
            if rec is _MALFORMED or _owner_file(path.stem) is None:
                # still on the trail, with no owner, before it goes (review rv_face2 D2)
                face = path.with_name(f'{path.stem}.mp4')
                sha, size = sha256_file(face) if face.is_file() else (None, None)
                record('video_out', direction='out', ref=str(face) if face.is_file() else None,
                       sha256=sha, size=size, mime='video/mp4', model=TOOL,
                       outcome='unknown_owner', sid=path.stem)
                logger.warning(f"{TOOL}: owner record {path.name} names nobody; recorded and removed")
                path.unlink()
                continue
            if rec is None or rec['audited']:
                continue
            sid = path.stem
            face = path.with_name(f'{sid}.mp4')
            if face.is_file():
                outcome, ref = 'complete', str(face)
            elif _being_made(path, rec, now):
                continue  # still being made, or stopped too recently to tell
            else:
                outcome, ref = 'stopped', None
            # hashed HERE, before it is marked audited: phase 6 prunes right after this, and the
            # writer's own later read could find the file gone (review rv_face2 D4)
            sha, size = sha256_file(ref) if ref else (None, None)
            record('video_out', direction='out', ref=ref, sha256=sha, size=size, mime='video/mp4',
                   model=TOOL, request_id=rec['request_id'], user_id=rec['user'],
                   agent_id=rec.get('agent_id'), outcome=outcome, sid=sid)
            rec['audited'] = True  # record() queues and logs rather than raises: it is called
            if _write_owner_record(sid, rec):
                done += 1
        except Exception as e:
            logger.warning(f"{TOOL}: face {path.name} not settled: {type(e).__name__}: {e}")
    return done


def stream_owner(sid):
    """The user a stream belongs to, or None (unknown or forgotten).  The map first; after a restart
    the map is empty, so the owner kept beside the face is read (and cached)."""
    with _lock:
        if sid in _owners:
            return _owners[sid]
    path = _owner_file(sid)
    if path is None:
        return None
    rec = _read_owner_record(path)
    if rec is None or rec is _MALFORMED:
        return None
    owner = rec['user']
    with _lock:
        _owners[sid] = owner
        while len(_owners) > OWNERS_KEPT:
            _owners.popitem(last=False)
    return owner


def sidecar_port() -> Optional[int]:
    """The running sidecar's loopback port, else None."""
    from .runtime_manager import runtime_tool_manager
    return runtime_tool_manager.get_tool_port(TOOL)


def _portrait(avatar_id) -> Optional[str]:
    """The avatar's portrait as a local file (core.teacher_avatar.image_reference), cached:
    found, for PORTRAIT_TTL_S; not found, for PORTRAIT_MISS_TTL_S (so an unreachable portrait
    does not hold every video turn's audio for the whole budget)."""
    now = time.time()
    with _lock:
        hit = _portraits.get(avatar_id)
    if hit:
        path, at = hit
        if path and now - at < PORTRAIT_TTL_S and Path(path).is_file():
            return path
        if not path and now - at < PORTRAIT_MISS_TTL_S:
            return None
    from core.teacher_avatar import image_reference
    try:
        path = image_reference(avatar_id)
    except Exception as e:
        logger.debug(f"{TOOL}: portrait of avatar {avatar_id} unread: {e}")
        path = None
    with _lock:
        _portraits[avatar_id] = (path, now)
    return path


def _room_report(rtm) -> str:
    """Why _can_run_here said no, for the log: CUDA, the advisor's answer, and the free VRAM
    it decided on with that reading's age.  Only what the advisor already read (the
    vram_manager memo, the card's own nvidia-smi figure): a log line never reads the card
    -- no second nvidia-smi path (rv_g6: a plain subprocess.run flashed a console window
    under Nunba.exe) and no refresh either (rv_g6 on 8f9616fd4: a refresh from the face
    worker could hold it up to the probe's 15 s timeout, raced the memo's blank-and-reread,
    and changed what other decisions read next)."""
    try:
        cuda = bool((rtm.vram.detect_gpu() or {}).get('cuda_available'))
        mode = rtm.vram.suggest_offload_mode(TOOL, need='live')
        read = rtm.vram.get_free_vram()
    except Exception as e:
        return f"the GPU check failed ({type(e).__name__}: {e})"
    if getattr(rtm.vram, '_gpu_info', None) is None:
        free = "no reading yet"  # never a stale zero
    else:
        taken = getattr(rtm.vram, '_gpu_info_ts', 0) or 0
        age = f"read {time.monotonic() - taken:.0f} s ago" if taken else "age unknown"
        free = f"free {read} GB ({age})"
    return (f"cuda={cuda}, live mode answer {mode!r} (a live face needs gpu or a mode measured at "
            f"real time here), {free}")


#: The engine placement a sidecar reports in /stats -> the mode it is (vram_manager.MODE_LADDER).
def _mode_of(engine) -> Optional[str]:
    if not isinstance(engine, dict):
        return None
    audio_cpu = engine.get('audio_device') == 'cpu'
    ft_cpu = engine.get('ft_device') == 'cpu'
    if not audio_cpu and not ft_cpu:
        return 'gpu'
    if audio_cpu and not ft_cpu:
        return 'cpu_audio'
    if audio_cpu and ft_cpu and engine.get('cpu_int8'):
        return 'cpu_audio_ft_int8'
    return None


#: The sidecar session last measured, so one finished session is recorded once.
_measured_session = {'n': None}
#: A real-time factor is frames made per frame shown; the engine streams at 25 fps.
STREAM_FPS = 25.0


def _record_last_run(port) -> None:
    """Record the last finished session of the running sidecar (its CUDA peak and speed, against
    the mode it reports) in vram_manager's measured store, so the next mode choice is MEASURED on
    this machine.  One local GET with a short timeout; any failure records nothing."""
    try:
        from core.http_pool import pooled_get
        st = pooled_get(f'http://127.0.0.1:{port}/stats', timeout=0.5).json()
        mode = _mode_of(st.get('engine'))
        n = st.get('session')
        if not mode or n is None or n == _measured_session['n'] or st.get('running'):
            return
        _measured_session['n'] = n
        peak = (st.get('cuda_peak_mb') or 0) / 1024.0 or None
        rtf = (st.get('rate_fps') or 0) / STREAM_FPS or None
        from .runtime_manager import runtime_tool_manager as rtm
        rtm.vram.record_mode_measurement(TOOL, mode, peak_gb=peak, rtf=rtf)
    except Exception as e:
        logger.debug(f"{TOOL}: last run not measured: {e}")


def _can_run_here(rtm) -> bool:
    """An NVIDIA GPU with room for the model: the engine is CUDA-only (a Mac's Metal, which
    the VRAM advisor also calls 'gpu', cannot run it)."""
    try:
        if not (rtm.vram.detect_gpu() or {}).get('cuda_available'):
            return False
        # a mode a person can watch live: gpu, or an offload mode measured at real time here
        return rtm.vram.suggest_offload_mode(TOOL, need='live') != 'cpu_only'
    except Exception as e:
        logger.debug(f"{TOOL}: GPU check failed: {e}")
        return False


def _start_setup(rtm, request_id=None) -> None:
    """Set the model up in the background for the video turns after this one: only where it
    can run (a GPU with room for it), and not again within SETUP_RETRY_S of the last attempt
    (SETUP_RETRY_AFTER_FAILURE_S after one that failed).  setup_tool_async joins a setup
    already running.  Each way out logs why this turn is heard (``request_id`` names it)."""
    now = time.time()
    with _lock:
        wait = SETUP_RETRY_AFTER_FAILURE_S if _setup['failed'] else SETUP_RETRY_S
        running = _setup['future'] is not None and not _setup['future'].done()
        since = now - _setup['started_at']
        if running or since < wait:
            _say(request_id, "not running; "
                 + ("its setup is still running" if running else
                    f"the last setup began {since:.0f} s ago, the next may begin after "
                    f"{wait:.0f} s ({'it failed' if _setup['failed'] else 'it ended'})"))
            return
        _setup['started_at'] = now
    if not _can_run_here(rtm):
        _say(request_id, f"not set up: no CUDA GPU with room for it here now "
                         f"({_room_report(rtm)}); video turns are heard")
        return

    def _done(future):
        try:
            result = future.result()
        except Exception as e:
            result = {'error': str(e)}
        failed = not result or bool(result.get('error'))
        with _lock:
            _setup['failed'] = failed
        if failed:
            logger.warning(f"{TOOL}: setup failed; video turns are heard for "
                           f"{SETUP_RETRY_AFTER_FAILURE_S / 60:.0f} min: {(result or {}).get('error')}")
        else:
            logger.info(f"{TOOL}: set up on port {result.get('port')}; the next video turn has a face")

    _say(request_id, "not running; setting it up for the next video turns")
    future = rtm.setup_tool_async(TOOL)
    with _lock:
        _setup['future'] = future
    future.add_done_callback(_done)


def _not_a_video_turn(media_mode, audio_path, avatar_id) -> Optional[str]:
    """Why a reply is not a video turn with a face to show, or None when it is one."""
    if media_mode != 'video' or not audio_path or avatar_id is None:
        return (f"media_mode={media_mode!r}, audio={'yes' if audio_path else 'none'}, "
                f"avatar={avatar_id!r} (a face needs media_mode 'video', an audio file and an "
                f"avatar)")
    return None


def live_session(audio_path, avatar_id, media_mode, request_id=None) -> Optional[dict]:
    """Start the live stream of one spoken reply: {'sid', 'frames', 'fps'}, or None.

    None, and the reply is heard as audio, unless all of these hold: a video turn
    (``media_mode == 'video'``), an audio file, an avatar whose portrait can be had, and the
    model running and loaded.  An avatar with a portrait on a node where the model is not
    running starts its setup (_start_setup), so the video turns after it are seen; until then,
    and whenever the lifecycle manager has evicted it for other work, a video turn is heard.
    Every None logs, once, why this turn (``request_id``) is heard (review G6: a heard turn
    used to leave no trace of which of these failed).
    """
    why = _not_a_video_turn(media_mode, audio_path, avatar_id)
    if why:
        _say(request_id, why)
        return None
    from .runtime_manager import runtime_tool_manager as rtm
    if not rtm.tool_enabled(TOOL):
        _say(request_id, f"the {TOOL} row is switched off on this node")
        return None
    port = rtm.get_tool_port(TOOL)
    if not port and not _can_run_here(rtm):
        # nothing to set up here; no portrait lookup either
        _say(request_id, f"not running, and it cannot run here now ({_room_report(rtm)})")
        return None
    face = _portrait(avatar_id)
    if not face:
        _say(request_id, f"avatar {avatar_id} has no portrait here")
        return None
    if not port:
        _start_setup(rtm, request_id)
        return None
    _record_last_run(port)
    try:  # a live reply takes the GPU from an idle clip being made (review hartos-17 I2)
        from .avatarforcing_idle import cancel_idle
        cancel_idle()
    except Exception as e:
        logger.debug(f"{TOOL}: idle clip not stopped: {e}")
    from core.http_pool import pooled_post_once
    try:
        # file: URLs -- the sidecar runs on this machine and reads them itself.
        r = pooled_post_once(f'http://127.0.0.1:{port}/session', timeout=SESSION_POST_TIMEOUT_S, json={
            'audio_url': Path(audio_path).resolve().as_uri(),
            'image_url': Path(face).resolve().as_uri()})
        if r.status_code == 503:  # still loading the model
            _say(request_id, f"the model is still loading (503 on port {port})")
            return None
        s = r.json()
        if r.status_code != 200 or not s.get('sid'):
            logger.warning(f"{TOOL}: request {request_id} heard, not seen: session refused "
                           f"({r.status_code}): {s.get('error')}")
            return None
    except Exception as e:
        with _lock:
            _backoff['until'] = time.time() + SIDECAR_BACKOFF_S
        logger.warning(f"{TOOL}: request {request_id} heard, not seen: no session for this reply "
                       f"({type(e).__name__}: {e}); video turns are heard for "
                       f"{SIDECAR_BACKOFF_S:.0f} s")
        return None
    try:  # a conversation it is serving keeps the model from the idle sweep
        from .model_lifecycle import get_model_lifecycle_manager
        get_model_lifecycle_manager().notify_access(TOOL)
    except Exception as e:
        logger.debug(f"{TOOL}: lifecycle access not marked: {e}")
    return {'sid': s['sid'], 'frames': int(s.get('frames') or 0), 'fps': int(s.get('fps') or 25)}


def add_live_avatar(payload: dict, *, audio_path, avatar_id, media_mode, user_id=None,
                    phone_base: str = '', page_base: str = '') -> bool:
    """Put one reply's live stream into its TTS bundle; True when it did.

    ``payload`` is the bundle as published (generated_audio_url, video_link with aud_url).
    Adds ``generated_video_url`` (``page_base`` + path: a same-origin path for the page,
    absolute when the audio url is) and ``video_seconds``; and to a phone's ``video_link``,
    when the bundle has one: ``video`` (``phone_base`` + path) and ``stream: 'live'``, leaving
    ``is_cartoon`` and ``aud_url`` as they are (see the module note: today's app keeps playing
    the voice).  A bundle without a phone link gets none.  Returns within SESSION_BUDGET_S
    whatever happens, and never raises.
    """
    rid = payload.get('request_id') if isinstance(payload, dict) else None
    why = _not_a_video_turn(media_mode, audio_path, avatar_id)
    if why:
        logger.info(f"{TOOL}: request {rid} heard, not seen: {why}")
        return False
    if time.time() < _backoff['until']:  # the sidecar did not answer a moment ago
        logger.info(f"{TOOL}: request {rid} heard, not seen: the sidecar did not answer a moment "
                    f"ago ({_backoff['until'] - time.time():.0f} s of back-off left)")
        return False
    future = _worker.submit(live_session, audio_path, avatar_id, media_mode, rid)
    try:
        session = future.result(timeout=SESSION_BUDGET_S)
    except FutureTimeout:
        future.cancel()  # not started yet: never opens a session nobody will watch
        if rid is not None:
            _late.append(rid)
        logger.info(f"{TOOL}: request {rid} heard, not seen: no face within "
                    f"{SESSION_BUDGET_S:.0f} s")
        return False
    except Exception as e:
        logger.warning(f"{TOOL}: request {rid} heard, not seen: live avatar skipped: "
                       f"{type(e).__name__}: {e}")
        return False
    if not session:
        return False
    _remember_owner(session['sid'], user_id, rid)
    path = STREAM_PATH.format(sid=session['sid'])
    page_url, phone_url = face_links(path, page_base=page_base, phone_base=phone_base)
    payload['generated_video_url'] = page_url
    payload['video_seconds'] = round(session['frames'] / max(1, session['fps']), 2)
    link = payload.get('video_link')
    if isinstance(link, dict):
        link['video'] = phone_url
        link['stream'] = 'live'
    return True
