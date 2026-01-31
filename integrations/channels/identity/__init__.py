"""
Identity Management for HevolveBot Integration.

This module provides identity management components including:
- Agent identity configuration
- Avatar management
- Sender identity mapping
- User preferences management
"""

from .agent_identity import AgentIdentity, IdentityManager
from .avatars import Avatar, AvatarType, AvatarManager
from .sender_mapping import (
    UserIdentity,
    ChannelIdentity,
    ChannelType,
    SenderIdentityMapper,
)
from .preferences import (
    UserPreferences,
    PreferenceManager,
    PreferenceValidator,
    PreferenceMigrator,
    ResponseStyle,
    Theme,
    get_preference_manager,
    SCHEMA_VERSION,
)

__all__ = [
    # Agent Identity
    "AgentIdentity",
    "IdentityManager",
    # Avatars
    "Avatar",
    "AvatarType",
    "AvatarManager",
    # Sender Mapping
    "UserIdentity",
    "ChannelIdentity",
    "ChannelType",
    "SenderIdentityMapper",
    # Preferences
    "UserPreferences",
    "PreferenceManager",
    "PreferenceValidator",
    "PreferenceMigrator",
    "ResponseStyle",
    "Theme",
    "get_preference_manager",
    "SCHEMA_VERSION",
]
