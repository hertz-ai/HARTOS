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

# ─── Lazy imports (deferred to avoid import-time side effects) ───

_registry = None
_memory_graph = None


def _get_registry():
    global _registry
    if _registry is None:
        from integrations.expert_agents.registry import ExpertAgentRegistry
        _registry = ExpertAgentRegistry()
    return _registry


def _get_db():
    from integrations.social.models import get_db
    return get_db()


def _get_memory_graph(user_id: str = 'system'):
    global _memory_graph
    if _memory_graph is None:
        from integrations.channels.memory.memory_graph import MemoryGraph
        try:
            from core.platform_paths import get_memory_graph_dir
            db_path = get_memory_graph_dir()
        except ImportError:
            db_path = os.path.join(
                os.path.expanduser('~'), 'Documents', 'Nunba', 'data', 'memory_graph'
            )
        _memory_graph = MemoryGraph(db_path=db_path, user_id=user_id)
    return _memory_graph


# ─── Tools ───

@mcp.tool()
def list_agents(category: Optional[str] = None, query: Optional[str] = None) -> str:
    """List available expert agents. Filter by category or search by query.

    Categories: software_dev, data_analytics, creative, business, education,
    health, security, devops, research, robotics
    """
    reg = _get_registry()

    if query:
        agents = reg.search_agents(query)
    elif category:
        from integrations.expert_agents.registry import AgentCategory
        cat_map = {name.lower(): member for name, member in AgentCategory.__members__.items()}
        cat = cat_map.get(category.lower())
        if not cat:
            return json.dumps({"error": f"Unknown category: {category}. Valid: {list(cat_map.keys())}"})
        agents = reg.get_agents_by_category(cat)
    else:
        agents = list(reg.agents.values())

    result = []
    for a in agents:
        result.append({
            "agent_id": a.agent_id,
            "name": a.name,
            "category": a.category.name if hasattr(a.category, 'name') else str(a.category),
            "description": a.description,
            "model_type": a.model_type,
        })

    # Also include dynamically discovered agents (trained recipes)
    prompts_dir = os.path.join(os.path.dirname(__file__), '..', '..', 'prompts')
    dynamic = []
    if os.path.isdir(prompts_dir):
        for f in _glob.glob(os.path.join(prompts_dir, '*.json')):
            try:
                with open(f) as fh:
                    data = json.load(fh)
                dynamic.append({
                    "agent_id": data.get("prompt_id", Path(f).stem),
                    "name": data.get("agent_name", Path(f).stem),
                    "category": "dynamic_recipe",
                    "description": data.get("description", "Trained agent recipe"),
                    "model_type": "llm",
                })
            except Exception:
                pass

    return json.dumps({
        "expert_agents": len(result),
        "dynamic_agents": len(dynamic),
        "agents": result[:50],  # cap at 50 to avoid token overflow
        "dynamic": dynamic[:20],
    }, indent=2)


@mcp.tool()
def list_goals(
    goal_type: Optional[str] = None,
    status: Optional[str] = None
) -> str:
    """List agent goals. Filter by type (marketing, coding, ip_protection, etc.) or status (active, pending, completed)."""
    try:
        from integrations.agent_engine.goal_manager import GoalManager
        db = _get_db()
        try:
            goals = GoalManager.list_goals(db, goal_type=goal_type, status=status)
            return json.dumps({"count": len(goals), "goals": goals}, indent=2, default=str)
        finally:
            db.close()
    except Exception as e:
        return json.dumps({"error": str(e)})


@mcp.tool()
def create_goal(
    goal_type: str,
    title: str,
    description: str = '',
    spark_budget: int = 200
) -> str:
    """Create a new goal for agents to pursue.

    goal_type: marketing, coding, ip_protection, revenue, finance, self_heal,
    federation, upgrade, thought_experiment, news, provision, content_gen
    """
    try:
        from integrations.agent_engine.goal_manager import GoalManager
        db = _get_db()
        try:
            result = GoalManager.create_goal(
                db,
                goal_type=goal_type,
                title=title,
                description=description,
                spark_budget=spark_budget,
            )
            db.commit()
            return json.dumps(result, indent=2, default=str)
        finally:
            db.close()
    except Exception as e:
        return json.dumps({"error": str(e)})


