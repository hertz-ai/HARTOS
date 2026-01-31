"""
Agent Identity Management for HevolveBot Integration.

This module provides AgentIdentity and IdentityManager for managing
bot identities across different channels.
"""

from dataclasses import dataclass, field, asdict
from typing import Dict, List, Optional, Any
from datetime import datetime
import uuid
import json
import logging

logger = logging.getLogger(__name__)


@dataclass
class AgentIdentity:
    """Represents an agent's identity configuration."""

    id: str = field(default_factory=lambda: str(uuid.uuid4()))
    name: str = "HevolveBot"
    description: str = "An intelligent assistant"
    avatar_url: Optional[str] = None
    emoji: str = "🤖"
    personality: Dict[str, Any] = field(default_factory=lambda: {
        "tone": "friendly",
        "formality": "casual",
        "verbosity": "balanced"
    })
    capabilities: List[str] = field(default_factory=lambda: [
        "conversation",
        "task_execution",
        "information_retrieval"
    ])
    metadata: Dict[str, Any] = field(default_factory=dict)
    created_at: datetime = field(default_factory=datetime.utcnow)
    updated_at: datetime = field(default_factory=datetime.utcnow)

    def to_dict(self) -> Dict[str, Any]:
        """Convert identity to dictionary."""
        data = asdict(self)
        data['created_at'] = self.created_at.isoformat()
        data['updated_at'] = self.updated_at.isoformat()
        return data

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> 'AgentIdentity':
        """Create identity from dictionary."""
        if 'created_at' in data and isinstance(data['created_at'], str):
            data['created_at'] = datetime.fromisoformat(data['created_at'])
        if 'updated_at' in data and isinstance(data['updated_at'], str):
            data['updated_at'] = datetime.fromisoformat(data['updated_at'])
        return cls(**data)

    def update(self, **kwargs) -> 'AgentIdentity':
        """Update identity fields and return self."""
        for key, value in kwargs.items():
            if hasattr(self, key):
                setattr(self, key, value)
        self.updated_at = datetime.utcnow()
        return self

    def has_capability(self, capability: str) -> bool:
        """Check if agent has a specific capability."""
        return capability in self.capabilities

    def add_capability(self, capability: str) -> None:
        """Add a capability to the agent."""
        if capability not in self.capabilities:
            self.capabilities.append(capability)
            self.updated_at = datetime.utcnow()

    def remove_capability(self, capability: str) -> bool:
        """Remove a capability from the agent."""
        if capability in self.capabilities:
            self.capabilities.remove(capability)
            self.updated_at = datetime.utcnow()
            return True
        return False

    def get_personality_trait(self, trait: str, default: Any = None) -> Any:
        """Get a specific personality trait."""
        return self.personality.get(trait, default)

    def set_personality_trait(self, trait: str, value: Any) -> None:
        """Set a specific personality trait."""
        self.personality[trait] = value
        self.updated_at = datetime.utcnow()


