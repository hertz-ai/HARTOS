"""
Flask Integration for Channel Adapters

Integrates the channel registry with the existing Flask API.
Routes incoming channel messages to the agent system.
"""

import asyncio
import logging
import os
import json
import threading
from datetime import datetime
from typing import Optional, Dict, Any
from functools import wraps

import requests
from core.http_pool import pooled_post

from .base import Message, ChannelConfig
from .registry import ChannelRegistry, ChannelRegistryConfig, get_registry

logger = logging.getLogger(__name__)


class FlaskChannelIntegration:
    """
    Integrates channel adapters with the Flask-based agent API.

    This bridges the async channel adapters with the sync Flask app.
    """

    def __init__(
        self,
        agent_api_url: str = None,
        default_user_id: int = None,
        default_prompt_id: int = None,
        create_mode: bool = False,
        device_id: str = None,
    ):
        from core.constants import DEFAULT_USER_ID, DEFAULT_PROMPT_ID
        if default_user_id is None:
            default_user_id = DEFAULT_USER_ID
        if default_prompt_id is None:
            default_prompt_id = DEFAULT_PROMPT_ID
        if agent_api_url is None:
            # #62 inbound leg: reach the LIVE local HARTOS, not a hardcoded
            # :6777 (dead in bundled mode, where HARTOS serves in-process on
            # :5000).  Shared resolver with dispatch Tier-2 — no parallel path.
            from core.port_registry import get_local_backend_url
            agent_api_url = get_local_backend_url() + '/chat'
        self.agent_api_url = agent_api_url
        self.default_user_id = default_user_id
        self.default_prompt_id = default_prompt_id
        self.create_mode = create_mode
        self._device_id = device_id

        self.registry = get_registry()
        self.registry.set_agent_handler(self._handle_message)

        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._thread: Optional[threading.Thread] = None

        # Persistent session manager (LRU cache + JSON persistence + 24h cleanup)
        from .session_manager import get_session_manager
        self._session_manager = get_session_manager()

        # Response router for fan-out, conversation logging, WAMP
        from .response.router import get_response_router
        self._response_router = get_response_router(registry=self.registry)

        # Self-chat handler — owner messaging their own WhatsApp number
        # becomes a private notebook-to-agent flow (persist + dispatch +
        # reply-in-thread, no fan-out). Feature gate on the adapter
        # config: extra.enable_self_chat_agent (default True).
        from .self_chat import SelfChatHandler
        self._self_chat = SelfChatHandler(
            agent_api_url=self.agent_api_url,
            owner_user_id=self.default_user_id,
            owner_prompt_id=self.default_prompt_id,
            device_id=self._device_id,
            session_manager=self._session_manager,
            response_router=self._response_router,
            registry=self.registry,
            get_loop=lambda: self._loop,
        )

    def _handle_message(self, message: Message) -> str:
        """
        Handle incoming message from any channel.

        Routes to Flask API and returns response. Resolves the
        Hevolve user_id via UserChannelBinding first — the user
        registered this channel (e.g. WhatsApp +1234) to their
        Hevolve account via Connect_Channel, and the binding row
        is the single source of truth for (channel, sender_id) →
        user_id. Falls back to the session cache and finally the
        configured default.
        """
        try:
            # Get or create persistent session (replaces plain dict)
            session = self._session_manager.get_session(
                message.channel, message.sender_id
            )

            # ── Self-chat short-circuit ──────────────────────────
            # Owner messaging their own number → private notebook-to-
            # agent flow (persist + dispatch + reply-in-thread, no
            # fan-out). Gated per-adapter via extra.enable_self_chat_agent.
            if self._self_chat.is_self_message(message):
                logger.debug("self-chat from %s", message.sender_id)
                return self._self_chat.handle(message, session)

            # ── Resolve user_id ───────────────────────────────────
            # 1. UserChannelBinding (durable DB row written by
            #    Connect_Channel tool + response router)
            # 2. Session cache (in-memory per (channel, sender_id))
            # 3. Configured default
            # Without step 1, a WhatsApp user who bound their
            # account via Connect_Channel would still hit the chat
            # as user_id=10077 (default) and lose access to their
            # per-user memory / bindings / tool permissions.
            user_id = self._resolve_user_id_for_sender(
                channel=message.channel,
                sender_id=message.sender_id,
                fallback=(session.user_id if session and session.user_id
                          else self.default_user_id),
            )
            # prompt_id priority: session (user override) > per-channel config > global default
            prompt_id = (
                (session.prompt_id if session and session.prompt_id else None)
                or self._get_channel_prompt_id(message.channel)
                or self.default_prompt_id
            )

            # Track message in session history
            if session:
                session.add_message('user', message.content)

            # Skip if group and bot not mentioned (configurable)
            adapter = self.registry.get(message.channel)
            if adapter and message.is_group and not message.is_bot_mentioned:
                if adapter.config.require_mention_in_groups:
                    logger.debug(f"Ignoring group message without mention")
                    return None

            # Prepare request to agent API
            from .chat_contract import chat_request_fields, chat_reply
            payload = {
                "user_id": user_id,
                "prompt_id": prompt_id,
                # Dual /chat contract (standalone HARTOS 'prompt' + bundled Nunba
                # 'text') — single source in chat_contract, shared with
                # SelfChatHandler so neither inbound path drifts.
                **chat_request_fields(message.content),
                "create_agent": self.create_mode,
                "device_id": self._device_id,
                "channel_context": {
                    "channel": message.channel,
                    "sender_id": message.sender_id,
                    "sender_name": message.sender_name,
                    "chat_id": message.chat_id,
                    "is_group": message.is_group,
                    "message_id": message.id,
                }
            }

            logger.info(f"Routing message from {message.channel}:{message.sender_id} to agent")

            # Authenticate the internal hop to /chat.
            #
            # This call had no headers at all. On central/regional tiers
            # security/middleware.py Gate 2 rejects an unauthenticated
            # internal /chat POST with 401 "Authentication required (Bearer
            # token)", so EVERY inbound channel message -- Telegram, Discord,
            # WhatsApp, Slack -- got back "Sorry, I encountered an error
            # processing your request." A connected channel looked wired up
            # and answered every message with an apology.
            #
            # Reusing agent_engine.dispatch._internal_auth_headers rather than
            # minting a header here: it already solves exactly this (its
            # docstring records the same 401 silently breaking the outreach
            # dispatch path from 2026-03-14). One implementation, so a future
            # change to internal auth cannot fix one caller and miss the
            # other -- which is precisely how this bug survived. Imported
            # lazily to keep channels -> agent_engine out of module import
            # order. Returns None on flat tier, where no header is needed.
            # 2026-08-06 fix: pass the REAL resolved user_id here, not the
            # function's 'system_daemon' default. /chat's JWT-vs-body
            # check always trusts the JWT over the body (correct — stops
            # body-spoofing), so leaving this at the default silently
            # collapsed every channel user's identity into one shared
            # 'system_daemon' agent session, corrupting concurrent turns
            # across channels (empty/lost replies). See
            # _internal_auth_headers' docstring for the full incident.
            try:
                from integrations.agent_engine.dispatch import (
                    _internal_auth_headers)
                _auth_headers = _internal_auth_headers(user_id=str(user_id))
            except Exception as _auth_err:  # never block a message on this
                logger.debug("internal auth header unavailable: %s", _auth_err)
                _auth_headers = None

            # Call agent API
            response = pooled_post(
                self.agent_api_url,
                json=payload,
                headers=_auth_headers,
                # 2 minute default for agent processing.  Overridable because
                # a multi-agent turn against a LOCAL model makes several LLM
                # calls (30-45s each on a 4B), blowing past 120s and replying
                # "Sorry, the request timed out" even though the agent went on
                # to produce a perfectly good answer.
                timeout=int(os.environ.get('HEVOLVE_CHANNEL_AGENT_TIMEOUT', '120')),
            )

            if response.status_code == 200:
                result = response.json()
                agent_reply = chat_reply(result, "I processed your request.")

                # Track response in session history
                if session:
                    session.add_message('assistant', agent_reply)

                # Auto-upsert channel binding + log user message
                self._response_router.upsert_binding(
                    user_id, message.channel, message.sender_id, message.chat_id)
                self._response_router.log_user_message(
                    user_id, message.channel, message.content)

                # Route response: WAMP desktop + fan-out to bound channels + log
                self._response_router.route_response(
                    user_id=user_id,
                    response_text=agent_reply,
                    channel_context=payload.get('channel_context'),
                    fan_out=True,
                )

                return agent_reply
            else:
                logger.error(f"Agent API error: {response.status_code} - {response.text}")
                return "Sorry, I encountered an error processing your request."

        except requests.Timeout:
            logger.error("Agent API timeout")
            return "Sorry, the request timed out. Please try again."
        except Exception as e:
            logger.error(f"Error handling message: {e}")
            return "Sorry, an unexpected error occurred."

    def _resolve_user_id_for_sender(
        self, channel: str, sender_id: str, fallback,
    ):
        """Resolve (channel_type, channel_sender_id) → Hevolve user_id
        via the UserChannelBinding table.

        Returns the bound user_id when the user has registered this
        channel via the Connect_Channel tool, otherwise the provided
        fallback (session cache or default). The lookup must never
        raise — binding DB failures log at debug and fall through so
        message handling is never blocked by a transient DB issue.
        """
        if not channel or not sender_id:
            return fallback
        try:
            from integrations.social.models import get_db, UserChannelBinding
        except ImportError:
            return fallback
        try:
            db = get_db()
            try:
                row = db.query(UserChannelBinding).filter_by(
                    channel_type=str(channel).lower(),
                    channel_sender_id=str(sender_id),
                    is_active=True,
                ).first()
                if row and row.user_id:
                    logger.debug(
                        f"Channel binding resolved: {channel}:{sender_id} "
                        f"→ user_id={row.user_id}"
                    )
                    return row.user_id
            finally:
                try:
                    db.close()
                except Exception:
                    pass
        except Exception as e:
            logger.debug(
                f"UserChannelBinding lookup failed "
                f"({channel}:{sender_id}): {e}"
            )
        return fallback

    def _get_channel_prompt_id(self, channel_type: str) -> Optional[int]:
        """Read per-channel prompt_id from admin config (if set)."""
        try:
            from .admin.api import get_api
            api = get_api()
            config = api._channels.get(channel_type, {})
            pid = config.get('prompt_id')
            return int(pid) if pid else None
        except Exception:
            return None

    # ── Adapter factory import paths ─────────────────────────────
    # Maps channel_type → (module_path, factory_function_name).
    # Core adapters live in integrations.channels, extensions in
    # integrations.channels.extensions, hardware in .hardware.
    _ADAPTER_FACTORIES: Dict[str, tuple] = {
        'telegram':       ('.telegram_adapter',       'create_telegram_adapter'),
        'discord':        ('.discord_adapter',        'create_discord_adapter'),
        'whatsapp':       ('.whatsapp_adapter',       'create_whatsapp_adapter'),
        'slack':          ('.slack_adapter',           'create_slack_adapter'),
        'signal':         ('.signal_adapter',          'create_signal_adapter'),
        'imessage':       ('.imessage_adapter',        'create_imessage_adapter'),
        'google_chat':    ('.google_chat_adapter',     'create_google_chat_adapter'),
        'web':            ('.web_adapter',             'create_web_adapter'),
        # Extensions
        'teams':          ('.extensions.teams_adapter',          'create_teams_adapter'),
        'matrix':         ('.extensions.matrix_adapter',         'create_matrix_adapter'),
        'mattermost':     ('.extensions.mattermost_adapter',     'create_mattermost_adapter'),
        'nextcloud':      ('.extensions.nextcloud_adapter',      'create_nextcloud_adapter'),
        'rocketchat':     ('.extensions.rocketchat_adapter',     'create_rocketchat_adapter'),
        'messenger':      ('.extensions.messenger_adapter',      'create_messenger_adapter'),
        'instagram':      ('.extensions.instagram_adapter',      'create_instagram_adapter'),
        'twitter':        ('.extensions.twitter_adapter',        'create_twitter_adapter'),
        'line':           ('.extensions.line_adapter',            'create_line_adapter'),
        'viber':          ('.extensions.viber_adapter',           'create_viber_adapter'),
        'wechat':         ('.extensions.wechat_adapter',         'create_wechat_adapter'),
        'zalo':           ('.extensions.zalo_adapter',            'create_zalo_adapter'),
        'twitch':         ('.extensions.twitch_adapter',          'create_twitch_adapter'),
        'nostr':          ('.extensions.nostr_adapter',           'create_nostr_adapter'),
        'tlon':           ('.extensions.tlon_adapter',            'create_tlon_adapter'),
        'openprose':      ('.extensions.openprose_adapter',       'create_openprose_adapter'),
        'telegram_user':  ('.extensions.telegram_user_adapter',   'create_telegram_user_adapter'),
        'discord_user':   ('.extensions.discord_user_adapter',    'create_discord_user_adapter'),
        'zalo_user':      ('.extensions.zalo_user_adapter',       'create_zalo_user_adapter'),
        'bluebubbles':    ('.extensions.bluebubbles_adapter',     'create_bluebubbles_adapter'),
        'email':          ('.extensions.email_adapter',            'create_email_adapter'),
        'voice':          ('.extensions.voice_adapter',            'create_voice_adapter'),
    }

    # Env var fallbacks for token/credential per channel type
    _ENV_FALLBACKS: Dict[str, str] = {
        'telegram':  'TELEGRAM_BOT_TOKEN',
        'discord':   'DISCORD_BOT_TOKEN',
        'whatsapp':  'WHATSAPP_API_URL',
        'slack':     'SLACK_BOT_TOKEN',
        'signal':    'SIGNAL_PHONE_NUMBER',
        'teams':     'TEAMS_BOT_TOKEN',
        'matrix':    'MATRIX_ACCESS_TOKEN',
        'mattermost': 'MATTERMOST_TOKEN',
        'rocketchat': 'ROCKETCHAT_AUTH_TOKEN',
        'nextcloud': 'NEXTCLOUD_APP_PASSWORD',
        'twitch': 'TWITCH_ACCESS_TOKEN',
        'nostr': 'NOSTR_PRIVATE_KEY',
        'bluebubbles': 'BLUEBUBBLES_PASSWORD',
        'tlon': 'URBIT_CODE',
        'messenger': 'MESSENGER_PAGE_TOKEN',
        'instagram': 'INSTAGRAM_PAGE_TOKEN',
        'twitter': 'TWITTER_ACCESS_TOKEN',
        'viber': 'VIBER_AUTH_TOKEN',
        'zalo': 'ZALO_ACCESS_TOKEN',
        'wechat': 'WECHAT_APP_SECRET',
        'google_chat': 'GOOGLE_CHAT_WEBHOOK',
    }

    # Channels that register without any external token/credential.
    _NO_TOKEN_CHANNELS = ('web', 'imessage', 'openprose')

    # Credential keys a persisted UserChannelBinding may carry in
    # metadata_json, in resolution order.  The binding API stores the
    # credential under ITS OWN naming — discord rows hold 'bot_token' — which
    # is not necessarily the factory's parameter name
    # (create_discord_adapter takes 'token').  So whatever is found here is
    # handed to register_channel as the generic `token` and mapped to the real
    # parameter by _CHANNEL_SPECS / _credential_kwarg, reusing the one mapping
    # layer the live-registration path already uses rather than duplicating it.
    _BINDING_CREDENTIAL_KEYS = (
        'bot_token', 'token', 'access_token', 'auth_token',
        'personal_access_token', 'app_password', 'phone_number',
        'api_url', 'webhook_url', 'private_key',
    )

    # WhatsApp is deliberately NOT restored generically: it already has a
    # dedicated rehydration path (_ensure_whatsapp_live_adapter in
    # hart_intelligence_entry) which additionally resolves the self-chat
    # identity from the live gateway.  Restoring it here would build a second,
    # identity-less adapter and silently replace that one, since
    # registry.register keys on adapter.name.
    _RESTORE_EXCLUDED = ('whatsapp',)

    # Declarative specs for channels that need more than the single generic
    # `token`: the token maps to a differently-named factory param
    # (`token_param`) and/or extra credentials must be resolved.  This replaces
    # a per-channel elif ladder in register_channel — to support a new
    # multi-input channel, add a dict entry here instead of a code branch.
    #   token_param: factory kwarg the generic `token` maps to
    #   extra: list of {param, env?, default?, required?}
    #          (param doubles as the caller-supplied kwarg name)
    #          resolution order per extra: explicit kwarg -> env -> default;
    #          a missing `required` extra skips registration with a warning.
    _CHANNEL_SPECS: Dict[str, Dict[str, Any]] = {
        'slack': {
            'token_param': 'bot_token',
            'extra': [
                # xapp- app-level token for Socket Mode's websocket.
                {'param': 'app_token', 'env': 'SLACK_APP_TOKEN', 'required': True},
            ],
        },
        'signal': {
            # "token" is the linked phone number; talks to signal-cli-rest-api.
            'token_param': 'phone_number',
            'extra': [
                {'param': 'api_url', 'env': 'SIGNAL_API_URL',
                 'default': 'http://localhost:8080'},
            ],
        },
        'matrix': {
            'token_param': 'token',
            'extra': [
                {'param': 'user_id', 'env': 'MATRIX_USER_ID', 'required': True},
                {'param': 'homeserver_url', 'env': 'MATRIX_HOMESERVER_URL',
                 'default': 'https://matrix.org'},
            ],
        },
        'mattermost': {
            # factory has no `token` param; the PAT must land on
            # personal_access_token or auth silently fails.
            'token_param': 'personal_access_token',
            'extra': [
                {'param': 'server_url', 'env': 'MATTERMOST_SERVER_URL',
                 'required': True},
            ],
        },
        'rocketchat': {
            # header auth (X-Auth-Token + X-User-Id) needs the token AND the
            # bot user_id, against a server_url.  (username/password login is
            # a separate mode not driven through the token path.)
            'token_param': 'auth_token',
            'extra': [
                {'param': 'server_url', 'env': 'ROCKETCHAT_URL', 'required': True},
                {'param': 'user_id', 'env': 'ROCKETCHAT_USER_ID', 'required': True},
            ],
        },
        'nextcloud': {
            # Nextcloud Talk uses Basic auth: the "token" is the bot's app
            # password, against a server_url + username.
            'token_param': 'app_password',
            'extra': [
                {'param': 'server_url', 'env': 'NEXTCLOUD_URL', 'required': True},
                {'param': 'username', 'env': 'NEXTCLOUD_USERNAME', 'required': True},
            ],
        },
        'twitch': {
            # Twitch IRC-over-WS: the "token" is the OAuth access token; the
            # bot_username is required to authenticate the IRC login.  The
            # factory pulls client_id/client_secret/channels from env itself.
            'token_param': 'access_token',
            'extra': [
                {'param': 'bot_username', 'env': 'TWITCH_BOT_USERNAME', 'required': True},
            ],
        },
        'nostr': {
            # Nostr identity is a private key (hex/nsec); relays come from
            # NOSTR_RELAYS env or the adapter's defaults, so no extra params.
            # (Live relay connect/sign additionally needs secp256k1.)
            'token_param': 'private_key',
        },
        'bluebubbles': {
            # BlueBubbles iMessage bridge: the "token" is the server password,
            # against a server_url. (Needs python-socketio for the live link.)
            'token_param': 'password',
            'extra': [
                {'param': 'server_url', 'env': 'BLUEBUBBLES_SERVER_URL', 'required': True},
            ],
        },
        'tlon': {
            # Urbit/Tlon: the "token" is the ship +code; ship_name identifies
            # the ship, ship_url is the ship's HTTP endpoint (defaults local).
            'token_param': 'ship_code',
            'extra': [
                {'param': 'ship_name', 'env': 'URBIT_SHIP', 'required': True},
                {'param': 'ship_url', 'env': 'URBIT_URL',
                 'default': 'http://localhost:8080'},
            ],
        },
        'messenger': {
            # Meta Messenger (webhook-based; served by register_webhook_routes).
            # "token" is the page access token; app_secret validates inbound
            # signatures (optional) and verify_token gates the GET handshake.
            'token_param': 'page_access_token',
            'extra': [
                {'param': 'app_secret', 'env': 'MESSENGER_APP_SECRET'},
                {'param': 'verify_token', 'env': 'MESSENGER_VERIFY_TOKEN'},
            ],
        },
        'instagram': {
            # Meta Instagram — same Graph webhook model as Messenger.
            'token_param': 'page_access_token',
            'extra': [
                {'param': 'app_secret', 'env': 'INSTAGRAM_APP_SECRET'},
                {'param': 'verify_token', 'env': 'INSTAGRAM_VERIFY_TOKEN'},
            ],
        },
        'twitter': {
            # Twitter/X OAuth 1.0a — all four credentials required.
            'token_param': 'access_token',
            'extra': [
                {'param': 'consumer_key', 'env': 'TWITTER_CONSUMER_KEY', 'required': True},
                {'param': 'consumer_secret', 'env': 'TWITTER_CONSUMER_SECRET', 'required': True},
                {'param': 'access_token_secret', 'env': 'TWITTER_ACCESS_TOKEN_SECRET', 'required': True},
            ],
        },
        'viber': {
            # Viber bot: "token" is the auth token; bot_name is a display name.
            'token_param': 'auth_token',
            'extra': [
                {'param': 'bot_name', 'env': 'VIBER_BOT_NAME', 'default': 'Bot'},
            ],
        },
        'zalo': {
            # Zalo OA: "token" is the access token; oa_id identifies the OA;
            # app_id/app_secret optional (token refresh).
            'token_param': 'access_token',
            'extra': [
                {'param': 'oa_id', 'env': 'ZALO_OA_ID', 'required': True},
                {'param': 'app_id', 'env': 'ZALO_APP_ID'},
                {'param': 'app_secret', 'env': 'ZALO_APP_SECRET'},
            ],
        },
        'wechat': {
            # WeChat OA: "token" maps to app_secret; app_id required; the
            # factory's own `token` is the webhook verification token.
            'token_param': 'app_secret',
            'extra': [
                {'param': 'app_id', 'env': 'WECHAT_APP_ID', 'required': True},
                {'param': 'token', 'env': 'WECHAT_TOKEN'},
            ],
        },
        'google_chat': {
            # Google Chat (webhook-based): "token" is the outgoing webhook_url;
            # service_account_file optionally enables full Chat-API mode.
            'token_param': 'webhook_url',
            'extra': [
                {'param': 'service_account_file', 'env': 'GOOGLE_CHAT_SA_FILE'},
            ],
        },
    }

    def register_channel(self, channel_type: str, token: str = None, **kwargs) -> bool:
        """Register any channel adapter by type.

        Generic factory — replaces per-channel register_* methods.
        Falls back to env var if no token provided.  Returns True on success.
        """
        factory_info = self._ADAPTER_FACTORIES.get(channel_type)
        if not factory_info:
            logger.warning(f"Unknown channel type: {channel_type}")
            return False

        module_path, factory_name = factory_info
        token = token or os.getenv(self._ENV_FALLBACKS.get(channel_type, ''))
        if not token and channel_type not in self._NO_TOKEN_CHANNELS:
            logger.warning(f"{channel_type} token not provided, skipping")
            return False

        # Build the factory call kwargs.  Multi-input channels (see
        # _CHANNEL_SPECS) map the generic `token` to their real param name and
        # resolve extra credentials from explicit kwarg -> env -> default,
        # skipping (with a warning) when a required one is missing.  Everything
        # else is a plain single-token (or no-token) factory call.
        call_kwargs = dict(kwargs)
        spec = self._CHANNEL_SPECS.get(channel_type)
        if spec:
            if token:
                call_kwargs[spec['token_param']] = token
            for p in spec.get('extra', ()):
                name = p['param']
                if call_kwargs.get(name):
                    continue  # explicit kwarg wins over env/default
                val = (os.getenv(p['env']) if p.get('env') else None) or p.get('default')
                if not val:
                    if p.get('required'):
                        env_hint = f" ({p['env']})" if p.get('env') else ''
                        logger.warning(
                            f"{channel_type} requires {name}{env_hint} — skipping")
                        return False
                    continue
                call_kwargs[name] = val

        try:
            import importlib
            mod = importlib.import_module(module_path, package='integrations.channels')
            factory_fn = getattr(mod, factory_name)

            # Single-credential channels: resolve the credential's REAL param
            # name from the factory signature. Done here because it needs
            # factory_fn, which only exists after the import above.
            #
            # NOT a hardcoded `token=`. create_whatsapp_adapter takes
            # (api_url, phone_number, ...) and has NO `token` parameter, so
            # token= raises TypeError, the except below swallows it, and the
            # channel silently never registers. That is the exact bug
            # _credential_kwarg was written to fix — its test file says so:
            # "register_channel() returned False for every credential-based
            # channel whose factory doesn't happen to name its parameter
            # 'token', and no live adapter was ever constructed."
            #
            # So the two paths divide cleanly and neither is orphaned:
            # _CHANNEL_SPECS for multi-credential channels, signature
            # introspection for the single-credential rest.
            if not spec and token:
                call_kwargs.update(self._credential_kwarg(factory_fn, token))

            adapter = factory_fn(**call_kwargs)
            self.registry.register(adapter)
            logger.info(f"{channel_type} adapter registered")
            return True
        except Exception as e:
            logger.warning(f"{channel_type} adapter registration failed: {e}")
            return False

    @staticmethod
    def _credential_kwarg(factory_fn, token: str) -> Dict[str, str]:
        """Map the caller's credential to the factory's OWN first parameter
        name, instead of always assuming ``token=``.

        Most adapter factories accept ``token``, but several don't:
        ``create_whatsapp_adapter(api_url=...)`` and
        ``create_signal_adapter(phone_number=..., api_url=...)`` both take
        the credential under a different name.  Passing ``token=`` to
        those either raised a TypeError (unexpected keyword argument) or —
        worse, for factories with a stray **kwargs catch-all — silently
        absorbed it into kwargs and left the real (required) parameter
        None, so the factory raised its own "required" ValueError instead.
        Either way, register_channel() failed for every credential-based
        channel whose factory doesn't happen to name its parameter
        ``token``. inspect the factory's actual signature instead of
        guessing.
        """
        import inspect
        try:
            params = list(inspect.signature(factory_fn).parameters.values())
        except (TypeError, ValueError):
            return {'token': token}
        names = [
            p.name for p in params
            if p.kind in (p.POSITIONAL_OR_KEYWORD, p.KEYWORD_ONLY)
        ]
        if 'token' in names:
            return {'token': token}
        if names:
            return {names[0]: token}
        return {'token': token}

    # Keep legacy methods as thin delegates for backward compat
    def register_telegram(self, token: str = None, **kwargs) -> None:
        self.register_channel('telegram', token=token, **kwargs)

    def register_discord(self, token: str = None, **kwargs) -> None:
        self.register_channel('discord', token=token, **kwargs)

    def register_whatsapp(self, api_url: str = None, **kwargs) -> None:
        self.register_channel('whatsapp', token=api_url, **kwargs)

    def register_webhook_routes(self, app, path_prefix: str = '/channels/webhook') -> None:
        """Register ONE generic inbound-webhook endpoint for the webhook-based
        channels (line, messenger, instagram, twitter, viber, wechat, zalo,
        google_chat, ...).  Those adapters expose ``handle_webhook()`` but
        nothing HTTP-facing ever calls it — this is the missing seam.

        Routes (one rule, per channel_type):
          GET  {prefix}/<channel_type>  → Meta-style verification handshake:
                echoes ``hub.challenge`` when ``hub.verify_token`` matches
                ``<CHANNEL>_VERIFY_TOKEN`` (or ``WEBHOOK_VERIFY_TOKEN``).
          POST {prefix}/<channel_type>  → hands the raw body (+ signature
                header) to the adapter's ``handle_webhook``, which parses and
                ``_dispatch_message`` → agent.  A dict return (e.g.
                google_chat) becomes the inline JSON response.

        handle_webhook signatures vary (``body:str, signature`` | ``body:Dict``
        | ``data:Dict``), so arguments are mapped by introspection.  The
        coroutine is scheduled on the channel event loop (adapters live there).
        """
        import inspect
        from flask import request, jsonify, Response

        def _webhook_caller_is_authenticated(channel_type: str, raw: str):
            """Return None when the caller may post, else a Flask error tuple.

            THIS ROUTE IS PUBLIC BY NATURE — Meta/LINE/Viber dial it from their
            own infrastructure and cannot hold a HART credential — so it needs
            an auth model that is neither "open" nor "our API key".

            Two accepted proofs, checked in this order:

              1. KONG. When the deployment fronts HART with Kong
                 (HEVOLVE_TRUST_KONG=true, the flag integrations/social/auth.py
                 already defines for exactly this), Kong has authenticated the
                 caller and stamps X-Consumer-*. Reusing that flag rather than
                 inventing a second gateway convention keeps ONE answer to
                 "did the gateway vouch for this caller".

              2. PROVIDER SIGNATURE. The HMAC the platform computes over the
                 body with the app secret. This is the real webhook credential
                 and the only one Meta can present.

            FAILS CLOSED. Without either proof the request is rejected before
            any adapter runs. The adapters cannot be relied on for this: their
            own check is `if signature and not verify(...)`, which SKIPS
            verification entirely when the header is absent, and zalo's
            handle_webhook takes no signature parameter at all. So an
            unsigned POST would have been parsed and dispatched to the agent
            as a genuine user message — an unauthenticated injection into
            agent dispatch, which is what this gate exists to stop.
            """
            # (1) Kong-authenticated consumer.
            if _kong_stamped(request.headers):
                return None

            # (2) Provider signature over the raw body.
            secret = (os.getenv(f'{channel_type.upper()}_APP_SECRET')
                      or os.getenv(f'{channel_type.upper()}_CHANNEL_SECRET')
                      or os.getenv(f'{channel_type.upper()}_WEBHOOK_SECRET'))
            sig_hdr = (request.headers.get('X-Hub-Signature-256')
                       or request.headers.get('X-Hub-Signature')
                       or request.headers.get('X-Line-Signature')
                       or request.headers.get('X-Signature'))
            if secret and sig_hdr:
                import base64
                import hashlib
                import hmac as _hmac
                body = raw.encode('utf-8')
                # Strip ONLY a real algorithm prefix. Meta sends
                # "sha256=<hex>"; LINE sends bare base64 — and base64 PADDING
                # is '=', so a blind split('=', 1)[-1] mangles every padded
                # LINE signature into a mismatch. (Caught by the base64 case in
                # tests/unit/test_channel_webhook_auth.py, which is why that
                # case exists.)
                presented = sig_hdr.strip()
                for _pfx in ('sha256=', 'sha1=', 'sha512='):
                    if presented.lower().startswith(_pfx):
                        presented = presented[len(_pfx):]
                        break
                for algo in (hashlib.sha256, hashlib.sha1):
                    mac = _hmac.new(secret.encode('utf-8'), body, algo)
                    # Meta sends hex ("sha256=<hex>"); LINE sends base64.
                    for candidate in (mac.hexdigest(),
                                      base64.b64encode(mac.digest()).decode()):
                        if _hmac.compare_digest(candidate, presented):
                            return None

            logger.warning(
                "webhook %s REJECTED: no Kong consumer and no valid provider "
                "signature (secret_configured=%s, signature_header=%s)",
                channel_type, bool(secret), bool(sig_hdr))
            return jsonify({'error': 'unauthenticated webhook'}), 401

        def _channel_inbound_webhook(channel_type):
            adapter = self.registry.get(channel_type)

            if request.method == 'GET':
                mode = request.args.get('hub.mode')
                token = request.args.get('hub.verify_token')
                challenge = request.args.get('hub.challenge')
                expected = (os.getenv(f'{channel_type.upper()}_VERIFY_TOKEN')
                            or os.getenv('WEBHOOK_VERIFY_TOKEN'))
                if mode == 'subscribe' and challenge and expected and token == expected:
                    return Response(challenge, mimetype='text/plain')
                return ('verification failed', 403)

            # POST — AUTHENTICATE FIRST, before the adapter is touched.
            raw = request.get_data(as_text=True)
            denied = _webhook_caller_is_authenticated(channel_type, raw)
            if denied is not None:
                return denied

            if adapter is None:
                return jsonify({'error': f'no adapter registered for {channel_type}'}), 404
            handler = getattr(adapter, 'handle_webhook', None)
            if not callable(handler):
                return jsonify({'error': f'{channel_type} has no webhook handler'}), 400
            if self._loop is None:
                return jsonify({'error': 'channel event loop not running'}), 503

            sig = (request.headers.get('X-Hub-Signature-256')
                   or request.headers.get('X-Hub-Signature')
                   or request.headers.get('X-Line-Signature')
                   or request.headers.get('X-Signature'))

            # Map raw body + signature onto handle_webhook's actual params.
            call: Dict[str, Any] = {}
            for i, p in enumerate(inspect.signature(handler).parameters.values()):
                ann = str(p.annotation)
                if i == 0 or p.name in ('body', 'data', 'payload'):
                    wants_dict = ('Dict' in ann or 'dict' in ann or p.name == 'data')
                    if wants_dict:
                        try:
                            call[p.name] = json.loads(raw) if raw else {}
                        except Exception:
                            call[p.name] = {}
                    else:
                        call[p.name] = raw
                elif p.name in ('signature', 'sig'):
                    call[p.name] = sig
                elif p.name == 'event_type':
                    call[p.name] = request.headers.get('X-Event-Type', '')

            try:
                fut = asyncio.run_coroutine_threadsafe(handler(**call), self._loop)
                result = fut.result(timeout=30)
            except Exception as e:
                logger.warning(f"webhook {channel_type} handler error: {e}")
                return jsonify({'error': str(e)}), 500

            if isinstance(result, dict):
                return jsonify(result)  # e.g. google_chat inline reply
            return ('', 200)

        app.add_url_rule(
            f'{path_prefix}/<channel_type>',
            'channel_inbound_webhook',
            _channel_inbound_webhook,
            methods=['GET', 'POST'],
        )
        logger.info(
            f"Channel inbound-webhook route registered at {path_prefix}/<channel_type>")

    def set_user_session(
        self,
        channel: str,
        sender_id: str,
        user_id: int,
        prompt_id: int,
    ) -> None:
        """Set user session mapping for a channel sender."""
        session = self._session_manager.get_session(channel, sender_id, user_id=user_id, prompt_id=prompt_id)

    def _run_async_loop(self) -> None:
        """Run asyncio event loop in background thread."""
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)

        try:
            self._loop.run_until_complete(self.registry.start_all())
            self._loop.run_forever()
        finally:
            self._loop.run_until_complete(self.registry.stop_all())
            self._loop.close()

    @classmethod
    def _binding_credentials(
        cls, channel_type: str, meta: Dict[str, Any],
    ) -> tuple:
        """Extract ``(credential, extra_kwargs)`` from a binding's
        metadata_json for the given channel type.

        The credential is looked up under the channel's own ``token_param``
        first (signal stores 'phone_number', slack 'bot_token'), then under
        the generic key list.  For multi-input channels the declared `extra`
        params are passed through when the binding carries them, so a stored
        value beats the env fallback — matching register_channel's documented
        "explicit kwarg wins over env/default" precedence.
        """
        spec = cls._CHANNEL_SPECS.get(channel_type)
        keys = []
        if spec and spec.get('token_param'):
            keys.append(spec['token_param'])
        keys.extend(k for k in cls._BINDING_CREDENTIAL_KEYS if k not in keys)

        token = None
        used_key = None
        for k in keys:
            v = meta.get(k)
            if isinstance(v, str) and v.strip():
                token, used_key = v.strip(), k
                break

        extras: Dict[str, str] = {}
        if spec:
            for p in spec.get('extra', ()):
                name = p.get('param')
                v = meta.get(name)
                if name and name != used_key and isinstance(v, str) and v.strip():
                    extras[name] = v.strip()
        return token, extras

    def restore_persisted_channels(self) -> Dict[str, Any]:
        """Re-register channel adapters from persisted UserChannelBinding rows.

        Bindings survive a restart — that is the table's stated purpose — but
        the live adapters did not: nothing read them back at boot, so every
        HARTOS restart left Discord/Telegram/Slack/Signal disconnected until
        someone re-POSTed the binding by hand.  WhatsApp was the only channel
        with a rehydration path; this generalises it to the rest.

        Exactly ONE adapter can exist per channel_type (registry.register keys
        on adapter.name), so where several bindings share a channel_type the
        most recently updated active one wins — the same row a manual rebind
        would pick.  Channels already registered are left alone, so explicit
        env/code registration keeps precedence and this is idempotent.

        Never raises: a binding that cannot be restored is logged and skipped,
        because one bad row must not stop the server from booting.
        """
        summary: Dict[str, Any] = {'restored': [], 'skipped': {}}
        if os.environ.get(
            'HEVOLVE_CHANNEL_RESTORE', '1',
        ).strip().lower() in ('0', 'false', 'no', 'off'):
            logger.info("Channel restore disabled (HEVOLVE_CHANNEL_RESTORE)")
            return summary

        try:
            from integrations.social.models import get_db, UserChannelBinding
        except ImportError as e:
            logger.debug(f"Channel restore unavailable (no social models): {e}")
            return summary

        try:
            db = get_db()
            try:
                rows = db.query(UserChannelBinding).filter_by(
                    is_active=True,
                ).all()
                # Newest first.  updated_at is NULL on legacy rows, so fall
                # back to id, which is autoincrement and therefore monotonic.
                rows.sort(
                    key=lambda r: (r.updated_at or datetime.min, r.id or 0),
                    reverse=True,
                )

                # Group by channel, preserving the newest-first order.  The
                # newest row is NOT automatically the one to use: a channel
                # commonly has several bindings and the most recent can be
                # credential-less (an out-of-band pair, or a stale row from an
                # ad-hoc script).  Picking it and stopping would skip the
                # channel entirely and restore nothing, silently — so walk the
                # candidates until one actually registers.
                by_channel: Dict[str, list] = {}
                for row in rows:
                    ct = (row.channel_type or '').strip().lower()
                    if ct:
                        by_channel.setdefault(ct, []).append(row)

                for ct, candidates in by_channel.items():
                    if ct in self._RESTORE_EXCLUDED:
                        summary['skipped'][ct] = 'dedicated restore path'
                        continue
                    if ct not in self._ADAPTER_FACTORIES:
                        summary['skipped'][ct] = 'no adapter factory'
                        continue
                    if self.registry.get(ct) is not None:
                        summary['skipped'][ct] = 'already registered'
                        continue

                    reason = 'no stored credential'
                    for row in candidates:
                        meta = row.metadata_json
                        if not isinstance(meta, dict):
                            meta = {}
                        token, extras = self._binding_credentials(ct, meta)
                        if not token and ct not in self._NO_TOKEN_CHANNELS:
                            continue
                        if self.register_channel(ct, token=token, **extras):
                            summary['restored'].append(ct)
                            reason = None
                            break
                        # register_channel already logged why; a stale token
                        # on a newer row shouldn't mask an older working one.
                        reason = 'registration failed'
                    if reason:
                        summary['skipped'][ct] = reason
            finally:
                try:
                    db.close()
                except Exception:
                    pass
        except Exception as e:
            logger.warning(f"Channel restore failed: {e}")
            return summary

        if summary['restored']:
            logger.info(
                f"Restored {len(summary['restored'])} channel adapter(s) "
                f"from persisted bindings: {', '.join(summary['restored'])}"
            )
        else:
            logger.info(
                f"No channel adapters restored from bindings "
                f"(skipped: {summary['skipped'] or 'none'})"
            )
        return summary

    def start(self) -> None:
        """Start all channel adapters in background thread."""
        if self._thread and self._thread.is_alive():
            logger.warning("Channels already running")
            return

        # Credential-in-environment channels.  Driven off _ENV_FALLBACKS —
        # the same declarative map register_channel itself reads — rather than
        # a second hardcoded list.  hartos_bootstrap keeps its own 5-entry
        # dict whose names partly DISAGREE with this map (WHATSAPP_ACCESS_TOKEN
        # vs WHATSAPP_API_URL, SIGNAL_SERVICE_URL vs SIGNAL_PHONE_NUMBER) and
        # which omits google_chat entirely, so a GOOGLE_CHAT_WEBHOOK in the
        # environment registered nothing on either boot path.  Using the map
        # keeps one source of truth and covers every channel in it.
        #
        # register_channel resolves the env var itself, so passing no token is
        # enough; the getenv here only decides whether it is worth attempting
        # (heavy SDK modules must not be imported speculatively).
        for _ct, _env in self._ENV_FALLBACKS.items():
            if (_ct in self._ADAPTER_FACTORIES and os.environ.get(_env)
                    and self.registry.get(_ct) is None):
                self.register_channel(_ct)

        # `web` is in-process and credential-less, and hartos_bootstrap
        # registers it unconditionally ("in-process, cheap, always register")
        # as part of the bundled boot.  The standalone launcher never did, and
        # there is no `web` row in user_channel_bindings for the restore below
        # to pick up — so outside the bundle the in-process channel simply did
        # not exist.  Registering here makes the two boot paths equivalent; it
        # no-ops under bootstrap, which has already registered it.
        #
        # Opt out with HEVOLVE_WEB_CHANNEL=0: connect() binds a real listening
        # socket (WEB_ADAPTER_HOST, default 0.0.0.0:8765), so a deployment that
        # does not want that port exposed needs a way to say so.
        if self.registry.get('web') is None and os.environ.get(
            'HEVOLVE_WEB_CHANNEL', '1',
        ).strip().lower() not in ('0', 'false', 'no', 'off'):
            self.register_channel('web')

        # Re-wire adapters persisted in UserChannelBinding BEFORE the loop
        # thread starts: _run_async_loop's first act is registry.start_all(),
        # which connects whatever is registered by then.  Registering here
        # therefore needs no extra lifecycle machinery.
        self.restore_persisted_channels()

        self._thread = threading.Thread(target=self._run_async_loop, daemon=True)
        self._thread.start()
        logger.info("Channel adapters started in background")

    def stop(self) -> None:
        """Stop all channel adapters."""
        if self._loop:
            self._loop.call_soon_threadsafe(self._loop.stop)

        if self._thread:
            self._thread.join(timeout=5)

        logger.info("Channel adapters stopped")

    def get_status(self) -> Dict[str, str]:
        """Get status of all channels."""
        return {
            name: status.value
            for name, status in self.registry.get_status().items()
        }


