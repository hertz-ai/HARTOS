"""core.media_bin -- THE ffmpeg locator and runner.

WHY THIS EXISTS
---------------
Reel Studio decodes, analyses, renders and checks phone video with ffmpeg,
and HARTOS had no single answer to "which ffmpeg?".  The few existing call
sites shell a bare ``'ffmpeg'`` and degrade when it is not on PATH, which on
a Windows desktop it never is.  This module is the one place that answers
the question and the one way to run the binary.

WHERE THE BINARY COMES FROM (first hit wins)
  1. ``IMAGEIO_FFMPEG_EXE`` -- an explicit override, used when it names an
     existing file (or a name PATH resolves).  A value that names nothing is
     reported and skipped rather than trusted.
  2. the ``imageio-ffmpeg`` package, which HARTOS already pins
     (requirements.txt) and Nunba already embeds; it ships a static binary
     per platform.
  3. ``shutil.which('ffmpeg')`` over ``subprocess_safe.system_search_path()``
     (a NixOS service does not inherit a login shell's PATH).
A found binary is kept for the process; a miss is retried on the next call.
There is no ffprobe anywhere in this stack; callers parse ``ffmpeg -i``.

HOW IT RUNS
-----------
* ``run_ffmpeg`` goes through ``core.subprocess_safe.run_bounded``: argv
  only, a mandatory bound, process-tree kill on timeout, no console window.
* ``ffmpeg_stream`` is for raw frames or PCM on stdout.  ``run_bounded``
  buffers the whole output, so the stream drives Popen itself: a watchdog
  Timer kills ffmpeg at the bound, a daemon thread keeps the last 16 KiB of
  stderr (so a full pipe can never stall ffmpeg), and closing the generator
  early kills the process.  ffmpeg runs no children, so killing the one
  process ends it; the pipes are closed only by their own readers, never
  from the timeout path (subprocess_safe D36).

core never imports integrations; everything here is generic ffmpeg.
"""
from __future__ import annotations

import dataclasses
import functools
import logging
import math
import os
import re
import shutil
import subprocess
import threading
from dataclasses import dataclass
from typing import Iterator, List, Optional, Sequence

from core import subprocess_safe

logger = logging.getLogger(__name__)

#: The override imageio-ffmpeg itself honours; one name for both.
ENV_FFMPEG_EXE = 'IMAGEIO_FFMPEG_EXE'

#: Bound on imageio's own lookup, which validates candidates by running
#: ``ffmpeg -version`` without a timeout.
_LOCATE_WAIT_S = 30.0
#: Bound on each capability listing (-encoders / -filters / -version).
_CAPS_TIMEOUT_S = 30.0
#: Bound on the one NVENC test encode.
_NVENC_PROBE_TIMEOUT_S = 30.0
#: 0.2 s of black through h264_nvenc: proves the driver, the GPU and a free
#: encoder session, which a listed encoder alone does not.
_NVENC_PROBE_ARGS = ('-loglevel', 'error', '-f', 'lavfi',
                     '-i', 'color=c=black:s=256x256:r=30', '-t', '0.2',
                     '-c:v', 'h264_nvenc', '-f', 'null', '-')
#: Added to every command unless the caller already has it: no banner, and
#: never read our stdin (ffmpeg reads 'q' from it otherwise).
_ALWAYS = ('-hide_banner', '-nostdin')
#: How much of a streaming ffmpeg's stderr is kept for the error message.
_STDERR_TAIL_BYTES = 16 * 1024
#: After stdout ends, how long ffmpeg gets to exit before it is killed.
_EXIT_GRACE_S = 10.0
#: How long the stderr reader gets to finish once ffmpeg has exited.
_TAIL_JOIN_S = 2.0


class MediaToolMissing(RuntimeError):
    """No usable ffmpeg binary on this machine."""


