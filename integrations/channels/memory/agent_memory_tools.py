"""
Agent Memory Tools — Framework-agnostic tool functions + framework adapters.

Core tools (plain Python functions, no framework dependencies):
- remember(): Register a memory with provenance tracking
- recall_memory(): Search memories with semantic/text/hybrid
- backtrace_memory(): Trace memory chains back to origin
- get_memory_context(): Auto-recall from current context
- record_lifecycle_event(): Record agent lifecycle transitions

Framework adapters (thin wrappers):
- register_autogen_tools(): Registers tools on autogen agents
- create_langchain_tools(): Creates LangChain StructuredTool instances
"""

import json
import logging
import re
import time
from datetime import datetime
from typing import Annotated, Any, Callable, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# UUID hex pattern (16 chars) — used to detect direct vs semantic backtrace
_UUID_PATTERN = re.compile(r'^[a-f0-9]{16}$')

# Shortcut like "2h" / "30m" / "7d" → seconds offset from now
_REL_TIME_PATTERN = re.compile(r'^\s*(\d+)\s*([smhd])\s*$', re.IGNORECASE)


def _parse_time_arg(arg: 'str | None') -> 'float | None':
    """Accept ISO-8601, bare date, or relative shortcut and return a
    UNIX epoch timestamp (seconds). Returns None on empty / unparseable
    input so callers pass it straight through to the store filter."""
    if arg is None:
        return None
    s = str(arg).strip()
    if not s or s.lower() in ('none', 'null', ''):
        return None
    # Relative: "1h", "30m", "7d", "45s" → now - offset
    m = _REL_TIME_PATTERN.match(s)
    if m:
        amount = int(m.group(1))
        unit = m.group(2).lower()
        scale = {'s': 1, 'm': 60, 'h': 3600, 'd': 86400}[unit]
        return time.time() - amount * scale
    # ISO-8601 + common variants
    s2 = s.rstrip('Z').rstrip('z')
    for fmt in (
        '%Y-%m-%dT%H:%M:%S.%f',
        '%Y-%m-%dT%H:%M:%S',
        '%Y-%m-%d %H:%M:%S',
        '%Y-%m-%d',
    ):
        try:
            return datetime.strptime(s2, fmt).timestamp()
        except ValueError:
            continue
    return None


