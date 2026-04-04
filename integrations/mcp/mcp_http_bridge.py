"""
MCP HTTP Bridge — Exposes HARTOS MCP tools via REST endpoints.

Nunba and external clients connect here instead of stdio.
MCPServerConnector (mcp_integration.py) already speaks this REST contract:
  GET  /health         -> {"status": "ok", "tools": N}
  GET  /tools/list     -> {"tools": [...]}
  POST /tools/execute  -> {"tool": "name", "arguments": {...}} -> result

Tool functions are implemented directly here (not imported from mcp_server.py)
to avoid FastMCP/pydantic v1-v2 import conflicts. Both modules call the same
underlying HARTOS APIs (GoalManager, MemoryGraph, ExpertAgentRegistry, etc.).
"""

import json
import os
import logging
import inspect
import glob as _glob
from pathlib import Path
from flask import Blueprint, request, jsonify
from typing import Dict, Any, List, Optional, Callable

logger = logging.getLogger('hartos_mcp')

mcp_local_bp = Blueprint('mcp_local', __name__, url_prefix='/api/mcp/local')

# ── Tool Registry ──────────────────────────────────────────────
_local_tools: List[Dict[str, Any]] = []
_tools_loaded = False


def _extract_parameters(fn) -> dict:
    """Extract JSON Schema-style parameters from a function's signature."""
    if fn is None:
        return {}
    sig = inspect.signature(fn)
    properties = {}
    required = []
    for param_name, param in sig.parameters.items():
        if param_name in ('self', 'cls'):
            continue
        prop = {"type": "string"}
        annotation = param.annotation
        if annotation != inspect.Parameter.empty:
            if annotation in (int, float):
                prop["type"] = "number"
            elif annotation == bool:
                prop["type"] = "boolean"
        if param.default != inspect.Parameter.empty:
            prop["default"] = param.default
        else:
            required.append(param_name)
        properties[param_name] = prop
    schema = {"type": "object", "properties": properties}
    if required:
        schema["required"] = required
    return schema


def _register_tool(name: str, description: str, fn: Callable):
    """Register a tool function in the local registry."""
    _local_tools.append({
        "name": name,
        "description": description,
        "parameters": _extract_parameters(fn),
        "fn": fn,
    })


# ── Lazy helpers (same as mcp_server.py, avoid import-time side effects) ──

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


# ── Tool implementations ──────────────────────────────────────
# Same logic as mcp_server.py tools, but without FastMCP decorators.

def _tool_list_agents(category: Optional[str] = None, query: Optional[str] = None) -> str:
    """List available expert agents. Filter by category or search by query."""
    reg = _get_registry()
    if query:
        agents = reg.search_agents(query)
    elif category:
        from integrations.expert_agents.registry import AgentCategory
        cat_map = {name.lower(): member for name, member in AgentCategory.__members__.items()}
        cat = cat_map.get(category.lower())
        if not cat:
            return json.dumps({"error": f"Unknown category: {category}"})
        agents = reg.get_agents_by_category(cat)
    else:
        agents = list(reg.agents.values())

    result = []
    for a in agents:
        result.append({
            "agent_id": a.agent_id, "name": a.name,
            "category": a.category.name if hasattr(a.category, 'name') else str(a.category),
            "description": a.description, "model_type": a.model_type,
        })

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
                })
            except Exception:
                pass

    return json.dumps({"expert_agents": len(result), "dynamic_agents": len(dynamic),
                       "agents": result[:50], "dynamic": dynamic[:20]}, indent=2)


def _tool_list_goals(goal_type: Optional[str] = None, status: Optional[str] = None) -> str:
    """List agent goals. Filter by type or status."""
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


def _tool_create_goal(goal_type: str, title: str, description: str = '', spark_budget: int = 200) -> str:
    """Create a new goal for agents to pursue."""
    try:
        from integrations.agent_engine.goal_manager import GoalManager
        db = _get_db()
        try:
            result = GoalManager.create_goal(db, goal_type=goal_type, title=title,
                                             description=description, spark_budget=spark_budget)
            db.commit()
            return json.dumps(result, indent=2, default=str)
        finally:
            db.close()
    except Exception as e:
        return json.dumps({"error": str(e)})


