"""is_downloaded() must answer about the DISK, not about bookkeeping.

Found live 2026-08-29: ~/.hevolve/models held 12.3 GB across four tools
(minicpm 6600MB, stt 5347MB, luxtts 342MB, tts 59MB) that were absent from
manifest.json, so is_downloaded() returned False for all of them.  The module
docstring promises the manifest lets "the RuntimeToolManager skip
re-downloads"; that promise fails whenever models arrive by a path that never
calls mark_downloaded().  Consumers affected: runtime_manager.py:126,160
(gates tool use) and :209,302 (reports download state to the UI).

The empty-directory case must keep returning False -- chatterbox/cosyvoice/
diffrhythm/kokoro each had a 0-file directory left behind by the
`tool_dir.mkdir()` that runs BEFORE a download attempt (diffrhythm's HF call
took a 401).  An empty dir is not a download.
"""
import pytest

from integrations.service_tools.model_storage import ModelStorageManager


@pytest.fixture
def store(tmp_path):
    return ModelStorageManager(base_dir=tmp_path)


def _populate(store, name, files=1):
    d = store.get_tool_dir(name)
    d.mkdir(parents=True, exist_ok=True)
    for i in range(files):
        (d / f"weights_{i}.bin").write_bytes(b"x" * 16)
    return d


def test_on_disk_without_manifest_entry_counts_as_downloaded(store):
    """The live defect: real weights present, no manifest row -> must be True."""
    _populate(store, "minicpm", files=3)
    assert store.is_downloaded("minicpm") is True


def test_empty_dir_without_manifest_entry_is_not_downloaded(store):
    """mkdir-before-download litter must NOT read as downloaded."""
    store.get_tool_dir("diffrhythm").mkdir(parents=True, exist_ok=True)
    assert store.is_downloaded("diffrhythm") is False


def test_absent_dir_is_not_downloaded(store):
    assert store.is_downloaded("never_fetched") is False


def test_manifest_entry_with_content_still_downloaded(store):
    """Pre-existing behaviour must not regress."""
    _populate(store, "acestep", files=2)
    store.mark_downloaded("acestep", "https://example/acestep", 1234)
    assert store.is_downloaded("acestep") is True


def test_manifest_entry_but_dir_emptied_is_not_downloaded(store):
    """Stale manifest row + wiped dir -> False (pre-existing behaviour)."""
    d = _populate(store, "ltx2", files=1)
    store.mark_downloaded("ltx2", "hf://x/ltx2", 99)
    for f in d.iterdir():
        f.unlink()
    assert store.is_downloaded("ltx2") is False
