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

## Goal Mailboxes and Held Emails

Central sends each business goal's outreach email from that goal's own company
mailbox (the goal's config `sender_email`), and from no other.  A mailbox is an
account of the email channel, connected once through the channel setup form; it
sends goals' email and reads its inbox only for prospects' replies (it never
chats with the agent or answers mail).  A goal may send from a mailbox only when
its creator is the account that connected it, or this node's owner.

Every goal email is verified before it leaves:

- A goal's follow-up sequences are created *held* until the owner verifies the
  goal's copy; the follow-up checker sends only *active* sequences.
- Any other goal email (words an agent wrote this turn, a reply, the journey's
  own copy) is kept as a held draft and sent only when the owner approves it,
  exactly as written.  The agent is told it goes out once approved.

```
GET  /api/admin/outreach/held?goal_id=&status=held   -- Held drafts (subject and body, never the recipient's address)
POST /api/admin/outreach/held/{draft_id}/approve      -- Send one held draft as written
POST /api/admin/outreach/held/{draft_id}/reject       -- Turn it down; body {"reason": "..."} is required
POST /api/admin/outreach/goals/{goal_id}/copy-verified -- Verify a goal's copy: its held sequences go active
```

These routes sit behind the same admin gate as the channel routes (on central,
the operator only).

## Source Files

- `integrations/channels/` (adapter implementations)
- `hart_intelligence_entry.py` (admin routes)
