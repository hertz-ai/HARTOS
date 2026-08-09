"""Cross-node recipe REUSE over the A2A surface (peer_reuse).

Behavioural, two-node in-process harness: node A is a REAL Flask app
serving the REAL A2A routes (A2AProtocolServer.setup_routes) over a
tmp prompts dir holding a REAL recipe bundle; node B runs the REAL
outbound client (discover/pull/invoke/try_peer_recipe_reuse) with its
OWN tmp prompts dir. The only mock is the network boundary:
peer_reuse's pooled_get/pooled_post are routed into node A's
test_client, and core.platform_paths.get_recipe_prompts_dir resolves
to A's dir while a request executes inside A and to B's dir otherwise
(synchronous test_client makes that deterministic).

Covers the directive's four legs:
  (a) discover + pull: recipe lands in B's prompts dir byte-identical
      under the peer's advertised naming, plus the local-pid alias
  (b) daemon split: after the pull the REAL classifier signal
      (_flow_recipe_exists) flips to REUSE for the local prompt_id
  (c) no peers / flag off / peer error fall through to CREATE with
      zero network calls when the flag is off
  (d) invoke_peer_agent happy + failure envelopes

The JSON-RPC invoke tests use a minimal sync Flask app that serves
the exact wire envelopes A2ATask.to_dict / the jsonrpc route produce
(the real route is an async view; this env has no flask[async]).
"""
import asyncio
import json
import os
import sys
import time
from types import SimpleNamespace
from unittest.mock import patch
from urllib.parse import urlsplit

import pytest
from flask import Flask, jsonify

_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from integrations.google_a2a import peer_reuse  # noqa: E402
from integrations.google_a2a.google_a2a_integration import (  # noqa: E402
    A2AProtocolServer)
from integrations.agent_engine.agent_daemon import (  # noqa: E402
    _flow_recipe_exists, _try_peer_recipe_reuse)

PEER_PID = '77777777777'
PEER_URL = 'http://node-a:6777'
GOAL_SLUG = 'bootstrap_growth_analytics'
A_GOAL_MAP = {PEER_PID: {
    'goal_id': 'a-goal-uuid',
    'goal_slug': GOAL_SLUG,
    'goal_type': 'analytics',
    'goal_title': 'Growth Analytics',
}}


def _identity(**over):
    base = {
        'goal_id': 'b-goal-uuid',
        'goal_slug': GOAL_SLUG,
        'goal_type': 'analytics',
        'goal_title': 'Growth Analytics',
        'goal_description': 'Track growth metrics for the platform.',
        'owner_id': 'user-b',
    }
    base.update(over)
    return base


def _write_node_a_bundle(prompts_dir):
    """A REAL banked recipe bundle on node A (single-line JSON so the
    byte-identical assertion is newline-safe on Windows)."""
    pdef = {
        'status': 'completed',
        'name': 'Growth Metrics Analyst',
        'agent_name': 'auto.growth1',
        'broadcast_agent': False,
        'personas': [{'name': 'Analyst', 'description': 'Growth analyst'}],
        'flows': [{
            'flow_name': 'analytics_flow', 'persona': 'Analyst',
            'sub_goal': 'Track growth metrics',
            'actions': ['Collect metrics', 'Report'],
        }],
        'prompt_id': PEER_PID,
        'creator_user_id': 'user-a',
    }
    recipe = {'status': 'completed', 'actions': [{
        'status': 'done', 'action': 'Collect metrics',
        'persona': 'Analyst', 'action_id': 1,
        'recipe': [{'steps': 'Query metrics service',
                    'tool_name': 'get_growth_metrics',
                    'agent_to_perform_this_action': 'Helper'}],
        'can_perform_without_user_input': 'yes',
    }]}
    action = dict(recipe['actions'][0])
    for fname, obj in ((f'{PEER_PID}.json', pdef),
                       (f'{PEER_PID}_0_recipe.json', recipe),
                       (f'{PEER_PID}_0_1.json', action)):
        with open(os.path.join(prompts_dir, fname), 'w',
                  encoding='utf-8', newline='') as f:
            f.write(json.dumps(obj))


