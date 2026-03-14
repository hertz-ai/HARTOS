"""
HART OS App Bridge Service — Cross-Subsystem Intelligence.

Makes subsystem boundaries invisible. An Android app can call a Linux
service. A Windows app can use an AI model. Everything talks to everything
through OS-native agents.

Cross-subsystem IPC:
  Android  → Intent  → App Bridge → D-Bus   → Linux service
  Web/PWA  → HTTP    → App Bridge → Pipe    → Windows app (Wine)
  AI Agent → Socket  → App Bridge → Binder  → Android Activity
  Any app  → Bridge  → Semantic Router → best handler regardless of subsystem

Also unifies:
  - Clipboard (copy in Android, paste in Linux)
  - Drag & drop (XDG portal)
  - File sharing (cross-subsystem file access)
  - Notifications (unified notification stream)
"""
import hashlib
import json
import logging
import os
import subprocess
import threading
import time
from typing import Any, Dict, List, Optional

logger = logging.getLogger('hevolve.app_bridge')

# ═══════════════════════════════════════════════════════════════
# Capability Registry
# ═══════════════════════════════════════════════════════════════

class Capability:
    """A registered capability from any subsystem."""

    def __init__(
        self,
        name: str,
        subsystem: str,
        handler: str,
        actions: Optional[List[str]] = None,
        mime_types: Optional[List[str]] = None,
        priority: int = 50,
        metadata: Optional[dict] = None,
    ):
        self.name = name
        self.subsystem = subsystem       # linux, android, windows, web, ai
        self.handler = handler           # D-Bus path, intent, COM object, URL, etc.
        self.actions = actions or []     # open, edit, share, view, translate, etc.
        self.mime_types = mime_types or []
        self.priority = priority         # 0-100, higher = preferred
        self.metadata = metadata or {}
        self.registered_at = time.time()

    def to_dict(self) -> dict:
        return {
            'name': self.name,
            'subsystem': self.subsystem,
            'handler': self.handler,
            'actions': self.actions,
            'mime_types': self.mime_types,
            'priority': self.priority,
            'metadata': self.metadata,
            'registered_at': self.registered_at,
        }

    def matches(self, action: str = '', mime_type: str = '') -> bool:
        """Check if this capability handles the given action/mime_type."""
        action_match = not action or action in self.actions or '*' in self.actions
        mime_match = not mime_type or mime_type in self.mime_types or '*/*' in self.mime_types
        return action_match and mime_match


class CapabilityRegistry:
    """Thread-safe registry of capabilities from all subsystems."""

    def __init__(self):
        self._capabilities: Dict[str, Capability] = {}
        self._lock = threading.Lock()

    def register(self, capability: Capability) -> str:
        """Register a capability. Returns capability ID."""
        cap_id = hashlib.sha256(
            f"{capability.subsystem}:{capability.name}:{capability.handler}".encode()
        ).hexdigest()[:12]

        with self._lock:
            self._capabilities[cap_id] = capability

        logger.info(
            f"Capability registered: {capability.name} "
            f"({capability.subsystem}) -> {capability.handler}"
        )
        return cap_id

    def unregister(self, cap_id: str) -> bool:
        with self._lock:
            if cap_id in self._capabilities:
                del self._capabilities[cap_id]
                return True
            return False

    def query(
        self, action: str = '', mime_type: str = '', subsystem: str = ''
    ) -> List[Capability]:
        """Find capabilities matching the query, sorted by priority."""
        with self._lock:
            results = []
            for cap in self._capabilities.values():
                if subsystem and cap.subsystem != subsystem:
                    continue
                if cap.matches(action, mime_type):
                    results.append(cap)

        results.sort(key=lambda c: c.priority, reverse=True)
        return results

    def list_all(self) -> List[Dict[str, Any]]:
        with self._lock:
            return [c.to_dict() for c in self._capabilities.values()]

    def get_subsystems(self) -> Dict[str, int]:
        """Count capabilities per subsystem."""
        with self._lock:
            counts: Dict[str, int] = {}
            for cap in self._capabilities.values():
                counts[cap.subsystem] = counts.get(cap.subsystem, 0) + 1
            return counts


