"""#113: behavioural test for neutts_tool — mock the ToolWorker boundary (no real
model subprocess), call the REAL public functions, and assert the forwarding
contract + voice listing. Replaces the prior membership-string check in
test_tts_router (which only proved the engine name survived in a list).
No grep/source-shape assertions.
"""
import json
import os
import sys

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import integrations.service_tools.neutts_tool as neutts  # noqa: E402


def test_synthesize_forwards_defaults_to_worker(monkeypatch):
    seen = {}
    monkeypatch.setattr(neutts._tool, 'synthesize',
                        lambda **kw: (seen.update(kw), '{"ok": true}')[1])
    out = neutts.neutts_synthesize('hello world')
    assert out == '{"ok": true}'          # worker result passed straight through
    assert seen['text'] == 'hello world'
    assert seen['voice'] == 'jo'          # default upstream sample voice
    assert seen['language'] == 'en'       # NeuTTS Air is English-only


def test_synthesize_forces_english_keeps_voice_and_path(monkeypatch):
    seen = {}
    monkeypatch.setattr(neutts._tool, 'synthesize',
                        lambda **kw: (seen.update(kw), '{}')[1])
    neutts.neutts_synthesize('bonjour', language='fr', voice='myclone',
                             output_path='/tmp/x.wav')
    assert seen['language'] == 'en'       # 'fr' is forced to 'en', not forwarded
    assert seen['voice'] == 'myclone'
    assert seen['output_path'] == '/tmp/x.wav'


def test_list_voices_lists_cloned_requires_transcript(monkeypatch, tmp_path):
    # A cloned voice = a .wav WITH a companion .txt transcript; a .wav without
    # one is not a usable NeuTTS reference and must be skipped.
    (tmp_path / 'alice.wav').write_bytes(b'RIFF....WAVE')
    (tmp_path / 'alice.txt').write_text('hello there')
    (tmp_path / 'orphan.wav').write_bytes(b'RIFF')      # no transcript
    monkeypatch.setattr(neutts, '_get_voices_dir', lambda: tmp_path)

    out = json.loads(neutts.neutts_list_voices())
    by_id = {v['id']: v for v in out['voices']}
    assert 'alice' in by_id
    assert by_id['alice']['type'] == 'cloned'
    assert by_id['alice']['language'] == 'en'
    assert 'orphan' not in by_id          # .wav without .txt -> not listed
