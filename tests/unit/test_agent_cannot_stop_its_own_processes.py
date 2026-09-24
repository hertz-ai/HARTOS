"""An agent's shell command must never stop the assistant's own processes.

#877, measured 2026-09-23 22:31 on the MSI desktop: a daemon VLM action ran
    Get-Process | Where-Object {$_.Name -like '*Nunba*'} | Stop-Process -Force
through the VLM shell action.  It passed destructive_computer_operation
(integrations/vlm/safety.py) because that policy has rules for power, reset
and erase, and none for stopping processes.  Nunba exited under the owner.

The rule lives in that one function, so every dispatcher that already calls
it (execute_action, local_loop, vlm_adapter, mobile, remote_executor, both
recipe tool wrappers, Nunba /execute) refuses the same commands.

"Own" is read from ResourceGovernor.own_process_pids -- the same pid walk the
governor uses to attribute its own CPU -- so there is one definition of which
processes are ours.  Names are matched literally; nothing from the command is
ever compiled as a regex (a first draft did, and a 26-char pattern took 14 s).
"""
import os
import sys
import time

import pytest

sys.path.insert(0, os.path.abspath(
    os.path.join(os.path.dirname(__file__), '..', '..')))

from integrations.vlm import safety  # noqa: E402

OWN_PID = 43210
PARENT_PID = 43209
OTHER_PID = 777


@pytest.fixture
def own(monkeypatch):
    monkeypatch.setattr(
        safety, '_own_process_identity',
        lambda: ({OWN_PID, PARENT_PID},
                 {'nunba', 'python', 'llama-server'}))


def _refused(value):
    return safety.destructive_computer_operation(value)


INCIDENT = ("Get-Process | Where-Object {$_.Name -like '*Nunba*'} "
            "| Stop-Process -Force")


class TestOwnProcessesAreRefused:

    def test_the_measured_incident(self, own):
        assert _refused(INCIDENT)

    def test_the_incident_as_a_vlm_shell_action(self, own):
        assert _refused({'action': 'shell', 'command': INCIDENT})

    @pytest.mark.parametrize('cmd', [
        f'taskkill /F /PID {OWN_PID}',
        f'taskkill /pid {PARENT_PID}',
        f'kill -9 {OWN_PID}',
        f'Stop-Process -Id {OWN_PID}',
        'taskkill /IM Nunba.exe /F',
        'taskkill /im llama-server.exe',
        'pkill -f llama-server',
        'killall python',
        'Stop-Process -Name nunba',
        'wmic process where name="nunba.exe" delete',
    ])
    def test_by_pid_or_name(self, own, cmd):
        assert _refused(cmd), cmd

    @pytest.mark.parametrize('cmd', [
        'task^kill /im nunba.exe',
        '%comspec% /c taskkill /im nunba.exe',
        'cmd /ctaskkill /im nunba.exe',
        'Stop`-Process -Name nunba',
        'powershell -Command "Stop-Process -Name nunba"',
    ])
    def test_spellings_that_hide_the_verb(self, own, cmd):
        assert _refused(cmd), cmd

    @pytest.mark.parametrize('cmd', [
        'Get-Process | Stop-Process -Force',
        '$p = Get-Process; $p | Stop-Process',
        'Stop-Process -Id $pid',
        'ps aux | awk "{print $2}" | xargs kill',
    ])
    def test_a_target_that_cannot_be_resolved_is_refused(self, own, cmd):
        assert _refused(cmd), cmd


class TestOrdinaryWorkStillRuns:
    """Anti-vacuity: a guard that refuses everything verifies nothing."""

    @pytest.mark.parametrize('cmd', [
        'taskkill /im notepad.exe',
        f'taskkill /pid {OTHER_PID}',
        'Stop-Process -Name calc',
        'Get-Process | Sort-Object CPU',
        'dir C:\\Users\\Public',
        'python -m pip list',
    ])
    def test_allowed(self, own, cmd):
        assert _refused(cmd) is None, cmd

    def test_plain_language_restart_is_still_allowed(self, own):
        """test_vlm_safety.py keeps 'restart the Nunba app' allowed; the rule
        acts on kill commands, not on prose about the app."""
        assert _refused('restart the Nunba app') is None

    def test_reasoning_prose_is_not_a_command(self, own):
        assert _refused({'action': 'click', 'coordinate': [1, 2],
                         'reasoning': 'I will not kill nunba'}) is None


class TestFailsClosed:

    def test_unknown_own_set_refuses_a_kill(self, monkeypatch):
        def boom():
            raise RuntimeError('psutil missing')
        monkeypatch.setattr(safety, '_own_process_identity', boom)
        assert _refused('taskkill /im notepad.exe')

    def test_unknown_own_set_does_not_touch_non_kill_commands(self, monkeypatch):
        def boom():
            raise RuntimeError('psutil missing')
        monkeypatch.setattr(safety, '_own_process_identity', boom)
        assert _refused('dir C:\\') is None


class TestTheShellToolUsesTheSamePolicy:
    """The LangChain Shell_Command tool called run_bounded without this
    policy (audit 2026-09-24: hart_intelligence_entry._handle_shell_command_tool
    had only its own denylist and the consent check)."""

    def test_incident_never_reaches_the_shell(self, own):
        from unittest.mock import patch
        hie = pytest.importorskip('hart_intelligence_entry')
        with patch.object(hie, 'run_bounded') as run, \
                patch('integrations.vlm.safety.computer_control_block',
                      return_value=None):
            result = hie._handle_shell_command_tool(
                f'powershell -Command "{INCIDENT}"')
        assert 'refused' in result.lower()
        run.assert_not_called()


def test_hostile_input_is_linear(own):
    """C2 from the 2026-09-14 review: no pattern is taken from the command."""
    cmd = 'pkill -f ' + '(a+)+' * 2000 + 'a' * 50000 + '!'
    start = time.monotonic()
    _refused(cmd)
    assert time.monotonic() - start < 1.0


class TestOneDefinitionOfOwn:
    """Needs psutil, which ships in python-embed (7.2.2) but not in every
    dev venv; without it _own_process_identity raises and kills fail closed
    (TestFailsClosed)."""

    @pytest.fixture(autouse=True)
    def _psutil(self):
        pytest.importorskip('psutil')

    def test_governor_names_this_process_and_optionally_its_parent(self):
        from core.resource_governor import ResourceGovernor
        gov = ResourceGovernor()
        pids = gov.own_process_pids()
        assert os.getpid() in pids
        assert os.getppid() not in pids or os.getppid() == os.getpid()
        assert os.getppid() in gov.own_process_pids(include_parent=True)

    def test_registered_subprocess_is_own(self):
        from core.resource_governor import ResourceGovernor
        gov = ResourceGovernor()
        gov.register_subprocess('child', os.getppid())
        assert os.getppid() in gov.own_process_pids()

    def test_real_identity_names_this_process(self):
        pids, names = safety._own_process_identity()
        assert os.getpid() in pids
        assert names, 'at least this process has a name'
