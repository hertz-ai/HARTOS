"""Browser Research — driver abstraction.

Two modes, one interface:
  B2 (default) — attach over CDP to a Chrome that listens on a debug port.
                 With an `identity` (an agent's mailbox), that Chrome is the
                 agent's OWN profile: its own --user-data-dir and its own
                 --remote-debugging-port, started here headless or with a
                 window (see "Per-agent profiles" below).  Since Chrome 136
                 the debug port is ignored on the person's everyday profile
                 folder, so attaching to "the user's running Chrome on 9222"
                 no longer works there.
  B1 (fallback) — Obscura launches its own stealth Chrome profile under
                  <data_dir>/browser_profile/.  Used when B2's CDP endpoint
                  is unreachable.

Per-agent profiles (owner 2026-11-24, verbatim: "each agent needs a differeny
mailbox investor session needs sathish@hevolve.ai and the email provider is
same unless you have multiple instance of chrome the session cookie will be
shared and mailbox will not be isolated"; owner 2026-11-27, verbatim: "the
llm shd be able to work on or use headless and with head browser (no-GUI) and
(with GUI) actions respectivelu"): every identity gets one profile folder
under <data_dir>/browser_profiles/ and one debug port from AGENT_CDP_PORTS,
recorded in that folder's ports.json (this module is its only writer).  The
person signs in once in a headed run; headless runs reuse the folder.

Lazy import: Obscura/Playwright bindings are NOT imported at module load.
A T3 tool (YouTube, web fetch) never touches this module's get_driver(); only
T2 tools (C4+) call into it.

C1 ships the interface + B2 probe only.  Actual Obscura subprocess launch
lands in C4 when the first T2 platform script needs it.
"""
import hashlib
import json
import logging
import os
import re
import shutil
import socket
import subprocess
import sys
import threading
import time
import urllib.request
from abc import ABC, abstractmethod
from typing import Optional

logger = logging.getLogger('browser_research.driver')

# B2 CDP attach: standard Chrome remote-debugging port.
CDP_PORT_DEFAULT = 9222
CDP_HOST_DEFAULT = '127.0.0.1'
CDP_PROBE_TIMEOUT_S = 1.5


def cdp_endpoint_reachable(host: str = CDP_HOST_DEFAULT, port: int = CDP_PORT_DEFAULT) -> bool:
    """Cheap TCP probe — is there a Chrome listening on the CDP port?

    Used to decide B2 vs B1 at driver init.  Does NOT speak CDP — just opens
    a TCP socket and closes it.  Sub-2-second timeout so it never blocks.
    """
    try:
        with socket.create_connection((host, port), timeout=CDP_PROBE_TIMEOUT_S):
            return True
    except (OSError, socket.timeout):
        return False


# ── Per-agent profiles ─────────────────────────────────────────────────
# One Chrome profile folder and one debug port per identity (a mailbox), so
# two agents' sign-ins never share a cookie jar.  Ports come from a range of
# their own, away from the 9222 people use by hand.
AGENT_CDP_PORTS = range(9300, 9400)
_PORTS_FILE = 'ports.json'
_PORTS_LOCK = threading.Lock()
AGENT_START_WAIT_S = 20.0


class AgentBrowserUnavailable(OSError):
    """An agent's browser could not be started or reached.  The message says
    what was wrong and what to do, as every refusal here must."""


def agent_profiles_root() -> str:
    """The folder that holds every agent profile (<data_dir>/browser_profiles)."""
    try:
        from core.platform_paths import get_data_dir
        return os.path.join(get_data_dir(), 'browser_profiles')
    except Exception:
        return os.path.join(os.getcwd(), '.browser_profiles')


def _identity_key(identity: str) -> str:
    """One mailbox is one mailbox whatever its letter case or edge spaces."""
    return (identity or '').strip().lower()


