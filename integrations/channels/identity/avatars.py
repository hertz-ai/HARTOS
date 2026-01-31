"""
Avatar Management for HevolveBot Integration.

This module provides AvatarManager for managing bot avatars
across different channels and contexts.
"""

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Any, Callable
from datetime import datetime
from enum import Enum
import uuid
import hashlib
import logging

logger = logging.getLogger(__name__)


class AvatarType(Enum):
    """Types of avatar sources."""
    URL = "url"
    BASE64 = "base64"
    GRAVATAR = "gravatar"
    GENERATED = "generated"
    EMOJI = "emoji"
    INITIALS = "initials"


@dataclass
class Avatar:
    """Represents an avatar configuration."""

    id: str = field(default_factory=lambda: str(uuid.uuid4()))
    name: str = "Default Avatar"
    avatar_type: AvatarType = AvatarType.URL
    source: str = ""  # URL, base64 data, email for gravatar, etc.
    fallback_emoji: str = "🤖"
    fallback_initials: str = "MB"
    metadata: Dict[str, Any] = field(default_factory=dict)
    created_at: datetime = field(default_factory=datetime.utcnow)

    def to_dict(self) -> Dict[str, Any]:
        """Convert avatar to dictionary."""
        return {
            'id': self.id,
            'name': self.name,
            'avatar_type': self.avatar_type.value,
            'source': self.source,
            'fallback_emoji': self.fallback_emoji,
            'fallback_initials': self.fallback_initials,
            'metadata': self.metadata,
            'created_at': self.created_at.isoformat()
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> 'Avatar':
        """Create avatar from dictionary."""
        if 'avatar_type' in data:
            data['avatar_type'] = AvatarType(data['avatar_type'])
        if 'created_at' in data and isinstance(data['created_at'], str):
            data['created_at'] = datetime.fromisoformat(data['created_at'])
        return cls(**data)


class AvatarManager:
    """
    Manages avatars for agent identities.

    Supports:
    - Multiple avatar types (URL, base64, gravatar, generated)
    - Channel-specific avatars
    - Avatar generation
    - Fallback handling
    """

    # Default avatar generation services
    AVATAR_SERVICES = {
        'dicebear': 'https://api.dicebear.com/7.x/{style}/svg?seed={seed}',
        'robohash': 'https://robohash.org/{seed}?set=set{set}',
        'ui_avatars': 'https://ui-avatars.com/api/?name={name}&background={bg}&color={fg}',
        'gravatar': 'https://www.gravatar.com/avatar/{hash}?d={default}&s={size}'
    }

    DICEBEAR_STYLES = [
        'bottts', 'avataaars', 'identicon', 'pixel-art',
        'shapes', 'thumbs', 'lorelei', 'notionists'
    ]

    def __init__(self, default_avatar: Optional[Avatar] = None):
        """
        Initialize the avatar manager.

        Args:
            default_avatar: Optional default avatar
        """
        self._default_avatar = default_avatar or Avatar(
            name="Default Bot Avatar",
            avatar_type=AvatarType.EMOJI,
            source="🤖"
        )
        self._avatars: Dict[str, Avatar] = {self._default_avatar.id: self._default_avatar}
        self._identity_avatars: Dict[str, str] = {}  # identity_id -> avatar_id
        self._channel_avatars: Dict[str, str] = {}  # channel -> avatar_id
        self._generators: Dict[str, Callable[[str], str]] = {}

        # Register default generators
        self._register_default_generators()

    def _register_default_generators(self) -> None:
        """Register default avatar generators."""
        self._generators['dicebear_bottts'] = lambda seed: self._generate_dicebear(seed, 'bottts')
        self._generators['dicebear_avataaars'] = lambda seed: self._generate_dicebear(seed, 'avataaars')
        self._generators['dicebear_identicon'] = lambda seed: self._generate_dicebear(seed, 'identicon')
        self._generators['robohash'] = lambda seed: self._generate_robohash(seed)
        self._generators['initials'] = lambda name: self._generate_initials(name)

    def _generate_dicebear(self, seed: str, style: str = 'bottts') -> str:
        """Generate a DiceBear avatar URL."""
        return self.AVATAR_SERVICES['dicebear'].format(style=style, seed=seed)

    def _generate_robohash(self, seed: str, robot_set: int = 1) -> str:
        """Generate a RoboHash avatar URL."""
        return self.AVATAR_SERVICES['robohash'].format(seed=seed, set=robot_set)

    def _generate_initials(
        self,
        name: str,
        background: str = 'random',
        foreground: str = 'fff'
    ) -> str:
        """Generate an initials-based avatar URL."""
        return self.AVATAR_SERVICES['ui_avatars'].format(
            name=name.replace(' ', '+'),
            bg=background,
            fg=foreground
        )

    def _generate_gravatar(
        self,
        email: str,
        size: int = 200,
        default: str = 'identicon'
    ) -> str:
        """Generate a Gravatar URL."""
        email_hash = hashlib.md5(email.lower().strip().encode()).hexdigest()
        return self.AVATAR_SERVICES['gravatar'].format(
            hash=email_hash,
            default=default,
            size=size
        )

    def get_avatar(self, avatar_id: Optional[str] = None) -> Optional[Avatar]:
        """
        Get an avatar by ID.

        Args:
            avatar_id: Optional avatar ID. Returns default if None.

        Returns:
            The avatar or None if not found
        """
        if avatar_id is None:
            return self._default_avatar
        return self._avatars.get(avatar_id)

    def set_avatar(self, avatar: Avatar) -> None:
        """
        Register or update an avatar.

        Args:
            avatar: The avatar to register
        """
        self._avatars[avatar.id] = avatar
        logger.info(f"Avatar registered: {avatar.id} ({avatar.name})")

    def get_avatar_url(
        self,
        avatar_id: Optional[str] = None,
        identity_id: Optional[str] = None,
        channel: Optional[str] = None
    ) -> str:
        """
        Get the URL for an avatar, with fallback handling.

        Priority: avatar_id > channel > identity_id > default

        Args:
            avatar_id: Direct avatar ID to look up
            identity_id: Identity to get avatar for
            channel: Channel to get avatar for

        Returns:
            Avatar URL string (or emoji/initials for non-URL types)
        """
        avatar = None

        # Direct avatar lookup
        if avatar_id:
            avatar = self._avatars.get(avatar_id)

        # Channel-specific avatar
        if avatar is None and channel and channel in self._channel_avatars:
            avatar = self._avatars.get(self._channel_avatars[channel])

        # Identity-specific avatar
        if avatar is None and identity_id and identity_id in self._identity_avatars:
            avatar = self._avatars.get(self._identity_avatars[identity_id])

        # Fall back to default
        if avatar is None:
            avatar = self._default_avatar

        # Return appropriate representation
        if avatar.avatar_type == AvatarType.URL:
            return avatar.source
        elif avatar.avatar_type == AvatarType.EMOJI:
            return avatar.source or avatar.fallback_emoji
        elif avatar.avatar_type == AvatarType.INITIALS:
            return self._generate_initials(avatar.fallback_initials)
        elif avatar.avatar_type == AvatarType.GRAVATAR:
            return self._generate_gravatar(avatar.source)
        elif avatar.avatar_type == AvatarType.GENERATED:
            generator_name = avatar.metadata.get('generator', 'dicebear_bottts')
            if generator_name in self._generators:
                return self._generators[generator_name](avatar.source)
            return avatar.fallback_emoji
        else:
            return avatar.source or avatar.fallback_emoji

    def generate_avatar(
        self,
        seed: str,
        style: str = 'dicebear_bottts',
        name: Optional[str] = None
    ) -> Avatar:
        """
        Generate a new avatar using a specified style.

        Args:
            seed: Seed for avatar generation
            style: Avatar style/generator to use
            name: Optional name for the avatar

        Returns:
            The generated Avatar object
        """
        if style not in self._generators:
            logger.warning(f"Unknown avatar style: {style}, using default")
            style = 'dicebear_bottts'

        url = self._generators[style](seed)

        avatar = Avatar(
            name=name or f"Generated Avatar ({style})",
            avatar_type=AvatarType.GENERATED,
            source=seed,
            metadata={
                'generator': style,
                'url': url
            }
        )

        self.set_avatar(avatar)
        return avatar

    def set_avatar_for_identity(self, identity_id: str, avatar_id: str) -> bool:
        """
        Associate an avatar with an identity.

        Args:
            identity_id: The identity ID
            avatar_id: The avatar ID

        Returns:
            True if successful
        """
        if avatar_id not in self._avatars:
            logger.warning(f"Avatar not found: {avatar_id}")
            return False

        self._identity_avatars[identity_id] = avatar_id
        logger.info(f"Avatar {avatar_id} set for identity {identity_id}")
        return True

    def set_avatar_for_channel(self, channel: str, avatar_id: str) -> bool:
        """
        Set a channel-specific avatar.

        Args:
            channel: The channel identifier
            avatar_id: The avatar ID

        Returns:
            True if successful
        """
        if avatar_id not in self._avatars:
            logger.warning(f"Avatar not found: {avatar_id}")
            return False

        self._channel_avatars[channel] = avatar_id
        logger.info(f"Avatar {avatar_id} set for channel {channel}")
        return True

    def remove_channel_avatar(self, channel: str) -> bool:
        """Remove channel-specific avatar."""
        if channel in self._channel_avatars:
            del self._channel_avatars[channel]
            return True
        return False

    def remove_identity_avatar(self, identity_id: str) -> bool:
        """Remove identity-specific avatar."""
        if identity_id in self._identity_avatars:
            del self._identity_avatars[identity_id]
            return True
        return False

    def delete_avatar(self, avatar_id: str) -> bool:
        """
        Delete an avatar.

        Args:
            avatar_id: ID of the avatar to delete

        Returns:
            True if deleted
        """
        if avatar_id == self._default_avatar.id:
            logger.warning("Cannot delete default avatar")
            return False

        if avatar_id in self._avatars:
            # Clean up references
            self._identity_avatars = {
                k: v for k, v in self._identity_avatars.items()
                if v != avatar_id
            }
            self._channel_avatars = {
                k: v for k, v in self._channel_avatars.items()
                if v != avatar_id
            }

            del self._avatars[avatar_id]
            logger.info(f"Avatar deleted: {avatar_id}")
            return True

        return False

    def list_avatars(self) -> List[Avatar]:
        """Get all registered avatars."""
        return list(self._avatars.values())

    def list_generators(self) -> List[str]:
        """Get available avatar generator names."""
        return list(self._generators.keys())

    def register_generator(self, name: str, generator: Callable[[str], str]) -> None:
        """
        Register a custom avatar generator.

        Args:
            name: Generator name
            generator: Function that takes a seed and returns a URL
        """
        self._generators[name] = generator
        logger.info(f"Avatar generator registered: {name}")

    def create_avatar_from_url(self, url: str, name: Optional[str] = None) -> Avatar:
        """
        Create an avatar from a URL.

        Args:
            url: The avatar URL
            name: Optional name

        Returns:
            The created Avatar
        """
        avatar = Avatar(
            name=name or "URL Avatar",
            avatar_type=AvatarType.URL,
            source=url
        )
        self.set_avatar(avatar)
        return avatar

    def create_avatar_from_emoji(self, emoji: str, name: Optional[str] = None) -> Avatar:
        """
        Create an emoji-based avatar.

        Args:
            emoji: The emoji to use
            name: Optional name

        Returns:
            The created Avatar
        """
        avatar = Avatar(
            name=name or "Emoji Avatar",
            avatar_type=AvatarType.EMOJI,
            source=emoji,
            fallback_emoji=emoji
        )
        self.set_avatar(avatar)
        return avatar
