"""
VRAM Manager — GPU memory tracking, allocation, and offload strategy.

Tracks which tools have reserved GPU memory and decides whether new
tools can fit. Provides offload mode suggestions (gpu / cpu_offload / cpu_only).

Pattern from: integrations/vision/minicpm_installer.py (detect_gpu)
              ltx2_server.py (VRAM stats, cpu_offload, tiling)
"""

import logging
import os
import sys
from typing import Any, Dict, Optional, Tuple

logger = logging.getLogger(__name__)

# VRAM budget table: tool_name -> (min_vram_gb, model_size_gb)
VRAM_BUDGETS: Dict[str, Tuple[float, float]] = {
    "acestep":              (6.0,  4.0),
    "diffrhythm":           (6.0,  4.0),    # singing voice synthesis
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
    "tts_f5":               (2.5,  1.3),    # English+Chinese, voice cloning
    "tts_indic_parler":     (2.0,  1.8),    # 21 Indic languages + English
    "tts_cosyvoice3":       (4.0,  3.5),    # zh/ja/ko/de/es/fr/it/ru, zero-shot
    "tts_chatterbox_ml":    (14.0, 12.0),   # 23 languages, needs 16GB+
    "tts_kokoro":           (0.5,  0.2),    # 82M neural English, CPU or GPU
    "tts_omnivoice":        (3.5,  3.0),    # 646 langs, Qwen3-0.6B+diffusion
                                             # — stub budget, auto-tightens
                                             # via record_actual_usage on
                                             # first successful load.
    # Mid-VRAM coverage tier (1–3 GB) — bridges the gap between F5/Indic
    # Parler/Kokoro (≤2.5 GB) and the heavy clone engines so EVERY
    # SUPPORTED_LANG_DICT code has at least one engine with vram_gb≤3.
    "tts_melotts":          (1.5,  1.0),    # en/es/fr/zh/ja/ko, neural CPU/GPU
    "tts_xtts_v2":          (2.5,  1.8),    # 17 langs, voice cloning (Coqui)
    "tts_mms_tts":          (1.0,  0.7),    # ~50+ langs (per-lang VITS, Meta)
}


