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
# How a driver attached to an agent's own profile reports itself.  Not
# 'obscura_b2_cdp_user_chrome': scripts/_base.py reads that one as the
# person's own signed-in Chrome.
AGENT_PROFILE_MECHANISM = 'agent_profile_cdp'
_USER_DATA_DIR_ARG = '--user-data-dir='
_DEBUG_PORT_ARG = '--remote-debugging-port='
_PROCESS_TYPE_ARG = '--type='
# Names Chrome's processes run under on Windows, macOS and Linux.  The
# program find_chrome() returns is added where they are matched, so a
# browser named by HEVOLVE_CHROME_PATH is recognised too.
_LINUX_CHROME_PROGRAMS = ('google-chrome', 'google-chrome-stable', 'chromium',
                          'chromium-browser')
_CHROME_PROCESS_NAMES = frozenset(
    ('chrome.exe', 'chrome', 'google chrome') + _LINUX_CHROME_PROGRAMS)
# How long a running agent browser may take to answer, or to bind its debug
# port while it starts, before the answer counts as "none".  A slow answer is
# never a reason to stop it: it may hold a sign-in in progress.
ANSWER_GRACE_S = 5.0
_GRACE_STEP_S = 0.25
_WARNED_ENTRIES = set()


class AgentBrowserUnavailable(OSError):
    """An agent's browser could not be started or reached.  The message says
    what was wrong and what to do, as every refusal here must."""


def agent_profiles_root() -> str:
    """The folder that holds every agent profile (<data_dir>/browser_profiles)."""
    try:
        from core.platform_paths import get_data_dir
        return os.path.join(get_data_dir(), 'browser_profiles')
    except Exception as exc:
        fallback = os.path.join(os.getcwd(), '.browser_profiles')
        logger.warning('the data folder could not be resolved (%s); agent '
                       'browser profiles go under %s', exc, fallback)
        return fallback


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
    """The port record, keeping only entries whose port is a whole number
    in AGENT_CDP_PORTS.  A record that cannot be read, or an entry that is
    not such a port, is logged and left out, so the next write drops it."""
    path = _ports_path()
    try:
        with open(path, encoding='utf-8') as f:
            data = json.load(f)
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as exc:
        logger.warning('agent port record %s is unreadable (%s); starting a '
                       'new one', path, exc)
        return {}
    if not isinstance(data, dict):
        logger.warning('agent port record %s is not a mapping; starting a new '
                       'one', path)
        return {}
    ports = {}
    for key, port in data.items():
        # A hand-edited key in capitals is the same mailbox; the first entry
        # for a mailbox wins.
        mailbox = _identity_key(str(key))
        # bool is an int subclass; true/false in the file is not a port.
        if type(port) is int and port in AGENT_CDP_PORTS:
            ports.setdefault(mailbox, port)
        elif (path, key, repr(port)) not in _WARNED_ENTRIES:
            # Once per entry per process: the record keeps it until the next
            # write, and every read would otherwise repeat the warning.
            _WARNED_ENTRIES.add((path, key, repr(port)))
            logger.warning('agent port record %s: dropped the entry for %r '
                           '(%r is not a port in %d-%d)', path, key, port,
                           AGENT_CDP_PORTS.start, AGENT_CDP_PORTS.stop - 1)
    return ports


def _write_ports(ports: dict) -> None:
    path = _ports_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(ports, f, indent=1, sort_keys=True)
    os.replace(tmp, path)


def _free_agent_port(ports: dict, avoid: Optional[int] = None) -> Optional[int]:
    """The lowest port in AGENT_CDP_PORTS that no recorded identity holds,
    is not `avoid`, and no program is bound to; None when there is none."""
    taken = set(ports.values())
    for port in AGENT_CDP_PORTS:
        if port != avoid and port not in taken and _port_free(port):
            return port
    return None


def _no_port_left(identity: str) -> AgentBrowserUnavailable:
    return AgentBrowserUnavailable(
        f'no free debug port is left for {identity!r}: every port '
        f'{AGENT_CDP_PORTS.start}-{AGENT_CDP_PORTS.stop - 1} is taken. Close '
        f'agent browsers that are no longer needed, or remove stale entries '
        f'from {_ports_path()}.')


