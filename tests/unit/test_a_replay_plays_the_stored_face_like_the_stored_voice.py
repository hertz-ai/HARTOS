"""A reply's face is kept like its voice, read by the one reader and capped by the one pruner
(owner 2026-11-26: "we have a play button ... if we dont store the video url and save the recent video we
would recompute em"; "just like audio we could store the generated video after live stream").
Review hartos-17 of the stored-face design: one HARTOS module, path safety, Windows-safe eviction,
oldest-first, one cap over voice and face, a share of free disk.
"""
import os
import time
from pathlib import Path

import pytest

from core import spoken_media as sm


@pytest.fixture
def home(monkeypatch, tmp_path):
    """A home whose spoken-media folders are a TTS engine's output, avatarforcing's output and piper."""
    monkeypatch.setattr(Path, 'home', classmethod(lambda cls: tmp_path))
    # model_storage resolves the models root from HEVOLVE_MODEL_DIR (its default was fixed at import,
    # from the real home), so the fake home's models root is set the way an operator would set it.
    monkeypatch.setenv('HEVOLVE_MODEL_DIR', str(tmp_path / '.hevolve' / 'models'))
    for d in ('.hevolve/models/kokoro/output', '.hevolve/models/avatarforcing/output', '.nunba/piper/cache'):
        (tmp_path / d).mkdir(parents=True)
    return tmp_path


def _file(path: Path, size: int, age_s: float):
    path.write_bytes(b'x' * size)
    t = time.time() - age_s
    os.utime(path, (t, t))
    return path


# ── the reader ───────────────────────────────────────────────────────────────────────────────

def test_a_stored_face_and_a_stored_voice_are_found_with_their_types(home):
    face = _file(home / '.hevolve/models/avatarforcing/output/abcdef012345.mp4', 10, 0)
    voice = _file(home / '.nunba/piper/cache/tts_1.wav', 10, 0)
    assert sm.find_spoken_media('abcdef012345.mp4', kind='video') == face
    assert sm.find_spoken_media('tts_1.wav', kind='audio') == voice
    assert sm.media_type(face) == 'video/mp4' and sm.media_type(voice) == 'audio/wav'


@pytest.mark.parametrize('name', [
    '../secret.mp4', '..\\secret.mp4', 'a/b.mp4', 'a\\b.mp4', '.hidden.mp4', 'abcdef012345.mp4.part',
    'notes.txt', 'x.mp4.exe', '', None, 42, 'C:\\x.mp4', '/etc/passwd',
])
def test_a_bad_name_is_never_served(home, name):
    _file(home / '.hevolve/models/avatarforcing/output/abcdef012345.mp4.part', 10, 0)
    assert sm.find_spoken_media(name, kind='video') is None
    assert sm.find_spoken_media(name, kind='audio') is None


def test_a_face_is_never_found_as_a_voice_nor_a_voice_as_a_face(home):
    """The voice route has no owner check (a voice is served by file name alone), so asked for audio
    the reader never returns a stored face: another user's face is served only by the owner-checked
    /avatar/live route."""
    _file(home / '.hevolve/models/avatarforcing/output/abcdef012345.mp4', 10, 0)
    _file(home / '.nunba/piper/cache/tts_1.wav', 10, 0)
    assert sm.find_spoken_media('abcdef012345.mp4', kind='audio') is None
    assert sm.find_spoken_media('tts_1.wav', kind='video') is None
    with pytest.raises(ValueError):
        sm.find_spoken_media('tts_1.wav', kind='any')


def test_a_voice_named_with_a_leading_underscore_is_still_served(home):
    """tts_engine's multi-voice output is _tts_multi_<x>.wav; the voice route served it before this
    module, so the one reader must too."""
    (home / '.nunba/tts_cache').mkdir(parents=True)
    _file(home / '.nunba/tts_cache/_tts_multi_ab12.wav', 10, 0)
    assert sm.find_spoken_media('_tts_multi_ab12.wav', kind='audio') == home / '.nunba/tts_cache/_tts_multi_ab12.wav'


def test_a_models_root_moved_by_the_operator_is_where_media_is_found(home, monkeypatch, tmp_path_factory):
    """HEVOLVE_MODEL_DIR moves the engines' output and the TTS memo (model_storage.get_base_dir), so
    the one reader follows it; the default home folder is no longer looked in for model output."""
    moved = tmp_path_factory.mktemp('fdrive') / 'models'
    (moved / 'tts_memo' / 'output').mkdir(parents=True)
    voice = _file(moved / 'tts_memo' / 'output' / 'abcdef0123456789.wav', 10, 0)
    monkeypatch.setenv('HEVOLVE_MODEL_DIR', str(moved))
    assert sm.find_spoken_media('abcdef0123456789.wav', kind='audio') == voice
    assert (moved / 'tts_memo' / 'output') in sm.spoken_media_dirs()
    assert (home / '.hevolve/models/kokoro/output') not in sm.spoken_media_dirs()


