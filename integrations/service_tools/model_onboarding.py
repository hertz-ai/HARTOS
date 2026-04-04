"""
Model Onboarding — one-command flow to go from model name to running inference.

    onboard("Qwen/Qwen3-8B")
    # 1. Finds unsloth/Qwen3-8B-GGUF on HuggingFace
    # 2. Picks Q4_K_M quantization for user's GPU
    # 3. Downloads GGUF to ~/.hevolve/models/
    # 4. Ensures llama.cpp binary is available
    # 5. Starts llama-server with optimal params
    # 6. Registers model in catalog + registry
    # 7. Returns endpoint URL ready for inference

Also provides CLI/API for listing available models, switching active model,
and removing downloaded models.
"""

import logging
import re
import threading
import time
from pathlib import Path
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)


# ── Nunba companion detection ──────────────────────────────────────

def _is_nunba_bundled() -> bool:
    """Detect if Nunba (companion desktop app) is managing llama.cpp.

    When HARTOS is pip-installed inside Nunba, `hartos_backend_adapter` is
    in sys.modules. Nunba owns: llama.cpp lifecycle, model downloads,
    config.json, port 8080. We should not duplicate that work.
    """
    import sys
    return 'hartos_backend_adapter' in sys.modules


def _onboard_via_nunba(model_name: str, quant: str, port: int) -> dict:
    """Onboard a model by delegating to Nunba's existing infrastructure.

    Nunba already has llama.cpp running on port 8080. We just need to
    tell it to load a different model via its adapter, or check if
    the requested model is already active.
    """
    import urllib.request
    import urllib.error
    import json

    # Check if Nunba's llama.cpp is already running
    llm_port = 8080
    try:
        req = urllib.request.urlopen(f'http://127.0.0.1:{llm_port}/health', timeout=3)
        if req.status == 200:
            logger.info("Nunba's llama.cpp already running on port %d", llm_port)
            return {
                'status': 'ready',
                'model': model_name,
                'quant': quant,
                'endpoint': f'http://127.0.0.1:{llm_port}',
                'source': 'nunba',
                'note': 'Nunba manages the llama.cpp server. '
                        'Use Nunba settings to change models.',
            }
    except (urllib.error.URLError, OSError):
        pass

    # Nunba not running yet — tell the user
    return {
        'status': 'waiting',
        'model': model_name,
        'endpoint': f'http://127.0.0.1:{llm_port}',
        'source': 'nunba',
        'note': 'HARTOS is bundled with Nunba. Start the Nunba desktop app '
                'to activate llama.cpp inference. Nunba manages model lifecycle.',
    }


# ── Module-level state ──────────────────────────────────────────────

_onboard_lock = threading.Lock()
_active_model: Optional[Dict] = None  # tracks the currently running model


# ── Lazy imports (all behind try/except) ────────────────────────────

def _get_resolver():
    """Lazy-load HFModelResolver singleton."""
    try:
        from integrations.service_tools.hf_model_resolver import get_resolver
        return get_resolver()
    except ImportError:
        logger.warning("hf_model_resolver not available")
        return None


def _get_llamacpp_manager():
    """Lazy-load llamacpp_manager singleton."""
    try:
        from integrations.service_tools.llamacpp_manager import get_llamacpp_manager
        return get_llamacpp_manager()
    except ImportError:
        logger.warning("llamacpp_manager not available")
        return None


def _get_catalog():
    """Lazy-load ModelCatalog singleton."""
    try:
        from integrations.service_tools.model_catalog import get_catalog
        return get_catalog()
    except ImportError:
        logger.warning("model_catalog not available")
        return None


def _get_model_registry():
    """Lazy-load ModelRegistry singleton."""
    try:
        from integrations.agent_engine.model_registry import model_registry
        return model_registry
    except ImportError:
        logger.warning("model_registry not available")
        return None


def _get_vram_manager():
    """Lazy-load VRAMManager singleton."""
    try:
        from integrations.service_tools.vram_manager import vram_manager
        return vram_manager
    except ImportError:
        logger.warning("vram_manager not available")
        return None


