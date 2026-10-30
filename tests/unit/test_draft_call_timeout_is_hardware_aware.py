"""The draft-first classifier call waits as long as the box's hardware needs.

With no HEVOLVE_DRAFT_LLM_URL, ``get_local_draft_url()`` answers with the MAIN
model's URL and draft-first posts its classifier prompt to the main llama-server
(by design: the draft keeps pointing at the main model).  That call had a fixed
15 s read timeout.  A 4B on a box with no CUDA cannot answer in 15 s, and
``pooled_post`` re-sends once after a raise inside the slot: 30 s, an empty
reply, confidence 0.0, then the normal 4B path ran anyway (measured on a
no-CUDA box: 18 of 18 draft-telemetry lines at 30.0 to 65.8 s with reply_len 0,
main-model completions there at a median of 55 to 71 s).

The timeout now comes from the GPU tier, the same classification ``/backend/health``
reports, and the draft is NOT taken off the main model.
"""
import ast
import os
import sys
from unittest.mock import MagicMock

import pytest

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

MAIN_URL = 'http://127.0.0.1:8080/v1'


def _vram_manager_module():
    """The module, not the singleton: integrations.service_tools re-exports the
    VRAMManager instance under the same name, which shadows the submodule in a
    ``from integrations.service_tools import vram_manager``."""
    import importlib
    return importlib.import_module('integrations.service_tools.vram_manager')


class TestDraftCallTimeoutTable:

    def test_every_tier_has_a_timeout(self):
        from core.gpu_tier import DRAFT_CALL_TIMEOUT_S, GpuTier
        assert set(DRAFT_CALL_TIMEOUT_S) == set(GpuTier)

    def test_a_box_with_no_gpu_gets_the_canonical_llm_completion_budget(self):
        """Not a second number: the read budget core.http_pool already names
        for a local completion."""
        from core.gpu_tier import GpuTier, draft_call_timeout
        from core.http_pool import LLM_COMPLETION_TIMEOUT
        assert draft_call_timeout(GpuTier.NONE) == LLM_COMPLETION_TIMEOUT[1]

    def test_tiers_with_a_gpu_keep_todays_fifteen_seconds(self):
        from core.gpu_tier import (
            DEFAULT_DRAFT_CALL_TIMEOUT_S, GpuTier, draft_call_timeout)
        assert DEFAULT_DRAFT_CALL_TIMEOUT_S == 15
        for tier in (GpuTier.ULTRA, GpuTier.FULL, GpuTier.STANDARD):
            assert draft_call_timeout(tier) == 15


class TestCurrentGpuTier:

    @pytest.mark.parametrize('gpu, expected', [
        ({'total_gb': 0.0, 'cuda_available': False}, 'none'),
        ({'total_gb': 8.0, 'cuda_available': True}, 'standard'),
        ({'total_gb': 12.0, 'cuda_available': True}, 'full'),
        ({'total_gb': 24.0, 'cuda_available': True}, 'ultra'),
        ({}, 'none'),
    ])
    def test_tier_comes_from_the_vram_manager_reading(self, monkeypatch, gpu, expected):
        vm = _vram_manager_module()
        monkeypatch.setattr(vm.vram_manager, 'detect_gpu', lambda: gpu)
        assert vm.current_gpu_tier().value == expected


@pytest.fixture
def dispatcher(monkeypatch):
    from integrations.agent_engine.model_registry import (
        ModelBackend, ModelRegistry, ModelTier)
    from integrations.agent_engine.speculative_dispatcher import SpeculativeDispatcher
    reg = ModelRegistry()
    reg.register(ModelBackend(
        model_id='qwen3.5-0.8b-draft', display_name='Qwen3.5 0.8B (Draft)',
        tier=ModelTier.DRAFT,
        config_list_entry={'model': 'Qwen3.5-0.8B-Instruct', 'api_key': 'dummy',
                           'base_url': MAIN_URL, 'price': [0, 0]},
        avg_latency_ms=300.0, accuracy_score=0.45, cost_per_1k_tokens=0.0,
        is_local=True))
    monkeypatch.setattr(SpeculativeDispatcher, '_draft_probe_ts', 0.0)
    monkeypatch.setattr(SpeculativeDispatcher, '_draft_probe_ok', False)
    return SpeculativeDispatcher(model_registry=reg)


def _ok_response():
    resp = MagicMock()
    resp.status_code = 200
    resp.json.return_value = {'choices': [{'message': {'content': '{"reply":"hi"}'}}]}
    return resp


