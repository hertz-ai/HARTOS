"""
core/venv_paths.py — single source of truth for per-backend venv paths.

Each TTS / VLM / STT backend that imposes a conflicting dep cage gets its
own venv under ``<data_dir>/venvs/<backend>/`` so its transitive deps
stay isolated from the bundled python-embed.  Two consumers depend on
this resolution:

  1. INSTALL path  — Nunba's ``tts.backend_venv`` creates the venv,
     pip-installs into it, runs verification.
  2. SPAWN path   — HARTOS's ``integrations.service_tools.gpu_worker``
     spawns the worker subprocess from the SAME venv's ``python.exe``.

If those two paths drift (e.g. install writes to ``A/venvs/`` but spawn
reads ``B/venvs/``), the worker fails at startup with ``ModuleNotFoundError``
because the dep was installed to a venv the spawn path never looks at.
That parallel-paths bug surfaced 2026-05-03 for chatterbox_turbo — install
went into the venv, spawn went into python-embed → ``died during startup
(exit=1)``.  Consolidating the resolution here is the close-out: both
``tts.backend_venv`` and ``gpu_worker`` import from this module so the
path is computed in exactly one place.

Public API
----------
    venv_root()                       -> str
    venv_path(backend)                -> str
    venv_python(backend)              -> str
    venv_python_if_exists(backend)    -> Optional[str]
"""
from __future__ import annotations

import os
import sys
from typing import Optional


_VENV_ROOT_CACHE: Optional[str] = None


def _reset_cache_for_tests() -> None:
    """Reset the cached venv root.  Test hook only — do not call from
    production code (the cache makes hot-path lookups O(1))."""
    global _VENV_ROOT_CACHE
    _VENV_ROOT_CACHE = None


def venv_root() -> str:
    """Return the directory that holds every per-backend venv.

    Resolution order (highest priority first):
        1. ``NUNBA_VENV_ROOT_OVERRIDE`` env var (tests / custom deploys).
        2. ``core.platform_paths.get_data_dir() / "data" / "venvs"``
           (the canonical answer in any normal install).
        3. OS-aware fallback when ``core.platform_paths`` is unimportable
           (pure-Nunba lint runs that have not yet activated HARTOS).
    """
    override = os.environ.get("NUNBA_VENV_ROOT_OVERRIDE", "").strip()
    if override:
        os.makedirs(override, exist_ok=True)
        return override

    global _VENV_ROOT_CACHE
    if _VENV_ROOT_CACHE is not None:
        return _VENV_ROOT_CACHE

    try:
        from core.platform_paths import get_data_dir  # type: ignore
        base = os.path.join(str(get_data_dir()), "data", "venvs")
    except Exception:
        # platform_paths unimportable — replicate its decision tree.
        home = os.path.expanduser("~")
        if sys.platform == "win32":
            base = os.path.join(home, "Documents", "Nunba", "data", "venvs")
        elif sys.platform == "darwin":
            base = os.path.join(home, "Library", "Application Support",
                                "Nunba", "data", "venvs")
        else:
            base = os.path.join(home, ".config", "nunba", "data", "venvs")

    os.makedirs(base, exist_ok=True)
    _VENV_ROOT_CACHE = base
    return base


def _validate_backend_name(backend: str) -> None:
    """Reject unsafe backend names before they touch the filesystem."""
    if not backend or not isinstance(backend, str):
        raise ValueError(f"backend must be a non-empty string, got {backend!r}")
    if not backend.replace("_", "").replace("-", "").isalnum():
        raise ValueError(
            f"backend name must be alphanumeric / underscore / dash only, "
            f"got {backend!r}"
        )
    if backend.startswith("."):
        raise ValueError(f"backend name must not start with a dot: {backend!r}")


def venv_path(backend: str) -> str:
    """Return the directory for a specific backend's venv."""
    _validate_backend_name(backend)
    return os.path.join(venv_root(), backend)


def venv_python(backend: str) -> str:
    """Return the canonical path to the Python executable inside a backend's venv.

    The path is returned whether or not the venv exists on disk.  Use
    ``venv_python_if_exists`` for the existence-checked variant.
    """
    vpath = venv_path(backend)
    if sys.platform == "win32":
        return os.path.join(vpath, "Scripts", "python.exe")
    return os.path.join(vpath, "bin", "python")


def venv_python_if_exists(backend: Optional[str]) -> Optional[str]:
    """Return the venv's python.exe path if it exists on disk, else None.

    The HARTOS spawn path uses this resolver: ``None`` lets the caller
    fall through to the bundled python-embed (the right behavior for
    backends that don't have their own venv yet).
    """
    if not backend:
        return None
    try:
        candidate = venv_python(backend)
    except ValueError:
        return None
    return candidate if os.path.isfile(candidate) else None