def _tool_agent_status() -> str:
    """Check agent daemon health, active dispatches, and system state."""
    from core.port_registry import get_port
    from core.http_pool import pooled_get
    status = {
        "daemon_enabled": os.environ.get('HEVOLVE_AGENT_ENGINE_ENABLED', 'false'),
        "poll_interval": int(os.environ.get('HEVOLVE_AGENT_POLL_INTERVAL', '30')),
    }
    try:
        resp = pooled_get(f'http://localhost:{get_port("llm")}/health', timeout=2)
        status['llm_server'] = 'running' if resp.status_code == 200 else f'status {resp.status_code}'
    except Exception:
        status['llm_server'] = 'not reachable'
    try:
        reg = _get_registry()
        status['expert_agents'] = len(reg.agents)
    except Exception:
        status['expert_agents'] = 'unknown'
    return json.dumps(status, indent=2, default=str)


def _tool_remember(content: str, memory_type: str = 'decision') -> str:
    """Store a memory in the persistent memory graph."""
    try:
        mg = _get_memory_graph()
        memory_id = mg.register(content=content,
                                metadata={'memory_type': memory_type, 'source_agent': 'mcp_bridge'})
        return json.dumps({"stored": True, "memory_id": memory_id})
    except Exception as e:
        return json.dumps({"error": str(e)})


def _tool_recall(query: str, top_k: int = 5) -> str:
    """Search the persistent memory graph."""
    try:
        mg = _get_memory_graph()
        memories = mg.recall(query=query, mode='hybrid', top_k=top_k)
        result = []
        for m in memories:
            result.append({
                "id": m.id, "content": m.content,
                "memory_type": m.memory_type, "source_agent": m.source_agent,
                "created_at": m.created_at,
            })
        return json.dumps({"count": len(result), "memories": result}, indent=2, default=str)
    except Exception as e:
        return json.dumps({"error": str(e)})


def _tool_list_recipes() -> str:
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


def _tool_system_health() -> str:
    """Full system health check: Flask server, LLM, DB, memory graph."""
    from core.port_registry import get_port
    from core.http_pool import pooled_get
    health = {}
    try:
        resp = pooled_get(f'http://localhost:{get_port("backend")}/status', timeout=2)
        health['backend'] = {'status': 'up', 'code': resp.status_code}
    except Exception:
        health['backend'] = {'status': 'down'}
    try:
        resp = pooled_get(f'http://localhost:{get_port("llm")}/health', timeout=2)
        health['llm'] = {'status': 'up', 'code': resp.status_code}
    except Exception:
        health['llm'] = {'status': 'down'}
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
    return json.dumps(health, indent=2, default=str)


def _tool_social_query(query_type: str, limit: int = 20) -> str:
    """Read-only social DB queries. Types: users, posts, goals, products, agents."""
    try:
        db = _get_db()
        try:
            if query_type == 'users':
                from integrations.social.models import User
                rows = db.query(User).order_by(User.created_at.desc()).limit(limit).all()
                return json.dumps([{"id": r.id, "username": r.username,
                                    "display_name": r.display_name} for r in rows], default=str)
            elif query_type == 'goals':
                from integrations.agent_engine.goal_manager import GoalManager
                goals = GoalManager.list_goals(db)
                return json.dumps({"count": len(goals), "goals": goals[:limit]}, default=str)
            else:
                return json.dumps({"error": f"Unknown query_type: {query_type}"})
        finally:
            db.close()
    except Exception as e:
        return json.dumps({"error": str(e)})


# ── Watchdog & Monitoring Tools (read-only, no bypass) ─────────

def _tool_watchdog_status() -> str:
    """Get NodeWatchdog status — all monitored daemon threads, heartbeat ages, frozen/dead status."""
    try:
        from security.node_watchdog import get_watchdog
        wd = get_watchdog()
        if wd is None:
            return json.dumps({"status": "not_started", "threads": {}})
        return json.dumps(wd.get_status(), indent=2, default=str)
    except Exception as e:
        return json.dumps({"error": str(e)})


