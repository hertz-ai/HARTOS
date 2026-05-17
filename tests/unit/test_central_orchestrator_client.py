"""Unit tests for central_orchestrator_client (Package D).

Covers: no-op behavior when unconfigured, heartbeat payload shape,
halt signal verification, graceful start/stop.
"""
from __future__ import annotations

import os
import sys
from unittest import mock

import pytest

_ROOT = os.path.abspath(
    os.path.join(os.path.dirname(__file__), '..', '..'),
)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from core import central_orchestrator_client as coc


@pytest.fixture(autouse=True)
def _reset_singleton(monkeypatch):
    """Each test gets a fresh client so state doesn't leak."""
    monkeypatch.setattr(coc, '_client', None)
    # Clear every env var this module consults — tests opt-in.
    for var in (
        coc.ENV_CENTRAL_URL, coc.ENV_HEARTBEAT_PATH, coc.ENV_HALT_PATH,
        coc.ENV_TENSORBOARD_URL, coc.ENV_HEARTBEAT_INTERVAL,
        coc.ENV_HALT_POLL_INTERVAL, coc.ENV_NODE_ID, coc.ENV_NODE_TIER,
    ):
        monkeypatch.delenv(var, raising=False)
    yield


def test_is_configured_false_when_env_unset():
    client = coc.CentralOrchestratorClient()
    assert client.is_configured() is False


def test_is_configured_true_when_env_set(monkeypatch):
    monkeypatch.setenv(coc.ENV_CENTRAL_URL, 'https://central.example.com')
    client = coc.CentralOrchestratorClient()
    assert client.is_configured() is True


def test_start_noop_when_unconfigured():
    client = coc.CentralOrchestratorClient()
    assert client.start() is False
    assert client._thread is None


def test_start_noop_when_tier_is_central(monkeypatch):
    monkeypatch.setenv(coc.ENV_CENTRAL_URL, 'https://central.example.com')
    monkeypatch.setenv(coc.ENV_NODE_TIER, 'central')
    client = coc.CentralOrchestratorClient()
    assert client.start() is False


def test_url_builder_joins_base_and_path(monkeypatch):
    monkeypatch.setenv(coc.ENV_CENTRAL_URL, 'https://central.example.com/')
    monkeypatch.setenv(coc.ENV_HEARTBEAT_PATH, '/custom/hb')
    client = coc.CentralOrchestratorClient()
    assert client._url(coc.ENV_HEARTBEAT_PATH, '/heartbeat') == (
        'https://central.example.com/custom/hb'
    )


def test_url_builder_uses_default_path_when_env_absent(monkeypatch):
    monkeypatch.setenv(coc.ENV_CENTRAL_URL, 'https://central.example.com')
    client = coc.CentralOrchestratorClient()
    assert client._url(coc.ENV_HEARTBEAT_PATH, '/heartbeat') == (
        'https://central.example.com/heartbeat'
    )


def test_heartbeat_payload_shape(monkeypatch):
    monkeypatch.setenv(coc.ENV_NODE_ID, 'node-42')
    monkeypatch.setenv(coc.ENV_NODE_TIER, 'flat')
    client = coc.CentralOrchestratorClient()
    payload = client._build_heartbeat_payload()
    assert payload['node_id'] == 'node-42'
    assert payload['node_tier'] == 'flat'
    assert 'timestamp' in payload
    assert payload['version'] == 1
    # Keys that are always present even when sub-services fail
    assert 'benchmark_best' in payload
    assert 'world_model' in payload


def test_post_heartbeat_success_sets_timestamp(monkeypatch):
    monkeypatch.setenv(coc.ENV_CENTRAL_URL, 'https://central.example.com')
    client = coc.CentralOrchestratorClient()
    fake_resp = mock.MagicMock(status_code=200)
    with mock.patch(
        'core.http_pool.pooled_post', return_value=fake_resp,
    ):
        assert client._post_heartbeat() is True
    assert client._last_heartbeat_ts > 0
    assert client._last_heartbeat_error is None


def test_post_heartbeat_failure_records_error(monkeypatch):
    monkeypatch.setenv(coc.ENV_CENTRAL_URL, 'https://central.example.com')
    client = coc.CentralOrchestratorClient()
    fake_resp = mock.MagicMock(status_code=502)
    with mock.patch(
        'core.http_pool.pooled_post', return_value=fake_resp,
    ):
        assert client._post_heartbeat() is False
    assert 'HTTP 502' in (client._last_heartbeat_error or '')


def test_halt_without_signature_is_ignored(monkeypatch):
    """Regression gate — a halt signal missing the master-key signature
    must NEVER be applied.  Brief §4 + security/master_key.py."""
    monkeypatch.setenv(coc.ENV_CENTRAL_URL, 'https://central.example.com')
    client = coc.CentralOrchestratorClient()
    fake_resp = mock.MagicMock(
        status_code=200,
        json=lambda: {'halt': True, 'reason': 'forged', 'signature': ''},
    )
    with mock.patch(
        'core.http_pool.pooled_post', return_value=fake_resp,
    ), mock.patch(
        'core.http_pool.pooled_get', return_value=fake_resp,
    ), mock.patch(
        'security.hive_guardrails.HiveCircuitBreaker.halt_network',
    ) as halt_mock:
        client._check_halt()
    halt_mock.assert_not_called()
    assert client._halt_applied is False


def test_halt_with_signature_calls_circuit_breaker(monkeypatch):
    monkeypatch.setenv(coc.ENV_CENTRAL_URL, 'https://central.example.com')
    client = coc.CentralOrchestratorClient()
    fake_resp = mock.MagicMock(
        status_code=200,
        json=lambda: {'halt': True, 'reason': 'ops drill',
                      'signature': 'fake-sig'},
    )
    with mock.patch(
        'core.http_pool.pooled_get', return_value=fake_resp,
    ), mock.patch(
        'security.hive_guardrails.HiveCircuitBreaker.halt_network',
        return_value=True,
    ) as halt_mock:
        client._check_halt()
    halt_mock.assert_called_once()
    call_kwargs = halt_mock.call_args.kwargs
    assert 'central:ops drill' in call_kwargs['reason']
    assert call_kwargs['signature'] == 'fake-sig'
    assert client._halt_applied is True


def test_get_status_returns_snapshot():
    client = coc.CentralOrchestratorClient()
    status = client.get_status()
    assert 'configured' in status
    assert 'running' in status
    assert 'last_heartbeat_ts' in status
    assert 'halt_applied' in status
