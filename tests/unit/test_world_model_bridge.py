"""
Comprehensive tests for integrations/agent_engine/world_model_bridge.py

Categories:
  FT  — Functional: record_interaction, submit_correction, query_hivemind,
        flush, get_learning_stats, check_health, distribute_skill_packet,
        send_action, ingest_sensor_batch, embodied interactions, federation
  NFT — Non-functional: thread safety, timeout configuration, circuit breaker
  BND — Boundary: empty strings, None, huge payloads, Unicode
  ERR — Error: stub mode, HTTP failures, malformed responses, ImportError
  CTR — Contract: return shapes, singleton, API compatibility
  SEC — Security: consent gate, constitutional filter, CCT gating, redaction
"""
import json
import os
import threading
import time
import unittest
from collections import deque
from unittest.mock import (
    MagicMock, PropertyMock, patch, call,
)

# ---------------------------------------------------------------------------
# Patch heavy external imports BEFORE importing the module under test.
# HevolveAI, security modules, social models, etc. are NOT installed in CI.
# ---------------------------------------------------------------------------

# Minimal CircuitBreaker stub so __init__ doesn't fail
_real_cb_imported = False
try:
    from core.circuit_breaker import CircuitBreaker as _RealCB
    _real_cb_imported = True
except Exception:
    pass


def _make_bridge(**env_overrides):
    """Factory: create a WorldModelBridge with controlled env and mocked deps."""
    env = {
        'HEVOLVEAI_API_URL': '',  # empty ⇒ http_disabled
        'HEVOLVE_NODE_TIER': 'flat',
        'HEVOLVE_WM_FLUSH_BATCH': '5',
        'HEVOLVE_WM_FLUSH_TIMEOUT': '2',
        'HEVOLVE_WM_CORRECTION_TIMEOUT': '3',
        'HEVOLVE_WM_HTTP_TIMEOUT': '1',
    }
    env.update(env_overrides)

    with patch.dict(os.environ, env, clear=False), \
         patch('integrations.agent_engine.world_model_bridge.WorldModelBridge._init_in_process'), \
         patch('integrations.agent_engine.world_model_bridge.WorldModelBridge._start_crawl_integrity_watcher'):
        from integrations.agent_engine.world_model_bridge import WorldModelBridge
        bridge = WorldModelBridge()
    # Prevent lazy _init_in_process retry (avoids importing hart_intelligence)
    bridge._in_process_retry_done = True
    return bridge


# ---------------------------------------------------------------------------
# Test suite
# ---------------------------------------------------------------------------

class TestWorldModelBridgeInit(unittest.TestCase):
    """CTR: Constructor contracts — defaults, env overrides, singleton."""

    def test_default_node_tier(self):
        """CTR: Default node tier is 'flat' when HEVOLVE_NODE_TIER unset."""
        bridge = _make_bridge()
        self.assertEqual(bridge._node_tier, 'flat')

    def test_env_node_tier_override(self):
        """CTR: HEVOLVE_NODE_TIER env var overrides default tier."""
        bridge = _make_bridge(HEVOLVE_NODE_TIER='central')
        self.assertEqual(bridge._node_tier, 'central')

    def test_flush_batch_size_from_env(self):
        """CTR: HEVOLVE_WM_FLUSH_BATCH env controls batch threshold."""
        bridge = _make_bridge(HEVOLVE_WM_FLUSH_BATCH='100')
        self.assertEqual(bridge._flush_batch_size, 100)

    def test_timeout_env_overrides(self):
        """CTR: All three timeout env vars are respected."""
        bridge = _make_bridge(
            HEVOLVE_WM_FLUSH_TIMEOUT='20',
            HEVOLVE_WM_CORRECTION_TIMEOUT='40',
            HEVOLVE_WM_HTTP_TIMEOUT='8',
        )
        self.assertEqual(bridge._timeout_flush, 20)
        self.assertEqual(bridge._timeout_correction, 40)
        self.assertEqual(bridge._timeout_default, 8)

    def test_experience_queue_is_bounded(self):
        """NFT: Experience queue has maxlen=10000 to bound memory."""
        bridge = _make_bridge()
        self.assertEqual(bridge._experience_queue.maxlen, 10000)

    def test_http_disabled_when_no_explicit_url(self):
        """ERR: With no HEVOLVEAI_API_URL and no in-process, HTTP is disabled."""
        bridge = _make_bridge(HEVOLVEAI_API_URL='')
        # _init_in_process is mocked (no-op) so _in_process stays False
        self.assertTrue(bridge._http_disabled)

    def test_singleton_returns_same_instance(self):
        """CTR: get_world_model_bridge() returns the same singleton."""
        import integrations.agent_engine.world_model_bridge as mod
        mod._bridge = None  # reset
        with patch.dict(os.environ, {'HEVOLVEAI_API_URL': ''}, clear=False), \
             patch.object(mod.WorldModelBridge, '_init_in_process'), \
             patch.object(mod.WorldModelBridge, '_start_crawl_integrity_watcher'):
            b1 = mod.get_world_model_bridge()
            b2 = mod.get_world_model_bridge()
            self.assertIs(b1, b2)
        mod._bridge = None  # cleanup