def _get_default_port() -> int:
    """Get the llama.cpp port from port_registry, default 8080."""
    try:
        from core.port_registry import get_port
        return get_port('llm')
    except Exception:
        return 8080


def _make_catalog_id(model_name: str, quant: str) -> str:
    """Create a stable catalog ID from model name and quantization.

    E.g. "Qwen/Qwen3-8B" + "Q4_K_M" -> "llm-qwen3-8b-q4-k-m"
    """
    # Take basename from repo-style names
    if '/' in model_name:
        basename = model_name.split('/')[-1]
    else:
        basename = model_name
    slug = re.sub(r'[^a-z0-9]+', '-', basename.lower()).strip('-')
    quant_slug = re.sub(r'[^a-z0-9]+', '-', quant.lower()).strip('-')
    return f"llm-{slug}-{quant_slug}"


def _extract_quant_from_path(gguf_path: Path) -> str:
    """Extract quantization label from a GGUF filename."""
    try:
        from integrations.service_tools.hf_model_resolver import _extract_quant
        q = _extract_quant(gguf_path.name)
        if q:
            return q
    except ImportError:
        pass
    # Fallback regex
    m = re.search(r'((?:IQ|Q)\d+(?:_K)?(?:_[A-Z0-9]+)?)', gguf_path.name, re.IGNORECASE)
    return m.group(1).upper() if m else 'unknown'


# ── Core functions ──────────────────────────────────────────────────

def onboard(model_name: str, quant: str = 'auto', port: int = 0) -> Dict:
    """Full onboarding pipeline: resolve, download, start, register.

    Args:
        model_name: HuggingFace model identifier (e.g. "Qwen/Qwen3-8B").
        quant: Quantization level ('Q4_K_M', 'Q8_0', etc.) or 'auto'.
        port: Port for llama-server. 0 = use port registry default.

    Returns:
        Status dict with keys: status, model, quant, endpoint, gguf_path.
        On error: status='error', error=<message>.
    """
    global _active_model

    if port == 0:
        port = _get_default_port()

    # ── Nunba companion detection ──
    # When Nunba (sibling repo) is installed, it owns the llama.cpp lifecycle
    # and model management. We defer to it instead of duplicating.
    if _is_nunba_bundled():
        return _onboard_via_nunba(model_name, quant, port)

    with _onboard_lock:
        try:
            # Step 1: Resolve and download GGUF
            resolver = _get_resolver()
            if resolver is None:
                return {
                    'status': 'error',
                    'error': 'hf_model_resolver is not available. '
                             'Install huggingface_hub: pip install huggingface_hub',
                }

            logger.info(f"Onboarding {model_name} (quant={quant}, port={port})")
            gguf_path = resolver.resolve(model_name, quant)
            quant_used = _extract_quant_from_path(gguf_path)

            # Step 2: Ensure llama.cpp binary is available
            lcpp = _get_llamacpp_manager()
            if lcpp is None:
                return {
                    'status': 'error',
                    'error': 'llamacpp_manager is not available',
                }

            server_bin = lcpp.get_server_binary()
            if server_bin is None:
                logger.info("llama-server binary not found, downloading...")
                lcpp.download_server()
                server_bin = lcpp.get_server_binary()
                if server_bin is None:
                    return {
                        'status': 'error',
                        'error': 'Failed to obtain llama-server binary',
                    }

            # Step 3: Start llama-server
            logger.info(f"Starting llama-server on port {port}...")
            if not lcpp.start(str(gguf_path), port):
                return {
                    'status': 'error',
                    'error': f'llama-server failed to start on port {port}. '
                             f'Check if the port is in use or the GGUF file is valid.',
                }

            # Step 4: Register in catalog
            catalog_id = _make_catalog_id(model_name, quant_used)
            _register_in_catalog(catalog_id, model_name, quant_used, gguf_path, port)

            # Step 5: Register in model registry
            _register_in_registry(catalog_id, model_name, port)

            # Step 6: Track active model
            endpoint = f'http://127.0.0.1:{port}'
            _active_model = {
                'catalog_id': catalog_id,
                'model': model_name,
                'quant': quant_used,
                'endpoint': endpoint,
                'gguf_path': str(gguf_path),
                'port': port,
                'started_at': time.time(),
            }

            result = {
                'status': 'ready',
                'model': model_name,
                'quant': quant_used,
                'endpoint': endpoint,
                'gguf_path': str(gguf_path),
            }
            logger.info(f"Onboarding complete: {model_name} ({quant_used}) at {endpoint}")
            return result

        except FileNotFoundError as e:
            logger.error(f"Onboard failed — model not found: {e}")
            return {'status': 'error', 'error': str(e)}
        except ImportError as e:
            logger.error(f"Onboard failed — missing dependency: {e}")
            return {'status': 'error', 'error': str(e)}
        except Exception as e:
            logger.error(f"Onboard failed: {e}", exc_info=True)
            return {'status': 'error', 'error': str(e)}


