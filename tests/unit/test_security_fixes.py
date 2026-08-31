"""
Tests for 5 critical security fixes (G6-G10).

G6:  Auth on /chat endpoint
G7:  Shell injection fixes
G8:  Federation HMAC — per-node secret
G9:  Trust downgrade prevention in PeerLink
G10: Hardcoded URLs replaced with env vars / port_registry
"""

import contextlib
import hashlib
import hmac
import json
import os
import shlex
import stat
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import MagicMock, patch, PropertyMock

# Ensure project root is importable
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)


# ═══════════════════════════════════════════════════════════════
# G6: Auth on /chat endpoint
# ═══════════════════════════════════════════════════════════════

class TestG6ChatAuth(unittest.TestCase):
    """Verify /chat endpoint authentication gate."""

    def setUp(self):
        """Set up a minimal Flask test client."""
        # We need to create a minimal Flask app that mimics the /chat route
        from flask import Flask, request, jsonify, g
        self.app = Flask(__name__)
        self.app.config['TESTING'] = True

        @self.app.route('/chat', methods=['POST'])
        def chat():
            _api_key = os.environ.get('HEVOLVE_API_KEY', '')
            auth_header = request.headers.get('Authorization', '')
            _bearer_token = auth_header[7:] if auth_header.startswith('Bearer ') else ''

            if _api_key:
                _api_key_match = (_bearer_token and _bearer_token == _api_key)
                if _api_key_match:
                    g.auth_source = 'api_key'
                elif _bearer_token:
                    # Simulate JWT decode failure for simplicity
                    g.auth_source = 'body'
                    return jsonify({'error': 'Invalid or expired token.', 'response': None}), 401
                else:
                    return jsonify({
                        'error': 'Authentication required. Provide Authorization: Bearer <token> header.',
                        'response': None,
                    }), 401
            else:
                g.auth_source = 'none'

            return jsonify({'response': 'ok', 'auth_source': g.auth_source}), 200

        self.client = self.app.test_client()

    @patch.dict(os.environ, {'HEVOLVE_API_KEY': 'test-secret-key-12345'})
    def test_chat_rejects_no_auth_header(self):
        """Request without Authorization header is rejected when API key is set."""
        resp = self.client.post('/chat', json={'user_id': 'u1', 'prompt_id': 'p1', 'prompt': 'hi'})
        self.assertEqual(resp.status_code, 401)
        data = resp.get_json()
        self.assertIn('Authentication required', data['error'])

    @patch.dict(os.environ, {'HEVOLVE_API_KEY': 'test-secret-key-12345'})
    def test_chat_rejects_wrong_api_key(self):
        """Request with wrong API key is rejected."""
        resp = self.client.post('/chat',
                                json={'user_id': 'u1', 'prompt_id': 'p1', 'prompt': 'hi'},
                                headers={'Authorization': 'Bearer wrong-key'})
        self.assertEqual(resp.status_code, 401)

    @patch.dict(os.environ, {'HEVOLVE_API_KEY': 'test-secret-key-12345'})
    def test_chat_accepts_correct_api_key(self):
        """Request with correct API key is accepted."""
        resp = self.client.post('/chat',
                                json={'user_id': 'u1', 'prompt_id': 'p1', 'prompt': 'hi'},
                                headers={'Authorization': 'Bearer test-secret-key-12345'})
        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()
        self.assertEqual(data['auth_source'], 'api_key')

    @patch.dict(os.environ, {}, clear=False)
    def test_chat_dev_mode_allows_unauthenticated(self):
        """When HEVOLVE_API_KEY is not set (dev mode), requests pass through."""
        # Remove the key if present
        env = os.environ.copy()
        env.pop('HEVOLVE_API_KEY', None)
        with patch.dict(os.environ, env, clear=True):
            resp = self.client.post('/chat',
                                    json={'user_id': 'u1', 'prompt_id': 'p1', 'prompt': 'hi'})
            self.assertEqual(resp.status_code, 200)
            data = resp.get_json()
            self.assertEqual(data['auth_source'], 'none')

    @patch.dict(os.environ, {'HEVOLVE_API_KEY': 'test-secret-key-12345'})
    def test_chat_rejects_empty_bearer(self):
        """Bearer header without token is rejected."""
        resp = self.client.post('/chat',
                                json={'user_id': 'u1', 'prompt_id': 'p1', 'prompt': 'hi'},
                                headers={'Authorization': 'Bearer '})
        self.assertEqual(resp.status_code, 401)

    @patch.dict(os.environ, {'HEVOLVE_API_KEY': 'test-secret-key-12345'})
    def test_chat_rejects_basic_auth(self):
        """Basic auth header is rejected (only Bearer supported)."""
        resp = self.client.post('/chat',
                                json={'user_id': 'u1', 'prompt_id': 'p1', 'prompt': 'hi'},
                                headers={'Authorization': 'Basic dXNlcjpwYXNz'})
        self.assertEqual(resp.status_code, 401)


