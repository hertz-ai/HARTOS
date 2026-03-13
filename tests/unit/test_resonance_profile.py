"""Tests for core.resonance_profile — UserResonanceProfile data model and persistence."""

import json
import os
import sys
import time

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..'))

from core.resonance_profile import (
    UserResonanceProfile, DEFAULT_TUNING,
    save_resonance_profile, load_resonance_profile, get_or_create_profile,
)


class TestUserResonanceProfile:
    """Data model tests."""

    def test_default_profile_has_neutral_tuning(self):
        """All tuning params default to 0.5-0.6 (neutral/warm)."""
        profile = UserResonanceProfile(user_id="test_user")
        assert len(profile.tuning) == len(DEFAULT_TUNING)
        for key, default in DEFAULT_TUNING.items():
            assert profile.tuning[key] == default

    def test_to_dict_and_from_dict_roundtrip(self):
        """Serialization/deserialization preserves all fields."""
        profile = UserResonanceProfile(
            user_id="u123",
            total_interactions=42,
            resonance_confidence=0.85,
            face_enrollment_count=3,
        )
        profile.tuning['formality_score'] = 0.8
        data = profile.to_dict()
        restored = UserResonanceProfile.from_dict(data)
        assert restored.user_id == "u123"
        assert restored.total_interactions == 42
        assert restored.resonance_confidence == 0.85
        assert restored.face_enrollment_count == 3
        assert restored.tuning['formality_score'] == 0.8

    def test_set_tuning_clamps_to_01(self):
        """Values outside [0,1] are clamped."""
        profile = UserResonanceProfile(user_id="clamp_test")
        profile.set_tuning('formality_score', 1.5)
        assert profile.tuning['formality_score'] == 1.0
        profile.set_tuning('warmth_score', -0.3)
        assert profile.tuning['warmth_score'] == 0.0

    def test_get_tuning_returns_default_for_unknown_key(self):
        """get_tuning falls back to DEFAULT_TUNING or 0.5."""
        profile = UserResonanceProfile(user_id="test")
        assert profile.get_tuning('formality_score') == DEFAULT_TUNING['formality_score']
        assert profile.get_tuning('nonexistent_key') == 0.5

    def test_from_dict_merges_missing_tuning_keys(self):
        """Old profiles missing new tuning keys get defaults filled in."""
        data = {
            'user_id': 'old_user',
            'tuning': {'formality_score': 0.9},  # missing other keys
            'total_interactions': 10,
        }
        profile = UserResonanceProfile.from_dict(data)
        assert profile.tuning['formality_score'] == 0.9
        assert profile.tuning['warmth_score'] == DEFAULT_TUNING['warmth_score']
        assert profile.tuning['pace_score'] == DEFAULT_TUNING['pace_score']


class TestResonancePersistence:
    """File persistence tests."""

    def test_save_and_load_roundtrip(self, tmp_path):
        """Save then load returns identical profile."""
        profile = UserResonanceProfile(user_id="persist_test")
        profile.tuning['verbosity_score'] = 0.75
        profile.total_interactions = 15
        save_resonance_profile(profile, base_dir=str(tmp_path))
        loaded = load_resonance_profile("persist_test", base_dir=str(tmp_path))
        assert loaded is not None
        assert loaded.user_id == "persist_test"
        assert loaded.tuning['verbosity_score'] == 0.75
        assert loaded.total_interactions == 15

    def test_load_nonexistent_returns_none(self, tmp_path):
        """Loading missing file returns None."""
        result = load_resonance_profile("no_such_user", base_dir=str(tmp_path))
        assert result is None

    def test_get_or_create_creates_new(self, tmp_path):
        """First call creates fresh profile."""
        profile = get_or_create_profile("new_user", base_dir=str(tmp_path))
        assert profile.user_id == "new_user"
        assert profile.total_interactions == 0
        assert profile.resonance_confidence == 0.0
