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
import socket
import subprocess
import threading
import time
from pathlib import Path
from typing import Any, Dict, Optional

from core.node_bin import find_node

logger = logging.getLogger(__name__)


# ── Filesystem layout (mirrors livekit_supervisor) ───────────────────
def _hevolve_home() -> Path:
    base = os.environ.get('HEVOLVE_HOME')
    if base:
        return Path(base).expanduser()
    return Path.home() / '.hevolve'


def _whatsapp_home() -> Path:
    return _hevolve_home() / 'whatsapp'


def _bundled_gateway_dir() -> Path:
    """Where the cx_Freeze installer ships gateway.js + package.json
    (read-only inside Program Files on Windows non-admin installs)."""
    return Path(__file__).resolve().parent.parent / 'channels' / 'whatsapp'


#: The gateway's npm project in its storage root (_gateway_storage).
GATEWAY_TOOL = 'gateway'
#: Bound on the gateway's ``npm install``.  It runs in the HARTOS bootstrap
#: thread (hartos_bootstrap._init_whatsapp_supervisor), ahead of the
#: consumer's on_bootstrap_complete, so it keeps the 180 s it always had
#: rather than the npm installer's longer default.
GATEWAY_NPM_TIMEOUT_S = 180.0


def _gateway_storage():
    """The storage root the gateway is set up in: ~/.hevolve/whatsapp
    (HEVOLVE_HOME honoured), through the one npm installer's own class.

    Not the models root (model_storage's HEVOLVE_MODEL_DIR): that may be an
    external drive, and WhatsApp must not stop working when it is unplugged;
    and the gateway has always lived here, beside its paired auth state
    (gateway.js keeps that in $HEVOLVE_HOME/whatsapp/auth, outside this
    folder), so no install moves or downloads again."""
    from integrations.service_tools.model_storage import ModelStorageManager
    return ModelStorageManager(base_dir=_whatsapp_home())


def _gateway_dir() -> Path:
    """User-writable directory holding the runtime gateway.js +
    package.json + node_modules/: ~/.hevolve/whatsapp/gateway/, the
    gateway tool's folder in _gateway_storage.

    It once returned the bundled path under Program Files directly, which
    broke every non-admin Windows install — npm install hit EPERM creating
    node_modules/ inside the read-only install dir.  Logs at
    ~/Documents/Nunba/logs/frozen_debug.log showed "npm install returned
    4294963248" identically on every boot for 6+ days (the unsigned
    wraparound of an EACCES exit).  All mutable state lives here instead;
    the files are staged into it by ensure_baileys_deps' setup.  Survives
    Nunba uninstall/reinstall, as does the paired Baileys auth state at
    ~/.hevolve/whatsapp/auth/.
    """
    return _gateway_storage().get_tool_dir(GATEWAY_TOOL)


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
# Node and npm are found by core.node_bin, the one locator (find_node
# returns None when absent; start() then says Node.js is missing).
def ensure_baileys_deps() -> bool:
    """Set the gateway's npm project (gateway.js + package.json +
    package-lock.json, shipped in _bundled_gateway_dir) up in
    _gateway_dir through the one npm installer,
    integrations.service_tools.model_storage.install_npm_project (A18/A20:
    this was a second copy of it).  Idempotent: a complete setup of the
    shipped project returns at once; a HARTOS update that changes any of
    its files sets it up again (its receipt names their fingerprint), and
    npm installs the tree package-lock.json pins.

    Returns True iff the gateway can start: the setup finished, or it
    failed (offline, say) where an earlier install left node_modules and
    gateway.js -- the gateway then starts on the packages it has, as it
    always did before the fold, and the next start tries the setup again
    (no receipt was written).  False when it failed with nothing to start
    on.  Never raises: it runs in the HARTOS bootstrap thread, so anything
    the setup raises is a failed setup (logged)."""
    from integrations.service_tools.model_storage import NpmSetupFailed
    try:
        _gateway_storage().install_npm_project(
            GATEWAY_TOOL, str(_bundled_gateway_dir()), timeout_s=GATEWAY_NPM_TIMEOUT_S)
        return True
    except NpmSetupFailed as e:
        why = str(e)
    except Exception as e:      # a bug in the setup: logged, never into start()
        logger.error("whatsapp_supervisor: the gateway's npm setup raised %s: %s",
                     type(e).__name__, e, exc_info=True)
        why = '%s: %s' % (type(e).__name__, e)
    gateway_dir = _gateway_dir()
    try:
        installed = (gateway_dir / 'node_modules').is_dir() \
            and (gateway_dir / 'gateway.js').is_file()
    except OSError as e:
        logger.warning("whatsapp_supervisor: %s could not be read: %s", gateway_dir, e)
        installed = False
    if installed:
        logger.warning(
            "whatsapp_supervisor: the gateway's npm setup failed (%s); starting it on "
            "the node_modules already in %s, and setting it up again next start.",
            why, gateway_dir)
        return True
    logger.warning(
        "whatsapp_supervisor: the gateway's npm setup failed: %s.  Set "
        "WHATSAPP_API_URL to a managed WAHA endpoint, or install Node.js "
        "(which provides npm).", why)
    return False


from core.process_supervisor import ProcessSupervisor


# ── Supervisor (spawn/stream/backoff loop in core.process_supervisor, #110) ──
class _Supervisor(ProcessSupervisor):
    """One per process.  Daemon thread → exits cleanly with parent."""

    name = 'whatsapp'
    # node missing at spawn → stop retrying (matches the original
    # FileNotFoundError break) instead of backing off forever.
    fatal_spawn_errors = (FileNotFoundError,)

    def __init__(self) -> None:
        super().__init__()

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

        node = find_node()
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

        self._spawn_thread()
        return self.info()

    def _build_popen(self):
        from core.subprocess_safe import hidden_popen_kwargs
        node = find_node() or 'node'
        gateway_js = _gateway_dir() / 'gateway.js'
        env = os.environ.copy()
        env['WHATSAPP_GATEWAY_PORT'] = str(_gateway_port())
        env['HEVOLVE_HOME'] = str(_hevolve_home())
        cmd = [node, str(gateway_js)]
        return cmd, dict(
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            cwd=str(_gateway_dir()),
            env=env,
            text=True,
            bufsize=1,  # line-buffered for line-by-line drain
            **hidden_popen_kwargs(),
        )

    def _format_stdout_line(self, line: str) -> None:
        # gateway.js emits JSON events; fall back to plain text.
        try:
            payload = json.loads(line)
            event = payload.get('event') or 'log'
            logger.info(
                "whatsapp_supervisor: %s %s", event,
                {k: v for k, v in payload.items() if k != 'event'})
        except (ValueError, AttributeError):
            logger.info("whatsapp_supervisor: %s", line)


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
