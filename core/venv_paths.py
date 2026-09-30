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
    venv_root()                            -> str
    reset_venv_root_cache()                -> None   (tests)
    validate_backend_name(backend)         -> None   (raises ValueError)
    venv_path(backend)                     -> str
    venv_python(backend)                   -> str
    venv_python_if_exists(backend)         -> Optional[str]
    python_embed_dir()                     -> str
    venv_creator_python()                  -> Optional[str]
    venv_mismatch(backend)                 -> Optional[str]
    venv_site_packages(backend)            -> str
    parent_package_roots()                 -> List[str]
    parent_package_root_of(module_name)    -> Optional[str]
    ensure_parent_packages_visible(backend) -> Optional[str]
"""
from __future__ import annotations

import importlib.util
import locale
import logging
import os
import sys
from typing import List, Optional

logger = logging.getLogger(__name__)


_VENV_ROOT_CACHE: Optional[str] = None


def reset_venv_root_cache() -> None:
    """Reset the cached venv root.  For tests (Nunba's tts.backend_venv
    forwards to it) — do not call from production code (the cache makes
    hot-path lookups O(1))."""
    global _VENV_ROOT_CACHE
    _VENV_ROOT_CACHE = None


# The old private name, kept for HARTOS tests that call it.
_reset_cache_for_tests = reset_venv_root_cache


def venv_root() -> str:
    """Return the directory that holds every per-backend venv.

    Resolution order (highest priority first):
        1. ``NUNBA_VENV_ROOT_OVERRIDE`` env var (tests / custom deploys).
        2. ``core.platform_paths.get_data_dir() / "data" / "venvs"``
           (the canonical answer; this module is in the same ``core``
           package, so there is no install where it is missing).
    """
    override = os.environ.get("NUNBA_VENV_ROOT_OVERRIDE", "").strip()
    if override:
        os.makedirs(override, exist_ok=True)
        return override

    global _VENV_ROOT_CACHE
    if _VENV_ROOT_CACHE is not None:
        return _VENV_ROOT_CACHE

    from core.platform_paths import get_data_dir
    base = os.path.join(str(get_data_dir()), "data", "venvs")

    os.makedirs(base, exist_ok=True)
    _VENV_ROOT_CACHE = base
    return base


def validate_backend_name(backend: str) -> None:
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


# The old private name, kept for callers that still use it.
_validate_backend_name = validate_backend_name


def venv_path(backend: str) -> str:
    """Return the directory for a specific backend's venv."""
    validate_backend_name(backend)
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


# ── Which interpreter builds a backend venv, and did it build this one ───────
#
# Measured 2026-09-25 on the installed build: neutts_air, melotts and kokoro
# had venvs made by a source-mode run on miniconda 3.11 (pyvenv.cfg home =
# miniconda3, version = 3.11.4) in the store the installed 3.12 app shares.
# The app adopted them because only python.exe's existence was checked,
# spawned them with its own 3.12 sys.path in PYTHONPATH, and each died before
# running any Python: "bad magic number in 'encodings'".  A venv made from
# python-embed runs isolated and ignores PYTHONPATH; a venv made by any other
# interpreter reads it, so even a same-version one would load python-embed's
# packages ahead of its own pins.  A venv is therefore used only when the
# interpreter this process creates venvs with is the one that built it.

def python_embed_dir() -> str:
    """The frozen build's bundled ``python-embed`` directory: beside the
    app binary, with ``sys.executable`` resolved (a launcher symlink points
    at the binary, and python-embed sits beside the binary, not the link).
    The one place that computes it; callers that name it in an error use
    this too."""
    return os.path.join(
        os.path.dirname(os.path.realpath(sys.executable)), "python-embed")


def venv_creator_python() -> Optional[str]:
    """Return the interpreter that creates (and whose venvs may run) the
    backend venvs for this process, or None when there is none.

    On the frozen build ``sys.executable`` is the app binary, not Python;
    the interpreter is the bundled ``python-embed`` beside it
    (``python_embed_dir``).  Running from source, it is ``sys.executable``.
    Every worker spawn asks this (gpu_worker, hevolveai_supervisor,
    diarization_service), so one answer decides which python runs a worker.
    """
    if getattr(sys, "frozen", False):
        embed = python_embed_dir()
        for parts in (("python.exe",), ("bin", "python3"), ("bin", "python")):
            candidate = os.path.join(embed, *parts)
            if os.path.isfile(candidate):
                return candidate
        return None
    return sys.executable


