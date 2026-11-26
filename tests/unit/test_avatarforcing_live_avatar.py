"""The live talking-avatar stream rides the reply's TTS bundle (avatarforcing_tool).

add_live_avatar is the one place both TTS publishers (hart_intelligence_entry's
_tts_synthesize_and_publish and Nunba's _fire_nunba_tts) give a reply a face.
These pin what a client receives and what it costs the reply: the stream only on
a video turn of an avatar with a portrait while the model runs, the audio always
kept, the phone's URL absolute, never more than SESSION_BUDGET_S added to the
audio publish, the model set up in the background only where it can run (a GPU
with room) and never in a storm, and the lifecycle's crash count untouched until
a session was really made.
"""
import json
import threading
import time
from concurrent.futures import Future
from pathlib import Path

import pytest

from integrations.service_tools import avatarforcing_tool as af
from integrations.service_tools import runtime_manager as rm


class _Resp:
    def __init__(self, code, body):
        self.status_code, self._body = code, body

    def json(self):
        return self._body


@pytest.fixture(autouse=True)
def clean_state(monkeypatch):
    monkeypatch.setattr(af, '_portraits', {})
    monkeypatch.setattr(af, '_setup', {'started_at': 0.0, 'failed': False, 'future': None})
    accessed = []
    import integrations.service_tools.model_lifecycle as ml
    monkeypatch.setattr(ml, 'get_model_lifecycle_manager',
                        lambda: type('M', (), {'notify_access': lambda self, t: accessed.append(t)})())
    return accessed


@pytest.fixture
def bundle():
    return {
        'text': ['hello'], 'generated_audio_url': '/tts/audio/a.wav',
        'request_id': 'r1', 'action': 'TTS',
        'video_link': {'aud_url': 'http://192.168.0.5:5000/tts/audio/a.wav',
                       'request_id': 'r1', 'action': 'TTS', 'is_cartoon': True,
                       'priority': 0},
    }


@pytest.fixture
def audio(tmp_path):
    a = tmp_path / 'a.wav'
    a.write_bytes(b'wav')
    return str(a)


@pytest.fixture
def running(monkeypatch, tmp_path):
    """The model is running on port 4321 and avatar 7 has a portrait."""
    face = tmp_path / 'face.png'
    face.write_bytes(b'png')
    posts = []
    monkeypatch.setattr(rm.runtime_tool_manager, 'tool_enabled', lambda t: True)
    monkeypatch.setattr(rm.runtime_tool_manager, 'get_tool_port', lambda t: 4321)
    monkeypatch.setattr(af, '_can_run_here', lambda rtm: True)
    lookups = []
    monkeypatch.setattr('core.teacher_avatar.image_reference',
                        lambda a: lookups.append(a) or (str(face) if a == 7 else None))

    def post(url, timeout=None, json=None):
        posts.append((url, timeout, json))
        return _Resp(200, {'sid': 'abcdef012345', 'stream': '/live.mp4?sid=abcdef012345',
                           'frames': 243, 'fps': 25})
    monkeypatch.setattr('core.http_pool.pooled_post_once', post)
    return type('Running', (), {'posts': posts, 'lookups': lookups, 'face': face})


def test_video_turn_gets_the_stream_and_keeps_the_audio(bundle, running, audio, clean_state):
    assert af.add_live_avatar(bundle, audio_path=audio, avatar_id=7, media_mode='video',
                              phone_base='http://192.168.0.5:5000/')
    assert bundle['generated_video_url'] == '/avatar/live/abcdef012345.mp4'
    assert bundle['video_seconds'] == 9.72  # 243 frames at 25 fps, the sidecar's own rate
    link = bundle['video_link']
    assert link['video'] == 'http://192.168.0.5:5000/avatar/live/abcdef012345.mp4'
    assert link['stream'] == 'live'
    # today's Android app plays `video` only when is_cartoon is False, with no device token:
    # the flag stays, so it keeps playing the voice over the idle avatar
    assert link['is_cartoon'] is True
    assert link['aud_url'] == 'http://192.168.0.5:5000/tts/audio/a.wav'
    assert bundle['generated_audio_url'] == '/tts/audio/a.wav'
    url, timeout, body = running.posts[0]
    assert url == 'http://127.0.0.1:4321/session'
    assert timeout <= af.SESSION_BUDGET_S
    assert body['audio_url'] == Path(audio).resolve().as_uri()   # the sidecar reads it itself
    assert body['image_url'] == Path(running.face).resolve().as_uri()
    assert clean_state == ['avatarforcing']  # a real session keeps the model from the idle sweep
    json.dumps(bundle)  # still publishable


def test_page_base_makes_the_page_url_absolute(bundle, running, audio):
    af.add_live_avatar(bundle, audio_path=audio, avatar_id=7, media_mode='video',
                       phone_base='https://node.example', page_base='https://node.example')
    assert bundle['generated_video_url'] == 'https://node.example/avatar/live/abcdef012345.mp4'