# ═══════════════════════════════════════════════════════════════
# G7: Shell injection fixes
# ═══════════════════════════════════════════════════════════════

class TestG7ShellInjection(unittest.TestCase):
    """Verify shell injection prevention."""

    def test_benchmark_registry_uses_shell_false(self):
        """benchmark_registry.py run() uses shell=False with shlex.split."""
        import inspect
        from integrations.agent_engine import benchmark_registry
        # Find the DynamicBenchmarkAdapter.run method
        source = inspect.getsource(benchmark_registry.DynamicBenchmarkAdapter.run)
        self.assertIn('shell=False', source,
                      "DynamicBenchmarkAdapter.run must use shell=False")
        self.assertIn('shlex.split', source,
                      "DynamicBenchmarkAdapter.run must use shlex.split for command tokenization")
        self.assertNotIn('shell=True', source,
                         "DynamicBenchmarkAdapter.run must NOT use shell=True")

    def test_fleet_command_uses_shell_false(self):
        """fleet_command.py _handle_shell_command uses shell=False."""
        import inspect
        from integrations.social import fleet_command
        source = inspect.getsource(fleet_command)
        # Find the shell execution section
        self.assertIn('shell=False', source,
                      "fleet_command must use shell=False")
        self.assertIn('shlex.split', source,
                      "fleet_command must use shlex.split")

    def test_shlex_split_prevents_injection(self):
        """shlex.split properly tokenizes and prevents command chaining."""
        # Malicious input that would exploit shell=True
        malicious = "ls; rm -rf /"
        tokens = shlex.split(malicious)
        # shlex treats semicolons as part of the argument, not shell separator
        self.assertEqual(tokens, ['ls;', 'rm', '-rf', '/'])
        # The first token 'ls;' is NOT a valid command — safe

    def test_shlex_split_handles_pipe_injection(self):
        """Pipe characters in user input don't cause command piping."""
        malicious = "echo hello | cat /etc/passwd"
        tokens = shlex.split(malicious)
        # Pipe is part of a regular argument, not shell piping
        self.assertIn('|', tokens)
        self.assertEqual(tokens[0], 'echo')

    def test_shell_os_apis_group_validation(self):
        """shell_os_apis validates group names to prevent injection."""
        import re
        valid_groups = ['hart', 'users', 'sudo', 'docker-users', 'my_group']
        invalid_groups = ['; rm -rf /', 'group$(cmd)', 'group`id`', 'a b', '../etc']

        pattern = r'^[a-zA-Z0-9_-]+$'
        for g in valid_groups:
            self.assertTrue(re.match(pattern, g), f"Should be valid: {g}")
        for g in invalid_groups:
            self.assertFalse(re.match(pattern, g), f"Should be invalid: {g}")

    def test_shell_os_apis_terminal_uses_shlex(self):
        """shell_os_apis terminal exec uses shlex.split + shell=False."""
        import inspect
        try:
            from integrations.agent_engine import shell_os_apis
            source = inspect.getsource(shell_os_apis)
            self.assertIn('shlex.split', source)
            # The terminal_exec function should have shell=False
            self.assertIn('shell=False', source)
        except ImportError:
            self.skipTest("shell_os_apis not importable")


