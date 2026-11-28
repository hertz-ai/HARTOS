"""A chat reply is read without its <sing> tags and spoken with them.

Owner, 28 Nov 2026, singing ("all 4 parts"); design review 1 items 2a and
2e (hevolve-66e6).  Every /chat return goes through _chat_reply: the spoken
copy (_spoken_text, turn_speech_text) is taken first and handed to the
speaker with its tags, so a singer can sing; what the person READS -- the
JSON 'response', the cross-device mirror -- carries the words only.  The
order is pinned (hevolve-66e6 asked for it): the words are taken AFTER the
speech, so a reply that is only <music> is still voiced.

The TTS bundle's own text (_tts_synthesize_and_publish, the 'text' the page
types out while the clip plays) is the words too, cut at 200 characters
after the tags are gone, so no tag is ever cut in half on screen.

Real: _chat_reply, turn_speech_text, words_without_media_tags.
Faked: the speech engine (recorded) and the realtime publisher (recorded).

    python -m pytest tests/unit/test_a_chat_reply_is_read_without_its_sing_tags.py -q
"""
import os
import sys
import tempfile
from unittest.mock import patch

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))))

TURN = '7b2c4d1e-6a5f-4e3d-8c9b-0a1f2e3d4c5b'


@pytest.fixture
def chat(monkeypatch):
    monkeypatch.setenv('HEVOLVE_CACHE_DIR', tempfile.mkdtemp())
    import hart_intelligence_entry as hie
    spoken, published = [], []
    monkeypatch.setattr(hie, '_tts_synthesize_and_publish',
                        lambda text, *a, **k: spoken.append(text))
    monkeypatch.setattr(
        'core.safe_hartos_attr.safe_hartos_attr',
        lambda name: (lambda topic, payload, **f: published.append(payload))
        if name == 'publish_async' else None)

    def reply(text, body=None):
        with hie.app.test_request_context(
                '/chat', json=body or {'media_mode': 'audio', 'prompt': 'sing'}):
            resp = hie._chat_reply('u1', TURN, text)
        return resp.get_json(), spoken
    return reply


def test_the_person_reads_the_words_and_the_speaker_gets_the_tags(chat):
    body, spoken = chat('Here you go! <sing>Twinkle twinkle little star</sing>')
    assert body['response'] == 'Here you go! Twinkle twinkle little star'
    assert spoken and '<sing>Twinkle twinkle little star</sing>' in spoken[-1]


def test_a_reply_that_is_only_music_is_still_voiced(chat):
    body, spoken = chat('<music>calm piano</music>')
    assert spoken and '<music>' in spoken[-1], 'taken after the speech, not before'
    assert '<music>' not in (body.get('response') or '')


def test_a_reply_with_no_tag_is_unchanged(chat):
    body, spoken = chat('Plain words, nothing sung.')
    assert body['response'] == 'Plain words, nothing sung.'
    assert spoken[-1] == 'Plain words, nothing sung.'


def test_the_bundle_text_is_the_words_cut_after_the_tags_are_gone(monkeypatch, tmp_path):
    """_tts_synthesize_and_publish's bundle 'text' (what the page types out
    while the clip plays): the words, at most 200 characters, never half a
    tag.  The real function, with the speech engine stubbed (a clip file on
    disk), the worker run inline and the publisher recorded."""
    import json
    import types
    import hart_intelligence_entry as hie
    clip = tmp_path / 'clip.wav'
    clip.write_bytes(b'RIFF' + b'\0' * 64)
    fake = types.ModuleType('tts.tts_engine')
    fake.get_tts_engine = lambda: types.SimpleNamespace(backend_name='stub')
    fake.synthesize_text = lambda text, language=None, **k: str(clip)
    if 'tts' not in sys.modules:
        monkeypatch.setitem(sys.modules, 'tts', types.ModuleType('tts'))
    monkeypatch.setitem(sys.modules, 'tts.tts_engine', fake)

    class _Inline:
        def submit(self, fn, *a, **k):
            fn(*a, **k)

    monkeypatch.setattr(hie, '_tts_executor', _Inline())
    published = []
    monkeypatch.setattr(hie, 'publish_async',
                        lambda topic, payload, **k: published.append(payload))
    text = 'x ' * 95 + '<sing>la la</sing>'   # the tag starts at 190: [:200] cut it
    hie._tts_synthesize_and_publish(text, 'u1', TURN, language='en',
                                    llm_rewrite=False)
    bundles = [json.loads(p) for p in published
               if isinstance(p, str) and '"generated_audio_url"' in p]
    assert bundles, published
    shown = bundles[-1]['text'][0]
    assert '<' not in shown and '>' not in shown, shown
    assert shown.endswith('la la') and len(shown) <= 200, shown
