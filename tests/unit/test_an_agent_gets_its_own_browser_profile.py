"""Each agent mailbox gets its own Chrome profile, headless or headed.

Owner, 2026-11-24 (verbatim): "each agent needs a differeny mailbox investor
session needs sathish@hevolve.ai and the email provider is same unless you
have multiple instance of chrome the session cookie will be shared and mailbox
will not be isolated".  Owner, 2026-11-27 (verbatim): "the llm shd be able to
work on or use headless and with head browser (no-GUI) and (with GUI) actions
respectivelu".

So integrations.browser_research.driver gives every identity (a mailbox) its
OWN --user-data-dir and its OWN --remote-debugging-port, starts the installed
Chrome on it headless or headed, and reuses it while it runs.  Chrome 136+
ignores the debug port on the person's everyday profile folder, which is why
an agent profile is its own folder.

The review of 242279af3 added: a start counts only in the mode asked for and
only from the profile's own Chrome (never another agent's browser that
answers on the same port); the port record corrects itself (a port another
program holds is moved, a Chrome found on another port is followed, bad
entries are dropped); and a stop touches only Chrome on exactly that folder.

Nothing here starts a real Chrome or reads real processes: a fake machine
stands in for Chrome, the ports and the process table, and the profile root
is a temp folder.

Run:
  pytest tests/unit/test_an_agent_gets_its_own_browser_profile.py -q
"""
import os
import sys
from types import SimpleNamespace

import pytest

_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from integrations.browser_research import driver  # noqa: E402

X = 'registration@mcgroce.com'
Y = 'sathish@hevolve.ai'


@pytest.fixture
def root(tmp_path, monkeypatch):
    monkeypatch.setattr(driver, 'agent_profiles_root', lambda: str(tmp_path))
    monkeypatch.setattr(driver, 'find_chrome', lambda: r'C:\chrome\chrome.exe')
    return tmp_path


def _inside(path, root):
    real = os.path.realpath(path)
    return os.path.commonpath([real, os.path.realpath(str(root))]) == os.path.realpath(str(root))


# ── Folders and ports ───────────────────────────────────────────────────

def test_two_mailboxes_get_two_folders_inside_the_root(root):
    a = driver.agent_profile_dir(X)
    b = driver.agent_profile_dir(Y)
    assert a != b
    assert _inside(a, root) and _inside(b, root)
    assert driver.agent_profile_dir('Registration@McGroce.com') == a, (
        'a mailbox is the same mailbox whatever its letter case')


def test_a_name_cannot_climb_out_of_the_root(root):
    for name in ('..', '../../x', '..\\..\\x', '/etc/passwd', 'C:\\Windows'):
        assert _inside(driver.agent_profile_dir(name), root), name


def test_names_that_slug_alike_still_get_their_own_folder(root):
    assert driver.agent_profile_dir('a@b.com') != driver.agent_profile_dir('a_b.com')


def test_each_mailbox_keeps_its_own_port_across_calls(root, monkeypatch):
    monkeypatch.setattr(driver, '_port_free', lambda port: True)
    p1 = driver.agent_cdp_port(X)
    p2 = driver.agent_cdp_port(Y)
    assert p1 != p2
    assert p1 in driver.AGENT_CDP_PORTS and p2 in driver.AGENT_CDP_PORTS
    assert driver.agent_cdp_port(X) == p1
    # A new process reads the same record.
    assert driver.agent_cdp_port('REGISTRATION@mcgroce.com') == p1


def test_a_port_another_program_holds_is_skipped(root, monkeypatch):
    first = driver.AGENT_CDP_PORTS[0]
    monkeypatch.setattr(driver, '_port_free', lambda port: port != first)
    assert driver.agent_cdp_port('someone@example.com') != first


@pytest.mark.parametrize('bad', ['"x"', 'null', '70000', '[1]', 'true'])
def test_a_bad_entry_in_the_port_record_is_replaced(root, monkeypatch, bad):
    monkeypatch.setattr(driver, '_port_free', lambda port: True)
    with open(os.path.join(str(root), 'ports.json'), 'w', encoding='utf-8') as f:
        f.write('{"%s": %s, "other@x.com": 9311}' % (X, bad))
    port = driver.agent_cdp_port(X)
    assert port in driver.AGENT_CDP_PORTS and port != 9311
    assert driver.agent_cdp_port('other@x.com') == 9311, 'good entries are kept'


