"""
User Preferences Management for HevolveBot Integration.

This module provides UserPreferences and PreferenceManager for managing
user preferences across different channels.
"""

from dataclasses import dataclass, field, asdict
from typing import Dict, List, Optional, Any, Callable, Set
from datetime import datetime
from enum import Enum
from pathlib import Path
import uuid
import json
import logging
import os

logger = logging.getLogger(__name__)


# Default container-compatible paths for Docker deployment
DEFAULT_PREFERENCES_PATH = os.environ.get(
    'PREFERENCES_STORAGE_PATH',
    '/app/data/preferences.json'
)

# Fallback for local development
if not os.path.exists(os.path.dirname(DEFAULT_PREFERENCES_PATH)):
    DEFAULT_PREFERENCES_PATH = os.path.join(
        os.path.dirname(os.path.abspath(__file__)),
        '..', '..', '..', 'data', 'preferences.json'
    )


class ResponseStyle(Enum):
    """Available response styles."""
    CONCISE = "concise"
    BALANCED = "balanced"
    DETAILED = "detailed"
    TECHNICAL = "technical"
    CASUAL = "casual"


class Theme(Enum):
    """Available UI themes."""
    AUTO = "auto"
    LIGHT = "light"
    DARK = "dark"


# Schema version for migrations
SCHEMA_VERSION = 1


@dataclass
class UserPreferences:
    """
    Represents a user's preferences configuration.

    Attributes:
        id: Unique identifier for this preferences instance
        user_id: The user this preferences belong to
        language: Preferred language code (e.g., "en", "es", "fr")
        timezone: Preferred timezone (e.g., "UTC", "America/New_York")
        model: Preferred AI model (None uses default)
        response_style: Preferred response style
        notifications: Whether to receive notifications
        theme: Preferred UI theme
        channel_overrides: Per-channel preference overrides
        metadata: Additional custom preferences
        schema_version: Schema version for migrations
        created_at: When preferences were created
        updated_at: When preferences were last updated
    """

    id: str = field(default_factory=lambda: str(uuid.uuid4()))
    user_id: str = ""
    language: str = "en"
    timezone: str = "UTC"
    model: Optional[str] = None
    response_style: str = "balanced"
    notifications: bool = True
    theme: str = "auto"
    channel_overrides: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    metadata: Dict[str, Any] = field(default_factory=dict)
    schema_version: int = SCHEMA_VERSION
    created_at: datetime = field(default_factory=datetime.utcnow)
    updated_at: datetime = field(default_factory=datetime.utcnow)

    def to_dict(self) -> Dict[str, Any]:
        """Convert preferences to dictionary."""
        data = asdict(self)
        data['created_at'] = self.created_at.isoformat()
        data['updated_at'] = self.updated_at.isoformat()
        return data

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> 'UserPreferences':
        """Create preferences from dictionary."""
        if 'created_at' in data and isinstance(data['created_at'], str):
            data['created_at'] = datetime.fromisoformat(data['created_at'])
        if 'updated_at' in data and isinstance(data['updated_at'], str):
            data['updated_at'] = datetime.fromisoformat(data['updated_at'])
        # Handle missing fields for backwards compatibility
        if 'channel_overrides' not in data:
            data['channel_overrides'] = {}
        if 'schema_version' not in data:
            data['schema_version'] = 1
        return cls(**data)

    def update(self, **kwargs) -> 'UserPreferences':
        """Update preferences fields and return self."""
        for key, value in kwargs.items():
            if hasattr(self, key) and key not in ('id', 'user_id', 'created_at'):
                setattr(self, key, value)
        self.updated_at = datetime.utcnow()
        return self

    def get_effective_preference(self, key: str, channel: Optional[str] = None, default: Any = None) -> Any:
        """
        Get effective preference value, considering channel overrides.

        Args:
            key: The preference key to retrieve
            channel: Optional channel to check for overrides
            default: Default value if not found

        Returns:
            The effective preference value
        """
        # Check channel override first
        if channel and channel in self.channel_overrides:
            override = self.channel_overrides[channel]
            if key in override:
                return override[key]

        # Fall back to base preference
        if hasattr(self, key):
            return getattr(self, key)

        # Check metadata
        if key in self.metadata:
            return self.metadata[key]

        return default

    def set_channel_override(self, channel: str, key: str, value: Any) -> None:
        """
        Set a channel-specific preference override.

        Args:
            channel: The channel identifier
            key: The preference key
            value: The override value
        """
        if channel not in self.channel_overrides:
            self.channel_overrides[channel] = {}
        self.channel_overrides[channel][key] = value
        self.updated_at = datetime.utcnow()

    def remove_channel_override(self, channel: str, key: Optional[str] = None) -> bool:
        """
        Remove channel override(s).

        Args:
            channel: The channel identifier
            key: Optional specific key to remove. If None, removes all overrides for channel.

        Returns:
            True if removed, False if not found
        """
        if channel not in self.channel_overrides:
            return False

        if key is None:
            del self.channel_overrides[channel]
            self.updated_at = datetime.utcnow()
            return True

        if key in self.channel_overrides[channel]:
            del self.channel_overrides[channel][key]
            if not self.channel_overrides[channel]:
                del self.channel_overrides[channel]
            self.updated_at = datetime.utcnow()
            return True

        return False

    def get_channel_overrides(self, channel: str) -> Dict[str, Any]:
        """Get all overrides for a specific channel."""
        return dict(self.channel_overrides.get(channel, {}))


