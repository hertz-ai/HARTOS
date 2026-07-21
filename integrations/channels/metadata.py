"""
Channel Metadata Catalog — Static registry of all 31 supported channels.

Provides per-channel: display name, auth method, setup fields, capabilities, icon, color.
Used by the setup wizard UI and the /api/social/channels/catalog endpoint.

OAuth click-through (PR O):
- Channels that publish an OAuth 2.0 authorize endpoint additionally declare
  ``oauth_authorize_url`` / ``oauth_token_url`` / ``oauth_scopes`` /
  ``oauth_extra_params`` / ``oauth_token_response_map`` / ``oauth_uses_pkce``.
- The Connect_Channel agent tool consults these fields *only when* the
  operator has set ``HARTOS_OAUTH_CLIENT_<TYPE>`` env (client id + secret).
  When env is unset the channel falls back to the existing setup_fields
  paste-token flow — zero regression for legacy operators.
- ``external_url`` points at the provider's app-management portal
  (BotFather, Discord dev portal, etc.) and is also used by the paste
  fallback for a "Open <provider>" button.
"""

CHANNEL_CATALOG = {
    # ── Core Adapters ──────────────────────────────────────────
    'telegram': {
        'display_name': 'Telegram',
        'icon': 'telegram',
        'color': '#0088cc',
        'category': 'core',
        'auth_method': 'api_key',
        'setup_fields': [
            {'key': 'bot_token', 'label': 'Bot Token', 'type': 'password',
             'help': 'Create a bot via @BotFather and paste the token here.'},
        ],
        'capabilities': {
            'text': True, 'image': True, 'video': True, 'audio': True,
            'document': True, 'sticker': True, 'location': True, 'voice': True,
            'reactions': True, 'message_edit': True, 'message_delete': True,
            'streaming': True, 'groups': True, 'threads': False,
            'buttons': True, 'typing': True,
            'max_message_length': 4096,
        },
    },
    'discord': {
        'display_name': 'Discord',
        'icon': 'discord',
        'color': '#5865f2',
        'category': 'core',
        'auth_method': 'api_key',
        # OAuth bot-install URL (PR O).  274877990912 = Send Messages |
        # Read Message History | View Channels — minimum bot perms.
        'oauth_authorize_url': 'https://discord.com/api/oauth2/authorize',
        'oauth_token_url': 'https://discord.com/api/oauth2/token',
        'oauth_scopes': 'bot applications.commands',
        'oauth_extra_params': {'permissions': '274877990912'},
        'oauth_token_response_map': {'access_token': 'bot_token'},
        'external_url': 'https://discord.com/developers/applications',
        'setup_fields': [
            {'key': 'bot_token', 'label': 'Bot Token', 'type': 'password',
             'help': 'Create an application at discord.com/developers, add a Bot, and copy the token.'},
        ],
        'capabilities': {
            'text': True, 'image': True, 'video': True, 'audio': True,
            'document': True, 'sticker': False, 'location': False, 'voice': False,
            'reactions': True, 'message_edit': True, 'message_delete': True,
            'streaming': True, 'groups': True, 'threads': True,
            'buttons': True, 'typing': True,
            'max_message_length': 2000,
        },
    },
    'slack': {
        'display_name': 'Slack',
        'icon': 'slack',
        'color': '#4a154b',
        'category': 'core',
        'auth_method': 'api_key',
        # Slack v2 OAuth — bot scopes only (no user scopes by default).
        # Returns access_token=xoxb-... in the bot section of v2 response;
        # token-exchange endpoint maps the bot.access_token into bot_token.
        'oauth_authorize_url': 'https://slack.com/oauth/v2/authorize',
        'oauth_token_url': 'https://slack.com/api/oauth.v2.access',
        'oauth_scopes': 'chat:write,channels:history,channels:read,im:history,im:read,im:write,users:read',
        'oauth_token_response_map': {
            # Slack's bot_user_id comes from auth.test at adapter
            # connect time (slack_adapter.py:101), not from this map —
            # that's why we only persist bot.access_token here.
            # signing_secret is operator-paste-only (not in OAuth resp).
            'bot.access_token': 'bot_token',
        },
        'external_url': 'https://api.slack.com/apps',
        'setup_fields': [
            {'key': 'bot_token', 'label': 'Bot Token (xoxb-...)', 'type': 'password',
             'help': 'Create a Slack App, install it to your workspace, and copy the Bot User OAuth Token.'},
            {'key': 'signing_secret', 'label': 'Signing Secret', 'type': 'password',
             'help': 'Found in your Slack App settings under Basic Information.'},
        ],
        'capabilities': {
            'text': True, 'image': True, 'video': False, 'audio': False,
            'document': True, 'sticker': False, 'location': False, 'voice': False,
            'reactions': True, 'message_edit': True, 'message_delete': True,
            'streaming': True, 'groups': True, 'threads': True,
            'buttons': True, 'typing': True,
            'max_message_length': 40000,
        },
    },
    'whatsapp': {
        'display_name': 'WhatsApp',
        'icon': 'whatsapp',
        'color': '#25d366',
        'category': 'core',
        # auth_method=gateway_qr — backed by the embedded Baileys gateway
        # auto-spawned by integrations/social/whatsapp_supervisor.py.  The
        # wizard fetches a real WhatsApp-Web QR from
        # GET /api/social/channels/whatsapp/qr (which proxies to the
        # gateway's GET /api/sessions/:id/qr) and the user scans it from
        # WhatsApp → Linked Devices → Link a Device.  No Docker, no
        # developer portal, no API keys.  Alternate path: "Link with
        # phone number" — POST /whatsapp/pair-code returns the 8-char
        # code that WhatsApp accepts under Link a Device → Link with
        # phone number instead, no QR scan needed.
        'auth_method': 'gateway_qr',
        'setup_fields': [
            {'key': 'phone_number', 'label': 'Your WhatsApp Number', 'type': 'tel',
             'help': 'Your own E.164 number (e.g. +<country><number>). '
                     'Required only for "Link with phone number"; QR scan '
                     'works without it.'},
            # auto:True — admin Channels page shows these so operators can
            # point at a remote WAHA endpoint instead of the local Baileys
            # gateway.  Connect_Channel's form-builder skips auto:True
            # fields on the user-facing wizard.
            {'key': 'api_url', 'label': 'Gateway Base URL', 'type': 'text',
             'auto': True, 'default': 'http://localhost:3000',
             'help': 'Defaults to the embedded Baileys gateway on '
                     'localhost:3000.  Override via WHATSAPP_API_URL env '
                     'to point at a remote WAHA endpoint instead.'},
            {'key': 'access_token', 'label': 'API Key', 'type': 'password',
             'auto': True, 'default': '',
             'help': 'Empty for the embedded gateway (no auth).  Set via '
                     'WHATSAPP_API_KEY env when fronting a remote WAHA '
                     'with auth.'},
            {'key': 'enable_self_chat_agent', 'label': 'Self-chat → Nunba',
             'type': 'toggle', 'default': True,
             'help': 'When you tap your own contact in WhatsApp ("Message '
                     'Yourself"), Nunba saves the note to memory and replies '
                     'in the same thread. Private — never fanned out.'},
        ],
        'capabilities': {
            'text': True, 'image': True, 'video': True, 'audio': True,
            'document': True, 'sticker': True, 'location': True, 'voice': True,
            'reactions': False, 'message_edit': False, 'message_delete': False,
            'streaming': False, 'groups': True, 'threads': False,
            'buttons': False, 'typing': True,
            'max_message_length': 4096,
        },
    },
    'signal': {
        'display_name': 'Signal',
        'icon': 'signal',
        'color': '#3a76f0',
        'category': 'core',
        'auth_method': 'credentials',
        'setup_fields': [
            {'key': 'phone_number', 'label': 'Registered Phone Number', 'type': 'text',
             'help': 'Phone number registered with Signal (e.g. +1234567890).'},
            # Server-side deployment detail, not something an end user
            # would know or should be asked to type on their phone — marked
            # auto so it's skipped in the connect form (same pattern as
            # WhatsApp's api_url/access_token auto fields) and never
            # actually consumed by create_signal_adapter() anyway (its
            # real params are phone_number/api_url — this key was dead,
            # matched nothing the adapter reads).
            {'key': 'signal_cli_path', 'label': 'signal-cli Path', 'type': 'text',
             'auto': True, 'default': '',
             'help': 'Path to signal-cli binary on the server.'},
        ],
        'capabilities': {
            'text': True, 'image': True, 'video': True, 'audio': True,
            'document': True, 'sticker': True, 'location': False, 'voice': True,
            'reactions': True, 'message_edit': False, 'message_delete': True,
            'streaming': False, 'groups': True, 'threads': False,
            'buttons': False, 'typing': True,
            'max_message_length': 6000,
        },
    },
    'imessage': {
        'display_name': 'iMessage',
        'icon': 'imessage',
        'color': '#34c759',
        'category': 'core',
        'auth_method': 'credentials',
        'setup_fields': [
            {'key': 'bridge_url', 'label': 'iMessage Bridge URL', 'type': 'text',
             'help': 'Requires a macOS machine running the iMessage bridge.'},
        ],
        'capabilities': {
            'text': True, 'image': True, 'video': True, 'audio': True,
            'document': True, 'sticker': False, 'location': False, 'voice': False,
            'reactions': True, 'message_edit': False, 'message_delete': False,
            'streaming': False, 'groups': True, 'threads': False,
            'buttons': False, 'typing': True,
            'max_message_length': 20000,
        },
    },
    'google_chat': {
        'display_name': 'Google Chat',
        'icon': 'google_chat',
        'color': '#00ac47',
        'category': 'core',
        'auth_method': 'oauth2',
        # Google requires PKCE for installed-app flows + access_type=offline
        # for refresh tokens.  prompt=consent forces the consent screen
        # so the user always sees what scopes are being granted.
        'oauth_authorize_url': 'https://accounts.google.com/o/oauth2/v2/auth',
        'oauth_token_url': 'https://oauth2.googleapis.com/token',
        'oauth_scopes': 'https://www.googleapis.com/auth/chat.messages '
                        'https://www.googleapis.com/auth/chat.spaces',
        'oauth_extra_params': {'access_type': 'offline', 'prompt': 'consent'},
        'oauth_token_response_map': {
            'access_token': 'access_token',
            'refresh_token': 'refresh_token',
        },
        'oauth_uses_pkce': True,
        'external_url': 'https://console.cloud.google.com/apis/credentials',
        'setup_fields': [
            {'key': 'client_id', 'label': 'OAuth Client ID', 'type': 'text',
             'help': 'From Google Cloud Console → APIs & Services → Credentials.'},
            {'key': 'client_secret', 'label': 'OAuth Client Secret', 'type': 'password'},
        ],
        'capabilities': {
            'text': True, 'image': True, 'video': False, 'audio': False,
            'document': True, 'sticker': False, 'location': False, 'voice': False,
            'reactions': False, 'message_edit': True, 'message_delete': True,
            'streaming': False, 'groups': True, 'threads': True,
            'buttons': True, 'typing': False,
            'max_message_length': 4096,
        },
    },
    'web': {
        'display_name': 'Web Chat',
        'icon': 'web',
        'color': '#6c63ff',
        'category': 'core',
        'auth_method': 'api_key',
        'setup_fields': [
            {'key': 'widget_key', 'label': 'Widget API Key', 'type': 'password',
             'help': 'Auto-generated. Embed the widget JS on your site.'},
        ],
        'capabilities': {
            'text': True, 'image': True, 'video': True, 'audio': True,
            'document': True, 'sticker': False, 'location': False, 'voice': False,
            'reactions': False, 'message_edit': True, 'message_delete': True,
            'streaming': True, 'groups': False, 'threads': False,
            'buttons': True, 'typing': True,
            'max_message_length': 10000,
        },
    },

    # ── Enterprise Adapters ────────────────────────────────────
    'teams': {
        'display_name': 'Microsoft Teams',
        'icon': 'teams',
        'color': '#6264a7',
        'category': 'enterprise',
        'auth_method': 'oauth2',
        # Microsoft Identity v2.0 endpoint.  Tenant 'common' supports
        # multi-tenant org sign-in; operator can override per-tenant via
        # HARTOS_OAUTH_TENANT_TEAMS env if they registered a single-tenant
        # app.  PKCE recommended for public clients; we always send it.
        'oauth_authorize_url': 'https://login.microsoftonline.com/common/oauth2/v2.0/authorize',
        'oauth_token_url': 'https://login.microsoftonline.com/common/oauth2/v2.0/token',
        'oauth_scopes': 'https://graph.microsoft.com/Chat.ReadWrite '
                        'https://graph.microsoft.com/ChannelMessage.Send '
                        'offline_access',
        'oauth_token_response_map': {
            'access_token': 'access_token',
            'refresh_token': 'refresh_token',
        },
        'oauth_uses_pkce': True,
        'external_url': 'https://portal.azure.com/#view/Microsoft_AAD_RegisteredApps/ApplicationsListBlade',
        'setup_fields': [
            {'key': 'app_id', 'label': 'Microsoft App ID', 'type': 'text',
             'help': 'From Azure Bot registration.'},
            {'key': 'app_password', 'label': 'App Password', 'type': 'password'},
        ],
        'capabilities': {
            'text': True, 'image': True, 'video': True, 'audio': True,
            'document': True, 'sticker': False, 'location': False, 'voice': False,
            'reactions': True, 'message_edit': True, 'message_delete': True,
            'streaming': True, 'groups': True, 'threads': True,
            'buttons': True, 'typing': True,
            'max_message_length': 28000,
        },
    },
    'mattermost': {
        'display_name': 'Mattermost',
        'icon': 'mattermost',
        'color': '#0058cc',
        'category': 'enterprise',
        'auth_method': 'websocket_token',
        'setup_fields': [
            {'key': 'server_url', 'label': 'Mattermost Server URL', 'type': 'text',
             'help': 'e.g. https://your-mattermost.example.com'},
            {'key': 'access_token', 'label': 'Personal Access Token', 'type': 'password',
             'help': 'Generate in Mattermost → Account Settings → Security.'},
        ],
        'capabilities': {
            'text': True, 'image': True, 'video': True, 'audio': True,
            'document': True, 'sticker': False, 'location': False, 'voice': False,
            'reactions': True, 'message_edit': True, 'message_delete': True,
            'streaming': True, 'groups': True, 'threads': True,
            'buttons': True, 'typing': True,
            'max_message_length': 16383,
        },
    },
    'matrix': {
        'display_name': 'Matrix',
        'icon': 'matrix',
        'color': '#0dbd8b',
        'category': 'enterprise',
        'auth_method': 'websocket_token',
        'setup_fields': [
            {'key': 'homeserver_url', 'label': 'Homeserver URL', 'type': 'text',
             'help': 'e.g. https://matrix.org'},
            {'key': 'access_token', 'label': 'Access Token', 'type': 'password',
             'help': 'From Element → Settings → Help & About → Access Token.'},
        ],
        'capabilities': {
            'text': True, 'image': True, 'video': True, 'audio': True,
            'document': True, 'sticker': True, 'location': True, 'voice': False,
            'reactions': True, 'message_edit': True, 'message_delete': True,
            'streaming': True, 'groups': True, 'threads': True,
            'buttons': False, 'typing': True,
            'max_message_length': 65536,
        },
    },
    'nextcloud': {
        'display_name': 'Nextcloud Talk',
        'icon': 'nextcloud',
        'color': '#0082c9',
        'category': 'enterprise',
        'auth_method': 'websocket_token',
        'setup_fields': [
            {'key': 'server_url', 'label': 'Nextcloud URL', 'type': 'text'},
            {'key': 'username', 'label': 'Username', 'type': 'text'},
            {'key': 'app_password', 'label': 'App Password', 'type': 'password',
             'help': 'Generate in Nextcloud → Settings → Security → App Passwords.'},
        ],
        'capabilities': {
            'text': True, 'image': True, 'video': True, 'audio': True,
            'document': True, 'sticker': False, 'location': False, 'voice': False,
            'reactions': True, 'message_edit': True, 'message_delete': True,
            'streaming': True, 'groups': True, 'threads': False,
            'buttons': False, 'typing': True,
            'max_message_length': 32000,
        },
    },
    'rocketchat': {
        'display_name': 'Rocket.Chat',
        'icon': 'rocketchat',
        'color': '#f5455c',
        'category': 'enterprise',
        'auth_method': 'websocket_token',
        'setup_fields': [
            {'key': 'server_url', 'label': 'Rocket.Chat URL', 'type': 'text'},
            {'key': 'user_id', 'label': 'User ID', 'type': 'text'},
            {'key': 'auth_token', 'label': 'Auth Token', 'type': 'password',
             'help': 'Generate in Administration → Integrations or your profile.'},
        ],
        'capabilities': {
            'text': True, 'image': True, 'video': True, 'audio': True,
            'document': True, 'sticker': False, 'location': False, 'voice': False,
            'reactions': True, 'message_edit': True, 'message_delete': True,
            'streaming': True, 'groups': True, 'threads': True,
            'buttons': True, 'typing': True,
            'max_message_length': 65536,
        },
    },

    # ── Social Adapters ────────────────────────────────────────
    'messenger': {
        'display_name': 'Facebook Messenger',
        'icon': 'messenger',
        'color': '#0084ff',
        'category': 'social',
        'auth_method': 'api_key',
        # Meta's Login Dialog → page access token.  pages_messaging is
        # required to send DMs; pages_show_list lets the user pick which
        # Page to bind.  After /oauth/callback, the access_token is a
        # short-lived user token; we exchange it for a long-lived page
        # token in api_channels._exchange_oauth_code (Meta-specific
        # post-step) so what gets stored is page_access_token.
        'oauth_authorize_url': 'https://www.facebook.com/v18.0/dialog/oauth',
        'oauth_token_url': 'https://graph.facebook.com/v18.0/oauth/access_token',
        'oauth_scopes': 'pages_messaging,pages_show_list,pages_manage_metadata',
        'oauth_token_response_map': {'access_token': 'page_access_token'},
        'external_url': 'https://developers.facebook.com/apps/',
        'setup_fields': [
            {'key': 'page_access_token', 'label': 'Page Access Token', 'type': 'password',
             'help': 'From Meta Developer Portal → Your App → Messenger → Settings.'},
            {'key': 'app_secret', 'label': 'App Secret', 'type': 'password'},
            {'key': 'verify_token', 'label': 'Webhook Verify Token', 'type': 'text'},
        ],
        'capabilities': {
            'text': True, 'image': True, 'video': True, 'audio': True,
            'document': True, 'sticker': False, 'location': True, 'voice': False,
            'reactions': False, 'message_edit': False, 'message_delete': False,
            'streaming': False, 'groups': False, 'threads': False,
            'buttons': True, 'typing': True,
            'max_message_length': 2000,
        },
    },
    'instagram': {
        'display_name': 'Instagram',
        'icon': 'instagram',
        'color': '#e4405f',
        'category': 'social',
        'auth_method': 'api_key',
        # Instagram Graph API uses Meta's same OAuth machinery; instagram_basic
        # + instagram_manage_messages = DM access on a Professional account.
        'oauth_authorize_url': 'https://www.facebook.com/v18.0/dialog/oauth',
        'oauth_token_url': 'https://graph.facebook.com/v18.0/oauth/access_token',
        'oauth_scopes': 'instagram_basic,instagram_manage_messages,pages_show_list',
        'oauth_token_response_map': {'access_token': 'page_access_token'},
        'external_url': 'https://developers.facebook.com/apps/',
        'setup_fields': [
            {'key': 'page_access_token', 'label': 'Instagram Access Token', 'type': 'password',
             'help': 'Requires a connected Facebook Page with Instagram Professional account.'},
            {'key': 'app_secret', 'label': 'App Secret', 'type': 'password'},
        ],
        'capabilities': {
            'text': True, 'image': True, 'video': False, 'audio': False,
            'document': False, 'sticker': False, 'location': False, 'voice': False,
            'reactions': False, 'message_edit': False, 'message_delete': False,
            'streaming': False, 'groups': False, 'threads': False,
            'buttons': False, 'typing': True,
            'max_message_length': 1000,
        },
    },
    'twitter': {
        'display_name': 'Twitter / X',
        'icon': 'twitter',
        'color': '#1da1f2',
        'category': 'social',
        'auth_method': 'api_key',
        # Twitter / X v2 OAuth 2.0 with PKCE (mandatory for OAuth 2.0).
        # The legacy 5-token v1.1 form stays as the paste fallback for
        # operators using the older API; OAuth 2.0 only returns
        # access_token + refresh_token (no api_key/secret).
        'oauth_authorize_url': 'https://twitter.com/i/oauth2/authorize',
        'oauth_token_url': 'https://api.twitter.com/2/oauth2/token',
        'oauth_scopes': 'tweet.read tweet.write users.read dm.read dm.write offline.access',
        'oauth_token_response_map': {
            'access_token': 'bearer_token',
            'refresh_token': 'refresh_token',
        },
        'oauth_uses_pkce': True,
        'external_url': 'https://developer.twitter.com/en/portal/dashboard',
        'setup_fields': [
            {'key': 'bearer_token', 'label': 'API v2 Bearer Token', 'type': 'password',
             'help': 'From developer.twitter.com → Projects → Keys & Tokens.'},
            {'key': 'api_key', 'label': 'API Key', 'type': 'password'},
            {'key': 'api_secret', 'label': 'API Secret', 'type': 'password'},
            {'key': 'access_token', 'label': 'Access Token', 'type': 'password'},
            {'key': 'access_secret', 'label': 'Access Token Secret', 'type': 'password'},
        ],
        'capabilities': {
            'text': True, 'image': True, 'video': True, 'audio': False,
            'document': False, 'sticker': False, 'location': False, 'voice': False,
            'reactions': False, 'message_edit': False, 'message_delete': True,
            'streaming': False, 'groups': False, 'threads': False,
            'buttons': False, 'typing': False,
            'max_message_length': 10000,
        },
    },
    'line': {
        'display_name': 'LINE',
        'icon': 'line',
        'color': '#00b900',
        'category': 'social',
        'auth_method': 'api_key',
        # LINE Login OAuth 2.1 — issues a user access_token; for Messaging
        # API channel binding the operator still needs a Channel Access
        # Token (long-lived) which OAuth doesn't issue.  We therefore
        # treat this as half-OAuth: the click-through performs identity
        # verification + paste-token follow-up for the Channel Access
        # Token.  oauth_token_response_map is intentionally empty so
        # /oauth/callback does NOT auto-write a binding; instead it
        # bounces back to the paste form pre-filled with the verified
        # channel_id (api_channels handles this LINE-specific path).
        'oauth_authorize_url': 'https://access.line.me/oauth2/v2.1/authorize',
        'oauth_token_url': 'https://api.line.me/oauth2/v2.1/token',
        'oauth_scopes': 'profile openid',
        'oauth_token_response_map': {},
        'external_url': 'https://developers.line.biz/console/',
        'setup_fields': [
            {'key': 'channel_access_token', 'label': 'Channel Access Token', 'type': 'password',
             'help': 'From LINE Developers Console → Messaging API → Channel access token.'},
            {'key': 'channel_secret', 'label': 'Channel Secret', 'type': 'password'},
        ],
        'capabilities': {
            'text': True, 'image': True, 'video': True, 'audio': True,
            'document': False, 'sticker': True, 'location': True, 'voice': False,
            'reactions': False, 'message_edit': False, 'message_delete': False,
            'streaming': False, 'groups': True, 'threads': False,
            'buttons': True, 'typing': True,
            'max_message_length': 5000,
        },
    },
    'viber': {
        'display_name': 'Viber',
        'icon': 'viber',
        'color': '#665cac',
        'category': 'social',
        'auth_method': 'api_key',
        'setup_fields': [
            {'key': 'auth_token', 'label': 'Bot Auth Token', 'type': 'password',
             'help': 'From partners.viber.com → Create Bot Account.'},
        ],
        'capabilities': {
            'text': True, 'image': True, 'video': True, 'audio': False,
            'document': True, 'sticker': True, 'location': True, 'voice': False,
            'reactions': False, 'message_edit': False, 'message_delete': False,
            'streaming': False, 'groups': True, 'threads': False,
            'buttons': True, 'typing': True,
            'max_message_length': 7000,
        },
    },
    'wechat': {
        'display_name': 'WeChat',
        'icon': 'wechat',
        'color': '#07c160',
        'category': 'social',
        'auth_method': 'api_key',
        'setup_fields': [
            {'key': 'app_id', 'label': 'Official Account App ID', 'type': 'text'},
            {'key': 'app_secret', 'label': 'App Secret', 'type': 'password'},
            {'key': 'token', 'label': 'Server Token', 'type': 'password',
             'help': 'The token you set in WeChat Official Account settings.'},
        ],
        'capabilities': {
            'text': True, 'image': True, 'video': True, 'audio': True,
            'document': False, 'sticker': False, 'location': True, 'voice': True,
            'reactions': False, 'message_edit': False, 'message_delete': False,
            'streaming': False, 'groups': False, 'threads': False,
            'buttons': False, 'typing': False,
            'max_message_length': 2048,
        },
    },
    'zalo': {
        'display_name': 'Zalo',
        'icon': 'zalo',
        'color': '#0068ff',
        'category': 'social',
        'auth_method': 'api_key',
        'setup_fields': [
            {'key': 'oa_access_token', 'label': 'Official Account Access Token', 'type': 'password',
             'help': 'From Zalo Developers → Your OA → Settings.'},
        ],
        'capabilities': {
            'text': True, 'image': True, 'video': False, 'audio': False,
            'document': True, 'sticker': True, 'location': False, 'voice': False,
            'reactions': False, 'message_edit': False, 'message_delete': False,
            'streaming': False, 'groups': False, 'threads': False,
            'buttons': True, 'typing': True,
            'max_message_length': 2000,
        },
    },
    'twitch': {
        'display_name': 'Twitch',
        'icon': 'twitch',
        'color': '#9146ff',
        'category': 'social',
        'auth_method': 'api_key',
        # Twitch authorization code flow.  chat:read + chat:edit are the
        # IRC scopes needed for the legacy chat bot; channel:bot is the
        # newer EventSub scope for the Helix-based bot path.  We request
        # both so the adapter can pick at runtime.
        'oauth_authorize_url': 'https://id.twitch.tv/oauth2/authorize',
        'oauth_token_url': 'https://id.twitch.tv/oauth2/token',
        'oauth_scopes': 'chat:read chat:edit channel:bot user:bot',
        'oauth_token_response_map': {
            'access_token': 'oauth_token',
            'refresh_token': 'refresh_token',
        },
        'external_url': 'https://dev.twitch.tv/console/apps',
        'setup_fields': [
            {'key': 'oauth_token', 'label': 'OAuth Token', 'type': 'password',
             'help': 'Generate at twitchapps.com/tmi or via Twitch Developer portal.'},
            {'key': 'channel_name', 'label': 'Channel Name', 'type': 'text'},
        ],
        'capabilities': {
            'text': True, 'image': False, 'video': False, 'audio': False,
            'document': False, 'sticker': False, 'location': False, 'voice': False,
            'reactions': False, 'message_edit': False, 'message_delete': True,
            'streaming': False, 'groups': True, 'threads': False,
            'buttons': False, 'typing': False,
            'max_message_length': 500,
        },
    },

    # ── Decentralized Adapters ─────────────────────────────────
    'nostr': {
        'display_name': 'Nostr',
        'icon': 'nostr',
        'color': '#8e30eb',
        'category': 'decentralized',
        'auth_method': 'api_key',
        'setup_fields': [
            {'key': 'private_key', 'label': 'Private Key (nsec)', 'type': 'password',
             'help': 'Your Nostr private key. Keep this safe!'},
            {'key': 'relays', 'label': 'Relay URLs (comma-separated)', 'type': 'text',
             'help': 'e.g. wss://relay.damus.io,wss://nos.lol'},
        ],
        'capabilities': {
            'text': True, 'image': True, 'video': False, 'audio': False,
            'document': False, 'sticker': False, 'location': False, 'voice': False,
            'reactions': True, 'message_edit': False, 'message_delete': False,
            'streaming': False, 'groups': True, 'threads': True,
            'buttons': False, 'typing': False,
            'max_message_length': 65535,
        },
    },
    'tlon': {
        'display_name': 'Tlon (Urbit)',
        'icon': 'tlon',
        'color': '#1a1a2e',
        'category': 'decentralized',
        'auth_method': 'credentials',
        'setup_fields': [
            {'key': 'ship_url', 'label': 'Ship URL', 'type': 'text',
             'help': 'Your Urbit ship HTTP URL (e.g. http://localhost:8080).'},
            {'key': 'access_code', 'label': 'Access Code (+code)', 'type': 'password'},
        ],
        'capabilities': {
            'text': True, 'image': True, 'video': False, 'audio': False,
            'document': False, 'sticker': False, 'location': False, 'voice': False,
            'reactions': False, 'message_edit': True, 'message_delete': True,
            'streaming': False, 'groups': True, 'threads': True,
            'buttons': False, 'typing': False,
            'max_message_length': 65535,
        },
    },
    'openprose': {
        'display_name': 'OpenProse',
        'icon': 'openprose',
        'color': '#ff6b35',
        'category': 'decentralized',
        'auth_method': 'api_key',
        'setup_fields': [
            {'key': 'node_url', 'label': 'Node URL', 'type': 'text'},
            {'key': 'api_key', 'label': 'API Key', 'type': 'password'},
        ],
        'capabilities': {
            'text': True, 'image': True, 'video': False, 'audio': False,
            'document': True, 'sticker': False, 'location': False, 'voice': False,
            'reactions': True, 'message_edit': True, 'message_delete': True,
            'streaming': False, 'groups': True, 'threads': True,
            'buttons': False, 'typing': False,
            'max_message_length': 65535,
        },
    },

    # ── Bridge / User-Account Adapters ─────────────────────────
    'telegram_user': {
        'display_name': 'Telegram (User Account)',
        'icon': 'telegram',
        'color': '#0088cc',
        'category': 'bridge',
        'auth_method': 'qr_session',
        'setup_fields': [
            {'key': 'api_id', 'label': 'API ID', 'type': 'text',
             'help': 'From my.telegram.org → API Development Tools.'},
            {'key': 'api_hash', 'label': 'API Hash', 'type': 'password'},
            {'key': 'phone', 'label': 'Phone Number', 'type': 'text',
             'help': 'Your Telegram phone number for 2FA verification.'},
        ],
        'capabilities': {
            'text': True, 'image': True, 'video': True, 'audio': True,
            'document': True, 'sticker': True, 'location': True, 'voice': True,
            'reactions': True, 'message_edit': True, 'message_delete': True,
            'streaming': True, 'groups': True, 'threads': False,
            'buttons': False, 'typing': True,
            'max_message_length': 4096,
        },
    },
    'discord_user': {
        'display_name': 'Discord (User Account)',
        'icon': 'discord',
        'color': '#5865f2',
        'category': 'bridge',
        'auth_method': 'qr_session',
        'setup_fields': [
            {'key': 'user_token', 'label': 'User Token', 'type': 'password',
             'help': 'WARNING: Self-bots violate Discord ToS. Use at your own risk.'},
        ],
        'capabilities': {
            'text': True, 'image': True, 'video': True, 'audio': True,
            'document': True, 'sticker': True, 'location': False, 'voice': False,
            'reactions': True, 'message_edit': True, 'message_delete': True,
            'streaming': True, 'groups': True, 'threads': True,
            'buttons': False, 'typing': True,
            'max_message_length': 2000,
        },
    },
    'zalo_user': {
        'display_name': 'Zalo (User Account)',
        'icon': 'zalo',
        'color': '#0068ff',
        'category': 'bridge',
        'auth_method': 'phone_2fa',
        'setup_fields': [
            {'key': 'phone', 'label': 'Phone Number', 'type': 'text'},
        ],
        'capabilities': {
            'text': True, 'image': True, 'video': False, 'audio': False,
            'document': True, 'sticker': True, 'location': False, 'voice': False,
            'reactions': False, 'message_edit': False, 'message_delete': False,
            'streaming': False, 'groups': True, 'threads': False,
            'buttons': False, 'typing': True,
            'max_message_length': 2000,
        },
    },
    'bluebubbles': {
        'display_name': 'BlueBubbles (iMessage)',
        'icon': 'imessage',
        'color': '#34c759',
        'category': 'bridge',
        'auth_method': 'websocket_token',
        'setup_fields': [
            {'key': 'server_url', 'label': 'BlueBubbles Server URL', 'type': 'text',
             'help': 'Your BlueBubbles server address (requires macOS host).'},
            {'key': 'password', 'label': 'Server Password', 'type': 'password'},
        ],
        'capabilities': {
            'text': True, 'image': True, 'video': True, 'audio': True,
            'document': True, 'sticker': False, 'location': False, 'voice': False,
            'reactions': True, 'message_edit': False, 'message_delete': False,
            'streaming': False, 'groups': True, 'threads': False,
            'buttons': False, 'typing': True,
            'max_message_length': 20000,
        },
    },

    # ── Utility Adapters ───────────────────────────────────────
    'email': {
        'display_name': 'Email',
        'icon': 'email',
        'color': '#ea4335',
        'category': 'utility',
        'auth_method': 'credentials',
        'setup_fields': [
            {'key': 'imap_host', 'label': 'IMAP Server', 'type': 'text',
             'help': 'e.g. imap.gmail.com'},
            {'key': 'smtp_host', 'label': 'SMTP Server', 'type': 'text',
             'help': 'e.g. smtp.gmail.com'},
            {'key': 'email', 'label': 'Email Address', 'type': 'text'},
            {'key': 'password', 'label': 'Password / App Password', 'type': 'password',
             'help': 'Use an App Password for Gmail (2FA required).'},
        ],
        'capabilities': {
            'text': True, 'image': True, 'video': False, 'audio': False,
            'document': True, 'sticker': False, 'location': False, 'voice': False,
            'reactions': False, 'message_edit': False, 'message_delete': False,
            'streaming': False, 'groups': False, 'threads': True,
            'buttons': False, 'typing': False,
            'max_message_length': 100000,
        },
    },
    'voice': {
        'display_name': 'Voice (Twilio/Vonage)',
        'icon': 'voice',
        'color': '#f22f46',
        'category': 'utility',
        'auth_method': 'api_key',
        'setup_fields': [
            {'key': 'provider', 'label': 'Provider', 'type': 'select',
             'options': ['twilio', 'vonage'],
             'help': 'Choose your voice provider.'},
            {'key': 'account_sid', 'label': 'Account SID', 'type': 'text'},
            {'key': 'auth_token', 'label': 'Auth Token', 'type': 'password'},
            {'key': 'phone_number', 'label': 'Phone Number', 'type': 'text'},
        ],
        'capabilities': {
            'text': False, 'image': False, 'video': False, 'audio': True,
            'document': False, 'sticker': False, 'location': False, 'voice': True,
            'reactions': False, 'message_edit': False, 'message_delete': False,
            'streaming': False, 'groups': False, 'threads': False,
            'buttons': False, 'typing': False,
            'max_message_length': 0,
        },
    },
    'hardware': {
        'display_name': 'Hardware (GPIO/ROS)',
        'icon': 'hardware',
        'color': '#607d8b',
        'category': 'utility',
        'auth_method': 'credentials',
        'setup_fields': [
            {'key': 'interface', 'label': 'Interface Type', 'type': 'select',
             'options': ['gpio', 'ros', 'serial'],
             'help': 'Choose the hardware interface.'},
            {'key': 'port', 'label': 'Port / Topic', 'type': 'text'},
        ],
        'capabilities': {
            'text': True, 'image': False, 'video': False, 'audio': False,
            'document': False, 'sticker': False, 'location': False, 'voice': False,
            'reactions': False, 'message_edit': False, 'message_delete': False,
            'streaming': False, 'groups': False, 'threads': False,
            'buttons': False, 'typing': False,
            'max_message_length': 1024,
        },
    },
}


