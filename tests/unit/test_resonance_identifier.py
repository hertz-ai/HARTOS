"""Tests for core.resonance_identifier — HevolveAI biometric dispatch proxy."""

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..'))

from core.resonance_identifier import ResonanceIdentifier


class TestResonanceIdentifier:
    """Test biometric dispatch proxy (graceful degradation)."""

    def test_identify_by_face_returns_none_when_no_backend(self):
        """Without HevolveAI running, identify_by_face returns None."""
        identifier = ResonanceIdentifier()
        result = identifier.identify_by_face(b'\x00' * 100)
        assert result is None

    def test_identify_by_voice_returns_none_when_no_backend(self):
        """Without HevolveAI running, identify_by_voice returns None."""
        identifier = ResonanceIdentifier()
        result = identifier.identify_by_voice(b'\x00' * 32000)
        assert result is None

    def test_enroll_face_returns_bool(self):
        """enroll_face returns bool (True=dispatched, False=unavailable)."""
        identifier = ResonanceIdentifier()
        result = identifier.enroll_face("user1", b'\x00' * 100)
        assert isinstance(result, bool)

    def test_enroll_voice_returns_bool(self):
        """enroll_voice returns bool (True=dispatched, False=unavailable)."""
        identifier = ResonanceIdentifier()
        result = identifier.enroll_voice("user1", b'\x00' * 32000)
        assert isinstance(result, bool)
