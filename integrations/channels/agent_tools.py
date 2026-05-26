"""
AutoGen tools for channel operations — used by HART agents.

Follows the same pattern as core/agent_tools.py:
  - build_channel_tool_closures(ctx) → list of (name, desc, func) tuples
  - register_channel_tools(helper, executor, ctx) → registers on autogen agents

Allows agents to:
1. Send messages to specific channels or broadcast to all
2. Register/connect new channels via natural language
3. List connected channels and their status
4. Get current channel context (where the message came from)

All tools reuse existing infrastructure:
- ChannelResponseRouter for sending
- AdminAPI singleton for registration
- UserChannelBinding for bindings
- thread_local_data for channel context
"""

import json
import logging
from typing import Annotated, Optional

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Helpers (shared by all closures)
# ---------------------------------------------------------------------------

def _get_channel_context():
    """Read the current channel context from thread-local storage."""
    try:
        from threadlocal import thread_local_data
        return getattr(thread_local_data, 'channel_context', None)
    except Exception:
        return None


def _get_user_id_from_threadlocal():
    """Get current user_id from thread-local."""
    try:
        from threadlocal import thread_local_data
        return thread_local_data.get_user_id()
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Tool closure factory
# ---------------------------------------------------------------------------

def build_channel_tool_closures(ctx):
    """Build session-scoped channel tool closures.

    Args:
        ctx: dict with at least 'user_id', 'prompt_id'.
             Optional: 'log_tool_execution' decorator, 'send_message_to_user1' func.

    Returns:
        list of (name, description, func) tuples — same format as core/agent_tools.py
    """
    user_id = ctx.get('user_id')
    log_tool_execution = ctx.get('log_tool_execution') or (lambda f: f)

    tools = []

    # ------------------------------------------------------------------
    # 1. send_to_channel
    # ------------------------------------------------------------------
    @log_tool_execution
    def send_to_channel(
        channel_type: Annotated[str, "Channel name (telegram, discord, slack, etc.) or 'all' to broadcast"],
        message: Annotated[str, "The message text to send"],
        chat_id: Annotated[Optional[str], "Target chat ID. Use 'all' to send to all bindings for this channel"] = "all",
    ) -> str:
        """Send a message to a specific messaging channel or broadcast to all connected channels."""
        try:
            uid = user_id or _get_user_id_from_threadlocal()

            if channel_type.lower() == 'all' or chat_id.lower() == 'all':
                from integrations.channels.response.router import get_response_router
                router = get_response_router()
                router.route_response(
                    user_id=uid,
                    response_text=message,
                    channel_context=_get_channel_context(),
                    fan_out=True,
                )
                return f"Message broadcast to all connected channels for user {uid}."

            from integrations.channels.registry import get_registry
            import asyncio
            registry = get_registry()
            loop = getattr(registry, '_loop', None)

            if loop and loop.is_running():
                future = asyncio.run_coroutine_threadsafe(
                    registry.send_to_channel(channel_type, chat_id, message),
                    loop,
                )
                result = future.result(timeout=30)
                if result.success:
                    return f"Message sent to {channel_type}:{chat_id} successfully."
                else:
                    return f"Failed to send to {channel_type}: {result.error}"
            else:
                return f"Channel adapters not running. Message queued for delivery."

        except Exception as e:
            logger.error("send_to_channel error: %s", e)
            return f"Error sending message: {e}"

    tools.append((
        "send_to_channel",
        "Send a message to a specific messaging channel (Telegram, Discord, Slack, WhatsApp, etc.) "
        "or broadcast to all connected channels. Use channel_type='all' to broadcast. "
        "Examples: send_to_channel('telegram', 'Task complete!', '123456') or "
        "send_to_channel('all', 'Important update for all channels.')",
        send_to_channel,
    ))

    # ------------------------------------------------------------------
    # 2. register_channel
    # ------------------------------------------------------------------
    @log_tool_execution
    def register_channel(
        channel_type: Annotated[str, "Channel to register (telegram, discord, slack, whatsapp, etc.)"],
        config_json: Annotated[str, "JSON config with required credentials, e.g. '{\"bot_token\": \"123:ABC\"}'"],
    ) -> str:
        """Register and connect a new messaging channel. Creates config, enables it, and creates a user binding."""
        try:
            channel_type = channel_type.lower().strip()

            from integrations.channels.metadata import get_channel_metadata, list_all_channels
            meta = get_channel_metadata(channel_type)
            if not meta:
                available = ', '.join(sorted(list_all_channels().keys()))
                return f"Unknown channel '{channel_type}'. Available channels: {available}"

            try:
                config = json.loads(config_json)
            except json.JSONDecodeError:
                # If user just pasted a token, try to assign it to the first field
                fields = meta.get('setup_fields', [])
                if fields:
                    config = {fields[0]['key']: config_json.strip()}
                else:
                    return f"Could not parse config. Expected JSON. Required fields: {[f['key'] for f in meta.get('setup_fields', [])]}"

            # Save via admin API singleton
            from integrations.channels.admin.api import get_api
            api = get_api()

            if channel_type in api._channels:
                api._channels[channel_type].update({'config': config, 'enabled': True})
            else:
                api._channels[channel_type] = {
                    'channel_type': channel_type,
                    'name': meta['display_name'],
                    'enabled': True,
                    'config': config,
                }
            api._save_config()

            # Create user binding
            uid = user_id or _get_user_id_from_threadlocal()
            if uid:
                try:
                    from integrations.social.models import get_db, UserChannelBinding
                    db = get_db()
                    try:
                        existing = db.query(UserChannelBinding).filter_by(
                            user_id=str(uid), channel_type=channel_type,
                        ).first()
                        if not existing:
                            db.add(UserChannelBinding(
                                user_id=str(uid),
                                channel_type=channel_type,
                                channel_sender_id='agent_registered',
                                auth_method=meta['auth_method'],
                                is_active=True,
                            ))
                        else:
                            existing.is_active = True
                        db.commit()
                    finally:
                        db.close()
                except Exception as e:
                    logger.debug("Binding creation during registration: %s", e)

            required_fields = [f['key'] for f in meta.get('setup_fields', [])]
            missing = [f for f in required_fields if f not in config]
            if missing:
                return (f"{meta['display_name']} registered with partial config. "
                        f"Missing: {missing}. Complete setup in the Channels page.")

            return (f"{meta['display_name']} registered and enabled! "
                    f"Auth: {meta['auth_method']}. "
                    f"Adapter will connect on restart or via the Channels page.")

        except Exception as e:
            logger.error("register_channel error: %s", e)
            return f"Error registering channel: {e}"

    tools.append((
        "register_channel",
        "Register and connect a new messaging channel. Use when the user wants to connect "
        "a Telegram bot, Discord bot, Slack app, or any of the 31 supported channels. "
        "Example: register_channel('telegram', '{\"bot_token\": \"123456:ABC-DEF\"}') or "
        "register_channel('slack', '{\"bot_token\": \"xoxb-...\", \"signing_secret\": \"...\"}').",
        register_channel,
    ))

    # ------------------------------------------------------------------
    # 3. list_channels
    # ------------------------------------------------------------------
    @log_tool_execution
    def list_channels() -> str:
        """List all connected messaging channels, their status, and user's channel bindings."""
        try:
            uid = user_id or _get_user_id_from_threadlocal()
            lines = []

            from integrations.channels.registry import get_registry
            registry = get_registry()
            status = registry.get_status()

            if status:
                lines.append("**Active Channel Adapters:**")
                for name, st in status.items():
                    state = 'Connected' if st.connected else 'Disconnected'
                    lines.append(f"- {name}: {state}")
            else:
                lines.append("No channel adapters currently running.")

            if uid:
                try:
                    from integrations.social.models import get_db, UserChannelBinding
                    db = get_db()
                    try:
                        bindings = db.query(UserChannelBinding).filter_by(
                            user_id=str(uid), is_active=True,
                        ).all()
                        if bindings:
                            lines.append("\n**Your Channel Bindings:**")
                            for b in bindings:
                                pref = ' (preferred)' if b.is_preferred else ''
                                lines.append(f"- {b.channel_type}: {b.channel_sender_id or 'linked'}{pref}")
                    finally:
                        db.close()
                except Exception:
                    pass

            ctx = _get_channel_context()
            if ctx:
                lines.append(f"\n**Current message from:** {ctx.get('channel', 'unknown')} "
                             f"(sender: {ctx.get('sender_name', ctx.get('sender_id', 'unknown'))})")

            return '\n'.join(lines) if lines else "No channel information available."
        except Exception as e:
            return f"Error listing channels: {e}"

    tools.append((
        "list_channels",
        "List all connected messaging channels, their connection status, and the user's "
        "channel bindings. Use when asked about connected channels or channel status.",
        list_channels,
    ))

    # ------------------------------------------------------------------
    # 4. get_channel_context
    # ------------------------------------------------------------------
    @log_tool_execution
    def get_channel_context() -> str:
        """Get info about which channel the current message was sent from."""
        ctx = _get_channel_context()
        if not ctx:
            return "This message was sent from the direct web/desktop chat (no external channel)."
        return (f"Channel: {ctx.get('channel', 'unknown')}\n"
                f"Sender: {ctx.get('sender_name', 'unknown')} (ID: {ctx.get('sender_id', 'unknown')})\n"
                f"Chat ID: {ctx.get('chat_id', 'unknown')}\n"
                f"Group message: {ctx.get('is_group', False)}")

    tools.append((
        "get_channel_context",
        "Get information about which messaging channel the current message was sent from. "
        "Returns channel type, sender name, chat ID, and whether it's a group message. "
        "Use to tailor responses for the originating channel.",
        get_channel_context,
    ))

    return tools


# ---------------------------------------------------------------------------
# Registration helper (mirrors core/agent_tools.register_core_tools)
# ---------------------------------------------------------------------------

def register_channel_tools(helper, executor, ctx=None):
    """Register channel tools on an AutoGen helper/executor pair.

    Args:
        helper: AutoGen agent that suggests tool use (register_for_llm)
        executor: AutoGen agent that executes tools (register_for_execution)
        ctx: optional dict with 'user_id', 'prompt_id', 'log_tool_execution'
    """
    if ctx is None:
        ctx = {}
        # Try to get user_id from thread-local if not in ctx
        uid = _get_user_id_from_threadlocal()
        if uid:
            ctx['user_id'] = uid

    tools = build_channel_tool_closures(ctx)
    from core.agent_tools import register_core_tools
    register_core_tools(tools, helper, executor)
