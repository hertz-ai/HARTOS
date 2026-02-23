"""
Tests for DM Pairing Security System

Tests the pairing code generation, verification,
and session management functionality.
"""

import pytest
import os
import sys
import json
import tempfile
from datetime import datetime, timedelta
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from integrations.channels.security import (
    PairingStatus,
    PairingCode,
    PairedSession,
    PairingManager,
    PairingMiddleware,
    get_pairing_manager,
)


class TestPairingCode:
    """Tests for PairingCode dataclass."""

    def test_code_creation(self):
        """Test PairingCode creation."""
        code = PairingCode(
            code="ABC123-XYZ1",
            user_id=100,
            prompt_id=200,
        )

        assert code.code == "ABC123-XYZ1"
        assert code.user_id == 100
        assert code.prompt_id == 200
        assert code.status == PairingStatus.PENDING
        assert code.expires_at is not None

    def test_code_expiration(self):
        """Test code expiration check."""
        # Create code that expires immediately
        code = PairingCode(
            code="ABC123-XYZ1",
            user_id=100,
            prompt_id=200,
            expires_at=datetime.now() - timedelta(minutes=1),
        )

        assert code.is_expired
        assert not code.is_valid

    def test_code_valid(self):
        """Test valid code check."""
        code = PairingCode(
            code="ABC123-XYZ1",
            user_id=100,
            prompt_id=200,
            expires_at=datetime.now() + timedelta(minutes=15),
        )

        assert not code.is_expired
        assert code.is_valid

    def test_code_serialization(self):
        """Test serialization and deserialization."""
        original = PairingCode(
            code="ABC123-XYZ1",
            user_id=100,
            prompt_id=200,
        )

        data = original.to_dict()
        restored = PairingCode.from_dict(data)

        assert restored.code == original.code
        assert restored.user_id == original.user_id
        assert restored.prompt_id == original.prompt_id
        assert restored.status == original.status


class TestPairedSession:
    """Tests for PairedSession dataclass."""

    def test_session_creation(self):
        """Test PairedSession creation."""
        session = PairedSession(
            channel="telegram",
            sender_id="user123",
            user_id=100,
            prompt_id=200,
        )

        assert session.channel == "telegram"
        assert session.sender_id == "user123"
        assert session.user_id == 100
        assert session.session_key == ("telegram", "user123")

    def test_session_serialization(self):
        """Test serialization and deserialization."""
        original = PairedSession(
            channel="discord",
            sender_id="user456",
            user_id=300,
            prompt_id=400,
            metadata={"role": "admin"},
        )

        data = original.to_dict()
        restored = PairedSession.from_dict(data)

        assert restored.channel == original.channel
        assert restored.sender_id == original.sender_id
        assert restored.user_id == original.user_id
        assert restored.metadata == {"role": "admin"}


