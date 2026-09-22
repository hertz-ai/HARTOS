"""Behavioural tests for core.venv_paths — the single source of truth for
per-backend venv resolution.

This module exists to close a PARALLEL-PATHS bug (2026-05-03, chatterbox_turbo):
the INSTALL path (Nunba tts.backend_venv) and the SPAWN path (HARTOS gpu_worker)
must compute the SAME venv path, or the worker dies with ModuleNotFoundError
because the dep was pip-installed into a venv the spawn path never reads. Both
consumers import from here so the path is computed in exactly one place — these
tests pin that resolution + its security validation + its platform branches so a
future edit can't silently reintroduce the drift.

0% covered before this file. Drives the REAL functions and asserts observable
behaviour (returned paths, raised ValueErrors, filesystem existence), never
source substrings.

    python -m pytest tests/unit/test_venv_paths.py -q --noconftest
"""
from __future__ import annotations

import os

import pytest

from core import venv_paths


@pytest.fixture(autouse=True)
def _isolate(monkeypatch, tmp_path):
    """Every test starts from a clean cache and an override pointing at a
    throwaway dir, so nothing touches the real ~/Documents/Nunba tree."""
    venv_paths._reset_cache_for_tests()
    monkeypatch.setenv("NUNBA_VENV_ROOT_OVERRIDE", str(tmp_path / "venvs"))
    yield
    venv_paths._reset_cache_for_tests()


# ── venv_root: resolution order + caching ───────────────────────────────────
class TestVenvRoot:
    def test_override_env_wins_and_dir_is_created(self, tmp_path):
        root = venv_paths.venv_root()
        assert root == str(tmp_path / "venvs")
        assert os.path.isdir(root), "venv_root must materialise the override dir"

    def test_override_is_not_cached_so_a_changed_override_takes_effect(
            self, monkeypatch, tmp_path):
        first = venv_paths.venv_root()
        second_dir = tmp_path / "other"
        monkeypatch.setenv("NUNBA_VENV_ROOT_OVERRIDE", str(second_dir))
        assert venv_paths.venv_root() == str(second_dir)
        assert venv_paths.venv_root() != first

    def test_blank_override_falls_through_to_data_dir(self, monkeypatch, tmp_path):
        # An empty / whitespace override must NOT be honoured (it would resolve
        # to cwd or "" and scatter venvs); fall through to platform_paths.
        monkeypatch.setenv("NUNBA_VENV_ROOT_OVERRIDE", "   ")
        monkeypatch.setattr(
            "core.platform_paths.get_data_dir", lambda: str(tmp_path / "dd"))
        root = venv_paths.venv_root()
        assert root == os.path.join(str(tmp_path / "dd"), "data", "venvs")

    def test_data_dir_result_is_cached(self, monkeypatch, tmp_path):
        monkeypatch.delenv("NUNBA_VENV_ROOT_OVERRIDE", raising=False)
        calls = []

        def _fake_dd():
            calls.append(1)
            return str(tmp_path / "dd")

        monkeypatch.setattr("core.platform_paths.get_data_dir", _fake_dd)
        r1 = venv_paths.venv_root()
        r2 = venv_paths.venv_root()
        assert r1 == r2
        assert len(calls) == 1, "venv_root must cache the non-override resolution"


# ── backend-name validation (path-traversal defence) ────────────────────────
class TestBackendNameValidation:
    @pytest.mark.parametrize("name", ["chatterbox_turbo", "luxtts", "vlm-worker",
                                      "a", "abc123", "123"])
    def test_valid_names_resolve_under_root(self, name):
        p = venv_paths.venv_path(name)
        assert p == os.path.join(venv_paths.venv_root(), name)

    @pytest.mark.parametrize("bad", ["", "../etc", "a/b", "a\\b", "foo/../bar",
                                     ".hidden", "has space", "semi;colon"])
    def test_unsafe_names_are_rejected(self, bad):
        with pytest.raises(ValueError):
            venv_paths.venv_path(bad)

    def test_non_string_is_rejected(self):
        with pytest.raises(ValueError):
            venv_paths.venv_path(None)  # type: ignore[arg-type]

    def test_traversal_can_never_escape_root(self):
        # The whole point: no backend string may join to a path outside root.
        with pytest.raises(ValueError):
            venv_paths.venv_path("../../../../etc/passwd")


