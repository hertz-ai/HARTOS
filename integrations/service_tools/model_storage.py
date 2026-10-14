"""
Model Storage Manager — centralized model storage at ~/.hevolve/models/

Tracks all downloaded models (git repos, HuggingFace weights, npm projects
set up at runtime) in a single manifest.json so the user can see where their
disk space is going and the RuntimeToolManager can skip re-downloads.

Pattern from: integrations/vision/minicpm_installer.py
"""

import hashlib
import json
import logging
import os
import shutil
import subprocess
import sys
import threading
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

from core.node_bin import find_node, find_npm
from core.subprocess_safe import run_bounded

logger = logging.getLogger(__name__)

#: Bounds on the two steps of an npm project's setup (install_npm_project):
#: ``npm install`` and the project's prepare script (which may download a
#: browser).  A step past its bound is killed and the setup fails.
NPM_INSTALL_TIMEOUT_S = 900.0
NPM_PREPARE_TIMEOUT_S = 900.0
#: Directories of an npm project that are never copied or fingerprinted.
_NPM_SKIP_DIRS = frozenset({'node_modules', '__pycache__'})
#: In a tool dir: the project files the last setup copied there (sorted
#: '/'-separated relative paths), so the next setup removes the ones the
#: project no longer has (_mirror_npm_project).
NPM_COPIED_LIST = '.hart_project_files.json'
#: One npm setup per tool directory at a time (two npm installs in one
#: folder corrupt it); the second caller waits, then finds the receipt.
_NPM_LOCKS: Dict[str, threading.Lock] = {}
_NPM_LOCKS_GUARD = threading.Lock()


class NpmSetupFailed(RuntimeError):
    """An npm project could not be set up; the message says why in words a
    person can be told (Node.js missing, npm install failed, timed out)."""

DEFAULT_BASE_DIR = Path.home() / '.hevolve' / 'models'

# Back-compat alias.  Import-time constant, so it does NOT see a
# HEVOLVE_MODEL_DIR set after this module is imported — call
# get_base_dir() instead of reading this.
BASE_DIR = DEFAULT_BASE_DIR
MANIFEST_FILE = BASE_DIR / 'manifest.json'


def get_base_dir() -> Path:
    """Return the model-storage root, honouring ``HEVOLVE_MODEL_DIR``.

    THE single resolver for "where models live".  gpu_worker's
    ToolWorker._get_output_dir already read HEVOLVE_MODEL_DIR to place a
    tool's ``output/`` subdirectory, but ModelStorageManager pinned
    ~/.hevolve/models unconditionally — so the env var moved a tool's
    OUTPUT while its WEIGHTS still landed on the home drive.  Measured
    2026-09-21 on this box: C: had 23 GB free against F:'s 468 GB, and
    every production caller constructs RuntimeToolManager() with
    defaults, so a 10 GB download had no way to be steered off C:.
    Resolved here once; gpu_worker imports this instead of re-reading
    the variable.

    An unset/blank value keeps the historical default, so existing
    installs do not move.
    """
    raw = os.environ.get('HEVOLVE_MODEL_DIR', '').strip()
    if not raw:
        return DEFAULT_BASE_DIR
    return Path(os.path.expanduser(raw))


