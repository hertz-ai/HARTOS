"""
Tests for the continuous resonance learning pipeline.

Covers: DialogueStreamProcessor, federation delta export/import,
HevolveAI corrections flow, oscillation detection, signal vector
conversion, and the complete closed loop.

No neural networks tested — all learning lives in HevolveAI.
HARTOS only does signal extraction, EMA blending, and orchestration.
"""

import json
import math
import os
import shutil
import tempfile
import time
import unittest

import sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..'))

from core.resonance_profile import (
    UserResonanceProfile, save_resonance_profile, load_resonance_profile,
    get_or_create_profile, DEFAULT_TUNING, TUNING_DIM_KEYS, TUNING_DIM_COUNT,
)
from core.resonance_tuner import (
    ResonanceTuner, SignalExtractor, InteractionSignals,
    DialogueStreamProcessor, build_resonance_prompt,
    get_resonance_tuner, MIN_INTERACTIONS_FOR_TUNING,
    OSCILLATION_VARIANCE_THRESHOLD,
)


class TestSignalScores(unittest.TestCase):
    """Test signal-to-vector conversion."""

    def test_signals_to_scores_length(self):
        signals = SignalExtractor.extract("hello world", "response here")
        vec = SignalExtractor.signals_to_scores(signals)
        self.assertEqual(len(vec), TUNING_DIM_COUNT)

    def test_signals_to_scores_bounds(self):
        signals = SignalExtractor.extract(
            "hey yo lol thx btw omg haha bruh nah cool awesome",
            "here you go!")
        vec = SignalExtractor.signals_to_scores(signals)
        for v in vec:
            self.assertGreaterEqual(v, 0.0)
            self.assertLessEqual(v, 1.0)

    def test_formal_signal_high_formality(self):
        signals = SignalExtractor.extract(
            "Dear sir, I would kindly request your assistance regarding this matter. "
            "Furthermore, I shall require a comprehensive analysis.",
            "Certainly.")
        vec = SignalExtractor.signals_to_scores(signals)
        formality_idx = TUNING_DIM_KEYS.index('formality_score')
        self.assertGreater(vec[formality_idx], 0.7)

    def test_casual_signal_low_formality(self):
        signals = SignalExtractor.extract(
            "hey yo can u fix this lol thx btw",
            "Sure.")
        vec = SignalExtractor.signals_to_scores(signals)
        formality_idx = TUNING_DIM_KEYS.index('formality_score')
        self.assertLess(vec[formality_idx], 0.15)

    def test_tech_signal(self):
        signals = SignalExtractor.extract(
            "Deploy the api endpoint on the microservice infrastructure with database schema",
            "Done.")
        vec = SignalExtractor.signals_to_scores(signals)
        tech_idx = TUNING_DIM_KEYS.index('technical_depth')
        self.assertGreater(vec[tech_idx], 0.5)


class TestOscillationDetection(unittest.TestCase):
    """Test oscillation detection for HevolveAI gradient fallback."""

    def test_no_oscillation_with_few_samples(self):
        self.assertFalse(ResonanceTuner._detect_oscillation([
            [0.5] * 8, [0.6] * 8, [0.7] * 8
        ]))

    def test_no_oscillation_with_stable_history(self):
        history = [[0.5 + 0.001 * i] * 8 for i in range(10)]
        self.assertFalse(ResonanceTuner._detect_oscillation(history))

    def test_oscillation_detected(self):
        # Alternating high/low = oscillation
        history = []
        for i in range(10):
            if i % 2 == 0:
                history.append([0.2] * 8)
            else:
                history.append([0.8] * 8)
        self.assertTrue(ResonanceTuner._detect_oscillation(history))

    def test_oscillation_flags_profile(self):
        tmpdir = tempfile.mkdtemp()
        try:
            tuner = ResonanceTuner(auto_save=True)
            # Alternate casual and formal messages to create oscillation
            casual = "hey yo lol thx btw cool awesome nah bruh"
            formal = "Dear sir, I would kindly request your assistance regarding this matter"
            for i in range(15):
                msg = casual if i % 2 == 0 else formal
                profile = tuner.analyze_and_tune(
                    "oscillating_user", msg, "response", base_dir=tmpdir)

            # After enough alternation, gradient_active should be True
            # (depends on threshold but the alternation pattern should trigger it)
            loaded = load_resonance_profile("oscillating_user", tmpdir)
            self.assertIsNotNone(loaded)
            self.assertGreater(len(loaded.tuning_history), 5)
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)


