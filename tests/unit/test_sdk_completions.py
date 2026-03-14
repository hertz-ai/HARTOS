"""
Tests for Kong API Gateway — /v1/chat/completions proxy, metering, and
node registration endpoints defined in langchain_gpt_api.py.

Uses source inspection for route registration checks, plus a lightweight
Flask test app that replays the proxy logic with mocked backends.

Run with: pytest tests/unit/test_sdk_completions.py -v --noconftest
"""
import json
import os
import sys
import unittest
from functools import wraps
from unittest.mock import patch, MagicMock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(__file__))))

from flask import Flask, request, jsonify


# ─── Helper: build a minimal Flask app mirroring the gateway routes ────

def _json_endpoint(f):
    """Replica of the decorator from langchain_gpt_api.py."""
    @wraps(f)
    def _wrapped(*args, **kwargs):
        try:
            return f(*args, **kwargs)
        except Exception as e:
            return jsonify({'error': str(e)}), 500
    return _wrapped


def _build_gateway_app(*, requests_post_mock=None, record_metered_mock=None,
                       db_session_mock=None):
    """Build a Flask app with the three gateway routes.

    The route bodies are faithful copies of langchain_gpt_api.py lines 752-838
    but with injected mocks so we don't need real backends.
    """
    app = Flask(__name__)
    app.config['TESTING'] = True

    # -- /v1/chat/completions ------------------------------------------
    @app.route('/v1/chat/completions', methods=['POST'])
    @_json_endpoint
    def _completions_proxy():
        data = request.get_json(silent=True) or {}
        hevolve_url = os.environ.get('HEVOLVE_API_URL', 'http://localhost:8000')
        headers = {'Content-Type': 'application/json'}
        try:
            resp = requests_post_mock(
                f'{hevolve_url}/v1/chat/completions',
                json=data,
                headers=headers,
                timeout=120
            )
            result = resp.json()
        except Exception as fwd_err:
            return jsonify({'error': f'HevolveAI backend unavailable: {fwd_err}'}), 502

        usage = result.get('usage', {})
        total_tokens = usage.get('total_tokens', 0)
        if total_tokens > 0:
            try:
                consumer = request.headers.get('X-Consumer-Username', 'anonymous')
                record_metered_mock(
                    provider='hevolve',
                    model=data.get('model', 'hevolve'),
                    tokens=total_tokens,
                    source=f'sdk:{consumer}'
                )
            except Exception:
                pass
        return jsonify(result)

    # -- /api/gateway/metering -----------------------------------------
    @app.route('/api/gateway/metering', methods=['GET'])
    @_json_endpoint
    def _gateway_metering():
        try:
            ctx = db_session_mock()
            session = ctx.__enter__()
            rows = session.query_result
            ctx.__exit__(None, None, None)
            return jsonify({
                'providers': [
                    {'provider': r[0], 'total_tokens': int(r[1] or 0), 'calls': r[2]}
                    for r in rows
                ]
            })
        except Exception as e:
            return jsonify({'error': str(e)}), 500

    # -- /api/gateway/register -----------------------------------------
    @app.route('/api/gateway/register', methods=['POST'])
    @_json_endpoint
    def _gateway_register_node():
        data = request.get_json(silent=True) or {}
        target = data.get('target')
        if not target:
            return jsonify({'error': 'target is required'}), 400
        kong_admin = os.environ.get('KONG_ADMIN_URL', 'http://localhost:8001')
        upstream = data.get('upstream', 'hevolve-nodes')
        try:
            resp = requests_post_mock(
                f'{kong_admin}/upstreams/{upstream}/targets',
                json={'target': target, 'weight': 100},
                timeout=10
            )
            return jsonify({'registered': True, 'status': resp.status_code})
        except Exception as e:
            return jsonify({'error': f'Kong admin unreachable: {e}'}), 502

    return app


# ═════════════════════════════════════════════════════════════════════
# 1. Route Registration (source inspection)
# ═════════════════════════════════════════════════════════════════════

class TestGatewayRouteRegistration(unittest.TestCase):
    """Verify gateway routes exist in langchain_gpt_api.py source."""

    @classmethod
    def setUpClass(cls):
        src = os.path.join(
            os.path.dirname(os.path.dirname(os.path.dirname(__file__))),
            'langchain_gpt_api.py')
        with open(src, 'r', encoding='utf-8') as f:
            cls.source = f.read()

    def test_completions_proxy_route_registered(self):
        self.assertIn("@app.route('/v1/chat/completions', methods=['POST'])",
                       self.source)

    def test_gateway_metering_route_registered(self):
        self.assertIn("@app.route('/api/gateway/metering', methods=['GET'])",
                       self.source)

    def test_gateway_register_route_registered(self):
        self.assertIn("@app.route('/api/gateway/register', methods=['POST'])",
                       self.source)

    def test_all_three_use_json_endpoint(self):
        for func in ('_completions_proxy', '_gateway_metering',
                      '_gateway_register_node'):
            idx = self.source.find(f'def {func}(')
            self.assertGreater(idx, 0, f"Function {func} not found")
            preceding = self.source[max(0, idx - 100):idx]
            self.assertIn('@_json_endpoint', preceding,
                          f"Missing @_json_endpoint on {func}")


