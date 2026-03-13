"""
MiniCPM Model Installer — auto-downloads MiniCPM-V-2 for the vision sidecar.

Follows the same sidecar installer pattern: detect GPU, download model,
verify cache, provide health status. Model is stored in ~/.hevolve/models/minicpm/.
"""
import logging
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Dict, Optional

logger = logging.getLogger('hevolve_vision')

DEFAULT_MODEL_ID = 'openbmb/MiniCPM-V-2'
DEFAULT_MODEL_DIR = os.path.join(Path.home(), '.hevolve', 'models', 'minicpm')


class MiniCPMInstaller:
    """Auto-download and verify MiniCPM-V-2 model weights."""

    def __init__(self, model_id: str = DEFAULT_MODEL_ID,
                 model_dir: str = DEFAULT_MODEL_DIR):
        self.model_id = model_id
        self.model_dir = model_dir
        self._installed = False
        self._gpu_available = False

    def detect_gpu(self) -> bool:
        """Check if a compatible GPU is available (CUDA or Apple Metal/MPS)."""
        try:
            from integrations.service_tools.vram_manager import detect_gpu as _detect_gpu
            info = _detect_gpu()
            self._gpu_available = info.get('cuda_available', False) or info.get('metal_available', False)
            if info.get('name'):
                logger.info(f"GPU detected: {info['name']} ({info.get('total_gb', 0):.1f} GB)")
            elif not self._gpu_available:
                logger.warning("No compatible GPU detected — MiniCPM requires GPU")
            return self._gpu_available
        except ImportError:
            # Fallback: inline detection when running standalone
            try:
                import torch
                if torch.cuda.is_available():
                    self._gpu_available = True
                    name = torch.cuda.get_device_name(0)
                    mem = torch.cuda.get_device_properties(0).total_memory / (1024**3)
                    logger.info(f"CUDA GPU detected: {name} ({mem:.1f} GB)")
                elif hasattr(torch.backends, 'mps') and torch.backends.mps.is_available():
                    self._gpu_available = True
                    import platform
                    chip = platform.processor() or 'Apple Silicon'
                    logger.info(f"Apple Metal (MPS) detected: {chip}")
                else:
                    self._gpu_available = False
                    logger.warning("No compatible GPU detected — MiniCPM requires GPU")
                return self._gpu_available
            except ImportError:
                logger.warning("PyTorch not installed — cannot detect GPU")
                return False

    def is_installed(self) -> bool:
        """Check if model weights are already cached."""
        marker = os.path.join(self.model_dir, 'config.json')
        self._installed = os.path.isfile(marker)
        return self._installed

    def install(self, force: bool = False) -> bool:
        """Download MiniCPM-V-2 model to local cache.

        Uses huggingface_hub snapshot_download for efficient partial downloads.
        Returns True on success.
        """
        if self.is_installed() and not force:
            logger.info(f"MiniCPM already installed at {self.model_dir}")
            return True

        os.makedirs(self.model_dir, exist_ok=True)

        try:
            from huggingface_hub import snapshot_download
            logger.info(f"Downloading {self.model_id} to {self.model_dir}...")
            snapshot_download(
                repo_id=self.model_id,
                local_dir=self.model_dir,
                local_dir_use_symlinks=False,
            )
            self._installed = True
            logger.info("MiniCPM download complete")
            return True
        except ImportError:
            logger.error("huggingface_hub not installed. Run: pip install huggingface_hub")
            return False
        except Exception as e:
            logger.error(f"MiniCPM download failed: {e}")
            return False

    def uninstall(self) -> bool:
        """Remove cached model weights."""
        if os.path.isdir(self.model_dir):
            shutil.rmtree(self.model_dir)
            self._installed = False
            logger.info("MiniCPM model removed")
            return True
        return False

    def get_status(self) -> Dict:
        """Return installer status."""
        return {
            'model_id': self.model_id,
            'model_dir': self.model_dir,
            'installed': self.is_installed(),
            'gpu_available': self._gpu_available,
        }

    def get_model_dir(self) -> Optional[str]:
        """Return model directory if installed, else None."""
        if self.is_installed():
            return self.model_dir
        return None
