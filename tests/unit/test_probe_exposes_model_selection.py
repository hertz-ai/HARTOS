"""The daemon probe must expose which models the LIVE registry would pick.

`speculative_enabled: true` is not enough to explain why the EXPERT tier never
gets used. should_speculate ALSO requires a fast and an expert model that
differ, and nothing exposed the running process's actual selection.

On 2026-09-01 that gap produced guesswork: a fresh shell showed
fast=pocket-tts-100m / expert=claude-code with every gate passing, while the
running desktop made ZERO copilot calls across ~40 minutes of active dispatch.
A fresh import is a different registry object, so the shell result said nothing
about the live one -- and no MCP tool exposed it (model_status reports the
llama.cpp server; system_health returns 285 chars with no registry at all).

Runs standalone (`python tests/unit/test_probe_exposes_model_selection.py`).
"""
import os
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..')))
os.environ['HEVOLVE_DB_PATH'] = ':memory:'

from core.health_probe import probe_agent_daemon


class _M:
    def __init__(self, mid): self.model_id = mid


class ProbeModelSelectionTest(unittest.TestCase):

    def test_reports_fast_and_expert(self):
        p = probe_agent_daemon()
        for k in ('fast_model', 'expert_model', 'speculation_possible'):
            self.assertIn(k, p, k + ' missing from the daemon probe')

    def test_speculation_possible_true_when_models_differ(self):
        from integrations.agent_engine import model_registry as mr
        with patch.object(mr.model_registry, 'get_fast_model', return_value=_M('fast-1')), \
             patch.object(mr.model_registry, 'get_expert_model', return_value=_M('claude-code')):
            p = probe_agent_daemon()
        self.assertEqual(p['fast_model'], 'fast-1')
        self.assertEqual(p['expert_model'], 'claude-code')
        self.assertTrue(p['speculation_possible'])

    def test_false_when_no_expert(self):
        """The case that would explain copilot never being called."""
        from integrations.agent_engine import model_registry as mr
        with patch.object(mr.model_registry, 'get_fast_model', return_value=_M('fast-1')), \
             patch.object(mr.model_registry, 'get_expert_model', return_value=None):
            p = probe_agent_daemon()
        self.assertIsNone(p['expert_model'])
        self.assertFalse(p['speculation_possible'])

    def test_false_when_fast_and_expert_are_the_same(self):
        """should_speculate rejects this: no point speculating against itself."""
        from integrations.agent_engine import model_registry as mr
        same = _M('only-model')
        with patch.object(mr.model_registry, 'get_fast_model', return_value=same), \
             patch.object(mr.model_registry, 'get_expert_model', return_value=same):
            p = probe_agent_daemon()
        self.assertFalse(p['speculation_possible'])

    def test_registry_failure_is_reported_not_swallowed(self):
        from integrations.agent_engine import model_registry as mr
        with patch.object(mr.model_registry, 'get_fast_model',
                          side_effect=RuntimeError('registry exploded')):
            p = probe_agent_daemon()
        self.assertIn('model_registry_error', p)
        self.assertFalse(p['speculation_possible'])

    def test_probe_still_reports_the_flag(self):
        self.assertIn('speculative_enabled', probe_agent_daemon())


if __name__ == '__main__':
    unittest.main(verbosity=2)
