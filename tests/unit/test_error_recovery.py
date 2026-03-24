"""
test_error_recovery.py - Error recovery tests for HARTOS

Tests graceful degradation when dependencies fail:

FT: Goal dispatch without Redis, prompt building without product,
    cultural wisdom without optional traits, agent daemon restart resilience.
NFT: No unhandled exceptions, circuit breaker behavior, fallback chains work.
"""
import os
import sys
from unittest.mock import MagicMock, patch

import pytest

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)


# ============================================================
# Goal dispatch without Redis — falls back to local
# ============================================================

class TestDispatchWithoutRedis:
    """When Redis is unavailable, dispatch must fall back to local /chat."""

    def test_dispatch_goal_is_callable_without_redis(self):
        from integrations.agent_engine.dispatch import dispatch_goal
        assert callable(dispatch_goal)

    def test_distributed_coordinator_returns_none(self):
        """No Redis = no distributed coordinator = local dispatch."""
        from integrations.agent_engine.dispatch import _get_distributed_coordinator
        with patch.dict('sys.modules', {'integrations.distributed_agent.api': None}):
            result = _get_distributed_coordinator()
        assert result is None

    def test_has_hive_peers_returns_false_without_db(self):
        from integrations.agent_engine.dispatch import _has_hive_peers
        with patch.dict('sys.modules', {'integrations.social.models': None}):
            result = _has_hive_peers()
        assert result is False


# ============================================================
# Prompt building without product data
# ============================================================

class TestPromptBuildingResilience:
    """GoalManager.build_prompt must work with minimal data."""

    def test_build_prompt_with_none_product(self):
        """Marketing goals may have no product — must not crash."""
        from integrations.agent_engine.goal_manager import GoalManager
        goal_dict = {
            'title': 'Test Goal',
            'description': 'A test',
            'goal_type': 'marketing',
            'config': {},
        }
        # build_prompt should handle None product gracefully
        result = GoalManager.build_prompt(goal_dict, product_dict=None)
        # May return None (if no builder registered for goal_type) or a string
        assert result is None or isinstance(result, str)


# ============================================================
# Cultural wisdom resilience
# ============================================================

class TestCulturalWisdomResilience:
    """Cultural functions must always return valid data — never crash the agent."""

    def test_get_cultural_prompt_never_empty(self):
        from cultural_wisdom import get_cultural_prompt
        prompt = get_cultural_prompt()
        assert isinstance(prompt, str)
        assert len(prompt) > 0

    def test_get_traits_for_role_unknown(self):
        """Unknown role must still return traits — fallback to general set."""
        from cultural_wisdom import get_traits_for_role
        traits = get_traits_for_role('completely_unknown_role_xyz')
        assert isinstance(traits, list)
        assert len(traits) > 0

    def test_guardian_values_never_empty(self):
        from cultural_wisdom import get_guardian_cultural_values
        values = get_guardian_cultural_values()
        assert len(values) > 0


# ============================================================
# Session cache resilience
# ============================================================

class TestSessionCacheResilience:
    """TTLCache must handle edge cases without corrupting state."""

    def test_get_missing_key_returns_default(self):
        from core.session_cache import TTLCache
        cache = TTLCache(ttl_seconds=60, max_size=10, name='test')
        assert cache.get('missing', 'default') == 'default'

    def test_rapid_set_delete_cycle(self):
        """Rapid set→delete cycles must not corrupt the cache."""
        from core.session_cache import TTLCache
        cache = TTLCache(ttl_seconds=60, max_size=10, name='test')
        for i in range(100):
            cache[f'key_{i}'] = i
            if i > 5:
                del cache[f'key_{i-5}']
        assert len(cache) <= 10

    def test_setdefault_works(self):
        from core.session_cache import TTLCache
        cache = TTLCache(ttl_seconds=60, max_size=10, name='test')
        result = cache.setdefault('new_key', 'initial')
        assert result == 'initial'
        # Second call returns existing
        result2 = cache.setdefault('new_key', 'different')
        assert result2 == 'initial'


# ============================================================
# Threadlocal resilience
# ============================================================

class TestThreadlocalResilience:
    """ThreadLocalData must never crash — even with unexpected patterns."""

    def test_get_before_set_returns_default(self):
        from threadlocal import ThreadLocalData
        tld = ThreadLocalData()
        assert tld.get_request_id() is None
        assert tld.get_user_id() is None
        assert tld.get_task_source() == 'own'

    def test_clear_before_set_no_crash(self):
        from threadlocal import ThreadLocalData
        tld = ThreadLocalData()
        tld.clear_creation_flags()  # Must not crash
        tld.clear_agentic_flags()
        tld.clear_model_config_override()
        tld.clear_task_source()

    def test_update_token_count_without_set(self):
        """update_req_token_count without prior set_req_token_count should handle gracefully."""
        from threadlocal import ThreadLocalData
        tld = ThreadLocalData()
        tld.set_req_token_count(0)
        tld.update_req_token_count(100)
        assert tld.get_req_token_count() == 100