def agent_cdp_port(identity: str) -> int:
    """The debug port that belongs to `identity`, the same one every time.

    The first ask takes the lowest port in AGENT_CDP_PORTS that no other
    identity holds and no other program is bound to, and records it.

    The lock covers one process only.  Two processes asking at the same
    moment can record one port twice or lose an entry.  ensure_agent_browser
    corrects that: it trusts a port only once this profile's own Chrome is
    seen listening on it, and it moves a record whose port another program
    holds."""
    key = _identity_key(identity)
    with _PORTS_LOCK:
        ports = _read_ports()
        if key in ports:
            return ports[key]
        port = _free_agent_port(ports)
        if port is not None:
            ports[key] = port
            _write_ports(ports)
            return port
    raise _no_port_left(identity)


def _set_agent_port(identity: str, port: Optional[int] = None,
                    avoid: Optional[int] = None) -> int:
    """Record `identity`'s port as `port` (its Chrome already runs there),
    or, with no `port`, as a new free one that is not `avoid` (another
    program holds the old one).  Returns the recorded port."""
    key = _identity_key(identity)
    with _PORTS_LOCK:
        ports = _read_ports()
        ports.pop(key, None)
        if port is None:
            port = _free_agent_port(ports, avoid=avoid)
            if port is None:
                raise _no_port_left(identity)
        ports[key] = port
        _write_ports(ports)
    return port


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
        for name in _LINUX_CHROME_PROGRAMS:
            found = shutil.which(name)
            if found:
                candidates.append(found)
    for path in candidates:
        if path and os.path.isfile(path):
            return path
    return None