class _FakeResp:
    def __init__(self, flask_resp):
        self.status_code = flask_resp.status_code
        self._json = flask_resp.get_json(silent=True)

    def json(self):
        if self._json is None:
            raise ValueError('no json body')
        return self._json


class TwoNodeHarness:
    """Node A app + routed transport + per-node prompts dirs."""

    def __init__(self, tmp_path):
        self.a_dir = str(tmp_path / 'node_a_prompts')
        self.b_dir = str(tmp_path / 'node_b_prompts')
        os.makedirs(self.a_dir)
        os.makedirs(self.b_dir)
        _write_node_a_bundle(self.a_dir)

        app = Flask('node_a')
        server = A2AProtocolServer(app, PEER_URL)
        server.setup_routes()
        self.client = app.test_client()

        # get_recipe_prompts_dir resolves per-node: A inside a request,
        # B outside (test_client is synchronous, so this is exact).
        self._current = {'dir': self.b_dir}
        self.get_calls = []
        self.post_calls = []

    def prompts_dir(self):
        return self._current['dir']

    def routed_get(self, url, timeout=None, **kw):
        assert timeout is not None, 'peer_reuse must set explicit timeouts'
        self.get_calls.append(url)
        prev = self._current['dir']
        self._current['dir'] = self.a_dir
        try:
            return _FakeResp(self.client.get(urlsplit(url).path))
        finally:
            self._current['dir'] = prev

    def routed_post(self, url, json=None, timeout=None, **kw):
        assert timeout is not None, 'peer_reuse must set explicit timeouts'
        self.post_calls.append(url)
        prev = self._current['dir']
        self._current['dir'] = self.a_dir
        try:
            return _FakeResp(
                self.client.post(urlsplit(url).path, json=json))
        finally:
            self._current['dir'] = prev


@pytest.fixture()
def harness(tmp_path):
    h = TwoNodeHarness(tmp_path)
    with patch.object(peer_reuse, '_attempt_cooldown', {}), \
            patch.object(peer_reuse, '_goal_map_cache', (0.0, {})), \
            patch('core.platform_paths.get_recipe_prompts_dir',
                  side_effect=h.prompts_dir), \
            patch.object(peer_reuse, 'pooled_get', h.routed_get), \
            patch.object(peer_reuse, 'pooled_post', h.routed_post), \
            patch.object(peer_reuse, 'local_goal_identity_by_prompt_id',
                         lambda: dict(A_GOAL_MAP)):
        yield h


LOCAL_PID = '88888888888'


class TestDiscoverAndPull:
    def test_directory_lists_goal_linked_agent_with_identity(self, harness):
        found = peer_reuse.discover_peer_agent(
            _identity(), peers=[{'node_id': 'a', 'url': PEER_URL}])
        assert found is not None
        url, entry = found
        assert url == PEER_URL
        assert entry['agent_id'] == f'{PEER_PID}_0'
        assert entry['goal_slug'] == GOAL_SLUG
        assert entry['status'] == 'completed'

    def test_pull_banks_byte_identical_under_peer_naming(self, harness):
        assert peer_reuse.pull_recipe(PEER_URL, f'{PEER_PID}_0') is True
        for fname in (f'{PEER_PID}.json', f'{PEER_PID}_0_recipe.json',
                      f'{PEER_PID}_0_1.json'):
            src = open(os.path.join(harness.a_dir, fname), 'rb').read()
            dst = open(os.path.join(harness.b_dir, fname), 'rb').read()
            assert dst == src, f'{fname} not byte-identical after pull'

    def test_pull_with_local_pid_writes_alias_bundle(self, harness):
        assert peer_reuse.pull_recipe(
            PEER_URL, f'{PEER_PID}_0', local_prompt_id=LOCAL_PID) is True
        # Local alias exists under the LOCAL goal's naming
        assert os.path.exists(
            os.path.join(harness.b_dir, f'{LOCAL_PID}_0_recipe.json'))
        alias_def = json.load(open(
            os.path.join(harness.b_dir, f'{LOCAL_PID}.json'),
            encoding='utf-8'))
        assert alias_def['prompt_id'] == LOCAL_PID
        assert alias_def['name'] == 'Growth Metrics Analyst'
        # Alias recipe content matches the peer's (same proven steps)
        peer_recipe = json.load(open(
            os.path.join(harness.b_dir, f'{PEER_PID}_0_recipe.json'),
            encoding='utf-8'))
        alias_recipe = json.load(open(
            os.path.join(harness.b_dir, f'{LOCAL_PID}_0_recipe.json'),
            encoding='utf-8'))
        assert alias_recipe == peer_recipe

    def test_pull_rejects_tampered_checksum(self, harness):
        real_get = harness.routed_get

        def tampering_get(url, timeout=None, **kw):
            resp = real_get(url, timeout=timeout, **kw)
            if resp.status_code == 200 and 'recipe' in url:
                body = resp.json()
                key = f'{PEER_PID}_0_recipe.json'
                body['files'][key] = body['files'][key] + ' '
                resp._json = body
            return resp

        with patch.object(peer_reuse, 'pooled_get', tampering_get):
            assert peer_reuse.pull_recipe(PEER_URL, f'{PEER_PID}_0') is False
        assert not os.listdir(harness.b_dir)

    def test_export_refused_for_private_agent(self, harness):
        """Privacy: a recipe that is neither a hive-goal work product
        nor broadcast_agent opted-in is not served and not listed."""
        with patch.object(peer_reuse, 'local_goal_identity_by_prompt_id',
                          lambda: {}):
            resp = harness.routed_get(
                f'{PEER_URL}/a2a/{PEER_PID}_0/recipe', timeout=1)
            assert resp.status_code == 403
            listing = harness.routed_get(
                f'{PEER_URL}/a2a/agents', timeout=1)
            assert listing.json()['agents'] == []
        assert not os.listdir(harness.b_dir)