class PreferenceValidator:
    """Validates preference values."""

    # Valid language codes (subset of ISO 639-1)
    VALID_LANGUAGES: Set[str] = {
        'en', 'es', 'fr', 'de', 'it', 'pt', 'ru', 'zh', 'ja', 'ko',
        'ar', 'hi', 'nl', 'pl', 'tr', 'vi', 'th', 'id', 'ms', 'sv'
    }

    # Valid response styles
    VALID_RESPONSE_STYLES: Set[str] = {s.value for s in ResponseStyle}

    # Valid themes
    VALID_THEMES: Set[str] = {t.value for t in Theme}

    # Common timezone prefixes (simplified validation)
    VALID_TIMEZONE_PREFIXES: Set[str] = {
        'UTC', 'GMT', 'America/', 'Europe/', 'Asia/', 'Africa/',
        'Australia/', 'Pacific/', 'Atlantic/', 'Indian/', 'Etc/'
    }

    @classmethod
    def validate(cls, prefs: UserPreferences) -> List[str]:
        """
        Validate preferences and return list of errors.

        Args:
            prefs: The preferences to validate

        Returns:
            List of validation error messages (empty if valid)
        """
        errors = []

        # Validate language
        if prefs.language not in cls.VALID_LANGUAGES:
            errors.append(f"Invalid language code: {prefs.language}")

        # Validate response style
        if prefs.response_style not in cls.VALID_RESPONSE_STYLES:
            errors.append(f"Invalid response style: {prefs.response_style}")

        # Validate theme
        if prefs.theme not in cls.VALID_THEMES:
            errors.append(f"Invalid theme: {prefs.theme}")

        # Validate timezone (simplified check)
        if not cls._validate_timezone(prefs.timezone):
            errors.append(f"Invalid timezone: {prefs.timezone}")

        # Validate channel overrides
        for channel, overrides in prefs.channel_overrides.items():
            if 'language' in overrides and overrides['language'] not in cls.VALID_LANGUAGES:
                errors.append(f"Invalid language in {channel} override: {overrides['language']}")
            if 'response_style' in overrides and overrides['response_style'] not in cls.VALID_RESPONSE_STYLES:
                errors.append(f"Invalid response_style in {channel} override: {overrides['response_style']}")
            if 'theme' in overrides and overrides['theme'] not in cls.VALID_THEMES:
                errors.append(f"Invalid theme in {channel} override: {overrides['theme']}")

        return errors

    @classmethod
    def _validate_timezone(cls, timezone: str) -> bool:
        """Check if timezone looks valid."""
        if timezone in ('UTC', 'GMT'):
            return True
        for prefix in cls.VALID_TIMEZONE_PREFIXES:
            if timezone.startswith(prefix):
                return True
        return False

    @classmethod
    def is_valid(cls, prefs: UserPreferences) -> bool:
        """Check if preferences are valid."""
        return len(cls.validate(prefs)) == 0


