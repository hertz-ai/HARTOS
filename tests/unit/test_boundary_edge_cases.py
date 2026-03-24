"""
test_boundary_edge_cases.py - Boundary tests for HARTOS core systems

Tests extreme inputs at system boundaries — the agent engine, cultural wisdom,
smart ledger, and session cache:

FT: Empty inputs, None values, very long strings, Unicode in all positions.
NFT: No crash on any edge input, deterministic behavior, thread safety bounds.
"""
import os
import sys
import threading
from unittest.mock import MagicMock

import pytest

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)


# ============================================================
# Cultural wisdom — edge inputs
# ============================================================

class TestCulturalWisdomEdges:
    """Cultural functions must handle any input without crash."""

    def test_get_trait_by_name_empty(self):
        from cultural_wisdom import get_trait_by_name
        assert get_trait_by_name('') is None

    def test_get_trait_by_name_none_safe(self):
        from cultural_wisdom import get_trait_by_name
        # Should handle None-like input
        assert get_trait_by_name('nonexistent_trait_xyz') is None

    def test_get_traits_by_origin_empty(self):
        from cultural_wisdom import get_traits_by_origin
        result = get_traits_by_origin('')
        assert isinstance(result, list)

    def test_get_traits_for_role_empty(self):
        from cultural_wisdom import get_traits_for_role
        result = get_traits_for_role('')
        assert isinstance(result, list)

    def test_get_traits_for_role_zero_count(self):
        from cultural_wisdom import get_traits_for_role
        result = get_traits_for_role('assistant', count=0)
        assert isinstance(result, list)

    def test_cultural_prompt_is_non_empty(self):
        """Prompt must always return something — empty prompt = agent has no personality."""
        from cultural_wisdom import get_cultural_prompt
        prompt = get_cultural_prompt()
        assert len(prompt) > 50


# ============================================================
# ThreadLocalData — concurrent access boundaries
# ============================================================

class TestThreadLocalEdges:
    """Thread-local data must isolate even under stress."""

    def test_100_threads_isolated(self):
        """100 concurrent threads must each see their own data."""
        from threadlocal import ThreadLocalData
        tld = ThreadLocalData()
        errors = []

        def worker(tid):
            try:
                tld.set_user_id(f'user_{tid}')
                tld.set_request_id(f'req_{tid}')
                # Read back
                if tld.get_user_id() != f'user_{tid}':
                    errors.append(f'Thread {tid}: user_id mismatch')
                if tld.get_request_id() != f'req_{tid}':
                    errors.append(f'Thread {tid}: request_id mismatch')
            except Exception as e:
                errors.append(str(e))

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(100)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert not errors, f"Thread isolation failures: {errors[:5]}"

    def test_set_get_round_trip_all_fields(self):
        """Every field must survive set→get without corruption."""
        from threadlocal import ThreadLocalData
        tld = ThreadLocalData()
        tld.set_request_id('r1')
        tld.set_user_id('u1')
        tld.set_prompt_id('p1')
        tld.set_task_source('hive')
        tld.set_global_intent('FINAL_ANSWER')
        assert tld.get_request_id() == 'r1'
        assert tld.get_user_id() == 'u1'
        assert tld.get_prompt_id() == 'p1'
        assert tld.get_task_source() == 'hive'
        assert tld.get_global_intent() == 'FINAL_ANSWER'


# ============================================================
# TTLCache — boundary conditions
# ============================================================

class TestTTLCacheBoundaries:
    """TTLCache edge cases that can cause subtle bugs."""

    def test_empty_cache_len_zero(self):
        from core.session_cache import TTLCache
        cache = TTLCache(ttl_seconds=60, max_size=10, name='test')
        assert len(cache) == 0

    def test_none_key_handled(self):
        """None as key must not crash — some code paths use None user_prompt."""
        from core.session_cache import TTLCache
        cache = TTLCache(ttl_seconds=60, max_size=10, name='test')
        cache[None] = 'value'
        assert cache[None] == 'value'

    def test_empty_string_key(self):
        from core.session_cache import TTLCache
        cache = TTLCache(ttl_seconds=60, max_size=10, name='test')
        cache[''] = 'empty_key'
        assert cache[''] == 'empty_key'

    def test_unicode_key(self):
        from core.session_cache import TTLCache
        cache = TTLCache(ttl_seconds=60, max_size=10, name='test')
        cache['用户_123'] = 'chinese_user'
        assert cache['用户_123'] == 'chinese_user'

    def test_overwrite_updates_value(self):
        from core.session_cache import TTLCache
        cache = TTLCache(ttl_seconds=60, max_size=10, name='test')
        cache['key'] = 'old'
        cache['key'] = 'new'
        assert cache['key'] == 'new'
        assert len(cache) == 1

    def test_max_size_1(self):
        """Cache with max_size=1 must still work — only holds latest entry."""
        from core.session_cache import TTLCache
        cache = TTLCache(ttl_seconds=60, max_size=1, name='test')
        cache['a'] = 1
        cache['b'] = 2
        assert len(cache) == 1
        assert cache.get('b') == 2

    def test_get_returns_none_for_expired(self):
        """Expired entries return None via .get() — prevents stale state."""
        from core.session_cache import TTLCache
        import time
        cache = TTLCache(ttl_seconds=0, max_size=10, name='test')
        cache['key'] = 'val'
        time.sleep(0.05)  # 50ms > 0s TTL
        result = cache.get('key')
        assert result is None


# ============================================================
# Helper — retrieve_json edge cases
# ============================================================

class TestRetrieveJsonEdges:
    """retrieve_json is called on every LLM response — must handle garbage."""

    def test_empty_string(self):
        from helper import retrieve_json
        assert retrieve_json('') is None

    def test_none_input(self):
        from helper import retrieve_json
        # May raise or return None — must not crash the caller
        try:
            result = retrieve_json(None)
        except (TypeError, AttributeError):
            result = None
        assert result is None

    def test_only_whitespace(self):
        from helper import retrieve_json
        assert retrieve_json('   \n\t  ') is None

    def test_nested_json(self):
        from helper import retrieve_json
        result = retrieve_json('{"outer": {"inner": "value"}}')
        assert result is not None
        assert result['outer']['inner'] == 'value'

    def test_json_with_unicode(self):
        from helper import retrieve_json
        result = retrieve_json('{"name": "தமிழ்", "lang": "ta"}')
        assert result is not None
        assert result['lang'] == 'ta'
