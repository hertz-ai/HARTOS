"""Cross-OS entrypoint for a single ComputeMesh node — a RUNNER, not a new mesh.

This starts the EXISTING ``ComputeMeshService`` behind one of two transports:

  * ``flask``  — reuses ``mesh.serve_forever()`` (waitress / Flask), the shipped
    path used when the full app stack is present.
  * ``stdlib`` — a ``http.server.ThreadingHTTPServer`` whose request handler
    dispatches purely from ``mesh.route_table()`` with ZERO Flask and ZERO pip
    dependencies. This is the zero-footprint vehicle for an ephemeral proof node
    (e.g. a peer that has only the system ``python3``) and the localhost
    self-test server. It speaks the identical ``/mesh/*`` + shard-envelope
    contract because it shares the route_table() handlers.
  * ``auto``   — ``flask`` when Flask imports, else ``stdlib``.

``--ephemeral`` points ``HEVOLVE_DATA_DIR`` at a throwaway ``/tmp`` directory and
writes a random ``mesh/keys/public.key`` there so ``ComputeMeshService`` derives a
well-defined, unique-per-node ``device_id`` (SHA256(public.key)[:16]) WITHOUT
touching ``agent_data`` or ``/var/lib/hart``. Nothing is written outside the
chosen data dir.

Reuses: ComputeMeshService.serve_forever(), route_table(), _start_background_loops(),
_load_identity(); the get_device_id() SHA256(public.key)[:16] identity pattern;
core.port_registry.get_port('mesh_relay').
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import tempfile
import threading
import types

logger = logging.getLogger('hevolve.mesh_runner')

# NOTE: every import of ComputeMeshService / envelope / port_registry is done
# LAZILY inside the functions below. This keeps this module's top-level imports
# to the stdlib only, so the standalone-script bootstrap (below) can seed bare
# parent packages BEFORE the heavy ``core`` / ``integrations`` package __init__
# bodies (which pull in requests/flask/etc.) would otherwise run.


def _bootstrap_standalone() -> None:
    """Make the 8-file ephemeral bundle importable with STDLIB ONLY.

    ``core/__init__.py`` eagerly imports http_pool (requests), config_cache,
    platform_paths, ... and ``integrations/__init__.py`` imports a transformers
    guard — none of which ship in the zero-pip bundle. When this file is run as
    a plain script (``python3 integrations/agent_engine/mesh_node_runner.py``),
    we pre-seed ``sys.modules`` with bare, __init__-skipped package objects whose
    ``__path__`` points at the bundle dirs. Python then imports the needed
    SUBMODULES (core.port_registry, core.shard_runtime.envelope,
    integrations.agent_engine.compute_mesh_service) directly, each of which is
    stdlib-only, without ever executing the heavy package __init__ bodies.

    Only runs in the ``__main__`` script path; when this module is imported
    normally (tests / the full app on a venv with all deps) it is a no-op.
    """
    here = os.path.dirname(os.path.abspath(__file__))
    root = os.path.dirname(os.path.dirname(here))  # bundle root (parent of core/)
    if root not in sys.path:
        sys.path.insert(0, root)

    def _bare(name: str, path: str) -> None:
        if name in sys.modules:
            return
        m = types.ModuleType(name)
        m.__path__ = [path]  # mark as a package so submodule import works
        sys.modules[name] = m

    _bare('core', os.path.join(root, 'core'))
    _bare('integrations', os.path.join(root, 'integrations'))
    _bare('integrations.agent_engine', os.path.join(root, 'integrations', 'agent_engine'))


# ─── Ephemeral identity ──────────────────────────────────────────

def _ensure_ephemeral_identity(data_dir: str) -> str:
    """Write a throwaway public.key (hex text) under data_dir/mesh/keys.

    hex text (not raw bytes) because ComputeMeshService._load_identity opens the
    key file in TEXT mode; random bytes would fail UTF-8 decode and leave
    device_id undefined. Unique random content => unique device_id per node.
    """
    key_dir = os.path.join(data_dir, 'mesh', 'keys')
    os.makedirs(key_dir, exist_ok=True)
    pub = os.path.join(key_dir, 'public.key')
    if not os.path.exists(pub):
        with open(pub, 'w') as f:
            f.write(os.urandom(32).hex())
    return pub


def build_ephemeral_mesh(port: int, data_dir: str = None):
    """Construct a ComputeMeshService with a throwaway /tmp identity.

    Returns (mesh, data_dir). Sets HEVOLVE_DATA_DIR to the chosen dir BEFORE
    constructing the service so _load_identity reads the throwaway key. Refuses
    to write anywhere but the chosen data dir.
    """
    if not data_dir:
        data_dir = tempfile.mkdtemp(prefix='hart_mesh_')
    os.makedirs(data_dir, exist_ok=True)
    _ensure_ephemeral_identity(data_dir)

    from integrations.agent_engine.compute_mesh_service import ComputeMeshService
    # ComputeMeshService._load_identity reads HEVOLVE_DATA_DIR at CONSTRUCTION only
    # (compute_mesh_service.py:124). Set it just for the constructor, then restore,
    # so building N ephemeral nodes in one process (tests / the harness) never
    # leaves the process-global var pointing at the last node's throwaway dir.
    _prev = os.environ.get('HEVOLVE_DATA_DIR')
    os.environ['HEVOLVE_DATA_DIR'] = data_dir
    try:
        mesh = ComputeMeshService(task_relay_port=port)
    finally:
        if _prev is None:
            os.environ.pop('HEVOLVE_DATA_DIR', None)
        else:
            os.environ['HEVOLVE_DATA_DIR'] = _prev
    return mesh, data_dir


# ─── Stdlib transport (ZERO Flask, ZERO pip) ─────────────────────

def _make_handler(mesh):
    from http.server import BaseHTTPRequestHandler

    class _MeshHandler(BaseHTTPRequestHandler):
        # Quiet: no per-request stderr spam.
        def log_message(self, *_a):
            return

        def _dispatch(self, verb: str):
            path = self.path.split('?', 1)[0]
            handler = mesh.route_table().get((verb, path))
            if handler is None:
                self._write(404, 'application/json',
                            json.dumps({'error': 'not found'}).encode('utf-8'))
                return
            length = int(self.headers.get('Content-Length') or 0)
            raw = self.rfile.read(length) if length else b''
            try:
                status, ctype, body = handler(raw)
            except Exception as e:  # safety net; handlers already fail-close
                status, ctype, body = (
                    500, 'application/json',
                    json.dumps({'error': str(e)}).encode('utf-8'))
            self._write(status, ctype, body)

        def _write(self, status: int, ctype: str, body: bytes):
            self.send_response(status)
            self.send_header('Content-Type', ctype)
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            self._dispatch('GET')

        def do_POST(self):
            self._dispatch('POST')

    return _MeshHandler


def build_stdlib_server(mesh, host: str = '0.0.0.0', port: int = None):
    """Build (do not start) a ThreadingHTTPServer bound to mesh.route_table()."""
    from http.server import ThreadingHTTPServer
    bind_port = mesh.task_relay_port if port is None else port

    class _MeshHTTPServer(ThreadingHTTPServer):
        # socketserver's default listen backlog is 5. A shard pipeline fans out
        # to peers in parallel, so more than five peers connecting in the same
        # instant overflow the accept queue and the kernel RESETS the excess
        # ones -- the relay sees ConnectionResetError(104) and the shard fails,
        # even though the node is healthy and idle. Serving is threaded and each
        # request is short, so the queue only needs to absorb the burst until
        # accept() catches up.
        request_queue_size = 128

    server = _MeshHTTPServer((host, bind_port), _make_handler(mesh))
    return server


# ─── Serve ───────────────────────────────────────────────────────

def serve(mode: str = 'auto', port: int = None, data_dir: str = None,
          ephemeral: bool = False, host: str = '127.0.0.1'):
    """Start one mesh node.

    mode 'flask'  -> mesh.serve_forever() (blocking).
    mode 'stdlib' -> ThreadingHTTPServer over route_table() (blocking).
    mode 'auto'   -> flask if importable, else stdlib.

    host defaults to loopback: the pairing handshake is not yet
    signature-authenticated (auto_accept), so LAN exposure is an explicit opt-in
    (pass --host 0.0.0.0), not the default.
    """
    from core.port_registry import get_port
    if port is None:
        port = get_port('mesh_relay')
    if host not in ('127.0.0.1', 'localhost', '::1'):
        logger.warning(
            "mesh node binding %s (non-loopback) while the pairing handshake is "
            "not yet signature-authenticated; only expose this on a trusted network.",
            host)

    if ephemeral:
        mesh, data_dir = build_ephemeral_mesh(port, data_dir or os.environ.get('HEVOLVE_DATA_DIR'))
    else:
        from integrations.agent_engine.compute_mesh_service import get_compute_mesh
        mesh = get_compute_mesh()
        mesh.task_relay_port = port

    resolved = mode
    if mode == 'auto':
        try:
            import flask  # noqa: F401
            resolved = 'flask'
        except Exception:
            resolved = 'stdlib'

    if resolved == 'flask':
        logger.info("flask mesh serving on %s:%s (device_id=%s)",
                    host, port, mesh._device_id)
        print(f"flask mesh serving on {host}:{port} device_id={mesh._device_id}",
              flush=True)
        mesh.serve_forever(host=host)
        return

    # stdlib transport
    mesh._start_background_loops()
    server = build_stdlib_server(mesh, host=host, port=port)
    actual = server.server_address[1]
    mesh.task_relay_port = actual
    logger.info("stdlib mesh serving on %s:%s (device_id=%s)",
                host, actual, mesh._device_id)
    print(f"stdlib mesh serving on {host}:{actual} device_id={mesh._device_id}",
          flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        server.shutdown()


def _parse_args(argv=None):
    ap = argparse.ArgumentParser(
        description='Run one HART ComputeMesh node (flask or zero-dep stdlib).')
    ap.add_argument('--serve', action='store_true',
                    help='Start serving (default action).')
    ap.add_argument('--flask', action='store_true', help='Force Flask transport.')
    ap.add_argument('--stdlib', action='store_true',
                    help='Force the zero-dependency stdlib transport.')
    ap.add_argument('--ephemeral', action='store_true',
                    help='Throwaway /tmp identity + data dir (no agent_data writes).')
    ap.add_argument('--port', type=int, default=None,
                    help="Relay port (defaults to get_port('mesh_relay') = 6796 in app mode).")
    ap.add_argument('--data-dir', default=None, help='Override HEVOLVE_DATA_DIR.')
    ap.add_argument('--host', default='127.0.0.1',
                    help='Bind host (default loopback; pairing is not yet '
                         'signature-authenticated, so LAN exposure is opt-in).')
    return ap.parse_args(argv)


def main(argv=None):
    logging.basicConfig(level=logging.INFO)
    args = _parse_args(argv)
    if args.flask:
        mode = 'flask'
    elif args.stdlib:
        mode = 'stdlib'
    else:
        mode = 'auto'
    serve(mode=mode, port=args.port, data_dir=args.data_dir,
          ephemeral=args.ephemeral, host=args.host)


if __name__ == '__main__':
    _bootstrap_standalone()
    main()
