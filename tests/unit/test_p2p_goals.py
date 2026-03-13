"""
Tests for all 12 P2P business vertical goal types.

Verifies registration, prompt generation, preamble/tools inclusion,
seed goals, rate limits, and config extraction for every P2P vertical.

Run with: pytest tests/unit/test_p2p_goals.py -v --noconftest
"""
import pytest
import sys
import os
import json

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..'))

from integrations.agent_engine.goal_manager import (
    get_prompt_builder,
    get_registered_types,
    get_tool_tags,
    _P2P_PREAMBLE,
    _P2P_TOOLS,
)
from integrations.agent_engine.goal_seeding import SEED_BOOTSTRAP_GOALS
from security.rate_limiter_redis import RedisRateLimiter

# ─── Constants ───

ALL_P2P_TYPES = [
    'p2p_marketplace',
    'p2p_rideshare',
    'p2p_grocery',
    'p2p_food',
    'p2p_freelance',
    'p2p_bills',
    'p2p_tickets',
    'p2p_tutoring',
    'p2p_services',
    'p2p_rental',
    'p2p_health',
    'p2p_logistics',
]


def _make_goal_dict(goal_type, config=None):
    """Create a minimal goal dict for prompt builders."""
    return {
        'id': 'test-goal-1',
        'goal_type': goal_type,
        'title': f'Test {goal_type}',
        'description': f'Test description for {goal_type}',
        'config': config or {},
    }


# ═══════════════════════════════════════════════════════════════════
# 1. Registration — all 12 types in the prompt builder registry
# ═══════════════════════════════════════════════════════════════════

class TestP2PRegistration:

    def test_all_p2p_types_registered(self):
        """All 12 P2P goal types appear in get_registered_types()."""
        registered = get_registered_types()
        for gtype in ALL_P2P_TYPES:
            assert gtype in registered, f"{gtype} not registered"

    @pytest.mark.parametrize("goal_type", ALL_P2P_TYPES)
    def test_prompt_builder_exists(self, goal_type):
        """Each P2P type has a non-None prompt builder."""
        builder = get_prompt_builder(goal_type)
        assert builder is not None, f"No prompt builder for {goal_type}"
        assert callable(builder)

    @pytest.mark.parametrize("goal_type", ALL_P2P_TYPES)
    def test_tool_tags_exist(self, goal_type):
        """Each P2P type has at least one tool tag."""
        tags = get_tool_tags(goal_type)
        assert isinstance(tags, list)
        assert len(tags) >= 1, f"{goal_type} has no tool tags"
        assert 'web_search' in tags, f"{goal_type} missing web_search tag"


# ═══════════════════════════════════════════════════════════════════
# 2. Prompt generation — valid non-empty strings
# ═══════════════════════════════════════════════════════════════════

class TestP2PPromptGeneration:

    @pytest.mark.parametrize("goal_type", ALL_P2P_TYPES)
    def test_prompt_is_nonempty_string(self, goal_type):
        """Each builder returns a non-empty string."""
        builder = get_prompt_builder(goal_type)
        goal_dict = _make_goal_dict(goal_type)
        result = builder(goal_dict)
        assert isinstance(result, str)
        assert len(result) > 100, f"{goal_type} prompt too short"

    @pytest.mark.parametrize("goal_type", ALL_P2P_TYPES)
    def test_prompt_with_product_dict_none(self, goal_type):
        """Prompt builders work when product_dict is None."""
        builder = get_prompt_builder(goal_type)
        goal_dict = _make_goal_dict(goal_type)
        result = builder(goal_dict, product_dict=None)
        assert isinstance(result, str)
        assert len(result) > 0


# ═══════════════════════════════════════════════════════════════════
# 3. P2P preamble — 90/9/1 revenue split in every prompt
# ═══════════════════════════════════════════════════════════════════