class TestIsExternalTarget(unittest.TestCase):
    """SEC: _is_external_target correctly classifies URLs."""

    def test_localhost_is_local(self):
        """SEC: http://localhost:8000 is NOT external."""
        bridge = _make_bridge()
        bridge._api_url = 'http://localhost:8000'
        self.assertFalse(bridge._is_external_target())

    def test_127_is_local(self):
        """SEC: http://127.0.0.1:8000 is NOT external."""
        bridge = _make_bridge()
        bridge._api_url = 'http://127.0.0.1:8000'
        self.assertFalse(bridge._is_external_target())

    def test_ipv6_loopback_is_local(self):
        """SEC: http://[::1]:8000 is NOT external."""
        bridge = _make_bridge()
        bridge._api_url = 'http://[::1]:8000'
        self.assertFalse(bridge._is_external_target())

    def test_cloud_url_is_external(self):
        """SEC: A real cloud URL IS external and triggers consent gate."""
        bridge = _make_bridge()
        bridge._api_url = 'http://hevolveai.example.com'
        self.assertTrue(bridge._is_external_target())


class TestRecordInteraction(unittest.TestCase):
    """FT: record_interaction queues experiences and triggers flush."""

    def setUp(self):
        self.bridge = _make_bridge(HEVOLVE_WM_FLUSH_BATCH='3')
        self.bridge._http_disabled = True

    @patch('security.secret_redactor.redact_experience', side_effect=lambda e: e)
    @patch('integrations.agent_engine.world_model_bridge.WorldModelBridge._flush_to_world_model')
    def test_records_experience_to_queue(self, mock_flush, mock_redact):
        """FT: A single interaction is appended to the experience queue."""
        self.bridge.record_interaction('u1', 'p1', 'hello', 'world')
        self.assertEqual(len(self.bridge._experience_queue), 1)
        exp = self.bridge._experience_queue[0]
        self.assertEqual(exp['user_id'], 'u1')
        self.assertEqual(exp['prompt_id'], 'p1')

    @patch('security.secret_redactor.redact_experience', side_effect=lambda e: e)
    @patch('integrations.agent_engine.world_model_bridge.WorldModelBridge._flush_to_world_model')
    def test_truncates_prompt_and_response(self, mock_flush, mock_redact):
        """BND: Prompt truncated to 2000 chars, response to 5000."""
        long_prompt = 'x' * 5000
        long_response = 'y' * 10000
        self.bridge.record_interaction('u1', 'p1', long_prompt, long_response)
        exp = self.bridge._experience_queue[0]
        self.assertEqual(len(exp['prompt']), 2000)
        self.assertEqual(len(exp['response']), 5000)

    def test_flush_triggered_at_batch_size(self):
        """FT: Flush fires when queue reaches _flush_batch_size."""
        with patch.object(self.bridge._flush_executor, 'submit') as mock_submit:
            for i in range(3):
                self.bridge.record_interaction(f'u{i}', 'p1', f'q{i}', f'a{i}')
            self.assertTrue(mock_submit.called)

    def test_stats_total_recorded_increments(self):
        """CTR: total_recorded stat increments per interaction."""
        self.bridge.record_interaction('u1', 'p1', 'q', 'a')
        self.bridge.record_interaction('u1', 'p1', 'q2', 'a2')
        self.assertEqual(self.bridge._stats['total_recorded'], 2)

    @patch('security.hive_guardrails.ConstitutionalFilter.check_prompt',
           return_value=(False, 'blocked'))
    def test_constitutional_filter_blocks(self, mock_cf):
        """SEC: Interaction rejected by ConstitutionalFilter is not queued."""
        self.bridge.record_interaction('u1', 'p1', 'harm', 'bad content')
        self.assertEqual(len(self.bridge._experience_queue), 0)

    def test_unicode_content(self):
        """BND: Unicode prompts and responses are stored correctly."""
        self.bridge.record_interaction('u1', 'p1', 'こんにちは世界', '你好世界🌍')
        exp = self.bridge._experience_queue[0]
        self.assertIn('こんにちは', exp['prompt'])

    def test_none_model_id_defaults_to_unknown(self):
        """BND: None model_id is stored as 'unknown'."""
        self.bridge.record_interaction('u1', 'p1', 'q', 'a', model_id=None)
        self.assertEqual(self.bridge._experience_queue[0]['model_id'], 'unknown')

    def test_lazy_in_process_retry(self):
        """FT: First call retries _init_in_process if not yet connected."""
        self.bridge._in_process = False
        self.bridge._in_process_retry_done = False
        with patch.object(self.bridge, '_init_in_process') as mock_init:
            self.bridge.record_interaction('u1', 'p1', 'q', 'a')
            mock_init.assert_called_once()
            self.assertTrue(self.bridge._in_process_retry_done)