# ═════════════════════════════════════════════════════════════════════
# 2. /v1/chat/completions — Backend Unavailable → 502
# ═════════════════════════════════════════════════════════════════════

class TestCompletionsProxy502(unittest.TestCase):
    """When HevolveAI backend is unreachable, return 502."""

    def setUp(self):
        self.post_mock = MagicMock(side_effect=ConnectionError('refused'))
        self.meter_mock = MagicMock()
        self.app = _build_gateway_app(
            requests_post_mock=self.post_mock,
            record_metered_mock=self.meter_mock)
        self.client = self.app.test_client()

    def test_returns_502_on_connection_error(self):
        resp = self.client.post('/v1/chat/completions',
                                json={'model': 'hevolve', 'messages': []})
        self.assertEqual(resp.status_code, 502)
        data = resp.get_json()
        self.assertIn('HevolveAI backend unavailable', data['error'])

    def test_metering_not_called_on_502(self):
        self.client.post('/v1/chat/completions',
                         json={'model': 'hevolve', 'messages': []})
        self.meter_mock.assert_not_called()


# ═════════════════════════════════════════════════════════════════════
# 3. /v1/chat/completions — Successful Proxy + Token Metering
# ═════════════════════════════════════════════════════════════════════

class TestCompletionsProxySuccess(unittest.TestCase):
    """Successful proxy: forward request, return response, meter tokens."""

    BACKEND_RESPONSE = {
        'id': 'chatcmpl-abc123',
        'object': 'chat.completion',
        'choices': [{'message': {'role': 'assistant', 'content': 'Hello!'}}],
        'usage': {'prompt_tokens': 10, 'completion_tokens': 5, 'total_tokens': 15}
    }

    def setUp(self):
        mock_resp = MagicMock()
        mock_resp.json.return_value = self.BACKEND_RESPONSE
        self.post_mock = MagicMock(return_value=mock_resp)
        self.meter_mock = MagicMock()
        self.app = _build_gateway_app(
            requests_post_mock=self.post_mock,
            record_metered_mock=self.meter_mock)
        self.client = self.app.test_client()

    def test_proxies_response_body(self):
        resp = self.client.post('/v1/chat/completions',
                                json={'model': 'hevolve', 'messages': []})
        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()
        self.assertEqual(data['id'], 'chatcmpl-abc123')
        self.assertEqual(data['choices'][0]['message']['content'], 'Hello!')

    def test_forwards_to_correct_url(self):
        self.client.post('/v1/chat/completions',
                         json={'model': 'hevolve', 'messages': []})
        call_args = self.post_mock.call_args
        self.assertEqual(call_args[0][0],
                         'http://localhost:8000/v1/chat/completions')

    def test_meters_total_tokens(self):
        self.client.post('/v1/chat/completions',
                         json={'model': 'test-model', 'messages': []})
        self.meter_mock.assert_called_once()
        kwargs = self.meter_mock.call_args[1]
        self.assertEqual(kwargs['provider'], 'hevolve')
        self.assertEqual(kwargs['model'], 'test-model')
        self.assertEqual(kwargs['tokens'], 15)

    def test_no_metering_when_zero_tokens(self):
        """If usage.total_tokens is 0, metering is skipped."""
        mock_resp = MagicMock()
        mock_resp.json.return_value = {
            'choices': [], 'usage': {'total_tokens': 0}
        }
        self.post_mock.return_value = mock_resp
        self.client.post('/v1/chat/completions',
                         json={'model': 'hevolve', 'messages': []})
        self.meter_mock.assert_not_called()

    def test_no_metering_when_usage_absent(self):
        """If response has no usage key, metering is skipped."""
        mock_resp = MagicMock()
        mock_resp.json.return_value = {'choices': []}
        self.post_mock.return_value = mock_resp
        self.client.post('/v1/chat/completions',
                         json={'model': 'hevolve', 'messages': []})
        self.meter_mock.assert_not_called()

    def test_metering_failure_does_not_block_response(self):
        """If record_metered_usage throws, response still returns 200."""
        self.meter_mock.side_effect = RuntimeError('DB down')
        resp = self.client.post('/v1/chat/completions',
                                json={'model': 'hevolve', 'messages': []})
        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()
        self.assertEqual(data['id'], 'chatcmpl-abc123')


# ═════════════════════════════════════════════════════════════════════
# 4. Kong Consumer Header (X-Consumer-Username)
# ═════════════════════════════════════════════════════════════════════

