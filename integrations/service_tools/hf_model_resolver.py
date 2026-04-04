"""
HF Model Resolver — find and download the best GGUF quantization from HuggingFace.

Prefers Unsloth quantizations (fastest fine-tuning tool, best GGUF exports).
Auto-selects quantization level based on available VRAM.

Usage:
    resolver = HFModelResolver()
    path = resolver.resolve("Qwen/Qwen3-8B")  # Returns local GGUF path
    # Internally: finds unsloth/Qwen3-8B-GGUF, picks Q4_K_M for 8GB GPU, downloads
"""

import logging
import re
import threading
from pathlib import Path
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# ── Quantization constants ───────────────────────────────────────────

# Quantization preference order (best quality first)
QUANT_PREFERENCE = [
    'Q8_0', 'Q6_K_L', 'Q6_K', 'Q5_K_M', 'Q5_K_S',
    'Q4_K_L', 'Q4_K_M', 'Q4_K_S', 'IQ4_XS', 'Q4_0',
    'IQ3_M', 'IQ2_M', 'Q2_K',
]

# VRAM thresholds for auto-selection: (min_free_vram_gb, target_quant)
VRAM_QUANT_MAP: List[Tuple[float, str]] = [
    (24.0, 'Q8_0'),
    (16.0, 'Q6_K'),
    (8.0, 'Q4_K_M'),
    (4.0, 'Q4_K_S'),
    (0.0, 'Q4_0'),
]

# Regex to extract quant label from GGUF filenames.
# Matches patterns like: Q4_K_M, Q8_0, IQ4_XS, Q6_K_L, Q2_K, etc.
_QUANT_RE = re.compile(
    r'(?:^|[._-])'
    r'((?:IQ|Q)\d+(?:_K)?(?:_[A-Z0-9]+)?)'
    r'(?:[._-]|$)',
    re.IGNORECASE,
)


def _extract_quant(filename: str) -> Optional[str]:
    """Extract quantization label from a GGUF filename.

    Returns the quant string in upper-case (e.g. 'Q4_K_M') or None.
    """
    m = _QUANT_RE.search(filename)
    if m:
        return m.group(1).upper()
    return None


def _quant_rank(quant: str) -> int:
    """Return rank of a quant in QUANT_PREFERENCE (lower = better quality).

    Unknown quants get a high rank so known ones are preferred.
    """
    try:
        return QUANT_PREFERENCE.index(quant)
    except ValueError:
        return len(QUANT_PREFERENCE) + 1


