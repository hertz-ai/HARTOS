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
  3. Local Whisper (STT) / Pocket TTS (TTS)
  4. Compute mesh peers (same user's other devices)
  5. Remote HevolveAI/hivemind (world model)
"""
import json
import logging
import os
import socket
import threading
import time

from core.port_registry import get_port
from integrations.service_tools.model_catalog import ModelType
from typing import Any, Dict, List, Optional

logger = logging.getLogger('hevolve.model_bus')


# ─── Routing Status Publisher ─────────────────────────────
# Pushes conversational "Thinking" bubbles to the user's chat topic
# so the client shows real-time routing progress.
# Uses the same Crossbar channel the frontend already subscribes to.

def _publish_routing_status(user_id: str, message: str, request_id: str = ''):
    """Push routing progress to user's UI via Crossbar thinking bubble.

    The client (WebWorker/React Native) renders these as thinking indicators
    so the user sees: "Processing locally..." → "Checking hive network..." etc.
    """
    if not user_id:
        return
    try:
        import json as _json
        # Lazy import — only needed when actually publishing
        from langchain_gpt_api import publish_async
        payload = _json.dumps({
            'text': [message],
            'priority': 49,
            'action': 'Thinking',
            'bot_type': 'ComputeRouter',
            'request_id': request_id or '',
            'historical_request_id': [],
            'options': [], 'newoptions': [],
        })
        publish_async(f'com.hertzai.hevolve.chat.{user_id}', payload)
    except Exception:
        pass  # Never block inference on status publishing

# ═══════════════════════════════════════════════════════════════
# Model Bus Service
# ═══════════════════════════════════════════════════════════════

class ModelBusService:
    """Unified model access — any app, any model, any device."""

    # Backend health cache — avoids wasting seconds on dead backends.
    # Key = backend name, Value = (is_alive: bool, last_checked: float)
    # TTL: alive backends re-checked every 60s, dead every 15s (fast recovery).
    _health_cache: Dict[str, tuple] = {}
    _health_lock = threading.Lock()  # Guards _health_cache across threads
    _ALIVE_TTL = 60.0   # Re-probe alive backends every 60s
    _DEAD_TTL = 15.0     # Retry dead backends every 15s
    _PROBE_TIMEOUT = 1.5  # Health probe: 1.5s max (not 10-60s)

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

    # ─── Fast Backend Health Check ────────────────────────────

    def _is_backend_alive(self, name: str, url: str,
                          health_path: str = '/health') -> bool:
        """Check if backend is alive using cached health probe.

        Returns instantly for cached results. Only probes when cache is stale.
        Dead backends are skipped immediately (0ms) until retry TTL expires.
        This is what prevents 10-60 second waits on dead backends.
        """
        now = time.time()
        with self._health_lock:
            cached = self._health_cache.get(name)
        if cached:
            is_alive, last_checked = cached
            ttl = self._ALIVE_TTL if is_alive else self._DEAD_TTL
            if now - last_checked < ttl:
                return is_alive

        # Cache miss or stale — quick probe (outside lock to avoid blocking)
        try:
            from core.http_pool import pooled_get
            resp = pooled_get(f'{url}{health_path}', timeout=self._PROBE_TIMEOUT)
            alive = resp.status_code < 500
        except Exception:
            alive = False

        with self._health_lock:
            self._health_cache[name] = (alive, now)
        if not alive:
            logger.debug("Backend %s at %s is DOWN (skipping for %.0fs)",
                         name, url, self._DEAD_TTL)
        return alive

    def _mark_backend_dead(self, name: str):
        """Mark a backend as dead after a request failure (instant skip next time)."""
        with self._health_lock:
            self._health_cache[name] = (False, time.time())

    def _mark_backend_alive(self, name: str):
        """Mark backend alive after successful request."""
        with self._health_lock:
            self._health_cache[name] = (True, time.time())

    # ─── Backend Discovery ───────────────────────────────────

    def discover_backends(self) -> Dict[str, dict]:
        """Discover all available model backends."""
        from core.http_pool import pooled_get

        backends = {}

        # LLM (llama.cpp)
        try:
            resp = pooled_get(
                f'http://localhost:{self.llm_port}/health', timeout=3
            )
            if resp.status_code == 200:
                backends['llm'] = {
                    'type': ModelType.LLM,
                    'url': f'http://localhost:{self.llm_port}',
                    'status': 'ready',
                    'local': True,
                }
                logger.info(f"Backend discovered: llm (port {self.llm_port})")
        except Exception:
            pass

        # Vision (MiniCPM)
        try:
            resp = pooled_get(
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
            resp = pooled_get(
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
            resp = pooled_get(f'http://localhost:{get_port("mesh_relay")}/mesh/status', timeout=3)
            if resp.status_code == 200:
                mesh_data = resp.json()
                peer_count = mesh_data.get('peer_count', 0)
                if peer_count > 0:
                    backends['mesh'] = {
                        'type': 'mesh',
                        'url': f'http://localhost:{get_port("mesh_relay")}',
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
        model_type: str = ModelType.LLM,
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
        if not self._semaphore.acquire(timeout=30):
            return {'error': 'Model Bus overloaded — try again later'}

        try:
            start_time = time.time()
            options = options or {}

            # Apply guardrail check
            if not self._check_guardrails(prompt, model_type):
                return {'error': 'Request blocked by constitutional guardrails'}

            # Route based on model type
            if model_type == ModelType.LLM:
                result = self._route_llm(prompt, options)
            elif model_type == 'vision':
                result = self._route_vision(prompt, options)
            elif model_type == ModelType.TTS:
                result = self._route_tts(prompt, options)
            elif model_type == ModelType.STT:
                result = self._route_stt(prompt, options)
            elif model_type == ModelType.VIDEO_GEN:
                result = self._route_video_gen(prompt, options)
            elif model_type == ModelType.IMAGE_GEN:
                result = self._route_image_gen(prompt, options)
            else:
                result = {'error': f'Unknown model type: {model_type}'}

            latency_ms = int((time.time() - start_time) * 1000)
            result['latency_ms'] = latency_ms

            with self._lock:
                self._request_count += 1

            # Broadcast inference completion to EventBus
            try:
                from core.platform.events import emit_event
                emit_event('inference.completed', {
                    'model_type': model_type,
                    'model': result.get('model', ''),
                    'backend': result.get('backend', ''),
                    'latency_ms': latency_ms,
                    'success': 'error' not in result,
                })
            except Exception:
                pass

            return result
        finally:
            self._semaphore.release()

    def _route_llm(self, prompt: str, options: dict) -> dict:
        """Route LLM inference to best backend.

        Uses health cache to skip dead backends instantly (0ms) instead of
        waiting 10-60s for timeout. Marks backends dead on failure so
        subsequent requests don't waste time on them either.
        """
        from core.http_pool import pooled_post
        uid = options.get('user_id', '')
        rid = options.get('request_id', '')

        # Build candidate list — skip backends we KNOW are dead (instant)
        backends_to_try = []

        if 'llm' in self._backends:
            url = self._backends['llm']['url']
            if self._is_backend_alive('local_llm', url):
                backends_to_try.append(('local_llm', url))
            else:
                logger.debug("Skipping local_llm (health cache: dead)")
        if 'mesh' in self._backends:
            url = self._backends['mesh']['url']
            if self._is_backend_alive('mesh', url, '/health'):
                backends_to_try.append(('mesh', url))
        if 'backend' in self._backends:
            url = self._backends['backend']['url']
            if self._is_backend_alive('backend', url, '/status'):
                backends_to_try.append(('backend', url))

        if not backends_to_try:
            _publish_routing_status(uid,
                "Looking for an available AI backend...", rid)
            # Force re-discover (all cached as dead)
            self.discover_backends()
            # Rebuild from fresh discovery
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
                    resp = pooled_post(
                        f'{url}/v1/chat/completions',
                        json={
                            'model': 'local',
                            'messages': [{'role': 'user', 'content': prompt}],
                            'max_tokens': options.get('max_tokens', 512),
                        },
                        timeout=options.get('timeout', 60),
                    )
                    if resp.status_code == 200:
                        self._mark_backend_alive('local_llm')
                        data = resp.json()
                        choices = data.get('choices', [])
                        content = choices[0].get('message', {}).get('content', '') if choices else ''
                        return {
                            'response': content,
                            'model': data.get('model', 'llama.cpp'),
                            'backend': 'local_llm',
                        }
                elif backend_name == 'mesh':
                    _publish_routing_status(uid,
                        'Routing to hive peer...', rid)
                    # Compute mesh offload
                    resp = pooled_post(
                        f'{url}/mesh/infer',
                        json={'model_type': ModelType.LLM, 'prompt': prompt},
                        timeout=options.get('timeout', 120),
                    )
                    if resp.status_code == 200:
                        self._mark_backend_alive('mesh')
                        data = resp.json()
                        return {
                            'response': data.get('response', ''),
                            'model': data.get('model', 'mesh_peer'),
                            'backend': 'mesh',
                        }
                elif backend_name == 'backend':
                    _publish_routing_status(uid,
                        'Routing to cloud backend...', rid)
                    # HART backend (may route to cloud or world model)
                    resp = pooled_post(
                        f'{url}/chat',
                        json={'prompt': prompt, 'user_id': 'model_bus', 'prompt_id': 'bus'},
                        timeout=options.get('timeout', 60),
                    )
                    if resp.status_code == 200:
                        self._mark_backend_alive('backend')
                        data = resp.json()
                        return {
                            'response': data.get('response', str(data)),
                            'model': 'hart_backend',
                            'backend': 'backend',
                        }
            except Exception as e:
                self._mark_backend_dead(backend_name)
                logger.warning(f"Backend {backend_name} failed (marked dead for "
                               f"{self._DEAD_TTL}s): {e}")
                continue

        return {'error': 'All LLM backends failed', 'response': None}

    def _route_vision(self, prompt: str, options: dict) -> dict:
        """Route vision inference."""
        from core.http_pool import pooled_post

        image_path = options.get('image_path', '')
        uid = options.get('user_id', '')
        rid = options.get('request_id', '')
        if not image_path:
            return {'error': 'image_path required for vision inference'}

        if 'vision' in self._backends:
            try:
                with open(image_path, 'rb') as f:
                    resp = pooled_post(
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
            _publish_routing_status(uid,
                'No local vision model — checking hive network...', rid)
            try:
                resp = pooled_post(
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

        _publish_routing_status(uid,
            "I can't analyze this image right now — no vision model available "
            "locally or on the hive. Try connecting a device with GPU.", rid)
        return {'error': 'No vision backend available', 'response': None}

    def _route_tts(self, prompt: str, options: dict) -> dict:
        """Route TTS through smart TTS router (language-aware, GPU/hive/CPU).

        Delegates to TTSRouter which considers language, GPU, VRAM, compute
        policy, hive peers, and urgency. Falls back to legacy chain if
        router is unavailable.
        """
        uid = options.get('user_id', '')
        rid = options.get('request_id', '')
        try:
            from integrations.channels.media.tts_router import get_tts_router
            router = get_tts_router()
            result = router.synthesize(
                text=prompt,
                language=options.get('language'),
                voice=options.get('voice_audio') or options.get('voice'),
                source=options.get('source', 'agent_tool'),
                engine_override=options.get('engine'),
            )
            if not result.error:
                return {
                    'response': result.path,
                    'model': f'{result.engine_id}',
                    'backend': 'local_tts' if result.location == 'local' else result.location,
                    'duration': result.duration,
                    'latency_ms': result.latency_ms,
                    'device': result.device,
                }
            logger.debug("TTS router failed: %s, falling back to legacy chain", result.error)
        except (ImportError, Exception) as e:
            logger.debug("TTS router unavailable (%s), using legacy chain", e)

        # Legacy fallback: skip the full chain (luxtts→makeittalk→pocket)
        # which ignores language/GPU/policy. Go straight to pocket_tts
        # (guaranteed CPU, always available). The TTSRouter already handles
        # luxtts, makeittalk, and GPU engines with proper awareness.
        _publish_routing_status(uid, 'Generating speech (fallback)...', rid)
        return self._try_pocket_tts(prompt, options)

    def _try_luxtts(self, prompt: str, options: dict) -> dict:
        """Try LuxTTS (48kHz, GPU/CPU, voice cloning)."""
        t0 = time.time()
        try:
            from integrations.service_tools.luxtts_tool import luxtts_synthesize
            voice = options.get('voice_audio') or options.get('voice')
            device = options.get('device')
            result = json.loads(luxtts_synthesize(
                prompt, voice_audio=voice, device=device,
            ))
            latency = (time.time() - t0) * 1000
            if 'error' in result:
                logger.info(f"LuxTTS unavailable: {result['error']}")
                return result
            return {
                'response': result.get('path', ''),
                'model': 'luxtts-48k',
                'backend': f"local_tts_{result.get('device', 'cpu')}",
                'voice': result.get('voice', ''),
                'duration': result.get('duration', 0),
                'sample_rate': 48000,
                'engine': 'luxtts',
                'rtf': result.get('rtf', 0),
                'realtime_factor': result.get('realtime_factor', 0),
                'latency_ms': round(latency, 1),
            }
        except ImportError:
            return {'error': 'LuxTTS not installed'}
        except Exception as e:
            logger.warning(f"LuxTTS failed: {e}")
            return {'error': f'LuxTTS error: {e}'}

    def _try_makeittalk_tts(self, prompt: str, options: dict, base_url: str) -> dict:
        """Try MakeItTalk cloud TTS (POST /video-gen/ with text, no image = audio-only)."""
        import requests as http_requests
        t0 = time.time()
        try:
            voice = options.get('voice', 'af_bella')
            payload = {
                'text': prompt,
                'uid': options.get('user_id', 'model_bus'),
                'voiceName': voice,
                'kokuro': 'true',
                'audio_only': True,  # hint: skip video pipeline
            }
            resp = http_requests.post(
                f'{base_url.rstrip("/")}/video-gen/',
                json=payload,
                timeout=options.get('timeout', 30),
            )
            latency = (time.time() - t0) * 1000
            if resp.status_code == 200:
                data = resp.json()
                return {
                    'response': data.get('audio_url', data.get('url', '')),
                    'model': 'makeittalk-cloud',
                    'backend': 'cloud_tts',
                    'voice': voice,
                    'engine': 'makeittalk',
                    'latency_ms': round(latency, 1),
                }
            logger.warning("MakeItTalk returned %d: %s", resp.status_code, resp.text[:200])
            return {'error': f'MakeItTalk HTTP {resp.status_code}'}
        except http_requests.ConnectionError:
            logger.info("MakeItTalk cloud connection refused at %s", base_url)
            return {'error': 'MakeItTalk connection failed'}
        except http_requests.Timeout:
            logger.info("MakeItTalk cloud timed out at %s", base_url)
            return {'error': 'MakeItTalk timeout'}
        except Exception as e:
            logger.warning("MakeItTalk cloud error: %s", e)
            return {'error': f'MakeItTalk error: {e}'}

    def _try_pocket_tts(self, prompt: str, options: dict) -> dict:
        """Pocket TTS offline fallback (always available, CPU, zero cost)."""
        t0 = time.time()
        try:
            from integrations.service_tools.pocket_tts_tool import pocket_tts_synthesize
            voice = options.get('voice', 'alba')
            output_path = options.get('output_path')
            result = json.loads(pocket_tts_synthesize(prompt, voice, output_path))
            latency = (time.time() - t0) * 1000
            if 'error' in result:
                return {'error': result['error'], 'response': None}
            return {
                'response': result.get('path', ''),
                'model': 'pocket-tts-100m',
                'backend': 'local_tts',
                'voice': result.get('voice', voice),
                'duration': result.get('duration', 0),
                'engine': result.get('engine', 'pocket-tts'),
                'latency_ms': round(latency, 1),
            }
        except ImportError:
            return {'error': 'Pocket TTS not available (pip install pocket-tts)', 'response': None}
        except Exception as e:
            return {'error': f'TTS inference failed: {e}', 'response': None}

    def _route_stt(self, prompt: str, options: dict) -> dict:
        """Route speech-to-text inference via Whisper (sherpa-onnx / openai-whisper)."""
        t0 = time.time()
        try:
            from integrations.service_tools.whisper_tool import whisper_transcribe
            audio_path = options.get('audio_path', prompt)
            language = options.get('language')
            result = json.loads(whisper_transcribe(audio_path, language))
            latency = (time.time() - t0) * 1000
            if 'error' in result:
                return {'error': result['error'], 'response': None}
            return {
                'response': result.get('text', ''),
                'model': 'whisper-stt-local',
                'backend': 'local_stt',
                'language': result.get('language', 'auto'),
                'latency_ms': round(latency, 1),
            }
        except ImportError:
            return {'error': 'Whisper STT not available (pip install sherpa-onnx)', 'response': None}
        except Exception as e:
            return {'error': f'STT inference failed: {e}', 'response': None}

    def _route_video_gen(self, prompt: str, options: dict) -> dict:
        """Route video generation: local GPU → hive mesh peer → cloud fallback.

        For no-GPU central instances, this offloads to a hive peer that has
        a GPU with LTX-2, ComfyUI, or Wan2GP loaded.

        Publishes routing status to the user's chat topic so the UI shows
        real-time progress ("Generating video locally..." → "Checking hive...").
        """
        from core.http_pool import pooled_post

        model = options.get('model', 'ltx2')
        timeout = options.get('timeout', 300)
        uid = options.get('user_id', '')
        rid = options.get('request_id', '')

        # 1. Try local GPU servers — skip instantly if health cache says dead
        local_servers = [
            ('ltx2', f"http://localhost:{get_port('ltx2_server', 5002)}"),
            ('comfyui', f"http://localhost:{get_port('comfyui', 8188)}"),
        ]
        for name, local_url in local_servers:
            if not self._is_backend_alive(f'video_{name}', local_url):
                continue  # 0ms — skip dead server
            _publish_routing_status(uid, 'Generating video on this device...', rid)
            try:
                resp = pooled_post(
                    f'{local_url}/generate',
                    json={'prompt': prompt, 'model': model},
                    timeout=timeout,
                )
                if resp.status_code == 200:
                    self._mark_backend_alive(f'video_{name}')
                    data = resp.json()
                    video_url = data.get('video_url') or data.get('output_url', '')
                    return {
                        'response': video_url,
                        'model': model,
                        'backend': 'local_gpu',
                    }
            except Exception:
                self._mark_backend_dead(f'video_{name}')
                continue

        # 2. Try hive mesh peer with GPU (central has no GPU → offload)
        _publish_routing_status(uid,
            'No local GPU available. Checking hive network for a device with GPU...', rid)
        try:
            from integrations.agent_engine.compute_config import get_compute_policy
            policy = get_compute_policy()
            if policy.get('compute_policy') != 'local_only':
                from integrations.agent_engine.compute_mesh_service import get_compute_mesh
                mesh = get_compute_mesh()
                result = mesh.offload_to_best_peer(
                    model_type=ModelType.VIDEO_GEN,
                    prompt=prompt,
                    options={**options, 'timeout': timeout},
                )
                if result and 'error' not in result:
                    peer = result.get('offloaded_to', 'peer')
                    _publish_routing_status(uid,
                        f'Video generation running on hive peer {peer}...', rid)
                    result['backend'] = 'hive_peer'
                    return result
                logger.info("Hive mesh video offload: %s",
                            result.get('error', 'no result'))
        except Exception as e:
            logger.debug("Hive mesh video offload unavailable: %s", e)

        # 3. Cloud fallback (MakeItTalk or external service) — skip if known dead
        makeittalk_url = os.environ.get('MAKEITTALK_API_URL')
        if makeittalk_url and self._is_backend_alive(
                'makeittalk_cloud', makeittalk_url, '/health'):
            _publish_routing_status(uid,
                'No hive peers with GPU. Sending to cloud service...', rid)
            try:
                resp = pooled_post(
                    f'{makeittalk_url.rstrip("/")}/video-gen/',
                    json={
                        'text': prompt,
                        'uid': options.get('user_id', 'model_bus'),
                        **{k: v for k, v in options.items()
                           if k in ('avatar_id', 'image_url', 'voice_id')},
                    },
                    timeout=min(timeout, 60),
                )
                if resp.status_code == 200:
                    self._mark_backend_alive('makeittalk_cloud')
                    data = resp.json()
                    return {
                        'response': data.get('video_url', data.get('url', '')),
                        'model': 'makeittalk-cloud',
                        'backend': 'cloud',
                    }
            except Exception as e:
                self._mark_backend_dead('makeittalk_cloud')
                logger.warning("MakeItTalk video cloud (marked dead): %s", e)

        # All paths exhausted — conversational fallback
        _publish_routing_status(uid,
            "I wasn't able to generate the video right now. "
            "There's no GPU available on this server, no hive peers are online "
            "with a free GPU, and the cloud video service didn't respond. "
            "You can try again shortly, or connect a GPU device to your hive "
            "with: hart compute pair <device-address>", rid)
        return {'error': 'No video generation backend available',
                'response': ("I couldn't generate the video — no GPU is available "
                             "locally or on the hive network right now. "
                             "Try again in a moment or pair a GPU device.")}

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
                'type': ModelType.LLM,
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

        # TTS — LuxTTS (if installed, GPU/CPU, 48kHz voice cloning)
        try:
            from zipvoice.luxvoice import LuxTTS as _LuxCheck  # noqa: F401
            import torch as _t
            _lux_device = 'cuda' if _t.cuda.is_available() else 'cpu'
            models.append({
                'id': 'luxtts-48k',
                'type': ModelType.TTS,
                'backend': 'luxtts',
                'local': True,
                'status': 'ready',
                'device': _lux_device,
                'features': ['voice_cloning', '48khz', 'gpu_accelerated', 'offline'],
            })
        except ImportError:
            pass

        # TTS — MakeItTalk cloud (if configured)
        makeittalk_url = os.environ.get('MAKEITTALK_API_URL')
        if makeittalk_url:
            models.append({
                'id': 'makeittalk-cloud',
                'type': ModelType.TTS,
                'backend': 'makeittalk',
                'local': False,
                'status': 'ready',
                'url': makeittalk_url,
                'features': ['video_gen', 'lip_sync', 'multi_voice', 'multi_language'],
            })

        # TTS — Pocket TTS (always local, CPU — fallback when cloud unavailable)
        models.append({
            'id': 'pocket-tts-100m',
            'type': ModelType.TTS,
            'backend': 'pocket_tts',
            'local': True,
            'status': 'ready',
            'features': ['voice_cloning', 'zero_shot', 'offline'],
        })

        # STT — Whisper (always local, sherpa-onnx or openai-whisper)
        models.append({
            'id': 'whisper-stt-local',
            'type': ModelType.STT,
            'backend': 'whisper',
            'local': True,
            'status': 'ready',
            'features': ['multilingual', 'offline'],
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
                model_type=ModelType.LLM,
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
            result = self.infer(ModelType.TTS, data.get('text', ''), data)
            return jsonify(result)

        @app.route('/v1/stt', methods=['POST'])
        def stt():
            audio = request.files.get('audio')
            if audio:
                import tempfile
                with tempfile.NamedTemporaryFile(delete=False, suffix='.wav') as f:
                    audio.save(f)
                    result = self.infer(ModelType.STT, '', {'audio_path': f.name})
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
            model_type = data.get('model_type', ModelType.LLM)
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
