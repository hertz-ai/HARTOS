"""LabeledTool factory — Tool construction with mandatory UI label (#508).

Every Tool() that appears in the chat agent's tool registry should be
constructed via labeled_tool() instead of the bare langchain Tool().
Required `ui_label` kwarg → Python raises TypeError at construction if a
new tool is added without supplying user-facing status text.

This is the compile-time replacement for an AST drift-guard test:
the constraint lives in the type system, not in a sibling regex check.

Coverage spans both static and dynamic tool sources:

  - Hardcoded literals in hart_intelligence_entry.get_tools(is_first=True)
  - integrations.skills.registry (HART skills loaded from disk)
  - integrations.service_tools.registry (HTTP microservice tools)
  - integrations.providers.agent_tools (provider gateway)
  - integrations.service_tools.system_introspect_tool

Each site supplies a ui_label appropriate to the tool.  Dynamic
registries that can't pre-compute a friendly label may pass the
generic_label() helper which returns "Running {name}…" — explicit
opt-in to the fallback, not silent drift.

The returned object is an unmodified langchain Tool — the factory adds
no runtime overhead.  TOOL_LABELS dict is the single source of truth
for the spinner's CyclingVerb override; this factory just ensures every
construction site populates it.
"""
from __future__ import annotations

from typing import Any, Callable

from langchain_classic.agents import Tool

from core.constants import register_tool_label


def labeled_tool(
    name: str,
    func: Callable[..., Any],
    description: str,
    *,
    ui_label: str,
) -> Tool:
    """Construct a langchain Tool with a mandatory UI status label.

    Args:
        name: Tool name as the LLM and `_with_tool_logging` see it.
        func: Tool function (single arg → output string).
        description: LLM-facing description that drives tool selection.
        ui_label: Short user-facing status text ≤ 60 chars shown in the
            spinner when this tool fires.  REQUIRED — call site must
            decide.  For tools without a meaningful verb phrase, pass
            generic_label(name) to opt explicitly into the fallback.

    Returns:
        A langchain Tool — drop-in for existing Tool() construction.

    Raises:
        TypeError: if ui_label is omitted (Python kwarg enforcement).
        ValueError: if ui_label is empty or non-string.
    """
    if not isinstance(ui_label, str) or not ui_label.strip():
        raise ValueError(
            f"labeled_tool({name!r}): ui_label must be a non-empty string; "
            f"pass generic_label({name!r}) to opt into the 'Running …' fallback")
    register_tool_label(name, ui_label)
    return Tool(name=name, func=func, description=description)


def generic_label(name: str) -> str:
    """Explicit opt-in to the 'Running {name}…' fallback for tools whose
    name is already self-descriptive.  Use sparingly — a real verb
    phrase (e.g. 'Searching the web…') is usually more polished."""
    return f'Running {name}…'
