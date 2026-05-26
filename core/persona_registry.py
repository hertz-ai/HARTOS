"""Per-(user, prompt) persona/role registry — single source of truth.

A single agent instance can be shared across multiple personas (student /
parent / teacher).  The Helper agent uses `send_message_to_roles(role,
message)` to deliver a message to a specific persona via crossbar.

This module hosts the TTLCache singletons that map `{user_id}_{prompt_id}`
to the persona/role state, plus the canonical broadcast routine the
`send_message_to_roles` tool registers across both flows.

Single-writer invariant:
  - These TTLCaches are declared ONLY here.
  - `reuse_recipe.py` and `create_recipe.py` import (not redeclare) them.
  - A drift-guard test enforces this.

Populated by:
  - `register_persona_for_session(user_id, prompt_id, persona_list)` —
    called from create_recipe.set_for_creating_actions (after the
    gather-requirements config is loaded) and from reuse_recipe's
    update_persona / create_agents_for_user paths.

#510 follow-up: this replaces the previous module-level declarations
in reuse_recipe.py:181, 185, 186 and the commented-out closure at
reuse_recipe.py:1203-1240.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from core.session_cache import TTLCache

logger = logging.getLogger(__name__)


# {f"{user_id}_{prompt_id}": [
#    {'agentInstanceID': str, 'user_id': Any, 'role': str, 'deviceID': str, ...},
#    ...
# ]}
agents_session: TTLCache = TTLCache(
    ttl_seconds=7200, max_size=500, name='persona_agents_session')

# {f"{user_id}_{prompt_id}": {user_id: role_name}}
agents_roles: TTLCache = TTLCache(
    ttl_seconds=7200, max_size=500, name='persona_agents_roles')

# {user_id: {prompt_id: chat_creator_user_id}} — cross-user joined chat
chat_joinees: TTLCache = TTLCache(
    ttl_seconds=7200, max_size=500, name='persona_chat_joinees')


# Canonical WAMP topic base for multi-persona broadcast.  Per-user
# suffix matches HARTOS's existing convention used by chat/action/
# vision/analogy/social/fleet/game/community topics — so the WAMP
# router's per-user-topic ACL (#246) can gate this topic too, and
# multi-node hive deployments won't leak one tenant's broadcast to
# another via a shared subscription.
MULTICHAT_TOPIC_BASE = 'com.hertzai.hevolve.agent.multichat'


def multichat_topic_for(target_user_id) -> str:
    """Return the per-user WAMP topic for a multi-persona broadcast.

    The target_user_id is the user who OWNS the target persona (i.e.
    `agents_session[key][i]['user_id']` for the matched entry).  Only
    that user's HARTOS process subscribes to the suffixed topic, which
    contains cross-tenant leaks in a multi-node hive deployment.

    Pattern mirrors `core.peer_link.message_bus.chat_topic_for(user_id)`
    for `com.hertzai.hevolve.chat.{user_id}`.
    """
    return f'{MULTICHAT_TOPIC_BASE}.{target_user_id}'


def register_persona_for_session(
    user_id: Any,
    prompt_id: Any,
    persona_list: List[Dict[str, Any]],
    device_id: str = 'something',
) -> int:
    """Populate `agents_session` + `agents_roles` from a persona list.

    Called at the start of each recipe flow:
      - create_recipe.set_for_creating_actions (after config load with
        `config['personas']` / `config['flows'][...]['persona']`)
      - reuse_recipe's update_persona flow (already does this inline
        at lines 723-731, 802-806 — those usage sites continue to work
        unchanged since `agents_session`/`agents_roles` are now
        imported from here)

    `persona_list` items may be dicts with 'name'/'role' keys or plain
    strings.  Idempotent — re-call overwrites the session's entries
    (useful after persona edits during recipe creation).

    Returns the count of personas registered.  Never raises — logs +
    returns 0 on a malformed persona_list (per `feedback_audit_evidence_discipline.md`:
    log everything, no silent gulps).
    """
    if user_id is None or prompt_id is None:
        logger.warning(
            "register_persona_for_session: missing user_id/prompt_id "
            "(user_id=%r, prompt_id=%r); skipping", user_id, prompt_id)
        return 0

    key = f"{user_id}_{prompt_id}"
    session_entries: List[Dict[str, Any]] = []
    roles_map: Dict[Any, str] = {}

    for persona in (persona_list or []):
        # Accept both string names and dict shapes
        if isinstance(persona, str):
            role_name = persona
        elif isinstance(persona, dict):
            role_name = persona.get('name') or persona.get('role')
        else:
            logger.warning(
                "register_persona_for_session: skipping unknown persona "
                "shape %r", type(persona))
            continue

        if not role_name:
            logger.warning(
                "register_persona_for_session: skipping persona without "
                "name/role: %r", persona)
            continue

        session_entries.append({
            'agentInstanceID': f'com.hertzai.hevolve.chat.{prompt_id}.{user_id}',
            'user_id': user_id,
            'role': role_name,
            'deviceID': device_id,
        })
        roles_map[user_id] = role_name

    agents_session[key] = session_entries
    agents_roles[key] = roles_map
    logger.info(
        "register_persona_for_session: registered %d persona(s) for %s",
        len(session_entries), key)
    return len(session_entries)


def _resolve_session_for_user(user_id: Any, prompt_id: Any):
    """Return (session_entries, key) for the user, falling back to the
    chat-creator's session if this user joined another user's chat."""
    key = f"{user_id}_{prompt_id}"
    sessions = agents_session.get(key, [])
    if sessions:
        return sessions, key

    # Cross-user join fallback
    try:
        joinees_for_user = chat_joinees.get(user_id, {}) or {}
        creator = joinees_for_user.get(prompt_id)
        if creator:
            creator_key = f"{creator}_{prompt_id}"
            sessions = agents_session.get(creator_key, [])
            if sessions:
                return sessions, creator_key
    except Exception:
        logger.warning(
            "_resolve_session_for_user: chat_joinees lookup failed for %s",
            key, exc_info=True)

    return [], key


def _send_message_to_roles_impl(
    user_id: Any,
    prompt_id: Any,
    role: str,
    message: str,
    publish_fn: Optional[Any] = None,
) -> str:
    """Canonical multi-persona broadcast.

    Looks up the target persona by `role` in `agents_session`, then
    publishes the message to crossbar topic `MULTICHAT_TOPIC` with
    caller metadata so the receiving persona's agent loop can route it.

    `publish_fn` is the crossbar publisher — pass
    `reuse_recipe.publish_async` (or `helper_fun.publish_async` /
    a `safe_hartos_attr('publish_async')`-resolved callable).  If
    omitted, the function lazy-resolves the canonical
    `core.safe_hartos_attr.safe_hartos_attr('publish_async')`.

    Returns a status string (per autogen tool contract — string return
    is fed back to the LLM as the tool's response).
    """
    if not role:
        logger.warning(
            "send_message_to_roles: empty role arg for %s_%s",
            user_id, prompt_id)
        return "Cannot broadcast: role argument is empty"

    sessions, key = _resolve_session_for_user(user_id, prompt_id)
    if not sessions:
        logger.warning(
            "send_message_to_roles: no agent_session for %s; "
            "personas not initialized yet?", key)
        return (
            f"No personas registered for session {key}. "
            "Run gather-requirements first.")

    caller_role = (agents_roles.get(key, {}) or {}).get(user_id)

    # Resolve publisher if caller didn't pass one
    if publish_fn is None:
        try:
            from core.safe_hartos_attr import safe_hartos_attr
            publish_fn = safe_hartos_attr('publish_async')
        except Exception:
            logger.error(
                "send_message_to_roles: cannot resolve publish_async via "
                "safe_hartos_attr", exc_info=True)
            return "Crossbar publisher unavailable"
        if publish_fn is None:
            logger.error(
                "send_message_to_roles: publish_async resolved to None")
            return "Crossbar publisher unavailable"

    for entry in sessions:
        if entry.get('role') == role:
            payload = dict(entry)
            payload.update({
                'message': message,
                'caller_role': caller_role,
                'caller_user_id': user_id,
                'caller_prompt_id': prompt_id,
            })
            # Per-user topic — only the target persona's owning user's
            # HARTOS process subscribes.  Falls back to caller's user_id
            # if the persona entry doesn't carry one (defensive — every
            # entry SHOULD have a user_id per the register_persona_for_session
            # contract).
            target_uid = entry.get('user_id') or user_id
            topic = multichat_topic_for(target_uid)
            try:
                publish_fn(topic, payload)
                logger.info(
                    "send_message_to_roles: published to %s role=%s "
                    "via %s", key, role, topic)
                return 'Message sent Successfully'
            except Exception:
                logger.error(
                    "send_message_to_roles: publish failed for %s role=%s",
                    key, role, exc_info=True)
                return f"Failed to publish to role={role}"

    logger.warning(
        "send_message_to_roles: no persona with role=%r in session %s "
        "(available roles=%s)",
        role, key, [e.get('role') for e in sessions])
    return f"No persona with role={role!r} in session"


__all__ = [
    'agents_session',
    'agents_roles',
    'chat_joinees',
    'MULTICHAT_TOPIC_BASE',
    'multichat_topic_for',
    'register_persona_for_session',
    '_send_message_to_roles_impl',
]
