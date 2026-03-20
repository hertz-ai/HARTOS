"""
User-facing Channel Bindings API — /api/social/channels

Provides endpoints for:
- Channel catalog (metadata, capabilities)
- User channel bindings (CRUD, preferred)
- Pairing (code generation + QR, verification)
- Presence (adapter status, heartbeat)
- Unified conversation history
"""

import base64
import io
import logging
from datetime import datetime

from flask import Blueprint, request, jsonify, g

from .auth import require_auth
from .models import (
    get_db, UserChannelBinding, ConversationEntry, ChannelPresence,
)

logger = logging.getLogger(__name__)

channel_user_bp = Blueprint('channel_user', __name__, url_prefix='/api/social/channels')


# ── Catalog ────────────────────────────────────────────────────

@channel_user_bp.route('/catalog', methods=['GET'])
def get_catalog():
    """Return the full channel metadata catalog (public, no auth)."""
    from integrations.channels.metadata import list_all_channels
    catalog = list_all_channels()
    return jsonify({'success': True, 'data': catalog})


@channel_user_bp.route('/catalog/<channel_type>', methods=['GET'])
def get_catalog_channel(channel_type):
    """Return metadata for a single channel."""
    from integrations.channels.metadata import get_channel_metadata
    meta = get_channel_metadata(channel_type)
    if not meta:
        return jsonify({'success': False, 'error': f'Unknown channel: {channel_type}'}), 404
    return jsonify({'success': True, 'data': meta})


# ── Bindings ───────────────────────────────────────────────────

@channel_user_bp.route('/bindings', methods=['GET'])
@require_auth
def list_bindings():
    """List current user's channel bindings."""
    bindings = g.db.query(UserChannelBinding).filter_by(
        user_id=g.user_id,
    ).order_by(UserChannelBinding.created_at.desc()).all()
    return jsonify({'success': True, 'data': [b.to_dict() for b in bindings]})


@channel_user_bp.route('/bindings', methods=['POST'])
@require_auth
def create_binding():
    """Create a new channel binding for the current user."""
    data = request.get_json(silent=True) or {}
    channel_type = data.get('channel_type')
    if not channel_type:
        return jsonify({'success': False, 'error': 'channel_type is required'}), 400

    from integrations.channels.metadata import get_channel_metadata
    if not get_channel_metadata(channel_type):
        return jsonify({'success': False, 'error': f'Unknown channel: {channel_type}'}), 400

    sender_id = data.get('channel_sender_id', '')
    chat_id = data.get('channel_chat_id', '')

    # Check for existing binding
    existing = g.db.query(UserChannelBinding).filter_by(
        user_id=g.user_id,
        channel_type=channel_type,
        channel_sender_id=sender_id,
    ).first()

    if existing:
        existing.is_active = True
        existing.channel_chat_id = chat_id or existing.channel_chat_id
        existing.metadata_json = data.get('metadata', existing.metadata_json)
        existing.auth_method = data.get('auth_method', existing.auth_method)
        binding = existing
    else:
        binding = UserChannelBinding(
            user_id=g.user_id,
            channel_type=channel_type,
            channel_sender_id=sender_id,
            channel_chat_id=chat_id,
            auth_method=data.get('auth_method'),
            metadata_json=data.get('metadata'),
            is_active=True,
            is_preferred=False,
        )
        g.db.add(binding)

    g.db.flush()
    return jsonify({'success': True, 'data': binding.to_dict()}), 201


@channel_user_bp.route('/bindings/<int:binding_id>', methods=['DELETE'])
@require_auth
def remove_binding(binding_id):
    """Remove a channel binding (soft-delete: set is_active=False)."""
    binding = g.db.query(UserChannelBinding).filter_by(
        id=binding_id, user_id=g.user_id,
    ).first()
    if not binding:
        return jsonify({'success': False, 'error': 'Binding not found'}), 404

    g.db.delete(binding)
    return jsonify({'success': True, 'data': {'deleted': binding_id}})


@channel_user_bp.route('/bindings/<int:binding_id>/preferred', methods=['PUT'])
@require_auth
def set_preferred(binding_id):
    """Set a binding as the preferred reply channel (unsets others)."""
    binding = g.db.query(UserChannelBinding).filter_by(
        id=binding_id, user_id=g.user_id,
    ).first()
    if not binding:
        return jsonify({'success': False, 'error': 'Binding not found'}), 404

    # Unset all other preferred for this user
    g.db.query(UserChannelBinding).filter(
        UserChannelBinding.user_id == g.user_id,
        UserChannelBinding.id != binding_id,
    ).update({'is_preferred': False})

    binding.is_preferred = True
    return jsonify({'success': True, 'data': binding.to_dict()})


# ── Pairing ────────────────────────────────────────────────────

