"""Transcription is language agnostic (owner 2026-11-24).

Owner, verbatim: "transcription shd be language agnostic based on language
spoken it used to work earlier regardless of which language i spoke".

Measured on the owner's desktop (Nunba server.log, 11-22 to 11-24): every STT
worker life logged "catalog picks faster-whisper 'medium' but it is not
downloaded; staying on 'base'" and base then auto-detected Tamil and English
speech as jw/ko/ja/zh/hi/ne.  The phones send the child's speech to the
desktop's /api/voice/transcribe with NO language (RN DesktopTranscriber), so
the size the one resolver (faster_whisper_model_size) picks must not hang on
the person's stored language: every person gets the multilingual floor
('small'), fetched off the request path by the one background fetch when it is
not on disk yet.  Everything else about the pick is unchanged (the admin's
size wins; a bigger catalog pick already on disk is kept; a request never
waits on a download).

    python -m pytest tests/unit/test_stt_floor_is_language_agnostic.py -q --noconftest
"""
import json
from types import SimpleNamespace

import pytest


@pytest.fixture
def wt(monkeypatch):
    from integrations.service_tools import whisper_tool as wt
    monkeypatch.delenv('HEVOLVE_STT_MODEL_SIZE', raising=False)
    fetched = []
    monkeypatch.setattr(wt, '_download_in_background',
                        lambda name: fetched.append(name) or True)
    monkeypatch.setattr(wt, '_fetched', fetched, raising=False)
    return wt


def _setup(monkeypatch, wt, *, lang, catalog_id, cached):
    import core.user_lang as ul
    monkeypatch.setattr(ul, 'get_preferred_lang', lambda *a, **k: lang)
    monkeypatch.setattr(
        wt, '_catalog_stt_entry',
        lambda exclude=None: SimpleNamespace(id=catalog_id) if catalog_id else None)
    monkeypatch.setattr(wt, '_faster_whisper_model_cached', lambda size: size in cached)


@pytest.mark.parametrize('lang', ['en', 'en-IN', None, 'ta', 'hi'])
def test_every_person_gets_small_when_it_is_on_disk(monkeypatch, wt, lang):
    _setup(monkeypatch, wt, lang=lang, catalog_id='stt-faster-whisper-base',
           cached={'base', 'small'})
    assert wt.faster_whisper_model_size() == 'small', (
        f'a person whose stored language is {lang!r} got a size that '
        f'misdetects other languages')
    assert wt._fetched == []


@pytest.mark.parametrize('lang', ['en', None, 'ta'])
def test_a_missing_small_is_fetched_in_the_background_for_every_person(monkeypatch, wt, lang):
    """The owner's box: the catalog picks medium, only base is on disk."""
    _setup(monkeypatch, wt, lang=lang, catalog_id='stt-faster-whisper-medium',
           cached={'base'})
    assert wt.faster_whisper_model_size() == 'base', 'a request waited on a download'
    assert wt._fetched == ['faster-whisper:small']


def test_a_bigger_pick_already_on_disk_is_kept(monkeypatch, wt):
    _setup(monkeypatch, wt, lang='en', catalog_id='stt-faster-whisper-medium',
           cached={'base', 'small', 'medium'})
    assert wt.faster_whisper_model_size() == 'medium'
    assert wt._fetched == []


def test_the_admins_size_still_wins(monkeypatch, wt):
    _setup(monkeypatch, wt, lang='en', catalog_id='stt-faster-whisper-medium',
           cached={'base'})
    monkeypatch.setenv('HEVOLVE_STT_MODEL_SIZE', 'base')
    assert wt.faster_whisper_model_size() == 'base'
    assert wt._fetched == []


class _FakeTool:
    def __init__(self):
        self.requests = []
        self.vram_budget = None

    def is_alive(self):
        return False

    def call(self, req):
        self.requests.append(dict(req))
        return {'raw_json': json.dumps({'text': 'vanakkam', 'language': 'ta'}),
                'text': 'vanakkam', 'language': 'ta'}


def test_the_phones_call_with_no_language_runs_on_small(monkeypatch, wt):
    """The route the phones use passes language=None (auto-detect); on a box
    whose person is stored as English it must still run the multilingual size."""
    _setup(monkeypatch, wt, lang='en', catalog_id='stt-faster-whisper-medium',
           cached={'base', 'small'})
    tool = _FakeTool()
    monkeypatch.setattr(wt, '_stt_tool', tool)
    monkeypatch.setattr(wt, '_stt_worker_size', None)
    monkeypatch.setattr(wt, '_audit_transcript', lambda *a, **k: None)

    wt.whisper_transcribe('child.wav', None)

    assert [r['model_size'] for r in tool.requests] == ['small']
    assert tool.requests[0]['language'] is None
    assert tool.vram_budget == wt._vram_key_for_size('small')
