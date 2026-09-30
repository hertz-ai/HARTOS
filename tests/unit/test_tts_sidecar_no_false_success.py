"""The TTS sidecar must not report success for audio it never produced.

Measured 2026-09-21 on the live box: RuntimeToolManager.setup_tool(
'tts_audio_suite') cloned the repo, started the sidecar on a dynamic port
and POST /synthesize answered

    HTTP 200 {"success": true, "audio_url": "/audio/f6937c77", ...}

while GET /audio/f6937c77 answered HTTP 404 and the server's OUTPUT_DIR held
zero files.  media_agent._generate_audio_speech turns that 200 into
{'status': 'completed', 'results': [{'type': 'audio', 'url': <dead>}]}, so an
agent asking for speech is told it got speech.

The upstream project (diodiogod/TTS-Audio-Suite) is a ComfyUI custom-node
pack -- pyproject.toml carries [tool.comfy], nodes.py defines 60
NODE_CLASS_MAPPINGS, requirements.txt says "This custom node uses install.py"
-- so it exposes no HTTP synthesis API for this sidecar to call, and its
engines (chatterbox, chatterbox_official_23lang, cosyvoice, f5_tts,
omnivoice) are already first-class entries in tts_router.ENGINE_REGISTRY.
The honest answer from this endpoint is "not implemented; use the canonical
router", never a success flag.
"""

import json
import os
from unittest.mock import MagicMock, patch

import pytest


# ── the sidecar endpoint itself ──────────────────────────────────

@pytest.fixture()
def sidecar_client():
    from integrations.service_tools.servers import tts_audio_suite_server as s
    s.app.config['TESTING'] = True
    with s.app.test_client() as c:
        yield c, s


def test_synthesize_does_not_claim_success_without_audio(sidecar_client):
    client, server = sidecar_client
    resp = client.post('/synthesize', json={'text': 'hello world'})

    assert resp.status_code != 200, (
        "a synthesis that writes no file must not answer 200"
    )
    body = resp.get_json()
    assert body.get('success') is not True, (
        f"endpoint still claims success: {body}"
    )
    assert 'audio_url' not in body, (
        f"endpoint still hands out a URL for a file it never wrote: {body}"
    )
    assert 'error' in body


def test_synthesize_points_the_caller_at_the_canonical_router(sidecar_client):
    client, _ = sidecar_client
    body = client.post('/synthesize', json={'text': 'hello'}).get_json()
    blob = json.dumps(body).lower()
    assert 'tts_router' in blob or 'canonical' in blob, (
        f"error should name the canonical TTS path, got: {body}"
    )


def test_missing_text_is_still_a_400(sidecar_client):
    """The pre-existing input contract must not regress."""
    client, _ = sidecar_client
    resp = client.post('/synthesize', json={})
    assert resp.status_code == 400


def test_health_still_answers(sidecar_client):
    """RuntimeToolManager registration probes /health -- keep it alive."""
    client, _ = sidecar_client
    resp = client.get('/health')
    assert resp.status_code == 200
    assert resp.get_json().get('status') == 'ok'


# ── the caller that turned the lie into 'completed' ──────────────

def test_media_agent_speech_reports_error_not_completed():
    from integrations.service_tools import media_agent

    fake = MagicMock()
    fake.status_code = 501
    fake.json.return_value = {'error': 'not implemented'}

    with patch.object(media_agent, '_ensure_tool_running', return_value=True), \
         patch.object(media_agent, '_get_tool_base_url',
                      return_value='http://127.0.0.1:1'), \
         patch('core.http_pool.pooled_post', return_value=fake):
        out = media_agent._generate_audio_speech('ctx', 'say this', 5)

    assert out['status'] == 'error', (
        f"a non-200 from the sidecar must not read as completed: {out}"
    )
    assert not out.get('results')


# ── media-service health used a RuntimeToolManager API that does not exist ──

def test_check_media_service_uses_the_real_runtime_manager_api():
    """_check_media_service called RuntimeToolManager.get_instance() and
    .is_tool_running() -- neither exists -- inside a bare `except Exception:
    return False`, so every media service always read 'offline'."""
    from integrations.agent_engine import content_gen_tracker as t
    from integrations.service_tools import runtime_manager as rm

    with patch.object(rm.runtime_tool_manager, 'get_tool_status',
                      return_value={'running': True}) as spy:
        assert t._check_media_service('tts') is True, (
            "a running tool must report available"
        )
    spy.assert_called_once_with('tts_audio_suite')

    with patch.object(rm.runtime_tool_manager, 'get_tool_status',
                      return_value={'running': False}):
        assert t._check_media_service('tts') is False


def test_restart_media_service_uses_the_real_runtime_manager_api():
    from integrations.agent_engine import content_gen_tracker as t
    from integrations.service_tools import runtime_manager as rm

    with patch.object(rm.runtime_tool_manager, 'setup_tool',
                      return_value={'running': True}) as spy:
        assert t._restart_media_service('tts') is True
    spy.assert_called_once_with('tts_audio_suite')

    with patch.object(rm.runtime_tool_manager, 'setup_tool',
                      return_value={'error': 'nope'}):
        assert t._restart_media_service('tts') is False


# ── model storage must honour the same env var gpu_worker reads ──

def test_model_storage_honours_hevolve_model_dir(tmp_path, monkeypatch):
    """gpu_worker._get_output_dir() reads HEVOLVE_MODEL_DIR, but
    ModelStorageManager hard-coded ~/.hevolve/models, so a production
    RuntimeToolManager() filled C: no matter what the operator set."""
    # NB: `from integrations.service_tools import model_storage` yields the
    # SINGLETON (the package __init__ re-exports it under the submodule's
    # own name), not the module -- import_module to get the module.
    from importlib import import_module
    ms = import_module('integrations.service_tools.model_storage')

    target = tmp_path / 'models_elsewhere'
    monkeypatch.setenv('HEVOLVE_MODEL_DIR', str(target))

    mgr = ms.ModelStorageManager()
    assert mgr.base_dir == target, (
        f"storage ignored HEVOLVE_MODEL_DIR: {mgr.base_dir}"
    )
    assert mgr.get_tool_dir('some_tool') == target / 'some_tool'


def test_explicit_base_dir_still_wins_over_the_env_var(tmp_path, monkeypatch):
    from importlib import import_module
    ms = import_module('integrations.service_tools.model_storage')

    monkeypatch.setenv('HEVOLVE_MODEL_DIR', str(tmp_path / 'from_env'))
    explicit = tmp_path / 'explicit'
    mgr = ms.ModelStorageManager(base_dir=explicit)
    assert mgr.base_dir == explicit
