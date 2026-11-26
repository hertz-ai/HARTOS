"""A fresh install sets the talking-avatar model up by itself (the git row's git_prepare).

setup_tool clones the checkout, then runs servers/avatarforcing/setup_model.py on it
(pinned upstream commit, the wav2vec2 guard, its own venv with CUDA torch, the
checkpoints) before starting the sidecar.  Pinned here: the prepare step runs after
the clone, one at a time per tool, and a failure stops the start with a sanitised
reason; setup_model.py answers "ready" from files alone (it runs on every start of the
model), refuses a folder that is not a checkout or a drive without room, rebuilds a
venv whose base interpreter is gone, takes a checkpoint only at its known size, guards
the wav2vec2 line exactly once, and puts a checkout that moved back on the pinned
commit.
"""
import importlib.util
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

from integrations.service_tools import runtime_manager as rm

SETUP_DIR = Path(rm.SERVERS_DIR) / 'avatarforcing'
_spec = importlib.util.spec_from_file_location('avatarforcing_setup_model', SETUP_DIR / 'setup_model.py')
sm = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(sm)  # by path: nothing of the engine folder lands on sys.path

UPSTREAM_W2V = (
    "class Wav2VecModel(Wav2Vec2Model):\n"
    "    def forward(self, x):\n"
    "        self.config.output_attentions = True\n"
    "        return x\n"
    "    def feature_extract(self, x):\n"
    "        self.config.output_attentions = True\n"
    "        return x\n")


@pytest.fixture(autouse=True)
def small_checkpoints(monkeypatch, tmp_path):
    """The real sizes are 181 MB and 614 MB; the size check is what is tested.  torch's hub
    cache (the face detector's weights) is a folder of this test's own."""
    monkeypatch.setattr(sm, 'CHECKPOINTS', tuple(
        (name, drive_id, 1000 + i) for i, (name, drive_id, _) in enumerate(sm.CHECKPOINTS)))
    monkeypatch.setattr(sm, 'S3FD_SIZE', 77)
    monkeypatch.setenv('TORCH_HOME', str(tmp_path / 'torch'))
    hub = tmp_path / 'torch' / 'hub' / 'checkpoints'
    hub.mkdir(parents=True)
    (hub / sm.S3FD_FILE).write_bytes(b'x' * 77)
    return hub


def _checkout(tmp_path, *, venv=True, weights=True, patched=True, base_exists=True):
    d = tmp_path / 'af'
    (d / 'configs').mkdir(parents=True)
    (d / 'configs' / 'inference.yaml').write_text('a: 1')
    (d / 'models').mkdir()
    (d / 'models' / 'wav2vec2.py').write_text(UPSTREAM_W2V)
    if patched:
        sm.patch_wav2vec2(d)
    if venv:
        py = sm.venv_python(d)
        py.parent.mkdir(parents=True)
        py.write_text('')
        home = tmp_path / ('python-base' if base_exists else 'gone')
        if base_exists:
            home.mkdir()
        (d / '.venv' / 'pyvenv.cfg').write_text(f'home = {home}\nversion = 3.12.3\n')
    if weights:
        pre = d / 'pretrained_dir'
        (pre / 'wav2vec2-base-960h').mkdir(parents=True)
        for name, _, size in sm.CHECKPOINTS:
            (pre / name).write_bytes(b'\0' * size)
        for f in sm.WAV2VEC2_FILES:
            (pre / 'wav2vec2-base-960h' / f).write_text('x')
    return d