class TestFlushToWorldModel(unittest.TestCase):
    """FT/ERR: _flush_to_world_model in-process and HTTP modes."""

    def setUp(self):
        self.bridge = _make_bridge()

    def test_in_process_flush_calls_provider(self):
        """FT: In-process mode calls provider.create_chat_completion for each exp."""
        provider = MagicMock()
        self.bridge._in_process = True
        self.bridge._provider = provider

        batch = [{'prompt': 'q', 'response': 'a', 'source': 'test',
                  'user_id': 'u1', 'prompt_id': 'p1', 'goal_id': None,
                  'model_id': 'm', 'latency_ms': 10, 'node_id': None}]
        self.bridge._flush_to_world_model(batch)
        provider.create_chat_completion.assert_called_once()
        self.assertEqual(self.bridge._stats['total_flushed'], 1)

    @patch('integrations.agent_engine.world_model_bridge.pooled_post')
    def test_http_flush_posts_to_completions_endpoint(self, mock_post):
        """FT: HTTP mode POSTs to /v1/chat/completions."""
        self.bridge._in_process = False
        self.bridge._http_disabled = False
        self.bridge._api_url = 'http://localhost:9999'
        mock_post.return_value = MagicMock(status_code=200)

        batch = [{'prompt': 'q', 'response': 'a', 'source': 'test',
                  'user_id': 'u1', 'prompt_id': 'p1', 'goal_id': None,
                  'model_id': 'm', 'latency_ms': 10, 'node_id': None}]
        self.bridge._flush_to_world_model(batch)
        mock_post.assert_called_once()
        url_arg = mock_post.call_args[0][0]
        self.assertIn('/v1/chat/completions', url_arg)

    def test_http_disabled_skips_flush(self):
        """ERR: HTTP-disabled mode silently skips flush."""
        self.bridge._in_process = False
        self.bridge._http_disabled = True
        # Should not raise
        self.bridge._flush_to_world_model([{'prompt': 'q', 'response': 'a'}])

    @patch('integrations.agent_engine.world_model_bridge.pooled_post')
    def test_consent_gate_filters_external_batch(self, mock_post):
        """SEC: External flush skips experiences without cloud consent."""
        self.bridge._in_process = False
        self.bridge._http_disabled = False
        self.bridge._api_url = 'http://cloud.example.com'

        with patch.object(self.bridge, '_has_cloud_consent', return_value=False):
            batch = [{'prompt': 'q', 'response': 'a', 'user_id': 'u1'}]
            self.bridge._flush_to_world_model(batch)
            mock_post.assert_not_called()

    @patch('integrations.agent_engine.world_model_bridge.pooled_post',
           side_effect=Exception("connection refused"))
    def test_http_failure_records_cb_failure(self, mock_post):
        """ERR: HTTP error triggers circuit breaker failure recording."""
        self.bridge._in_process = False
        self.bridge._http_disabled = False
        self.bridge._api_url = 'http://localhost:9999'

        with patch.object(self.bridge, '_cb_record_failure') as mock_cb:
            batch = [{'prompt': 'q', 'response': 'a', 'source': 'test',
                      'user_id': 'u1', 'prompt_id': 'p1', 'goal_id': None,
                      'model_id': 'm', 'latency_ms': 0, 'node_id': None}]
            self.bridge._flush_to_world_model(batch)
            mock_cb.assert_called()


