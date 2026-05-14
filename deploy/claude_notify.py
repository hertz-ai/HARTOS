#!/usr/bin/env python3
"""Claude → Nunba notification bridge.

This is the orchestrator's voice channel.  Instead of pushing through
the host terminal (PushNotification) or surfacing in the chat
transcript only, this helper writes a Notification row into the live
Nunba DB.  HARTOS NotificationService.create installs an `after_commit`
hook that pushes the notif via SSE + WAMP, so the message reaches
EVERY device the user is logged into (desktop, phone, browser tab) on
next session tick.  Multi-device by design — exactly what the user
asked for.

Usage::

    python deploy/claude_notify.py "agents are blocked on Twitter API key — add TWITTER_BEARER_TOKEN to .env"

Or, to target a specific user_id explicitly::

    NUNBA_NOTIFY_USER_ID=<uuid> python deploy/claude_notify.py "..."

If user_id is omitted, the helper finds the most recently-active
non-system user (the operator's logged-in account, NOT the
`hevolve_system_agent` bootstrap user) and routes the notification
there.

Auth-source identity: notifications are tagged `source_user_id` =
`claude_orchestrator` so the user can filter / mute / inspect what the
orchestrator agent has been saying separately from human messages.
"""

from __future__ import annotations
import os
import sys

# Bootstrap import path
_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO not in sys.path:
    sys.path.insert(0, _REPO)


CLAUDE_ORCHESTRATOR_ID = 'claude_orchestrator'
# Free-form message length cap.  The notif feed renders these inline
# so anything beyond a paragraph just gets truncated by the UI anyway.
MAX_LEN = 500


def _resolve_user_id() -> str:
    """Find the operator's user_id.  Explicit env > most-recent
    non-system user > raises."""
    explicit = os.environ.get('NUNBA_NOTIFY_USER_ID', '').strip()
    if explicit:
        return explicit

    from integrations.social.models import db_session, User
    # Most recently active human user.  Skip the bootstrap system agent
    # and any obvious daemon principals so the message lands on the
    # actual human operator.
    with db_session() as db:
        u = (
            db.query(User)
            .filter(User.user_type != 'agent')
            .filter(~User.username.in_([
                'hevolve_system_agent',
                'system_bootstrap',
                'guest',
            ]))
            .order_by(User.last_active_at.desc().nullslast())
            .first()
        )
        if u is None:
            # Fall back to any human user if last_active_at is unpopulated
            u = db.query(User).filter(User.user_type != 'agent').first()
        if u is None:
            raise SystemExit("No human user found in DB — sign in to Nunba first.")
        return str(u.id)


def notify(message: str, kind: str = 'orchestrator_message') -> str:
    """Push a notification to the operator's Nunba feed.  Returns the
    new notification id."""
    if not message:
        raise ValueError("message is required")
    message = message[:MAX_LEN]

    user_id = _resolve_user_id()

    from integrations.social.models import db_session
    from integrations.social.services import NotificationService

    with db_session() as db:
        notif = NotificationService.create(
            db,
            user_id=user_id,
            type=kind,
            source_user_id=CLAUDE_ORCHESTRATOR_ID,
            target_type='orchestrator',
            target_id=None,
            message=message,
        )
        db.commit()
        # Best-effort live push.  NotificationService's after_commit
        # hook already calls integrations.social.realtime.on_notification
        # — repeat-call here is harmless (it's idempotent on msg_id at
        # the SSE bus and dedup-guarded on the client per
        # realtimeService.js _isDuplicate).
        try:
            from integrations.social.realtime import on_notification
            on_notification(user_id, notif.to_dict())
        except Exception:
            pass
        return str(notif.id)


def main() -> int:
    if len(sys.argv) < 2:
        print("Usage: python deploy/claude_notify.py \"<message>\"", file=sys.stderr)
        return 2
    msg = ' '.join(sys.argv[1:])
    nid = notify(msg)
    print(f"notified: {nid}")
    return 0


if __name__ == '__main__':
    sys.exit(main())
