"""A backend venv counts only when the interpreter this process creates venvs
with built it.

Measured 2026-09-25 on the installed build (gui_app.log.4 16:48:08-34): the
venvs for neutts_air, melotts and kokoro had been made by a source-mode run on
miniconda 3.11 (their pyvenv.cfg: home = C:\\Users\\sathi\\miniconda3,
version = 3.11.4).  The frozen 3.12 app adopted them because
``venv_python_if_exists`` only asked whether python.exe existed, spawned them
with its own 3.12 sys.path in PYTHONPATH, and each died before running a line
of Python: ``bad magic number in 'encodings'``.  Only Piper spoke.

These tests drive the real resolver against venv layouts in tmp_path and the
real gpu_worker spawn resolver; only ``sys.frozen`` / ``sys.executable`` /
``sys._base_executable`` are patched, to stand in for the installed build.

    python -m pytest tests/unit/test_venv_built_by_this_interpreter.py -q
"""
from __future__ import annotations

import os
import subprocess
import sys

import pytest

from core import venv_paths


@pytest.fixture(autouse=True)
def _isolate(monkeypatch, tmp_path):
    venv_paths._reset_cache_for_tests()
    monkeypatch.setenv("NUNBA_VENV_ROOT_OVERRIDE", str(tmp_path / "venvs"))
    yield
    venv_paths._reset_cache_for_tests()


def _this_home():
    base = getattr(sys, "_base_executable", None) or sys.executable
    return os.path.dirname(os.path.abspath(base))


def _this_version():
    return "%d.%d.%d" % sys.version_info[:3]


def _make_venv(backend, home, version):
    """A venv as CPython leaves it: the interpreter, site-packages, and the
    pyvenv.cfg that records who built it (home=None writes no cfg)."""
    py = venv_paths.venv_python(backend)
    os.makedirs(os.path.dirname(py), exist_ok=True)
    with open(py, "w", encoding="utf-8") as fh:
        fh.write("")
    os.makedirs(venv_paths.venv_site_packages(backend), exist_ok=True)
    if home is not None:
        cfg = os.path.join(venv_paths.venv_path(backend), "pyvenv.cfg")
        with open(cfg, "w", encoding="utf-8") as fh:
            fh.write(f"home = {home}\n"
                     "include-system-site-packages = false\n"
                     f"version = {version}\n")
    return py


class TestSourceMode:
    def test_a_venv_this_interpreter_built_is_used(self):
        py = _make_venv("kokoro", _this_home(), _this_version())
        assert venv_paths.venv_python_if_exists("kokoro") == py

    def test_a_venv_built_by_another_interpreter_is_not_used(self, tmp_path,
                                                             caplog):
        other_home = str(tmp_path / "miniconda3")
        os.makedirs(other_home)
        _make_venv("kokoro", other_home, _this_version())
        with caplog.at_level("WARNING", logger=venv_paths.logger.name):
            assert venv_paths.venv_python_if_exists("kokoro") is None
        said = " ".join(r.getMessage() for r in caplog.records)
        assert "kokoro" in said and other_home in said

    def test_same_home_but_another_python_version_is_not_used(self):
        other = "%d.%d.0" % (sys.version_info[0], sys.version_info[1] - 1)
        _make_venv("kokoro", _this_home(), other)
        assert venv_paths.venv_python_if_exists("kokoro") is None

    def test_a_micro_release_difference_still_matches(self):
        # The stdlib bytecode magic changes per minor release, not per micro.
        same_minor = "%d.%d.99" % sys.version_info[:2]
        py = _make_venv("kokoro", _this_home(), same_minor)
        assert venv_paths.venv_python_if_exists("kokoro") == py

    def test_a_venv_without_pyvenv_cfg_is_not_used(self):
        _make_venv("kokoro", None, None)
        assert venv_paths.venv_python_if_exists("kokoro") is None

    def test_the_reason_names_both_interpreters(self, tmp_path):
        other_home = str(tmp_path / "miniconda3")
        _make_venv("kokoro", other_home, "3.11.4")
        reason = venv_paths.venv_mismatch("kokoro")
        assert reason and other_home in reason and _this_home() in reason
        assert venv_paths.venv_mismatch("never_installed") is None

    def test_a_matching_venv_has_no_mismatch(self):
        _make_venv("kokoro", _this_home(), _this_version())
        assert venv_paths.venv_mismatch("kokoro") is None


