"""
Channel Extensions - Phases 7 & 8

Additional messaging channel adapters for extended platform support.
These adapters follow the same patterns as core adapters (Telegram, Discord, Slack)
but target additional platforms for broader integration capabilities.

Available Extensions:

Phase 7:
- Matrix: End-to-end encrypted messaging via Matrix protocol
- Teams: Microsoft Teams integration via Bot Framework
- LINE: LINE Messaging API for LINE platform
- Mattermost: Open-source Slack alternative with WebSocket and REST API
- Nextcloud Talk: Nextcloud's communication platform with file sharing

Phase 8:
- Twitch: Twitch chat and whispers
- Zalo: Vietnamese messaging platform OA API
- Nostr: Decentralized social protocol
- BlueBubbles: iMessage bridge for cross-platform
- Voice: Voice calls via Twilio/Vonage
- RocketChat: Open-source team chat with REST + Realtime API
- WeChat: WeChat Official Account API
- Viber: Viber Bot API
- Messenger: Facebook Messenger via Meta Graph API
- Instagram: Instagram Direct Messages
- Twitter: Twitter/X DMs and mentions
- Email: IMAP/SMTP email integration

Docker Support:
    All adapters are designed to work in containerized environments.
    Configuration can be passed via environment variables.
"""

from typing import TYPE_CHECKING

# Lazy imports to avoid loading dependencies that may not be installed
__all__ = [
    # Phase 7 - Matrix
    "MatrixAdapter", "MatrixConfig", "MatrixRoom", "ThreadInfo", "create_matrix_adapter",
    # Phase 7 - Teams
    "TeamsAdapter", "TeamsConfig", "AdaptiveCard", "ConversationRef", "create_teams_adapter",
    # Phase 7 - LINE
    "LINEAdapter", "LINEConfig", "FlexBubble", "QuickReplyItem", "create_line_adapter",
    # Phase 7 - Mattermost
    "MattermostAdapter", "MattermostConfig", "MattermostChannel", "MattermostUser",
    "InteractiveMessage", "SlashCommand", "create_mattermost_adapter",
    # Phase 7 - Nextcloud
    "NextcloudAdapter", "NextcloudConfig", "NextcloudConversation", "NextcloudParticipant",
    "NextcloudMessage", "ConversationType", "ParticipantType", "RichObjectParameter", "create_nextcloud_adapter",
    # Phase 8 - Twitch
    "TwitchAdapter", "TwitchConfig", "create_twitch_adapter",
    # Phase 8 - Zalo
    "ZaloAdapter", "ZaloConfig", "create_zalo_adapter",
    # Phase 8 - Nostr
    "NostrAdapter", "NostrConfig", "create_nostr_adapter",
    # Phase 8 - BlueBubbles
    "BlueBubblesAdapter", "BlueBubblesConfig", "create_bluebubbles_adapter",
    # Phase 8 - Voice
    "VoiceAdapter", "VoiceConfig", "create_voice_adapter",
    # Phase 8 - RocketChat
    "RocketChatAdapter", "RocketChatConfig", "create_rocketchat_adapter",
    # Phase 8 - WeChat
    "WeChatAdapter", "WeChatConfig", "create_wechat_adapter",
    # Phase 8 - Viber
    "ViberAdapter", "ViberConfig", "create_viber_adapter",
    # Phase 8 - Messenger
    "MessengerAdapter", "MessengerConfig", "create_messenger_adapter",
    # Phase 8 - Instagram
    "InstagramAdapter", "InstagramConfig", "create_instagram_adapter",
    # Phase 8 - Twitter
    "TwitterAdapter", "TwitterConfig", "create_twitter_adapter",
    # Phase 8 - Email
    "EmailAdapter", "EmailConfig", "create_email_adapter",
    # Phase 8 - Tlon (Urbit)
    "TlonAdapter", "TlonConfig", "create_tlon_adapter",
    # Phase 8 - Open Prose
    "OpenProseAdapter", "OpenProseConfig", "create_openprose_adapter",
    # Phase 8 - Telegram User
    "TelegramUserAdapter", "TelegramUserConfig", "create_telegram_user_adapter",
    # Phase 8 - Discord User
    "DiscordUserAdapter", "DiscordUserConfig", "create_discord_user_adapter",
    # Phase 8 - Zalo User
    "ZaloUserAdapter", "ZaloUserConfig", "create_zalo_user_adapter",
]


