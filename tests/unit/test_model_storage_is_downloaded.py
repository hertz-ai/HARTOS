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

from integrations.service_tools.model_storage import (
    DEFAULT_BASE_DIR, ModelStorageManager, get_base_dir,
)


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


def test_dir_holding_only_an_empty_subdir_is_not_downloaded(store):
    """Self-caught regression 2026-08-29.

    kokoro / chatterbox / cosyvoice each held ONLY an empty `output/`
    subdirectory -- 0 files recursively.  `any(tool_dir.iterdir())` counts that
    directory entry as content, so a first pass at this fix flipped all three
    from correct-False to wrong-True.  The predicate must require a FILE.
    """
    (store.get_tool_dir("kokoro") / "output").mkdir(parents=True, exist_ok=True)
    assert store.is_downloaded("kokoro") is False


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


# ── Where downloads LAND ────────────────────────────────────────────
# Found 2026-09-21: ToolWorker._get_output_dir honoured HEVOLVE_MODEL_DIR
# but ModelStorageManager pinned ~/.hevolve/models, so the env var moved a
# tool's output/ while its WEIGHTS still went to the home drive.  Every
# production caller builds RuntimeToolManager() with defaults, so on this
# box a 10 GB ACE-Step download had no way to avoid a C: with 23 GB free.


def test_default_base_dir_when_env_unset(monkeypatch):
    monkeypatch.delenv("HEVOLVE_MODEL_DIR", raising=False)
    assert get_base_dir() == DEFAULT_BASE_DIR
    assert ModelStorageManager().base_dir == DEFAULT_BASE_DIR


def test_blank_env_keeps_default(monkeypatch):
    """A blank value must not resolve to Path('') / cwd."""
    monkeypatch.setenv("HEVOLVE_MODEL_DIR", "   ")
    assert get_base_dir() == DEFAULT_BASE_DIR


def test_env_var_relocates_storage_root(monkeypatch, tmp_path):
    """The whole point: weights follow HEVOLVE_MODEL_DIR."""
    monkeypatch.setenv("HEVOLVE_MODEL_DIR", str(tmp_path / "otherdrive"))
    assert get_base_dir() == tmp_path / "otherdrive"

    store = ModelStorageManager()
    assert store.base_dir == tmp_path / "otherdrive"
    assert store.get_tool_dir("acestep") == tmp_path / "otherdrive" / "acestep"
    # ...and the manifest travels with the weights, not left on C:.
    assert store.manifest_file.parent == tmp_path / "otherdrive"


def test_explicit_base_dir_still_wins_over_env(monkeypatch, tmp_path):
    """Callers that pass base_dir (the tests above, onboarding) are unaffected."""
    monkeypatch.setenv("HEVOLVE_MODEL_DIR", str(tmp_path / "ignored"))
    store = ModelStorageManager(base_dir=tmp_path / "explicit")
    assert store.base_dir == tmp_path / "explicit"


def test_gpu_worker_output_dir_follows_same_resolver(monkeypatch, tmp_path):
    """gpu_worker must not keep a second reading of the variable.

    Weights and a worker's output/ landing on different drives is the
    exact split this consolidation removes.
    """
    from integrations.service_tools.gpu_worker import ToolWorker

    monkeypatch.setenv("HEVOLVE_MODEL_DIR", str(tmp_path / "drive"))
    worker = ToolWorker.__new__(ToolWorker)
    worker.output_subdir = "acestep/output"

    assert worker._get_output_dir() == tmp_path / "drive" / "acestep" / "output"
    assert worker._get_output_dir().is_dir()