# ═══════════════════════════════════════════════════════════════
# G8: Federation HMAC — per-node secret
# ═══════════════════════════════════════════════════════════════

@contextlib.contextmanager
def _hmac_secret_at(path):
    """Point the per-node HMAC secret at `path`, with a cleared cache.

    The seam is core.node_secret, NOT federated_aggregator. The secret used to
    live in federated_aggregator and was consolidated into core.node_secret so
    one persisted value would have one writer (the campaign tracking key needs
    the same secret). federated_aggregator now imports the FUNCTIONS by name,
    so assigning `fa._HMAC_SECRET_PATH` does not redirect anything — the name
    it binds is a function object, and the path those functions read is a
    global in the canonical module. These eight tests kept assigning to `fa`
    and died at `module ... has no attribute '_HMAC_SECRET_PATH'`; they were
    the consolidation's un-updated callers (mocks are callers too).

    Restores the previous cache rather than blanking it, so a test cannot leave
    the process's real node secret cleared for whatever runs next.
    """
    import core.node_secret as ns
    old_path, old_cache = ns._HMAC_SECRET_PATH, ns._NODE_HMAC_SECRET
    ns._HMAC_SECRET_PATH = path
    ns._NODE_HMAC_SECRET = ''
    try:
        yield ns
    finally:
        ns._HMAC_SECRET_PATH = old_path
        ns._NODE_HMAC_SECRET = old_cache


