"""Shared HARTOS MCP tool implementations — single source for both transports.

The stdio server (`mcp_server.py`, FastMCP) and the HTTP bridge
(`mcp_http_bridge.py`, Flask) historically carried byte-for-byte copies of
these tool bodies — the HTTP file's own comment even read "Same logic as
mcp_server.py tools, but without FastMCP decorators." Two copies always drift,
and they had: the HTTP `list_agents` dropped the valid-category list + dynamic
`model_type`; HTTP `agent_status` lacked the nunba_server probe + goals
breakdown; HTTP `social_query` handled only users/goals; HTTP `system_health`
keyed Flask as 'backend' not 'flask'.

This module holds the canonical (richest, most complete) version of each shared
tool. Both transports import and register these — keeping their OWN registration
mechanism (FastMCP decorator vs `_register_tool`), which is the legitimate
reason the two files exist (FastMCP pulls in a pydantic v2 stack the Flask
bridge must not import). These impls deliberately import NEITHER FastMCP NOR
Flask, so sharing them reintroduces no dependency conflict (#98c).

`remember` takes a `source_agent` argument because the provenance tag genuinely
differs per transport ('claude_orchestrator' for stdio, 'mcp_bridge' for HTTP);
each transport passes its own via a one-line wrapper, so the body is shared
while behaviour is preserved exactly.
"""
import os
import json
import glob as _glob
import logging
from pathlib import Path
from typing import Optional

logger = logging.getLogger('hartos_mcp.tools')

# ─── Lazy resources (deferred to avoid import-time side effects) ───
# These caches live here now (one home), shared by both transports.

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


# ─── Canonical tool implementations ───

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


def list_goals(goal_type: Optional[str] = None, status: Optional[str] = None) -> str:
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


def create_goal(goal_type: str, title: str, description: str = '', spark_budget: int = 200) -> str:
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


def agent_status() -> str:
    """Check agent daemon health, active dispatches, and system state.

    All probes flow through ``core.health_probe`` (single canonical
    source).  See that module's docstring for the root-cause notes
    on why we route through it instead of reading env vars directly.

    ── T2 fix (2026-06-09): MCP module-shadow defence ────────────────
    ``probe_agent_daemon()`` does ``from integrations.agent_engine
    .agent_daemon import agent_daemon`` and reads ``_running`` /
    ``_tick_count`` off the imported module.  When Python's import
    resolution returns a different module instance than the one the
    live Flask process started the daemon from (dual ``integrations/``
    locations in the Nunba install root + python-embed — both
    legitimate per ``feedback_hartos_bundle_srp.md``), the probe sees
    a fresh-zero singleton even though the real daemon is alive and
    ticking.  Today's session caught this: MCP reported
    ``daemon_enabled=false, _tick_count=0`` while the canonical Flask
    endpoint /api/agent-engine/ledger/stats showed 5265 historical
    tasks with active progression.

    Defence: also fetch the canonical ledger stats over Flask
    loopback HTTP and merge them in.  Loopback bypasses Python import
    ambiguity — the request hits whichever ``agent_daemon`` singleton
    Flask's ``sys.path`` resolves to, which is by definition the one
    that's actually doing the work.  Callers now see both views
    (module-attr + ledger truth) and can detect the shadow themselves
    by comparing.
    """
    from core.health_probe import (
        probe_agent_daemon, probe_llm, probe_nunba_flask,
    )
    status = probe_agent_daemon()
    status['nunba_server'] = probe_nunba_flask()
    status['llm_server'] = probe_llm()

    # ── Canonical ledger probe via Flask loopback ────────────────────
    # Shadow-immune source of truth.  If the HTTP probe fails (Flask
    # down, port mismatch), we still return the module-attr view so
    # the tool degrades gracefully — but mark the canonical view as
    # unavailable so callers know not to trust the shadow.
    try:
        import requests as _r
        from core.port_registry import get_port as _get_port
        _port = _get_port('nunba') if 'nunba' in dir() else 5000
        _resp = _r.get(
            f"http://127.0.0.1:{_port}/api/agent-engine/ledger/stats",
            timeout=3,
        )
        if _resp.status_code == 200:
            _data = _resp.json()
            _stats = _data.get('stats') or {}
            status['ledger'] = {
                'source': 'flask_loopback_canonical',
                'total_tasks': _stats.get('total'),
                'by_status': _stats.get('by_status'),
                'sessions': _stats.get('sessions'),
            }
        else:
            status['ledger'] = {
                'source': 'flask_loopback_canonical',
                'error': f'HTTP {_resp.status_code}',
            }
    except Exception as _le:
        status['ledger'] = {
            'source': 'flask_loopback_canonical',
            'error': f'probe failed: {_le}',
        }

    # Goal counts (DB query — kept inline; not a "probe" in the
    # health-check sense, this is a count-by-status aggregation).
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

    # ── Shadow-detection hint ────────────────────────────────────────
    # If the module-attr view says daemon is dead (tick_count=0) but
    # the canonical ledger view shows recent task activity, flag the
    # discrepancy so audits stop chasing the shadow.
    try:
        _ledger = status.get('ledger') or {}
        _ledger_total = (
            _ledger.get('total_tasks') if isinstance(_ledger, dict) else None
        )
        if (status.get('daemon_tick_count') == 0
                and isinstance(_ledger_total, int)
                and _ledger_total > 0):
            status['shadow_module_suspected'] = (
                "daemon_tick_count=0 but canonical ledger has "
                f"{_ledger_total} tasks — module-attr probe is reading "
                "a shadow singleton; trust the 'ledger' block instead."
            )
    except Exception:
        pass

    return json.dumps(status, indent=2, default=str)


def remember(content: str, memory_type: str = 'decision', source_agent: str = 'mcp') -> str:
    """Store a memory in the persistent memory graph. Types: fact, decision, insight, lifecycle.

    `source_agent` is the provenance tag and differs per transport — callers
    pass their own ('claude_orchestrator' for stdio, 'mcp_bridge' for HTTP).
    """
    try:
        mg = _get_memory_graph()
        memory_id = mg.register(
            content=content,
            metadata={'memory_type': memory_type, 'source_agent': source_agent},
        )
        return json.dumps({"stored": True, "memory_id": memory_id})
    except Exception as e:
        return json.dumps({"error": str(e)})


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


def system_health() -> str:
    """Full system health check: Flask server, LLM, DB, memory graph.

    All non-DB probes flow through ``core.health_probe`` (single
    canonical source).  See that module for why we no longer hit
    ``localhost:{get_port('llm')}/health`` directly.
    """
    from core.health_probe import probe_nunba_flask, probe_llm, probe_langchain
    health = {
        'flask': probe_nunba_flask(),
        'llm': probe_llm(),
        'langchain': probe_langchain(),
    }

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
