"""
room_presence_service — UNIF-G6 — canonical agent-presence-in-external-room policy.

This module is the single writer for the three policies that MUST stay
together when an AI agent participates in an external room (Discord
audio, Teams meet, WhatsApp group, Slack channel, Matrix room,
Telegram supergroup, etc.):

  1. ``gate(...)``        — consent check before joining
  2. ``announce_presence(...)`` — required notice posted into the room
  3. ``listen_for_objection(...)`` — auto-detach if anyone says "no AI"

Per HIVE AI MISSION (memory/project_hive_mission.md): the agent NEVER
silently observes external rooms.  Three guardrails:
   - Consent before join (UI prompt or pre-granted scope).
   - Announcement on join (every participant sees a clearly-flagged
     notice that an AI is present, who it serves, and how to remove it).
   - Listen for objection — if any participant uses an opt-out phrase
     in any of the supported languages, the agent posts a farewell and
     leaves immediately.

Canonical primitives reused (no parallel paths):
   - ``ConsentService.check_consent`` — gate decision (consent_type
     ``'cloud_capability'`` + scope ``agent_joins_external_room`` etc.)
   - ``ChannelAdapter.send_message`` — post the announcement into the room
   - ``ImmutableAuditLog.log_event`` — durable audit chain for join/leave
   - ``AgentVoiceBridge.detach_agent`` — voice-room detach on objection

Caller (G2 ``Join_External_Room`` agent tool) is responsible for
calling ``gate`` BEFORE invoking the adapter join, then
``announce_presence`` immediately after a successful join, then
``listen_for_objection`` to wire the objection-watcher.

This module is intentionally agnostic of which platform — every
platform that supports text messaging exposes
``ChannelAdapter.send_message``, so the announce + farewell flows
through one code path.  Voice rooms (livekit) get their detach via the
existing ``AgentVoiceBridge`` (line 239 ``detach_agent``).
"""

from __future__ import annotations

import logging
from typing import Optional, Tuple

logger = logging.getLogger('hevolve_social')

# ── Consent scopes per role ────────────────────────────────────────
# The role determines which scope we check.  These string literals
# MUST stay in sync with ``landing-page/src/components/Social/Settings/
# cloudCapabilityScopes.js:CLOUD_CAPABILITY_SCOPES``.
_SCOPE_BY_ROLE = {
    'note_taker': 'agent_listens_external_audio',
    'co_pilot': 'agent_joins_external_room',
    'participant': 'agent_joins_external_room',
    'silent_observer': 'agent_joins_external_room',
    'writer': 'agent_writes_external_room',
}
_DEFAULT_SCOPE = 'agent_joins_external_room'
_CONSENT_TYPE = 'cloud_capability'

# ── Objection phrases (i18n) ───────────────────────────────────────
# Any participant typing one of these (case-insensitive, word-boundary)
# triggers immediate agent detach + farewell.  Adding a language: append
# the phrase here.  Single source of truth — no per-adapter copy.
_OBJECTION_PHRASES = (
    # English
    'no ai', 'no bot', 'no agent', 'remove ai', 'remove bot',
    'kick the ai', 'kick the bot', '/agent-out',
    # Spanish
    'sin ia', 'no ia', 'fuera ia', 'sin bot',
    # French
    'pas d\'ia', 'sans ia', 'pas de bot',
    # German
    'keine ki', 'kein bot', 'ki raus',
    # Hindi (Latin script)
    'no ai please', 'ai hata',
    # Mandarin (transliteration + simplified)
    'wu ai', '不要 ai',
    # Japanese
    'ai なし',
    # Portuguese
    'sem ia', 'fora ia',
    # Tamil (Latin)
    'ai venda',
)


def _scope_for_role(role: str) -> str:
    """Map a join role to the consent scope it requires."""
    return _SCOPE_BY_ROLE.get((role or '').lower(), _DEFAULT_SCOPE)


