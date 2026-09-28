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