# ═══════════════════════════════════════════════════════════════
# Semantic Router
# ═══════════════════════════════════════════════════════════════

class SemanticRouter:
    """Routes requests to the best capability across subsystems."""

    def __init__(self, registry: CapabilityRegistry, model_bus_port: int = 6790):
        self.registry = registry
        self.model_bus_port = model_bus_port

    def route(
        self,
        action: str,
        data: str = '',
        mime_type: str = '',
        preferred_subsystem: str = '',
    ) -> Dict[str, Any]:
        """Route an action to the best available handler."""
        candidates = self.registry.query(
            action=action, mime_type=mime_type, subsystem=preferred_subsystem
        )

        if not candidates:
            # No native handler — try AI fallback
            return self._ai_fallback(action, data, mime_type)

        best = candidates[0]
        return self._dispatch(best, action, data)

    def _dispatch(self, capability: Capability, action: str, data: str) -> Dict[str, Any]:
        """Dispatch to the handler based on subsystem type."""
        subsystem = capability.subsystem

        if subsystem == 'linux':
            return self._dispatch_linux(capability, action, data)
        elif subsystem == 'android':
            return self._dispatch_android(capability, action, data)
        elif subsystem == 'windows':
            return self._dispatch_windows(capability, action, data)
        elif subsystem == 'web':
            return self._dispatch_web(capability, action, data)
        elif subsystem == 'ai':
            return self._dispatch_ai(capability, action, data)
        else:
            return {'error': f'Unknown subsystem: {subsystem}'}

    def _dispatch_linux(self, cap: Capability, action: str, data: str) -> Dict[str, Any]:
        """Dispatch to Linux D-Bus service or CLI tool."""
        handler = cap.handler

        if handler.startswith('dbus:'):
            # D-Bus method call
            dbus_dest = handler[5:]
            try:
                result = subprocess.run(
                    ['busctl', 'call', '--system', dbus_dest, '/', 'Execute', 's', data],
                    capture_output=True, text=True, timeout=30,
                )
                return {
                    'status': 'success' if result.returncode == 0 else 'error',
                    'subsystem': 'linux',
                    'handler': handler,
                    'output': result.stdout.strip() if result.returncode == 0 else result.stderr.strip(),
                }
            except Exception as e:
                return {'error': f'D-Bus call failed: {str(e)}', 'subsystem': 'linux'}

        elif handler.startswith('cli:'):
            # CLI tool
            cmd = handler[4:]
            try:
                result = subprocess.run(
                    [cmd, data] if data else [cmd],
                    capture_output=True, text=True, timeout=30,
                )
                return {
                    'status': 'success' if result.returncode == 0 else 'error',
                    'subsystem': 'linux',
                    'handler': handler,
                    'output': result.stdout.strip(),
                }
            except Exception as e:
                return {'error': f'CLI execution failed: {str(e)}', 'subsystem': 'linux'}

        return {'error': f'Unknown Linux handler format: {handler}', 'subsystem': 'linux'}

    def _dispatch_android(self, cap: Capability, action: str, data: str) -> Dict[str, Any]:
        """Dispatch to Android via ART bridge (am command)."""
        handler = cap.handler

        if handler.startswith('intent:'):
            # Android Intent via am (Activity Manager)
            intent_action = handler[7:]
            try:
                cmd = ['am', 'start', '-a', intent_action]
                if data:
                    cmd.extend(['-d', data])

                result = subprocess.run(
                    cmd, capture_output=True, text=True, timeout=15,
                )
                return {
                    'status': 'success' if result.returncode == 0 else 'error',
                    'subsystem': 'android',
                    'handler': handler,
                    'output': result.stdout.strip(),
                }
            except Exception as e:
                return {'error': f'Android intent failed: {str(e)}', 'subsystem': 'android'}

        elif handler.startswith('activity:'):
            # Direct Activity launch
            activity = handler[9:]
            try:
                result = subprocess.run(
                    ['am', 'start', '-n', activity],
                    capture_output=True, text=True, timeout=15,
                )
                return {
                    'status': 'success' if result.returncode == 0 else 'error',
                    'subsystem': 'android',
                    'handler': handler,
                    'output': result.stdout.strip(),
                }
            except Exception as e:
                return {'error': f'Android activity failed: {str(e)}', 'subsystem': 'android'}

        return {'error': f'Unknown Android handler: {handler}', 'subsystem': 'android'}

    def _dispatch_windows(self, cap: Capability, action: str, data: str) -> Dict[str, Any]:
        """Dispatch to Windows app via Wine."""
        handler = cap.handler

        if handler.startswith('wine:'):
            exe_path = handler[5:]
            try:
                result = subprocess.run(
                    ['wine', exe_path, data] if data else ['wine', exe_path],
                    capture_output=True, text=True, timeout=30,
                )
                return {
                    'status': 'success' if result.returncode == 0 else 'error',
                    'subsystem': 'windows',
                    'handler': handler,
                    'output': result.stdout.strip(),
                }
            except Exception as e:
                return {'error': f'Wine execution failed: {str(e)}', 'subsystem': 'windows'}

        return {'error': f'Unknown Windows handler: {handler}', 'subsystem': 'windows'}

    def _dispatch_web(self, cap: Capability, action: str, data: str) -> Dict[str, Any]:
        """Dispatch to Web/PWA via HTTP."""
        from core.http_pool import pooled_post

        handler = cap.handler
        if handler.startswith('http'):
            try:
                resp = pooled_post(
                    handler,
                    json={'action': action, 'data': data},
                    timeout=30,
                )
                return {
                    'status': 'success' if resp.status_code == 200 else 'error',
                    'subsystem': 'web',
                    'handler': handler,
                    'output': resp.text[:1000],
                }
            except Exception as e:
                return {'error': f'Web dispatch failed: {str(e)}', 'subsystem': 'web'}

        return {'error': f'Unknown Web handler: {handler}', 'subsystem': 'web'}

    def _dispatch_ai(self, cap: Capability, action: str, data: str) -> Dict[str, Any]:
        """Dispatch to AI model via Model Bus."""
        return self._ai_fallback(action, data, '')

    def _ai_fallback(self, action: str, data: str, mime_type: str) -> Dict[str, Any]:
        """Fall back to AI agent when no native handler available."""
        from core.http_pool import pooled_post

        prompt = f"Action: {action}\nData: {data}"
        if mime_type:
            prompt += f"\nMIME type: {mime_type}"

        try:
            resp = pooled_post(
                f'http://localhost:{self.model_bus_port}/v1/chat',
                json={'prompt': prompt, 'max_tokens': 512},
                timeout=60,
            )
            if resp.status_code == 200:
                result = resp.json()
                return {
                    'status': 'success',
                    'subsystem': 'ai',
                    'handler': 'model_bus',
                    'output': result.get('response', str(result)),
                    'ai_fallback': True,
                }
        except Exception as e:
            logger.warning(f"AI fallback failed: {e}")

        return {
            'error': f'No handler found for action={action}, mime={mime_type}',
            'ai_fallback_attempted': True,
        }