def test_a_corrupt_port_record_is_rebuilt(root, monkeypatch):
    monkeypatch.setattr(driver, '_port_free', lambda port: True)
    with open(os.path.join(str(root), 'ports.json'), 'w', encoding='utf-8') as f:
        f.write('{not json')
    assert driver.agent_cdp_port('a@b.com') in driver.AGENT_CDP_PORTS


def test_the_command_names_its_own_folder_and_port(root, monkeypatch):
    monkeypatch.setattr(driver, '_port_free', lambda port: True)
    headed = driver.agent_chrome_command(X, headless=False)
    assert headed[0] == r'C:\chrome\chrome.exe'
    assert f'--user-data-dir={driver.agent_profile_dir(X)}' in headed
    assert f'--remote-debugging-port={driver.agent_cdp_port(X)}' in headed
    assert not any(a.startswith('--headless') for a in headed)
    headless = driver.agent_chrome_command(X, headless=True, url='https://email.ionos.com')
    assert '--headless' in headless
    assert headless[-1] == 'https://email.ionos.com'


# ── Starting and reusing, on a fake machine ─────────────────────────────

def _arg(cmd, prefix):
    return next(a[len(prefix):] for a in cmd if a.startswith(prefix))


class _Machine:
    """The computer as the driver sees it: which profile's own Chrome
    listens on which port, how each port answers /json/version, which ports
    other programs hold, and what a Chrome start does."""

    def __init__(self):
        self.listening = {}     # profile folder -> port its own Chrome listens on
        self.portless = set()   # agent starts (debug port named) with no listener
        self.by_hand = set()    # Chrome opened by hand on the folder: no debug port
        self.answers = {}       # port -> 'headed' | 'headless'
        self.held = set()       # ports other programs are bound to
        self.started = []       # every Chrome command started
        self.closed = []        # every identity asked to stop
        self.start_does = 'listen'
        self.unstoppable = False

    def run(self, who, port, mode):
        self.listening[driver.agent_profile_dir(who)] = port
        self.answers[port] = mode

    def start(self, cmd):
        self.started.append(cmd)
        folder = _arg(cmd, '--user-data-dir=')
        port = int(_arg(cmd, '--remote-debugging-port='))
        mode = 'headless' if '--headless' in cmd else 'headed'
        if self.start_does == 'listen':
            self.listening[folder] = port
            self.answers[port] = mode
        elif self.start_does == 'comes_up_headless':
            self.listening[folder] = port
            self.answers[port] = 'headless'
        elif self.start_does == 'never_listens':
            self.portless.add(folder)
        elif self.start_does == 'loses_its_port':
            # Another program's browser binds the port first and answers on
            # it; this profile's Chrome runs on without a listener.
            self.answers[port] = mode
            self.portless.add(folder)
        elif self.start_does == 'loses_its_port_to_the_other_mode':
            self.answers[port] = 'headless' if mode == 'headed' else 'headed'
            self.portless.add(folder)
        return 21 if self.start_does == 'exits' else None

    def processes(self, who):
        folder = driver.agent_profile_dir(who)
        if folder in self.listening:
            argv = [f'--remote-debugging-port={self.listening[folder]}']
        elif folder in self.portless:
            argv = ['--remote-debugging-port=9399']
        elif folder in self.by_hand:
            argv = []
        else:
            return []
        return [(SimpleNamespace(pid=1), [f'--user-data-dir={folder}'] + argv)]

    def close(self, who):
        self.closed.append(who)
        if self.unstoppable:
            return False
        folder = driver.agent_profile_dir(who)
        port = self.listening.pop(folder, None)
        if port is not None:
            self.answers.pop(port, None)
        was_running = (port is not None or folder in self.portless
                       or folder in self.by_hand)
        self.portless.discard(folder)
        self.by_hand.discard(folder)
        return was_running


