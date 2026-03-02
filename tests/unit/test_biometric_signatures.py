"""Tests for biometric dispatch proxy — all ML runs in HevolveAI, not HARTOS."""

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..'))

from core.resonance_identifier import ResonanceIdentifier


class TestBiometricProxy:
    """Test that biometric operations dispatch to HevolveAI (not run locally)."""

    def test_identify_by_face_returns_none_without_hevolveai(self):
        """Without HevolveAI running, face identification returns None."""
        identifier = ResonanceIdentifier()
        result = identifier.identify_by_face(b'\x00' * 100)
        assert result is None

    def test_identify_by_voice_returns_none_without_hevolveai(self):
        """Without HevolveAI running, voice identification returns None."""
        identifier = ResonanceIdentifier()
        result = identifier.identify_by_voice(b'\x00' * 32000)
        assert result is None

    def test_enroll_face_graceful_without_hevolveai(self):
        """Face enrollment dispatches without crash when HevolveAI unavailable."""
        identifier = ResonanceIdentifier()
        result = identifier.enroll_face("user1", b'\x00' * 100)
        # Returns True if dispatch sent, False if bridge unavailable
        assert isinstance(result, bool)

    def test_enroll_voice_graceful_without_hevolveai(self):
        """Voice enrollment dispatches without crash when HevolveAI unavailable."""
        identifier = ResonanceIdentifier()
        result = identifier.enroll_voice("user1", b'\x00' * 32000)
        assert isinstance(result, bool)

    def test_no_ml_imports_in_identifier(self):
        """Verify ResonanceIdentifier has no ML library imports."""
        import inspect
        source = inspect.getsource(ResonanceIdentifier)
        # No ML libraries should be referenced
        assert 'insightface' not in source
        assert 'speechbrain' not in source
        assert 'numpy' not in source
        assert 'torch' not in source
        assert 'cv2' not in source

    def test_no_biometric_signatures_module(self):
        """Verify biometric_signatures.py has been removed from HARTOS."""
        bio_path = os.path.join(
            os.path.dirname(__file__), '..', '..', 'core', 'biometric_signatures.py')
        assert not os.path.exists(bio_path), \
            "biometric_signatures.py should not exist in HARTOS — ML belongs in HevolveAI"