def switch_model(model_name: str, quant: str = 'auto') -> Dict:
    """Hot-swap the active model without full restart.

    Resolves and downloads if needed, then calls llamacpp_manager.swap_model().
    Updates catalog and registry entries.

    Args:
        model_name: HuggingFace model identifier.
        quant: Quantization level or 'auto'.

    Returns:
        Status dict. On error: status='error', error=<message>.
    """
    global _active_model

    with _onboard_lock:
        try:
            # Resolve + download
            resolver = _get_resolver()
            if resolver is None:
                return {
                    'status': 'error',
                    'error': 'hf_model_resolver is not available',
                }

            gguf_path = resolver.resolve(model_name, quant)
            quant_used = _extract_quant_from_path(gguf_path)

            # Swap model in running server
            lcpp = _get_llamacpp_manager()
            if lcpp is None:
                return {
                    'status': 'error',
                    'error': 'llamacpp_manager is not available',
                }

            logger.info(f"Swapping to {model_name} ({quant_used})...")
            lcpp.swap_model(str(gguf_path))

            # Unmark previous active model in catalog
            if _active_model:
                catalog = _get_catalog()
                if catalog:
                    catalog.mark_unloaded(_active_model.get('catalog_id', ''))

            # Register new model
            catalog_id = _make_catalog_id(model_name, quant_used)
            port = (_active_model or {}).get('port', _get_default_port())
            _register_in_catalog(catalog_id, model_name, quant_used, gguf_path, port)
            _register_in_registry(catalog_id, model_name, port)

            endpoint = f'http://127.0.0.1:{port}'
            _active_model = {
                'catalog_id': catalog_id,
                'model': model_name,
                'quant': quant_used,
                'endpoint': endpoint,
                'gguf_path': str(gguf_path),
                'port': port,
                'started_at': time.time(),
            }

            result = {
                'status': 'ready',
                'model': model_name,
                'quant': quant_used,
                'endpoint': endpoint,
                'gguf_path': str(gguf_path),
            }
            logger.info(f"Model swap complete: {model_name} ({quant_used})")
            return result

        except FileNotFoundError as e:
            logger.error(f"Switch failed — model not found: {e}")
            return {'status': 'error', 'error': str(e)}
        except Exception as e:
            logger.error(f"Switch failed: {e}", exc_info=True)
            return {'status': 'error', 'error': str(e)}


def list_downloaded() -> List[Dict]:
    """List all downloaded GGUF models with their sizes, quant types, and paths.

    Returns:
        List of dicts with keys: filename, quant, size_bytes, size_gb, path, repo.
    """
    gguf_dir = Path.home() / '.hevolve' / 'models' / 'gguf'
    if not gguf_dir.exists():
        return []

    results = []
    for repo_dir in gguf_dir.iterdir():
        if not repo_dir.is_dir():
            continue
        for gguf_file in repo_dir.glob('*.gguf'):
            try:
                size_bytes = gguf_file.stat().st_size
            except OSError:
                size_bytes = 0

            quant_label = _extract_quant_from_path(gguf_file)
            # Convert repo dir name back to repo_id
            repo_name = repo_dir.name.replace('--', '/')

            results.append({
                'filename': gguf_file.name,
                'quant': quant_label,
                'size_bytes': size_bytes,
                'size_gb': round(size_bytes / (1024 ** 3), 2),
                'path': str(gguf_file),
                'repo': repo_name,
            })

    # Sort by size descending (biggest models first)
    results.sort(key=lambda x: x['size_bytes'], reverse=True)
    return results


