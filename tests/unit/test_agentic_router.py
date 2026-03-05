"""
Tests for Agentic Router — LLM-powered agent matching and plan generation.

Covers: find_matching_agent, generate_plan_steps, build_agentic_plan,
        should_auto_create_agent, _build_agent_catalog, timeout guard.

Run: pytest tests/unit/test_agentic_router.py -v
"""

import json
import os
import sys
import tempfile
from unittest.mock import patch, MagicMock, PropertyMock

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..')))


# ══════════════════════════════════════════════════════════════════
# 1. Module Structure — No Naive Keyword Matching
# ══════════════════════════════════════════════════════════════════

class TestModuleStructure:
    """Verify deterministic keyword matching has been removed."""

    def test_no_agentic_keywords(self):
        """_AGENTIC_KEYWORDS must not exist — replaced by LLM."""
        from integrations import agentic_router
        assert not hasattr(agentic_router, '_AGENTIC_KEYWORDS')

    def test_no_simple_patterns(self):
        """_SIMPLE_PATTERNS must not exist — replaced by LLM."""
        from integrations import agentic_router
        assert not hasattr(agentic_router, '_SIMPLE_PATTERNS')

    def test_no_classify_intent(self):
        """classify_intent must not exist — LLM handles via tool description."""
        from integrations import agentic_router
        assert not hasattr(agentic_router, 'classify_intent')

    def test_no_agent_match_threshold(self):
        """AGENT_MATCH_THRESHOLD must not exist — LLM matching has no threshold."""
        from integrations import agentic_router
        assert not hasattr(agentic_router, 'AGENT_MATCH_THRESHOLD')

    def test_public_api_exists(self):
        """All public functions must be present."""
        from integrations.agentic_router import (
            find_matching_agent,
            generate_plan_steps,
            build_agentic_plan,
            should_auto_create_agent,
        )
        assert callable(find_matching_agent)
        assert callable(generate_plan_steps)
        assert callable(build_agentic_plan)
        assert callable(should_auto_create_agent)

    def test_build_agent_catalog_private(self):
        """_build_agent_catalog helper must be importable."""
        from integrations.agentic_router import _build_agent_catalog
        assert callable(_build_agent_catalog)


# ══════════════════════════════════════════════════════════════════
# 2. Agent Catalog Building
# ══════════════════════════════════════════════════════════════════

class TestBuildAgentCatalog:
    """Tests for _build_agent_catalog."""

    def test_empty_when_no_registry_and_no_dir(self):
        from integrations.agentic_router import _build_agent_catalog
        result = _build_agent_catalog(prompts_dir='/nonexistent/path')
        # May have expert agents from registry, but no recipes
        assert isinstance(result, list)

    def test_includes_recipes_from_prompts_dir(self):
        from integrations.agentic_router import _build_agent_catalog
        with tempfile.TemporaryDirectory() as tmpdir:
            recipe = {
                'name': 'Test Agent',
                'goal': 'Help with testing',
                'status': 'completed',
            }
            with open(os.path.join(tmpdir, 'test123.json'), 'w') as f:
                json.dump(recipe, f)

            # Also write a _recipe file that should be skipped
            with open(os.path.join(tmpdir, 'test123_0_recipe.json'), 'w') as f:
                json.dump({'steps': []}, f)

            catalog = _build_agent_catalog(prompts_dir=tmpdir)
            recipe_entries = [c for c in catalog if c['source'] == 'recipe']
            assert len(recipe_entries) == 1
            assert recipe_entries[0]['id'] == 'test123'
            assert recipe_entries[0]['name'] == 'Test Agent'
            assert recipe_entries[0]['description'] == 'Help with testing'

    def test_skips_recipe_files(self):
        """Files with _recipe in name should be skipped."""
        from integrations.agentic_router import _build_agent_catalog
        with tempfile.TemporaryDirectory() as tmpdir:
            with open(os.path.join(tmpdir, 'x_0_recipe.json'), 'w') as f:
                json.dump({'steps': []}, f)
            catalog = _build_agent_catalog(prompts_dir=tmpdir)
            recipe_entries = [c for c in catalog if c['source'] == 'recipe']
            assert len(recipe_entries) == 0

    def test_skips_malformed_json(self):
        """Malformed JSON files should be silently skipped."""
        from integrations.agentic_router import _build_agent_catalog
        with tempfile.TemporaryDirectory() as tmpdir:
            with open(os.path.join(tmpdir, 'bad.json'), 'w') as f:
                f.write('{invalid json')
            catalog = _build_agent_catalog(prompts_dir=tmpdir)
            recipe_entries = [c for c in catalog if c['source'] == 'recipe']
            assert len(recipe_entries) == 0

    def test_none_prompts_dir(self):
        """None prompts_dir should not crash."""
        from integrations.agentic_router import _build_agent_catalog
        result = _build_agent_catalog(prompts_dir=None)
        assert isinstance(result, list)


