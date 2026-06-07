"""
LiveKit supervisor — self-hosted SFU lifecycle, baked into HARTOS.

PURPOSE
-------
HARTOS is mostly P2P (PeerLink + WebRTC mesh signaling).  LiveKit is the
FALLBACK SFU that kicks in when:
  - Call has > 4 participants (mesh inefficient)
  - One participant is an AgentVoiceBridge (needs a stable rendezvous URL)

ARCHITECTURE INTENT (per user clarification 2026-05-07)
-------------------------------------------------------
* **regional** — multi-tenant SFU host.  Runs `livekit-server` locally,
  signing JWTs with locally-generated dev keys.  This module spawns the
  binary as a managed subprocess.
* **flat** (single device) — same as regional but typically only one
  tenant.  Supervisor still runs so >4-participant calls work.
* **central** — sync / federation / backup-restore only.  Does NOT run
  an SFU.  This module is a no-op when LIVEKIT_DISABLE=1 or
  HEVOLVE_DEPLOY_MODE=central.
* **embedded** — bundled mobile node.  No SFU; same as central.

ZERO-CONFIG GOAL
----------------
`pip install -e .` plus first start of HARTOS should produce a working
SFU on regional/flat with no manual setup.  This module:
  1. Generates AES-256-grade API key + secret on first start, persists
     them in ~/.hevolve/livekit/dev_keys.json (mode 0600).
  2. Lazy-downloads the official `livekit-server` Go binary from the
     LiveKit GitHub release that matches the pinned version, verifies
     the SHA-256 against an embedded checksum, and installs to
     ~/.hevolve/livekit/livekit-server (or .exe on Windows).
  3. Generates a config file (~/.hevolve/livekit/livekit.yaml) wiring
     the dev keys, port (default 7880), TURN/TCP fallback, and Redis
     when HEVOLVE_REDIS_URL is set (multi-node regional clusters).
  4. Spawns the binary as a daemon-thread-managed subprocess; restarts
     on crash with exponential backoff capped at 60s.
  5. Exposes runtime status via .info() so /health endpoints can report.

The token issuer (livekit_service.py) reads the same dev keys file, so
both sides share a single source of truth — no copy-paste configuration.

This module is INTENTIONALLY decoupled from the binary fetch URL: the
default GitHub release URL can be overridden via LIVEKIT_BINARY_URL for
air-gapped installs, ISO builds (which pre-stage the binary), or
proxied environments.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import platform
import secrets
import shutil
import socket
import stat
import subprocess
import threading
import time
import urllib.request
from pathlib import Path
from typing import Any, Dict, Optional

logger = logging.getLogger('hevolve_social')


# ── Pinned binary version ──────────────────────────────────────────────
# Bump together: VERSION + SHA-256 table.  When upgrading:
#   curl -sL https://github.com/livekit/livekit/releases/download/v$VERSION/checksums.txt
# and copy the hashes for the platforms we ship.
LIVEKIT_VERSION = '1.7.2'

# SHA-256 of the official release artifacts from
# https://github.com/livekit/livekit/releases/download/v1.7.2/checksums.txt
# These are pinned for supply-chain integrity — a download whose hash
# doesn't match is rejected by ensure_binary().
#
# Note: LiveKit does NOT ship darwin (macOS) builds in this release
# series.  Operators on Apple Silicon must either install via Homebrew
# (`brew install livekit-server`) and let the supervisor's PATH lookup
# find it, or set LIVEKIT_BINARY_PATH explicitly.
_LIVEKIT_SHA256 = {
    'linux-amd64':   '7669b1a112449e71ff80cb82460dae7e526e92b3d81e15c70f66a030fac62f4a',
    'linux-arm64':   '482ced7026cbf4c661ab262d04e2d1ba4a723a478bd87028cd27a8a4bcf38035',
    'linux-armv7':   '68a48cf10b2641aaca449ec61018922a2e3294b2682ce0eb9d40ad7fb5e14c2e',
    'windows-amd64': '9589bd307b4a908beaf65c6887f675090a8299f47979447e49a3b2a78d07a1d8',
    'windows-arm64': '746adc54325d82e080c32501e17f66cd1830e937bc496026eb155c06cc6fd257',
    'windows-armv7': '855007017fd5c2043ada6d43d21eb74e1cad8d496a74476be8af9e33bce296bc',
}


# ── Filesystem layout ─────────────────────────────────────────────────
def _hevolve_home() -> Path:
    """`~/.hevolve` — shared HARTOS data dir.  Override via HEVOLVE_HOME."""
    base = os.environ.get('HEVOLVE_HOME')
    if base:
        return Path(base).expanduser()
    return Path.home() / '.hevolve'


def _livekit_home() -> Path:
    return _hevolve_home() / 'livekit'


DEV_KEYS_FILE = 'dev_keys.json'   # {api_key, api_secret, generated_at}
CONFIG_FILE   = 'livekit.yaml'
BINARY_NAME   = 'livekit-server.exe' if os.name == 'nt' else 'livekit-server'


# ── Deploy-mode detection ─────────────────────────────────────────────
def _deploy_mode() -> str:
    """Return one of `flat | regional | central | embedded`.

    Resolution order:
      1. `HEVOLVE_DEPLOY_MODE` env var.
      2. `LIVEKIT_DISABLE=1` → forces 'central' (skip everything).
      3. Default 'flat' (laptop dev / single-device install).
    """
    if os.environ.get('LIVEKIT_DISABLE') == '1':
        return 'central'
    return os.environ.get('HEVOLVE_DEPLOY_MODE', 'flat').lower().strip()


def supervisor_should_run() -> bool:
    """True iff this deploy mode hosts the SFU itself.

    Central + embedded skip everything (sync/federation/backup-only).
    Regional + flat run the supervised binary.

    Override: set `LIVEKIT_AUTOSTART=0` to force-disable, or
    `LIVEKIT_AUTOSTART=1` to force-enable regardless of deploy mode.
    """
    forced = os.environ.get('LIVEKIT_AUTOSTART')
    if forced == '0':
        return False
    if forced == '1':
        return True
    return _deploy_mode() in ('flat', 'regional')


# ── Dev-key bootstrap (shared with livekit_service token issuer) ──────
def ensure_dev_keys() -> Dict[str, str]:
    """Return {api_key, api_secret} — generates + persists on first call.

    Idempotent: same keys returned across restarts.  Safe to call from
    multiple processes (atomic write via temp + rename).  Permissions:
    0700 dir, 0600 file (POSIX).  On Windows we rely on user-profile
    ACLs (CreateDirectoryW restricts to the current user by default).

    Env override: if LIVEKIT_API_KEY + LIVEKIT_API_SECRET are both set,
    those win — we don't write the dev_keys.json.  This is the path
    operators use when deploying with managed LiveKit Cloud or a
    central-issued key/secret pair.
    """
    env_key = os.environ.get('LIVEKIT_API_KEY')
    env_secret = os.environ.get('LIVEKIT_API_SECRET')
    if env_key and env_secret:
        return {'api_key': env_key, 'api_secret': env_secret}

    home = _livekit_home()
    home.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(home, 0o700)
    except OSError:
        pass  # Windows / restricted FS — best effort.

    keys_path = home / DEV_KEYS_FILE
    if keys_path.exists():
        try:
            with keys_path.open('r', encoding='utf-8') as fp:
                data = json.load(fp)
            if data.get('api_key') and data.get('api_secret'):
                return data
        except (OSError, json.JSONDecodeError) as e:
            logger.warning(
                "livekit_supervisor: could not read %s (%s); regenerating",
                keys_path, e)

    # First boot — generate.  LiveKit accepts an arbitrary string for
    # api_key (commonly prefixed `API`); we use a 16-byte random hex for
    # easy diffing in logs.  Secret is 32 bytes (256-bit HMAC key).
    new_keys = {
        'api_key': 'API' + secrets.token_hex(8),
        'api_secret': secrets.token_urlsafe(32),
        'generated_at': int(time.time()),
        'generated_by': 'livekit_supervisor.ensure_dev_keys',
    }
    tmp_path = keys_path.with_suffix('.tmp')
    with tmp_path.open('w', encoding='utf-8') as fp:
        json.dump(new_keys, fp, indent=2)
    try:
        os.chmod(tmp_path, 0o600)
    except OSError:
        pass
    os.replace(tmp_path, keys_path)
    logger.info(
        "livekit_supervisor: generated dev keys (api_key=%s) at %s",
        new_keys['api_key'], keys_path)
    return new_keys


def get_livekit_url() -> str:
    """Resolve the URL clients should connect to.

    Order:
      1. `LIVEKIT_URL` env var (managed cloud or operator override).
      2. `ws://localhost:<LIVEKIT_PORT>` (default 7880) for self-hosted.
    """
    url = os.environ.get('LIVEKIT_URL')
    if url:
        return url
    port = os.environ.get('LIVEKIT_PORT', '7880')
    return f'ws://localhost:{port}'


# ── Binary download + verify ──────────────────────────────────────────
def _platform_tag() -> str:
    sys_name = platform.system().lower()  # 'linux'/'darwin'/'windows'
    machine = platform.machine().lower()
    if machine in ('x86_64', 'amd64'):
        arch = 'amd64'
    elif machine in ('aarch64', 'arm64'):
        arch = 'arm64'
    else:
        arch = machine
    if sys_name == 'darwin':
        return f'darwin-{arch}'
    if sys_name == 'windows':
        return f'windows-{arch}'
    return f'linux-{arch}'


def _binary_url() -> str:
    """Resolve the binary archive URL.  `LIVEKIT_BINARY_URL` lets ISO /
    air-gapped builds point at a local mirror or cached file://.

    LiveKit's release filenames use underscore between os and arch
    (`livekit_1.7.2_linux_amd64.tar.gz`), but our internal platform
    tag uses hyphen (`linux-amd64`) since it doubles as a dict key in
    _LIVEKIT_SHA256.  The translation happens here.
    """
    override = os.environ.get('LIVEKIT_BINARY_URL')
    if override:
        return override
    tag = _platform_tag()                  # e.g. 'linux-amd64'
    url_tag = tag.replace('-', '_')        # e.g. 'linux_amd64'
    ext = 'zip' if tag.startswith('windows-') else 'tar.gz'
    return (
        f'https://github.com/livekit/livekit/releases/download/'
        f'v{LIVEKIT_VERSION}/livekit_{LIVEKIT_VERSION}_{url_tag}.{ext}'
    )


def _verify_sha256(path: Path, tag: str) -> None:
    expected = _LIVEKIT_SHA256.get(tag, '')
    h = hashlib.sha256()
    with path.open('rb') as fp:
        for chunk in iter(lambda: fp.read(1 << 16), b''):
            h.update(chunk)
    actual = h.hexdigest()
    if not expected:
        logger.info(
            "livekit_supervisor: download checksum (%s) = %s — pinning "
            "deferred; set _LIVEKIT_SHA256[%r] in livekit_supervisor.py "
            "to lock this version", tag, actual, tag)
        return
    if expected.lower() != actual.lower():
        raise RuntimeError(
            f'LiveKit binary checksum mismatch for {tag}: '
            f'expected {expected}, got {actual}')


def _find_prestaged_binary() -> Optional[Path]:
    """Look for an already-installed livekit-server in standard
    locations.  Used by Docker (Dockerfile installs to /usr/local/bin)
    and ISO builds (apt/manual install) so the supervisor doesn't
    re-download a binary already present.

    Order: explicit `LIVEKIT_BINARY_PATH` env > `~/.hevolve/livekit/`
    > shutil.which() (PATH lookup).
    """
    explicit = os.environ.get('LIVEKIT_BINARY_PATH')
    if explicit:
        p = Path(explicit).expanduser()
        if p.exists():
            return p

    home_path = _livekit_home() / BINARY_NAME
    if home_path.exists():
        return home_path

    on_path = shutil.which('livekit-server')
    if on_path:
        return Path(on_path)
    return None


def ensure_binary() -> Optional[Path]:
    """Download + extract the livekit-server binary if missing.

    Returns the absolute path to the binary, or None if the download
    couldn't be completed (logged; supervisor will degrade to
    p2p_mesh-only mode).

    Safe to call repeatedly: existence check short-circuits.  Will
    prefer a pre-staged binary (Dockerfile / ISO / apt install) over
    a fresh download.
    """
    pre = _find_prestaged_binary()
    if pre:
        return pre

    home = _livekit_home()
    binary_path = home / BINARY_NAME
    if binary_path.exists():
        return binary_path

    home.mkdir(parents=True, exist_ok=True)
    tag = _platform_tag()
    url = _binary_url()

    logger.info(
        "livekit_supervisor: fetching livekit-server v%s for %s from %s",
        LIVEKIT_VERSION, tag, url)

    archive_ext = 'zip' if url.endswith('.zip') else 'tar.gz'
    archive_path = home / f'livekit-server-{LIVEKIT_VERSION}.{archive_ext}'

    try:
        # Use urllib only — no extra deps.  Set a UA so GitHub's CDN
        # doesn't 403 on default Python signature.
        req = urllib.request.Request(
            url, headers={'User-Agent': 'HARTOS-livekit-supervisor/1.0'})
        with urllib.request.urlopen(req, timeout=120) as resp:
            with archive_path.open('wb') as fp:
                shutil.copyfileobj(resp, fp)
    except Exception as e:  # network / 404 / permissions
        logger.warning(
            "livekit_supervisor: download failed (%s); SFU will not "
            "start.  Calls fall back to P2P mesh.  To install manually: "
            "download %s and extract to %s",
            e, url, home)
        return None

    _verify_sha256(archive_path, tag)

    # Extract binary out of the archive.  LiveKit ships either a .tar.gz
    # (Linux/macOS) or .zip (Windows) containing a single
    # `livekit-server` (or `.exe`) at the root.
    try:
        if archive_ext == 'zip':
            import zipfile
            with zipfile.ZipFile(archive_path) as z:
                for name in z.namelist():
                    base = os.path.basename(name)
                    if base in ('livekit-server', 'livekit-server.exe'):
                        with z.open(name) as src, binary_path.open('wb') as dst:
                            shutil.copyfileobj(src, dst)
                        break
        else:
            import tarfile
            with tarfile.open(archive_path, 'r:gz') as t:
                for member in t.getmembers():
                    base = os.path.basename(member.name)
                    if base == 'livekit-server':
                        f = t.extractfile(member)
                        if f is None:
                            continue
                        with binary_path.open('wb') as dst:
                            shutil.copyfileobj(f, dst)
                        break
    finally:
        try:
            archive_path.unlink()
        except OSError:
            pass

    if not binary_path.exists():
        logger.warning(
            "livekit_supervisor: archive extraction did not produce a "
            "livekit-server binary at %s; SFU disabled", binary_path)
        return None

    # 0755 so it's runnable by the current user.
    try:
        st = binary_path.stat()
        os.chmod(binary_path, st.st_mode | stat.S_IXUSR | stat.S_IXGRP)
    except OSError:
        pass
    logger.info(
        "livekit_supervisor: livekit-server v%s installed at %s",
        LIVEKIT_VERSION, binary_path)
    return binary_path


# ── Config generation ─────────────────────────────────────────────────
def _bind_addresses_for_mode() -> list:
    """Resolve which interface(s) the SFU should listen on.

    Default policy is **silent install** — flat mode binds loopback only
    so first start never triggers a Windows / macOS firewall prompt
    (binding 0.0.0.0 is what triggers the OS dialog; loopback never
    does).  Regional mode binds all interfaces because LAN peers must
    reach the SFU; that single first-start prompt is acceptable for
    the deploy-mode that's explicitly intended to host other users.

    Override priority (highest first):
      1. `LIVEKIT_BIND_HOST` env — single literal address.
            "127.0.0.1" → loopback (silent)
            "0.0.0.0"   → all interfaces (firewall-prompts)
            "192.168.1.50" → specific NIC
      2. Mode-aware default:
            flat / embedded → loopback only (no prompt)
            regional         → all interfaces (one-time prompt)
    """
    override = os.environ.get('LIVEKIT_BIND_HOST', '').strip()
    if override:
        # 0.0.0.0 → empty string = LiveKit "all interfaces" sentinel
        return [''] if override == '0.0.0.0' else [override]
    mode = _deploy_mode()
    if mode == 'regional':
        return ['']  # all interfaces; LAN peers reach us
    # flat / embedded / unknown → loopback (silent first-start)
    return ['127.0.0.1']


def _use_external_ip_for_mode() -> bool:
    """Loopback-bound SFU has no external IP to advertise.  ICE
    candidates from a loopback bind would only confuse remote clients.
    Disable external-IP auto-detection on flat mode; enable on regional.
    """
    bind = _bind_addresses_for_mode()
    # If we're listening on any non-loopback interface, advertise it.
    return not (len(bind) == 1 and bind[0] in ('127.0.0.1', '::1'))


def _generate_config(keys: Dict[str, str]) -> Path:
    """Emit `livekit.yaml` with the dev keys + standard ports.

    Uses the official LiveKit config schema.  Operators can override
    fields by editing the file or setting env vars HARTOS reads on
    next start.
    """
    home = _livekit_home()
    home.mkdir(parents=True, exist_ok=True)
    cfg_path = home / CONFIG_FILE

    port = int(os.environ.get('LIVEKIT_PORT', '7880'))
    rtc_tcp_port = int(os.environ.get('LIVEKIT_RTC_TCP_PORT', '7881'))
    rtc_udp_min = int(os.environ.get('LIVEKIT_RTC_UDP_MIN', '50000'))
    rtc_udp_max = int(os.environ.get('LIVEKIT_RTC_UDP_MAX', '60000'))
    redis_url = os.environ.get('HEVOLVE_REDIS_URL') or os.environ.get(
        'LIVEKIT_REDIS_URL')

    bind_addresses = _bind_addresses_for_mode()
    use_external_ip = _use_external_ip_for_mode()

    yaml_lines = [
        '# Auto-generated by HARTOS livekit_supervisor.  Edit if you',
        '# need to override; HARTOS will not overwrite an existing',
        '# config — delete the file to regenerate from current env.',
        f'port: {port}',
        'bind_addresses:',
    ]
    # YAML list — quote each value so empty-string ("all interfaces")
    # round-trips correctly to LiveKit's parser.
    for addr in bind_addresses:
        yaml_lines.append(f"  - '{addr}'")
    yaml_lines += [
        'rtc:',
        f'  tcp_port: {rtc_tcp_port}',
        f'  port_range_start: {rtc_udp_min}',
        f'  port_range_end: {rtc_udp_max}',
        f'  use_external_ip: {str(use_external_ip).lower()}',
        'keys:',
        f'  {keys["api_key"]}: {keys["api_secret"]}',
        'logging:',
        '  level: info',
        '  json: false',
    ]
    if redis_url:
        yaml_lines += [
            'redis:',
            f'  address: {redis_url}',
        ]

    if not cfg_path.exists():
        with cfg_path.open('w', encoding='utf-8') as fp:
            fp.write('\n'.join(yaml_lines) + '\n')
        try:
            os.chmod(cfg_path, 0o600)
        except OSError:
            pass
        logger.info(
            "livekit_supervisor: wrote default config at %s", cfg_path)
    else:
        logger.info(
            "livekit_supervisor: keeping existing config at %s "
            "(delete to regenerate)", cfg_path)
    return cfg_path


def _port_in_use(port: int) -> bool:
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(0.5)
            return s.connect_ex(('127.0.0.1', port)) == 0
    except OSError:
        return False


from core.process_supervisor import ProcessSupervisor


# ── Supervisor lifecycle ──────────────────────────────────────────────
class _Supervisor(ProcessSupervisor):
    """Single instance per HARTOS process.  Started by start_supervisor()
    and lives for the lifetime of the process.  Daemon thread → exits
    cleanly when the parent dies.  The spawn/stream/backoff loop + stop()
    live in core.process_supervisor.ProcessSupervisor (#110).
    """

    name = 'livekit'

    def __init__(self) -> None:
        super().__init__()
        self.binary: Optional[Path] = None
        self.config: Optional[Path] = None
        self.url: str = get_livekit_url()

    def start(self) -> Dict[str, Any]:
        """Provision keys + binary + config; spawn the supervisor thread.

        Returns an info dict with status the caller can log or surface
        on a health endpoint.
        """
        if self.thread is not None and self.thread.is_alive():
            return self.info()

        keys = ensure_dev_keys()
        self.binary = ensure_binary()
        self.config = _generate_config(keys)

        if self.binary is None:
            self.last_error = (
                'binary unavailable — calls fall back to P2P mesh only')
            logger.warning(
                "livekit_supervisor: %s", self.last_error)
            return self.info()

        port = int(os.environ.get('LIVEKIT_PORT', '7880'))
        if _port_in_use(port):
            self.last_error = (
                f'port {port} already in use; assuming an operator-'
                f'managed livekit-server is running and skipping spawn')
            logger.info("livekit_supervisor: %s", self.last_error)
            return self.info()

        self._spawn_thread()
        return self.info()

    def _build_popen(self):
        # Hide the cmd console window on Windows via the canonical
        # core.subprocess_safe.hidden_popen_kwargs (no inline os.name checks).
        from core.subprocess_safe import hidden_popen_kwargs
        cmd = [str(self.binary), '--config', str(self.config)]
        return cmd, dict(
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            cwd=str(_livekit_home()),
            **hidden_popen_kwargs(),
        )

    def _format_stdout_line(self, line: str) -> None:
        logger.info('livekit-server: %s', line)

    def info(self) -> Dict[str, Any]:
        running = (
            self.proc is not None
            and self.proc.poll() is None
            and self.thread is not None
            and self.thread.is_alive()
        )
        return {
            'mode': _deploy_mode(),
            'should_run': supervisor_should_run(),
            'binary_path': str(self.binary) if self.binary else None,
            'config_path': str(self.config) if self.config else None,
            'url': self.url,
            'running': running,
            'restart_count': self.restart_count,
            'last_started': self.last_started,
            'last_error': self.last_error,
        }


_INSTANCE: Optional[_Supervisor] = None
_INSTANCE_LOCK = threading.Lock()


def start_supervisor() -> Dict[str, Any]:
    """Idempotent entrypoint — call once during HARTOS bootstrap.

    No-op when the deploy mode shouldn't host an SFU (central /
    embedded), preserving the architectural intent that central
    instances do sync/federation/backup-restore only.
    """
    global _INSTANCE
    if not supervisor_should_run():
        return {
            'mode': _deploy_mode(),
            'should_run': False,
            'reason':
                'deploy mode does not host an SFU; calls use P2P mesh '
                'and any operator-managed external LiveKit URL',
        }

    with _INSTANCE_LOCK:
        if _INSTANCE is None:
            _INSTANCE = _Supervisor()
        return _INSTANCE.start()


def stop_supervisor() -> None:
    """Process-shutdown hook.  Safe to call when supervisor never ran."""
    global _INSTANCE
    with _INSTANCE_LOCK:
        if _INSTANCE is not None:
            _INSTANCE.stop()
            _INSTANCE = None


def supervisor_info() -> Dict[str, Any]:
    """Read-only state — useful for /health, debug pages, tests."""
    with _INSTANCE_LOCK:
        if _INSTANCE is None:
            return {
                'mode': _deploy_mode(),
                'should_run': supervisor_should_run(),
                'running': False,
                'reason': 'not yet started',
            }
        return _INSTANCE.info()


__all__ = [
    'LIVEKIT_VERSION',
    'ensure_dev_keys',
    'get_livekit_url',
    'ensure_binary',
    'supervisor_should_run',
    'start_supervisor',
    'stop_supervisor',
    'supervisor_info',
]
