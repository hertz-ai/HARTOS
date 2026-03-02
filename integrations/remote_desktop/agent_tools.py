"""
Remote Desktop Agent Tools — AutoGen tool definitions for remote desktop.

Agents can programmatically:
  - Offer remote help (start hosting, share device ID + password)
  - Request screen view (connect view-only, take screenshot)
  - Execute remote actions (click, type, key via connected session)
  - Transfer files between devices
  - Manage sessions

Follows core/agent_tools.py pattern:
  build_remote_desktop_tools(ctx) -> List[(name, desc, func)]
  register_remote_desktop_tools(tools, helper, executor)
"""
import logging
from typing import Annotated, Any, List, Optional, Tuple

logger = logging.getLogger('hevolve.remote_desktop')


def build_remote_desktop_tools(ctx) -> List[Tuple[str, str, Any]]:
    """Build remote desktop tool closures for AutoGen agents.

    Args:
        ctx: dict with session variables (user_id, prompt_id, etc.)

    Returns:
        List of (name, description, func) tuples.
    """
    user_id = ctx.get('user_id', 'agent')
    tools: List[Tuple[str, str, Any]] = []

    # ── offer_remote_help ─────────────────────────────────────

    def offer_remote_help(
        allow_control: Annotated[bool, "Allow remote control (False=view-only)"] = True,
    ) -> str:
        """Start hosting this device for remote desktop. Returns Device ID + password."""
        try:
            from integrations.remote_desktop.orchestrator import get_orchestrator
            result = get_orchestrator().start_hosting(
                allow_control=allow_control,
                user_id=user_id,
            )
            if result.get('status') == 'error':
                return f"Failed to start hosting: {result.get('error')}"
            return (
                f"Remote desktop hosting started.\n"
                f"Device ID: {result.get('formatted_id', 'Unknown')}\n"
                f"Password: {result.get('password', 'N/A')}\n"
                f"Mode: {result.get('mode', 'full_control')}\n"
                f"Engine: {result.get('engine', 'auto')}\n"
                f"Share Device ID + Password with the viewer to connect."
            )
        except Exception as e:
            return f"Failed to start hosting: {e}"

    tools.append((
        "offer_remote_help",
        "Start hosting this device for remote desktop access. Returns Device ID and password to share.",
        offer_remote_help,
    ))

    # ── request_screen_view ───────────────────────────────────

    def request_screen_view(
        device_id: Annotated[str, "Remote device ID to connect to"],
        password: Annotated[str, "Access password for the remote device"],
    ) -> str:
        """Connect to a remote device in view-only mode."""
        try:
            from integrations.remote_desktop.orchestrator import get_orchestrator
            result = get_orchestrator().connect(
                device_id=device_id,
                password=password,
                mode='view_only',
                gui=False,
                user_id=user_id,
            )
            if result.get('status') == 'connected':
                return (f"Connected to {device_id} (view-only) "
                        f"via {result.get('engine', 'auto')}")
            return f"Connection failed: {result.get('error', 'Unknown error')}"
        except Exception as e:
            return f"Connection error: {e}"

    tools.append((
        "request_screen_view",
        "Connect to a remote device in view-only mode to observe the screen.",
        request_screen_view,
    ))

    # ── remote_execute_action ─────────────────────────────────

    def remote_execute_action(
        action: Annotated[str, "Action type: click, type, key, hotkey, scroll"],
        x: Annotated[Optional[int], "X coordinate (for click/move)"] = None,
        y: Annotated[Optional[int], "Y coordinate (for click/move)"] = None,
        text: Annotated[Optional[str], "Text to type or key name"] = None,
    ) -> str:
        """Execute a mouse/keyboard action on the connected remote device."""
        try:
            from integrations.remote_desktop.input_handler import InputHandler
            handler = InputHandler()

            event = {'type': action}
            if x is not None:
                event['x'] = x
            if y is not None:
                event['y'] = y
            if text is not None:
                if action in ('type',):
                    event['text'] = text
                elif action in ('key', 'hotkey'):
                    event['key'] = text

            result = handler.handle_input_event(event)
            return f"Action executed: {result}"
        except Exception as e:
            return f"Action failed: {e}"

    tools.append((
        "remote_execute_action",
        "Execute a click, type, key, hotkey, or scroll action on the connected remote device.",
        remote_execute_action,
    ))

    # ── remote_screenshot ─────────────────────────────────────

    def remote_screenshot() -> str:
        """Capture a screenshot of the local or connected remote screen."""
        try:
            from integrations.remote_desktop.frame_capture import FrameCapture
            capture = FrameCapture()
            frame = capture.capture_frame()
            if frame:
                return f"Screenshot captured ({len(frame)} bytes JPEG)"
            return "Screenshot capture failed — no frame returned"
        except Exception as e:
            return f"Screenshot failed: {e}"

    tools.append((
        "remote_screenshot",
        "Capture a screenshot of the current screen (local or remote session).",
        remote_screenshot,
    ))

    # ── remote_transfer_file ──────────────────────────────────

    def remote_transfer_file(
        device_id: Annotated[str, "Target device ID"],
        local_path: Annotated[str, "Local file path to transfer"],
    ) -> str:
        """Transfer a file to a connected remote device via RustDesk."""
        try:
            import os
            if not os.path.exists(local_path):
                return f"File not found: {local_path}"

            from integrations.remote_desktop.rustdesk_bridge import get_rustdesk_bridge
            bridge = get_rustdesk_bridge()
            if not bridge.available:
                return "RustDesk not installed (required for file transfer)"

            ok, msg = bridge.connect(device_id, file_transfer=True)
            if ok:
                return f"File transfer session opened to {device_id}: {msg}"
            return f"File transfer failed: {msg}"
        except Exception as e:
            return f"File transfer error: {e}"

    tools.append((
        "remote_transfer_file",
        "Transfer a file to a remote device using RustDesk file transfer.",
        remote_transfer_file,
    ))

    # ── get_remote_sessions ───────────────────────────────────

    def get_remote_sessions() -> str:
        """List all active remote desktop sessions."""
        try:
            from integrations.remote_desktop.session_manager import get_session_manager
            sm = get_session_manager()
            sessions = sm.get_active_sessions()
            if not sessions:
                return "No active remote desktop sessions."
            lines = [f"Active sessions ({len(sessions)}):"]
            for s in sessions:
                lines.append(
                    f"  {s.session_id[:8]} host={s.host_device_id[:12]} "
                    f"mode={s.mode.value} state={s.state.value} "
                    f"viewers={len(s.viewer_device_ids)}"
                )
            return "\n".join(lines)
        except Exception as e:
            return f"Error listing sessions: {e}"

    tools.append((
        "get_remote_sessions",
        "List all active remote desktop sessions with their status.",
        get_remote_sessions,
    ))

    # ── disconnect_remote ─────────────────────────────────────

    def disconnect_remote(
        session_id: Annotated[Optional[str], "Session ID to disconnect (all if empty)"] = None,
    ) -> str:
        """End a remote desktop session or all sessions."""
        try:
            from integrations.remote_desktop.session_manager import get_session_manager
            sm = get_session_manager()
            if session_id:
                sm.disconnect_session(session_id)
                return f"Disconnected session {session_id[:8]}"
            else:
                sessions = sm.get_active_sessions()
                for s in sessions:
                    sm.disconnect_session(s.session_id)
                return f"Disconnected {len(sessions)} session(s)"
        except Exception as e:
            return f"Disconnect error: {e}"

    tools.append((
        "disconnect_remote",
        "Disconnect a specific remote desktop session or all sessions.",
        disconnect_remote,
    ))

    # ── list_remote_windows ──────────────────────────────────

    def list_remote_windows() -> str:
        """List available application windows on this host for per-window streaming."""
        try:
            from integrations.remote_desktop.orchestrator import get_orchestrator
            windows = get_orchestrator().list_remote_windows()
            if not windows:
                return "No application windows found."
            lines = [f"Available windows ({len(windows)}):"]
            for w in windows:
                lines.append(
                    f"  hwnd={w.get('hwnd')} \"{w.get('title', 'Untitled')}\" "
                    f"({w.get('process_name', 'unknown')})"
                )
            return "\n".join(lines)
        except Exception as e:
            return f"Error listing windows: {e}"

    tools.append((
        "list_remote_windows",
        "List available application windows on the host for per-window streaming (tab detach).",
        list_remote_windows,
    ))

    # ── stream_remote_window ──────────────────────────────────

    def stream_remote_window(
        window_title: Annotated[str, "Window title or pattern to stream"],
    ) -> str:
        """Start streaming a specific application window from the host."""
        try:
            from integrations.remote_desktop.window_capture import WindowEnumerator
            enum = WindowEnumerator()
            winfo = enum.get_window_by_title(window_title)
            if not winfo:
                return f"No window matching '{window_title}' found."

            from integrations.remote_desktop.orchestrator import get_orchestrator
            result = get_orchestrator().stream_window(
                window_hwnd=winfo.hwnd,
                window_title=winfo.title,
            )
            if result.get('status') == 'error':
                return f"Failed: {result.get('error')}"
            return (
                f"Streaming window: {result.get('window_title')}\n"
                f"Session: {result.get('session_id')}\n"
                f"Process: {result.get('process_name', 'unknown')}"
            )
        except Exception as e:
            return f"Error streaming window: {e}"

    tools.append((
        "stream_remote_window",
        "Start streaming a specific application window from the host (tab detach).",
        stream_remote_window,
    ))

    # ── list_peripherals ───────────────────────────────────────

    def list_peripherals(
        types: Annotated[Optional[str], "Filter by type: usb,bluetooth,gamepad (comma-sep, or None for all)"] = None,
    ) -> str:
        """List local peripheral devices available for forwarding to remote host."""
        try:
            from integrations.remote_desktop.orchestrator import get_orchestrator
            type_list = [t.strip() for t in types.split(',')] if types else None
            peripherals = get_orchestrator().list_peripherals(types=type_list)
            if not peripherals:
                return "No peripherals found."
            lines = [f"Peripherals ({len(peripherals)}):"]
            for p in peripherals:
                fwd = " [FORWARDING]" if p.get('forwarded') else ""
                lines.append(
                    f"  {p.get('peripheral_id')} {p.get('name')} "
                    f"type={p.get('type')}{fwd}"
                )
            return "\n".join(lines)
        except Exception as e:
            return f"Error listing peripherals: {e}"

    tools.append((
        "list_peripherals",
        "List local peripheral devices (USB, Bluetooth, gamepad) available for remote forwarding.",
        list_peripherals,
    ))

    # ── forward_peripheral ─────────────────────────────────────

    def forward_peripheral(
        peripheral_id: Annotated[str, "Peripheral ID to forward"],
        session_id: Annotated[Optional[str], "Remote session ID (auto-detect if empty)"] = None,
    ) -> str:
        """Forward a local peripheral device to the connected remote host."""
        try:
            from integrations.remote_desktop.orchestrator import get_orchestrator
            orch = get_orchestrator()

            # Auto-detect session if not provided
            if not session_id:
                sessions = orch.get_status().get('active_sessions', [])
                if sessions:
                    session_id = sessions[0].get('session_id', '')
                else:
                    return "No active session. Connect to a remote host first."

            result = orch.forward_peripheral(session_id, peripheral_id)
            if result.get('success'):
                return (
                    f"Forwarding {result.get('type', 'device')}: {result.get('name')}\n"
                    f"Peripheral ID: {result.get('peripheral_id')}"
                )
            return f"Forward failed: {result.get('error', 'Unknown error')}"
        except Exception as e:
            return f"Forward error: {e}"

    tools.append((
        "forward_peripheral",
        "Forward a local peripheral (USB, Bluetooth, gamepad) to the connected remote host.",
        forward_peripheral,
    ))

    # ── discover_cast_targets ──────────────────────────────────

    def discover_cast_targets() -> str:
        """Discover DLNA/UPnP renderers (smart TVs, speakers) on the local network."""
        try:
            from integrations.remote_desktop.orchestrator import get_orchestrator
            targets = get_orchestrator().discover_cast_targets()
            if not targets:
                return "No DLNA/UPnP renderers found on the network."
            lines = [f"Cast targets ({len(targets)}):"]
            for t in targets:
                lines.append(
                    f"  {t.get('device_id', 'unknown')[:12]} "
                    f"\"{t.get('friendly_name', 'Unknown')}\" "
                    f"at {t.get('ip', '?')}:{t.get('port', '?')}"
                )
            return "\n".join(lines)
        except Exception as e:
            return f"Error discovering cast targets: {e}"

    tools.append((
        "discover_cast_targets",
        "Discover DLNA/UPnP renderers (smart TVs, speakers) for screen casting.",
        discover_cast_targets,
    ))

    # ── cast_to_tv ─────────────────────────────────────────────

    def cast_to_tv(
        renderer_id: Annotated[str, "DLNA renderer device ID to cast to"],
        session_id: Annotated[Optional[str], "Session to cast (auto-detect if empty)"] = None,
    ) -> str:
        """Cast a remote desktop session to a DLNA/UPnP renderer (smart TV)."""
        try:
            from integrations.remote_desktop.orchestrator import get_orchestrator
            orch = get_orchestrator()

            if not session_id:
                sessions = orch.get_status().get('active_sessions', [])
                if sessions:
                    session_id = sessions[0].get('session_id', '')
                else:
                    return "No active session to cast."

            result = orch.cast_to_device(session_id, renderer_id)
            if result.get('success'):
                return (
                    f"Casting to: {result.get('renderer_name', 'Unknown')}\n"
                    f"Stream URL: {result.get('stream_url', 'N/A')}\n"
                    f"Cast session: {result.get('cast_session_id', 'N/A')}"
                )
            return f"Cast failed: {result.get('error', 'Unknown error')}"
        except Exception as e:
            return f"Cast error: {e}"

    tools.append((
        "cast_to_tv",
        "Cast a remote desktop session to a smart TV or DLNA/UPnP renderer.",
        cast_to_tv,
    ))

    return tools


def register_remote_desktop_tools(tools, helper, executor):
    """Register remote desktop tools on an AutoGen helper/executor pair.

    Same pattern as core/agent_tools.py:register_core_tools().
    """
    for name, desc, func in tools:
        helper.register_for_llm(name=name, description=desc)(func)
        executor.register_for_execution(name=name)(func)
