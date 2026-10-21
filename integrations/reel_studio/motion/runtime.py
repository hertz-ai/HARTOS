"""integrations.reel_studio.motion.runtime -- Remotion on this computer: ready?, set up, render.

Remotion is set up at runtime through RuntimeToolManager, never packaged
(owner ruling A16): its row ``reel_motion`` (TOOL) names this package's
project/ (our templates and scripts), the flag that switches it on, the
consent card's capability and words, and the free disk it needs.  Whoever
sets it up meets those checks in setup_tool itself.  This module asks the
manager, never a second installer:

* ``status()``: 'off' (the flag is not set: the owner has not confirmed
  Remotion's license, amendment A14), 'not_set_up', or 'ready';
* ``ensure_ready(wait_s, progress)``: ready now, or ask the owner on the one
  consent card (capability_setup through the manager's ask_tool_consent),
  and once the answer is yes start the setup on its own thread
  (setup_tool_async) and wait for it at most ``wait_s`` seconds, telling
  ``progress`` what is happening; a setup still running after that keeps
  running, and the next reel uses it;
* ``render(items, workdir)``: ``node render.js <job>`` in the tool's folder
  (core.node_bin finds Node; core.subprocess_safe.run_bounded bounds it and
  kills the whole tree on timeout), one ProRes 4444 clip with alpha per item.

Nothing here decides where an overlay goes (layout.py) or what the reel
gets when Remotion is not ready (compose.py).
"""
from __future__ import annotations

import concurrent.futures
import json
import logging
import os
import time
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence

logger = logging.getLogger(__name__)

#: The RuntimeToolManager row (integrations/service_tools/runtime_manager.py).
TOOL = 'reel_motion'

#: ensure_ready's answers (Readiness.state).
READY = 'ready'
OFF = 'off'
ASKED = 'asked'
DECLINED = 'declined'
UNAVAILABLE = 'unavailable'
SETTING_UP = 'setting_up'
FAILED = 'failed'
NOT_SET_UP = 'not_set_up'

#: How often a waiting caller is told the setup is still going (seconds).
PROGRESS_EVERY_S = 15.0
#: Bound on one render job, and the per-item timeout Remotion is given for
#: a frame's delayRender (images loading).
RENDER_TIMEOUT_S = 240.0
FRAME_TIMEOUT_MS = 60000
#: Remotion's render concurrency (browser tabs) per clip.
CONCURRENCY = 2

SETUP_TEXT = ('Setting up animated overlays on this computer (a one-time download of a few '
              'hundred MB)...')
STILL_SETTING_UP_TEXT = 'Still setting up animated overlays...'


class MotionFailed(RuntimeError):
    """Remotion could not render the clips; the message says why."""


@dataclass(frozen=True)
class Readiness:
    """Where Remotion stands for one caller: ``state`` (READY, OFF, ASKED,
    DECLINED, UNAVAILABLE, SETTING_UP, FAILED) and, for FAILED, why."""
    state: str
    detail: str = ''

    @property
    def ready(self) -> bool:
        return self.state == READY


def _manager():
    from integrations.service_tools.runtime_manager import runtime_tool_manager
    return runtime_tool_manager


def enabled() -> bool:
    """Is Remotion switched on here (the row's flag)?"""
    return _manager().tool_enabled(TOOL)


def status() -> str:
    """'off', 'not_set_up' or 'ready'; asks nobody and sets nothing up."""
    manager = _manager()
    if not manager.tool_enabled(TOOL):
        return OFF
    return READY if manager.tool_ready(TOOL) else NOT_SET_UP


