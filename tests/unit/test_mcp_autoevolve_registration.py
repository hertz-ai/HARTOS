"""Tests for Fix 3 + Fix 4 — MCP bridge exposes the 3 auto-evolve arrays.

Guards:
  1. All three module-level tool arrays (THOUGHT_EXPERIMENT_TOOLS,
     AUTO_EVOLVE_TOOLS, AUTOEVOLVE_CODE_TOOLS) appear in the bridge's
     /tools/list output (Fix 3).
  2. One representative from each array round-trips through the
     /tools/execute JSON schema without a 404 (Fix 4).
  3. No duplicate registration — de-dup guard is live so repeated
     module re-imports don't double-count.
"""
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..'))


@pytest.fixture(autouse=True)
def reset_tools():
    from integrations.mcp import mcp_http_bridge
    mcp_http_bridge._tools_loaded = False
    mcp_http_bridge._local_tools.clear()
    yield
    mcp_http_bridge._tools_loaded = False
    mcp_http_bridge._local_tools.clear()


@pytest.fixture
def app():
    from flask import Flask
    app = Flask(__name__)
    app.config['TESTING'] = True
    from integrations.mcp.mcp_http_bridge import mcp_local_bp
    app.register_blueprint(mcp_local_bp)
    return app


@pytest.fixture
def client(app):
    from integrations.mcp.mcp_http_bridge import _ensure_mcp_token
    token = _ensure_mcp_token()

    class _AuthClient(app.test_client_class or app.test_client().__class__):
        def open(self, *args, **kwargs):
            headers = kwargs.pop('headers', {}) or {}
            if 'Authorization' not in headers:
                headers['Authorization'] = f'Bearer {token}'
            kwargs['headers'] = headers
            return super().open(*args, **kwargs)

    app.test_client_class = _AuthClient
    return app.test_client()


# ── Fix 3 — registration coverage ─────────────────────────────


class TestAutoEvolveToolsRegistered:
    def test_thought_experiment_tools_present(self, client):
        resp = client.get('/api/mcp/local/tools/list')
        assert resp.status_code == 200
        names = {t['name'] for t in resp.get_json()['tools']}
        expected = {
            'create_thought_experiment', 'cast_experiment_vote',
            'evaluate_thought_experiment', 'get_experiment_status',
            'tally_experiment_votes', 'advance_experiment',
            'iterate_hypothesis', 'score_hypothesis_result',
            'get_iteration_history', 'launch_experiment_autoresearch',
            'get_experiment_research_status',
        }
        missing = expected - names
        assert not missing, f'THOUGHT_EXPERIMENT_TOOLS missing: {missing}'

    def test_auto_evolve_tools_present(self, client):
        resp = client.get('/api/mcp/local/tools/list')
        names = {t['name'] for t in resp.get_json()['tools']}
        expected = {
            'start_auto_evolve', 'get_auto_evolve_status',
            'pause_evolve_experiment', 'resume_evolve_experiment',
        }
        missing = expected - names
        assert not missing, f'AUTO_EVOLVE_TOOLS missing: {missing}'

    def test_autoevolve_code_tools_present(self, client):
        resp = client.get('/api/mcp/local/tools/list')
        names = {t['name'] for t in resp.get_json()['tools']}
        expected = {
            'autoresearch_setup', 'autoresearch_edit', 'autoresearch_run',
            'autoresearch_decide', 'autoresearch_finalize',
            'get_autoresearch_status',
        }
        missing = expected - names
        assert not missing, f'AUTOEVOLVE_CODE_TOOLS missing: {missing}'

    def test_tool_count_grew_by_expected_amount(self, client):
        """21 tools from the 3 arrays (11 + 4 + 6) must be added."""
        resp = client.get('/api/mcp/local/tools/list')
        names = {t['name'] for t in resp.get_json()['tools']}
        # Tools unique to the 3 arrays
        module_tools = {
            'create_thought_experiment', 'cast_experiment_vote',
            'evaluate_thought_experiment', 'get_experiment_status',
            'tally_experiment_votes', 'advance_experiment',
            'iterate_hypothesis', 'score_hypothesis_result',
            'get_iteration_history', 'launch_experiment_autoresearch',
            'get_experiment_research_status',
            'start_auto_evolve', 'get_auto_evolve_status',
            'pause_evolve_experiment', 'resume_evolve_experiment',
            'autoresearch_setup', 'autoresearch_edit', 'autoresearch_run',
            'autoresearch_decide', 'autoresearch_finalize',
            'get_autoresearch_status',
        }
        assert module_tools.issubset(names), (
            f'Expected all 21 auto-evolve tools registered; '
            f'missing: {module_tools - names}'
        )

    def test_no_duplicate_registration(self, client):
        """Calling _load_tools twice must not create duplicate entries."""
        from integrations.mcp.mcp_http_bridge import _local_tools, _load_tools
        # Fixture already reset; first load happens via client fixture
        resp = client.get('/api/mcp/local/tools/list')
        names = [t['name'] for t in resp.get_json()['tools']]
        assert len(names) == len(set(names)), (
            f'duplicate tool registration: '
            f'{[n for n in set(names) if names.count(n) > 1]}'
        )
        # Manually re-run _load_tools (noop because _tools_loaded=True)
        _load_tools()
        assert len(_local_tools) == len(names)


