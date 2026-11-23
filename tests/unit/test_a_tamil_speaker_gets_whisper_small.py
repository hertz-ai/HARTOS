"""The STT size follows the person's language (owner 2026-11-20, choice (a)).

"When a turn's preferred_lang isn't English (Tamil and so on), the one STT
pick prefers faster-whisper-small, through the existing setup. English-only
users keep today's size."  faster_whisper_model_size is the ONE pick; it only
ever upgraded to a size already on disk and nothing fetched one (its own
docstring: "that producer is open").  Now a non-English person's floor is
'small', fetched off the request path by the one background fetch when it is
not on disk yet; an English person's pick is unchanged and fetches nothing.

    python -m pytest tests/unit/test_a_tamil_speaker_gets_whisper_small.py -q
"""
from types import SimpleNamespace

import pytest


@pytest.fixture
def wt(monkeypatch):
    from integrations.service_tools import whisper_tool as wt
    monkeypatch.delenv('HEVOLVE_STT_MODEL_SIZE', raising=False)
    wt._fetched = []
    monkeypatch.setattr(wt, '_download_in_background',
                        lambda name: wt._fetched.append(name) or True)
    return wt


def _setup(monkeypatch, wt, *, lang, catalog_id, cached):
    import core.user_lang as ul
    monkeypatch.setattr(ul, 'get_preferred_lang', lambda *a, **k: lang)
    monkeypatch.setattr(wt, '_catalog_stt_entry',
                        lambda exclude=None: SimpleNamespace(id=catalog_id) if catalog_id else None)
    monkeypatch.setattr(wt, '_faster_whisper_model_cached', lambda size: size in cached)


class TestEnglishKeepsTodaysSize:
    def test_base_stays_base(self, monkeypatch, wt):
        _setup(monkeypatch, wt, lang='en', catalog_id='stt-faster-whisper-base', cached={'base'})
        assert wt.faster_whisper_model_size() == 'base'
        assert wt._fetched == []

    def test_a_bigger_pick_not_on_disk_fetches_nothing(self, monkeypatch, wt):
        _setup(monkeypatch, wt, lang='en-IN', catalog_id='stt-faster-whisper-small', cached={'base'})
        assert wt.faster_whisper_model_size() == 'base'
        assert wt._fetched == [], 'an English person started a download they never had'

    def test_unknown_language_is_english(self, monkeypatch, wt):
        _setup(monkeypatch, wt, lang=None, catalog_id=None, cached={'base'})
        assert wt.faster_whisper_model_size() == 'base'


class TestOtherLanguagesGetSmall:
    def test_tamil_uses_small_when_it_is_on_disk(self, monkeypatch, wt):
        _setup(monkeypatch, wt, lang='ta', catalog_id='stt-faster-whisper-base',
               cached={'base', 'small'})
        assert wt.faster_whisper_model_size() == 'small'

    def test_tamil_fetches_small_in_the_background_and_waits_on_base(self, monkeypatch, wt):
        _setup(monkeypatch, wt, lang='ta', catalog_id='stt-faster-whisper-base', cached={'base'})
        assert wt.faster_whisper_model_size() == 'base', 'a request waited on a download'
        assert wt._fetched == ['faster-whisper:small']

    def test_a_bigger_catalog_pick_is_kept(self, monkeypatch, wt):
        _setup(monkeypatch, wt, lang='hi', catalog_id='stt-faster-whisper-medium',
               cached={'base', 'small', 'medium'})
        assert wt.faster_whisper_model_size() == 'medium'

    def test_a_bigger_pick_not_on_disk_falls_back_to_small_not_base(self, monkeypatch, wt):
        """rv_66e6 nit: catalog picks medium, medium isn't downloaded, small is."""
        _setup(monkeypatch, wt, lang='ta', catalog_id='stt-faster-whisper-medium',
               cached={'base', 'small'})
        assert wt.faster_whisper_model_size() == 'small'
        assert wt._fetched == []

    def test_a_bigger_pick_with_nothing_on_disk_fetches_small_only(self, monkeypatch, wt):
        _setup(monkeypatch, wt, lang='ta', catalog_id='stt-faster-whisper-medium',
               cached={'base'})
        assert wt.faster_whisper_model_size() == 'base'
        assert wt._fetched == ['faster-whisper:small']

    def test_the_admins_size_still_wins(self, monkeypatch, wt):
        _setup(monkeypatch, wt, lang='ta', catalog_id='stt-faster-whisper-base', cached={'base'})
        monkeypatch.setenv('HEVOLVE_STT_MODEL_SIZE', 'tiny')
        assert wt.faster_whisper_model_size() == 'tiny'
        assert wt._fetched == []


def test_the_background_fetch_gets_the_faster_whisper_repo(monkeypatch):
    """The one background fetch knows the faster-whisper names it is handed."""
    from integrations.service_tools import whisper_tool as wt
    import huggingface_hub
    got = []
    monkeypatch.setattr(huggingface_hub, 'snapshot_download',
                        lambda repo_id, **k: got.append(repo_id))

    class _Now:
        def __init__(self, target, name, daemon):
            self.target = target

        def start(self):
            self.target()
    monkeypatch.setattr(wt.threading, 'Thread', _Now)
    wt._background_downloads.discard('faster-whisper:small')
    wt._background_failures.pop('faster-whisper:small', None)
    assert wt._download_in_background('faster-whisper:small') is True
    assert got == ['Systran/faster-whisper-small']