class TestSubmitCorrection(unittest.TestCase):
    """FT/SEC: submit_correction in-process, HTTP, and consent gate."""

    def setUp(self):
        self.bridge = _make_bridge()
        self.bridge._http_disabled = True

    def test_http_disabled_returns_failure(self):
        """ERR: Correction in bundled/disabled mode returns failure dict."""
        self.bridge._api_url = 'http://localhost:8000'  # local, so consent gate won't fire
        result = self.bridge.submit_correction('orig', 'fixed')
        self.assertFalse(result['success'])
        self.assertIn('bundled', result.get('reason', '').lower())

    @patch('integrations.agent_engine.world_model_bridge.pooled_post')
    def test_http_correction_success(self, mock_post):
        """FT: HTTP correction returns parsed JSON on 200."""
        self.bridge._http_disabled = False
        self.bridge._api_url = 'http://localhost:9999'
        mock_resp = MagicMock(status_code=200)
        mock_resp.json.return_value = {'success': True, 'correction_id': '123'}
        mock_post.return_value = mock_resp

        result = self.bridge.submit_correction('orig', 'fixed')
        self.assertTrue(result['success'])
        self.assertEqual(self.bridge._stats['total_corrections'], 1)

    @patch('integrations.agent_engine.world_model_bridge.pooled_post')
    def test_http_correction_non_200(self, mock_post):
        """ERR: Non-200 response returns failure with status code."""
        self.bridge._http_disabled = False
        self.bridge._api_url = 'http://localhost:9999'
        mock_post.return_value = MagicMock(status_code=500)

        result = self.bridge.submit_correction('orig', 'fixed')
        self.assertFalse(result['success'])
        self.assertIn('500', result['reason'])

    @unittest.skipUnless(
        __import__('importlib').util.find_spec('hevolveai') is not None,
        'hevolveai not installed')
    def test_confidence_clamped(self):
        """BND: Confidence is clamped to [0.0, 1.0]."""
        self.bridge._in_process = True
        provider = MagicMock()
        self.bridge._provider = provider

        with patch('hevolveai.embodied_ai.rl_ef.send_expert_correction',
                   return_value={'success': True}) as mock_send:
            self.bridge.submit_correction('o', 'c', confidence=5.0)
            args = mock_send.call_args
            self.assertLessEqual(args[1]['confidence'], 1.0)

    def test_consent_gate_blocks_external_correction(self):
        """SEC: External HTTP correction blocked without cloud consent."""
        self.bridge._http_disabled = False
        self.bridge._api_url = 'http://cloud.example.com'
        with patch.object(self.bridge, '_has_cloud_consent', return_value=False):
            result = self.bridge.submit_correction(
                'orig', 'fixed', context={'user_id': 'u1'})
            self.assertFalse(result['success'])
            self.assertIn('consent', result['reason'].lower())

    @patch('security.hive_guardrails.ConstitutionalFilter.check_prompt',
           return_value=(False, 'unconstitutional'))
    def test_constitutional_filter_blocks_correction(self, mock_cf):
        """SEC: ConstitutionalFilter rejects harmful correction text."""
        result = self.bridge.submit_correction('orig', 'harmful correction')
        self.assertFalse(result['success'])
        self.assertEqual(result['reason'], 'unconstitutional')

    def test_circuit_breaker_open_blocks_correction(self):
        """ERR: Open circuit breaker returns failure immediately."""
        self.bridge._http_disabled = False
        self.bridge._api_url = 'http://localhost:9999'
        with patch.object(self.bridge, '_cb_is_open', return_value=True):
            result = self.bridge.submit_correction('orig', 'fixed')
            self.assertFalse(result['success'])
            self.assertIn('circuit', result['reason'].lower())

    def test_truncation_of_long_strings(self):
        """BND: Original/corrected truncated to 5000, explanation to 2000."""
        self.bridge._http_disabled = False
        self.bridge._api_url = 'http://localhost:9999'
        with patch('integrations.agent_engine.world_model_bridge.pooled_post') as mock_post:
            mock_post.return_value = MagicMock(status_code=200)
            mock_post.return_value.json.return_value = {'success': True}
            self.bridge.submit_correction(
                'x' * 10000, 'y' * 10000, explanation='z' * 5000)
            body = mock_post.call_args[1]['json']
            self.assertEqual(len(body['original_response']), 5000)
            self.assertEqual(len(body['corrected_response']), 5000)
            self.assertEqual(len(body['explanation']), 2000)