class PreferenceMigrator:
    """Handles schema migrations for preferences."""

    # Migration functions: (from_version, to_version) -> migration_func
    _migrations: Dict[tuple, Callable[[Dict[str, Any]], Dict[str, Any]]] = {}

    @classmethod
    def register_migration(
        cls,
        from_version: int,
        to_version: int,
        migration_func: Callable[[Dict[str, Any]], Dict[str, Any]]
    ) -> None:
        """
        Register a migration function.

        Args:
            from_version: Source schema version
            to_version: Target schema version
            migration_func: Function that transforms the data
        """
        cls._migrations[(from_version, to_version)] = migration_func

    @classmethod
    def migrate(cls, data: Dict[str, Any], target_version: int = SCHEMA_VERSION) -> Dict[str, Any]:
        """
        Migrate preferences data to target version.

        Args:
            data: The preferences data to migrate
            target_version: The target schema version

        Returns:
            Migrated data
        """
        current_version = data.get('schema_version', 1)

        while current_version < target_version:
            next_version = current_version + 1
            migration_key = (current_version, next_version)

            if migration_key in cls._migrations:
                data = cls._migrations[migration_key](data)
                data['schema_version'] = next_version
                logger.info(f"Migrated preferences from v{current_version} to v{next_version}")
            else:
                # No migration needed, just update version
                data['schema_version'] = next_version

            current_version = next_version

        # Ensure schema_version is always present in output
        if 'schema_version' not in data:
            data['schema_version'] = target_version

        return data