def _creator_home() -> Optional[str]:
    """The ``home`` a venv made by ``venv_creator_python()`` records: the
    directory of that interpreter's base executable (``python -m venv``
    writes ``dirname(sys._base_executable)``, so a creator that is itself a
    venv records its base install)."""
    creator = venv_creator_python()
    if not creator:
        return None
    if getattr(sys, "frozen", False):
        return os.path.dirname(os.path.abspath(creator))
    base = getattr(sys, "_base_executable", None) or sys.executable
    return os.path.dirname(os.path.abspath(base))


def _read_pyvenv_cfg(vpath: str) -> Optional[dict]:
    """``key = value`` pairs of a venv's pyvenv.cfg, or None if unreadable."""
    try:
        with open(os.path.join(vpath, "pyvenv.cfg"), "r",
                  encoding="utf-8", errors="replace") as fh:
            lines = fh.read().splitlines()
    except OSError:
        return None
    cfg = {}
    for line in lines:
        key, sep, value = line.partition("=")
        if sep:
            cfg[key.strip().lower()] = value.strip()
    return cfg


def _same_dir(a: str, b: str) -> bool:
    def norm(p, resolve):
        p = os.path.realpath(p) if resolve else os.path.abspath(p)
        return os.path.normcase(os.path.normpath(p))
    return norm(a, False) == norm(b, False) or norm(a, True) == norm(b, True)


def venv_mismatch(backend: Optional[str]) -> Optional[str]:
    """Why ``backend``'s venv may not be used by this process, or None.

    None means either there is no venv (nothing to refuse) or the venv was
    built by ``venv_creator_python()``: its pyvenv.cfg ``home`` is that
    interpreter's home and its ``version`` has this Python's major.minor
    (the stdlib bytecode magic changes per minor release).
    """
    if not backend:
        return None
    try:
        candidate = venv_python(backend)
    except ValueError:
        return None
    if not os.path.isfile(candidate):
        return None
    vpath = venv_path(backend)
    want_home = _creator_home()
    want_version = "%d.%d" % sys.version_info[:2]
    if not want_home:
        return (f"venv {backend!r} at {vpath}: this process has no "
                f"interpreter to run backend venvs with")
    cfg = _read_pyvenv_cfg(vpath)
    if not cfg:
        return (f"venv {backend!r} at {vpath} has no readable pyvenv.cfg, so "
                f"what built it is unknown; this process runs venvs built "
                f"from {want_home} (Python {want_version})")
    home = cfg.get("home", "")
    version = cfg.get("version") or cfg.get("version_info") or ""
    got_version = ".".join(version.split(".")[:2])
    if not home or not _same_dir(home, want_home) or got_version != want_version:
        return (f"venv {backend!r} at {vpath} was built from {home or '?'} "
                f"(Python {version or '?'}); this process runs venvs built "
                f"from {want_home} (Python {want_version})")
    return None


_MISMATCH_LOGGED: set = set()


def venv_python_if_exists(backend: Optional[str]) -> Optional[str]:
    """Return the venv's python.exe path if it exists on disk AND was built
    by this process's venv interpreter (``venv_mismatch``), else None.

    The HARTOS spawn path uses this resolver: ``None`` lets the caller
    fall through to the bundled python-embed (the right behavior for
    backends that don't have their own venv yet, and for a venv another
    interpreter built, which would die at startup or break its cage).
    """
    if not backend:
        return None
    try:
        candidate = venv_python(backend)
    except ValueError:
        return None
    if not os.path.isfile(candidate):
        return None
    reason = venv_mismatch(backend)
    if reason:
        if reason not in _MISMATCH_LOGGED:
            _MISMATCH_LOGGED.add(reason)
            logger.warning("%s; not using it until it is rebuilt", reason)
        return None
    return candidate


