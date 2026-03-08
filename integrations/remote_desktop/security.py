"""
Remote Desktop Security — Auth, audit, DLP, input classification, E2E encryption.

Reuses existing HARTOS security infrastructure:
  - security/channel_encryption.py → encrypt_for_peer() for E2E frame encryption
  - security/immutable_audit_log.py → log_event() for session audit trail
  - security/action_classifier.py → classify_action() for destructive input gating
  - security/dlp_engine.py → check_outbound() for file transfer DLP
  - security/rate_limiter_redis.py → rate_limit() for connection attempt limiting
  - integrations/social/auth.py → generate_token_pair() for JWT session tokens
  - integrations/social/services.py → NotificationService.create() for connection requests
"""

import logging
import time
from typing import Any, Dict, Optional, Tuple

logger = logging.getLogger('hevolve.remote_desktop')


# ── Authentication ──────────────────────────────────────────────

def authenticate_connection(host_device_id: str, viewer_device_id: str,
                            password: str,
                            host_user_id: Optional[str] = None,
                            viewer_user_id: Optional[str] = None
                            ) -> Tuple[bool, str]:
    """Authenticate a remote desktop connection.

    Same-user: auto-accept (no OTP needed) — compute_mesh_service.py:398 pattern.
    Cross-user: OTP + explicit consent.

    Returns:
        (success, reason)
    """
    from integrations.remote_desktop.session_manager import get_session_manager

    sm = get_session_manager()

    # Same-user auto-accept
    if sm.is_same_user(host_user_id, viewer_user_id):
        logger.info(
            f"Same-user auto-accept: host={host_device_id[:8]}, "
            f"viewer={viewer_device_id[:8]}"
        )
        return True, 'same_user_auto_accept'

    # Cross-user: verify OTP
    if not password:
        return False, 'password_required'

    if sm.verify_otp(host_device_id, password):
        return True, 'otp_verified'

    return False, 'invalid_password'


def generate_session_token(session_id: str, device_id: str,
                           user_id: Optional[str] = None) -> Optional[str]:
    """Generate JWT token for persistent remote desktop session.

    Reuses integrations/social/auth.py:127 generate_token_pair().
    Falls back to simple token if auth module unavailable.
    """
    try:
        from integrations.social.auth import generate_token_pair
        token_pair = generate_token_pair(
            user_id=int(user_id) if user_id and user_id.isdigit() else 0,
            extra_claims={
                'session_id': session_id,
                'device_id': device_id,
                'type': 'remote_desktop',
            },
        )
        return token_pair.get('access_token')
    except Exception as e:
        logger.debug(f"JWT auth unavailable, using simple token: {e}")
        import secrets
        return secrets.token_urlsafe(32)


# ── Notifications ───────────────────────────────────────────────

def notify_connection_request(source_user_id: str, target_user_id: str,
                              source_device_id: str) -> bool:
    """Send notification to target user about incoming connection request.

    Reuses NotificationService.create() from integrations/social/services.py.
    """
    try:
        from integrations.social.services import NotificationService
        from integrations.social.models import db_session
        with db_session() as db:
            NotificationService.create(
                db=db,
                recipient_id=int(target_user_id),
                sender_id=int(source_user_id) if source_user_id.isdigit() else None,
                notification_type='remote_desktop_request',
                content=f"Remote desktop connection request from device {source_device_id[:8]}...",
                data={
                    'source_device_id': source_device_id,
                    'source_user_id': source_user_id,
                    'action': 'remote_desktop_connect',
                },
            )
            db.commit()
        logger.info(
            f"Connection request notification sent: "
            f"user {source_user_id} → user {target_user_id}"
        )
        return True
    except Exception as e:
        logger.warning(f"Notification failed (service unavailable): {e}")
        return False


# ── Audit Logging ───────────────────────────────────────────────

def audit_session_event(event_type: str, session_id: str,
                        actor_id: str, detail: Optional[Dict] = None
                        ) -> Optional[Tuple[int, str]]:
    """Log remote desktop event to immutable audit log.

    Reuses security/immutable_audit_log.py log_event().
    """
    try:
        from security.immutable_audit_log import get_audit_log
        audit = get_audit_log()
        return audit.log_event(
            event_type=f'remote_desktop.{event_type}',
            actor_id=actor_id,
            action=f'remote_desktop_{event_type}',
            detail={**(detail or {}), 'session_id': session_id},
            target_id=session_id,
        )
    except Exception as e:
        logger.debug(f"Audit log unavailable: {e}")
        return None


# ── Input Classification ────────────────────────────────────────