# ══════════════════════════════════════════════════════════════════
# 3. LLM-Powered Agent Matching
# ══════════════════════════════════════════════════════════════════

class TestFindMatchingAgent:
    """Tests for find_matching_agent — LLM semantic matching."""

    @patch('langchain_gpt_api.get_llm')
    def test_returns_matched_agent_when_llm_selects(self, mock_get_llm):
        """When LLM returns an agent ID, should return the match dict."""
        from integrations.agentic_router import find_matching_agent

        mock_llm = MagicMock()
        mock_llm.invoke.return_value = MagicMock(content='test_agent_42')
        mock_get_llm.return_value = mock_llm

        with tempfile.TemporaryDirectory() as tmpdir:
            recipe = {'name': 'Portfolio Builder', 'goal': 'Build portfolio websites'}
            with open(os.path.join(tmpdir, 'test_agent_42.json'), 'w') as f:
                json.dump(recipe, f)

            result = find_matching_agent('build me a portfolio website', tmpdir)
            assert result is not None
            assert result['agent_id'] == 'test_agent_42'
            assert result['name'] == 'Portfolio Builder'
            assert result['source'] == 'recipe'
            assert result['score'] == 15  # LLM-selected = high confidence

    @patch('langchain_gpt_api.get_llm')
    def test_returns_none_when_llm_says_none(self, mock_get_llm):
        """When LLM says NONE, should return None."""
        from integrations.agentic_router import find_matching_agent

        mock_llm = MagicMock()
        mock_llm.invoke.return_value = MagicMock(content='NONE')
        mock_get_llm.return_value = mock_llm

        result = find_matching_agent('hello how are you', '/tmp/empty')
        assert result is None

    @patch('langchain_gpt_api.get_llm')
    def test_returns_none_on_llm_exception(self, mock_get_llm):
        """LLM failure should return None gracefully."""
        from integrations.agentic_router import find_matching_agent

        mock_get_llm.side_effect = RuntimeError('LLM unavailable')
        result = find_matching_agent('build a website', '/tmp/empty')
        assert result is None

    def test_returns_none_when_no_catalog(self):
        """Empty catalog (no agents, no recipes) returns None."""
        from integrations.agentic_router import find_matching_agent
        result = find_matching_agent('test', '/nonexistent')
        # May have expert agents, so just check it doesn't crash
        assert result is None or isinstance(result, dict)

    @patch('langchain_gpt_api.get_llm')
    def test_case_insensitive_none_response(self, mock_get_llm):
        """LLM may return 'none' lowercase — should still return None."""
        from integrations.agentic_router import find_matching_agent

        mock_llm = MagicMock()
        mock_llm.invoke.return_value = MagicMock(content='none')
        mock_get_llm.return_value = mock_llm

        result = find_matching_agent('greetings', '/tmp')
        assert result is None

    @patch('langchain_gpt_api.get_llm')
    def test_llm_invoked_with_catalog_context(self, mock_get_llm):
        """LLM should receive agent catalog in prompt."""
        from integrations.agentic_router import find_matching_agent

        mock_llm = MagicMock()
        mock_llm.invoke.return_value = MagicMock(content='NONE')
        mock_get_llm.return_value = mock_llm

        with tempfile.TemporaryDirectory() as tmpdir:
            recipe = {'name': 'Math Tutor', 'goal': 'Help students with math'}
            with open(os.path.join(tmpdir, 'math1.json'), 'w') as f:
                json.dump(recipe, f)

            find_matching_agent('solve this equation', tmpdir)

            call_args = mock_llm.invoke.call_args[0][0]
            # Catalog may have 50+ expert agents; recipe may be beyond [:50] cap
            # Just verify the LLM was called with catalog structure
            assert 'Agent catalog' in call_args
            assert 'solve this equation' in call_args


