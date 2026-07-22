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
import re
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
                # If user just pasted a token, try to assign it to the first
                # USER-VISIBLE field (skip auto:True infrastructure fields
                # like WhatsApp's api_url/access_token — the user wouldn't
                # paste a WAHA URL when prompted for "WhatsApp number").
                fields = [f for f in meta.get('setup_fields', [])
                          if not f.get('auto')]
                if fields:
                    config = {fields[0]['key']: config_json.strip()}
                else:
                    return f"Could not parse config. Expected JSON. Required fields: {[f['key'] for f in meta.get('setup_fields', [])]}"

            # Auto-fill auto:True fields from env-var defaults so the
            # user doesn't have to know about gateway infrastructure
            # (WAHA api_url for WhatsApp, etc).  Order:
            #   1. config[key] explicitly supplied by caller — wins
            #   2. WHATSAPP_<KEY_UPPER> env var — operator override
            #      (e.g. WHATSAPP_API_URL=https://my-waha.example.com)
            #   3. setup_fields[].default — schema default
            #   4. '' if no default
            # Single helper inside this closure — DRY across all
            # auto-fill paths in register_channel.
            import os
            env_prefix = f"{channel_type.upper()}_"
            for f in meta.get('setup_fields', []) or []:
                if not f.get('auto'):
                    continue
                key = f.get('key')
                if not key or config.get(key) not in (None, ''):
                    continue
                env_val = os.getenv(env_prefix + key.upper())
                if env_val is not None:
                    config[key] = env_val
                elif 'default' in f:
                    config[key] = f['default']
                else:
                    config[key] = ''

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

            # PR P.4 — best-effort adapter probe so we surface a toast
            # the moment the credential turns out to be wrong.  Runs
            # in a daemon thread with its own event loop so:
            #   - the agent-tool return is not delayed by the probe
            #     (some adapters open long-lived sockets — sub-second
            #     to seconds depending on provider RTT);
            #   - the loop we own is closed only after the connect()
            #     coroutine actually exits, avoiding the dangling-
            #     adapter / loop-closed-mid-task class of bug;
            #   - on failure we emit a Liquid UI toast (handled by
            #     AgentOverlay's case 'toast' renderer) so the user
            #     sees actionable feedback in chat.
            #
            # The registration itself stays committed — the toast is
            # advisory, not authoritative; operator can fix in admin.
            try:
                from integrations.channels.registry import get_registry
                registry = get_registry()
                adapter = registry.get(channel_type) if registry else None
                if adapter is not None:
                    import threading as _threading
                    _probe_uid = (
                        user_id or _get_user_id_from_threadlocal() or 'system'
                    )
                    _probe_meta = meta  # capture for the thread closure

                    def _probe_in_thread():
                        import asyncio as _asyncio
                        loop = _asyncio.new_event_loop()
                        _asyncio.set_event_loop(loop)
                        try:
                            loop.run_until_complete(
                                _asyncio.wait_for(adapter.connect(), timeout=10),
                            )
                        except Exception as probe_err:
                            logger.info(
                                "register_channel: adapter probe failed "
                                "for %s: %s", channel_type, probe_err,
                            )
                            try:
                                from core.platform.registry import (
                                    get_registry,
                                )
                                _lui = get_registry().get_or_none('LiquidUIService')
                                if _lui:
                                    _lui.agent_ui_update(_probe_uid, {
                                        'type': 'toast',
                                        'severity': 'error',
                                        'channel': channel_type,
                                        'channel_type': channel_type,
                                        'text': (
                                            f"{_probe_meta.get('display_name') or channel_type} "
                                            f"couldn't connect: "
                                            f"{str(probe_err)[:120]}"
                                        ),
                                    })
                            except Exception as toast_err:
                                logger.debug(
                                    "Probe-failure toast emit skipped: %s",
                                    toast_err,
                                )
                            # PR Q — also fan out a channel_unhealthy
                            # fleet command so the user's OTHER devices
                            # surface the same banner (toast above only
                            # reaches the device currently in the chat).
                            try:
                                from integrations.social.fleet_command import (
                                    emit_channel_unhealthy,
                                )
                                from integrations.social.models import get_db
                                _db = get_db()
                                try:
                                    emit_channel_unhealthy(
                                        _db,
                                        user_id=_probe_uid,
                                        channel_type=channel_type,
                                        reason=str(probe_err)[:120],
                                    )
                                    _db.commit()
                                finally:
                                    _db.close()
                            except Exception as fanout_err:
                                logger.debug(
                                    "Probe-failure fleet fan-out skipped: %s",
                                    fanout_err,
                                )
                        finally:
                            loop.close()

                    _threading.Thread(
                        target=_probe_in_thread,
                        name=f'channel-probe-{channel_type}',
                        daemon=True,
                    ).start()
            except Exception as e:
                logger.debug("Probe thread spawn skipped: %s", e)

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

    # ------------------------------------------------------------------
    # disconnect_channel (PR P.5)
    # ------------------------------------------------------------------
    @log_tool_execution
    def disconnect_channel(
        channel_type: Annotated[str, "Channel to disconnect (telegram, discord, slack, ...)"],
    ) -> str:
        """Disconnect the user's binding for a channel.  Marks the
        UserChannelBinding row inactive (same row the register_channel
        path created) — the adapter stops being used for this user but
        the channel-wide config and other users' bindings stay intact.
        Single owner of the binding lifecycle: register_channel writes,
        disconnect_channel reverses.
        """
        try:
            channel_type = channel_type.lower().strip()
            from integrations.channels.metadata import get_channel_metadata
            meta = get_channel_metadata(channel_type)
            if not meta:
                return f"Unknown channel '{channel_type}'."
            uid = user_id or _get_user_id_from_threadlocal()
            if not uid:
                return "Could not determine the current user."
            from integrations.social.models import get_db, UserChannelBinding
            db = get_db()
            try:
                row = db.query(UserChannelBinding).filter_by(
                    user_id=str(uid), channel_type=channel_type, is_active=True,
                ).first()
                if not row:
                    return (
                        f"No active {meta['display_name']} binding to disconnect."
                    )
                row.is_active = False
                db.commit()
            finally:
                db.close()
            # User-visible toast confirming the action.
            try:
                from core.platform.registry import get_registry
                _lui = get_registry().get_or_none('LiquidUIService')
                if _lui:
                    _lui.agent_ui_update(uid, {
                        'type': 'toast', 'severity': 'info',
                        'channel': channel_type, 'channel_type': channel_type,
                        'text': f"{meta['display_name']} disconnected.",
                    })
            except Exception as e:
                logger.debug("disconnect toast emit skipped: %s", e)
            return (
                f"{meta['display_name']} disconnected.  Run "
                f"reconnect_channel('{channel_type}') to bring it back."
            )
        except Exception as e:
            logger.error("disconnect_channel error: %s", e)
            return f"Error disconnecting channel: {e}"

    tools.append((
        "disconnect_channel",
        "Disconnect the user's existing binding for a channel.  Use when "
        "the user wants to stop using a previously connected channel "
        "(Telegram, Discord, WhatsApp, etc.).  Reversible via "
        "reconnect_channel.  Example: disconnect_channel('telegram').",
        disconnect_channel,
    ))

    # ------------------------------------------------------------------
    # reconnect_channel (PR P.6)
    # ------------------------------------------------------------------
    @log_tool_execution
    def reconnect_channel(
        channel_type: Annotated[str, "Channel to reconnect"],
    ) -> str:
        """Re-activate a previously disconnected binding (or trigger a
        fresh connection flow if no inactive binding exists).  Single
        flow: if an inactive binding exists, flip is_active back to True.
        Otherwise re-emit the form / qr_pair / oauth_link prompt the
        user originally went through — same Connect_Channel pipeline,
        no parallel re-onboarding code path.
        """
        try:
            channel_type = channel_type.lower().strip()
            from integrations.channels.metadata import get_channel_metadata
            meta = get_channel_metadata(channel_type)
            if not meta:
                return f"Unknown channel '{channel_type}'."
            uid = user_id or _get_user_id_from_threadlocal()
            if not uid:
                return "Could not determine the current user."
            from integrations.social.models import get_db, UserChannelBinding
            db = get_db()
            try:
                row = db.query(UserChannelBinding).filter_by(
                    user_id=str(uid), channel_type=channel_type,
                ).first()
                if row and not row.is_active:
                    row.is_active = True
                    db.commit()
                    return (
                        f"{meta['display_name']} reconnected (existing binding "
                        f"reactivated).  The adapter will start using it on "
                        f"the next message tick."
                    )
            finally:
                db.close()
            # No prior binding (or already active) — bounce the user
            # through the standard Connect_Channel onboarding so they
            # can re-paste a token / scan a QR / click OAuth, exactly
            # like a first-time setup.  No parallel onboarding path.
            return (
                f"No inactive {meta['display_name']} binding found.  "
                f"Run Connect_Channel('{channel_type}') to start a fresh setup."
            )
        except Exception as e:
            logger.error("reconnect_channel error: %s", e)
            return f"Error reconnecting channel: {e}"

    tools.append((
        "reconnect_channel",
        "Re-enable a previously disconnected channel binding.  If the "
        "binding row exists but is inactive, this flips it back on.  "
        "Otherwise the user is bounced through the standard "
        "Connect_Channel flow (form / QR / OAuth, depending on the "
        "channel) to re-establish credentials.  Example: "
        "reconnect_channel('discord').",
        reconnect_channel,
    ))

    # ------------------------------------------------------------------
    # set_announcement_channel — where growth announcements get posted
    # ------------------------------------------------------------------
    #
    # Setting a destination is deliberately NOT the same as authorising
    # broadcast. With public_exposure consent absent, a configured
    # destination still sends nothing, so exposing this as an agent tool
    # cannot escalate anything: the worst case is an address written down
    # that never gets used. Granting the consent itself is not a tool, and
    # should not become one -- an autonomous goal able to grant its own
    # permission to post publicly is the fail-closed gate flipping its own
    # switch.
    @log_tool_execution
    def set_announcement_channel(
        channel_type: Annotated[str, "Channel to announce on (telegram, discord, slack, whatsapp)"],
        chat_id: Annotated[str, "Destination id. Telegram groups/channels are NEGATIVE ids; WhatsApp groups end in @g.us"],
    ) -> str:
        """Set where HARTOS posts growth announcements for a channel.

        Refuses private one-to-one destinations: broadcasting into somebody's
        DM is unsolicited messaging regardless of who configured it."""
        try:
            from integrations.channels.announcement_broadcaster import (
                destination_shape, is_subscribed)
            from integrations.channels.admin.api import get_api

            channel_type = (channel_type or '').strip().lower()
            chat_id = (chat_id or '').strip()
            if not channel_type or not chat_id:
                return "Both channel_type and chat_id are required."

            shape = destination_shape(channel_type, chat_id)
            if shape == 'one_to_one' and not is_subscribed(channel_type, chat_id):
                return (f"Refused: {chat_id} is a private one-to-one chat on "
                        f"{channel_type}. Announcements go to groups or channels "
                        f"people joined, not to individuals who did not ask. "
                        f"Use a group id (Telegram groups are negative; WhatsApp "
                        f"groups end in @g.us).")

            api = get_api()
            cfg = api._channels.get(channel_type)
            if cfg is None:
                return (f"Channel '{channel_type}' is not registered. "
                        f"Register it first, then set its announcement target.")
            cfg['announce_chat_id'] = chat_id
            api._channels[channel_type] = cfg
            api._save_config()

            # Say plainly whether this will actually broadcast yet. Reporting
            # "configured" when nothing can send would be the same false
            # success this system has been bitten by before.
            try:
                from integrations.agent_engine.marketing_tools import (
                    _external_post_allowed)
                consented = bool(_external_post_allowed(
                    user_id or _get_user_id_from_threadlocal()))
            except Exception:
                consented = False

            status = ("Announcements are ENABLED and will post here."
                      if consented else
                      "NOTE: public_exposure consent is not granted, so nothing "
                      "will actually be sent yet. That grant is an operator "
                      "action, deliberately not an agent tool.")
            return f"Announcement target for {channel_type} set to {chat_id}. {status}"
        except Exception as e:
            logger.error("set_announcement_channel error: %s", e)
            return f"Error setting announcement channel: {e}"

    tools.append((
        "set_announcement_channel",
        "Set where HARTOS posts growth announcements for a messaging channel "
        "(e.g. a Telegram group, a Discord channel, a WhatsApp group). Refuses "
        "private one-to-one chats. Does NOT authorise posting on its own -- "
        "public_exposure consent is a separate operator decision. "
        "Example: set_announcement_channel('telegram', '-1001234567890')",
        set_announcement_channel,
    ))

    # ------------------------------------------------------------------
    # send_email_campaign — outbound email through our own mail server
    # ------------------------------------------------------------------
    #
    # dry_run defaults True: the first call returns what WOULD be sent so the
    # agent (and the human reading it) can see the blast radius before any
    # mail leaves. Same preview-then-confirm shape as the other write-side
    # tools here.
    @log_tool_execution
    def send_email_campaign(
        recipients: Annotated[str, "Comma or newline separated email addresses"],
        subject: Annotated[str, "Subject line"],
        body_text: Annotated[str, "Plain-text body. Disclose who is writing."],
        body_html: Annotated[Optional[str], "Optional HTML body; falls back to the text"] = None,
        campaign: Annotated[str, "Campaign name — scopes the resume log so a rerun never double-sends"] = "default",
        delay_seconds: Annotated[float, "Seconds between messages. Higher is safer for deliverability."] = 2.0,
        limit: Annotated[Optional[int], "Cap this run (e.g. warm-up batch)"] = None,
        daily_cap: Annotated[Optional[int], "Max messages today. Overrides the warm-up ramp."] = None,
        warmup: Annotated[bool, "Ramp daily volume over the first week. Leave on for any list over a few thousand."] = True,
        dry_run: Annotated[bool, "True previews without sending. Set False to actually deliver."] = True,
    ) -> str:
        """Send an email campaign via our own mail server, paced and resumable.

        Skips anyone already sent for this campaign or previously unsubscribed.
        Every message carries List-Unsubscribe.

        Volume is capped per day as well as paced per message. Spacing alone
        does not protect a domain: a sender with no history that delivers
        seventeen thousand messages in a day is blocked on reputation grounds
        however evenly they were spread."""
        try:
            from integrations.channels.email_campaign import send_campaign
            addrs = [a.strip() for a in re.split(r"[,\n;]+", recipients or "")
                     if a.strip()]
            if not addrs:
                return "No recipients supplied."
            html = body_html or (
                '<div style="font-family:system-ui,Arial;max-width:560px;'
                'line-height:1.6">'
                + "".join("<p>%s</p>" % p for p in body_text.split("\n\n"))
                + "</div>")
            res = send_campaign(addrs, subject, html, body_text,
                                campaign=campaign, dry_run=bool(dry_run),
                                delay_seconds=float(delay_seconds), limit=limit,
                                daily_cap=daily_cap, warmup=bool(warmup))
            cap_note = ""
            if res.get("daily_cap") is not None:
                cap_note = (" Day %d of the ramp, cap %d, %d already sent today."
                            % (res["campaign_day"], res["daily_cap"],
                               res["sent_today_before"]))
            if res.get("dry_run"):
                return ("DRY RUN for campaign '%s': %d would be sent "
                        "(%d already sent, %d opted out), ~%s min at %.1fs pacing.%s "
                        "Re-run with dry_run=False to deliver."
                        % (campaign, res["candidates"], res["already_sent"],
                           res["opted_out"], res["estimated_minutes"],
                           res["delay_seconds"], cap_note))
            if res.get("error"):
                return "Campaign '%s' failed: %s" % (campaign, res["error"])
            out = ("Campaign '%s': sent=%d failed=%d of %d candidates.%s"
                   % (campaign, res["sent"], res["failed"], res["candidates"],
                      cap_note))
            if res.get("halted"):
                out += " HALTED: " + res["halted"]
            return out
        except Exception as e:
            logger.error("send_email_campaign error: %s", e)
            return "Error running campaign: %s" % e

    tools.append((
        "send_email_campaign",
        "Send an email campaign through our own mail server, paced and resumable. "
        "ALWAYS previews first (dry_run=True by default) — call again with "
        "dry_run=False to actually deliver. Skips already-sent and unsubscribed "
        "addresses automatically. delay_seconds controls pacing (higher is safer "
        "for deliverability). Example: send_email_campaign('a@x.com,b@y.com', "
        "'Subject', 'Body text', campaign='welcome', delay_seconds=2.0)",
        send_email_campaign,
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