class TestQueryHivemind(unittest.TestCase):
    """FT/SEC: query_hivemind with CCT gating, consent, and fallbacks."""

    def setUp(self):
        self.bridge = _make_bridge()
        self.bridge._http_disabled = True

    def test_returns_none_when_http_disabled(self):
        """ERR: HTTP-disabled mode returns None."""
        with patch.object(self.bridge, '_check_cct_access', return_value=True):
            result = self.bridge.query_hivemind('test query')
        self.assertIsNone(result)

    def test_cct_gate_returns_cached_thought(self):
        """SEC: Without CCT, returns cached federation thought if available."""
        self.bridge._federation_aggregated = {'last_thought': 'cached answer'}
        with patch.object(self.bridge, '_check_cct_access', return_value=False):
            result = self.bridge.query_hivemind('test query')
        self.assertIsNotNone(result)
        self.assertEqual(result['source'], 'cached')
        self.assertTrue(result['cct_gated'])

    def test_cct_gate_returns_none_without_cache(self):
        """SEC: Without CCT and no cache, returns None (graceful degradation)."""
        self.bridge._federation_aggregated = {}
        with patch.object(self.bridge, '_check_cct_access', return_value=False):
            result = self.bridge.query_hivemind('test query')
        self.assertIsNone(result)

    def test_user_hive_opt_out(self):
        """SEC/U3: User opted out of hive participation returns None."""
        with patch.object(self.bridge, '_has_hive_participation', return_value=False):
            result = self.bridge.query_hivemind('test', user_id='opted_out')
        self.assertIsNone(result)

    @patch('integrations.agent_engine.world_model_bridge.pooled_post')
    def test_http_query_success(self, mock_post):
        """FT: HTTP hivemind query returns parsed JSON on 200."""
        self.bridge._http_disabled = False
        self.bridge._api_url = 'http://localhost:9999'
        mock_resp = MagicMock(status_code=200)
        mock_resp.json.return_value = {'thought': 'collective answer'}
        mock_post.return_value = mock_resp

        with patch.object(self.bridge, '_check_cct_access', return_value=True), \
             patch('core.peer_link.link_manager.get_link_manager', side_effect=ImportError):
            result = self.bridge.query_hivemind('test query')
        self.assertIsNotNone(result)
        self.assertEqual(self.bridge._stats['total_hivemind_queries'], 1)

    def test_consent_gate_blocks_external_query(self):
        """SEC: External hivemind query blocked without cloud consent."""
        self.bridge._http_disabled = False
        self.bridge._api_url = 'http://cloud.example.com'
        with patch.object(self.bridge, '_check_cct_access', return_value=True), \
             patch.object(self.bridge, '_has_cloud_consent', return_value=False), \
             patch('core.peer_link.link_manager.get_link_manager', side_effect=ImportError):
            result = self.bridge.query_hivemind('test', user_id='u1')
        self.assertIsNone(result)


class TestCheckHealth(unittest.TestCase):
    """FT/CTR: check_health return shape contracts."""

    def setUp(self):
        self.bridge = _make_bridge()

    def test_in_process_healthy(self):
        """FT: In-process mode with provider reports healthy."""
        self.bridge._in_process = True
        self.bridge._provider = MagicMock()
        result = self.bridge.check_health()
        self.assertTrue(result['healthy'])
        self.assertEqual(result['mode'], 'in_process')

    def test_http_disabled_unhealthy(self):
        """ERR: Disabled mode reports unhealthy."""
        self.bridge._http_disabled = True
        result = self.bridge.check_health()
        self.assertFalse(result['healthy'])
        self.assertEqual(result['mode'], 'disabled')

    @patch('integrations.agent_engine.world_model_bridge.pooled_get')
    def test_http_health_success(self, mock_get):
        """FT: HTTP health check returns healthy on 200."""
        self.bridge._http_disabled = False
        self.bridge._api_url = 'http://localhost:9999'
        mock_resp = MagicMock(status_code=200)
        mock_resp.headers = {'content-type': 'application/json'}
        mock_resp.json.return_value = {'version': '1.0'}
        mock_get.return_value = mock_resp

        result = self.bridge.check_health()
        self.assertTrue(result['healthy'])
        self.assertEqual(result['mode'], 'http')

    @patch('integrations.agent_engine.world_model_bridge.pooled_get',
           side_effect=__import__('requests').RequestException("timeout"))
    def test_http_health_failure(self, mock_get):
        """ERR: HTTP health failure returns unhealthy with error details."""
        self.bridge._http_disabled = False
        self.bridge._api_url = 'http://localhost:9999'
        result = self.bridge.check_health()
        self.assertFalse(result['healthy'])
        self.assertIn('error', result.get('details', {}))

    def test_health_always_has_node_tier(self):
        """CTR: All health responses include node_tier."""
        for scenario in ['in_process', 'disabled', 'http_error']:
            bridge = _make_bridge()
            if scenario == 'in_process':
                bridge._in_process = True
                bridge._provider = MagicMock()
            elif scenario == 'disabled':
                bridge._http_disabled = True
            result = bridge.check_health()
            self.assertIn('node_tier', result, f"Missing node_tier in {scenario}")