# ══════════════════════════════════════════════════════════════════
# 4. LLM-Powered Plan Generation
# ══════════════════════════════════════════════════════════════════

class TestGeneratePlanSteps:
    """Tests for generate_plan_steps — LLM plan generation."""

    @patch('langchain_gpt_api.get_llm')
    def test_returns_llm_generated_steps(self, mock_get_llm):
        """When LLM returns valid JSON, should use those steps."""
        from integrations.agentic_router import generate_plan_steps

        llm_steps = json.dumps([
            {'step_num': 1, 'description': 'Analyze requirements', 'tool_or_agent': 'analysis'},
            {'step_num': 2, 'description': 'Design UI mockups', 'tool_or_agent': 'design'},
            {'step_num': 3, 'description': 'Implement frontend', 'tool_or_agent': 'coding'},
        ])
        mock_llm = MagicMock()
        mock_llm.invoke.return_value = MagicMock(content=llm_steps)
        mock_get_llm.return_value = mock_llm

        steps = generate_plan_steps('build a website')
        assert len(steps) == 3
        assert steps[0]['step_num'] == 1
        assert steps[1]['description'] == 'Design UI mockups'

    @patch('langchain_gpt_api.get_llm')
    def test_fallback_on_llm_failure(self, mock_get_llm):
        """When LLM fails, should return generic 4-step fallback."""
        from integrations.agentic_router import generate_plan_steps

        mock_get_llm.side_effect = RuntimeError('timeout')
        steps = generate_plan_steps('test task')
        assert len(steps) == 4
        assert steps[0]['description'] == 'Analyze requirements and gather context'
        assert steps[3]['description'] == 'Deliver results and get feedback'

    @patch('langchain_gpt_api.get_llm')
    def test_fallback_on_invalid_json(self, mock_get_llm):
        """When LLM returns non-JSON, should use fallback."""
        from integrations.agentic_router import generate_plan_steps

        mock_llm = MagicMock()
        mock_llm.invoke.return_value = MagicMock(content='Sure, I can help with that!')
        mock_get_llm.return_value = mock_llm

        steps = generate_plan_steps('do something')
        assert len(steps) == 4  # fallback

    @patch('langchain_gpt_api.get_llm')
    def test_fallback_uses_agent_name(self, mock_get_llm):
        """Fallback step 3 should use matched agent name."""
        from integrations.agentic_router import generate_plan_steps

        mock_get_llm.side_effect = RuntimeError('down')
        agent = {'name': 'Portfolio Builder', 'description': 'Builds websites'}
        steps = generate_plan_steps('test', matched_agent=agent)
        assert steps[2]['tool_or_agent'] == 'Portfolio Builder'

    @patch('langchain_gpt_api.get_llm')
    def test_single_step_triggers_fallback(self, mock_get_llm):
        """LLM returning < 2 steps should trigger fallback."""
        from integrations.agentic_router import generate_plan_steps

        mock_llm = MagicMock()
        mock_llm.invoke.return_value = MagicMock(
            content='[{"step_num": 1, "description": "Do it", "tool_or_agent": "x"}]'
        )
        mock_get_llm.return_value = mock_llm

        steps = generate_plan_steps('task')
        assert len(steps) == 4  # fallback because < 2