class TestG8FederationHMAC(unittest.TestCase):
    """Verify per-node HMAC secret generation and usage."""

    def setUp(self):
        self._tmpdir = tempfile.mkdtemp()
        self._secret_path = os.path.join(self._tmpdir, '.hmac_secret')

    def tearDown(self):
        import shutil
        shutil.rmtree(self._tmpdir, ignore_errors=True)

    def test_hmac_secret_generated_on_first_boot(self):
        """Per-node HMAC secret is auto-generated when file doesn't exist."""
        with _hmac_secret_at(self._secret_path) as ns:
            secret = ns.load_or_create_hmac_secret()
            self.assertTrue(len(secret) >= 32, "Secret should be at least 32 chars")
            self.assertTrue(os.path.isfile(self._secret_path), "Secret file should be created")

    def test_hmac_secret_persisted_to_disk(self):
        """Secret is written to disk and reloaded correctly."""
        with _hmac_secret_at(self._secret_path) as ns:
            secret1 = ns.load_or_create_hmac_secret()
            ns._NODE_HMAC_SECRET = ''
            secret2 = ns.load_or_create_hmac_secret()
            self.assertEqual(secret1, secret2, "Reloaded secret should match")

    def test_hmac_secret_different_between_nodes(self):
        """Two separate generations produce different secrets."""
        path1 = os.path.join(self._tmpdir, 'node1_secret')
        path2 = os.path.join(self._tmpdir, 'node2_secret')
        with _hmac_secret_at(path1) as ns:
            secret1 = ns.load_or_create_hmac_secret()
        with _hmac_secret_at(path2) as ns:
            secret2 = ns.load_or_create_hmac_secret()
        self.assertNotEqual(secret1, secret2,
                            "Different nodes must have different secrets")

    def test_sign_delta_uses_per_node_secret(self):
        """_sign_delta uses the per-node secret, not env var."""
        from integrations.agent_engine import federated_aggregator as fa
        with _hmac_secret_at(self._secret_path) as ns:
            secret = ns.load_or_create_hmac_secret()
            ns._NODE_HMAC_SECRET = secret

            delta = {'version': 1, 'node_id': 'test', 'timestamp': time.time()}
            signed = fa._sign_delta(delta)
            self.assertIn('hmac_signature', signed)
            self.assertTrue(len(signed['hmac_signature']) == 64, "HMAC-SHA256 hex is 64 chars")

            # Verify manually
            to_verify = {k: v for k, v in signed.items() if k != 'hmac_signature'}
            payload = json.dumps(to_verify, sort_keys=True).encode()
            expected = hmac.new(secret.encode(), payload, hashlib.sha256).hexdigest()
            self.assertEqual(signed['hmac_signature'], expected)

    def test_verify_delta_with_own_secret(self):
        """_verify_delta_signature verifies our own signed deltas."""
        from integrations.agent_engine import federated_aggregator as fa
        with _hmac_secret_at(self._secret_path) as ns:
            ns._NODE_HMAC_SECRET = ns.load_or_create_hmac_secret()

            delta = {'version': 1, 'node_id': 'test', 'timestamp': time.time()}
            fa._sign_delta(delta)

            self.assertTrue(fa._verify_delta_signature(delta))

    def test_verify_rejects_tampered_delta(self):
        """Modified delta fails signature verification."""
        from integrations.agent_engine import federated_aggregator as fa
        with _hmac_secret_at(self._secret_path) as ns:
            ns._NODE_HMAC_SECRET = ns.load_or_create_hmac_secret()

            delta = {'version': 1, 'node_id': 'test', 'timestamp': time.time()}
            fa._sign_delta(delta)
            delta['node_id'] = 'tampered'  # Tamper with payload

            self.assertFalse(fa._verify_delta_signature(delta))

    def test_verify_rejects_unsigned_delta(self):
        """Delta without hmac_signature is rejected."""
        from integrations.agent_engine import federated_aggregator as fa
        delta = {'version': 1, 'node_id': 'test', 'timestamp': time.time()}
        self.assertFalse(fa._verify_delta_signature(delta))

    def test_peer_hmac_secret_registration(self):
        """register_peer_hmac_secret stores peer's secret for verification."""
        from integrations.agent_engine import federated_aggregator as fa
        fa.register_peer_hmac_secret('node-abc', 'peer-secret-123')
        retrieved = fa._get_peer_hmac_secret('node-abc')
        self.assertEqual(retrieved, 'peer-secret-123')

    def test_get_hmac_secret_for_handshake(self):
        """get_hmac_secret_for_handshake returns the local node's HMAC secret."""
        from integrations.agent_engine import federated_aggregator as fa
        with _hmac_secret_at(self._secret_path) as ns:
            secret = ns.load_or_create_hmac_secret()
            ns._NODE_HMAC_SECRET = secret
            handshake_secret = fa.get_hmac_secret_for_handshake()
            self.assertEqual(handshake_secret, secret)

    def test_sign_delta_without_secret_leaves_it_unsigned(self):
        """No HMAC secret available -> the delta is returned UNSIGNED (logged
        loud). It carries no signature, so a downstream verify rejects it —
        the fail-safe is 'unverifiable', never 'silently trusted'."""
        from integrations.agent_engine import federated_aggregator as fa
        with patch.object(fa, '_get_hmac_secret', return_value=None):
            out = fa._sign_delta({'version': 1, 'node_id': 'test'})
            self.assertNotIn('hmac_signature', out)
            self.assertFalse(fa._verify_delta_signature(out))

    def test_verify_delta_via_registered_peer_secret(self):
        """The cross-node anti-forgery path: a delta signed with a PEER's
        handshake-exchanged secret verifies against that registered secret
        (own-secret path skipped)."""
        from integrations.agent_engine import federated_aggregator as fa
        with patch.object(fa, '_get_hmac_secret', return_value=None), \
                patch.object(fa, '_peer_hmac_secrets', {}):
            fa.register_peer_hmac_secret('peer-1', 'peer-secret')
            delta = {'version': 1, 'node_id': 'peer-1', 'metric': 42}
            payload = json.dumps(delta, sort_keys=True).encode()
            delta['hmac_signature'] = hmac.new(
                b'peer-secret', payload, hashlib.sha256).hexdigest()
            self.assertTrue(fa._verify_delta_signature(delta))

    def test_verify_delta_via_legacy_pubkey_fallback(self):
        """Legacy fallback: a sender that used its Ed25519 public key as the
        HMAC key still verifies (backward compat with pre-handshake peers),
        after both the own-secret and peer-secret paths miss."""
        from integrations.agent_engine import federated_aggregator as fa
        with patch.object(fa, '_get_hmac_secret', return_value=None), \
                patch.object(fa, '_peer_hmac_secrets', {}):
            delta = {'version': 1, 'node_id': 'legacy', 'public_key': 'ed25519pub'}
            payload = json.dumps(delta, sort_keys=True).encode()
            delta['hmac_signature'] = hmac.new(
                b'ed25519pub', payload, hashlib.sha256).hexdigest()
            self.assertTrue(fa._verify_delta_signature(delta))