class TestDaemonSplit:
    def test_classifier_flips_to_reuse_after_pull(self, harness):
        """The REAL classifier signal: _flow_recipe_exists is False
        before the peer leg and True after, for the LOCAL prompt_id."""
        assert _flow_recipe_exists(LOCAL_PID) is False
        goal = SimpleNamespace(
            id='b-goal-uuid', goal_type='analytics',
            title='Growth Analytics',
            description='Track growth metrics for the platform.',
            config_json={'bootstrap_slug': GOAL_SLUG},
            owner_id='user-b', created_by=None)
        with patch.object(peer_reuse, 'admitted_peers',
                          lambda *a, **k: [{'node_id': 'a',
                                            'url': PEER_URL}]):
            verdict = _try_peer_recipe_reuse(
                goal, LOCAL_PID, time.monotonic() + 10.0)
        assert verdict == 'pulled'
        assert _flow_recipe_exists(LOCAL_PID) is True

    def test_daemon_helper_never_raises(self, harness):
        goal = SimpleNamespace(
            id='g', goal_type='analytics', title='t', description='d',
            config_json=None, owner_id=None, created_by=None)
        with patch.object(peer_reuse, 'try_peer_recipe_reuse',
                          side_effect=RuntimeError('boom')):
            assert _try_peer_recipe_reuse(
                goal, LOCAL_PID, time.monotonic() + 1.0) is None


class TestFallThroughToCreate:
    def test_flag_off_makes_zero_network_calls(self, harness):
        def bomb(*a, **k):
            raise AssertionError('network call while flag off')
        with patch.dict(os.environ,
                        {peer_reuse.PEER_REUSE_ENV: '0'}), \
                patch.object(peer_reuse, 'pooled_get', bomb), \
                patch.object(peer_reuse, 'pooled_post', bomb), \
                patch.object(peer_reuse, 'admitted_peers', bomb):
            assert peer_reuse.try_peer_recipe_reuse(
                _identity(), LOCAL_PID) is None

    def test_no_peers_falls_through(self, harness):
        with patch.object(peer_reuse, 'admitted_peers',
                          lambda *a, **k: []):
            assert peer_reuse.try_peer_recipe_reuse(
                _identity(), LOCAL_PID) is None
        assert harness.get_calls == []

    def test_peer_error_falls_through(self, harness):
        def down(url, timeout=None, **kw):
            raise ConnectionError('peer unreachable')
        with patch.object(peer_reuse, 'admitted_peers',
                          lambda *a, **k: [{'node_id': 'a',
                                            'url': PEER_URL}]), \
                patch.object(peer_reuse, 'pooled_get', down):
            assert peer_reuse.try_peer_recipe_reuse(
                _identity(), LOCAL_PID) is None
        assert not os.listdir(harness.b_dir)

    def test_no_match_falls_through(self, harness):
        with patch.object(peer_reuse, 'admitted_peers',
                          lambda *a, **k: [{'node_id': 'a',
                                            'url': PEER_URL}]):
            assert peer_reuse.try_peer_recipe_reuse(
                _identity(goal_slug='bootstrap_other_goal',
                          goal_title='Something Else'),
                LOCAL_PID) is None
        assert not os.listdir(harness.b_dir)

    def test_cooldown_blocks_repeat_attempts(self, harness):
        calls = []
        with patch.object(peer_reuse, 'admitted_peers',
                          lambda *a, **k: (calls.append(1), [])[1]):
            peer_reuse.try_peer_recipe_reuse(_identity(), LOCAL_PID)
            peer_reuse.try_peer_recipe_reuse(_identity(), LOCAL_PID)
        assert len(calls) == 1, 'second attempt inside cooldown hit peers'