class PreferenceManager:
    """
    Manages user preferences across channels.

    Supports:
    - Preference persistence (file-based or in-memory)
    - Default preferences configuration
    - Channel-specific preference overrides
    - Preference validation
    - Schema migrations
    """

    def __init__(
        self,
        storage_path: Optional[str] = None,
        default_preferences: Optional[Dict[str, Any]] = None,
        auto_persist: bool = True,
        validate_on_set: bool = True
    ):
        """
        Initialize the preference manager.

        Args:
            storage_path: Path to preferences storage file (container-compatible)
            default_preferences: Default preference values for new users
            auto_persist: Automatically persist on changes
            validate_on_set: Validate preferences when setting
        """
        self._storage_path = storage_path or DEFAULT_PREFERENCES_PATH
        self._default_preferences = default_preferences or {}
        self._auto_persist = auto_persist
        self._validate_on_set = validate_on_set
        self._preferences: Dict[str, UserPreferences] = {}
        self._dirty = False

        # Ensure storage directory exists
        self._ensure_storage_dir()

        # Load existing preferences
        self._load()

    def _ensure_storage_dir(self) -> None:
        """Ensure the storage directory exists."""
        storage_dir = os.path.dirname(self._storage_path)
        if storage_dir and not os.path.exists(storage_dir):
            try:
                os.makedirs(storage_dir, exist_ok=True)
                logger.info(f"Created preferences storage directory: {storage_dir}")
            except OSError as e:
                logger.warning(f"Could not create storage directory: {e}")

    def _load(self) -> None:
        """Load preferences from storage."""
        if not os.path.exists(self._storage_path):
            logger.info(f"No existing preferences file at {self._storage_path}")
            return

        try:
            with open(self._storage_path, 'r', encoding='utf-8') as f:
                data = json.load(f)

            for user_id, prefs_data in data.get('preferences', {}).items():
                # Migrate if needed
                prefs_data = PreferenceMigrator.migrate(prefs_data)
                prefs = UserPreferences.from_dict(prefs_data)
                self._preferences[user_id] = prefs

            logger.info(f"Loaded {len(self._preferences)} user preferences")
        except Exception as e:
            logger.error(f"Error loading preferences: {e}")

    def _persist(self) -> None:
        """Persist preferences to storage."""
        try:
            data = {
                'schema_version': SCHEMA_VERSION,
                'updated_at': datetime.utcnow().isoformat(),
                'preferences': {
                    user_id: prefs.to_dict()
                    for user_id, prefs in self._preferences.items()
                }
            }

            with open(self._storage_path, 'w', encoding='utf-8') as f:
                json.dump(data, f, indent=2)

            self._dirty = False
            logger.debug(f"Persisted {len(self._preferences)} user preferences")
        except Exception as e:
            logger.error(f"Error persisting preferences: {e}")

    def _mark_dirty(self) -> None:
        """Mark preferences as modified."""
        self._dirty = True
        if self._auto_persist:
            self._persist()

    def get(self, user_id: str) -> UserPreferences:
        """
        Get preferences for a user.

        If no preferences exist, creates default preferences.

        Args:
            user_id: The user identifier

        Returns:
            The user's preferences
        """
        if user_id not in self._preferences:
            # Create default preferences
            prefs = UserPreferences(
                user_id=user_id,
                **self._default_preferences
            )
            self._preferences[user_id] = prefs
            self._mark_dirty()
            logger.info(f"Created default preferences for user: {user_id}")

        return self._preferences[user_id]

    def get_or_none(self, user_id: str) -> Optional[UserPreferences]:
        """
        Get preferences for a user without creating defaults.

        Args:
            user_id: The user identifier

        Returns:
            The user's preferences or None if not found
        """
        return self._preferences.get(user_id)

    def set(self, user_id: str, prefs: UserPreferences) -> None:
        """
        Set preferences for a user.

        Args:
            user_id: The user identifier
            prefs: The preferences to set

        Raises:
            ValueError: If validation is enabled and preferences are invalid
        """
        if self._validate_on_set:
            errors = PreferenceValidator.validate(prefs)
            if errors:
                raise ValueError(f"Invalid preferences: {'; '.join(errors)}")

        prefs.user_id = user_id
        prefs.updated_at = datetime.utcnow()
        self._preferences[user_id] = prefs
        self._mark_dirty()
        logger.info(f"Set preferences for user: {user_id}")

    def update(self, user_id: str, **kwargs) -> UserPreferences:
        """
        Update specific preferences for a user.

        Args:
            user_id: The user identifier
            **kwargs: Preference fields to update

        Returns:
            The updated preferences

        Raises:
            ValueError: If validation is enabled and resulting preferences are invalid
        """
        prefs = self.get(user_id)
        prefs.update(**kwargs)

        if self._validate_on_set:
            errors = PreferenceValidator.validate(prefs)
            if errors:
                raise ValueError(f"Invalid preferences: {'; '.join(errors)}")

        self._mark_dirty()
        logger.info(f"Updated preferences for user: {user_id}")
        return prefs

    def delete(self, user_id: str) -> bool:
        """
        Delete preferences for a user.

        Args:
            user_id: The user identifier

        Returns:
            True if deleted, False if not found
        """
        if user_id in self._preferences:
            del self._preferences[user_id]
            self._mark_dirty()
            logger.info(f"Deleted preferences for user: {user_id}")
            return True
        return False

    def has_preferences(self, user_id: str) -> bool:
        """Check if a user has stored preferences."""
        return user_id in self._preferences

    def reset_to_defaults(self, user_id: str) -> UserPreferences:
        """
        Reset a user's preferences to defaults.

        Args:
            user_id: The user identifier

        Returns:
            The reset preferences
        """
        prefs = UserPreferences(
            user_id=user_id,
            **self._default_preferences
        )
        # Preserve ID if exists
        if user_id in self._preferences:
            prefs.id = self._preferences[user_id].id
            prefs.created_at = self._preferences[user_id].created_at

        self._preferences[user_id] = prefs
        self._mark_dirty()
        logger.info(f"Reset preferences to defaults for user: {user_id}")
        return prefs

    def get_effective_preference(
        self,
        user_id: str,
        key: str,
        channel: Optional[str] = None,
        default: Any = None
    ) -> Any:
        """
        Get effective preference value for a user, considering channel overrides.

        Args:
            user_id: The user identifier
            key: The preference key
            channel: Optional channel for override lookup
            default: Default value if not found

        Returns:
            The effective preference value
        """
        prefs = self.get(user_id)
        return prefs.get_effective_preference(key, channel, default)

    def set_channel_override(
        self,
        user_id: str,
        channel: str,
        key: str,
        value: Any
    ) -> None:
        """
        Set a channel-specific preference override for a user.

        Args:
            user_id: The user identifier
            channel: The channel identifier
            key: The preference key
            value: The override value
        """
        prefs = self.get(user_id)
        prefs.set_channel_override(channel, key, value)

        if self._validate_on_set:
            errors = PreferenceValidator.validate(prefs)
            if errors:
                # Rollback the override
                prefs.remove_channel_override(channel, key)
                raise ValueError(f"Invalid preferences: {'; '.join(errors)}")

        self._mark_dirty()
        logger.info(f"Set channel override for user {user_id}, channel {channel}: {key}={value}")

    def remove_channel_override(
        self,
        user_id: str,
        channel: str,
        key: Optional[str] = None
    ) -> bool:
        """
        Remove channel override(s) for a user.

        Args:
            user_id: The user identifier
            channel: The channel identifier
            key: Optional specific key to remove

        Returns:
            True if removed, False if not found
        """
        if user_id not in self._preferences:
            return False

        prefs = self._preferences[user_id]
        result = prefs.remove_channel_override(channel, key)

        if result:
            self._mark_dirty()
            logger.info(f"Removed channel override for user {user_id}, channel {channel}")

        return result

    def list_users(self) -> List[str]:
        """Get list of all users with stored preferences."""
        return list(self._preferences.keys())

    def list_preferences(self) -> List[UserPreferences]:
        """Get all stored preferences."""
        return list(self._preferences.values())

    def export_preferences(self, user_id: Optional[str] = None) -> str:
        """
        Export preferences as JSON.

        Args:
            user_id: Optional specific user to export. If None, exports all.

        Returns:
            JSON string of preferences
        """
        if user_id:
            if user_id not in self._preferences:
                return json.dumps({})
            return json.dumps(self._preferences[user_id].to_dict(), indent=2)

        return json.dumps({
            user_id: prefs.to_dict()
            for user_id, prefs in self._preferences.items()
        }, indent=2)

    def import_preferences(self, json_data: str, merge: bool = True) -> int:
        """
        Import preferences from JSON.

        Args:
            json_data: JSON string of preferences
            merge: If True, merges with existing. If False, replaces.

        Returns:
            Number of preferences imported
        """
        data = json.loads(json_data)
        count = 0

        if not merge:
            self._preferences.clear()

        # Handle single preference or dict of preferences
        if 'user_id' in data:
            # Single preference
            prefs = UserPreferences.from_dict(data)
            self._preferences[prefs.user_id] = prefs
            count = 1
        else:
            # Dict of preferences
            for user_id, prefs_data in data.items():
                prefs_data = PreferenceMigrator.migrate(prefs_data)
                prefs = UserPreferences.from_dict(prefs_data)
                prefs.user_id = user_id
                self._preferences[user_id] = prefs
                count += 1

        self._mark_dirty()
        logger.info(f"Imported {count} preferences")
        return count

    def persist(self) -> None:
        """Manually persist preferences to storage."""
        self._persist()

    def get_default_preferences(self) -> Dict[str, Any]:
        """Get the default preference values."""
        return dict(self._default_preferences)

    def set_default_preferences(self, defaults: Dict[str, Any]) -> None:
        """Set the default preference values."""
        self._default_preferences = dict(defaults)
        logger.info("Updated default preferences")


# Global preference manager instance
_preference_manager: Optional[PreferenceManager] = None


def get_preference_manager(
    storage_path: Optional[str] = None,
    default_preferences: Optional[Dict[str, Any]] = None
) -> PreferenceManager:
    """
    Get the global preference manager instance.

    Args:
        storage_path: Optional custom storage path
        default_preferences: Optional default preferences

    Returns:
        The global PreferenceManager instance
    """
    global _preference_manager

    if _preference_manager is None:
        _preference_manager = PreferenceManager(
            storage_path=storage_path,
            default_preferences=default_preferences
        )

    return _preference_manager
