"""
Device Identity — Deterministic device IDs for remote desktop sessions.

Reuses compute_mesh_service.py:116 pattern: device_id = SHA256(public_key)[:16].
Device IDs are user-scoped (tied to user_id), displayed in 3-group format (847-291-053).
"""

import hashlib
import logging
import os
import platform
import uuid
from typing import Optional

logger = logging.getLogger('hevolve.remote_desktop')

# ── Device ID cache ─────────────────────────────────────────────
_cached_device_id: Optional[str] = None


def _resolve_key_dir() -> str:
    """Resolve key directory — same logic as compute_mesh_service.py."""
    data_dir = os.environ.get('HEVOLVE_DATA_DIR', '')
    if data_dir:
        return os.path.join(data_dir, 'mesh', 'keys')
    # Fallback: agent_data in project root or user home
    for candidate in [
        os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(
            os.path.abspath(__file__)))), 'agent_data'),
        os.path.join(os.path.expanduser('~'), 'Documents', 'Nunba', 'data', 'agent_data'),
    ]:
        if os.path.isdir(candidate):
            return candidate
    return os.path.join(os.path.expanduser('~'), '.hart', 'keys')


def _generate_machine_fingerprint() -> str:
    """Generate stable machine fingerprint when no public key file exists.

    Uses platform + hostname + MAC address — deterministic per machine.
    """
    components = [
        platform.node(),
        platform.machine(),
        platform.system(),
        str(uuid.getnode()),  # MAC address as int
    ]
    return '|'.join(components)


def get_device_id() -> str:
    """Get this device's 16-char hex ID.

    Priority:
      1. Public key file (compute_mesh_service.py:116 pattern)
      2. Machine fingerprint fallback (deterministic)

    Returns:
        16-character hex string (e.g., '847291053def3a21')
    """
    global _cached_device_id
    if _cached_device_id is not None:
        return _cached_device_id

    key_dir = _resolve_key_dir()

    # Try public key file first (same as compute_mesh_service.py:113-116)
    for key_filename in ('public.key', 'node_public.key', 'node_x25519_public.key'):
        key_path = os.path.join(key_dir, key_filename)
        if os.path.exists(key_path):
            try:
                with open(key_path, 'r') as f:
                    pub_key = f.read().strip()
                if pub_key:
                    _cached_device_id = hashlib.sha256(pub_key.encode()).hexdigest()[:16]
                    logger.info(f"Device ID from {key_filename}: {_cached_device_id}")
                    return _cached_device_id
            except (OSError, UnicodeDecodeError):
                continue

    # Fallback: machine fingerprint (deterministic per machine)
    fingerprint = _generate_machine_fingerprint()
    _cached_device_id = hashlib.sha256(fingerprint.encode()).hexdigest()[:16]
    logger.info(f"Device ID from fingerprint: {_cached_device_id}")
    return _cached_device_id


def format_device_id(device_id: str) -> str:
    """Format 16-char hex ID for display: '847291053def3a21' → '847-291-053'.

    Uses first 9 hex chars split into groups of 3 (like AnyDesk's numeric IDs).
    """
    digits = device_id[:9]
    return f"{digits[:3]}-{digits[3:6]}-{digits[6:9]}"


def parse_device_id(formatted: str) -> str:
    """Parse display-formatted ID back to lookup key.

    '847-291-053' → '847291053' (prefix match against full 16-char IDs).
    """
    return formatted.replace('-', '').replace(' ', '').lower()


def get_user_device_id(user_id: str) -> str:
    """Get device ID scoped to a user (for cross-device lookup).

    Combines machine device_id with user_id for user-scoped identity.
    This matches the proximity_service.py:41 pattern where device_id
    is used for cross-device dedup per user.
    """
    raw_device = get_device_id()
    return hashlib.sha256(f"{user_id}:{raw_device}".encode()).hexdigest()[:16]


def register_device(user_id: str, device_id: Optional[str] = None) -> dict:
    """Register this device for a user (enables cross-device discovery).

    Reuses PeerNode model from integrations/social/models.py:580
    (node_operator_id FK→User).

    Returns:
        {'device_id': str, 'user_id': str, 'registered': bool}
    """
    dev_id = device_id or get_device_id()
    try:
        from integrations.social.models import db_session, PeerNode
        with db_session() as db:
            existing = db.query(PeerNode).filter(
                PeerNode.node_id == dev_id,
            ).first()
            if existing:
                existing.node_operator_id = int(user_id) if user_id.isdigit() else None
                existing.status = 'active'
            else:
                node = PeerNode(
                    node_id=dev_id,
                    url=f'localhost:{os.environ.get("HART_PORT", "6777")}',
                    node_operator_id=int(user_id) if user_id.isdigit() else None,
                    status='active',
                )
                db.add(node)
            db.commit()
        logger.info(f"Device {dev_id} registered for user {user_id}")
        return {'device_id': dev_id, 'user_id': user_id, 'registered': True}
    except Exception as e:
        logger.warning(f"Device registration failed (DB unavailable): {e}")
        return {'device_id': dev_id, 'user_id': user_id, 'registered': False}


def discover_user_devices(user_id: str) -> list:
    """Find all devices registered to a user.

    Queries PeerNode.node_operator_id (models.py:580) — same as
    compute_mesh_service.py:131 discover_peers().

    Returns:
        List of {'device_id': str, 'url': str, 'status': str, 'last_seen': str}
    """
    try:
        from integrations.social.models import db_session, PeerNode
        with db_session() as db:
            nodes = db.query(PeerNode).filter(
                PeerNode.node_operator_id == (int(user_id) if user_id.isdigit() else -1),
                PeerNode.status == 'active',
            ).all()
            return [
                {
                    'device_id': n.node_id,
                    'url': n.url,
                    'status': n.status,
                    'last_seen': str(n.last_seen) if n.last_seen else None,
                }
                for n in nodes
            ]
    except Exception as e:
        logger.warning(f"Device discovery failed (DB unavailable): {e}")
        return []