class TestGetStats(unittest.TestCase):
    """CTR: get_stats return shape."""

    def test_stats_include_queue_size(self):
        """CTR: Stats dict includes queue_size, api_url, in_process."""
        bridge = _make_bridge()
        stats = bridge.get_stats()
        self.assertIn('queue_size', stats)
        self.assertIn('api_url', stats)
        self.assertIn('in_process', stats)
        self.assertIn('total_recorded', stats)
        self.assertIn('total_flushed', stats)


class TestGetLearningStats(unittest.TestCase):
    """FT: get_learning_stats merges bridge + learning + hivemind."""

    def test_in_process_merges_provider_and_hive(self):
        """FT: In-process stats merge provider.get_stats() + hive.get_stats()."""
        bridge = _make_bridge()
        bridge._in_process = True
        bridge._provider = MagicMock()
        bridge._provider.get_stats.return_value = {'lr': 0.001}
        bridge._hive_mind = MagicMock()
        bridge._hive_mind.get_stats.return_value = {'agents': 5}

        result = bridge.get_learning_stats()
        self.assertEqual(result['learning']['lr'], 0.001)
        self.assertEqual(result['hivemind']['agents'], 5)
        self.assertIn('bridge', result)

    def test_http_disabled_returns_empty_learning(self):
        """ERR: HTTP-disabled mode returns empty learning/hivemind dicts."""
        bridge = _make_bridge()
        bridge._http_disabled = True
        result = bridge.get_learning_stats()
        self.assertEqual(result['learning'], {})
        self.assertEqual(result['hivemind'], {})


class TestDistributeSkillPacket(unittest.TestCase):
    """FT/SEC: distribute_skill_packet with CCT + guardrails."""

    def setUp(self):
        self.bridge = _make_bridge()

    def test_cct_gate_blocks_without_capability(self):
        """SEC: Skill distribution blocked without CCT skill_distribution capability."""
        with patch.object(self.bridge, '_check_cct_access', return_value=False):
            result = self.bridge.distribute_skill_packet({'task_id': 't1'}, 'node1')
        self.assertFalse(result['success'])
        self.assertIn('no_cct', result['reason'])
        self.assertEqual(self.bridge._stats['total_skills_blocked'], 1)

    @patch('security.hive_guardrails.WorldModelSafetyBounds.gate_ralt_export',
           return_value=(False, 'rate_limited'))
    def test_safety_bounds_block(self, mock_gate):
        """SEC: WorldModelSafetyBounds blocks excessive RALT export."""
        with patch.object(self.bridge, '_check_cct_access', return_value=True):
            result = self.bridge.distribute_skill_packet({'task_id': 't1'}, 'node1')
        self.assertFalse(result['success'])
        self.assertEqual(result['reason'], 'rate_limited')

    @patch('security.hive_guardrails.ConstructiveFilter.check_output',
           return_value=(False, 'destructive'))
    @patch('security.hive_guardrails.WorldModelSafetyBounds.gate_ralt_export',
           return_value=(True, ''))
    def test_constructive_filter_blocks(self, mock_gate, mock_cf):
        """SEC: ConstructiveFilter rejects destructive skill descriptions."""
        with patch.object(self.bridge, '_check_cct_access', return_value=True):
            result = self.bridge.distribute_skill_packet(
                {'task_id': 't1', 'description': 'destroy'}, 'node1')
        self.assertFalse(result['success'])


class TestSendAction(unittest.TestCase):
    """FT: send_action HTTP path and safety gate."""

    def setUp(self):
        self.bridge = _make_bridge()

    def test_http_disabled_returns_false(self):
        """ERR: send_action returns False when HTTP disabled."""
        self.bridge._http_disabled = True
        self.assertFalse(self.bridge.send_action({'type': 'motor'}))

    @patch('integrations.agent_engine.world_model_bridge.pooled_post')
    def test_successful_action_returns_true(self, mock_post):
        """FT: Successful action POST returns True."""
        self.bridge._http_disabled = False
        self.bridge._api_url = 'http://localhost:9999'
        mock_post.return_value = MagicMock(status_code=200)

        self.assertTrue(self.bridge.send_action({'type': 'motor'}))

    @patch('integrations.robotics.safety_monitor.get_safety_monitor')
    def test_estop_blocks_action(self, mock_get_monitor):
        """SEC: E-stop active blocks all actions."""
        monitor = MagicMock()
        monitor.is_estopped = True
        mock_get_monitor.return_value = monitor

        self.assertFalse(self.bridge.send_action({'type': 'motor'}))