@channel_user_bp.route('/pair/generate', methods=['POST'])
@require_auth
def generate_pair_code():
    """Generate a pairing code + QR code for cross-device linking."""
    try:
        from integrations.channels.security import PairingManager
        pm = PairingManager()
        code = pm.generate_pairing_code(
            user_id=int(g.user_id) if g.user_id.isdigit() else hash(g.user_id) % 100000,
            prompt_id=0,
            expiry_minutes=15,
        )

        # Generate QR as base64 PNG
        qr_data_url = _generate_qr_data_url(f'hevolve://pair?code={code}')

        return jsonify({
            'success': True,
            'data': {
                'code': code,
                'qr_data_url': qr_data_url,
                'expires_in_seconds': 900,
            },
        })
    except Exception as e:
        logger.error("Failed to generate pairing code: %s", e)
        return jsonify({'success': False, 'error': 'Failed to generate pairing code'}), 500


@channel_user_bp.route('/pair/verify', methods=['POST'])
@require_auth
def verify_pair_code():
    """Verify a pairing code and create a UserChannelBinding."""
    data = request.get_json(silent=True) or {}
    code = data.get('code')
    channel_type = data.get('channel', 'mobile')
    sender_id = data.get('sender_id', '')

    if not code:
        return jsonify({'success': False, 'error': 'code is required'}), 400

    try:
        from integrations.channels.security import PairingManager
        pm = PairingManager()
        result = pm.verify_pairing(channel_type, sender_id, code)

        if result is None:
            return jsonify({'success': False, 'error': 'Invalid or expired pairing code'}), 400

        # Create binding
        binding = UserChannelBinding(
            user_id=g.user_id,
            channel_type=channel_type,
            channel_sender_id=sender_id,
            auth_method='pairing',
            is_active=True,
            is_preferred=False,
        )
        g.db.add(binding)
        g.db.flush()

        return jsonify({'success': True, 'data': binding.to_dict()})
    except Exception as e:
        logger.error("Pairing verification failed: %s", e)
        return jsonify({'success': False, 'error': 'Verification failed'}), 500


# ── Presence ───────────────────────────────────────────────────

@channel_user_bp.route('/presence', methods=['GET'])
def get_presence():
    """Get all channel adapter statuses (public)."""
    db = get_db()
    try:
        presences = db.query(ChannelPresence).all()
        return jsonify({'success': True, 'data': [p.to_dict() for p in presences]})
    finally:
        db.close()


@channel_user_bp.route('/presence/heartbeat', methods=['POST'])
def post_heartbeat():
    """Adapter reports heartbeat — upserts ChannelPresence row."""
    data = request.get_json(silent=True) or {}
    channel_type = data.get('channel_type')
    status = data.get('status', 'online')

    if not channel_type:
        return jsonify({'success': False, 'error': 'channel_type required'}), 400

    db = get_db()
    try:
        existing = db.query(ChannelPresence).filter_by(channel_type=channel_type).first()
        if existing:
            existing.status = status
            existing.last_heartbeat = datetime.utcnow()
            existing.error_message = data.get('error_message')
        else:
            presence = ChannelPresence(
                channel_type=channel_type,
                status=status,
                last_heartbeat=datetime.utcnow(),
                error_message=data.get('error_message'),
            )
            db.add(presence)
        db.commit()
        return jsonify({'success': True})
    except Exception as e:
        db.rollback()
        return jsonify({'success': False, 'error': str(e)}), 500
    finally:
        db.close()


# ── Conversation History ───────────────────────────────────────

@channel_user_bp.route('/conversations', methods=['GET'])
@require_auth
def get_conversations():
    """Paginated unified conversation history for the current user."""
    page = request.args.get('page', 1, type=int)
    per_page = min(request.args.get('per_page', 50, type=int), 100)
    channel_filter = request.args.get('channel_type')

    query = g.db.query(ConversationEntry).filter_by(user_id=g.user_id)
    if channel_filter:
        query = query.filter_by(channel_type=channel_filter)

    total = query.count()
    entries = query.order_by(
        ConversationEntry.created_at.desc()
    ).offset((page - 1) * per_page).limit(per_page).all()

    return jsonify({
        'success': True,
        'data': [e.to_dict() for e in entries],
        'pagination': {
            'page': page,
            'per_page': per_page,
            'total': total,
            'pages': (total + per_page - 1) // per_page,
        },
    })


# ── Helpers ────────────────────────────────────────────────────

def _generate_qr_data_url(data: str) -> str:
    """Generate a QR code as a base64 data URL (PNG)."""
    try:
        import qrcode
        qr = qrcode.QRCode(version=1, box_size=8, border=2)
        qr.add_data(data)
        qr.make(fit=True)
        img = qr.make_image(fill_color='black', back_color='white')
        buf = io.BytesIO()
        img.save(buf, format='PNG')
        b64 = base64.b64encode(buf.getvalue()).decode('ascii')
        return f'data:image/png;base64,{b64}'
    except ImportError:
        logger.warning("qrcode package not installed — QR generation unavailable")
        return ''
    except Exception as e:
        logger.warning("QR generation failed: %s", e)
        return ''
