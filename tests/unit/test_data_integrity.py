"""
test_data_integrity.py - Static data integrity tests for HARTOS

Verifies all static data structures — seed goals, achievements, ad placements,
cultural traits, TTS priority — are well-formed. Corrupt data causes silent
runtime failures that are extremely hard to debug:

FT: SEED_BOOTSTRAP_GOALS structure, SEED_ACHIEVEMENTS criteria, AD_COSTS,
    DEFAULT_PLACEMENTS, CULTURAL_TRAITS completeness, TTS_PRIORITY order.
NFT: No empty slugs, no duplicate IDs, budgets positive, thresholds monotonic.
"""
import os
import sys
import json

import pytest

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)


# ============================================================
# Seed goals — the 14+ bootstrap goals that start the flywheel
# ============================================================

class TestSeedGoalsIntegrity:
    """SEED_BOOTSTRAP_GOALS are created on first boot — corrupt data = dead flywheel."""

    def test_minimum_count(self):
        from integrations.agent_engine.goal_seeding import SEED_BOOTSTRAP_GOALS
        assert len(SEED_BOOTSTRAP_GOALS) >= 14

    def test_all_have_goal_type(self):
        from integrations.agent_engine.goal_seeding import SEED_BOOTSTRAP_GOALS
        for g in SEED_BOOTSTRAP_GOALS:
            assert g.get('goal_type'), f"Goal '{g.get('slug', '?')}' missing goal_type"

    def test_all_have_title_and_description(self):
        from integrations.agent_engine.goal_seeding import SEED_BOOTSTRAP_GOALS
        for g in SEED_BOOTSTRAP_GOALS:
            assert g.get('title', '').strip(), f"Goal '{g['slug']}' has empty title"
            assert g.get('description', '').strip(), f"Goal '{g['slug']}' has empty description"

    def test_budgets_are_positive(self):
        from integrations.agent_engine.goal_seeding import SEED_BOOTSTRAP_GOALS
        for g in SEED_BOOTSTRAP_GOALS:
            budget = g.get('spark_budget', 0)
            assert budget > 0, f"Goal '{g['slug']}' has non-positive budget: {budget}"

    def test_continuous_goals_have_config(self):
        """Continuous goals need config.continuous=True — missing = one-shot execution."""
        from integrations.agent_engine.goal_seeding import SEED_BOOTSTRAP_GOALS
        for g in SEED_BOOTSTRAP_GOALS:
            config = g.get('config', {})
            if config.get('continuous'):
                assert isinstance(config['continuous'], bool)


# ============================================================
# Achievement definitions — drives the profile achievement UI
# ============================================================

class TestAchievementIntegrity:
    """SEED_ACHIEVEMENTS must be complete and consistent."""

    def test_all_have_parseable_criteria(self):
        from integrations.social.gamification_service import SEED_ACHIEVEMENTS
        for ach in SEED_ACHIEVEMENTS:
            raw = ach.get('criteria_json', '{}')
            parsed = json.loads(raw)
            assert 'type' in parsed, f"'{ach['slug']}' criteria has no type"

    def test_no_duplicate_slugs(self):
        from integrations.social.gamification_service import SEED_ACHIEVEMENTS
        slugs = [a['slug'] for a in SEED_ACHIEVEMENTS]
        assert len(slugs) == len(set(slugs))

    def test_all_have_rewards(self):
        from integrations.social.gamification_service import SEED_ACHIEVEMENTS
        for ach in SEED_ACHIEVEMENTS:
            total = sum(ach.get(k, 0) for k in ('pulse_reward', 'spark_reward', 'xp_reward'))
            assert total > 0, f"'{ach['slug']}' gives zero rewards"


# ============================================================
# Ad service constants — economic rules
# ============================================================

class TestAdConstantsIntegrity:
    """AD_COSTS and revenue splits control the ad economy."""

    def test_revenue_split_sums_to_one(self):
        from integrations.social.ad_service import HOSTER_REVENUE_SHARE, PLATFORM_REVENUE_SHARE
        assert abs(HOSTER_REVENUE_SHARE + PLATFORM_REVENUE_SHARE - 1.0) < 0.001

    def test_cpc_exceeds_cpi(self):
        from integrations.social.ad_service import AD_COSTS
        assert AD_COSTS['default_cpc'] > AD_COSTS['default_cpi']

    def test_default_placements_no_duplicates(self):
        from integrations.social.ad_service import DEFAULT_PLACEMENTS
        names = [p['name'] for p in DEFAULT_PLACEMENTS]
        assert len(names) == len(set(names))


# ============================================================
# Cultural traits — completeness and diversity
# ============================================================

class TestCulturalTraitsIntegrity:
    """CULTURAL_TRAITS shape every agent's personality — must be comprehensive."""

    def test_at_least_25_traits(self):
        from cultural_wisdom import CULTURAL_TRAITS
        assert len(CULTURAL_TRAITS) >= 25

    def test_all_have_required_keys(self):
        from cultural_wisdom import CULTURAL_TRAITS
        required = {'name', 'origin', 'meaning', 'trait', 'behavior'}
        for t in CULTURAL_TRAITS:
            missing = required - set(t.keys())
            assert not missing, f"Trait '{t.get('name', '?')}' missing: {missing}"

    def test_no_duplicate_names(self):
        from cultural_wisdom import CULTURAL_TRAITS
        names = [t['name'] for t in CULTURAL_TRAITS]
        dupes = [n for n in names if names.count(n) > 1]
        assert not dupes, f"Duplicate traits: {set(dupes)}"

    def test_multiple_cultural_regions(self):
        """Must represent at least 5 cultural regions — prevents monoculture bias."""
        from cultural_wisdom import CULTURAL_TRAITS
        regions = set()
        for t in CULTURAL_TRAITS:
            region = t['origin'].split('(')[0].strip().split(',')[0].strip()
            regions.add(region)
        assert len(regions) >= 5, f"Only {len(regions)} regions"


# ============================================================
# Device routing priority — TTS device selection order
# ============================================================

class TestDeviceRoutingIntegrity:
    """_TTS_PRIORITY determines which device speaks to the user."""

    def test_phone_is_first(self):
        from integrations.social.device_routing_service import _TTS_PRIORITY
        assert _TTS_PRIORITY[0] == 'phone'

    def test_robot_is_last(self):
        from integrations.social.device_routing_service import _TTS_PRIORITY
        assert _TTS_PRIORITY[-1] == 'robot'

    def test_desktop_before_tablet(self):
        from integrations.social.device_routing_service import _TTS_PRIORITY
        assert _TTS_PRIORITY.index('desktop') < _TTS_PRIORITY.index('tablet')