@pytest.mark.parametrize('mode', ['audio', 'text', None, 'VIDEO'])
def test_only_a_video_turn_is_seen(bundle, running, audio, mode):
    before = json.dumps(bundle, sort_keys=True)
    assert not af.add_live_avatar(bundle, audio_path=audio, avatar_id=7, media_mode=mode)
    assert json.dumps(bundle, sort_keys=True) == before
    assert running.posts == [] and running.lookups == []  # nothing looked up or started


def test_no_avatar_or_no_portrait_is_heard_only(bundle, running, audio):
    assert not af.add_live_avatar(bundle, audio_path=audio, avatar_id=None, media_mode='video')
    assert not af.add_live_avatar(bundle, audio_path=audio, avatar_id=8, media_mode='video')
    assert 'generated_video_url' not in bundle
    assert running.posts == []


def test_the_portrait_is_looked_up_once_per_conversation(bundle, running, audio):
    for _ in range(3):
        af.add_live_avatar(dict(bundle), audio_path=audio, avatar_id=7, media_mode='video')
    assert running.lookups == [7]


def test_model_still_loading_is_heard_and_marks_nothing(bundle, running, audio, monkeypatch, clean_state):
    monkeypatch.setattr('core.http_pool.pooled_post_once',
                        lambda *a, **k: _Resp(503, {'error': 'loading'}))
    assert not af.add_live_avatar(bundle, audio_path=audio, avatar_id=7, media_mode='video')
    assert bundle['video_link']['is_cartoon'] is True
    assert clean_state == []  # the lifecycle's crash count is not reset by a failed session


def test_sidecar_failure_never_breaks_the_reply(bundle, running, audio, monkeypatch):
    def boom(*a, **k):
        raise ConnectionError('refused')
    monkeypatch.setattr('core.http_pool.pooled_post_once', boom)
    assert not af.add_live_avatar(bundle, audio_path=audio, avatar_id=7, media_mode='video')
    assert 'video' not in bundle['video_link']


def test_a_slow_face_never_holds_the_audio_past_the_budget(bundle, running, audio, monkeypatch):
    release = threading.Event()
    monkeypatch.setattr(af, 'SESSION_BUDGET_S', 0.3)
    monkeypatch.setattr('core.teacher_avatar.image_reference',
                        lambda a: release.wait(5) and None)  # a portrait download that hangs
    try:
        t = time.monotonic()
        assert not af.add_live_avatar(bundle, audio_path=audio, avatar_id=7, media_mode='video')
        assert time.monotonic() - t < 1.0
        assert 'generated_video_url' not in bundle
    finally:
        release.set()  # the shared worker is free for the next test whatever happened


def test_bundle_without_a_phone_link_gets_none(running, audio):
    """Nunba's fallback publisher sends no phone bundle: the page gets the stream, and no
    phone link is made up (one without aud_url would play the app's error video)."""
    payload = {'generated_audio_url': '/tts/audio/a.wav', 'request_id': 'r2', 'action': 'TTS'}
    assert af.add_live_avatar(payload, audio_path=audio, avatar_id=7, media_mode='video',
                              phone_base='http://10.0.0.2:5000')
    assert payload['generated_video_url'] == '/avatar/live/abcdef012345.mp4'
    assert 'video_link' not in payload


