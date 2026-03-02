"""
Remote Desktop Orchestrator — Unified AI-native coordinator for HARTOS Remote Desktop.

This is THE single entry point for all remote desktop operations. It coordinates:
  - Engine selection (RustDesk vs Sunshine/Moonlight vs Native)
  - Service lifecycle (start, stop, health via ServiceManager)
  - Session management (create, authenticate, disconnect via SessionManager)
  - Cross-app clipboard bridge (works regardless of which engine)
  - File transfer (engine-specific routing)
  - AI-native features (context-aware engine switching, smart connect)

Key insight: HARTOS doesn't replace RustDesk/Sunshine — it orchestrates them
as engines the way the coding agent orchestrates Aider/KiloCode/ClaudeCode.
The user never thinks about which engine is running.
"""

import logging
import threading
import time
from typing import Any, Callable, Dict, List, Optional, Tuple

logger = logging.getLogger('hevolve.remote_desktop')


class RemoteDesktopOrchestrator:
    """AI-native remote desktop orchestrator.

    Composes ServiceManager, SessionManager, EngineSelector, ClipboardSync,
    and all engine bridges into a unified interface.
    """

    def __init__(self):
        self._active_sessions: Dict[str, dict] = {}  # session_id → session_info
        self._clipboard_syncs: Dict[str, Any] = {}   # session_id → ClipboardSync
        self._lock = threading.Lock()
        self._started = False

    # ── Lifecycle ─────────────────────────────────────────────

    def startup(self) -> dict:
        """Initialize the orchestrator at HARTOS boot.

        Starts ServiceManager, detects engines, registers with NodeWatchdog.
        """
        if self._started:
            return self.get_status()

        try:
            from integrations.remote_desktop.service_manager import get_service_manager
            sm = get_service_manager()
            engine_status = sm.start_all_available()
            sm.register_with_watchdog()
            self._started = True
            logger.info("Remote Desktop Orchestrator started")
            return {
                'status': 'started',
                'engines': engine_status,
            }
        except Exception as e:
            logger.error(f"Orchestrator startup failed: {e}")
            return {'status': 'error', 'error': str(e)}

    def shutdown(self) -> None:
        """Clean shutdown — disconnect all sessions, stop clipboard, stop engines."""
        # Disconnect all sessions
        session_ids = list(self._active_sessions.keys())
        for sid in session_ids:
            try:
                self.disconnect(sid)
            except Exception:
                pass

        # Stop all engines
        try:
            from integrations.remote_desktop.service_manager import get_service_manager
            get_service_manager().stop_all()
        except Exception:
            pass

        self._started = False
        logger.info("Remote Desktop Orchestrator shut down")

    # ── Host Operations ──────────────────────────────────────

    def start_hosting(self, engine: str = 'auto', allow_control: bool = True,
                      use_case: str = 'general', user_id: Optional[str] = None) -> dict:
        """Start hosting this device for remote desktop access.

        Args:
            engine: Engine to use ('auto', 'rustdesk', 'sunshine', 'native')
            allow_control: Allow remote input (False = view-only)
            use_case: Context hint for engine selection
            user_id: Host user ID for session tracking

        Returns:
            {device_id, password, engine, session_id, status}
        """
        # 1. Get device ID
        try:
            from integrations.remote_desktop.device_id import get_device_id, format_device_id
            device_id = get_device_id()
            formatted_id = format_device_id(device_id)
        except Exception as e:
            return {'status': 'error', 'error': f'Device ID unavailable: {e}'}

        # 2. Select engine
        selected_engine = self._resolve_engine(engine, use_case, role='host')

        # 3. Ensure engine is running
        from integrations.remote_desktop.service_manager import get_service_manager
        svc = get_service_manager()
        ready, msg = svc.ensure_engine(selected_engine)
        if not ready:
            return {'status': 'error', 'error': msg, 'engine': selected_engine}

        # 4. Generate password (OTP)
        from integrations.remote_desktop.session_manager import (
            get_session_manager, SessionMode,
        )
        sm = get_session_manager()
        password = sm.generate_otp(device_id)

        # 5. Engine-specific setup
        engine_info = {}
        if selected_engine == 'rustdesk':
            engine_info = self._setup_rustdesk_host(password)
        elif selected_engine == 'sunshine':
            engine_info = self._setup_sunshine_host()

        # 6. Create session
        mode = SessionMode.FULL_CONTROL if allow_control else SessionMode.VIEW_ONLY
        session = sm.create_session(
            host_device_id=device_id,
            viewer_device_id='pending',
            mode=mode,
            host_user_id=user_id,
        )

        # 7. Track session
        session_info = {
            'session_id': session.session_id,
            'device_id': device_id,
            'formatted_id': formatted_id,
            'password': password,
            'engine': selected_engine,
            'mode': mode.value,
            'user_id': user_id,
            'started_at': time.time(),
            **engine_info,
        }
        with self._lock:
            self._active_sessions[session.session_id] = session_info

        # 8. Audit
        self._audit('host_started', session.session_id, user_id or device_id,
                     f'Engine: {selected_engine}, Mode: {mode.value}')

        logger.info(f"Hosting started: {formatted_id} via {selected_engine}")
        return {
            'status': 'hosting',
            'device_id': device_id,
            'formatted_id': formatted_id,
            'password': password,
            'engine': selected_engine,
            'session_id': session.session_id,
            'mode': mode.value,
        }

    def stop_hosting(self, session_id: Optional[str] = None) -> bool:
        """Stop hosting a session or all sessions."""
        if session_id:
            return self.disconnect(session_id)

        # Stop all hosting sessions
        with self._lock:
            host_sessions = [
                sid for sid, info in self._active_sessions.items()
                if info.get('device_id')  # Has device_id → is a host session
            ]
        for sid in host_sessions:
            self.disconnect(sid)
        return True

    # ── Viewer Operations ────────────────────────────────────

    def connect(self, device_id: str, password: str,
                mode: str = 'full_control', engine: str = 'auto',
                use_case: str = 'general', gui: bool = True,
                user_id: Optional[str] = None) -> dict:
        """Connect to a remote device.

        Args:
            device_id: Remote device's ID
            password: Access password / OTP
            mode: 'full_control', 'view_only', or 'file_transfer'
            engine: Engine preference
            use_case: Context hint for engine selection
            gui: Open GUI window (False for headless/agent use)
            user_id: Viewer user ID for session tracking

        Returns:
            {session_id, engine, status}
        """
        # 1. Select engine
        if mode == 'file_transfer':
            use_case = 'file_transfer'
        selected_engine = self._resolve_engine(engine, use_case, role='viewer')

        # 2. Ensure engine is ready
        from integrations.remote_desktop.service_manager import get_service_manager
        svc = get_service_manager()
        ready, msg = svc.ensure_engine(selected_engine)
        if not ready:
            return {'status': 'error', 'error': msg, 'engine': selected_engine}

        # 3. Authenticate
        auth_ok, auth_msg = self._authenticate(device_id, password, user_id)
        if not auth_ok:
            return {'status': 'auth_failed', 'error': auth_msg}

        # 4. Engine-specific connection
        connect_result = {}
        if selected_engine == 'rustdesk':
            connect_result = self._connect_rustdesk(device_id, password,
                                                     mode == 'file_transfer')
        elif selected_engine == 'moonlight':
            connect_result = self._connect_moonlight(device_id, gui)
        elif selected_engine == 'native':
            connect_result = self._connect_native(device_id, password)

        if connect_result.get('status') == 'error':
            return connect_result

        # 5. Create session
        from integrations.remote_desktop.session_manager import (
            get_session_manager, SessionMode,
        )
        sm = get_session_manager()
        mode_enum = {
            'full_control': SessionMode.FULL_CONTROL,
            'view_only': SessionMode.VIEW_ONLY,
            'file_transfer': SessionMode.FILE_TRANSFER,
        }.get(mode, SessionMode.FULL_CONTROL)

        from integrations.remote_desktop.device_id import get_device_id
        local_device_id = get_device_id()

        session = sm.create_session(
            host_device_id=device_id,
            viewer_device_id=local_device_id,
            mode=mode_enum,
            viewer_user_id=user_id,
        )

        # 6. Start clipboard bridge
        self._start_clipboard_bridge(session.session_id, selected_engine)

        # 7. Track session
        session_info = {
            'session_id': session.session_id,
            'remote_device_id': device_id,
            'engine': selected_engine,
            'mode': mode,
            'user_id': user_id,
            'gui': gui,
            'connected_at': time.time(),
            **connect_result,
        }
        with self._lock:
            self._active_sessions[session.session_id] = session_info

        # 8. Audit
        self._audit('viewer_connected', session.session_id,
                     user_id or local_device_id,
                     f'Remote: {device_id}, Engine: {selected_engine}')

        logger.info(f"Connected to {device_id} via {selected_engine}")
        return {
            'status': 'connected',
            'session_id': session.session_id,
            'engine': selected_engine,
            'mode': mode,
        }

    def disconnect(self, session_id: Optional[str] = None) -> bool:
        """Disconnect a session or all sessions.

        Args:
            session_id: Specific session to disconnect. None = disconnect all.
        """
        if session_id is None:
            sessions = list(self._active_sessions.keys())
            for sid in sessions:
                self._disconnect_one(sid)
            return True

        return self._disconnect_one(session_id)

    def _disconnect_one(self, session_id: str) -> bool:
        """Disconnect a single session."""
        with self._lock:
            info = self._active_sessions.pop(session_id, None)

        if not info:
            return False

        # Stop clipboard sync
        clipboard = self._clipboard_syncs.pop(session_id, None)
        if clipboard:
            clipboard.stop_monitoring()

        # Disconnect session manager
        try:
            from integrations.remote_desktop.session_manager import get_session_manager
            get_session_manager().disconnect_session(session_id)
        except Exception:
            pass

        # Audit
        self._audit('session_disconnected', session_id,
                     info.get('user_id', 'unknown'),
                     f'Engine: {info.get("engine", "unknown")}')

        logger.info(f"Session {session_id[:8]} disconnected")
        return True

    # ── AI-Native Operations ─────────────────────────────────

    def smart_connect(self, device_id: str, password: str,
                      context: Optional[dict] = None,
                      user_id: Optional[str] = None) -> dict:
        """AI-driven connection — auto-select engine and mode from context.

        Context examples:
          {'intent': 'file_transfer'} → RustDesk file transfer mode
          {'intent': 'gaming'}         → Moonlight
          {'intent': 'support'}        → RustDesk full control
          {'intent': 'observe'}        → View-only mode
          {'app': 'game'}              → Moonlight for low-latency
        """
        if context is None:
            context = {}

        intent = context.get('intent', 'general')
        app = context.get('app', '')

        # Infer use case
        use_case = 'general'
        mode = 'full_control'

        if intent == 'file_transfer' or 'transfer' in intent:
            use_case = 'file_transfer'
            mode = 'file_transfer'
        elif intent in ('gaming', 'game') or 'game' in app.lower():
            use_case = 'gaming'
        elif intent == 'support':
            use_case = 'remote_support'
        elif intent in ('observe', 'view', 'watch'):
            mode = 'view_only'
        elif intent in ('app_streaming', 'tab_detach', 'window'):
            use_case = 'general'
            mode = 'full_control'
        elif intent == 'vlm':
            use_case = 'vlm_computer_use'

        gui = context.get('gui', True)

        return self.connect(
            device_id=device_id,
            password=password,
            mode=mode,
            engine='auto',
            use_case=use_case,
            gui=gui,
            user_id=user_id,
        )

    def switch_engine(self, session_id: str, new_engine: str) -> dict:
        """Switch engine mid-session (e.g., RustDesk → Moonlight for gaming).

        Preserves clipboard bridge, transfers session state.
        """
        with self._lock:
            info = self._active_sessions.get(session_id)
        if not info:
            return {'status': 'error', 'error': 'Session not found'}

        old_engine = info.get('engine', 'unknown')
        if old_engine == new_engine:
            return {'status': 'no_change', 'engine': old_engine}

        remote_device_id = info.get('remote_device_id')
        if not remote_device_id:
            return {'status': 'error', 'error': 'Not a viewer session'}

        # Disconnect old engine connection (keep session alive)
        self._disconnect_engine(old_engine, remote_device_id)

        # Ensure new engine
        from integrations.remote_desktop.service_manager import get_service_manager
        ready, msg = get_service_manager().ensure_engine(new_engine)
        if not ready:
            # Re-connect old engine
            return {'status': 'error', 'error': f'Cannot switch to {new_engine}: {msg}'}

        # Connect new engine
        password = info.get('password', '')
        mode = info.get('mode', 'full_control')
        connect_result = {}
        if new_engine == 'rustdesk':
            connect_result = self._connect_rustdesk(remote_device_id, password,
                                                     mode == 'file_transfer')
        elif new_engine == 'moonlight':
            connect_result = self._connect_moonlight(remote_device_id, info.get('gui', True))
        elif new_engine == 'native':
            connect_result = self._connect_native(remote_device_id, password)

        # Update session info
        with self._lock:
            if session_id in self._active_sessions:
                self._active_sessions[session_id]['engine'] = new_engine
                self._active_sessions[session_id]['switched_at'] = time.time()

        self._audit('engine_switched', session_id, info.get('user_id', 'unknown'),
                     f'{old_engine} → {new_engine}')

        logger.info(f"Session {session_id[:8]} switched {old_engine} → {new_engine}")
        return {
            'status': 'switched',
            'old_engine': old_engine,
            'new_engine': new_engine,
            'session_id': session_id,
        }

    def recommend_engine_switch(self, session_id: str) -> Optional[dict]:
        """AI-native: Check if a better engine is available for the current session.

        Returns recommendation dict or None if no switch recommended.
        """
        with self._lock:
            info = self._active_sessions.get(session_id)
        if not info:
            return None

        current = info.get('engine', 'native')
        mode = info.get('mode', 'full_control')

        try:
            from integrations.remote_desktop.engine_selector import (
                get_available_engines, Engine, UseCase,
            )
            available = get_available_engines()

            # File transfer on non-RustDesk → suggest RustDesk
            if mode == 'file_transfer' and current != 'rustdesk':
                if Engine.RUSTDESK in available:
                    return {
                        'recommend': 'rustdesk',
                        'reason': 'RustDesk has native file transfer support',
                        'current': current,
                    }

            # Gaming/VLM on RustDesk → suggest Moonlight
            if current == 'rustdesk' and mode != 'file_transfer':
                if Engine.MOONLIGHT in available:
                    return {
                        'recommend': 'moonlight',
                        'reason': 'Moonlight offers lower latency for interactive use',
                        'current': current,
                    }
        except Exception:
            pass

        return None

    # ── Cross-App Clipboard Bridge ───────────────────────────

    def _start_clipboard_bridge(self, session_id: str, engine: str) -> bool:
        """Start clipboard bridge for a session.

        Works across engine boundaries — HARTOS-level clipboard monitoring.
        """
        try:
            from integrations.remote_desktop.clipboard_sync import ClipboardSync

            def on_clipboard_change(content):
                self._handle_clipboard_outbound(session_id, engine, content)

            sync = ClipboardSync(on_change=on_clipboard_change, dlp_enabled=True)
            started = sync.start_monitoring()
            if started:
                self._clipboard_syncs[session_id] = sync
                logger.debug(f"Clipboard bridge started for session {session_id[:8]}")
            return started
        except Exception as e:
            logger.debug(f"Clipboard bridge failed: {e}")
            return False

    def _handle_clipboard_outbound(self, session_id: str, engine: str,
                                    content: str) -> None:
        """Push local clipboard change to remote via active engine."""
        # Each engine handles clipboard differently:
        # - RustDesk: clipboard flows through its own protocol
        # - Sunshine/Moonlight: no cross-clipboard API — use native fallback
        # - Native: send via transport channel

        if engine == 'native':
            # Send via transport as clipboard event
            logger.debug(f"Clipboard → remote via native transport ({len(content)} chars)")
        # For RustDesk/Sunshine, clipboard sync is handled by the engine itself.
        # Our clipboard bridge catches what leaks through to ensure nothing is missed.

    # ── Window Streaming (Tab Detach) ─────────────────────────

    def list_remote_windows(self, session_id: Optional[str] = None) -> List[dict]:
        """List available windows on the local host for per-window streaming.

        If a session_id is provided, sends a list_windows request to the remote
        host via the session's transport. Otherwise, lists local windows.
        """
        try:
            from integrations.remote_desktop.window_session import (
                get_window_session_manager,
            )
            return get_window_session_manager().list_available_windows()
        except Exception as e:
            return [{'error': str(e)}]

    def stream_window(self, window_hwnd: int,
                       window_title: str = '',
                       transport=None) -> dict:
        """Start streaming a specific window (creates a sub-session)."""
        try:
            from integrations.remote_desktop.window_session import (
                get_window_session_manager,
            )
            return get_window_session_manager().start_window_session(
                window_hwnd=window_hwnd,
                window_title=window_title,
                transport=transport,
            )
        except Exception as e:
            return {'status': 'error', 'error': str(e)}

    def stop_window_stream(self, window_session_id: str) -> bool:
        """Stop a window stream."""
        try:
            from integrations.remote_desktop.window_session import (
                get_window_session_manager,
            )
            return get_window_session_manager().stop_window_session(
                window_session_id)
        except Exception:
            return False

    def detach_tab(self, window_session_id: str) -> dict:
        """Detach a window stream into a standalone viewer."""
        try:
            from integrations.remote_desktop.window_session import (
                get_window_session_manager,
            )
            return get_window_session_manager().detach_window(
                window_session_id)
        except Exception as e:
            return {'status': 'error', 'error': str(e)}

    def get_window_sessions(self) -> List[dict]:
        """Get all active window streaming sessions."""
        try:
            from integrations.remote_desktop.window_session import (
                get_window_session_manager,
            )
            return get_window_session_manager().get_active_window_sessions()
        except Exception:
            return []

    # ── Peripheral Forwarding ─────────────────────────────────

    def list_peripherals(self,
                          types: Optional[list] = None) -> List[dict]:
        """Discover locally connected peripherals (USB, BT, gamepad)."""
        try:
            from integrations.remote_desktop.peripheral_bridge import (
                get_peripheral_bridge,
            )
            peripherals = get_peripheral_bridge().discover_peripherals(types)
            return [p.to_dict() for p in peripherals]
        except Exception as e:
            return [{'error': str(e)}]

    def forward_peripheral(self, session_id: str,
                           peripheral_id: str) -> dict:
        """Forward a local peripheral to the remote device in a session."""
        with self._lock:
            info = self._active_sessions.get(session_id)
        if not info:
            return {'status': 'error', 'error': 'Session not found'}

        try:
            from integrations.remote_desktop.peripheral_bridge import (
                get_peripheral_bridge,
            )
            # Get the transport for this session (native only)
            transport = info.get('transport')
            result = get_peripheral_bridge().forward_peripheral(
                peripheral_id, transport, session_id)
            return result
        except Exception as e:
            return {'success': False, 'error': str(e)}

    def stop_peripheral_forwarding(self,
                                    peripheral_id: str) -> bool:
        """Stop forwarding a peripheral."""
        try:
            from integrations.remote_desktop.peripheral_bridge import (
                get_peripheral_bridge,
            )
            return get_peripheral_bridge().stop_forwarding(peripheral_id)
        except Exception:
            return False

    # ── DLNA Screen Casting ──────────────────────────────────

    def discover_cast_targets(self,
                               timeout: float = 5.0) -> List[dict]:
        """Discover DLNA/UPnP renderers on the local network."""
        try:
            from integrations.remote_desktop.dlna_bridge import get_dlna_bridge
            renderers = get_dlna_bridge().discover_renderers(timeout)
            return [r.to_dict() for r in renderers]
        except Exception as e:
            return [{'error': str(e)}]

    def cast_to_device(self, session_id: str,
                        renderer_id: str,
                        stream_port: int = 0) -> dict:
        """Cast a remote desktop session to a DLNA renderer."""
        try:
            from integrations.remote_desktop.dlna_bridge import get_dlna_bridge
            return get_dlna_bridge().cast_session(
                session_id, renderer_id, stream_port=stream_port)
        except Exception as e:
            return {'success': False, 'error': str(e)}

    def stop_cast(self, cast_session_id: str) -> bool:
        """Stop a DLNA cast session."""
        try:
            from integrations.remote_desktop.dlna_bridge import get_dlna_bridge
            return get_dlna_bridge().stop_cast(cast_session_id)
        except Exception:
            return False

    def get_cast_status(self) -> List[dict]:
        """Get status of all active DLNA cast sessions."""
        try:
            from integrations.remote_desktop.dlna_bridge import get_dlna_bridge
            return get_dlna_bridge().get_cast_status()
        except Exception:
            return []

    # ── File Transfer ────────────────────────────────────────

    def transfer_file(self, session_id: str, local_path: str) -> dict:
        """Transfer a file to remote device via the session's engine."""
        import os
        if not os.path.exists(local_path):
            return {'status': 'error', 'error': f'File not found: {local_path}'}

        with self._lock:
            info = self._active_sessions.get(session_id)
        if not info:
            return {'status': 'error', 'error': 'Session not found'}

        # DLP scan
        try:
            from integrations.remote_desktop.security import scan_file_transfer
            allowed, reason = scan_file_transfer(os.path.basename(local_path))
            if not allowed:
                return {'status': 'blocked', 'error': f'DLP: {reason}'}
        except Exception:
            pass

        engine = info.get('engine', 'native')
        remote_id = info.get('remote_device_id', '')

        if engine == 'rustdesk':
            try:
                from integrations.remote_desktop.rustdesk_bridge import get_rustdesk_bridge
                ok, msg = get_rustdesk_bridge().connect(remote_id, file_transfer=True)
                return {'status': 'transferring' if ok else 'error',
                        'message': msg, 'engine': engine}
            except Exception as e:
                return {'status': 'error', 'error': str(e)}

        return {
            'status': 'error',
            'error': f'File transfer not supported via {engine}. Switch to RustDesk.',
            'recommendation': 'Use switch_engine() to switch to RustDesk for file transfer.',
        }

    # ── Status ───────────────────────────────────────────────

    def get_status(self) -> dict:
        """Get unified orchestrator status."""
        # Engine status
        engine_status = {}
        try:
            from integrations.remote_desktop.service_manager import get_service_manager
            engine_status = get_service_manager().get_all_status()
        except Exception as e:
            engine_status = {'error': str(e)}

        # Active sessions
        with self._lock:
            sessions = [
                {
                    'session_id': sid,
                    'engine': info.get('engine'),
                    'mode': info.get('mode'),
                    'remote_device_id': info.get('remote_device_id'),
                    'device_id': info.get('device_id'),
                    'clipboard_active': sid in self._clipboard_syncs,
                }
                for sid, info in self._active_sessions.items()
            ]

        # Device ID
        device_id = None
        formatted_id = None
        try:
            from integrations.remote_desktop.device_id import get_device_id, format_device_id
            device_id = get_device_id()
            formatted_id = format_device_id(device_id)
        except Exception:
            pass

        # Window sessions
        window_sessions = self.get_window_sessions()

        # Peripherals
        peripheral_status = {}
        try:
            from integrations.remote_desktop.peripheral_bridge import (
                get_peripheral_bridge,
            )
            peripheral_status = get_peripheral_bridge().get_status()
        except Exception:
            pass

        # DLNA casts
        cast_status = self.get_cast_status()

        return {
            'started': self._started,
            'device_id': device_id,
            'formatted_id': formatted_id,
            'engines': engine_status,
            'sessions': sessions,
            'active_session_count': len(sessions),
            'window_sessions': window_sessions,
            'window_session_count': len(window_sessions),
            'peripherals': peripheral_status,
            'casts': cast_status,
            'cast_count': len(cast_status),
        }

    def get_sessions(self) -> List[dict]:
        """Get list of active sessions."""
        with self._lock:
            return list(self._active_sessions.values())

    # ── Internal Helpers ─────────────────────────────────────

    def _resolve_engine(self, engine: str, use_case: str, role: str) -> str:
        """Resolve 'auto' engine to a specific engine name."""
        if engine != 'auto':
            return engine

        try:
            from integrations.remote_desktop.engine_selector import (
                select_engine, UseCase, Engine,
            )
            uc = {
                'general': UseCase.GENERAL,
                'remote_support': UseCase.REMOTE_SUPPORT,
                'file_transfer': UseCase.FILE_TRANSFER,
                'gaming': UseCase.GAMING,
                'vlm_computer_use': UseCase.VLM_COMPUTER_USE,
            }.get(use_case, UseCase.GENERAL)

            result = select_engine(use_case=uc, role=role)
            return result.value
        except Exception:
            return 'native'

    def _authenticate(self, device_id: str, password: str,
                       user_id: Optional[str] = None) -> Tuple[bool, str]:
        """Authenticate connection attempt."""
        try:
            from integrations.remote_desktop.security import authenticate_connection
            from integrations.remote_desktop.device_id import get_device_id
            local_id = get_device_id()
            return authenticate_connection(
                host_device_id=device_id,
                viewer_device_id=local_id,
                password=password,
                viewer_user_id=user_id,
            )
        except Exception as e:
            # If security module fails, allow with warning
            logger.warning(f"Auth module unavailable, allowing connection: {e}")
            return True, 'auth_bypassed'

    def _setup_rustdesk_host(self, password: str) -> dict:
        """Configure RustDesk for hosting."""
        try:
            from integrations.remote_desktop.rustdesk_bridge import get_rustdesk_bridge
            bridge = get_rustdesk_bridge()
            bridge.set_password(password)
            bridge.start_service()
            rustdesk_id = bridge.get_id()
            return {'rustdesk_id': rustdesk_id}
        except Exception as e:
            logger.warning(f"RustDesk host setup: {e}")
            return {}

    def _setup_sunshine_host(self) -> dict:
        """Ensure Sunshine is running for hosting."""
        try:
            from integrations.remote_desktop.sunshine_bridge import get_sunshine_bridge
            bridge = get_sunshine_bridge()
            if not bridge.is_running():
                bridge.start_service()
            clients = bridge.get_paired_clients() or []
            return {'sunshine_clients': len(clients)}
        except Exception as e:
            logger.warning(f"Sunshine host setup: {e}")
            return {}

    def _connect_rustdesk(self, device_id: str, password: str,
                           file_transfer: bool = False) -> dict:
        """Connect via RustDesk."""
        try:
            from integrations.remote_desktop.rustdesk_bridge import get_rustdesk_bridge
            bridge = get_rustdesk_bridge()
            ok, msg = bridge.connect(device_id, password=password,
                                      file_transfer=file_transfer)
            if ok:
                return {'status': 'connected', 'message': msg}
            return {'status': 'error', 'error': msg}
        except Exception as e:
            return {'status': 'error', 'error': str(e)}

    def _connect_moonlight(self, host: str, gui: bool = True) -> dict:
        """Connect via Moonlight."""
        try:
            from integrations.remote_desktop.sunshine_bridge import get_moonlight_bridge
            bridge = get_moonlight_bridge()
            ok, msg = bridge.stream(host)
            if ok:
                return {'status': 'connected', 'message': msg}
            return {'status': 'error', 'error': msg}
        except Exception as e:
            return {'status': 'error', 'error': str(e)}

    def _connect_native(self, device_id: str, password: str) -> dict:
        """Connect via native HARTOS transport (fallback)."""
        # Native transport uses frame_capture + transport + input_handler
        # Full implementation in host_service.py / viewer_client.py
        return {'status': 'connected', 'transport': 'native'}

    def _disconnect_engine(self, engine: str, device_id: str) -> None:
        """Disconnect engine-specific connection (for engine switching)."""
        try:
            if engine == 'rustdesk':
                from integrations.remote_desktop.rustdesk_bridge import get_rustdesk_bridge
                get_rustdesk_bridge().disconnect_all()
        except Exception:
            pass

    def _audit(self, event_type: str, session_id: str, actor_id: str,
               detail: Optional[str] = None) -> None:
        """Audit log a session event."""
        try:
            from integrations.remote_desktop.security import audit_session_event
            audit_session_event(event_type, session_id, actor_id, detail)
        except Exception:
            pass


# ── Singleton ────────────────────────────────────────────────

_orchestrator: Optional[RemoteDesktopOrchestrator] = None


def get_orchestrator() -> RemoteDesktopOrchestrator:
    """Get or create the singleton RemoteDesktopOrchestrator."""
    global _orchestrator
    if _orchestrator is None:
        _orchestrator = RemoteDesktopOrchestrator()
    return _orchestrator