class FFmpegFailed(RuntimeError):
    """ffmpeg exited non-zero, or was killed at its bound."""

    def __init__(self, message: str, *, returncode: Optional[int] = None,
                 stderr_tail: str = '', timed_out: bool = False) -> None:
        super().__init__(message)
        self.returncode = returncode
        self.stderr_tail = stderr_tail
        self.timed_out = timed_out


@dataclass(frozen=True)
class FFmpegCaps:
    """What the located binary can do."""
    version: str
    encoders: frozenset
    filters: frozenset
    nvenc_ok: bool = False


# ── locating ──────────────────────────────────────────────────────────────

def _resolve(candidate: Optional[str]) -> Optional[str]:
    """An existing file path, or a name PATH resolves; else None."""
    if not candidate:
        return None
    if os.path.isfile(candidate):
        return os.path.abspath(candidate)
    return shutil.which(candidate, path=subprocess_safe.system_search_path())


def _imageio_candidate() -> Optional[str]:
    """What imageio-ffmpeg would use (env override first, then its bundle)."""
    try:
        import imageio_ffmpeg
    except ImportError:
        logger.debug('imageio_ffmpeg is not installed; looking on PATH')
        return None
    finished, value, error = subprocess_safe.call_bounded(
        imageio_ffmpeg.get_ffmpeg_exe, _LOCATE_WAIT_S, name='hart-ffmpeg-locate')
    if not finished:
        logger.warning('imageio_ffmpeg.get_ffmpeg_exe() did not answer within %.1f s; '
                       'looking on PATH instead', _LOCATE_WAIT_S)
        return None
    if error is not None:
        logger.info('imageio_ffmpeg found no ffmpeg (%s); looking on PATH', error)
        return None
    return value


#: The binary once found.  A miss is never stored: the lookup that missed may
#: only have run out of time (antivirus scanning an 88 MB binary on its first
#: run), and a remembered miss would disable reels until the next restart.
_located: Optional[str] = None


def get_ffmpeg() -> Optional[str]:
    """Absolute path of the ffmpeg binary to use, or None when there is none.

    A found binary is kept for the process (``get_ffmpeg.cache_clear()``
    forgets it); a miss is looked up again on the next call.
    """
    global _located
    if _located is None:
        _located = _locate_ffmpeg()
    return _located


def _forget_ffmpeg() -> None:
    global _located
    _located = None


get_ffmpeg.cache_clear = _forget_ffmpeg


def _locate_ffmpeg() -> Optional[str]:
    explicit = os.environ.get(ENV_FFMPEG_EXE, '').strip()
    if explicit:
        found = _resolve(explicit)
        if found:
            return found
        logger.warning('%s=%r names no existing file; ignoring it', ENV_FFMPEG_EXE, explicit)
    bundled = _imageio_candidate()
    if bundled and bundled != explicit:
        found = _resolve(bundled)
        if found:
            return found
    return shutil.which('ffmpeg', path=subprocess_safe.system_search_path())


def require_ffmpeg() -> str:
    """The binary from get_ffmpeg(), or MediaToolMissing saying how to get one."""
    exe = get_ffmpeg()
    if not exe:
        raise MediaToolMissing(
            'ffmpeg was not found: set %s to the binary, install the imageio-ffmpeg '
            'package (pinned in requirements.txt), or put ffmpeg on PATH' % ENV_FFMPEG_EXE)
    return exe


# ── capabilities ──────────────────────────────────────────────────────────

# ` V....D libx264   description`: a type letter, five flag columns, the name.
_ENCODER_ROW = re.compile(r'^\s*[VAS][.A-Z]{5}\s+([A-Za-z0-9_][\w.-]*)\s')
# ` T.C drawtext   V->V   description`: three flag columns, name, in->out.
_FILTER_ROW = re.compile(r'^\s*[T.][S.][C.]\s+([A-Za-z0-9_][\w.-]*)\s+\S*->\S*')
_VERSION = re.compile(r'ffmpeg version (\S+)')


