"""Per-(user, prompt) persona/role registry — single source of truth.

A single agent instance can be shared across multiple personas (student /
parent / teacher).  The Helper agent uses `send_message_to_roles(role,
message)` to ask the agent of another person in the same chat, which
answers from its saved notes about that person.

This module hosts the TTLCache singletons that map `{user_id}_{prompt_id}`
to the persona/role state, plus the one routine the
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
    node keeps no consent rows at all (_consent_store_kept_here), so the join
    lasts this process only; False when the store is there but the grant was
    not written: then the join must not go ahead, because joined_chats reads
    the store and would not see it.  Central is NOT a no-store node: its
    langchain container keeps consent rows in its own social database
    (/app/logs/hartos_social.db), apart from every desktop's, and consent
    rows are not synced between nodes, so a join is read on the node that
    approved it."""
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
        if not _consent_store_kept_here():
            logger.info("record_join: no consent store on this node; %s's join to "
                        "%s's chat of agent %s lasts this process only",
                        joiner, creator, prompt_id)
            return None
        logger.warning("record_join: %s's join to %s's chat of agent %s not kept",
                       joiner, creator, prompt_id, exc_info=True)
        return False


def _consent_store_kept_here() -> bool:
    """Whether this node keeps consent rows at all (the consent table exists),
    asked after a read or write of them failed.  It tells a node with no store
    (a join lasts this process only) from a store that is there but did not
    answer (locked, busy; central's SQLite has logged "database is locked"
    under load).  Only the first may fall back to this
    process's chat_joinees, which a revoke never clears: with the second, the
    fallback brought a revoked join back (review of 86f4c71f8).  A failure to
    tell counts as a store that is there, so a revoke always wins."""
    try:
        from integrations.social.models import UserConsent, get_engine
    except ImportError:
        return False
    try:
        from sqlalchemy import inspect
        return bool(inspect(get_engine()).has_table(UserConsent.__tablename__))
    except Exception:
        logger.warning("could not tell whether this node keeps consent rows; "
                       "treating it as one that does", exc_info=True)
        return True


def _this_process_join(joiner: Any, prompt_id: Any) -> Optional[str]:
    """This process's record of whose chat ``joiner`` joined for agent
    ``prompt_id``.  Matched as text: a join written with integer ids is found
    by the string ids /chat uses, and the other way round."""
    for who, chats in list(chat_joinees.items()):
        if str(who) != str(joiner):
            continue
        for pid, creator in list((chats or {}).items()):
            if str(pid) == str(prompt_id) and creator:
                return str(creator)
    return None


def joined_chats(joiner: Any, prompt_id: Any) -> List[str]:
    """Whose chats with agent ``prompt_id`` ``joiner`` has joined, oldest
    first: the one reader of the join link (_others_in_the_chat,
    reuse_recipe.get_role and move_person_to_flow_impl all ask here).  The
    kept chat_join grants (record_join) when they can be read, so a revoked
    grant ends the join at once.  When they cannot: on a node with no consent
    store, this process's chat_joinees entry; on a node whose store is there
    but did not answer, no chat at all, because a revoke never clears
    chat_joinees and the entry would bring the revoked join back (review of
    86f4c71f8)."""
    try:
        return _kept_joins(joiner, prompt_id)
    except Exception:
        if _consent_store_kept_here():
            logger.warning("joined_chats: %s's joins to agent %s could not be read; "
                           "none reached until they can", joiner, prompt_id,
                           exc_info=True)
            return []
        logger.debug("joined_chats: no consent store on this node; using this "
                     "process's joins for %s", joiner)
    creator = _this_process_join(joiner, prompt_id)
    return [creator] if creator else []


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
    # Told to the agent, about the person it serves: the agent never joined
    # anything (owner 11-07: a person is never the agent).
    if not joined:
        return ("Not moved: the person you are serving has not joined anyone's chat "
                "with this agent, so there is no one to move. Whoever's chat they want "
                "to join has to allow the join first.")
    wanted = str(person or '').strip()
    if wanted:
        if wanted not in joined:
            return (f"Not moved: the person you are serving has not joined {wanted}'s chat "
                    f"with this agent; they joined {', '.join(joined)}. Call again with "
                    f"one of those as person.")
        child = wanted
    elif len(joined) == 1:
        child = joined[0]
    else:
        return (f"Not moved: the person you are serving joined more than one chat with "
                f"this agent ({', '.join(joined)}). Call again with person set to the "
                f"one to move.")
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
    return f"{child} is now on '{own[target]}'; their next turn starts there."


