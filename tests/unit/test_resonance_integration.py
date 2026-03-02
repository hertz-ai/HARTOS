"""Tests for resonance integration — end-to-end wiring verification."""

import os
import sys
import time

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..'))

from core.resonance_profile import UserResonanceProfile
from core.resonance_tuner import (
    ResonanceTuner, SignalExtractor, build_resonance_prompt,
    _score_to_label, get_resonance_tuner, MIN_INTERACTIONS_FOR_TUNING,
)
from core.agent_personality import (
    AgentPersonality, generate_personality, build_personality_prompt,
)


class TestResonanceIntegration:
    """End-to-end integration tests."""

    def test_personality_prompt_includes_resonance_addon(self):
        """build_personality_prompt with profile appends resonance block."""
        personality = generate_personality('coder', 'Build a website')
        profile = UserResonanceProfile(user_id="integration_user")
        profile.total_interactions = 20
        profile.resonance_confidence = 0.7
        profile.tuning['formality_score'] = 0.8

        result = build_personality_prompt(personality, resonance_profile=profile)
        assert "RESONANCE TUNING" in result
        assert "Formality:" in result

    def test_personality_prompt_without_profile_unchanged(self):
        """build_personality_prompt(p, None) returns same as without resonance."""
        personality = generate_personality('designer', 'Create a logo')
        result_none = build_personality_prompt(personality, resonance_profile=None)
        result_default = build_personality_prompt(personality)
        assert result_none == result_default

    def test_personality_prompt_skips_resonance_for_new_user(self):
        """Profile with < MIN_INTERACTIONS doesn't inject resonance."""
        personality = generate_personality('coder', 'Build API')
        profile = UserResonanceProfile(user_id="new_user")
        profile.total_interactions = 1  # below threshold

        result = build_personality_prompt(personality, resonance_profile=profile)
        assert "RESONANCE TUNING" not in result

    def test_score_to_label_mapping(self):
        """0.0->first label, 1.0->last label, 0.5->middle."""
        labels = ['low', 'medium', 'high']
        assert _score_to_label(0.0, labels) == 'low'
        assert _score_to_label(0.5, labels) == 'medium'
        assert _score_to_label(0.99, labels) == 'high'

    def test_full_tuning_pipeline_integration(self, tmp_path):
        """Extract signals -> tune -> build prompt -> verify output."""
        tuner = ResonanceTuner(alpha=0.3, auto_save=True)
        user_id = "pipeline_user"

        # Run several interactions
        for i in range(5):
            profile = tuner.analyze_and_tune(
                user_id,
                "Please kindly help me deploy the API endpoint to the container",
                "Certainly! Here's the deployment configuration.",
                response_time_ms=350.0,
                base_dir=str(tmp_path),
            )

        assert profile.total_interactions == 5
        assert profile.resonance_confidence > 0.2

        prompt = build_resonance_prompt(profile)
        assert "RESONANCE TUNING" in prompt
        assert "5 interactions" in prompt

    def test_singleton_tuner_reused(self):
        """get_resonance_tuner() returns same instance."""
        t1 = get_resonance_tuner()
        t2 = get_resonance_tuner()
        assert t1 is t2
