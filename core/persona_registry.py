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
import time
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


#: The consent a child's side grants when it lets another account join its
#: chat with an agent (integrations.social.consent_service.CONSENT_TYPES).
JOIN_CONSENT_TYPE = 'chat_join'


def _kept_joins(joiner: Any, prompt_id: Any) -> List[str]:
    """The creators whose chat_join grants for ``joiner`` and agent
    ``prompt_id`` are active, oldest first.  Raises when this node has no
    consent store to read."""
    from integrations.social.consent_service import ConsentService
    from integrations.social.models import db_session
    with db_session(commit=False) as db:
        return [str(c) for c in ConsentService.granted_by(
            db, JOIN_CONSENT_TYPE, f'{joiner}#', agent_id=str(prompt_id), prefix=True)]


def record_join(creator: Any, prompt_id: Any, joiner: Any) -> Optional[bool]:
    """Keep that ``creator``'s side let ``joiner`` into its chat with agent
    ``prompt_id`` (owner 2026-11-06: "multiple persons multiple accounts
    associated via consent"): a chat_join grant on the creator's account,
    which ends the join when revoked (the consent API's revoke; the
    privacy page does not list chat_join rows yet).  One row per
    approval (scope '<joiner>#<time>'): consent rows are append-only and
    unique per scope, so a join allowed again after a revoke needs a row of
    its own (hartos-16).  True when kept (or already kept); None when this
    node has no consent store (central), so the join lasts this process
    only, as joins always did there; False when the store is there but the
    grant was not written: then the join must not go ahead, because
    joined_chats reads the store and would not see it."""
    try:
        from integrations.social.consent_service import ConsentService
        from integrations.social.models import db_session
        if str(creator) in _kept_joins(joiner, prompt_id):
            return True
        with db_session() as db:
            ConsentService.grant_consent(db, str(creator), JOIN_CONSENT_TYPE,
                                         scope=f'{joiner}#{time.time_ns()}',
                                         agent_id=str(prompt_id))
        return True
    except Exception:
        try:
            _kept_joins(joiner, prompt_id)
        except Exception:
            logger.info("record_join: no consent store on this node; %s's join to "
                        "%s's chat of agent %s lasts this process only",
                        joiner, creator, prompt_id)
            return None
        logger.warning("record_join: %s's join to %s's chat of agent %s not kept",
                       joiner, creator, prompt_id, exc_info=True)
        return False


def joined_chats(joiner: Any, prompt_id: Any) -> List[str]:
    """Whose chats with agent ``prompt_id`` ``joiner`` has joined, oldest
    first: the one reader of the join link (_resolve_session_for_user,
    reuse_recipe.get_role and move_person_to_flow_impl all ask here).  The
    kept chat_join grants (record_join) when they can be read, so a revoked
    grant ends the join at once; else this process's chat_joinees entry."""
    try:
        return _kept_joins(joiner, prompt_id)
    except Exception:
        logger.debug("joined_chats: grants unreadable for %s; using this "
                     "process's joins", joiner, exc_info=True)
    creator = (chat_joinees.get(joiner) or {}).get(prompt_id)
    return [str(creator)] if creator else []


def _resolve_session_for_user(user_id: Any, prompt_id: Any):
    """Return (session_entries, key) for the user, falling back to the
    chat-creator's session if this user joined another user's chat."""
    key = f"{user_id}_{prompt_id}"
    sessions = agents_session.get(key, [])
    if sessions:
        return sessions, key

    # Cross-user join fallback.  A lookup that raises never reaches the live
    # send path; it is logged and the caller gets no session.
    try:
        for creator in joined_chats(user_id, prompt_id):
            creator_key = f"{creator}_{prompt_id}"
            sessions = agents_session.get(creator_key, [])
            if sessions:
                return sessions, creator_key
    except Exception:
        logger.warning(
            "_resolve_session_for_user: chat_joinees lookup failed for %s",
            key, exc_info=True)

    return [], key


def move_person_to_flow_impl(joiner: Any, prompt_id: Any, flows: List[dict],
                             personas: List[str], flow_name: str,
                             person: Optional[str] = None) -> str:
    """A parent or therapist who joined a child's chat moves the child to
    another of the child's own flows (owner 2026-11-04: "may move the child
    to another flow by telling the agent in their own chat").  ``person``
    names the child when the joiner joined more than one chat of this agent.
    The move is core.flow_progress.set_person_flow, by the joiner; the
    child's next run starts there.  What to tell the joiner (autogen tool
    contract)."""
    from core.flow_progress import _flow_name, persona_flows, set_person_flow, stored_persona
    joined = [c for c in joined_chats(joiner, prompt_id) if c != str(joiner)]
    if not joined:
        return "You have not joined anyone's chat with this agent, so there is no one to move."
    wanted = str(person or '').strip()
    if wanted:
        if wanted not in joined:
            return (f"You have not joined {wanted}'s chat with this agent; "
                    f"you joined: {', '.join(joined)}.")
        child = wanted
    elif len(joined) == 1:
        child = joined[0]
    else:
        return (f"You joined more than one chat with this agent ({', '.join(joined)}); "
                f"say whose flow to change.")
    persona = stored_persona(child, prompt_id, personas)
    if not persona:
        return (f"{child} has not used this agent yet, so they are on no flow to move from; "
                f"ask again after their first session.")
    own = {i: _flow_name(flows, i) for i in persona_flows(flows, persona)}
    wanted_flow = str(flow_name or '').strip().lower()
    target = next((i for i, name in own.items() if name.strip().lower() == wanted_flow), None)
    if target is None:
        return (f"'{flow_name}' is not one of {child}'s flows; their flows are: "
                f"{', '.join(own.values())}.")
    if not set_person_flow(child, prompt_id, flows, persona, target, by=str(joiner)):
        return f"{child} could not be moved to '{own[target]}'; nothing was changed."
    logger.info("move_person_to_flow: %s moved %s to flow %s of agent %s",
                joiner, child, target, prompt_id)
    return f"{child} is now on '{own[target]}'; their next session starts there."


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
    'JOIN_CONSENT_TYPE',
    'record_join',
    'joined_chats',
    'move_person_to_flow_impl',
]