def test_the_folder_list_is_globbed_once_per_ttl_and_a_new_root_at_once(home, monkeypatch, tmp_path_factory):
    """The voice route cached its folder list for 30 s (a glob per file was ~2 ms); the one list keeps
    that, keyed by the roots, so a moved models root is not served from the old list."""
    globs = []
    real_glob = Path.glob
    monkeypatch.setattr(Path, 'glob', lambda self, pat: globs.append(pat) or real_glob(self, pat))
    sm.spoken_media_dirs(); sm.spoken_media_dirs(); sm.spoken_media_dirs()
    assert globs == ['*/output']
    moved = tmp_path_factory.mktemp('gdrive')
    monkeypatch.setenv('HEVOLVE_MODEL_DIR', str(moved))
    sm.spoken_media_dirs()
    assert globs == ['*/output', '*/output']
    monkeypatch.setattr(sm, '_dirs_memo', dict(sm._dirs_memo, at=sm._dirs_memo['at'] - sm.DIRS_TTL_S - 1))
    sm.spoken_media_dirs()
    assert len(globs) == 3


def test_a_face_is_never_served_from_the_system_temp(home, monkeypatch, tmp_path_factory):
    tmp = tmp_path_factory.mktemp('systemp')
    _file(tmp / 'abcdef012345.mp4', 10, 0)
    monkeypatch.setenv('TEMP', str(tmp))
    monkeypatch.setenv('TMP', str(tmp))
    assert sm.find_spoken_media('abcdef012345.mp4', kind='video') is None
    assert all('temp' not in str(d).lower() for d in sm.spoken_media_dirs())


# ── the pruner ───────────────────────────────────────────────────────────────────────────────

def test_the_oldest_go_first_voice_and_face_alike_down_to_the_share_of_free_disk(home):
    old_face = _file(home / '.hevolve/models/avatarforcing/output/aaaaaaaaaaaa.mp4', 400, 300)
    old_voice = _file(home / '.nunba/piper/cache/tts_old.wav', 300, 200)
    new_voice = _file(home / '.hevolve/models/kokoro/output/tts_new.wav', 200, 100)
    new_face = _file(home / '.hevolve/models/avatarforcing/output/bbbbbbbbbbbb.mp4', 100, 0)
    # held 1000 B, free 9000 B: 5 % of (9000 + 1000) = 500 B may stay
    got = sm.prune_spoken_media(pct_of_free=5, free_bytes=lambda p: 9000)
    assert not old_face.exists() and not old_voice.exists()
    assert new_voice.exists() and new_face.exists()
    assert got['files'] == 2 and got['bytes'] == 700


def test_under_the_cap_nothing_is_removed(home):
    f = _file(home / '.nunba/piper/cache/tts_a.wav', 100, 1000)
    assert sm.prune_spoken_media(pct_of_free=5, free_bytes=lambda p: 10 ** 9)['files'] == 0
    assert f.exists()


def test_an_open_file_is_kept_logged_once_and_retried(home, monkeypatch, caplog):
    busy = _file(home / '.nunba/piper/cache/tts_busy.wav', 600, 500)
    other = _file(home / '.nunba/piper/cache/tts_other.wav', 600, 400)
    real_unlink = Path.unlink

    def unlink(self, *a, **k):
        if self.name == 'tts_busy.wav':
            raise PermissionError('in use')
        return real_unlink(self, *a, **k)
    monkeypatch.setattr(Path, 'unlink', unlink)
    monkeypatch.setattr(sm, '_busy_logged', set())
    caplog.set_level('INFO', logger=sm.logger.name)
    for _ in range(2):
        got = sm.prune_spoken_media(pct_of_free=1, free_bytes=lambda p: 1000)
    assert busy.exists() and not other.exists()
    assert got['busy'] == 1
    assert sum('tts_busy.wav is open' in r.getMessage() for r in caplog.records) == 1


def test_a_face_still_being_written_is_left_and_a_stale_one_removed(home):
    out = home / '.hevolve/models/avatarforcing/output'
    fresh = _file(out / 'cccccccccccc.mp4.part', 50, 10)
    stale = _file(out / 'dddddddddddd.mp4.part', 50, sm.STALE_PART_S + 60)
    got = sm.prune_spoken_media(pct_of_free=100, free_bytes=lambda p: 10 ** 9)
    assert fresh.exists() and not stale.exists() and got['stale_parts'] == 1


def test_the_reader_and_the_pruner_walk_the_same_folders(home):
    names = {d.as_posix() for d in sm.spoken_media_dirs()}
    assert (home / '.hevolve/models/avatarforcing/output').as_posix() in names
    assert (home / '.nunba/piper/cache').as_posix() in names


# ── what may be pruned: a named subset of what is served (review hartos-17, P1 and C1-C5) ─────────

@pytest.fixture
def models(home, monkeypatch):
    """The fake home's models root as model_storage's singleton sees it too (its base is fixed at
    import), so every writer's own resolver answers inside the fake home."""
    import importlib
    # the module, not the package attribute (which is the singleton instance)
    ms = importlib.import_module('integrations.service_tools.model_storage')
    root = home / '.hevolve' / 'models'
    monkeypatch.setattr(ms, 'model_storage', ms.ModelStorageManager(base_dir=root))
    monkeypatch.setattr(sm, '_dirs_memo', {'key': None, 'at': 0.0, 'dirs': []})
    return root


