# Channel Adapters

Over 30 channel adapters providing a unified interface for agent communication across platforms.

## Supported Channels

| Category | Channels |
|----------|----------|
| **Chat platforms** | Discord, Telegram, Slack, Matrix, WhatsApp, Signal, IRC |
| **Communication** | Email (SMTP/IMAP), SMS (Twilio), Voice (WebRTC) |
| **Social media** | Twitter/X, Reddit, Mastodon, Bluesky |
| **Collaboration** | Microsoft Teams, Google Chat, Mattermost, Rocket.Chat |
| **Web** | REST webhook, WebSocket, Server-Sent Events |
| **Other** | Custom adapters via the adapter interface |

## Adapter Interface

Each adapter implements a standard send/receive interface:

- **send(message)** -- Deliver a message to the channel.
- **receive()** -- Poll or listen for incoming messages from the channel.
- **on_message(callback)** -- Register an event handler for real-time message streams.

This uniform interface allows the agent engine to interact with any channel without channel-specific logic in the core.

## Channel Admin API

```
/api/admin/channels        -- List configured channels
/api/admin/channels/{id}   -- Get/update/delete a channel configuration
```

Administrators can enable, disable, and configure channels through the admin API without restarting the server.

## Source Files

- `integrations/channels/` (adapter implementations)
- `hart_intelligence_entry.py` (admin routes)