def ensure_ready(*, wait_s: float, progress: Optional[Callable[[str], None]] = None) -> Readiness:
    """Remotion ready to render, or why not (see the module note).

    ``wait_s`` bounds how long this call waits for a setup it starts or
    finds running; ``progress(text)`` is told when one starts and every
    PROGRESS_EVERY_S while it waits.  Never raises for a failed setup."""
    tell = progress or (lambda text: None)
    manager = _manager()
    if not manager.tool_enabled(TOOL):
        return Readiness(OFF)
    if manager.tool_ready(TOOL):
        return Readiness(READY)
    answer = manager.ask_tool_consent(TOOL)
    if answer != 'granted':
        return Readiness(answer if answer in (ASKED, DECLINED) else UNAVAILABLE)
    future = manager.setup_tool_async(TOOL)
    tell(SETUP_TEXT)
    deadline = time.monotonic() + max(0.0, float(wait_s))
    while True:
        left = deadline - time.monotonic()
        try:
            result = future.result(timeout=max(0.0, min(PROGRESS_EVERY_S, left)))
            break
        except concurrent.futures.TimeoutError:
            if deadline - time.monotonic() <= 0:
                return Readiness(SETTING_UP)
            tell(STILL_SETTING_UP_TEXT)
        except Exception as e:          # a bug in the setup: reported, never raised
            logger.error('reel motion: the setup raised %s: %s', type(e).__name__, e)
            return Readiness(FAILED, '%s: %s' % (type(e).__name__, e))
    if result.get('error'):
        return Readiness(FAILED, str(result['error']))
    return Readiness(READY) if manager.tool_ready(TOOL) else Readiness(FAILED, 'not ready after setup')


def tool_dir() -> str:
    """The folder Remotion's project is set up in (the models dir)."""
    return str(_manager().storage.get_tool_dir(TOOL))


def render(items: Sequence[Mapping[str, Any]], workdir: str, *,
           timeout_s: float = RENDER_TIMEOUT_S) -> List[str]:
    """Render ``items`` (each ``{'id': composition id, 'props': {...,
    'width', 'height', 'fps', 'durationInFrames'}, 'out': absolute .mov}``)
    with ``node render.js`` in the tool's folder; returns the outputs.

    Raises MotionFailed when Node is missing, the run fails or runs past
    ``timeout_s``, or an output is missing or empty."""
    from core.node_bin import find_node
    from core.subprocess_safe import run_bounded
    if not items:
        return []
    node = find_node()
    if not node:
        raise MotionFailed('Node.js is not installed on this computer')
    os.makedirs(workdir, exist_ok=True)
    job_path = os.path.join(workdir, 'motion_job.json')
    with open(job_path, 'w', encoding='utf-8') as fh:
        json.dump({'concurrency': CONCURRENCY, 'timeout_ms': FRAME_TIMEOUT_MS,
                   'items': [dict(item) for item in items]}, fh)
    try:
        result = run_bounded([node, 'render.js', job_path], timeout=float(timeout_s),
                             cwd=tool_dir(), encoding='utf-8', errors='replace')
    except (OSError, ValueError) as e:
        raise MotionFailed('the renderer could not be started: %s' % e) from e
    if result.timed_out:
        raise MotionFailed('the overlays took longer than %d s to render' % int(timeout_s))
    if result.returncode != 0:
        tail = ' '.join((result.stderr or result.stdout or '').split())[-400:]
        logger.warning('reel motion: render.js returned %d:\n%s', result.returncode,
                       ((result.stdout or '') + (result.stderr or '')).strip()[-2000:])
        raise MotionFailed('the renderer failed (exit %d): %s' % (result.returncode, tail))
    outs = [str(item['out']) for item in items]
    missing = [o for o in outs if not (os.path.isfile(o) and os.path.getsize(o) > 0)]
    if missing:
        raise MotionFailed('the renderer wrote no clip at %s' % ', '.join(missing))
    return outs


__all__ = ['TOOL', 'READY', 'OFF', 'ASKED', 'DECLINED', 'UNAVAILABLE', 'SETTING_UP', 'FAILED',
           'NOT_SET_UP', 'PROGRESS_EVERY_S', 'RENDER_TIMEOUT_S', 'MotionFailed', 'Readiness',
           'enabled', 'status', 'ensure_ready', 'tool_dir', 'render']