def agent_profile_slug(identity: str) -> str:
    """A folder name for `identity`: a readable part plus a digest of the
    whole name.  The readable part keeps only [a-z0-9._-] with no leading
    dot, so no name can reach outside the root ('..', '/etc', 'C:\\'), and
    the digest keeps two names that read alike ('a@b.com', 'a_b.com') in
    two folders.  The digest names a folder; it secures nothing."""
    key = _identity_key(identity)
    readable = re.sub(r'[^a-z0-9._-]+', '_', key).strip('._')[:48] or 'agent'
    digest = hashlib.sha1(key.encode('utf-8'), usedforsecurity=False).hexdigest()[:10]
    return f'{readable}-{digest}'


def agent_profile_dir(identity: str) -> str:
    """The profile folder (Chrome's --user-data-dir) for `identity`."""
    return os.path.join(agent_profiles_root(), agent_profile_slug(identity))


def _port_free(port: int) -> bool:
    from core.port_registry import check_port_available
    return check_port_available(port, host=CDP_HOST_DEFAULT)


def _ports_path() -> str:
    return os.path.join(agent_profiles_root(), _PORTS_FILE)


def _read_ports() -> dict:
    try:
        with open(_ports_path(), encoding='utf-8') as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as exc:
        logger.warning('agent port record unreadable (%s); starting a new one', exc)
        return {}


def _write_ports(ports: dict) -> None:
    path = _ports_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(ports, f, indent=1, sort_keys=True)
    os.replace(tmp, path)


def agent_cdp_port(identity: str) -> int:
    """The debug port that belongs to `identity`, the same one every time.

    The first ask takes the lowest port in AGENT_CDP_PORTS that no other
    identity holds and no other program is bound to, and records it."""
    key = _identity_key(identity)
    with _PORTS_LOCK:
        ports = _read_ports()
        if key in ports:
            return int(ports[key])
        taken = {int(p) for p in ports.values()}
        for port in AGENT_CDP_PORTS:
            if port in taken or not _port_free(port):
                continue
            ports[key] = port
            _write_ports(ports)
            return port
    raise AgentBrowserUnavailable(
        f'no free debug port is left for {identity!r}: every port '
        f'{AGENT_CDP_PORTS.start}-{AGENT_CDP_PORTS.stop - 1} is taken. Close '
        f'agent browsers that are no longer needed, or remove stale entries '
        f'from {_ports_path()}.')


def find_chrome() -> Optional[str]:
    """The installed Chrome to start, or None.  HEVOLVE_CHROME_PATH wins."""
    named = os.environ.get('HEVOLVE_CHROME_PATH', '').strip()
    if named and os.path.isfile(named):
        return named
    candidates = []
    if sys.platform == 'win32':
        try:
            import winreg
            for hive in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
                try:
                    with winreg.OpenKey(hive, r'SOFTWARE\Microsoft\Windows'
                                        r'\CurrentVersion\App Paths\chrome.exe') as key:
                        candidates.append(winreg.QueryValueEx(key, None)[0])
                except OSError:
                    continue
        except ImportError:
            pass
        for var in ('PROGRAMFILES', 'PROGRAMFILES(X86)', 'LOCALAPPDATA'):
            base = os.environ.get(var)
            if base:
                candidates.append(os.path.join(base, 'Google', 'Chrome',
                                               'Application', 'chrome.exe'))
    elif sys.platform == 'darwin':
        app = os.path.join('Google Chrome.app', 'Contents', 'MacOS', 'Google Chrome')
        candidates += [os.path.join('/Applications', app),
                       os.path.join(os.path.expanduser('~'), 'Applications', app)]
    else:
        for name in ('google-chrome', 'google-chrome-stable', 'chromium',
                     'chromium-browser'):
            found = shutil.which(name)
            if found:
                candidates.append(found)
    for path in candidates:
        if path and os.path.isfile(path):
            return path
    return None