@pytest.fixture
def frozen_app(tmp_path, monkeypatch):
    """The installed build: Nunba.exe with python-embed beside it."""
    app = tmp_path / "Nunba"
    embed = app / "python-embed"
    embed.mkdir(parents=True)
    (app / "Nunba.exe").write_text("", encoding="utf-8")
    (embed / "python.exe").write_text("", encoding="utf-8")
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(app / "Nunba.exe"))
    monkeypatch.setattr(sys, "platform", "win32")
    return app, embed


class TestFrozenBuild:
    def test_the_creator_is_python_embed(self, frozen_app):
        app, embed = frozen_app
        assert venv_paths.venv_creator_python() == str(embed / "python.exe")

    def test_a_python_embed_venv_is_used(self, frozen_app):
        app, embed = frozen_app
        py = _make_venv("chatterbox_turbo", str(embed), _this_version())
        assert venv_paths.venv_python_if_exists("chatterbox_turbo") == py

    def test_the_measured_miniconda_venv_is_not_used(self, frozen_app,
                                                     tmp_path):
        _make_venv("neutts_air", str(tmp_path / "miniconda3"), "3.11.4")
        assert venv_paths.venv_python_if_exists("neutts_air") is None

    def test_a_dev_venv_of_the_same_version_is_not_used(self, frozen_app,
                                                        tmp_path):
        # xtts_v2 on this box: made by the HARTOS dev venv, home C:\\Python312,
        # 3.12.3.  Same minor as the app, but not isolated: spawned with the
        # app's PYTHONPATH it would read python-embed's packages ahead of its
        # own pins, which is the cage the venv exists for.
        _make_venv("xtts_v2", str(tmp_path / "Python312"), _this_version())
        assert venv_paths.venv_python_if_exists("xtts_v2") is None

    def test_a_symlinked_app_binary_still_finds_python_embed(
            self, frozen_app, tmp_path, monkeypatch):
        # Review of 02931fdd4: backend_venv resolved sys.executable with
        # Path.resolve(); venv_creator_python used abspath, so an app binary
        # reached through a symlink looked for python-embed beside the LINK.
        app, embed = frozen_app
        elsewhere = tmp_path / "launcher"
        elsewhere.mkdir()
        link = elsewhere / "Nunba.exe"
        try:
            os.symlink(app / "Nunba.exe", link)
        except (OSError, NotImplementedError) as exc:
            pytest.skip(f"no symlinks here: {exc}")
        monkeypatch.setattr(sys, "executable", str(link))
        want = os.path.realpath(embed / "python.exe")
        assert venv_paths.venv_creator_python() == want
        assert venv_paths.python_embed_dir() == os.path.realpath(embed)

    @pytest.mark.parametrize("posix_name", ["python3", "python"])
    def test_a_posix_python_embed_is_found_in_bin(self, tmp_path, monkeypatch,
                                                  posix_name):
        # The macOS / Linux bundle lays python-embed out as bin/python3 (or
        # bin/python), not python.exe.
        app = tmp_path / "Nunba.app"
        interp = app / "python-embed" / "bin" / posix_name
        interp.parent.mkdir(parents=True)
        interp.write_text("", encoding="utf-8")
        (app / "Nunba").write_text("", encoding="utf-8")
        monkeypatch.setattr(sys, "frozen", True, raising=False)
        monkeypatch.setattr(sys, "executable", str(app / "Nunba"))
        assert venv_paths.venv_creator_python() == os.path.realpath(interp)

    def test_without_python_embed_there_is_no_creator_and_no_venv(
            self, frozen_app):
        app, embed = frozen_app
        os.remove(embed / "python.exe")
        assert venv_paths.venv_creator_python() is None
        _make_venv("kokoro", str(embed), _this_version())
        assert venv_paths.venv_python_if_exists("kokoro") is None


class TestSpawnPath:
    def test_a_foreign_venv_is_never_spawned_nor_written(self, tmp_path):
        from integrations.service_tools import gpu_worker
        _make_venv("melotts", str(tmp_path / "miniconda3"), "3.11.4")
        assert gpu_worker._resolve_backend_venv_python("melotts") is None
        pth = os.path.join(venv_paths.venv_site_packages("melotts"),
                           venv_paths.PARENT_PACKAGES_PTH)
        assert not os.path.exists(pth)

    def test_an_own_venv_is_spawned_and_told_the_app_packages(self):
        from integrations.service_tools import gpu_worker
        py = _make_venv("melotts", _this_home(), _this_version())
        assert gpu_worker._resolve_backend_venv_python("melotts") == py
        assert os.path.isfile(os.path.join(
            venv_paths.venv_site_packages("melotts"),
            venv_paths.PARENT_PACKAGES_PTH))

    def test_the_worker_default_is_the_venv_creator(self, frozen_app,
                                                    monkeypatch):
        from integrations.service_tools import gpu_worker
        app, embed = frozen_app
        monkeypatch.delenv("HARTOS_WORKER_PYTHON", raising=False)
        assert gpu_worker._resolve_python_exe() == str(embed / "python.exe")
        os.remove(embed / "python.exe")
        assert gpu_worker._resolve_python_exe() == sys.executable