def list_available(model_name: str) -> List[Dict]:
    """List available GGUF files for a model on HuggingFace.

    Proxy to hf_model_resolver.list_available().

    Args:
        model_name: e.g. "Qwen/Qwen3-8B"

    Returns:
        List of dicts with keys: repo_id, filename, quant, quant_rank, size_bytes.
        On error: empty list.
    """
    resolver = _get_resolver()
    if resolver is None:
        return []
    try:
        return resolver.list_available(model_name)
    except ImportError as e:
        logger.warning(f"Cannot list available models: {e}")
        return []
    except Exception as e:
        logger.error(f"Error listing available models: {e}")
        return []


def remove_model(model_id: str) -> bool:
    """Remove a downloaded GGUF model and its catalog entry.

    Args:
        model_id: Either a catalog ID (e.g. "llm-qwen3-8b-q4-k-m") or
                  a GGUF filename (e.g. "Qwen3-8B-Q4_K_M.gguf").

    Returns:
        True if something was removed, False otherwise.
    """
    global _active_model
    removed = False

    # If active model matches, stop tracking
    if _active_model and _active_model.get('catalog_id') == model_id:
        _active_model = None

    # Remove from catalog
    catalog = _get_catalog()
    if catalog:
        if catalog.unregister(model_id):
            removed = True

    # Try to find and delete the GGUF file
    gguf_dir = Path.home() / '.hevolve' / 'models' / 'gguf'
    if gguf_dir.exists():
        for gguf_file in gguf_dir.rglob('*.gguf'):
            # Match by filename or by catalog_id derived from filename
            if (gguf_file.name == model_id or
                    _make_catalog_id(
                        gguf_file.parent.name.replace('--', '/'),
                        _extract_quant_from_path(gguf_file)
                    ) == model_id):
                try:
                    gguf_file.unlink()
                    logger.info(f"Deleted GGUF file: {gguf_file}")
                    removed = True
                    # Clean up empty parent directory
                    try:
                        if not any(gguf_file.parent.iterdir()):
                            gguf_file.parent.rmdir()
                    except OSError:
                        pass
                except OSError as e:
                    logger.error(f"Failed to delete {gguf_file}: {e}")

    # Remove from storage manifest
    try:
        from integrations.service_tools.model_storage import model_storage
        # Check all gguf/* entries in the manifest
        manifest = model_storage.get_manifest()
        for tool_name in list(manifest.get('tools', {}).keys()):
            if tool_name.startswith('gguf/') and model_id in tool_name:
                model_storage.remove_tool(tool_name)
                removed = True
    except ImportError:
        pass

    if removed:
        logger.info(f"Removed model: {model_id}")
    else:
        logger.warning(f"Model not found for removal: {model_id}")

    return removed


def get_active_model() -> Optional[Dict]:
    """Return info about the currently running model, or None if nothing is active.

    Returns:
        Dict with keys: catalog_id, model, quant, endpoint, gguf_path, port,
        started_at, uptime_s.
    """
    if _active_model is None:
        return None

    result = dict(_active_model)
    result['uptime_s'] = round(time.time() - result.get('started_at', time.time()), 1)
    return result