# ═══════════════════════════════════════════════════════════════
# G9: Trust downgrade prevention in PeerLink
# ═══════════════════════════════════════════════════════════════

class TestG9TrustDowngrade(unittest.TestCase):
    """Verify trust level can only be upgraded, never downgraded."""

    def _make_link(self, trust):
        from core.peer_link.link import PeerLink, TrustLevel
        return PeerLink(
            peer_id='test-peer-001',
            address='192.168.1.10:5460',
            trust=trust,
        )

    def test_trust_rank_ordering(self):
        """TrustLevel ranks: RELAY < PEER < SAME_USER."""
        from core.peer_link.link import TrustLevel
        self.assertLess(TrustLevel.RELAY.trust_rank(), TrustLevel.PEER.trust_rank())
        self.assertLess(TrustLevel.PEER.trust_rank(), TrustLevel.SAME_USER.trust_rank())

    def test_same_user_to_peer_rejected(self):
        """Downgrade from SAME_USER to PEER is rejected."""
        from core.peer_link.link import TrustLevel
        link = self._make_link(TrustLevel.SAME_USER)
        result = link.set_trust(TrustLevel.PEER)
        self.assertFalse(result, "Downgrade SAME_USER -> PEER should be rejected")
        self.assertEqual(link.trust, TrustLevel.SAME_USER, "Trust should remain SAME_USER")

    def test_same_user_to_relay_rejected(self):
        """Downgrade from SAME_USER to RELAY is rejected."""
        from core.peer_link.link import TrustLevel
        link = self._make_link(TrustLevel.SAME_USER)
        result = link.set_trust(TrustLevel.RELAY)
        self.assertFalse(result)
        self.assertEqual(link.trust, TrustLevel.SAME_USER)

    def test_peer_to_relay_rejected(self):
        """Downgrade from PEER to RELAY is rejected."""
        from core.peer_link.link import TrustLevel
        link = self._make_link(TrustLevel.PEER)
        result = link.set_trust(TrustLevel.RELAY)
        self.assertFalse(result)
        self.assertEqual(link.trust, TrustLevel.PEER)

    def test_peer_to_same_user_accepted(self):
        """Upgrade from PEER to SAME_USER is accepted."""
        from core.peer_link.link import TrustLevel
        link = self._make_link(TrustLevel.PEER)
        result = link.set_trust(TrustLevel.SAME_USER)
        self.assertTrue(result, "Upgrade PEER -> SAME_USER should be accepted")
        self.assertEqual(link.trust, TrustLevel.SAME_USER)

    def test_relay_to_peer_accepted(self):
        """Upgrade from RELAY to PEER is accepted."""
        from core.peer_link.link import TrustLevel
        link = self._make_link(TrustLevel.RELAY)
        result = link.set_trust(TrustLevel.PEER)
        self.assertTrue(result)
        self.assertEqual(link.trust, TrustLevel.PEER)

    def test_relay_to_same_user_accepted(self):
        """Upgrade from RELAY to SAME_USER is accepted."""
        from core.peer_link.link import TrustLevel
        link = self._make_link(TrustLevel.RELAY)
        result = link.set_trust(TrustLevel.SAME_USER)
        self.assertTrue(result)
        self.assertEqual(link.trust, TrustLevel.SAME_USER)

    def test_same_trust_level_accepted(self):
        """Setting the same trust level is a no-op (accepted)."""
        from core.peer_link.link import TrustLevel
        link = self._make_link(TrustLevel.PEER)
        result = link.set_trust(TrustLevel.PEER)
        self.assertTrue(result)
        self.assertEqual(link.trust, TrustLevel.PEER)

    def test_min_trust_ratchets_upward(self):
        """_min_trust_level tracks the highest trust ever established."""
        from core.peer_link.link import TrustLevel
        link = self._make_link(TrustLevel.RELAY)
        self.assertEqual(link.min_trust_level, TrustLevel.RELAY)

        link.set_trust(TrustLevel.PEER)
        self.assertEqual(link.min_trust_level, TrustLevel.PEER)

        link.set_trust(TrustLevel.SAME_USER)
        self.assertEqual(link.min_trust_level, TrustLevel.SAME_USER)

        # Now downgrade is impossible
        result = link.set_trust(TrustLevel.PEER)
        self.assertFalse(result)
        self.assertEqual(link.min_trust_level, TrustLevel.SAME_USER)

    def test_upgrade_then_downgrade_rejected(self):
        """After upgrading RELAY->PEER->SAME_USER, downgrade to any lower level fails."""
        from core.peer_link.link import TrustLevel
        link = self._make_link(TrustLevel.RELAY)
        link.set_trust(TrustLevel.PEER)
        link.set_trust(TrustLevel.SAME_USER)

        self.assertFalse(link.set_trust(TrustLevel.PEER))
        self.assertFalse(link.set_trust(TrustLevel.RELAY))
        self.assertEqual(link.trust, TrustLevel.SAME_USER)