def _jsonrpc_peer_app(result_envelope=None, rpc_error=None, status=200):
    """Minimal sync peer serving the exact jsonrpc wire envelopes the
    real (async) route produces."""
    app = Flask('fake_peer')

    @app.route('/a2a/<agent_id>/jsonrpc', methods=['POST'])
    def jsonrpc(agent_id):
        if rpc_error is not None:
            return jsonify({'jsonrpc': '2.0', 'error': rpc_error,
                            'id': None}), status
        return jsonify({'jsonrpc': '2.0', 'result': result_envelope,
                        'id': 'rpc-1'}), status
    return app


def _routed_post_for(app):
    client = app.test_client()

    def _post(url, json=None, timeout=None, **kw):
        assert timeout is not None
        return _FakeResp(client.post(urlsplit(url).path, json=json))
    return _post


class TestInvokePeerAgent:
    HAPPY = {
        'id': 'msg-1', 'contextId': 'ctx-1', 'state': 'completed',
        'timestamp': 1234.5,
        'usage_metadata': {'total_token_count': 0},
        'content': {'role': 'model',
                    'parts': [{'text': 'metrics collected'}]},
    }

    def test_happy_envelope_returned_with_text(self):
        app = _jsonrpc_peer_app(result_envelope=self.HAPPY)
        with patch.object(peer_reuse, 'pooled_post',
                          _routed_post_for(app)):
            result = peer_reuse.invoke_peer_agent(
                PEER_URL, f'{PEER_PID}_0', 'collect metrics')
        assert result is not None
        assert result['state'] == 'completed'
        assert peer_reuse._result_text(result) == 'metrics collected'

    def test_failed_state_envelope_is_returned_as_is(self):
        failed = dict(self.HAPPY, state='failed')
        failed.pop('content')
        failed['error'] = 'executor blew up'
        app = _jsonrpc_peer_app(result_envelope=failed)
        with patch.object(peer_reuse, 'pooled_post',
                          _routed_post_for(app)):
            result = peer_reuse.invoke_peer_agent(
                PEER_URL, f'{PEER_PID}_0', 'collect metrics')
        assert result is not None and result['state'] == 'failed'

    def test_jsonrpc_error_envelope_returns_none(self):
        app = _jsonrpc_peer_app(
            rpc_error={'code': -32602, 'message': 'Agent not found'},
            status=404)
        with patch.object(peer_reuse, 'pooled_post',
                          _routed_post_for(app)):
            assert peer_reuse.invoke_peer_agent(
                PEER_URL, 'nope_0', 'x') is None

    def test_transport_failure_returns_none(self):
        def down(url, json=None, timeout=None, **kw):
            raise ConnectionError('unreachable')
        with patch.object(peer_reuse, 'pooled_post', down):
            assert peer_reuse.invoke_peer_agent(
                PEER_URL, f'{PEER_PID}_0', 'x') is None

    def test_pull_refused_falls_back_to_invoke_and_records(self, harness):
        """Export 403 (private agent on A) but jsonrpc works: the
        orchestration remote-invokes and records the outcome."""
        recorded = []

        class _Bridge:
            def record_interaction(self, **kw):
                recorded.append(kw)

        happy_post = _routed_post_for(_jsonrpc_peer_app(
            result_envelope=self.HAPPY))

        real_get = harness.routed_get

        def get_with_refused_export(url, timeout=None, **kw):
            if url.endswith('/recipe'):
                app = Flask('refuse')

                @app.route('/x')
                def x():
                    return jsonify({'error': 'export_refused'}), 403
                return _FakeResp(app.test_client().get('/x'))
            return real_get(url, timeout=timeout, **kw)

        with patch.object(peer_reuse, 'admitted_peers',
                          lambda *a, **k: [{'node_id': 'a',
                                            'url': PEER_URL}]), \
                patch.object(peer_reuse, 'pooled_get',
                             get_with_refused_export), \
                patch.object(peer_reuse, 'pooled_post', happy_post), \
                patch('integrations.agent_engine.world_model_bridge.'
                      'get_world_model_bridge', lambda: _Bridge()):
            verdict = peer_reuse.try_peer_recipe_reuse(
                _identity(), LOCAL_PID, deadline=time.monotonic() + 30.0)
        assert verdict == 'invoked'
        assert len(recorded) == 1
        assert recorded[0]['prompt_id'] == LOCAL_PID
        assert recorded[0]['response'] == 'metrics collected'
        assert not os.path.exists(
            os.path.join(harness.b_dir, f'{LOCAL_PID}_0_recipe.json'))