def parse_caps(encoders_text: str, filters_text: str, version_text: str, *,
               nvenc_ok: bool = False) -> FFmpegCaps:
    """Pure: the ``-encoders``, ``-filters`` and ``-version`` listings -> FFmpegCaps.

    Encoder rows are read only after the listing's ``------`` separator, so
    the legend above it (`` V..... = Video``) is never taken for a name.
    """
    encoders = set()
    seen_separator = False
    for line in (encoders_text or '').splitlines():
        if not seen_separator:
            seen_separator = line.strip().startswith('---')
            continue
        m = _ENCODER_ROW.match(line)
        if m:
            encoders.add(m.group(1))
    filters = set()
    for line in (filters_text or '').splitlines():
        m = _FILTER_ROW.match(line)
        if m:
            filters.add(m.group(1))
    m = _VERSION.search(version_text or '')
    return FFmpegCaps(version=m.group(1) if m else '', encoders=frozenset(encoders),
                      filters=frozenset(filters), nvenc_ok=bool(nvenc_ok))


def _listing(flag: str) -> str:
    result = run_ffmpeg([flag], timeout_s=_CAPS_TIMEOUT_S)
    if result.timed_out or result.returncode != 0:
        raise FFmpegFailed('ffmpeg %s failed' % flag, returncode=result.returncode,
                           stderr_tail=result.stderr[-_STDERR_TAIL_BYTES:],
                           timed_out=result.timed_out)
    return result.stdout


def _nvenc_works() -> bool:
    result = run_ffmpeg(list(_NVENC_PROBE_ARGS), timeout_s=_NVENC_PROBE_TIMEOUT_S)
    ok = (not result.timed_out) and result.returncode == 0
    if not ok:
        logger.info('h264_nvenc is listed but a 0.2 s test encode failed '
                    '(rc=%s, timed_out=%s): %s', result.returncode, result.timed_out,
                    result.stderr.strip()[-300:])
    return ok


@functools.lru_cache(maxsize=1)
def ffmpeg_caps() -> FFmpegCaps:
    """Encoders, filters and version of the located binary, measured once.

    ``nvenc_ok`` comes from one 0.2 s test encode, run only when h264_nvenc
    is listed.  A failed listing raises FFmpegFailed and is not cached.
    """
    caps = parse_caps(_listing('-encoders'), _listing('-filters'), _listing('-version'))
    if 'h264_nvenc' in caps.encoders:
        caps = dataclasses.replace(caps, nvenc_ok=_nvenc_works())
    return caps


# ── running ───────────────────────────────────────────────────────────────

def _bound(timeout_s) -> float:
    seconds = float(timeout_s)
    if not (math.isfinite(seconds) and seconds > 0):
        raise ValueError('ffmpeg needs a finite timeout_s > 0, got %r' % (timeout_s,))
    return seconds


def _command(args: Sequence[str]) -> List[str]:
    argv = [str(a) for a in args]
    return [require_ffmpeg()] + [flag for flag in _ALWAYS if flag not in argv] + argv


def run_ffmpeg(args: Sequence[str], *, timeout_s: float) -> subprocess_safe.BoundedResult:
    """Run the located ffmpeg with ``args`` (no binary, no shell), bounded.

    Returns the BoundedResult; ``timed_out`` is True when the bound killed
    it.  stdout/stderr are decoded as UTF-8 with replacement, because
    ffmpeg echoes container metadata (Tamil titles, device names) verbatim.
    """
    seconds = _bound(timeout_s)
    cmd = _command(args)
    try:
        return subprocess_safe.run_bounded(cmd, timeout=seconds,
                                           encoding='utf-8', errors='replace')
    except FileNotFoundError as e:
        raise MediaToolMissing('ffmpeg at %r could not be started: %s' % (cmd[0], e)) from e