class TestPairingManager:
    """Tests for PairingManager."""

    @pytest.fixture
    def manager(self, tmp_path):
        """Create a PairingManager with temp storage."""
        storage_path = tmp_path / "pairing_data.json"
        return PairingManager(
            code_length=6,
            code_expiry_minutes=15,
            storage_path=str(storage_path),
        )

    def test_generate_pairing_code(self, manager):
        """Test pairing code generation."""
        code = manager.generate_pairing_code(user_id=100, prompt_id=200)

        assert code is not None
        assert len(code) > 6  # Code + hyphen + signature
        assert "-" in code

    def test_generate_unique_codes(self, manager):
        """Test that generated codes are unique."""
        codes = set()
        for _ in range(100):
            code = manager.generate_pairing_code(user_id=100, prompt_id=200)
            codes.add(code)

        assert len(codes) == 100  # All unique

    def test_verify_valid_code(self, manager):
        """Test verifying a valid pairing code."""
        code = manager.generate_pairing_code(user_id=100, prompt_id=200)

        session = manager.verify_pairing(
            channel="telegram",
            sender_id="user123",
            code=code,
        )

        assert session is not None
        assert session.user_id == 100
        assert session.prompt_id == 200
        assert session.channel == "telegram"
        assert session.sender_id == "user123"

    def test_verify_invalid_code(self, manager):
        """Test verifying an invalid code."""
        session = manager.verify_pairing(
            channel="telegram",
            sender_id="user123",
            code="INVALID-CODE",
        )

        assert session is None

    def test_verify_expired_code(self, manager):
        """Test verifying an expired code."""
        # Generate code
        code = manager.generate_pairing_code(
            user_id=100,
            prompt_id=200,
        )

        # Manually expire the code
        pairing = manager._pending_codes[code]
        pairing.expires_at = datetime.now() - timedelta(minutes=1)

        # Code should be invalid
        session = manager.verify_pairing(
            channel="telegram",
            sender_id="user123",
            code=code,
        )

        assert session is None

    def test_code_single_use(self, manager):
        """Test that codes can only be used once."""
        code = manager.generate_pairing_code(user_id=100, prompt_id=200)

        # First use should succeed
        session1 = manager.verify_pairing("telegram", "user1", code)
        assert session1 is not None

        # Second use should fail
        session2 = manager.verify_pairing("telegram", "user2", code)
        assert session2 is None

    def test_is_paired(self, manager):
        """Test checking pairing status."""
        code = manager.generate_pairing_code(user_id=100, prompt_id=200)
        manager.verify_pairing("telegram", "user123", code)

        assert manager.is_paired("telegram", "user123")
        assert not manager.is_paired("telegram", "user456")
        assert not manager.is_paired("discord", "user123")

    def test_get_user_mapping(self, manager):
        """Test getting user mapping."""
        code = manager.generate_pairing_code(user_id=100, prompt_id=200)
        manager.verify_pairing("telegram", "user123", code)

        mapping = manager.get_user_mapping("telegram", "user123")

        assert mapping == (100, 200)

    def test_unpair(self, manager):
        """Test unpairing."""
        code = manager.generate_pairing_code(user_id=100, prompt_id=200)
        manager.verify_pairing("telegram", "user123", code)

        assert manager.is_paired("telegram", "user123")

        result = manager.unpair("telegram", "user123")

        assert result is True
        assert not manager.is_paired("telegram", "user123")

    def test_unpair_user_all_sessions(self, manager):
        """Test unpairing all sessions for a user."""
        # Pair same user on multiple channels
        code1 = manager.generate_pairing_code(user_id=100, prompt_id=200)
        code2 = manager.generate_pairing_code(user_id=100, prompt_id=200)
        code3 = manager.generate_pairing_code(user_id=999, prompt_id=200)

        manager.verify_pairing("telegram", "user1", code1)
        manager.verify_pairing("discord", "user1", code2)
        manager.verify_pairing("telegram", "other", code3)

        count = manager.unpair_user(100)

        assert count == 2
        assert not manager.is_paired("telegram", "user1")
        assert not manager.is_paired("discord", "user1")
        assert manager.is_paired("telegram", "other")

    def test_list_user_pairings(self, manager):
        """Test listing pairings for a user."""
        code1 = manager.generate_pairing_code(user_id=100, prompt_id=200)
        code2 = manager.generate_pairing_code(user_id=100, prompt_id=200)

        manager.verify_pairing("telegram", "user1", code1)
        manager.verify_pairing("discord", "user1", code2)

        pairings = manager.list_user_pairings(100)

        assert len(pairings) == 2
        channels = {p.channel for p in pairings}
        assert channels == {"telegram", "discord"}

    def test_persistence(self, tmp_path):
        """Test that sessions are persisted."""
        storage_path = tmp_path / "pairing_data.json"

        # Create manager and pair
        manager1 = PairingManager(storage_path=str(storage_path))
        code = manager1.generate_pairing_code(user_id=100, prompt_id=200)
        manager1.verify_pairing("telegram", "user123", code)

        # Create new manager instance
        manager2 = PairingManager(storage_path=str(storage_path))

        # Should still be paired
        assert manager2.is_paired("telegram", "user123")
        mapping = manager2.get_user_mapping("telegram", "user123")
        assert mapping == (100, 200)