# ── Parent-package visibility inside a backend venv ──────────────────────────
#
# Measured 2026-09-20 on the installed build: python-embed ships a
# ``python312._pth`` next to its DLL, and CPython applies that file to every
# venv created from that interpreter.  The venv's python.exe therefore boots
# with ``sys.flags.isolated == 1``, ignores PYTHONPATH entirely, and (with
# ``include-system-site-packages = false``) never sees python-embed's
# site-packages, where the app packages live.  So the spawn path's
# "propagate the parent's sys.path via PYTHONPATH" reached no venv worker at
# all: chatterbox_turbo died with ``No module named 'integrations'`` before
# its engine ever loaded.  A ``.pth`` file in the venv's own site-packages IS
# processed in that mode, so it is the one way to tell a quarantined worker
# where the app packages are.  Written here, by the one function both the
# INSTALL path (``tts.backend_venv.ensure_venv``) and the SPAWN path
# (``gpu_worker._resolve_backend_venv_python``) call.

PARENT_PACKAGES_PTH = "nunba_parent_packages.pth"

# The top-level app packages the worker dispatcher imports.  The venv must
# see the directory each one lives in: the install root on a frozen build,
# the repo root in a source checkout.
_PARENT_PACKAGES = ("integrations", "core")


def venv_site_packages(backend: str) -> str:
    """Return the site-packages directory inside a backend's venv.

    Windows venvs use ``Lib/site-packages``; POSIX venvs use
    ``lib/pythonX.Y/site-packages``, found on disk when the venv exists and
    otherwise named after the running interpreter (the one that creates
    every backend venv).  Returned whether or not it exists.
    """
    vpath = venv_path(backend)
    if sys.platform == "win32":
        return os.path.join(vpath, "Lib", "site-packages")
    lib = os.path.join(vpath, "lib")
    try:
        for entry in sorted(os.listdir(lib)):
            candidate = os.path.join(lib, entry, "site-packages")
            if entry.startswith("python") and os.path.isdir(candidate):
                return candidate
    except OSError as exc:
        logger.debug("venv %r: %s not listable (%s); naming site-packages "
                     "after this interpreter", backend, lib, exc)
    return os.path.join(
        lib, f"python{sys.version_info[0]}.{sys.version_info[1]}",
        "site-packages",
    )


def _package_location(name: str) -> Optional[str]:
    """Where THIS process resolves top-level ``name`` from, without
    importing it: the package directory, or the module file.  None when the
    name does not resolve at all."""
    try:
        spec = importlib.util.find_spec(name)
    except (ImportError, ValueError):
        return None
    if spec is None:
        return None
    locations = list(spec.submodule_search_locations or [])
    where = locations[0] if locations else spec.origin
    if not where or where in ("built-in", "frozen"):
        return None
    return os.path.abspath(where)


def _is_distribution_dir(path: str) -> bool:
    """A ``site-packages`` / ``dist-packages`` directory holds third-party
    distributions plus a ``sitecustomize`` hook.  Naming one in a venv's
    ``.pth`` would hand the venv every bundled package AND run that hook;
    on the frozen build the bundled ``sitecustomize`` prepends
    ``~/.nunba/site-packages`` ahead of the venv's own pins, and pip inside
    the venv would resolve against the union.  The dependency cage the venv
    exists for would be gone, so such a directory is never written."""
    base = os.path.basename(os.path.normpath(path)).lower()
    return base in ("site-packages", "dist-packages")


def _holds_package(root: str, name: str) -> bool:
    return (os.path.isdir(os.path.join(root, name))
            or os.path.isfile(os.path.join(root, name + ".py")))


