"""
VRAM Manager — GPU memory tracking, allocation, and offload strategy.

Tracks which tools have reserved GPU memory and decides whether new
tools can fit. Provides offload mode suggestions (gpu / cpu_offload / cpu_only).

Pattern from: integrations/vision/minicpm_installer.py (detect_gpu)
              ltx2_server.py (VRAM stats, cpu_offload, tiling)
"""

import logging
import subprocess
import sys
from typing import Dict, Optional, Tuple

logger = logging.getLogger(__name__)

# VRAM budget table: tool_name -> (min_vram_gb, model_size_gb)
VRAM_BUDGETS: Dict[str, Tuple[float, float]] = {
    "acestep":              (6.0,  4.0),
    "wan2gp":               (8.0,  8.0),
    "ltx2":                 (6.0,  4.0),
    "minicpm":              (6.0,  4.0),
    # STT engines
    "whisper":              (2.0,  1.5),
    "whisper_base":         (0.5,  0.2),    # faster-whisper base (CPU-friendly)
    "whisper_medium":       (2.0,  1.5),    # faster-whisper medium
    "whisper_large":        (4.0,  3.0),    # faster-whisper large-v3-turbo
    # TTS engines
    "tts_chatterbox_turbo": (5.6,  3.8),    # English, [laugh]/[chuckle] tags
    "tts_f5":               (2.0,  1.3),    # English+Chinese, voice cloning
    "tts_indic_parler":     (2.0,  1.8),    # 21 Indic languages + English
    "tts_cosyvoice3":       (4.0,  3.5),    # zh/ja/ko/de/es/fr/it/ru, zero-shot
    "tts_chatterbox_ml":    (14.0, 12.0),   # 23 languages, needs 16GB+
}


