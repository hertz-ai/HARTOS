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
            from integrations.remote_desktop.device_id import get_device_id, format_device_id
            from integrations.remote_desktop.session_manager import (
                get_session_manager, SessionMode,
            )

            device_id = get_device_id()
            sm = get_session_manager()
            mode = SessionMode.FULL_CONTROL if allow_control else SessionMode.VIEW_ONLY
            password = sm.generate_otp(device_id)

            # Try to start RustDesk
            engine = 'native'
            try:
                from integrations.remote_desktop.rustdesk_bridge import get_rustdesk_bridge
                bridge = get_rustdesk_bridge()
                if bridge.available:
                    bridge.set_password(password)
                    bridge.start_service()
                    engine = 'rustdesk'
            except Exception:
                pass

            formatted = format_device_id(device_id)
            return (
                f"Remote desktop hosting started.\n"
                f"Device ID: {formatted}\n"
                f"Password: {password}\n"
                f"Mode: {mode.value}\n"
                f"Engine: {engine}\n"
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
            from integrations.remote_desktop.rustdesk_bridge import get_rustdesk_bridge
            bridge = get_rustdesk_bridge()
            if bridge.available:
                ok, msg = bridge.connect(device_id, password=password)
                if ok:
                    return f"Connected to {device_id} (view-only): {msg}"
                return f"Connection failed: {msg}"
            return "No remote desktop engine available. Install RustDesk."
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

    return tools


def register_remote_desktop_tools(tools, helper, executor):
    """Register remote desktop tools on an AutoGen helper/executor pair.

    Same pattern as core/agent_tools.py:register_core_tools().
    """
    for name, desc, func in tools:
        helper.register_for_llm(name=name, description=desc)(func)
        executor.register_for_execution(name=name)(func)
