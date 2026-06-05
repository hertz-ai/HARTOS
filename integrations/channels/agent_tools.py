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
        return thread_local_data.get_channel_context()
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
    # 3b. suggest_channels  (browser auto-association — #63)
    # ------------------------------------------------------------------
    #
    # Suggests channels the user ALREADY uses (detected from their own
    # browser history, scoped to a messaging-domain allowlist) that aren't
    # connected yet, so the agent can offer to connect them via the existing
    # register_channel / OAuth flow.  Privacy posture (see browser_detect):
    #   - OFF by default; only runs when the user has enabled browser scan
    #     (HART_BROWSER_HISTORY_SCAN) or passes explicit consent.
    #   - History only, scoped to the allowlist; cookies/logins never read.
    #   - Suggests only — the connect itself is the existing consented flow.
    @log_tool_execution
    def suggest_channels() -> str:
        """Suggest messaging channels the user already uses but hasn't connected,
        detected from their own browser history (scoped to known app domains)."""
        try:
            uid = user_id or _get_user_id_from_threadlocal()
            from integrations.channels.browser_detect import detect_channel_usage
            result = detect_channel_usage()  # gated; consent via env flag
            if not result.get('enabled'):
                return result.get('notice') or "Browser channel detection is off."

            detected = set(result.get('channels') or [])
            notice = result.get('notice', '')
            if not detected:
                return ("No known messaging-app domains found in your browser "
                        f"history. {notice}")

            # Drop channels the user already has an active binding for.
            connected = set()
            if uid:
                try:
                    from integrations.social.models import get_db, UserChannelBinding
                    db = get_db()
                    try:
                        for b in db.query(UserChannelBinding).filter_by(
                                user_id=str(uid), is_active=True).all():
                            connected.add(b.channel_type)
                    finally:
                        db.close()
                except Exception:
                    pass

            suggestions = sorted(detected - connected)
            if not suggestions:
                return ("The channels you use are already connected. " + notice)

            from integrations.channels.metadata import get_channel_metadata
            names = ', '.join(
                (get_channel_metadata(c) or {}).get('display_name', c)
                for c in suggestions
            )
            return (f"You appear to use these channels that aren't connected yet: "
                    f"{names}. Want me to connect any of them? {notice}")
        except Exception as e:
            return f"Error suggesting channels: {e}"

    tools.append((
        "suggest_channels",
        "Suggest messaging channels the user already uses but hasn't connected, "
        "detected from their OWN browser history (scoped to known messaging-app "
        "domains; cookies/logins are never read; OFF unless the user enabled "
        "browser scan). Use during onboarding or when the user asks what they "
        "can connect. Always confirm before connecting.",
        suggest_channels,
    ))

    # ------------------------------------------------------------------
    # 3c. list_upcoming_events  (calendar awareness — #64 bridge READ side)
    # ------------------------------------------------------------------
    #
    # The READ side of the Event ingest (ics / Zoom / Meet).  Without a reader
    # the ingested events are write-only — this lets the agent answer "what
    # meetings do I have?" from the user's own ingested calendar.  Scoped to the
    # caller's user_id (created_by) so one user never sees another's events.
    @log_tool_execution
    def list_upcoming_events(
        within_hours: Annotated[int, "Look-ahead window in hours (default 168 = one week)"] = 168,
        limit: Annotated[int, "Maximum number of events to return"] = 20,
    ) -> str:
        """List the user's upcoming meetings/events (from ingested calendar / Zoom / Meet feeds)."""
        try:
            uid = user_id or _get_user_id_from_threadlocal()
            from integrations.social.events import list_upcoming_events as _list_events
            rows = _list_events(within_hours=within_hours, limit=limit,
                                created_by=str(uid) if uid else None)
            if not rows:
                return "No upcoming events in your calendar for that window."
            lines = ["**Upcoming events:**"]
            for r in rows:
                when = r.get('start_time') or '(time TBD)'
                loc = f" — {r['location']}" if r.get('location') else ''
                url = f" ({r['url']})" if r.get('url') else ''
                lines.append(f"- {when}: {r.get('title', '(untitled)')}{loc}{url}")
            return '\n'.join(lines)
        except Exception as e:
            return f"Error listing events: {e}"

    tools.append((
        "list_upcoming_events",
        "List the user's upcoming meetings/events ingested from their calendar, "
        "Zoom, or Google Meet feeds. Use when the user asks about their schedule, "
        "meetings, or what's coming up.",
        list_upcoming_events,
    ))

    # 3d. sync_meetings  (calendar INGEST — #64, closes the Zoom/Meet orphan)
    # fetch_and_ingest_zoom/gmeet were implemented + unit-tested but had NO
    # caller, so list_upcoming_events (the READ side) had nothing to read for
    # those sources. This wires the caller. Token resolution mirrors every other
    # channel adapter: explicit param > env var (the param is also the seam the
    # OAuth-connect flow uses to pass a per-user binding token). Degrades with a
    # clear "connect your account" message — never a silent no-op.
    @log_tool_execution
    def sync_meetings(
        provider: Annotated[str, "Which calendar to sync: 'zoom' or 'meet' (Google Meet)"],
        access_token: Annotated[str, "OAuth bearer token; omit to use the connected account / env token"] = "",
    ) -> str:
        """Fetch the user's upcoming Zoom or Google Meet meetings and ingest them into their calendar (then list_upcoming_events surfaces them)."""
        import os
        prov = (provider or "").strip().lower()
        uid = user_id or _get_user_id_from_threadlocal()
        created_by = str(uid) if uid else None
        try:
            from integrations.social.events import (
                fetch_and_ingest_zoom, fetch_and_ingest_gmeet)
            if prov == "zoom":
                tok = access_token or os.getenv("ZOOM_ACCESS_TOKEN", "")
                if not tok:
                    return ("No Zoom token available — connect your Zoom account "
                            "(OAuth) or set ZOOM_ACCESS_TOKEN, then try again.")
                events = fetch_and_ingest_zoom(tok, created_by=created_by)
            elif prov in ("meet", "gmeet", "google", "google_meet"):
                tok = access_token or os.getenv("GOOGLE_CALENDAR_TOKEN", "")
                if not tok:
                    return ("No Google token available — connect your Google account "
                            "(OAuth) or set GOOGLE_CALENDAR_TOKEN, then try again.")
                events = fetch_and_ingest_gmeet(tok, created_by=created_by)
            else:
                return f"Unknown provider '{provider}'. Use 'zoom' or 'meet'."
            n = len(events)
            if n == 0:
                return (f"No upcoming {prov} meetings found (or the token was "
                        "rejected). Nothing new ingested.")
            return (f"Synced {n} upcoming {prov} meeting(s) into your calendar. "
                    "Ask me to list your upcoming events to see them.")
        except Exception as e:
            return f"Error syncing {prov or 'meetings'}: {e}"

    tools.append((
        "sync_meetings",
        "Fetch the user's upcoming Zoom or Google Meet meetings and ingest them "
        "into their calendar so list_upcoming_events can surface them. Use when "
        "the user asks to sync / import / connect their Zoom or Google Meet schedule.",
        sync_meetings,
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