@pytest.fixture
def machine(root, monkeypatch):
    m = _Machine()

    class _Popen:
        def __init__(self, cmd, **kwargs):
            self._code = m.start(cmd)

        def poll(self):
            return self._code

    monkeypatch.setattr(driver.subprocess, 'Popen', _Popen)
    monkeypatch.setattr(driver, '_port_free',
                        lambda port: port not in m.answers and port not in m.held)
    monkeypatch.setattr(driver, 'agent_browser_state', lambda port: m.answers.get(port))
    monkeypatch.setattr(driver, '_profile_debug_port',
                        lambda who: m.listening.get(driver.agent_profile_dir(who)))
    monkeypatch.setattr(driver, '_agent_processes', m.processes)
    monkeypatch.setattr(driver, 'close_agent_browser', m.close)
    monkeypatch.setattr(driver.time, 'sleep', lambda s: None)
    return m


def test_a_running_profile_is_reused_not_started_again(machine):
    port = driver.agent_cdp_port(X)
    machine.run(X, port, 'headed')
    assert driver.ensure_agent_browser(X, headless=False) == f'http://127.0.0.1:{port}'
    assert machine.started == []


def test_a_stopped_profile_is_started_and_waited_for(machine):
    endpoint = driver.ensure_agent_browser(Y, headless=True)
    assert len(machine.started) == 1 and '--headless' in machine.started[0]
    assert endpoint.endswith(str(driver.agent_cdp_port(Y)))


def test_a_headless_run_is_restarted_when_a_window_is_wanted(machine):
    port = driver.agent_cdp_port(X)
    machine.run(X, port, 'headless')
    driver.ensure_agent_browser(X, headless=False)
    assert machine.closed == [X]
    assert len(machine.started) == 1
    assert not any(a.startswith('--headless') for a in machine.started[0])
    assert machine.answers[port] == 'headed'


def test_a_window_already_open_serves_a_headless_ask(machine):
    machine.run(X, driver.agent_cdp_port(X), 'headed')
    driver.ensure_agent_browser(X, headless=True)
    assert machine.started == []


def test_a_start_that_never_answers_is_stopped_and_says_what_to_do(machine):
    machine.start_does = 'never_listens'
    with pytest.raises(driver.AgentBrowserUnavailable) as err:
        driver.ensure_agent_browser(X, wait_s=0.01)
    text = str(err.value)
    assert 'did not answer' in text and X in text
    assert machine.closed == [X], 'a start that never came up is not left running'


def test_no_chrome_on_the_machine_says_how_to_fix(machine, monkeypatch):
    monkeypatch.setattr(driver, 'find_chrome', lambda: None)
    with pytest.raises(driver.AgentBrowserUnavailable) as err:
        driver.ensure_agent_browser(X)
    assert 'HEVOLVE_CHROME_PATH' in str(err.value)
    assert machine.started == []


def test_get_driver_attaches_to_the_mailboxs_own_browser(machine):
    port = driver.agent_cdp_port(Y)
    machine.run(Y, port, 'headless')
    d = driver.get_driver(identity=Y, headless=True)
    assert isinstance(d, driver.ObscuraB2Driver)
    assert d.port == port


def test_an_agent_driver_is_not_labelled_as_the_persons_own_chrome(machine):
    machine.run(Y, driver.agent_cdp_port(Y), 'headless')
    d = driver.get_driver(identity=Y, headless=True)
    assert d.connection_mechanism == 'agent_profile_cdp'
    assert driver.ObscuraB2Driver().connection_mechanism == 'obscura_b2_cdp_user_chrome'


# Review of 242279af3, finding 1: a start counts only in the mode asked for.

def test_a_window_is_not_reported_while_only_the_headless_run_answers(machine):
    machine.run(X, driver.agent_cdp_port(X), 'headless')
    machine.unstoppable = True
    with pytest.raises(driver.AgentBrowserUnavailable) as err:
        driver.ensure_agent_browser(X, headless=False)
    assert 'could not be stopped' in str(err.value)
    assert machine.started == []


def test_a_started_browser_in_the_wrong_mode_is_not_success(machine):
    machine.start_does = 'comes_up_headless'
    with pytest.raises(driver.AgentBrowserUnavailable) as err:
        driver.ensure_agent_browser(X, headless=False, wait_s=0.01)
    assert 'answered headless' in str(err.value)
    assert machine.closed == [X], 'a start that came up wrong is stopped'