def create_memory_tools(
    graph: Any,  # MemoryGraph — typed as Any to avoid circular import
    user_id: str,
    session_id: str,
) -> Dict[str, Tuple[Callable, str]]:
    """
    Create framework-agnostic memory tool functions.

    Args:
        graph: MemoryGraph instance.
        user_id: Current user ID.
        session_id: Current session scope (e.g. user_id_prompt_id).

    Returns:
        Dict of {tool_name: (function, description)}.
        Any framework can wrap these — they're plain Python functions.
    """

    def remember(
        content: str,
        memory_type: str = "fact",
        context: str = "",
    ) -> str:
        """Register a memory with provenance tracking. Automatically links to recent memories in the same session."""
        try:
            # Auto-find recent memories in this session as parents
            recent = graph._get_latest_session_memory(session_id)
            parent_ids = [recent.id] if recent else []

            memory_id = graph.register(
                content=content,
                metadata={
                    "memory_type": memory_type,
                    "source_agent": "agent",
                    "session_id": session_id,
                },
                parent_ids=parent_ids,
                context_snapshot=context or f"Remembered during session {session_id}",
            )
            return f"Remembered (id={memory_id}). Use backtrace_memory('{memory_id}') to trace its origin chain."
        except Exception as e:
            logger.warning(f"Remember failed: {e}")
            return f"Failed to remember: {e}"

    def recall_memory(
        query: Annotated[str, "What to look for, in natural language (a topic or keywords). For a pure time-recall like 'what did we discuss', pass a broad term or the topic."],
        mode: Annotated[str, "Search mode: 'hybrid' (default, FTS+semantic), 'semantic', or 'text'."] = "hybrid",
        since: Annotated[Optional[str], "Start of an OPTIONAL time window. ISO date ('2026-05-22'), a bare date, or a relative shortcut ('15d', '7d', '24h'). Use this for 'N days back' recalls — e.g. '15 days back' → since='16d'."] = None,
        until: Annotated[Optional[str], "End of the OPTIONAL time window (same formats as since). Pass WITH since to bound a range around a past date — e.g. '15 days back' → until='14d'."] = None,
    ) -> str:
        """Search past memories by natural language, optionally limited to a time
        window. ``since`` / ``until`` accept ISO-8601 ('2026-04-10T15:00'), a bare
        date ('2026-04-10'), or a relative shortcut ('1h', '24h', '7d'). Pass both
        for a bounded range, either for a one-sided filter. Returns matching
        memories with IDs for backtrace.
        """
        try:
            _since_ts = _parse_time_arg(since)
            _until_ts = _parse_time_arg(until)
            nodes = graph.recall(
                query, mode=mode, top_k=5,
                since=_since_ts, until=_until_ts,
            )
            if not nodes:
                return "No matching memories found."

            lines = []
            for i, node in enumerate(nodes, 1):
                created = ""
                try:
                    from datetime import datetime
                    created = datetime.fromtimestamp(node.created_at).strftime("%Y-%m-%d %H:%M")
                except Exception:
                    pass
                lines.append(
                    f"{i}. [id={node.id}] {node.content[:200]} "
                    f"(type={node.memory_type}, agent={node.source_agent}, created={created})"
                )
            return "\n".join(lines)
        except Exception as e:
            logger.warning(f"Recall failed: {e}")
            return f"Memory recall failed: {e}"

    def backtrace_memory(
        memory_id_or_query: str,
    ) -> str:
        """Trace a memory back to its origin. Pass a memory ID for direct backtrace, or a query for semantic backtrace."""
        try:
            if _UUID_PATTERN.match(memory_id_or_query):
                # Direct backtrace by ID
                chain = graph.backtrace(memory_id_or_query, depth=10)
                if not chain:
                    return f"No memory found with id={memory_id_or_query}"

                lines = ["Memory chain (origin → current):"]
                for i, node in enumerate(chain):
                    arrow = "  " if i == 0 else "→ "
                    lines.append(
                        f"  {arrow}[{node.id}] {node.memory_type} by {node.source_agent}: "
                        f"{node.content[:150]}"
                    )
                return "\n".join(lines)
            else:
                # Semantic backtrace by query
                chains = graph.backtrace_semantic(memory_id_or_query, depth=5, top_k=3)
                if not chains:
                    return f"No memories found matching '{memory_id_or_query}'"

                lines = []
                for ci, chain in enumerate(chains, 1):
                    lines.append(f"\nChain {ci}:")
                    for i, node in enumerate(chain):
                        arrow = "  " if i == 0 else "→ "
                        lines.append(
                            f"  {arrow}[{node.id}] {node.memory_type} by {node.source_agent}: "
                            f"{node.content[:150]}"
                        )
                return "\n".join(lines)
        except Exception as e:
            logger.warning(f"Backtrace failed: {e}")
            return f"Memory backtrace failed: {e}"

    def get_memory_context() -> str:
        """Get automatically recalled memories relevant to the current conversation context."""
        try:
            # Get recent session memories as context source
            recent_memories = graph.get_session_memories(session_id, limit=5)
            if not recent_memories:
                return "No session memories available for context recall."

            recent_texts = [m.content for m in recent_memories[-3:]]
            relevant = graph.context_recall(recent_texts, top_k=3)

            if not relevant:
                return "No relevant memories found for current context."

            lines = ["Relevant memories from past sessions:"]
            for node in relevant:
                lines.append(
                    f"- [{node.memory_type}] {node.content[:200]} (by {node.source_agent})"
                )
            return "\n".join(lines)
        except Exception as e:
            logger.warning(f"Context recall failed: {e}")
            return f"Context recall failed: {e}"

    def record_lifecycle_event(
        event: str,
        details: str = "",
    ) -> str:
        """Record an agent lifecycle event (e.g. 'Creation Mode', 'completed', 'Reuse Mode')."""
        try:
            memory_id = graph.register_lifecycle(
                event=event,
                agent_id=user_id,
                session_id=session_id,
                details=details,
            )
            return f"Lifecycle event recorded: {event} (id={memory_id})"
        except Exception as e:
            logger.warning(f"Lifecycle recording failed: {e}")
            return f"Failed to record lifecycle event: {e}"

    return {
        "remember": (
            remember,
            "Save important facts, decisions, or insights to persistent memory with provenance tracking. "
            "Automatically links to recent memories for backtrace.",
        ),
        "recall_memory": (
            recall_memory,
            "Search past conversation memories by natural-language query, OPTIONALLY filtered to a "
            "time window via since/until (ISO date '2026-05-22', or relative '15d'/'7d'/'24h'). USE "
            "THIS for recall questions about the past — e.g. 'what did we discuss 15 days back' → "
            "recall_memory(query='what we discussed', since='16d', until='14d'). Returns matching "
            "memories with IDs for backtrace_memory.",
        ),
        "backtrace_memory": (
            backtrace_memory,
            "Trace a memory back to its origin. Pass a memory ID (from recall_memory) for direct "
            "backtrace, or a natural language query for semantic backtrace. Shows the chain of "
            "memories that led to the current one.",
        ),
        "get_memory_context": (
            get_memory_context,
            "Get relevant memories from past sessions based on the current conversation context. "
            "Useful for recalling related information without an explicit query.",
        ),
        "record_lifecycle_event": (
            record_lifecycle_event,
            "Record an agent lifecycle event such as 'Creation Mode', 'Review Mode', 'completed', "
            "'Evaluation Mode', or 'Reuse Mode'.",
        ),
    }


