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


def _has_livekit_config() -> bool:
    """True iff the central LiveKit env vars are set.  Flat / regional
    / Nunba bundled deploys leave these unset, so this returns False
    and clients route to P2P mesh.
    """
    return bool(
        os.environ.get('LIVEKIT_URL')
        and os.environ.get('LIVEKIT_API_KEY')
        and os.environ.get('LIVEKIT_API_SECRET'))


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
        if not _has_livekit_config():
            return {
                'mode': 'p2p_mesh',
                'call_id': call_id,
                'reason': 'no LIVEKIT_URL configured (flat/regional/bundled deploy)',
            }

        url = os.environ['LIVEKIT_URL']
        api_key = os.environ['LIVEKIT_API_KEY']
        api_secret = os.environ['LIVEKIT_API_SECRET']

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
        if not _has_livekit_config():
            return {'mode': 'p2p_mesh', 'ended': True}
        # SDK delete_room is async (asyncio coroutine) — for now we
        # just signal end-of-call; the actual API call lands in a
        # background task wired up by api_calls.end_call.
        return {'mode': 'livekit', 'ended': True}


__all__ = ['LiveKitService']