def classify_remote_input(action: dict) -> str:
    """Classify remote input action for safety gating.

    Reuses security/action_classifier.py classify_action().
    Destructive actions (Alt+F4, Ctrl+Alt+Del, etc.) require EXPERT tier.

    Returns:
        'safe', 'destructive', or 'unknown'
    """
    # Build text description of the action for the classifier
    action_type = action.get('type', '')
    key = action.get('key', '')
    text = action.get('text', '')
    hotkey = action.get('hotkey', '')

    # Check for known destructive keyboard shortcuts
    DESTRUCTIVE_HOTKEYS = {
        'alt+f4', 'ctrl+alt+delete', 'ctrl+alt+del',
        'super+l', 'ctrl+shift+escape', 'alt+tab',
    }
    if hotkey and hotkey.lower() in DESTRUCTIVE_HOTKEYS:
        return 'destructive'

    # Mouse events are inherently safe (click, move, scroll, drag)
    SAFE_INPUT_TYPES = {'click', 'rightclick', 'doubleclick', 'middleclick',
                        'move', 'drag', 'scroll', 'mouse_move', 'mouse_down',
                        'mouse_up', 'cursor'}
    if action_type in SAFE_INPUT_TYPES:
        return 'safe'

    # Delegate to action_classifier for text/key-based classification
    try:
        from security.action_classifier import classify_action
        action_text = f"{action_type} {key} {text} {hotkey}".strip()
        if action_text:
            return classify_action(action_text)
    except Exception:
        pass

    # If classifier unavailable, default to unknown for key events, safe for others
    return 'unknown' if hotkey or action_type == 'key' else 'safe'


# ── DLP (Data Loss Prevention) ──────────────────────────────────

def scan_file_transfer(filename: str,
                       content_preview: Optional[str] = None
                       ) -> Tuple[bool, str]:
    """Scan file transfer for PII/sensitive data.

    Reuses security/dlp_engine.py check_outbound().

    Returns:
        (allowed, reason)
    """
    try:
        from security.dlp_engine import get_dlp_engine
        dlp = get_dlp_engine()

        # Scan filename
        allowed, reason = dlp.check_outbound(filename)
        if not allowed:
            return False, f"Filename blocked: {reason}"

        # Scan content preview if available
        if content_preview:
            allowed, reason = dlp.check_outbound(content_preview)
            if not allowed:
                return False, f"Content blocked: {reason}"

        return True, ''
    except Exception as e:
        logger.debug(f"DLP engine unavailable: {e}")
        return True, 'dlp_unavailable'


def scan_clipboard(text: str) -> Tuple[bool, str]:
    """Scan clipboard content before sending to remote device.

    Returns:
        (allowed, reason)
    """
    try:
        from security.dlp_engine import get_dlp_engine
        dlp = get_dlp_engine()
        return dlp.check_outbound(text)
    except Exception:
        return True, ''


# ── E2E Encryption ──────────────────────────────────────────────

def encrypt_frame(frame_bytes: bytes,
                  peer_x25519_public_hex: str) -> Optional[Dict[str, str]]:
    """Encrypt frame data for peer using X25519+AES-256-GCM.

    Reuses security/channel_encryption.py encrypt_for_peer().

    Returns:
        Envelope dict {eph, nonce, ct, v} or None if encryption unavailable.
    """
    try:
        from security.channel_encryption import encrypt_for_peer
        return encrypt_for_peer(frame_bytes, peer_x25519_public_hex)
    except Exception as e:
        logger.debug(f"Frame encryption failed: {e}")
        return None


def decrypt_frame(envelope: Dict[str, str]) -> Optional[bytes]:
    """Decrypt frame envelope from peer.

    Returns:
        Decrypted bytes or None.
    """
    try:
        from security.channel_encryption import decrypt_from_peer
        return decrypt_from_peer(envelope)
    except Exception as e:
        logger.debug(f"Frame decryption failed: {e}")
        return None


def encrypt_event(event: dict,
                  peer_x25519_public_hex: str) -> Optional[Dict[str, str]]:
    """Encrypt JSON event (input/clipboard/control) for peer.

    Reuses security/channel_encryption.py encrypt_json_for_peer().
    """
    try:
        from security.channel_encryption import encrypt_json_for_peer
        return encrypt_json_for_peer(event, peer_x25519_public_hex)
    except Exception as e:
        logger.debug(f"Event encryption failed: {e}")
        return None


def decrypt_event(envelope: Dict[str, str]) -> Optional[dict]:
    """Decrypt JSON event envelope from peer."""
    try:
        from security.channel_encryption import decrypt_json_from_peer
        return decrypt_json_from_peer(envelope)
    except Exception as e:
        logger.debug(f"Event decryption failed: {e}")
        return None