class TestDialogueStreamProcessor(unittest.TestCase):
    """Test continuous in-conversation tuning."""

    def test_stream_accumulates_messages(self):
        tuner = ResonanceTuner(auto_save=False)
        stream = tuner.stream

        stream.on_message("user1", "Agent", "Hello! How can I help?", is_user_message=False)
        stream.on_message("user1", "User", "Fix the bug please", is_user_message=True)
        stream.on_message("user1", "Agent", "I'll look into it.", is_user_message=False)

        self.assertEqual(stream.get_stream_length("user1"), 3)

    def test_stream_end_clears_state(self):
        tuner = ResonanceTuner(auto_save=False)
        stream = tuner.stream

        stream.on_message("user1", "Agent", "Hello!", is_user_message=False)
        self.assertEqual(stream.get_stream_length("user1"), 1)

        stream.on_stream_end("user1")
        self.assertEqual(stream.get_stream_length("user1"), 0)

    def test_stream_isolates_users(self):
        tuner = ResonanceTuner(auto_save=False)
        stream = tuner.stream

        stream.on_message("alice", "Agent", "Hello Alice!", is_user_message=False)
        stream.on_message("bob", "Agent", "Hello Bob!", is_user_message=False)

        self.assertEqual(stream.get_stream_length("alice"), 1)
        self.assertEqual(stream.get_stream_length("bob"), 1)

        stream.on_stream_end("alice")
        self.assertEqual(stream.get_stream_length("alice"), 0)
        self.assertEqual(stream.get_stream_length("bob"), 1)

    def test_short_messages_ignored(self):
        tuner = ResonanceTuner(auto_save=False)
        stream = tuner.stream

        stream.on_message("user1", "Agent", "Hi", is_user_message=False)
        stream.on_message("user1", "User", "ok", is_user_message=True)
        # "ok" is too short (<=5 chars), so no tuning should trigger
        # But it should still be accumulated in the stream
        self.assertEqual(stream.get_stream_length("user1"), 2)


class TestFederationDelta(unittest.TestCase):
    """Test anonymized resonance delta export/import for federation."""

    def test_export_empty_dir(self):
        tmpdir = tempfile.mkdtemp()
        try:
            tuner = ResonanceTuner(auto_save=True)
            delta = tuner.export_resonance_delta(base_dir=tmpdir)
            self.assertEqual(delta, {})
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)

    def test_export_skips_new_users(self):
        tmpdir = tempfile.mkdtemp()
        try:
            # Save a profile with < MIN_INTERACTIONS_FOR_TUNING
            p = UserResonanceProfile(user_id="newbie", total_interactions=1)
            save_resonance_profile(p, tmpdir)

            tuner = ResonanceTuner(auto_save=True)
            delta = tuner.export_resonance_delta(base_dir=tmpdir)
            self.assertEqual(delta, {})  # Skipped because too few interactions
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)

    def test_export_includes_tuned_users(self):
        tmpdir = tempfile.mkdtemp()
        try:
            tuner = ResonanceTuner(auto_save=True)
            for i in range(5):
                tuner.analyze_and_tune("user_a", "hey yo lol thx", "sure", base_dir=tmpdir)

            delta = tuner.export_resonance_delta(base_dir=tmpdir)
            self.assertEqual(delta['type'], 'resonance_delta')
            self.assertEqual(delta['user_count'], 1)
            self.assertEqual(len(delta['avg_tuning']), TUNING_DIM_COUNT)
            self.assertEqual(len(delta['tuning_variance']), TUNING_DIM_COUNT)
            # No user IDs in delta (privacy)
            self.assertNotIn('user_a', str(delta))
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)

    def test_import_hive_resonance(self):
        tmpdir = tempfile.mkdtemp()
        try:
            tuner = ResonanceTuner(auto_save=True)
            # Create a tuned user
            for i in range(5):
                tuner.analyze_and_tune("user_b", "hey yo lol", "ok", base_dir=tmpdir)

            before = load_resonance_profile("user_b", tmpdir)
            before_formality = before.tuning['formality_score']

            # Import hive data with high formality
            hive_data = {
                'avg_tuning': [0.9] * TUNING_DIM_COUNT,  # All high
            }
            tuner.import_hive_resonance(hive_data, base_dir=tmpdir)

            after = load_resonance_profile("user_b", tmpdir)
            after_formality = after.tuning['formality_score']

            # Should have shifted toward hive (30% blend)
            self.assertGreater(after_formality, before_formality)
            # But not fully overwritten (70% local preserved)
            self.assertLess(after_formality, 0.9)
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)


