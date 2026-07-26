"""
Behavioral tests for the Model Bus Unix socket transport.

The nix module advertises /run/hart/model-bus.sock for native Linux apps + AI
agents, and its ExecStartPost waits for that socket to exist. These tests prove
the transport (added in model_bus_service.py) actually decodes a line-delimited
JSON request and routes it through the SAME infer() / list_models() /
get_status() the HTTP app uses — no parallel routing path — and degrades (never
crashes) on malformed input.

The pure dispatch (_handle_socket_request / _handle_socket_line) is exercised on
every platform. A real end-to-end AF_UNIX round-trip is exercised only where
AF_UNIX exists (Linux); it is skipped on the Windows dev box.
"""
import json
import os
import socket
import tempfile
import threading
import time
import unittest
from unittest.mock import MagicMock

from integrations.agent_engine.model_bus_service import ModelBusService
from integrations.service_tools.model_catalog import ModelType


class TestSocketDispatch(unittest.TestCase):
    """The pure request handler routes to the shared inference paths."""

    def setUp(self):
        self.svc = ModelBusService(socket_path='/tmp/hart-test-unused.sock')

    def test_ping_answers_without_touching_backends(self):
        out = self.svc._handle_socket_request({'op': 'ping'})
        self.assertTrue(out.get('ok'))
        self.assertEqual(out.get('service'), 'model-bus')

    def test_infer_routes_through_infer_with_args(self):
        self.svc.infer = MagicMock(return_value={'response': 'hi',
                                                  'backend': 'local_llm'})
        out = self.svc._handle_socket_request({
            'op': 'infer',
            'model_type': 'llm',
            'prompt': 'hello',
            'options': {'max_tokens': 7},
        })
        self.svc.infer.assert_called_once_with(
            model_type='llm', prompt='hello', options={'max_tokens': 7})
        self.assertEqual(out['response'], 'hi')

    def test_infer_defaults_model_type_to_llm(self):
        self.svc.infer = MagicMock(return_value={'response': 'x'})
        self.svc._handle_socket_request({'op': 'infer', 'prompt': 'y'})
        _, kwargs = self.svc.infer.call_args
        self.assertEqual(kwargs['model_type'], ModelType.LLM)
        self.assertEqual(kwargs['options'], {})

    def test_list_models_op(self):
        self.svc.list_models = MagicMock(return_value=[{'id': 'local-llm'}])
        out = self.svc._handle_socket_request({'op': 'list_models'})
        self.assertEqual(out, {'models': [{'id': 'local-llm'}]})

    def test_status_op(self):
        self.svc.get_status = MagicMock(return_value={'status': 'running'})
        out = self.svc._handle_socket_request({'op': 'status'})
        self.assertEqual(out['status'], 'running')

    def test_unknown_op_is_error_not_crash(self):
        out = self.svc._handle_socket_request({'op': 'launch_missiles'})
        self.assertIn('error', out)

    def test_non_dict_payload_is_error(self):
        out = self.svc._handle_socket_request(['not', 'a', 'dict'])
        self.assertIn('error', out)

    def test_infer_exception_degrades_to_error(self):
        self.svc.infer = MagicMock(side_effect=RuntimeError('boom'))
        out = self.svc._handle_socket_request({'op': 'infer', 'prompt': 'z'})
        self.assertIn('error', out)
        self.assertIn('boom', out['error'])

    def test_line_handler_rejects_malformed_json(self):
        reply = self.svc._handle_socket_line('{not valid json')
        parsed = json.loads(reply)  # the reply itself must be valid JSON
        self.assertIn('error', parsed)

    def test_line_handler_roundtrips_a_valid_request(self):
        self.svc.list_models = MagicMock(return_value=[])
        reply = self.svc._handle_socket_line('{"op": "list_models"}')
        self.assertEqual(json.loads(reply), {'models': []})


@unittest.skipUnless(hasattr(socket, 'AF_UNIX'),
                     'AF_UNIX transport is Linux-only (skipped on this platform)')
class TestSocketEndToEnd(unittest.TestCase):
    """A real client can connect to the bound socket and get a JSON reply."""

    def test_real_unix_socket_roundtrip(self):
        tmpdir = tempfile.mkdtemp()
        sock_path = os.path.join(tmpdir, 'model-bus.sock')
        svc = ModelBusService(socket_path=sock_path)
        svc.list_models = MagicMock(return_value=[{'id': 'local-llm'}])
        svc._running = True
        t = threading.Thread(target=svc._serve_unix_socket, daemon=True)
        t.start()
        try:
            # Wait for the socket to appear (the readiness the nix unit waits on).
            for _ in range(50):
                if os.path.exists(sock_path):
                    break
                time.sleep(0.05)
            self.assertTrue(os.path.exists(sock_path),
                            'the advertised socket must actually be bound')

            client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            client.settimeout(5)
            client.connect(sock_path)
            client.sendall(b'{"op": "list_models"}\n')
            buf = b''
            while b'\n' not in buf:
                chunk = client.recv(4096)
                if not chunk:
                    break
                buf += chunk
            client.close()
            reply = json.loads(buf.decode().strip())
            self.assertEqual(reply, {'models': [{'id': 'local-llm'}]})
        finally:
            svc._running = False
            if svc._unix_sock is not None:
                try:
                    svc._unix_sock.close()
                except OSError:
                    pass


if __name__ == '__main__':
    unittest.main()
