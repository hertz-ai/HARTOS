"""
test_device_routing_and_recipes.py - Tests for device_routing_service.py + recipe_sharing.py

Device routing: Routes agent actions (TTS, consent) to the user's best device.
Recipe sharing: Load, summarize, and fork trained agent recipes.
Each test verifies a specific routing decision or data integrity guarantee.

FT: Device priority (phone > desktop > tablet), capability matching,
    recipe load/summary/fork, fork dedup (no overwrite).
NFT: Unknown form factor handling, empty device list safety,
    corrupt recipe resilience, fork filesystem safety.
"""
import os
import sys
import json
import tempfile
from unittest.mock import MagicMock, patch

import pytest

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)


# ============================================================
# Device Routing — picks the best device for TTS/mic
# ============================================================

class TestDeviceRouting:
    """DeviceRoutingService routes TTS to the device with a speaker."""

    def _mock_device(self, form_factor, capabilities=None, is_active=True):
        d = MagicMock()
        d.form_factor = form_factor
        d.capabilities = capabilities or {'tts': True, 'speaker': True}
        d.is_active = is_active
        d.to_dict.return_value = {
            'form_factor': form_factor,
            'capabilities': d.capabilities,
            'is_active': is_active,
        }
        return d

    def test_phone_preferred_over_desktop(self):
        """Phone has speaker + portability — preferred for TTS."""
        from integrations.social.device_routing_service import DeviceRoutingService
        db = MagicMock()
        phone = self._mock_device('phone')
        desktop = self._mock_device('desktop')
        db.query.return_value.filter_by.return_value.all.return_value = [desktop, phone]
        result = DeviceRoutingService.pick_device(db, 'user_1', 'tts')
        assert result is not None
        assert result['form_factor'] == 'phone'

    def test_desktop_preferred_over_tablet(self):
        from integrations.social.device_routing_service import DeviceRoutingService
        db = MagicMock()
        tablet = self._mock_device('tablet')
        desktop = self._mock_device('desktop')
        db.query.return_value.filter_by.return_value.all.return_value = [tablet, desktop]
        result = DeviceRoutingService.pick_device(db, 'user_1', 'tts')
        assert result['form_factor'] == 'desktop'

    def test_returns_none_when_no_devices(self):
        """User with no devices — cloud fallback needed."""
        from integrations.social.device_routing_service import DeviceRoutingService
        db = MagicMock()
        db.query.return_value.filter_by.return_value.all.return_value = []
        result = DeviceRoutingService.pick_device(db, 'user_1', 'tts')
        assert result is None

    def test_returns_none_when_no_capability(self):
        """Device exists but doesn't have the required capability."""
        from integrations.social.device_routing_service import DeviceRoutingService
        db = MagicMock()
        device = self._mock_device('phone', capabilities={'tts': False})
        db.query.return_value.filter_by.return_value.all.return_value = [device]
        result = DeviceRoutingService.pick_device(db, 'user_1', 'tts')
        assert result is None

    def test_unknown_form_factor_lowest_priority(self):
        """Unknown device types get lowest priority — don't crash, just rank last."""
        from integrations.social.device_routing_service import DeviceRoutingService
        db = MagicMock()
        unknown = self._mock_device('smartfridge')
        phone = self._mock_device('phone')
        db.query.return_value.filter_by.return_value.all.return_value = [unknown, phone]
        result = DeviceRoutingService.pick_device(db, 'user_1', 'tts')
        assert result['form_factor'] == 'phone'

    def test_get_user_device_map_returns_list(self):
        from integrations.social.device_routing_service import DeviceRoutingService
        db = MagicMock()
        d1 = self._mock_device('phone')
        d2 = self._mock_device('desktop')
        db.query.return_value.filter_by.return_value.all.return_value = [d1, d2]
        result = DeviceRoutingService.get_user_device_map(db, 'user_1')
        assert isinstance(result, list)
        assert len(result) == 2

    def test_tts_priority_order(self):
        """TTS priority must be: phone > desktop > tablet > tv > embedded > robot."""
        from integrations.social.device_routing_service import _TTS_PRIORITY
        assert _TTS_PRIORITY.index('phone') < _TTS_PRIORITY.index('desktop')
        assert _TTS_PRIORITY.index('desktop') < _TTS_PRIORITY.index('tablet')
        assert _TTS_PRIORITY.index('tablet') < _TTS_PRIORITY.index('robot')


