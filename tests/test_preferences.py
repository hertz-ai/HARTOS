"""
Tests for User Preferences Management

Tests preference persistence, validation, channel overrides,
schema migrations, and integration with identity modules.
"""

import pytest
import os
import sys
import json
from datetime import datetime, timedelta
from unittest.mock import Mock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from integrations.channels.identity.preferences import (
    UserPreferences,
    PreferenceManager,
    PreferenceValidator,
    PreferenceMigrator,
    ResponseStyle,
    Theme,
    get_preference_manager,
    SCHEMA_VERSION,
)


class TestUserPreferences:
    """Tests for UserPreferences dataclass."""

    def test_default_creation(self):
        """Test creating preferences with defaults."""
        prefs = UserPreferences()

        assert prefs.language == "en"
        assert prefs.timezone == "UTC"
        assert prefs.model is None
        assert prefs.response_style == "balanced"
        assert prefs.notifications is True
        assert prefs.theme == "auto"
        assert prefs.channel_overrides == {}
        assert prefs.schema_version == SCHEMA_VERSION
        assert prefs.created_at is not None
        assert prefs.updated_at is not None

    def test_custom_creation(self):
        """Test creating preferences with custom values."""
        prefs = UserPreferences(
            user_id="user123",
            language="es",
            timezone="America/New_York",
            model="gpt-4",
            response_style="detailed",
            notifications=False,
            theme="dark",
        )

        assert prefs.user_id == "user123"
        assert prefs.language == "es"
        assert prefs.timezone == "America/New_York"
        assert prefs.model == "gpt-4"
        assert prefs.response_style == "detailed"
        assert prefs.notifications is False
        assert prefs.theme == "dark"

    def test_serialization(self):
        """Test serialization and deserialization."""
        original = UserPreferences(
            user_id="user123",
            language="fr",
            timezone="Europe/Paris",
            model="claude-3",
            response_style="concise",
            notifications=True,
            theme="light",
            metadata={"custom_key": "custom_value"},
        )

        data = original.to_dict()
        restored = UserPreferences.from_dict(data)

        assert restored.user_id == original.user_id
        assert restored.language == original.language
        assert restored.timezone == original.timezone
        assert restored.model == original.model
        assert restored.response_style == original.response_style
        assert restored.notifications == original.notifications
        assert restored.theme == original.theme
        assert restored.metadata == {"custom_key": "custom_value"}

    def test_update(self):
        """Test updating preferences."""
        prefs = UserPreferences(user_id="user123")
        original_updated = prefs.updated_at

        # Small delay to ensure timestamp changes
        prefs.update(language="de", theme="dark")

        assert prefs.language == "de"
        assert prefs.theme == "dark"
        assert prefs.updated_at >= original_updated

    def test_update_immutable_fields(self):
        """Test that immutable fields are not updated."""
        prefs = UserPreferences(user_id="user123")
        original_id = prefs.id
        original_user_id = prefs.user_id
        original_created = prefs.created_at

        prefs.update(id="new_id", user_id="new_user", created_at=datetime.utcnow())

        assert prefs.id == original_id
        assert prefs.user_id == original_user_id
        assert prefs.created_at == original_created

    def test_channel_overrides(self):
        """Test channel-specific preference overrides."""
        prefs = UserPreferences(user_id="user123", language="en")

        prefs.set_channel_override("telegram", "language", "ru")
        prefs.set_channel_override("discord", "theme", "dark")

        assert prefs.get_effective_preference("language", None) == "en"
        assert prefs.get_effective_preference("language", "telegram") == "ru"
        assert prefs.get_effective_preference("language", "discord") == "en"
        assert prefs.get_effective_preference("theme", "discord") == "dark"
        assert prefs.get_effective_preference("theme", "telegram") == "auto"

    def test_get_channel_overrides(self):
        """Test getting all overrides for a channel."""
        prefs = UserPreferences(user_id="user123")
        prefs.set_channel_override("telegram", "language", "ru")
        prefs.set_channel_override("telegram", "theme", "dark")

        overrides = prefs.get_channel_overrides("telegram")

        assert overrides == {"language": "ru", "theme": "dark"}
        assert prefs.get_channel_overrides("discord") == {}

    def test_remove_channel_override(self):
        """Test removing channel overrides."""
        prefs = UserPreferences(user_id="user123")
        prefs.set_channel_override("telegram", "language", "ru")
        prefs.set_channel_override("telegram", "theme", "dark")

        # Remove specific key
        result = prefs.remove_channel_override("telegram", "language")
        assert result is True
        assert prefs.get_channel_overrides("telegram") == {"theme": "dark"}

        # Remove all for channel
        prefs.set_channel_override("telegram", "language", "ru")
        result = prefs.remove_channel_override("telegram")
        assert result is True
        assert prefs.get_channel_overrides("telegram") == {}

        # Remove non-existent
        result = prefs.remove_channel_override("discord", "language")
        assert result is False

    def test_effective_preference_with_metadata(self):
        """Test effective preference falls back to metadata."""
        prefs = UserPreferences(user_id="user123")
        prefs.metadata["custom_setting"] = "custom_value"

        assert prefs.get_effective_preference("custom_setting") == "custom_value"
        assert prefs.get_effective_preference("missing", default="default") == "default"


