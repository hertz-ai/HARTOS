"""Tests for the collapsed expert background path.

The dispatcher's expert turn runs the ORIGINAL prompt through the full
langchain pipeline (local expert) or the hive peer's OpenAI-compat
endpoint (hive expert).  No improve-this-draft wrapper, no similarity
gate.

Verifies that:

  * Hive expert (is_local=False) → OpenAI-compatible POST to base_url
    with Bearer auth.
  * Local expert (is_local=True) → HTTP POST to /chat with re-entry
    guard flags set.
  * Empty response → no delivery, draft standby remains final.
  * Guardrail block → no delivery.
  * Successful response → _deliver_expert_response invoked exactly once.
  * served_by tag distinguishes hive_langchain_bg vs local_langchain_bg.
  * Escalation reason flows from _active through to WorldModelBridge.
"""
from __future__ import annotations

import os
import sys
from unittest.mock import MagicMock, patch

import pytest

PROJECT_ROOT = os.path.dirname(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)


@pytest.fixture
def registry_with_local_expert():
    from integrations.agent_engine.model_registry import (
        ModelRegistry, ModelBackend, ModelTier,
    )
    reg = ModelRegistry()
    reg.register(ModelBackend(
        model_id='qwen3.5-4b-local',
        display_name='Qwen3.5 4B (Local)',
        tier=ModelTier.FAST,
        config_list_entry={
            'model': 'Qwen3.5-4B', 'api_key': 'dummy',
            'base_url': 'http://localhost:8080/v1', 'price': [0, 0],
        },
        avg_latency_ms=700.0, accuracy_score=0.60, is_local=True,
    ))
    return reg


@pytest.fixture
def registry_with_hive_expert():
    from integrations.agent_engine.model_registry import (
        ModelRegistry, ModelBackend, ModelTier,
    )
    reg = ModelRegistry()
    reg.register(ModelBackend(
        model_id='hive-node-alpha-qwen-27b',
        display_name='Hive: Qwen 27B (alpha)',
        tier=ModelTier.EXPERT,
        config_list_entry={
            'model': 'qwen-27b',
            'api_key': 'hive-token-xyz',
            'base_url': 'https://node-alpha.example.com/v1',
            'price': [0, 0],
        },
        avg_latency_ms=2000.0, accuracy_score=0.85,
        is_local=False,
    ))
    return reg


@pytest.fixture
def dispatcher(registry_with_local_expert):
    from integrations.agent_engine.speculative_dispatcher import (
        SpeculativeDispatcher,
    )
    d = SpeculativeDispatcher(model_registry=registry_with_local_expert)
    d._health_probe_enabled = False
    return d


def _mock_guardrails(monkeypatch):
    import security.hive_guardrails as hg
    monkeypatch.setattr(hg.HiveCircuitBreaker, 'is_halted', lambda: False)
    monkeypatch.setattr(
        hg.ConstitutionalFilter, 'check_prompt',
        staticmethod(lambda p: (True, '')),
    )


# ─────────────────────────────────────────────────────────────────────
# _expert_background_task — delegates to the collapsed path
# ─────────────────────────────────────────────────────────────────────


class TestBackgroundTaskDelegation:

    def test_routes_to_collapsed_path(
            self, dispatcher, monkeypatch):
        _mock_guardrails(monkeypatch)
        expert = dispatcher._registry.get_fast_model()
        with patch.object(dispatcher,
                          '_run_collapsed_expert_path') as collapsed:
            dispatcher._expert_background_task(
                'spec-1', 'original', 'fast', expert,
                'u', 'pid', None, 'general')
        collapsed.assert_called_once()

    def test_active_cleanup_runs(self, dispatcher, monkeypatch):
        """Shared finally clause must clean _active even when the
        helper raises."""
        _mock_guardrails(monkeypatch)
        expert = dispatcher._registry.get_fast_model()
        with dispatcher._lock:
            dispatcher._active['spec-cleanup'] = {'started_at': 0}
        with patch.object(dispatcher, '_run_collapsed_expert_path',
                          side_effect=RuntimeError('boom')):
            dispatcher._expert_background_task(
                'spec-cleanup', 'p', 'r', expert,
                'u', 'pid', None, 'general')
        assert 'spec-cleanup' not in dispatcher._active

    def test_circuit_breaker_short_circuits(
            self, dispatcher, monkeypatch):
        """Halted circuit breaker → no dispatch attempt."""
        import security.hive_guardrails as hg
        monkeypatch.setattr(
            hg.HiveCircuitBreaker, 'is_halted', lambda: True)
        expert = dispatcher._registry.get_fast_model()
        with patch.object(dispatcher,
                          '_run_collapsed_expert_path') as collapsed:
            dispatcher._expert_background_task(
                'spec-halted', 'p', 'r', expert,
                'u', 'pid', None, 'general')
        collapsed.assert_not_called()


