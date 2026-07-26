"""
HARTOS MCP Server — stdio-based Model Context Protocol server

Exposes HARTOS agent ecosystem tools to Claude Code for orchestration.
Run: python -m integrations.mcp.mcp_server

Tools:
  list_agents, list_goals, create_goal, dispatch_goal, agent_status,
  remember, recall, list_recipes, system_health, social_query
"""
import os
import sys
import json
import glob as _glob
import logging
from pathlib import Path
from typing import Optional
from core.port_registry import get_port
from core.http_pool import pooled_get, pooled_post

from mcp.server.fastmcp import FastMCP

logger = logging.getLogger('hartos_mcp')
logging.basicConfig(level=logging.INFO, format='%(name)s %(levelname)s %(message)s')

mcp = FastMCP("hartos", instructions=(
    "HARTOS agent ecosystem tools. Use these to orchestrate autonomous agents, "
    "manage goals, query memory, and monitor system health."
))

# ─── Shared tool implementations (single source: _tool_impls) ───
# Tool bodies live in integrations.mcp._tool_impls so the stdio server and the
# HTTP bridge can't drift (#98c).  _get_* are re-imported so the transport-
# specific tools below (dispatch_goal, switch_model, code, onboard_kong) keep
# their existing calls unchanged.
from integrations.mcp import _tool_impls as impls
from integrations.mcp._tool_impls import _get_registry, _get_db, _get_memory_graph


# ─── Tools ───
# Shared, identical-across-transports tools register the canonical impl
# directly.  Transport-specific tools (dispatch_goal, switch_model, code,
# onboard_kong) keep their own @mcp.tool() defs further down.
mcp.tool()(impls.list_agents)
mcp.tool()(impls.list_goals)
mcp.tool()(impls.create_goal)
mcp.tool()(impls.steer_goal)
mcp.tool()(impls.agent_status)
mcp.tool()(impls.recall)
mcp.tool()(impls.list_recipes)
mcp.tool()(impls.system_health)
mcp.tool()(impls.social_query)


@mcp.tool()
def remember(content: str, memory_type: str = 'decision') -> str:
    """Store a memory in the persistent memory graph. Types: fact, decision, insight, lifecycle."""
    # Provenance tag is transport-specific; the body is shared (#98c).
    return impls.remember(content, memory_type, source_agent='claude_orchestrator')

@mcp.tool()
def dispatch_goal(goal_id: str, goal_type: str = 'marketing') -> str:
    """Dispatch a goal to an idle agent for execution. The daemon does this automatically every 30s, but this forces immediate dispatch.

    Persists ``last_dispatched_at`` on the Goal row so that flywheel
    progress is observable in subsequent ``list_goals`` queries —
    without this, repeated MCP dispatches looked identical to "stuck"
    goals because the agent_daemon was the only writer of that field
    (and the daemon has been silent for days on this install).
    """
    try:
        from integrations.agent_engine.goal_manager import GoalManager
        from integrations.social.models import AgentGoal as _GoalModel
        from datetime import datetime as _dt
        db = _get_db()
        try:
            goal_result = GoalManager.get_goal(db, goal_id)
            if not goal_result.get('success'):
                return json.dumps({"error": f"Goal {goal_id} not found"})

            goal = goal_result['goal']
            prompt = goal.get('description', goal.get('title', ''))

            # Resolve the canonical system user (Nunba).  Falls back to
            # ensure_system_user so the row is auto-created on first
            # call — the prior `db.query(...).first() or 'system'`
            # pattern returned the literal string 'system' when the
            # User row didn't exist, which failed downstream FK checks
            # on posts.author_id / goals.created_by.
            from integrations.social.services import UserService
            sys_user = UserService.ensure_system_user(
                db, 'nunba', display_name='Nunba',
                bio='The Hevolve hive — autonomous orchestrator '
                    'for benchmarks, experiments, and dispatch.')
            user_id = sys_user.id
            # ensure_system_user may have inserted a new row — commit so
            # subsequent queries (incl. the persistence write below)
            # see it, instead of losing it on db.close().
            db.commit()
        finally:
            db.close()

        from integrations.agent_engine.dispatch import dispatch_goal as _dispatch
        response = _dispatch(
            prompt=prompt,
            user_id=user_id,
            goal_id=goal_id,
            goal_type=goal_type,
        )

        # Stamp last_dispatched_at so the goal row shows progress.
        # Best-effort — a write failure here must not mask a successful
        # dispatch (the response is what callers act on).
        try:
            db = _get_db()
            try:
                row = db.query(_GoalModel).filter_by(id=goal_id).first()
                if row is not None:
                    row.last_dispatched_at = _dt.utcnow()
                    db.flush()
                    db.commit()
            finally:
                db.close()
        except Exception as _persist_err:
            logger.warning(
                "mcp.dispatch_goal: persist last_dispatched_at "
                "failed for %s: %s", goal_id, _persist_err)

        return json.dumps({"dispatched": True, "goal_id": goal_id, "response_preview": str(response)[:500]}, default=str)
    except Exception as e:
        return json.dumps({"error": str(e)})