class TestIngestSensorBatch(unittest.TestCase):
    """FT/BND: ingest_sensor_batch."""

    def test_empty_readings_returns_zero(self):
        """BND: Empty readings list returns 0 without any HTTP call."""
        bridge = _make_bridge()
        self.assertEqual(bridge.ingest_sensor_batch([]), 0)

    @patch('integrations.agent_engine.world_model_bridge.pooled_post')
    def test_successful_ingest(self, mock_post):
        """FT: Successful batch ingest returns count of readings."""
        bridge = _make_bridge()
        bridge._http_disabled = False
        bridge._api_url = 'http://localhost:9999'
        mock_post.return_value = MagicMock(status_code=200)

        readings = [{'sensor': 'temp', 'value': 25.0}] * 5
        count = bridge.ingest_sensor_batch(readings)
        self.assertEqual(count, 5)


class TestEmergencyStop(unittest.TestCase):
    """FT: emergency_stop always uses HTTP (reliability)."""

    @patch('integrations.agent_engine.world_model_bridge.pooled_post')
    def test_estop_sends_to_estop_endpoint(self, mock_post):
        """FT: Emergency stop POSTs to /v1/actions/estop."""
        bridge = _make_bridge()
        bridge._api_url = 'http://localhost:9999'
        mock_post.return_value = MagicMock(status_code=200)

        result = bridge.emergency_stop()
        self.assertTrue(result)
        url = mock_post.call_args[0][0]
        self.assertIn('/v1/actions/estop', url)

    @patch('integrations.agent_engine.world_model_bridge.pooled_post',
           side_effect=__import__('requests').RequestException("network down"))
    def test_estop_failure_returns_false(self, mock_post):
        """ERR: Emergency stop returns False on network failure."""
        bridge = _make_bridge()
        self.assertFalse(bridge.emergency_stop())


class TestRecordEmbodiedInteraction(unittest.TestCase):
    """FT: record_embodied_interaction queues embodied triples."""

    def test_embodied_interaction_queued(self):
        """FT: Action+sensor+outcome triple is appended to experience queue."""
        bridge = _make_bridge()
        bridge.record_embodied_interaction(
            action={'type': 'grasp'},
            sensor_context={'force': 1.2},
            outcome={'success': True},
        )
        self.assertEqual(len(bridge._experience_queue), 1)
        exp = bridge._experience_queue[0]
        self.assertEqual(exp['type'], 'embodied_interaction')
        self.assertEqual(exp['action']['type'], 'grasp')


class TestFederation(unittest.TestCase):
    """FT: Federation delta extraction and application."""

    def test_extract_learning_delta_shape(self):
        """CTR: extract_learning_delta returns bridge/learning/hivemind keys."""
        bridge = _make_bridge()
        bridge._http_disabled = True
        delta = bridge.extract_learning_delta()
        self.assertIn('bridge', delta)
        self.assertIn('learning', delta)
        self.assertIn('hivemind', delta)

    def test_apply_federation_update(self):
        """FT: apply_federation_update stores aggregated metrics."""
        bridge = _make_bridge()
        data = {'network_agents': 42, 'last_thought': 'hello'}
        result = bridge.apply_federation_update(data)
        self.assertTrue(result)
        self.assertEqual(bridge._federation_aggregated['network_agents'], 42)


class TestGetHivemindAgents(unittest.TestCase):
    """FT: get_hivemind_agents in-process and HTTP."""

    def test_in_process_returns_agent_list(self):
        """FT: In-process mode returns hive_mind.get_all_agents()."""
        bridge = _make_bridge()
        bridge._in_process = True
        bridge._hive_mind = MagicMock()
        bridge._hive_mind.get_all_agents.return_value = [{'id': 'a1'}]
        self.assertEqual(bridge.get_hivemind_agents(), [{'id': 'a1'}])

    def test_http_disabled_returns_empty_list(self):
        """ERR: HTTP-disabled mode returns empty list."""
        bridge = _make_bridge()
        bridge._http_disabled = True
        self.assertEqual(bridge.get_hivemind_agents(), [])