# ─────────────────────────────────────────────────────────────────────
# _dispatch_expert_langchain — hive routing
# ─────────────────────────────────────────────────────────────────────


class TestHiveDispatch:

    def test_hive_posts_to_base_url_with_auth(self, monkeypatch):
        from integrations.agent_engine.speculative_dispatcher import (
            SpeculativeDispatcher,
        )
        from integrations.agent_engine.model_registry import (
            ModelRegistry,
        )
        d = SpeculativeDispatcher(model_registry=ModelRegistry())

        from integrations.agent_engine.model_registry import (
            ModelBackend, ModelTier,
        )
        hive_model = ModelBackend(
            model_id='hive-node-a-qwen-27b',
            display_name='Hive 27B', tier=ModelTier.EXPERT,
            config_list_entry={
                'model': 'qwen-27b', 'api_key': 'TOKEN',
                'base_url': 'https://node-a.example.com/v1',
            },
            is_local=False,
        )
        fake_resp = MagicMock()
        fake_resp.status_code = 200
        fake_resp.json.return_value = {
            'choices': [{'message': {'content': 'expert answer'}}]
        }
        with patch('requests.post', return_value=fake_resp) as post:
            out = d._dispatch_expert_langchain(
                hive_model, 'original prompt',
                'u', 'pid', 'general', None)
        assert out == 'expert answer'
        post.assert_called_once()
        call = post.call_args
        # URL: base_url + /chat/completions
        assert call.args[0] == (
            'https://node-a.example.com/v1/chat/completions')
        # Auth header set from registered api_key
        assert call.kwargs['headers']['Authorization'] == 'Bearer TOKEN'
        # OpenAI-compatible payload
        body = call.kwargs['json']
        assert body['model'] == 'qwen-27b'
        assert body['messages'] == [
            {'role': 'user', 'content': 'original prompt'}]

    def test_hive_returns_empty_on_missing_base_url(self):
        from integrations.agent_engine.speculative_dispatcher import (
            SpeculativeDispatcher,
        )
        from integrations.agent_engine.model_registry import (
            ModelRegistry, ModelBackend, ModelTier,
        )
        d = SpeculativeDispatcher(model_registry=ModelRegistry())
        bad = ModelBackend(
            model_id='hive-bad', display_name='Hive Bad',
            tier=ModelTier.EXPERT,
            config_list_entry={'model': 'qwen'},  # no base_url
            is_local=False,
        )
        assert d._dispatch_expert_langchain(
            bad, 'p', 'u', 'pid', 'g', None) == ''

    def test_hive_returns_empty_on_http_error(self):
        from integrations.agent_engine.speculative_dispatcher import (
            SpeculativeDispatcher,
        )
        from integrations.agent_engine.model_registry import (
            ModelRegistry, ModelBackend, ModelTier,
        )
        d = SpeculativeDispatcher(model_registry=ModelRegistry())
        hive = ModelBackend(
            model_id='hive-x', display_name='X',
            tier=ModelTier.EXPERT,
            config_list_entry={
                'model': 'qwen', 'api_key': 'tok',
                'base_url': 'https://x.example.com/v1',
            },
            is_local=False,
        )
        fake_resp = MagicMock(status_code=503)
        fake_resp.json.return_value = {}
        with patch('requests.post', return_value=fake_resp):
            assert d._dispatch_expert_langchain(
                hive, 'p', 'u', 'pid', 'g', None) == ''

    def test_hive_returns_empty_on_network_exception(self):
        from integrations.agent_engine.speculative_dispatcher import (
            SpeculativeDispatcher,
        )
        from integrations.agent_engine.model_registry import (
            ModelRegistry, ModelBackend, ModelTier,
        )
        d = SpeculativeDispatcher(model_registry=ModelRegistry())
        hive = ModelBackend(
            model_id='hive-y', display_name='Y',
            tier=ModelTier.EXPERT,
            config_list_entry={
                'model': 'qwen', 'api_key': 'tok',
                'base_url': 'https://y.example.com/v1',
            },
            is_local=False,
        )
        import requests
        with patch('requests.post',
                   side_effect=requests.ConnectionError('boom')):
            assert d._dispatch_expert_langchain(
                hive, 'p', 'u', 'pid', 'g', None) == ''


