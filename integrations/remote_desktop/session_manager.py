"""
Remote Desktop Session Manager — Session lifecycle, OTP auth, multi-viewer support.

Session flow:
  1. Host starts hosting → SessionManager.generate_otp(device_id) → 6-char password
  2. Viewer connects → SessionManager.create_session(host_id, viewer_id, mode)
  3. Auth → SessionManager.authenticate_session(session_id, password)
  4. Connected → streaming begins
  5. Disconnect → SessionManager.disconnect_session(session_id)

Same-user devices auto-accept (no OTP needed), matching compute_mesh_service.py:398.
Cross-user requires OTP + explicit consent notification.
"""

import logging
import os
import secrets
import string
import threading
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Dict, List, Optional

logger = logging.getLogger('hevolve.remote_desktop')


# ── Enums ───────────────────────────────────────────────────────

class SessionMode(Enum):
    VIEW_ONLY = 'view_only'
    FULL_CONTROL = 'full_control'
    FILE_TRANSFER = 'file_transfer'


class SessionState(Enum):
    PENDING = 'pending'
    AUTHENTICATING = 'authenticating'
    CONNECTED = 'connected'
    DISCONNECTED = 'disconnected'


# ── Data Classes ────────────────────────────────────────────────

@dataclass
class RemoteSession:
    session_id: str
    host_device_id: str
    host_user_id: Optional[str]
    mode: SessionMode
    state: SessionState = SessionState.PENDING
    viewers: List[dict] = field(default_factory=list)
    created_at: float = field(default_factory=time.time)
    connected_at: Optional[float] = None
    disconnected_at: Optional[float] = None
    transport_tier: Optional[str] = None

    def add_viewer(self, device_id: str, user_id: Optional[str] = None) -> None:
        if not any(v['device_id'] == device_id for v in self.viewers):
            self.viewers.append({
                'device_id': device_id,
                'user_id': user_id,
                'joined_at': time.time(),
            })

    def remove_viewer(self, device_id: str) -> None:
        self.viewers = [v for v in self.viewers if v['device_id'] != device_id]

    def to_dict(self) -> dict:
        return {
            'session_id': self.session_id,
            'host_device_id': self.host_device_id,
            'host_user_id': self.host_user_id,
            'mode': self.mode.value,
            'state': self.state.value,
            'viewers': self.viewers,
            'created_at': self.created_at,
            'connected_at': self.connected_at,
            'disconnected_at': self.disconnected_at,
            'transport_tier': self.transport_tier,
            'duration_seconds': self._duration(),
        }

    def _duration(self) -> Optional[float]:
        if self.connected_at:
            end = self.disconnected_at or time.time()
            return round(end - self.connected_at, 1)
        return None


# ── Session Manager (singleton) ─────────────────────────────────

