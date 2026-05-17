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

    # 2. Run prompt through the canonical LLM resolver.
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
        logger.warning("dispatch_to_agent: LLM call failed for agent=%s: %s",
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
            else:
                logger.info("dispatch_to_agent: source_kind=%s not yet "
                            "supported", source_kind)
    except Exception as e:
        logger.warning("dispatch_to_agent: post-reply persist failed: %s", e)
