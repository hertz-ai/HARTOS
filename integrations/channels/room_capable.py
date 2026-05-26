"""
room_capable — UNIF-G2 — opt-in mixin marking adapters that support
room/channel join semantics.

Why a Mixin and not a base-class extension:
   Not every channel concept supports rooms (SMS / email / iMessage 1:1
   / signal 1:1 don't).  Adding ``join_room`` to ``ChannelAdapter`` would
   force those adapters to raise ``UnsupportedRoomError`` from a method
   they have no business implementing.  A separate Mixin keeps
   ``ChannelAdapter`` minimal (single-responsibility) and lets G2's
   ``Join_External_Room`` agent tool detect support via
   ``isinstance(adapter, RoomCapableAdapter)``.

Adapters that should subclass this Mixin (alongside ``ChannelAdapter``):
   - DiscordAdapter (text channels + voice rooms via livekit bridge)
   - SlackAdapter (channels)
   - MatrixAdapter (rooms)
   - TelegramAdapter (super-groups + channels)
   - TeamsAdapter (channels + meetings)
   - WhatsAppAdapter (groups)

Adapters that should NOT subclass this Mixin:
   - SignalAdapter (1:1 only at the protocol level we use today)
   - IMessageAdapter (1:1 only via current bridge)
   - WebAdapter (per-user web chat, no rooms)
   - EmailAdapter (threads, not rooms)
   - VoiceAdapter (per-call, not rooms)
   - SMSAdapter (1:1)

Voice rooms (Discord audio, Teams meet, Zoom-style) route through the
canonical ``LiveKitService.issue_token`` + ``AgentVoiceBridge.attach_agent``
pair (see ``integrations/social/agent_voice_bridge.py``).  The adapter's
``join_room`` is for TEXT room-membership only; voice participation is
livekit-side.

Per-platform implementation notes are deferred to each adapter — this
module only declares the contract.  The contract is a SUBSET of
``ChannelAdapter`` so a class can ``extend`` either order:
   ``class DiscordAdapter(ChannelAdapter, RoomCapableAdapter): ...``

Per HIVE AI MISSION: G2's ``Join_External_Room`` tool ALWAYS calls
``room_presence_service.gate(...)`` BEFORE ``join_room(...)`` and
``announce_presence(...)`` IMMEDIATELY AFTER a successful join.
``join_room`` itself is the platform-specific transport — never the
policy point.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional


class UnsupportedRoomError(Exception):
    """Raised when a caller tries to join a room on an adapter that does
    not support rooms (e.g. SMS).  Caught by the Join_External_Room tool
    so it can return a graceful "this platform doesn't support rooms"
    message instead of a stack trace."""


class RoomCapableAdapter:
    """Mixin marker for channel adapters that support room/channel join
    semantics.

    Subclasses MUST override ``join_room``, ``leave_room``, and
    ``list_room_members`` (per platform).  All three are async because
    every existing channel adapter uses asyncio and ``ChannelAdapter``
    is async-first.

    Roles passed to ``join_room`` are the SAME role strings used by
    ``room_presence_service`` so consent + adapter speak the same
    vocabulary:
       - ``co_pilot`` — full read+write+react participation
       - ``participant`` — full read+write
       - ``note_taker`` — read+react only (no write)
       - ``silent_observer`` — read only
       - ``writer`` — write-on-behalf only (uncommon — usually paired
         with another role)
    """

    async def join_room(self, room_id: str,
                        role: str = 'participant') -> bool:
        """Join the named room/channel as the agent's presence.

        Args:
            room_id: Platform-native room id (e.g. Discord channel id,
                     Slack channel id, Matrix room id, Telegram chat id).
            role:    One of the role strings above.  Adapters MAY use
                     this to set platform-side permissions (e.g. mute
                     for ``silent_observer``).

        Returns ``True`` iff the join succeeded.  Adapters that fail
        for transient reasons (rate limit, network) should return
        ``False`` rather than raise — the Join_External_Room tool
        handles the retry / fallback on the caller side.

        Adapters MUST raise ``UnsupportedRoomError`` if the protocol
        cannot represent room membership (e.g. attempting to join a
        DM "room").
        """
        raise NotImplementedError(
            f"{type(self).__name__} subclasses RoomCapableAdapter "
            f"but does not implement join_room")

    async def leave_room(self, room_id: str) -> bool:
        """Leave the named room/channel.  Counterpart to ``join_room``."""
        raise NotImplementedError(
            f"{type(self).__name__} subclasses RoomCapableAdapter "
            f"but does not implement leave_room")

    async def list_room_members(self, room_id: str) -> List[Dict[str, Any]]:
        """Return a list of member dicts for the room.

        Each dict at minimum contains ``id`` and ``display_name``.
        Adapters MAY include additional platform-specific fields.
        Optional — implementations may return ``[]`` if listing is not
        supported or rate-limited.
        """
        return []


def is_room_capable(adapter: Any) -> bool:
    """Return True iff ``adapter`` supports room operations.

    Convenience helper used by ``Join_External_Room`` so the agent tool
    can return a clean "platform support pending" message for adapters
    that haven't been wired with the Mixin yet, without paying the cost
    of a try/except around the actual join.
    """
    return isinstance(adapter, RoomCapableAdapter)
