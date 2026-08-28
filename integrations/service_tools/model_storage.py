"""
Model Storage Manager — centralized model storage at ~/.hevolve/models/

Tracks all downloaded models (git repos, HuggingFace weights) in a single
manifest.json so the user can see where their disk space is going and
the RuntimeToolManager can skip re-downloads.

Pattern from: integrations/vision/minicpm_installer.py
"""

import json
import logging
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import Dict, Optional

logger = logging.getLogger(__name__)

BASE_DIR = Path.home() / '.hevolve' / 'models'
MANIFEST_FILE = BASE_DIR / 'manifest.json'


class ModelStorageManager:
    """Centralized model storage with manifest tracking."""

    def __init__(self, base_dir: Path = None):
        self.base_dir = base_dir or BASE_DIR
        self.manifest_file = self.base_dir / 'manifest.json'
        self.base_dir.mkdir(parents=True, exist_ok=True)

    # ── Path helpers ──────────────────────────────────────────────

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
                        size_bytes: int = 0) -> None:
        """Record that a tool's models have been downloaded."""
        manifest = self._read_manifest()
        manifest.setdefault("tools", {})[tool_name] = {
            "source_url": source_url,
            "size_bytes": size_bytes,
            "downloaded_at": datetime.now().isoformat(),
            "path": str(self.get_tool_dir(tool_name)),
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

    def download_hf_model(self, tool_name: str, repo_id: str,
                          **kwargs) -> Optional[Path]:
        """Download a HuggingFace model using snapshot_download.

        Pattern from minicpm_installer.py.
        Returns the tool directory on success, None on failure.
        """
        tool_dir = self.get_tool_dir(tool_name)

        if self.is_downloaded(tool_name):
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
            self.mark_downloaded(tool_name, f"hf://{repo_id}", size)
            return tool_dir
        except ImportError:
            logger.error("huggingface_hub not installed. pip install huggingface_hub")
            return None
        except Exception as e:
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
