"""
HevolveSocial — LiveKit token + room helper (deploy-mode aware stub).

Phase 7d.  Plan reference: sunny-gliding-eich.md, Part E.7 + Part R.6.

LiveKit is the FALLBACK media transport when:
  - Call has > 4 participants (mesh efficiency drops).
  - Network topology prevents direct WebRTC P2P (NAT, mobile carrier).
  - One participant is an AgentVoiceBridge (the bridge needs a stable
    rendezvous URL, which LiveKit provides).

For 1:1 + small group calls (≤ 4) on the same network, the clients
run a WebRTC P2P mesh signaled over PeerLink DISPATCH channel.  This
service issues a LiveKit token only when called explicitly — clients
that succeed at mesh never request one.

Deploy-mode adaptation (Plan E.7 + Part Q):
  - central         → managed LiveKit Cloud or self-hosted SFU.
                       LIVEKIT_URL + LIVEKIT_API_KEY + LIVEKIT_API_SECRET
                       env vars come from per-tenant config.
  - regional        → ship `livekit-server` binary alongside HARTOS,
                       one per LAN node.
  - flat / Nunba    → LIVEKIT_URL empty → token issuance returns
                       {'mode': 'p2p_mesh'} so clients fall back to
                       WebRTC P2P + PeerLink signaling.

This module is INTENTIONALLY a stub today: the actual `livekit-server-
sdk-python` import + token signing land alongside Phase 7d.B (when the
deployment infra is provisioned).  The contract is the JSON shape the
clients expect; we ship the contract first so tests + RN client work
land green ahead of infra.
"""

from __future__ import annotations

import json
import logging
import os
import time
from datetime import timedelta
from typing import Any, Dict, Optional

logger = logging.getLogger('hevolve_social')


# Phase 7d.B — best-effort import of the LiveKit server SDK.
# When installed (`pip install livekit-api`), this module signs
# real JWTs.  When not installed (flat / regional / Nunba bundled
# without LiveKit), we fall back to the stub shape and clients
# route to WebRTC P2P mesh via PeerLink.
try:
    from livekit import api as livekit_api  # type: ignore
    _HAS_LIVEKIT_SDK = True
except Exception:
    livekit_api = None
    _HAS_LIVEKIT_SDK = False


def _resolved_config():
    """Return (url, api_key, api_secret) — env-vars override; else fall
    back to the supervisor's auto-generated dev keys + localhost URL.

    Flat/regional installs that haven't been provisioned with central-
    issued keys still get a working signer because livekit_supervisor
    auto-generates a dev key/secret pair on first start.  Central
    deploys (which don't host an SFU) leave LIVEKIT_DISABLE=1; that
    short-circuits supervisor_should_run() so no dev keys exist and
    we fall back to the {mode: 'p2p_mesh'} response.
    """
    env_url = os.environ.get('LIVEKIT_URL')
    env_key = os.environ.get('LIVEKIT_API_KEY')
    env_secret = os.environ.get('LIVEKIT_API_SECRET')
    if env_url and env_key and env_secret:
        return env_url, env_key, env_secret

    # Lazy import to avoid a circular dep during module init.
    try:
        from .livekit_supervisor import (
            ensure_dev_keys,
            get_livekit_url,
            supervisor_should_run,
        )
    except Exception:  # pragma: no cover — defensive
        return None, None, None

    if not supervisor_should_run():
        return None, None, None

    keys = ensure_dev_keys()
    url = env_url or get_livekit_url()
    return url, keys.get('api_key'), keys.get('api_secret')


def _has_livekit_config() -> bool:
    """True iff we have a complete (url, api_key, api_secret) triple
    — either from operator-set env vars OR from the supervisor's auto-
    generated dev keys when running in flat/regional mode.
    """
    url, key, secret = _resolved_config()
    return bool(url and key and secret)


