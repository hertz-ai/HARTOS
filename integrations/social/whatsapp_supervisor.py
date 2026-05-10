"""WhatsApp gateway supervisor — embedded Baileys via Node subprocess.

Sister of ``livekit_supervisor.py``: same filesystem layout under
``~/.hevolve/<service>/``, same daemon-thread restart pattern, same
``core.subprocess_safe.hidden_popen_kwargs`` for silent Windows spawn,
same per-deploy-mode gating.

Why this exists:
    The existing WhatsApp adapter at ``integrations/channels/whatsapp_adapter.py``
    talks WAHA's HTTP+WS API at ``http://localhost:3000`` (or whatever
    ``WHATSAPP_API_URL`` resolves to).  Asking every Nunba install to
    install Docker + run WAHA's container is a heavy ask — the user
    explicitly told us "Docker is a new system on the user machine and
    we cannot rely on that".

    This supervisor replaces the WAHA-Docker path with an embedded
    Baileys gateway (``integrations/channels/whatsapp/gateway.js``)
    that exposes the same WAHA API subset on the same port, so the
    adapter stays identical.  The only "new system" is Node.js — but
    Nunba's web build already requires Node, so most user machines
    already have it.  When Node is missing we surface a clear single-
    line error and the adapter falls back to the existing
    ``WHATSAPP_API_URL`` override path (operator-managed remote WAHA).

Singleton: one instance per HARTOS process.  Lifecycle owned by
``start_supervisor()``, called from ``hartos_bootstrap.py``.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import socket
import subprocess
import threading
import time
from pathlib import Path
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)


# ── Filesystem layout (mirrors livekit_supervisor) ───────────────────
def _hevolve_home() -> Path:
    base = os.environ.get('HEVOLVE_HOME')
    if base:
        return Path(base).expanduser()
    return Path.home() / '.hevolve'


def _whatsapp_home() -> Path:
    return _hevolve_home() / 'whatsapp'


def _gateway_dir() -> Path:
    """Repo path that holds gateway.js + package.json — installed-once
    during ensure_baileys_deps()."""
    return Path(__file__).resolve().parent.parent / 'channels' / 'whatsapp'


# ── Deploy-mode gating (mirrors livekit_supervisor pattern) ──────────
def _deploy_mode() -> str:
    return os.environ.get('HEVOLVE_DEPLOY_MODE', 'flat').lower().strip()


def supervisor_should_run() -> bool:
    """True iff this process should host the Baileys gateway itself.

    Override:
      WHATSAPP_AUTOSTART=0 → force-disable (operator runs WAHA elsewhere)
      WHATSAPP_AUTOSTART=1 → force-enable (regardless of deploy mode)
      WHATSAPP_API_URL set to a non-localhost address → operator
        explicitly wants remote WAHA; we skip spawning the local
        gateway so the adapter's existing override path takes effect.
    """
    forced = os.environ.get('WHATSAPP_AUTOSTART')
    if forced == '0':
        return False
    if forced == '1':
        return True

    # Operator points at a remote WAHA — let them.  Single source of
    # truth for "use external gateway" is the adapter's WHATSAPP_API_URL
    # env (already honoured at adapter init).
    api_url = os.environ.get('WHATSAPP_API_URL', '').strip().lower()
    if api_url and not (
        'localhost' in api_url or '127.0.0.1' in api_url
    ):
        return False

    return _deploy_mode() in ('flat', 'regional')


# ── Port helpers (lifted from livekit_supervisor.py — same idiom) ────
def _port_in_use(port: int) -> bool:
    """True iff *something* is already listening on 127.0.0.1:port.

    When True, we skip the Baileys spawn and assume an operator has
    WAHA running there manually — same short-circuit livekit_supervisor
    uses.  Single check, single rule.
    """
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(0.5)
    try:
        return sock.connect_ex(('127.0.0.1', port)) == 0
    finally:
        try:
            sock.close()
        except OSError:
            pass


def _gateway_port() -> int:
    return int(os.environ.get('WHATSAPP_GATEWAY_PORT', '3000'))


# ── Node + dep ensures (one-time on first start) ─────────────────────
def ensure_node() -> Optional[str]:
    """Return path to ``node`` executable, or None if Node isn't on
    PATH.  Caller surfaces a "please install Node" message to the user
    when this returns None."""
    return shutil.which('node')


def ensure_npm() -> Optional[str]:
    """Return path to ``npm``, or None.  Same docs as ensure_node."""
    return shutil.which('npm')


def ensure_baileys_deps() -> bool:
    """Idempotent: install gateway.js's npm deps under the gateway dir
    if its ``node_modules/`` doesn't yet exist.  Returns True iff the
    deps are now ready (cached or freshly installed).  False on
    install failure — caller logs and falls back."""
    gateway_dir = _gateway_dir()
    if not gateway_dir.exists():
        logger.error(
            "whatsapp_supervisor: gateway dir missing at %s", gateway_dir)
        return False

    node_modules = gateway_dir / 'node_modules'
    if node_modules.exists() and node_modules.is_dir():
        return True

    npm = ensure_npm()
    if npm is None:
        logger.warning(
            "whatsapp_supervisor: npm not on PATH; cannot install Baileys "
            "deps.  Set WHATSAPP_API_URL to a managed WAHA endpoint, or "
            "install Node.js (which provides npm).")
        return False

    # First-run install — pulls @whiskeysockets/baileys + express.
    # Hidden cmd window on Windows.
    from core.subprocess_safe import hidden_popen_kwargs
    logger.info(
        "whatsapp_supervisor: installing Baileys deps in %s "
        "(one-time, ~80 MB)…", gateway_dir)
    try:
        result = subprocess.run(
            [npm, 'install', '--no-audit', '--no-fund',
             '--loglevel=error', '--prefer-offline'],
            cwd=str(gateway_dir),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            timeout=180,
            **hidden_popen_kwargs(),
        )
    except (subprocess.TimeoutExpired, OSError) as e:
        logger.warning("whatsapp_supervisor: npm install failed (%s)", e)
        return False
    if result.returncode != 0:
        logger.warning(
            "whatsapp_supervisor: npm install returned %d:\n%s",
            result.returncode, (result.stdout or '')[:500])
        return False
    logger.info("whatsapp_supervisor: Baileys deps installed")
    return True


# ── Supervisor (mirrors livekit_supervisor._Supervisor) ──────────────
class _Supervisor:
    """One per process.  Daemon thread → exits cleanly with parent."""

    def __init__(self) -> None:
        self.proc: Optional[subprocess.Popen] = None
        self.thread: Optional[threading.Thread] = None
        self.stop_event = threading.Event()
        self.last_error: Optional[str] = None
        self.last_started: Optional[float] = None
        self.restart_count = 0
        self.lock = threading.Lock()

    def info(self) -> Dict[str, Any]:
        running = bool(self.proc and self.proc.poll() is None)
        return {
            'running': running,
            'port': _gateway_port(),
            'pid': self.proc.pid if running and self.proc else None,
            'last_error': self.last_error,
            'last_started': self.last_started,
            'restart_count': self.restart_count,
        }

    def start(self) -> Dict[str, Any]:
        if self.thread is not None and self.thread.is_alive():
            return self.info()

        node = ensure_node()
        if node is None:
            self.last_error = (
                'Node.js not on PATH; install Node ≥18 or set '
                'WHATSAPP_API_URL to a remote WAHA endpoint')
            logger.warning("whatsapp_supervisor: %s", self.last_error)
            return self.info()

        if not ensure_baileys_deps():
            self.last_error = (
                'Baileys npm install failed — see prior log lines')
            return self.info()

        port = _gateway_port()
        if _port_in_use(port):
            self.last_error = (
                f'port {port} already in use; assuming operator-managed '
                f'WhatsApp gateway is running and skipping spawn')
            logger.info("whatsapp_supervisor: %s", self.last_error)
            return self.info()

        self.thread = threading.Thread(
            target=self._run, daemon=True, name='whatsapp-supervisor')
        self.thread.start()
        return self.info()

    def stop(self) -> None:
        self.stop_event.set()
        with self.lock:
            if self.proc and self.proc.poll() is None:
                try:
                    self.proc.terminate()
                    try:
                        self.proc.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        self.proc.kill()
                except OSError:
                    pass

    def _run(self) -> None:
        from core.subprocess_safe import hidden_popen_kwargs
        node = ensure_node() or 'node'
        gateway_js = _gateway_dir() / 'gateway.js'
        env = os.environ.copy()
        env['WHATSAPP_GATEWAY_PORT'] = str(_gateway_port())
        env['HEVOLVE_HOME'] = str(_hevolve_home())

        backoff = 1.0
        while not self.stop_event.is_set():
            try:
                cmd = [node, str(gateway_js)]
                logger.info(
                    "whatsapp_supervisor: spawning %s on port %d",
                    ' '.join(cmd), _gateway_port())
                with self.lock:
                    self.proc = subprocess.Popen(
                        cmd,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.STDOUT,
                        cwd=str(_gateway_dir()),
                        env=env,
                        text=True,
                        bufsize=1,  # line-buffered for line-by-line drain
                        **hidden_popen_kwargs(),
                    )
                    self.last_started = time.time()
                    self.restart_count += 1

                # Drain stdout into logger so operators see what
                # Baileys is doing without a separate log file.  Lines
                # are JSON when emitted by gateway.js (matches
                # livekit_supervisor's stdout-streaming idiom).
                if self.proc.stdout is not None:
                    for raw in self.proc.stdout:
                        if self.stop_event.is_set():
                            break
                        line = raw.rstrip()
                        if not line:
                            continue
                        # Try JSON event; fall back to plain text.
                        try:
                            payload = json.loads(line)
                            event = payload.get('event') or 'log'
                            logger.info(
                                "whatsapp_supervisor: %s %s",
                                event,
                                {k: v for k, v in payload.items()
                                 if k != 'event'},
                            )
                        except (ValueError, AttributeError):
                            logger.info("whatsapp_supervisor: %s", line)

                rc = self.proc.wait() if self.proc else None
                self.last_error = f'gateway exited rc={rc}'
                if self.stop_event.is_set():
                    break
                logger.warning(
                    "whatsapp_supervisor: gateway exited rc=%s; "
                    "respawning in %.1fs", rc, backoff)
                time.sleep(backoff)
                backoff = min(backoff * 2, 60.0)
            except FileNotFoundError as e:
                self.last_error = f'node not found: {e}'
                logger.error("whatsapp_supervisor: %s", self.last_error)
                break
            except Exception as e:  # noqa: BLE001 — supervisor catches all
                self.last_error = f'spawn error: {e}'
                logger.exception("whatsapp_supervisor: spawn error")
                if self.stop_event.is_set():
                    break
                time.sleep(backoff)
                backoff = min(backoff * 2, 60.0)


_supervisor: Optional[_Supervisor] = None
_supervisor_lock = threading.Lock()


def start_supervisor() -> Dict[str, Any]:
    """Idempotent entrypoint called from hartos_bootstrap.py.  No-op
    when supervisor_should_run() is False (central deploy, operator-
    managed remote WAHA, etc.)."""
    if not supervisor_should_run():
        logger.info(
            "whatsapp_supervisor: skipped (deploy_mode=%s, autostart=%s, "
            "WHATSAPP_API_URL=%s)",
            _deploy_mode(),
            os.environ.get('WHATSAPP_AUTOSTART'),
            os.environ.get('WHATSAPP_API_URL'),
        )
        return {'running': False, 'reason': 'gated_off'}

    global _supervisor
    with _supervisor_lock:
        if _supervisor is None:
            _supervisor = _Supervisor()
        return _supervisor.start()


def stop_supervisor() -> None:
    """Best-effort shutdown — used by tests + bootstrap teardown."""
    global _supervisor
    with _supervisor_lock:
        if _supervisor is not None:
            _supervisor.stop()


def info() -> Dict[str, Any]:
    """Reflection for /health endpoints + agent diagnostics."""
    global _supervisor
    if _supervisor is None:
        return {'running': False, 'reason': 'not_started'}
    return _supervisor.info()
