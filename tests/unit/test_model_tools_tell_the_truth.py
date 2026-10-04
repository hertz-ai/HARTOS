"""The model tools report what the LLM server is actually doing.

Agents and the CLI ask onboard_model / model_status, and the first-boot
wizard asks onboard(), what the local LLM is doing.  When the server was
started by someone other than this process (Nunba on the desktop,
hart-llm.service on HART OS) the answers were wrong:

  * onboard() under Nunba said 'ready' for WHATEVER model was asked for, as
    long as port 8080 answered /health -- a model that does not exist
    included (tool sweep #682).
  * status() said server_healthy=False while llama served chat, because it
    only looked at the model this process had launched.
  * the wizard treated onboard()'s own success ('ready') as a failure.

The truth comes from the canonical live probe, core.health_probe.probe_llm,
faked here so the tests do not depend on a llama-server on the test box.
"""
import pytest

from integrations.service_tools import model_onboarding as mo


def _llm(monkeypatch, status, models=None):
    out = {'status': status, 'url': 'http://127.0.0.1:8080/v1'}
    if models is not None:
        out['models'] = models
    monkeypatch.setattr('core.health_probe.probe_llm',
                        lambda include_models=False: dict(out))


@pytest.fixture
def under_nunba(monkeypatch):
    monkeypatch.setattr(mo, '_is_nunba_bundled', lambda: True)
    monkeypatch.setattr(mo, '_active_model', None)


# ── onboard() when Nunba owns the server ───────────────────────────────────

def test_the_model_being_served_is_ready(under_nunba, monkeypatch):
    _llm(monkeypatch, 'up', ['Qwen3.5-4B-Q4_K_M.gguf'])
    out = mo.onboard('Qwen/Qwen3.5-4B')
    assert out['status'] == 'ready'
    assert out['model'] == 'Qwen3.5-4B-Q4_K_M.gguf'


def test_a_model_that_is_not_being_served_is_not_ready(under_nunba, monkeypatch):
    """RED before: 'ready', with the requested name echoed back."""
    _llm(monkeypatch, 'up', ['Qwen3.5-4B-Q4_K_M.gguf'])
    out = mo.onboard('meta-llama/Llama-3-8B')
    assert out['status'] == 'not_loaded'
    assert out['loaded_models'] == ['Qwen3.5-4B-Q4_K_M.gguf']
    assert 'Qwen3.5-4B' in out['error']


def test_a_server_that_will_not_say_what_it_serves_is_not_called_ready(
        under_nunba, monkeypatch):
    _llm(monkeypatch, 'up', [])
    assert mo.onboard('Qwen/Qwen3.5-4B')['status'] == 'unverified'


def test_no_server_is_still_waiting(under_nunba, monkeypatch):
    _llm(monkeypatch, 'down')
    assert mo.onboard('Qwen/Qwen3.5-4B')['status'] == 'waiting'


def test_a_similar_name_is_not_the_same_model(under_nunba, monkeypatch):
    _llm(monkeypatch, 'up', ['Qwen3-80B-Q4_K_M.gguf'])
    assert mo.onboard('Qwen/Qwen3-8B')['status'] == 'not_loaded'


# ── status() reports a server this process did not launch ──────────────────

def test_status_sees_a_server_it_did_not_launch(monkeypatch):
    """RED before: server_healthy=False while llama answered."""
    monkeypatch.setattr(mo, '_active_model', None)
    monkeypatch.setattr(mo, 'list_downloaded', lambda: [])
    monkeypatch.setattr(mo, '_get_vram_manager', lambda: None)
    _llm(monkeypatch, 'up', ['Qwen3.5-4B-Q4_K_M.gguf'])
    s = mo.status()
    assert s['server_healthy'] is True
    assert s['serving_models'] == ['Qwen3.5-4B-Q4_K_M.gguf']


def test_status_down_server_is_unhealthy(monkeypatch):
    monkeypatch.setattr(mo, '_active_model', None)
    monkeypatch.setattr(mo, 'list_downloaded', lambda: [])
    monkeypatch.setattr(mo, '_get_vram_manager', lambda: None)
    _llm(monkeypatch, 'down')
    assert mo.status()['server_healthy'] is False


# ── one definition of "onboard succeeded" ──────────────────────────────────

@pytest.mark.parametrize('result, ok', [
    ({'status': 'ready', 'endpoint': 'x'}, True),     # RED before for the wizard
    ({'status': 'not_loaded'}, False),
    ({'status': 'waiting'}, False),
    ({'status': 'error', 'error': 'x'}, False),
    (None, False),
    ({}, False),
])
def test_onboard_succeeded_is_what_onboard_calls_success(result, ok):
    assert mo.onboard_succeeded(result) is ok