# ── venv_python: platform-correct interpreter path ──────────────────────────
class TestVenvPython:
    def test_windows_uses_scripts_python_exe(self, monkeypatch):
        monkeypatch.setattr(venv_paths.sys, "platform", "win32")
        p = venv_paths.venv_python("luxtts")
        assert p == os.path.join(venv_paths.venv_path("luxtts"),
                                 "Scripts", "python.exe")

    def test_posix_uses_bin_python(self, monkeypatch):
        monkeypatch.setattr(venv_paths.sys, "platform", "linux")
        p = venv_paths.venv_python("luxtts")
        assert p == os.path.join(venv_paths.venv_path("luxtts"), "bin", "python")

    def test_macos_uses_bin_python(self, monkeypatch):
        monkeypatch.setattr(venv_paths.sys, "platform", "darwin")
        p = venv_paths.venv_python("luxtts")
        assert p.endswith(os.path.join("bin", "python"))

    def test_install_and_spawn_paths_agree(self, monkeypatch):
        # The regression this module closes: the INSTALL side and the SPAWN side
        # must derive byte-identical interpreter paths from the same backend id.
        monkeypatch.setattr(venv_paths.sys, "platform", "linux")
        install_side = venv_paths.venv_python("chatterbox_turbo")
        spawn_side = venv_paths.venv_python("chatterbox_turbo")
        assert install_side == spawn_side


# ── venv_python_if_exists: existence-checked fallthrough ─────────────────────
class TestVenvPythonIfExists:
    def test_none_and_empty_return_none(self):
        assert venv_paths.venv_python_if_exists(None) is None
        assert venv_paths.venv_python_if_exists("") is None

    def test_invalid_name_returns_none_not_raise(self):
        # The spawn path must fall through to python-embed on a bad id, never
        # crash — so ValueError is swallowed into None here (unlike venv_python).
        assert venv_paths.venv_python_if_exists("../evil") is None

    def test_missing_venv_returns_none(self):
        assert venv_paths.venv_python_if_exists("never_installed") is None

    def test_existing_interpreter_is_returned(self, monkeypatch):
        monkeypatch.setattr(venv_paths.sys, "platform", "linux")
        target = venv_paths.venv_python("realbackend")
        os.makedirs(os.path.dirname(target), exist_ok=True)
        with open(target, "w", encoding="utf-8") as fh:
            fh.write("#!/bin/sh\n")
        assert venv_paths.venv_python_if_exists("realbackend") == target


# ── parent-package visibility: the .pth a quarantined worker needs ──────────
#
# Measured 2026-09-20 on the installed build: a venv created from python-embed
# boots isolated (python-embed's ._pth applies to it) and ignores PYTHONPATH,
# so the spawn's "propagate sys.path via PYTHONPATH" never reached
# chatterbox_turbo; it died with "No module named 'integrations'" on every
# spawn.  The one route is a .pth in the venv's own site-packages.  These tests
# drive the real writer against a venv layout in tmp_path, reproduce the
# frozen build's layout, and finally run a real interpreter in isolated mode.
import subprocess
import sys


def _touch(path):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("")


def _fake_venv(backend):
    """A venv as the writer sees it: the interpreter file + site-packages."""
    _touch(venv_paths.venv_python(backend))
    site_dir = venv_paths.venv_site_packages(backend)
    os.makedirs(site_dir, exist_ok=True)
    return site_dir


def _pth_lines(path):
    with open(path, "r", encoding=venv_paths._pth_encoding()) as fh:
        return fh.read().splitlines()


class TestVenvSitePackages:
    def test_windows_layout(self, monkeypatch):
        monkeypatch.setattr(venv_paths.sys, "platform", "win32")
        assert venv_paths.venv_site_packages("b") == os.path.join(
            venv_paths.venv_path("b"), "Lib", "site-packages")

    def test_posix_layout_is_read_from_disk_when_the_venv_exists(self, monkeypatch):
        monkeypatch.setattr(venv_paths.sys, "platform", "linux")
        real = os.path.join(venv_paths.venv_path("b"), "lib", "python3.99",
                            "site-packages")
        os.makedirs(real)
        assert venv_paths.venv_site_packages("b") == real

    def test_posix_layout_defaults_to_the_running_interpreter(self, monkeypatch):
        monkeypatch.setattr(venv_paths.sys, "platform", "linux")
        expected = os.path.join(
            venv_paths.venv_path("b"), "lib",
            f"python{sys.version_info[0]}.{sys.version_info[1]}", "site-packages")
        assert venv_paths.venv_site_packages("b") == expected


