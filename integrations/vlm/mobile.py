"""
integrations.vlm.mobile — Android + iOS surface for the VLM stack.

Phases 8 + 9 of memory/vlm_best_of_all_worlds_plan.md §6 / §7.

**Android (Phase 8)** — full participant.  An on-device companion
service (Kotlin, in a sibling Nunba-HART-Companion sub-project)
exposes the Accessibility tree + MediaProjection capture over the
PeerLink ``compute`` channel.  HARTOS Python here exposes the
client side: shape contracts, dispatch helpers, and per-platform
guards so callers don't need to ``sys.platform`` branch themselves.

**iOS (Phase 9)** — sandbox forbids cross-app capture and dispatch.
Functions return ``{'status': 'platform_unsupported', 'platform':
'ios', 'reason': '...'}`` so callers can fall back to URL-scheme
launchers + Shortcuts (the only Apple-permitted dispatch).

The Android companion app is out of scope for this module — it
ships separately in Nunba-HART-Companion/android/.  This module
defines the wire protocol both sides agree on.

Wire protocol (compute channel, JSON-encoded):
    REQUEST (HARTOS → companion):
      {
        'type': 'android_list_windows' | 'android_capture_window'
              | 'android_get_node_tree' | 'android_dispatch_action',
        'request_id': 'uuid-...',
        'window_id': '...' (optional, for capture/dispatch),
        'action':    {...} (optional, for dispatch),
      }
    RESPONSE (companion → HARTOS):
      {
        'type': '<request_type>_result',
        'request_id': 'uuid-...',
        'status': 'ok' | 'error' | 'platform_unsupported',
        'error':  '...' (when status=error),
        'data':   {...} (shape per request type — see callers below),
      }
"""

import logging
import os
import platform
import sys
import time
import uuid
from typing import List, Optional

logger = logging.getLogger('hevolve.vlm.mobile')


# ─── Platform detection ──────────────────────────────────────────────

def _detect_mobile_platform() -> str:
    """Return one of 'android', 'ios', or '' (desktop / unknown).

    Android: ``ANDROID_ARGUMENT`` env var set by Termux / Pydroid;
    or ``sys.platform == 'android'`` on newer CPython builds.
    iOS: ``platform.machine()`` starts with 'iP' (iPhone/iPad/iPod);
    or ``HEVOLVE_FORCE_PLATFORM=ios`` for testing.
    """
    forced = os.environ.get('HEVOLVE_FORCE_PLATFORM', '').lower()
    if forced in ('android', 'ios'):
        return forced
    if 'ANDROID_ARGUMENT' in os.environ or sys.platform == 'android':
        return 'android'
    if platform.system() == 'Darwin' and platform.machine().startswith('iP'):
        return 'ios'
    return ''


# ─── iOS stubs (Phase 9) ─────────────────────────────────────────────

_IOS_UNSUPPORTED = {
    'status': 'platform_unsupported',
    'platform': 'ios',
    'reason': (
        'iOS sandbox forbids cross-app screen capture and action '
        'dispatch from third-party apps.  Use URL schemes / '
        'Shortcuts for Apple-permitted dispatch, or run Nunba '
        'in-app for in-Nunba grounding only.'
    ),
}


def _ios_unsupported_response(extra: Optional[dict] = None) -> dict:
    """Standard iOS-unsupported envelope.  Callers JSON-serialize."""
    response = dict(_IOS_UNSUPPORTED)
    if extra:
        response.update(extra)
    return response


# ─── Android client (Phase 8) ────────────────────────────────────────

def list_android_windows(*, peer_dispatch=None,
                          timeout: float = 5.0) -> List[dict]:
    """Enumerate Android app windows + activities visible to the
    companion app.

    Args:
        peer_dispatch: optional callable
            ``peer_dispatch(channel, payload, timeout) -> response_dict``
            for sending to the paired companion device.  When None,
            this function falls back to the local companion (Termux
            UNIX socket at /data/data/com.termux/files/usr/var/run/
            nunba-companion.sock) — only useful when HARTOS itself is
            running ON the Android device.
        timeout: max wait for companion response, in seconds.

    Returns:
        Per the wire-protocol shape — list of window dicts:
          [{window_id, package, activity, title, rect, monitor_idx,
            is_foreground, is_accessible}]
        Empty list when no companion is reachable, or a list with
        a single ``{'platform_unsupported': True}`` marker on iOS.
    """
    plat = _detect_mobile_platform()
    if plat == 'ios':
        return [_ios_unsupported_response({'request': 'list_windows'})]
    if plat != 'android':
        # Caller is running on a desktop and asking about Android —
        # only reachable via PeerLink.  Without peer_dispatch we
        # can't talk to the companion, so return empty.
        if peer_dispatch is None:
            logger.debug(
                'list_android_windows: no peer_dispatch and not on Android')
            return []
    payload = {
        'type': 'android_list_windows',
        'request_id': str(uuid.uuid4()),
        'ts': time.time(),
    }
    response = _send_to_companion(payload, peer_dispatch, timeout)
    if response is None:
        return []
    if response.get('status') != 'ok':
        logger.debug(f'list_android_windows companion error: '
                     f'{response.get("error")}')
        return []
    return list(response.get('data', {}).get('windows') or [])