@mcp.tool()
def dispatch_goal(goal_id: str, goal_type: str = 'marketing') -> str:
    """Dispatch a goal to an idle agent for execution. The daemon does this automatically every 30s, but this forces immediate dispatch."""
    try:
        from integrations.agent_engine.goal_manager import GoalManager
        db = _get_db()
        try:
            goal_result = GoalManager.get_goal(db, goal_id)
            if not goal_result.get('success'):
                return json.dumps({"error": f"Goal {goal_id} not found"})

            goal = goal_result['goal']
            prompt = goal.get('description', goal.get('title', ''))

            # Get system agent user_id
            from integrations.social.models import User
            sys_agent = db.query(User).filter_by(username='hevolve_system_agent').first()
            user_id = sys_agent.id if sys_agent else 'system'
        finally:
            db.close()

        from integrations.agent_engine.dispatch import dispatch_goal as _dispatch
        response = _dispatch(
            prompt=prompt,
            user_id=user_id,
            goal_id=goal_id,
            goal_type=goal_type,
        )
        return json.dumps({"dispatched": True, "goal_id": goal_id, "response_preview": str(response)[:500]}, default=str)
    except Exception as e:
        return json.dumps({"error": str(e)})


@mcp.tool()
def agent_status() -> str:
    """Check agent daemon health, active dispatches, and system state."""
    status = {
        "daemon_enabled": os.environ.get('HEVOLVE_AGENT_ENGINE_ENABLED', 'false'),
        "poll_interval": int(os.environ.get('HEVOLVE_AGENT_POLL_INTERVAL', '30')),
        "max_concurrent": int(os.environ.get('HEVOLVE_AGENT_MAX_CONCURRENT', '10')),
        "speculative_enabled": os.environ.get('HEVOLVE_SPECULATIVE_ENABLED', 'false'),
    }

    # Check running server
    try:
        resp = pooled_get('http://localhost:5000/health', timeout=2)
        status['nunba_server'] = 'running' if resp.status_code == 200 else f'status {resp.status_code}'
    except Exception:
        status['nunba_server'] = 'not reachable'

    # Check LLM
    try:
        resp = pooled_get(f'http://localhost:{get_port("llm")}/health', timeout=2)
        status['llm_server'] = 'running' if resp.status_code == 200 else f'status {resp.status_code}'
    except Exception:
        status['llm_server'] = 'not reachable'

    # Goal counts
    try:
        from integrations.agent_engine.goal_manager import GoalManager
        db = _get_db()
        try:
            all_goals = GoalManager.list_goals(db)
            by_status = {}
            for g in all_goals:
                s = g.get('status', 'unknown')
                by_status[s] = by_status.get(s, 0) + 1
            status['goals'] = {'total': len(all_goals), 'by_status': by_status}
        finally:
            db.close()
    except Exception as e:
        status['goals'] = {'error': str(e)}

    # Expert agent count
    try:
        reg = _get_registry()
        status['expert_agents'] = len(reg.agents)
    except Exception:
        status['expert_agents'] = 'unknown'

    return json.dumps(status, indent=2, default=str)


@mcp.tool()
def remember(content: str, memory_type: str = 'decision') -> str:
    """Store a memory in the persistent memory graph. Types: fact, decision, insight, lifecycle."""
    try:
        mg = _get_memory_graph()
        memory_id = mg.register(
            content=content,
            metadata={'memory_type': memory_type, 'source_agent': 'claude_orchestrator'},
        )
        return json.dumps({"stored": True, "memory_id": memory_id})
    except Exception as e:
        return json.dumps({"error": str(e)})


@mcp.tool()
def recall(query: str, top_k: int = 5) -> str:
    """Search the persistent memory graph. Returns relevant memories ranked by relevance."""
    try:
        mg = _get_memory_graph()
        memories = mg.recall(query=query, mode='hybrid', top_k=top_k)
        result = []
        for m in memories:
            result.append({
                "id": m.id,
                "content": m.content,
                "memory_type": m.memory_type,
                "source_agent": m.source_agent,
                "created_at": m.created_at,
            })
        return json.dumps({"count": len(result), "memories": result}, indent=2, default=str)
    except Exception as e:
        return json.dumps({"error": str(e)})


