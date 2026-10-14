"""The one `claude -p` launcher returns on its timeout, whatever its children do.

invoke_claude (integrations/coding_agent/claude_code_backend.py) is the single
place a Claude Code run starts: the copilot daemon's agentic branch work, the
EXPERT-tier inference shim, and the coding agent's claude_code backend.  An
agentic run starts tool processes of its own (shells, git, test runners).
subprocess.run(timeout=N) kills only the direct child when N passes, then
waits, with no timeout, for the output pipes to close; a surviving tool
process holds them open, so the caller waits as long as that process lives
(D35/D36, core/subprocess_safe.py).  core.subprocess_safe.run_bounded kills
the whole tree and returns.

These run real processes: the "claude" here is a Python child that starts a
grandchild inheriting its stdout and then sleeps.  Nothing reaches Anthropic:
launch_argv is replaced, so no claude binary is ever started.

    python -m pytest tests/unit/test_claude_launcher_is_bounded.py -q
"""
import os
import sys
import tempfile
import threading
import time
from unittest.mock import patch

# The owner's switch is a marker in Claude's config dir; point it at an empty
# dir before the backend is imported, so this file decides the switch (the
# owner's desktop has the marker, which would refuse every run here).
os.environ['CLAUDE_CONFIG_DIR'] = tempfile.mkdtemp(prefix='claude_bounded_')

import pytest  # noqa: E402

import core.llm_outbound_logger as outbound  # noqa: E402
import integrations.coding_agent.claude_code_backend as cc  # noqa: E402
from core.subprocess_safe import BoundedResult  # noqa: E402

# A direct child that starts a grandchild inheriting its stdout, then sleeps.
# Killing the direct child alone leaves the pipe's write end open.
_HOLDS_PIPE_AFTER_DEATH = (
    "import subprocess,sys,time;"
    "subprocess.Popen([sys.executable,'-c','import time;time.sleep(30)']);"
    "time.sleep(30)"
)


@pytest.fixture(autouse=True)
def _copilot_on(monkeypatch, tmp_path):
    """Switch on, a resolvable binary, the outbound record in a temp file."""
    monkeypatch.delenv('HARTOS_COPILOT_ENABLED', raising=False)
    cc.set_copilot_enabled(True)
    monkeypatch.setattr(cc, '_resolve_claude_bin', lambda: '/usr/bin/claude')
    log_path = str(tmp_path / 'llm_outbound.jsonl')
    outbound._close_handle()
    monkeypatch.setattr(outbound, '_get_log_path', lambda: log_path)
    yield
    outbound._close_handle()


def test_a_timed_out_run_returns_although_a_grandchild_holds_the_pipe():
    box = {}

    def _call():
        t0 = time.monotonic()
        try:
            box['result'] = cc.invoke_claude('fix it', mode='agentic',
                                             timeout_s=2)
        except Exception as exc:  # pragma: no cover - diagnostic
            box['error'] = exc
        box['secs'] = time.monotonic() - t0

    with patch('integrations.coding_agent.installer.launch_argv',
               return_value=[sys.executable, '-c', _HOLDS_PIPE_AFTER_DEATH]):
        worker = threading.Thread(target=_call, daemon=True)
        worker.start()
        # 2 s budget + 2 s reap after the kill + tree kill + slack.
        worker.join(20.0)

    assert not worker.is_alive(), (
        'invoke_claude did not return within 20 s of a 2 s timeout: it is '
        'waiting on a pipe a surviving grandchild holds open')
    assert 'error' not in box, f"invoke_claude raised: {box.get('error')!r}"
    result = box['result']
    assert result['ok'] is False
    assert result['category'] == 'timeout'
    assert cc.classify_failure(result) == 'timeout'


def test_the_run_goes_through_the_bounded_runner_with_its_argv_cwd_and_budget():
    with patch.object(cc, 'run_bounded',
                      return_value=BoundedResult(0, 'done', '')) as rb:
        r = cc.invoke_claude('fix the bug', mode='agentic', cwd='/repo',
                             timeout_s=42)
    assert r == {'ok': True, 'returncode': 0, 'stdout': 'done', 'stderr': ''}
    argv = rb.call_args[0][0]
    assert argv[0] == '/usr/bin/claude' and argv[1] == '-p'
    assert rb.call_args[1]['cwd'] == '/repo'
    assert rb.call_args[1]['timeout'] == 42


def test_a_run_the_runner_timed_out_is_a_timeout_not_an_exit_code():
    with patch.object(cc, 'run_bounded',
                      return_value=BoundedResult(-1, '', '', timed_out=True)):
        r = cc.invoke_claude('q', mode='inference', timeout_s=5)
    assert r['ok'] is False and r['category'] == 'timeout'
    assert 'returncode' not in r      # a killed run never completed
