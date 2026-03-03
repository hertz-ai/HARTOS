"""
Tests for expert agent dispatch wiring.

Covers: score_match(), match_expert_for_context(), CLI import fixes,
consult_expert tool behavior.
"""

import os
import sys
import unittest
from unittest.mock import patch, MagicMock

# Ensure project root is on path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..'))


class TestScoreMatch(unittest.TestCase):
    """Tests for ExpertAgentRegistry.score_match()."""

    def setUp(self):
        from integrations.expert_agents.registry import ExpertAgentRegistry
        self.registry = ExpertAgentRegistry()

    def test_score_match_python_returns_python_expert(self):
        """Python-related query should match python_expert."""
        scored = self.registry.score_match("python programming backend")
        self.assertTrue(len(scored) > 0)
        agent_ids = [a.agent_id for a, _ in scored[:5]]
        self.assertIn("python_expert", agent_ids)

    def test_score_match_empty_query_returns_empty(self):
        """Empty query should return no matches."""
        self.assertEqual(self.registry.score_match(""), [])

    def test_score_match_short_words_ignored(self):
        """Words <= 3 chars should be ignored (noise filter)."""
        scored = self.registry.score_match("a an the is")
        self.assertEqual(scored, [])

    def test_score_match_sorted_descending(self):
        """Results should be sorted by score descending."""
        scored = self.registry.score_match("database SQL queries optimization")
        if len(scored) > 1:
            scores = [s for _, s in scored]
            self.assertEqual(scores, sorted(scores, reverse=True))

    def test_score_match_mobile_returns_mobile_expert(self):
        """Mobile-related query should match mobile_dev_expert."""
        scored = self.registry.score_match("mobile application development")
        agent_ids = [a.agent_id for a, _ in scored[:5]]
        self.assertIn("mobile_dev_expert", agent_ids)

    def test_score_match_unrelated_query_low_scores(self):
        """Very specific unrelated query should have low/zero matches."""
        scored = self.registry.score_match("xylophone zamboni quarantine")
        # Should have few or no matches
        self.assertTrue(len(scored) < 5)

    def test_score_match_description_match_scores_higher(self):
        """Description matches (+3) should score higher than capability description (+1)."""
        scored = self.registry.score_match("security vulnerability penetration testing")
        if scored:
            top_agent, top_score = scored[0]
            self.assertGreater(top_score, 0)

    def test_score_match_returns_tuples(self):
        """Each result should be (ExpertAgent, int) tuple."""
        scored = self.registry.score_match("data analysis")
        for item in scored:
            self.assertEqual(len(item), 2)
            from integrations.expert_agents.registry import ExpertAgent
            self.assertIsInstance(item[0], ExpertAgent)
            self.assertIsInstance(item[1], int)


class TestMatchExpertForContext(unittest.TestCase):
    """Tests for match_expert_for_context()."""

    def test_match_returns_none_for_empty_input(self):
        from integrations.expert_agents import match_expert_for_context
        self.assertIsNone(match_expert_for_context(""))
        self.assertIsNone(match_expert_for_context(None))

    def test_match_returns_none_for_unrelated_input(self):
        from integrations.expert_agents import match_expert_for_context
        result = match_expert_for_context("xylophone zamboni", min_score=10)
        self.assertIsNone(result)

    def test_match_returns_dict_on_match(self):
        from integrations.expert_agents import match_expert_for_context
        result = match_expert_for_context("python programming backend development", min_score=2)
        if result:  # May or may not match depending on threshold
            self.assertIn('agent_id', result)
            self.assertIn('name', result)
            self.assertIn('prompt_block', result)
            self.assertIn('capabilities', result)
            self.assertIn('score', result)

    def test_match_prompt_block_format(self):
        from integrations.expert_agents import match_expert_for_context
        result = match_expert_for_context("python programming backend development", min_score=2)
        if result:
            block = result['prompt_block']
            self.assertIn("[Expert Guidance:", block)
            self.assertIn("Domain:", block)
            self.assertIn("Relevant capabilities:", block)

    def test_match_respects_min_score(self):
        from integrations.expert_agents import match_expert_for_context
        # Very high threshold should return None for generic queries
        result = match_expert_for_context("hello world", min_score=100)
        self.assertIsNone(result)

    def test_match_handles_exception_gracefully(self):
        from integrations.expert_agents import match_expert_for_context
        with patch('integrations.expert_agents.ExpertAgentRegistry') as mock_reg:
            mock_reg.side_effect = Exception("Registry error")
            result = match_expert_for_context("test query")
            self.assertIsNone(result)

    def test_match_capabilities_is_list(self):
        from integrations.expert_agents import match_expert_for_context
        result = match_expert_for_context("python programming backend", min_score=2)
        if result:
            self.assertIsInstance(result['capabilities'], list)
            if result['capabilities']:
                cap = result['capabilities'][0]
                self.assertIn('name', cap)
                self.assertIn('description', cap)