class TestSetupModel:
    def test_a_ready_folder_is_answered_from_files_alone(self, tmp_path, monkeypatch):
        d = _checkout(tmp_path)
        monkeypatch.setattr(sm.subprocess, 'run', lambda *a, **k: pytest.fail('ran a process'))
        assert sm.is_ready(d)
        assert sm.main([str(d)]) == 0

    @pytest.mark.parametrize('missing', ['venv', 'weights', 'patched', 'base_exists'])
    def test_anything_missing_is_not_ready(self, tmp_path, missing):
        assert not sm.is_ready(_checkout(tmp_path, **{missing: False}))

    def test_the_face_detector_belongs_to_ready(self, tmp_path, small_checkpoints):
        d = _checkout(tmp_path)
        (small_checkpoints / sm.S3FD_FILE).unlink()
        assert not sm.is_ready(d)  # the sidecar would otherwise download it at start, unchecked

    def test_a_checkpoint_of_the_wrong_size_is_not_ready(self, tmp_path):
        d = _checkout(tmp_path)
        name, _, size = sm.CHECKPOINTS[1]
        (d / 'pretrained_dir' / name).write_bytes(b'\0' * (size - 1))  # an interrupted download
        assert not sm.weights_ready(d)

    def test_a_folder_that_is_not_a_checkout_is_refused(self, tmp_path):
        assert sm.main([str(tmp_path)]) == 2

    def test_the_wav2vec2_guard_is_applied_once(self, tmp_path):
        d = _checkout(tmp_path, patched=False)
        assert not sm.w2v_patched(d)
        sm.patch_wav2vec2(d)
        once = (d / 'models' / 'wav2vec2.py').read_text()
        sm.patch_wav2vec2(d)
        assert (d / 'models' / 'wav2vec2.py').read_text() == once
        assert sm.w2v_patched(d)
        assert once.count(sm.W2V_GUARD) == 2
        compile(once, 'wav2vec2.py', 'exec')  # still Python

    def test_an_upstream_file_it_does_not_know_is_refused(self, tmp_path):
        d = _checkout(tmp_path, patched=False)
        (d / 'models' / 'wav2vec2.py').write_text('class Wav2VecModel:\n    pass\n')
        assert not sm.w2v_patched(d)  # the old line is gone, but so is the guard
        with pytest.raises(SystemExit, match='not the upstream file'):
            sm.patch_wav2vec2(d)

    def test_a_failed_download_keeps_nothing(self, tmp_path, monkeypatch):
        d = _checkout(tmp_path, weights=False)
        monkeypatch.setattr(sm, 'run', lambda cmd, timeout: (Path(cmd[-1]).write_bytes(b'x'), 0)[1])
        with pytest.raises(SystemExit, match='incomplete'):
            sm.fetch_weights(d)
        assert not list((d / 'pretrained_dir').glob('*.pth'))
        assert not list((d / 'pretrained_dir').glob('*.part'))

    def test_a_temp_drive_without_room_is_refused_too(self, tmp_path, monkeypatch, capsys):
        d = _checkout(tmp_path, venv=False, weights=False)
        import tempfile
        temp = tempfile.gettempdir()
        monkeypatch.setattr(sm.shutil, 'disk_usage', lambda p: SimpleNamespace(
            free=(1 if str(p) == str(temp) else 50) * 2**30))
        monkeypatch.setattr(sm, 'run', lambda *a, **k: pytest.fail('started a download'))
        assert sm.main([str(d)]) == 1
        assert "temp folder" in capsys.readouterr().out.strip().splitlines()[-1]

    def test_a_drive_without_room_is_refused_before_any_download(self, tmp_path, monkeypatch, capsys):
        d = _checkout(tmp_path, venv=False, weights=False)
        monkeypatch.setattr(sm.shutil, 'disk_usage', lambda p: SimpleNamespace(free=2 * 2**30))
        monkeypatch.setattr(sm, 'run', lambda *a, **k: pytest.fail('started a download'))
        assert sm.main([str(d)]) == 1
        assert 'GB free' in capsys.readouterr().out.strip().splitlines()[-1]

    def test_a_venv_whose_base_is_gone_is_rebuilt(self, tmp_path, monkeypatch):
        d = _checkout(tmp_path, base_exists=False)
        ran = []
        monkeypatch.setattr(sm, 'run', lambda cmd, timeout: ran.append([str(c) for c in cmd]) or 0)
        monkeypatch.setattr(sm, 'venv_ready', lambda m: bool(ran))
        sm.make_venv(d)
        assert ran[0][1:4] == ['-m', 'venv', '--clear']

    def test_a_moved_checkout_goes_back_to_the_pinned_commit(self, tmp_path, monkeypatch):
        d = _checkout(tmp_path)
        monkeypatch.setattr(sm, 'git_head', lambda m: 'f' * 40)
        assert not sm.is_ready(d)
        ran = []
        monkeypatch.setattr(sm, 'run', lambda cmd, timeout: ran.append([str(c) for c in cmd]) or 0)
        sm.pin_checkout(d)
        assert any(c[-1] == sm.PINNED_COMMIT and 'fetch' in c for c in ran)
        assert any(c[-1] == sm.PINNED_COMMIT and 'checkout' in c for c in ran)

    def test_an_unexpected_error_is_the_last_line(self, tmp_path, monkeypatch, capsys):
        d = _checkout(tmp_path, venv=False)
        monkeypatch.setattr(sm, 'make_venv', lambda m: (_ for _ in ()).throw(PermissionError('denied')))
        monkeypatch.setattr(sm.shutil, 'disk_usage', lambda p: SimpleNamespace(free=100 * 2**30))
        assert sm.main([str(d)]) == 1
        assert capsys.readouterr().out.strip().splitlines()[-1].endswith('FAILED: PermissionError: denied')


