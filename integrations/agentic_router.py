"""
Agentic Intent Router — detects when a user prompt requires multi-step
execution and routes from LangChain to autogen.

Used by the LangChain Agentic_Router tool. When a prompt is classified as
agentic, this module:
1. Uses the LLM to semantically match against 96 expert agents + user recipes
2. Uses the LLM to generate a real execution plan (3-7 steps)
3. Returns structured plan data for the frontend Plan Mode UI

Intent classification itself is handled by the LLM deciding whether to call
the Agentic_Router tool — no keyword heuristics needed.
"""

import json
import logging
import os
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)


from core.llm_outbound_logger import with_source as _with_source


@_with_source('langchain.matcher')
def find_matching_agent(prompt: str, prompts_dir: str = None) -> Optional[Dict]:
    """Use LLM to semantically match prompt against available agents + recipes.

    Sends agent summaries to the LLM and asks it to select the best match.
    Falls back to None if LLM fails or no match found.
    """
    agent_summaries = _build_agent_catalog(prompts_dir)
    if not agent_summaries:
        return None

    try:
        from core.safe_hartos_attr import safe_hartos_attr
        get_llm = safe_hartos_attr('get_llm')
        if get_llm is None:
            logger.info(
                "Agent matching unavailable: HARTOS get_llm not yet "
                "resolvable — returning None (caller falls back to "
                "default agent dispatch)."
            )
            return None
        llm = get_llm(temperature=0.1, max_tokens=300)
        logger.info(
            "Agent matching: prompt=%r catalog_size=%d",
            (prompt or '')[:80], len(agent_summaries),
        )

        catalog_text = "\n".join(
            f"- ID:{a['id']} | {a['name']} | {a['source']} | {a['description'][:120]}"
            for a in agent_summaries[:50]
        )

        result = llm.invoke(
            f"Given this user task, select the single best matching agent from the catalog below. "
            f"If no agent is a good semantic match, respond with just 'NONE'.\n"
            f"Otherwise respond with ONLY the agent ID.\n\n"
            f"User task: {prompt}\n\n"
            f"Agent catalog:\n{catalog_text}"
        )

        text = (result.content if hasattr(result, 'content') else str(result)).strip()

        if text.upper() == 'NONE' or not text:
            return None

        for a in agent_summaries:
            if a['id'] in text:
                return {
                    'agent_id': a['id'],
                    'name': a['name'],
                    'score': 15,
                    'source': a['source'],
                    'description': a['description'],
                }
        return None
    except Exception as e:
        logger.warning(f"LLM agent matching failed: {e}")
        return None


def _build_agent_catalog(prompts_dir: str = None) -> List[Dict]:
    """Build unified catalog of expert agents + user recipes for LLM matching."""
    catalog = []

    try:
        from integrations.expert_agents.registry import ExpertAgentRegistry
        registry = ExpertAgentRegistry()
        for agent in registry.agents.values():
            catalog.append({
                'id': agent.agent_id,
                'name': agent.name,
                'description': agent.description,
                'source': 'expert',
            })
    except Exception:
        pass

    if prompts_dir and os.path.isdir(prompts_dir):
        try:
            for fname in os.listdir(prompts_dir):
                if not fname.endswith('.json') or '_recipe' in fname:
                    continue
                try:
                    with open(os.path.join(prompts_dir, fname)) as f:
                        recipe = json.load(f)
                    catalog.append({
                        'id': fname.replace('.json', ''),
                        'name': recipe.get('name', fname),
                        'description': recipe.get('goal', ''),
                        'source': 'recipe',
                    })
                except Exception:
                    continue
        except Exception:
            pass

    # Hive recipes — federated index from peer nodes
    try:
        from integrations.agent_engine.federated_aggregator import get_federated_aggregator
        agg = get_federated_aggregator()
        hive_index = agg.aggregate_recipes()
        if hive_index and isinstance(hive_index, dict):
            for rid, info in hive_index.items():
                if isinstance(info, dict) and info.get('name'):
                    catalog.append({
                        'id': rid,
                        'name': info['name'],
                        'description': info.get('description', ''),
                        'source': 'hive',
                    })
    except Exception:
        pass

    # Google A2A registered agents
    try:
        from integrations.google_a2a.dynamic_agent_registry import get_registry
        a2a_registry = get_registry()
        for agent in a2a_registry.list_agents():
            catalog.append({
                'id': agent.get('id', ''),
                'name': agent.get('name', ''),
                'description': agent.get('description', ''),
                'source': 'a2a',
            })
    except Exception:
        pass

    return catalog