class TestPreferenceValidator:
    """Tests for PreferenceValidator."""

    def test_valid_preferences(self):
        """Test validation of valid preferences."""
        prefs = UserPreferences(
            language="en",
            timezone="UTC",
            response_style="balanced",
            theme="auto",
        )

        errors = PreferenceValidator.validate(prefs)
        assert errors == []
        assert PreferenceValidator.is_valid(prefs)

    def test_invalid_language(self):
        """Test validation catches invalid language."""
        prefs = UserPreferences(language="invalid_lang")

        errors = PreferenceValidator.validate(prefs)
        assert len(errors) == 1
        assert "Invalid language" in errors[0]
        assert not PreferenceValidator.is_valid(prefs)

    def test_invalid_response_style(self):
        """Test validation catches invalid response style."""
        prefs = UserPreferences(response_style="invalid_style")

        errors = PreferenceValidator.validate(prefs)
        assert len(errors) == 1
        assert "Invalid response style" in errors[0]

    def test_invalid_theme(self):
        """Test validation catches invalid theme."""
        prefs = UserPreferences(theme="invalid_theme")

        errors = PreferenceValidator.validate(prefs)
        assert len(errors) == 1
        assert "Invalid theme" in errors[0]

    def test_invalid_timezone(self):
        """Test validation catches invalid timezone."""
        prefs = UserPreferences(timezone="Invalid/Timezone")

        errors = PreferenceValidator.validate(prefs)
        assert len(errors) == 1
        assert "Invalid timezone" in errors[0]

    def test_valid_timezones(self):
        """Test various valid timezones pass validation."""
        valid_timezones = [
            "UTC",
            "GMT",
            "America/New_York",
            "Europe/London",
            "Asia/Tokyo",
            "Australia/Sydney",
            "Pacific/Auckland",
        ]

        for tz in valid_timezones:
            prefs = UserPreferences(timezone=tz)
            errors = PreferenceValidator.validate(prefs)
            assert errors == [], f"Timezone {tz} should be valid"

    def test_multiple_errors(self):
        """Test validation returns multiple errors."""
        prefs = UserPreferences(
            language="invalid",
            response_style="invalid",
            theme="invalid",
            timezone="invalid",
        )

        errors = PreferenceValidator.validate(prefs)
        assert len(errors) == 4

    def test_invalid_channel_override_validation(self):
        """Test validation catches invalid values in channel overrides."""
        prefs = UserPreferences(language="en")
        prefs.set_channel_override("telegram", "language", "invalid_lang")

        errors = PreferenceValidator.validate(prefs)
        assert len(errors) == 1
        assert "telegram" in errors[0]
        assert "Invalid language" in errors[0]

    def test_all_valid_languages(self):
        """Test all valid language codes pass validation."""
        for lang in PreferenceValidator.VALID_LANGUAGES:
            prefs = UserPreferences(language=lang)
            assert PreferenceValidator.is_valid(prefs), f"Language {lang} should be valid"

    def test_all_valid_response_styles(self):
        """Test all valid response styles pass validation."""
        for style in ResponseStyle:
            prefs = UserPreferences(response_style=style.value)
            assert PreferenceValidator.is_valid(prefs), f"Style {style.value} should be valid"

    def test_all_valid_themes(self):
        """Test all valid themes pass validation."""
        for theme in Theme:
            prefs = UserPreferences(theme=theme.value)
            assert PreferenceValidator.is_valid(prefs), f"Theme {theme.value} should be valid"