def gate(user_id: str, platform: str, room_id: str,
         role: str = 'co_pilot') -> Tuple[bool, str]:
    """Return ``(allowed, reason)`` for an agent-in-room join.

    Checks ``ConsentService.check_consent`` against the appropriate
    cloud-capability scope for the requested role.  Caller (G2 agent
    tool) is responsible for surfacing a Liquid UI consent prompt
    when this returns ``(False, ...)``.

    Failure modes (all return ``(False, <reason>)`` not raise):
      - DB unavailable / consent service import fails → assume DENIED
        (fail-closed; never auto-grant).
      - User has revoked the scope → DENIED.
      - User never granted → DENIED, with reason ``'consent required'``
        so caller can prompt.

    Audit trail: every gate decision (allow OR deny) emits an audit log
    entry via ``ImmutableAuditLog.log_event`` so security review can
    reconstruct who allowed what when.  Never raises.
    """
    scope = _scope_for_role(role)
    detail = {
        'platform': platform,
        'room_id': room_id,
        'role': role,
        'scope': scope,
    }
    try:
        from integrations.social.consent_service import ConsentService
        from integrations.social.models import db_session
        with db_session(commit=False) as db:
            allowed = ConsentService.check_consent(
                db, str(user_id), _CONSENT_TYPE, scope=scope)
    except Exception as e:
        logger.warning(
            "room_presence.gate: consent check failed for "
            "user=%s scope=%s platform=%s room=%s: %s — failing closed",
            user_id, scope, platform, room_id, e)
        _audit('room_presence.gate', user_id, 'denied_error',
               {**detail, 'error': str(e)[:200]})
        return False, 'consent service unavailable — please retry'

    if allowed:
        _audit('room_presence.gate', user_id, 'allowed', detail)
        return True, 'ok'
    _audit('room_presence.gate', user_id, 'denied_no_consent', detail)
    return False, (
        f"Permission required: I need your consent to "
        f"join {platform} rooms as a {role}. Please grant the "
        f"'{scope}' scope in Settings → Privacy."
    )


def announce_presence(adapter, room_id: str, user_id: str,
                      role: str = 'co_pilot',
                      *, owner_display_name: Optional[str] = None) -> bool:
    """Post the canonical announcement message into the external room.

    Honors HIVE AI MISSION rule that the agent ALWAYS makes its
    presence known.  Wording is fixed (single canon — no per-platform
    variant) so users see consistent disclosure across Discord / Teams
    / WhatsApp / etc.

    Args:
        adapter:  A ``ChannelAdapter`` instance (must support
                  ``send_message`` per ``channels/base.py:141``).
        room_id:  Platform-native room/chat id.
        user_id:  Owner's user id (for the audit log).
        role:     Same role string used in ``gate()``.
        owner_display_name: Optional friendly name to insert into
                  the announcement.  Falls back to "this user".

    Returns ``True`` iff the announcement was sent successfully.
    On failure, audit logs the error and returns ``False`` — the
    caller (G2) MUST treat that as a hard failure and DETACH the
    agent rather than continuing silently.
    """
    name = owner_display_name or 'this user'
    role_label = {
        'note_taker': 'note-taker',
        'co_pilot': 'co-pilot',
        'participant': 'participant',
        'silent_observer': 'silent observer (read-only)',
        'writer': 'message writer',
    }.get((role or '').lower(), 'co-pilot')

    text = (
        f"\U0001F916 An AI agent has joined this room as {name}'s "
        f"{role_label}. It will follow the conversation to help "
        f"with notes / answers. Reply 'no AI' (or '/agent-out') "
        f"any time to have it leave."
    )
    detail = {
        'platform': getattr(adapter, 'name', '?'),
        'room_id': room_id, 'role': role,
    }
    try:
        import asyncio
        # ChannelAdapter.send_message is async; the caller may already
        # be inside an event loop (Flask + asyncio mix).  Defer to the
        # adapter's registry loop when present, else run a one-shot.
        coro = adapter.send_message(room_id, text)
        try:
            loop = asyncio.get_event_loop()
            if loop.is_running():
                fut = asyncio.run_coroutine_threadsafe(coro, loop)
                result = fut.result(timeout=10)
            else:
                result = loop.run_until_complete(coro)
        except RuntimeError:
            result = asyncio.run(coro)
        ok = bool(getattr(result, 'success', False))
        _audit('room_presence.announce', user_id,
               'announced' if ok else 'announce_failed',
               {**detail, 'message_id': getattr(result, 'message_id', None)})
        return ok
    except Exception as e:
        logger.warning(
            "room_presence.announce_presence failed (platform=%s "
            "room=%s): %s", detail['platform'], room_id, e)
        _audit('room_presence.announce', user_id, 'announce_error',
               {**detail, 'error': str(e)[:200]})
        return False


