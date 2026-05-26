"""
DM Pairing Security System

Implements secure user-agent linking through pairing codes.
Users must pair their messaging accounts with agent accounts
before interacting with the system.

Features:
- Pairing code generation and validation
- Time-limited pairing codes
- Per-channel user authentication
- Session persistence
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import os
import secrets
import json
from datetime import datetime, timedelta
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional, Dict, Tuple, List, Any
from pathlib import Path

logger = logging.getLogger(__name__)


class PairingStatus(Enum):
    """Status of a pairing request."""
    PENDING = "pending"
    VERIFIED = "verified"
    EXPIRED = "expired"
    REJECTED = "rejected"


@dataclass
class PairingCode:
    """Represents a pairing code for user verification."""
    code: str
    user_id: int
    prompt_id: int
    created_at: datetime = field(default_factory=datetime.now)
    expires_at: Optional[datetime] = None
    status: PairingStatus = PairingStatus.PENDING

    def __post_init__(self):
        if self.expires_at is None:
            # Default 15 minute expiration
            self.expires_at = self.created_at + timedelta(minutes=15)

    @property
    def is_expired(self) -> bool:
        """Check if the pairing code has expired."""
        return datetime.now() > self.expires_at

    @property
    def is_valid(self) -> bool:
        """Check if the pairing code is still valid."""
        return self.status == PairingStatus.PENDING and not self.is_expired

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for serialization."""
        return {
            "code": self.code,
            "user_id": self.user_id,
            "prompt_id": self.prompt_id,
            "created_at": self.created_at.isoformat(),
            "expires_at": self.expires_at.isoformat() if self.expires_at else None,
            "status": self.status.value,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> PairingCode:
        """Create from dictionary."""
        return cls(
            code=data["code"],
            user_id=data["user_id"],
            prompt_id=data["prompt_id"],
            created_at=datetime.fromisoformat(data["created_at"]),
            expires_at=datetime.fromisoformat(data["expires_at"]) if data.get("expires_at") else None,
            status=PairingStatus(data["status"]),
        )


@dataclass
class PairedSession:
    """Represents a verified pairing between channel user and agent user."""
    channel: str
    sender_id: str
    user_id: int
    prompt_id: int
    paired_at: datetime = field(default_factory=datetime.now)
    last_active: datetime = field(default_factory=datetime.now)
    metadata: Dict[str, Any] = field(default_factory=dict)

    @property
    def session_key(self) -> Tuple[str, str]:
        """Get the session key."""
        return (self.channel, self.sender_id)

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for serialization."""
        return {
            "channel": self.channel,
            "sender_id": self.sender_id,
            "user_id": self.user_id,
            "prompt_id": self.prompt_id,
            "paired_at": self.paired_at.isoformat(),
            "last_active": self.last_active.isoformat(),
            "metadata": self.metadata,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> PairedSession:
        """Create from dictionary."""
        return cls(
            channel=data["channel"],
            sender_id=data["sender_id"],
            user_id=data["user_id"],
            prompt_id=data["prompt_id"],
            paired_at=datetime.fromisoformat(data["paired_at"]),
            last_active=datetime.fromisoformat(data["last_active"]),
            metadata=data.get("metadata", {}),
        )


class PairingManager:
    """
    Manages user pairing for secure channel access.

    Usage:
        manager = PairingManager()

        # Generate a pairing code for a user
        code = manager.generate_pairing_code(user_id=123, prompt_id=456)

        # User enters code in their DM
        session = manager.verify_pairing("telegram", "user123", code)

        # Check if user is paired
        if manager.is_paired("telegram", "user123"):
            user_id, prompt_id = manager.get_user_mapping("telegram", "user123")
    """

    def __init__(
        self,
        code_length: int = 6,
        code_expiry_minutes: int = 15,
        storage_path: Optional[str] = None,
        secret_key: Optional[str] = None,
    ):
        self.code_length = code_length
        self.code_expiry_minutes = code_expiry_minutes
        self.storage_path = storage_path or os.path.join(
            os.path.dirname(os.path.dirname(os.path.dirname(__file__))),
            "agent_data",
            "pairing_data.json"
        )
        self.secret_key = secret_key or os.getenv("PAIRING_SECRET_KEY", secrets.token_hex(32))

        # In-memory stores
        self._pending_codes: Dict[str, PairingCode] = {}  # code -> PairingCode
        self._paired_sessions: Dict[Tuple[str, str], PairedSession] = {}  # (channel, sender) -> Session

        # Load persisted data
        self._load_state()

    def generate_pairing_code(
        self,
        user_id: int,
        prompt_id: int,
        expiry_minutes: Optional[int] = None,
    ) -> str:
        """
        Generate a new pairing code for a user.

        Args:
            user_id: Agent user ID
            prompt_id: Agent prompt ID
            expiry_minutes: Optional custom expiry time

        Returns:
            The generated pairing code
        """
        # Generate secure random code
        code = self._generate_secure_code()

        # Calculate expiry
        expiry = timedelta(minutes=expiry_minutes or self.code_expiry_minutes)
        expires_at = datetime.now() + expiry

        # Create pairing code record
        pairing = PairingCode(
            code=code,
            user_id=user_id,
            prompt_id=prompt_id,
            expires_at=expires_at,
        )

        # Store in pending codes
        self._pending_codes[code] = pairing

        # Clean up expired codes
        self._cleanup_expired_codes()

        logger.info(f"Generated pairing code for user {user_id}: {code}")
        return code

    def verify_pairing(
        self,
        channel: str,
        sender_id: str,
        code: str,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> Optional[PairedSession]:
        """
        Verify a pairing code and create session.

        Args:
            channel: Channel name (e.g., "telegram", "discord")
            sender_id: Sender ID from the channel
            code: The pairing code entered by user
            metadata: Optional metadata to attach to session

        Returns:
            PairedSession if successful, None otherwise
        """
        code = code.upper().strip()

        # Check if code exists
        pairing = self._pending_codes.get(code)
        if not pairing:
            logger.warning(f"Invalid pairing code attempted: {code}")
            return None

        # Check if code is valid
        if not pairing.is_valid:
            logger.warning(f"Expired/used pairing code attempted: {code}")
            return None

        # Create paired session
        session = PairedSession(
            channel=channel,
            sender_id=sender_id,
            user_id=pairing.user_id,
            prompt_id=pairing.prompt_id,
            metadata=metadata or {},
        )

        # Mark code as used
        pairing.status = PairingStatus.VERIFIED
        del self._pending_codes[code]

        # Store session
        self._paired_sessions[session.session_key] = session

        # Persist state
        self._save_state()

        logger.info(f"Paired {channel}:{sender_id} with user {pairing.user_id}")
        return session

    def is_paired(self, channel: str, sender_id: str) -> bool:
        """Check if a channel user is paired."""
        return (channel, sender_id) in self._paired_sessions

    def get_user_mapping(
        self,
        channel: str,
        sender_id: str,
    ) -> Optional[Tuple[int, int]]:
        """
        Get the user mapping for a paired channel user.

        Returns:
            Tuple of (user_id, prompt_id) if paired, None otherwise
        """
        session = self._paired_sessions.get((channel, sender_id))
        if session:
            # Update last active
            session.last_active = datetime.now()
            return (session.user_id, session.prompt_id)
        return None

    def get_session(self, channel: str, sender_id: str) -> Optional[PairedSession]:
        """Get the full session for a paired user."""
        return self._paired_sessions.get((channel, sender_id))

    def unpair(self, channel: str, sender_id: str) -> bool:
        """
        Remove a pairing.

        Returns:
            True if unpaired, False if not found
        """
        key = (channel, sender_id)
        if key in self._paired_sessions:
            del self._paired_sessions[key]
            self._save_state()
            logger.info(f"Unpaired {channel}:{sender_id}")
            return True
        return False

    def unpair_user(self, user_id: int) -> int:
        """
        Remove all pairings for a user.

        Returns:
            Number of pairings removed
        """
        to_remove = [
            key for key, session in self._paired_sessions.items()
            if session.user_id == user_id
        ]

        for key in to_remove:
            del self._paired_sessions[key]

        if to_remove:
            self._save_state()
            logger.info(f"Unpaired {len(to_remove)} sessions for user {user_id}")

        return len(to_remove)

    def list_user_pairings(self, user_id: int) -> List[PairedSession]:
        """List all pairings for a user."""
        return [
            session for session in self._paired_sessions.values()
            if session.user_id == user_id
        ]

    def _generate_secure_code(self) -> str:
        """Generate a secure random pairing code."""
        # Generate alphanumeric code (excluding ambiguous chars)
        alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
        code = ''.join(secrets.choice(alphabet) for _ in range(self.code_length))

        # Add HMAC signature component for extra security
        signature = hmac.new(
            self.secret_key.encode(),
            code.encode(),
            hashlib.sha256
        ).hexdigest()[:4].upper()

        return f"{code}-{signature}"

    def _cleanup_expired_codes(self) -> None:
        """Remove expired pairing codes."""
        expired = [
            code for code, pairing in self._pending_codes.items()
            if pairing.is_expired
        ]
        for code in expired:
            del self._pending_codes[code]

    def _save_state(self) -> None:
        """Persist paired sessions to storage."""
        try:
            data = {
                "sessions": [
                    session.to_dict()
                    for session in self._paired_sessions.values()
                ],
                "pending_codes": [
                    code.to_dict()
                    for code in self._pending_codes.values()
                ],
            }

            os.makedirs(os.path.dirname(self.storage_path), exist_ok=True)
            with open(self.storage_path, 'w') as f:
                json.dump(data, f, indent=2)

        except Exception as e:
            logger.error(f"Failed to save pairing state: {e}")

    def _load_state(self) -> None:
        """Load persisted paired sessions."""
        try:
            if os.path.exists(self.storage_path):
                with open(self.storage_path, 'r') as f:
                    data = json.load(f)

                # Load sessions
                for session_data in data.get("sessions", []):
                    session = PairedSession.from_dict(session_data)
                    self._paired_sessions[session.session_key] = session

                # Load pending codes (skip expired)
                for code_data in data.get("pending_codes", []):
                    code = PairingCode.from_dict(code_data)
                    if code.is_valid:
                        self._pending_codes[code.code] = code

                logger.info(f"Loaded {len(self._paired_sessions)} paired sessions")

        except Exception as e:
            logger.error(f"Failed to load pairing state: {e}")


class PairingMiddleware:
    """
    Middleware that enforces pairing for channel messages.

    Usage:
        middleware = PairingMiddleware(pairing_manager)

        # In message handler:
        result = middleware.check_pairing(message)
        if not result.is_paired:
            await send_pairing_instructions(result.instructions)
            return

        # Process message with result.user_id, result.prompt_id
    """

    @dataclass
    class CheckResult:
        """Result of pairing check."""
        is_paired: bool
        user_id: Optional[int] = None
        prompt_id: Optional[int] = None
        instructions: Optional[str] = None

    def __init__(
        self,
        manager: PairingManager,
        require_pairing: bool = True,
        default_user_id: Optional[int] = None,
        default_prompt_id: Optional[int] = None,
    ):
        self.manager = manager
        self.require_pairing = require_pairing
        self.default_user_id = default_user_id
        self.default_prompt_id = default_prompt_id

    def check_pairing(self, channel: str, sender_id: str, text: str) -> CheckResult:
        """
        Check if sender is paired and handle pairing flow.

        Args:
            channel: Channel name
            sender_id: Sender ID
            text: Message text (to check for pairing code)

        Returns:
            CheckResult with pairing status
        """
        # Check if already paired
        mapping = self.manager.get_user_mapping(channel, sender_id)
        if mapping:
            return self.CheckResult(
                is_paired=True,
                user_id=mapping[0],
                prompt_id=mapping[1],
            )

        # Check if message contains pairing code
        if self._looks_like_pairing_code(text):
            session = self.manager.verify_pairing(channel, sender_id, text.strip())
            if session:
                return self.CheckResult(
                    is_paired=True,
                    user_id=session.user_id,
                    prompt_id=session.prompt_id,
                    instructions="Pairing successful! You can now chat with me.",
                )
            else:
                return self.CheckResult(
                    is_paired=False,
                    instructions="Invalid or expired pairing code. Please get a new code.",
                )

        # Not paired
        if self.require_pairing:
            return self.CheckResult(
                is_paired=False,
                instructions=(
                    "Welcome! To use this bot, you need to pair your account.\n\n"
                    "1. Go to the web interface and get a pairing code\n"
                    "2. Send the code here (e.g., ABC123-XYZ1)\n\n"
                    "This links your account securely."
                ),
            )
        else:
            # Use defaults if pairing not required
            return self.CheckResult(
                is_paired=True,
                user_id=self.default_user_id,
                prompt_id=self.default_prompt_id,
            )

    def _looks_like_pairing_code(self, text: str) -> bool:
        """Check if text looks like a pairing code."""
        text = text.strip().upper()
        # Code format: XXXXXX-YYYY (6 chars + hyphen + 4 chars)
        if len(text) >= 10 and '-' in text:
            parts = text.split('-')
            if len(parts) == 2 and all(p.isalnum() for p in parts):
                return True
        return False


# Singleton instance
_pairing_manager: Optional[PairingManager] = None


def get_pairing_manager() -> PairingManager:
    """Get or create the global pairing manager."""
    global _pairing_manager
    if _pairing_manager is None:
        _pairing_manager = PairingManager()
    return _pairing_manager
