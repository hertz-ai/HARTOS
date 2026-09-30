"""A sidecar is "running" when it SERVES, not when its launcher survives.

MEASURED 2026-09-21 on this machine: get_tool_status('acestep') answered
running=True with port 51168 while the child had already died at import on

    ImportError: cannot import name 'validate_core_schema' from 'pydantic_core'

A launcher like `uv run acestep-api` is a wrapper. The wrapper stays up, so
`proc.poll() is None` stays None, and every caller downstream believes the
capability is available. The real reason music could not be composed stayed
invisible for a day while "no music model is installed" was assumed instead
-- and the model had been on disk the whole time.

Same family as #86, where a voice engine reported "installed" for a venv
that did not exist and died at import on every voiced turn. A status that
confirms the PROCESS rather than the SERVICE is a measurement of the wrong
thing.

    python -m pytest tests/unit/test_sidecar_liveness_is_serving.py -q
"""
import os
import sys
from unittest.mock import patch

import pytest

_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

os.environ.setdefault('HEVOLVE_DB_PATH', ':memory:')

from integrations.service_tools.runtime_manager import (  # noqa: E402
    RuntimeToolManager)


class FakeProc:
    """A launcher process. `alive` is what proc.poll() reports."""

    def __init__(self, alive=True, rc=0):
        self._alive, self.returncode = alive, rc

    def poll(self):
        return None if self._alive else self.returncode


@pytest.fixture
def rtm():
    m = RuntimeToolManager.__new__(RuntimeToolManager)
    m._processes = {}
    m._ports = {}
    return m


def _serving(rtm, yes):
    """Pin what the socket probe finds, without opening one."""
    return patch.object(rtm, '_accepts_on', return_value=yes)


class TestTheLieThisFixes:
    def test_a_live_wrapper_with_a_dead_child_is_not_running(self, rtm):
        """THE regression. The launcher survived, the server did not, and
        nothing accepts on the port it announced."""
        rtm._processes['acestep'] = FakeProc(alive=True)
        rtm._ports['acestep'] = 51168
        with _serving(rtm, False):
            assert rtm._is_server_alive('acestep') is False

    def test_it_says_so_out_loud(self, rtm, caplog):
        """A live wrapper with a dead child is the hardest state to diagnose
        from downstream, because every symptom appears somewhere else. It
        must not be silent."""
        rtm._processes['acestep'] = FakeProc(alive=True)
        rtm._ports['acestep'] = 51168
        with _serving(rtm, False), caplog.at_level('WARNING'):
            rtm._is_server_alive('acestep')
        joined = ' '.join(r.message for r in caplog.records).lower()
        assert 'acestep' in joined and '51168' in joined
        assert 'nothing accepts' in joined

    def test_a_serving_sidecar_is_running(self, rtm):
        rtm._processes['acestep'] = FakeProc(alive=True)
        rtm._ports['acestep'] = 51168
        with _serving(rtm, True):
            assert rtm._is_server_alive('acestep') is True


class TestTheCheapChecksStillComeFirst:
    def test_an_exited_process_needs_no_probe(self, rtm):
        """A process that has exited is definitely not serving, and saying
        so must not cost a socket."""
        rtm._processes['acestep'] = FakeProc(alive=False, rc=1)
        rtm._ports['acestep'] = 51168
        with patch.object(rtm, '_accepts_on') as probe:
            assert rtm._is_server_alive('acestep') is False
            probe.assert_not_called()

    def test_an_unknown_tool_is_not_running(self, rtm):
        assert rtm._is_server_alive('never_started') is False

    def test_no_port_announced_means_no_probe_and_no_downgrade(self, rtm):
        """Nothing announced a port, so there is nothing to probe. Do not
        invent a stricter answer than this manager has evidence for."""
        rtm._processes['quiet'] = FakeProc(alive=True)
        with patch.object(rtm, '_accepts_on') as probe:
            assert rtm._is_server_alive('quiet') is True
            probe.assert_not_called()


class TestAMalformedRecordCannotDisableAWorkingTool:
    def test_an_odd_probe_failure_is_not_read_as_dead(self, rtm):
        """A bad port value is a bookkeeping fault, not evidence the server
        died. Failing closed here would silently disable a working tool."""
        rtm._processes['acestep'] = FakeProc(alive=True)
        rtm._ports['acestep'] = 'not-a-port'
        assert rtm._is_server_alive('acestep') is True

    def test_a_refused_connection_IS_read_as_dead(self, rtm):
        """The opposite case, so the rule above cannot be read as 'probe
        failures never count'. A refusal is real evidence."""
        rtm._processes['acestep'] = FakeProc(alive=True)
        rtm._ports['acestep'] = 51168
        assert rtm._accepts_on(51168, timeout=0.05) is False
        with _serving(rtm, False):
            assert rtm._is_server_alive('acestep') is False


class TestOneSocketQuestion:
    """Start-time readiness and ongoing liveness must not drift apart."""

    def test_the_wait_loop_uses_the_same_probe(self):
        import inspect

        from integrations.service_tools import runtime_manager
        src = inspect.getsource(runtime_manager.RuntimeToolManager
                                ._wait_for_listen)
        assert '_accepts_on' in src, (
            '_wait_for_listen must ask the same socket question as '
            '_is_server_alive, or "is it up yet" and "is it still up" will '
            'answer differently')
        assert 'socket.create_connection' not in src, (
            'the socket call belongs in _accepts_on alone; a second one here '
            'is the parallel path that lets the two answers drift')

    def test_the_probe_is_bounded(self, rtm):
        """get_all_status calls this once per tool for a dashboard, so an
        unbounded connect would hang the view."""
        import inspect
        sig = inspect.signature(rtm._accepts_on)
        assert 'timeout' in sig.parameters
        assert sig.parameters['timeout'].default <= 1.0


class TestInProcessToolsAreUnchanged:
    def test_a_non_whisper_inprocess_tool_is_not_running(self, rtm):
        with patch.dict(
                'integrations.service_tools.runtime_manager.TOOL_CONFIGS',
                {'inproc': {'is_inprocess': True}}, clear=False):
            assert rtm._is_server_alive('inproc') is False

    def test_an_unreadable_whisper_worker_logs_rather_than_going_quiet(
            self, rtm, caplog):
        with patch.dict(
                'integrations.service_tools.runtime_manager.TOOL_CONFIGS',
                {'whisper': {'is_inprocess': True}}, clear=False), \
             patch.dict(sys.modules,
                        {'integrations.service_tools.whisper_tool': None}), \
             caplog.at_level('WARNING'):
            assert rtm._is_server_alive('whisper') is False
        assert any('whisper' in r.message.lower() for r in caplog.records)