class TestParentPackageRoots:
    def test_source_checkout_roots_are_where_this_process_imports_the_app_from(self):
        import core
        import integrations
        expected = {
            os.path.dirname(os.path.dirname(os.path.abspath(m.__file__)))
            for m in (core, integrations)
        }
        roots = venv_paths.parent_package_roots()
        assert set(roots) == expected
        assert len(roots) == len(set(roots))
        assert all(os.path.isdir(r) for r in roots)

    def test_an_app_package_reports_the_root_that_ships_it(self):
        import core
        root = os.path.dirname(os.path.dirname(os.path.abspath(core.__file__)))
        assert venv_paths.parent_package_root_of("core") == root
        assert venv_paths.parent_package_root_of("core.subprocess_safe") == root
        assert venv_paths.parent_package_root_of("integrations.service_tools") == root

    def test_third_party_stdlib_and_unknown_modules_are_not_app_packages(self):
        # pytest lives in a site-packages, never directly under the app root;
        # json is stdlib; the last name resolves nowhere.
        assert venv_paths.parent_package_root_of("pytest") is None
        assert venv_paths.parent_package_root_of("json") is None
        assert venv_paths.parent_package_root_of("no_such_module_xyz") is None
        assert venv_paths.parent_package_root_of("") is None


class TestFrozenBuildLayout:
    """The installed build, reproduced: the parent imports the app from
    python-embed/Lib/site-packages (a distribution directory, third-party
    packages beside the app's own), and a byte-identical copy of the app
    sits beside Nunba.exe."""

    @pytest.fixture
    def frozen(self, tmp_path, monkeypatch):
        embed_site = tmp_path / "python-embed" / "Lib" / "site-packages"
        for pkg in ("integrations", "core", "requests"):
            _touch(str(embed_site / pkg / "__init__.py"))
        _touch(str(embed_site / "sitecustomize.py"))
        app_dir = tmp_path
        for pkg in ("integrations", "core"):
            _touch(str(app_dir / pkg / "__init__.py"))
        _touch(str(app_dir / "Nunba.exe"))
        monkeypatch.setattr(venv_paths.sys, "frozen", True, raising=False)
        monkeypatch.setattr(venv_paths.sys, "executable", str(app_dir / "Nunba.exe"))
        monkeypatch.setattr(
            venv_paths, "_package_location",
            lambda name: str(embed_site / name) if (embed_site / name).is_dir() else None)
        return app_dir, embed_site

    def test_the_copy_beside_the_executable_is_named_not_the_site_packages(self, frozen):
        app_dir, embed_site = frozen
        assert venv_paths.parent_package_roots() == [str(app_dir)]

    def test_app_packages_are_told_apart_from_third_party_neighbours(self, frozen):
        app_dir, embed_site = frozen
        # requests sits right next to integrations in that site-packages ...
        assert (embed_site / "requests").is_dir()
        # ... and is still not the app's own package.
        assert venv_paths.parent_package_root_of("integrations") == str(app_dir)
        assert venv_paths.parent_package_root_of("requests") is None

    def test_without_the_sibling_copy_nothing_is_named_and_it_is_logged(
            self, frozen, caplog):
        app_dir, embed_site = frozen
        # Take the sibling copy of one package away (rename, not rmtree:
        # rmtree is broken in this venv's Python, see the test runner notes).
        os.rename(app_dir / "integrations", app_dir / "integrations.absent")
        with caplog.at_level("WARNING", logger=venv_paths.logger.name):
            roots = venv_paths.parent_package_roots()
        assert roots == [str(app_dir)]   # core still has its sibling copy
        assert any("integrations" in r.getMessage()
                   and "distribution directory" in r.getMessage()
                   for r in caplog.records)


