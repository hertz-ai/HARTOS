"""Tests for core.resonance_tuner — SignalExtractor, ResonanceTuner, prompt builder."""

import math
import os
import sys
import time

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..'))

from core.resonance_tuner import (
    InteractionSignals, SignalExtractor, ResonanceTuner,
    build_resonance_prompt, _score_to_label, get_resonance_tuner,
    MIN_INTERACTIONS_FOR_TUNING,
)
from core.resonance_profile import UserResonanceProfile


class TestSignalExtractor:
    """Test signal extraction from raw text."""

    def test_formal_message_gets_high_formality_score(self):
        """'Please kindly regarding...' -> formality > 0.7."""
        signals = SignalExtractor.extract(
            "Please kindly regarding the pursuant matter hereby",
            "Sure, I'll look into it.")
        assert signals.formality_markers > 0.7

    def test_casual_message_gets_low_formality_score(self):
        """'hey yo gonna wanna...' -> formality < 0.3."""
        signals = SignalExtractor.extract(
            "hey yo gonna wanna lol thx",
            "No problem!")
        assert signals.formality_markers < 0.3

    def test_technical_message_detected(self):
        """'deploy the API endpoint to the container' -> tech_count >= 3."""
        signals = SignalExtractor.extract(
            "deploy the api endpoint to the container with the pipeline",
            "Done.")
        assert signals.technical_term_count >= 3

    def test_positive_sentiment_detected(self):
        """'great thanks amazing' -> positive_sentiment > 0.7."""
        signals = SignalExtractor.extract(
            "great thanks that was amazing and perfect",
            "Glad to help!")
        assert signals.positive_sentiment > 0.7

    def test_negative_sentiment_detected(self):
        """'terrible broken frustrated' -> positive_sentiment < 0.3."""
        signals = SignalExtractor.extract(
            "terrible broken frustrated and disappointed",
            "I'm sorry about that.")
        assert signals.positive_sentiment < 0.3

    def test_question_count_accurate(self):
        """'What? Where? How?' -> question_count == 3."""
        signals = SignalExtractor.extract(
            "What? Where? How?",
            "Let me explain.")
        assert signals.question_count == 3


class TestResonanceTuner:
    """Test EMA-based tuning engine."""

    def test_ema_smoothing_math(self):
        """EMA(0.5, 1.0, alpha=0.15) == 0.575."""
        result = ResonanceTuner._ema(0.5, 1.0, 0.15)
        assert abs(result - 0.575) < 0.001

    def test_tune_increases_formality_for_formal_user(self, tmp_path):
        """Repeated formal messages shift formality_score upward."""
        tuner = ResonanceTuner(alpha=0.3, auto_save=False)
        profile = UserResonanceProfile(user_id="formal_user")
        initial = profile.tuning['formality_score']

        for _ in range(5):
            signals = SignalExtractor.extract(
                "Please kindly regarding furthermore accordingly",
                "Of course.")
            vec = SignalExtractor.signals_to_scores(signals)
            profile = tuner._tune_profile(profile, signals, vec)

        assert profile.tuning['formality_score'] > initial

    def test_tune_preserves_stability_with_mixed_signals(self, tmp_path):
        """Mixed casual/formal doesn't oscillate wildly."""
        tuner = ResonanceTuner(alpha=0.15, auto_save=False)
        profile = UserResonanceProfile(user_id="mixed_user")

        # Alternate formal and casual
        for i in range(10):
            if i % 2 == 0:
                msg = "please kindly regarding the matter"
            else:
                msg = "hey yo lol gonna wanna"
            signals = SignalExtractor.extract(msg, "response")
            vec = SignalExtractor.signals_to_scores(signals)
            profile = tuner._tune_profile(profile, signals, vec)

        # Should stay near middle, not at extremes
        assert 0.2 < profile.tuning['formality_score'] < 0.8

    def test_confidence_grows_with_interactions(self, tmp_path):
        """After 20 interactions, confidence > 0.6."""
        tuner = ResonanceTuner(auto_save=False)
        profile = UserResonanceProfile(user_id="confidence_user")

        for _ in range(20):
            signals = SignalExtractor.extract("hello", "hi there")
            vec = SignalExtractor.signals_to_scores(signals)
            profile = tuner._tune_profile(profile, signals, vec)

        assert profile.resonance_confidence > 0.6
        assert profile.total_interactions == 20

    def test_min_interactions_gate(self):
        """build_resonance_prompt returns empty for < 3 interactions."""
        profile = UserResonanceProfile(user_id="new_user")
        profile.total_interactions = 2
        result = build_resonance_prompt(profile)
        assert result == ""

    def test_build_resonance_prompt_contains_tuning_labels(self):
        """Prompt includes formality/warmth/pace labels."""
        profile = UserResonanceProfile(user_id="labeled_user")
        profile.total_interactions = 10
        profile.resonance_confidence = 0.5
        result = build_resonance_prompt(profile)
        assert "Formality:" in result
        assert "Warmth:" in result
        assert "Pace:" in result
        assert "RESONANCE TUNING" in result