# ═══════════════════════════════════════════════════════════════
# Unified Clipboard
# ═══════════════════════════════════════════════════════════════

class UnifiedClipboard:
    """Cross-subsystem clipboard synchronization."""

    def __init__(self):
        self._content: str = ''
        self._content_type: str = 'text/plain'
        self._source: str = ''
        self._timestamp: float = 0
        self._lock = threading.Lock()

    def set_content(self, content: str, content_type: str = 'text/plain',
                    source: str = 'unknown') -> Dict[str, Any]:
        with self._lock:
            self._content = content
            self._content_type = content_type
            self._source = source
            self._timestamp = time.time()

        logger.debug(f"Clipboard updated from {source}: {content_type} ({len(content)} chars)")
        return {'status': 'set', 'source': source, 'content_type': content_type}

    def get_content(self) -> Dict[str, Any]:
        with self._lock:
            return {
                'content': self._content,
                'content_type': self._content_type,
                'source': self._source,
                'timestamp': self._timestamp,
                'age_seconds': int(time.time() - self._timestamp) if self._timestamp else 0,
            }

    def clear(self):
        with self._lock:
            self._content = ''
            self._content_type = 'text/plain'
            self._source = ''
            self._timestamp = 0


# ═══════════════════════════════════════════════════════════════
# App Bridge Service
# ═══════════════════════════════════════════════════════════════

