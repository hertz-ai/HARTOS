"""Draft-first only runs when a draft server of its own is listening.

With no HEVOLVE_DRAFT_LLM_URL, ``get_local_draft_url()`` answers with the MAIN
model's URL.  The dispatcher's liveness gate was a TCP connect to that URL, so
on a box with no draft (no CUDA, or under the 10 GB draft gate) it connected to
the main llama-server, said "alive", and posted the draft-classifier prompt to
the 4B with a 15 s read timeout.  A 4B on CPU cannot answer in 15 s, and
``pooled_post`` re-sends once after a raise inside the slot: 30 s, an empty
reply, confidence 0.0, then the normal 4B path ran anyway (measured live
2026-10-28: 7 of 7 draft-telemetry lines at 30.0 to 32.0 s, 47 to 58 s per chat
turn).  The gate must say ``draft_server_offline`` when no draft server exists.

An api-kind node (a configured endpoint serves every role, the draft slot IS the
main model by owner design) keeps its draft-first path.
"""
import os
import sys
from contextlib import contextmanager
from unittest.mock import MagicMock

import pytest

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

MAIN_URL = 'http://127.0.0.1:8080/v1'
DRAFT_URL = 'http://127.0.0.1:8081/v1'


@pytest.fixture
def registry():
    from integrations.agent_engine.model_registry import (
        ModelBackend,
        ModelRegistry,
        ModelTier,
    )
    reg = ModelRegistry()
    reg.register(ModelBackend(
        model_id='qwen3.5-0.8b-draft', display_name='Qwen3.5 0.8B (Draft)',
        tier=ModelTier.DRAFT,
        config_list_entry={'model': 'Qwen3.5-0.8B-Instruct', 'api_key': 'dummy',
                           'base_url': MAIN_URL, 'price': [0, 0]},
        avg_latency_ms=300.0, accuracy_score=0.45, cost_per_1k_tokens=0.0,
        is_local=True))
    return reg


@pytest.fixture
def dispatcher(registry, monkeypatch):
    from integrations.agent_engine.speculative_dispatcher import SpeculativeDispatcher
    # The probe result is cached on the class for 30 s; start every test cold.
    monkeypatch.setattr(SpeculativeDispatcher, '_draft_probe_ts', 0.0)
    monkeypatch.setattr(SpeculativeDispatcher, '_draft_probe_ok', False)
    return SpeculativeDispatcher(model_registry=registry)


def _wire(monkeypatch, *, kind, draft_url, main_url, listening):
    """Pin the resolvers and the TCP connect; return the list of connects."""
    import core.autogen_config as ac
    import core.port_registry as pr
    monkeypatch.setattr(pr, 'get_local_draft_url', lambda: draft_url)
    monkeypatch.setattr(pr, 'get_local_llm_url', lambda: main_url)
    monkeypatch.setattr(ac, 'resolve_llm_backend', lambda: (kind, {'model': 'm'}))
    connects = []

    @contextmanager
    def _connect(addr, timeout=None):
        connects.append(addr)
        if not listening:
            raise ConnectionRefusedError(addr)
        yield MagicMock()

    import socket
    monkeypatch.setattr(socket, 'create_connection', _connect)
    return connects


class TestHasDedicatedDraftServer:

    def test_no_draft_configured_means_the_main_model_is_the_draft(self, monkeypatch):
        import core.port_registry as pr
        monkeypatch.setattr(pr, 'get_local_llm_url', lambda: MAIN_URL)
        monkeypatch.setattr(pr, 'get_local_draft_url', lambda: MAIN_URL)
        assert pr.has_dedicated_draft_server() is False

    def test_a_trailing_slash_is_still_the_same_endpoint(self, monkeypatch):
        import core.port_registry as pr
        monkeypatch.setattr(pr, 'get_local_llm_url', lambda: MAIN_URL)
        monkeypatch.setattr(pr, 'get_local_draft_url', lambda: MAIN_URL + '/')
        assert pr.has_dedicated_draft_server() is False

    def test_a_separate_listening_draft_is_dedicated(self, monkeypatch):
        import core.port_registry as pr
        monkeypatch.setattr(pr, 'get_local_llm_url', lambda: MAIN_URL)
        monkeypatch.setattr(pr, 'get_local_draft_url', lambda: DRAFT_URL)
        assert pr.has_dedicated_draft_server() is True


class TestDraftServerAlive:

    def test_a_box_with_no_draft_server_is_not_alive(self, dispatcher, monkeypatch):
        connects = _wire(monkeypatch, kind='local', draft_url=MAIN_URL,
                         main_url=MAIN_URL, listening=True)
        assert dispatcher._draft_server_alive() is False
        assert connects == [], 'the main llama-server must not be probed as a draft'

    def test_a_listening_dedicated_draft_is_alive(self, dispatcher, monkeypatch):
        connects = _wire(monkeypatch, kind='local', draft_url=DRAFT_URL,
                         main_url=MAIN_URL, listening=True)
        assert dispatcher._draft_server_alive() is True
        assert connects == [('127.0.0.1', 8081)]

    def test_a_dedicated_draft_that_stopped_listening_is_not_alive(
            self, dispatcher, monkeypatch):
        _wire(monkeypatch, kind='local', draft_url=DRAFT_URL,
              main_url=MAIN_URL, listening=False)
        assert dispatcher._draft_server_alive() is False

    def test_an_api_node_keeps_the_main_model_as_its_draft(
            self, dispatcher, monkeypatch):
        """Owner design: on the api kind the one configured endpoint serves the
        draft slot too, so draft == main there is not 'no draft server'."""
        _wire(monkeypatch, kind='api', draft_url=MAIN_URL,
              main_url=MAIN_URL, listening=True)
        assert dispatcher._draft_server_alive() is True


class TestDispatchDraftFirstOnADraftlessBox:

    def test_the_classifier_prompt_is_never_posted_to_the_main_model(
            self, dispatcher, monkeypatch):
        _wire(monkeypatch, kind='local', draft_url=MAIN_URL,
              main_url=MAIN_URL, listening=True)
        dispatcher._dispatch_to_model = MagicMock(return_value='')
        out = dispatcher.dispatch_draft_first(
            prompt='hi', user_id='u1', prompt_id='p1')
        assert out['error'] == 'draft_server_offline'
        assert out['response'] == ''
        dispatcher._dispatch_to_model.assert_not_called()
