"""
Tests for restorable TTLCache - loader callback and cache_loaders.

Verifies that TTLCache auto-loads from persistent storage on cache miss,
and that individual loader functions correctly restore data from disk/Redis.
"""

import os
import sys
import json
import time
import pytest
import tempfile
import shutil
from unittest.mock import patch, MagicMock

# Add project root to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.session_cache import TTLCache


# =============================================================================
# TTLCache loader callback tests
# =============================================================================

class TestTTLCacheLoader:
    """Test TTLCache loader callback behavior."""

    def test_loader_called_on_miss(self):
        """Loader should be called when key is not in cache."""
        loader = MagicMock(return_value={'restored': True})
        cache = TTLCache(ttl_seconds=60, max_size=10, name='test', loader=loader)

        result = cache['my_key']

        loader.assert_called_once_with('my_key')
        assert result == {'restored': True}

    def test_loader_not_called_on_hit(self):
        """Loader should NOT be called when key exists and is not expired."""
        loader = MagicMock(return_value={'restored': True})
        cache = TTLCache(ttl_seconds=60, max_size=10, name='test', loader=loader)

        cache['my_key'] = {'cached': True}
        result = cache['my_key']

        loader.assert_not_called()
        assert result == {'cached': True}

    def test_loader_called_on_expired(self):
        """Loader should be called when key exists but is expired."""
        loader = MagicMock(return_value={'restored': True})
        cache = TTLCache(ttl_seconds=1, max_size=10, name='test', loader=loader)

        cache['my_key'] = {'cached': True}
        # Force expiry by backdating the timestamp
        cache._timestamps['my_key'] = time.monotonic() - 2

        result = cache['my_key']

        loader.assert_called_once_with('my_key')
        assert result == {'restored': True}

    def test_loader_returns_none_raises_keyerror(self):
        """KeyError should be raised when loader returns None."""
        loader = MagicMock(return_value=None)
        cache = TTLCache(ttl_seconds=60, max_size=10, name='test', loader=loader)

        with pytest.raises(KeyError):
            cache['missing_key']

        loader.assert_called_once_with('missing_key')

    def test_loader_exception_raises_keyerror(self):
        """KeyError should be raised when loader throws an exception."""
        loader = MagicMock(side_effect=RuntimeError("disk error"))
        cache = TTLCache(ttl_seconds=60, max_size=10, name='test', loader=loader)

        with pytest.raises(KeyError):
            cache['bad_key']

    def test_get_uses_loader(self):
        """cache.get() should use loader on miss and return loaded value."""
        loader = MagicMock(return_value={'loaded': True})
        cache = TTLCache(ttl_seconds=60, max_size=10, name='test', loader=loader)

        result = cache.get('my_key')
        assert result == {'loaded': True}
        loader.assert_called_once_with('my_key')

    def test_get_default_when_loader_returns_none(self):
        """cache.get() should return default when loader returns None."""
        loader = MagicMock(return_value=None)
        cache = TTLCache(ttl_seconds=60, max_size=10, name='test', loader=loader)

        result = cache.get('missing', 'default_val')
        assert result == 'default_val'

    def test_contains_uses_loader(self):
        """'in' operator should use loader on miss."""
        loader = MagicMock(return_value={'exists': True})
        cache = TTLCache(ttl_seconds=60, max_size=10, name='test', loader=loader)

        assert 'my_key' in cache
        loader.assert_called_once_with('my_key')

    def test_contains_false_when_loader_returns_none(self):
        """'in' operator should return False when loader returns None."""
        loader = MagicMock(return_value=None)
        cache = TTLCache(ttl_seconds=60, max_size=10, name='test', loader=loader)

        assert 'missing' not in cache

    def test_setdefault_uses_loader(self):
        """setdefault() should prefer loaded value over default."""
        loader = MagicMock(return_value={'from_loader': True})
        cache = TTLCache(ttl_seconds=60, max_size=10, name='test', loader=loader)

        result = cache.setdefault('my_key', {'default': True})
        assert result == {'from_loader': True}

    def test_setdefault_uses_default_when_loader_returns_none(self):
        """setdefault() should use default when loader returns None."""
        loader = MagicMock(return_value=None)
        cache = TTLCache(ttl_seconds=60, max_size=10, name='test', loader=loader)

        result = cache.setdefault('my_key', {'default': True})
        assert result == {'default': True}

    def test_loaded_value_is_cached(self):
        """After loader fills the cache, subsequent access should NOT call loader again."""
        call_count = 0

        def counting_loader(key):
            nonlocal call_count
            call_count += 1
            return {'count': call_count}

        cache = TTLCache(ttl_seconds=60, max_size=10, name='test', loader=counting_loader)

        # First access - loader called
        result1 = cache['my_key']
        assert result1 == {'count': 1}
        assert call_count == 1

        # Second access - should use cached value, NOT call loader
        result2 = cache['my_key']
        assert result2 == {'count': 1}
        assert call_count == 1

    def test_no_loader_behaves_like_regular_dict(self):
        """Without loader, TTLCache should behave exactly like before."""
        cache = TTLCache(ttl_seconds=60, max_size=10, name='test')

        with pytest.raises(KeyError):
            cache['nonexistent']

        assert cache.get('nonexistent') is None
        assert 'nonexistent' not in cache