# ─────────────────────────────────────────────────────────────────────
# _dispatch_expert_langchain — local routing
# ─────────────────────────────────────────────────────────────────────


class TestLocalDispatch:

    def test_local_non_bundled_posts_to_chat_route(self, monkeypatch):
        """Non-bundled mode HTTP POSTs to /chat with re-entry guard
        flags set."""
        monkeypatch.delenv('NUNBA_BUNDLED', raising=False)
        # Force frozen=False so the non-bundled path is taken
        monkeypatch.setattr(sys, 'frozen', False, raising=False)

        from integrations.agent_engine.speculative_dispatcher import (
            SpeculativeDispatcher,
        )
        from integrations.agent_engine.model_registry import (
            ModelRegistry, ModelBackend, ModelTier,
        )
        d = SpeculativeDispatcher(model_registry=ModelRegistry())
        local = ModelBackend(
            model_id='qwen-4b-local', display_name='4B', tier=ModelTier.FAST,
            config_list_entry={
                'model': 'qwen-4b', 'api_key': 'k',
                'base_url': 'http://127.0.0.1:8080/v1',
            },
            is_local=True,
        )
        fake_resp = MagicMock(status_code=200)
        fake_resp.json.return_value = {'response': 'collapsed answer'}
        with patch('requests.post', return_value=fake_resp) as post:
            out = d._dispatch_expert_langchain(
                local, 'tell me a story', 'u', 'pid', 'general', None)
        assert out == 'collapsed answer'
        post.assert_called_once()

    def test_local_non_bundled_honors_channel_agent_timeout_env_var(
            self, monkeypatch):
        """Found live 2026-08-31: this re-entrant /chat call hardcoded
        timeout=60, but it runs the SAME full multi-agent turn as any
        other channel's /chat call, which routinely takes well over 60s
        locally — the future completed "cleanly" (no exception, no log)
        exactly 60.0s after starting, silently discarding a real answer
        that was still generating. Must honor the same
        HEVOLVE_CHANNEL_AGENT_TIMEOUT every other channel already does."""
        monkeypatch.delenv('NUNBA_BUNDLED', raising=False)
        monkeypatch.setattr(sys, 'frozen', False, raising=False)
        monkeypatch.setenv('HEVOLVE_CHANNEL_AGENT_TIMEOUT', '300')

        from integrations.agent_engine.speculative_dispatcher import (
            SpeculativeDispatcher,
        )
        from integrations.agent_engine.model_registry import (
            ModelRegistry, ModelBackend, ModelTier,
        )
        d = SpeculativeDispatcher(model_registry=ModelRegistry())
        local = ModelBackend(
            model_id='qwen-4b-local', display_name='4B', tier=ModelTier.FAST,
            config_list_entry={
                'model': 'qwen-4b', 'api_key': 'k',
                'base_url': 'http://127.0.0.1:8080/v1',
            },
            is_local=True,
        )
        fake_resp = MagicMock(status_code=200)
        fake_resp.json.return_value = {'response': 'collapsed answer'}
        with patch('requests.post', return_value=fake_resp) as post:
            d._dispatch_expert_langchain(
                local, 'tell me a story', 'u', 'pid', 'general', None)
        assert post.call_args.kwargs['timeout'] == 300
        body = post.call_args.kwargs['json']
        # Re-entry guard: inner /chat reads these and skips dispatcher
        assert body['speculative'] is False
        assert body['draft_first'] is False
        # Original prompt — no "improve this draft" wrapping
        assert body['prompt'] == 'tell me a story'

    def test_none_model_returns_empty(self):
        from integrations.agent_engine.speculative_dispatcher import (
            SpeculativeDispatcher,
        )
        from integrations.agent_engine.model_registry import ModelRegistry
        d = SpeculativeDispatcher(model_registry=ModelRegistry())
        assert d._dispatch_expert_langchain(
            None, 'p', 'u', 'pid', 'g', None) == ''