class TestPreferenceMigrator:
    """Tests for PreferenceMigrator."""

    def test_migrate_no_change_needed(self):
        """Test migration when already at target version."""
        data = {
            "user_id": "user123",
            "language": "en",
            "schema_version": SCHEMA_VERSION,
        }

        migrated = PreferenceMigrator.migrate(data)

        assert migrated["schema_version"] == SCHEMA_VERSION
        assert migrated["language"] == "en"

    def test_migrate_missing_version(self):
        """Test migration from data without version."""
        data = {
            "user_id": "user123",
            "language": "en",
        }

        migrated = PreferenceMigrator.migrate(data)

        assert migrated["schema_version"] == SCHEMA_VERSION

    def test_register_and_run_migration(self):
        """Test registering and running a custom migration."""
        def migrate_1_to_2(data):
            # Example migration: rename a field
            if "old_field" in data:
                data["new_field"] = data.pop("old_field")
            return data

        PreferenceMigrator.register_migration(1, 2, migrate_1_to_2)

        data = {
            "user_id": "user123",
            "old_field": "value",
            "schema_version": 1,
        }

        migrated = PreferenceMigrator.migrate(data, target_version=2)

        assert migrated["schema_version"] == 2
        assert "new_field" in migrated
        assert "old_field" not in migrated