def agent_chrome_command(identity: str, headless: bool = False,
                         url: Optional[str] = None) -> list:
    """The command that starts `identity`'s own Chrome, headless or headed."""
    chrome = find_chrome()
    if not chrome:
        raise AgentBrowserUnavailable(
            'no Chrome was found on this computer, so an agent browser cannot '
            'start. Install Google Chrome, or set HEVOLVE_CHROME_PATH to the '
            'full path of chrome.exe (or the Chrome program on macOS/Linux).')
    cmd = [chrome,
           f'--user-data-dir={agent_profile_dir(identity)}',
           f'--remote-debugging-port={agent_cdp_port(identity)}',
           '--no-first-run', '--no-default-browser-check']
    if headless:
        cmd.append('--headless')
    if url:
        cmd.append(url)
    return cmd


def agent_browser_state(port: int) -> Optional[str]:
    """'headless' or 'headed' when a Chrome answers on `port`, else None.
    Chrome's /json/version user agent says HeadlessChrome when it has no
    window."""
    try:
        with urllib.request.urlopen(
                f'http://{CDP_HOST_DEFAULT}:{port}/json/version',
                timeout=CDP_PROBE_TIMEOUT_S) as resp:
            info = json.loads(resp.read().decode('utf-8', 'replace'))
    except (OSError, ValueError):
        return None
    agent = str(info.get('User-Agent', ''))
    return 'headless' if 'HeadlessChrome' in agent else 'headed'


def close_agent_browser(identity: str) -> bool:
    """Stop the Chrome that runs `identity`'s profile.  True when one was
    stopped.  Only processes started on that exact profile folder are
    touched, never the person's own Chrome."""
    import psutil
    marker = f'--user-data-dir={agent_profile_dir(identity)}'.lower()
    stopped = []
    for proc in psutil.process_iter(['cmdline']):
        try:
            cmdline = ' '.join(proc.info.get('cmdline') or []).lower()
            if marker in cmdline:
                proc.terminate()
                stopped.append(proc)
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    if stopped:
        psutil.wait_procs(stopped, timeout=10)
    return bool(stopped)


def _launch(cmd: list) -> None:
    """Start Chrome detached, so it outlives the turn that started it."""
    kwargs = {'stdin': subprocess.DEVNULL, 'stdout': subprocess.DEVNULL,
              'stderr': subprocess.DEVNULL, 'close_fds': True}
    if sys.platform == 'win32':
        kwargs['creationflags'] = (getattr(subprocess, 'DETACHED_PROCESS', 0)
                                   | getattr(subprocess, 'CREATE_NEW_PROCESS_GROUP', 0))
    else:
        kwargs['start_new_session'] = True
    subprocess.Popen(cmd, **kwargs)


def ensure_agent_browser(identity: str, headless: bool = False,
                         url: Optional[str] = None,
                         wait_s: float = AGENT_START_WAIT_S) -> str:
    """Make sure `identity`'s own Chrome runs, and return its CDP endpoint.

    A running one is reused.  A window that is already open also serves a
    headless ask; a headless run is restarted when a window is wanted (GUI
    actions need one).  A start that never answers raises
    AgentBrowserUnavailable saying what to do."""
    port = agent_cdp_port(identity)
    endpoint = f'http://{CDP_HOST_DEFAULT}:{port}'
    state = agent_browser_state(port)
    if state == 'headed' or (state == 'headless' and headless):
        return endpoint
    if state == 'headless':
        logger.info('agent browser for %s runs headless; restarting it with a '
                    'window', identity)
        close_agent_browser(identity)
    cmd = agent_chrome_command(identity, headless=headless, url=url)
    os.makedirs(agent_profile_dir(identity), exist_ok=True)
    _launch(cmd)
    deadline = time.monotonic() + wait_s
    while True:
        if agent_browser_state(port):
            logger.info('agent browser for %s is up on port %d (%s)', identity,
                        port, 'headless' if headless else 'with a window')
            return endpoint
        if time.monotonic() >= deadline:
            raise AgentBrowserUnavailable(
                f'the agent browser for {identity!r} did not answer on port '
                f'{port} within {wait_s:.0f} s. Check that no other program '
                f'holds that port and that Chrome starts on this computer; '
                f'its profile folder is {agent_profile_dir(identity)}.')
        time.sleep(0.25)