# =============================================================================
# Cache loader function tests
# =============================================================================

class TestLoadAgentData:
    """Test load_agent_data loader."""

    def setup_method(self):
        self.tmpdir = tempfile.mkdtemp()
        self.orig_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'agent_data')

    def teardown_method(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_load_existing_agent_data(self):
        """Should load agent_data from JSON file."""
        from core import cache_loaders

        # Create test file
        test_data = {'data': {'user_id': 123, 'flow_idx': 0}}
        os.makedirs(os.path.join(self.tmpdir), exist_ok=True)
        file_path = os.path.join(self.tmpdir, '456_agent_data.json')
        with open(file_path, 'w') as f:
            json.dump(test_data, f)

        with patch.object(cache_loaders, 'AGENT_DATA_DIR', self.tmpdir):
            result = cache_loaders.load_agent_data(456)

        assert result is not None
        assert result['user_id'] == 123
        assert result['flow_idx'] == 0

    def test_load_nonexistent_returns_none(self):
        """Should return None when file doesn't exist."""
        from core import cache_loaders

        with patch.object(cache_loaders, 'AGENT_DATA_DIR', self.tmpdir):
            result = cache_loaders.load_agent_data(999)

        assert result is None

    def test_load_old_format(self):
        """Should handle old format (no 'data' wrapper)."""
        from core import cache_loaders

        test_data = {'user_id': 123, 'direct': True}
        file_path = os.path.join(self.tmpdir, '789_agent_data.json')
        with open(file_path, 'w') as f:
            json.dump(test_data, f)

        with patch.object(cache_loaders, 'AGENT_DATA_DIR', self.tmpdir):
            result = cache_loaders.load_agent_data(789)

        assert result is not None
        assert result['user_id'] == 123
        assert result['direct'] is True


class TestLoadRecipe:
    """Test load_recipe loader."""

    def setup_method(self):
        self.tmpdir = tempfile.mkdtemp()

    def teardown_method(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_load_existing_recipe(self):
        """Should load recipe from prompts dir."""
        from core import cache_loaders

        recipe_data = {
            "flows": [{"actions": ["action1"]}],
            "actions": [{"action": "test", "recipe": ["step1"]}]
        }
        file_path = os.path.join(self.tmpdir, '456_0_recipe.json')
        with open(file_path, 'w') as f:
            json.dump(recipe_data, f)

        with patch.object(cache_loaders, 'PROMPTS_DIR', self.tmpdir):
            with patch('helper.retrieve_json', return_value=recipe_data):
                result = cache_loaders.load_recipe('123_456')

        assert result is not None
        assert result['flows'] == [{"actions": ["action1"]}]

    def test_load_nonexistent_recipe_returns_none(self):
        """Should return None when no recipe file exists."""
        from core import cache_loaders

        with patch.object(cache_loaders, 'PROMPTS_DIR', self.tmpdir):
            result = cache_loaders.load_recipe('123_999')

        assert result is None

    def test_load_recipe_invalid_key_format(self):
        """Should return None for invalid key format."""
        from core import cache_loaders
        assert cache_loaders.load_recipe('invalid') is None


class TestLoadUserLedger:
    """Test load_user_ledger loader."""

    def test_load_existing_ledger(self):
        """Should load ledger from Redis/JSON backend."""
        from core import cache_loaders

        mock_ledger = MagicMock()
        mock_ledger.tasks = {'action_1': MagicMock(), 'action_2': MagicMock()}

        with patch('helper_ledger.create_ledger_with_auto_backend', return_value=mock_ledger) as mock_create:
            with patch('lifecycle_hooks.restore_action_states_from_ledger', return_value=2):
                result = cache_loaders.load_user_ledger('123_456')

        mock_create.assert_called_once_with(123, 456)
        assert result is mock_ledger

    def test_load_empty_ledger_returns_none(self):
        """Should return None when ledger has no tasks."""
        from core import cache_loaders

        mock_ledger = MagicMock()
        mock_ledger.tasks = {}

        with patch('helper_ledger.create_ledger_with_auto_backend', return_value=mock_ledger):
            result = cache_loaders.load_user_ledger('123_456')

        assert result is None

    def test_load_ledger_invalid_key(self):
        """Should return None for invalid key."""
        from core import cache_loaders
        assert cache_loaders.load_user_ledger('invalid') is None
        assert cache_loaders.load_user_ledger('abc_def') is None


# =============================================================================
# Action state restoration tests
# =============================================================================

class TestRestoreActionStates:
    """Test restore_action_states_from_ledger."""

    def setup_method(self):
        from lifecycle_hooks import action_states, _state_lock
        import threading
        with _state_lock:
            action_states.clear()

    def test_restore_from_ledger(self):
        """Should restore action_states from ledger task statuses."""
        from lifecycle_hooks import restore_action_states_from_ledger, action_states, ActionState
        from agent_ledger import TaskStatus as LedgerTaskStatus

        # Create mock ledger with tasks
        mock_task_1 = MagicMock()
        mock_task_1.status = LedgerTaskStatus.COMPLETED
        mock_task_2 = MagicMock()
        mock_task_2.status = LedgerTaskStatus.IN_PROGRESS
        mock_task_3 = MagicMock()
        mock_task_3.status = LedgerTaskStatus.FAILED

        mock_ledger = MagicMock()
        mock_ledger.tasks = {
            'action_1': mock_task_1,
            'action_2': mock_task_2,
            'action_3': mock_task_3,
        }

        restored = restore_action_states_from_ledger('123_456', mock_ledger)

        assert restored == 3
        assert action_states['123_456'][1] == ActionState.TERMINATED  # COMPLETED → TERMINATED
        assert action_states['123_456'][2] == ActionState.IN_PROGRESS
        assert action_states['123_456'][3] == ActionState.ERROR  # FAILED → ERROR

    def test_restore_skips_non_action_tasks(self):
        """Should skip tasks that don't start with 'action_'."""
        from lifecycle_hooks import restore_action_states_from_ledger, action_states
        from agent_ledger import TaskStatus as LedgerTaskStatus

        mock_task = MagicMock()
        mock_task.status = LedgerTaskStatus.COMPLETED

        mock_ledger = MagicMock()
        mock_ledger.tasks = {
            'subtask_1': mock_task,
            'flow_0': mock_task,
        }

        restored = restore_action_states_from_ledger('123_456', mock_ledger)
        assert restored == 0
        assert '123_456' not in action_states

    def test_restore_empty_ledger(self):
        """Should handle empty ledger gracefully."""
        from lifecycle_hooks import restore_action_states_from_ledger

        mock_ledger = MagicMock()
        mock_ledger.tasks = {}

        restored = restore_action_states_from_ledger('123_456', mock_ledger)
        assert restored == 0