def __getattr__(name: str):
    """Lazy import of adapters to avoid loading unused dependencies."""

    # Matrix imports
    if name in ("MatrixAdapter", "MatrixConfig", "MatrixRoom", "ThreadInfo", "create_matrix_adapter"):
        from .matrix_adapter import (
            MatrixAdapter,
            MatrixConfig,
            MatrixRoom,
            ThreadInfo,
            create_matrix_adapter,
        )
        return locals()[name]

    # Teams imports
    if name in ("TeamsAdapter", "TeamsConfig", "AdaptiveCard", "ConversationRef", "create_teams_adapter"):
        from .teams_adapter import (
            TeamsAdapter,
            TeamsConfig,
            AdaptiveCard,
            ConversationRef,
            create_teams_adapter,
        )
        return locals()[name]

    # LINE imports
    if name in ("LINEAdapter", "LINEConfig", "FlexBubble", "QuickReplyItem", "create_line_adapter"):
        from .line_adapter import (
            LINEAdapter,
            LINEConfig,
            FlexBubble,
            QuickReplyItem,
            create_line_adapter,
        )
        return locals()[name]

    # Mattermost imports
    if name in ("MattermostAdapter", "MattermostConfig", "MattermostChannel", "MattermostUser",
                "InteractiveMessage", "SlashCommand", "create_mattermost_adapter"):
        from .mattermost_adapter import (
            MattermostAdapter,
            MattermostConfig,
            MattermostChannel,
            MattermostUser,
            InteractiveMessage,
            SlashCommand,
            create_mattermost_adapter,
        )
        return locals()[name]

    # Nextcloud imports
    if name in ("NextcloudAdapter", "NextcloudConfig", "NextcloudConversation", "NextcloudParticipant",
                "NextcloudMessage", "ConversationType", "ParticipantType", "RichObjectParameter",
                "create_nextcloud_adapter"):
        from .nextcloud_adapter import (
            NextcloudAdapter, NextcloudConfig, NextcloudConversation, NextcloudParticipant,
            NextcloudMessage, ConversationType, ParticipantType, RichObjectParameter, create_nextcloud_adapter,
        )
        return locals()[name]

    # Phase 8 - Twitch
    if name in ("TwitchAdapter", "TwitchConfig", "create_twitch_adapter"):
        from .twitch_adapter import TwitchAdapter, TwitchConfig, create_twitch_adapter
        return locals()[name]

    # Phase 8 - Zalo
    if name in ("ZaloAdapter", "ZaloConfig", "create_zalo_adapter"):
        from .zalo_adapter import ZaloAdapter, ZaloConfig, create_zalo_adapter
        return locals()[name]

    # Phase 8 - Nostr
    if name in ("NostrAdapter", "NostrConfig", "create_nostr_adapter"):
        from .nostr_adapter import NostrAdapter, NostrConfig, create_nostr_adapter
        return locals()[name]

    # Phase 8 - BlueBubbles
    if name in ("BlueBubblesAdapter", "BlueBubblesConfig", "create_bluebubbles_adapter"):
        from .bluebubbles_adapter import BlueBubblesAdapter, BlueBubblesConfig, create_bluebubbles_adapter
        return locals()[name]

    # Phase 8 - Voice
    if name in ("VoiceAdapter", "VoiceConfig", "create_voice_adapter"):
        from .voice_adapter import VoiceAdapter, VoiceConfig, create_voice_adapter
        return locals()[name]

    # Phase 8 - RocketChat
    if name in ("RocketChatAdapter", "RocketChatConfig", "create_rocketchat_adapter"):
        from .rocketchat_adapter import RocketChatAdapter, RocketChatConfig, create_rocketchat_adapter
        return locals()[name]

    # Phase 8 - WeChat
    if name in ("WeChatAdapter", "WeChatConfig", "create_wechat_adapter"):
        from .wechat_adapter import WeChatAdapter, WeChatConfig, create_wechat_adapter
        return locals()[name]

    # Phase 8 - Viber
    if name in ("ViberAdapter", "ViberConfig", "create_viber_adapter"):
        from .viber_adapter import ViberAdapter, ViberConfig, create_viber_adapter
        return locals()[name]

    # Phase 8 - Messenger
    if name in ("MessengerAdapter", "MessengerConfig", "create_messenger_adapter"):
        from .messenger_adapter import MessengerAdapter, MessengerConfig, create_messenger_adapter
        return locals()[name]

    # Phase 8 - Instagram
    if name in ("InstagramAdapter", "InstagramConfig", "create_instagram_adapter"):
        from .instagram_adapter import InstagramAdapter, InstagramConfig, create_instagram_adapter
        return locals()[name]

    # Phase 8 - Twitter
    if name in ("TwitterAdapter", "TwitterConfig", "create_twitter_adapter"):
        from .twitter_adapter import TwitterAdapter, TwitterConfig, create_twitter_adapter
        return locals()[name]

    # Phase 8 - Email
    if name in ("EmailAdapter", "EmailConfig", "create_email_adapter"):
        from .email_adapter import EmailAdapter, EmailConfig, create_email_adapter
        return locals()[name]

    # Phase 8 - Tlon (Urbit)
    if name in ("TlonAdapter", "TlonConfig", "create_tlon_adapter"):
        from .tlon_adapter import TlonAdapter, TlonConfig, create_tlon_adapter
        return locals()[name]

    # Phase 8 - Open Prose
    if name in ("OpenProseAdapter", "OpenProseConfig", "create_openprose_adapter"):
        from .openprose_adapter import OpenProseAdapter, OpenProseConfig, create_openprose_adapter
        return locals()[name]

    # Phase 8 - Telegram User
    if name in ("TelegramUserAdapter", "TelegramUserConfig", "create_telegram_user_adapter"):
        from .telegram_user_adapter import TelegramUserAdapter, TelegramUserConfig, create_telegram_user_adapter
        return locals()[name]

    # Phase 8 - Discord User
    if name in ("DiscordUserAdapter", "DiscordUserConfig", "create_discord_user_adapter"):
        from .discord_user_adapter import DiscordUserAdapter, DiscordUserConfig, create_discord_user_adapter
        return locals()[name]

    # Phase 8 - Zalo User
    if name in ("ZaloUserAdapter", "ZaloUserConfig", "create_zalo_user_adapter"):
        from .zalo_user_adapter import ZaloUserAdapter, ZaloUserConfig, create_zalo_user_adapter
        return locals()[name]

    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