# ============================================================
# Recipe Sharing — load, summarize, fork
# ============================================================

class TestRecipeLoad:
    """load_recipe reads recipe JSON from the prompts directory."""

    def test_returns_none_for_missing_file(self):
        from integrations.social.recipe_sharing import load_recipe
        with patch('integrations.social.recipe_sharing.PROMPTS_DIR', '/nonexistent'):
            result = load_recipe('nonexistent.json')
        assert result is None

    def test_loads_valid_recipe(self):
        from integrations.social.recipe_sharing import load_recipe
        with tempfile.TemporaryDirectory() as tmpdir:
            recipe = {'persona': 'coder', 'action': 'write code', 'recipe': [{'steps': 'step1'}]}
            path = os.path.join(tmpdir, 'test_recipe.json')
            with open(path, 'w') as f:
                json.dump(recipe, f)
            with patch('integrations.social.recipe_sharing.PROMPTS_DIR', tmpdir):
                result = load_recipe('test_recipe.json')
        assert result is not None
        assert result['persona'] == 'coder'

    def test_returns_none_for_corrupt_json(self):
        from integrations.social.recipe_sharing import load_recipe
        with tempfile.TemporaryDirectory() as tmpdir:
            path = os.path.join(tmpdir, 'bad.json')
            with open(path, 'w') as f:
                f.write("not valid json{{{")
            with patch('integrations.social.recipe_sharing.PROMPTS_DIR', tmpdir):
                result = load_recipe('bad.json')
        assert result is None


class TestRecipeSummary:
    """get_recipe_summary returns metadata for the recipe browser UI."""

    def test_returns_error_for_missing(self):
        from integrations.social.recipe_sharing import get_recipe_summary
        with patch('integrations.social.recipe_sharing.PROMPTS_DIR', '/nonexistent'):
            result = get_recipe_summary('missing.json')
        assert 'error' in result

    def test_returns_summary_fields(self):
        from integrations.social.recipe_sharing import get_recipe_summary
        with tempfile.TemporaryDirectory() as tmpdir:
            recipe = {'persona': 'writer', 'action': 'draft', 'recipe': [1, 2, 3]}
            path = os.path.join(tmpdir, 'r.json')
            with open(path, 'w') as f:
                json.dump(recipe, f)
            with patch('integrations.social.recipe_sharing.PROMPTS_DIR', tmpdir):
                result = get_recipe_summary('r.json')
        assert result['persona'] == 'writer'
        assert result['steps'] == 3


class TestRecipeFork:
    """fork_recipe copies a recipe for a new agent — the "remix" mechanic."""

    def test_fork_creates_new_file(self):
        from integrations.social.recipe_sharing import fork_recipe
        with tempfile.TemporaryDirectory() as tmpdir:
            source = os.path.join(tmpdir, 'source.json')
            with open(source, 'w') as f:
                json.dump({'action': 'original'}, f)
            with patch('integrations.social.recipe_sharing.PROMPTS_DIR', tmpdir):
                result = fork_recipe('source.json', 999, 0)
            assert result == '999_0_recipe.json'
            assert os.path.exists(os.path.join(tmpdir, result))

    def test_fork_does_not_overwrite_existing(self):
        """Forking to an existing prompt_id must not overwrite — prevents data loss."""
        from integrations.social.recipe_sharing import fork_recipe
        with tempfile.TemporaryDirectory() as tmpdir:
            source = os.path.join(tmpdir, 'source.json')
            existing = os.path.join(tmpdir, '999_0_recipe.json')
            with open(source, 'w') as f:
                json.dump({'action': 'original'}, f)
            with open(existing, 'w') as f:
                json.dump({'action': 'existing'}, f)
            with patch('integrations.social.recipe_sharing.PROMPTS_DIR', tmpdir):
                result = fork_recipe('source.json', 999, 0)
            assert result is None  # Did not overwrite

    def test_fork_returns_none_for_missing_source(self):
        from integrations.social.recipe_sharing import fork_recipe
        with patch('integrations.social.recipe_sharing.PROMPTS_DIR', '/nonexistent'):
            result = fork_recipe('missing.json', 1, 0)
        assert result is None