class TestP2PPreamble:

    def test_preamble_contains_revenue_split(self):
        """The shared preamble references 90/9/1."""
        assert '90%' in _P2P_PREAMBLE
        assert '9%' in _P2P_PREAMBLE
        assert '1%' in _P2P_PREAMBLE

    def test_preamble_contains_escrow(self):
        """The shared preamble references escrow."""
        assert 'escrow' in _P2P_PREAMBLE.lower()

    @pytest.mark.parametrize("goal_type", ALL_P2P_TYPES)
    def test_prompt_contains_preamble(self, goal_type):
        """Each generated prompt includes the P2P preamble text."""
        builder = get_prompt_builder(goal_type)
        prompt = builder(_make_goal_dict(goal_type))
        # Check for a distinctive substring from the preamble
        assert '90% to service provider' in prompt, (
            f"{goal_type} prompt missing P2P preamble"
        )


# ═══════════════════════════════════════════════════════════════════
# 4. Shared tools section in every prompt
# ═══════════════════════════════════════════════════════════════════

class TestP2PToolsSection:

    def test_tools_section_mentions_ap2(self):
        """The shared tools section references AP2 payment protocol."""
        assert 'request_payment' in _P2P_TOOLS
        assert 'authorize_payment' in _P2P_TOOLS
        assert 'process_payment' in _P2P_TOOLS

    def test_tools_section_mentions_channels(self):
        """The shared tools section references channel adapters."""
        assert 'channel adapters' in _P2P_TOOLS.lower()

    def test_tools_section_mentions_mcgroce(self):
        """The shared tools section references McGDroid/McGroce backend."""
        assert 'McGDroid' in _P2P_TOOLS or 'McGroce' in _P2P_TOOLS

    def test_tools_section_mentions_ridesnap(self):
        """The shared tools section references RideSnap backend."""
        assert 'RideSnap' in _P2P_TOOLS

    @pytest.mark.parametrize("goal_type", ALL_P2P_TYPES)
    def test_prompt_contains_tools_section(self, goal_type):
        """Each generated prompt includes the shared tools text."""
        builder = get_prompt_builder(goal_type)
        prompt = builder(_make_goal_dict(goal_type))
        assert 'request_payment' in prompt, (
            f"{goal_type} prompt missing shared tools section"
        )


# ═══════════════════════════════════════════════════════════════════
# 5. Rideshare prompt — references RideSnap API
# ═══════════════════════════════════════════════════════════════════

class TestRidesharePrompt:

    def test_rideshare_mentions_ridesnap(self):
        """Rideshare prompt references the RideSnap backend."""
        builder = get_prompt_builder('p2p_rideshare')
        prompt = builder(_make_goal_dict('p2p_rideshare'))
        assert 'RideSnap' in prompt or 'RIDESNAP' in prompt

    def test_rideshare_has_api_endpoints(self):
        """Rideshare prompt lists RideSnap API endpoints."""
        builder = get_prompt_builder('p2p_rideshare')
        prompt = builder(_make_goal_dict('p2p_rideshare'))
        assert '/rides' in prompt
        assert '/captains' in prompt
        assert '/payments' in prompt

    def test_rideshare_default_backend_url(self):
        """Default RideSnap URL is localhost:8000/api."""
        builder = get_prompt_builder('p2p_rideshare')
        prompt = builder(_make_goal_dict('p2p_rideshare'))
        assert 'localhost:8000/api' in prompt

    def test_rideshare_custom_backend_url(self):
        """Custom ridesnap_url flows into prompt."""
        builder = get_prompt_builder('p2p_rideshare')
        goal = _make_goal_dict('p2p_rideshare', config={
            'ridesnap_url': 'https://ridesnap.example.com/api',
        })
        prompt = builder(goal)
        assert 'ridesnap.example.com' in prompt


# ═══════════════════════════════════════════════════════════════════
# 6. Tutoring prompt — references Enlight21
# ═══════════════════════════════════════════════════════════════════