# Global integration instance
_integration: Optional[FlaskChannelIntegration] = None


def get_channel_integration() -> FlaskChannelIntegration:
    """Get or create the global channel integration."""
    global _integration
    if _integration is None:
        _integration = FlaskChannelIntegration()
    return _integration


def _kong_stamped(headers) -> bool:
    """True if Kong authenticated the caller upstream and stamped its identity
    on the request. Trusted ONLY when HEVOLVE_TRUST_KONG=true — otherwise any
    client could forge the X-Consumer-* headers directly. Single source for the
    Kong-vouch check shared by the webhook gate and the /channels/send gate
    (was copy-pasted in both — DRY consolidation, behaviour-identical)."""
    if os.environ.get('HEVOLVE_TRUST_KONG', '').lower() != 'true':
        return False
    return bool(headers.get('X-Consumer-ID')
                or headers.get('X-Consumer-Username')
                or headers.get('X-Consumer-Custom-ID'))


def _channel_send_authenticated(req) -> bool:
    """Is the caller authorized to POST /channels/send?

    ``/channels/send`` relays a message through ANY registered channel to ANY
    ``chat_id``. Left open it is a spoof/spam relay — a reachable caller can
    impersonate the node to arbitrary recipients. Its sibling
    ``/channels/webhook`` is carefully gated by
    ``_webhook_caller_is_authenticated`` (fails closed), but this route shipped
    with no gate at all, and it lives in NEITHER of the security middleware's
    protected tuples (``ADMIN_PATHS`` / ``NETWORK_PROTECTED_PATHS``), so the
    app-level auth in ``security/middleware.py`` never covered it on any tier —
    it was public on flat, regional AND central.

    This mirrors the model that middleware already applies to state-mutating
    operator routes, reusing the SAME primitives so there is ONE answer to "is
    this caller authenticated", never a parallel auth scheme:

      * ``NUNBA_BUNDLED`` single-user in-process desktop → trusted, exactly as
        the middleware's ``check_api_auth`` early-returns for it.
      * KONG vouched — ``HEVOLVE_TRUST_KONG=true`` and an ``X-Consumer-*`` stamp
        present. The same proof the sibling webhook gate accepts.
      * A configured ``HEVOLVE_API_KEY`` presented as ``X-API-Key`` (constant
        time), the shared key the middleware's ``_require_api_key_or_bearer``
        honors.
      * A valid Bearer JWT, verified by the canonical
        ``integrations.social.auth.decode_jwt`` (the very function the
        middleware decodes with — not a second decoder).

    Fails closed: anything else is unauthenticated.
    """
    # (0) Bundled single-user desktop is trusted — the desktop UI and the
    #     in-process test client have no network exposure.
    if os.environ.get('NUNBA_BUNDLED'):
        return True

    # (1) Kong authenticated the caller upstream and stamped its identity.
    if _kong_stamped(req.headers):
        return True

    # (2) Shared API key.
    expected_key = os.environ.get('HEVOLVE_API_KEY', '')
    if expected_key:
        import hmac as _hmac
        presented = req.headers.get('X-API-Key', '')
        if presented and _hmac.compare_digest(presented, expected_key):
            return True

    # (3) Bearer JWT — canonical verifier, no parallel decode path.
    auth_header = req.headers.get('Authorization', '')
    if auth_header.startswith('Bearer '):
        try:
            from integrations.social.auth import decode_jwt
            payload = decode_jwt(auth_header[7:])
            if payload and payload.get('user_id'):
                return True
        except Exception as e:  # never authenticate on a verifier error
            logger.debug("channel_send bearer verify failed: %s", e)

    return False