def generate_plan_steps(prompt: str, matched_agent: Optional[Dict] = None) -> List[Dict]:
    """Generate plan steps using the LLM. Falls back to generic steps on failure."""
    try:
        from core.safe_hartos_attr import safe_hartos_attr
        get_llm = safe_hartos_attr('get_llm')
        if get_llm is None:
            logger.info(
                "Plan generation falling back to generic steps: HARTOS "
                "get_llm not yet resolvable (loader still init)."
            )
            raise RuntimeError("HARTOS get_llm unavailable")
        llm = get_llm(temperature=0.3, max_tokens=800)
        logger.info(
            "Plan generation: prompt=%r matched_agent=%s",
            (prompt or '')[:80],
            matched_agent.get('name') if matched_agent else None,
        )

        agent_context = ""
        if matched_agent:
            agent_context = (f"\nMatched expert: {matched_agent['name']} — "
                             f"{matched_agent.get('description', '')}")

        result = llm.invoke(
            f"Generate a 3-7 step execution plan for this task. "
            f"Return ONLY a JSON array: "
            f'[{{"step_num": 1, "description": "...", "tool_or_agent": "..."}}]'
            f"{agent_context}\n\nTask: {prompt}"
        )

        text = result.content if hasattr(result, 'content') else str(result)
        import re
        match = re.search(r'\[.*\]', text, re.DOTALL)
        if match:
            steps = json.loads(match.group())
            if isinstance(steps, list) and len(steps) >= 2:
                return steps
    except Exception as e:
        logger.warning(f"LLM plan generation failed, using fallback: {e}")

    agent_name = matched_agent['name'] if matched_agent else 'execution'
    return [
        {'step_num': 1, 'description': 'Analyze requirements and gather context', 'tool_or_agent': 'analysis'},
        {'step_num': 2, 'description': 'Plan approach and identify resources', 'tool_or_agent': 'planning'},
        {'step_num': 3, 'description': 'Execute the task', 'tool_or_agent': agent_name},
        {'step_num': 4, 'description': 'Deliver results and get feedback', 'tool_or_agent': 'delivery'},
    ]


def should_auto_create_agent(prompt: str, prompts_dir: str = None) -> bool:
    """Return True only if NO existing agent can handle this task.

    This is the gate that prevents unnecessary agent creation.
    """
    match = find_matching_agent(prompt, prompts_dir)
    return match is None


def build_agentic_plan(prompt: str, prompts_dir: str = None) -> Dict:
    """Full pipeline: match → plan. Returns structured plan dict."""
    matched_agent = find_matching_agent(prompt, prompts_dir)
    plan_steps = generate_plan_steps(prompt, matched_agent)

    return {
        'task_description': prompt,
        'steps': plan_steps,
        'matched_agent_id': matched_agent['agent_id'] if matched_agent else None,
        'matched_agent_name': matched_agent['name'] if matched_agent else None,
        'matched_agent_source': matched_agent['source'] if matched_agent else None,
        'confidence': 'high' if matched_agent else 'medium',
        'requires_new_agent': matched_agent is None,
    }


# ─────────────────────────────────────────────────────────────────────
# Direct-named dispatch (Phase 7b — mention path)
# Plan reference: sunny-gliding-eich.md, Part B.4 + Part E.5.
#
# Used when a specific agent is named (`@solar-architect`) so we don't
# need to run the matcher — the caller already knows which agent should
# reply. The dispatch runs the full guardrail pipeline that any other
# agent action runs through, and posts the reply via
# CommentService.create so it picks up DLP, classification, fan-out,
# resonance, etc. — same path a human reply takes. No new privileged
# code path.
#
# Async by default: a daemon thread does the LLM call so the calling
# Flask request (post create / comment create) returns immediately.
# If anything fails — guardrail rejection, LLM unreachable, source
# missing — the worker logs and exits. The Mention + Notification rows
# persisted upstream survive, so the agent runtime can pick the work up
# asynchronously the next tick.
# ─────────────────────────────────────────────────────────────────────

# Cap agent reply chains to keep two misbehaving agents in a group
# from spinning up an unbounded thread chain. Reviewer-flagged M2.
# The depth is carried through the context dict so each recursive
# trigger increments and refuses past the ceiling.
_MAX_AGENT_DISPATCH_DEPTH = 2