class BrowserDriver(ABC):
    """Abstract driver.  Concrete: ObscuraB2Driver (CDP), ObscuraB1Driver (headless)."""

    connection_mechanism: str = 'unknown'

    @abstractmethod
    def goto(self, url: str) -> None: ...

    @abstractmethod
    def evaluate(self, script: str) -> object: ...

    @abstractmethod
    def close(self) -> None: ...


class ObscuraB2Driver(BrowserDriver):
    """Attach to user's running Chrome via CDP at port 9222.

    Stub in C1 — concrete CDP-client wiring lands in C4 when first needed.
    """
    connection_mechanism = 'obscura_b2_cdp_user_chrome'

    def __init__(self, host: str = CDP_HOST_DEFAULT, port: int = CDP_PORT_DEFAULT) -> None:
        self.host = host
        self.port = port
        # Concrete obscura/CDP client init lands in C4.

    def goto(self, url: str) -> None:
        raise NotImplementedError('ObscuraB2Driver lands in C4 (first T2 platform).')

    def evaluate(self, script: str) -> object:
        raise NotImplementedError('ObscuraB2Driver lands in C4 (first T2 platform).')

    def close(self) -> None:
        pass


class ObscuraB1Driver(BrowserDriver):
    """Launch a Nunba-managed Obscura headless profile.

    Stub in C1 — concrete subprocess + profile dir lands in C4.
    """
    connection_mechanism = 'obscura_b1_headless_profile'

    def __init__(self, profile_dir: Optional[str] = None) -> None:
        if profile_dir is None:
            try:
                from core.platform_paths import get_data_dir
                profile_dir = os.path.join(get_data_dir(), 'browser_profile')
            except Exception:
                profile_dir = os.path.join(os.getcwd(), '.browser_profile')
        self.profile_dir = profile_dir

    def goto(self, url: str) -> None:
        raise NotImplementedError('ObscuraB1Driver lands in C4 (first T2 platform).')

    def evaluate(self, script: str) -> object:
        raise NotImplementedError('ObscuraB1Driver lands in C4 (first T2 platform).')

    def close(self) -> None:
        pass


def get_driver(mode: str = 'auto', identity: Optional[str] = None,
               headless: bool = False) -> BrowserDriver:
    """Resolve the right driver for current host state.

    identity (an agent's mailbox): attach to that agent's OWN Chrome profile,
    started here headless or with a window when it is not running yet (see
    ensure_agent_browser); `mode` is not consulted.
    mode='auto' (default): try B2 (attach to a Chrome on 9222); fall back to B1.
    mode='b2': force B2 (raises if no CDP endpoint).
    mode='b1': force B1 (Obscura headless).
    """
    if identity:
        ensure_agent_browser(identity, headless=headless)
        return ObscuraB2Driver(port=agent_cdp_port(identity))
    if mode == 'b2':
        if not cdp_endpoint_reachable():
            raise RuntimeError(
                'B2 driver requested but no Chrome is listening on the CDP '
                'port. Since Chrome 136 the debug port is ignored on the '
                'everyday profile folder, so starting that Chrome with '
                '--remote-debugging-port=9222 no longer works; pass '
                'identity=<the agent\'s mailbox> so the agent gets its own '
                'Chrome profile, or use mode="b1".'
            )
        return ObscuraB2Driver()
    if mode == 'b1':
        return ObscuraB1Driver()
    if mode == 'auto':
        if cdp_endpoint_reachable():
            logger.debug('B2 reachable, using ObscuraB2Driver')
            return ObscuraB2Driver()
        logger.debug('B2 unreachable, falling back to ObscuraB1Driver')
        return ObscuraB1Driver()
    raise ValueError(f'unknown driver mode: {mode!r}')