def agent_chrome_command(identity: str, headless: bool = False,
                         url: Optional[str] = None,
                         port: Optional[int] = None) -> list:
    """The command that starts `identity`'s own Chrome, headless or headed,
    on `port` (ensure_agent_browser passes the one it settled on, so a record
    another process rewrote in between cannot split the launch from the
    wait); the recorded port when none is given."""
    chrome = find_chrome()
    if not chrome:
        raise AgentBrowserUnavailable(
            'no Chrome was found on this computer, so an agent browser cannot '
            'start. Install Google Chrome, or set HEVOLVE_CHROME_PATH to the '
            'full path of chrome.exe (or the Chrome program on macOS/Linux).')
    cmd = [chrome,
           f'--user-data-dir={agent_profile_dir(identity)}',
           f'--remote-debugging-port={port or agent_cdp_port(identity)}',
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


def _norm_path(path: str) -> str:
    return os.path.normcase(os.path.normpath(path))


def _arg_value(argv: list, prefix: str) -> Optional[str]:
    """The value of the first `prefix`<value> argument in argv, or None."""
    for arg in argv:
        if arg.startswith(prefix):
            return arg[len(prefix):]
    return None


def _agent_processes(identity: str) -> list:
    """(process, argv) for every Chrome process started on exactly
    `identity`'s profile folder: a Chrome program whose --user-data-dir is
    that folder, compared as a path, never as a substring of the command
    line (so neither a shell that merely mentions the folder nor a profile
    whose folder name starts the same is matched).  A Chrome process whose
    command line cannot be read is skipped and counted in the debug log."""
    import psutil
    folder = _norm_path(agent_profile_dir(identity))
    names = set(_CHROME_PROCESS_NAMES)
    chrome = find_chrome()
    if chrome:
        names.add(os.path.basename(chrome).lower())
    found, unreadable = [], 0
    # Names first, command lines only for Chrome: reading every process's
    # command line cost ~2 s per call on Windows (measured 2026-11-27).
    for proc in psutil.process_iter(['name']):
        if (proc.info.get('name') or '').lower() not in names:
            continue
        try:
            argv = proc.cmdline()
        except (psutil.NoSuchProcess, psutil.ZombieProcess):
            continue
        except psutil.AccessDenied:
            unreadable += 1
            continue
        value = _arg_value(argv, _USER_DATA_DIR_ARG)
        if value and _norm_path(value.strip('"')) == folder:
            found.append((proc, argv))
    if unreadable:
        logger.debug('%d Chrome processes had a command line this user cannot '
                     'read; they were not checked against %s', unreadable, folder)
    return found


def _profile_debug_port(identity: str) -> Optional[int]:
    """The port `identity`'s own Chrome really listens on: the browser
    process of that profile (the one without --type=; measured 2026-11-27,
    it owns the DevTools listener) holding a listening socket on the port
    its command line names.  None when no Chrome runs on the profile, or it
    runs without that listener (another program had the port).

    Only a port seen here is trusted as this agent's browser, so another
    profile's Chrome answering on a recorded port is never taken for it.

    "Could not look" is not "not listening" (core.resource_governor
    ._listen_rows keeps the same difference for the whole-machine table,
    which needs root on macOS, so this reads the profile's own processes):
    when the browser's sockets cannot be read and no listener was seen,
    AgentBrowserUnavailable is raised and the browser is left running."""
    import psutil
    denied = None
    for proc, argv in _agent_processes(identity):
        if _arg_value(argv, _PROCESS_TYPE_ARG) is not None:
            continue
        declared = _arg_value(argv, _DEBUG_PORT_ARG) or ''
        if not declared.isdigit():
            continue
        # psutil 6 renamed connections() to net_connections().
        read = getattr(proc, 'net_connections', None) or proc.connections
        try:
            conns = read(kind='tcp')
        except psutil.NoSuchProcess:
            continue
        except psutil.AccessDenied as exc:
            denied = exc
            continue
        if any(c.status == psutil.CONN_LISTEN and c.laddr
               and c.laddr.port == int(declared) for c in conns):
            return int(declared)
    if denied is not None:
        raise AgentBrowserUnavailable(
            f'the agent browser for {identity!r} runs, but this account may '
            f'not read its network sockets ({denied}), so it cannot be told '
            f'apart from another program\'s browser. It was left running. '
            f'Run HARTOS as the user who started that Chrome, or close the '
            f'Chrome that uses the profile folder {agent_profile_dir(identity)} '
            f'and ask again.')
    return None


def close_agent_browser(identity: str) -> bool:
    """Stop the Chrome that runs `identity`'s profile.  True when it ran and
    every one of its processes is gone.  Only Chrome processes started on
    exactly that profile folder are touched, never the person's own Chrome
    or another agent's.  One still running 10 s after the stop is killed."""
    import psutil
    procs = [proc for proc, _argv in _agent_processes(identity)]
    if not procs:
        return False
    for proc in procs:
        try:
            proc.terminate()
        except psutil.NoSuchProcess:
            continue
        except psutil.AccessDenied as exc:
            logger.warning('Chrome process %s of %s could not be stopped: %s',
                           proc.pid, agent_profile_dir(identity), exc)
    _gone, alive = psutil.wait_procs(procs, timeout=10)
    for proc in alive:
        try:
            proc.kill()
        except (psutil.NoSuchProcess, psutil.AccessDenied) as exc:
            logger.warning('Chrome process %s of %s could not be killed: %s',
                           proc.pid, agent_profile_dir(identity), exc)
    if alive:
        _gone, alive = psutil.wait_procs(alive, timeout=5)
    return not alive


def _launch(cmd: list) -> subprocess.Popen:
    """Start Chrome detached, so it outlives the turn that started it."""
    kwargs = {'stdin': subprocess.DEVNULL, 'stdout': subprocess.DEVNULL,
              'stderr': subprocess.DEVNULL, 'close_fds': True}
    if sys.platform == 'win32':
        kwargs['creationflags'] = (getattr(subprocess, 'DETACHED_PROCESS', 0)
                                   | getattr(subprocess, 'CREATE_NEW_PROCESS_GROUP', 0))
    else:
        kwargs['start_new_session'] = True
    return subprocess.Popen(cmd, **kwargs)


def _endpoint(port: int) -> str:
    return f'http://{CDP_HOST_DEFAULT}:{port}'


def _mode_words(mode: Optional[str]) -> str:
    return {'headed': 'with a window', 'headless': 'headless'}.get(mode, 'nothing')


def _cannot_stop(identity: str, state: Optional[str], port: int,
                 want: str) -> AgentBrowserUnavailable:
    return AgentBrowserUnavailable(
        f'the agent browser for {identity!r} runs {_mode_words(state)} on port '
        f'{port} and could not be stopped to start it {_mode_words(want)}. '
        f'Close the Chrome that uses the profile folder '
        f'{agent_profile_dir(identity)} (or end it in the task manager) and '
        f'ask again.')


def ensure_agent_browser(identity: str, headless: bool = False,
                         url: Optional[str] = None,
                         wait_s: float = AGENT_START_WAIT_S) -> str:
    """Make sure `identity`'s own Chrome runs, and return its CDP endpoint.

    Only a port this profile's own Chrome is seen listening on is used, so
    an agent never attaches to another profile's browser; the record
    follows a Chrome found running on another port.  A running one is
    reused; one with a window also serves a headless ask.  A headless run is
    stopped and started again with a window when a window is wanted (GUI
    actions need one).  A running browser is never stopped because it is
    slow: one that holds its listener but does not answer within
    ANSWER_GRACE_S is left running and the call says so (it may hold a
    sign-in in progress).  A Chrome on the profile without a listener is
    given the same grace to bind; an agent start that lost its port is then
    started again, and a Chrome opened by hand on the folder (no debug port)
    is left running.  When another program holds the recorded port the
    profile moves to a free one.  A start counts only when the profile's own
    Chrome answers in the mode asked for; otherwise it is stopped and
    AgentBrowserUnavailable says what to do."""
    want = 'headless' if headless else 'headed'
    live = _profile_debug_port(identity)
    if live is None and _agent_processes(identity):
        live = _within_grace(lambda: _profile_debug_port(identity))
        if live is None:
            _restart_portless(identity, want)
    if live is not None:
        if live != agent_cdp_port(identity) and live in AGENT_CDP_PORTS:
            logger.info('agent browser for %s already runs on port %d; the '
                        'record now says so', identity, live)
            _set_agent_port(identity, port=live)
        state = _within_grace(lambda: agent_browser_state(live))
        if state == 'headed' or state == want:
            return _endpoint(live)
        if state is None:
            raise AgentBrowserUnavailable(
                f'the agent browser for {identity!r} holds port {live} but did '
                f'not answer within {ANSWER_GRACE_S:g} s. It was left running: '
                f'it may be busy, or a sign-in may be open in it. Ask again in '
                f'a moment; if it stays silent, close the Chrome that uses the '
                f'profile folder {agent_profile_dir(identity)}.')
        logger.info('agent browser for %s runs headless on port %d; starting '
                    'it again with a window', identity, live)
        if not close_agent_browser(identity):
            raise _cannot_stop(identity, state, live, want)
    port = agent_cdp_port(identity)
    if not _port_free(port):
        held = port
        port = _set_agent_port(identity, avoid=held)
        logger.warning('port %d recorded for the agent browser of %s is held '
                       'by another program; moved it to %d', held, identity, port)
    cmd = agent_chrome_command(identity, headless=headless, url=url, port=port)
    os.makedirs(agent_profile_dir(identity), exist_ok=True)
    return _wait_until_up(identity, port, want, _launch(cmd), wait_s)


def _within_grace(read):
    """The first non-None answer of `read` within ANSWER_GRACE_S, else None.
    Counted in steps, not by the clock, so a stubbed sleep stays fast."""
    for _ in range(int(ANSWER_GRACE_S / _GRACE_STEP_S) + 1):
        answer = read()
        if answer is not None:
            return answer
        time.sleep(_GRACE_STEP_S)
    return None


def _restart_portless(identity: str, want: str) -> None:
    """Chrome runs on `identity`'s profile but never bound a debug port.  An
    agent start names one (--remote-debugging-port) and lost it: stop it so
    it can start again.  A Chrome opened by hand on the folder names none:
    it may be the owner's own window, so it is left running and the call
    says what to do."""
    started_by_agent = any(
        _arg_value(argv, _DEBUG_PORT_ARG)
        for _proc, argv in _agent_processes(identity)
        if _arg_value(argv, _PROCESS_TYPE_ARG) is None)
    if not started_by_agent:
        raise AgentBrowserUnavailable(
            f'a Chrome opened by hand runs on the profile folder of '
            f'{identity!r} ({agent_profile_dir(identity)}) without a debug '
            f'port. It was left running. Close it and ask again, and the '
            f'agent browser starts on its own port.')
    logger.info('the agent browser for %s runs without its debug port; '
                'starting it again', identity)
    if not close_agent_browser(identity):
        raise _cannot_stop(identity, None, agent_cdp_port(identity), want)


def _wait_until_up(identity: str, port: int, want: str, proc,
                   wait_s: float) -> str:
    """Wait for the Chrome just started for `identity` to answer `want` on
    `port` from its own profile.  A start that exits at once with nothing
    left on the profile raises at once; one that is not up by the deadline
    is stopped (the message says when that failed) and raises."""
    deadline = time.monotonic() + wait_s
    state, foreign = None, False
    while True:
        state = agent_browser_state(port)
        if state is not None:
            own = _profile_debug_port(identity) == port
            if own and state == want:
                logger.info('agent browser for %s is up on port %d (%s)',
                            identity, port, _mode_words(want))
                return _endpoint(port)
            foreign = not own
        code = proc.poll() if proc is not None else None
        if code is not None:
            # A launcher that hands the start to a Chrome already running on
            # the profile exits too; only an exit with nothing left counts.
            proc = None
            if not _agent_processes(identity):
                raise AgentBrowserUnavailable(
                    f'Chrome exited at once (exit code {code}) when starting '
                    f'the agent browser for {identity!r}, and nothing runs on '
                    f'its profile folder {agent_profile_dir(identity)}. Start '
                    f'Chrome by hand once to see what stops it, or set '
                    f'HEVOLVE_CHROME_PATH to a Chrome that starts.')
        if time.monotonic() >= deadline:
            break
        time.sleep(0.25)
    stopped = close_agent_browser(identity) or not _agent_processes(identity)
    if foreign:
        why = (f'a browser that is not this agent\'s answers on port {port}; '
               f'the next start moves this agent to a free port')
    elif state:
        why = (f'it answered {_mode_words(state)} on port {port}, not '
               f'{_mode_words(want)}')
    else:
        why = f'it did not answer on port {port}'
    outcome = ('It was stopped.' if stopped else
               'It could not be stopped and may still be running; close the '
               'Chrome that uses its profile folder.')
    hint = ('' if want == 'headless' else
            ' A window needs a signed-in desktop session; on a computer '
            'without a display ask for it headless.')
    raise AgentBrowserUnavailable(
        f'the agent browser for {identity!r} did not come up within '
        f'{wait_s:g} s: {why}. {outcome} Check that Chrome starts on this '
        f'computer; its profile folder is {agent_profile_dir(identity)}.{hint}')


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
    """Attach over CDP to a Chrome that listens on a debug port: the
    person's own Chrome on 9222 ('obscura_b2_cdp_user_chrome'), or an
    agent's own profile started by ensure_agent_browser (get_driver passes
    AGENT_PROFILE_MECHANISM).  The label matters: scripts/_base.py reads
    'obscura_b2_cdp_user_chrome' as the person's own signed-in session.

    Stub in C1 — concrete CDP-client wiring lands in C4 when first needed.
    """
    connection_mechanism = 'obscura_b2_cdp_user_chrome'

    def __init__(self, host: str = CDP_HOST_DEFAULT, port: int = CDP_PORT_DEFAULT,
                 connection_mechanism: Optional[str] = None) -> None:
        self.host = host
        self.port = port
        if connection_mechanism:
            self.connection_mechanism = connection_mechanism
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
        from urllib.parse import urlparse
        endpoint = ensure_agent_browser(identity, headless=headless)
        return ObscuraB2Driver(port=urlparse(endpoint).port,
                               connection_mechanism=AGENT_PROFILE_MECHANISM)
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