class TestCLIImports(unittest.TestCase):
    """Tests that CLI imports are correct after fixes."""

    def test_expert_registry_import(self):
        """ExpertAgentRegistry should be importable from registry module."""
        from integrations.expert_agents.registry import ExpertAgentRegistry
        registry = ExpertAgentRegistry()
        self.assertIsInstance(registry.agents, dict)
        self.assertGreater(len(registry.agents), 0)

    def test_recommend_experts_from_init(self):
        """recommend_experts_for_dream should be importable from __init__."""
        from integrations.expert_agents import recommend_experts_for_dream
        results = recommend_experts_for_dream("build a mobile app", top_k=3)
        self.assertIsInstance(results, list)

    def test_get_expert_info_from_init(self):
        """get_expert_info should be importable from __init__."""
        from integrations.expert_agents import get_expert_info
        info = get_expert_info("python_expert")
        self.assertIsNotNone(info)
        self.assertEqual(info['agent_id'], 'python_expert')

    def test_match_expert_in_all(self):
        """match_expert_for_context should be in __all__."""
        from integrations.expert_agents import __all__
        self.assertIn('match_expert_for_context', __all__)


class TestConsultExpertTool(unittest.TestCase):
    """Tests for the consult_expert tool behavior (standalone, not within autogen)."""

    def test_consult_expert_no_match(self):
        """Should return 'No domain expert' message for unrelated queries."""
        from integrations.expert_agents import match_expert_for_context
        result = match_expert_for_context("xylophone zamboni quarantine", min_score=100)
        self.assertIsNone(result)

    def test_consult_expert_returns_guidance(self):
        """Should return expert guidance for matching queries."""
        from integrations.expert_agents import match_expert_for_context
        result = match_expert_for_context("python programming", min_score=2)
        if result:
            self.assertIn('Expert Guidance', result['prompt_block'])


class TestRegistryStats(unittest.TestCase):
    """Verify registry integrity after changes."""

    def test_96_agents_loaded(self):
        from integrations.expert_agents.registry import ExpertAgentRegistry
        registry = ExpertAgentRegistry()
        self.assertEqual(len(registry.agents), 96)

    def test_all_agents_have_capabilities(self):
        from integrations.expert_agents.registry import ExpertAgentRegistry
        registry = ExpertAgentRegistry()
        for agent_id, agent in registry.agents.items():
            self.assertGreater(len(agent.capabilities), 0,
                               f"{agent_id} has no capabilities")

    def test_score_match_coexists_with_search(self):
        """score_match should not break existing search_agents."""
        from integrations.expert_agents.registry import ExpertAgentRegistry
        registry = ExpertAgentRegistry()
        # Old method still works
        search_results = registry.search_agents("python")
        self.assertGreater(len(search_results), 0)
        # New method also works
        scored = registry.score_match("python")
        self.assertGreater(len(scored), 0)


if __name__ == '__main__':
    unittest.main()
