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

    # ------------------------------------------------------------------
    # 5. send_install_link  (cross-device handoff — Phase 1)
    # ------------------------------------------------------------------
    #
    # When a user says "send Nunba to my phone" / "I want this on my work
    # laptop", the agent dispatches the install link to ONE of the user's
    # PAIRED channels.  The tool enforces three guarantees:
    #
    #   1. No cross-user spam: the destination chat_id MUST belong to a
    #      currently-active UserChannelBinding for the *caller's* user_id.
    #      Alice cannot resolve / target Bob's bindings.
    #   2. URL allowlist: if `install_link` is provided as an override,
    #      it MUST resolve to a host in `core.install_links.ALLOWED_HOSTS`
    #      (github.com / play.google.com / apps.apple.com / hevolve.ai /
    #      testflight.apple.com).  Otherwise the canonical mapping is used.
    #   3. Explicit consent: the tool description (read by the LLM) tells
    #      it to confirm the channel choice with the user FIRST.  We don't
    #      enforce this in code — the system prompt + this description do.
    #
    # See `core/install_links.py` for the canonical (device, locale) → URL
    # table.  See `tests/unit/test_install_handoff.py` for the FT/NFT
    # coverage.

    @log_tool_execution
    def send_install_link(
        channel_type: Annotated[str, "Channel to dispatch through: telegram, discord, whatsapp, slack, signal, web, email"],
        target_device: Annotated[str, "Device the user wants Nunba on: android, ios, windows, macos, linux"],
        chat_id: Annotated[Optional[str], "Specific chat_id from one of the user's bindings; if omitted, uses the user's preferred binding for that channel"] = None,
        install_link: Annotated[Optional[str], "Optional URL override; MUST be on the allowlist (github.com / play.google.com / apps.apple.com / hevolve.ai / testflight.apple.com).  If omitted, the canonical link for target_device is used."] = None,
        locale: Annotated[str, "BCP-47 locale tag for localized install pages; 'default' falls back to the global URL"] = 'default',
    ) -> str:
        """Send a Nunba install link for `target_device` through `channel_type`.

        Use ONLY when the user has explicitly asked to install / set up /
        get / send Nunba on another device AND has confirmed which channel
        to use.  Never auto-dispatch — always confirm first.
        """
        try:
            from core.install_links import (
                get_install_link,
                is_allowed_install_link,
                is_supported_device,
                is_supported_install_channel,
            )

            channel_type_n = (channel_type or '').lower().strip()
            target_n = (target_device or '').lower().strip()

            if not is_supported_install_channel(channel_type_n):
                return (
                    f"Error: '{channel_type}' is not a supported install-handoff "
                    f"channel.  Allowed: telegram, discord, whatsapp, slack, signal, "
                    f"web, email."
                )
            if not is_supported_device(target_n):
                return (
                    f"Error: '{target_device}' is not a supported target device. "
                    f"Allowed: android, ios, windows, macos, linux."
                )

            # Resolve the URL
            if install_link:
                if not is_allowed_install_link(install_link):
                    return (
                        "Error: install_link override is not on the allowlist. "
                        "Allowed hosts: github.com, play.google.com, "
                        "apps.apple.com, hevolve.ai, testflight.apple.com."
                    )
                url = install_link
            else:
                url = get_install_link(target_n, locale)
                if not url:
                    return (
                        f"Error: no canonical install link configured for "
                        f"target_device={target_n}, locale={locale}."
                    )

            # Resolve the destination chat_id from the caller's bindings
            # only.  Cross-user lookups are impossible by construction:
            # we filter by user_id == caller.
            uid = user_id or _get_user_id_from_threadlocal()
            if not uid:
                return (
                    "Error: cannot identify the requesting user; refusing "
                    "to dispatch install link without an authenticated "
                    "session."
                )

            resolved_chat_id = chat_id
            if not resolved_chat_id:
                try:
                    from integrations.social.models import (
                        get_db, UserChannelBinding,
                    )
                    db = get_db()
                    try:
                        q = db.query(UserChannelBinding).filter_by(
                            user_id=str(uid),
                            channel_type=channel_type_n,
                            is_active=True,
                        )
                        # Prefer the explicitly-flagged preferred binding
                        binding = q.filter_by(is_preferred=True).first() or q.first()
                        if not binding:
                            return (
                                f"You don't have a paired {channel_type_n} "
                                f"yet.  Open the Channels page to connect "
                                f"one, then I can send the install link there."
                            )
                        resolved_chat_id = (
                            binding.channel_chat_id
                            or binding.channel_sender_id
                        )
                    finally:
                        db.close()
                except Exception as e:
                    logger.error("send_install_link binding lookup error: %s", e)
                    return (
                        f"Error: could not resolve a {channel_type_n} "
                        f"binding for the requesting user."
                    )
            else:
                # Caller passed an explicit chat_id — verify it belongs to
                # this user, NOT to someone else (no-spam guarantee).
                try:
                    from integrations.social.models import (
                        get_db, UserChannelBinding,
                    )
                    db = get_db()
                    try:
                        owns = db.query(UserChannelBinding).filter_by(
                            user_id=str(uid),
                            channel_type=channel_type_n,
                            is_active=True,
                        ).filter(
                            (UserChannelBinding.channel_chat_id == resolved_chat_id)
                            | (UserChannelBinding.channel_sender_id == resolved_chat_id)
                        ).first()
                        if not owns:
                            return (
                                f"Refusing to send: chat_id {resolved_chat_id} "
                                f"is not bound to your account on "
                                f"{channel_type_n}."
                            )
                    finally:
                        db.close()
                except Exception as e:
                    logger.error("send_install_link ownership check error: %s", e)
                    return f"Error: could not verify chat_id ownership: {e}"

            # Compose the message — short, friendly, links open natively
            message = (
                f"Here's the Nunba install link for your {target_n} device:\n"
                f"{url}\n\n"
                f"Open it on the {target_n} device and follow the prompts.  "
                f"Reply here if you hit any issue during setup."
            )

            # Dispatch via the registry (re-uses the same plumbing as
            # send_to_channel) so all channel adapters share one path.
            from integrations.channels.registry import get_registry
            import asyncio
            registry = get_registry()
            loop = getattr(registry, '_loop', None)

            if loop and loop.is_running():
                future = asyncio.run_coroutine_threadsafe(
                    registry.send_to_channel(
                        channel_type_n, resolved_chat_id, message,
                    ),
                    loop,
                )
                result = future.result(timeout=30)
                if getattr(result, 'success', False):
                    msg_id = (
                        getattr(result, 'message_id', None)
                        or getattr(result, 'id', None)
                        or ''
                    )
                    logger.info(
                        "send_install_link OK uid=%s ch=%s dev=%s url=%s msg=%s",
                        uid, channel_type_n, target_n, url, msg_id,
                    )
                    return (
                        f"Install link for {target_n} sent via "
                        f"{channel_type_n}."
                    )
                return (
                    f"Failed to send via {channel_type_n}: "
                    f"{getattr(result, 'error', 'unknown error')}"
                )

            # Adapter loop not running — return a graceful failure rather
            # than silently dropping.  The user / agent can retry.
            return (
                f"Channel adapters are not running right now.  Try again "
                f"in a moment, or pick another channel."
            )
        except Exception as e:
            logger.error("send_install_link error: %s", e)
            return f"Error sending install link: {e}"

    tools.append((
        "send_install_link",
        "Send a Nunba install link to one of the user's PAIRED channels "
        "(Telegram / Discord / WhatsApp / Slack / Signal / Web / Email). "
        "Call this when the user explicitly asks to install / set up / get / "
        "send Nunba on another device.  Always CONFIRM the channel and target "
        "device with the user before calling — never auto-dispatch.  "
        "target_device must be one of: android, ios, windows, macos, linux.  "
        "Example: send_install_link('telegram', 'android') sends the Play "
        "Store link via the user's preferred Telegram binding.  The tool "
        "refuses to send to a chat_id that is not bound to the requesting "
        "user (no cross-user spam) and refuses install_link overrides that "
        "are not on the host allowlist (no phishing-URL injection).",
        send_install_link,
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