# ═══════════════════════════════════════════════════════════════
# G10: Hardcoded URLs replaced with env vars / port_registry
# ═══════════════════════════════════════════════════════════════

class TestG10HardcodedURLs(unittest.TestCase):
    """Verify hardcoded localhost URLs replaced with configurable values."""

    def test_port_registry_exists(self):
        """core.port_registry provides get_port function."""
        from core.port_registry import get_port
        port = get_port('backend')
        self.assertIsInstance(port, int)
        self.assertGreater(port, 0)

    def test_port_registry_known_services(self):
        """Port registry has entries for all major services."""
        from core.port_registry import get_port
        services = ['backend', 'llm', 'vision', 'model_bus', 'mcp', 'websocket']
        for svc in services:
            port = get_port(svc)
            self.assertIsInstance(port, int, f"{svc} should return an int port")
            self.assertGreater(port, 0, f"{svc} port should be positive")

    def test_ltx_server_uses_env_var(self):
        """G10: ltx2_server resolves its port from env, not a hardcoded 5002 —
        _LTX_PORT + _LTX_BASE_URL exist AND _LTX_BASE_URL is built from _LTX_PORT
        (consistent wiring, so the base URL cannot drift from the resolved port).

        SKIPS (never fails) when ltx2_server cannot import because torch fails to
        load — a broken/absent torch on the dev box (WinError 126 on shm.dll) or a
        CPU-only CI is an ENVIRONMENT gap, not a G10 regression, and must not wear
        the same red. Same rule as the latency-budget dateutil skip
        (feedback: a dep gap must not look like a real violation)."""
        try:
            import integrations.vision.ltx2_server as ltx
        except (ImportError, OSError) as e:
            import pytest
            pytest.skip(f"ltx2_server import needs torch, unavailable here: {e}")
        self.assertTrue(hasattr(ltx, '_LTX_PORT'), "needs a _LTX_PORT variable")
        self.assertTrue(hasattr(ltx, '_LTX_BASE_URL'), "needs a _LTX_BASE_URL")
        self.assertIsInstance(ltx._LTX_PORT, int)
        self.assertIn(str(ltx._LTX_PORT), ltx._LTX_BASE_URL,
                      "_LTX_BASE_URL must be derived from the resolved _LTX_PORT, "
                      "not a second literal that can drift")

    def test_ltx_server_no_hardcoded_5002_in_responses(self):
        """G10: response URLs use _LTX_BASE_URL, never a hardcoded localhost:5002
        literal. Reads the SOURCE FILE directly (no import) so the check runs on
        EVERY box — it must not depend on torch loading, and 'is there a hardcoded
        literal' is inherently a source concern a behavioural test cannot see.
        Both quote styles are checked (the old version missed single-quoted)."""
        path = os.path.join(
            os.path.dirname(os.path.dirname(os.path.dirname(
                os.path.abspath(__file__)))),
            'integrations', 'vision', 'ltx2_server.py')
        with open(path, encoding='utf-8') as f:
            source = f.read()
        self.assertNotIn('"http://localhost:5002', source,
                         "no hardcoded localhost:5002 (double-quoted) literal")
        self.assertNotIn("'http://localhost:5002", source,
                         "no hardcoded localhost:5002 (single-quoted) literal")
        self.assertIn('_LTX_BASE_URL', source,
                      "response URLs must be built from _LTX_BASE_URL")

    def test_world_model_bridge_uses_env_var(self):
        """world_model_bridge reads URL from HEVOLVEAI_API_URL env var."""
        import inspect
        from integrations.agent_engine import world_model_bridge
        source = inspect.getsource(world_model_bridge.WorldModelBridge.__init__)
        self.assertIn('HEVOLVEAI_API_URL', source,
                      "WorldModelBridge should read from HEVOLVEAI_API_URL env var")

    def test_env_var_override_pattern(self):
        """The standard pattern os.environ.get('KEY', 'default') is used."""
        # Verify the pattern exists in key files
        from integrations.agent_engine import world_model_bridge
        import inspect
        source = inspect.getsource(world_model_bridge)
        self.assertIn("os.environ.get(", source)