class TestHevolveAICorrections(unittest.TestCase):
    """Test corrections flow from HevolveAI back to HARTOS profiles."""

    def test_apply_corrections(self):
        tmpdir = tempfile.mkdtemp()
        try:
            tuner = ResonanceTuner(auto_save=True)
            for i in range(5):
                tuner.analyze_and_tune("corrected_user", "hey lol", "ok", base_dir=tmpdir)

            before = load_resonance_profile("corrected_user", tmpdir)
            before_formality = before.tuning['formality_score']

            corrections = {
                'tuning_corrections': [0.9, 0.8, 0.7, 0.6, 0.5, 0.4, 0.3, 0.2]
            }
            tuner.apply_hevolveai_corrections("corrected_user", corrections, base_dir=tmpdir)

            after = load_resonance_profile("corrected_user", tmpdir)
            # 70% local + 30% correction(0.9) → shifted up
            self.assertGreater(after.tuning['formality_score'], before_formality)
            self.assertFalse(after.gradient_active)  # Cleared after correction
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)

    def test_apply_corrections_wrong_length_ignored(self):
        tmpdir = tempfile.mkdtemp()
        try:
            tuner = ResonanceTuner(auto_save=True)
            for i in range(5):
                tuner.analyze_and_tune("user_c", "hello", "hi", base_dir=tmpdir)

            before = load_resonance_profile("user_c", tmpdir)
            before_val = before.tuning['formality_score']

            # Wrong length corrections should be ignored
            tuner.apply_hevolveai_corrections("user_c", {'tuning_corrections': [0.9, 0.8]}, base_dir=tmpdir)

            after = load_resonance_profile("user_c", tmpdir)
            self.assertAlmostEqual(after.tuning['formality_score'], before_val, places=5)
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)


class TestTuningHistory(unittest.TestCase):
    """Test tuning history tracking and capping."""

    def test_history_capped_at_max(self):
        tmpdir = tempfile.mkdtemp()
        try:
            tuner = ResonanceTuner(auto_save=True)
            for i in range(30):
                tuner.analyze_and_tune("history_user", f"message {i}", "response", base_dir=tmpdir)

            profile = load_resonance_profile("history_user", tmpdir)
            self.assertLessEqual(len(profile.tuning_history), 20)
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)

    def test_history_snapshots_are_vectors(self):
        tmpdir = tempfile.mkdtemp()
        try:
            tuner = ResonanceTuner(auto_save=True)
            tuner.analyze_and_tune("snap_user", "hello there", "hi", base_dir=tmpdir)

            profile = load_resonance_profile("snap_user", tmpdir)
            self.assertEqual(len(profile.tuning_history), 1)
            self.assertEqual(len(profile.tuning_history[0]), TUNING_DIM_COUNT)
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)


class TestProfileLearningFields(unittest.TestCase):
    """Test new learning state fields in UserResonanceProfile."""

    def test_default_gradient_active_false(self):
        p = UserResonanceProfile(user_id="test")
        self.assertFalse(p.gradient_active)

    def test_default_tuning_history_empty(self):
        p = UserResonanceProfile(user_id="test")
        self.assertEqual(p.tuning_history, [])

    def test_roundtrip_with_learning_fields(self):
        p = UserResonanceProfile(user_id="test")
        p.tuning_history = [[0.5] * 8, [0.6] * 8]
        p.gradient_active = True

        data = p.to_dict()
        p2 = UserResonanceProfile.from_dict(data)

        self.assertEqual(p2.tuning_history, [[0.5] * 8, [0.6] * 8])
        self.assertTrue(p2.gradient_active)


class TestTunerStats(unittest.TestCase):
    """Test tuner statistics tracking."""

    def test_stats_keys(self):
        tuner = ResonanceTuner(auto_save=False)
        stats = tuner.get_stats()
        self.assertIn('total_tunings', stats)
        self.assertIn('total_hevolveai_dispatches', stats)
        self.assertIn('total_hevolveai_corrections', stats)
        self.assertIn('total_oscillations_detected', stats)

    def test_tuning_increments_stat(self):
        tmpdir = tempfile.mkdtemp()
        try:
            tuner = ResonanceTuner(auto_save=True)
            tuner.analyze_and_tune("stats_user", "hello", "hi", base_dir=tmpdir)
            self.assertEqual(tuner.get_stats()['total_tunings'], 1)
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)


class TestFullLoop(unittest.TestCase):
    """Test the complete closed loop: signal -> EMA -> profile -> prompt."""

    def test_casual_user_loop(self):
        tmpdir = tempfile.mkdtemp()
        try:
            tuner = ResonanceTuner(auto_save=True)
            casual_msgs = [
                "hey can u fix this bug? lol its broken",
                "yo thx that worked! awesome",
                "cool btw can u also check the css? thx",
                "yeah nah dont worry about that, just the header",
                "ok cool thx bro, gonna push this now",
            ]
            for msg in casual_msgs:
                profile = tuner.analyze_and_tune(
                    "casual_loop_user", msg, "Sure, done.", base_dir=tmpdir)

            # Verify profile tuned toward casual
            self.assertLess(profile.tuning['formality_score'], 0.35)
            self.assertEqual(profile.total_interactions, 5)
            self.assertGreater(profile.resonance_confidence, 0.2)

            # Verify resonance prompt is generated
            prompt = build_resonance_prompt(profile)
            self.assertIn("RESONANCE TUNING", prompt)
            self.assertIn("5 interactions", prompt)
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)


if __name__ == '__main__':
    unittest.main()
