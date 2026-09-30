"""
Model Storage Manager — centralized model storage at ~/.hevolve/models/

Tracks all downloaded models (git repos, HuggingFace weights) in a single
manifest.json so the user can see where their disk space is going and
the RuntimeToolManager can skip re-downloads.

Pattern from: integrations/vision/minicpm_installer.py
"""

import json
import logging
import os
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import Dict, Optional

logger = logging.getLogger(__name__)

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
