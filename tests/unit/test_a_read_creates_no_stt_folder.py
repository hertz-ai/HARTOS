"""Asking whether a speech model is on disk writes nothing (rv_dlf minor, 11-24).

The catalog's downloaded flag now comes from the loaders (owner 2026-11-24:
"single source of truth of what exists in disk"), and the STT loader answers
through whisper_tool._sherpa_model_cached.  That check went through
_get_stt_dir(), which CREATES ~/.hevolve/models/stt, so on a fresh install a
mere boot-time read made an empty model folder.  A read must not change the
disk it is reading; only a download creates the folder.

    python -m pytest tests/unit/test_a_read_creates_no_stt_folder.py -q --noconftest
"""
import pytest


@pytest.fixture
def stt_root(monkeypatch, tmp_path):
    # The package re-exports the instance under the submodule's name, so
    # import the instance from the submodule itself.
    from integrations.service_tools.model_storage import model_storage
    root = tmp_path / 'models'
    monkeypatch.setattr(model_storage, 'get_tool_dir',
                        lambda tool_name: root / tool_name)
    return root / 'stt'


def test_the_on_disk_check_creates_no_folder(stt_root):
    from integrations.service_tools import whisper_tool as wt
    assert not stt_root.exists()
    for key in wt._SHERPA_MODELS:
        assert wt._sherpa_model_cached(key) is False
    assert not stt_root.exists(), 'a read created the STT model folder'


def test_a_model_that_is_on_disk_still_reads_as_on_disk(stt_root):
    from integrations.service_tools import whisper_tool as wt
    key = 'whisper-small'
    cfg = wt._SHERPA_MODELS[key]
    model_dir = stt_root / cfg['dir']
    model_dir.mkdir(parents=True)
    for name in cfg['files'].values():
        (model_dir / name).write_bytes(b'x')
    assert wt._sherpa_model_cached(key) is True


def test_a_download_makes_the_folder_when_it_writes(stt_root, monkeypatch):
    """No caller asks for the folder: the download creates it as it writes
    the archive there.  The fetch itself is stubbed (no network)."""
    import urllib.request
    from integrations.service_tools import whisper_tool as wt
    wrote_into = []

    def _fake_fetch(url, path):
        wrote_into.append(path)
        raise OSError('stubbed fetch, no network in tests')

    monkeypatch.setattr(urllib.request, 'urlretrieve', _fake_fetch)
    # Cleanup of the work folder is not what this test is about, and the
    # dev venv's mixed stdlib breaks shutil.rmtree (task #35).
    monkeypatch.setattr(wt.shutil, 'rmtree', lambda *a, **k: None)
    assert not stt_root.exists()
    with pytest.raises(OSError):
        wt._download_model('whisper-small')
    assert wrote_into and str(stt_root) in wrote_into[0], wrote_into
    assert stt_root.is_dir(), 'the download had no folder to write into'
