"""
Tests for integrations/agent_engine/tool_allowlist.py — model tier tool restrictions.

Run: pytest tests/unit/test_tool_allowlist.py -v --noconftest
"""
import os
import sys
import unittest
from unittest.mock import patch, MagicMock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(__file__))))

from integrations.agent_engine.tool_allowlist import (
    filter_tools_for_model, check_tool_allowed,
    _FAST_TOOLS, _BALANCED_TOOLS,
)


def _make_tools(*names):
    return [{'name': n, 'description': f'{n} tool'} for n in names]


class TestFilterToolsForModel(unittest.TestCase):
    """Filter tool list by model tier."""

    @patch('integrations.agent_engine.tool_allowlist._resolve_tier')
    def test_fast_model_restricted_to_read_only(self, mock_tier):
        from integrations.agent_engine.model_registry import ModelTier
        mock_tier.return_value = ModelTier.FAST

        all_tools = _make_tools('web_search', 'read_file', 'write_file', 'delete_file')
        filtered = filter_tools_for_model('groq-llama', all_tools)

        names = [t['name'] for t in filtered]
        self.assertIn('web_search', names)
        self.assertIn('read_file', names)
        self.assertNotIn('write_file', names)
        self.assertNotIn('delete_file', names)

    @patch('integrations.agent_engine.tool_allowlist._resolve_tier')
    def test_balanced_model_gets_write_tools(self, mock_tier):
        from integrations.agent_engine.model_registry import ModelTier
        mock_tier.return_value = ModelTier.BALANCED

        all_tools = _make_tools('web_search', 'write_file', 'send_message', 'delete_file')
        filtered = filter_tools_for_model('gpt-4o-mini', all_tools)

        names = [t['name'] for t in filtered]
        self.assertIn('web_search', names)
        self.assertIn('write_file', names)
        self.assertIn('send_message', names)
        self.assertNotIn('delete_file', names)

    @patch('integrations.agent_engine.tool_allowlist._resolve_tier')
    def test_expert_model_unrestricted(self, mock_tier):
        from integrations.agent_engine.model_registry import ModelTier
        mock_tier.return_value = ModelTier.EXPERT

        all_tools = _make_tools('web_search', 'write_file', 'delete_file', 'admin_panel')
        filtered = filter_tools_for_model('gpt-4.1', all_tools)

        self.assertEqual(len(filtered), len(all_tools))

    @patch('integrations.agent_engine.tool_allowlist._resolve_tier')
    def test_unknown_model_fail_closed(self, mock_tier):
        mock_tier.return_value = None

        all_tools = _make_tools('web_search', 'read_file')
        filtered = filter_tools_for_model('unknown-model', all_tools)

        self.assertEqual(len(filtered), 0, "Unknown model should get no tools (fail-closed)")


class TestCheckToolAllowed(unittest.TestCase):
    """Gate function for individual tool checks."""

    @patch('integrations.agent_engine.tool_allowlist._resolve_tier')
    def test_fast_allowed_read(self, mock_tier):
        from integrations.agent_engine.model_registry import ModelTier
        mock_tier.return_value = ModelTier.FAST

        allowed, reason = check_tool_allowed('groq-llama', 'web_search')
        self.assertTrue(allowed)

    @patch('integrations.agent_engine.tool_allowlist._resolve_tier')
    def test_fast_blocked_write(self, mock_tier):
        from integrations.agent_engine.model_registry import ModelTier
        mock_tier.return_value = ModelTier.FAST

        allowed, reason = check_tool_allowed('groq-llama', 'write_file')
        self.assertFalse(allowed)
        self.assertIn('not allowed', reason)

    @patch('integrations.agent_engine.tool_allowlist._resolve_tier')
    def test_unknown_blocked(self, mock_tier):
        mock_tier.return_value = None

        allowed, reason = check_tool_allowed('mystery', 'web_search')
        self.assertFalse(allowed)
        self.assertIn('fail-closed', reason)


class TestToolSets(unittest.TestCase):
    """Validate tool set hierarchy."""

    def test_fast_is_subset_of_balanced(self):
        self.assertTrue(_FAST_TOOLS.issubset(_BALANCED_TOOLS))

    def test_fast_has_no_write_tools(self):
        write_tools = {'write_file', 'send_message', 'create_task', 'update_task'}
        self.assertEqual(len(_FAST_TOOLS & write_tools), 0)


if __name__ == '__main__':
    unittest.main()