class TestPairingMiddleware:
    """Tests for PairingMiddleware."""

    @pytest.fixture
    def manager(self, tmp_path):
        """Create a PairingManager."""
        storage_path = tmp_path / "pairing_data.json"
        return PairingManager(storage_path=str(storage_path))

    @pytest.fixture
    def middleware(self, manager):
        """Create middleware."""
        return PairingMiddleware(manager)

    def test_check_unpaired_user(self, middleware):
        """Test checking an unpaired user."""
        result = middleware.check_pairing("telegram", "user123", "Hello!")

        assert not result.is_paired
        assert result.instructions is not None

    def test_check_paired_user(self, middleware, manager):
        """Test checking a paired user."""
        code = manager.generate_pairing_code(user_id=100, prompt_id=200)
        manager.verify_pairing("telegram", "user123", code)

        result = middleware.check_pairing("telegram", "user123", "Hello!")

        assert result.is_paired
        assert result.user_id == 100
        assert result.prompt_id == 200

    def test_pairing_via_message(self, middleware, manager):
        """Test pairing through message text."""
        code = manager.generate_pairing_code(user_id=100, prompt_id=200)

        result = middleware.check_pairing("telegram", "user123", code)

        assert result.is_paired
        assert result.user_id == 100
        assert "successful" in result.instructions.lower()

    def test_invalid_pairing_code_in_message(self, middleware):
        """Test invalid pairing code in message."""
        result = middleware.check_pairing("telegram", "user123", "INVALID-CODE")

        assert not result.is_paired
        assert "invalid" in result.instructions.lower()

    def test_no_pairing_required(self, manager):
        """Test middleware with pairing not required."""
        middleware = PairingMiddleware(
            manager,
            require_pairing=False,
            default_user_id=999,
            default_prompt_id=888,
        )

        result = middleware.check_pairing("telegram", "user123", "Hello!")

        assert result.is_paired
        assert result.user_id == 999
        assert result.prompt_id == 888


class TestCodeFormatDetection:
    """Tests for code format detection."""

    @pytest.fixture
    def middleware(self, tmp_path):
        storage_path = tmp_path / "pairing_data.json"
        manager = PairingManager(storage_path=str(storage_path))
        return PairingMiddleware(manager)

    def test_detects_valid_code_format(self, middleware):
        """Test that valid code formats are detected."""
        assert middleware._looks_like_pairing_code("ABC123-XYZ1")
        assert middleware._looks_like_pairing_code("ABCDEF-1234")
        assert middleware._looks_like_pairing_code("abc123-xyz1")  # Case insensitive

    def test_ignores_normal_messages(self, middleware):
        """Test that normal messages are not detected as codes."""
        assert not middleware._looks_like_pairing_code("Hello!")
        assert not middleware._looks_like_pairing_code("How are you?")
        assert not middleware._looks_like_pairing_code("12345")
        assert not middleware._looks_like_pairing_code("test-")


class TestGlobalPairingManager:
    """Tests for global pairing manager."""

    def test_get_pairing_manager_singleton(self):
        """Test that get_pairing_manager returns singleton."""
        # Reset singleton
        import integrations.channels.security as security_module
        security_module._pairing_manager = None

        manager1 = get_pairing_manager()
        manager2 = get_pairing_manager()

        assert manager1 is manager2


class TestRegressionChannels:
    """Regression tests to ensure security doesn't break channels."""

    def test_channel_imports_still_work(self):
        """Test that channel imports still work."""
        from integrations.channels import (
            ChannelAdapter,
            ChannelStatus,
            Message,
            ChannelRegistry,
        )

        assert ChannelAdapter is not None
        assert ChannelStatus is not None
        assert Message is not None
        assert ChannelRegistry is not None

    def test_flask_integration_still_works(self):
        """Test that Flask integration still works."""
        from integrations.channels.flask_integration import FlaskChannelIntegration

        integration = FlaskChannelIntegration()
        assert integration is not None

    def test_security_can_be_imported_independently(self):
        """Test security module imports independently."""
        from integrations.channels.security import (
            PairingManager,
            PairingMiddleware,
            PairingCode,
            PairedSession,
        )

        assert PairingManager is not None
        assert PairingMiddleware is not None
        assert PairingCode is not None
        assert PairedSession is not None


if __name__ == "__main__":
    pytest.main([__file__, "-v", "-s"])
