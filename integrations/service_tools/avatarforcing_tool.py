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
import threading
import time
from collections import OrderedDict
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


def stream_owner(sid):
    """The user a stream belongs to, or None (unknown or forgotten)."""
    with _lock:
        return _owners.get(sid)


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


def _can_run_here(rtm) -> bool:
    """An NVIDIA GPU with room for the model: the engine is CUDA-only (a Mac's Metal, which
    the VRAM advisor also calls 'gpu', cannot run it)."""
    try:
        if not (rtm.vram.detect_gpu() or {}).get('cuda_available'):
            return False
        return rtm.vram.suggest_offload_mode(TOOL) == 'gpu'
    except Exception as e:
        logger.debug(f"{TOOL}: GPU check failed: {e}")
        return False


def _start_setup(rtm) -> None:
    """Set the model up in the background for the video turns after this one: only where it
    can run (a GPU with room for it), and not again within SETUP_RETRY_S of the last attempt
    (SETUP_RETRY_AFTER_FAILURE_S after one that failed).  setup_tool_async joins a setup
    already running."""
    now = time.time()
    with _lock:
        wait = SETUP_RETRY_AFTER_FAILURE_S if _setup['failed'] else SETUP_RETRY_S
        running = _setup['future'] is not None and not _setup['future'].done()
        if running or now - _setup['started_at'] < wait:
            return
        _setup['started_at'] = now
    if not _can_run_here(rtm):
        logger.info(f"{TOOL}: not set up: no CUDA GPU with room for it here now; video turns are heard")
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

    logger.info(f"{TOOL}: not running; setting it up for the next video turns")
    future = rtm.setup_tool_async(TOOL)
    with _lock:
        _setup['future'] = future
    future.add_done_callback(_done)


def live_session(audio_path, avatar_id, media_mode) -> Optional[dict]:
    """Start the live stream of one spoken reply: {'sid', 'frames', 'fps'}, or None.

    None, and the reply is heard as audio, unless all of these hold: a video turn
    (``media_mode == 'video'``), an audio file, an avatar whose portrait can be had, and the
    model running and loaded.  An avatar with a portrait on a node where the model is not
    running starts its setup (_start_setup), so the video turns after it are seen; until then,
    and whenever the lifecycle manager has evicted it for other work, a video turn is heard.
    """
    if media_mode != 'video' or not audio_path or avatar_id is None:
        return None
    from .runtime_manager import runtime_tool_manager as rtm
    if not rtm.tool_enabled(TOOL):
        return None
    port = rtm.get_tool_port(TOOL)
    if not port and not _can_run_here(rtm):
        return None  # nothing to set up here; no portrait lookup either
    face = _portrait(avatar_id)
    if not face:
        logger.info(f"{TOOL}: avatar {avatar_id} has no portrait here; the reply is heard")
        return None
    if not port:
        _start_setup(rtm)
        return None
    from core.http_pool import pooled_post_once
    try:
        # file: URLs -- the sidecar runs on this machine and reads them itself.
        r = pooled_post_once(f'http://127.0.0.1:{port}/session', timeout=SESSION_POST_TIMEOUT_S, json={
            'audio_url': Path(audio_path).resolve().as_uri(),
            'image_url': Path(face).resolve().as_uri()})
        if r.status_code == 503:  # still loading the model
            return None
        s = r.json()
        if r.status_code != 200 or not s.get('sid'):
            logger.warning(f"{TOOL}: session refused ({r.status_code}): {s.get('error')}")
            return None
    except Exception as e:
        with _lock:
            _backoff['until'] = time.time() + SIDECAR_BACKOFF_S
        logger.warning(f"{TOOL}: no session for this reply ({type(e).__name__}: {e}); "
                       f"video turns are heard for {SIDECAR_BACKOFF_S:.0f} s")
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
    if media_mode != 'video' or not audio_path or avatar_id is None:
        return False
    if time.time() < _backoff['until']:  # the sidecar did not answer a moment ago
        return False
    future = _worker.submit(live_session, audio_path, avatar_id, media_mode)
    try:
        session = future.result(timeout=SESSION_BUDGET_S)
    except FutureTimeout:
        future.cancel()  # not started yet: never opens a session nobody will watch
        logger.info(f"{TOOL}: no face within {SESSION_BUDGET_S:.0f} s; the reply is heard")
        return False
    except Exception as e:
        logger.warning(f"{TOOL}: live avatar skipped: {type(e).__name__}: {e}")
        return False
    if not session:
        return False
    with _lock:
        _owners[session['sid']] = None if user_id is None else str(user_id)
        while len(_owners) > OWNERS_KEPT:
            _owners.popitem(last=False)
    path = STREAM_PATH.format(sid=session['sid'])
    payload['generated_video_url'] = f"{(page_base or '').rstrip('/')}{path}"
    payload['video_seconds'] = round(session['frames'] / max(1, session['fps']), 2)
    link = payload.get('video_link')
    if isinstance(link, dict):
        link['video'] = f"{(phone_base or '').rstrip('/')}{path}"
        link['stream'] = 'live'
    return True