# ─────────────────────────────────────────────────────────────────────
# _run_collapsed_expert_path — delivery semantics
# ─────────────────────────────────────────────────────────────────────


class TestCollapsedPathDelivery:

    def test_successful_response_delivered_via_existing_channel(
            self, dispatcher, monkeypatch):
        _mock_guardrails(monkeypatch)
        expert = dispatcher._registry.get_fast_model()
        with patch.object(dispatcher, '_dispatch_expert_langchain',
                          return_value='real expert answer'), \
             patch.object(dispatcher,
                          '_deliver_expert_response') as deliver, \
             patch.object(dispatcher, '_record_interaction_safely'):
            dispatcher._run_collapsed_expert_path(
                'spec-success', 'original prompt', 'draft standby',
                expert, 'u', 'pid', None, 'general')
        deliver.assert_called_once_with(
            'u', 'pid', 'spec-success', 'real expert answer')

    def test_empty_response_does_not_deliver(
            self, dispatcher, monkeypatch):
        _mock_guardrails(monkeypatch)
        expert = dispatcher._registry.get_fast_model()
        with patch.object(dispatcher, '_dispatch_expert_langchain',
                          return_value=''), \
             patch.object(dispatcher,
                          '_deliver_expert_response') as deliver:
            dispatcher._run_collapsed_expert_path(
                'spec-empty', 'original', 'standby',
                expert, 'u', 'pid', None, 'general')
        deliver.assert_not_called()
        # _results entry records the run so admin /diag can tell expert
        # fired but returned empty
        assert dispatcher._results['spec-empty']['improved'] is False
        assert (
            dispatcher._results['spec-empty']['response']
            == 'standby'
        )

    def test_guardrail_block_does_not_deliver(
            self, dispatcher, monkeypatch):
        """ConstitutionalFilter rejects → no delivery."""
        import security.hive_guardrails as hg
        monkeypatch.setattr(
            hg.HiveCircuitBreaker, 'is_halted', lambda: False)
        monkeypatch.setattr(
            hg.ConstitutionalFilter, 'check_prompt',
            staticmethod(lambda p: (False, 'policy_violation')),
        )
        expert = dispatcher._registry.get_fast_model()
        with patch.object(dispatcher, '_dispatch_expert_langchain',
                          return_value='something the filter rejects'), \
             patch.object(dispatcher,
                          '_deliver_expert_response') as deliver:
            dispatcher._run_collapsed_expert_path(
                'spec-blocked', 'original', 'standby',
                expert, 'u', 'pid', None, 'general')
        deliver.assert_not_called()

    def test_served_by_local_tag(
            self, dispatcher, monkeypatch):
        _mock_guardrails(monkeypatch)
        expert = dispatcher._registry.get_fast_model()  # is_local=True
        with patch.object(dispatcher, '_dispatch_expert_langchain',
                          return_value='answer'), \
             patch.object(dispatcher, '_deliver_expert_response'), \
             patch.object(dispatcher, '_record_interaction_safely'):
            dispatcher._run_collapsed_expert_path(
                'spec-local', 'p', 'r', expert,
                'u', 'pid', None, 'general')
        assert (
            dispatcher._results['spec-local']['served_by']
            == 'local_langchain_bg'
        )

    def test_served_by_hive_tag(
            self, registry_with_hive_expert, monkeypatch):
        _mock_guardrails(monkeypatch)
        from integrations.agent_engine.speculative_dispatcher import (
            SpeculativeDispatcher,
        )
        d = SpeculativeDispatcher(
            model_registry=registry_with_hive_expert)
        d._health_probe_enabled = False
        hive_expert = registry_with_hive_expert.get_expert_model()
        with patch.object(d, '_dispatch_expert_langchain',
                          return_value='hive answer'), \
             patch.object(d, '_deliver_expert_response'), \
             patch.object(d, '_record_interaction_safely'):
            d._run_collapsed_expert_path(
                'spec-hive', 'p', 'r', hive_expert,
                'u', 'pid', None, 'general')
        assert (
            d._results['spec-hive']['served_by']
            == 'hive_langchain_bg'
        )

    def test_record_interaction_carries_escalation_reason(
            self, dispatcher, monkeypatch):
        """The reason stamped on _active flows through to
        WorldModelBridge for distillation weighting."""
        _mock_guardrails(monkeypatch)
        expert = dispatcher._registry.get_fast_model()
        with dispatcher._lock:
            dispatcher._active['spec-r'] = {
                'escalation_reason': 'refusal_override',
                'started_at': 0,
            }
        with patch.object(dispatcher, '_dispatch_expert_langchain',
                          return_value='ok'), \
             patch.object(dispatcher, '_deliver_expert_response'), \
             patch.object(dispatcher,
                          '_record_interaction_safely') as rec:
            dispatcher._run_collapsed_expert_path(
                'spec-r', 'p', 'r', expert,
                'u', 'pid', None, 'general')
        assert rec.call_args.kwargs['escalation_reason'] == 'refusal_override'


