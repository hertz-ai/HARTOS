"""A backend that cannot answer /chat/completions must never win a selection.

is_dispatchable() is documented as "the Selection guard ... One check, used by
every selector, keeps this a single source of truth" -- but it excluded only
'shard://', and two selectors did not call it at all.

The registry also carries in-process media backends: 'inprocess://pocket_tts',
'inprocess://whisper', 'inprocess://luxtts', 'local://onnxruntime'. Those are
TTS/STT engines, not chat servers, and they were registered at FAST tier with
high accuracy scores and tiny latencies -- so every latency-ordered selector
preferred them over the real language models.

Measured on this desktop 2026-09-01 with six models registered:

    get_fast_model()                  -> pocket-tts-100m   (200ms, acc 0.85)
    get_model_by_policy('any', 0.7)   -> pocket-tts-100m
    get_expert_model()  [on central]  -> whisper-stt-local

So speculative dispatch was asking a SPEECH SYNTHESISER to draft chat
completions, and ai_capabilities' documented "min_accuracy > 0.7 -> prefer
EXPERT / allow cloud for high quality" resolved to a TTS engine.

Runs standalone (`python tests/unit/test_model_selection_dispatchable.py`).
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..')))
os.environ['HEVOLVE_DB_PATH'] = ':memory:'

from integrations.agent_engine.model_registry import (
    ModelBackend, ModelRegistry, ModelTier,
)


def _mk(mid, tier, url, latency, acc, is_local=True):
    return ModelBackend(
        model_id=mid, display_name=mid, tier=tier,
        config_list_entry={'model': mid, 'api_key': 'x', 'base_url': url},
        avg_latency_ms=latency, accuracy_score=acc, cost_per_1k_tokens=0.0,
        is_local=is_local,
    )


class DispatchableGuardTest(unittest.TestCase):

    def test_inprocess_is_not_dispatchable(self):
        self.assertFalse(_mk('tts', ModelTier.FAST, 'inprocess://pocket_tts', 200, 0.85).is_dispatchable())

    def test_local_scheme_is_not_dispatchable(self):
        self.assertFalse(_mk('onnx', ModelTier.FAST, 'local://onnxruntime', 100, 0.8).is_dispatchable())

    def test_shard_placeholder_still_excluded(self):
        """The original contract must survive."""
        self.assertFalse(_mk('shard', ModelTier.FAST, 'shard://cluster', 10, 0.9).is_dispatchable())

    def test_http_endpoints_are_dispatchable(self):
        self.assertTrue(_mk('a', ModelTier.FAST, 'http://127.0.0.1:8080/v1', 500, 0.8).is_dispatchable())
        self.assertTrue(_mk('b', ModelTier.EXPERT, 'https://api.example.com/v1', 900, 0.9).is_dispatchable())

    def test_missing_base_url_is_not_dispatchable(self):
        m = _mk('x', ModelTier.FAST, '', 10, 0.9)
        self.assertFalse(m.is_dispatchable())


class SelectorsHonourTheGuardTest(unittest.TestCase):
    """The production shape: a fast TTS engine against a slower real model."""

    def setUp(self):
        self.r = ModelRegistry()
        self.r.register(_mk('pocket-tts-100m', ModelTier.FAST, 'inprocess://pocket_tts', 200, 0.85))
        self.r.register(_mk('whisper-stt-local', ModelTier.FAST, 'inprocess://whisper', 500, 0.88))
        self.r.register(_mk('qwen-draft', ModelTier.DRAFT, 'http://127.0.0.1:8080/v1', 120, 0.6))
        self.r.register(_mk('qwen-4b', ModelTier.FAST, 'http://127.0.0.1:8080/v1', 1500, 0.8))
        self.r.register(_mk('claude-code', ModelTier.EXPERT, 'http://127.0.0.1:6777/api/claude/v1', 6000, 0.95))

    def test_fast_skips_the_tts_engine(self):
        m = self.r.get_fast_model()
        self.assertEqual(m.model_id, 'qwen-4b',
                         'a speech synthesiser won text inference on latency')

    def test_expert_is_the_real_expert(self):
        self.assertEqual(self.r.get_expert_model().model_id, 'claude-code')

    def test_draft_selector_guards_too(self):
        self.r.register(_mk('bogus-draft', ModelTier.DRAFT, 'inprocess://luxtts', 10, 0.9))
        self.assertEqual(self.r.get_draft_model().model_id, 'qwen-draft')

    def test_local_selector_guards_too(self):
        """local_only / local_preferred policies resolved to a speech engine."""
        m = self.r.get_local_model()
        self.assertTrue(m.is_dispatchable())
        self.assertNotIn('tts', m.model_id)
        self.assertNotIn('whisper', m.model_id)

    def test_high_quality_policy_reaches_the_expert(self):
        """ai_capabilities maps min_accuracy>=0.7 to policy 'any' and comments
        'allow cloud for high quality'. It used to return the TTS engine."""
        m = self.r.get_model_by_policy(policy='any', min_accuracy=0.9)
        self.assertEqual(m.model_id, 'claude-code')

    def test_speculation_pair_is_usable(self):
        """should_speculate needs a fast and an expert that differ, and BOTH
        must be things the router can actually POST to."""
        fast, expert = self.r.get_fast_model(), self.r.get_expert_model()
        self.assertTrue(fast.is_dispatchable() and expert.is_dispatchable())
        self.assertNotEqual(fast.model_id, expert.model_id)


if __name__ == '__main__':
    unittest.main(verbosity=2)