def _tool_exception_report() -> str:
    """Get recent exception patterns — grouped by type, count, and recency. Use this to find bugs to fix."""
    try:
        from exception_collector import ExceptionCollector
        collector = ExceptionCollector.instance()
        if collector is None:
            return json.dumps({"error": "ExceptionCollector not initialized"})
        import time
        day_ago = time.time() - 86400
        patterns = collector.get_patterns(since=day_ago, min_count=1)
        result = []
        for key, records in patterns.items():
            result.append({
                "pattern": key,
                "count": len(records),
                "first_seen": records[0].timestamp if records else None,
                "last_seen": records[-1].timestamp if records else None,
                "sample_traceback": records[-1].traceback_str[:500] if records else '',
                "file": records[-1].filename if records else '',
                "line": records[-1].lineno if records else 0,
            })
        result.sort(key=lambda x: x['count'], reverse=True)
        return json.dumps({"total_patterns": len(result), "exceptions": result[:20]},
                          indent=2, default=str)
    except Exception as e:
        return json.dumps({"error": str(e)})


def _tool_runtime_integrity() -> str:
    """Check runtime integrity monitor — code tampering detection, guardrail hash verification."""
    try:
        from security.runtime_monitor import get_monitor
        mon = get_monitor()
        if mon is None:
            return json.dumps({"status": "not_started"})
        return json.dumps({
            "running": mon._running,
            "tampered": mon._tampered,
            "check_interval": mon._check_interval,
        })
    except Exception as e:
        return json.dumps({"error": str(e)})


# ── Universal HARTOS API Gateway ───────────────────────────────

def _tool_call_endpoint(method: str, path: str, body: Optional[str] = None) -> str:
    """Call any HARTOS API endpoint. This gives access to ALL channels, handlers, and services.

    Examples:
        call_endpoint("GET", "/status")
        call_endpoint("POST", "/chat", '{"user_id":"1","prompt_id":"demo","prompt":"hello"}')
        call_endpoint("GET", "/api/social/communities")
        call_endpoint("POST", "/api/social/posts", '{"title":"Hello","content":"World"}')
        call_endpoint("GET", "/api/mcp/local/tools/list")
        call_endpoint("GET", "/prompts")
        call_endpoint("POST", "/api/instructions/enqueue", '{"user_id":"1","text":"research AI"}')

    Available route prefixes:
        /chat, /status, /prompts — core agent pipeline
        /api/social/* — 82 social endpoints (posts, communities, feeds, karma, encounters)
        /api/mcp/* — MCP server management
        /api/instructions/* — instruction queue
        /api/settings/* — compute/provider settings
        /api/credentials/* — credential management
        /a2a/* — agent-to-agent protocol
    """
    try:
        from flask import current_app
        app = current_app._get_current_object()
    except RuntimeError:
        # Not in request context — import app directly
        try:
            from hart_intelligence_entry import app
        except ImportError:
            try:
                from hart_intelligence import app
            except ImportError:
                return json.dumps({"error": "HARTOS app not available"})

    if not path.startswith('/'):
        path = '/' + path

    method = method.upper()
    parsed_body = None
    if body:
        try:
            parsed_body = json.loads(body)
        except json.JSONDecodeError:
            return json.dumps({"error": f"Invalid JSON body: {body[:200]}"})

    try:
        with app.test_client() as client:
            if method == 'GET':
                resp = client.get(path)
            elif method == 'POST':
                resp = client.post(path, json=parsed_body, content_type='application/json')
            elif method == 'PUT':
                resp = client.put(path, json=parsed_body, content_type='application/json')
            elif method == 'PATCH':
                resp = client.patch(path, json=parsed_body, content_type='application/json')
            elif method == 'DELETE':
                resp = client.delete(path)
            else:
                return json.dumps({"error": f"Unsupported method: {method}"})

            result = resp.get_json(silent=True)
            if result is not None:
                return json.dumps({"status": resp.status_code, "data": result}, default=str)
            return json.dumps({"status": resp.status_code, "text": resp.get_data(as_text=True)[:2000]})
    except Exception as e:
        return json.dumps({"error": str(e)})


