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

Nothing here starts a real Chrome: subprocess.Popen and the endpoint probe are
replaced, and the profile root is a temp folder.

Run:
  pytest tests/unit/test_an_agent_gets_its_own_browser_profile.py -q
"""
import os
import sys

import pytest

_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from integrations.browser_research import driver  # noqa: E402


@pytest.fixture
def root(tmp_path, monkeypatch):
    monkeypatch.setattr(driver, 'agent_profiles_root', lambda: str(tmp_path))
    monkeypatch.setattr(driver, 'find_chrome', lambda: r'C:\chrome\chrome.exe')
    return tmp_path


def _inside(path, root):
    real = os.path.realpath(path)
    return os.path.commonpath([real, os.path.realpath(str(root))]) == os.path.realpath(str(root))


def test_two_mailboxes_get_two_folders_inside_the_root(root):
    a = driver.agent_profile_dir('registration@mcgroce.com')
    b = driver.agent_profile_dir('sathish@hevolve.ai')
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
    p1 = driver.agent_cdp_port('registration@mcgroce.com')
    p2 = driver.agent_cdp_port('sathish@hevolve.ai')
    assert p1 != p2
    assert p1 in driver.AGENT_CDP_PORTS and p2 in driver.AGENT_CDP_PORTS
    assert driver.agent_cdp_port('registration@mcgroce.com') == p1
    # A new process reads the same record.
    assert driver.agent_cdp_port('REGISTRATION@mcgroce.com') == p1


def test_a_port_another_program_holds_is_skipped(root, monkeypatch):
    first = driver.AGENT_CDP_PORTS[0]
    monkeypatch.setattr(driver, '_port_free', lambda port: port != first)
    assert driver.agent_cdp_port('someone@example.com') != first


def test_the_command_names_its_own_folder_and_port(root, monkeypatch):
    monkeypatch.setattr(driver, '_port_free', lambda port: True)
    who = 'registration@mcgroce.com'
    headed = driver.agent_chrome_command(who, headless=False)
    assert headed[0] == r'C:\chrome\chrome.exe'
    assert f'--user-data-dir={driver.agent_profile_dir(who)}' in headed
    assert f'--remote-debugging-port={driver.agent_cdp_port(who)}' in headed
    assert not any(a.startswith('--headless') for a in headed)
    headless = driver.agent_chrome_command(who, headless=True, url='https://email.ionos.com')
    assert '--headless' in headless
    assert headless[-1] == 'https://email.ionos.com'


class _Popen:
    calls = []

    def __init__(self, cmd, **kwargs):
        _Popen.calls.append(cmd)


@pytest.fixture
def popen(monkeypatch):
    _Popen.calls = []
    monkeypatch.setattr(driver.subprocess, 'Popen', _Popen)
    monkeypatch.setattr(driver, '_port_free', lambda port: True)
    return _Popen


def test_a_running_profile_is_reused_not_started_again(root, popen, monkeypatch):
    monkeypatch.setattr(driver, 'agent_browser_state', lambda port: 'headed')
    who = 'registration@mcgroce.com'
    endpoint = driver.ensure_agent_browser(who, headless=False)
    assert endpoint == f'http://127.0.0.1:{driver.agent_cdp_port(who)}'
    assert popen.calls == []


def test_a_stopped_profile_is_started_and_waited_for(root, popen, monkeypatch):
    states = iter([None, None, 'headless'])
    monkeypatch.setattr(driver, 'agent_browser_state', lambda port: next(states))
    monkeypatch.setattr(driver.time, 'sleep', lambda s: None)
    who = 'sathish@hevolve.ai'
    endpoint = driver.ensure_agent_browser(who, headless=True)
    assert len(popen.calls) == 1
    assert '--headless' in popen.calls[0]
    assert endpoint.endswith(str(driver.agent_cdp_port(who)))


def test_a_headless_run_is_restarted_when_a_window_is_wanted(root, popen, monkeypatch):
    states = iter(['headless', None, 'headed'])
    monkeypatch.setattr(driver, 'agent_browser_state', lambda port: next(states))
    monkeypatch.setattr(driver.time, 'sleep', lambda s: None)
    closed = []
    monkeypatch.setattr(driver, 'close_agent_browser', lambda who: closed.append(who) or True)
    who = 'registration@mcgroce.com'
    driver.ensure_agent_browser(who, headless=False)
    assert closed == [who]
    assert len(popen.calls) == 1 and not any(
        a.startswith('--headless') for a in popen.calls[0])


def test_a_window_already_open_serves_a_headless_ask(root, popen, monkeypatch):
    monkeypatch.setattr(driver, 'agent_browser_state', lambda port: 'headed')
    driver.ensure_agent_browser('registration@mcgroce.com', headless=True)
    assert popen.calls == []


def test_a_start_that_never_answers_says_what_to_do(root, popen, monkeypatch):
    monkeypatch.setattr(driver, 'agent_browser_state', lambda port: None)
    monkeypatch.setattr(driver.time, 'sleep', lambda s: None)
    with pytest.raises(driver.AgentBrowserUnavailable) as err:
        driver.ensure_agent_browser('registration@mcgroce.com', wait_s=0.01)
    text = str(err.value)
    assert 'did not answer' in text and 'registration@mcgroce.com' in text


def test_no_chrome_on_the_machine_says_how_to_fix(root, popen, monkeypatch):
    monkeypatch.setattr(driver, 'find_chrome', lambda: None)
    monkeypatch.setattr(driver, 'agent_browser_state', lambda port: None)
    with pytest.raises(driver.AgentBrowserUnavailable) as err:
        driver.ensure_agent_browser('registration@mcgroce.com')
    assert 'HEVOLVE_CHROME_PATH' in str(err.value)
    assert popen.calls == []


def test_get_driver_attaches_to_the_mailboxs_own_browser(root, popen, monkeypatch):
    monkeypatch.setattr(driver, 'agent_browser_state', lambda port: 'headless')
    who = 'sathish@hevolve.ai'
    d = driver.get_driver(identity=who, headless=True)
    assert isinstance(d, driver.ObscuraB2Driver)
    assert d.port == driver.agent_cdp_port(who)


def test_the_b2_refusal_no_longer_sends_people_to_the_default_profile(monkeypatch):
    monkeypatch.setattr(driver, 'cdp_endpoint_reachable', lambda *a, **k: False)
    with pytest.raises(RuntimeError) as err:
        driver.get_driver(mode='b2')
    text = str(err.value)
    assert 'Chrome 136' in text and 'identity' in text