def ffmpeg_stream(args: Sequence[str], *, frame_bytes: int,
                  timeout_s: float) -> Iterator[bytes]:
    """Iterate ffmpeg's stdout in chunks of exactly ``frame_bytes``.

    ``args`` must send the output to ``pipe:1`` (rawvideo, f32le, ...).  The
    argument checks and the binary lookup happen now; ffmpeg starts on the
    first ``next()``.  A trailing partial chunk is dropped.  Raises
    FFmpegFailed (``timed_out=True`` at the bound) when ffmpeg fails; closing
    the iterator early stops ffmpeg.
    """
    size = int(frame_bytes)
    if size <= 0:
        raise ValueError('frame_bytes must be > 0, got %r' % (frame_bytes,))
    seconds = _bound(timeout_s)
    return _stream_chunks(_command(args), size, seconds)


def _keep_stderr_tail(pipe) -> tuple:
    """Drain ``pipe`` on a daemon thread, keeping its last bytes.

    Returns (thread, buffer).  Read the buffer only after the thread ends.
    """
    tail = bytearray()

    def _drain():
        try:
            while True:
                chunk = pipe.read1(4096)
                if not chunk:
                    break
                tail.extend(chunk)
                if len(tail) > _STDERR_TAIL_BYTES:
                    del tail[:-_STDERR_TAIL_BYTES]
        except (OSError, ValueError) as e:
            logger.debug('ffmpeg stderr reader stopped: %s', e)
        finally:
            pipe.close()

    reader = threading.Thread(target=_drain, name='hart-ffmpeg-stderr', daemon=True)
    reader.start()
    return reader, tail


def _kill(proc, why: str) -> None:
    """Kill ffmpeg and reap it.  Never closes its pipes (their readers do)."""
    if proc.poll() is not None:
        return
    logger.warning('stopping ffmpeg pid %s: %s', proc.pid, why)
    try:
        proc.kill()
    except OSError as e:
        logger.debug('ffmpeg pid %s was already gone: %s', proc.pid, e)
    try:
        proc.wait(timeout=_TAIL_JOIN_S)
    except subprocess.TimeoutExpired:
        logger.warning('ffmpeg pid %s did not exit after kill; the OS will reap it', proc.pid)


def _stream_chunks(cmd: List[str], size: int, seconds: float) -> Iterator[bytes]:
    try:
        proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, **subprocess_safe.no_window_kwargs())
    except FileNotFoundError as e:
        raise MediaToolMissing('ffmpeg at %r could not be started: %s' % (cmd[0], e)) from e
    reader, tail = _keep_stderr_tail(proc.stderr)
    expired = threading.Event()

    def _at_the_bound():
        expired.set()
        _kill(proc, 'exceeded its %.1f s bound' % seconds)

    watchdog = threading.Timer(seconds, _at_the_bound)
    watchdog.daemon = True
    watchdog.start()
    returncode = None
    try:
        while True:
            chunk = proc.stdout.read(size)
            if len(chunk) < size:
                if chunk:
                    logger.debug('ffmpeg output ended mid-chunk (%d of %d bytes); dropped',
                                  len(chunk), size)
                break
            yield chunk
        try:
            returncode = proc.wait(timeout=_EXIT_GRACE_S)
        except subprocess.TimeoutExpired:
            _kill(proc, 'still running %.1f s after its output ended' % _EXIT_GRACE_S)
            returncode = proc.returncode
    finally:
        watchdog.cancel()
        _kill(proc, 'its reader stopped early')
        proc.stdout.close()
    reader.join(_TAIL_JOIN_S)
    text = bytes(tail).decode('utf-8', 'replace')
    if expired.is_set():
        raise FFmpegFailed('ffmpeg exceeded its %.1f s bound' % seconds,
                           returncode=returncode, stderr_tail=text, timed_out=True)
    if returncode != 0:
        raise FFmpegFailed('ffmpeg exited with %s: %s' % (returncode, text.strip()[-500:]),
                           returncode=returncode, stderr_tail=text)


__all__ = ['ENV_FFMPEG_EXE', 'MediaToolMissing', 'FFmpegFailed', 'FFmpegCaps',
           'get_ffmpeg', 'require_ffmpeg', 'parse_caps', 'ffmpeg_caps',
           'run_ffmpeg', 'ffmpeg_stream']