# ---------------------------------------------------------------------------
# Server-side A2A route gates (google_a2a_integration.setup_routes).
#
# These exercise the REAL Flask routes an untrusted peer can reach with no
# credentials:
#   - GET  /a2a/<agent_id>/recipe   (sync)  -> _safe_filename traversal gate
#   - POST /a2a/<agent_id>/jsonrpc  (async) -> unknown-agent / unknown-method
#                                              routing + unauthenticated exec
# The recipe route runs through the full werkzeug stack via test_client so
# the "'..' after URL-decode" case is genuinely decoded by the router, not
# hand-fed. The jsonrpc route is an ``async def`` view and this env has no
# flask[async] (asgiref absent), so it is driven by running the REAL route
# coroutine (``app.view_functions['handle_jsonrpc']``) under asyncio.run
# inside a real request context; the only mock is the agent-executor
# boundary (a spy coroutine).
# ---------------------------------------------------------------------------


def _bare_a2a_app():
    app = Flask('a2a_route_test')
    server = A2AProtocolServer(app, 'http://node:6777')
    return app, server


class TestRecipeExportTraversalGate:
    """/a2a/<agent_id>/recipe derives prompt_id from the untrusted URL
    segment; _safe_filename is the only barrier between a '..' that
    survived URL-decode and an arbitrary-path recipe read. Fail-closed
    means such a request must never return a 200 recipe body."""

    def _client(self):
        app, server = _bare_a2a_app()
        server.setup_routes()
        return app.test_client()

    def test_dotdot_surviving_urldecode_is_rejected_400(self):
        # %2e%2e decodes to the single segment '..' -> the exact
        # "Flask permits '..' after URL-decode" case the route docstring
        # warns about. It reaches the handler and must hit the gate.
        r = self._client().get('/a2a/%2e%2e/recipe')
        assert r.status_code == 400
        assert r.get_json()['error'] == 'unsafe agent_id'

    def test_double_encoded_dotdot_rejected_400(self):
        # ..%252f..%252fx -> literal '..%2f..%2fx' as the agent_id;
        # startswith('.') trips the gate before any disk access.
        r = self._client().get('/a2a/..%252f..%252fx/recipe')
        assert r.status_code == 400
        assert r.get_json()['error'] == 'unsafe agent_id'

    def test_dotfile_agent_id_rejected_400(self):
        r = self._client().get('/a2a/.hidden/recipe')
        assert r.status_code == 400
        assert r.get_json()['error'] == 'unsafe agent_id'

    def test_windows_drive_prefix_rejected_400(self):
        # agent_id 'C:evil' -> prompt_id 'C:evil'; os.path.join silently
        # honors a drive-relative anchor, so the gate must reject it.
        r = self._client().get('/a2a/C:evil/recipe')
        assert r.status_code == 400
        assert r.get_json()['error'] == 'unsafe agent_id'

    def test_encoded_slash_traversal_never_returns_recipe(self):
        # ..%2f..%2fsecret carries embedded slashes; werkzeug refuses to
        # bind it to the single-segment <agent_id> converter (404). Either
        # way it is fail-closed: never a 200 recipe body reaches the peer.
        r = self._client().get('/a2a/..%2f..%2fsecret/recipe')
        assert r.status_code in (400, 404)
        assert r.status_code != 200


