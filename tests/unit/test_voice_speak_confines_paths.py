"""/api/voice/speak cannot write or read an arbitrary file (#67).

hart_intelligence_entry.voice_speak() carries no auth decorator, so on a
bundled desktop a LOCAL same-machine caller reaches it with no credential (the
socket gate trusts 127.0.0.1).  It passed the caller's ``output_path`` straight
into TTSRouter.synthesize -- which writes the WAV to that exact path -- and the
caller's ``voice`` -- which a clone-capable engine READS as a reference-audio
file.  So an unauth local caller could overwrite any file the node may write
and read any file it may read.

The route now:
  - drops the caller's output_path (None -> the router writes to its own TTS
    output dir, the one /api/voice/audio serves), and
  - passes ``voice`` through only when it is a bare saved-voice NAME (no path
    separators, no traversal); a path-like value is dropped to None.

``engine`` is left as-is: TTSRouter.synthesize already accepts an override only
when it is in ENGINE_REGISTRY (tts_router.py), so an arbitrary engine string is
ignored there -- no route-level parallel guard is added.

Belt-and-suspenders: /api/voice is added to NETWORK_PROTECTED_PATHS so a keyed
or central node also asks another machine for a credential (a bundled desktop
already gates every non-local path).

    python -m pytest tests/unit/test_voice_speak_confines_paths.py -q
"""
import os
import sys
import types
from unittest.mock import MagicMock, patch

import pytest

_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)


@pytest.fixture(autouse=True)
def _flat_no_key(monkeypatch):
    """A plain flat node with no API key: network paths are public, so the
    route runs and we can inspect what it forwarded."""
    for name in ('NUNBA_BUNDLED', 'HEVOLVE_API_KEY', 'NUNBA_CI'):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv('HEVOLVE_NODE_TIER', 'flat')


@pytest.fixture
def result():
    from integrations.channels.media.tts_router import TTSResult
    return TTSResult(
        path='/home/n/.hevolve/models/pocket_tts/output/tts_1.wav',
        duration=1.0, engine_id='pocket_tts', device='cpu', location='local',
        latency_ms=100.0, sample_rate=24000, voice='alba', quality_score=0.8,
    )


def _speak(result, **body):
    """POST /api/voice/speak with a mocked router; return (resp, synth kwargs)."""
    from hart_intelligence_entry import app
    router = MagicMock()
    router.synthesize.return_value = result
    payload = {'text': 'hello'}
    payload.update(body)
    no_key = {'security.secrets_manager':
              types.SimpleNamespace(get_secret=lambda name: '')}
    with patch('integrations.channels.media.tts_router.get_tts_router',
               return_value=router), patch.dict(sys.modules, no_key):
        with app.test_client() as client:
            resp = client.post('/api/voice/speak', json=payload)
    assert router.synthesize.called, 'the route did not reach the router'
    return resp, router.synthesize.call_args.kwargs


def test_a_windows_output_path_is_not_honoured(result):
    resp, kw = _speak(result,
                      output_path='C:\\Windows\\System32\\drivers\\etc\\hosts')
    assert kw['output_path'] is None, (
        'the route wrote to a caller-chosen path: arbitrary file write')


def test_a_traversal_output_path_is_not_honoured(result):
    resp, kw = _speak(result, output_path='../../../../etc/cron.d/pwn')
    assert kw['output_path'] is None


def test_an_absolute_output_path_is_not_honoured(result):
    resp, kw = _speak(result, output_path='/etc/passwd')
    assert kw['output_path'] is None


@pytest.mark.parametrize('voice', [
    '../../etc/passwd',
    '/etc/shadow',
    'C:\\Users\\me\\secret.wav',
    'a/b.wav',
    'a\\b.wav',
    '..',
    '~/secret',
    'sound:with:colons',
])
def test_a_path_like_voice_is_dropped(result, voice):
    resp, kw = _speak(result, voice=voice)
    assert kw['voice'] is None, (
        'voice=%r reached the cloning engine as a file reference' % voice)


@pytest.mark.parametrize('voice',
                         ['alba', 'jo', 'alice', 'my_voice', 'voice-2', 'nova.v1'])
def test_a_saved_voice_name_is_kept(result, voice):
    resp, kw = _speak(result, voice=voice)
    assert kw['voice'] == voice, 'a legitimate saved voice name was dropped'


def test_no_voice_stays_none(result):
    resp, kw = _speak(result)
    assert kw['voice'] is None


def test_text_still_synthesizes_with_a_clean_call(result):
    resp, kw = _speak(result, voice='alba', source='chat_response',
                      language='en')
    assert resp.status_code == 200
    assert kw['text'] == 'hello'
    assert kw['source'] == 'chat_response'
    assert kw['language'] == 'en'
    body = resp.get_json()
    assert body['audio_url'] == '/api/voice/audio/tts_1.wav'