class TestCloudConsentCache(unittest.TestCase):
    """SEC: _has_cloud_consent caching and empty user_id."""

    def test_empty_user_id_returns_false(self):
        """SEC: Empty user_id returns False (no consent assumed)."""
        bridge = _make_bridge()
        self.assertFalse(bridge._has_cloud_consent(''))

    def test_consent_cached(self):
        """NFT: Consent result is cached for TTL period."""
        bridge = _make_bridge()
        bridge._consent_cache['u1'] = (True, time.time())
        self.assertTrue(bridge._has_cloud_consent('u1'))

    def test_expired_cache_reloads(self):
        """NFT: Expired consent cache triggers DB reload."""
        bridge = _make_bridge()
        bridge._consent_cache['u1'] = (True, time.time() - 600)  # expired
        # DB query will fail (no real DB) → defaults to False
        self.assertFalse(bridge._has_cloud_consent('u1'))


class TestHiveParticipation(unittest.TestCase):
    """SEC/U3: _has_hive_participation opt-out."""

    def test_empty_user_id_defaults_participate(self):
        """SEC: Empty user_id defaults to True (participate)."""
        bridge = _make_bridge()
        self.assertTrue(bridge._has_hive_participation(''))

    def test_cached_opt_out(self):
        """NFT: Cached opt-out is returned from cache."""
        bridge = _make_bridge()
        bridge._consent_cache['hive_u1'] = (False, time.time())
        self.assertFalse(bridge._has_hive_participation('u1'))


class TestCrawlTamperCallback(unittest.TestCase):
    """SEC: _on_crawl_tamper_detected disables in-process mode."""

    def test_tamper_disables_in_process(self):
        """SEC: Tamper detection disables in-process and nulls provider."""
        bridge = _make_bridge()
        bridge._in_process = True
        bridge._provider = MagicMock()
        bridge._hive_mind = MagicMock()

        bridge._on_crawl_tamper_detected()

        self.assertFalse(bridge._in_process)
        self.assertIsNone(bridge._provider)
        self.assertIsNone(bridge._hive_mind)


class TestThreadSafety(unittest.TestCase):
    """NFT: Concurrent access to shared state."""

    def test_concurrent_record_interaction(self):
        """NFT: 50 concurrent record_interaction calls don't corrupt stats."""
        bridge = _make_bridge(HEVOLVE_WM_FLUSH_BATCH='9999')  # prevent flush
        bridge._http_disabled = True

        errors = []

        def record(i):
            try:
                bridge.record_interaction(f'u{i}', 'p1', f'q{i}', f'a{i}')
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=record, args=(i,)) for i in range(50)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=5)

        self.assertEqual(len(errors), 0, f"Thread errors: {errors}")
        self.assertEqual(bridge._stats['total_recorded'], 50)

    def test_concurrent_get_stats(self):
        """NFT: get_stats is safe under concurrent access."""
        bridge = _make_bridge()
        results = []

        def read_stats():
            for _ in range(20):
                results.append(bridge.get_stats())

        threads = [threading.Thread(target=read_stats) for _ in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=5)

        self.assertEqual(len(results), 100)


class TestSubmitOutputFeedback(unittest.TestCase):
    """FT: submit_output_feedback routes to correct internal method."""

    def test_error_routes_to_correction(self):
        """FT: Error status routes to submit_correction."""
        bridge = _make_bridge()
        with patch.object(bridge, 'submit_correction') as mock_corr:
            bridge.submit_output_feedback(
                output_modality='image',
                status='error',
                context='generate cat',
                error_message='OOM',
            )
            mock_corr.assert_called_once()

    def test_success_without_data_routes_to_record(self):
        """FT: Success without generated_data routes to record_interaction."""
        bridge = _make_bridge()
        with patch.object(bridge, 'record_interaction') as mock_rec:
            bridge.submit_output_feedback(
                output_modality='audio_speech',
                status='completed',
                context='say hello',
                model_used='tts-1',
                generation_time_seconds=1.5,
            )
            mock_rec.assert_called_once()


class TestGetLearningFeedback(unittest.TestCase):
    """FT: get_learning_feedback in-process and HTTP paths."""

    def test_in_process_returns_provider_stats(self):
        """FT: In-process returns provider.get_stats() content."""
        bridge = _make_bridge()
        bridge._in_process = True
        bridge._provider = MagicMock()
        bridge._provider.get_stats.return_value = {
            'last_feedback': {'correction': 0.01}}
        result = bridge.get_learning_feedback()
        self.assertEqual(result['correction'], 0.01)

    def test_http_disabled_returns_none(self):
        """ERR: HTTP-disabled returns None."""
        bridge = _make_bridge()
        bridge._http_disabled = True
        self.assertIsNone(bridge.get_learning_feedback())


if __name__ == '__main__':
    unittest.main()
