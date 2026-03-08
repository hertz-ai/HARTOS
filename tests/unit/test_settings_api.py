"""
Tests for Settings API endpoints — verified via source code inspection
to avoid langchain_gpt_api import issues on Python 3.11.

Run with: pytest tests/unit/test_settings_api.py -v --noconftest
"""
import inspect
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(__file__))))


class TestSettingsAPIRouteRegistration(unittest.TestCase):
    """Verify settings API routes are registered via source code inspection."""

    @classmethod
    def setUpClass(cls):
        """Read langchain_gpt_api.py source once."""
        src_path = os.path.join(
            os.path.dirname(os.path.dirname(os.path.dirname(__file__))),
            'langchain_gpt_api.py')
        with open(src_path, 'r', encoding='utf-8') as f:
            cls.source = f.read()

    def test_settings_compute_get_route_exists(self):
        """GET /api/settings/compute route is defined."""
        self.assertIn("@app.route('/api/settings/compute', methods=['GET'])",
                       self.source)

    def test_settings_compute_put_route_exists(self):
        """PUT /api/settings/compute route is defined."""
        self.assertIn("@app.route('/api/settings/compute', methods=['PUT'])",
                       self.source)

    def test_settings_provider_get_route_exists(self):
        """GET /api/settings/compute/provider route is defined."""
        self.assertIn("@app.route('/api/settings/compute/provider', methods=['GET'])",
                       self.source)

    def test_settings_provider_join_route_exists(self):
        """POST /api/settings/compute/provider/join route is defined."""
        self.assertIn("@app.route('/api/settings/compute/provider/join', methods=['POST'])",
                       self.source)

    def test_all_settings_endpoints_use_json_endpoint(self):
        """All settings endpoints use @_json_endpoint decorator."""
        # Find all settings handler functions
        for func_name in ('settings_compute_get', 'settings_compute_put',
                          'settings_compute_provider', 'settings_compute_provider_join'):
            # Check decorator is applied
            idx = self.source.find(f'def {func_name}(')
            self.assertGreater(idx, 0, f"Function {func_name} not found")
            # Look backward for @_json_endpoint
            preceding = self.source[max(0, idx-100):idx]
            self.assertIn('@_json_endpoint', preceding,
                          f"Missing @_json_endpoint on {func_name}")


class TestSettingsComputeGetLogic(unittest.TestCase):
    """Test GET /api/settings/compute handler logic."""

    @classmethod
    def setUpClass(cls):
        src_path = os.path.join(
            os.path.dirname(os.path.dirname(os.path.dirname(__file__))),
            'langchain_gpt_api.py')
        with open(src_path, 'r', encoding='utf-8') as f:
            cls.source = f.read()

    def test_get_uses_compute_config(self):
        """Handler calls get_compute_policy()."""
        # Find the function body
        start = self.source.find('def settings_compute_get(')
        end = self.source.find('\ndef ', start + 1)
        body = self.source[start:end]
        self.assertIn('get_compute_policy', body)

    def test_get_reads_peer_node_identity(self):
        """Handler reads provider identity from PeerNode (single source of truth)."""
        start = self.source.find('def settings_compute_get(')
        end = self.source.find('\ndef ', start + 1)
        body = self.source[start:end]
        self.assertIn('electricity_rate_kwh', body)
        self.assertIn('cause_alignment', body)
        self.assertIn('PeerNode', body)


class TestSettingsComputePutLogic(unittest.TestCase):
    """Test PUT /api/settings/compute handler logic."""

    @classmethod
    def setUpClass(cls):
        src_path = os.path.join(
            os.path.dirname(os.path.dirname(os.path.dirname(__file__))),
            'langchain_gpt_api.py')
        with open(src_path, 'r', encoding='utf-8') as f:
            cls.source = f.read()

    def _get_put_body(self):
        start = self.source.find('def settings_compute_put(')
        end = self.source.find('\ndef ', start + 1)
        return self.source[start:end]

    def test_put_has_tier_guard(self):
        """Central tier nodes blocked from enabling metered for hive."""
        body = self._get_put_body()
        self.assertIn("node_tier == 'central'", body)
        self.assertIn('403', body)

    def test_put_writes_to_node_compute_config(self):
        """Policy fields go to NodeComputeConfig."""
        body = self._get_put_body()
        self.assertIn('NodeComputeConfig', body)

    def test_put_writes_to_peer_node(self):
        """Provider identity goes to PeerNode."""
        body = self._get_put_body()
        self.assertIn('PeerNode', body)
        self.assertIn('peer_fields', body)

    def test_put_invalidates_cache(self):
        """PUT invalidates compute_config cache."""
        body = self._get_put_body()
        self.assertIn('invalidate_cache', body)

    def test_put_policy_fields_correct(self):
        """PUT recognizes the right policy fields."""
        body = self._get_put_body()
        for field in ('compute_policy', 'hive_compute_policy', 'max_hive_gpu_pct',
                      'allow_metered_for_hive', 'metered_daily_limit_usd'):
            self.assertIn(f"'{field}'", body, f"Missing policy field: {field}")

    def test_put_peer_fields_correct(self):
        """PUT recognizes the right peer identity fields."""
        body = self._get_put_body()
        for field in ('electricity_rate_kwh', 'cause_alignment'):
            self.assertIn(f"'{field}'", body, f"Missing peer field: {field}")


class TestSettingsProviderLogic(unittest.TestCase):
    """Test provider dashboard and join endpoint logic."""

    @classmethod
    def setUpClass(cls):
        src_path = os.path.join(
            os.path.dirname(os.path.dirname(os.path.dirname(__file__))),
            'langchain_gpt_api.py')
        with open(src_path, 'r', encoding='utf-8') as f:
            cls.source = f.read()

    def test_provider_dashboard_has_contribution_score(self):
        start = self.source.find('def settings_compute_provider(')
        end = self.source.find('\ndef ', start + 1)
        body = self.source[start:end]
        self.assertIn('compute_contribution_score', body)

    def test_provider_dashboard_has_pending_settlements(self):
        start = self.source.find('def settings_compute_provider(')
        end = self.source.find('\ndef ', start + 1)
        body = self.source[start:end]
        self.assertIn('pending', body)
        self.assertIn('settlement', body.lower())

    def test_provider_join_creates_config(self):
        start = self.source.find('def settings_compute_provider_join(')
        end = self.source.find('\ndef ', start + 1)
        body = self.source[start:end]
        self.assertIn('NodeComputeConfig', body)
        self.assertIn('node_id=node_id', body)

    def test_provider_join_sets_default_cause(self):
        start = self.source.find('def settings_compute_provider_join(')
        end = self.source.find('\ndef ', start + 1)
        body = self.source[start:end]
        self.assertIn('democratize_compute', body)

    def test_chat_handler_reads_task_source(self):
        """POST /chat extracts and sets task_source."""
        start = self.source.find('def chat(')
        end = self.source.find('\ndef ', start + 1)
        body = self.source[start:end]
        self.assertIn("task_source", body)
        self.assertIn("set_task_source", body)


if __name__ == '__main__':
    unittest.main()