class TestConsumerHeaderMetering(unittest.TestCase):
    """X-Consumer-Username from Kong is included in metering source."""

    BACKEND_RESPONSE = {
        'choices': [],
        'usage': {'total_tokens': 42}
    }

    def setUp(self):
        mock_resp = MagicMock()
        mock_resp.json.return_value = self.BACKEND_RESPONSE
        self.post_mock = MagicMock(return_value=mock_resp)
        self.meter_mock = MagicMock()
        self.app = _build_gateway_app(
            requests_post_mock=self.post_mock,
            record_metered_mock=self.meter_mock)
        self.client = self.app.test_client()

    def test_consumer_header_passed_to_source(self):
        self.client.post('/v1/chat/completions',
                         json={'model': 'hevolve', 'messages': []},
                         headers={'X-Consumer-Username': 'alice'})
        kwargs = self.meter_mock.call_args[1]
        self.assertEqual(kwargs['source'], 'sdk:alice')

    def test_anonymous_when_no_consumer_header(self):
        self.client.post('/v1/chat/completions',
                         json={'model': 'hevolve', 'messages': []})
        kwargs = self.meter_mock.call_args[1]
        self.assertEqual(kwargs['source'], 'sdk:anonymous')


# ═════════════════════════════════════════════════════════════════════
# 5. /api/gateway/metering — Usage Stats
# ═════════════════════════════════════════════════════════════════════

class TestGatewayMeteringEndpoint(unittest.TestCase):
    """GET /api/gateway/metering returns aggregated usage stats."""

    def _make_db_session(self, rows):
        """Create a mock db_session context manager returning *rows*."""
        session = MagicMock()
        session.query_result = rows
        ctx = MagicMock()
        ctx.__enter__ = MagicMock(return_value=session)
        ctx.__exit__ = MagicMock(return_value=False)
        return MagicMock(return_value=ctx)

    def test_returns_provider_stats(self):
        db_mock = self._make_db_session([
            ('hevolve', 15000, 120),
            ('openai', 8000, 45),
        ])
        app = _build_gateway_app(
            requests_post_mock=MagicMock(),
            record_metered_mock=MagicMock(),
            db_session_mock=db_mock)
        client = app.test_client()

        resp = client.get('/api/gateway/metering')
        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()
        providers = data['providers']
        self.assertEqual(len(providers), 2)
        self.assertEqual(providers[0]['provider'], 'hevolve')
        self.assertEqual(providers[0]['total_tokens'], 15000)
        self.assertEqual(providers[0]['calls'], 120)

    def test_empty_table_returns_empty_list(self):
        db_mock = self._make_db_session([])
        app = _build_gateway_app(
            requests_post_mock=MagicMock(),
            record_metered_mock=MagicMock(),
            db_session_mock=db_mock)
        client = app.test_client()

        resp = client.get('/api/gateway/metering')
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.get_json()['providers'], [])

    def test_db_error_returns_500(self):
        db_mock = MagicMock(side_effect=RuntimeError('DB offline'))
        app = _build_gateway_app(
            requests_post_mock=MagicMock(),
            record_metered_mock=MagicMock(),
            db_session_mock=db_mock)
        client = app.test_client()

        resp = client.get('/api/gateway/metering')
        self.assertEqual(resp.status_code, 500)
        self.assertIn('error', resp.get_json())


# ═════════════════════════════════════════════════════════════════════
# 6. /api/gateway/register — Node Registration Validation
# ═════════════════════════════════════════════════════════════════════

class TestGatewayRegisterEndpoint(unittest.TestCase):
    """POST /api/gateway/register validates target and proxies to Kong."""

    def setUp(self):
        mock_resp = MagicMock()
        mock_resp.status_code = 201
        self.post_mock = MagicMock(return_value=mock_resp)
        self.app = _build_gateway_app(
            requests_post_mock=self.post_mock,
            record_metered_mock=MagicMock())
        self.client = self.app.test_client()

    def test_missing_target_returns_400(self):
        resp = self.client.post('/api/gateway/register', json={})
        self.assertEqual(resp.status_code, 400)
        self.assertIn('target is required', resp.get_json()['error'])

    def test_empty_body_returns_400(self):
        resp = self.client.post('/api/gateway/register',
                                content_type='application/json',
                                data='{}')
        self.assertEqual(resp.status_code, 400)

    def test_valid_target_registers_and_returns_201_status(self):
        resp = self.client.post('/api/gateway/register',
                                json={'target': '192.168.1.5:8000'})
        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()
        self.assertTrue(data['registered'])
        self.assertEqual(data['status'], 201)

    def test_kong_admin_url_used(self):
        self.client.post('/api/gateway/register',
                         json={'target': '10.0.0.1:8000'})
        call_url = self.post_mock.call_args[0][0]
        self.assertIn('/upstreams/hevolve-nodes/targets', call_url)

    def test_custom_upstream(self):
        self.client.post('/api/gateway/register',
                         json={'target': '10.0.0.1:8000',
                               'upstream': 'my-cluster'})
        call_url = self.post_mock.call_args[0][0]
        self.assertIn('/upstreams/my-cluster/targets', call_url)

    def test_kong_unreachable_returns_502(self):
        self.post_mock.side_effect = ConnectionError('refused')
        resp = self.client.post('/api/gateway/register',
                                json={'target': '10.0.0.1:8000'})
        self.assertEqual(resp.status_code, 502)
        self.assertIn('Kong admin unreachable', resp.get_json()['error'])


if __name__ == '__main__':
    unittest.main()