# Finding 2, and what it hid: a port is trusted only from the profile's
# own Chrome, and the record corrects itself.

def test_another_agents_browser_on_the_recorded_port_is_never_attached(machine):
    port = driver.agent_cdp_port(X)
    machine.run(Y, port, 'headed')   # Y's Chrome got X's recorded port
    endpoint = driver.ensure_agent_browser(X, headless=False)
    assert endpoint != f'http://127.0.0.1:{port}'
    assert machine.started and _arg(machine.started[0], '--user-data-dir=') == \
        driver.agent_profile_dir(X)
    assert endpoint.endswith(str(driver.agent_cdp_port(X)))


def test_a_start_whose_port_another_browser_took_is_not_success(machine):
    machine.start_does = 'loses_its_port'
    with pytest.raises(driver.AgentBrowserUnavailable) as err:
        driver.ensure_agent_browser(X, headless=False, wait_s=0.01)
    assert "not this agent's" in str(err.value)
    assert machine.closed == [X]


def test_a_recorded_port_another_program_holds_is_moved(machine):
    held = driver.agent_cdp_port(X)
    machine.held.add(held)
    endpoint = driver.ensure_agent_browser(X, headless=False)
    moved = driver.agent_cdp_port(X)
    assert moved != held and endpoint.endswith(str(moved))
    assert f'--remote-debugging-port={moved}' in machine.started[0]


def test_a_profile_still_running_on_another_port_is_followed(machine):
    recorded = driver.agent_cdp_port(X)
    live = recorded + 7
    machine.run(X, live, 'headed')
    assert driver.ensure_agent_browser(X) == f'http://127.0.0.1:{live}'
    assert driver.agent_cdp_port(X) == live, 'the record follows the running browser'
    assert machine.started == []


def test_chrome_on_the_profile_without_its_port_is_started_again(machine):
    machine.portless.add(driver.agent_profile_dir(X))
    endpoint = driver.ensure_agent_browser(X, headless=True)
    assert machine.closed == [X]
    assert len(machine.started) == 1
    assert endpoint.endswith(str(driver.agent_cdp_port(X)))


def test_another_browser_in_the_other_mode_is_not_blamed_on_this_agent(machine):
    machine.start_does = 'loses_its_port_to_the_other_mode'
    with pytest.raises(driver.AgentBrowserUnavailable) as err:
        driver.ensure_agent_browser(X, headless=False, wait_s=0.01)
    text = str(err.value)
    assert "not this agent's" in text and 'answered headless' not in text


def test_a_timed_out_start_that_could_not_be_stopped_says_so(machine):
    machine.start_does = 'never_listens'
    machine.unstoppable = True
    with pytest.raises(driver.AgentBrowserUnavailable) as err:
        driver.ensure_agent_browser(X, wait_s=0.01)
    text = str(err.value)
    assert 'could not be stopped' in text and 'was stopped' not in text


# Review of b2ae1f66e, finding 1: a slow answer never kills a running browser.

def test_a_running_browser_slow_to_answer_is_asked_again_not_killed(machine, monkeypatch):
    port = driver.agent_cdp_port(X)
    machine.run(X, port, 'headed')
    replies = iter([None, None, 'headed'])
    monkeypatch.setattr(driver, 'agent_browser_state', lambda p: next(replies))
    assert driver.ensure_agent_browser(X, headless=False) == f'http://127.0.0.1:{port}'
    assert machine.closed == [] and machine.started == []


def test_a_running_browser_that_stays_silent_is_left_running(machine, monkeypatch):
    port = driver.agent_cdp_port(X)
    machine.run(X, port, 'headed')
    machine.answers.pop(port)          # holds its listener, answers nothing
    monkeypatch.setattr(driver, 'ANSWER_GRACE_S', 0.01)
    with pytest.raises(driver.AgentBrowserUnavailable) as err:
        driver.ensure_agent_browser(X, headless=True)
    assert 'left running' in str(err.value)
    assert machine.closed == [] and machine.started == []


# Finding 3: "could not look" is not "not listening".