# ============================================================
# TTS routing — route_tts picks the best device for voice output
# ============================================================

class TestRouteTTS:
    """route_tts is called when an agent needs to speak to a user."""

    def _mock_device(self, form_factor, capabilities=None, device_id='dev_1'):
        d = MagicMock()
        d.form_factor = form_factor
        d.capabilities = capabilities or {'tts': True, 'speaker': True}
        d.device_id = device_id
        d.to_dict.return_value = {'form_factor': form_factor, 'device_id': device_id}
        return d

    def test_no_devices_returns_error(self):
        """User with no linked devices = TTS fails gracefully."""
        from integrations.social.device_routing_service import DeviceRoutingService
        db = MagicMock()
        db.query.return_value.filter_by.return_value.all.return_value = []
        result = DeviceRoutingService.route_tts(db, 'user_1', 'Hello')
        assert result['success'] is False
        assert 'error' in result

    def test_routes_to_phone(self):
        """Phone with TTS capability = success via fleet_command."""
        from integrations.social.device_routing_service import DeviceRoutingService
        db = MagicMock()
        phone = self._mock_device('phone', device_id='phone_1')
        db.query.return_value.filter_by.return_value.all.return_value = [phone]
        result = DeviceRoutingService.route_tts(db, 'user_1', 'Hello')
        assert result['success'] is True
        assert result['device_id'] == 'phone_1'
        assert result['method'] == 'fleet_command'

    def test_watch_gets_relay(self):
        """Watch + phone: TTS goes to phone with relay_to pointing at watch."""
        from integrations.social.device_routing_service import DeviceRoutingService
        db = MagicMock()
        phone = self._mock_device('phone', device_id='phone_1')
        watch = self._mock_device('watch', capabilities={'tts': False}, device_id='watch_1')
        watch.form_factor = 'watch'
        db.query.return_value.filter_by.return_value.all.return_value = [phone, watch]
        result = DeviceRoutingService.route_tts(db, 'user_1', 'Hello')
        assert result['success'] is True
        assert result['relay_to'] == 'watch_1'

    def test_no_tts_device_falls_back_to_notification(self):
        """If no device has TTS, send a notification instead."""
        from integrations.social.device_routing_service import DeviceRoutingService
        db = MagicMock()
        no_tts = self._mock_device('tablet', capabilities={'tts': False})
        db.query.return_value.filter_by.return_value.all.return_value = [no_tts]
        with patch('integrations.social.device_routing_service.NotificationService') as mock_notif:
            result = DeviceRoutingService.route_tts(db, 'user_1', 'Hello')
        assert result['success'] is True
        assert result['method'] == 'notification_fallback'


# ============================================================
# Recipe summary — what the recipe browser shows
# ============================================================

class TestRecipeSummaryEdgeCases:
    """Edge cases for get_recipe_summary used by the recipe marketplace."""

    def test_summary_with_steps_key(self):
        """Some recipes use 'steps' instead of 'recipe' for the action list."""
        from integrations.social.recipe_sharing import get_recipe_summary
        with tempfile.TemporaryDirectory() as tmpdir:
            recipe = {'persona': 'writer', 'action': 'draft', 'steps': [1, 2]}
            path = os.path.join(tmpdir, 'r.json')
            with open(path, 'w') as f:
                json.dump(recipe, f)
            with patch('integrations.social.recipe_sharing.PROMPTS_DIR', tmpdir):
                result = get_recipe_summary('r.json')
        assert result['steps'] == 2

    def test_summary_has_fallback_indicator(self):
        """has_fallback tells the UI whether a fallback strategy exists."""
        from integrations.social.recipe_sharing import get_recipe_summary
        with tempfile.TemporaryDirectory() as tmpdir:
            recipe = {'persona': 'p', 'action': 'a', 'recipe': [], 'fallback_strategy': 'retry'}
            path = os.path.join(tmpdir, 'r.json')
            with open(path, 'w') as f:
                json.dump(recipe, f)
            with patch('integrations.social.recipe_sharing.PROMPTS_DIR', tmpdir):
                result = get_recipe_summary('r.json')
        assert result['has_fallback'] is True