def dispatch_to_agent(agent_id: str, prompt: str,
                      context: Dict = None,
                      synchronous: bool = False) -> None:
    """Spawn an async worker to generate + post the agent's reply.

    Args:
      agent_id: User.id of the @-mentioned agent (User.user_type='agent').
      prompt: Inline content the agent should reason about — already
              includes surrounding source text from MentionService.
      context: dict with 'source_kind' ('post' | 'comment' | 'message'),
              'source_id', 'author_id', 'tenant_id', and optional
              '_dispatch_depth' (recursion counter, set automatically).
      synchronous: testing knob. When True, run inline so unit tests can
              assert post-conditions without thread coordination. Default
              False — production callers always want fire-and-forget.

    Recursion guard: if context['_dispatch_depth'] > _MAX_AGENT_DISPATCH_DEPTH
    the call is refused. This prevents two mention-each-other agents
    from spinning an unbounded chain (M2 — reviewer-flagged).

    Never raises — agent dispatch failure must never break the calling
    post/comment create path.
    """
    ctx = dict(context) if context else {}
    depth = int(ctx.get('_dispatch_depth') or 0)
    # `>=` so the cap names the max number of levels exactly. With
    # _MAX_AGENT_DISPATCH_DEPTH=2 we permit depth 0 → 1 (the call
    # itself) and depth 1 → 2 (one recursive level), refusing at 2.
    # Reviewer N-NEW-3.
    if depth >= _MAX_AGENT_DISPATCH_DEPTH:
        logger.info(
            "dispatch_to_agent: refusing recursive dispatch at depth=%d "
            "(ceiling=%d) for agent=%s",
            depth, _MAX_AGENT_DISPATCH_DEPTH, agent_id)
        return
    ctx['_dispatch_depth'] = depth + 1
    args = (agent_id, prompt, ctx)
    if synchronous:
        _dispatch_to_agent_worker(*args)
        return
    import threading
    threading.Thread(
        target=_dispatch_to_agent_worker,
        args=args,
        daemon=True,
        name=f'mention_dispatch_{(agent_id or "?")[:8]}',
    ).start()


def dispatch_via_chat(agent_id: str, rewritten_prompt: str,
                      context: Dict) -> Optional[str]:
    """Reuse the canonical /chat endpoint instead of doing a raw
    ``llm.invoke``.

    /chat already runs the full agent runtime (autogen / langchain /
    draft-first routing, persona via
    ``agent_identity.build_identity_prompt``, dynamic per-agent tools,
    multi-turn history) that ``flask_integration._handle_message`` has
    used for 31-channel adapter inbound since 2026-01-31.  Social-
    platform mention dispatch should use the same runtime — this
    helper lets it.

    Body shape mirrors what ``flask_integration._handle_message``
    builds at line 150 so /chat sees the same payload regardless of
    which caller dispatched.  Missing ``agent_id``/``prompt_id``/recipe
    is the existing /chat fallback signal that routes to LangChain.

    Transport is HTTP loopback to localhost via ``pooled_post`` — the
    same connection-pooled transport ``flask_integration`` uses, so we
    don't open a parallel HTTP client.  In every HARTOS deploy mode
    (flat / regional / central / Docker / ISO / pip-installed server)
    the HARTOS Flask app is reachable on its configured backend port,
    so loopback is universally available.

    Returns the agent reply text on success, or ``None`` on any failure
    (chat unreachable, non-200, malformed response, missing
    ``response`` field).  Caller falls back to raw ``llm.invoke`` so
    agents still respond rather than going silent.

    Public, fail-safe helper (returns None on any error).  Two callers:
    the agent-MENTION worker below gates its use behind the
    ``HEVOLVE_FLAG_DISPATCH_VIA_CHAT`` env flag (default off, dormant
    until production verifies the /chat path for social-platform agent
    dispatches); the morphable Nunba chat (#115) calls it directly so the
    Nunba assistant shares the canonical /chat brain — that path is
    unconditionally fail-safe to its heuristic, so it needs no flag.
    """
    try:
        from core.http_pool import pooled_post
    except Exception as e:
        logger.warning("dispatch_via_chat: core.http_pool unavailable (%s)", e)
        return None

    try:
        from core.constants import DEFAULT_USER_ID, DEFAULT_PROMPT_ID
    except Exception:
        DEFAULT_USER_ID, DEFAULT_PROMPT_ID = 10077, 8888

    try:
        from core.port_registry import get_port
        port = get_port('backend')
    except Exception:
        port = int(os.environ.get('FLASK_PORT')
                   or os.environ.get('HEVOLVE_BACKEND_PORT')
                   or '5000')

    body = {
        'user_id': context.get('owner_id') or DEFAULT_USER_ID,
        # /chat early-validation requires prompt_id key to exist —
        # DEFAULT_PROMPT_ID is the social-platform fallback.  /chat's
        # own logic resolves the actual recipe / persona for this
        # dispatch from agent_id (when set) before falling back to the
        # prompt_id default; this field is just the validation
        # placeholder.
        'prompt_id': context.get('prompt_id') or DEFAULT_PROMPT_ID,
        'prompt': rewritten_prompt,
        # agent_id is the social-platform agent's User.id — /chat uses
        # this to load the persona via agent_identity, register tools,
        # and route to the autogen runtime.  When absent /chat
        # gracefully falls back to LangChain (per the existing check).
        'agent_id': agent_id,
        # Don't auto-create — social agents are pre-registered via
        # UserService.register_agent at signup / onboarding time.
        'create_agent': False,
        # Forward the originating room context so /chat's response
        # router can fan out to the right surface.
        'channel_context': dict({
            'source_kind': context.get('source_kind'),
            'source_id': context.get('source_id'),
        }, **(context.get('channel_context') or {})),
    }

    url = f"http://localhost:{port}/chat"
    try:
        resp = pooled_post(url, json=body, timeout=120)
    except Exception as e:
        logger.warning(
            "dispatch_via_chat: pooled_post to %s failed: %s", url, e)
        return None

    if getattr(resp, 'status_code', 0) != 200:
        logger.warning(
            "dispatch_via_chat: /chat returned %s for agent=%s — body=%s",
            getattr(resp, 'status_code', '?'),
            agent_id,
            (getattr(resp, 'text', '') or '')[:200])
        return None

    try:
        data = resp.json()
    except Exception as e:
        logger.warning(
            "dispatch_via_chat: response not JSON for agent=%s: %s",
            agent_id, e)
        return None

    reply = data.get('response') if isinstance(data, dict) else None
    if not isinstance(reply, str) or not reply.strip():
        return None
    return reply.strip()