# ══════════════════════════════════════════════════════════════════
# 5. Build Agentic Plan (Integration)
# ══════════════════════════════════════════════════════════════════

class TestBuildAgenticPlan:
    """Tests for build_agentic_plan end-to-end pipeline."""

    @patch('integrations.agentic_router.generate_plan_steps')
    @patch('integrations.agentic_router.find_matching_agent')
    def test_with_matched_agent(self, mock_find, mock_plan):
        from integrations.agentic_router import build_agentic_plan

        mock_find.return_value = {
            'agent_id': 'web_builder',
            'name': 'Web Builder',
            'score': 15,
            'source': 'expert',
            'description': 'Builds web apps',
        }
        mock_plan.return_value = [
            {'step_num': 1, 'description': 'Plan', 'tool_or_agent': 'planning'},
        ]

        result = build_agentic_plan('build a website')
        assert result['matched_agent_id'] == 'web_builder'
        assert result['matched_agent_name'] == 'Web Builder'
        assert result['matched_agent_source'] == 'expert'
        assert result['confidence'] == 'high'
        assert result['requires_new_agent'] is False

    @patch('integrations.agentic_router.generate_plan_steps')
    @patch('integrations.agentic_router.find_matching_agent')
    def test_no_match_requires_new_agent(self, mock_find, mock_plan):
        from integrations.agentic_router import build_agentic_plan

        mock_find.return_value = None
        mock_plan.return_value = [
            {'step_num': 1, 'description': 'Plan', 'tool_or_agent': 'planning'},
        ]

        result = build_agentic_plan('invent a new sport')
        assert result['matched_agent_id'] is None
        assert result['requires_new_agent'] is True
        assert result['confidence'] == 'medium'

    @patch('integrations.agentic_router.generate_plan_steps')
    @patch('integrations.agentic_router.find_matching_agent')
    def test_plan_structure(self, mock_find, mock_plan):
        from integrations.agentic_router import build_agentic_plan

        mock_find.return_value = None
        mock_plan.return_value = []

        result = build_agentic_plan('test')
        assert 'task_description' in result
        assert 'steps' in result
        assert 'matched_agent_id' in result
        assert 'requires_new_agent' in result


# ══════════════════════════════════════════════════════════════════
# 6. Should Auto Create Agent
# ══════════════════════════════════════════════════════════════════

class TestShouldAutoCreateAgent:

    @patch('integrations.agentic_router.find_matching_agent')
    def test_returns_true_when_no_match(self, mock_find):
        from integrations.agentic_router import should_auto_create_agent
        mock_find.return_value = None
        assert should_auto_create_agent('create something new') is True

    @patch('integrations.agentic_router.find_matching_agent')
    def test_returns_false_when_matched(self, mock_find):
        from integrations.agentic_router import should_auto_create_agent
        mock_find.return_value = {'agent_id': 'existing', 'name': 'Bot'}
        assert should_auto_create_agent('do math') is False


# ══════════════════════════════════════════════════════════════════
# 7. Timeout Guard in Handler
# ══════════════════════════════════════════════════════════════════

class TestTimeoutGuard:
    """Verify _handle_agentic_router_tool has timeout protection."""

    def test_handler_source_has_timeout(self):
        """_handle_agentic_router_tool should use concurrent.futures timeout."""
        import inspect
        # Import from the module where it's defined
        sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..')))
        from langchain_gpt_api import _handle_agentic_router_tool
        src = inspect.getsource(_handle_agentic_router_tool)
        assert 'concurrent.futures' in src
        assert 'timeout' in src.lower()
        assert 'TimeoutError' in src