def test_engine_override_is_still_forwarded(result):
    # TTSRouter.synthesize validates the override against ENGINE_REGISTRY, so
    # the route forwards it unchanged rather than duplicating that guard.
    resp, kw = _speak(result, engine='luxtts')
    assert kw['engine_override'] == 'luxtts'


# --- belt-and-suspenders: the network gate now covers /api/voice ------------

def test_the_write_read_voice_routes_are_gated_audio_is_not():
    """Gate the two routes that write/read files (speak, clone); leave the
    read-only, traversal-safe audio serve (and the voices list) public so a
    browser <audio src=...> fetch is not broken on a keyed node (hartos-3e
    ruling (B))."""
    from security.middleware import NETWORK_PROTECTED_PATHS

    def matches(path):
        return any(path == p or path.startswith(p + '/')
                   for p in NETWORK_PROTECTED_PATHS)

    assert matches('/api/voice/speak')
    assert matches('/api/voice/clone')
    assert not matches('/api/voice/audio/x.wav')
    assert not matches('/api/voice/voices')


def test_api_voice_needs_a_credential_from_another_machine(monkeypatch):
    """On a keyed node, /api/voice/* asks a LAN caller for the credential.
    (A bundled desktop already gates every non-local path; this covers the
    central/keyed tiers, where /api/voice was public before.)"""
    from flask import Flask
    from security.middleware import _apply_api_auth

    monkeypatch.delenv('NUNBA_BUNDLED', raising=False)
    monkeypatch.delenv('NUNBA_CI', raising=False)
    monkeypatch.setenv('HEVOLVE_NODE_TIER', 'flat')

    app = Flask('voice_gate')
    app.add_url_rule('/api/voice/speak', 'vs', lambda: {'ok': True},
                     methods=['POST'])
    _apply_api_auth(app)
    client = app.test_client()
    keyed = {'security.secrets_manager':
             types.SimpleNamespace(get_secret=lambda name: 'k-1')}
    lan = {'REMOTE_ADDR': '192.168.0.50'}
    with patch.dict(sys.modules, keyed):
        assert client.post('/api/voice/speak',
                           environ_base=lan).status_code == 401
        assert client.post('/api/voice/speak', environ_base=lan,
                           headers={'X-API-Key': 'k-1'}).status_code == 200


# --- /api/voice/clone: a LOCAL caller cannot read/write an arbitrary file ---
# The network gate does not cover a bundled desktop's own callers, so clone is
# confined at the route: audio_path must be an audio file with no traversal
# (it is READ and copied into the voices dir) and name must have no path
# separators (it is joined into the voices dir as <name>.wav = a write) (#67).

def _clone(**body):
    from hart_intelligence_entry import app
    import json as _json
    fake = MagicMock(return_value=_json.dumps(
        {'saved': True, 'name': 'v', 'path': '/x/v.wav'}))
    payload = {'audio_path': '/tmp/sample.wav', 'name': 'myvoice'}
    payload.update(body)
    no_key = {'security.secrets_manager':
              types.SimpleNamespace(get_secret=lambda name: '')}
    with patch('integrations.service_tools.luxtts_tool.luxtts_clone_voice',
               fake), patch.dict(sys.modules, no_key):
        with app.test_client() as client:
            resp = client.post('/api/voice/clone', json=payload)
    return resp, fake


@pytest.mark.parametrize('audio_path', [
    '/etc/passwd',
    '/home/n/.ssh/id_ed25519',
    'C:\\Windows\\win.ini',
    'notes.txt',
])
def test_clone_refuses_a_non_audio_sample(audio_path):
    resp, fake = _clone(audio_path=audio_path)
    assert resp.status_code == 400, 'a non-audio file was read as a voice sample'
    assert not fake.called


def test_clone_refuses_a_traversal_sample():
    resp, fake = _clone(audio_path='../../../../etc/secret.wav')
    assert resp.status_code == 400
    assert not fake.called


@pytest.mark.parametrize('name', [
    '../../etc/cron.d/pwn',
    'a/b',
    'a\\b',
    'c:evil',
    '..',
])
def test_clone_refuses_a_path_like_name(name):
    resp, fake = _clone(name=name)
    assert resp.status_code == 400, 'a path-like name could write outside voices'
    assert not fake.called


def test_clone_allows_a_normal_request(monkeypatch):
    resp, fake = _clone(audio_path='/tmp/sample.wav', name='My Voice')
    assert resp.status_code == 200
    fake.assert_called_once_with('/tmp/sample.wav', 'My Voice')