#: The tool's name, as every refusal names it (core.tool_refusal.refuse).
ROLE_TOOL = 'send_message_to_roles'
#: How long the one model call may take once it is sent
#: (core.llm_json.ask_model_json's ``timeout``, the request's own timeout;
#: sent once, so it is not waited twice).  A background turn may first wait
#: for a model slot (core.http_pool._SCHED_ACQUIRE_TIMEOUT_S); a person's turn
#: is given one at once.
ROLE_ANSWER_TIMEOUT_S = 30.0
#: How much of the saved notes one answer reads, the person's own first.
ROLE_NOTES_MAX_CHARS = 6000
_ROLE_ANSWER_SCHEMA = {'type': 'object', 'additionalProperties': False,
                       'required': ['answer'],
                       'properties': {'answer': {'type': 'string'}}}
ROLE_ANSWER_PROMPT = (
    "You are {agent}, an agent. {goal}\n"
    "One of the people you work with is {role}{persona}.\n"
    "Another person in the same chat, {caller_role}, asks you about {role} "
    "through their own agent.\n\n"
    "Everything you have saved about {role} (your notes, as JSON):\n{notes}\n\n"
    "Their message:\n{message}\n\n"
    "Answer {caller_role} from your notes alone, in plain words. If the notes "
    "do not hold the answer, say it is not in your notes. Promise nothing on "
    "{role}'s behalf and do not say you will tell or ask {role}: nothing "
    "reaches them from here. Answer with the JSON object only.")
#: What every reply ends with, an answer or not: the tool reaches the other
#: person's agent memory only (owner 2026-11-12, rule 1).  Rule 2 (a steer
#: kept for the other person's next turn) is not built.
_ROLE_NOTHING_KEPT = ("Nothing was sent to {role}, and nothing in your message "
                      "is kept for their next turn.")


def _role_of(user_id: Any, prompt_id: Any) -> Optional[str]:
    """The persona ``user_id`` has with agent ``prompt_id``:
    reuse_recipe.get_role, the one reader (their kept pick first, then this
    process's member lists)."""
    from hartos.reuse_recipe import get_role
    return get_role(user_id, prompt_id)


def _others_in_the_chat(user_id: Any, prompt_id: Any):
    """Whom ``user_id``'s agent may ask about, for agent ``prompt_id``:
    ``(others, turned_off, held)``.

    ``others`` is [(their user id, their role)]: the people whose chat
    ``user_id`` joined (joined_chats: the chat_join grants that stand), and
    no one else.  That is what the join card told the person who allowed the
    join ("their agent can ask this agent what it remembers about your
    sessions"), so it is all the grant covers.  Each role is the person's own
    (_role_of), never the role a member list files them under: a creator's
    list holds every persona under the creator's id.  Never this person.

    ``turned_off``: the role of the person whose chat this process saw
    ``user_id`` join (_this_process_join, which a revoke never clears) once
    that grant is gone.

    ``held``: the roles of everyone else in these chats, the joiners of this
    person's own chat and the other joiners of a chat they joined, whether
    their join stands or not.  Nobody agreed to be asked about in those
    directions, so they are refused until the owner rules on them."""
    me = str(user_id)
    try:
        creators = [str(c) for c in joined_chats(user_id, prompt_id)]
    except Exception:
        logger.warning("send_message_to_roles: %s's joins to agent %s could not "
                       "be read; no joined chat reached", user_id, prompt_id,
                       exc_info=True)
        creators = []
    creators = [c for c in dict.fromkeys(creators) if c != me]

    def role_of(uid):
        try:
            return str(_role_of(uid, prompt_id) or '')
        except Exception:
            logger.warning("send_message_to_roles: %s's role with agent %s could "
                           "not be read", uid, prompt_id, exc_info=True)
            return ''

    others = [(uid, role) for uid, role in ((c, role_of(c)) for c in creators) if role]
    held = set()
    for owner in [me] + creators:
        for entry in list(agents_session.get(f'{owner}_{prompt_id}', []) or []):
            uid = str(entry.get('user_id') or owner)
            if uid == me or uid in creators:
                continue
            held.update(r for r in (str(entry.get('role') or ''), role_of(uid)) if r)
    turned_off = set()
    try:
        left = _this_process_join(user_id, prompt_id)
        if left and left != me and left not in creators:
            turned_off.add(role_of(left))
    except Exception:
        logger.debug("send_message_to_roles: this process's joins for %s not "
                     "readable", user_id, exc_info=True)
    return others, {r for r in turned_off if r}, held