def capture_android_window(window_id: str, *, peer_dispatch=None,
                            timeout: float = 5.0) -> Optional[bytes]:
    """Capture an Android window's pixels via MediaProjection.

    Returns JPEG bytes or None.  Only works when:
      * HARTOS is on the device with companion installed + accessibility
        service enabled, OR
      * peer_dispatch routes to a paired Android via PeerLink.

    iOS not supported (sandbox); returns None.
    """
    plat = _detect_mobile_platform()
    if plat == 'ios':
        return None
    payload = {
        'type': 'android_capture_window',
        'request_id': str(uuid.uuid4()),
        'window_id': window_id,
        'ts': time.time(),
    }
    response = _send_to_companion(payload, peer_dispatch, timeout)
    if response is None or response.get('status') != 'ok':
        return None
    import base64
    b64 = response.get('data', {}).get('jpeg_base64')
    if not b64:
        return None
    try:
        return base64.b64decode(b64)
    except Exception:
        return None


def get_android_node_tree(*, window_id: Optional[str] = None,
                           peer_dispatch=None,
                           timeout: float = 5.0) -> Optional[dict]:
    """Fetch the AccessibilityNodeInfo tree of the foreground window
    (or *window_id* if specified).  This is often a SUPERIOR signal
    to VLM grounding on Android — text/contentDescription/clickable
    flags are exposed directly without pixel reasoning.  Most agents
    operate primarily by tree matching and only fall back to VLM
    when the UI is canvas-rendered (games, Compose without semantics).

    Returns:
        Tree dict ``{root: {class, text, content_description,
        clickable, bounds, children: [...]}}`` or None on failure.
    """
    plat = _detect_mobile_platform()
    if plat == 'ios':
        return _ios_unsupported_response({'request': 'get_node_tree'})
    payload = {
        'type': 'android_get_node_tree',
        'request_id': str(uuid.uuid4()),
        'window_id': window_id,
        'ts': time.time(),
    }
    response = _send_to_companion(payload, peer_dispatch, timeout)
    if response is None or response.get('status') != 'ok':
        return None
    return response.get('data', {}).get('tree')


def dispatch_android_action(action: dict, *,
                              peer_dispatch=None,
                              timeout: float = 5.0) -> dict:
    """Send a VLM-emitted action to the Android companion for execution.

    Action mapping (companion handles):
      ``left_click [x,y]``    → AccessibilityService.dispatchGesture
                                 OR node.performAction(ACTION_CLICK)
      ``type "text"``         → node.performAction(ACTION_SET_TEXT)
      ``key "BACK"|"HOME"``   → performGlobalAction(GLOBAL_ACTION_BACK)
      ``scroll_down``         → dispatchGesture swipe
      ``open_file_gui "X"``   → Intent.ACTION_VIEW launcher
    """
    plat = _detect_mobile_platform()
    if plat == 'ios':
        return _ios_unsupported_response({'request': 'dispatch_action'})
    payload = {
        'type': 'android_dispatch_action',
        'request_id': str(uuid.uuid4()),
        'action': action,
        'ts': time.time(),
    }
    response = _send_to_companion(payload, peer_dispatch, timeout)
    return response or {'status': 'no_response'}


# ─── Companion transport ─────────────────────────────────────────────

def _send_to_companion(payload: dict, peer_dispatch, timeout: float
                        ) -> Optional[dict]:
    """Best-effort send to the companion app.

    Resolution order (mirrors plan §10's resolver):
      1. peer_dispatch callable (caller-supplied, typically wraps
         PeerLink compute channel)
      2. Local UNIX socket on Android (companion-on-same-device)
      3. None (no companion reachable)
    """
    if peer_dispatch is not None:
        try:
            return peer_dispatch('compute', payload, timeout=timeout)
        except Exception as e:
            logger.debug(f'peer_dispatch failed: {e}')
            return None
    return _send_via_local_socket(payload, timeout)


def _send_via_local_socket(payload: dict, timeout: float
                            ) -> Optional[dict]:
    """UNIX-socket transport for Termux / on-device deployment."""
    if _detect_mobile_platform() != 'android':
        return None
    import json
    import socket as _sk
    sock_path = os.environ.get(
        'HEVOLVE_ANDROID_COMPANION_SOCK',
        '/data/data/com.termux/files/usr/var/run/nunba-companion.sock')
    if not os.path.exists(sock_path):
        logger.debug(f'companion socket missing at {sock_path}')
        return None
    try:
        with _sk.socket(_sk.AF_UNIX, _sk.SOCK_STREAM) as s:
            s.settimeout(timeout)
            s.connect(sock_path)
            s.sendall((json.dumps(payload) + '\n').encode('utf-8'))
            data = b''
            while b'\n' not in data:
                chunk = s.recv(8192)
                if not chunk:
                    break
                data += chunk
            return json.loads(data.decode('utf-8').strip())
    except Exception as e:
        logger.debug(f'local socket transport failed: {e}')
        return None
