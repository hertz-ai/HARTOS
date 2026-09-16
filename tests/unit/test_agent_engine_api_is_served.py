"""The agent-engine ledger API must actually be SERVED, not merely exist.

THE DEFECT, measured on the box 2026-09-09. integrations/mcp/_tool_impls.py
documents a known false negative: probe_agent_daemon() reads _running /
_tick_count off an imported module, and when a different agent_daemon instance
is resolved than the live one it reports `daemon_enabled=false, _tick_count=0`
while the daemon is ticking normally. On HART OS that is not even a Python
module shadow -- :6777 is hart-backend (pid 1263) while the daemon lives in
hart-nunba (pid 1268), two separate processes.

Its defence is to ALSO fetch canonical ledger stats over Flask loopback,
"shadow-immune" because the request lands on whichever singleton Flask
actually resolved. That defence had never been able to run: agent_engine_bp
was registered NOWHERE. `list_routes` on the live backend returned 829 routes
and zero agent-engine, and /api/agent-engine/ledger/stats 404'd on :6777 AND
on the nunba socket. So the corrective probe 404'd, the unreliable
module-attr view was the only view, and the agent engine was structurally
unobservable from outside its own process.
"""
import os
import re

from flask import Flask

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def test_the_blueprint_serves_the_probe_endpoint():
    """Behavioural: register the REAL blueprint and look for the route the MCP
    daemon probe fetches by name."""
    from integrations.agent_engine import get_engine_blueprint
    app = Flask(__name__)
    app.register_blueprint(get_engine_blueprint())
    rules = {r.rule for r in app.url_map.iter_rules()}
    assert '/api/agent-engine/ledger/stats' in rules, (
        'the shadow-immune daemon probe fetches this exact path')


def test_the_probe_endpoint_answers_rather_than_404ing():
    """A registered route that raises is no better than a missing one: the
    probe treats any non-200 as unavailable and falls back to the view it
    cannot trust."""
    from integrations.agent_engine import get_engine_blueprint
    app = Flask(__name__)
    app.register_blueprint(get_engine_blueprint())
    r = app.test_client().get('/api/agent-engine/ledger/stats')
    assert r.status_code != 404, 'the route must be served'


def test_the_backend_entry_registers_it():
    """Source-shape, deliberately: importing hart_intelligence_entry pulls the
    whole backend (langchain, chromadb, autogen), which is not a unit test. The
    behavioural half is covered above; this only pins that the entry WIRES it,
    which is the part that was missing on the box while the blueprint itself
    was fine."""
    p = os.path.join(REPO_ROOT, 'hart_intelligence_entry.py')
    with open(p, encoding='utf-8') as fh:
        src = fh.read()
    assert 'get_engine_blueprint' in src, (
        'the backend must register the agent-engine blueprint, or the MCP '
        'shadow-immune probe 404s and the daemon stays unobservable')
    # And it must be guarded like every neighbouring registration: a route-drop
    # must never take the whole app down.
    i = src.index('get_engine_blueprint')
    window = src[max(0, i - 400):i + 400]
    assert 'except ImportError' in window and 'register_blueprint' in window