class TestOneAnswerForTheWorkerInterpreter:
    """Review of 02931fdd4: hevolveai_supervisor and diarization_service each
    joined dirname(sys.executable)/python-embed/python.exe themselves.  Every
    worker spawn now asks venv_creator_python, the answer the venv check uses."""

    def _supervisor(self):
        from integrations.agent_engine import hevolveai_supervisor as sup
        return sup._resolve_python_exe()

    def _diarization(self, monkeypatch):
        from integrations.audio import diarization_service as ds
        seen = []

        class _Proc:
            stdout = None

        monkeypatch.setattr(ds.subprocess, "Popen",
                            lambda cmd, **kw: seen.append(cmd) or _Proc())
        svc = ds.DiarizationService.__new__(ds.DiarizationService)
        svc._port = 1
        svc._process = None
        svc._start_subprocess()
        return seen[0][0]

    def test_frozen_workers_run_python_embed(self, frozen_app, monkeypatch):
        app, embed = frozen_app
        want = venv_paths.venv_creator_python()
        assert want == os.path.realpath(embed / "python.exe")
        assert self._supervisor() == want
        assert self._diarization(monkeypatch) == want

    def test_a_symlinked_frozen_app_is_one_answer_everywhere(
            self, frozen_app, tmp_path, monkeypatch):
        app, embed = frozen_app
        link_dir = tmp_path / "launcher"
        link_dir.mkdir()
        try:
            os.symlink(app / "Nunba.exe", link_dir / "Nunba.exe")
        except (OSError, NotImplementedError) as exc:
            pytest.skip(f"no symlinks here: {exc}")
        monkeypatch.setattr(sys, "executable", str(link_dir / "Nunba.exe"))
        want = os.path.realpath(embed / "python.exe")
        assert self._supervisor() == want
        assert self._diarization(monkeypatch) == want

    def test_source_workers_run_this_interpreter(self, monkeypatch):
        monkeypatch.setattr(sys, "frozen", False, raising=False)
        assert self._supervisor() == sys.executable
        assert self._diarization(monkeypatch) == sys.executable


class TestPublicNames:
    """Nunba's tts.backend_venv imported _reset_cache_for_tests and
    _validate_backend_name; they are public now, the old names aliases."""

    def test_validate_backend_name(self):
        venv_paths.validate_backend_name("kokoro")
        for bad in ("", "../evil", ".hidden", "a b"):
            with pytest.raises(ValueError):
                venv_paths.validate_backend_name(bad)
        assert venv_paths._validate_backend_name is venv_paths.validate_backend_name

    def test_reset_venv_root_cache(self, monkeypatch, tmp_path):
        first = venv_paths.venv_root()
        monkeypatch.delenv("NUNBA_VENV_ROOT_OVERRIDE")
        monkeypatch.setattr("core.platform_paths.get_data_dir",
                            lambda: str(tmp_path / "a"))
        cached = venv_paths.venv_root()
        monkeypatch.setattr("core.platform_paths.get_data_dir",
                            lambda: str(tmp_path / "b"))
        assert venv_paths.venv_root() == cached
        venv_paths.reset_venv_root_cache()
        assert venv_paths.venv_root() == os.path.join(str(tmp_path / "b"), "data", "venvs")
        assert first != cached
        assert venv_paths._reset_cache_for_tests is venv_paths.reset_venv_root_cache


class TestRealInterpreter:
    @pytest.mark.timeout(180)
    def test_a_venv_made_by_this_interpreter_is_recognised(self):
        made = subprocess.run(
            [sys.executable, "-m", "venv", "--without-pip",
             venv_paths.venv_path("real_one")],
            capture_output=True, text=True, timeout=120,
        )
        if made.returncode != 0:
            pytest.skip(f"venv module unavailable here: {made.stderr[-200:]!r}")
        assert venv_paths.venv_mismatch("real_one") is None
        assert venv_paths.venv_python_if_exists("real_one") == \
            venv_paths.venv_python("real_one")