class TestJsonRpcRouteUnauthenticated:
    """The jsonrpc route carries NO auth: any caller can drive a
    registered agent's executor. Pin the routing gates (unknown agent,
    unknown method), document the unauthenticated execution path, and
    guard the error handler against its own crash on malformed input."""

    def _server_with_agent(self):
        app, server = _bare_a2a_app()
        calls = []

        async def spy(text, ctx):
            calls.append((text, ctx))
            return {'role': 'model', 'parts': [{'text': f'ran:{text}'}]}

        server.register_agent('agentX_0', 'X', 'd', [{'id': 's'}], spy)
        server.setup_routes()
        return app, app.view_functions['handle_jsonrpc'], calls

    @staticmethod
    def _run(app, view, agent_id, *, json_body=None, raw_data=None,
             content_type=None):
        kw = {}
        if json_body is not None:
            kw['json'] = json_body
        if raw_data is not None:
            kw['data'] = raw_data
            kw['content_type'] = content_type or 'text/plain'
        with app.test_request_context(
                f'/a2a/{agent_id}/jsonrpc', method='POST', **kw):
            resp = asyncio.run(view(agent_id))
            status = resp[1] if isinstance(resp, tuple) else 200
            body = (resp[0] if isinstance(resp, tuple) else resp).get_json()
        return status, body

    def test_unknown_agent_returns_404_envelope(self):
        app, view, calls = self._server_with_agent()
        status, body = self._run(
            app, view, 'ghost',
            json_body={'method': 'message/send', 'params': {}, 'id': '1'})
        assert status == 404
        assert body['error']['code'] == -32602
        assert 'ghost' in body['error']['message']
        assert body['id'] is None
        assert calls == []

    def test_unknown_method_returns_400_envelope(self):
        app, view, calls = self._server_with_agent()
        status, body = self._run(
            app, view, 'agentX_0',
            json_body={'method': 'evil/exec', 'params': {}, 'id': '9'})
        assert status == 400
        assert body['error']['code'] == -32601
        assert 'evil/exec' in body['error']['message']
        assert body['id'] == '9'
        assert calls == []  # unknown method must not touch the executor

    def test_missing_method_field_is_unknown_method_400(self):
        app, view, _ = self._server_with_agent()
        status, body = self._run(
            app, view, 'agentX_0',
            json_body={'params': {}, 'id': '3'})
        assert status == 400
        assert body['error']['code'] == -32601

    def test_message_send_executes_agent_unauthenticated(self):
        # No token, no signature: the untrusted 'text' part drives the
        # registered executor and the completed envelope is returned.
        # This documents the current (auth-free) execution surface.
        app, view, calls = self._server_with_agent()
        status, body = self._run(
            app, view, 'agentX_0',
            json_body={'method': 'message/send', 'id': '7', 'params': {
                'message': {'messageId': 'm1',
                            'parts': [{'type': 'text',
                                       'text': 'attacker input'}]}}})
        assert status == 200
        result = body['result']
        assert result['state'] == 'completed'
        assert result['content']['parts'][0]['text'] == 'ran:attacker input'
        assert calls == [('attacker input', result['contextId'])]
        assert body['id'] == '7'

    def test_malformed_body_returns_clean_jsonrpc_error_not_crash(self):
        # request.json raises (415 UnsupportedMediaType) inside the try.
        # The except handler must still emit a JSON-RPC -32603 envelope.
        # Regression guard: it previously referenced an unbound
        # rpc_request and raised UnboundLocalError, so the peer got an
        # opaque 500 crash instead of a protocol-level error object.
        app, view, _ = self._server_with_agent()
        status, body = self._run(
            app, view, 'agentX_0',
            raw_data='not json', content_type='text/plain')
        assert status == 500
        assert body['jsonrpc'] == '2.0'
        assert body['error']['code'] == -32603
        assert body['id'] is None