class TestPreferenceManager:
    """Tests for PreferenceManager."""

    @pytest.fixture
    def manager(self, tmp_path):
        """Create a preference manager with temp storage."""
        storage_path = tmp_path / "preferences.json"
        return PreferenceManager(
            storage_path=str(storage_path),
            default_preferences={"language": "en", "theme": "auto"},
            auto_persist=False,
        )

    def test_get_creates_default(self, manager):
        """Test get creates default preferences for new user."""
        prefs = manager.get("user123")

        assert prefs is not None
        assert prefs.user_id == "user123"
        assert prefs.language == "en"
        assert prefs.theme == "auto"

    def test_get_returns_existing(self, manager):
        """Test get returns existing preferences."""
        prefs1 = manager.get("user123")
        prefs1.update(language="es")

        prefs2 = manager.get("user123")

        assert prefs1 is prefs2
        assert prefs2.language == "es"

    def test_get_or_none(self, manager):
        """Test get_or_none returns None for missing user."""
        prefs = manager.get_or_none("user123")
        assert prefs is None

        manager.get("user123")
        prefs = manager.get_or_none("user123")
        assert prefs is not None

    def test_set_preferences(self, manager):
        """Test setting preferences for a user."""
        prefs = UserPreferences(language="de", theme="dark")

        manager.set("user123", prefs)

        retrieved = manager.get("user123")
        assert retrieved.language == "de"
        assert retrieved.theme == "dark"
        assert retrieved.user_id == "user123"

    def test_set_validates(self, tmp_path):
        """Test set validates preferences when validation enabled."""
        storage_path = tmp_path / "preferences.json"
        manager = PreferenceManager(
            storage_path=str(storage_path),
            validate_on_set=True,
        )

        prefs = UserPreferences(language="invalid_lang")

        with pytest.raises(ValueError) as exc_info:
            manager.set("user123", prefs)

        assert "Invalid" in str(exc_info.value)

    def test_update_preferences(self, manager):
        """Test updating specific preferences."""
        manager.get("user123")  # Create with defaults

        updated = manager.update("user123", language="fr", theme="light")

        assert updated.language == "fr"
        assert updated.theme == "light"
        assert updated.notifications is True  # Unchanged

    def test_update_validates(self, tmp_path):
        """Test update validates preferences when validation enabled."""
        storage_path = tmp_path / "preferences.json"
        manager = PreferenceManager(
            storage_path=str(storage_path),
            validate_on_set=True,
        )

        manager.get("user123")

        with pytest.raises(ValueError):
            manager.update("user123", language="invalid_lang")

    def test_delete_preferences(self, manager):
        """Test deleting preferences."""
        manager.get("user123")
        assert manager.has_preferences("user123")

        result = manager.delete("user123")

        assert result is True
        assert not manager.has_preferences("user123")

    def test_delete_nonexistent(self, manager):
        """Test deleting non-existent preferences."""
        result = manager.delete("user123")
        assert result is False

    def test_has_preferences(self, manager):
        """Test checking preference existence."""
        assert not manager.has_preferences("user123")

        manager.get("user123")

        assert manager.has_preferences("user123")

    def test_reset_to_defaults(self, manager):
        """Test resetting preferences to defaults."""
        prefs = manager.get("user123")
        prefs.update(language="ja", theme="dark")

        reset_prefs = manager.reset_to_defaults("user123")

        assert reset_prefs.language == "en"  # Default
        assert reset_prefs.theme == "auto"  # Default
        assert reset_prefs.id == prefs.id  # Preserved

    def test_get_effective_preference(self, manager):
        """Test getting effective preference with channel overrides."""
        prefs = manager.get("user123")
        prefs.update(language="en")
        prefs.set_channel_override("telegram", "language", "ru")

        assert manager.get_effective_preference("user123", "language") == "en"
        assert manager.get_effective_preference("user123", "language", "telegram") == "ru"
        assert manager.get_effective_preference("user123", "language", "discord") == "en"

    def test_set_channel_override(self, manager):
        """Test setting channel override via manager."""
        manager.get("user123")

        manager.set_channel_override("user123", "telegram", "theme", "dark")

        prefs = manager.get("user123")
        assert prefs.get_effective_preference("theme", "telegram") == "dark"

    def test_set_channel_override_validates(self, tmp_path):
        """Test channel override validation."""
        storage_path = tmp_path / "preferences.json"
        manager = PreferenceManager(
            storage_path=str(storage_path),
            validate_on_set=True,
        )

        manager.get("user123")

        with pytest.raises(ValueError):
            manager.set_channel_override("user123", "telegram", "language", "invalid")

    def test_remove_channel_override(self, manager):
        """Test removing channel override via manager."""
        manager.get("user123")
        manager.set_channel_override("user123", "telegram", "theme", "dark")

        result = manager.remove_channel_override("user123", "telegram", "theme")

        assert result is True
        prefs = manager.get("user123")
        assert prefs.get_effective_preference("theme", "telegram") == "auto"

    def test_list_users(self, manager):
        """Test listing users with preferences."""
        manager.get("user1")
        manager.get("user2")
        manager.get("user3")

        users = manager.list_users()

        assert len(users) == 3
        assert "user1" in users
        assert "user2" in users
        assert "user3" in users

    def test_list_preferences(self, manager):
        """Test listing all preferences."""
        manager.get("user1")
        manager.get("user2")

        prefs_list = manager.list_preferences()

        assert len(prefs_list) == 2

    def test_persistence(self, tmp_path):
        """Test preference persistence."""
        storage_path = tmp_path / "preferences.json"

        # Create manager and add preferences
        manager1 = PreferenceManager(storage_path=str(storage_path), auto_persist=False)
        prefs = manager1.get("user123")
        prefs.update(language="ja", theme="dark")
        prefs.set_channel_override("telegram", "notifications", False)
        manager1.persist()

        # Create new manager instance
        manager2 = PreferenceManager(storage_path=str(storage_path))
        restored = manager2.get_or_none("user123")

        assert restored is not None
        assert restored.language == "ja"
        assert restored.theme == "dark"
        assert restored.get_effective_preference("notifications", "telegram") is False

    def test_export_preferences(self, manager):
        """Test exporting preferences."""
        prefs = manager.get("user123")
        prefs.update(language="ko")

        # Export single user
        single_export = manager.export_preferences("user123")
        data = json.loads(single_export)
        assert data["language"] == "ko"

        # Export all
        all_export = manager.export_preferences()
        data = json.loads(all_export)
        assert "user123" in data

    def test_import_preferences(self, manager):
        """Test importing preferences."""
        json_data = json.dumps({
            "user123": {
                "user_id": "user123",
                "language": "pt",
                "timezone": "America/Sao_Paulo",
                "response_style": "detailed",
                "notifications": True,
                "theme": "light",
                "channel_overrides": {},
                "metadata": {},
                "schema_version": SCHEMA_VERSION,
            }
        })

        count = manager.import_preferences(json_data)

        assert count == 1
        prefs = manager.get("user123")
        assert prefs.language == "pt"
        assert prefs.timezone == "America/Sao_Paulo"

    def test_import_single_preference(self, manager):
        """Test importing a single preference object."""
        json_data = json.dumps({
            "id": "pref-id",
            "user_id": "user456",
            "language": "it",
            "timezone": "Europe/Rome",
            "response_style": "balanced",
            "notifications": True,
            "theme": "auto",
            "channel_overrides": {},
            "metadata": {},
            "schema_version": SCHEMA_VERSION,
        })

        count = manager.import_preferences(json_data)

        assert count == 1
        prefs = manager.get_or_none("user456")
        assert prefs is not None
        assert prefs.language == "it"

    def test_import_replace_mode(self, manager):
        """Test import with replace mode clears existing."""
        manager.get("existing_user")

        json_data = json.dumps({
            "new_user": {
                "user_id": "new_user",
                "language": "zh",
                "timezone": "Asia/Shanghai",
                "response_style": "balanced",
                "notifications": True,
                "theme": "auto",
                "channel_overrides": {},
                "metadata": {},
                "schema_version": SCHEMA_VERSION,
            }
        })

        manager.import_preferences(json_data, merge=False)

        assert not manager.has_preferences("existing_user")
        assert manager.has_preferences("new_user")

    def test_default_preferences_config(self, tmp_path):
        """Test custom default preferences."""
        storage_path = tmp_path / "preferences.json"
        manager = PreferenceManager(
            storage_path=str(storage_path),
            default_preferences={
                "language": "ja",
                "theme": "dark",
                "response_style": "concise",
            },
        )

        prefs = manager.get("user123")

        assert prefs.language == "ja"
        assert prefs.theme == "dark"
        assert prefs.response_style == "concise"

    def test_get_and_set_default_preferences(self, manager):
        """Test getting and setting default preferences."""
        defaults = manager.get_default_preferences()
        assert defaults["language"] == "en"

        manager.set_default_preferences({"language": "fr", "theme": "light"})
        new_defaults = manager.get_default_preferences()
        assert new_defaults["language"] == "fr"


