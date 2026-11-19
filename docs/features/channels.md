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
its creator is the account that connected it, or this node's owner.  A connect
counts only once it is proven: every setting is given and the mail server
accepts the IMAP and SMTP login (tried on a throwaway adapter).  A wrong or
missing password is refused with what to fix, and nothing is recorded: no
binding, no config, no adapter, and a running adapter of the same mailbox is
left as it was.  `POST /api/social/channels/bindings` never binds a mailbox: a
form naming one (top level, `metadata` or the wizards' `config`) goes to that
connect, and a body that names a mailbox this node holds only by its sender id
is refused, saying so.  Every reply that returns a binding
(`flask_integration.binding_for_client`) carries only its non-secret display
fields (address, servers, name): no password, token or sealed blob leaves the
server.

Every goal email is verified before it leaves:

- A follow-up sequence is created *held* unless every one of its steps is copy
  the owner already verified for that goal.  Verification is of the exact text:
  the goal's config keeps a content hash (subject and body, whitespace
  normalised) per verified step, so new or changed text is held again.  The
  follow-up checker sends only *active* sequences.
- Any other email (words an agent wrote this turn, a reply, the journey's own
  copy) is kept as a held draft and sent only when the owner approves it,
  exactly as written.  The agent is told it goes out once approved.  On a node
  that is not flat (central, regional) this covers an email for a prospect of no
  goal too, and a goal-less sequence is held (it has no goal to verify it for).
- A prospect has one pending first email: while it is held, the agent cannot
  write a second first email for that person (it is told why), and approving a
  first email never sends one to a person already emailed.  A prospect is
  'contacted' only when an email reaches them, by every path.

```
GET  /api/admin/outreach/held?goal_id=&status=held   -- Held drafts and the held sequences' steps (subject and body, never the recipient's address)
POST /api/admin/outreach/held/{draft_id}/approve      -- Send one held draft as written
POST /api/admin/outreach/held/{draft_id}/reject       -- Turn it down; body {"reason": "..."} is required
POST /api/admin/outreach/goals/{goal_id}/copy-verified -- Verify the copy held for a goal (optional body {"content_hashes": [...]} from the held list): the sequences whose steps are then all verified go active
```

These routes sit behind the same admin gate as the channel routes (on central,
the operator only).

## Source Files

- `integrations/channels/` (adapter implementations)
- `hart_intelligence_entry.py` (admin routes)