def _kept_on_this_computer(user_id: Any, prompt_id: Any) -> bool:
    """Whether this node holds anything of ``user_id``'s use of agent
    ``prompt_id``: a live chat, their kept place, or their saved notes.  A
    person whose agent runs on another computer has none of these here."""
    from core.agent_tools import person_store_for
    from core.cache_loaders import load_agent_data
    from core.flow_progress import read
    return bool(agents_session.get(f'{user_id}_{prompt_id}')
                or read(user_id, prompt_id)
                or load_agent_data(person_store_for(user_id, prompt_id)) is not None)


def _role_notes(target_user_id: Any, prompt_id: Any) -> Optional[str]:
    """What agent ``prompt_id`` has saved about ``target_user_id``, as that
    person's own memory tools read it (core.agent_tools.memory_view over the
    saved stores), secrets and credentials out (security.edge_privacy
    .redact_secret_leaves with credentials: a value under a secret-named or
    credential-named key withheld whole whatever its type, a text that names
    a password or a credential withheld whole, a secret inside any other
    text replaced), the person's own entries first, cut at
    ROLE_NOTES_MAX_CHARS.  '' when nothing is saved.  Read only: nothing is
    loaded into a session's cache and nothing is written.  Raises when the
    secrets cannot be taken out, so the notes are withheld, never sent raw."""
    import json
    from core.agent_tools import memory_view, person_store_for
    from core.cache_loaders import load_agent_data
    from security.edge_privacy import redact_secret_leaves
    user_prompt = f'{target_user_id}_{prompt_id}'
    own_key = person_store_for(target_user_id, prompt_id)
    own = load_agent_data(own_key)
    stores = {prompt_id: load_agent_data(prompt_id) or {}, own_key: own or {}}
    view = memory_view(stores, prompt_id, user_prompt)
    if not view:
        return ''
    view, _withheld = redact_secret_leaves(view, credentials=True)
    mine = [k for k in view if isinstance(own, dict) and k in own]
    ordered = mine + [k for k in view if k not in mine]
    text = '\n'.join(json.dumps({k: view[k]}, ensure_ascii=False, default=str)
                     for k in ordered)
    if len(text) > ROLE_NOTES_MAX_CHARS:
        text = text[:ROLE_NOTES_MAX_CHARS] + '\n[the rest of the notes is not shown]'
    return text


def _answer_from_role_notes(target_user_id: Any, prompt_id: Any, role: str,
                            caller_role: str, message: str, agent: str,
                            config: dict):
    """``(outcome, text)``: the agent's answer about ``role`` from its saved
    notes alone, by ONE call to the node's own model (core.llm_json
    .ask_model_json: core.autogen_config.llm_http_target, never the model a
    turn's override names; sent once, bounded by its request timeout).  No
    group chat,
    no tool, no step state and no write.  Outcomes: 'answered' (text is the
    answer), 'no_notes', 'withheld' (secrets could not be taken out),
    'provider_refusing' (the node's provider breaker is open, the check a
    turn's dispatch makes) and 'no_answer' (the model did not reply in time
    or not as asked)."""
    try:
        notes = _role_notes(target_user_id, prompt_id)
    except Exception:
        logger.warning("send_message_to_roles: the notes about %s could not be "
                       "cleared of secrets; withheld", target_user_id, exc_info=True)
        return 'withheld', None
    if not notes:
        return 'no_notes', None
    try:
        from integrations.agent_engine.dispatch import local_dispatch_provider_breaker_open
        if local_dispatch_provider_breaker_open():
            return 'provider_refusing', None
    except Exception:
        logger.debug("send_message_to_roles: provider breaker not readable",
                     exc_info=True)
    described = next((str(p.get('description') or '').strip()
                      for p in (config.get('personas') or [])
                      if isinstance(p, dict) and str(p.get('name') or '').strip().lower()
                      == role.strip().lower()), '')
    from core.llm_json import ask_model_json
    reply = ask_model_json(
        ROLE_ANSWER_PROMPT.format(
            agent=agent, goal=str(config.get('goal') or '').strip(), role=role,
            persona=f' ({described})' if described else '',
            caller_role=caller_role, notes=notes, message=message),
        _ROLE_ANSWER_SCHEMA, 'role_memory_answer', max_tokens=400,
        timeout=ROLE_ANSWER_TIMEOUT_S, once=True)
    answer = str((reply or {}).get('answer') or '').strip()
    return ('answered', answer) if answer else ('no_answer', None)