class VRAMManager:
    """GPU memory tracking and allocation decisions."""

    def __init__(self):
        self._allocations: Dict[str, float] = {}  # tool → GB reserved
        self._gpu_info: Optional[Dict] = None
        self._gpu_info_ts: float = 0.0  # timestamp of last nvidia-smi call
        # Bundled mode: GPU state is stable (one model loaded at startup).
        # Poll every 120s not 30s to reduce subprocess overhead.
        _bundled = os.environ.get('NUNBA_BUNDLED') == '1'
        self._refresh_ttl: float = 120.0 if _bundled else 30.0
        # Serializes allocate() + can_fit() so two concurrent model loads
        # can't both pass can_fit() and overcommit the GPU.  Previously
        # an atomic-less read-modify-write across _allocations on hot
        # path (TOCTOU: read free → read budget → mutate dict).  Under
        # a cold startup with parallel LLM + TTS + VLM spawns, both
        # could see 5GB free, both think 4GB fits, both allocate → 8GB
        # claimed on a 5GB device → CUDA OOM.
        import threading as _threading  # noqa: E402  (runtime deferred)
        self._alloc_lock = _threading.RLock()

        # Measured VRAM usage telemetry: tool → actual model_size_gb seen
        # after a successful load.  Populated via record_actual_usage() —
        # worker subprocesses self-report post-load GPU usage, parent
        # stores the value and uses it in preference to the VRAM_BUDGETS
        # estimate the next time the tool is considered.  Enables
        # conservative stub budgets (e.g. new OmniVoice at 3.0 GB) to
        # auto-tighten after the first real load without a code change.
        self._measured: Dict[str, float] = {}
        self._measured_path = self._resolve_measured_path()
        self._load_measured()

    # ── Measured-usage telemetry ─────────────────────────────────

    @staticmethod
    def _resolve_measured_path():
        from pathlib import Path
        # Prefer the project agent_data dir, fall back to ~/.hevolve
        cwd_path = Path.cwd() / 'agent_data' / 'vram_measured.json'
        try:
            cwd_path.parent.mkdir(parents=True, exist_ok=True)
            return cwd_path
        except Exception:
            fallback = Path.home() / '.hevolve' / 'vram_measured.json'
            fallback.parent.mkdir(parents=True, exist_ok=True)
            return fallback

    def _load_measured(self) -> None:
        import json
        try:
            if self._measured_path.exists():
                data = json.loads(
                    self._measured_path.read_text(encoding='utf-8')
                )
                self._measured = {
                    str(k): float(v)
                    for k, v in data.items()
                    if isinstance(v, (int, float)) and v > 0
                }
        except Exception as e:
            logger.debug(f"VRAM measured load failed (ignoring): {e}")
            self._measured = {}

    def _persist_measured(self) -> None:
        """Atomic JSON write — tmp-then-rename so we can't half-write."""
        import json
        try:
            tmp = self._measured_path.with_suffix('.json.tmp')
            tmp.write_text(
                json.dumps(self._measured, indent=2),
                encoding='utf-8',
            )
            tmp.replace(self._measured_path)
        except Exception as e:
            logger.debug(f"VRAM measured persist failed: {e}")

    def record_actual_usage(self, tool_name: str, measured_gb: float) -> None:
        """Worker-reported post-load GPU usage.

        Called from ToolWorker._wait_ready after parsing the worker's
        '__WORKER_VRAM_GB__ <n>' marker.  Values are persisted so the
        measurement survives restarts and tightens the budget used by
        can_fit() / allocate() on subsequent loads.

        Safety rails:
          - Ignore non-positive values (worker emits 0.0 when it can't
            measure — e.g. CPU-only, Metal, broken nvidia-smi).
          - Clamp to [0.1, 64.0] GB — protects against obviously bad
            telemetry (negative deltas from concurrent workers, runaway
            leaks).
          - Compare vs VRAM_BUDGETS declared size — log a prominent
            warning if measured > declared * 1.5 (the declared budget
            is wrong and won't fit on the target GPU class).
        """
        with self._alloc_lock:
            if not tool_name or measured_gb is None:
                return
            try:
                gb = float(measured_gb)
            except (TypeError, ValueError):
                return
            if gb <= 0 or gb > 64.0:
                logger.debug(
                    f"VRAM measurement for {tool_name} out of range ({gb}) — ignored"
                )
                return
            prev = self._measured.get(tool_name)
            self._measured[tool_name] = round(gb, 2)
            self._persist_measured()

            declared = VRAM_BUDGETS.get(tool_name)
            if declared and gb > declared[1] * 1.5:
                logger.warning(
                    f"{tool_name} measured {gb:.1f} GB — 50%+ over declared "
                    f"{declared[1]:.1f} GB.  Consider raising VRAM_BUDGETS.  "
                    f"can_fit() will use the measurement from now on."
                )
            elif prev is None:
                logger.info(
                    f"{tool_name}: first measured VRAM = {gb:.2f} GB "
                    f"(budget was {declared[1] if declared else '—'} GB)"
                )

    def get_effective_budget(
        self,
        tool_name: str,
    ) -> Optional[Tuple[float, float]]:
        """Return (min_vram_gb, model_size_gb) using measured value if any.

        Measurement is tighter than the declared budget in the common case
        (stub budget is conservative), so we swap in the measured
        model_size.  When the measurement exceeds the declared model_size
        we honor the measurement — the tool really does need that much.

        min_vram (headroom) is never lowered below the declared minimum,
        because overhead like activation buffers is not captured in the
        static post-load measurement.
        """
        declared = VRAM_BUDGETS.get(tool_name)
        if not declared:
            return None
        measured_size = self._measured.get(tool_name)
        if measured_size is None:
            return declared
        min_vram, _declared_size = declared
        # Require at least measured_size + 0.3 GB overhead headroom
        effective_min = max(min_vram, measured_size + 0.3)
        return (effective_min, measured_size)

    def get_measured_usage(self) -> Dict[str, float]:
        """Return a copy of current measured-usage telemetry (tool → GB)."""
        return dict(self._measured)

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

        # run_bounded wraps Popen + explicit pipe close on timeout so the
        # child's _readerthread can't orphan — see core/subprocess_safe.py
        # for the failure mode (2026-04-15 wmic 27-min hang, same class).
        from core.subprocess_safe import run_bounded

        # nvidia-smi can be slow when the GPU is under heavy compute load
        # (driver call queues serialize behind kernel launches, NVML init
        # contends with active CUDA contexts).  5s was too tight on
        # 8GB systems running concurrent VLM benchmarks - hit the
        # subprocess_safe kill-pipes path every cycle and flooded the
        # log.  15s gives slow systems breathing room without leaving
        # zombie nvidia-smi processes around.  Override via env for
        # truly degraded systems.
        _nvsmi_timeout = float(os.environ.get(
            'HEVOLVE_NVIDIA_SMI_TIMEOUT', '15'))

        # 1) nvidia-smi — zero-dependency, works on any NVIDIA GPU system
        try:
            result = run_bounded(
                ["nvidia-smi", "--query-gpu=name,memory.total,memory.free",
                 "--format=csv,noheader,nounits"],
                timeout=_nvsmi_timeout,
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

        # 1b) rocm-smi — AMD GPUs via ROCm.  Same loaded-GPU rationale
        # as nvidia-smi above; honour the same env override.
        try:
            result = run_bounded(
                ["rocm-smi", "--showmeminfo", "vram", "--csv"],
                timeout=_nvsmi_timeout,
            )
            if result.returncode == 0 and result.stdout.strip():
                # Parse CSV output: header line then data lines
                lines = [l.strip() for l in result.stdout.strip().split("\n") if l.strip()]
                for line in lines[1:]:  # skip header
                    parts = [p.strip() for p in line.split(",")]
                    if len(parts) >= 3:
                        try:
                            total_bytes = float(parts[1])
                            used_bytes = float(parts[2])
                            total_gb = round(total_bytes / (1024 ** 3), 2)
                            free_gb = round((total_bytes - used_bytes) / (1024 ** 3), 2)
                            info.update({
                                "name": f"AMD GPU (ROCm)",
                                "total_gb": total_gb,
                                "free_gb": free_gb,
                                "cuda_available": False,
                                "rocm_available": True,
                            })
                            logger.info(
                                f"GPU (rocm-smi): {info['name']} — "
                                f"{info['total_gb']} GB total, {info['free_gb']} GB free"
                            )
                            self._gpu_info = info
                            return info
                        except (ValueError, IndexError):
                            continue
        except FileNotFoundError:
            pass  # rocm-smi not on PATH — no AMD GPU or ROCm drivers
        except Exception as e:
            logger.debug(f"rocm-smi failed: {e}")

        # 2) PyTorch — only if already imported (don't trigger a 2GB import)
        if "torch" in sys.modules:
            try:
                import torch
                # Detect frozen build torch stub (version 0.0.0, _is_stub=True).
                # Replace with real torch so CUDA detection works across all
                # deployments (Nunba frozen, HART OS standalone, cloud).
                if getattr(torch, '_is_stub', False):
                    import importlib
                    _stale = [k for k in sys.modules if k == 'torch' or k.startswith('torch.')]
                    for _k in _stale:
                        del sys.modules[_k]
                    torch = importlib.import_module('torch')
                    logger.info(f"Replaced torch stub with real torch {torch.__version__}")
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
        """Return free VRAM in GB — actual free from nvidia-smi.

        nvidia-smi already reports real free VRAM (total - all processes).
        Do NOT subtract our allocations — that double-counts and reports
        0GB when there's actually GB free, causing false OOM decisions.
        """
        info = self.detect_gpu()
        if not info["cuda_available"]:
            return 0.0
        return info["free_gb"]

    def get_total_vram(self) -> float:
        """Return total VRAM in GB."""
        return self.detect_gpu().get("total_gb", 0.0)

    # ── Allocation ───────────────────────────────────────────────

    def can_fit(self, tool_name: str) -> bool:
        """Check if a tool can fit in remaining VRAM.

        Uses the measured budget (post first successful load) if present,
        otherwise falls back to the VRAM_BUDGETS declared value.
        """
        if tool_name in self._allocations:
            return True  # already allocated
        effective = self.get_effective_budget(tool_name)
        if not effective:
            return True  # unknown tool — assume it fits
        min_vram, _model_size = effective
        gpu = self.detect_gpu()
        if not gpu["cuda_available"]:
            return False  # no GPU at all
        return self.get_free_vram() >= min_vram

    def allocate(self, tool_name: str) -> bool:
        """Reserve VRAM for a tool. Returns False if it won't fit.

        Lock-serialized: check-then-mutate must be atomic so two
        parallel allocations can't both win can_fit().  can_fit is
        called under the same RLock so the 'free' read sees prior
        pending allocations, not raw GPU stats.
        """
        with self._alloc_lock:
            if tool_name in self._allocations:
                return True
            if not self.can_fit(tool_name):
                logger.warning(f"VRAM rejected: {tool_name} won't fit "
                               f"(free={self.get_free_vram():.1f}GB)")
                return False
            effective = self.get_effective_budget(tool_name)
            model_gb = effective[1] if effective else 0.0
            self._allocations[tool_name] = model_gb
            logger.info(f"Allocated {model_gb} GB VRAM for {tool_name}")
            return True

    def release(self, tool_name: str) -> None:
        """Release VRAM reservation for a tool."""
        with self._alloc_lock:
            freed = self._allocations.pop(tool_name, 0.0)
            if freed:
                logger.info(f"Released {freed} GB VRAM from {tool_name}")

    def get_allocations(self) -> Dict[str, float]:
        """Return current VRAM allocations {tool → GB}."""
        return dict(self._allocations)

    def get_allocations_display(self) -> Dict[str, Any]:
        """Return VRAM allocations with rich model details for UI display.

        Each value is either a float (GB) for unknown models, or a dict with
        name, gb, device, and extra details (quant, context, mmproj) for known models.
        The frontend VRAMBar can render either format.
        """
        import re as _re
        raw = dict(self._allocations)
        try:
            from integrations.service_tools.model_catalog import get_catalog
            catalog = get_catalog()
            enriched = {}
            for key, gb in raw.items():
                display_key = key
                detail = {'gb': gb}
                for mid, entry in catalog._models.items():
                    if entry.loaded and (
                        mid == key or
                        entry.model_type == key or
                        mid.startswith(f'{key}-')
                    ):
                        display_key = entry.name
                        detail = {
                            'gb': gb,
                            'device': entry.device or 'gpu',
                            'backend': entry.backend,
                            'model_id': mid,
                        }
                        # Extract quant from filename (e.g. Q4_K_XL from Qwen3.5-4B-UD-Q4_K_XL.gguf)
                        fname = entry.files.get('model', '') or entry.files.get('file_name', '')
                        if not fname and entry.repo_id:
                            fname = entry.repo_id.split('/')[-1] if '/' in entry.repo_id else ''
                        quant_match = _re.search(r'(Q\d+_K(?:_[A-Z]+)?|F16|F32|INT[48]|GPTQ|AWQ)', fname, _re.I)
                        if quant_match:
                            detail['quant'] = quant_match.group(1)
                        # Context length from capabilities or tags
                        ctx = entry.capabilities.get('context_length') or entry.capabilities.get('n_ctx')
                        if ctx:
                            detail['context'] = ctx
                        # mmproj for vision models
                        if entry.capabilities.get('vision') or 'vision' in (entry.tags if hasattr(entry, 'tags') else []):
                            detail['vision'] = True
                        break
                enriched[display_key] = detail
            # NOTE: LLM quant/context/mmproj enrichment is handled by
            # Nunba's orchestrator shim (models/orchestrator.py), not here.
            # HARTOS must not import from Nunba (upward dependency).
            return enriched
        except Exception:
            return raw

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

    # ── Allocation drift detection ───────────────────────────────

    def detect_allocation_drift(self) -> Dict:
        """Compare advisory allocations vs actual VRAM usage.

        Returns drift info — positive drift means something is using
        more VRAM than we budgeted (possible leak or untracked process).
        """
        self.refresh_gpu_info()
        info = self._gpu_info or {}
        total = info.get('total_gb', 0)
        actual_free = info.get('free_gb', 0)
        actual_used = total - actual_free if total > 0 else 0

        advisory_used = sum(self._allocations.values())
        # Some baseline VRAM is always used by OS/drivers (~0.5-1.5GB typically)
        os_baseline = min(1.5, total * 0.1) if total > 0 else 0

        drift_gb = actual_used - advisory_used - os_baseline

        return {
            'actual_used_gb': round(actual_used, 2),
            'advisory_used_gb': round(advisory_used, 2),
            'os_baseline_gb': round(os_baseline, 2),
            'drift_gb': round(drift_gb, 2),
            'drift_pct': round((drift_gb / total * 100) if total > 0 else 0, 1),
            'untracked_process': drift_gb > 1.0,  # >1GB unaccounted = suspicious
        }

    # ── Dashboard ────────────────────────────────────────────────

    def get_status(self) -> Dict:
        """Full VRAM status for dashboard."""
        gpu = self.detect_gpu()
        drift = self.detect_allocation_drift()
        return {
            "gpu": gpu,
            "allocations": self.get_allocations_display(),
            "total_allocated_gb": round(sum(self._allocations.values()), 2),
            "effective_free_gb": round(self.get_free_vram(), 2),
            "drift": drift,
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