# =============================================================================
# Framework Adapters
# =============================================================================


def register_autogen_tools(
    tools_dict: Dict[str, Tuple[Callable, str]],
    assistant,
    helper,
):
    """
    Autogen adapter: register memory tools on autogen agents.

    Uses assistant.register_for_execution() and helper.register_for_llm()
    following the pattern in reuse_recipe.py:1547.

    Args:
        tools_dict: Output of create_memory_tools().
        assistant: Autogen AssistantAgent (executor).
        helper: Autogen AssistantAgent (LLM-callable).
    """
    for name, (func, desc) in tools_dict.items():
        helper.register_for_llm(
            name=name, api_style="tool", description=desc
        )(func)
        assistant.register_for_execution(name=name)(func)

    logger.info(f"Registered {len(tools_dict)} memory tools on autogen agents")


def create_langchain_tools(
    tools_dict: Dict[str, Tuple[Callable, str]],
) -> list:
    """
    LangChain adapter: wrap memory tools as StructuredTool instances.

    Args:
        tools_dict: Output of create_memory_tools().

    Returns:
        List of LangChain Tool instances.
    """
    try:
        # Import from langchain_core (the canonical home of StructuredTool),
        # NOT the `langchain` meta-package. `from langchain.tools import …`
        # triggers langchain's lazy re-export machinery, which in the frozen
        # bundle deep-imports the whole langchain/langchain_classic tree on
        # first use — measured at 169s on the user's first chat
        # (GET_TOOLS_TIMING memory_create_langchain_tools=169.15s, 2026-06-12).
        # langchain_core.tools is the direct source (~4s cold, warm after
        # boot) and re-exports the identical class.
        from langchain_core.tools import StructuredTool
    except ImportError:
        # Fallback to the meta-package only if core is somehow unavailable.
        try:
            from langchain.tools import StructuredTool
        except ImportError:
            logger.debug("LangChain StructuredTool not available — memory tools skipped")
            return []

    tools = []
    for name, (func, desc) in tools_dict.items():
        tool = StructuredTool.from_function(
            func=func,
            name=name,
            description=desc,
        )
        tools.append(tool)

    logger.info(f"Created {len(tools)} LangChain memory tools")
    return tools