def _tool_list_channels() -> str:
    """List all available channel adapters and their status."""
    try:
        from integrations.channels.extensions import get_available_adapters
        adapters = get_available_adapters()
        channels = []
        for name, factory in adapters.items():
            channels.append({"name": name, "type": "extension"})
        # Also check core adapters
        core_adapters = ['discord', 'telegram', 'slack', 'whatsapp', 'signal', 'web', 'google_chat']
        for name in core_adapters:
            if name not in [c['name'] for c in channels]:
                try:
                    mod = __import__(f'integrations.channels.{name}_adapter', fromlist=['_'])
                    channels.append({"name": name, "type": "core"})
                except ImportError:
                    pass
        return json.dumps({"count": len(channels), "channels": channels}, indent=2)
    except Exception as e:
        return json.dumps({"error": str(e)})


def _tool_list_routes() -> str:
    """List all registered Flask routes — shows every endpoint Claude Code can call via call_endpoint."""
    try:
        from flask import current_app
        app = current_app._get_current_object()
    except RuntimeError:
        try:
            from hart_intelligence_entry import app
        except ImportError:
            return json.dumps({"error": "HARTOS app not available"})

    routes = []
    for rule in app.url_map.iter_rules():
        if rule.endpoint == 'static':
            continue
        routes.append({
            "path": rule.rule,
            "methods": sorted(list(rule.methods - {'HEAD', 'OPTIONS'})),
        })
    routes.sort(key=lambda r: r['path'])
    return json.dumps({"count": len(routes), "routes": routes}, indent=2)


# ── Tool Loading ──────────────────────────────────────────────