class TestTutoringPrompt:

    def test_tutoring_mentions_enlight21(self):
        """Tutoring prompt references Enlight21."""
        builder = get_prompt_builder('p2p_tutoring')
        prompt = builder(_make_goal_dict('p2p_tutoring'))
        assert 'Enlight21' in prompt

    def test_tutoring_enlight21_with_url(self):
        """When enlight_url is set, it appears in the prompt."""
        builder = get_prompt_builder('p2p_tutoring')
        goal = _make_goal_dict('p2p_tutoring', config={
            'enlight_url': 'https://enlight21.example.com',
        })
        prompt = builder(goal)
        assert 'enlight21.example.com' in prompt
        assert 'E2E encrypted chat' in prompt

    def test_tutoring_without_enlight_url(self):
        """Without enlight_url, prompt still mentions Enlight21 availability."""
        builder = get_prompt_builder('p2p_tutoring')
        prompt = builder(_make_goal_dict('p2p_tutoring'))
        assert 'Enlight21' in prompt
        assert 'Configure enlight_url' in prompt or 'E2E' in prompt

    def test_tutoring_subjects_in_prompt(self):
        """Subjects list flows into the tutoring prompt."""
        builder = get_prompt_builder('p2p_tutoring')
        goal = _make_goal_dict('p2p_tutoring', config={
            'subjects': ['math', 'physics'],
        })
        prompt = builder(goal)
        assert 'math' in prompt
        assert 'physics' in prompt


# ═══════════════════════════════════════════════════════════════════
# 6b. Grocery prompt — references McGDroid/McGroce
# ═══════════════════════════════════════════════════════════════════

class TestGroceryPrompt:

    def test_grocery_mentions_mcgroce(self):
        """Grocery prompt references the McGroce backend."""
        builder = get_prompt_builder('p2p_grocery')
        prompt = builder(_make_goal_dict('p2p_grocery'))
        assert 'McGroce' in prompt or 'McGDROID' in prompt or 'McGROCE' in prompt

    def test_grocery_has_store_discovery_endpoints(self):
        """Grocery prompt lists McGroce store discovery API."""
        builder = get_prompt_builder('p2p_grocery')
        prompt = builder(_make_goal_dict('p2p_grocery'))
        assert 'zipcodesearch/stores' in prompt
        assert '/search/' in prompt

    def test_grocery_has_voice_ordering(self):
        """Grocery prompt mentions voice ordering via audioorder."""
        builder = get_prompt_builder('p2p_grocery')
        prompt = builder(_make_goal_dict('p2p_grocery'))
        assert 'audioorder' in prompt
        assert 'voiceorders' in prompt

    def test_grocery_has_wamp_events(self):
        """Grocery prompt mentions WAMP real-time store events."""
        builder = get_prompt_builder('p2p_grocery')
        prompt = builder(_make_goal_dict('p2p_grocery'))
        assert 'WAMP' in prompt
        assert 'chat{storeId}' in prompt or "chat{storeId}" in prompt

    def test_grocery_default_mcgroce_url(self):
        """Default McGroce URL is localhost:8080/api/v1."""
        builder = get_prompt_builder('p2p_grocery')
        prompt = builder(_make_goal_dict('p2p_grocery'))
        assert 'localhost:8080/api/v1' in prompt

    def test_grocery_custom_mcgroce_url(self):
        """Custom mcgroce_url flows into prompt."""
        builder = get_prompt_builder('p2p_grocery')
        goal = _make_goal_dict('p2p_grocery', config={
            'mcgroce_url': 'https://beta.mcgroce.com/api/v1',
        })
        prompt = builder(goal)
        assert 'beta.mcgroce.com' in prompt

    def test_grocery_has_fallback_mode(self):
        """Grocery prompt includes fallback when McGroce unavailable."""
        builder = get_prompt_builder('p2p_grocery')
        prompt = builder(_make_goal_dict('p2p_grocery'))
        assert 'FALLBACK' in prompt or 'fallback' in prompt or 'unavailable' in prompt

    def test_grocery_has_product_search_dto(self):
        """Grocery prompt documents the ProductSearchDTO fields."""
        builder = get_prompt_builder('p2p_grocery')
        prompt = builder(_make_goal_dict('p2p_grocery'))
        assert 'ProductSearchDTO' in prompt

    def test_grocery_has_store_model_fields(self):
        """Grocery prompt documents Store domain model fields."""
        builder = get_prompt_builder('p2p_grocery')
        prompt = builder(_make_goal_dict('p2p_grocery'))
        assert 'deliveryAvailable' in prompt
        assert 'deliveryRadius' in prompt

    def test_grocery_has_customer_auth(self):
        """Grocery prompt includes customer auth endpoints."""
        builder = get_prompt_builder('p2p_grocery')
        prompt = builder(_make_goal_dict('p2p_grocery'))
        assert 'customer/register' in prompt
        assert 'customer/username' in prompt

    def test_grocery_seed_has_mcgroce_url(self):
        """Grocery seed goal includes mcgroce_url in config."""
        grocery_seeds = [s for s in SEED_BOOTSTRAP_GOALS
                         if s.get('goal_type') == 'p2p_grocery']
        assert len(grocery_seeds) >= 1
        config = grocery_seeds[0].get('config', {})
        assert 'mcgroce_url' in config
        assert 'localhost:8080' in config['mcgroce_url']

    def test_grocery_seed_description_mentions_mcgdroid(self):
        """Grocery seed goal description mentions McGDroid/McGroce."""
        grocery_seeds = [s for s in SEED_BOOTSTRAP_GOALS
                         if s.get('goal_type') == 'p2p_grocery']
        assert len(grocery_seeds) >= 1
        desc = grocery_seeds[0].get('description', '')
        assert 'McGroce' in desc or 'McGDroid' in desc