def _dispatch_to_agent_worker(agent_id: str, prompt: str, context: Dict):
    """Worker body: guardrails → LLM → guardrails → post as Comment.

    Each stage degrades gracefully — missing LLM or guardrails just
    short-circuits the worker, never bubbles. The Mention rows are
    already persisted by MentionService so the conversation isn't lost.
    """
    if not agent_id or not prompt:
        return

    # 1. Pre-dispatch: Constitutional Filter + Ethos + circuit breaker.
    rewritten = prompt
    try:
        from security.hive_guardrails import GuardrailEnforcer
        passed, reason, rewritten = GuardrailEnforcer.before_dispatch(prompt)
        if not passed:
            logger.info("dispatch_to_agent: rejected pre-dispatch (%s); "
                        "agent=%s", reason, agent_id)
            return
    except Exception as e:
        # Guardrails module missing — degrade open in dev, but still
        # block in any production where the module is expected. Log
        # loudly so this is visible in audits.
        logger.warning("dispatch_to_agent: guardrails unavailable; "
                       "proceeding without pre-dispatch check (%s)", e)

    # 2. Run prompt through the canonical agent runtime.
    #
    # Two paths, gated by HEVOLVE_FLAG_DISPATCH_VIA_CHAT:
    #
    #   ON (target state) — delegate to the canonical /chat HTTP
    #     endpoint flask_integration._handle_message has used for
    #     31-channel adapter inbound since 2026-01-31.  /chat brings:
    #       - autogen / langchain / draft-first routing (chooses the
    #         right runtime per request, with the existing
    #         "no agent_id/prompt_id/recipe → langchain" fallback)
    #       - per-agent persona via build_identity_prompt(agent_config)
    #       - dynamic tool registration per agent
    #       - multi-turn conversation history
    #     This is the unification the social-platform dispatch was
    #     missing — without it, social agents make raw single-shot
    #     llm.invoke calls and have NO tools / persona / history.
    #
    #   OFF (default — preserves existing behavior) — raw `llm.invoke`
    #     via safe_hartos_attr('get_llm').  Same code path social
    #     agents have used since the 2026-05-04 Phase-7+8+9 mega-commit.
    #     Flag stays off until the /chat path is validated for social-
    #     platform dispatches; flipping the flag is the rollout switch.
    #
    # On flag-on `dispatch_via_chat` returning None (chat endpoint
    # unreachable, non-200, malformed response), we fall back to raw
    # llm.invoke so agents still respond rather than going silent.
    reply_text: Optional[str] = None
    use_chat = (
        os.environ.get('HEVOLVE_FLAG_DISPATCH_VIA_CHAT', '').strip().lower()
        in ('1', 'true', 'yes', 'on')
    )
    if use_chat:
        reply_text = dispatch_via_chat(agent_id, rewritten, context)
        if reply_text is None:
            logger.info(
                "dispatch_to_agent: /chat path failed; falling back to "
                "raw LLM for agent=%s", agent_id)

    if not reply_text:
        try:
            from core.safe_hartos_attr import safe_hartos_attr
            get_llm = safe_hartos_attr('get_llm')
            if get_llm is None:
                logger.info("dispatch_to_agent: get_llm unresolved; "
                            "agent=%s — runtime will pick up", agent_id)
                return
            llm = get_llm(temperature=0.7, max_tokens=600)
            result = llm.invoke(rewritten)
            reply_text = (result.content if hasattr(result, 'content')
                          else str(result)).strip()
        except Exception as e:
            logger.warning(
                "dispatch_to_agent: LLM call failed for agent=%s: %s",
                agent_id, e)
            return

    if not reply_text:
        return

    # 3. Post-response: Constructive Filter + energy tracking.
    try:
        from security.hive_guardrails import GuardrailEnforcer
        passed, reason = GuardrailEnforcer.after_response(reply_text)
        if not passed:
            logger.info("dispatch_to_agent: response blocked post-LLM "
                        "(%s); agent=%s", reason, agent_id)
            return
    except Exception:
        pass

    # 4. Post via existing CommentService — same fan-out a human gets.
    _post_agent_reply(agent_id, context or {}, reply_text)