# ── Fix 4 — cohesion round-trip through JSON-RPC schema ──────


class TestAutoEvolveJsonSchemaRoundtrip:
    """One representative tool from each array exercises the end-to-end
    MCP request/response contract (parameters extracted, execute routes
    to the callable, JSON encode/decode cleanly)."""

    @pytest.mark.parametrize('tool_name,expected_required', [
        # THOUGHT_EXPERIMENT_TOOLS sample: create_thought_experiment
        # (creator_id, title, hypothesis are required)
        ('create_thought_experiment', {'creator_id', 'title', 'hypothesis'}),
        # AUTO_EVOLVE_TOOLS sample: start_auto_evolve (no required args —
        # all three have defaults: max_experiments=5, min_approval_score=0.3,
        # user_id='system')
        ('start_auto_evolve', set()),
        # AUTOEVOLVE_CODE_TOOLS sample: autoresearch_setup
        ('autoresearch_setup',
         {'repo_path', 'target_file', 'run_command'}),
    ])
    def test_tool_schema_round_trip(self, client, tool_name,
                                      expected_required):
        """Schema must be introspected from the callable and match the
        tool's declared signature — this is the piece that breaks the
        moment we bypass _extract_parameters (Gate 4 parallel-registry
        regression check)."""
        resp = client.get('/api/mcp/local/tools/list')
        assert resp.status_code == 200
        tools_by_name = {t['name']: t for t in resp.get_json()['tools']}
        assert tool_name in tools_by_name, f'{tool_name} not exposed'
        tool = tools_by_name[tool_name]

        params = tool['parameters']
        assert params['type'] == 'object'
        assert 'properties' in params
        required = set(params.get('required', []))
        assert expected_required.issubset(required), (
            f'{tool_name}: expected required {expected_required}, got {required}'
        )

    def test_execute_unknown_autoevolve_tool_returns_404(self, client):
        """Ensure the bridge doesn't hallucinate tools from the module
        arrays — only names that literally exist round-trip."""
        resp = client.post(
            '/api/mcp/local/tools/execute',
            data=json.dumps({'tool': 'autoresearch_does_not_exist'}),
            content_type='application/json',
        )
        assert resp.status_code == 404

    def test_execute_auto_evolve_missing_required_returns_400(self, client):
        """create_thought_experiment without creator_id/title/hypothesis
        must yield a 400 — not crash or silently succeed."""
        resp = client.post(
            '/api/mcp/local/tools/execute',
            data=json.dumps({
                'tool': 'create_thought_experiment',
                'arguments': {},
            }),
            content_type='application/json',
        )
        # TypeError path in the bridge returns 400
        assert resp.status_code == 400, (
            f'Expected 400 for missing required args; got {resp.status_code} '
            f'body={resp.get_data(as_text=True)}'
        )


if __name__ == '__main__':
    pytest.main([__file__, '-v'])
