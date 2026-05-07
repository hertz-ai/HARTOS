"""
HevolveSocial — REST blueprint for voice/video/screen-share calls.

Phase 7d.  Plan reference: sunny-gliding-eich.md, Part E.4.

Endpoints (all flag-gated by `calls_v1` server-side; off → 503 via
the requires_flag decorator pattern from api.py):

  POST   /api/social/calls               start (parent_kind, parent_id, kind)
  GET    /api/social/calls/<id>          current state + participants
  POST   /api/social/calls/<id>/token    issue LiveKit token (per participant)
  POST   /api/social/calls/<id>/join     mark participant joined
  POST   /api/social/calls/<id>/leave    leave call
  POST   /api/social/calls/<id>/end      owner / parent admin only
  GET    /api/social/calls/<id>/participants  current roster
  POST   /api/social/calls/<id>/agents   add agent participant (requires AgentJoinGrant)
  POST   /api/social/calls/<id>/agents/<aid>/control  mute/unmute the agent's mic
  POST   /api/social/agent-grants        owner grants an agent join permission
  DELETE /api/social/agent-grants/<id>   owner revokes a grant
"""

from __future__ import annotations

import logging
import os

from flask import Blueprint, request, g, jsonify

from .auth import require_auth
from .api import requires_flag  # canonical flag-gate decorator
from .call_service import CallService, CallError, ALLOWED_CALL_KINDS
from .livekit_service import LiveKitService

logger = logging.getLogger('hevolve_social')

calls_bp = Blueprint('calls', __name__, url_prefix='/api/social')


def _ok(data, status=200):
    return jsonify({'success': True, 'data': data}), status


def _err(message, status=400):
    return jsonify({'success': False, 'error': message}), status


def _get_json():
    return request.get_json(force=True, silent=True) or {}


# ── Call lifecycle ───────────────────────────────────────────────────

@calls_bp.route('/calls', methods=['POST'])
@require_auth
@requires_flag('calls_v1')
def start_call():
    data = _get_json()
    parent_kind = data.get('parent_kind')
    parent_id = data.get('parent_id')
    kind = data.get('kind') or 'voice'
    if not parent_kind or not parent_id:
        return _err('parent_kind and parent_id required')
    if kind not in ALLOWED_CALL_KINDS:
        return _err(f"unsupported call kind: {kind}")
    try:
        sess = CallService.create(
            g.db, parent_kind, parent_id, g.user.id,
            kind=kind, title=data.get('title'),
            settings=data.get('settings'),
            tenant_id=getattr(g, 'tenant_id', None))
    except CallError as e:
        # 'parent not found' shape mirrors tenant isolation: 404 not 403.
        if 'not found' in str(e):
            return _err(str(e), 404)
        return _err(str(e), 400)
    return _ok(sess, status=201)


@calls_bp.route('/calls/<call_id>', methods=['GET'])
@require_auth
@requires_flag('calls_v1')
def get_call(call_id):
    try:
        sess = CallService.get(g.db, call_id)
    except CallError:
        return _err('not found', 404)
    sess['participants'] = CallService.list_participants(g.db, call_id)
    return _ok(sess)


# ── Mesh-first media-mode decision (PeerLink ↔ LiveKit complement) ──
# By design HARTOS keeps voice/video on a P2P WebRTC mesh signaled
# over PeerLink DISPATCH for small calls (cheap, low-latency, no SFU
# round-trip).  LiveKit only takes over when:
#   - participant count exceeds the mesh threshold (default 4 — N×N
#     mesh fanout becomes inefficient past that), OR
#   - any participant is an agent_bridge (the bridge needs a stable
#     SFU rendezvous URL to publish TTS / subscribe STT), OR
#   - the call kind is 'screen_share' or 'mixed' (multi-track → SFU
#     simpler than re-negotiating mesh tracks).
# Override via `LIVEKIT_MESH_THRESHOLD` env var (e.g. set to 0 to
# always promote, or to 999 to disable promotion entirely for testing).


def _mesh_threshold() -> int:
    try:
        return max(1, int(os.environ.get('LIVEKIT_MESH_THRESHOLD', '4')))
    except (TypeError, ValueError):
        return 4


def _decide_media_mode(sess, participants, *, is_agent: bool) -> str:
    """Return either 'p2p_mesh' or 'livekit'.

    The endpoint uses this to choose between a LiveKit token (forwarded
    to LiveKitService) or a {mode: 'p2p_mesh', signaling: 'peerlink'}
    response telling the client to do its own WebRTC handshake over
    the existing PeerLink DISPATCH channel.
    """
    if is_agent:
        return 'livekit'
    kind = (sess or {}).get('kind') or 'voice'
    if kind in ('screen_share', 'mixed'):
        return 'livekit'
    # Count *active* participants — left_at IS NULL.
    active = [p for p in (participants or []) if not p.get('left_at')]
    # +1 for the caller about to join, if not already in the list.
    caller_id = getattr(g.user, 'id', None)
    if caller_id and not any(p.get('user_id') == caller_id for p in active):
        active_count = len(active) + 1
    else:
        active_count = len(active)
    return 'livekit' if active_count > _mesh_threshold() else 'p2p_mesh'