def _post_agent_reply(agent_id: str, context: Dict, reply_text: str):
    """Post the agent's reply through the appropriate existing surface.

    For source_kind='post' → top-level comment on that post.
    For source_kind='comment' → reply nested under the parent comment.
    For source_kind='message' → message in the same conversation
      (Phase 7c.3 path; agent's reply gets the same fan-out + mention
      parsing any human reply gets — no privileged path).
    Unknown source kinds are logged and skipped.
    """
    source_kind = context.get('source_kind')
    source_id = context.get('source_id')
    if not source_kind or not source_id:
        return
    try:
        from integrations.social.models import db_session, User, Post, Comment
        from integrations.social.services import CommentService
        with db_session() as db:
            agent = db.query(User).filter(User.id == agent_id).first()
            if not agent:
                logger.info("dispatch_to_agent: agent user_id=%s not found",
                            agent_id)
                return

            if source_kind == 'post':
                post = db.query(Post).filter(Post.id == source_id).first()
                if post:
                    CommentService.create(db, post, agent, reply_text)
            elif source_kind == 'comment':
                parent = db.query(Comment).filter(
                    Comment.id == source_id).first()
                if parent:
                    post = db.query(Post).filter(
                        Post.id == parent.post_id).first()
                    if post:
                        CommentService.create(db, post, agent, reply_text,
                                              parent_id=parent.id)
            elif source_kind == 'message':
                # Conversation message — find the parent conversation
                # via the source message row, post the agent's reply
                # as a new Message in that same conversation.
                from sqlalchemy import text as _text
                row = db.execute(_text(
                    "SELECT parent_id FROM messages WHERE id = :mid"),
                    {'mid': source_id}
                ).fetchone()
                if row is None:
                    logger.info("dispatch_to_agent: source message %s not "
                                "found", source_id)
                    return
                conv_id = row[0]
                from integrations.social.conversation_service import (
                    ConversationService)
                # Auto-add the agent as a conversation member if its
                # owner already is and the conversation allows agents.
                # For now we require the agent to already be a member —
                # joining is handled by AgentJoinGrant in Phase 7d.
                from integrations.social.conversation_service import (
                    _is_member)
                if not _is_member(db, conv_id, agent.id):
                    logger.info("dispatch_to_agent: agent %s not a member "
                                "of conv %s; skipping reply",
                                agent.id, conv_id)
                    return
                ConversationService.send_message(
                    db, conv_id=conv_id, author_id=agent.id,
                    content=reply_text,
                    tenant_id=context.get('tenant_id'))
            elif source_kind == 'call':
                # Live voice/video call — the agent's reply text is
                # destined for an audio publisher (PocketTTS → LiveKit
                # frames).  Three deliveries:
                #
                #   1. TTS outbox enqueue → bridge worker drains, hands
                #      to audio publisher (canonical reply path for the
                #      magic loop: speaker → STT → router → TTS → speaker).
                #   2. Cross-channel transcript persist → the call
                #      transcript reads chronologically alongside any
                #      adapter-channel chat (UNIF-G3).  Best-effort.
                #   3. Liquid UI meet_copilot card emit → the user sees
                #      the agent's reply in the UI overlay even before
                #      audio arrives (UNIF-G5).  Best-effort.
                #
                # source_id == call_id (CallSession).  The agent must
                # already be attached as a CallParticipant; any
                # AgentJoinGrant + scope.can_voice gating happens at
                # CallService.attach_agent before the bridge spins up.
                call_id = source_id
                try:
                    from integrations.social.agent_voice_bridge import (
                        enqueue_tts_text)
                    enqueue_tts_text(call_id, agent.id, reply_text)
                except Exception as e:
                    logger.warning(
                        "dispatch_to_agent: TTS outbox enqueue failed "
                        "(call=%s, agent=%s): %s",
                        call_id, agent.id, e)
                try:
                    from integrations.social.chat_messages import (
                        persist_external_room_event)
                    persist_external_room_event(
                        user_id=str(context.get('owner_id') or agent.id),
                        platform=str(context.get('platform') or 'livekit'),
                        room_id=str(call_id),
                        sender_id=str(agent.id),
                        text=reply_text,
                        kind='agent_reply',
                    )
                except Exception as e:
                    logger.debug(
                        "dispatch_to_agent: cross-channel persist "
                        "skipped (%s)", e)
                try:
                    from core.platform.registry import get_registry
                    svc = get_registry().get_or_none('LiquidUIService')
                    if svc is not None:
                        svc.agent_ui_update(
                            str(agent.id),
                            {
                                'type': 'meet_copilot',
                                'call_id': str(call_id),
                                'platform': str(
                                    context.get('platform') or 'livekit'),
                                'room_id': str(call_id),
                                'state': 'live',
                                'agent_reply': reply_text,
                            },
                        )
                except Exception as e:
                    logger.debug(
                        "dispatch_to_agent: meet_copilot emit "
                        "skipped (%s)", e)
            elif source_kind == 'external_room':
                # Adapter-bound rooms (Discord channel / WhatsApp group /
                # Slack channel / Matrix room / Teams channel / etc.).
                # The agent's reply text is destined for the SAME room
                # the user wrote in.  Delegate to the canonical
                # ``ChannelResponseRouter.route_response`` — it:
                #   1. Logs the assistant turn to ConversationEntry.
                #   2. Sends back to the originating channel via the
                #      ChannelRegistry (each adapter's send_message).
                #   3. Optionally fans out to other bound channels.
                #   4. WAMP-notifies the user's desktop.
                #
                # Caller's ``context['channel_context']`` MUST carry
                # ``{channel, chat_id, sender_id}`` — the same dict
                # ``flask_integration._handle_message`` already builds
                # for the legacy /chat path.  Reusing the same shape
                # means a future migration can swap the legacy path
                # over by changing one call site.
                ch_ctx = context.get('channel_context') or {}
                if not ch_ctx.get('channel') or not ch_ctx.get('chat_id'):
                    logger.info(
                        "dispatch_to_agent: source_kind='external_room' "
                        "needs context['channel_context'] with "
                        "{channel, chat_id}; got %r — skipping reply",
                        ch_ctx)
                    return
                try:
                    from integrations.channels.response.router import (
                        get_response_router)
                    get_response_router().route_response(
                        user_id=context.get('owner_id') or agent.id,
                        response_text=reply_text,
                        channel_context=ch_ctx,
                        agent_id=agent.id,
                        # Fan-out is a caller decision — default False
                        # so the agent reply lands in the originating
                        # room only.  Callers that want bound-channel
                        # broadcast can pass `fan_out_external=True` in
                        # context.
                        fan_out=bool(context.get('fan_out_external')),
                    )
                except Exception as e:
                    logger.warning(
                        "dispatch_to_agent: external_room reply via "
                        "ChannelResponseRouter failed (channel=%s "
                        "chat_id=%s): %s",
                        ch_ctx.get('channel'), ch_ctx.get('chat_id'), e)
            else:
                logger.info("dispatch_to_agent: source_kind=%s not yet "
                            "supported", source_kind)
    except Exception as e:
        logger.warning("dispatch_to_agent: post-reply persist failed: %s", e)