@mcp.tool()
def switch_model(model_name: str) -> str:
    """Switch the local LLM model at runtime. Restarts llama-server with the new model.

    Available models:
    - "default" or "qwen35-4b": Qwen3.5-4B VL (recommended, vision+text) [index 0]
    - "qwen35-2b": Qwen3.5-2B VL (lightweight, low VRAM / CPU) [index 1]
    - "vision" or "qwen3-vl-2b": Qwen3-VL-2B (older vision model) [index 2]
    - "gemma": Gemma-3-1B (smallest, fastest, text-only) [index 3]
    - "qwen3-2b": Qwen3-2B (text-only) [index 4]
    """
    name_to_index = {
        "default": 0, "text": 0, "qwen35-4b": 0, "qwen3.5-4b": 0,
        "qwen35-2b": 1, "qwen3.5-2b": 1,
        "vision": 2, "qwen3-vl-2b": 2, "vl": 2, "vl-2b": 2,
        "gemma": 3, "gemma-1b": 3,
        "qwen3-2b": 4,
    }

    model_index = name_to_index.get(model_name.lower().strip())
    if model_index is None:
        try:
            model_index = int(model_name)
        except ValueError:
            return json.dumps({
                "error": f"Unknown model: {model_name}",
                "valid": list(name_to_index.keys()),
            })

    try:
        import requests
        resp = pooled_post('http://localhost:5000/api/llm/switch', json={"model_index": model_index}, timeout=120)
        if resp.status_code == 200:
            return json.dumps(resp.json(), default=str)
        return json.dumps({"error": f"Server returned {resp.status_code}: {resp.text[:300]}"})
    except requests.exceptions.ConnectionError:
        # Server not running — update config directly
        config_path = os.path.join(os.path.expanduser('~'), '.nunba', 'llama_config.json')
        try:
            with open(config_path) as f:
                cfg = json.load(f)
            cfg['selected_model_index'] = model_index
            with open(config_path, 'w') as f:
                json.dump(cfg, f, indent=2)
            from llama.llama_installer import MODEL_PRESETS
            preset = MODEL_PRESETS[model_index] if model_index < len(MODEL_PRESETS) else None
            return json.dumps({
                "config_updated": True,
                "model_index": model_index,
                "model_name": preset.display_name if preset else "unknown",
                "note": "Server not running. Config saved — will use this model on next start."
            })
        except Exception as e:
            return json.dumps({"error": str(e)})
    except Exception as e:
        return json.dumps({"error": str(e)})


@mcp.tool()
def code(
    task: str,
    task_type: str = 'feature',
    preferred_tool: str = '',
    working_dir: str = '',
    model: str = '',
) -> str:
    """Execute a coding task via the distributed coding agent.

    Routes to the best available tool (kilocode, claude_code, opencode, aider).
    Records benchmarks. Captures edits as recipes for REUSE mode.

    task_type: feature, bug_fix, refactor, code_review, app_build
    """
    try:
        from integrations.coding_agent.orchestrator import get_coding_orchestrator
        orchestrator = get_coding_orchestrator()
        result = orchestrator.execute(
            task=task,
            task_type=task_type,
            preferred_tool=preferred_tool,
            user_id='claude_mcp',
            model=model,
            working_dir=working_dir or os.getcwd(),
        )
        return json.dumps(result, indent=2, default=str)
    except Exception as e:
        return json.dumps({"error": str(e)})


@mcp.tool()
def onboard_kong(
    kong_url: str = 'http://localhost:8001',
    upstream_url: str = 'http://localhost:8000',
) -> str:
    """Onboard the Mindstory SDK into Kong API Gateway.

    Creates service, routes, and plugins (key-auth, rate-limiting, cors).
    Idempotent — safe to call multiple times. Queries existing config first.
    """
    try:
        from integrations.gateway.kong_onboard import onboard
        ok = onboard(kong_url=kong_url, upstream_url=upstream_url)
        return json.dumps({"success": ok})
    except Exception as e:
        return json.dumps({"error": str(e)})


# ─── Entry point ───

def start_sse_server(host: str = '127.0.0.1', port: int = None):
    """Start MCP server with SSE transport for HTTP clients (Nunba, external).

    This runs the FastMCP server on a dedicated port using Server-Sent Events.
    Clients connect via standard MCP SSE protocol.
    """
    if port is None:
        port = get_port('mcp')
    logger.info(f"Starting MCP SSE server on {host}:{port}")
    mcp.run(transport="sse", host=host, port=port)


def start_sse_server_background(host: str = '127.0.0.1', port: int = None):
    """Start MCP SSE server in a background thread."""
    import threading
    t = threading.Thread(
        target=start_sse_server,
        args=(host, port),
        daemon=True,
        name='mcp-sse-server',
    )
    t.start()
    logger.info("MCP SSE server started in background thread")
    return t


if __name__ == "__main__":
    # Ensure HARTOS root is on sys.path
    hartos_root = str(Path(__file__).resolve().parent.parent.parent)
    if hartos_root not in sys.path:
        sys.path.insert(0, hartos_root)

    # Support --sse flag for HTTP mode, default to stdio for Claude Code
    transport = "stdio"
    if "--sse" in sys.argv:
        transport = "sse"
    elif "--http" in sys.argv:
        transport = "streamable-http"

    if transport == "stdio":
        mcp.run(transport="stdio")
    else:
        port = get_port('mcp')
        logger.info(f"Starting MCP server with {transport} transport on port {port}")
        mcp.run(transport=transport, host="127.0.0.1", port=port)