def status() -> Dict:
    """Return full onboarding status: active model, server health, VRAM, downloads.

    Returns:
        Dict with keys: active_model, server_healthy, vram, downloaded_count,
        downloaded_size_gb.
    """
    result = {
        'active_model': get_active_model(),
        'server_healthy': False,
        'vram': {},
        'downloaded_count': 0,
        'downloaded_size_gb': 0.0,
    }

    # Server health check
    lcpp = _get_llamacpp_manager()
    if lcpp and _active_model:
        try:
            # Try to check if the server process is running
            healthy = lcpp.is_running() if hasattr(lcpp, 'is_running') else False
            result['server_healthy'] = healthy
        except Exception:
            result['server_healthy'] = False

    # VRAM info
    vm = _get_vram_manager()
    if vm:
        try:
            gpu_info = vm.detect_gpu()
            result['vram'] = {
                'gpu_name': gpu_info.get('name'),
                'total_gb': gpu_info.get('total_gb', 0.0),
                'free_gb': gpu_info.get('free_gb', 0.0),
                'cuda_available': gpu_info.get('cuda_available', False),
            }
        except Exception:
            pass

    # Downloaded models
    downloaded = list_downloaded()
    result['downloaded_count'] = len(downloaded)
    result['downloaded_size_gb'] = round(
        sum(m.get('size_gb', 0.0) for m in downloaded), 2
    )

    return result


# ── Registration helpers ────────────────────────────────────────────

def _register_in_catalog(catalog_id: str, model_name: str, quant: str,
                         gguf_path: Path, port: int) -> None:
    """Register or update a model entry in the ModelCatalog."""
    catalog = _get_catalog()
    if catalog is None:
        return

    try:
        from integrations.service_tools.model_catalog import ModelEntry, ModelType
    except ImportError:
        return

    # Human-readable display name
    if '/' in model_name:
        display_name = model_name.split('/')[-1]
    else:
        display_name = model_name

    # Calculate size for disk_gb
    try:
        disk_gb = round(gguf_path.stat().st_size / (1024 ** 3), 2)
    except OSError:
        disk_gb = 0.0

    entry = ModelEntry(
        id=catalog_id,
        name=f"{display_name} ({quant})",
        model_type=ModelType.LLM,
        source='huggingface',
        repo_id=model_name,
        files={'model': gguf_path.name, 'path': str(gguf_path)},
        disk_gb=disk_gb,
        backend='llama.cpp',
        supports_gpu=True,
        supports_cpu=True,
        quality_score=0.7,
        speed_score=0.8,
        cost_per_1k=0.0,
        tags=['local', 'gguf', 'onboarded', quant.lower()],
        capabilities={
            'quant': quant,
            'endpoint': f'http://127.0.0.1:{port}',
            'openai_compatible': True,
        },
    )

    catalog.register(entry)
    catalog.mark_downloaded(catalog_id)
    catalog.mark_loaded(catalog_id, device='gpu')
    logger.info(f"Registered {catalog_id} in model catalog")


def _register_in_registry(catalog_id: str, model_name: str, port: int) -> None:
    """Register a ModelBackend in the ModelRegistry for LLM routing."""
    registry = _get_model_registry()
    if registry is None:
        return

    try:
        from integrations.agent_engine.model_registry import ModelBackend, ModelTier
    except ImportError:
        return

    # Human-readable display name
    if '/' in model_name:
        display_name = model_name.split('/')[-1]
    else:
        display_name = model_name

    backend = ModelBackend(
        model_id=catalog_id,
        display_name=display_name,
        tier=ModelTier.FAST,
        config_list_entry={
            'model': catalog_id,
            'base_url': f'http://127.0.0.1:{port}/v1',
            'api_key': 'not-needed',
        },
        avg_latency_ms=500.0,
        accuracy_score=0.7,
        cost_per_1k_tokens=0.0,
        is_local=True,
        hardware_dependent=True,
    )

    registry.register(backend)
    logger.info(f"Registered {catalog_id} in model registry (tier=FAST, local)")


# ── Flask Blueprint (lazy) ──────────────────────────────────────────

