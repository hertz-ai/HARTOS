"""
VRAM Manager — GPU memory tracking, allocation, and offload strategy.

Tracks which tools have reserved GPU memory and decides whether new
tools can fit. Provides offload mode suggestions (gpu / cpu_offload / cpu_only).

Pattern from: integrations/vision/minicpm_installer.py (detect_gpu)
              ltx2_server.py (VRAM stats, cpu_offload, tiling)
"""

import logging
from typing import Dict, Optional, Tuple

logger = logging.getLogger(__name__)

# VRAM budget table: tool_name -> (min_vram_gb, model_size_gb)
VRAM_BUDGETS: Dict[str, Tuple[float, float]] = {
    "acestep":          (6.0,  4.0),
    "wan2gp":           (8.0,  8.0),
    "whisper":          (2.0,  1.5),
    "tts_audio_suite":  (4.0,  2.0),
    "ltx2":             (6.0,  4.0),
    "minicpm":          (6.0,  4.0),
}


class VRAMManager:
    """GPU memory tracking and allocation decisions."""

    def __init__(self):
        self._allocations: Dict[str, float] = {}  # tool → GB reserved
        self._gpu_info: Optional[Dict] = None

    # ── GPU Detection ────────────────────────────────────────────

    def detect_gpu(self) -> Dict:
        """Detect GPU and return info dict.

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

        try:
            import torch
            if torch.cuda.is_available():
                props = torch.cuda.get_device_properties(0)
                total = props.total_mem / (1024 ** 3)
                allocated = torch.cuda.memory_allocated(0) / (1024 ** 3)
                reserved = torch.cuda.memory_reserved(0) / (1024 ** 3)
                info.update({
                    "name": torch.cuda.get_device_name(0),
                    "total_gb": round(total, 2),
                    "free_gb": round(total - allocated, 2),
                    "cuda_available": True,
                })
                logger.info(
                    f"GPU: {info['name']} — {info['total_gb']} GB total, "
                    f"{info['free_gb']} GB free"
                )
        except ImportError:
            logger.warning("PyTorch not installed — GPU detection unavailable")
        except Exception as e:
            logger.warning(f"GPU detection failed: {e}")

        self._gpu_info = info
        return info

    def refresh_gpu_info(self) -> Dict:
        """Force re-detect GPU (clears cache)."""
        self._gpu_info = None
        return self.detect_gpu()

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