class TestAutoSetup:
    @pytest.fixture
    def not_running(self, monkeypatch, tmp_path):
        face = tmp_path / 'face.png'
        face.write_bytes(b'png')
        calls = []
        monkeypatch.setattr(rm.runtime_tool_manager, 'tool_enabled', lambda t: True)
        monkeypatch.setattr(rm.runtime_tool_manager, 'get_tool_port', lambda t: None)
        monkeypatch.setattr('core.teacher_avatar.image_reference', lambda a: str(face) if a == 7 else None)
        monkeypatch.setattr(rm.runtime_tool_manager.vram, 'suggest_offload_mode', lambda t: 'gpu')
        monkeypatch.setattr(rm.runtime_tool_manager.vram, 'detect_gpu', lambda: {'cuda_available': True})

        def setup_async(tool, result=None):
            calls.append(tool)
            f = Future()
            f.set_result(result or {'error': 'no CUDA device'})
            return f
        monkeypatch.setattr(rm.runtime_tool_manager, 'setup_tool_async', setup_async)
        return calls

    def test_starts_once_then_waits(self, bundle, audio, not_running, monkeypatch):
        assert not af.add_live_avatar(bundle, audio_path=audio, avatar_id=7, media_mode='video')
        assert not_running == ['avatarforcing']
        for _ in range(3):
            af.add_live_avatar(bundle, audio_path=audio, avatar_id=7, media_mode='video')
        assert not_running == ['avatarforcing']  # no storm
        # a failed one waits the longer window, whatever happened
        monkeypatch.setitem(af._setup, 'started_at', time.time() - af.SETUP_RETRY_S - 1)
        af.add_live_avatar(bundle, audio_path=audio, avatar_id=7, media_mode='video')
        assert not_running == ['avatarforcing']
        monkeypatch.setitem(af._setup, 'started_at', time.time() - af.SETUP_RETRY_AFTER_FAILURE_S - 1)
        af.add_live_avatar(bundle, audio_path=audio, avatar_id=7, media_mode='video')
        assert not_running == ['avatarforcing', 'avatarforcing']

    def test_never_on_a_node_that_cannot_run_it(self, bundle, audio, not_running, monkeypatch):
        monkeypatch.setattr(rm.runtime_tool_manager.vram, 'suggest_offload_mode', lambda t: 'cpu_only')
        af.add_live_avatar(bundle, audio_path=audio, avatar_id=7, media_mode='video')
        assert not_running == []  # no CUDA venv, no weights, for a model that cannot start

    def test_never_on_a_mac_whose_metal_the_advisor_calls_gpu(self, bundle, audio, not_running, monkeypatch):
        monkeypatch.setattr(rm.runtime_tool_manager.vram, 'detect_gpu',
                            lambda: {'cuda_available': False, 'metal_available': True})
        lookups = []
        monkeypatch.setattr('core.teacher_avatar.image_reference', lambda a: lookups.append(a))
        af.add_live_avatar(bundle, audio_path=audio, avatar_id=7, media_mode='video')
        assert not_running == []  # the engine is CUDA-only
        assert lookups == []      # and no portrait lookup holds the audio for nothing

    def test_never_for_an_avatar_without_a_portrait(self, bundle, audio, not_running):
        af.add_live_avatar(bundle, audio_path=audio, avatar_id=8, media_mode='video')
        assert not_running == []

    def test_switched_off_model_is_never_set_up(self, bundle, audio, not_running, monkeypatch):
        monkeypatch.setattr(rm.runtime_tool_manager, 'tool_enabled', lambda t: False)
        af.add_live_avatar(bundle, audio_path=audio, avatar_id=7, media_mode='video')
        assert not_running == []


def test_the_sidecar_is_not_offered_to_agents(monkeypatch, caplog):
    """The reply pipeline drives it; an agent tool would hand agents a URL that plays nowhere.
    Known on purpose (no "No tool wrapper" warning), registered as nothing."""
    from integrations.service_tools.registry import service_tool_registry
    registered = []
    monkeypatch.setattr(service_tool_registry, 'register_tool', lambda info: registered.append(info))
    monkeypatch.setattr(service_tool_registry, 'unregister_tool', lambda name: None)
    with caplog.at_level('WARNING'):
        rm.runtime_tool_manager._register_tool_at_port('avatarforcing', 4321)
    assert registered == []
    assert 'No tool wrapper' not in caplog.text


def test_a_talking_avatar_is_not_an_installed_text_to_video_model(monkeypatch):
    """generate_media(video) on a node whose only video_gen model is the talking avatar says
    "offline" (worth installing one), not "installed but busy" (hartos-3a F4's distinction)."""
    from integrations.service_tools import media_agent
    from integrations.service_tools.model_catalog import ModelCatalog

    cat = ModelCatalog.__new__(ModelCatalog)
    rows = [type('E', (), {'downloaded': True, 'capabilities': {'talking_head': True, 'txt2vid': False}})()]
    monkeypatch.setattr(cat, 'list_by_type', lambda t: rows if t == 'video_gen' else [], raising=False)
    monkeypatch.setattr('integrations.service_tools.model_catalog.get_catalog', lambda: cat)
    assert media_agent._node_has_any('video_gen')
    assert not media_agent._node_has_any('video_gen', 'txt2vid')
    assert media_agent._node_has_any('video_gen', 'talking_head')


class TestImageReference:
    """core.teacher_avatar.image_reference: the portrait as a local file."""

    def _avatar(self, monkeypatch, url):
        import core.teacher_avatar as ta
        monkeypatch.setattr(ta, 'lookup_avatar', lambda a, database_url=None: {'image_url': url})
        return ta

    def test_this_nodes_upload(self, monkeypatch, tmp_path):
        f = tmp_path / 'x.png'
        f.write_bytes(b'png')
        ta = self._avatar(monkeypatch, '/uploads/avatars/x.png')
        monkeypatch.setattr('integrations.learning.book_pipeline.resolve_upload_url',
                            lambda u: f if u == '/uploads/avatars/x.png' else None)
        assert ta.image_reference(5) == str(f)

    def test_central_url_is_downloaded_as_an_image(self, monkeypatch):
        ta = self._avatar(monkeypatch, 'https://cdn.example/x.png')
        seen = []
        monkeypatch.setattr('integrations.agent_engine.video_orchestrator.download_asset',
                            lambda u, kind: seen.append((u, kind)) or '/cache/x.png')
        assert ta.image_reference(5) == '/cache/x.png'
        assert seen == [('https://cdn.example/x.png', 'images')]

    @pytest.mark.parametrize('url', [None, '', 'C:/Windows/x.png', 'file:///etc/passwd', 42])
    def test_anything_else_has_no_face(self, monkeypatch, url):
        ta = self._avatar(monkeypatch, url)
        assert ta.image_reference(5) is None
