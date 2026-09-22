"""download_hf_model must be able to REPAIR a download, not just skip one.

Found live 2026-09-21: ~/.hevolve/models/ltx2 carried a manifest row reading
28,405,153,793 bytes (written 2026-04-16) while the directory held
1,336,240,620 -- 4.7% -- the rest having been reclaimed by a disk-full sweep.
download_hf_model gated on is_downloaded(), which answers "are there files",
so it returned early every time and the missing 27 GB could never come back;
RuntimeToolManager.setup_tool('ltx2') then started a server against weights
that were not there.  An interrupted or pruned fetch was permanent.

The receipt also has to say WHAT it is a receipt for: self-caught the same
day, a probe fetch narrowed to 4 config files (2,194 bytes) wrote a row that
made the real 28 GB download a no-op.

These tests drive the real ModelStorageManager with snapshot_download mocked
at the boundary, and assert on whether the fetch was attempted.
"""
import json
from unittest.mock import patch

import pytest

from integrations.service_tools.model_storage import ModelStorageManager

PIPELINE_PATTERNS = ['transformer/*', 'vae/*']


@pytest.fixture
def store(tmp_path):
    return ModelStorageManager(base_dir=tmp_path)


def _write(store, tool, name='weights.bin', size=4096):
    d = store.get_tool_dir(tool)
    d.mkdir(parents=True, exist_ok=True)
    (d / name).write_bytes(b'x' * size)
    return d


def _fake_download(store, tool, size=8192):
    """A snapshot_download stand-in that actually lands bytes on disk."""
    def _inner(*a, **kw):
        _write(store, tool, 'fetched.bin', size)
        return str(store.get_tool_dir(tool))
    return _inner


def test_intact_download_is_not_refetched(store):
    """The fast path must survive: receipt + bytes present -> no network."""
    with patch('huggingface_hub.snapshot_download',
               side_effect=_fake_download(store, 'ltx2')) as dl:
        store.download_hf_model('ltx2', 'org/repo', allow_patterns=PIPELINE_PATTERNS)
        assert dl.call_count == 1

    with patch('huggingface_hub.snapshot_download') as dl2:
        store.download_hf_model('ltx2', 'org/repo', allow_patterns=PIPELINE_PATTERNS)
        assert dl2.call_count == 0, "an intact download must not re-fetch"


def test_pruned_download_is_refetched(store):
    """THE live defect: receipt says 28 GB, disk holds 4.7% -> must re-fetch."""
    _write(store, 'ltx2', 'leftover_lora.bin', 1024)
    store.mark_downloaded('ltx2', 'hf://org/repo', 28_405_153_793,
                          patterns=PIPELINE_PATTERNS)

    assert store.is_downloaded('ltx2') is True, (
        "precondition: the old gate saw files and said yes")
    assert store.hf_download_is_complete('ltx2', PIPELINE_PATTERNS) is False

    with patch('huggingface_hub.snapshot_download',
               side_effect=_fake_download(store, 'ltx2')) as dl:
        out = store.download_hf_model('ltx2', 'org/repo',
                                      allow_patterns=PIPELINE_PATTERNS)
    assert dl.call_count == 1, "a pruned download must be repaired, not skipped"
    assert out == store.get_tool_dir('ltx2')


def test_receipt_from_narrower_patterns_does_not_satisfy_a_wider_request(store):
    """A 4-config probe must not vouch for the whole pipeline."""
    with patch('huggingface_hub.snapshot_download',
               side_effect=_fake_download(store, 'ltx2', size=64)):
        store.download_hf_model('ltx2', 'org/repo',
                                allow_patterns=['model_index.json'])

    assert store.hf_download_is_complete('ltx2', ['model_index.json']) is True
    assert store.hf_download_is_complete('ltx2', PIPELINE_PATTERNS) is False

    with patch('huggingface_hub.snapshot_download',
               side_effect=_fake_download(store, 'ltx2')) as dl:
        store.download_hf_model('ltx2', 'org/repo', allow_patterns=PIPELINE_PATTERNS)
    assert dl.call_count == 1


def test_files_without_a_receipt_are_reverified(store):
    """Weights placed by some other path get one metadata call, not a skip."""
    _write(store, 'minicpm', size=999)
    assert store.is_downloaded('minicpm') is True

    with patch('huggingface_hub.snapshot_download',
               side_effect=_fake_download(store, 'minicpm')) as dl:
        store.download_hf_model('minicpm', 'org/minicpm')
    assert dl.call_count == 1


def test_offline_failure_keeps_an_existing_install(store):
    """A fetch that cannot reach the Hub must not condemn present weights.

    setup_tool no longer pre-checks is_downloaded for hf tools, so without
    this an offline boot would turn every already-present model into
    "Download failed for <tool>".
    """
    _write(store, 'whisper', size=4096)
    with patch('huggingface_hub.snapshot_download',
               side_effect=OSError('getaddrinfo failed')):
        out = store.download_hf_model('whisper', 'openai/whisper-base')
    assert out == store.get_tool_dir('whisper'), (
        "present weights must survive an offline fetch")


def test_offline_failure_with_nothing_on_disk_still_fails(store):
    with patch('huggingface_hub.snapshot_download',
               side_effect=OSError('getaddrinfo failed')):
        out = store.download_hf_model('acestep', 'org/acestep')
    assert out is None


def test_receipt_records_the_patterns_it_was_taken_under(store):
    with patch('huggingface_hub.snapshot_download',
               side_effect=_fake_download(store, 'ltx2')):
        store.download_hf_model('ltx2', 'org/repo', allow_patterns=PIPELINE_PATTERNS)

    row = json.loads(store.manifest_file.read_text())['tools']['ltx2']
    assert row['patterns'] == sorted(PIPELINE_PATTERNS)
    assert row['size_bytes'] == store.get_tool_size('ltx2')


def test_legacy_receipt_without_patterns_key_still_skips(store):
    """Rows written before patterns were recorded must not all re-fetch."""
    _write(store, 'tts', size=5000)
    store.mark_downloaded('tts', 'hf://org/tts', 5000)
    row_path = store.manifest_file
    data = json.loads(row_path.read_text())
    del data['tools']['tts']['patterns']          # simulate an old manifest
    row_path.write_text(json.dumps(data))

    assert store.hf_download_is_complete('tts', None) is True
    with patch('huggingface_hub.snapshot_download') as dl:
        store.download_hf_model('tts', 'org/tts')
    assert dl.call_count == 0
