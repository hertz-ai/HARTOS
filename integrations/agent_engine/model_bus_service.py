"""
HART OS Model Bus Service — Unified AI Access for Every Application.

The Model Bus is the OS-level abstraction that makes AI a native capability.
Any application — Linux, Android, Windows, or Web — can access any deployed
model through a single unified interface.

Transports:
  - Unix socket:  /run/hart/model-bus.sock  (native Linux apps)
  - D-Bus:        com.hart.ModelBus          (desktop apps)
  - HTTP API:     localhost:6790             (Android, Wine, Web apps)

The bus routes to the best available backend:
  1. Local llama.cpp (LLM)
  2. Local MiniCPM (vision)
  3. Local Whisper (STT) / Piper (TTS)
  4. Compute mesh peers (same user's other devices)
  5. Remote HevolveAI/hivemind (world model)
"""
import json
import logging
import os
import socket
import threading
import time
from typing import Any, Dict, List, Optional

logger = logging.getLogger('hevolve.model_bus')

# ═══════════════════════════════════════════════════════════════
# Model Bus Service
# ═══════════════════════════════════════════════════════════════

class ModelBusService:
    """Unified model access — any app, any model, any device."""

    def __init__(
        self,
        socket_path: str = '/run/hart/model-bus.sock',
        http_port: int = 6790,
        grpc_port: int = 6791,
        max_concurrent: int = 32,
        routing_strategy: str = 'speculative',
        llm_port: int = 8080,
        vision_port: int = 9891,
        backend_port: int = 6777,
    ):
        self.socket_path = socket_path
        self.http_port = http_port
        self.grpc_port = grpc_port
        self.max_concurrent = max_concurrent
        self.routing_strategy = routing_strategy
        self.llm_port = llm_port
        self.vision_port = vision_port
        self.backend_port = backend_port

        self._backends: Dict[str, dict] = {}
        self._request_count = 0
        self._semaphore = threading.Semaphore(max_concurrent)
        self._lock = threading.Lock()
        self._running = False

        logger.info(
            f"ModelBusService initialized: socket={socket_path}, "
            f"http={http_port}, strategy={routing_strategy}"
        )

    # ─── Backend Discovery ───────────────────────────────────

    def discover_backends(self) -> Dict[str, dict]:
        """Discover all available model backends."""
        import requests

        backends = {}

        # LLM (llama.cpp)
        try:
            resp = requests.get(
                f'http://localhost:{self.llm_port}/health', timeout=3
            )
            if resp.status_code == 200:
                backends['llm'] = {
                    'type': 'llm',
                    'url': f'http://localhost:{self.llm_port}',
                    'status': 'ready',
                    'local': True,
                }
                logger.info(f"Backend discovered: llm (port {self.llm_port})")
        except Exception:
            pass

        # Vision (MiniCPM)
        try:
            resp = requests.get(
                f'http://localhost:{self.vision_port}/health', timeout=3
            )
            if resp.status_code == 200:
                backends['vision'] = {
                    'type': 'vision',
                    'url': f'http://localhost:{self.vision_port}',
                    'status': 'ready',
                    'local': True,
                }
                logger.info(f"Backend discovered: vision (port {self.vision_port})")
        except Exception:
            pass

        # HART backend (for world model bridge)
        try:
            resp = requests.get(
                f'http://localhost:{self.backend_port}/status', timeout=3
            )
            if resp.status_code == 200:
                backends['backend'] = {
                    'type': 'backend',
                    'url': f'http://localhost:{self.backend_port}',
                    'status': 'ready',
                    'local': True,
                }
                logger.info(f"Backend discovered: backend (port {self.backend_port})")
        except Exception:
            pass

        # Compute mesh peers
        try:
            resp = requests.get('http://localhost:6796/mesh/status', timeout=3)
            if resp.status_code == 200:
                mesh_data = resp.json()
                peer_count = mesh_data.get('peer_count', 0)
                if peer_count > 0:
                    backends['mesh'] = {
                        'type': 'mesh',
                        'url': 'http://localhost:6796',
                        'status': 'ready',
                        'local': False,
                        'peers': peer_count,
                    }
                    logger.info(f"Backend discovered: mesh ({peer_count} peers)")
        except Exception:
            pass

        self._backends = backends
        return backends

    # ─── Inference Routing ───────────────────────────────────

    def infer(
        self,
        model_type: str = 'llm',
        prompt: str = '',
        options: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """Route inference to the best available backend.

        Args:
            model_type: 'llm', 'vision', 'tts', 'stt', 'image_gen'
            prompt: The input text/query
            options: Additional options (image_path, voice, format, etc.)

        Returns:
            Dict with 'response', 'model', 'backend', 'latency_ms'
        """
        import requests

        if not self._semaphore.acquire(timeout=30):
            return {'error': 'Model Bus overloaded — try again later'}

        try:
            start_time = time.time()
            options = options or {}

            # Apply guardrail check
            if not self._check_guardrails(prompt, model_type):
                return {'error': 'Request blocked by constitutional guardrails'}

            # Route based on model type
            if model_type == 'llm':
                result = self._route_llm(prompt, options)
            elif model_type == 'vision':
                result = self._route_vision(prompt, options)
            elif model_type == 'tts':
                result = self._route_tts(prompt, options)
            elif model_type == 'stt':
                result = self._route_stt(prompt, options)
            elif model_type == 'image_gen':
                result = self._route_image_gen(prompt, options)
            else:
                result = {'error': f'Unknown model type: {model_type}'}

            latency_ms = int((time.time() - start_time) * 1000)
            result['latency_ms'] = latency_ms

            with self._lock:
                self._request_count += 1

            return result
        finally:
            self._semaphore.release()

    def _route_llm(self, prompt: str, options: dict) -> dict:
        """Route LLM inference to best backend."""
        import requests

        # Strategy: try local first, then mesh, then cloud
        backends_to_try = []

        if 'llm' in self._backends:
            backends_to_try.append(('local_llm', self._backends['llm']['url']))
        if 'mesh' in self._backends:
            backends_to_try.append(('mesh', self._backends['mesh']['url']))
        if 'backend' in self._backends:
            backends_to_try.append(('backend', self._backends['backend']['url']))

        if not backends_to_try:
            return {'error': 'No LLM backend available', 'response': None}

        for backend_name, url in backends_to_try:
            try:
                if backend_name == 'local_llm':
                    # llama.cpp OpenAI-compatible API
                    resp = requests.post(
                        f'{url}/v1/chat/completions',
                        json={
                            'model': 'local',
                            'messages': [{'role': 'user', 'content': prompt}],
                            'max_tokens': options.get('max_tokens', 512),
                        },
                        timeout=options.get('timeout', 60),
                    )
                    if resp.status_code == 200:
                        data = resp.json()
                        choices = data.get('choices', [])
                        content = choices[0].get('message', {}).get('content', '') if choices else ''
                        return {
                            'response': content,
                            'model': data.get('model', 'llama.cpp'),
                            'backend': 'local_llm',
                        }
                elif backend_name == 'mesh':
                    # Compute mesh offload
                    resp = requests.post(
                        f'{url}/mesh/infer',
                        json={'model_type': 'llm', 'prompt': prompt},
                        timeout=options.get('timeout', 120),
                    )
                    if resp.status_code == 200:
                        data = resp.json()
                        return {
                            'response': data.get('response', ''),
                            'model': data.get('model', 'mesh_peer'),
                            'backend': 'mesh',
                        }
                elif backend_name == 'backend':
                    # HART backend (may route to cloud or world model)
                    resp = requests.post(
                        f'{url}/chat',
                        json={'prompt': prompt, 'user_id': 'model_bus', 'prompt_id': 'bus'},
                        timeout=options.get('timeout', 60),
                    )
                    if resp.status_code == 200:
                        data = resp.json()
                        return {
                            'response': data.get('response', str(data)),
                            'model': 'hart_backend',
                            'backend': 'backend',
                        }
            except Exception as e:
                logger.warning(f"Backend {backend_name} failed: {e}")
                continue

        return {'error': 'All LLM backends failed', 'response': None}

    def _route_vision(self, prompt: str, options: dict) -> dict:
        """Route vision inference."""
        import requests

        image_path = options.get('image_path', '')
        if not image_path:
            return {'error': 'image_path required for vision inference'}

        if 'vision' in self._backends:
            try:
                with open(image_path, 'rb') as f:
                    resp = requests.post(
                        f"{self._backends['vision']['url']}/describe",
                        files={'image': f},
                        data={'prompt': prompt},
                        timeout=60,
                    )
                    if resp.status_code == 200:
                        return {
                            'response': resp.json().get('description', ''),
                            'model': 'minicpm',
                            'backend': 'local_vision',
                        }
            except Exception as e:
                logger.warning(f"Local vision failed: {e}")

        # Fallback to mesh
        if 'mesh' in self._backends:
            try:
                resp = requests.post(
                    f"{self._backends['mesh']['url']}/mesh/infer",
                    json={'model_type': 'vision', 'prompt': prompt, 'image_path': image_path},
                    timeout=120,
                )
                if resp.status_code == 200:
                    return {
                        'response': resp.json().get('response', ''),
                        'model': 'mesh_vision',
                        'backend': 'mesh',
                    }
            except Exception:
                pass

        return {'error': 'No vision backend available', 'response': None}

    def _route_tts(self, prompt: str, options: dict) -> dict:
        """Route text-to-speech inference."""
        # TTS backends: local Piper → mesh peer → cloud
        return {
            'response': 'TTS not yet implemented — install Piper model via Model Store',
            'model': 'none',
            'backend': 'stub',
        }

    def _route_stt(self, prompt: str, options: dict) -> dict:
        """Route speech-to-text inference."""
        # STT backends: local Whisper → mesh peer → cloud
        return {
            'response': 'STT not yet implemented — install Whisper model via Model Store',
            'model': 'none',
            'backend': 'stub',
        }

    def _route_image_gen(self, prompt: str, options: dict) -> dict:
        """Route image generation inference."""
        return {
            'response': 'Image generation not yet implemented',
            'model': 'none',
            'backend': 'stub',
        }

    # ─── Guardrail Gate ──────────────────────────────────────

    def _check_guardrails(self, prompt: str, model_type: str) -> bool:
        """Apply constitutional guardrail check to request."""
        try:
            from security.hive_guardrails import ConstitutionalFilter
            approved, reason = ConstitutionalFilter.check_prompt(prompt)
            if not approved:
                logger.warning(f"Guardrail blocked {model_type} request: {reason}")
                return False
        except ImportError:
            pass  # Guardrails not available — allow request
        return True

    # ─── Model Listing ───────────────────────────────────────

    def list_models(self) -> List[Dict[str, Any]]:
        """List all available models across all backends."""
        models = []

        if 'llm' in self._backends:
            models.append({
                'id': 'local-llm',
                'type': 'llm',
                'backend': 'llama.cpp',
                'local': True,
                'status': 'ready',
            })

        if 'vision' in self._backends:
            models.append({
                'id': 'local-vision',
                'type': 'vision',
                'backend': 'minicpm',
                'local': True,
                'status': 'ready',
            })

        if 'mesh' in self._backends:
            models.append({
                'id': 'mesh-models',
                'type': 'multiple',
                'backend': 'compute_mesh',
                'local': False,
                'status': 'ready',
                'peers': self._backends['mesh'].get('peers', 0),
            })

        return models

    def get_status(self) -> Dict[str, Any]:
        """Get Model Bus status."""
        return {
            'status': 'running' if self._running else 'stopped',
            'backends': {k: v.get('status') for k, v in self._backends.items()},
            'backend_count': len(self._backends),
            'request_count': self._request_count,
            'max_concurrent': self.max_concurrent,
            'routing_strategy': self.routing_strategy,
        }

    # ─── HTTP Server ─────────────────────────────────────────

    def _create_flask_app(self):
        """Create Flask app for HTTP API."""
        from flask import Flask, request, jsonify

        app = Flask(__name__)

        @app.route('/v1/chat', methods=['POST'])
        def chat():
            data = request.get_json(force=True)
            result = self.infer(
                model_type='llm',
                prompt=data.get('prompt', ''),
                options=data,
            )
            return jsonify(result)

        @app.route('/v1/vision', methods=['POST'])
        def vision():
            prompt = request.form.get('prompt', 'Describe this image')
            image = request.files.get('image')
            if image:
                import tempfile
                with tempfile.NamedTemporaryFile(delete=False, suffix='.jpg') as f:
                    image.save(f)
                    result = self.infer('vision', prompt, {'image_path': f.name})
            else:
                result = {'error': 'No image provided'}
            return jsonify(result)

        @app.route('/v1/tts', methods=['POST'])
        def tts():
            data = request.get_json(force=True)
            result = self.infer('tts', data.get('text', ''), data)
            return jsonify(result)

        @app.route('/v1/stt', methods=['POST'])
        def stt():
            audio = request.files.get('audio')
            if audio:
                import tempfile
                with tempfile.NamedTemporaryFile(delete=False, suffix='.wav') as f:
                    audio.save(f)
                    result = self.infer('stt', '', {'audio_path': f.name})
            else:
                result = {'error': 'No audio provided'}
            return jsonify(result)

        @app.route('/v1/models', methods=['GET'])
        def models():
            return jsonify({'models': self.list_models()})

        @app.route('/v1/status', methods=['GET'])
        def status():
            return jsonify(self.get_status())

        @app.route('/v1/prefetch', methods=['POST'])
        def prefetch():
            data = request.get_json(force=True)
            model_type = data.get('model_type', 'llm')
            # Prefetch is a hint — trigger backend warmup
            logger.info(f"Prefetch request for {model_type}")
            return jsonify({'status': 'prefetch_acknowledged', 'model_type': model_type})

        @app.route('/health', methods=['GET'])
        def health():
            return jsonify({'status': 'ok', 'service': 'model-bus'})

        return app

    # ─── Serve ───────────────────────────────────────────────

    def serve_forever(self):
        """Start the Model Bus service (HTTP + Unix socket)."""
        self._running = True

        # Initial backend discovery
        self.discover_backends()

        # Background: periodic backend re-discovery
        def _rediscover_loop():
            while self._running:
                time.sleep(30)
                try:
                    self.discover_backends()
                except Exception as e:
                    logger.error(f"Backend discovery error: {e}")

        discovery_thread = threading.Thread(target=_rediscover_loop, daemon=True)
        discovery_thread.start()

        # Start Flask HTTP server
        app = self._create_flask_app()
        logger.info(f"Model Bus HTTP API starting on port {self.http_port}")

        try:
            from waitress import serve
            serve(app, host='0.0.0.0', port=self.http_port, threads=8)
        except ImportError:
            app.run(host='0.0.0.0', port=self.http_port, threaded=True)


def start_dbus_bridge():
    """Start D-Bus bridge for com.hart.ModelBus (called via D-Bus activation)."""
    logger.info("D-Bus bridge for com.hart.ModelBus — delegating to HTTP API")
    # D-Bus bridge is a thin wrapper — actual logic in HTTP service
    # This avoids running two Python processes
    import time
    while True:
        time.sleep(3600)