def get_channel_metadata(channel_type: str):
    """Get metadata for a single channel, or None."""
    return CHANNEL_CATALOG.get(channel_type)


def list_all_channels():
    """Return the full catalog dict."""
    return CHANNEL_CATALOG


def get_channels_by_category(category: str):
    """Filter channels by category (core, enterprise, social, decentralized, bridge, utility)."""
    return {k: v for k, v in CHANNEL_CATALOG.items() if v.get('category') == category}


def get_channels_by_auth_method(method: str):
    """Filter channels by auth method."""
    return {k: v for k, v in CHANNEL_CATALOG.items() if v.get('auth_method') == method}


def is_oauth_capable(channel_type: str) -> bool:
    """True iff the channel's metadata declares an OAuth authorize URL."""
    meta = CHANNEL_CATALOG.get(channel_type)
    return bool(meta and meta.get('oauth_authorize_url'))


def is_oauth_configured(channel_type: str, env_lookup=None) -> bool:
    """True iff the channel is OAuth-capable AND the operator has set
    HARTOS_OAUTH_CLIENT_<TYPE> + HARTOS_OAUTH_SECRET_<TYPE> env vars.

    When False, Connect_Channel falls back to the paste-token form
    so legacy operators with pre-registered tokens stay unaffected.
    """
    if not is_oauth_capable(channel_type):
        return False
    if env_lookup is None:
        import os
        env_lookup = os.environ.get
    upper = channel_type.upper()
    return bool(
        env_lookup(f'HARTOS_OAUTH_CLIENT_{upper}')
        and env_lookup(f'HARTOS_OAUTH_SECRET_{upper}')
    )