# ═══════════════════════════════════════════════════════════════════
# 7. Config with region/category overrides
# ═══════════════════════════════════════════════════════════════════

class TestConfigOverrides:

    @pytest.mark.parametrize("goal_type", [
        'p2p_rideshare', 'p2p_grocery', 'p2p_food', 'p2p_bills',
        'p2p_tickets', 'p2p_services', 'p2p_logistics',
    ])
    def test_region_override(self, goal_type):
        """Prompt builders that accept region use config override."""
        builder = get_prompt_builder(goal_type)
        goal = _make_goal_dict(goal_type, config={'region': 'Mumbai'})
        prompt = builder(goal)
        assert 'Mumbai' in prompt

    @pytest.mark.parametrize("goal_type", [
        'p2p_marketplace', 'p2p_freelance', 'p2p_rental',
    ])
    def test_category_override(self, goal_type):
        """Prompt builders that accept category use config override."""
        builder = get_prompt_builder(goal_type)
        goal = _make_goal_dict(goal_type, config={'category': 'electronics'})
        prompt = builder(goal)
        assert 'electronics' in prompt

    def test_default_region_is_auto_detect(self):
        """When no region is provided, default is 'auto-detect'."""
        builder = get_prompt_builder('p2p_rideshare')
        prompt = builder(_make_goal_dict('p2p_rideshare'))
        assert 'auto-detect' in prompt

    def test_default_category_is_general(self):
        """When no category is provided, default is 'general'."""
        builder = get_prompt_builder('p2p_marketplace')
        prompt = builder(_make_goal_dict('p2p_marketplace'))
        assert 'general' in prompt


# ═══════════════════════════════════════════════════════════════════
# 8. Seed goals — SEED_BOOTSTRAP_GOALS has entries for all 12 types
# ═══════════════════════════════════════════════════════════════════