class TestResponseStyleEnum:
    """Tests for ResponseStyle enum."""

    def test_all_styles(self):
        """Test all response styles are defined."""
        styles = [s.value for s in ResponseStyle]

        assert "concise" in styles
        assert "balanced" in styles
        assert "detailed" in styles
        assert "technical" in styles
        assert "casual" in styles


class TestThemeEnum:
    """Tests for Theme enum."""

    def test_all_themes(self):
        """Test all themes are defined."""
        themes = [t.value for t in Theme]

        assert "auto" in themes
        assert "light" in themes
        assert "dark" in themes


class TestGlobalPreferenceManager:
    """Tests for global preference manager."""

    def test_singleton_pattern(self, tmp_path):
        """Test singleton pattern."""
        import integrations.channels.identity.preferences as prefs_module
        prefs_module._preference_manager = None

        storage_path = tmp_path / "preferences.json"

        manager1 = get_preference_manager(storage_path=str(storage_path))
        manager2 = get_preference_manager()

        assert manager1 is manager2


class TestIntegrationWithIdentityModules:
    """Integration tests with other identity modules."""

    def test_import_all_identity_modules(self):
        """Test that all identity modules can be imported together."""
        from integrations.channels.identity import (
            AgentIdentity,
            IdentityManager,
            Avatar,
            AvatarType,
            AvatarManager,
            UserIdentity,
            ChannelIdentity,
            ChannelType,
            SenderIdentityMapper,
            UserPreferences,
            PreferenceManager,
            PreferenceValidator,
            ResponseStyle,
            Theme,
            get_preference_manager,
        )

        assert AgentIdentity is not None
        assert UserPreferences is not None
        assert PreferenceManager is not None

    def test_preferences_with_sender_mapping(self, tmp_path):
        """Test using preferences with sender identity mapping."""
        from integrations.channels.identity import (
            SenderIdentityMapper,
            ChannelType,
        )

        # Setup
        storage_path = tmp_path / "preferences.json"
        pref_manager = PreferenceManager(storage_path=str(storage_path))
        mapper = SenderIdentityMapper()

        # Create user and channel identity
        user = mapper.create_user(username="testuser", email="test@example.com")
        channel_identity = mapper.map(
            ChannelType.TELEGRAM,
            "server1",
            "tg_user_123",
            channel_username="TelegramUser",
        )
        mapper.set_mapping(channel_identity, user)

        # Set user preferences
        prefs = pref_manager.get(user.id)
        prefs.update(language="de", theme="dark")
        prefs.set_channel_override("telegram", "language", "ru")

        # Verify preferences work with mapped user
        retrieved_user = mapper.get_user_from_channel(
            ChannelType.TELEGRAM,
            "server1",
            "tg_user_123",
        )
        assert retrieved_user is not None

        user_prefs = pref_manager.get(retrieved_user.id)
        assert user_prefs.language == "de"
        assert user_prefs.get_effective_preference("language", "telegram") == "ru"


class TestDockerCompatibility:
    """Tests for Docker/container compatibility."""

    def test_default_path_configuration(self):
        """Test default path is container-compatible."""
        from integrations.channels.identity.preferences import DEFAULT_PREFERENCES_PATH

        # Should either be /app/data path or local fallback
        assert DEFAULT_PREFERENCES_PATH is not None
        assert "preferences.json" in DEFAULT_PREFERENCES_PATH

    def test_storage_directory_creation(self, tmp_path):
        """Test storage directory is created if needed."""
        nested_path = tmp_path / "nested" / "deep" / "preferences.json"

        manager = PreferenceManager(storage_path=str(nested_path))
        manager.get("user123")
        manager.persist()

        assert os.path.exists(str(nested_path))

    def test_handles_missing_storage_gracefully(self, tmp_path):
        """Test graceful handling when storage is unavailable."""
        # Use a path that definitely doesn't exist
        storage_path = "/nonexistent/path/that/should/not/exist/preferences.json"

        # Should not raise, just log warning
        manager = PreferenceManager(storage_path=storage_path, auto_persist=False)

        # Should still work in-memory
        prefs = manager.get("user123")
        assert prefs is not None


if __name__ == "__main__":
    pytest.main([__file__, "-v", "-s"])
