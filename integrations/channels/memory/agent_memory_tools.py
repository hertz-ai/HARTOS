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
from typing import Any, Callable, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# UUID hex pattern (16 chars) — used to detect direct vs semantic backtrace
_UUID_PATTERN = re.compile(r'^[a-f0-9]{16}$')


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
        query: str,
        mode: str = "hybrid",
    ) -> str:
        """Search all memories using natural language. Returns matching memories with IDs for backtrace."""
        try:
            nodes = graph.recall(query, mode=mode, top_k=5)
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
            "Search all memories using natural language query. Returns matching memories with IDs "
            "that can be used with backtrace_memory to trace their origin chain.",
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
            name=name, api_style="function", description=desc
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
        from langchain.tools import StructuredTool
    except ImportError:
        logger.warning("LangChain not available — cannot create LangChain tools")
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