def test_every_prunable_folder_is_a_served_one(models):
    """C1: nothing is pruned that the reader could not serve."""
    for d in sm.prunable_dirs():
        d.mkdir(parents=True, exist_ok=True)
    served = set(sm.spoken_media_dirs())
    assert set(sm.prunable_dirs()) <= served


def test_a_kept_composition_survives_a_prune_over_the_cap(models):
    """P1/C4: acestep/output is served but never pruned: media_agent._keep_composition keeps a
    composition there for good (a game's memo names it), whatever the cap."""
    from integrations.service_tools.media_agent import composer_output_dir
    kept_dir = composer_output_dir()
    kept_dir.mkdir(parents=True, exist_ok=True)
    composition = _file(kept_dir / 'song_for_the_memo.wav', 5000, 9000)
    voice = _file(models / 'kokoro' / 'output' / 'tts_old.wav', 5000, 8000)
    assert kept_dir in sm.spoken_media_dirs() and kept_dir not in sm.prunable_dirs()
    counts = sm.prune_spoken_media(pct_of_free=0.0001, free_bytes=lambda _p: 1000)
    assert composition.exists() and not voice.exists() and counts['files'] == 1


def test_building_the_prune_list_creates_no_folder(models):
    """C2: the list is built from data; no writer's folder helper (each makes its folder) is called."""
    models.mkdir(parents=True, exist_ok=True)
    before = sorted(p.relative_to(models.parent) for p in models.parent.rglob('*'))
    sm.prunable_dirs()
    assert sorted(p.relative_to(models.parent) for p in models.parent.rglob('*')) == before


def test_building_the_prune_list_imports_no_model_library():
    """C2: proven in a fresh interpreter, not stated: building the list (the engine registry and the
    tool modules it names) loads no model library.  Measured as what the CALL adds: importing the
    core package already loads torch on its own (measured 2026-11-27), before this module runs."""
    import subprocess
    import sys
    code = ("import sys; from core import spoken_media as sm; before = set(sys.modules); "
            "sm.prunable_dirs(); new = {m.split('.')[0] for m in set(sys.modules) - before}; "
            "print(sorted(new & {'torch', 'transformers', 'onnxruntime', 'librosa', 'soundfile', "
            "'scipy', 'numpy', 'cv2', 'sklearn'}))")
    out = subprocess.run([sys.executable, '-c', code], capture_output=True, text=True, timeout=300,
                         cwd=str(Path(sm.__file__).resolve().parents[1]))
    assert out.returncode == 0, out.stderr[-2000:]
    assert out.stdout.strip().splitlines()[-1] == '[]'


def test_each_named_folder_is_where_its_writer_puts_its_files(models, monkeypatch):
    """C3: luxtts, pocket_tts and the TTS memo have no ToolWorker, so their folders are named; each
    name must be what the writer's own resolver gives under a moved models root."""
    from integrations.channels.media import tts_router
    from integrations.service_tools import luxtts_tool, pocket_tts_tool
    prunable = set(sm.prunable_dirs())
    assert luxtts_tool._get_tts_dir() / 'output' in prunable
    assert pocket_tts_tool._get_tts_dir() / 'output' in prunable
    monkeypatch.setattr(tts_router, '_voiced_memo_dir', {})
    assert Path(tts_router._memo_dir()) in prunable
    assert models / 'avatarforcing' / 'output' in prunable


def test_every_engine_worker_folder_is_prunable(models):
    """The ToolWorker engines' folders come from the registry, read as data (output_subdir)."""
    from integrations.channels.media.tts_router import ENGINE_REGISTRY
    import importlib
    prunable = set(sm.prunable_dirs())
    for spec in ENGINE_REGISTRY.values():
        if spec.tool_module and spec.tool_worker_attr:
            worker = getattr(importlib.import_module(spec.tool_module), spec.tool_worker_attr, None)
            sub = getattr(worker, 'output_subdir', None)
            if isinstance(sub, str) and sub.endswith('/output'):
                assert models / sub in prunable, spec.engine_id


def test_an_idle_face_is_never_pruned(models):
    """An agent's idle face (avatarforcing_idle) sits in the face folder; losing it costs a 60 s GPU job,
    so the prune skips it even over the cap (review hartos-17 MUST before the idle module lands)."""
    out = models / 'avatarforcing' / 'output'
    out.mkdir(parents=True, exist_ok=True)
    idle_clip = _file(out / 'idle_7_123.mp4', 5000, 9000)
    face = _file(out / 'abcdef012345.mp4', 5000, 8000)
    sm.prune_spoken_media(pct_of_free=0.0001, free_bytes=lambda _p: 1000)
    assert idle_clip.exists() and not face.exists()


def test_the_cap_reads_the_disk_the_models_root_is_on(models):
    asked = []
    models.mkdir(parents=True, exist_ok=True)
    sm.prune_spoken_media(free_bytes=lambda p: asked.append(p) or 10 ** 12)
    assert asked == [str(models)]