class AppBridgeService:
    """Cross-subsystem agent routing via OS-native IPC."""

    def __init__(
        self,
        socket_path: str = '/run/hart/app-bridge.sock',
        http_port: int = 6810,
        cross_subsystem: bool = True,
        intent_router: bool = True,
        clipboard_sync: bool = True,
        drag_and_drop: bool = True,
        ai_fallback: bool = True,
        model_bus_port: int = 6790,
        backend_port: int = 6777,
    ):
        self.socket_path = socket_path
        self.http_port = http_port
        self.cross_subsystem = cross_subsystem
        self.intent_router = intent_router
        self.clipboard_sync = clipboard_sync
        self.drag_and_drop = drag_and_drop
        self.ai_fallback = ai_fallback
        self.model_bus_port = model_bus_port
        self.backend_port = backend_port

        self.registry = CapabilityRegistry()
        self.router = SemanticRouter(self.registry, model_bus_port)
        self.clipboard = UnifiedClipboard()

        self._running = False
        self._active_subsystems: List[str] = []

        logger.info(
            f"AppBridgeService initialized: http_port={http_port}, "
            f"cross_subsystem={cross_subsystem}, ai_fallback={ai_fallback}"
        )

    # ─── Subsystem Detection ─────────────────────────────────

    def detect_subsystems(self) -> List[str]:
        """Detect which subsystems are active on this device."""
        subsystems = ['linux']  # Linux always present

        # Android (check for ART runtime)
        try:
            result = subprocess.run(
                ['pgrep', '-f', 'zygote'], capture_output=True, timeout=5,
            )
            if result.returncode == 0:
                subsystems.append('android')
        except Exception:
            pass

        # Windows (Wine)
        try:
            result = subprocess.run(
                ['which', 'wine'], capture_output=True, timeout=5,
            )
            if result.returncode == 0:
                subsystems.append('windows')
        except Exception:
            pass

        # Web/PWA
        try:
            result = subprocess.run(
                ['which', 'chromium'], capture_output=True, timeout=5,
            )
            if result.returncode == 0:
                subsystems.append('web')
        except Exception:
            pass

        # AI (Model Bus)
        from core.http_pool import pooled_get
        try:
            resp = pooled_get(
                f'http://localhost:{self.model_bus_port}/v1/status', timeout=3,
            )
            if resp.status_code == 200:
                subsystems.append('ai')
        except Exception:
            pass

        self._active_subsystems = subsystems
        logger.info(f"Active subsystems: {subsystems}")
        return subsystems

    # ─── Default Capability Seeding ───────────────────────────

    def _seed_default_capabilities(self):
        """Register built-in capabilities for detected subsystems."""
        if 'linux' in self._active_subsystems:
            self.registry.register(Capability(
                name='file_manager', subsystem='linux',
                handler='cli:xdg-open',
                actions=['open', 'view'],
                mime_types=['*/*'],
                priority=30,
            ))
            self.registry.register(Capability(
                name='text_editor', subsystem='linux',
                handler='cli:xdg-open',
                actions=['edit'],
                mime_types=['text/*', 'application/json', 'application/xml'],
                priority=40,
            ))

        if 'android' in self._active_subsystems:
            self.registry.register(Capability(
                name='android_share', subsystem='android',
                handler='intent:android.intent.action.SEND',
                actions=['share'],
                mime_types=['*/*'],
                priority=60,
            ))
            self.registry.register(Capability(
                name='android_view', subsystem='android',
                handler='intent:android.intent.action.VIEW',
                actions=['open', 'view'],
                mime_types=['*/*'],
                priority=50,
            ))
            self.registry.register(Capability(
                name='android_camera', subsystem='android',
                handler='intent:android.media.action.IMAGE_CAPTURE',
                actions=['capture', 'photo'],
                mime_types=['image/*'],
                priority=80,
            ))

        if 'ai' in self._active_subsystems:
            self.registry.register(Capability(
                name='ai_describe', subsystem='ai',
                handler=f'http://localhost:{self.model_bus_port}/v1/vision',
                actions=['describe', 'analyze', 'classify'],
                mime_types=['image/*'],
                priority=70,
            ))
            self.registry.register(Capability(
                name='ai_translate', subsystem='ai',
                handler=f'http://localhost:{self.model_bus_port}/v1/chat',
                actions=['translate', 'summarize', 'explain'],
                mime_types=['text/*'],
                priority=80,
            ))
            self.registry.register(Capability(
                name='ai_tts', subsystem='ai',
                handler=f'http://localhost:{self.model_bus_port}/v1/tts',
                actions=['speak', 'tts'],
                mime_types=['text/plain'],
                priority=90,
            ))
            self.registry.register(Capability(
                name='ai_stt', subsystem='ai',
                handler=f'http://localhost:{self.model_bus_port}/v1/stt',
                actions=['transcribe', 'stt', 'listen'],
                mime_types=['audio/*'],
                priority=90,
            ))

        logger.info(
            f"Seeded {len(self.registry.list_all())} default capabilities"
        )

    # ─── Intent Router ────────────────────────────────────────

    def route_intent(
        self, action: str, data: str = '', mime_type: str = '',
        source_subsystem: str = '', preferred_subsystem: str = '',
    ) -> Dict[str, Any]:
        """Route an intent/action to the best handler across subsystems."""
        if not self.cross_subsystem and source_subsystem:
            # Only route within the same subsystem
            preferred_subsystem = source_subsystem

        result = self.router.route(
            action=action,
            data=data,
            mime_type=mime_type,
            preferred_subsystem=preferred_subsystem,
        )

        result['source_subsystem'] = source_subsystem
        result['action'] = action
        return result

    # ─── File Open (Cross-Subsystem) ──────────────────────────

    def open_file(self, path: str, preferred_subsystem: str = '') -> Dict[str, Any]:
        """Open a file with the best handler from any subsystem."""
        import mimetypes
        mime_type, _ = mimetypes.guess_type(path)
        mime_type = mime_type or 'application/octet-stream'

        return self.route_intent(
            action='open',
            data=path,
            mime_type=mime_type,
            preferred_subsystem=preferred_subsystem,
        )

    # ─── Status ───────────────────────────────────────────────

    def get_status(self) -> Dict[str, Any]:
        capabilities = self.registry.list_all()
        subsystem_counts = self.registry.get_subsystems()

        return {
            'status': 'running' if self._running else 'stopped',
            'active_subsystems': self._active_subsystems,
            'capability_count': len(capabilities),
            'capabilities_by_subsystem': subsystem_counts,
            'features': {
                'cross_subsystem': self.cross_subsystem,
                'intent_router': self.intent_router,
                'clipboard_sync': self.clipboard_sync,
                'drag_and_drop': self.drag_and_drop,
                'ai_fallback': self.ai_fallback,
            },
            'http_port': self.http_port,
        }

    # ─── HTTP Server ──────────────────────────────────────────

    def _create_flask_app(self):
        """Create Flask app for bridge HTTP API."""
        from flask import Flask, request, jsonify

        app = Flask(__name__)

        @app.route('/v1/capabilities', methods=['GET'])
        def list_capabilities():
            return jsonify({
                'capabilities': self.registry.list_all(),
                'count': len(self.registry.list_all()),
            })

        @app.route('/v1/capabilities/register', methods=['POST'])
        def register_capability():
            data = request.get_json(force=True)
            cap = Capability(
                name=data.get('name', ''),
                subsystem=data.get('subsystem', 'linux'),
                handler=data.get('handler', ''),
                actions=data.get('actions', []),
                mime_types=data.get('mime_types', []),
                priority=data.get('priority', 50),
                metadata=data.get('metadata', {}),
            )
            cap_id = self.registry.register(cap)
            return jsonify({'cap_id': cap_id, 'status': 'registered'})

        @app.route('/v1/capabilities/query', methods=['POST'])
        def query_capabilities():
            data = request.get_json(force=True)
            results = self.registry.query(
                action=data.get('action', ''),
                mime_type=data.get('mime_type', ''),
                subsystem=data.get('subsystem', ''),
            )
            return jsonify({
                'results': [c.to_dict() for c in results],
                'count': len(results),
            })

        @app.route('/v1/subsystems', methods=['GET'])
        def list_subsystems():
            return jsonify({
                'active': self._active_subsystems,
                'capability_counts': self.registry.get_subsystems(),
            })

        @app.route('/v1/route', methods=['POST'])
        def route_action():
            data = request.get_json(force=True)
            result = self.route_intent(
                action=data.get('action', ''),
                data=data.get('data', ''),
                mime_type=data.get('mime_type', ''),
                source_subsystem=data.get('source', ''),
                preferred_subsystem=data.get('preferred', ''),
            )
            return jsonify(result)

        @app.route('/v1/open', methods=['POST'])
        def open_file_route():
            data = request.get_json(force=True)
            result = self.open_file(
                path=data.get('path', ''),
                preferred_subsystem=data.get('preferred', ''),
            )
            return jsonify(result)

        @app.route('/v1/clipboard', methods=['GET'])
        def get_clipboard():
            return jsonify(self.clipboard.get_content())

        @app.route('/v1/clipboard', methods=['POST'])
        def set_clipboard():
            data = request.get_json(force=True)
            result = self.clipboard.set_content(
                content=data.get('content', ''),
                content_type=data.get('content_type', 'text/plain'),
                source=data.get('source', 'http'),
            )
            return jsonify(result)

        @app.route('/v1/status', methods=['GET'])
        def status():
            return jsonify(self.get_status())

        @app.route('/health', methods=['GET'])
        def health():
            return jsonify({'status': 'ok', 'service': 'app-bridge'})

        return app

    # ─── Serve ────────────────────────────────────────────────

    def serve_forever(self):
        """Start the App Bridge service."""
        self._running = True

        # Detect active subsystems
        self.detect_subsystems()

        # Seed default capabilities
        self._seed_default_capabilities()

        # Background: periodic subsystem re-detection
        def _detect_loop():
            while self._running:
                time.sleep(60)
                try:
                    self.detect_subsystems()
                except Exception as e:
                    logger.error(f"Subsystem detection error: {e}")

        threading.Thread(target=_detect_loop, daemon=True).start()

        # Start Flask HTTP server
        app = self._create_flask_app()
        logger.info(f"App Bridge HTTP API starting on port {self.http_port}")

        try:
            from waitress import serve
            serve(app, host='0.0.0.0', port=self.http_port, threads=4)
        except ImportError:
            app.run(host='0.0.0.0', port=self.http_port, threaded=True)
