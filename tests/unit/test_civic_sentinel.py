"""
Civic Sentinel Agent Tests — goal type registration, prompt builder,
seed goal, rate limit, and tool tag validation.

Run: pytest tests/unit/test_civic_sentinel.py -v --noconftest
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(__file__))))


# ═══════════════════════════════════════════════════════════════
# 1. Goal Type Registration
# ═══════════════════════════════════════════════════════════════

class TestCivicSentinelRegistration(unittest.TestCase):
    """Verify civic_sentinel goal type is registered correctly."""

    def test_civic_sentinel_registered(self):
        """civic_sentinel should be in registered goal types."""
        from integrations.agent_engine.goal_manager import get_registered_types
        self.assertIn('civic_sentinel', get_registered_types())

    def test_prompt_builder_exists(self):
        """civic_sentinel should have a prompt builder."""
        from integrations.agent_engine.goal_manager import get_prompt_builder
        builder = get_prompt_builder('civic_sentinel')
        self.assertIsNotNone(builder)
        self.assertTrue(callable(builder))

    def test_tool_tags(self):
        """civic_sentinel should have news, web_search, content_gen, feed_management."""
        from integrations.agent_engine.goal_manager import get_tool_tags
        tags = get_tool_tags('civic_sentinel')
        self.assertIn('news', tags)
        self.assertIn('web_search', tags)
        self.assertIn('content_gen', tags)
        self.assertIn('feed_management', tags)

    def test_tool_tags_no_coding(self):
        """civic_sentinel should NOT have coding or destructive tool tags."""
        from integrations.agent_engine.goal_manager import get_tool_tags
        tags = get_tool_tags('civic_sentinel')
        self.assertNotIn('coding', tags)
        self.assertNotIn('robot', tags)
        self.assertNotIn('trading', tags)


# ═══════════════════════════════════════════════════════════════
# 2. Prompt Builder — Content Validation
# ═══════════════════════════════════════════════════════════════

class TestCivicSentinelPrompt(unittest.TestCase):
    """Verify the prompt builder generates correct content."""

    def _build(self, config=None):
        from integrations.agent_engine.goal_manager import get_prompt_builder
        builder = get_prompt_builder('civic_sentinel')
        goal_dict = {'config': config or {}}
        return builder(goal_dict)

    def test_prompt_contains_mission(self):
        """Prompt should state the civic sentinel mission."""
        prompt = self._build()
        self.assertIn('Civic Sentinel', prompt)
        self.assertIn('transparency', prompt.lower())

    def test_prompt_contains_censorship_phase(self):
        """Prompt should include censorship detection phase."""
        prompt = self._build()
        self.assertIn('CENSORSHIP DETECTION', prompt)
        self.assertIn('fetch_news_feeds', prompt)
        self.assertIn('web_search', prompt)

    def test_prompt_contains_hypocrisy_phase(self):
        """Prompt should include hypocrisy detection with historical search."""
        prompt = self._build()
        self.assertIn('HYPOCRISY DETECTION', prompt)
        self.assertIn('OLD articles', prompt)
        self.assertIn('CONTRADICT', prompt)
        self.assertIn('Timeline', prompt.upper().replace('TIMELINE', 'Timeline') or prompt)

    def test_prompt_contains_flag_analysis(self):
        """Prompt should include autonomous flag evaluation."""
        prompt = self._build()
        self.assertIn('FLAG ANALYSIS', prompt)
        self.assertIn('counter-flag', prompt)
        self.assertIn('censorship_detected', prompt)

    def test_prompt_contains_confidence_threshold(self):
        """Prompt should require >80% confidence before counter-flagging."""
        prompt = self._build()
        self.assertIn('80%', prompt)
        self.assertIn('UNCERTAIN', prompt)

    def test_prompt_warns_false_positives(self):
        """Prompt should warn about false positive dangers."""
        prompt = self._build()
        self.assertIn('false positive', prompt.lower())
        self.assertIn('when in doubt', prompt.lower())

    def test_prompt_contains_legal_citations(self):
        """Prompt should require legal-grade source citations."""
        prompt = self._build()
        self.assertIn('LEGAL CITATIONS', prompt)
        self.assertIn('Full article/document title', prompt)
        self.assertIn('Publication name and date', prompt)
        self.assertIn('Direct quote', prompt)
        self.assertIn('archive.org', prompt)

    def test_prompt_contains_confidence_scores(self):
        """Prompt should require confidence scores on findings."""
        prompt = self._build()
        self.assertIn('CONFIDENCE SCORES', prompt)
        self.assertIn('high/medium/low', prompt)
        self.assertIn('independent sources', prompt)

    def test_prompt_contains_evidence_quality_standards(self):
        """Prompt should set evidence quality minimum standards."""
        prompt = self._build()
        self.assertIn('EVIDENCE QUALITY STANDARDS', prompt)
        self.assertIn('2 independent sources', prompt)
        self.assertIn('FACT', prompt)
        self.assertIn('INFERENCE', prompt)

    def test_prompt_contains_autonomy_principles(self):
        """Prompt should have autonomy principles."""
        prompt = self._build()
        self.assertIn('AUTONOMOUS agent', prompt)
        self.assertIn('community voting', prompt)
        self.assertIn('political pressure', prompt)

    def test_prompt_requires_correction_on_inaccuracy(self):
        """Prompt should require corrections if community votes finding inaccurate."""
        prompt = self._build()
        self.assertIn('correction', prompt)
        self.assertIn('community votes', prompt)

    def test_prompt_contains_rules(self):
        """Prompt should include ethical rules."""
        prompt = self._build()
        self.assertIn('Redact bystander personal information', prompt)
        self.assertIn('PUBLIC FIGURES', prompt)
        self.assertIn('NO fake accounts', prompt)

    def test_prompt_includes_topics(self):
        """Prompt should interpolate topics from config."""
        prompt = self._build({'topics': ['healthcare', 'education']})
        self.assertIn('healthcare', prompt)
        self.assertIn('education', prompt)

    def test_prompt_includes_channels(self):
        """Prompt should interpolate channels from config."""
        prompt = self._build({'channels': ['twitter', 'reddit']})
        self.assertIn('twitter', prompt)
        self.assertIn('reddit', prompt)

    def test_prompt_includes_parties(self):
        """Prompt should interpolate parties from config."""
        prompt = self._build({'parties': ['Party A', 'Party B']})
        self.assertIn('Party A', prompt)
        self.assertIn('Party B', prompt)

    def test_prompt_no_parties_graceful(self):
        """Prompt should handle empty parties list gracefully."""
        prompt = self._build({'parties': []})
        # Should not crash and should not include empty party line
        self.assertIsInstance(prompt, str)
        self.assertNotIn('Parties/figures to fact-check: ', prompt)

    def test_prompt_contains_anti_bias_immunity(self):
        """Prompt should include anti-bias immunity against mass propaganda."""
        prompt = self._build()
        self.assertIn('ANTI-BIAS IMMUNITY', prompt)
        self.assertIn('mass followers', prompt)
        self.assertIn('coordinated', prompt)

    def test_prompt_contains_ground_reality_test(self):
        """Prompt should apply ground reality test for political claims."""
        prompt = self._build()
        self.assertIn('GROUND REALITY TEST', prompt)
        self.assertIn('common man', prompt.lower())

    def test_prompt_contains_common_sense(self):
        """Prompt should use common sense and basic intuition."""
        prompt = self._build()
        self.assertIn('COMMON SENSE', prompt)
        self.assertIn('BASIC INTUITION', prompt)

    def test_prompt_contains_developing_nations_awareness(self):
        """Prompt should understand developing nation political realities."""
        prompt = self._build()
        self.assertIn('DEVELOPING NATIONS', prompt)
        self.assertIn('political benefit', prompt)
        self.assertIn('common citizens', prompt)

    def test_prompt_prioritizes_common_man(self):
        """Prompt should prioritize common citizen perspective over propaganda."""
        prompt = self._build()
        self.assertIn('COMMON MAN PERSPECTIVE', prompt)
        self.assertIn('ordinary citizens', prompt)
        self.assertIn('ground truth', prompt)

    def test_prompt_detects_coordinated_campaigns(self):
        """Prompt should detect bot/coordinated amplification campaigns."""
        prompt = self._build()
        self.assertIn('coordinated accounts', prompt)
        self.assertIn('identical phrasing', prompt)
        self.assertIn('synchronized timing', prompt)

    def test_prompt_uses_config_json_key(self):
        """Prompt should also accept config_json key (DB format)."""
        from integrations.agent_engine.goal_manager import get_prompt_builder
        builder = get_prompt_builder('civic_sentinel')
        goal_dict = {'config_json': {'topics': ['corruption']}}
        prompt = builder(goal_dict)
        self.assertIn('corruption', prompt)


# ═══════════════════════════════════════════════════════════════
# 3. Seed Goal
# ═══════════════════════════════════════════════════════════════

class TestCivicSentinelSeed(unittest.TestCase):
    """Verify civic sentinel seed goal exists in bootstrap goals."""

    def test_seed_goal_exists(self):
        """bootstrap_civic_sentinel should be in SEED_BOOTSTRAP_GOALS."""
        from integrations.agent_engine.goal_seeding import SEED_BOOTSTRAP_GOALS
        slugs = [g['slug'] for g in SEED_BOOTSTRAP_GOALS]
        self.assertIn('bootstrap_civic_sentinel', slugs)

    def test_seed_goal_type(self):
        """Seed goal should have goal_type civic_sentinel."""
        from integrations.agent_engine.goal_seeding import SEED_BOOTSTRAP_GOALS
        goal = next(g for g in SEED_BOOTSTRAP_GOALS
                    if g['slug'] == 'bootstrap_civic_sentinel')
        self.assertEqual(goal['goal_type'], 'civic_sentinel')

    def test_seed_goal_autonomous(self):
        """Seed goal config should mark agent as autonomous."""
        from integrations.agent_engine.goal_seeding import SEED_BOOTSTRAP_GOALS
        goal = next(g for g in SEED_BOOTSTRAP_GOALS
                    if g['slug'] == 'bootstrap_civic_sentinel')
        self.assertTrue(goal['config']['autonomous'])

    def test_seed_goal_community_governed(self):
        """Seed goal should be governed by community vote."""
        from integrations.agent_engine.goal_seeding import SEED_BOOTSTRAP_GOALS
        goal = next(g for g in SEED_BOOTSTRAP_GOALS
                    if g['slug'] == 'bootstrap_civic_sentinel')
        self.assertEqual(goal['config']['governance'], 'community_vote')

    def test_seed_goal_posts_publicly(self):
        """Seed goal should post findings publicly."""
        from integrations.agent_engine.goal_seeding import SEED_BOOTSTRAP_GOALS
        goal = next(g for g in SEED_BOOTSTRAP_GOALS
                    if g['slug'] == 'bootstrap_civic_sentinel')
        self.assertTrue(goal['config']['post_findings_publicly'])

    def test_seed_goal_not_tied_to_product(self):
        """Seed goal should NOT be tied to a product (use_product=False)."""
        from integrations.agent_engine.goal_seeding import SEED_BOOTSTRAP_GOALS
        goal = next(g for g in SEED_BOOTSTRAP_GOALS
                    if g['slug'] == 'bootstrap_civic_sentinel')
        self.assertFalse(goal['use_product'])

    def test_seed_goal_has_spark_budget(self):
        """Seed goal should have a reasonable spark budget."""
        from integrations.agent_engine.goal_seeding import SEED_BOOTSTRAP_GOALS
        goal = next(g for g in SEED_BOOTSTRAP_GOALS
                    if g['slug'] == 'bootstrap_civic_sentinel')
        self.assertGreater(goal['spark_budget'], 0)

    def test_seed_goal_description_mentions_evidence(self):
        """Seed goal description should mention evidence-based approach."""
        from integrations.agent_engine.goal_seeding import SEED_BOOTSTRAP_GOALS
        goal = next(g for g in SEED_BOOTSTRAP_GOALS
                    if g['slug'] == 'bootstrap_civic_sentinel')
        desc = goal['description'].lower()
        self.assertIn('evidence', desc)

    def test_seed_goal_description_mentions_community_voting(self):
        """Seed goal should mention community voting governance."""
        from integrations.agent_engine.goal_seeding import SEED_BOOTSTRAP_GOALS
        goal = next(g for g in SEED_BOOTSTRAP_GOALS
                    if g['slug'] == 'bootstrap_civic_sentinel')
        desc = goal['description'].lower()
        self.assertIn('community', desc)
        self.assertIn('voting', desc)


# ═══════════════════════════════════════════════════════════════
# 4. Rate Limit
# ═══════════════════════════════════════════════════════════════

class TestCivicSentinelRateLimit(unittest.TestCase):
    """Verify civic sentinel rate limit exists."""

    def test_rate_limit_entry_exists(self):
        """civic_sentinel should have a rate limit entry."""
        from security.rate_limiter_redis import RedisRateLimiter
        self.assertIn('civic_sentinel', RedisRateLimiter.LIMITS)

    def test_rate_limit_values(self):
        """Rate limit should be 20 ops per 60 seconds."""
        from security.rate_limiter_redis import RedisRateLimiter
        max_req, window = RedisRateLimiter.LIMITS['civic_sentinel']
        self.assertEqual(max_req, 20)
        self.assertEqual(window, 60)


# ═══════════════════════════════════════════════════════════════
# 5. Structural — No New Modules
# ═══════════════════════════════════════════════════════════════

class TestCivicSentinelNoNewModules(unittest.TestCase):
    """Verify civic sentinel is pure runtime — no new module created."""

    def test_no_civic_sentinel_module(self):
        """There should be no civic_sentinel.py module anywhere."""
        import glob
        matches = glob.glob(
            os.path.join(os.path.dirname(os.path.dirname(
                os.path.dirname(__file__))), '**', 'civic_sentinel.py'),
            recursive=True)
        # Filter out test files
        non_test = [m for m in matches if 'test' not in m.lower()]
        self.assertEqual(len(non_test), 0,
                         f"Found unexpected civic_sentinel.py: {non_test}")

    def test_prompt_builder_is_function_not_class(self):
        """Civic sentinel should be a prompt function, not a class."""
        from integrations.agent_engine.goal_manager import get_prompt_builder
        import inspect
        builder = get_prompt_builder('civic_sentinel')
        self.assertTrue(inspect.isfunction(builder))


if __name__ == '__main__':
    unittest.main()