def _load_tools():
    """Register all local MCP tool functions."""
    global _tools_loaded
    if _tools_loaded:
        return
    _tools_loaded = True

    # Read-only: observe the system
    _register_tool('list_agents', 'List available expert agents', _tool_list_agents)
    _register_tool('list_goals', 'List agent goals', _tool_list_goals)
    _register_tool('agent_status', 'Check agent daemon health', _tool_agent_status)
    _register_tool('list_recipes', 'List trained agent recipes', _tool_list_recipes)
    _register_tool('system_health', 'Full system health check', _tool_system_health)
    _register_tool('social_query', 'Read-only social DB queries', _tool_social_query)

    # Memory (safe — memory graph only, no framework bypass)
    _register_tool('remember', 'Store a memory in the memory graph', _tool_remember)
    _register_tool('recall', 'Search the persistent memory graph', _tool_recall)

    # Framework gateway — ALL writes go through Flask routes (guardrails, constitution, budget gate)
    _register_tool('call_endpoint', 'Call any HARTOS API endpoint through the framework', _tool_call_endpoint)
    _register_tool('list_routes', 'List all registered Flask routes', _tool_list_routes)
    _register_tool('list_channels', 'List all available channel adapters', _tool_list_channels)

    # Watchdog & monitoring (read-only)
    _register_tool('watchdog_status', 'Get daemon thread health — frozen/dead detection', _tool_watchdog_status)
    _register_tool('exception_report', 'Get recent exception patterns — find bugs to fix', _tool_exception_report)
    _register_tool('runtime_integrity', 'Check code tampering and guardrail hash verification', _tool_runtime_integrity)

    # ── Hive Meta-Orchestrator Tools ─────────────────────────────
    # These let Claude Code drive the entire hive as a meta-network

    def _tool_onboard_model(model: str, quant: str = 'auto'):
        """Onboard a HuggingFace model: find GGUF, download, start llama.cpp, register.
        Example: onboard_model(model='Qwen/Qwen3-8B', quant='Q4_K_M')"""
        try:
            from integrations.service_tools.model_onboarding import onboard
            return onboard(model, quant=quant)
        except Exception as e:
            return {'status': 'error', 'error': str(e)}

    def _tool_switch_model(model: str, quant: str = 'auto'):
        """Hot-swap the active LLM to a different model (downloads if needed)."""
        try:
            from integrations.service_tools.model_onboarding import switch_model
            return switch_model(model, quant=quant)
        except Exception as e:
            return {'status': 'error', 'error': str(e)}

    def _tool_model_status():
        """Get active model, server health, VRAM usage, downloaded models."""
        try:
            from integrations.service_tools.model_onboarding import status
            return status()
        except Exception as e:
            return {'status': 'error', 'error': str(e)}

    def _tool_hive_connect(user_id: str, task_scope: str = 'own_repos'):
        """Connect this Claude Code session to the hive as a coding worker node.
        task_scope: own_repos | public | any"""
        try:
            from integrations.coding_agent.claude_hive_session import get_hive_session
            session = get_hive_session()
            return session.connect(user_id, task_scope=task_scope)
        except Exception as e:
            return {'status': 'error', 'error': str(e)}

    def _tool_hive_disconnect():
        """Disconnect this Claude Code session from the hive."""
        try:
            from integrations.coding_agent.claude_hive_session import get_hive_session
            return get_hive_session().disconnect()
        except Exception as e:
            return {'status': 'error', 'error': str(e)}

    def _tool_hive_session_status():
        """Get hive session status: connected, tasks completed, spark earned."""
        try:
            from integrations.coding_agent.claude_hive_session import get_hive_session
            return get_hive_session().get_status()
        except Exception as e:
            return {'status': 'error', 'error': str(e)}

    def _tool_create_hive_task(task_type: str, title: str, description: str, instructions: str):
        """Create a coding task for the hive. Types: code_review, code_write, code_test,
        model_onboard, benchmark, documentation, bug_fix, refactor"""
        try:
            from integrations.coding_agent.hive_task_protocol import get_dispatcher
            task = get_dispatcher().create_task(task_type, title, description, instructions)
            return task.to_dict()
        except Exception as e:
            return {'status': 'error', 'error': str(e)}

    def _tool_dispatch_hive_tasks():
        """Dispatch all pending hive tasks to available Claude Code sessions. Returns count dispatched."""
        try:
            from integrations.coding_agent.hive_task_protocol import get_dispatcher
            count = get_dispatcher().dispatch_pending()
            return {'dispatched': count}
        except Exception as e:
            return {'status': 'error', 'error': str(e)}

    def _tool_hive_signal_stats():
        """Get channel signal statistics: signal counts by type, by channel, total processed."""
        try:
            from integrations.channels.hive_signal_bridge import get_signal_bridge
            return get_signal_bridge().get_stats()
        except Exception as e:
            return {'status': 'error', 'error': str(e)}

    def _tool_hive_signal_feed(limit: int = 20):
        """Get recent hive signals from all channels — what the community is talking about."""
        try:
            from integrations.channels.hive_signal_bridge import get_signal_bridge
            return get_signal_bridge().get_signal_feed(limit=int(limit))
        except Exception as e:
            return {'status': 'error', 'error': str(e)}

    def _tool_seed_goals():
        """Seed all bootstrap goals (47 agents including 6 hive acceleration agents). Idempotent."""
        try:
            from integrations.social.models import get_db
            from integrations.agent_engine.goal_seeding import seed_bootstrap_goals
            db = get_db()
            try:
                count = seed_bootstrap_goals(db)
                db.commit()
                return {'seeded': count, 'status': 'ok'}
            finally:
                db.close()
        except Exception as e:
            return {'status': 'error', 'error': str(e)}

    # Register hive orchestrator tools
    _register_tool('onboard_model', 'Download HF model → GGUF → start llama.cpp inference', _tool_onboard_model)
    _register_tool('switch_model', 'Hot-swap active LLM to a different model', _tool_switch_model)
    _register_tool('model_status', 'Active model, server health, VRAM, downloads', _tool_model_status)
    _register_tool('hive_connect', 'Connect Claude Code session to hive as worker node', _tool_hive_connect)
    _register_tool('hive_disconnect', 'Disconnect Claude Code session from hive', _tool_hive_disconnect)
    _register_tool('hive_session_status', 'Hive session: connected, tasks done, spark earned', _tool_hive_session_status)
    _register_tool('create_hive_task', 'Create a coding task for hive Claude Code sessions', _tool_create_hive_task)
    _register_tool('dispatch_hive_tasks', 'Dispatch pending tasks to available sessions', _tool_dispatch_hive_tasks)
    _register_tool('hive_signal_stats', 'Channel signal stats: what the community needs', _tool_hive_signal_stats)
    _register_tool('hive_signal_feed', 'Recent signals from all 30 channels', _tool_hive_signal_feed)
    _register_tool('seed_goals', 'Seed all 47 bootstrap agents (idempotent)', _tool_seed_goals)

    logger.info(f"MCP HTTP bridge loaded {len(_local_tools)} local tools")