# ─────────────────────────────────────────────────────────────────────
# _deliver_expert_response — a failed expert turn must not leak its raw
# internal error string to the user (2026-08-31: found live on Signal —
# a malformed tool-call from the local model 500'd, and the literal
# "Error getting response: Error code: 500 - {...}" was delivered to the
# channel as if it were the real answer, landing right after the standby
# and reading as a second, broken reply).
# ─────────────────────────────────────────────────────────────────────


def _run_deliver(dispatcher, monkeypatch, response, speculation_id='spec-x'):
    """Drive _deliver_expert_response's channel leg to completion and
    return whatever text actually reached ChannelRegistry.send_to_channel
    (or None if the leg didn't fire)."""
    import asyncio
    import core.safe_hartos_attr as sha
    import core.peer_link.message_bus as mb
    import integrations.channels.registry as reg
    import integrations.channels.flask_integration as fi

    monkeypatch.setattr(sha, 'safe_hartos_attr', lambda name: None)
    monkeypatch.setattr(mb, 'chat_topic_for', lambda uid: f'chat.{uid}')

    captured = {}

    class FakeRegistry:
        async def send_to_channel(self, channel, chat_id, text):
            captured['channel_text'] = text

            class R:
                success = True
            return R()

    monkeypatch.setattr(reg, 'get_registry', lambda: FakeRegistry())

    class FakeLoop:
        def is_running(self):
            return True

    monkeypatch.setattr(
        fi, 'get_channel_integration',
        lambda: type('X', (), {'_loop': FakeLoop()})(),
    )
    monkeypatch.setattr(
        asyncio, 'run_coroutine_threadsafe',
        lambda coro, loop: asyncio.ensure_future(coro),
    )

    with dispatcher._lock:
        dispatcher._active[speculation_id] = {
            'channel_context': {'channel': 'signal', 'chat_id': '+1'},
        }

    async def _run():
        dispatcher._deliver_expert_response(
            'u', 'pid', speculation_id, response)
        await asyncio.sleep(0.05)

    loop = asyncio.new_event_loop()
    try:
        loop.run_until_complete(_run())
    finally:
        loop.close()
    return captured.get('channel_text')


class TestErrorLeakGuard:

    def test_error_shaped_response_not_delivered_verbatim(
            self, dispatcher, monkeypatch):
        raw_error = (
            "Error getting response: Error code: 500 - {'error': "
            "{'code': 500, 'message': 'Failed to parse tool call "
            "arguments as JSON'}}"
        )
        delivered = _run_deliver(dispatcher, monkeypatch, raw_error)
        assert delivered != raw_error
        assert delivered == (
            "I ran into a problem working on that — could you try again?"
        )

    def test_successful_response_still_delivered_unchanged(
            self, dispatcher, monkeypatch):
        delivered = _run_deliver(
            dispatcher, monkeypatch,
            "Sure! Here's the answer you asked for: 42.")
        assert delivered == "Sure! Here's the answer you asked for: 42."
