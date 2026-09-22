"""The Wan2GP sidecar must not report a video it never generated.

Found 2026-09-21 by the video-generation agent while proving the
RuntimeToolManager path, and verified here by reading the worker:

    servers/wan2gp_server.py::_generate_video_worker
        # TODO: Replace with actual Wan2GP inference call once repo is cloned
        _tasks[task_id] = {
            'status': 'complete',
            'video_url': f"/video/{task_id}",
            'output_path': output_path,
            ...
        }

It reads prompt / num_frames / width / height / steps, discards all five,
writes no file, and reports success with a URL and a path that lead
nowhere.  `_load_pipeline` is the same shape one level down: it imports
torch, builds a dict with `'loaded': True`, logs "Wan2GP pipeline loaded"
and has loaded nothing.

So setup, start, register and generate all succeed, and every generation
silently returns nothing.  This is the same defect the TTS sidecar had
this session -- a fabricated `{'success': True, 'audio_url': ...}` for a
file that was never written -- and it gets the same answer: refuse
honestly, so the caller learns immediately instead of polling a task that
will never produce anything.
"""
import pytest


@pytest.fixture
def client(monkeypatch):
    from integrations.service_tools.servers import wan2gp_server
    monkeypatch.setattr(wan2gp_server, '_tasks', wan2gp_server._tasks.__class__())
    wan2gp_server.app.config['TESTING'] = True
    return wan2gp_server.app.test_client()


class TestGenerateRefusesRatherThanFabricates:

    def test_generate_does_not_accept_a_task_it_cannot_perform(self, client):
        resp = client.post('/generate', json={'prompt': 'a cat on a wall'})
        assert resp.status_code == 501, (
            "an unimplemented generator must refuse, not queue a task whose "
            "only possible outcome is a fabricated success")
        body = resp.get_json()
        assert 'task_id' not in body, (
            "no task id may be handed out for work that will not happen -- "
            "the caller would poll it forever")
        assert body.get('error'), "the refusal has to say why"

    def test_refusal_names_what_is_missing(self, client):
        body = client.post('/generate', json={'prompt': 'hello'}).get_json()
        text = (body.get('error', '') + ' ' + body.get('detail', '')).lower()
        assert 'wan2gp' in text
        assert any(word in text for word in ('not implemented', 'unimplemented',
                                             'not integrated', 'no adapter')), \
            f"the reason must be legible to a person, got {body!r}"

    def test_missing_prompt_is_still_a_400(self, client):
        """The refusal must not swallow ordinary request validation."""
        resp = client.post('/generate', json={})
        assert resp.status_code == 400

    def test_no_task_survives_the_refusal(self, client):
        from integrations.service_tools.servers import wan2gp_server
        client.post('/generate', json={'prompt': 'x'})
        assert wan2gp_server._tasks == {}, (
            "a refused request must leave no pending task behind")


class TestPipelineDoesNotClaimToBeLoaded:

    def test_load_pipeline_reports_not_loaded(self, monkeypatch):
        from integrations.service_tools.servers import wan2gp_server
        monkeypatch.setattr(wan2gp_server, '_pipeline', None)
        result = wan2gp_server._load_pipeline()
        assert result.get('loaded') is False, (
            "building a dict is not loading a model; `loaded: True` here is "
            "what let /generate believe it could serve")
        assert result.get('error'), "an unloaded pipeline must say why"


class TestHealthDoesNotAdvertiseAServiceItCannotGive:

    def test_health_declares_the_generator_unavailable(self, client):
        body = client.get('/health').get_json()
        assert body.get('generate_available') is False, (
            "a health probe that says ok while /generate cannot work is the "
            "false-healthy signal this repo keeps paying for")


class TestTheCallerRoutesAroundTheRefusal:
    """A refusal is only half an answer: the selector picks wan2gp on any
    card with >=8 GB free, so without a fall-through every such machine
    turns a request ltx2 could serve into an error."""

    def test_wan2gp_error_falls_back_to_ltx2(self, monkeypatch):
        from integrations.service_tools import media_agent
        monkeypatch.setattr(media_agent, '_select_video_tool', lambda: 'wan2gp')
        monkeypatch.setattr(media_agent, '_ensure_tool_running', lambda name: True)
        monkeypatch.setattr(
            media_agent, '_generate_video_wan2gp',
            lambda prompt, duration: {'status': 'error',
                                      'error': 'Wan2GP HTTP 501',
                                      'output_modality': 'video'})
        monkeypatch.setattr(
            media_agent, '_generate_video_ltx2',
            lambda prompt, duration: {'status': 'ok', 'model_used': 'ltx2'})
        out = media_agent._generate_video('ctx', 'a cat', 2, '', 'auto')
        assert out['model_used'] == 'ltx2'

    def test_a_working_wan2gp_is_not_second_guessed(self, monkeypatch):
        from integrations.service_tools import media_agent
        monkeypatch.setattr(media_agent, '_select_video_tool', lambda: 'wan2gp')
        monkeypatch.setattr(media_agent, '_ensure_tool_running', lambda name: True)
        monkeypatch.setattr(
            media_agent, '_generate_video_wan2gp',
            lambda prompt, duration: {'status': 'pending', 'model_used': 'wan2gp'})

        def _must_not_run(prompt, duration):
            raise AssertionError('ltx2 must not be called when wan2gp served')

        monkeypatch.setattr(media_agent, '_generate_video_ltx2', _must_not_run)
        assert media_agent._generate_video('c', 'x', 2, '', 'auto')['model_used'] == 'wan2gp'