def _create_blueprint():
    """Create the Flask Blueprint for model onboarding API endpoints.

    Imports Flask lazily so this module can be imported without Flask
    being installed.
    """
    try:
        from flask import Blueprint, request, jsonify
    except ImportError:
        logger.debug("Flask not available — model_onboarding blueprint disabled")
        return None

    model_onboarding_bp = Blueprint('model_onboarding', __name__)

    @model_onboarding_bp.route('/api/models/onboard', methods=['POST'])
    def api_onboard():
        """Onboard a new model from HuggingFace.

        Body: {"model": "Qwen/Qwen3-8B", "quant": "auto", "port": 8080}
        """
        try:
            data = request.get_json(force=True, silent=True) or {}
            model = data.get('model', '').strip()
            if not model:
                return jsonify({'status': 'error', 'error': 'Missing "model" field'}), 400

            q = data.get('quant', 'auto')
            p = data.get('port', 0)
            result = onboard(model, quant=q, port=int(p))

            code = 200 if result.get('status') == 'ready' else 500
            return jsonify(result), code
        except Exception as e:
            logger.error(f"API onboard error: {e}", exc_info=True)
            return jsonify({'status': 'error', 'error': str(e)}), 500

    @model_onboarding_bp.route('/api/models/switch', methods=['POST'])
    def api_switch():
        """Switch the active model.

        Body: {"model": "meta-llama/Llama-3.1-8B", "quant": "auto"}
        """
        try:
            data = request.get_json(force=True, silent=True) or {}
            model = data.get('model', '').strip()
            if not model:
                return jsonify({'status': 'error', 'error': 'Missing "model" field'}), 400

            q = data.get('quant', 'auto')
            result = switch_model(model, quant=q)

            code = 200 if result.get('status') == 'ready' else 500
            return jsonify(result), code
        except Exception as e:
            logger.error(f"API switch error: {e}", exc_info=True)
            return jsonify({'status': 'error', 'error': str(e)}), 500

    @model_onboarding_bp.route('/api/models/available', methods=['GET'])
    def api_available():
        """List available GGUF files for a model on HuggingFace.

        Query: ?model=Qwen/Qwen3-8B
        """
        try:
            model = request.args.get('model', '').strip()
            if not model:
                return jsonify({'status': 'error', 'error': 'Missing "model" query param'}), 400

            results = list_available(model)
            return jsonify({'status': 'ok', 'models': results}), 200
        except Exception as e:
            logger.error(f"API available error: {e}", exc_info=True)
            return jsonify({'status': 'error', 'error': str(e)}), 500

    @model_onboarding_bp.route('/api/models/status', methods=['GET'])
    def api_status():
        """Return full onboarding status."""
        try:
            return jsonify(status()), 200
        except Exception as e:
            logger.error(f"API status error: {e}", exc_info=True)
            return jsonify({'status': 'error', 'error': str(e)}), 500

    @model_onboarding_bp.route('/api/models/downloaded', methods=['GET'])
    def api_downloaded():
        """List all downloaded GGUF models."""
        try:
            models = list_downloaded()
            return jsonify({'status': 'ok', 'models': models}), 200
        except Exception as e:
            logger.error(f"API downloaded error: {e}", exc_info=True)
            return jsonify({'status': 'error', 'error': str(e)}), 500

    @model_onboarding_bp.route('/api/models/<model_id>', methods=['DELETE'])
    def api_remove(model_id):
        """Remove a downloaded GGUF model.

        Path: /api/models/<model_id>
        """
        try:
            success = remove_model(model_id)
            if success:
                return jsonify({'status': 'ok', 'removed': model_id}), 200
            else:
                return jsonify({'status': 'error', 'error': f'Model {model_id} not found'}), 404
        except Exception as e:
            logger.error(f"API remove error: {e}", exc_info=True)
            return jsonify({'status': 'error', 'error': str(e)}), 500

    return model_onboarding_bp


# ── Module-level Blueprint accessor ─────────────────────────────────

_blueprint_instance = None
_blueprint_lock = threading.Lock()


def get_blueprint():
    """Get or create the model_onboarding Flask Blueprint.

    Returns None if Flask is not installed.
    """
    global _blueprint_instance
    if _blueprint_instance is None:
        with _blueprint_lock:
            if _blueprint_instance is None:
                _blueprint_instance = _create_blueprint()
    return _blueprint_instance


# Convenience alias for registration in hart_intelligence_entry.py:
#   from integrations.service_tools.model_onboarding import model_onboarding_bp
#   if model_onboarding_bp: app.register_blueprint(model_onboarding_bp)
model_onboarding_bp = get_blueprint()