class HFModelResolver:
    """Resolve HuggingFace model names to local GGUF file paths.

    Search strategy:
      1. Unsloth GGUF repos (preferred — best GGUF exports)
      2. Original org GGUF repos
      3. bartowski GGUF repos (popular community uploader)
      4. Original repo (may contain GGUF files directly)

    Auto-selects quantization based on available VRAM via vram_manager.
    Downloads to ~/.hevolve/models/gguf/{repo_safe_name}/.
    """

    def __init__(self):
        self._download_lock = threading.Lock()
        self._storage = None  # lazy
        self._hf_api = None   # lazy

    # ── Lazy accessors ───────────────────────────────────────────

    def _get_storage(self):
        """Lazy-load ModelStorageManager to avoid import cycles."""
        if self._storage is None:
            from .model_storage import ModelStorageManager
            self._storage = ModelStorageManager()
        return self._storage

    def _get_hf_api(self):
        """Lazy-load HfApi. Raises ImportError if huggingface_hub missing."""
        if self._hf_api is None:
            from huggingface_hub import HfApi
            self._hf_api = HfApi()
        return self._hf_api

    def _get_gpu_info(self) -> Dict:
        """Get GPU info from vram_manager singleton."""
        try:
            from .vram_manager import vram_manager
            return vram_manager.detect_gpu()
        except Exception as e:
            logger.debug(f"GPU detection unavailable: {e}")
            return {
                'cuda_available': False,
                'total_gb': 0.0,
                'free_gb': 0.0,
                'name': None,
            }

    # ── Main entry point ─────────────────────────────────────────

    def resolve(self, model_name: str, quant: str = 'auto') -> Path:
        """Resolve a HF model name to a local GGUF file path.

        Args:
            model_name: HuggingFace model identifier, e.g. "Qwen/Qwen3-8B"
                        or "meta-llama/Llama-3.1-8B".
            quant: Quantization level ('Q4_K_M', 'Q8_0', etc.) or 'auto'
                   to pick based on available VRAM.

        Returns:
            Path to the downloaded GGUF file on disk.

        Raises:
            FileNotFoundError: If no GGUF repo could be found.
            RuntimeError: If download fails.
            ImportError: If huggingface_hub is not installed.
        """
        logger.info(f"Resolving GGUF for {model_name} (quant={quant})")

        # Step 1: find a repo that has GGUF files
        repo_id = self.find_gguf_repo(model_name)
        logger.info(f"Found GGUF repo: {repo_id}")

        # Step 2: pick quantization
        filename = self.select_quantization(repo_id, quant)
        logger.info(f"Selected quantization file: {filename}")

        # Step 3: download if needed
        local_path = self.download(repo_id, filename)
        logger.info(f"GGUF ready at: {local_path}")

        return local_path

    # ── Repo discovery ───────────────────────────────────────────

    def find_gguf_repo(self, model_name: str) -> str:
        """Search for a GGUF repo for the given model.

        Search order (prefers Unsloth):
          1. unsloth/{basename}-GGUF
          2. {org}/{model}-GGUF
          3. bartowski/{basename}-GGUF
          4. {org}/{model} (original repo, check for .gguf files)

        Args:
            model_name: e.g. "Qwen/Qwen3-8B" or "meta-llama/Llama-3.1-8B"

        Returns:
            The repo_id string (e.g. "unsloth/Qwen3-8B-GGUF").

        Raises:
            FileNotFoundError: If no repo with GGUF files is found.
            ImportError: If huggingface_hub is not installed.
        """
        # Parse org/basename
        if '/' in model_name:
            org, basename = model_name.split('/', 1)
        else:
            org = None
            basename = model_name

        candidates = [
            f"unsloth/{basename}-GGUF",
        ]
        if org:
            candidates.append(f"{org}/{basename}-GGUF")
        candidates.append(f"bartowski/{basename}-GGUF")
        # Original repo as last resort
        if org:
            candidates.append(f"{org}/{basename}")
        else:
            candidates.append(basename)

        for repo_id in candidates:
            gguf_files = self._list_gguf_files(repo_id)
            if gguf_files:
                logger.info(
                    f"Found {len(gguf_files)} GGUF file(s) in {repo_id}"
                )
                return repo_id
            logger.debug(f"No GGUF files in {repo_id}")

        raise FileNotFoundError(
            f"No GGUF repository found for '{model_name}'. "
            f"Searched: {', '.join(candidates)}"
        )

    def _list_gguf_files(self, repo_id: str) -> List[str]:
        """List .gguf files in a HuggingFace repo.

        Returns an empty list if the repo does not exist or has no GGUF files.
        """
        try:
            api = self._get_hf_api()
            all_files = api.list_repo_files(repo_id)
            return [f for f in all_files if f.lower().endswith('.gguf')]
        except ImportError:
            raise
        except Exception as e:
            # Repo not found (404), rate limited, network error, etc.
            logger.debug(f"Could not list files in {repo_id}: {e}")
            return []

    # ── Quantization selection ───────────────────────────────────

    def select_quantization(self, repo_id: str, quant: str = 'auto') -> str:
        """Select the best GGUF file from a repo.

        If quant='auto', selects based on available VRAM:
            >= 24GB free: Q8_0
            >= 16GB free: Q6_K
            >=  8GB free: Q4_K_M
            >=  4GB free: Q4_K_S
            CPU only:     Q4_0

        If a specific quant is requested (e.g. 'Q4_K_M'), finds the closest
        available file.

        Args:
            repo_id: HuggingFace repo containing GGUF files.
            quant: 'auto' or a specific quant label.

        Returns:
            Filename of the selected GGUF file.

        Raises:
            FileNotFoundError: If no suitable GGUF file is found.
        """
        gguf_files = self._list_gguf_files(repo_id)
        if not gguf_files:
            raise FileNotFoundError(
                f"No GGUF files found in {repo_id}"
            )

        # Build a map of quant_label -> filename
        quant_map: Dict[str, str] = {}
        for fname in gguf_files:
            label = _extract_quant(fname)
            if label:
                # If multiple files have the same quant, prefer smaller
                # (single-file over split shards)
                if label not in quant_map or len(fname) < len(quant_map[label]):
                    quant_map[label] = fname

        if not quant_map:
            # No recognizable quant labels — return the first GGUF file
            logger.warning(
                f"No quant labels recognized in {repo_id}; "
                f"returning first GGUF file: {gguf_files[0]}"
            )
            return gguf_files[0]

        # Determine target quant
        if quant == 'auto':
            target = self._auto_select_quant()
            logger.info(f"Auto-selected target quant: {target}")
        else:
            target = quant.upper()

        # Exact match
        if target in quant_map:
            return quant_map[target]

        # Find the closest available quant by walking QUANT_PREFERENCE
        # from the target's position downward (lower quality), then upward.
        target_rank = _quant_rank(target)
        available = sorted(quant_map.keys(), key=_quant_rank)

        # Prefer the next-lower quality that exists
        best_file = None
        best_distance = float('inf')
        for q in available:
            distance = abs(_quant_rank(q) - target_rank)
            if distance < best_distance:
                best_distance = distance
                best_file = quant_map[q]
                if distance == 0:
                    break  # exact match

        if best_file is None:
            # Should not happen (quant_map is non-empty) but be safe
            best_file = next(iter(quant_map.values()))

        logger.info(
            f"Requested {target}, best available: "
            f"{_extract_quant(best_file)} -> {best_file}"
        )
        return best_file

    def _auto_select_quant(self) -> str:
        """Pick a quant target based on current free VRAM."""
        gpu_info = self._get_gpu_info()
        free_gb = gpu_info.get('free_gb', 0.0)

        if not gpu_info.get('cuda_available', False):
            logger.info("No GPU detected, targeting CPU-friendly Q4_0")
            return 'Q4_0'

        for threshold, quant in VRAM_QUANT_MAP:
            if free_gb >= threshold:
                logger.info(
                    f"Free VRAM: {free_gb:.1f} GB >= {threshold} GB, "
                    f"targeting {quant}"
                )
                return quant

        # Fallback (should not reach here since 0.0 is in the map)
        return 'Q4_0'

    @staticmethod
    def _validate_gguf(path: Path) -> bool:
        """Check GGUF magic bytes (0x47475546 = 'GGUF') at file start."""
        try:
            with open(path, 'rb') as f:
                magic = f.read(4)
                return magic == b'GGUF'
        except Exception:
            return False

    # ── Download ─────────────────────────────────────────────────

    def download(self, repo_id: str, filename: str) -> Path:
        """Download a GGUF file from HuggingFace.

        Downloads to ~/.hevolve/models/gguf/{repo_safe_name}/{filename}.
        Thread-safe: only one download runs at a time.
        Skips download if file already exists with non-zero size.
        Updates the ModelStorageManager manifest on success.

        Args:
            repo_id: HuggingFace repo, e.g. "unsloth/Qwen3-8B-GGUF".
            filename: GGUF filename within the repo.

        Returns:
            Path to the local GGUF file.

        Raises:
            RuntimeError: If the download fails.
            ImportError: If huggingface_hub is not installed.
        """
        # Build local path
        repo_safe = repo_id.replace('/', '--')
        gguf_dir = Path.home() / '.hevolve' / 'models' / 'gguf' / repo_safe
        local_path = gguf_dir / filename

        # Skip if already downloaded AND valid GGUF (magic bytes check)
        if local_path.exists() and local_path.stat().st_size > 0:
            if self._validate_gguf(local_path):
                logger.info(f"Already downloaded: {local_path}")
                return local_path
            else:
                logger.warning(f"Corrupt/partial GGUF detected, re-downloading: {local_path}")
                local_path.unlink(missing_ok=True)

        with self._download_lock:
            # Double-check after acquiring lock
            if local_path.exists() and local_path.stat().st_size > 0:
                if self._validate_gguf(local_path):
                    logger.info(f"Already downloaded (post-lock): {local_path}")
                    return local_path
                else:
                    local_path.unlink(missing_ok=True)

            gguf_dir.mkdir(parents=True, exist_ok=True)

            logger.info(
                f"Downloading {filename} from {repo_id} "
                f"to {gguf_dir}..."
            )

            try:
                from huggingface_hub import hf_hub_download

                downloaded_path = hf_hub_download(
                    repo_id=repo_id,
                    filename=filename,
                    local_dir=str(gguf_dir),
                    local_dir_use_symlinks=False,
                )
                downloaded_path = Path(downloaded_path)

                # hf_hub_download may place the file in a subfolder or
                # directly in local_dir — ensure we return the right path.
                if downloaded_path.exists():
                    actual_path = downloaded_path
                elif local_path.exists():
                    actual_path = local_path
                else:
                    raise RuntimeError(
                        f"Download completed but file not found at "
                        f"{downloaded_path} or {local_path}"
                    )

                size_bytes = actual_path.stat().st_size
                size_gb = size_bytes / (1024 ** 3)
                logger.info(
                    f"Download complete: {actual_path.name} "
                    f"({size_gb:.2f} GB)"
                )

                # Update manifest
                try:
                    storage = self._get_storage()
                    tool_name = f"gguf/{repo_safe}"
                    storage.mark_downloaded(
                        tool_name,
                        source_url=f"hf://{repo_id}/{filename}",
                        size_bytes=size_bytes,
                    )
                except Exception as e:
                    logger.warning(f"Manifest update failed: {e}")

                return actual_path

            except ImportError:
                raise ImportError(
                    "huggingface_hub is required for GGUF downloads. "
                    "Install it with: pip install huggingface_hub"
                )
            except Exception as e:
                logger.error(f"Download failed for {repo_id}/{filename}: {e}")
                raise RuntimeError(
                    f"Failed to download {filename} from {repo_id}: {e}"
                ) from e

    # ── Listing ──────────────────────────────────────────────────

    def list_available(self, model_name: str) -> List[Dict]:
        """List all available GGUF files for a model.

        Searches all candidate repos (Unsloth, original, bartowski) and
        returns a consolidated list of available files.

        Args:
            model_name: e.g. "Qwen/Qwen3-8B"

        Returns:
            List of dicts with keys:
              - repo_id: str
              - filename: str
              - quant: str or None
              - quant_rank: int (lower = better quality)
              - size_bytes: int or None (if available from API)
        """
        if '/' in model_name:
            org, basename = model_name.split('/', 1)
        else:
            org = None
            basename = model_name

        candidates = [f"unsloth/{basename}-GGUF"]
        if org:
            candidates.append(f"{org}/{basename}-GGUF")
        candidates.append(f"bartowski/{basename}-GGUF")
        if org:
            candidates.append(f"{org}/{basename}")

        results: List[Dict] = []
        seen_files = set()

        for repo_id in candidates:
            try:
                api = self._get_hf_api()
                repo_info = api.list_repo_tree(repo_id)
                for item in repo_info:
                    # item is a RepoFile or RepoFolder
                    fname = getattr(item, 'rfilename', None)
                    if fname is None:
                        # Might be a RepoFolder or different API object
                        fname = getattr(item, 'path', None)
                    if not fname or not fname.lower().endswith('.gguf'):
                        continue
                    # Deduplicate by filename
                    key = f"{repo_id}/{fname}"
                    if key in seen_files:
                        continue
                    seen_files.add(key)

                    quant = _extract_quant(fname)
                    size = getattr(item, 'size', None)

                    results.append({
                        'repo_id': repo_id,
                        'filename': fname,
                        'quant': quant,
                        'quant_rank': _quant_rank(quant) if quant else 999,
                        'size_bytes': size,
                    })
            except ImportError:
                raise
            except Exception as e:
                logger.debug(f"Could not list {repo_id}: {e}")

        # Sort by quant quality (best first)
        results.sort(key=lambda r: r['quant_rank'])
        return results


# ── Singleton ────────────────────────────────────────────────────────

_resolver: Optional[HFModelResolver] = None
_resolver_lock = threading.Lock()


def get_resolver() -> HFModelResolver:
    """Get or create the global HFModelResolver singleton."""
    global _resolver
    if _resolver is None:
        with _resolver_lock:
            if _resolver is None:
                _resolver = HFModelResolver()
    return _resolver
