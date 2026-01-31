"""
Sender Identity Mapping for HevolveBot Integration.

This module provides SenderIdentityMapper for mapping user identities
across different channels and platforms.
"""

from dataclasses import dataclass, field, asdict
from typing import Dict, List, Optional, Any, Set
from datetime import datetime
from enum import Enum
import uuid
import logging

logger = logging.getLogger(__name__)


class ChannelType(Enum):
    """Supported channel types."""
    DISCORD = "discord"
    TELEGRAM = "telegram"
    SLACK = "slack"
    TEAMS = "teams"
    WHATSAPP = "whatsapp"
    EMAIL = "email"
    WEB = "web"
    API = "api"
    CUSTOM = "custom"


@dataclass
class UserIdentity:
    """Represents a user's identity."""

    id: str = field(default_factory=lambda: str(uuid.uuid4()))
    username: str = ""
    display_name: str = ""
    email: Optional[str] = None
    avatar_url: Optional[str] = None
    verified: bool = False
    roles: List[str] = field(default_factory=list)
    metadata: Dict[str, Any] = field(default_factory=dict)
    created_at: datetime = field(default_factory=datetime.utcnow)
    last_seen: datetime = field(default_factory=datetime.utcnow)

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary."""
        data = asdict(self)
        data['created_at'] = self.created_at.isoformat()
        data['last_seen'] = self.last_seen.isoformat()
        return data

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> 'UserIdentity':
        """Create from dictionary."""
        if 'created_at' in data and isinstance(data['created_at'], str):
            data['created_at'] = datetime.fromisoformat(data['created_at'])
        if 'last_seen' in data and isinstance(data['last_seen'], str):
            data['last_seen'] = datetime.fromisoformat(data['last_seen'])
        return cls(**data)

    def update_last_seen(self) -> None:
        """Update the last seen timestamp."""
        self.last_seen = datetime.utcnow()

    def has_role(self, role: str) -> bool:
        """Check if user has a specific role."""
        return role in self.roles

    def add_role(self, role: str) -> None:
        """Add a role to the user."""
        if role not in self.roles:
            self.roles.append(role)

    def remove_role(self, role: str) -> bool:
        """Remove a role from the user."""
        if role in self.roles:
            self.roles.remove(role)
            return True
        return False


@dataclass
class ChannelIdentity:
    """Represents a user's identity on a specific channel."""

    id: str = field(default_factory=lambda: str(uuid.uuid4()))
    channel_type: ChannelType = ChannelType.CUSTOM
    channel_id: str = ""  # Platform-specific identifier
    channel_user_id: str = ""  # User's ID on that channel
    channel_username: str = ""  # User's username on that channel
    channel_display_name: str = ""
    channel_avatar_url: Optional[str] = None
    is_bot: bool = False
    is_verified: bool = False
    permissions: List[str] = field(default_factory=list)
    metadata: Dict[str, Any] = field(default_factory=dict)
    linked_user_id: Optional[str] = None  # Link to unified UserIdentity
    created_at: datetime = field(default_factory=datetime.utcnow)
    updated_at: datetime = field(default_factory=datetime.utcnow)

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary."""
        data = asdict(self)
        data['channel_type'] = self.channel_type.value
        data['created_at'] = self.created_at.isoformat()
        data['updated_at'] = self.updated_at.isoformat()
        return data

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> 'ChannelIdentity':
        """Create from dictionary."""
        if 'channel_type' in data and isinstance(data['channel_type'], str):
            data['channel_type'] = ChannelType(data['channel_type'])
        if 'created_at' in data and isinstance(data['created_at'], str):
            data['created_at'] = datetime.fromisoformat(data['created_at'])
        if 'updated_at' in data and isinstance(data['updated_at'], str):
            data['updated_at'] = datetime.fromisoformat(data['updated_at'])
        return cls(**data)

    def has_permission(self, permission: str) -> bool:
        """Check if channel identity has a permission."""
        return permission in self.permissions

    @property
    def channel_key(self) -> str:
        """Get unique key for this channel identity."""
        return f"{self.channel_type.value}:{self.channel_id}:{self.channel_user_id}"


class SenderIdentityMapper:
    """
    Maps sender identities across channels.

    Supports:
    - User identity management
    - Channel-specific identity tracking
    - Cross-channel identity linking
    - Identity unification
    """

    def __init__(self):
        """Initialize the sender identity mapper."""
        self._users: Dict[str, UserIdentity] = {}
        self._channel_identities: Dict[str, ChannelIdentity] = {}  # channel_key -> identity
        self._user_channels: Dict[str, Set[str]] = {}  # user_id -> set of channel_keys
        self._mappings: Dict[str, str] = {}  # channel_key -> user_id

    def map(
        self,
        channel_type: ChannelType,
        channel_id: str,
        channel_user_id: str,
        **kwargs
    ) -> ChannelIdentity:
        """
        Map a channel-specific identity.

        Args:
            channel_type: Type of channel
            channel_id: Channel/server identifier
            channel_user_id: User's ID on that channel
            **kwargs: Additional channel identity attributes

        Returns:
            The ChannelIdentity (created or existing)
        """
        channel_key = f"{channel_type.value}:{channel_id}:{channel_user_id}"

        if channel_key in self._channel_identities:
            # Update existing
            identity = self._channel_identities[channel_key]
            for key, value in kwargs.items():
                if hasattr(identity, key):
                    setattr(identity, key, value)
            identity.updated_at = datetime.utcnow()
            logger.debug(f"Updated channel identity: {channel_key}")
        else:
            # Create new
            identity = ChannelIdentity(
                channel_type=channel_type,
                channel_id=channel_id,
                channel_user_id=channel_user_id,
                **kwargs
            )
            self._channel_identities[channel_key] = identity
            logger.info(f"Created channel identity: {channel_key}")

        return identity

    def set_mapping(
        self,
        channel_identity: ChannelIdentity,
        user: UserIdentity
    ) -> None:
        """
        Link a channel identity to a unified user identity.

        Args:
            channel_identity: The channel-specific identity
            user: The unified user identity
        """
        # Ensure user is registered
        if user.id not in self._users:
            self._users[user.id] = user

        # Link channel to user
        channel_key = channel_identity.channel_key
        channel_identity.linked_user_id = user.id
        self._mappings[channel_key] = user.id

        # Track user's channels
        if user.id not in self._user_channels:
            self._user_channels[user.id] = set()
        self._user_channels[user.id].add(channel_key)

        logger.info(f"Mapped {channel_key} to user {user.id}")

    def get_user(self, user_id: str) -> Optional[UserIdentity]:
        """Get a user by ID."""
        return self._users.get(user_id)

    def get_channel_identity(
        self,
        channel_type: ChannelType,
        channel_id: str,
        channel_user_id: str
    ) -> Optional[ChannelIdentity]:
        """Get a channel identity."""
        channel_key = f"{channel_type.value}:{channel_id}:{channel_user_id}"
        return self._channel_identities.get(channel_key)

    def get_user_from_channel(
        self,
        channel_type: ChannelType,
        channel_id: str,
        channel_user_id: str
    ) -> Optional[UserIdentity]:
        """
        Get the unified user identity from a channel identity.

        Args:
            channel_type: Type of channel
            channel_id: Channel identifier
            channel_user_id: User's ID on that channel

        Returns:
            The linked UserIdentity or None
        """
        channel_key = f"{channel_type.value}:{channel_id}:{channel_user_id}"
        user_id = self._mappings.get(channel_key)
        if user_id:
            return self._users.get(user_id)
        return None

    def get_cross_channel(self, user_id: str) -> List[ChannelIdentity]:
        """
        Get all channel identities for a user.

        Args:
            user_id: The unified user ID

        Returns:
            List of all channel identities linked to this user
        """
        if user_id not in self._user_channels:
            return []

        identities = []
        for channel_key in self._user_channels[user_id]:
            if channel_key in self._channel_identities:
                identities.append(self._channel_identities[channel_key])

        return identities

    def get_cross_channel_by_channel(
        self,
        channel_type: ChannelType,
        channel_id: str,
        channel_user_id: str
    ) -> List[ChannelIdentity]:
        """
        Get all channel identities for a user, starting from one channel identity.

        Args:
            channel_type: Starting channel type
            channel_id: Starting channel ID
            channel_user_id: User's ID on that channel

        Returns:
            List of all channel identities for this user
        """
        channel_key = f"{channel_type.value}:{channel_id}:{channel_user_id}"
        user_id = self._mappings.get(channel_key)
        if not user_id:
            return []
        return self.get_cross_channel(user_id)

    def create_user(self, **kwargs) -> UserIdentity:
        """
        Create a new unified user identity.

        Args:
            **kwargs: User attributes

        Returns:
            The created UserIdentity
        """
        user = UserIdentity(**kwargs)
        self._users[user.id] = user
        self._user_channels[user.id] = set()
        logger.info(f"Created user: {user.id} ({user.username})")
        return user

    def delete_user(self, user_id: str) -> bool:
        """
        Delete a user and all their channel mappings.

        Args:
            user_id: ID of the user to delete

        Returns:
            True if deleted
        """
        if user_id not in self._users:
            return False

        # Remove channel mappings
        if user_id in self._user_channels:
            for channel_key in self._user_channels[user_id]:
                if channel_key in self._mappings:
                    del self._mappings[channel_key]
                if channel_key in self._channel_identities:
                    self._channel_identities[channel_key].linked_user_id = None
            del self._user_channels[user_id]

        # Remove user
        del self._users[user_id]
        logger.info(f"Deleted user: {user_id}")
        return True

    def unlink_channel(
        self,
        channel_type: ChannelType,
        channel_id: str,
        channel_user_id: str
    ) -> bool:
        """
        Unlink a channel identity from its user.

        Args:
            channel_type: Type of channel
            channel_id: Channel identifier
            channel_user_id: User's ID on that channel

        Returns:
            True if unlinked
        """
        channel_key = f"{channel_type.value}:{channel_id}:{channel_user_id}"

        if channel_key not in self._mappings:
            return False

        user_id = self._mappings[channel_key]
        del self._mappings[channel_key]

        if user_id in self._user_channels:
            self._user_channels[user_id].discard(channel_key)

        if channel_key in self._channel_identities:
            self._channel_identities[channel_key].linked_user_id = None

        logger.info(f"Unlinked channel: {channel_key}")
        return True

    def merge_users(self, primary_id: str, secondary_id: str) -> bool:
        """
        Merge two users, keeping the primary and merging secondary's channels.

        Args:
            primary_id: ID of the user to keep
            secondary_id: ID of the user to merge into primary

        Returns:
            True if merged successfully
        """
        if primary_id not in self._users or secondary_id not in self._users:
            return False

        if primary_id == secondary_id:
            return False

        # Move secondary's channels to primary
        if secondary_id in self._user_channels:
            for channel_key in self._user_channels[secondary_id]:
                self._mappings[channel_key] = primary_id
                if channel_key in self._channel_identities:
                    self._channel_identities[channel_key].linked_user_id = primary_id

            if primary_id not in self._user_channels:
                self._user_channels[primary_id] = set()
            self._user_channels[primary_id].update(self._user_channels[secondary_id])
            del self._user_channels[secondary_id]

        # Remove secondary user
        del self._users[secondary_id]
        logger.info(f"Merged user {secondary_id} into {primary_id}")
        return True

    def list_users(self) -> List[UserIdentity]:
        """Get all users."""
        return list(self._users.values())

    def list_channel_identities(
        self,
        channel_type: Optional[ChannelType] = None
    ) -> List[ChannelIdentity]:
        """
        Get channel identities, optionally filtered by type.

        Args:
            channel_type: Optional filter by channel type

        Returns:
            List of channel identities
        """
        identities = list(self._channel_identities.values())
        if channel_type:
            identities = [i for i in identities if i.channel_type == channel_type]
        return identities

    def find_users_by_email(self, email: str) -> List[UserIdentity]:
        """Find users by email address."""
        return [u for u in self._users.values() if u.email == email]

    def find_users_by_username(self, username: str) -> List[UserIdentity]:
        """Find users by username (partial match)."""
        username_lower = username.lower()
        return [
            u for u in self._users.values()
            if username_lower in u.username.lower()
        ]

    def get_user_channel_count(self, user_id: str) -> int:
        """Get the number of channels linked to a user."""
        return len(self._user_channels.get(user_id, set()))

    def export_mappings(self) -> Dict[str, Any]:
        """Export all mappings as a dictionary."""
        return {
            'users': [u.to_dict() for u in self._users.values()],
            'channel_identities': [c.to_dict() for c in self._channel_identities.values()],
            'mappings': dict(self._mappings)
        }

    def import_mappings(self, data: Dict[str, Any]) -> int:
        """
        Import mappings from a dictionary.

        Returns:
            Number of users imported
        """
        count = 0

        # Import users
        for user_data in data.get('users', []):
            user = UserIdentity.from_dict(user_data)
            self._users[user.id] = user
            self._user_channels[user.id] = set()
            count += 1

        # Import channel identities
        for channel_data in data.get('channel_identities', []):
            identity = ChannelIdentity.from_dict(channel_data)
            self._channel_identities[identity.channel_key] = identity

        # Restore mappings
        for channel_key, user_id in data.get('mappings', {}).items():
            self._mappings[channel_key] = user_id
            if user_id in self._user_channels:
                self._user_channels[user_id].add(channel_key)

        return count
