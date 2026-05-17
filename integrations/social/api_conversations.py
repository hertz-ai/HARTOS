"""
HevolveSocial — Conversations API blueprint.

Phase 7c.3.  Plan reference: sunny-gliding-eich.md, Part E.3.

Mounted at /api/social/conversations.  Distinct from
/api/social/channels (which is the legacy 31-channel external adapter
— Telegram / WhatsApp / Discord etc., served by api_channels.py).
The two blueprints coexist; their URL prefixes never overlap.

Endpoints (all flag-gated by `conversations`; mutating endpoints return
503 when the flag is off, read endpoints return []):

  GET    /                          list current user's conversations
  POST   /                          create (kind, member_ids, title?)
  GET    /<id>                      detail + (no messages — caller
                                    fetches messages separately)
  GET    /<id>/messages             paginated message history
  POST   /<id>/messages             send a message
  PATCH  /<id>/messages/<msg_id>    edit own (≤24 h)
  DELETE /<id>/messages/<msg_id>    soft-delete own
  POST   /<id>/members              add (group only, admin only)
  DELETE /<id>/members/<user_id>    remove (admin removes others, self leaves)

The blueprint is registered in social __init__.py / hartos_bootstrap
alongside the existing api.social_bp.
"""

from __future__ import annotations

import logging
from flask import Blueprint, request, jsonify, g

from .auth import require_auth

logger = logging.getLogger('hevolve_social')

conversations_bp = Blueprint(
    'conversations', __name__, url_prefix='/api/social/conversations')


def _ok(data=None, status=200):
    r = {'success': True}
    if data is not None:
        r['data'] = data
    return jsonify(r), status


def _err(msg, status=400):
    return jsonify({'success': False, 'error': msg}), status


def _get_json():
    return request.get_json(force=True, silent=True) or {}


def _flag_on() -> bool:
    flags = getattr(g, 'feature_flags', {}) or {}
    return bool(flags.get('conversations', False))


# ── List + create ────────────────────────────────────────────────

@conversations_bp.route('', methods=['GET'])
@require_auth
def list_conversations():
    if not _flag_on():
        return _ok([])
    include_archived = (request.args.get('include_archived',
                                         'false').lower() == 'true')
    limit = max(1, min(int(request.args.get('limit', 50)), 200))
    from .conversation_service import ConversationService
    return _ok(ConversationService.list_for_user(
        g.db, g.user.id, include_archived=include_archived, limit=limit))


@conversations_bp.route('', methods=['POST'])
@require_auth
def create_conversation():
    if not _flag_on():
        return _err("conversations feature flag is off", 503)
    data = _get_json()
    kind = data.get('kind')
    member_ids = data.get('member_ids') or []
    title = data.get('title')
    if not kind:
        return _err("kind required ('dm' | 'group')")
    if not isinstance(member_ids, list):
        return _err("member_ids must be a list")
    try:
        from .conversation_service import (
            ConversationService, ConversationError)
        result = ConversationService.create(
            g.db, kind=kind, member_ids=member_ids,
            created_by=g.user.id, title=title,
            tenant_id=getattr(g, 'tenant_id', None))
        return _ok(result, status=201)
    except ConversationError as e:
        return _err(str(e), 400)


@conversations_bp.route('/<conv_id>', methods=['GET'])
@require_auth
def get_conversation(conv_id):
    if not _flag_on():
        return _err("conversations feature flag is off", 503)
    from .conversation_service import ConversationService, _is_member
    if not _is_member(g.db, conv_id, g.user.id):
        return _err("not found", 404)  # 404 (not 403) to avoid leak
    result = ConversationService.get(g.db, conv_id)
    if result is None:
        return _err("not found", 404)
    return _ok(result)


# ── Messages ─────────────────────────────────────────────────────

@conversations_bp.route('/<conv_id>/messages', methods=['GET'])
@require_auth
def list_messages(conv_id):
    if not _flag_on():
        return _ok([])
    before = request.args.get('before')
    limit = max(1, min(int(request.args.get('limit', 50)), 200))
    try:
        from .conversation_service import (
            ConversationService, ConversationError)
        return _ok(ConversationService.list_messages(
            g.db, conv_id, requester_id=g.user.id,
            before=before, limit=limit))
    except ConversationError as e:
        return _err(str(e), 403)