def test_a_browser_whose_sockets_cannot_be_read_is_left_alone(machine, monkeypatch):
    def cannot_look(who):
        raise driver.AgentBrowserUnavailable('may not read its network sockets')
    monkeypatch.setattr(driver, '_profile_debug_port', cannot_look)
    with pytest.raises(driver.AgentBrowserUnavailable):
        driver.ensure_agent_browser(X)
    assert machine.closed == [] and machine.started == []


def test_the_launch_uses_the_port_it_chose(machine, monkeypatch):
    """The command takes the port ensure_agent_browser settled on, not a
    second read of the record that another process may have rewritten."""
    held = driver.agent_cdp_port(X)
    machine.held.add(held)
    real_read = driver._read_ports
    reads = []

    def read_once_then_stale():
        reads.append(1)
        ports = real_read()
        return ports if len(reads) < 3 else dict(ports, **{X: held})
    monkeypatch.setattr(driver, '_read_ports', read_once_then_stale)
    driver.ensure_agent_browser(X, headless=True)
    assert f'--remote-debugging-port={held}' not in machine.started[0]


def test_a_capitalised_key_in_the_record_is_the_same_mailbox(root, monkeypatch):
    monkeypatch.setattr(driver, '_port_free', lambda port: True)
    with open(os.path.join(str(root), 'ports.json'), 'w', encoding='utf-8') as f:
        f.write('{"Registration@McGroce.com": 9333}')
    assert driver.agent_cdp_port(X) == 9333


def test_the_grace_is_bounded_by_the_clock_not_only_by_steps(monkeypatch):
    """rv_br2 on 922155e0d: 21 steps x (1.5 s request + 0.25 s pause) took
    37.4 s against a hung browser while the refusal said 5 s."""
    now = [0.0]

    class _Clock:
        @staticmethod
        def monotonic():
            return now[0]

        @staticmethod
        def sleep(s):
            now[0] += s

    def hung_probe():
        now[0] += 1.5          # one /json/version request that times out
        return None

    monkeypatch.setattr(driver, 'time', _Clock)
    assert driver._within_grace(hung_probe) is None
    assert now[0] <= driver.ANSWER_GRACE_S + 1.5 + driver._GRACE_STEP_S


def test_a_chrome_that_exits_during_the_grace_is_started_again(machine, monkeypatch):
    """rv_br2: it used to be called "opened by hand ... left running" when
    nothing ran any more."""
    calls = []

    def processes(who):
        calls.append(who)
        return [(SimpleNamespace(pid=1), ['--remote-debugging-port=9399'])] if len(calls) == 1 else []
    monkeypatch.setattr(driver, '_agent_processes', processes)
    endpoint = driver.ensure_agent_browser(X, headless=True)
    assert len(machine.started) == 1 and endpoint.endswith(str(driver.agent_cdp_port(X)))


def test_chrome_opened_by_hand_on_the_profile_is_left_running(machine):
    machine.by_hand.add(driver.agent_profile_dir(X))
    with pytest.raises(driver.AgentBrowserUnavailable) as err:
        driver.ensure_agent_browser(X, headless=True)
    assert 'left running' in str(err.value)
    assert machine.closed == [] and machine.started == []


def test_a_chrome_that_exits_at_once_is_reported_at_once(machine):
    machine.start_does = 'exits'
    t0 = driver.time.monotonic()
    with pytest.raises(driver.AgentBrowserUnavailable) as err:
        driver.ensure_agent_browser(X, wait_s=30)
    assert driver.time.monotonic() - t0 < 5, 'no full wait for a Chrome that already exited'
    assert 'exit code 21' in str(err.value)


# ── Finding 3: the process table, read for real through a fake psutil ──

class _Proc:
    def __init__(self, name, cmdline, listening=()):
        self.info = {'name': name, 'cmdline': cmdline}
        self.pid = 4242
        self.terminated = self.killed = False
        self._listening = list(listening)

    def cmdline(self):
        if self.info['cmdline'] is None:
            import psutil
            raise psutil.AccessDenied(self.pid)
        return self.info['cmdline']

    def terminate(self):
        self.terminated = True

    def kill(self):
        self.killed = True

    def net_connections(self, kind='tcp'):
        return [SimpleNamespace(status='LISTEN',
                                laddr=SimpleNamespace(ip='127.0.0.1', port=p))
                for p in self._listening]