# ── REST Endpoints ─────────────────────────────────────────────

@mcp_local_bp.route('/health', methods=['GET'])
def mcp_health():
    """Health check for the local MCP bridge."""
    _load_tools()
    return jsonify({
        "status": "ok",
        "tools": len(_local_tools),
        "server": "hartos-mcp-local",
    })


@mcp_local_bp.route('/tools/list', methods=['GET'])
def mcp_list_tools():
    """List all locally available MCP tools with their schemas."""
    _load_tools()
    tools_out = []
    for t in _local_tools:
        tools_out.append({
            "name": t["name"],
            "description": t["description"],
            "parameters": t["parameters"],
        })
    return jsonify({"tools": tools_out})


@mcp_local_bp.route('/tools/execute', methods=['POST'])
def mcp_execute_tool():
    """Execute a local MCP tool.

    Request body: {"tool": "tool_name", "arguments": {"key": "value"}}
    """
    _load_tools()
    data = request.get_json(force=True, silent=True) or {}
    tool_name = data.get('tool', '').strip()
    arguments = data.get('arguments', {})

    if not tool_name:
        return jsonify({"success": False, "error": "tool name required"}), 400

    tool_entry = None
    for t in _local_tools:
        if t["name"] == tool_name:
            tool_entry = t
            break

    if tool_entry is None:
        available = [t["name"] for t in _local_tools]
        return jsonify({
            "success": False,
            "error": f"Unknown tool: {tool_name}",
            "available_tools": available,
        }), 404

    fn = tool_entry["fn"]
    if fn is None:
        return jsonify({"success": False, "error": f"Tool {tool_name} has no callable"}), 500

    try:
        result = fn(**arguments)
        if isinstance(result, str):
            try:
                parsed = json.loads(result)
                return jsonify({"success": True, "result": parsed})
            except json.JSONDecodeError:
                return jsonify({"success": True, "result": result})
        return jsonify({"success": True, "result": result})
    except TypeError as e:
        return jsonify({"success": False, "error": f"Invalid arguments: {e}"}), 400
    except Exception as e:
        logger.error(f"MCP tool {tool_name} execution error: {e}")
        return jsonify({"success": False, "error": str(e)}), 500


# ── Auto-registration ─────────────────────────────────────────

def auto_register_local_mcp():
    """Register the local HARTOS MCP server in the MCPToolRegistry.

    Called at boot so Nunba's MCPServerConnector auto-discovers local tools.
    Uses the backend port since tools are served from the same Flask app.
    """
    try:
        from core.port_registry import get_port
        from integrations.mcp.mcp_integration import mcp_registry, MCPServerConnector

        backend_port = get_port('backend')
        local_url = f"http://127.0.0.1:{backend_port}/api/mcp/local"

        if 'hartos_local' not in mcp_registry.servers:
            connector = MCPServerConnector('hartos_local', local_url)
            connector.connected = True  # We are the server, skip health check
            mcp_registry.servers['hartos_local'] = connector
            logger.info(f"Auto-registered local MCP server at {local_url}")
    except Exception as e:
        logger.debug(f"Auto-register local MCP failed (non-critical): {e}")