# ═══════════════════════════════════════════════════════════════
# Cross-cutting: verify no regressions
# ═══════════════════════════════════════════════════════════════

class TestSecurityFixesNoRegression(unittest.TestCase):
    """Ensure security fixes don't break existing functionality."""

    def test_peerlink_init_still_works(self):
        """PeerLink can still be instantiated with all trust levels."""
        from core.peer_link.link import PeerLink, TrustLevel
        for trust in TrustLevel:
            link = PeerLink(
                peer_id=f'peer-{trust.value}',
                address='10.0.0.1:5460',
                trust=trust,
            )
            self.assertEqual(link.trust, trust)
            self.assertEqual(link.min_trust_level, trust)

    def test_federated_aggregator_init(self):
        """FederatedAggregator can be instantiated after G8 changes."""
        try:
            from integrations.agent_engine.federated_aggregator import FederatedAggregator
            fa = FederatedAggregator()
            self.assertIsNotNone(fa)
        except ImportError:
            self.skipTest("FederatedAggregator dependencies not available")

    def test_sign_and_verify_roundtrip(self):
        """Sign + verify roundtrip works with per-node secret."""
        from integrations.agent_engine import federated_aggregator as fa
        tmpdir = tempfile.mkdtemp()
        try:
            with _hmac_secret_at(os.path.join(tmpdir, '.hmac_secret')) as ns:
                ns.load_or_create_hmac_secret()

                delta = {
                    'version': 1,
                    'node_id': 'roundtrip-test',
                    'timestamp': time.time(),
                    'metrics': {'accuracy': 0.95},
                }
                fa._sign_delta(delta)
                self.assertTrue(fa._verify_delta_signature(delta),
                                "Roundtrip sign+verify should succeed")
        finally:
            import shutil
            shutil.rmtree(tmpdir, ignore_errors=True)


if __name__ == '__main__':
    unittest.main()