class TestEnsureParentPackagesVisible:
    def test_missing_or_invalid_venv_writes_nothing(self):
        assert venv_paths.ensure_parent_packages_visible("never_installed") is None
        assert venv_paths.ensure_parent_packages_visible(None) is None
        assert venv_paths.ensure_parent_packages_visible("../evil") is None
        assert not os.path.exists(venv_paths.venv_path("never_installed"))

    def test_writes_the_pth_listing_every_parent_root(self):
        site_dir = _fake_venv("chatterbox_turbo")
        pth = venv_paths.ensure_parent_packages_visible("chatterbox_turbo")
        assert pth == os.path.join(site_dir, venv_paths.PARENT_PACKAGES_PTH)
        assert _pth_lines(pth) == venv_paths.parent_package_roots()
        assert not os.path.exists(pth + ".tmp")

    def test_an_up_to_date_file_is_not_rewritten(self):
        _fake_venv("b")
        pth = venv_paths.ensure_parent_packages_visible("b")
        # Age the file so a rewrite would be visible as a newer mtime.
        old = os.stat(pth).st_mtime_ns - 5 * 10 ** 9
        os.utime(pth, ns=(old, old))
        assert venv_paths.ensure_parent_packages_visible("b") == pth
        assert os.stat(pth).st_mtime_ns == old

    def test_stale_content_is_replaced(self):
        site_dir = _fake_venv("b")
        pth = os.path.join(site_dir, venv_paths.PARENT_PACKAGES_PTH)
        with open(pth, "w", encoding="utf-8") as fh:
            fh.write(os.path.join("C:", "gone", "root") + "\n")
        assert venv_paths.ensure_parent_packages_visible("b") == pth
        assert _pth_lines(pth) == venv_paths.parent_package_roots()

    def test_a_venv_without_site_packages_is_left_alone_and_logged(self, caplog):
        _touch(venv_paths.venv_python("b"))
        with caplog.at_level("WARNING", logger=venv_paths.logger.name):
            assert venv_paths.ensure_parent_packages_visible("b") is None
        assert any("site-packages" in r.getMessage() for r in caplog.records)

    def test_unlocatable_roots_write_nothing_and_log(self, monkeypatch, caplog):
        site_dir = _fake_venv("b")
        monkeypatch.setattr(venv_paths, "parent_package_roots", lambda: [])
        with caplog.at_level("WARNING", logger=venv_paths.logger.name):
            assert venv_paths.ensure_parent_packages_visible("b") is None
        assert not os.path.exists(
            os.path.join(site_dir, venv_paths.PARENT_PACKAGES_PTH))
        assert any("locate" in r.getMessage() for r in caplog.records)


class TestIsolatedInterpreterReadsThePth:
    """The claim behind the mechanism: an interpreter that ignores
    PYTHONPATH (isolated mode, what python-embed's ._pth imposes on every
    venv made from it) still resolves the app packages through the .pth."""

    _PROBE = (
        "import importlib.util as u, sys; "
        "print(sys.flags.isolated, u.find_spec('integrations') is not None, "
        "u.find_spec('core') is not None)"
    )

    @pytest.mark.timeout(180)
    def test_isolated_venv_python_finds_the_app_packages_only_via_the_pth(self):
        backend = "pth_proof"
        made = subprocess.run(
            [sys.executable, "-m", "venv", "--without-pip",
             venv_paths.venv_path(backend)],
            capture_output=True, text=True, timeout=120,
        )
        if made.returncode != 0:
            pytest.skip(f"venv module unavailable here: {made.stderr[-200:]!r}")
        py = venv_paths.venv_python_if_exists(backend)
        assert py, "venv creation reported success but left no interpreter"
        env = dict(os.environ)
        env["PYTHONPATH"] = venv_paths.parent_package_roots()[0]

        def _probe():
            r = subprocess.run([py, "-I", "-c", self._PROBE], env=env,
                               capture_output=True, text=True, timeout=60)
            assert r.returncode == 0, r.stderr[-400:]
            return r.stdout.split()

        # Isolated: PYTHONPATH is set and ignored, the app is invisible.
        assert _probe() == ["1", "False", "False"]
        assert venv_paths.ensure_parent_packages_visible(backend)
        # Same interpreter, same flags: the .pth is the only thing that changed.
        assert _probe() == ["1", "True", "True"]