def _audit_role_ask(prompt_id, caller, caller_role, target, role, message,
                    outcome, answer=None) -> None:
    """The ask and its answer on the audit trail (security.audit_trail, kind
    'message'), once for each side: the asker's agent and person, and the
    asked agent and person when one was found.  The model call itself is the
    trail's 'llm_call' line (core.llm_json -> log_outbound)."""
    try:
        from hartos.threadlocal import thread_local_data
        from security.audit_trail import record
        request_id = thread_local_data.get_request_id()
        common = dict(agent_id=prompt_id, request_id=request_id, actor='agent',
                      via=ROLE_TOOL, outcome=outcome, from_user_id=str(caller),
                      from_role=caller_role, to_user_id=str(target) if target else None,
                      to_role=role)
        sides = [(caller, caller_role, 'out', 'in')]
        if target:
            sides.append((target, role, 'in', 'out'))
        for person, persona, asked, answered in sides:
            record('message', direction=asked, text=message, user_id=str(person),
                   agent_role=persona, part='ask', **common)
            if answer:
                record('message', direction=answered, text=answer, user_id=str(person),
                       agent_role=persona, part='answer', **common)
    except Exception:
        logger.warning("send_message_to_roles: the ask was not put on the audit "
                       "trail", exc_info=True)


def _send_message_to_roles_impl(
    user_id: Any,
    prompt_id: Any,
    role: str,
    message: str,
    publish_fn: Optional[Any] = None,
) -> str:
    """The send_message_to_roles tool: this person's agent asks the agent of
    another person in the same chat (owner 2026-11-12, rule 1: "the parent's
    agent asks the child's agent"; the child's agent "answers from its own
    memory" and each person's store stays sealed).

    The other person is the one whose chat this person joined, found by
    ``role`` (_others_in_the_chat), so it rests on the chat_join grant the
    join already took: while it stands nothing is asked again, and once it
    is turned off that person is no longer reached.  The other directions
    (the chat's own person asking about a joiner, one joiner about another)
    are refused: the join card promised only this one.  The
    answer is _answer_from_role_notes: the agent's saved notes about that
    person and one model call.  It comes back as this tool's result and goes
    nowhere else: nothing is sent to the other person and nothing is kept
    for their next turn, and the result says so.

    It used to publish the message to the per-user multichat topic and
    answer 'Message sent Successfully'.  Nothing on a desktop subscribes to
    that topic, so nobody received it; the one subscriber there is
    (crossbar_server's hart-crossbar worker) would run the receiver's whole
    step-holding group on the sender's words.  ``publish_fn`` is still
    accepted, so both tool registrations call as before, and is not used.

    Returns the tool's reply (autogen tool contract): the answer, or a
    refusal that says what was wrong and what to do (core.tool_refusal)."""
    from core.tool_refusal import RETRY_AFTER_FIX, RETRY_FINISH_WITHOUT, refuse
    key = f"{user_id}_{prompt_id}"

    def refused(reason, problem, fix, target=None, caller_role=None,
                retry=RETRY_FINISH_WITHOUT):
        _audit_role_ask(prompt_id, user_id, caller_role, target, role, message,
                        f'refused: {reason}')
        return refuse(ROLE_TOOL, problem, fix, retry=retry)

    if not role:
        logger.warning(
            "send_message_to_roles: empty role arg for %s_%s",
            user_id, prompt_id)
        return refused('empty role', "Cannot broadcast: role argument is empty",
                       "Set role to the persona of the person to ask about",
                       retry=RETRY_AFTER_FIX)
    try:
        caller_role = _role_of(user_id, prompt_id)
    except Exception:
        logger.warning("send_message_to_roles: the caller's role for %s could not "
                       "be read", key, exc_info=True)
        caller_role = None
    others, turned_off, held = _others_in_the_chat(user_id, prompt_id)
    wanted = str(role).strip().lower()
    target = next((uid for uid, theirs in others if theirs.strip().lower() == wanted), None)
    if target is None:
        # A person with nothing of this agent's on this computer has no role
        # read here (_role_of answers its default), so they are named by id.
        away = [uid for uid, _theirs in others
                if not _kept_on_this_computer(uid, prompt_id)]
        names = sorted({theirs for uid, theirs in others if uid not in away})
        mine = bool(agents_session.get(key))
        if wanted in {r.strip().lower() for r in turned_off}:
            logger.warning("send_message_to_roles: role=%r in session %s was let "
                           "in by a join that is turned off", role, key)
            return refused(
                'join turned off',
                f"No persona with role={role!r} in session: their join to this "
                f"chat was turned off",
                "They can be asked about again only once the join is allowed again"
                + (f"; the roles that can be asked about now are: {', '.join(names)}"
                   if names else ''), caller_role=caller_role)
        if caller_role and wanted == str(caller_role).strip().lower():
            return refused(
                'own role',
                f"role={role!r} is the person you are serving, not another person "
                f"in this chat",
                "Ask them in this chat with send_message_to_user"
                + (f", or set role to one of: {', '.join(names)}" if names else ''),
                caller_role=caller_role)
        if wanted in {r.strip().lower() for r in held}:
            return refused(
                'not the chat joined',
                f"No persona with role={role!r} can be asked about from here: only "
                f"a person who joined can ask about the person whose chat they "
                f"joined",
                (f"The roles that can be asked about from here are: {', '.join(names)}"
                 if names else "No one can be asked about from this chat"),
                caller_role=caller_role)
        if not names and away:
            return refused(
                'other computer',
                f"No persona with role={role!r} in session on this computer: user "
                f"{away[0]}, whose chat with this agent is shared, is not on this "
                f"computer",
                "They can be asked about on the computer their chat with this "
                "agent runs on", target=away[0], caller_role=caller_role)
        if not names and not mine:
            logger.warning(
                "send_message_to_roles: no agent_session for %s; "
                "personas not initialized yet?", key)
            return refused(
                'no session',
                f"No personas registered for session {key}",
                "Run gather-requirements first; another person can be asked about "
                "once they have joined this chat", caller_role=caller_role)
        logger.warning(
            "send_message_to_roles: no persona with role=%r in session %s "
            "(available roles=%s)", role, key, names)
        if not names:
            return refused(
                'no such role',
                f"No persona with role={role!r} in session: no other person has "
                f"joined this chat",
                "Another person can be asked about once they have joined this chat",
                caller_role=caller_role)
        return refused(
            'no such role', f"No persona with role={role!r} in session",
            f"Set role to one of the roles in this chat: {', '.join(names)}",
            caller_role=caller_role, retry=RETRY_AFTER_FIX)
    if not _kept_on_this_computer(target, prompt_id):
        return refused(
            'other computer',
            f"{role} (user {target}) is not on this computer: this agent has "
            f"nothing of theirs here to answer from",
            f"{role} can be asked about on the computer their chat with this "
            f"agent runs on", target=target, caller_role=caller_role)
    from core.cache_loaders import load_agent_config
    config = load_agent_config(prompt_id) or {}
    agent = str(config.get('name') or '').strip() or f'agent {prompt_id}'
    asked_as = str(caller_role or 'another person in this chat')
    outcome, answer = _answer_from_role_notes(target, prompt_id, str(role), asked_as,
                                              str(message or ''), agent, config)
    nothing_kept = _ROLE_NOTHING_KEPT.format(role=role)
    if outcome == 'answered':
        _audit_role_ask(prompt_id, user_id, caller_role, target, role, message,
                        'answered', answer)
        logger.info("send_message_to_roles: %s asked about role=%s (user %s); "
                    "answered from the agent's notes", key, role, target)
        return (f"Answered from {agent}'s notes about {role} (user {target}): "
                f"{answer}\n{nothing_kept}")
    if outcome == 'no_notes':
        said = (f"{agent} has no notes about {role} (user {target}) yet, so there "
                f"is nothing to answer from.")
        _audit_role_ask(prompt_id, user_id, caller_role, target, role, message,
                        'answered: no notes', said)
        return f"{said} {nothing_kept}"
    problem = {
        'withheld': (f"{agent}'s notes about {role} could not be cleared of "
                     f"secrets, so they were not read"),
        'provider_refusing': (f"{role}'s agent cannot answer now: this "
                              f"computer's model provider is refusing calls"),
    }.get(outcome, (f"{role}'s agent did not answer: the model gave no reply "
                    f"within {ROLE_ANSWER_TIMEOUT_S:.0f} s of being asked, and it "
                    f"was asked once"))
    return refused(outcome.replace('_', ' '), problem,
                   f"Carry on without it and say that {role}'s agent could not "
                   f"be asked. {nothing_kept}", target=target, caller_role=caller_role)


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