class IdentityManager:
    """
    Manages agent identities across channels.

    Supports:
    - Default identity management
    - Per-channel identity customization
    - Identity persistence
    - Identity switching
    """

    def __init__(self, default_identity: Optional[AgentIdentity] = None):
        """
        Initialize the identity manager.

        Args:
            default_identity: Optional default identity to use
        """
        self._default_identity = default_identity or AgentIdentity()
        self._channel_identities: Dict[str, AgentIdentity] = {}
        self._identity_store: Dict[str, AgentIdentity] = {}
        self._active_identity_id: str = self._default_identity.id

        # Register default identity
        self._identity_store[self._default_identity.id] = self._default_identity

    @property
    def default_identity(self) -> AgentIdentity:
        """Get the default identity."""
        return self._default_identity

    def get_identity(self, identity_id: Optional[str] = None) -> Optional[AgentIdentity]:
        """
        Get an identity by ID.

        Args:
            identity_id: Optional ID of the identity to retrieve.
                        Returns active identity if None.

        Returns:
            The requested identity or None if not found
        """
        if identity_id is None:
            return self._identity_store.get(self._active_identity_id)
        return self._identity_store.get(identity_id)

    def set_identity(self, identity: AgentIdentity) -> None:
        """
        Register or update an identity.

        Args:
            identity: The identity to register
        """
        self._identity_store[identity.id] = identity
        logger.info(f"Identity registered: {identity.id} ({identity.name})")

    def set_active_identity(self, identity_id: str) -> bool:
        """
        Set the active identity.

        Args:
            identity_id: ID of the identity to activate

        Returns:
            True if successful, False if identity not found
        """
        if identity_id in self._identity_store:
            self._active_identity_id = identity_id
            logger.info(f"Active identity set to: {identity_id}")
            return True
        logger.warning(f"Identity not found: {identity_id}")
        return False

    def get_identity_for_channel(self, channel: str) -> AgentIdentity:
        """
        Get the identity configured for a specific channel.

        Args:
            channel: The channel identifier (e.g., 'discord', 'telegram', 'slack')

        Returns:
            The channel-specific identity or the default identity
        """
        if channel in self._channel_identities:
            return self._channel_identities[channel]
        return self._default_identity

    def set_identity_for_channel(self, channel: str, identity: AgentIdentity) -> None:
        """
        Set the identity for a specific channel.

        Args:
            channel: The channel identifier
            identity: The identity to use for this channel
        """
        self._channel_identities[channel] = identity
        # Also register in store if not already
        if identity.id not in self._identity_store:
            self._identity_store[identity.id] = identity
        logger.info(f"Identity set for channel {channel}: {identity.name}")

    def remove_channel_identity(self, channel: str) -> bool:
        """
        Remove channel-specific identity (will fall back to default).

        Args:
            channel: The channel identifier

        Returns:
            True if removed, False if no custom identity was set
        """
        if channel in self._channel_identities:
            del self._channel_identities[channel]
            logger.info(f"Channel identity removed for: {channel}")
            return True
        return False

    def list_identities(self) -> List[AgentIdentity]:
        """Get all registered identities."""
        return list(self._identity_store.values())

    def list_channel_identities(self) -> Dict[str, AgentIdentity]:
        """Get all channel-specific identities."""
        return dict(self._channel_identities)

    def create_identity(self, **kwargs) -> AgentIdentity:
        """
        Create and register a new identity.

        Args:
            **kwargs: Identity attributes

        Returns:
            The newly created identity
        """
        identity = AgentIdentity(**kwargs)
        self.set_identity(identity)
        return identity

    def delete_identity(self, identity_id: str) -> bool:
        """
        Delete an identity.

        Args:
            identity_id: ID of the identity to delete

        Returns:
            True if deleted, False if not found or is default
        """
        if identity_id == self._default_identity.id:
            logger.warning("Cannot delete default identity")
            return False

        if identity_id in self._identity_store:
            # Remove from channel mappings
            channels_to_remove = [
                ch for ch, ident in self._channel_identities.items()
                if ident.id == identity_id
            ]
            for channel in channels_to_remove:
                del self._channel_identities[channel]

            # Remove from store
            del self._identity_store[identity_id]

            # Reset active if needed
            if self._active_identity_id == identity_id:
                self._active_identity_id = self._default_identity.id

            logger.info(f"Identity deleted: {identity_id}")
            return True

        return False

    def clone_identity(self, identity_id: str, new_name: Optional[str] = None) -> Optional[AgentIdentity]:
        """
        Clone an existing identity.

        Args:
            identity_id: ID of the identity to clone
            new_name: Optional new name for the clone

        Returns:
            The cloned identity or None if source not found
        """
        source = self.get_identity(identity_id)
        if source is None:
            return None

        data = source.to_dict()
        data['id'] = str(uuid.uuid4())
        data['name'] = new_name or f"{source.name} (Copy)"
        data['created_at'] = datetime.utcnow()
        data['updated_at'] = datetime.utcnow()

        clone = AgentIdentity.from_dict(data)
        self.set_identity(clone)
        return clone

    def export_identities(self) -> str:
        """Export all identities as JSON."""
        data = {
            'default_identity_id': self._default_identity.id,
            'active_identity_id': self._active_identity_id,
            'identities': [ident.to_dict() for ident in self._identity_store.values()],
            'channel_mappings': {
                channel: ident.id
                for channel, ident in self._channel_identities.items()
            }
        }
        return json.dumps(data, indent=2)

    def import_identities(self, json_data: str) -> int:
        """
        Import identities from JSON.

        Args:
            json_data: JSON string containing identity data

        Returns:
            Number of identities imported
        """
        data = json.loads(json_data)
        count = 0

        for ident_data in data.get('identities', []):
            identity = AgentIdentity.from_dict(ident_data)
            self.set_identity(identity)
            count += 1

        # Restore channel mappings
        for channel, ident_id in data.get('channel_mappings', {}).items():
            if ident_id in self._identity_store:
                self._channel_identities[channel] = self._identity_store[ident_id]

        return count