def _bundled_draft_call(dispatcher, monkeypatch, *, kind, tier=None, tier_error=None):
    """Run _dispatch_to_model in bundled mode; return pooled_post's kwargs."""
    import core.autogen_config as ac
    import core.http_pool as hp
    import core.port_registry as pr
    vm = _vram_manager_module()
    monkeypatch.setenv('NUNBA_BUNDLED', '1')
    monkeypatch.setattr(ac, 'resolve_llm_backend', lambda: (kind, {'model': 'm'}))
    monkeypatch.setattr(ac, 'llm_http_target',
                        lambda draft=False: (MAIN_URL + '/chat/completions', {}, 'm'))
    monkeypatch.setattr(pr, 'get_local_draft_url', lambda: MAIN_URL)
    monkeypatch.setattr(pr, 'get_local_llm_url', lambda: MAIN_URL)

    def _tier():
        if tier_error is not None:
            raise tier_error
        return tier
    monkeypatch.setattr(vm, 'current_gpu_tier', _tier)
    post = MagicMock(return_value=_ok_response())
    monkeypatch.setattr(hp, 'pooled_post', post)
    model = dispatcher._registry.get_draft_model()
    out = dispatcher._dispatch_to_model(model, 'hi', 'u1', None, 'general')
    assert out, 'the draft reply must come back'
    assert post.call_count == 1
    return post.call_args.kwargs


class TestDraftCallUsesTheTier:

    def test_no_gpu_waits_for_a_completion(self, dispatcher, monkeypatch):
        from core.gpu_tier import GpuTier
        kw = _bundled_draft_call(dispatcher, monkeypatch, kind='local', tier=GpuTier.NONE)
        assert kw['timeout'] == 120

    def test_a_full_gpu_keeps_fifteen_seconds(self, dispatcher, monkeypatch):
        from core.gpu_tier import GpuTier
        kw = _bundled_draft_call(dispatcher, monkeypatch, kind='local', tier=GpuTier.FULL)
        assert kw['timeout'] == 15

    def test_an_unreadable_gpu_keeps_fifteen_seconds(self, dispatcher, monkeypatch):
        kw = _bundled_draft_call(dispatcher, monkeypatch, kind='local',
                                 tier_error=RuntimeError('nvidia-smi hung'))
        assert kw['timeout'] == 15

    def test_an_api_node_keeps_fifteen_seconds_whatever_the_local_gpu(
            self, dispatcher, monkeypatch):
        """The draft call goes to the configured API there, not to a local model
        the box has to run: the local GPU says nothing about its latency."""
        from core.gpu_tier import GpuTier
        kw = _bundled_draft_call(dispatcher, monkeypatch, kind='api', tier=GpuTier.NONE)
        assert kw['timeout'] == 15

    def test_the_timeout_is_not_a_literal_in_the_draft_call(self):
        """AST guard: the pooled_post call in _dispatch_to_model must not pass
        a bare number, or the hardware awareness is silently lost again."""
        path = os.path.join(PROJECT_ROOT, 'integrations', 'agent_engine',
                            'speculative_dispatcher.py')
        with open(path, encoding='utf-8') as fh:
            tree = ast.parse(fh.read())
        fn = next(n for n in ast.walk(tree)
                  if isinstance(n, ast.FunctionDef) and n.name == '_dispatch_to_model')
        literals = [
            kw for call in ast.walk(fn) if isinstance(call, ast.Call)
            and getattr(call.func, 'id', '') == '_pooled_post'
            for kw in call.keywords
            if kw.arg == 'timeout' and isinstance(kw.value, ast.Constant)
        ]
        assert literals == []


class TestTheDraftStaysOnTheMainModel:
    """Sathish, 2026-10-29: the timeout is hardware aware, and taking the draft
    off the main LLM would make things worse.  A box with no draft server of its
    own still runs draft-first against the main llama-server."""

    def test_the_main_llama_server_counts_as_the_draft_when_it_listens(
            self, dispatcher, monkeypatch):
        import core.port_registry as pr
        import socket
        monkeypatch.setattr(pr, 'get_local_draft_url', lambda: MAIN_URL)
        monkeypatch.setattr(pr, 'get_local_llm_url', lambda: MAIN_URL)
        connects = []

        class _Conn:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        def _connect(addr, timeout=None):
            connects.append(addr)
            return _Conn()
        monkeypatch.setattr(socket, 'create_connection', _connect)
        assert dispatcher._draft_server_alive() is True
        assert connects == [('127.0.0.1', 8080)]

    def test_draft_first_dispatches_to_the_model_on_a_draftless_box(
            self, dispatcher, monkeypatch):
        monkeypatch.setattr(type(dispatcher), '_health_probe_enabled', False)
        dispatcher._dispatch_to_model = MagicMock(return_value='')
        out = dispatcher.dispatch_draft_first(prompt='hi', user_id='u1', prompt_id='p1')
        assert out.get('error') != 'draft_server_offline'
        dispatcher._dispatch_to_model.assert_called_once()