# Type hints for IDE support
if TYPE_CHECKING:
    from .matrix_adapter import (
        MatrixAdapter,
        MatrixConfig,
        MatrixRoom,
        ThreadInfo,
        create_matrix_adapter,
    )
    from .teams_adapter import (
        TeamsAdapter,
        TeamsConfig,
        AdaptiveCard,
        ConversationRef,
        create_teams_adapter,
    )
    from .line_adapter import (
        LINEAdapter,
        LINEConfig,
        FlexBubble,
        QuickReplyItem,
        create_line_adapter,
    )
    from .mattermost_adapter import (
        MattermostAdapter,
        MattermostConfig,
        MattermostChannel,
        MattermostUser,
        InteractiveMessage,
        SlashCommand,
        create_mattermost_adapter,
    )
    from .nextcloud_adapter import (
        NextcloudAdapter,
        NextcloudConfig,
        NextcloudConversation,
        NextcloudParticipant,
        NextcloudMessage,
        ConversationType,
        ParticipantType,
        RichObjectParameter,
        create_nextcloud_adapter,
    )
    from .tlon_adapter import TlonAdapter, TlonConfig, create_tlon_adapter
    from .openprose_adapter import OpenProseAdapter, OpenProseConfig, create_openprose_adapter
    from .telegram_user_adapter import TelegramUserAdapter, TelegramUserConfig, create_telegram_user_adapter
    from .discord_user_adapter import DiscordUserAdapter, DiscordUserConfig, create_discord_user_adapter
    from .zalo_user_adapter import ZaloUserAdapter, ZaloUserConfig, create_zalo_user_adapter