class TestSeedGoals:

    def _seed_goal_types(self):
        """Extract unique P2P goal_types from seed goals."""
        return {
            g['goal_type'] for g in SEED_BOOTSTRAP_GOALS
            if g['goal_type'].startswith('p2p_')
        }

    def test_all_12_p2p_types_have_seed_goals(self):
        """Every P2P type has at least one entry in SEED_BOOTSTRAP_GOALS."""
        seed_types = self._seed_goal_types()
        for gtype in ALL_P2P_TYPES:
            assert gtype in seed_types, (
                f"{gtype} missing from SEED_BOOTSTRAP_GOALS"
            )

    @pytest.mark.parametrize("goal_type", ALL_P2P_TYPES)
    def test_seed_goal_has_required_fields(self, goal_type):
        """Each P2P seed goal has slug, goal_type, title, description, config."""
        seeds = [g for g in SEED_BOOTSTRAP_GOALS if g['goal_type'] == goal_type]
        assert len(seeds) >= 1, f"No seed goal for {goal_type}"
        for seed in seeds:
            assert 'slug' in seed
            assert 'title' in seed
            assert 'description' in seed
            assert 'config' in seed
            assert 'spark_budget' in seed

    @pytest.mark.parametrize("goal_type", ALL_P2P_TYPES)
    def test_seed_goal_slug_starts_with_bootstrap(self, goal_type):
        """P2P seed goal slugs follow the bootstrap_ naming convention."""
        seeds = [g for g in SEED_BOOTSTRAP_GOALS if g['goal_type'] == goal_type]
        for seed in seeds:
            assert seed['slug'].startswith('bootstrap_'), (
                f"Seed slug '{seed['slug']}' doesn't start with 'bootstrap_'"
            )


# ═══════════════════════════════════════════════════════════════════
# 9. Rate limits — LIMITS dict has entries for all 12 types
# ═══════════════════════════════════════════════════════════════════

class TestRateLimits:

    @pytest.mark.parametrize("goal_type", ALL_P2P_TYPES)
    def test_rate_limit_exists(self, goal_type):
        """Each P2P type has an entry in RedisRateLimiter.LIMITS."""
        assert goal_type in RedisRateLimiter.LIMITS, (
            f"{goal_type} missing from LIMITS"
        )

    @pytest.mark.parametrize("goal_type", ALL_P2P_TYPES)
    def test_rate_limit_is_tuple(self, goal_type):
        """Each rate limit entry is a (max_requests, window_seconds) tuple."""
        limit = RedisRateLimiter.LIMITS[goal_type]
        assert isinstance(limit, tuple)
        assert len(limit) == 2

    @pytest.mark.parametrize("goal_type", ALL_P2P_TYPES)
    def test_rate_limit_values_positive(self, goal_type):
        """Rate limit max_requests and window are positive integers."""
        max_req, window = RedisRateLimiter.LIMITS[goal_type]
        assert isinstance(max_req, int) and max_req > 0
        assert isinstance(window, int) and window > 0


# ═══════════════════════════════════════════════════════════════════
# 10. Config extraction — config vs config_json
# ═══════════════════════════════════════════════════════════════════

class TestConfigExtraction:

    def test_config_key_used(self):
        """Builder uses 'config' key from goal dict."""
        builder = get_prompt_builder('p2p_marketplace')
        goal = {'config': {'category': 'vintage'}}
        prompt = builder(goal)
        assert 'vintage' in prompt

    def test_config_json_fallback(self):
        """Builder falls back to 'config_json' when 'config' absent."""
        builder = get_prompt_builder('p2p_marketplace')
        goal = {'config_json': {'category': 'antiques'}}
        prompt = builder(goal)
        assert 'antiques' in prompt

    def test_config_takes_precedence_over_config_json(self):
        """When both 'config' and 'config_json' present, 'config' wins."""
        builder = get_prompt_builder('p2p_marketplace')
        goal = {
            'config': {'category': 'winner'},
            'config_json': {'category': 'loser'},
        }
        prompt = builder(goal)
        assert 'winner' in prompt

    def test_empty_config_uses_defaults(self):
        """Empty config dict falls back to default values."""
        builder = get_prompt_builder('p2p_marketplace')
        goal = {'config': {}}
        prompt = builder(goal)
        assert 'general' in prompt  # default category

    def test_none_config_uses_defaults(self):
        """None config falls back to default values."""
        builder = get_prompt_builder('p2p_rideshare')
        goal = {'config': None}
        prompt = builder(goal)
        assert 'auto-detect' in prompt  # default region

    def test_missing_config_entirely(self):
        """No config key at all still produces a valid prompt."""
        builder = get_prompt_builder('p2p_health')
        goal = {}
        prompt = builder(goal)
        assert isinstance(prompt, str)
        assert len(prompt) > 100