@mcp.tool()
def list_recipes() -> str:
    """List trained agent recipes (prompts/*.json files)."""
    prompts_dir = os.path.join(os.path.dirname(__file__), '..', '..', 'prompts')
    recipes = []
    if os.path.isdir(prompts_dir):
        for f in sorted(_glob.glob(os.path.join(prompts_dir, '*.json'))):
            try:
                with open(f) as fh:
                    data = json.load(fh)
                recipes.append({
                    "file": Path(f).name,
                    "prompt_id": data.get("prompt_id", ""),
                    "agent_name": data.get("agent_name", ""),
                    "status": data.get("agent_status", ""),
                    "description": data.get("description", "")[:200],
                })
            except Exception:
                recipes.append({"file": Path(f).name, "error": "parse failed"})

    return json.dumps({"count": len(recipes), "recipes": recipes}, indent=2)


@mcp.tool()
def system_health() -> str:
    """Full system health check: Flask server, LLM, DB, memory graph."""
    health = {}

    # Flask server
    try:
        resp = pooled_get('http://localhost:5000/health', timeout=2)
        health['flask'] = {'status': 'up', 'code': resp.status_code}
    except Exception:
        health['flask'] = {'status': 'down'}

    # LLM server
    try:
        resp = pooled_get(f'http://localhost:{get_port("llm")}/health', timeout=2)
        health['llm'] = {'status': 'up', 'code': resp.status_code}
        try:
            models_resp = pooled_get(f'http://localhost:{get_port("llm")}/v1/models', timeout=2)
            if models_resp.status_code == 200:
                data = models_resp.json()
                models = data.get('data', [])
                health['llm']['models'] = [m.get('id', 'unknown') for m in models]
        except Exception:
            pass
    except Exception:
        health['llm'] = {'status': 'down'}

    # LangChain agent (port 6778)
    try:
        resp = pooled_get('http://localhost:6778/health', timeout=2)
        health['langchain'] = {'status': 'up' if resp.status_code == 200 else 'error'}
    except Exception:
        health['langchain'] = {'status': 'down'}

    # DB
    try:
        db = _get_db()
        try:
            from integrations.social.models import User
            count = db.query(User).count()
            health['db'] = {'status': 'up', 'user_count': count}
        finally:
            db.close()
    except Exception as e:
        health['db'] = {'status': 'error', 'detail': str(e)}

    # Memory graph
    try:
        mg = _get_memory_graph()
        health['memory'] = {'status': 'up', 'db_path': mg.db_path if hasattr(mg, 'db_path') else 'unknown'}
    except Exception as e:
        health['memory'] = {'status': 'error', 'detail': str(e)}

    return json.dumps(health, indent=2, default=str)


@mcp.tool()
def social_query(query_type: str, limit: int = 20) -> str:
    """Read-only social DB queries. Types: users, posts, goals, products, agents.

    Returns recent entries. For safety, only SELECT operations are performed.
    """
    try:
        db = _get_db()
        try:
            if query_type == 'users':
                from integrations.social.models import User
                rows = db.query(User).order_by(User.created_at.desc()).limit(limit).all()
                return json.dumps([{
                    "id": r.id, "username": r.username, "display_name": r.display_name,
                    "user_type": r.user_type, "role": r.role, "karma_score": r.karma_score,
                } for r in rows], indent=2, default=str)

            elif query_type == 'posts':
                from integrations.social.models import Post
                rows = db.query(Post).order_by(Post.created_at.desc()).limit(limit).all()
                return json.dumps([{
                    "id": r.id, "title": getattr(r, 'title', ''), "author_id": r.author_id,
                    "content": (r.content or '')[:200], "vote_count": getattr(r, 'vote_count', 0),
                } for r in rows], indent=2, default=str)

            elif query_type == 'goals':
                from integrations.agent_engine.goal_manager import GoalManager
                goals = GoalManager.list_goals(db)
                return json.dumps({"count": len(goals), "goals": goals[:limit]}, indent=2, default=str)

            elif query_type == 'products':
                from integrations.agent_engine.goal_manager import ProductManager
                products = ProductManager.list_products(db)
                return json.dumps({"count": len(products), "products": products[:limit]}, indent=2, default=str)

            elif query_type == 'agents':
                from integrations.social.models import User
                rows = db.query(User).filter_by(user_type='agent').limit(limit).all()
                return json.dumps([{
                    "id": r.id, "username": r.username, "display_name": r.display_name,
                    "agent_id": r.agent_id, "karma_score": r.karma_score,
                } for r in rows], indent=2, default=str)

            else:
                return json.dumps({"error": f"Unknown query_type: {query_type}. Valid: users, posts, goals, products, agents"})
        finally:
            db.close()
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