class TestGitPrepare:
    def test_the_row_names_the_shipped_script(self):
        row = rm.TOOL_CONFIGS['avatarforcing']
        assert row['download_type'] == 'git'
        assert Path(row['git_prepare']) == SETUP_DIR / 'setup_model.py'
        assert Path(row['git_prepare']).is_file()
        assert Path(row['server_script']).is_file()
        assert 'consent' not in row  # set up by itself for a video turn (owner, 2026-11-25)

    def _ran(self, monkeypatch, result):
        calls = []
        monkeypatch.setattr(rm, 'run_bounded', lambda cmd, timeout: calls.append(cmd) or result)
        return calls

    def test_ok(self, monkeypatch, tmp_path):
        calls = self._ran(monkeypatch, SimpleNamespace(returncode=0, stdout='ready\n', stderr='', timed_out=False))
        assert rm._git_prepare('avatarforcing', 'setup_model.py', tmp_path) is None
        assert calls[0][1:] == ['setup_model.py', str(tmp_path)]

    def test_a_failure_is_answered_with_its_reason_paths_cut(self, monkeypatch, tmp_path):
        self._ran(monkeypatch, SimpleNamespace(
            returncode=1, stdout='step\n[avatarforcing setup] FAILED: no file D:\\models\\af\\x.part\n',
            stderr='', timed_out=False))
        err = rm._git_prepare('avatarforcing', 'setup_model.py', tmp_path)
        assert 'FAILED: no file' in err and 'x.part' in err
        assert 'D:\\models' not in err

    def test_a_timeout_is_said(self, monkeypatch, tmp_path):
        self._ran(monkeypatch, SimpleNamespace(returncode=-1, stdout='', stderr='', timed_out=True))
        assert 'limit' in rm._git_prepare('avatarforcing', 'setup_model.py', tmp_path)

    def test_two_setups_prepare_one_at_a_time(self, monkeypatch, tmp_path):
        inside, most = [0], [0]
        gate = threading.Lock()

        def slow(cmd, timeout):
            with gate:
                inside[0] += 1
                most[0] = max(most[0], inside[0])
            threading.Event().wait(0.2)
            with gate:
                inside[0] -= 1
            return SimpleNamespace(returncode=0, stdout='ready', stderr='', timed_out=False)
        monkeypatch.setattr(rm, 'run_bounded', slow)
        ts = [threading.Thread(target=rm._git_prepare, args=('avatarforcing', 's.py', tmp_path)) for _ in range(2)]
        [t.start() for t in ts]
        [t.join() for t in ts]
        assert most[0] == 1

    def test_setup_tool_prepares_after_the_clone_and_stops_on_failure(self, monkeypatch, tmp_path):
        mgr = rm.RuntimeToolManager.__new__(rm.RuntimeToolManager)
        mgr.storage = SimpleNamespace(is_downloaded=lambda t: False,
                                      clone_repo=lambda t, url: tmp_path,
                                      get_tool_dir=lambda t: tmp_path)
        order = []
        monkeypatch.setattr(mgr, 'tool_enabled', lambda t: True, raising=False)
        monkeypatch.setattr(mgr, 'ask_tool_consent', lambda t: 'granted', raising=False)
        monkeypatch.setattr(rm, '_git_prepare', lambda t, s, p: order.append(('prepare', p)) or 'setting avatarforcing up failed: no GPU')
        monkeypatch.setattr(mgr, '_start_sidecar', lambda *a: order.append('start') or {}, raising=False)
        r = mgr.setup_tool('avatarforcing')
        assert r['error'] == 'setting avatarforcing up failed: no GPU'
        assert order == [('prepare', tmp_path)]  # never started