def init_channels(app=None, config: Dict[str, Any] = None) -> FlaskChannelIntegration:
    """
    Initialize channel integrations.

    Call this from your Flask app startup:

        from integrations.channels.flask_integration import init_channels

        app = Flask(__name__)
        channels = init_channels(app)
        channels.register_telegram()
        channels.start()

    Args:
        app: Flask app instance (optional)
        config: Configuration dict (optional)

    Returns:
        FlaskChannelIntegration instance
    """
    config = config or {}
    from core.constants import DEFAULT_USER_ID, DEFAULT_PROMPT_ID

    integration = FlaskChannelIntegration(
        agent_api_url=config.get("agent_api_url", "http://localhost:6777/chat"),
        default_user_id=config.get("default_user_id", DEFAULT_USER_ID),
        default_prompt_id=config.get("default_prompt_id", DEFAULT_PROMPT_ID),
        create_mode=config.get("create_mode", False),
        device_id=config.get("device_id"),
    )

    global _integration
    _integration = integration

    # Add Flask routes if app provided
    if app:
        @app.route("/channels/status", methods=["GET"])
        def channel_status():
            return integration.get_status()

        @app.route("/channels/send", methods=["POST"])
        def channel_send():
            from flask import request, jsonify

            # AUTHENTICATE FIRST — before the body is read or any message is
            # sent. Unauthenticated, this route is an outbound spoof/spam relay
            # (see _channel_send_authenticated). Fail closed on every exposed
            # tier; only bundled single-user desktop is trusted.
            if not _channel_send_authenticated(request):
                return jsonify({"error": "Authentication required"}), 401

            data = request.json
            channel = data.get("channel")
            chat_id = data.get("chat_id")
            text = data.get("text")

            if not all([channel, chat_id, text]):
                return jsonify({"error": "Missing required fields"}), 400

            # Run async send in the event loop
            if integration._loop:
                future = asyncio.run_coroutine_threadsafe(
                    integration.registry.send_to_channel(channel, chat_id, text),
                    integration._loop,
                )
                result = future.result(timeout=30)
                return jsonify({
                    "success": result.success,
                    "message_id": result.message_id,
                    "error": result.error,
                })
            else:
                return jsonify({"error": "Channels not running"}), 503

    return integration