@calls_bp.route('/calls/<call_id>/token', methods=['POST'])
@require_auth
@requires_flag('calls_v1')
def issue_token(call_id):
    try:
        sess = CallService.get(g.db, call_id)
    except CallError:
        return _err('not found', 404)
    if sess.get('ended_at'):
        return _err('call has ended', 410)
    data = _get_json()
    is_agent = bool(data.get('is_agent', False))
    participants = CallService.list_participants(g.db, call_id)

    # Mesh-first: keep small calls on PeerLink-signaled WebRTC mesh.
    # The client receives {mode: 'p2p_mesh'} and uses its existing
    # PeerLink DISPATCH-channel signaling path — no LiveKit involved.
    mode = _decide_media_mode(sess, participants, is_agent=is_agent)
    if mode == 'p2p_mesh':
        return _ok({
            'mode': 'p2p_mesh',
            'call_id': call_id,
            'signaling': 'peerlink',
            'participant_count': len(
                [p for p in participants if not p.get('left_at')]),
            'mesh_threshold': _mesh_threshold(),
        })

    token = LiveKitService.issue_token(
        call_id, g.user.id,
        can_publish=bool(data.get('can_publish', True)),
        can_publish_screen=bool(data.get('can_publish_screen', False)),
        is_agent=is_agent,
        agent_bridge_node_id=data.get('agent_bridge_node_id'))
    return _ok(token)


@calls_bp.route('/calls/<call_id>/join', methods=['POST'])
@require_auth
@requires_flag('calls_v1')
def join_call(call_id):
    data = _get_json()
    try:
        p = CallService.join(
            g.db, call_id, g.user.id,
            device_kind=data.get('device_kind') or 'mobile',
            tenant_id=getattr(g, 'tenant_id', None))
    except CallError as e:
        return _err(str(e), 404 if 'not found' in str(e).lower() else 400)
    return _ok(p)


@calls_bp.route('/calls/<call_id>/leave', methods=['POST'])
@require_auth
@requires_flag('calls_v1')
def leave_call(call_id):
    left = CallService.leave(g.db, call_id, g.user.id)
    return _ok({'left': left})


@calls_bp.route('/calls/<call_id>/end', methods=['POST'])
@require_auth
@requires_flag('calls_v1')
def end_call(call_id):
    try:
        sess = CallService.end(g.db, call_id, g.user.id)
    except CallError as e:
        if 'admin' in str(e) or 'starter' in str(e):
            return _err(str(e), 403)
        return _err(str(e), 404 if 'not found' in str(e) else 400)
    return _ok(sess)


@calls_bp.route('/calls/<call_id>/participants', methods=['GET'])
@require_auth
@requires_flag('calls_v1', else_value=[])
def list_participants(call_id):
    include_left = request.args.get('include_left') == 'true'
    rows = CallService.list_participants(
        g.db, call_id, include_left=include_left)
    return _ok(rows)


# ── Agent join grant ─────────────────────────────────────────────────

@calls_bp.route('/agent-grants', methods=['POST'])
@require_auth
@requires_flag('calls_v1')
def create_agent_grant():
    data = _get_json()
    agent_id = data.get('agent_id')
    parent_kind = data.get('parent_kind')
    parent_id = data.get('parent_id')
    scope = data.get('scope') or {}
    if not agent_id or not parent_kind or not parent_id:
        return _err('agent_id, parent_kind, parent_id required')
    try:
        grant = CallService.grant_agent(
            g.db, agent_id, g.user.id, parent_kind, parent_id, scope,
            tenant_id=getattr(g, 'tenant_id', None))
    except CallError as e:
        return _err(str(e), 400)
    return _ok(grant, status=201)


@calls_bp.route('/agent-grants/<grant_id>', methods=['DELETE'])
@require_auth
@requires_flag('calls_v1')
def revoke_agent_grant(grant_id):
    try:
        revoked = CallService.revoke_agent(g.db, grant_id, g.user.id)
    except CallError as e:
        if 'granter' in str(e):
            return _err(str(e), 403)
        return _err(str(e), 404 if 'not found' in str(e) else 400)
    return _ok({'revoked': revoked})


@calls_bp.route('/calls/<call_id>/agents', methods=['POST'])
@require_auth
@requires_flag('calls_v1')
def add_agent_to_call(call_id):
    data = _get_json()
    agent_id = data.get('agent_id')
    if not agent_id:
        return _err('agent_id required')
    try:
        p = CallService.attach_agent(
            g.db, call_id, agent_id,
            tenant_id=getattr(g, 'tenant_id', None))
    except CallError as e:
        return _err(str(e), 400)
    return _ok(p, status=201)