class VRAMManager:
    """GPU memory tracking and allocation decisions."""

    def __init__(self):
        self._allocations: Dict[str, float] = {}  # tool → GB reserved
        self._gpu_info: Optional[Dict] = None
        self._gpu_info_ts: float = 0.0  # timestamp of last nvidia-smi call
        self._refresh_ttl: float = 30.0  # seconds between nvidia-smi calls

    # ── GPU Detection ────────────────────────────────────────────

    def detect_gpu(self) -> Dict:
        """Detect GPU and return info dict.

        Priority: nvidia-smi (no deps) → PyTorch (if already loaded) → macOS Metal.
        Returns: {name, total_gb, free_gb, cuda_available}
        """
        if self._gpu_info is not None:
            return self._gpu_info

        info = {
            "name": None,
            "total_gb": 0.0,
            "free_gb": 0.0,
            "cuda_available": False,
        }

        # 1) nvidia-smi — zero-dependency, works on any NVIDIA GPU system
        try:
            _smi_kwargs = dict(capture_output=True, text=True, timeout=5)
            if sys.platform == 'win32':
                si = subprocess.STARTUPINFO()
                si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
                si.wShowWindow = 0
                _smi_kwargs['startupinfo'] = si
                _smi_kwargs['creationflags'] = subprocess.CREATE_NO_WINDOW
            result = subprocess.run(
                ["nvidia-smi", "--query-gpu=name,memory.total,memory.free",
                 "--format=csv,noheader,nounits"],
                **_smi_kwargs,
            )
            if result.returncode == 0 and result.stdout.strip():
                line = result.stdout.strip().split("\n")[0]
                parts = [p.strip() for p in line.split(",")]
                if len(parts) >= 3:
                    total_mb = float(parts[1])
                    free_mb = float(parts[2])
                    info.update({
                        "name": parts[0],
                        "total_gb": round(total_mb / 1024, 2),
                        "free_gb": round(free_mb / 1024, 2),
                        "cuda_available": True,
                    })
                    logger.info(
                        f"GPU (nvidia-smi): {info['name']} — "
                        f"{info['total_gb']} GB total, {info['free_gb']} GB free"
                    )
                    self._gpu_info = info
                    return info
        except FileNotFoundError:
            pass  # nvidia-smi not on PATH — no NVIDIA GPU or drivers
        except Exception as e:
            logger.debug(f"nvidia-smi failed: {e}")

        # 2) PyTorch — only if already imported (don't trigger a 2GB import)
        if "torch" in sys.modules:
            try:
                import torch
                if torch.cuda.is_available():
                    props = torch.cuda.get_device_properties(0)
                    total = props.total_memory / (1024 ** 3)
                    allocated = torch.cuda.memory_allocated(0) / (1024 ** 3)
                    info.update({
                        "name": torch.cuda.get_device_name(0),
                        "total_gb": round(total, 2),
                        "free_gb": round(total - allocated, 2),
                        "cuda_available": True,
                    })
                    logger.info(
                        f"GPU (PyTorch): {info['name']} — "
                        f"{info['total_gb']} GB total, {info['free_gb']} GB free"
                    )
                    self._gpu_info = info
                    return info
            except Exception as e:
                logger.debug(f"PyTorch GPU detection failed: {e}")

        # 3) macOS Metal
        if sys.platform == "darwin":
            try:
                import platform
                info.update({
                    "name": f"Apple Metal ({'Apple Silicon' if platform.machine() == 'arm64' else 'Intel'})",
                    "total_gb": 0.0,  # shared memory — hard to measure
                    "free_gb": 0.0,
                    "cuda_available": False,
                    "metal_available": True,
                })
            except Exception:
                pass

        if not info["cuda_available"]:
            logger.info("No NVIDIA GPU detected (nvidia-smi not found or no CUDA device)")

        self._gpu_info = info
        return info

    def refresh_gpu_info(self) -> Dict:
        """Re-detect GPU with TTL cache (avoids nvidia-smi spam from multiple threads)."""
        import time as _t
        now = _t.monotonic()
        if self._gpu_info is not None and (now - self._gpu_info_ts) < self._refresh_ttl:
            return self._gpu_info  # recent enough — skip subprocess
        self._gpu_info = None
        result = self.detect_gpu()
        self._gpu_info_ts = _t.monotonic()
        return result

    # ── VRAM queries ─────────────────────────────────────────────

    def get_free_vram(self) -> float:
        """Return free VRAM in GB (accounting for our allocations)."""
        info = self.detect_gpu()
        if not info["cuda_available"]:
            return 0.0
        used_by_us = sum(self._allocations.values())
        return max(0.0, info["free_gb"] - used_by_us)

    def get_total_vram(self) -> float:
        """Return total VRAM in GB."""
        return self.detect_gpu().get("total_gb", 0.0)

    # ── Allocation ───────────────────────────────────────────────

    def can_fit(self, tool_name: str) -> bool:
        """Check if a tool can fit in remaining VRAM."""
        if tool_name in self._allocations:
            return True  # already allocated
        budget = VRAM_BUDGETS.get(tool_name)
        if not budget:
            return True  # unknown tool — assume it fits
        min_vram, model_size = budget
        gpu = self.detect_gpu()
        if not gpu["cuda_available"]:
            return False  # no GPU at all
        return self.get_free_vram() >= model_size

    def allocate(self, tool_name: str) -> bool:
        """Reserve VRAM for a tool. Returns True if allocated."""
        if tool_name in self._allocations:
            return True
        budget = VRAM_BUDGETS.get(tool_name)
        model_gb = budget[1] if budget else 0.0
        self._allocations[tool_name] = model_gb
        logger.info(f"Allocated {model_gb} GB VRAM for {tool_name}")
        return True

    def release(self, tool_name: str) -> None:
        """Release VRAM reservation for a tool."""
        freed = self._allocations.pop(tool_name, 0.0)
        if freed:
            logger.info(f"Released {freed} GB VRAM from {tool_name}")

    def get_allocations(self) -> Dict[str, float]:
        """Return current VRAM allocations {tool → GB}."""
        return dict(self._allocations)

    # ── Offload strategy ─────────────────────────────────────────

    def suggest_offload_mode(self, tool_name: str) -> str:
        """Suggest the best offload mode for a tool.

        Returns: 'gpu' | 'cpu_offload' | 'cpu_only'
        """
        gpu = self.detect_gpu()
        if not gpu["cuda_available"]:
            return "cpu_only"

        budget = VRAM_BUDGETS.get(tool_name)
        if not budget:
            return "gpu"  # unknown tool, try GPU

        min_vram, model_size = budget
        free = self.get_free_vram()

        if free >= model_size:
            return "gpu"
        elif free >= model_size * 0.5:
            return "cpu_offload"
        else:
            return "cpu_only"

    # ── Pressure detection ────────────────────────────────────────

    def get_actual_free_vram(self) -> float:
        """Return ACTUAL free VRAM by refreshing nvidia-smi (not cached advisory).

        Unlike get_free_vram(), this re-reads hardware state every call.
        Used by ModelLifecycleManager for real-time pressure detection.
        """
        self.refresh_gpu_info()
        info = self._gpu_info or {}
        return info.get('free_gb', 0.0)

    def get_vram_usage_pct(self) -> float:
        """Return current VRAM usage as percentage (0-100).

        Refreshes GPU info first for accuracy.
        """
        self.refresh_gpu_info()
        info = self._gpu_info or {}
        total = info.get('total_gb', 0)
        free = info.get('free_gb', 0)
        if total <= 0:
            return 0.0
        return ((total - free) / total) * 100

    # ── CUDA Cache Clearing ─────────────────────────────────────

    @staticmethod
    def clear_cuda_cache() -> bool:
        """Clear GPU cache (CUDA or MPS) if torch is loaded. Returns True if cleared."""
        if 'torch' in sys.modules:
            try:
                import torch
                if torch.cuda.is_available():
                    torch.cuda.empty_cache()
                    return True
                elif hasattr(torch.backends, 'mps') and torch.backends.mps.is_available():
                    torch.mps.empty_cache()
                    return True
            except Exception:
                pass
        return False

    # ── Dashboard ────────────────────────────────────────────────

    def get_status(self) -> Dict:
        """Full VRAM status for dashboard."""
        gpu = self.detect_gpu()
        return {
            "gpu": gpu,
            "allocations": self.get_allocations(),
            "total_allocated_gb": round(sum(self._allocations.values()), 2),
            "effective_free_gb": round(self.get_free_vram(), 2),
        }


# Global singleton
vram_manager = VRAMManager()


# ── Module-level convenience functions ──────────────────────────
# Allow: from integrations.service_tools.vram_manager import detect_gpu, clear_cuda_cache

def detect_gpu() -> Dict:
    """Detect GPU via the singleton VRAMManager. See VRAMManager.detect_gpu."""
    return vram_manager.detect_gpu()


def clear_cuda_cache() -> bool:
    """Clear GPU cache via the singleton VRAMManager. See VRAMManager.clear_cuda_cache."""
    return VRAMManager.clear_cuda_cache()


def get_vram_manager() -> VRAMManager:
    """Return the global VRAMManager singleton."""
    return vram_manager