def parent_package_roots() -> List[str]:
    """Return the directories a venv may be pointed at to import the app
    packages this process runs.

    In a source checkout ``integrations`` and ``core`` share the HARTOS repo
    root, and that is the answer.  On the frozen build the parent loads
    them from ``python-embed/Lib/site-packages`` (measured 2026-09-20 from
    the parent's own traceback frames), a distribution directory a venv
    must not see; the same build ships a byte-identical copy of every app
    package beside the executable (hash-compared 2026-09-20: integrations,
    core, hartos, security, agent_ledger all SAME), so that copy is used
    instead.  A package with no usable root is logged and skipped.  Roots
    are returned in package order, without duplicates.
    """
    roots: List[str] = []
    for name in _PARENT_PACKAGES:
        location = _package_location(name)
        if not location:
            continue
        located = os.path.dirname(location)
        chosen: Optional[str] = None
        if not _is_distribution_dir(located):
            chosen = located
        elif getattr(sys, "frozen", False):
            beside_exe = os.path.dirname(os.path.abspath(sys.executable))
            if (not _is_distribution_dir(beside_exe)
                    and _holds_package(beside_exe, name)):
                chosen = beside_exe
        if chosen is None:
            logger.warning(
                "%r is loaded from %s, a distribution directory a venv must "
                "not be pointed at, and no copy sits beside %s; venv workers "
                "will not see it", name, located, sys.executable,
            )
            continue
        if chosen not in roots:
            roots.append(chosen)
    return roots


def parent_package_root_of(module_name: str) -> Optional[str]:
    """Return the app root that ships ``module_name``'s top-level package,
    or None when it is not one of the app's own packages.

    A package is the app's own when it sits DIRECTLY under a parent root
    (``<root>/integrations``, ``<root>/hart_intelligence_entry.py``); a
    third-party package under a site-packages directory never does, even
    on the frozen build where app and third-party packages share
    ``python-embed/Lib/site-packages``.  The self-heal uses this: a module
    the app ships cannot be missing from a child for lack of a dependency,
    so pip is never the fix.
    """
    top = (module_name or "").split(".")[0]
    if not top:
        return None
    for root in parent_package_roots():
        if _holds_package(root, top):
            return root
    return None


def _pth_encoding() -> str:
    """The encoding ``site`` reads ``.pth`` files with: UTF-8 from 3.13,
    the locale encoding before that.  The venv that reads the file is
    created by the interpreter running this code, so they agree."""
    if sys.version_info >= (3, 13):
        return "utf-8"
    return locale.getpreferredencoding(False) or "utf-8"


def ensure_parent_packages_visible(backend: Optional[str]) -> Optional[str]:
    """Write the ``.pth`` that lets ``backend``'s venv import the app
    packages this process runs from.

    Returns the ``.pth`` path, or None when the venv does not exist, has no
    site-packages, or the roots cannot be located.  Idempotent: a file that
    already lists the current roots is left untouched (no write, no mtime
    change).  Never raises: a venv that cannot be repaired is logged and
    left to fail at spawn, where the worker's own error names the module.
    """
    if not venv_python_if_exists(backend):
        return None
    site_dir = venv_site_packages(backend)  # backend validated above
    if not os.path.isdir(site_dir):
        logger.warning(
            "venv %r has no site-packages at %s; its worker cannot be told "
            "where the app packages are", backend, site_dir,
        )
        return None
    roots = parent_package_roots()
    if not roots:
        logger.warning(
            "venv %r: this process cannot locate its own %s packages, so "
            "no %s was written", backend, _PARENT_PACKAGES, PARENT_PACKAGES_PTH,
        )
        return None

    content = "".join(root + "\n" for root in roots)
    pth = os.path.join(site_dir, PARENT_PACKAGES_PTH)
    encoding = _pth_encoding()
    try:
        with open(pth, "r", encoding=encoding) as fh:
            if fh.read() == content:
                return pth
    except (OSError, UnicodeError) as exc:
        # absent, unreadable or stale: rewrite below
        logger.debug("venv %r: %s not read (%s); rewriting it", backend,
                     pth, exc)

    tmp = pth + ".tmp"
    try:
        with open(tmp, "w", encoding=encoding, newline="\n") as fh:
            fh.write(content)
        os.replace(tmp, pth)
    except (OSError, UnicodeError) as exc:
        logger.warning(
            "venv %r: could not write %s (%s); its worker will not see %s",
            backend, pth, exc, roots,
        )
        try:
            os.remove(tmp)
        except OSError as rm_exc:
            logger.debug("venv %r: temp %s not removed (%s)", backend, tmp,
                         rm_exc)
        return None
    logger.info("venv %r: %s now lists %s", backend, PARENT_PACKAGES_PTH, roots)
    return pth