class ModelStorageManager:
    """Centralized model storage with manifest tracking."""

    def __init__(self, base_dir: Path = None):
        # Resolved per-instance (not at import) so a HEVOLVE_MODEL_DIR set
        # by the launching process is honoured by default construction.
        self.base_dir = Path(base_dir) if base_dir else get_base_dir()
        self.manifest_file = self.base_dir / 'manifest.json'
        # The directory is created LAZILY, on the first write
        # (_write_manifest, and the per-tool writers that already mkdir with
        # parents=True), never here.  This constructor runs at IMPORT through
        # the module singleton below, and base_dir can be a user path on an
        # external drive (HEVOLVE_MODEL_DIR).  An eager mkdir turned an
        # unplugged drive into "cannot import model_storage", which took
        # vram_manager and Nunba's TTS down with it (2026-09-23).  Nor is
        # there a fallback to the home directory: that would make the
        # person's models "disappear" and could re-download them onto C:.

    # ── Path helpers ──────────────────────────────────────────────

    def is_reachable(self) -> bool:
        """Can models be stored here at all -- as opposed to "none are here"?

        An empty manifest cannot tell those apart: an unplugged external
        drive (HEVOLVE_MODEL_DIR) reads as empty, exactly like a fresh
        install. Only the fresh install means "go download them"; the
        unplugged drive means "the models exist, they are just not
        attached". Callers that would act on "not downloaded" -- fetching
        again, or telling the person a model is missing -- ask this first.

        True when base_dir is a directory, or when its nearest existing
        ancestor is a directory (so the first write can create it). False
        when no ancestor exists (a drive that is not there) or the nearest
        one is a file.
        """
        for candidate in (self.base_dir, *self.base_dir.parents):
            try:
                if candidate.exists():
                    return candidate.is_dir()
            except OSError:
                return False
        return False

    def get_tool_dir(self, tool_name: str) -> Path:
        """Return the storage directory for a given tool."""
        return self.base_dir / tool_name

    # ── Manifest I/O ─────────────────────────────────────────────

    def _read_manifest(self) -> Dict:
        if self.manifest_file.exists():
            try:
                return json.loads(self.manifest_file.read_text())
            except (json.JSONDecodeError, OSError):
                logger.warning("Corrupt manifest.json — resetting")
        return {"tools": {}}

    def _write_manifest(self, data: Dict) -> None:
        # The one writer that goes straight into base_dir, so it makes the
        # directory; an unreachable one raises here, honestly (see __init__).
        self.base_dir.mkdir(parents=True, exist_ok=True)
        self.manifest_file.write_text(json.dumps(data, indent=2))

    def get_manifest(self) -> Dict:
        """Return the full manifest."""
        return self._read_manifest()

    # ── Download state ───────────────────────────────────────────

    def is_downloaded(self, tool_name: str) -> bool:
        """Check if a tool's models are already downloaded.

        The DIRECTORY is the source of truth, not the manifest.  Models can
        arrive by paths that never call mark_downloaded(): measured on a live
        box 2026-08-29, ~/.hevolve/models held 12.3 GB across minicpm (6600MB),
        stt (5347MB), luxtts (342MB) and tts (59MB) with no manifest row, so
        this returned False for all four.  Callers act on that -- runtime_manager
        :126 and :160 gate tool use on it, :209 and :302 report it to the UI,
        and download_hf_model:163 would re-fetch every one of those gigabytes.
        manifest.json stays what its own module docstring calls it, a ledger of
        where disk space went; presence on disk answers "is it downloaded".

        A directory without FILES is not a download.  `tool_dir.mkdir()` runs
        BEFORE each fetch, so a failure leaves an empty directory behind
        (diffrhythm's HF call took a 401 and left one).  Several tools also
        pre-create an `output/` subdirectory, so kokoro/chatterbox/cosyvoice
        each held one empty child dir and nothing else -- a plain
        `any(iterdir())` counts that as content and answers True.  Requiring a
        real file is what separates "downloaded" from "directory exists".
        any() short-circuits on the first hit, so this does not walk a 6 GB
        tree.
        """
        tool_dir = self.get_tool_dir(tool_name)
        if not tool_dir.exists():
            return False
        return any(p.is_file() for p in tool_dir.rglob("*"))

    def mark_downloaded(self, tool_name: str, source_url: str,
                        size_bytes: int = 0, patterns=None) -> None:
        """Record that a tool's models have been downloaded.

        ``patterns`` is the ``allow_patterns`` the fetch ran with (None =
        the whole repo).  It is recorded because the receipt otherwise
        cannot say WHAT it is a receipt for: a fetch narrowed to
        ``model_index.json`` writes the same row shape as a fetch of the
        full 28 GB pipeline, and the next caller asking for the pipeline
        would be told it already has it.  Self-caught 2026-09-21 — an
        instrument check that pulled 4 config files (2,194 bytes) left a
        row that made the real download a no-op.
        """
        manifest = self._read_manifest()
        manifest.setdefault("tools", {})[tool_name] = {
            "source_url": source_url,
            "size_bytes": size_bytes,
            "downloaded_at": datetime.now().isoformat(),
            "path": str(self.get_tool_dir(tool_name)),
            "patterns": sorted(patterns) if patterns else None,
        }
        self._write_manifest(manifest)
        logger.info(f"Marked {tool_name} as downloaded ({size_bytes / 1e9:.2f} GB)")

    # ── Size tracking ────────────────────────────────────────────

    def get_tool_size(self, tool_name: str) -> int:
        """Return total bytes used by a tool's directory."""
        tool_dir = self.get_tool_dir(tool_name)
        if not tool_dir.exists():
            return 0
        total = 0
        for f in tool_dir.rglob('*'):
            if f.is_file():
                total += f.stat().st_size
        return total

    def get_total_size(self) -> int:
        """Return total bytes used by all models."""
        if not self.base_dir.exists():
            return 0
        total = 0
        for f in self.base_dir.rglob('*'):
            if f.is_file():
                total += f.stat().st_size
        return total

    # ── Git clone ────────────────────────────────────────────────

    def clone_repo(self, tool_name: str, repo_url: str,
                   branch: str = None) -> Optional[Path]:
        """Clone (or pull) a git repo into the tool's directory.

        Returns the tool directory on success, None on failure.
        """
        tool_dir = self.get_tool_dir(tool_name)

        if tool_dir.exists() and (tool_dir / '.git').exists():
            # Already cloned — pull latest
            logger.info(f"Pulling latest for {tool_name}...")
            try:
                _git_kwargs = dict(cwd=str(tool_dir), capture_output=True, timeout=120)
                if sys.platform == 'win32':
                    _git_kwargs['creationflags'] = subprocess.CREATE_NO_WINDOW
                subprocess.run(['git', 'pull'], **_git_kwargs)
                return tool_dir
            except Exception as e:
                logger.warning(f"git pull failed for {tool_name}: {e}")
                return tool_dir  # still usable

        # Fresh clone
        logger.info(f"Cloning {repo_url} into {tool_dir}...")
        tool_dir.mkdir(parents=True, exist_ok=True)
        cmd = ['git', 'clone', '--depth', '1']
        if branch:
            cmd += ['--branch', branch]
        cmd += [repo_url, str(tool_dir)]

        try:
            _clone_kwargs = dict(capture_output=True, text=True, timeout=300)
            if sys.platform == 'win32':
                _clone_kwargs['creationflags'] = subprocess.CREATE_NO_WINDOW
            result = subprocess.run(cmd, **_clone_kwargs)
            if result.returncode != 0:
                logger.error(f"git clone failed: {result.stderr[:300]}")
                return None
            size = self.get_tool_size(tool_name)
            self.mark_downloaded(tool_name, repo_url, size)
            return tool_dir
        except Exception as e:
            logger.error(f"git clone failed for {tool_name}: {e}")
            return None

    # ── HuggingFace download ─────────────────────────────────────

    # A download is "complete" only within this margin of the byte count
    # mark_downloaded() recorded on the run that finished.  Not 1.0: the
    # receipt is written from get_tool_size() AFTER the fetch, so a tool
    # that writes a log line or an output/ file later grows past its own
    # receipt, and a hair under is ordinary filesystem noise.
    _COMPLETE_SIZE_RATIO = 0.99

    def hf_download_is_complete(self, tool_name: str,
                                allow_patterns=None) -> bool:
        """True when the recorded download is still all there on disk.

        ``is_downloaded()`` answers "are there files", which is the right
        question for its callers and must not change (its own docstring
        records why it trusts the disk over the manifest).  It is the
        WRONG question for "may I skip the fetch", because ANY one file
        answers it.  MEASURED 2026-09-21 on this box:
        ~/.hevolve/models/ltx2 carried a manifest row reading
        28,405,153,793 bytes from 2026-04-16 while the directory held
        1,336,240,620 — 4.7% — everything but a stray LoRA having been
        reclaimed by the 2026-09-13 disk-full triage.  is_downloaded()
        said True, so download_hf_model returned early and the 27 GB
        could never come back: an interrupted or pruned fetch was
        permanent, and the tool it belonged to could never start.

        Requiring the receipt AND the bytes makes the repair automatic —
        snapshot_download re-runs and skips the files that survived, so
        resuming costs only what is actually missing.  A tool with files
        but no receipt (weights placed by some other path, the case
        is_downloaded exists for) also re-runs, which is a metadata call
        and no re-transfer.
        """
        row = self._read_manifest().get("tools", {}).get(tool_name)
        if not row:
            return False
        # A receipt covers only the pattern set it was taken under.  A
        # narrower one (or one from before patterns were recorded, which
        # reads as the whole repo) cannot vouch for a wider request.
        if "patterns" in row:
            want = sorted(allow_patterns) if allow_patterns else None
            if row.get("patterns") != want:
                return False
        recorded = row.get("size_bytes") or 0
        if recorded <= 0:
            # Older receipts carry no size; fall back to presence so we
            # do not re-fetch a download that predates size tracking.
            return self.is_downloaded(tool_name)
        return self.get_tool_size(tool_name) >= recorded * self._COMPLETE_SIZE_RATIO

    def download_hf_model(self, tool_name: str, repo_id: str,
                          **kwargs) -> Optional[Path]:
        """Download a HuggingFace model using snapshot_download.

        Pattern from minicpm_installer.py.
        Returns the tool directory on success, None on failure.
        """
        tool_dir = self.get_tool_dir(tool_name)
        allow_patterns = kwargs.get("allow_patterns")

        if self.hf_download_is_complete(tool_name, allow_patterns):
            logger.info(f"HF model for {tool_name} already downloaded")
            return tool_dir

        tool_dir.mkdir(parents=True, exist_ok=True)

        try:
            from huggingface_hub import snapshot_download
            logger.info(f"Downloading {repo_id} to {tool_dir}...")
            snapshot_download(
                repo_id=repo_id,
                local_dir=str(tool_dir),
                local_dir_use_symlinks=False,
                **kwargs,
            )
            size = self.get_tool_size(tool_name)
            self.mark_downloaded(tool_name, f"hf://{repo_id}", size,
                                 patterns=allow_patterns)
            return tool_dir
        except ImportError:
            logger.error("huggingface_hub not installed. pip install huggingface_hub")
            return None
        except Exception as e:
            # A fetch that cannot reach the Hub must not destroy an install
            # that already works offline.  setup_tool no longer pre-checks
            # is_downloaded for hf tools, so without this an air-gapped or
            # briefly-offline boot would turn every already-present model
            # (whisper, minicpm, ...) into "Download failed".  Degraded, and
            # said so: the files are there, their completeness is unproven.
            if self.is_downloaded(tool_name):
                logger.warning(
                    f"HF download failed for {tool_name}: {e} — keeping the "
                    f"{self.get_tool_size(tool_name) / 1e9:.2f} GB already in "
                    f"{tool_dir}; completeness UNVERIFIED this run"
                )
                return tool_dir
            logger.error(f"HF download failed for {tool_name}: {e}")
            return None

    # ── npm project (Node packages set up at runtime) ────────────
    #
    # Owner ruling A16 (2026-10-14): an optional capability is set up on
    # demand at runtime, into this storage, never packaged into a build.  An
    # npm project is HARTOS's own source (package.json naming exact versions,
    # plus its scripts); the packages it names arrive here through npm, and
    # its prepare script (run with Node afterwards) fetches anything else it
    # needs, such as a headless browser, into the same folder.  The receipt
    # (mark_downloaded) names the project's fingerprint, so a HARTOS update
    # that changes the project sets it up again.

    @staticmethod
    def _npm_project_files(project_dir: str) -> List[str]:
        """The project's files to copy, as sorted relative paths: everything
        but Python files (the project folder is also a Python package, so
        package-data ships it) and the _NPM_SKIP_DIRS."""
        out = []
        for root, dirs, files in os.walk(project_dir):
            dirs[:] = sorted(d for d in dirs if d not in _NPM_SKIP_DIRS)
            for name in files:
                if name.endswith(('.py', '.pyc')):
                    continue
                out.append(os.path.relpath(os.path.join(root, name), project_dir))
        return sorted(out)

    def npm_project_fingerprint(self, project_dir: str) -> str:
        """sha256 over the project's files (each relative path, then its bytes)."""
        digest = hashlib.sha256()
        for rel in self._npm_project_files(project_dir):
            digest.update(rel.replace(os.sep, '/').encode('utf-8') + b'\0')
            with open(os.path.join(project_dir, rel), 'rb') as fh:
                digest.update(fh.read())
            digest.update(b'\0')
        return digest.hexdigest()

    @staticmethod
    def _npm_declares_dependencies(project_dir: str) -> bool:
        """Does the project's package.json name any dependency?  npm makes no
        node_modules for a project that names none.  An unreadable
        package.json counts as naming some (its install then has to show
        them)."""
        try:
            with open(os.path.join(project_dir, 'package.json'), encoding='utf-8') as fh:
                doc = json.load(fh)
        except (OSError, ValueError):
            return True
        if not isinstance(doc, dict):
            return True
        return any(doc.get(key) for key in ('dependencies', 'optionalDependencies'))

    def npm_install_is_complete(self, tool_name: str, project_dir: str) -> bool:
        """True when this exact project was set up here and its packages are
        still on disk: the receipt names the project's current fingerprint
        and ``node_modules`` exists (or the project names no dependency, for
        which npm makes none).  A partly finished setup leaves no receipt
        (mark_downloaded is its last step)."""
        row = self._read_manifest().get("tools", {}).get(tool_name)
        if not row or not os.path.isdir(project_dir):
            return False
        if row.get("source_url") != 'npm:' + self.npm_project_fingerprint(project_dir):
            return False
        return ((self.get_tool_dir(tool_name) / 'node_modules').is_dir()
                or not self._npm_declares_dependencies(project_dir))

    def _mirror_npm_project(self, project_dir: str, tool_dir: Path) -> None:
        """Make the project's files in ``tool_dir`` the source's own.

        Removes each file an earlier setup copied (NPM_COPIED_LIST) that the
        project no longer has, and the folders that leaves empty; writes the
        new list; then copies every project file.  A HARTOS update that
        deletes or renames a file therefore leaves no stale copy behind
        (peer validation of 38a220495).  Only listed files are touched:
        node_modules, the browser a prepare script downloaded there, npm's
        lock file and anything else not copied by a setup stay.  The list
        is written before the copy, so a copy that fails half way still
        names every file it may have placed.  Raises OSError.
        """
        root = os.path.normpath(os.path.abspath(str(tool_dir)))
        files = [rel.replace(os.sep, '/') for rel in self._npm_project_files(project_dir)]
        listing = tool_dir / NPM_COPIED_LIST
        try:
            before = json.loads(listing.read_text(encoding='utf-8'))
        except FileNotFoundError:
            before = []
        except (OSError, ValueError) as e:
            logger.warning("%s is unreadable (%s); no earlier project file is removed",
                           listing, e)
            before = []
        keep = set(files)
        for rel in before if isinstance(before, list) else ():
            if not isinstance(rel, str) or rel in keep:
                continue
            # A list entry is data on disk, not trusted.  An absolute one, or
            # one naming a drive, is never ours: refused before it is joined
            # (on Windows os.path.relpath raises ValueError for a path on
            # another drive -- A23a N2 -- where setup_tool expects only
            # NpmSetupFailed).
            if os.path.isabs(rel) or os.path.splitdrive(rel)[0] or rel.startswith(('/', '\\')):
                continue
            path = os.path.normpath(os.path.join(root, rel))
            inside = os.path.relpath(path, root).replace(os.sep, '/')
            # Never outside the tool dir, never into what npm or a prepare
            # script own.
            if inside.startswith('../') or inside == '..' \
                    or inside.split('/')[0] in _NPM_SKIP_DIRS or inside == NPM_COPIED_LIST:
                continue
            try:
                os.remove(path)
            except FileNotFoundError:
                pass
            parent = os.path.dirname(path)
            while parent != root and parent.startswith(root + os.sep):
                try:
                    os.rmdir(parent)            # only when empty
                except OSError:
                    break
                parent = os.path.dirname(parent)
        tool_dir.mkdir(parents=True, exist_ok=True)
        listing.write_text(json.dumps(files), encoding='utf-8')
        for rel in files:
            dest = tool_dir / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(os.path.join(project_dir, rel), dest)

    @staticmethod
    def _npm_lock(tool_dir: Path) -> threading.Lock:
        key = os.path.normcase(os.path.abspath(str(tool_dir)))
        with _NPM_LOCKS_GUARD:
            return _NPM_LOCKS.setdefault(key, threading.Lock())

    @staticmethod
    def _npm_step(cmd: List[str], what: str, cwd: Path, timeout_s: float) -> None:
        """Run one setup step in ``cwd``, bounded; NpmSetupFailed unless it exits 0."""
        env = dict(os.environ, npm_config_update_notifier='false', npm_config_fund='false',
                   npm_config_audit='false')
        try:
            result = run_bounded(cmd, timeout=timeout_s, cwd=str(cwd), env=env,
                                 encoding='utf-8', errors='replace')
        except (OSError, ValueError) as e:
            raise NpmSetupFailed(f'{what} could not be started: {e}') from e
        if result.timed_out:
            raise NpmSetupFailed(f'{what} timed out after {int(timeout_s)} s')
        if result.returncode != 0:
            tail = ' '.join((result.stderr or result.stdout or '').split())[-400:]
            raise NpmSetupFailed(f'{what} failed (exit {result.returncode}): {tail}')
        logger.info("%s finished in %s: %s", what, cwd,
                    ' '.join((result.stdout or '').split())[-300:])

    def install_npm_project(self, tool_name: str, project_dir: str, *,
                            prepare: Optional[str] = None) -> Path:
        """Set the npm project at ``project_dir`` up in this tool's directory.

        Mirrors the project's files there (_mirror_npm_project: copied, and
        files an earlier setup copied that the project no longer has
        removed), runs ``npm install``
        there, then ``node <prepare>`` when the project names a prepare
        script, and records the receipt.  Idempotent: a complete setup of
        this exact project returns at once; one setup per tool directory
        runs at a time.  Returns the tool directory; raises NpmSetupFailed
        (no package.json in the project, Node.js or npm missing, a step
        failed or ran past its bound, npm made no node_modules for a project
        that names dependencies, the copy failed), leaving no receipt.
        """
        tool_dir = self.get_tool_dir(tool_name)
        with self._npm_lock(tool_dir):
            if self.npm_install_is_complete(tool_name, project_dir):
                return tool_dir
            if not os.path.isfile(os.path.join(project_dir, 'package.json')):
                # npm install in a folder with no package.json walks up to the
                # nearest ancestor that has one and installs there.
                raise NpmSetupFailed('the project at %s has no package.json' % project_dir)
            node, npm = find_node(), find_npm()
            if not node or not npm:
                raise NpmSetupFailed(
                    'Node.js is not installed on this computer (%s not found)'
                    % ' and '.join(n for n, p in (('node', node), ('npm', npm)) if not p))
            fingerprint = self.npm_project_fingerprint(project_dir)
            try:
                self._mirror_npm_project(project_dir, tool_dir)
            except OSError as e:
                raise NpmSetupFailed(f'the project could not be copied into {tool_dir}: {e}') from e
            # npm on Windows is a .cmd shim; launch_argv is how a shim is run.
            from integrations.coding_agent.installer import launch_argv
            self._npm_step(launch_argv([npm, 'install', '--no-audit', '--no-fund',
                                        '--loglevel=error']),
                           'npm install', tool_dir, NPM_INSTALL_TIMEOUT_S)
            if not (tool_dir / 'node_modules').is_dir() \
                    and self._npm_declares_dependencies(project_dir):
                raise NpmSetupFailed('npm install finished but made no node_modules in %s'
                                     % tool_dir)
            if prepare:
                self._npm_step([node, prepare], prepare, tool_dir, NPM_PREPARE_TIMEOUT_S)
            self.mark_downloaded(tool_name, 'npm:' + fingerprint, self.get_tool_size(tool_name))
            return tool_dir

    # ── Cleanup ──────────────────────────────────────────────────

    def remove_tool(self, tool_name: str) -> bool:
        """Remove a tool's models and manifest entry."""
        tool_dir = self.get_tool_dir(tool_name)
        if tool_dir.exists():
            shutil.rmtree(tool_dir, ignore_errors=True)

        manifest = self._read_manifest()
        manifest.get("tools", {}).pop(tool_name, None)
        self._write_manifest(manifest)

        logger.info(f"Removed {tool_name} from model storage")
        return True


# Global singleton
model_storage = ModelStorageManager()