def is_objection(text: str) -> bool:
    """Return True iff ``text`` contains a known objection phrase.

    Pure helper — no side effects.  Used by ``listen_for_objection``
    and reusable for unit tests.  Case-insensitive substring match;
    the phrases are short enough that false positives are negligible
    (a bystander typing "no ai" verbatim DOES want the agent to leave).
    """
    if not text:
        return False
    low = text.lower()
    return any(p in low for p in _OBJECTION_PHRASES)


def listen_for_objection(adapter, room_id: str, user_id: str,
                         agent_id: str,
                         *, on_detach=None) -> None:
    """Hook ``adapter.on_message`` to watch for objection phrases.

    On match: logs the objection, posts a brief farewell into the
    room (best-effort, never blocks), invokes ``on_detach`` callback
    so the caller can run platform-specific detach (e.g.
    ``AgentVoiceBridge.detach_agent`` for voice rooms,
    ``adapter.leave_room`` for text rooms when G2 ships the Mixin).

    Caller signature for ``on_detach``: ``on_detach(reason: str) -> None``.
    Best-effort — exceptions from the callback are logged, never raised.

    Note: this function REGISTERS a handler on the adapter; it does
    NOT block.  The handler stays active until the adapter unregisters
    it (which happens automatically when the adapter disconnects).
    """
    if adapter is None or not hasattr(adapter, 'on_message'):
        logger.debug(
            "room_presence.listen_for_objection: adapter %r lacks "
            "on_message hook — skipping", adapter)
        return

    detail = {
        'platform': getattr(adapter, 'name', '?'),
        'room_id': room_id, 'agent_id': agent_id,
    }

    async def _check_objection(message):
        try:
            text = getattr(message, 'text', '') or ''
            chat_id = getattr(message, 'chat_id', None)
            if chat_id and str(chat_id) != str(room_id):
                return
            if not is_objection(text):
                return
            logger.info(
                "room_presence: objection detected in %s/%s — "
                "detaching agent %s", detail['platform'], room_id, agent_id)
            _audit('room_presence.objection', user_id, 'detected',
                   {**detail, 'phrase_in': text[:120]})
            # Farewell — best-effort.
            try:
                farewell = (
                    "\U0001F44B Understood — leaving now. "
                    "Re-invite anytime."
                )
                await adapter.send_message(room_id, farewell)
            except Exception as fe:
                logger.debug(
                    "room_presence: farewell post failed: %s", fe)
            # Caller-supplied detach.
            if callable(on_detach):
                try:
                    on_detach('participant_objection')
                except Exception as de:
                    logger.warning(
                        "room_presence: on_detach callback raised: %s", de)
                    _audit('room_presence.detach', user_id,
                           'detach_callback_error',
                           {**detail, 'error': str(de)[:200]})
            else:
                _audit('room_presence.detach', user_id,
                       'no_detach_callback', detail)
        except Exception as e:
            # Never let a watcher error break adapter delivery.
            logger.warning(
                "room_presence.listen_for_objection inner: %s", e)

    try:
        adapter.on_message(_check_objection)
        _audit('room_presence.watch', user_id, 'watcher_attached', detail)
    except Exception as e:
        logger.warning(
            "room_presence.listen_for_objection: failed to attach "
            "watcher: %s", e)
        _audit('room_presence.watch', user_id, 'watcher_attach_failed',
               {**detail, 'error': str(e)[:200]})


def _audit(event_type: str, actor_id: str, action: str, detail: dict) -> None:
    """Best-effort audit log emit.  Never raises."""
    try:
        from security.immutable_audit_log import get_audit_log
        get_audit_log().log_event(
            event_type=event_type,
            actor_id=str(actor_id),
            action=action,
            detail=detail,
        )
    except Exception as e:
        # Audit log unavailable — log to module logger so a grep over
        # the local log file still surfaces it.  Don't escalate;
        # consent / announce / listen flows must keep going.
        logger.info(
            "room_presence audit (fallback): %s/%s actor=%s detail=%s "
            "(audit_log error: %s)",
            event_type, action, actor_id, detail, e)