@conversations_bp.route('/<conv_id>/messages', methods=['POST'])
@require_auth
def send_message(conv_id):
    if not _flag_on():
        return _err("conversations feature flag is off", 503)
    data = _get_json()
    content = data.get('content')
    thread_root_id = data.get('thread_root_id')
    metadata = data.get('metadata')
    if not content:
        return _err("content required")
    try:
        from .conversation_service import (
            ConversationService, ConversationError)
        result = ConversationService.send_message(
            g.db, conv_id=conv_id, author_id=g.user.id,
            content=content, thread_root_id=thread_root_id,
            metadata=metadata,
            tenant_id=getattr(g, 'tenant_id', None))
        return _ok(result, status=201)
    except ConversationError as e:
        return _err(str(e), 400)


@conversations_bp.route('/<conv_id>/messages/<message_id>',
                        methods=['PATCH'])
@require_auth
def edit_message(conv_id, message_id):
    if not _flag_on():
        return _err("conversations feature flag is off", 503)
    data = _get_json()
    content = data.get('content')
    if not content:
        return _err("content required")
    try:
        from .conversation_service import (
            ConversationService, ConversationError)
        return _ok(ConversationService.edit_message(
            g.db, message_id=message_id, requester_id=g.user.id,
            new_content=content))
    except ConversationError as e:
        return _err(str(e), 400)


@conversations_bp.route('/<conv_id>/messages/<message_id>',
                        methods=['DELETE'])
@require_auth
def delete_message(conv_id, message_id):
    if not _flag_on():
        return _err("conversations feature flag is off", 503)
    try:
        from .conversation_service import (
            ConversationService, ConversationError)
        return _ok(ConversationService.soft_delete_message(
            g.db, message_id=message_id, requester_id=g.user.id))
    except ConversationError as e:
        return _err(str(e), 400)


# ── Members (group only) ─────────────────────────────────────────

@conversations_bp.route('/<conv_id>/members', methods=['POST'])
@require_auth
def add_member(conv_id):
    if not _flag_on():
        return _err("conversations feature flag is off", 503)
    data = _get_json()
    new_member_id = data.get('user_id') or data.get('member_id')
    if not new_member_id:
        return _err("user_id required")
    try:
        from .conversation_service import (
            ConversationService, ConversationError)
        return _ok(ConversationService.add_member(
            g.db, conv_id=conv_id, requester_id=g.user.id,
            new_member_id=new_member_id,
            tenant_id=getattr(g, 'tenant_id', None)))
    except ConversationError as e:
        return _err(str(e), 400)


@conversations_bp.route('/<conv_id>/members/<user_id>', methods=['DELETE'])
@require_auth
def remove_member(conv_id, user_id):
    if not _flag_on():
        return _err("conversations feature flag is off", 503)
    try:
        from .conversation_service import (
            ConversationService, ConversationError)
        return _ok(ConversationService.remove_member(
            g.db, conv_id=conv_id, requester_id=g.user.id,
            target_id=user_id))
    except ConversationError as e:
        return _err(str(e), 400)


# ── Typing + read-receipt (Phase 7c.7) ───────────────────────────

@conversations_bp.route('/<conv_id>/typing', methods=['POST'])
@require_auth
def emit_typing(conv_id):
    """Broadcast that the caller is typing in the conversation.

    Body is empty; the typing payload is just (user_id, conv_id).
    Receivers see it through tenant.{tid}.conv.{id}.typing WAMP
    subscription.  Pure broadcast — never persisted.
    """
    if not _flag_on():
        return _err("conversations feature flag is off", 503)
    try:
        from .conversation_service import (
            ConversationService, ConversationError)
        return _ok(ConversationService.emit_typing(
            g.db, conv_id=conv_id, user_id=g.user.id,
            tenant_id=getattr(g, 'tenant_id', None)))
    except ConversationError as e:
        return _err(str(e), 400)


@conversations_bp.route('/<conv_id>/read-receipt', methods=['POST'])
@require_auth
def mark_read(conv_id):
    """Mark conversation read up to a specific message (or latest).

    Body: { "message_id": "<id>" }  — optional; if omitted we mark the
    most recent message read.  Persists last_read_message_id +
    last_read_at on the user's memberships row AND broadcasts a
    receipt to other members via tenant.{tid}.conv.{id}.read.
    """
    if not _flag_on():
        return _err("conversations feature flag is off", 503)
    data = _get_json()
    try:
        from .conversation_service import (
            ConversationService, ConversationError)
        return _ok(ConversationService.mark_read(
            g.db, conv_id=conv_id, user_id=g.user.id,
            message_id=data.get('message_id'),
            tenant_id=getattr(g, 'tenant_id', None)))
    except ConversationError as e:
        return _err(str(e), 400)