class SessionManager:
    """Manages remote desktop sessions, OTP passwords, and multi-viewer support."""

    OTP_LENGTH = 6
    OTP_CHARS = string.ascii_lowercase + string.digits  # a-z, 0-9
    OTP_EXPIRY_SECONDS = 300  # 5 minutes
    MAX_SESSIONS_PER_HOST = 5
    SESSION_TIMEOUT_SECONDS = 86400  # 24 hours

    def __init__(self):
        self._sessions: Dict[str, RemoteSession] = {}
        self._otps: Dict[str, dict] = {}  # device_id → {password, created_at}
        self._lock = threading.Lock()
        logger.info("SessionManager initialized")

    def generate_otp(self, device_id: str) -> str:
        """Generate one-time password for a hosting device.

        Returns:
            6-char alphanumeric password (e.g., 'a8f2k9')
        """
        password = ''.join(secrets.choice(self.OTP_CHARS) for _ in range(self.OTP_LENGTH))
        with self._lock:
            self._otps[device_id] = {
                'password': password,
                'created_at': time.time(),
                'used': False,
            }
        logger.info(f"OTP generated for device {device_id[:8]}...")
        return password

    def verify_otp(self, device_id: str, password: str) -> bool:
        """Verify one-time password (single-use, expires after OTP_EXPIRY_SECONDS).

        Returns:
            True if password matches and hasn't been used/expired.
        """
        with self._lock:
            otp_entry = self._otps.get(device_id)
            if not otp_entry:
                return False
            if otp_entry['used']:
                return False
            if time.time() - otp_entry['created_at'] > self.OTP_EXPIRY_SECONDS:
                del self._otps[device_id]
                return False
            if otp_entry['password'] != password:
                return False
            # Mark as used (single-use)
            otp_entry['used'] = True
            return True

    def is_same_user(self, host_user_id: Optional[str],
                     viewer_user_id: Optional[str]) -> bool:
        """Check if host and viewer belong to same user.

        Same-user devices auto-accept (no OTP needed),
        matching compute_mesh_service.py:398 auto_accept pattern.
        """
        if not host_user_id or not viewer_user_id:
            return False
        return str(host_user_id) == str(viewer_user_id)

    def create_session(self, host_device_id: str, viewer_device_id: str,
                       mode: SessionMode,
                       host_user_id: Optional[str] = None,
                       viewer_user_id: Optional[str] = None) -> RemoteSession:
        """Create a new remote desktop session.

        Args:
            host_device_id: Device ID of the host
            viewer_device_id: Device ID of the viewer
            mode: Session mode (VIEW_ONLY, FULL_CONTROL, FILE_TRANSFER)
            host_user_id: User ID of the host device owner
            viewer_user_id: User ID of the viewer

        Returns:
            RemoteSession instance
        """
        session_id = secrets.token_hex(8)

        # Check session limit per host
        with self._lock:
            active_count = sum(
                1 for s in self._sessions.values()
                if s.host_device_id == host_device_id
                and s.state in (SessionState.PENDING, SessionState.AUTHENTICATING,
                                SessionState.CONNECTED)
            )
            if active_count >= self.MAX_SESSIONS_PER_HOST:
                raise ValueError(
                    f"Host {host_device_id[:8]} has {active_count} active sessions "
                    f"(max {self.MAX_SESSIONS_PER_HOST})"
                )

        session = RemoteSession(
            session_id=session_id,
            host_device_id=host_device_id,
            host_user_id=host_user_id,
            mode=mode,
            state=SessionState.PENDING,
        )
        session.add_viewer(viewer_device_id, viewer_user_id)

        # Same-user auto-accept (compute_mesh_service.py:398 pattern)
        if self.is_same_user(host_user_id, viewer_user_id):
            session.state = SessionState.CONNECTED
            session.connected_at = time.time()
            logger.info(
                f"Session {session_id}: same-user auto-accept "
                f"(host={host_device_id[:8]}, viewer={viewer_device_id[:8]})"
            )
        else:
            session.state = SessionState.AUTHENTICATING
            logger.info(
                f"Session {session_id}: cross-user, OTP required "
                f"(host_user={host_user_id}, viewer_user={viewer_user_id})"
            )

        with self._lock:
            self._sessions[session_id] = session
        return session

    def authenticate_session(self, session_id: str, password: str) -> bool:
        """Authenticate a pending session with OTP.

        Returns:
            True if session authenticated successfully.
        """
        with self._lock:
            session = self._sessions.get(session_id)
        if not session:
            return False
        if session.state != SessionState.AUTHENTICATING:
            return False

        if self.verify_otp(session.host_device_id, password):
            session.state = SessionState.CONNECTED
            session.connected_at = time.time()
            logger.info(f"Session {session_id} authenticated")
            return True

        logger.warning(f"Session {session_id} auth failed")
        return False

    def add_viewer(self, session_id: str, device_id: str,
                   user_id: Optional[str] = None) -> bool:
        """Add a viewer to an existing session (multi-viewer support).

        Returns:
            True if viewer added successfully.
        """
        with self._lock:
            session = self._sessions.get(session_id)
        if not session or session.state != SessionState.CONNECTED:
            return False
        session.add_viewer(device_id, user_id)
        logger.info(f"Viewer {device_id[:8]} added to session {session_id}")
        return True

    def disconnect_session(self, session_id: str) -> bool:
        """Disconnect a session.

        Returns:
            True if session was found and disconnected.
        """
        with self._lock:
            session = self._sessions.get(session_id)
        if not session:
            return False
        if session.state == SessionState.DISCONNECTED:
            return False

        session.state = SessionState.DISCONNECTED
        session.disconnected_at = time.time()
        logger.info(f"Session {session_id} disconnected")
        return True

    def get_session(self, session_id: str) -> Optional[RemoteSession]:
        """Get session by ID."""
        with self._lock:
            return self._sessions.get(session_id)

    def get_active_sessions(self) -> List[RemoteSession]:
        """Get all active (non-disconnected) sessions."""
        with self._lock:
            return [
                s for s in self._sessions.values()
                if s.state != SessionState.DISCONNECTED
            ]

    def get_sessions_for_device(self, device_id: str) -> List[RemoteSession]:
        """Get all sessions where device is host or viewer."""
        with self._lock:
            results = []
            for s in self._sessions.values():
                if s.host_device_id == device_id:
                    results.append(s)
                elif any(v['device_id'] == device_id for v in s.viewers):
                    results.append(s)
            return results

    def cleanup_stale(self) -> int:
        """Remove expired sessions. Returns count of removed sessions."""
        cutoff = time.time() - self.SESSION_TIMEOUT_SECONDS
        removed = 0
        with self._lock:
            stale_ids = [
                sid for sid, s in self._sessions.items()
                if s.created_at < cutoff or (
                    s.state == SessionState.DISCONNECTED
                    and s.disconnected_at
                    and s.disconnected_at < cutoff
                )
            ]
            for sid in stale_ids:
                del self._sessions[sid]
                removed += 1

            # Clean expired OTPs
            expired_devices = [
                dev for dev, otp in self._otps.items()
                if time.time() - otp['created_at'] > self.OTP_EXPIRY_SECONDS
            ]
            for dev in expired_devices:
                del self._otps[dev]

        if removed:
            logger.info(f"Cleaned up {removed} stale sessions")
        return removed


# ── Singleton ───────────────────────────────────────────────────

_session_manager: Optional[SessionManager] = None


def get_session_manager() -> SessionManager:
    """Get or create the singleton SessionManager."""
    global _session_manager
    if _session_manager is None:
        _session_manager = SessionManager()
    return _session_manager