def _processes(monkeypatch, procs):
    import psutil
    monkeypatch.setattr(psutil, 'process_iter', lambda attrs=None: list(procs))


def test_stop_touches_only_chrome_on_exactly_this_folder(root, monkeypatch):
    import psutil
    mine = driver.agent_profile_dir('a')
    other = driver.agent_profile_dir('a-86f7e437fa')
    procs = [
        _Proc('chrome.exe', ['chrome.exe', f'--user-data-dir={mine}', '--headless']),
        _Proc('powershell.exe', ['powershell.exe', '-c', f'echo --user-data-dir={mine}']),
        _Proc('chrome.exe', ['chrome.exe', f'--user-data-dir={other}']),
        _Proc('chrome.exe', ['chrome.exe', f'--user-data-dir={mine}-old']),
        _Proc('chrome.exe', ['chrome.exe']),
    ]
    _processes(monkeypatch, procs)
    monkeypatch.setattr(psutil, 'wait_procs', lambda ps, timeout=None: (list(ps), []))
    assert driver.close_agent_browser('a') is True
    assert [p.terminated for p in procs] == [True, False, False, False, False]


def test_a_chrome_that_ignores_the_stop_is_killed(root, monkeypatch):
    import psutil
    p = _Proc('chrome.exe', ['chrome.exe', f'--user-data-dir={driver.agent_profile_dir("a")}'])
    _processes(monkeypatch, [p])
    waits = []

    def wait_procs(ps, timeout=None):
        waits.append(timeout)
        return ([], list(ps)) if len(waits) == 1 else (list(ps), [])

    monkeypatch.setattr(psutil, 'wait_procs', wait_procs)
    assert driver.close_agent_browser('a') is True
    assert p.terminated and p.killed


def test_nothing_running_on_the_profile_stops_nothing(root, monkeypatch):
    _processes(monkeypatch, [_Proc('chrome.exe', ['chrome.exe']),
                             _Proc('chrome.exe', None)])   # another user's Chrome
    assert driver.close_agent_browser('a') is False


def test_only_the_profiles_own_browser_listening_on_its_port_counts(root, monkeypatch):
    mine = driver.agent_profile_dir('a')
    other = driver.agent_profile_dir('b')
    port_arg = '--remote-debugging-port=9305'
    _processes(monkeypatch, [
        _Proc('chrome.exe', ['chrome.exe', f'--user-data-dir={other}', port_arg], [9305]),
        _Proc('chrome.exe', ['chrome.exe', '--type=renderer', f'--user-data-dir={mine}',
                             port_arg], [9305]),
    ])
    assert driver._profile_debug_port('a') is None, (
        "another profile's browser and a renderer of this one do not count")
    _processes(monkeypatch, [
        _Proc('chrome.exe', ['chrome.exe', f'--user-data-dir={mine}', port_arg], [])])
    assert driver._profile_debug_port('a') is None, 'running without its listener'
    _processes(monkeypatch, [
        _Proc('chrome.exe', ['chrome.exe', f'--user-data-dir={mine}', port_arg], [9305])])
    assert driver._profile_debug_port('a') == 9305


def test_unreadable_sockets_of_this_profiles_browser_raise_not_none(root, monkeypatch):
    import psutil
    mine = driver.agent_profile_dir('a')
    proc = _Proc('chrome.exe', ['chrome.exe', f'--user-data-dir={mine}',
                                '--remote-debugging-port=9305'])

    def denied(kind='tcp'):
        raise psutil.AccessDenied(proc.pid)
    proc.net_connections = denied
    _processes(monkeypatch, [proc])
    with pytest.raises(driver.AgentBrowserUnavailable) as err:
        driver._profile_debug_port('a')
    assert 'left running' in str(err.value)


def test_the_b2_refusal_no_longer_sends_people_to_the_default_profile(monkeypatch):
    monkeypatch.setattr(driver, 'cdp_endpoint_reachable', lambda *a, **k: False)
    with pytest.raises(RuntimeError) as err:
        driver.get_driver(mode='b2')
    text = str(err.value)
    assert 'Chrome 136' in text and 'identity' in text
