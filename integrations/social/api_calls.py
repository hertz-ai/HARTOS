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
    token = LiveKitService.issue_token(
        call_id, g.user.id,
        can_publish=bool(data.get('can_publish', True)),
        can_publish_screen=bool(data.get('can_publish_screen', False)),
        is_agent=bool(data.get('is_agent', False)),
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