class LiveKitService:

    @staticmethod
    def issue_token(call_id: str, user_id: str,
                    *, can_publish: bool = True,
                    can_publish_screen: bool = False,
                    is_agent: bool = False,
                    agent_bridge_node_id: Optional[str] = None,
                    ttl_seconds: int = 3600) -> Dict[str, Any]:
        """Return a token-issuance result.

        Shape always includes `mode` so the client knows whether to
        connect to LiveKit (mode='livekit') or fall back to P2P mesh
        (mode='p2p_mesh').

        When mode='livekit', the result includes:
          - url: the LiveKit server URL
          - token: signed JWT for room=call_id, identity=user_id
          - metadata: {agent_kind, agent_bridge_node_id, ...}

        When mode='p2p_mesh', the result is just {mode, call_id} and
        the client does its own WebRTC handshake via PeerLink.
        """
        url, api_key, api_secret = _resolved_config()
        if not (url and api_key and api_secret):
            return {
                'mode': 'p2p_mesh',
                'call_id': call_id,
                'reason': 'no LIVEKIT config; central/embedded deploy or '
                          'LIVEKIT_DISABLE=1 set',
            }

        metadata = {
            'agent_kind': 'agent' if is_agent else 'human',
            'agent_bridge_node_id': agent_bridge_node_id,
            'can_publish_screen': can_publish_screen,
        }

        # Phase 7d.B — sign a real LiveKit JWT when the SDK is
        # available.  The SDK builds the JWT with the identity +
        # grants we want.  Falls back to a stub shape when the SDK
        # isn't installed so the REST contract stays testable.
        if _HAS_LIVEKIT_SDK and livekit_api is not None:
            try:
                grants = livekit_api.VideoGrants(
                    room_join=True,
                    room=call_id,
                    can_publish=can_publish,
                    can_publish_data=True,
                    can_subscribe=True,
                )
                # `can_publish_screen` controls screen-share track
                # publishing — gated by the AgentJoinGrant.scope on
                # the caller side.
                if can_publish_screen and hasattr(grants, 'can_publish_sources'):
                    grants.can_publish_sources = ['camera', 'microphone',
                                                  'screen_share',
                                                  'screen_share_audio']
                token = (
                    livekit_api.AccessToken(api_key, api_secret)
                    .with_identity(user_id)
                    .with_grants(grants)
                    .with_metadata(json.dumps(metadata))
                    .with_ttl(timedelta(seconds=ttl_seconds))
                    .to_jwt()
                )
                return {
                    'mode': 'livekit',
                    'url': url,
                    'token': token,
                    'metadata': metadata,
                    'expires_at': int(time.time()) + ttl_seconds,
                }
            except Exception as e:
                logger.warning(
                    "LiveKitService.issue_token: SDK failed (%s); "
                    "falling back to livekit_pending shape", e)

        # SDK absent OR signing failed — return the pending shape
        # so the client knows infra is configured but not ready.
        # (Pass-4 P4-6: renamed from 'livekit_stub' for clarity.)
        return {
            'mode': 'livekit_pending',
            'url': url,
            'token': '',
            'metadata': metadata,
            'expires_at': int(time.time()) + ttl_seconds,
            'reason': ('livekit-api SDK not installed; pip install '
                       'livekit-api to enable real token signing'),
        }

    @staticmethod
    def end_room(call_id: str) -> Dict[str, Any]:
        """Tear down a LiveKit room.  No-op when no LiveKit configured."""
        url, _key, _secret = _resolved_config()
        if not url:
            return {'mode': 'p2p_mesh', 'ended': True}
        # SDK delete_room is async (asyncio coroutine) — for now we
        # just signal end-of-call; the actual API call lands in a
        # background task wired up by api_calls.end_call.
        return {'mode': 'livekit', 'ended': True}

    # ── Server-side recording via LiveKit Egress (#72) ──────────────
    #
    # The "Teams recording while screen-sharing" model for the mobile
    # demo: instead of an external OS screen-grab (which would capture
    # everything outside the app), the LiveKit SERVER composites the
    # call room — INCLUDING any screen-share track already published in
    # the call (MediaProjection on Android) — and encodes it to an mp4
    # server-side.  Records exactly what participants see, no per-device
    # recorder.  Reuses the same (url, key, secret) issue_token signs
    # with; SDK-guarded like issue_token so flat/no-SDK deploys degrade
    # gracefully instead of raising.

    @staticmethod
    def _api_base_url(client_url: str) -> str:
        """LiveKit's server (twirp) API shares the signaling port but
        speaks http(s)://, not ws(s)://.  Convert the client URL."""
        if client_url.startswith('wss://'):
            return 'https://' + client_url[len('wss://'):]
        if client_url.startswith('ws://'):
            return 'http://' + client_url[len('ws://'):]
        return client_url  # already http(s) (or bare host)

    @staticmethod
    def start_recording(call_id: str, *, layout: str = 'grid',
                        output_path: Optional[str] = None,
                        audio_only: bool = False) -> Dict[str, Any]:
        """Start a server-side LiveKit Egress recording of room ``call_id``.

        Composites the whole room (camera + the screen-share track the
        call already carries) to an mp4 on the egress host.  Returns
        {ok, egress_id, filepath, status, room} or {ok: False, reason}
        — the False shapes cover: no LiveKit config (p2p/central), SDK
        absent, or a reachable SFU with no egress worker (the request was
        well-formed + authenticated; only the egress *service* is missing).
        """
        url, api_key, api_secret = _resolved_config()
        if not (url and api_key and api_secret):
            return {'ok': False, 'mode': 'p2p_mesh',
                    'reason': 'no LIVEKIT config; central/embedded deploy'}
        if not (_HAS_LIVEKIT_SDK and livekit_api is not None):
            return {'ok': False,
                    'reason': 'livekit-api SDK not installed; pip install livekit-api'}

        if not output_path:
            output_path = f'{call_id}-{int(time.time())}.mp4'

        async def _start():
            lkapi = livekit_api.LiveKitAPI(
                LiveKitService._api_base_url(url), api_key, api_secret)
            try:
                req = livekit_api.RoomCompositeEgressRequest(
                    room_name=call_id,
                    layout=layout,
                    audio_only=audio_only,
                    file_outputs=[livekit_api.EncodedFileOutput(
                        file_type=livekit_api.EncodedFileType.MP4,
                        filepath=output_path,
                    )],
                )
                return await lkapi.egress.start_room_composite_egress(req)
            finally:
                await lkapi.aclose()

        try:
            from core.event_loop import run_async  # canonical sync→async runner
            info = run_async(_start())
            return {
                'ok': True,
                'egress_id': info.egress_id,
                'filepath': output_path,
                'status': int(info.status),
                'room': call_id,
            }
        except Exception as e:
            logger.warning("LiveKitService.start_recording failed: %s", e)
            return {'ok': False, 'reason': str(e), 'room': call_id}

    @staticmethod
    def stop_recording(egress_id: str) -> Dict[str, Any]:
        """Stop a running egress recording.  Returns {ok, egress_id,
        status} or {ok: False, reason}."""
        url, api_key, api_secret = _resolved_config()
        if not (url and api_key and api_secret):
            return {'ok': False, 'reason': 'no LIVEKIT config'}
        if not (_HAS_LIVEKIT_SDK and livekit_api is not None):
            return {'ok': False, 'reason': 'livekit-api SDK not installed'}

        async def _stop():
            lkapi = livekit_api.LiveKitAPI(
                LiveKitService._api_base_url(url), api_key, api_secret)
            try:
                return await lkapi.egress.stop_egress(
                    livekit_api.StopEgressRequest(egress_id=egress_id))
            finally:
                await lkapi.aclose()

        try:
            from core.event_loop import run_async  # canonical sync→async runner
            info = run_async(_stop())
            return {'ok': True, 'egress_id': egress_id, 'status': int(info.status)}
        except Exception as e:
            logger.warning("LiveKitService.stop_recording failed: %s", e)
            return {'ok': False, 'reason': str(e)}


__all__ = ['LiveKitService']
